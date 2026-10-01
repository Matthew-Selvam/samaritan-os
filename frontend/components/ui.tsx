"use client";

/**
 * ui.tsx — the shared Signal-OS design system.
 *
 * Every view composes from these primitives; no view invents its own panel
 * chrome, button or badge. They are deliberately thin wrappers over the tokens
 * already declared in `app/globals.css` (`.panel`, `.tone-*`, `.skeleton`,
 * `.focus-ring`, `.mono-label`) plus the Tailwind `@theme` colours, so the
 * CSS-first tokens and React components can never drift apart.
 *
 * Accessibility contract, applied to every primitive here:
 *  - interactive elements are real `<button>` / `<a>` / `<input>` elements and
 *    are reachable by keyboard;
 *  - every icon-only control carries an `aria-label`;
 *  - `.focus-ring` gives a visible 2px focus ring;
 *  - colour is never the only signal — badges carry text, status dots carry
 *    `aria-label`s, and contrast holds AA on the dark surface.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import clsx from "clsx";
import { toneClass, toneColor, type Tone } from "@/lib/format";

/* ══════════════════════════════════════════════════════════════════════════
   Primitives
   ══════════════════════════════════════════════════════════════════════════ */

export type PanelTone = "default" | "raised" | "flush";

export interface PanelProps {
  children: ReactNode;
  /** Header row; omit for a borderless panel body. */
  title?: ReactNode;
  /** Right-aligned header content (counts, actions). */
  actions?: ReactNode;
  /** Small uppercase kicker above the title. */
  eyebrow?: ReactNode;
  className?: string;
  bodyClassName?: string;
  tone?: PanelTone;
  /** Adds the hover accent from `.panel-interactive`. */
  interactive?: boolean;
  /** Removes body padding — for tables and canvases that own their own. */
  flush?: boolean;
  /** Rendered instead of `children` when the panel has nothing to show. */
  empty?: ReactNode;
  /** `aria-label` when there is no visible header. */
  "aria-label"?: string;
  id?: string;
}

/**
 * The base container for every region in the app.
 *
 * @example
 * <Panel title="Service health" actions={<Badge tone="ok">7/7</Badge>}>…</Panel>
 */
export function Panel({
  children,
  title,
  actions,
  eyebrow,
  className,
  bodyClassName,
  tone = "default",
  interactive = false,
  flush = false,
  empty,
  "aria-label": ariaLabel,
  id,
}: PanelProps) {
  const hasHeader = title !== undefined || actions !== undefined || eyebrow !== undefined;
  const body = (
    <div
      className={clsx(
        flush ? "" : "panel-body",
        bodyClassName,
        empty !== undefined && "text-ink-muted",
      )}
    >
      {empty !== undefined ? empty : children}
    </div>
  );

  return (
    <section
      id={id}
      aria-label={ariaLabel ?? (typeof title === "string" ? title : undefined)}
      className={clsx(
        "panel flex min-w-0 flex-col",
        tone === "raised" && "bg-surface-2",
        tone === "flush" && "border-transparent",
        interactive && "panel-interactive",
        className,
      )}
    >
      {hasHeader && (
        <header className="panel-header">
          <div className="flex min-w-0 items-baseline gap-2">
            {eyebrow && <span className="mono-label shrink-0">{eyebrow}</span>}
            {title && (
              <h2 className="m-0 truncate text-[11px] font-bold uppercase tracking-[0.14em] text-ink">
                {title}
              </h2>
            )}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
        </header>
      )}
      {body}
    </section>
  );
}

/* ── Button ─────────────────────────────────────────────────────────────────── */

export type ButtonVariant = "solid" | "outline" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

const BUTTON_BASE =
  "focus-ring inline-flex items-center justify-center gap-1.5 rounded-md font-mono uppercase tracking-[0.12em] transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-45";

const BUTTON_VARIANT: Record<ButtonVariant, string> = {
  solid: "border border-accent/60 bg-accent/12 text-accent hover:bg-accent/20",
  outline: "border border-border-strong bg-surface-3/60 text-ink hover:border-accent/50 hover:text-accent",
  ghost: "border border-transparent bg-transparent text-ink-muted hover:border-border-strong hover:text-ink",
  danger:
    "border border-signal-err/45 bg-signal-err/10 text-signal-err hover:bg-signal-err/20",
};

const BUTTON_SIZE: Record<ButtonSize, string> = {
  sm: "px-2 py-0.5 text-[9px]",
  md: "px-3 py-1.5 text-[10px]",
};

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** When set, the button becomes icon-only and this becomes its label. */
  iconLabel?: string;
  /** Shows a spinner and disables the button. */
  loading?: boolean;
  full?: boolean;
}

/**
 * The single button primitive. Icon-only usage requires `iconLabel`, which is
 * rendered as the accessible name.
 */
export function Button({
  variant = "outline",
  size = "md",
  iconLabel,
  loading = false,
  full = false,
  className,
  children,
  disabled,
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      aria-label={iconLabel ?? rest["aria-label"]}
      aria-busy={loading || undefined}
      className={clsx(
        BUTTON_BASE,
        BUTTON_VARIANT[variant],
        BUTTON_SIZE[size],
        full && "w-full",
        !children && "px-2",
        className,
      )}
      {...rest}
    >
      {loading && <Spinner size={size === "sm" ? 9 : 11} />}
      {children}
    </button>
  );
}

/** Tiny inline spinner; honours `prefers-reduced-motion` via globals.css. */
export function Spinner({ size = 12, label }: { size?: number; label?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 12 12"
      role={label ? "img" : "presentation"}
      aria-label={label}
      style={{ animation: "spin-slow 0.8s linear infinite", flexShrink: 0 }}
    >
      <circle
        cx="6"
        cy="6"
        r="4.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeDasharray="20"
        strokeDashoffset="5"
      />
    </svg>
  );
}

/* ── Badge ──────────────────────────────────────────────────────────────────── */

export interface BadgeProps {
  children: ReactNode;
  tone?: Tone;
  /** Overrides the tone palette with an explicit hex (entity colours, etc). */
  color?: string;
  /** Adds the glow animation for live states. */
  pulse?: boolean;
  className?: string;
  title?: string;
  size?: "sm" | "md";
}

/**
 * Status chip. Always renders text — colour is a secondary cue, never the only
 * one. When `color` is supplied the chip is tinted with that entity colour.
 */
export function Badge({
  children,
  tone = "idle",
  color,
  pulse = false,
  className,
  title,
  size = "sm",
}: BadgeProps) {
  const style: CSSProperties | undefined = color
    ? {
        color,
        background: `${color}17`,
        borderColor: `${color}4d`,
      }
    : undefined;
  return (
    <span
      title={title}
      className={clsx(
        toneClass(tone),
        pulse && "tone-pulse",
        size === "sm" && "!px-1.5 !text-[9px]",
        className,
      )}
      style={style}
    >
      {children}
    </span>
  );
}

/** A coloured square/dot used for entity types and series in legends. */
export function ColorDot({ color, size = 8 }: { color: string; size?: number }) {
  return (
    <span
      aria-hidden="true"
      className="inline-block shrink-0 rounded-full"
      style={{
        width: size,
        height: size,
        background: color,
        boxShadow: `0 0 4px ${color}88`,
      }}
    />
  );
}

/* ── Skeleton ───────────────────────────────────────────────────────────────── */

export interface SkeletonProps {
  width?: number | string;
  height?: number | string;
  className?: string;
  /** Screen-reader text, e.g. "Loading investigations". */
  label?: string;
  rounded?: boolean;
}

/**
 * Loading placeholder. Reserves layout space (`.skeleton` sets a min-height)
 * so content never jumps when data lands.
 */
export function Skeleton({
  width = "100%",
  height = 14,
  className,
  label,
  rounded = false,
}: SkeletonProps) {
  return (
    <div
      className={clsx("skeleton", rounded && "!rounded-full", className)}
      style={{ width, height, borderRadius: rounded ? 999 : undefined }}
      role={label ? "status" : undefined}
      aria-label={label}
      aria-live={label ? "polite" : undefined}
    >
      {label ? <span className="sr-only">{label}</span> : null}
    </div>
  );
}

/** A column of skeleton lines, for list-shaped loading states. */
export function SkeletonRows({ rows = 3, height = 34 }: { rows?: number; height?: number }) {
  return (
    <div className="flex flex-col gap-2" role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} height={height} />
      ))}
    </div>
  );
}

/** Grid of skeleton cards. */
export function SkeletonGrid({
  count = 6,
  minWidth = 220,
  height = 84,
}: {
  count?: number;
  minWidth?: number;
  height?: number;
}) {
  return (
    <div
      className="grid gap-2"
      style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${minWidth}px, 1fr))` }}
      role="status"
      aria-label="Loading"
    >
      {Array.from({ length: count }, (_, i) => (
        <Skeleton key={i} height={height} />
      ))}
    </div>
  );
}

/* ── Empty / error states ───────────────────────────────────────────────────── */

export interface EmptyStateProps {
  title: string;
  description?: string;
  /** Single glyph rendered above the title. */
  glyph?: ReactNode;
  /** Primary call to action. */
  action?: ReactNode;
  tone?: Tone;
  className?: string;
  compact?: boolean;
}

/**
 * The "nothing here yet" panel. Used for every empty collection so no view can
 * render a bare blank area.
 */
export function EmptyState({
  title,
  description,
  glyph = "◌",
  action,
  tone = "idle",
  className,
  compact = false,
}: EmptyStateProps) {
  return (
    <div
      className={clsx(
        "flex flex-col items-center justify-center gap-2 text-center",
        compact ? "py-6" : "py-12",
        className,
      )}
    >
      <span
        aria-hidden="true"
        style={{
          fontSize: compact ? 20 : 30,
          lineHeight: 1,
          color: toneColor(tone),
          opacity: 0.6,
        }}
      >
        {glyph}
      </span>
      <p className="mono-label !text-[10px] !tracking-[0.16em] text-ink-muted">{title}</p>
      {description && (
        <p className="m-0 max-w-[46ch] text-[11px] leading-relaxed text-ink-muted">
          {description}
        </p>
      )}
      {action}
    </div>
  );
}

/** Error state with an optional retry — paired with `useAsyncResource`. */
export function ErrorState({
  title = "Request failed",
  message,
  onRetry,
  className,
  compact = false,
}: {
  title?: string;
  message: string | null | undefined;
  onRetry?: () => void;
  className?: string;
  compact?: boolean;
}) {
  return (
    <div
      role="alert"
      className={clsx(
        "flex flex-col items-center justify-center gap-2 text-center",
        compact ? "py-5" : "py-9",
        className,
      )}
    >
      <span className="tone-err">⚠ ERROR</span>
      <p className="m-0 max-w-[60ch] break-words text-[11px] leading-relaxed text-signal-err/90">
        {message && message.trim() ? message : "No detail available."}
      </p>
      {onRetry && (
        <Button size="sm" variant="outline" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

/* ── Tabs ───────────────────────────────────────────────────────────────────── */

export interface TabItem<T extends string = string> {
  id: T;
  label: string;
  /** Count badge rendered after the label. */
  count?: number;
  icon?: ReactNode;
  disabled?: boolean;
}

export interface TabsProps<T extends string = string> {
  items: readonly TabItem<T>[];
  active: T;
  onChange: (id: T) => void;
  /** Accessible name for the tablist. */
  label: string;
  className?: string;
  /** Right-aligned controls rendered on the same row. */
  actions?: ReactNode;
  size?: "sm" | "md";
}

/**
 * Roving-tabindex tablist with arrow-key navigation and Home/End support,
 * following the WAI-ARIA tabs pattern.
 */
export function Tabs<T extends string = string>({
  items,
  active,
  onChange,
  label,
  className,
  actions,
  size = "md",
}: TabsProps<T>) {
  const baseId = useId();
  const refs = useRef(new Map<string, HTMLButtonElement>());

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const enabled = items.filter((item) => !item.disabled);
    if (enabled.length === 0) return;
    const index = enabled.findIndex((item) => item.id === active);
    let next = index;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      next = (index + 1 + enabled.length) % enabled.length;
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      next = (index - 1 + enabled.length) % enabled.length;
    } else if (event.key === "Home") {
      next = 0;
    } else if (event.key === "End") {
      next = enabled.length - 1;
    } else {
      return;
    }
    event.preventDefault();
    const target = enabled[next];
    onChange(target.id);
    refs.current.get(target.id)?.focus();
  };

  return (
    <div
      className={clsx(
        "flex items-center gap-2 border-b border-border-subtle bg-surface-1 px-3",
        className,
      )}
    >
      <div
        role="tablist"
        aria-label={label}
        aria-orientation="horizontal"
        onKeyDown={onKeyDown}
        className="scroll-thin flex flex-1 items-center gap-0.5 overflow-x-auto"
      >
        {items.map((item) => {
          const selected = item.id === active;
          return (
            <button
              key={item.id}
              ref={(node) => {
                if (node) refs.current.set(item.id, node);
                else refs.current.delete(item.id);
              }}
              role="tab"
              id={`${baseId}-${item.id}`}
              aria-selected={selected}
              aria-controls={`${baseId}-${item.id}-panel`}
              tabIndex={selected ? 0 : -1}
              disabled={item.disabled}
              onClick={() => onChange(item.id)}
              className={clsx(
                "focus-ring flex shrink-0 items-center gap-1.5 border-b-2 font-mono uppercase tracking-[0.12em] transition-colors disabled:cursor-not-allowed disabled:opacity-40",
                size === "sm" ? "px-2 py-1 text-[9px]" : "px-3 py-1.5 text-[10px]",
                selected
                  ? "border-accent text-accent"
                  : "border-transparent text-ink-muted hover:text-ink",
              )}
            >
              {item.icon}
              <span>{item.label}</span>
              {typeof item.count === "number" && item.count > 0 && (
                <span
                  className={clsx(
                    "rounded px-1 text-[9px] font-bold",
                    selected ? "bg-accent/15 text-accent" : "bg-surface-3 text-ink-muted",
                  )}
                >
                  {item.count}
                </span>
              )}
            </button>
          );
        })}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
    </div>
  );
}

/** Body of one tab; wires up the `aria-controls` relationship from {@link Tabs}. */
export function TabPanel({
  tabId,
  children,
  className,
}: {
  tabId: string;
  children: ReactNode;
  className?: string;
}) {
  const baseId = useId().replace(/[^a-zA-Z0-9]/g, "");
  return (
    <div
      role="tabpanel"
      id={`${baseId}-${tabId}-panel`}
      className={clsx("min-h-0 flex-1", className)}
    >
      {children}
    </div>
  );
}

/* ── Table ──────────────────────────────────────────────────────────────────── */

export interface Column<T> {
  /** Stable key; also the default sort key. */
  key: string;
  header: string;
  /** Cell renderer. */
  render: (row: T) => ReactNode;
  /** Sort comparator value; omit to make the column unsortable. */
  sortValue?: (row: T) => string | number;
  /** Column width, e.g. `"120px"` or `"minmax(160px, 1fr)"`. */
  width?: string;
  align?: "left" | "right" | "center";
  /** Hide below the given breakpoint to keep dense tables readable. */
  hideBelow?: "sm" | "md" | "lg" | "xl";
}

export type SortDirection = "asc" | "desc";

export interface TableProps<T> {
  columns: readonly Column<T>[];
  rows: readonly T[];
  rowKey: (row: T, index: number) => string;
  onRowClick?: (row: T) => void;
  /** Initial sort. */
  defaultSort?: { key: string; direction: SortDirection };
  /** Controlled sort; when supplied the table never sorts internally. */
  sort?: { key: string; direction: SortDirection } | null;
  onSortChange?: (next: { key: string; direction: SortDirection }) => void;
  empty?: ReactNode;
  loading?: boolean;
  skeletonRows?: number;
  /** Marks the currently selected row. */
  isRowActive?: (row: T) => boolean;
  className?: string;
  /** Sticky header for long tables. */
  maxHeight?: number | string;
  /** Caption for screen readers. */
  caption?: string;
}

const HIDE_CLASS = {
  sm: "hidden sm:table-cell",
  md: "hidden md:table-cell",
  lg: "hidden lg:table-cell",
  xl: "hidden xl:table-cell",
} as const;

/**
 * Sortable, keyboard-navigable data table.
 *
 * Sorting is internal unless a controlled `sort` is passed. Header buttons carry
 * `aria-sort` so assistive tech announces the current order.
 */
export function Table<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  defaultSort,
  sort,
  onSortChange,
  empty,
  loading = false,
  skeletonRows = 6,
  isRowActive,
  className,
  maxHeight,
  caption,
}: TableProps<T>) {
  const [internalSort, setInternalSort] = useState<{
    key: string;
    direction: SortDirection;
  } | null>(defaultSort ?? null);
  const activeSort = sort !== undefined ? sort : internalSort;

  const sorted = useMemo(() => {
    if (!activeSort) return rows;
    const column = columns.find((c) => c.key === activeSort.key);
    if (!column?.sortValue) return rows;
    const factor = activeSort.direction === "asc" ? 1 : -1;
    return rows
      .slice()
      .sort((a, b) => {
        const av = column.sortValue!(a);
        const bv = column.sortValue!(b);
        if (typeof av === "number" && typeof bv === "number") return (av - bv) * factor;
        return String(av).localeCompare(String(bv)) * factor;
      });
  }, [rows, columns, activeSort]);

  const toggleSort = (column: Column<T>) => {
    if (!column.sortValue) return;
    const next = {
      key: column.key,
      direction:
        activeSort?.key === column.key && activeSort.direction === "asc"
          ? ("desc" as const)
          : ("asc" as const),
    };
    if (onSortChange) onSortChange(next);
    else setInternalSort(next);
  };

  if (loading) {
    return (
      <div className="p-3">
        <SkeletonRows rows={skeletonRows} height={28} />
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <>
        {empty ?? (
          <EmptyState
            title="NO ROWS"
            description="Nothing matched the current filter. Clear it to see everything available."
            compact
          />
        )}
      </>
    );
  }

  return (
    <div
      className={clsx("scroll-thin min-w-0 overflow-auto", className)}
      style={maxHeight ? { maxHeight } : undefined}
    >
      <table className="w-full border-collapse text-left text-[11px]">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead className="sticky top-0 z-[1] bg-surface-1">
          <tr className="border-b border-border-subtle">
            {columns.map((column) => {
              const sortable = Boolean(column.sortValue);
              const isSorted = activeSort?.key === column.key;
              return (
                <th
                  key={column.key}
                  scope="col"
                  style={{ width: column.width }}
                  aria-sort={
                    isSorted
                      ? activeSort.direction === "asc"
                        ? "ascending"
                        : "descending"
                      : sortable
                        ? "none"
                        : undefined
                  }
                  className={clsx(
                    "mono-label !text-[9px] whitespace-nowrap p-2 font-medium",
                    column.hideBelow && HIDE_CLASS[column.hideBelow],
                    column.align === "right" && "text-right",
                    column.align === "center" && "text-center",
                  )}
                >
                  {sortable ? (
                    <button
                      type="button"
                      onClick={() => toggleSort(column)}
                      className="focus-ring inline-flex items-center gap-1 rounded-sm text-inherit hover:text-accent"
                    >
                      {column.header}
                      <span aria-hidden="true" className="text-[8px]">
                        {isSorted ? (activeSort.direction === "asc" ? "▲" : "▼") : "◆"}
                      </span>
                    </button>
                  ) : (
                    column.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, index) => {
            const active = isRowActive?.(row) ?? false;
            return (
              <tr
                key={rowKey(row, index)}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                tabIndex={onRowClick ? 0 : undefined}
                onKeyDown={
                  onRowClick
                    ? (event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          onRowClick(row);
                        }
                      }
                    : undefined
                }
                aria-selected={onRowClick ? active : undefined}
                className={clsx(
                  "border-b border-border-subtle/60 transition-colors",
                  onRowClick && "cursor-pointer hover:bg-accent/[0.05]",
                  active && "bg-accent/[0.09]",
                )}
              >
                {columns.map((column) => (
                  <td
                    key={column.key}
                    className={clsx(
                      "px-2 py-1.5 align-middle text-ink",
                      column.hideBelow && HIDE_CLASS[column.hideBelow],
                      column.align === "right" && "text-right",
                      column.align === "center" && "text-center",
                    )}
                  >
                    {column.render(row)}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* ── Modal ──────────────────────────────────────────────────────────────────── */

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  /** Footer row for actions. */
  footer?: ReactNode;
  width?: number | string;
  className?: string;
  /** Label for the close button. */
  closeLabel?: string;
}

/**
 * Focus-trapping modal dialog. Traps Tab, restores focus on close, closes on
 * Escape and on backdrop click, and locks body scroll while open.
 */
export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  width = 560,
  className,
  closeLabel = "Close dialog",
}: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);
  const titleId = useId();

  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const focusables = () => {
      const root = panelRef.current;
      if (!root) return [] as HTMLElement[];
      return Array.from(
        root.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((node) => node.offsetParent !== null);
    };

    const timer = window.setTimeout(() => {
      const nodes = focusables();
      (nodes[0] ?? panelRef.current)?.focus();
    }, 0);

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const nodes = focusables();
      if (nodes.length === 0) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("keydown", onKeyDown, true);
      document.body.style.overflow = previousOverflow;
      restoreRef.current?.focus?.();
    };
  }, [open, onClose]);

  if (!open || typeof document === "undefined") return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[200] flex items-start justify-center overflow-y-auto p-4 sm:p-8"
      style={{ background: "var(--shadow-modal)", backdropFilter: "blur(2px)" }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={clsx(
          "panel flex max-h-full w-full flex-col bg-surface-1 shadow-[0_0_60px_var(--shadow-modal)]",
          className,
        )}
        style={{ maxWidth: typeof width === "number" ? `${width}px` : width }}
      >
        <header className="panel-header shrink-0">
          <h2
            id={titleId}
            className="m-0 truncate text-[11px] font-bold uppercase tracking-[0.14em] text-ink"
          >
            {title}
          </h2>
          <Button size="sm" variant="ghost" iconLabel={closeLabel} onClick={onClose}>
            ✕
          </Button>
        </header>
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">{children}</div>
        {footer && (
          <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-border-subtle bg-surface-2 px-3 py-2">
            {footer}
          </footer>
        )}
      </div>
    </div>,
    document.body,
  );
}

/* ── Drawer ─────────────────────────────────────────────────────────────────── */

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  /** Side the panel slides in from. */
  side?: "right" | "left";
  width?: number | string;
  closeLabel?: string;
  className?: string;
}

/**
 * Side panel for detail views (agent drawer, entity inspector). Shares the
 * Modal focus behaviour and renders on the trailing edge.
 */
export function Drawer({
  open,
  onClose,
  title,
  children,
  footer,
  side = "right",
  width = 380,
  closeLabel = "Close panel",
  className,
}: DrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [open, onClose]);

  if (!open || typeof document === "undefined") return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[190] flex"
      style={{ background: "var(--shadow-pop)" }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={clsx(
          "flex h-full min-h-0 flex-col border-border-subtle bg-surface-1 shadow-[0_0_60px_var(--shadow-modal)]",
          side === "right" ? "ml-auto border-l" : "mr-auto border-r",
          className,
        )}
        style={{ width: typeof width === "number" ? `${width}px` : width, maxWidth: "92vw" }}
      >
        <header className="panel-header shrink-0">
          <h2
            id={titleId}
            className="m-0 min-w-0 truncate text-[11px] font-bold uppercase tracking-[0.14em] text-ink"
          >
            {title}
          </h2>
          <Button size="sm" variant="ghost" iconLabel={closeLabel} onClick={onClose}>
            ✕
          </Button>
        </header>
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">{children}</div>
        {footer && (
          <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-border-subtle bg-surface-2 px-3 py-2">
            {footer}
          </footer>
        )}
      </div>
    </div>,
    document.body,
  );
}

/* ── Tooltip ────────────────────────────────────────────────────────────────── */

export interface TooltipProps {
  label: string;
  children: ReactNode;
  side?: "top" | "bottom" | "left" | "right";
  className?: string;
}

/**
 * CSS-only tooltip. The trigger gets `aria-label` (so the text is reachable by
 * screen readers even when the bubble is hover-only) and the bubble is exposed
 * via `aria-hidden` to avoid a double announcement.
 */
export function Tooltip({ label, children, side = "top", className }: TooltipProps) {
  const position: Record<NonNullable<TooltipProps["side"]>, string> = {
    top: "bottom-full left-1/2 -translate-x-1/2 mb-1.5",
    bottom: "top-full left-1/2 -translate-x-1/2 mt-1.5",
    left: "right-full top-1/2 -translate-y-1/2 mr-1.5",
    right: "left-full top-1/2 -translate-y-1/2 ml-1.5",
  };
  return (
    <span className={clsx("group/tt relative inline-flex", className)}>
      <span aria-label={label} className="inline-flex">
        {children}
      </span>
      <span
        aria-hidden="true"
        role="presentation"
        className={clsx(
          "pointer-events-none absolute z-[150] hidden whitespace-nowrap rounded border border-border-strong bg-surface-0 px-1.5 py-1 text-[9px] uppercase tracking-[0.1em] text-ink opacity-0 shadow-[0_4px_16px_var(--shadow-pop)] transition-opacity duration-150 group-hover/tt:block group-hover/tt:opacity-100 group-focus-within/tt:block group-focus-within/tt:opacity-100",
          position[side],
        )}
      >
        {label}
      </span>
    </span>
  );
}

/* ── Stat tile ──────────────────────────────────────────────────────────────── */

export interface StatTileProps {
  label: string;
  value: ReactNode;
  /** Unit or qualifier rendered after the value. */
  unit?: string;
  /** Secondary line, e.g. a delta or a hint. */
  hint?: string;
  tone?: Tone;
  /** Glyph in the corner. */
  icon?: ReactNode;
  /** Rendered as a progress bar under the value, 0..1. */
  progress?: number;
  /** `true` while the underlying value is unknown. */
  loading?: boolean;
  className?: string;
  onClick?: () => void;
  /** Accessible description of what the number means. */
  title?: string;
}

/**
 * Headline metric. Colour-coded by `tone` with the label always present, so the
 * value never depends on hue alone.
 */
export function StatTile({
  label,
  value,
  unit,
  hint,
  tone = "info",
  icon,
  progress,
  loading = false,
  className,
  onClick,
  title,
}: StatTileProps) {
  const interactive = Boolean(onClick);
  const Element = interactive ? "button" : "div";
  const pct = typeof progress === "number" ? Math.max(0, Math.min(1, progress)) : null;

  return (
    <Element
      {...(interactive
        ? { type: "button" as const, onClick, "aria-label": `${label}: ${String(value)}` }
        : { role: "group" })}
      title={title}
      className={clsx(
        "panel focus-ring relative flex min-w-0 flex-col gap-1 overflow-hidden px-3 py-2 text-left",
        interactive && "panel-interactive cursor-pointer",
        className,
      )}
    >
      <span
        aria-hidden="true"
        className="absolute inset-y-0 left-0 w-[2px]"
        style={{ background: toneColor(tone), opacity: 0.75 }}
      />
      <span className="flex items-center justify-between gap-2">
        <span className="mono-label truncate !text-[9px]">{label}</span>
        {icon && (
          <span aria-hidden="true" className="shrink-0 text-[11px]" style={{ color: toneColor(tone) }}>
            {icon}
          </span>
        )}
      </span>
      {loading ? (
        <Skeleton height={20} width="70%" label={`Loading ${label}`} />
      ) : (
        <span className="flex items-baseline gap-1">
          <span
            className="truncate text-[20px] font-bold leading-none tabular-nums"
            style={{ color: toneColor(tone) }}
          >
            {value}
          </span>
          {unit && <span className="text-[10px] text-ink-muted">{unit}</span>}
        </span>
      )}
      {pct !== null && (
        <span className="h-[3px] w-full overflow-hidden rounded-full bg-surface-3">
          <span
            className="block h-full rounded-full transition-[width] duration-500"
            style={{ width: `${pct * 100}%`, background: toneColor(tone) }}
          />
        </span>
      )}
      {hint && <span className="truncate text-[9px] text-ink-muted">{hint}</span>}
    </Element>
  );
}

/* ── Progress meter ─────────────────────────────────────────────────────────── */

/** Thin 0..1 meter used by confidence bars and coverage readouts. */
export function Meter({
  value,
  tone = "ok",
  className,
  height = 4,
  label,
}: {
  value: number;
  tone?: Tone;
  className?: string;
  height?: number;
  label?: string;
}) {
  const pct = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0));
  return (
    <span
      role="meter"
      aria-valuenow={Math.round(pct * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label ?? "Progress"}
      className={clsx("block w-full overflow-hidden rounded-full bg-surface-3", className)}
      style={{ height }}
    >
      <span
        className="block h-full rounded-full transition-[width] duration-500"
        style={{ width: `${pct * 100}%`, background: toneColor(tone) }}
      />
    </span>
  );
}

/* ── Toast ──────────────────────────────────────────────────────────────────── */

/** One transient notification. */
export interface ToastItem {
  id: string;
  message: string;
  tone: Tone;
  /** Optional inline action, e.g. "Retry". */
  action?: { label: string; run: () => void };
}

/** Imperative toast API. */
export interface ToastApi {
  push: (message: string, tone?: Tone, action?: ToastItem["action"]) => void;
  success: (message: string) => void;
  error: (message: string, action?: ToastItem["action"]) => void;
  dismiss: (id: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

/**
 * Access the toast API.
 * @throws When called outside {@link ToastProvider} — that is a wiring bug, not
 *         a runtime condition to handle at each call site.
 */
export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}

const TOAST_TTL_MS = 5000;

/** Hosts the toast stack; renders nothing itself. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const timers = useRef(new Map<string, number>());

  const dismiss = useCallback((id: string) => {
    setItems((prev) => prev.filter((item) => item.id !== id));
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const push = useCallback(
    (message: string, tone: Tone = "info", action?: ToastItem["action"]) => {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      setItems((prev) => [...prev.slice(-3), { id, message, tone, action }]);
      const timer = window.setTimeout(() => dismiss(id), TOAST_TTL_MS);
      timers.current.set(id, timer);
    },
    [dismiss],
  );

  useEffect(() => {
    const map = timers.current;
    return () => {
      for (const timer of map.values()) window.clearTimeout(timer);
      map.clear();
    };
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      push,
      success: (message: string) => push(message, "ok"),
      error: (message, action) => push(message, "err", action),
      dismiss,
    }),
    [push, dismiss],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <ToastStack items={items} onDismiss={dismiss} />
    </ToastContext.Provider>
  );
}

function ToastStack({
  items,
  onDismiss,
}: {
  items: readonly ToastItem[];
  onDismiss: (id: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      className="pointer-events-none fixed bottom-9 right-4 z-[300] flex w-[min(360px,calc(100vw-2rem))] flex-col gap-2"
    >
      {items.map((item) => (
        <div
          key={item.id}
          role={item.tone === "err" ? "alert" : "status"}
          className="panel pointer-events-auto flex items-start gap-2 bg-surface-1 px-3 py-2 shadow-[0_6px_24px_var(--shadow-pop)]"
          style={{ borderColor: `${toneColor(item.tone)}55`, animation: "slide-in 0.16s ease-out" }}
        >
          <Badge tone={item.tone} className="mt-[1px] shrink-0">
            {item.tone}
          </Badge>
          <span className="min-w-0 flex-1 break-words text-[11px] leading-relaxed text-ink">
            {item.message}
          </span>
          {item.action && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                item.action?.run();
                onDismiss(item.id);
              }}
            >
              {item.action.label}
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            iconLabel="Dismiss notification"
            onClick={() => onDismiss(item.id)}
          >
            ✕
          </Button>
        </div>
      ))}
    </div>
  );
}

/* ── Field primitives ──────────────────────────────────────────────────────── */

export interface FieldProps {
  label: string;
  children: ReactNode;
  hint?: string;
  className?: string;
  htmlFor?: string;
}

/** Label + control + hint, with a shared `id` for the label association. */
export function Field({ label, children, hint, className, htmlFor }: FieldProps) {
  return (
    <div className={clsx("flex min-w-0 flex-col gap-1", className)}>
      <label className="mono-label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {hint && <p className="m-0 text-[9px] text-ink-muted">{hint}</p>}
    </div>
  );
}

export const inputClass =
  "focus-ring w-full rounded-md border border-border-strong bg-surface-0 px-2.5 py-1.5 text-[12px] text-ink placeholder:text-ink-faint focus:border-accent/60";

/** Text input styled from the design system. */
export function TextInput({
  className,
  ...rest
}: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input className={clsx(inputClass, className)} {...rest} />;
}

/** Textarea styled from the design system. */
export function TextArea({
  className,
  ...rest
}: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={clsx(inputClass, "resize-y leading-relaxed", className)} {...rest} />;
}

/** Select styled from the design system. */
export function Select({
  className,
  children,
  ...rest
}: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={clsx(inputClass, "cursor-pointer pr-6", className)} {...rest}>
      {children}
    </select>
  );
}

/* ── Segmented control ──────────────────────────────────────────────────────── */

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  title?: string;
  icon?: ReactNode;
}

/** Small radio-group control for view-local mode switches. */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
  className,
  size = "sm",
}: {
  value: T;
  options: readonly SegmentedOption<T>[];
  onChange: (value: T) => void;
  label: string;
  className?: string;
  size?: "sm" | "md";
}) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={clsx(
        "inline-flex items-center gap-0.5 rounded-md border border-border-subtle bg-surface-0 p-0.5",
        className,
      )}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={active}
            title={option.title ?? option.label}
            onClick={() => onChange(option.value)}
            className={clsx(
              "focus-ring flex items-center gap-1 rounded-sm font-mono uppercase tracking-[0.1em] transition-colors",
              size === "sm" ? "px-1.5 py-0.5 text-[9px]" : "px-2 py-1 text-[10px]",
              active
                ? "bg-accent/12 text-accent"
                : "text-ink-muted hover:text-ink",
            )}
          >
            {option.icon}
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/* ── Toggle ─────────────────────────────────────────────────────────────────── */

/** Accessible on/off switch. */
export function Toggle({
  checked,
  onChange,
  label,
  hint,
  disabled = false,
  className,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  hint?: string;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <label
      className={clsx(
        "focus-within:outline focus-within:outline-2 focus-within:outline-accent flex min-w-0 cursor-pointer items-center justify-between gap-3 rounded-sm py-0.5",
        disabled && "cursor-not-allowed opacity-50",
        className,
      )}
    >
      <span className="min-w-0">
        <span className="mono-label block truncate !text-[9px]">{label}</span>
        {hint && <span className="block truncate text-[9px] text-ink-muted">{hint}</span>}
      </span>
      <input
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="sr-only"
      />
      <span
        aria-hidden="true"
        className={clsx(
          "relative h-[14px] w-[26px] shrink-0 rounded-full border transition-colors",
          checked ? "border-accent/60 bg-accent/25" : "border-border-strong bg-surface-3",
        )}
      >
        <span
          className={clsx(
            "absolute top-[1px] h-[10px] w-[10px] rounded-full transition-all",
            checked ? "left-[13px] bg-accent" : "left-[1px] bg-ink-muted",
          )}
        />
      </span>
    </label>
  );
}

/* ── Key hint ───────────────────────────────────────────────────────────────── */

/** Render a keyboard shortcut, e.g. `⌘K` or `Ctrl+K`. */
export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={clsx(
        "focus-ring rounded border border-border-strong bg-surface-0 px-1 py-[1px] font-mono text-[9px] leading-none text-ink-muted",
        className,
      )}
    >
      {children}
    </kbd>
  );
}

/* ── Definition row ─────────────────────────────────────────────────────────── */

/** Label/value pair used by every detail drawer. */
export function DefRow({
  label,
  children,
  mono = false,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border-subtle/60 py-1 last:border-b-0">
      <span className="mono-label shrink-0 !text-[9px]">{label}</span>
      <span
        className={clsx(
          "min-w-0 break-words text-right text-[11px] text-ink",
          mono && "font-mono",
        )}
      >
        {children}
      </span>
    </div>
  );
}

/** Section heading inside a drawer or panel body. */
export function SectionTitle({
  children,
  actions,
  className,
}: {
  children: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={clsx("flex items-center justify-between gap-2 py-1", className)}>
      <h3 className="mono-label m-0 !text-[9px]">{children}</h3>
      {actions && <div className="flex items-center gap-1">{actions}</div>}
    </div>
  );
}

export { clsx };
