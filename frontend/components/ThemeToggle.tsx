"use client";

/**
 * ThemeToggle.tsx — the dark/light switch.
 *
 * Matches the TopBar aesthetic: mono micro-label, 10px uppercase tracking,
 * a 6px status dot with the existing `pulse-green` animation, and the
 * `focus-ring` utility for keyboard users. The glyph comes from lucide-react
 * (both `Sun` and `Moon` verified present in the installed v1.17.0).
 */

import { Moon, Sun } from "lucide-react";
import { useCallback, useId } from "react";

import {
  oppositeTheme,
  useTheme,
  type Theme,
} from "@/lib/theme";

/** Props for {@link ThemeToggle}. */
export interface ThemeToggleProps {
  /** Theme applied before mount; defaults to dark. */
  readonly defaultTheme?: Theme;
  /** Render only the glyph, no text — for very narrow headers. */
  readonly compact?: boolean;
  /** Hide the "THEME" eyebrow and show only the current theme name. */
  readonly hideLabel?: boolean;
  /** Notified after the theme changes, with the new value. */
  readonly onChange?: (theme: Theme) => void;
  /** Extra class on the button (layout only). */
  readonly className?: string;
}

export function ThemeToggle({
  defaultTheme = "dark",
  compact = false,
  hideLabel = false,
  onChange,
  className = "",
}: ThemeToggleProps) {
  const { theme, set } = useTheme(defaultTheme);
  const labelId = useId();

  const next = oppositeTheme(theme);
  const handle = useCallback(() => {
    set(next);
    onChange?.(next);
  }, [onChange, next, set]);

  const isDark = theme === "dark";
  const Glyph = isDark ? Moon : Sun;

  return (
    <button
      type="button"
      onClick={handle}
      className={`focus-ring flex items-center gap-1.5 px-2 py-1 rounded transition-colors ${className}`.trim()}
      style={{
        background: isDark ? "rgba(0,212,255,0.07)" : "rgba(255,176,32,0.09)",
        border: `1px solid ${isDark ? "rgba(0,212,255,0.24)" : "rgba(255,176,32,0.28)"}`,
        color: isDark ? "var(--cyan)" : "var(--amber)",
        cursor: "pointer",
      }}
      aria-labelledby={labelId}
      aria-label={`Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
    >
      {/* State dot — mirrors the TopBar connection indicator. */}
      <span
        aria-hidden="true"
        className="rounded-full inline-block"
        style={{
          width: 5,
          height: 5,
          background: "currentColor",
          boxShadow: `0 0 6px currentColor`,
          animation: isDark ? "pulse-green 2s ease-in-out infinite" : undefined,
        }}
      />
      <Glyph size={12} strokeWidth={2} aria-hidden="true" />
      {!compact && (
        <span id={labelId} className="label" style={{ fontSize: 9, color: "currentColor" }}>
          {hideLabel ? (isDark ? "DARK" : "LIGHT") : `THEME · ${isDark ? "DARK" : "LIGHT"}`}
        </span>
      )}
    </button>
  );
}

export default ThemeToggle;