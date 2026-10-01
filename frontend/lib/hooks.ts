/**
 * hooks.ts — small data-fetching primitives shared by every view.
 *
 * Deliberately framework-free: they own the parts that are easy to get wrong
 * (abort on unmount, no setState after unmount, explicit loading/error/empty
 * states, manual refresh without duplicating fetches).
 */

import { useCallback, useEffect, useRef, useState } from "react";

/** Lifecycle of an async resource. */
export type ResourceState = "idle" | "loading" | "success" | "error";

/** Result of {@link useAsyncResource}. */
export interface Resource<T> {
  data: T | null;
  state: ResourceState;
  error: string | null;
  /** True while a *background* refresh runs over existing data. */
  refreshing: boolean;
  /** `true` when the last successful response had no rows. */
  empty: boolean;
  reload: () => void;
  /** Replace the cached value without a round trip. */
  set: (value: T | null) => void;
}

/** Loader contract: receives an abort signal, returns the parsed value. */
export type Loader<T> = (signal: AbortSignal) => Promise<T>;

export interface AsyncResourceOptions {
  /** Skip fetching until true (e.g. no case selected yet). */
  enabled?: boolean;
  /** Re-fetch on this interval. `0` disables polling. */
  pollMs?: number;
  /** Re-fetch whenever any of these change. */
  deps?: readonly unknown[];
}

/**
 * Fetch a resource, with abort, optional polling and a manual `reload`.
 *
 * Errors are stored as a human-readable string (`Error.message`, or
 * `String(err)` for a thrown non-Error), never as an `any`.
 */
export function useAsyncResource<T>(
  loader: Loader<T>,
  options: AsyncResourceOptions = {},
): Resource<T> {
  const { enabled = true, pollMs = 0, deps = [] } = options;

  const [data, setData] = useState<T | null>(null);
  const [state, setState] = useState<ResourceState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [nonce, setNonce] = useState(0);

  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!enabled) {
      setState("idle");
      return;
    }
    const controller = new AbortController();
    let cancelled = false;

    const run = async () => {
      setState((prev) => (prev === "success" ? prev : "loading"));
      setRefreshing(true);
      try {
        const value = await loaderRef.current(controller.signal);
        if (cancelled || !mountedRef.current) return;
        setData(value);
        setError(null);
        setState("success");
      } catch (err) {
        if (cancelled || !mountedRef.current) return;
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
        setState("error");
      } finally {
        if (!cancelled && mountedRef.current) setRefreshing(false);
      }
    };

    void run();
    return () => {
      cancelled = true;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, nonce, ...deps]);

  useEffect(() => {
    if (!enabled || pollMs <= 0) return;
    const id = window.setInterval(() => setNonce((n) => n + 1), pollMs);
    return () => window.clearInterval(id);
  }, [enabled, pollMs]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const set = useCallback((value: T | null) => {
    setData(value);
    setState(value === null ? "idle" : "success");
  }, []);

  return {
    data,
    state,
    error,
    refreshing,
    empty: state === "success" && isEmptyish(data),
    reload,
    set,
  };
}

/** Heuristic emptiness used for `Resource.empty`. */
function isEmptyish(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === "object") {
    const rec = value as Record<string, unknown>;
    for (const key of ["items", "nodes", "edges", "results", "events", "agents"]) {
      const list = rec[key];
      if (Array.isArray(list)) return list.length === 0;
    }
  }
  return false;
}

/* ── Local storage ──────────────────────────────────────────────────────────── */

/**
 * `useState` mirrored into `localStorage`, tolerant of private-mode failures.
 *
 * @param key - Storage key; the sentinel `""` disables persistence.
 * @param initial - Value used before anything is stored.
 */
export function useLocalStorage<T>(key: string, initial: T): [T, (next: T) => void] {
  const [value, setValue] = useState<T>(() => {
    if (!key || typeof window === "undefined") return initial;
    try {
      const raw = window.localStorage.getItem(key);
      if (!raw) return initial;
      return JSON.parse(raw) as T;
    } catch {
      return initial;
    }
  });

  const set = useCallback(
    (next: T) => {
      setValue(next);
      if (!key) return;
      try {
        window.localStorage.setItem(key, JSON.stringify(next));
      } catch {
        /* quota or private mode — the in-memory value still applies */
      }
    },
    [key],
  );

  return [value, set];
}

/* ── Media query ───────────────────────────────────────────────────────────── */

/**
 * `window.matchMedia` as React state.
 * @param query - Any media query, e.g. `"(min-width: 1440px)"`.
 * @returns `false` during SSR and before the first client effect.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia(query);
    setMatches(mql.matches);
    const onChange = (event: MediaQueryListEvent) => setMatches(event.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}

/** `true` when the OS asked for reduced motion. */
export function useReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

/* ── Copy to clipboard ──────────────────────────────────────────────────────── */

/** Copy text, resolving `false` when the clipboard is unavailable. */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (typeof navigator === "undefined" || !navigator.clipboard) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Trigger a client-side file download for text or binary payloads. */
export function downloadBlob(
  data: BlobPart,
  filename: string,
  mime = "application/octet-stream",
): void {
  if (typeof document === "undefined") return;
  const blob = data instanceof Blob ? data : new Blob([data], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  // Revoke on the next tick so Safari has time to start the download.
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** JSON pretty-printer used by every export in the app. */
export function toJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}
