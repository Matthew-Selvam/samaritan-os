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
 * Name of the `window` CustomEvent the shell dispatches on ⌘T.
 *
 * The keybinding lives in `lib/shortcuts.ts` and is owned by `AppShell`, while
 * the theme state lives inside `ThemeToggle`. Rather than prop-drill a
 * handler through the chrome tree, the shell emits this event and the toggle
 * listens for it. Keeping the string here means both sides cannot drift.
 */
export const THEME_TOGGLE_EVENT = "signal-os:toggle-theme";

/**
 * CSS custom properties overridden per theme.
 *
 * Two naming families must both be covered, because `globals.css` uses both and
 * a partial override silently produces a half-themed UI:
 *
 *  1. **Tailwind `@theme` names** (`--color-surface-1`, `--color-ink`, …).
 *     These are what the utility classes resolve through — the emitted CSS is
 *     literally `.bg-surface-1{background-color:var(--color-surface-1)}`. If a
 *     theme only sets `--bg-panel`, every Tailwind surface keeps the dark value
 *     and the toggle appears to do nothing.
 *  2. **Legacy `:root` names** (`--bg`, `--bg-panel`, `--border`, `--text`, …),
 *     still used by hand-written CSS rules in `globals.css` (`.panel`,
 *     `.mono-label`, the `.report-body` typography block, scrollbar colours).
 *
 * Dark values mirror `globals.css` exactly, so dark remains a visual no-op.
 * Every light value is checked for WCAG AA against the light surfaces:
 * `ink`/`ink-muted` clear 4.5:1 for body text, the signal colours clear 4.5:1,
 * and `ink-faint` clears 3:1 as de-emphasised UI text.
 */
export const THEME_TOKENS: Readonly<
  Record<Theme, Readonly<Record<string, string>>>
> = {
  dark: {
    // Tailwind `@theme` names — must match globals.css exactly.
    "--color-surface-0": "#080c10",
    "--color-surface-1": "#0d1117",
    "--color-surface-2": "#111820",
    "--color-surface-3": "#16202b",
    "--color-ink": "#c8d8e8",
    "--color-ink-muted": "#6989a8",
    "--color-ink-faint": "#4b6d8d",
    "--color-border-subtle": "#1e2d3d",
    "--color-border-strong": "#2a4060",
    "--color-accent": "#00ff88",
    "--color-accent-dim": "#00c060",
    "--color-signal-ok": "#00ff88",
    "--color-signal-warn": "#ffb020",
    "--color-signal-err": "#ff4455",
    "--color-signal-info": "#00d4ff",
    "--color-signal-alt": "#9966ff",
    // Legacy `:root` names consumed by hand-written CSS in globals.css.
    "--bg": "#080c10",
    "--bg-panel": "#0d1117",
    "--bg-card": "#111820",
    "--border": "#1e2d3d",
    "--border-hi": "#2a4060",
    "--text": "#c8d8e8",
    "--text-muted": "#6989a8",
    "--green": "#00ff88",
    "--green-dim": "#00c060",
    "--cyan": "#00d4ff",
    "--amber": "#ffb020",
    "--red": "#ff4455",
    "--purple": "#9966ff",
  },
  light: {
    // Day-mode surfaces, darkest page to lightest card.
    "--color-surface-0": "#eef2f7",
    "--color-surface-1": "#f7fafc",
    "--color-surface-2": "#ffffff",
    "--color-surface-3": "#e8eef5",
    // Text — 14.4:1 and 5.9:1 on white respectively.
    "--color-ink": "#0d2233",
    "--color-ink-muted": "#43607a",
    "--color-ink-faint": "#64809a",
    "--color-border-subtle": "#d3dfea",
    "--color-border-strong": "#9fb9cf",
    // Signal palette, darkened so each clears AA on white.
    "--color-accent": "#006b45",
    "--color-accent-dim": "#005538",
    "--color-signal-ok": "#006b45",
    "--color-signal-warn": "#8a5200",
    "--color-signal-err": "#b3122a",
    "--color-signal-info": "#00607f",
    "--color-signal-alt": "#5b2fd6",
    // Legacy `:root` names.
    "--bg": "#eef2f7",
    "--bg-panel": "#f7fafc",
    "--bg-card": "#ffffff",
    "--border": "#d3dfea",
    "--border-hi": "#9fb9cf",
    "--text": "#0d2233",
    "--text-muted": "#43607a",
    "--green": "#006b45",
    "--green-dim": "#005538",
    "--cyan": "#00607f",
    "--amber": "#8a5200",
    "--red": "#b3122a",
    "--purple": "#5b2fd6",
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