"use client";

/**
 * CommandPalette.tsx — the ⌘K command palette surface.
 *
 * This file is deliberately a *view*: every ranking decision lives in
 * `@/lib/palette`, which is pure and independently testable. Here we only
 * manage DOM concerns — focus, selection, the active listbox, backdrop and
 * scroll-into-view.
 *
 * Data sources, in order of precedence:
 *   1. `items` — commands the host app passes in (its own navigation, actions).
 *   2. `providers` — async sources (recent investigations, cases) surfaced via
 *      the {@link PaletteProvider} interface.
 *   3. Nothing else — the palette never guesses at the host's capabilities.
 */

import {
  CornerDownLeft,
  Loader,
  Search,
  Terminal,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  collectDynamicItems,
  groupBySource,
  loadRecents,
  moveSelection,
  saveRecents,
  searchPalette,
  splitResults,
  touchRecent,
  type PaletteItem,
  type PaletteProvider,
  type PaletteResult,
  type RecentList,
} from "@/lib/palette";
import { formatBinding, isApplePlatform, type BindingSpec } from "@/lib/shortcuts";

/** Props for {@link CommandPalette}. */
export interface CommandPaletteProps {
  /** Controlled visibility. */
  readonly open: boolean;
  /** Called when the palette should close (Esc, backdrop, or after a pick). */
  readonly onClose: () => void;
  /** Static commands supplied by the host app. */
  readonly items?: readonly PaletteItem[];
  /** Async sources — recent investigations, cases, saved searches. */
  readonly providers?: readonly PaletteProvider[];
  /** Optional right-aligned footer hint, e.g. the active case id. */
  readonly footerHint?: string;
  /** Invoked with the chosen item's payload after the palette closes. */
  readonly onSelect?: (item: PaletteItem) => void;
  /** Placeholder for the search field. */
  readonly placeholder?: string;
  /** Rendered while the first dynamic fetch is in flight. */
  readonly emptyMessage?: string;
}

/** How long to wait after the last keystroke before querying providers. */
const PROVIDER_DEBOUNCE_MS = 180;

export function CommandPalette({
  open,
  onClose,
  items = [],
  providers = [],
  footerHint,
  onSelect,
  placeholder = "Search commands, agents, cases…",
  emptyMessage = "No matches",
}: CommandPaletteProps) {
  const apple = isApplePlatform();
  const listId = useId();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  const [query, setQuery] = useState("");
  const [recents, setRecents] = useState<RecentList>([]);
  const [dynamic, setDynamic] = useState<readonly PaletteItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState(0);

  // ── Load recents on open ────────────────────────────────────────────────
  useEffect(() => {
    if (!open) return;
    setQuery("");
    setSelected(0);
    setDynamic([]);
    setRecents(loadRecents());
    restoreRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    // Focus the input on the next frame so the dialog is painted first.
    const raf = requestAnimationFrame(() => inputRef.current?.focus());
    return () => {
      cancelAnimationFrame(raf);
      restoreRef.current?.focus?.();
    };
  }, [open]);

  // ── Debounced provider fetch ────────────────────────────────────────────
  useEffect(() => {
    if (!open || providers.length === 0) return;
    if (query.length === 0) {
      setDynamic([]);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    const timer = setTimeout(() => {
      collectDynamicItems(providers, query, controller.signal)
        .then((fetched) => {
          if (controller.signal.aborted) return;
          setDynamic(fetched);
          setLoading(false);
        })
        .catch(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, PROVIDER_DEBOUNCE_MS);

    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [open, query, providers]);

  // ── Ranking (pure engine) ───────────────────────────────────────────────
  const results = useMemo(
    () => searchPalette(items, dynamic, query, recents),
    [items, dynamic, query, recents],
  );

  const { recent: recentResults, rest } = useMemo(
    () => splitResults(results, recents, query),
    [results, recents, query],
  );

  /** Flattened ordering, so index → item is unambiguous for the listbox. */
  const flat: readonly PaletteResult[] = useMemo(
    () => [...recentResults, ...rest],
    [recentResults, rest],
  );

  // Keep the selection inside the list as it shrinks or re-ranks.
  useEffect(() => {
    if (selected >= flat.length) setSelected(Math.max(0, flat.length - 1));
  }, [flat.length, selected]);

  const commit = useCallback(
    (result: PaletteResult | undefined) => {
      if (!result) return;
      const next = touchRecent(recents, result.id);
      setRecents(next);
      saveRecents(next);
      onSelect?.(result);
      onClose();
    },
    [onClose, onSelect, recents],
  );

  // ── Keyboard handling ───────────────────────────────────────────────────
  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      switch (event.key) {
        case "Escape":
          event.preventDefault();
          event.stopPropagation();
          onClose();
          return;
        case "ArrowDown":
          event.preventDefault();
          setSelected((current) => moveSelection(flat.length, current, 1));
          return;
        case "ArrowUp":
          event.preventDefault();
          setSelected((current) => moveSelection(flat.length, current, -1));
          return;
        case "Home":
          if (query.length === 0) {
            event.preventDefault();
            setSelected(0);
          }
          return;
        case "End":
          if (query.length === 0) {
            event.preventDefault();
            setSelected(Math.max(0, flat.length - 1));
          }
          return;
        case "Enter":
          event.preventDefault();
          commit(flat[selected]);
          return;
        case "Tab": {
          // Trap focus inside the dialog.
          event.preventDefault();
          return;
        }
        default:
          return;
      }
    },
    [commit, flat, onClose, query.length, selected],
  );

  // ── Keep the selected row in view ──────────────────────────────────────
  useEffect(() => {
    const container = listRef.current;
    if (!container) return;
    const row = container.querySelector<HTMLElement>(`[data-index="${selected}"]`);
    row?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  if (!open) return null;

  const sections = groupBySource(flat);
  let cursor = -1;

  return (
    <div
      className="fixed inset-0 z-[1000] flex items-start justify-center pt-[10vh] px-4"
      style={{ background: "rgba(2,6,10,0.78)", backdropFilter: "blur(2px)" }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onKeyDown={onKeyDown}
        className="panel w-full max-w-xl overflow-hidden flex flex-col"
        style={{ background: "var(--bg-panel)", borderColor: "var(--border-hi)" }}
      >
        {/* ── Search field ── */}
        <div
          className="flex items-center gap-2 px-3 py-2 flex-shrink-0"
          style={{ borderBottom: "1px solid var(--border)" }}
        >
          <Search size={13} strokeWidth={2} style={{ color: "var(--green)", flexShrink: 0 }} aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            role="combobox"
            aria-expanded="true"
            aria-controls={listId}
            aria-activedescendant={flat[selected] ? `${listId}-${selected}` : undefined}
            aria-autocomplete="list"
            autoComplete="off"
            spellCheck={false}
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setSelected(0);
            }}
            placeholder={placeholder}
            className="flex-1 outline-none"
            style={{
              background: "transparent",
              border: "none",
              color: "var(--text)",
              fontSize: 13,
              fontFamily: "var(--font-mono)",
            }}
          />
          {loading ? (
            <Loader
              size={12}
              strokeWidth={2}
              aria-label="Loading results"
              style={{ color: "var(--cyan)", animation: "spin-slow 0.9s linear infinite" }}
            />
          ) : (
            <kbd
              className="px-1.5 py-0.5 rounded flex-shrink-0"
              style={{
                fontSize: 9,
                color: "var(--text-muted)",
                border: "1px solid var(--border)",
                fontFamily: "var(--font-mono)",
              }}
            >
              ESC
            </kbd>
          )}
        </div>

        {/* ── Results ── */}
        <div
          ref={listRef}
          id={listId}
          role="listbox"
          aria-label="Commands"
          className="overflow-y-auto scroll-thin flex-1"
          style={{ maxHeight: "46vh" }}
        >
          {flat.length === 0 ? (
            <div className="px-3 py-6 text-center">
              <Terminal size={16} strokeWidth={1.5} aria-hidden="true" style={{ color: "var(--text-muted)" }} className="mx-auto mb-2" />
              <p className="label" style={{ fontSize: 9 }}>
                {emptyMessage.toUpperCase()}
              </p>
              {query.length > 0 ? (
                <p className="label mt-1" style={{ fontSize: 8, textTransform: "none" }}>
                  {`no command, agent or case matches “${query}”`}
                </p>
              ) : null}
            </div>
          ) : (
            <>
              {query.length === 0 && recentResults.length > 0 ? (
                <SectionHeading label="RECENT" count={recentResults.length} />
              ) : null}
              {flat.map((result) => {
                cursor += 1;
                const index = cursor;
                const isSelected = index === selected;
                return (
                  <PaletteRow
                    key={result.id}
                    result={result}
                    index={index}
                    listId={listId}
                    selected={isSelected}
                    onHover={() => setSelected(index)}
                    onClick={() => commit(result)}
                    showRecentBadge={query.length === 0 && recentResults.some((r) => r.id === result.id)}
                  />
                );
              })}
            </>
          )}
        </div>

        {/* ── Footer ── */}
        <div
          className="flex items-center justify-between px-3 py-1.5 flex-shrink-0"
          style={{ borderTop: "1px solid var(--border)", background: "var(--bg)" }}
        >
          <div className="flex items-center gap-3">
            <FooterHint keys={apple ? "↑↓" : "↑↓"} label="navigate" />
            <FooterHint keys={apple ? "↩" : "Enter"} label="run" />
            <span
              className="px-1 rounded"
              style={{
                fontSize: 9,
                color: "var(--green)",
                background: "rgba(0,255,136,0.07)",
                border: "1px solid rgba(0,255,136,0.25)",
              }}
            >
              <CornerDownLeft size={9} strokeWidth={2} aria-hidden="true" />
            </span>
          </div>
          <span className="label" style={{ fontSize: 8 }}>
            {footerHint ?? `${flat.length} RESULT${flat.length === 1 ? "" : "S"}`}
          </span>
        </div>
      </div>
    </div>
  );
}

/** A section heading inside the results list. */
function SectionHeading({ label, count }: { label: string; count: number }) {
  return (
    <div
      className="px-3 pt-2 pb-1 label"
      style={{ fontSize: 8, borderBottom: "1px solid rgba(30,45,61,0.6)" }}
      aria-hidden="true"
    >
      {label} · {count}
    </div>
  );
}

/** Footer key + label pair. */
function FooterHint({ keys, label }: { keys: string; label: string }) {
  return (
    <span className="flex items-center gap-1 label" style={{ fontSize: 8 }}>
      <kbd
        style={{
          color: "var(--text-muted)",
          border: "1px solid var(--border)",
          borderRadius: 2,
          padding: "0 3px",
          fontFamily: "var(--font-mono)",
        }}
      >
        {keys}
      </kbd>
      {label}
    </span>
  );
}

/** Props for {@link PaletteRow}. */
interface PaletteRowProps {
  readonly result: PaletteResult;
  readonly index: number;
  readonly listId: string;
  readonly selected: boolean;
  readonly onHover: () => void;
  readonly onClick: () => void;
  readonly showRecentBadge: boolean;
}

/** One result row, with the matched characters highlighted. */
function PaletteRow({
  result,
  index,
  listId,
  selected,
  onHover,
  onClick,
  showRecentBadge,
}: PaletteRowProps) {
  const segments = highlight(result.label, result.matches);

  return (
    <div
      id={`${listId}-${index}`}
      role="option"
      aria-selected={selected}
      data-index={index}
      onMouseMove={onHover}
      onClick={onClick}
      className="flex items-center gap-2 px-3 py-1.5 cursor-pointer"
      style={{
        background: selected ? "rgba(0,255,136,0.08)" : "transparent",
        borderLeft: `2px solid ${selected ? "var(--green)" : "transparent"}`,
      }}
    >
      {result.icon ? (
        <span
          aria-hidden="true"
          className="flex-shrink-0 text-center"
          style={{ width: 14, fontSize: 11, color: selected ? "var(--green)" : "var(--text-muted)" }}
        >
          {result.icon}
        </span>
      ) : null}

      <div className="flex-1 min-w-0">
        <div className="truncate" style={{ fontSize: 11, color: selected ? "var(--text)" : "var(--text-muted)" }}>
          {segments.map((segment, i) =>
            segment.highlight ? (
              <span key={i} className="highlight">
                {segment.text}
              </span>
            ) : (
              <span key={i}>{segment.text}</span>
            ),
          )}
        </div>
        {result.hint ? (
          <div className="label truncate" style={{ fontSize: 8, textTransform: "none", letterSpacing: "0.04em" }}>
            {result.hint}
          </div>
        ) : null}
      </div>

      {showRecentBadge ? (
        <span className="label flex-shrink-0" style={{ fontSize: 7, color: "var(--cyan)" }}>
          RECENT
        </span>
      ) : null}
      {result.trailing ? (
        <span className="label flex-shrink-0" style={{ fontSize: 8 }}>
          {result.trailing}
        </span>
      ) : null}
      {selected && result.source === "static" ? (
        <CornerDownLeft size={10} strokeWidth={2} aria-hidden="true" style={{ color: "var(--green)", flexShrink: 0 }} />
      ) : null}
    </div>
  );
}

/** One piece of a highlighted label. */
interface HighlightSegment {
  readonly text: string;
  readonly highlight: boolean;
}

/**
 * Split a label into plain/highlighted segments from match ranges.
 * Pure; ranges are sorted, non-overlapping `[start, end)` pairs.
 */
export function highlight(text: string, matches: readonly [number, number][]): readonly HighlightSegment[] {
  if (matches.length === 0) return [{ text, highlight: false }];
  const segments: HighlightSegment[] = [];
  let cursor = 0;
  for (const [from, to] of matches) {
    if (from > cursor) segments.push({ text: text.slice(cursor, from), highlight: false });
    segments.push({ text: text.slice(from, to), highlight: true });
    cursor = to;
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor), highlight: false });
  return segments;
}

export default CommandPalette;

/** Re-exported so hosts can build palette items without importing the engine. */
export type { PaletteItem, PaletteProvider };
export { formatBinding };
export type { BindingSpec };