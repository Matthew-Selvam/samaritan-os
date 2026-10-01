"use client";

/**
 * ShortcutsDialog.tsx — the `?` keyboard shortcut reference.
 *
 * A modal dialog with real focus management (trap, restore, Esc to close) and
 * no animation unless the user hasn't asked for reduced motion. Content comes
 * entirely from `SHORTCUT_COMMANDS` in `@/lib/shortcuts`, so this file never
 * hard-codes a key that the dispatcher doesn't actually listen for.
 */

import { Keyboard, X } from "lucide-react";
import { useCallback, useEffect, useRef } from "react";

import {
  SHORTCUT_COMMANDS,
  commandsByCategory,
  formatBinding,
  isApplePlatform,
  type ShortcutCategory,
} from "@/lib/shortcuts";

/** A host-supplied row, so the dialog can advertise app-specific bindings. */
export interface ShortcutCommandRow {
  readonly id: string;
  readonly label: string;
  readonly category: ShortcutCategory;
  readonly keys: readonly string[];
  readonly description?: string;
}

/** Props for {@link ShortcutsDialog}. */
export interface ShortcutsDialogProps {
  /** Controlled visibility. */
  readonly open: boolean;
  /** Called on close — Esc, the ✕ button, or a backdrop click. */
  readonly onClose: () => void;
  /** Optional extra rows injected by the host app, e.g. "jump to agent 3". */
  readonly extraCommands?: readonly ShortcutCommandRow[];
}

export function ShortcutsDialog({
  open,
  onClose,
  extraCommands = [],
}: ShortcutsDialogProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);
  const apple = isApplePlatform();

  // Remember what had focus, move focus into the panel, and put it back on close.
  useEffect(() => {
    if (!open) return;
    restoreRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const panel = panelRef.current;
    if (panel) {
      const first = panel.querySelector<HTMLElement>(
        "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])",
      );
      (first ?? panel).focus();
    }
    return () => {
      restoreRef.current?.focus?.();
    };
  }, [open]);

  // Esc closes. Tab is trapped inside the panel.
  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const panel = panelRef.current;
      if (!panel) return;
      const focusables = Array.from(
        panel.querySelectorAll<HTMLElement>(
          "button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex='-1'])",
        ),
      ).filter((el) => el.offsetParent !== null || el === document.activeElement);
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    },
    [onClose],
  );

  if (!open) return null;

  const groups = commandsByCategory().filter((group) => group.commands.length > 0);
  const extrasByCategory = new Map<ShortcutCategory, ShortcutCommandRow[]>();
  for (const extra of extraCommands) {
    const list: ShortcutCommandRow[] = [...(extrasByCategory.get(extra.category) ?? []), extra];
    extrasByCategory.set(extra.category, list);
  }

  return (
    <div
      className="fixed inset-0 z-[1000] flex items-start justify-center pt-[12vh] px-4"
      style={{ background: "var(--scrim)", backdropFilter: "blur(2px)" }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="shortcuts-dialog-title"
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="panel w-full max-w-3xl max-h-[76vh] overflow-hidden flex flex-col"
        style={{ background: "var(--bg-panel)", borderColor: "var(--border-hi)" }}
      >
        {/* ── Header ── */}
        <div className="panel-header flex-shrink-0">
          <div className="flex items-center gap-2">
            <Keyboard size={13} strokeWidth={2} style={{ color: "var(--green)" }} aria-hidden="true" />
            <h2
              id="shortcuts-dialog-title"
              className="font-bold tracking-[0.2em]"
              style={{ fontSize: 11, color: "var(--green)" }}
            >
              KEYBOARD SHORTCUTS
            </h2>
            <span className="label" style={{ fontSize: 9 }}>
              {apple ? "macOS" : "PC"} LAYOUT
            </span>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close shortcuts dialog"
            className="focus-ring p-1 rounded"
            style={{ color: "var(--text-muted)", cursor: "pointer", background: "transparent", border: "1px solid transparent" }}
            onMouseEnter={(e) => {
              e.currentTarget.style.color = "var(--red)";
              e.currentTarget.style.borderColor = "var(--line-err-30)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.color = "var(--text-muted)";
              e.currentTarget.style.borderColor = "transparent";
            }}
          >
            <X size={13} strokeWidth={2} aria-hidden="true" />
          </button>
        </div>

        {/* ── Body ── */}
        <div className="panel-body overflow-y-auto scroll-thin flex-1">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-5">
            {groups.map((group) => {
              const extras = extrasByCategory.get(group.category) ?? [];
              return (
                <section key={group.category} aria-labelledby={`sc-group-${group.category}`}>
                  <h3
                    id={`sc-group-${group.category}`}
                    className="label mb-2 pb-1"
                    style={{ fontSize: 9, borderBottom: "1px solid var(--border)" }}
                  >
                    {group.category.toUpperCase()}
                  </h3>
                  <ul className="flex flex-col">
                    {group.commands.map((command) => (
                      <li
                        key={command.id}
                        className="flex items-center justify-between gap-3 py-1.5"
                        style={{ borderBottom: "1px solid var(--line-hairline-50)" }}
                      >
                        <div className="min-w-0">
                          <div style={{ fontSize: 11, color: "var(--text)" }}>{command.label}</div>
                          <div
                            className="label truncate"
                            style={{ fontSize: 8, textTransform: "none", letterSpacing: "0.04em" }}
                          >
                            {command.description}
                          </div>
                        </div>
                        <kbd
                          className="flex-shrink-0 px-1.5 py-0.5 rounded"
                          style={{
                            fontSize: 10,
                            color: "var(--green)",
                            background: "var(--tint-accent-07)",
                            border: "1px solid var(--line-accent-25)",
                            fontFamily: "var(--font-mono)",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {formatBinding(command.binding, apple)}
                        </kbd>
                      </li>
                    ))}
                    {extras.map((extra) => (
                      <li
                        key={extra.id}
                        className="flex items-center justify-between gap-3 py-1.5"
                        style={{ borderBottom: "1px solid var(--line-hairline-50)" }}
                      >
                        <div className="min-w-0">
                          <div style={{ fontSize: 11, color: "var(--text)" }}>{extra.label}</div>
                          {extra.description ? (
                            <div
                              className="label truncate"
                              style={{ fontSize: 8, textTransform: "none", letterSpacing: "0.04em" }}
                            >
                              {extra.description}
                            </div>
                          ) : null}
                        </div>
                        <kbd
                          className="flex-shrink-0 px-1.5 py-0.5 rounded"
                          style={{
                            fontSize: 10,
                            color: "var(--cyan)",
                            background: "var(--tint-info-07)",
                            border: "1px solid var(--line-info-25)",
                            fontFamily: "var(--font-mono)",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {extra.keys.join(apple ? "" : "+")}
                        </kbd>
                      </li>
                    ))}
                  </ul>
                </section>
              );
            })}
          </div>

          {/* Single-key agent jump reference. */}
          <section className="mt-5 pt-3" aria-labelledby="sc-agent-keys" style={{ borderTop: "1px solid var(--border)" }}>
            <h3 id="sc-agent-keys" className="label mb-2" style={{ fontSize: 9 }}>
              AGENT QUICK-SELECT
            </h3>
            <div className="flex flex-wrap gap-1">
              {AGENT_QUICK_SELECT.map((agent) => (
                <span
                  key={agent.key}
                  className="flex items-center gap-1 px-1.5 py-0.5 rounded"
                  style={{ fontSize: 9, border: "1px solid var(--border)", background: "var(--bg-card)" }}
                >
                  <kbd
                    style={{
                      color: agent.color,
                      background: "transparent",
                      fontFamily: "var(--font-mono)",
                      fontWeight: 700,
                    }}
                  >
                    {agent.key}
                  </kbd>
                  <span style={{ color: "var(--text-muted)" }}>{agent.name}</span>
                </span>
              ))}
            </div>
          </section>
        </div>

        {/* ── Footer ── */}
        <div
          className="panel-header flex-shrink-0"
          style={{ borderTop: "1px solid var(--border)", borderBottom: "none", borderRadius: "0 0 6px 6px" }}
        >
          <span className="label" style={{ fontSize: 8 }}>
            ESC TO CLOSE · TAB CYCLES FOCUS
          </span>
          <span className="label" style={{ fontSize: 8 }}>
            {SHORTCUT_COMMANDS.length} COMMANDS
          </span>
        </div>
      </div>
    </div>
  );
}

/**
 * The 1–9 agent quick-select strip. Mirrors the first nine catalogue entries,
 * which is the order agents appear in the AGENT panel.
 */
const AGENT_QUICK_SELECT: readonly { key: string; name: string; color: string }[] = [
  { key: "1", name: "GRAPH", color: "var(--cyan)" },
  { key: "2", name: "TIMELINE", color: "var(--amber)" },
  { key: "3", name: "REPORTS", color: "var(--text)" },
  { key: "4", name: "ENTITIES", color: "var(--purple)" },
  { key: "5", name: "SIGNALS", color: "var(--green)" },
  { key: "6", name: "CASES", color: "var(--cyan)" },
  { key: "7", name: "AGENTS", color: "var(--green)" },
  { key: "8", name: "MONITOR", color: "var(--red)" },
  { key: "9", name: "EXPORT", color: "var(--text-muted)" },
] as const;

export default ShortcutsDialog;