"use client";

/**
 * EntityBadge.tsx — the one chip that renders an entity anywhere in the app.
 *
 * Uses `lib/entityTypes.ts` for the canonical colour/label/icon so the graph,
 * tables, graph inspector and vault all agree on what a `crypto_wallet` looks
 * like. Optional pin/unpin affordance is used by the KNOWLEDGE view.
 */

import { colorFor, iconFor, labelFor, normalizeEntityType } from "@/lib/entityTypes";
import type { Entity, EntityType } from "@/lib/types";
import { confidencePct } from "@/lib/format";
import clsx from "clsx";

export interface EntityBadgeProps {
  /** Canonical or raw type string from the API. */
  type: EntityType | string;
  /** Optional label shown after the icon. */
  label?: string;
  /** Omit the label for dense contexts (graph legend, table cells). */
  showLabel?: boolean;
  /** Appends a muted confidence percentage. */
  confidence?: number;
  /** Renders as a toggle instead of a static chip. */
  active?: boolean;
  onToggle?: () => void;
  className?: string;
  title?: string;
  size?: "sm" | "md";
}

/**
 * Coloured entity-type chip.
 *
 * When `onToggle` is supplied it becomes a real `<button>` with
 * `aria-pressed`, so the graph legend is keyboard operable and its state is
 * announced — the colour is only ever a secondary cue.
 */
export function EntityBadge({
  type,
  label,
  showLabel = true,
  confidence,
  active = true,
  onToggle,
  className,
  title,
  size = "sm",
}: EntityBadgeProps) {
  const canonical = normalizeEntityType(type);
  const color = colorFor(canonical);
  const text = label ?? labelFor(canonical);
  const icon = iconFor(canonical);

  const body = (
    <>
      <span aria-hidden="true" className="text-[10px] leading-none">
        {icon}
      </span>
      {showLabel && <span className="truncate">{text}</span>}
      {typeof confidence === "number" && Number.isFinite(confidence) && (
        <span className="tabular-nums opacity-70">{confidencePct(confidence)}</span>
      )}
    </>
  );

  const style = {
    color: active ? color : undefined,
    background: active ? `${color}17` : "transparent",
    borderColor: active ? `${color}4d` : undefined,
  };

  if (onToggle) {
    return (
      <button
        type="button"
        aria-pressed={active}
        aria-label={`${text}${active ? " (visible)" : " (hidden)"}`}
        onClick={onToggle}
        title={title ?? `${text} — click to ${active ? "hide" : "show"}`}
        className={clsx(
          "focus-ring inline-flex max-w-full items-center gap-1 rounded border px-1.5 font-mono uppercase tracking-[0.08em] transition-all",
          size === "sm" ? "py-[1px] text-[9px]" : "py-0.5 text-[10px]",
          !active && "border-border-subtle text-ink-faint line-through",
          className,
        )}
        style={style}
      >
        {body}
      </button>
    );
  }

  return (
    <span
      title={title ?? text}
      className={clsx(
        "inline-flex max-w-full items-center gap-1 rounded border px-1.5 font-mono uppercase tracking-[0.08em]",
        size === "sm" ? "py-[1px] text-[9px]" : "py-0.5 text-[10px]",
        className,
      )}
      style={style}
    >
      {body}
    </span>
  );
}

/**
 * One row of an entity table: badge, label, value, confidence and source.
 * Truncates aggressively so a 1000-row table stays scannable.
 */
export function EntityRow({
  entity,
  onClick,
  selected = false,
}: {
  entity: Entity;
  onClick?: (entity: Entity) => void;
  selected?: boolean;
}) {
  const value = entity.value ?? entity.label;
  return (
    <button
      type="button"
      onClick={onClick ? () => onClick(entity) : undefined}
      aria-pressed={onClick ? selected : undefined}
      className={clsx(
        "focus-ring flex w-full min-w-0 items-center gap-2 border-b border-border-subtle/60 px-2 py-1.5 text-left transition-colors",
        onClick && "cursor-pointer hover:bg-accent/[0.05]",
        selected && "bg-accent/[0.09]",
      )}
    >
      <EntityBadge type={entity.type} showLabel={false} />
      <span className="min-w-0 flex-1 truncate text-[11px] text-ink" title={value}>
        {entity.label || value}
      </span>
      {entity.confidence !== undefined && (
        <span className="shrink-0 tabular-nums text-[9px] text-ink-muted">
          {confidencePct(entity.confidence)}
        </span>
      )}
      {entity.source && (
        <span className="shrink-0 font-mono text-[9px] uppercase tracking-[0.08em] text-ink-muted">
          {entity.source}
        </span>
      )}
    </button>
  );
}
