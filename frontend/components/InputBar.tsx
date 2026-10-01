"use client";

/**
 * InputBar.tsx — the single search bar for the whole platform.
 *
 * "ONE SEARCH BAR" is the product's core promise, so this stays deliberately
 * plain: one field, one submit affordance, rotating placeholders that show the
 * accepted input types. The shell drives it imperatively (focus, prefill, submit)
 * so ⌘K and `/` can drive it without prop-drilling a ref through every view.
 */

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { Badge, Spinner } from "./ui";

/** Imperative handle exposed to the app shell for shortcuts and the palette. */
export interface InputBarHandle {
  /** Move keyboard focus into the field. */
  focus: () => void;
  /** Replace the current value (used by palette actions). */
  prefill: (value: string) => void;
  /** Submit whatever is currently in the field. */
  submit: () => void;
  /** Current field value. */
  readonly value: string;
}

export interface InputBarProps {
  onSubmit: (value: string) => void;
  /** True while a pipeline is running — the field locks and shows progress. */
  running?: boolean;
  /** Case the run will be bound to, shown as a chip. */
  caseLabel?: string | null;
  /** Extra hint chips rendered under the field. */
  hints?: readonly string[];
  /** `false` removes the autofocus, for the dashboard's duplicate instance. */
  autoFocus?: boolean;
  className?: string;
  /** Accessible name for the text field. */
  label?: string;
}

const PLACEHOLDERS = [
  "target@example.com",
  "192.168.1.1",
  "john_doe_official",
  "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
  "suspicious-domain.io",
  "https://pastebin.com/xyz...",
] as const;

const DEFAULT_HINTS = [
  "email",
  "domain",
  "username",
  "ip",
  "wallet",
  "url",
  "image",
  "⌘↵",
] as const;

/** 3s placeholder rotation, matching the original component's cadence. */
const PLACEHOLDER_MS = 3000;

/**
 * The global input bar.
 *
 * @example
 * const bar = useRef<InputBarHandle>(null);
 * <InputBar ref={bar} onSubmit={run} running={busy} />
 */
export const InputBar = forwardRef<InputBarHandle, InputBarProps>(function InputBar(
  {
    onSubmit,
    running = false,
    caseLabel = null,
    hints,
    autoFocus = true,
    className,
    label = "Investigation target — any input type accepted",
  },
  ref,
) {
  const [value, setValue] = useState("");
  const [placeholderIndex, setPlaceholderIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const id = window.setInterval(() => {
      setPlaceholderIndex((index) => (index + 1) % PLACEHOLDERS.length);
    }, PLACEHOLDER_MS);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus();
  }, [autoFocus]);

  const submit = useCallback(() => {
    const trimmed = value.trim();
    if (running || !trimmed) return;
    onSubmit(trimmed);
    setValue("");
  }, [onSubmit, running, value]);

  useImperativeHandle(
    ref,
    () => ({
      focus: () => inputRef.current?.focus(),
      prefill: (next: string) => {
        setValue(next);
        inputRef.current?.focus();
      },
      submit,
      get value() {
        return value;
      },
    }),
    [submit, value],
  );

  const canSubmit = value.trim().length > 0 && !running;
  const hintList = hints ?? DEFAULT_HINTS;

  return (
    <div className={className}>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <label className="mono-label !text-[9px]" htmlFor="signal-input">
          INVESTIGATE · ANY INPUT TYPE
        </label>
        {caseLabel && (
          <Badge tone="info" title="Investigations will be bound to this case">
            CASE · {caseLabel}
          </Badge>
        )}
      </div>

      <div
        className="flex items-center gap-2 rounded-md px-3 py-2 transition-shadow duration-200"
        style={{
          background: "var(--bg-card)",
          border: `1px solid ${running ? "var(--green)" : "var(--border-hi)"}`,
          boxShadow: running ? "0 0 16px var(--tint-accent-12)" : undefined,
        }}
      >
        <span aria-hidden="true" className="select-none text-[14px] text-accent">
          ›
        </span>

        <input
          ref={inputRef}
          id="signal-input"
          type="text"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            // Plain Enter and ⌘/Ctrl+Enter both submit; the shell owns the global
            // ⌘Enter binding but this keeps the field usable without it.
            if (event.key === "Enter") {
              event.preventDefault();
              submit();
            }
          }}
          placeholder={PLACEHOLDERS[placeholderIndex]}
          disabled={running}
          aria-label={label}
          aria-describedby="signal-input-hints"
          autoComplete="off"
          spellCheck={false}
          className="min-w-0 flex-1 bg-transparent text-[13px] text-ink outline-none placeholder:text-ink-faint disabled:opacity-60"
          style={{ caretColor: "var(--green)" }}
        />

        {running ? (
          <span className="flex items-center gap-1.5 text-accent">
            <Spinner size={12} label="Investigation running" />
            <span className="mono-label !text-[9px]">RUNNING</span>
          </span>
        ) : (
          <button
            type="button"
            onClick={submit}
            disabled={!canSubmit}
            className="focus-ring rounded border px-3 py-1 font-mono text-[10px] uppercase tracking-[0.1em] transition-colors"
            style={{
              background: canSubmit ? "var(--tint-accent-12)" : "transparent",
              borderColor: canSubmit ? "var(--green)" : "var(--border)",
              color: canSubmit ? "var(--green)" : "var(--text-muted)",
              cursor: canSubmit ? "pointer" : "not-allowed",
            }}
          >
            Investigate
          </button>
        )}
      </div>

      <div
        id="signal-input-hints"
        className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1"
      >
        {hintList.map((hint) => (
          <span key={hint} className="mono-label !text-[8px]">
            {hint}
          </span>
        ))}
      </div>
    </div>
  );
});
