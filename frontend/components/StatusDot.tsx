"use client";

/**
 * StatusDot.tsx — the single status indicator used across the app.
 *
 * A dot alone is invisible to screen readers and colour-blind users, so this
 * always renders a text label for assistive tech (`aria-label`) even when the
 * visual form is dot-only.
 */

import clsx from "clsx";
import { toneClass, toneColor, type Tone } from "@/lib/format";

export interface StatusDotProps {
  tone: Tone;
  /** Visible text. Defaults to the uppercase tone name. */
  label?: string;
  /** Hides the text, keeping it for assistive tech. */
  dotOnly?: boolean;
  /** Adds the glow animation for live/transient states. */
  pulse?: boolean;
  className?: string;
  title?: string;
}

/** Status dot with an accessible label. */
export function StatusDot({
  tone,
  label,
  dotOnly = false,
  pulse = false,
  className,
  title,
}: StatusDotProps) {
  const color = toneColor(tone);
  const text = label ?? tone.toUpperCase();
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 whitespace-nowrap",
        dotOnly ? "" : toneClass(tone),
        pulse && "tone-pulse",
        className,
      )}
      title={title ?? text}
    >
      <span
        aria-hidden="true"
        className="inline-block shrink-0 rounded-full"
        style={{
          width: dotOnly ? 6 : 5,
          height: dotOnly ? 6 : 5,
          background: color,
          boxShadow: `0 0 6px ${color}`,
          animation: pulse ? "pulse-green 2s ease-in-out infinite" : undefined,
        }}
      />
      <span className={dotOnly ? "sr-only" : undefined}>{text}</span>
    </span>
  );
}
