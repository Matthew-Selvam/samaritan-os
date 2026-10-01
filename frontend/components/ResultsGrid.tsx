"use client";

/**
 * ResultsGrid.tsx — result cards for the photo (IRIS) and name (PRISM) searches.
 *
 * The original card designs are preserved (thumbnail + similarity bar for media,
 * avatar + profile for names); what changed is that similarity is clamped and
 * never the sole signal, URLs are validated before being rendered as links, and
 * the grid is built from the shared `Panel` primitive.
 */

import clsx from "clsx";
import { EmptyState, Panel } from "./ui";

/** One result row, covering both search-result shapes. */
export interface ResultItem {
  url?: string;
  title?: string;
  similarity?: number;
  platform?: string;
  username?: string;
  bio?: string;
  avatar_url?: string;
  source?: string;
  /** Present on `SearchResultItem` results. */
  snippet?: string;
  confidence?: number;
}

export interface ResultsGridProps {
  results: readonly ResultItem[] | null | undefined;
  mode: "photo" | "name";
  /** Wall-clock search time in seconds. */
  searchTime?: number;
  /** Called when a card is activated. */
  onSelect?: (item: ResultItem, index: number) => void;
  className?: string;
  title?: string;
}

/** Similarity colour ramp: strong / medium / weak. */
function similarityColor(pct: number): string {
  if (pct >= 80) return "var(--green)";
  if (pct >= 50) return "var(--amber)";
  return "var(--red)";
}

function similarityGlow(pct: number): string {
  if (pct >= 80) return "0 0 8px var(--line-accent-30)";
  if (pct >= 50) return "0 0 8px var(--line-warn-25)";
  return "0 0 8px var(--line-err-25)";
}

/** Clamp an arbitrary similarity into 0..100. */
function clampSimilarity(value: number | undefined): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

function initials(name?: string): string {
  if (!name) return "??";
  return name
    .split(/[\s._-]+/)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? "")
    .join("");
}

/** Platform chip. */
function PlatformBadge({ platform }: { platform?: string }) {
  if (!platform) return null;
  return (
    <span className="inline-block shrink-0 rounded border border-signal-info/25 bg-signal-info/10 px-1.5 text-[8px] font-bold uppercase tracking-[0.12em] text-signal-info">
      {platform}
    </span>
  );
}

/** Only render a link when the URL is actually safe to open. */
function safeUrl(value: string | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : null;
  } catch {
    return null;
  }
}

/* ── Media card ─────────────────────────────────────────────────────────────── */

function PhotoCard({
  item,
  index,
  onSelect,
}: {
  item: ResultItem;
  index: number;
  onSelect?: (item: ResultItem, index: number) => void;
}) {
  const sim = clampSimilarity(item.similarity);
  const href = safeUrl(item.source);
  const thumb = safeUrl(item.url);

  return (
    <div
      role={onSelect ? "button" : undefined}
      tabIndex={onSelect ? 0 : undefined}
      onClick={onSelect ? () => onSelect(item, index) : undefined}
      onKeyDown={
        onSelect
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onSelect(item, index);
              }
            }
          : undefined
      }
      className={clsx(
        "focus-ring flex gap-3 rounded-lg p-3 text-left transition-colors",
        "border border-border-subtle bg-surface-2 hover:border-border-strong",
        onSelect && "cursor-pointer",
      )}
      style={{ animation: "slide-in 0.2s ease-out" }}
    >
      <span
        aria-hidden="true"
        className="block shrink-0 overflow-hidden rounded-md border border-border-subtle bg-surface-1"
        style={{ width: 80, height: 80 }}
      >
        {thumb ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={thumb}
            alt={item.title ?? "Image match"}
            loading="lazy"
            className="h-full w-full"
            style={{ objectFit: "cover" }}
          />
        ) : (
          <span className="flex h-full w-full items-center justify-center text-[20px] text-ink-muted">
            ◻
          </span>
        )}
      </span>

      <span className="flex min-w-0 flex-1 flex-col justify-between">
        <span className="block min-w-0">
          {href && (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(event) => event.stopPropagation()}
              className="block truncate text-[10px] text-signal-info no-underline hover:underline"
              title={href}
            >
              {item.source} ↗
            </a>
          )}
          {item.title && (
            <span className="mt-0.5 block truncate text-[11px] text-ink">{item.title}</span>
          )}
          {item.snippet && (
            <span className="mt-0.5 block truncate text-[10px] text-ink-muted">{item.snippet}</span>
          )}
        </span>

        <span className="mt-1 flex items-center gap-2">
          <span
            className="text-[12px] font-bold tabular-nums"
            style={{ color: similarityColor(sim), textShadow: similarityGlow(sim) }}
          >
            {sim.toFixed(1)}%
          </span>
          <span
            role="meter"
            aria-label="Match similarity"
            aria-valuenow={Math.round(sim)}
            aria-valuemin={0}
            aria-valuemax={100}
            className="block h-[3px] flex-1 overflow-hidden rounded-full bg-surface-3"
          >
            <span
              className="block h-full rounded-full transition-[width] duration-500"
              style={{ width: `${sim}%`, background: similarityColor(sim) }}
            />
          </span>
          <PlatformBadge platform={item.platform} />
        </span>
      </span>
    </div>
  );
}

/* ── Profile card ───────────────────────────────────────────────────────────── */

function NameCard({
  item,
  index,
  onSelect,
}: {
  item: ResultItem;
  index: number;
  onSelect?: (item: ResultItem, index: number) => void;
}) {
  const href = safeUrl(item.source);
  const avatar = safeUrl(item.avatar_url);

  return (
    <div
      role={onSelect ? "button" : undefined}
      tabIndex={onSelect ? 0 : undefined}
      onClick={onSelect ? () => onSelect(item, index) : undefined}
      onKeyDown={
        onSelect
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onSelect(item, index);
              }
            }
          : undefined
      }
      className={clsx(
        "focus-ring flex gap-3 rounded-lg p-3 text-left transition-colors",
        "border border-border-subtle bg-surface-2 hover:border-border-strong",
        onSelect && "cursor-pointer",
      )}
      style={{ animation: "slide-in 0.2s ease-out" }}
    >
      <span
        aria-hidden="true"
        className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full border border-border-strong text-[13px] font-bold"
        style={{
          background: avatar
            ? `url(${avatar}) center/cover no-repeat`
            : "linear-gradient(135deg, var(--tint-info-15), var(--tint-alt-15))",
          color: "var(--cyan)",
        }}
      >
        {!avatar && initials(item.username)}
      </span>

      <span className="flex min-w-0 flex-1 flex-col">
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[12px] font-semibold text-ink">
            {item.username ?? "Unknown"}
          </span>
          <PlatformBadge platform={item.platform} />
        </span>

        {item.bio && (
          <span
            className="mt-1 block overflow-hidden text-[10px] leading-relaxed text-ink-muted"
            style={{
              display: "-webkit-box",
              WebkitLineClamp: 2,
              WebkitBoxOrient: "vertical",
            }}
          >
            {item.bio}
          </span>
        )}

        {href && (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(event) => event.stopPropagation()}
            className="mt-1 block truncate text-[9px] text-signal-info/80 no-underline hover:underline"
            title={href}
          >
            {item.source} ↗
          </a>
        )}
      </span>
    </div>
  );
}

/**
 * Grid of search results.
 *
 * An empty list renders an explicit empty state rather than a blank area, and
 * the header always states the count so a truncated render is never ambiguous.
 */
export function ResultsGrid({
  results,
  mode,
  searchTime,
  onSelect,
  className,
  title,
}: ResultsGridProps) {
  const items = results ?? [];

  if (items.length === 0) {
    return (
      <Panel className={className} title={title} eyebrow={mode === "photo" ? "IRIS" : "PRISM"}>
        <EmptyState
          glyph="⊘"
          title="NO RESULTS"
          description={
            mode === "photo"
              ? "No visual matches were returned. Try a clearer crop, or run the reverse image search again."
              : "No profiles matched this name across the searched surfaces."
          }
        />
      </Panel>
    );
  }

  return (
    <Panel
      className={className}
      title={title ?? (mode === "photo" ? "Image matches" : "Profile matches")}
      eyebrow={mode === "photo" ? "IRIS" : "PRISM"}
      actions={
        <>
          <span className="text-[11px] font-bold tabular-nums text-accent">{items.length}</span>
          {typeof searchTime === "number" && Number.isFinite(searchTime) && (
            <span className="mono-label tabular-nums">{searchTime.toFixed(2)}s</span>
          )}
        </>
      }
    >
      <div
        className="grid gap-2"
        style={{
          gridTemplateColumns:
            mode === "photo"
              ? "repeat(auto-fill, minmax(320px, 1fr))"
              : "repeat(auto-fill, minmax(280px, 1fr))",
        }}
      >
        {items.map((item, index) =>
          mode === "photo" ? (
            <PhotoCard key={`photo-${index}`} item={item} index={index} onSelect={onSelect} />
          ) : (
            <NameCard key={`name-${index}`} item={item} index={index} onSelect={onSelect} />
          ),
        )}
      </div>
    </Panel>
  );
}
