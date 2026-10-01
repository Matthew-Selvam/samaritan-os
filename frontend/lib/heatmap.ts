/**
 * heatmap.ts — a small, pure visualization toolkit for the Signal-OS dashboard.
 *
 * Every function returns plain data ready to be rendered as SVG. There is no
 * chart library, no DOM access and no React import in this module, so the same
 * functions can be used from a server component, a client component or a test.
 *
 * Included:
 *   (a) day-of-week × hour-of-day activity heatmap
 *   (b) confidence histogram
 *   (c) sparkline SVG path generator
 *   (d) stacked-bar segment calculator
 *   (e) sequential / diverging colour scales
 *
 * Everything here is timezone-local by default: an analyst reads "Monday 14:00"
 * in their own working hours, not in UTC. Pass `utc: true` to opt out.
 */

/** Days of the week, Sunday-first, matching `Date.prototype.getDay()`. */
export const DAY_LABELS: readonly string[] = [
  "Sun",
  "Mon",
  "Tue",
  "Wed",
  "Thu",
  "Fri",
  "Sat",
] as const;

/** Single-letter day labels for dense heatmap rows. */
export const DAY_LABELS_SHORT: readonly string[] = [
  "S",
  "M",
  "T",
  "W",
  "T",
  "F",
  "S",
] as const;

/** One cell of the activity heatmap. */
export interface HeatmapCell {
  /** 0 = Sunday … 6 = Saturday. */
  readonly day: number;
  /** 0–23. */
  readonly hour: number;
  /** Number of signals/events in this bucket. */
  readonly count: number;
  /** `count / maxCount`, clamped to 0..1 — drives the colour scale. */
  readonly intensity: number;
  /** Ready-to-use CSS colour for `intensity` via the default sequential scale. */
  readonly color: string;
}

/** The full 7×24 grid, always complete (empty buckets are `count: 0`). */
export interface SignalActivityHeatmap {
  /** 168 cells, indexed `day * 24 + hour`. */
  readonly cells: readonly HeatmapCell[];
  readonly maxCount: number;
  readonly total: number;
  readonly days: readonly string[];
  readonly hours: readonly number[];
  /** The busiest day/hour pair, or `null` when there is no activity. */
  readonly peak: { readonly day: number; readonly hour: number; readonly count: number } | null;
}

/** Any value a timestamp may arrive as. */
export type TimestampLike = string | number | Date;

/** Options for {@link buildActivityHeatmap}. */
export interface ActivityHeatmapOptions {
  /** Interpret timestamps as UTC rather than local time. */
  readonly utc?: boolean;
  /** Colour ramp; defaults to the Signal-OS green sequential scale. */
  readonly scale?: (t: number) => string;
}

/**
 * Parse a timestamp into a Date, or `null` when unusable.
 * Pure: no `Date.now()` fallback, so callers stay deterministic.
 */
export function toDate(value: TimestampLike | null | undefined): Date | null {
  if (value === null || value === undefined) return null;
  const date =
    value instanceof Date
      ? value
      : typeof value === "number"
        ? new Date(value)
        : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Day index (0=Sun) for a date, honouring the `utc` flag. */
function dayOf(date: Date, utc: boolean): number {
  return utc ? date.getUTCDay() : date.getDay();
}

/** Hour (0–23) for a date, honouring the `utc` flag. */
function hourOf(date: Date, utc: boolean): number {
  return utc ? date.getUTCHours() : date.getHours();
}

/**
 * Bucket signal timestamps into a day-of-week × hour-of-day grid.
 *
 * Always returns all 168 cells so the SVG geometry never depends on the data.
 * Invalid timestamps are skipped silently — a corrupt `ts` must not blank the
 * dashboard.
 */
export function buildActivityHeatmap(
  timestamps: readonly (TimestampLike | null | undefined)[],
  options: ActivityHeatmapOptions = {},
): SignalActivityHeatmap {
  const utc = options.utc === true;
  const scale = options.scale ?? sequentialGreen;

  const counts: number[] = new Array(7 * 24).fill(0);
  for (const raw of timestamps) {
    const date = toDate(raw);
    if (!date) continue;
    counts[dayOf(date, utc) * 24 + hourOf(date, utc)]++;
  }

  const maxCount = counts.reduce((max, n) => (n > max ? n : max), 0);
  const cells: HeatmapCell[] = counts.map((count, index) => {
    const day = Math.floor(index / 24);
    const hour = index % 24;
    const intensity = maxCount === 0 ? 0 : count / maxCount;
    return { day, hour, count, intensity, color: scale(intensity) };
  });

  let total = 0;
  let peak: { day: number; hour: number; count: number } | null = null;
  counts.forEach((count, index) => {
    total += count;
    if (count > 0 && (peak === null || count > peak.count)) {
      peak = { day: Math.floor(index / 24), hour: index % 24, count };
    }
  });

  return {
    cells,
    maxCount,
    total,
    days: DAY_LABELS,
    hours: Array.from({ length: 24 }, (_, h) => h),
    peak,
  };
}

// ── Confidence histogram ────────────────────────────────────────────────────

/** One histogram bin. */
export interface HistogramBin {
  /** Inclusive lower edge. */
  readonly from: number;
  /** Exclusive upper edge (equal to `from + binSize`). */
  readonly to: number;
  readonly count: number;
  /** `count / maxCount`, 0..1. */
  readonly ratio: number;
  readonly color: string;
  /** Human label for the bin, e.g. `"0.7–0.8"`. */
  readonly label: string;
}

/** Result of {@link confidenceHistogram}. */
export interface ConfidenceHistogram {
  readonly bins: readonly HistogramBin[];
  readonly maxCount: number;
  readonly binSize: number;
  readonly total: number;
  /** Plain-language mean confidence, 0..1, or `null` with no data. */
  readonly mean: number | null;
  /** Median confidence, or `null` with no data. */
  readonly median: number | null;
}

/** Options for {@link confidenceHistogram}. */
export interface HistogramOptions {
  /** Number of bins; default 10. */
  readonly bins?: number;
  /** Lower bound; default 0. */
  readonly min?: number;
  /** Upper bound; default 1. */
  readonly max?: number;
  /** Drop values outside [min, max]; default true. */
  readonly clamp?: boolean;
  /** Colour ramp; defaults to the sequential green scale. */
  readonly scale?: (t: number) => string;
}

/**
 * Bucket confidence scores into evenly-spaced bins.
 * Values are clamped to [0,1] by default; non-finite values are ignored.
 */
export function confidenceHistogram(
  values: readonly (number | null | undefined)[],
  options: HistogramOptions = {},
): ConfidenceHistogram {
  const binCount = Math.max(1, Math.floor(options.bins ?? 10));
  const min = options.min ?? 0;
  const max = options.max ?? 1;
  const clamp = options.clamp !== false;
  const scale = options.scale ?? sequentialGreen;
  const span = max - min || 1;
  const binSize = span / binCount;

  const counts: number[] = new Array(binCount).fill(0);
  const kept: number[] = [];
  for (const value of values) {
    if (typeof value !== "number" || !Number.isFinite(value)) continue;
    const clamped = clamp ? Math.min(max, Math.max(min, value)) : value;
    if (clamped < min || clamped > max) continue;
    kept.push(clamped);
    const index = Math.min(binCount - 1, Math.floor((clamped - min) / binSize));
    counts[index]++;
  }

  const maxCount = counts.reduce((m, n) => (n > m ? n : m), 0);
  const bins: HistogramBin[] = counts.map((count, index) => {
    const from = min + index * binSize;
    const to = from + binSize;
    const ratio = maxCount === 0 ? 0 : count / maxCount;
    return {
      from,
      to,
      count,
      ratio,
      color: scale(ratio),
      label: `${formatBinEdge(from, min, max)}–${formatBinEdge(to, min, max)}`,
    };
  });

  const total = kept.length;
  let mean: number | null = null;
  let median: number | null = null;
  if (total > 0) {
    mean = kept.reduce((sum, n) => sum + n, 0) / total;
    const sorted = [...kept].sort((a, b) => a - b);
    const mid = Math.floor(sorted.length / 2);
    median =
      sorted.length % 2 === 0
        ? (((sorted[mid - 1] ?? 0) + (sorted[mid] ?? 0)) / 2)
        : (sorted[mid] ?? 0);
  }

  return { bins, maxCount, binSize, total, mean, median };
}

/** Format a bin edge compactly (0.85 rather than 0.8499999). */
function formatBinEdge(value: number, min: number, max: number): string {
  const decimals = max - min >= 10 ? 0 : 2;
  return value.toFixed(decimals);
}

// ── Sparkline ───────────────────────────────────────────────────────────────

/** Geometry of a generated sparkline. */
export interface SparklinePath {
  /** SVG `d` attribute for the line. */
  readonly d: string;
  /** SVG `d` for the optional soft area fill under the line. */
  readonly area: string;
  readonly min: number;
  readonly max: number;
  readonly last: number;
  /** Count of input points actually plotted. */
  readonly points: number;
}

/** Options for {@link sparklinePath}. */
export interface SparklineOptions {
  /** ViewBox width; default 100. */
  readonly width?: number;
  /** ViewBox height; default 24. */
  readonly height?: number;
  /** Vertical padding in px; default 2. */
  readonly padding?: number;
  /** Include an area-fill path; default true. */
  readonly area?: boolean;
  /**
   * Smooth with Catmull-Rom→bezier curves instead of straight segments.
   * Default true — reads better on a dark canvas.
   */
  readonly smooth?: boolean;
}

/**
 * Generate an SVG path for a sparkline, normalised into
 * `width × height`. Pure and total: empty input yields empty paths.
 */
export function sparklinePath(
  values: readonly number[],
  options: SparklineOptions = {},
): SparklinePath {
  const width = options.width ?? 100;
  const height = options.height ?? 24;
  const padding = options.padding ?? 2;
  const includeArea = options.area !== false;
  const smooth = options.smooth !== false;

  const finite = values.filter((v) => typeof v === "number" && Number.isFinite(v));
  if (finite.length === 0) {
    return { d: "", area: "", min: 0, max: 0, last: 0, points: 0 };
  }

  const min = Math.min(...finite);
  const max = Math.max(...finite);
  const span = max - min || 1;
  const innerW = Math.max(0, width - padding * 2);
  const innerH = Math.max(0, height - padding * 2);
  const stepX = finite.length > 1 ? innerW / (finite.length - 1) : 0;

  const points = finite.map((value, index) => {
    const x = padding + (finite.length > 1 ? index * stepX : innerW / 2);
    const ratio = (value - min) / span;
    const y = padding + innerH - ratio * innerH;
    return { x, y: round(y) };
  });

  let d: string;
  if (smooth && points.length > 2) {
    d = catmullRomPath(points);
  } else {
    d = points
      .map((p, i) => `${i === 0 ? "M" : "L"}${round(p.x)},${p.y}`)
      .join(" ");
  }

  const baseline = round(height - padding);
  const area = includeArea && points.length > 1
    ? `${d} L${round(points[points.length - 1]?.x ?? width)},${baseline} L${round(points[0]?.x ?? padding)},${baseline} Z`
    : "";

  return {
    d,
    area,
    min,
    max,
    last: finite[finite.length - 1] ?? 0,
    points: finite.length,
  };
}

/** Round to 2dp — keeps path strings compact without visible stepping. */
function round(value: number): number {
  return Math.round(value * 100) / 100;
}

/** Catmull-Rom spline converted to cubic bezier segments. */
function catmullRomPath(points: readonly { x: number; y: number }[]): string {
  const first = points[0];
  if (!first) return "";
  let path = `M${round(first.x)},${first.y}`;
  for (let i = 0; i < points.length - 1; i++) {
    const p0 = points[i - 1] ?? points[i] ?? first;
    const p1 = points[i] ?? first;
    const p2 = points[i + 1] ?? first;
    const p3 = points[i + 2] ?? p2;
    const c1x = p1.x + (p2.x - p0.x) / 6;
    const c1y = p1.y + (p2.y - p0.y) / 6;
    const c2x = p2.x - (p3.x - p1.x) / 6;
    const c2y = p2.y - (p3.y - p1.y) / 6;
    path += ` C${round(c1x)},${round(c1y)} ${round(c2x)},${round(c2y)} ${round(p2.x)},${round(p2.y)}`;
  }
  return path;
}

// ── Stacked bars ────────────────────────────────────────────────────────────

/** One segment of a stacked bar. */
export interface StackSegment {
  readonly key: string;
  readonly label: string;
  readonly value: number;
  /** `value / total`. */
  readonly ratio: number;
  /** Pixel offset from the bar's left edge. */
  readonly x: number;
  /** Pixel width of the segment. */
  readonly width: number;
  readonly color: string;
}

/** Result of {@link computeStack}. */
export interface StackLayout {
  readonly segments: readonly StackSegment[];
  /** Sum of all values. */
  readonly total: number;
  /** Plot width actually allocated (bar width minus padding). */
  readonly plotWidth: number;
}

/** A value to be stacked: a numeric amount plus a label. */
export interface StackInput {
  readonly key: string;
  readonly label: string;
  readonly value: number;
  /** Explicit colour; otherwise taken from the sequential scale by ratio. */
  readonly color?: string;
}

/** Options for {@link computeStack}. */
export interface StackOptions {
  /** Total pixel width available for the bar. Default 100. */
  readonly width?: number;
  /** Leave horizontal gaps between segments; px per internal edge. Default 1. */
  readonly gap?: number;
  /** Scale for auto-coloured segments. */
  readonly scale?: (t: number) => string;
}

/**
 * Compute pixel geometry for a stacked bar.
 *
 * Handles the awkward cases explicitly: a zero total (no segments),
 * negative values (dropped — a stacked bar cannot express them), and
 * sub-pixel rounding so the last segment always ends exactly at `plotWidth`.
 */
export function computeStack(
  inputs: readonly StackInput[],
  options: StackOptions = {},
): StackLayout {
  const width = options.width ?? 100;
  const gap = options.gap ?? 1;
  const scale = options.scale ?? sequentialGreen;

  const usable = inputs.filter(
    (input) => typeof input.value === "number" && Number.isFinite(input.value) && input.value > 0,
  );
  const total = usable.reduce((sum, input) => sum + input.value, 0);
  const gapTotal = Math.max(0, usable.length - 1) * gap;
  const plotWidth = Math.max(0, width - gapTotal);

  if (total <= 0 || plotWidth <= 0) {
    return { segments: [], total: 0, plotWidth: 0 };
  }

  let cursor = 0;
  const segments: StackSegment[] = usable.map((input, index) => {
    const ratio = input.value / total;
    const isLast = index === usable.length - 1;
    // Give the final segment the remaining pixels so the bar closes flush.
    const segWidth = isLast
      ? Math.max(0, plotWidth - cursor)
      : Math.max(0, Math.round(ratio * plotWidth));
    const segment: StackSegment = {
      key: input.key,
      label: input.label,
      value: input.value,
      ratio,
      x: round(cursor),
      width: round(segWidth),
      color: input.color ?? scale(ratio),
    };
    cursor += segWidth;
    return segment;
  });

  return { segments, total, plotWidth: round(plotWidth) };
}

// ── Colour scales ───────────────────────────────────────────────────────────

/** Anchor stops for a colour ramp: `[position 0..1, r, g, b]`. */
export type ColorStop = readonly [number, number, number, number];

/** The Signal-OS sequential ramp: deep navy → green glow. */
export const SEQUENTIAL_GREEN_STOPS: readonly ColorStop[] = [
  [0, 13, 17, 23],
  [0.35, 0, 120, 90],
  [0.7, 0, 220, 140],
  [1, 0, 255, 136],
] as const;

/** Diverging ramp for signed values: red ← neutral → green. */
export const DIVERGING_STOPS: readonly ColorStop[] = [
  [0, 255, 68, 85],
  [0.5, 26, 40, 52],
  [1, 0, 255, 136],
] as const;

/** Interpolate between two `[r,g,b]` triplets. */
function mixRgb(a: readonly [number, number, number], b: readonly [number, number, number], t: number): [number, number, number] {
  return [
    Math.round(a[0] + (b[0] - a[0]) * t),
    Math.round(a[1] + (b[1] - a[1]) * t),
    Math.round(a[2] + (b[2] - a[2]) * t),
  ];
}

/**
 * Build a colour function from anchor stops. The returned function takes a
 * normalised position and returns `#rrggbb`. Pure.
 */
export function makeScale(stops: readonly ColorStop[]): (t: number) => string {
  const sorted = [...stops].sort((a, b) => a[0] - b[0]);
  return (t: number): string => {
    const clamped = Number.isFinite(t) ? Math.min(1, Math.max(0, t)) : 0;
    if (sorted.length === 0) return "#000000";
    const first = sorted[0];
    const last = sorted[sorted.length - 1];
    if (!first || !last) return "#000000";
    if (clamped <= first[0]) {
      const [r, g, b] = [first[1], first[2], first[3]];
      return rgb(r, g, b);
    }
    if (clamped >= last[0]) {
      const [r, g, b] = [last[1], last[2], last[3]];
      return rgb(r, g, b);
    }
    for (let i = 0; i < sorted.length - 1; i++) {
      const a = sorted[i];
      const b = sorted[i + 1];
      if (!a || !b) continue;
      if (clamped >= a[0] && clamped <= b[0]) {
        const span = b[0] - a[0] || 1;
        const [r, g, bb] = mixRgb(
          [a[1], a[2], a[3]],
          [b[1], b[2], b[3]],
          (clamped - a[0]) / span,
        );
        return rgb(r, g, bb);
      }
    }
    const [r, g, bb] = [last[1], last[2], last[3]];
    return rgb(r, g, bb);
  };
}

/** `rgb()` → hex string. */
function rgb(r: number, g: number, b: number): string {
  const hex = (value: number): string =>
    Math.min(255, Math.max(0, value)).toString(16).padStart(2, "0");
  return `#${hex(r)}${hex(g)}${hex(b)}`;
}

/** Sequential Signal-OS scale — pass the result of {@link makeScale}. */
export const sequentialGreen: (t: number) => string = makeScale(SEQUENTIAL_GREEN_STOPS);

/** Diverging scale for signed deltas around zero. */
export const divergingSignal: (t: number) => string = makeScale(DIVERGING_STOPS);

/**
 * Diverging scale for a signed value with an explicit midpoint (default 0).
 * `|value| <= range` maps to the neutral colour; beyond it saturates.
 */
export function divergingByValue(
  value: number,
  range: number,
  scale: (t: number) => string = divergingSignal,
): string {
  const safeRange = range > 0 ? range : 1;
  const ratio = Math.min(1, Math.max(-1, value / safeRange));
  return scale((ratio + 1) / 2);
}

/** Legend entries for a colour scale — ready to render as a gradient bar. */
export interface ColorLegend {
  readonly stops: readonly { readonly position: number; readonly color: string }[];
  readonly min: number;
  readonly max: number;
}

/** Sample a scale into legend stops. */
export function scaleLegend(
  scale: (t: number) => string,
  min: number,
  max: number,
  samples = 5,
): ColorLegend {
  const count = Math.max(2, Math.floor(samples));
  const stops = Array.from({ length: count }, (_, index) => {
    const position = index / (count - 1);
    return { position, color: scale(position) };
  });
  return { stops, min, max };
}