"use client";

/**
 * Onboarding.tsx — the first-run tour.
 *
 * Design intent: this platform performs OSINT on real people, so the tour leads
 * with what Signal-OS *is* and makes the authorized-use acknowledgement the
 * second step rather than a dismissible footer. Skipping is always allowed and
 * never punished (the record is written either way, so the tour does not
 * reappear to harass the user), but the acknowledgement is recorded
 * explicitly and separately.
 *
 * Accessibility:
 *   - `role="dialog"` + `aria-modal`, labelled by the step title.
 *   - Full keyboard control: →/Enter advance, ←/Backspace retreat, Esc skips,
 *     Tab is trapped inside the panel, focus moves to each new step's heading.
 *   - The progress rail is a real `tablist`-style indicator with aria state, not
 *     decoration.
 *   - No motion unless the OS allows it; the CSS in globals.css already
 *     neutralises animation under `prefers-reduced-motion`, and we add no
 *     JavaScript-driven animation.
 */

import { ArrowLeft, ArrowRight, Check, ShieldAlert, X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

import {
  ONBOARDING_EXAMPLE_INPUTS,
  ONBOARDING_STEPS,
  ONBOARDING_VERSION,
  clampStep,
  isLastStep,
  loadOnboardingRecord,
  saveOnboardingRecord,
  shouldShowOnboarding,
  type OnboardingRecord,
} from "@/lib/onboarding";

/** Props for {@link Onboarding}. */
export interface OnboardingProps {
  /** Force visibility regardless of the stored record. */
  readonly open?: boolean;
  /** Called once the tour is dismissed, with whether it was completed. */
  readonly onClose?: (completed: boolean) => void;
  /** Called when the authorized-use acknowledgement is ticked. */
  readonly onAcknowledge?: (acknowledged: boolean) => void;
  /** Skip the "show once" behaviour — used by the replay-tour command. */
  readonly force?: boolean;
  /** Override the persistence key (tests, embedded use). */
  readonly storageKey?: string;
}

export function Onboarding({
  open,
  onClose,
  onAcknowledge,
  force = false,
  storageKey,
}: OnboardingProps) {
  const [visible, setVisible] = useState(false);
  const [step, setStep] = useState(0);
  const [acknowledged, setAcknowledged] = useState(false);
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);
  const titleId = useId();

  const current = ONBOARDING_STEPS[clampStep(step)];
  const needsAck = current?.requiresAcknowledgement === true;
  const canAdvance = !needsAck || acknowledged;
  const last = isLastStep(step, ONBOARDING_STEPS.length);

  // ── Decide whether to show ──────────────────────────────────────────────
  useEffect(() => {
    if (open !== undefined) {
      setVisible(open);
      return;
    }
    if (force) {
      setVisible(true);
      return;
    }
    setVisible(shouldShowOnboarding(storageKey, ONBOARDING_VERSION));
  }, [open, force, storageKey]);

  // ── Move focus to the new step's heading whenever it changes ────────────
  useEffect(() => {
    if (!visible) return;
    headingRef.current?.focus();
  }, [step, visible]);

  // ── Focus capture/restore + Tab trap ───────────────────────────────────
  const finish = useCallback(
    (completed: boolean) => {
      const record: OnboardingRecord = {
        version: ONBOARDING_VERSION,
        status: completed ? "completed" : "skipped",
        acknowledged,
        updatedAt: new Date().toISOString(),
      };
      saveOnboardingRecord(record, storageKey);
      setVisible(false);
      restoreRef.current?.focus?.();
      onClose?.(completed);
    },
    [acknowledged, onClose, storageKey],
  );

  const advance = useCallback(() => {
    if (needsAck && !acknowledged) return;
    if (last) finish(true);
    else setStep((index) => clampStep(index + 1));
  }, [acknowledged, finish, last, needsAck]);

  const retreat = useCallback(() => {
    setStep((index) => clampStep(index - 1));
  }, []);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      switch (event.key) {
        case "Escape":
          event.preventDefault();
          event.stopPropagation();
          finish(false);
          return;
        case "ArrowRight":
          event.preventDefault();
          advance();
          return;
        case "ArrowLeft":
          event.preventDefault();
          retreat();
          return;
        case "Enter":
          // Let the acknowledgement checkbox handle its own Enter.
          if (event.target instanceof HTMLInputElement) return;
          event.preventDefault();
          advance();
          return;
        case " ":
          if (event.target instanceof HTMLInputElement) return;
          event.preventDefault();
          advance();
          return;
        case "Backspace":
          if (event.target instanceof HTMLInputElement) return;
          event.preventDefault();
          retreat();
          return;
        case "Tab": {
          event.preventDefault();
          const panel = panelRef.current;
          if (!panel) return;
          const focusables = Array.from(
            panel.querySelectorAll<HTMLElement>(
              "button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex='-1'])",
            ),
          );
          if (focusables.length === 0) return;
          const first = focusables[0];
          const lastEl = focusables[focusables.length - 1];
          if (!first || !lastEl) return;
          if (event.shiftKey && document.activeElement === first) {
            lastEl.focus();
          } else if (!event.shiftKey && document.activeElement === lastEl) {
            first.focus();
          }
          return;
        }
        default:
          return;
      }
    },
    [advance, finish, retreat],
  );

  if (!visible || !current) return null;

  const handleAcknowledge = (value: boolean) => {
    setAcknowledged(value);
    onAcknowledge?.(value);
  };

  return (
    <div
      className="fixed inset-0 z-[1100] flex items-center justify-center p-4"
      style={{ background: "var(--scrim-deep)", backdropFilter: "blur(3px)" }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) finish(false);
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={onKeyDown}
        className="panel w-full max-w-2xl overflow-hidden flex flex-col"
        style={{ background: "var(--bg-panel)", borderColor: "var(--border-hi)" }}
      >
        {/* ── Progress rail ── */}
        <div
          className="flex items-center justify-between px-4 py-2.5 flex-shrink-0"
          style={{ borderBottom: "1px solid var(--border)", background: "var(--bg)" }}
        >
          <div className="flex items-center gap-1.5" role="list" aria-label="Tour progress">
            {ONBOARDING_STEPS.map((s, index) => {
              const done = index < step;
              const active = index === step;
              return (
                <span
                  key={s.id}
                  role="listitem"
                  aria-current={active ? "step" : undefined}
                  className="flex items-center gap-1.5"
                >
                  <span
                    className="rounded-full inline-block"
                    style={{
                      width: active ? 18 : 6,
                      height: 6,
                      background: done ? "var(--green-dim)" : active ? "var(--green)" : "var(--border-hi)",
                      boxShadow: active ? "0 0 8px var(--green)" : undefined,
                      transition: "width 0.2s cubic-bezier(0.4,0,0.2,1)",
                    }}
                  />
                  {active ? (
                    <span className="label" style={{ fontSize: 8, color: "var(--green)" }}>
                      {`${index + 1}/${ONBOARDING_STEPS.length}`}
                    </span>
                  ) : null}
                </span>
              );
            })}
          </div>

          <button
            type="button"
            onClick={() => finish(false)}
            aria-label="Skip the tour"
            className="focus-ring p-1 rounded"
            style={{ color: "var(--text-muted)", cursor: "pointer", background: "transparent", border: "1px solid transparent" }}
          >
            <X size={13} strokeWidth={2} aria-hidden="true" />
          </button>
        </div>

        {/* ── Step body ── */}
        <div className="panel-body flex-1 overflow-y-auto scroll-thin">
          <div className="flex items-start gap-3 mb-3">
            <span
              aria-hidden="true"
              className="flex-shrink-0 flex items-center justify-center"
              style={{
                width: 34,
                height: 34,
                fontSize: 16,
                color: needsAck ? "var(--amber)" : "var(--green)",
                border: `1px solid ${needsAck ? "var(--line-warn-30)" : "var(--line-accent-30)"}`,
                background: needsAck ? "var(--tint-warn-07)" : "var(--tint-accent-07)",
                borderRadius: 5,
                textShadow: needsAck ? "0 0 8px var(--amber)" : "0 0 8px var(--green)",
              }}
            >
              {current.icon}
            </span>
            <div className="min-w-0 flex-1">
              <p className="label" style={{ fontSize: 8, color: needsAck ? "var(--amber)" : "var(--text-muted)" }}>
                {current.eyebrow.toUpperCase()}
              </p>
              <h2
                id={titleId}
                ref={headingRef}
                tabIndex={-1}
                className="outline-none font-bold"
                style={{
                  fontSize: 14,
                  lineHeight: 1.35,
                  color: "var(--text)",
                  marginTop: 2,
                }}
              >
                {current.title}
              </h2>
            </div>
          </div>

          <p style={{ fontSize: 12, lineHeight: 1.65, color: "var(--text-muted)" }}>{current.body}</p>

          {/* Acknowledgement — the only gated control in the tour. */}
          {needsAck ? (
            <div
              className="mt-4 p-3 rounded flex gap-3"
              style={{
                border: `1px solid ${acknowledged ? "var(--line-accent-35)" : "var(--line-warn-35)"}`,
                background: acknowledged ? "var(--tint-accent-05)" : "var(--tint-warn-05)",
              }}
            >
              <ShieldAlert
                size={15}
                strokeWidth={2}
                aria-hidden="true"
                style={{ color: acknowledged ? "var(--green)" : "var(--amber)", flexShrink: 0 }}
              />
              <label
                htmlFor={`${titleId}-ack`}
                className="flex items-start gap-2 cursor-pointer"
                style={{ fontSize: 11, lineHeight: 1.6, color: "var(--text)" }}
              >
                <input
                  id={`${titleId}-ack`}
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(event) => handleAcknowledge(event.target.checked)}
                  className="mt-0.5 flex-shrink-0"
                  style={{ accentColor: "var(--green)", width: 13, height: 13 }}
                />
                <span>
                  I will only investigate targets I am authorised to assess, and will handle the
                  resulting personal data in line with applicable law.
                </span>
              </label>
            </div>
          ) : null}

          {/* Bullets */}
          <ul className="mt-4 flex flex-col gap-2" style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {current.bullets.map((bullet) => (
              <li key={bullet} className="flex items-start gap-2" style={{ fontSize: 11, lineHeight: 1.6 }}>
                <Check size={11} strokeWidth={2.5} aria-hidden="true" style={{ color: "var(--green)", flexShrink: 0, marginTop: 3 }} />
                <span style={{ color: "var(--text-muted)" }}>{bullet}</span>
              </li>
            ))}
          </ul>

          {/* Example inputs, shown on the input step only. */}
          {current.id === "inputs" ? (
            <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-2">
              {ONBOARDING_EXAMPLE_INPUTS.map((example) => (
                <div
                  key={example.value}
                  className="px-2 py-1.5 rounded"
                  style={{ border: "1px solid var(--border)", background: "var(--bg)" }}
                >
                  <code style={{ fontSize: 11, color: "var(--green)", fontFamily: "var(--font-mono)" }}>
                    {example.value}
                  </code>
                  <p className="label" style={{ fontSize: 8, textTransform: "none", letterSpacing: "0.04em" }}>
                    {example.hint}
                  </p>
                </div>
              ))}
            </div>
          ) : null}
        </div>

        {/* ── Footer ── */}
        <div
          className="flex items-center justify-between gap-3 px-4 py-2.5 flex-shrink-0"
          style={{ borderTop: "1px solid var(--border)", background: "var(--bg)" }}
        >
          <span className="label truncate" style={{ fontSize: 8 }}>
            {needsAck && !acknowledged ? "ACKNOWLEDGE TO CONTINUE" : current.hint.toUpperCase()}
          </span>

          <div className="flex items-center gap-2 flex-shrink-0">
            <button
              type="button"
              onClick={() => finish(false)}
              className="focus-ring px-2.5 py-1 rounded"
              style={{
                fontSize: 10,
                letterSpacing: "0.1em",
                color: "var(--text-muted)",
                background: "transparent",
                border: "1px solid var(--border)",
                cursor: "pointer",
              }}
            >
              SKIP
            </button>

            {step > 0 ? (
              <button
                type="button"
                onClick={retreat}
                aria-label="Previous step"
                className="focus-ring px-2 py-1 rounded flex items-center"
                style={{
                  color: "var(--text)",
                  background: "var(--bg-card)",
                  border: "1px solid var(--border-hi)",
                  cursor: "pointer",
                }}
              >
                <ArrowLeft size={12} strokeWidth={2} aria-hidden="true" />
              </button>
            ) : null}

            <button
              type="button"
              onClick={advance}
              disabled={!canAdvance}
              aria-disabled={!canAdvance}
              className="focus-ring px-3 py-1 rounded flex items-center gap-1.5"
              style={{
                fontSize: 10,
                fontWeight: 700,
                letterSpacing: "0.12em",
                color: canAdvance ? "var(--bg)" : "var(--text-muted)",
                background: canAdvance ? "var(--green)" : "var(--bg-card)",
                border: `1px solid ${canAdvance ? "var(--green)" : "var(--border)"}`,
                cursor: canAdvance ? "pointer" : "not-allowed",
                opacity: canAdvance ? 1 : 0.5,
              }}
            >
              {last ? "BEGIN" : "CONTINUE"}
              {last ? (
                <ArrowRight size={12} strokeWidth={2.5} aria-hidden="true" />
              ) : null}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/** Has the visitor already completed or skipped the tour? */
export function hasSeenOnboarding(storageKey?: string): boolean {
  const record = loadOnboardingRecord(storageKey);
  return record !== null && record.version === ONBOARDING_VERSION;
}

export default Onboarding;