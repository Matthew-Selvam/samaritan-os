/**
 * format.ts — presentation helpers for the Signal-OS dashboard.
 *
 * Pure functions only: no React, no network, no globals mutated. Every helper
 * is total — bad input yields a readable placeholder, never `NaN`, `undefined`
 * or a thrown error.
 */

import type { AgentStatus, InvestigationStatus } from "./types";

/** Placeholder rendered when a value is missing or unparseable. */
const EM_DASH = "—";

// ── Numbers & sizes ──────────────────────────────────────────────────────────

/**
 * Human-readable byte size using binary units.
 * @param bytes - Byte count.
 * @param decimals - Fraction digits (default 1).
 * @returns e.g. `"1.4 MB"`, `"512 B"`, or `"—"` for non-finite input.
 */
export function formatBytes(bytes: number | null | undefined, decimals = 1): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return EM_DASH;
  const negative = bytes < 0;
  const abs = Math.abs(bytes);
  if (abs < 1024) return `${negative ? "-" : ""}${Math.round(abs)} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let value = abs / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${negative ? "-" : ""}${value.toFixed(decimals)} ${units[unitIndex]}`;
}

/**
 * Duration in milliseconds rendered at the largest sensible unit.
 * @param ms - Milliseconds; seconds are accepted and normalised.
 * @returns e.g. `"340ms"`, `"2.4s"`, `"1m 12s"`, `"3h 04m"`.
 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return EM_DASH;
  const abs = Math.abs(ms);
  if (abs < 1000) return `${Math.round(abs)}ms`;

  const totalSeconds = abs / 1000;
  if (totalSeconds < 60) return `${trimZero(totalSeconds.toFixed(1))}s`;

  const totalMinutes = Math.floor(totalSeconds / 60);
  const seconds = Math.floor(totalSeconds % 60);
  if (totalMinutes < 60) return `${totalMinutes}m ${String(seconds).padStart(2, "0")}s`;

  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours < 24) return `${hours}h ${String(minutes).padStart(2, "0")}m`;

  const days = Math.floor(hours / 24);
  return `${days}d ${String(hours % 24).padStart(2, "0")}h`;
}

/** Seconds, as reported by agent/connector latency fields. */
export function formatSeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return EM_DASH;
  return formatDuration(seconds * 1000);
}

function trimZero(value: string): string {
  return value.endsWith(".0") ? value.slice(0, -2) : value;
}

// ── Time ────────────────────────────────────────────────────────────────────

/**
 * Relative time against now, e.g. `"4m ago"`, `"in 2h"`.
 * @param value - ISO 8601 string, epoch millis, or Date.
 * @param now - Reference instant for deterministic tests.
 * @returns A compact relative label, or `"—"` when unparseable.
 */
export function formatRelativeTime(
  value: string | number | Date | null | undefined,
  now: number = Date.now(),
): string {
  const date = toDate(value);
  if (!date) return EM_DASH;
  const deltaMs = now - date.getTime();
  const future = deltaMs < 0;
  const abs = Math.abs(deltaMs);

  if (abs < 5_000) return "just now";

  const units: [limit: number, ms: number, suffix: string][] = [
    [60_000, 1_000, "s"],
    [3_600_000, 60_000, "m"],
    [86_400_000, 3_600_000, "h"],
    [2_592_000_000, 86_400_000, "d"],
    [31_536_000_000, 2_592_000_000, "w"],
  ];

  if (abs < 60_000) {
    const seconds = Math.floor(abs / 1_000);
    return future ? `in ${seconds}s` : `${seconds}s ago`;
  }
  for (let i = 1; i < units.length; i++) {
    const [limit, ms, suffix] = units[i];
    if (abs < limit) {
      const value = Math.floor(abs / ms);
      return future ? `in ${value}${suffix}` : `${value}${suffix} ago`;
    }
  }
  const years = Math.floor(abs / 31_536_000_000);
  return future ? `in ${years}y` : `${years}y ago`;
}

/**
 * Absolute UTC timestamp, matching the existing top-bar clock format.
 * @param value - ISO 8601 string, epoch millis, or Date.
 * @returns `"YYYY-MM-DD HH:MM:SS UTC"`, or `"—"`.
 */
export function formatTimestamp(value: string | number | Date | null | undefined): string {
  const date = toDate(value);
  if (!date) return EM_DASH;
  const iso = date.toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 19)} UTC`;
}

/** ISO 8601 UTC string, for `datetime` attributes and copy-to-clipboard. */
export function toIsoUtc(value: string | number | Date | null | undefined): string {
  const date = toDate(value);
  return date ? date.toISOString() : "";
}

/** Coerce mixed date input to a `Date`, or `null` when unusable. */
function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  const trimmed = value.trim();
  // Bare digits are epoch millis, not a year.
  if (/^\d+$/.test(trimmed)) return toDate(Number(trimmed));
  const date = new Date(trimmed);
  return Number.isNaN(date.getTime()) ? null : date;
}

// ── Strings ─────────────────────────────────────────────────────────────────

/**
 * Truncate the middle of a string, keeping both ends recognisable.
 * @param value - Text to shorten, e.g. an email or a long URL.
 * @param maxLength - Maximum output length, ellipsis included (default 32).
 * @returns `"jo***ple@example.com"`-style shortened text.
 */
export function truncateMiddle(value: string | null | undefined, maxLength = 32): string {
  if (!value) return EM_DASH;
  if (value.length <= maxLength) return value;
  if (maxLength <= 3) return value.slice(0, maxLength);
  const keep = maxLength - 3; // 1 char for the ellipsis, 2 for the two dots
  const head = Math.ceil(keep / 2);
  const tail = Math.floor(keep / 2);
  return `${value.slice(0, head)}…${tail === 0 ? "" : value.slice(value.length - tail)}`;
}

/** Truncate at the end with an ellipsis (for snippets and labels). */
export function truncate(value: string | null | undefined, maxLength = 80): string {
  if (!value) return EM_DASH;
  if (value.length <= maxLength) return value;
  return `${value.slice(0, Math.max(1, maxLength - 1))}…`;
}

/**
 * Confidence (0..1) as a percentage string.
 * @param confidence - 0..1 value; values > 1 are assumed to be already scaled.
 * @returns e.g. `"87%"`.
 */
export function confidencePct(confidence: number | null | undefined): string {
  if (confidence === null || confidence === undefined || !Number.isFinite(confidence)) {
    return EM_DASH;
  }
  const scaled = confidence > 1 ? confidence : confidence * 100;
  const clamped = Math.max(0, Math.min(100, scaled));
  return `${Math.round(clamped)}%`;
}

/** Confidence as a clamped 0..100 number (for width styles). */
export function confidenceNumber(confidence: number | null | undefined): number {
  if (confidence === null || confidence === undefined || !Number.isFinite(confidence)) return 0;
  const scaled = confidence > 1 ? confidence : confidence * 100;
  return Math.max(0, Math.min(100, scaled));
}

// ── Status tone mapping ─────────────────────────────────────────────────────

/** Badge tones matching the `.tone-*` classes in `app/globals.css`. */
export type Tone = "ok" | "warn" | "err" | "info" | "idle";

const TONE_COLOR: Record<Tone, string> = {
  ok: "#00ff88",
  warn: "#ffb020",
  err: "#ff4455",
  info: "#00d4ff",
  idle: "#5a7a9a",
};

const TONE_CLASS: Record<Tone, string> = {
  ok: "tone-ok",
  warn: "tone-warn",
  err: "tone-err",
  info: "tone-info",
  idle: "tone-idle",
};

const INVESTIGATION_TONE: Record<InvestigationStatus, Tone> = {
  idle: "idle",
  routing: "warn",
  running: "info",
  done: "ok",
  error: "err",
};

const AGENT_TONE: Record<AgentStatus, Tone> = {
  done: "ok",
  error: "err",
  partial: "warn",
  skipped: "idle",
};

/**
 * Map an investigation status to a badge tone.
 * @param status - e.g. `"routing"`.
 * @returns The tone used by `.tone-*` and by `toneColor`.
 */
export function statusTone(status: InvestigationStatus | string | null | undefined): Tone {
  if (!status) return "idle";
  return INVESTIGATION_TONE[status as InvestigationStatus] ?? "idle";
}

/**
 * Map an agent status to a badge tone.
 * @param status - e.g. `"partial"`.
 * @returns The tone used by `.tone-*` and by `toneColor`.
 */
export function agentTone(status: AgentStatus | string | null | undefined): Tone {
  if (!status) return "idle";
  return AGENT_TONE[status as AgentStatus] ?? "idle";
}

/**
 * Tone for either kind of status in one call.
 * @param status - An investigation or agent status string.
 * @returns The resolved tone; unknown values fall back to `"idle"`.
 */
export function toneForStatus(status: string | null | undefined): Tone {
  if (!status) return "idle";
  if (status in INVESTIGATION_TONE) return INVESTIGATION_TONE[status as InvestigationStatus];
  if (status in AGENT_TONE) return AGENT_TONE[status as AgentStatus];
  return "idle";
}

/** CSS class for a tone, e.g. `"tone-ok"`. */
export function toneClass(tone: Tone): string {
  return TONE_CLASS[tone] ?? TONE_CLASS.idle;
}

/** Hex colour for a tone, matching the `.tone-*` palette. */
export function toneColor(tone: Tone): string {
  return TONE_COLOR[tone] ?? TONE_COLOR.idle;
}

/** Uppercase label for a status, e.g. `"RUNNING"`. */
export function statusLabel(status: string | null | undefined): string {
  return (status ?? "unknown").replace(/[_-]+/g, " ").toUpperCase();
}

// ── Search snippets ─────────────────────────────────────────────────────────

export interface HighlightSegment {
  text: string;
  match: boolean;
}

/**
 * Split a snippet into alternating plain / matched segments.
 *
 * Purely lexical — the caller renders `<mark>` (or `.highlight`) around the
 * matched segments, so this helper never emits HTML and is XSS-safe.
 *
 * @param snippet - Raw result text.
 * @param terms - Terms to highlight; case-insensitive, matched as substrings.
 * @returns Ordered segments; a single segment when nothing matched.
 */
export function highlightTerms(
  snippet: string | null | undefined,
  terms: string | readonly string[] | null | undefined,
): HighlightSegment[] {
  const text = snippet ?? "";
  if (!text) return [];

  const needles = (Array.isArray(terms) ? terms : [terms ?? ""])
    .filter((term): term is string => typeof term === "string" && term.trim().length > 0)
    .map((term) => term.trim().toLowerCase())
    .filter((term, index, all) => all.indexOf(term) === index);

  if (needles.length === 0) return [{ text, match: false }];

  // Longest first so "john.doe" wins over "john".
  needles.sort((a, b) => b.length - a.length);

  const escaped = needles.map((needle) => needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pattern = new RegExp(`(${escaped.join("|")})`, "gi");

  const segments: HighlightSegment[] = [];
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > cursor) segments.push({ text: text.slice(cursor, index), match: false });
    segments.push({ text: match[0], match: true });
    cursor = index + match[0].length;
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor), match: false });
  return segments;
}

/** Case-insensitive substring test used by entity filters. */
export function matchesQuery(haystack: string | null | undefined, query: string): boolean {
  if (!query.trim()) return true;
  return (haystack ?? "").toLowerCase().includes(query.trim().toLowerCase());
}

export { EM_DASH };