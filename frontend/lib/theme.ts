/**
 * theme.ts — theme manager for Signal-OS.
 *
 * Two themes: `dark` (the existing console aesthetic, the default) and `light`.
 *
 * Design constraints:
 *   - **Never fights `globals.css`.** This module sets exactly one attribute
 *     (`data-theme` on `<html>`) plus a small, documented set of CSS custom
 *     properties. It does not touch `:root`, does not inject a stylesheet, and
 *     does not reorder existing tokens — `globals.css` remains authoritative.
 *   - Light mode is expressed purely as an override block inside this file's
 *     consumer, keyed off `[data-theme="light"]`.
 *   - First visit respects `prefers-color-scheme`; after the user picks, the
 *     choice is persisted and the OS preference is no longer consulted.
 */

"use client";

import { useCallback, useEffect, useState } from "react";

/** The themes this app ships. */
export type Theme = "dark" | "light";

/** Both themes, in toggle order. */
export const THEMES: readonly Theme[] = ["dark", "light"] as const;

/** localStorage key holding the user's explicit choice. */
export const THEME_STORAGE_KEY = "signal-os:theme";

/** Attribute written on `<html>`; `globals.css` may key off it. */
export const THEME_ATTRIBUTE = "data-theme";

/**
 * CSS custom properties overridden per theme. Dark values mirror `:root` in
 * `globals.css` exactly (so the dark theme is a no-op) and light values are the
 * day-mode equivalents. Anything not listed keeps its `globals.css` value in
 * both themes — that is what "don't fight globals.css" means in practice.
 */
export const THEME_TOKENS: Readonly<
  Record<Theme, Readonly<Record<string, string>>>
> = {
  dark: {
    "--bg": "#080c10",
    "--bg-panel": "#0d1117",
    "--bg-card": "#111820",
    "--border": "#1e2d3d",
    "--border-hi": "#2a4060",
    "--text": "#c8d8e8",
    "--text-muted": "#5a7a9a",
    // Keep the green readable on a light canvas.
    "--green-dim": "#00c060",
  },
  light: {
    "--bg": "#f4f7fa",
    "--bg-panel": "#ffffff",
    "--bg-card": "#ffffff",
    "--border": "#d3dfea",
    "--border-hi": "#a9c2d6",
    "--text": "#0d2233",
    "--text-muted": "#4a6b85",
    // Darker green so the accent passes contrast on white.
    "--green": "#00875a",
    "--green-dim": "#00694a",
  },
};

/** Is this a valid theme id? */
export function isTheme(value: unknown): value is Theme {
  return value === "dark" || value === "light";
}

/** The opposite theme — what a toggle button switches to. */
export function oppositeTheme(theme: Theme): Theme {
  return theme === "dark" ? "light" : "dark";
}

/**
 * The OS preference, defaulting to `dark` (Signal-OS is a dark-first console).
 * Safe to call during SSR, where it always reports `dark`.
 */
export function systemTheme(): Theme {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return "dark";
  }
  try {
    return window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark";
  } catch {
    return "dark";
  }
}

/**
 * Resolve the theme to use: persisted choice wins, else OS preference.
 * Pure apart from reading storage, which it guards.
 */
export function resolveTheme(
  storageKey: string = THEME_STORAGE_KEY,
): Theme {
  if (typeof window === "undefined") return "dark";
  try {
    const stored = window.localStorage.getItem(storageKey);
    if (isTheme(stored)) return stored;
  } catch {
    // Storage blocked — fall through to the OS preference.
  }
  return systemTheme();
}

/** Apply a theme to the document element: attribute plus token overrides. */
export function applyTheme(
  theme: Theme,
  root: HTMLElement | null = typeof document !== "undefined"
    ? document.documentElement
    : null,
): void {
  if (!root) return;
  root.setAttribute(THEME_ATTRIBUTE, theme);
  // Keep native form controls / scrollbars in step with the theme.
  root.style.colorScheme = theme;

  const tokens = THEME_TOKENS[theme];
  for (const [name, value] of Object.entries(tokens)) {
    root.style.setProperty(name, value);
  }

  // The dark theme must restore the green that globals.css defines, because a
  // previous light theme overwrote it inline.
  if (theme === "dark") {
    root.style.removeProperty("--green");
  }
}

/**
 * React binding for the theme.
 *
 * Hydration-safe: it starts at `defaultTheme` (dark), then syncs to the stored
 * or system theme in an effect, so server and client markup agree.
 *
 * `set()` applies and persists. `toggle()` flips to `oppositeTheme(current)`.
 */
export function useTheme(
  defaultTheme: Theme = "dark",
  storageKey: string = THEME_STORAGE_KEY,
): {
  theme: Theme;
  set: (next: Theme) => void;
  toggle: () => void;
} {
  const [theme, setTheme] = useState<Theme>(defaultTheme);

  useEffect(() => {
    const resolved = resolveTheme(storageKey);
    setTheme(resolved);
    applyTheme(resolved);
  }, [storageKey]);

  const set = useCallback(
    (next: Theme) => {
      setTheme(next);
      applyTheme(next);
      if (typeof window !== "undefined") {
        try {
          window.localStorage.setItem(storageKey, next);
        } catch {
          // Persistence is best-effort; the theme still applies for this session.
        }
      }
    },
    [storageKey],
  );

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next = oppositeTheme(current);
      applyTheme(next);
      if (typeof window !== "undefined") {
        try {
          window.localStorage.setItem(storageKey, next);
        } catch {
          // As above — ignore storage failures.
        }
      }
      return next;
    });
  }, [storageKey]);

  return { theme, set, toggle };
}

/**
 * Subscribe to OS theme changes. Returns an unsubscribe function. Only useful
 * while the user has *not* made an explicit choice.
 */
export function watchSystemTheme(
  onChange: (theme: Theme) => void,
): () => void {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return () => undefined;
  }
  let query: MediaQueryList;
  try {
    query = window.matchMedia("(prefers-color-scheme: light)");
  } catch {
    return () => undefined;
  }
  const handler = (event: MediaQueryListEvent) => onChange(event.matches ? "light" : "dark");
  if (typeof query.addEventListener === "function") {
    query.addEventListener("change", handler);
    return () => query.removeEventListener("change", handler);
  }
  return () => undefined;
}

/** Clear the persisted theme so the next visit follows the OS again. */
export function clearStoredTheme(storageKey: string = THEME_STORAGE_KEY): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(storageKey);
  } catch {
    // Nothing to do.
  }
}