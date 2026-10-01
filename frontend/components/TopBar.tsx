"use client";

/**
 * TopBar.tsx — persistent application chrome: brand, view nav, health, clock
 * and the case selector.
 *
 * Health is a single shared fetch owned by the shell and passed down, so the
 * top bar, the rail and the OPS view never poll `/api/health` independently.
 */

import { useEffect, useState } from "react";
import { Badge, Button, Select, Tooltip } from "./ui";
import { StatusDot } from "./StatusDot";
import { ThemeToggle } from "./ThemeToggle";
import { formatTimestamp } from "@/lib/format";
import { VIEWS, type ViewId } from "@/lib/views";
import type { Case, HealthLevel } from "@/lib/types";

export interface TopBarProps {
  view: ViewId;
  onViewChange: (view: ViewId) => void;
  /** `null` while health is unknown. */
  health: HealthLevel | null;
  healthDetail?: string;
  /** Cases for the selector. */
  cases?: readonly Case[];
  /** Currently bound case id, or `null` for "no case". */
  caseId?: string | null;
  onCaseChange?: (caseId: string | null) => void;
  onOpenPalette?: () => void;
  onOpenShortcuts?: () => void;
  /** Number of agents registered, shown in the chrome. */
  agentCount?: number;
  /** Notified after the theme toggle switches dark <-> light. */
  onThemeChange?: (theme: "dark" | "light") => void;
  className?: string;
}

const HEALTH_TONE = { ok: "ok", degraded: "warn", down: "err" } as const;

const HEALTH_TEXT = {
  ok: "BACKEND ONLINE",
  degraded: "BACKEND DEGRADED",
  down: "BACKEND OFFLINE",
} as const;

/**
 * The application top bar.
 *
 * The nav is a row of real buttons so it is fully keyboard reachable; the clock
 * is `aria-hidden` because it changes every second and would otherwise spam a
 * screen reader.
 */
export function TopBar({
  view,
  onViewChange,
  health,
  healthDetail,
  cases = [],
  caseId = null,
  onCaseChange,
  onOpenPalette,
  onOpenShortcuts,
  agentCount,
  onThemeChange,
  className,
}: TopBarProps) {
  const [now, setNow] = useState<string>("");

  useEffect(() => {
    const tick = () => setNow(formatTimestamp(new Date()));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <header
      className={`flex shrink-0 items-center gap-3 border-b border-border-subtle bg-surface-1 px-3 py-1.5 ${
        className ?? ""
      }`}
    >
      {/* Brand */}
      <div className="flex shrink-0 items-center gap-2">
        <span
          aria-hidden="true"
          className="text-[16px] text-accent"
          style={{ textShadow: "0 0 8px var(--green)" }}
        >
          ⊕
        </span>
        <span className="font-mono text-[12px] font-bold tracking-[0.25em] text-accent">
          SIGNAL-OS
        </span>
        <span className="mono-label hidden !text-[8px] lg:inline">v0.1.0-alpha</span>
      </div>

      {/* View navigation */}
      <nav
        aria-label="Primary views"
        className="scroll-thin hidden min-w-0 flex-1 items-center gap-0.5 overflow-x-auto xl:flex"
      >
        {VIEWS.map((def) => {
          const active = def.id === view;
          return (
            <button
              key={def.id}
              type="button"
              onClick={() => onViewChange(def.id)}
              aria-current={active ? "page" : undefined}
              title={`${def.description}  (${def.hotkey})`}
              className={`focus-ring shrink-0 rounded border px-2 py-1 font-mono text-[9px] uppercase tracking-[0.12em] transition-colors ${
                active
                  ? "border-accent/50 bg-accent/10 text-accent"
                  : "border-transparent text-ink-muted hover:text-ink"
              }`}
            >
              {def.label}
            </button>
          );
        })}
      </nav>

      {/* Right cluster */}
      <div className="flex shrink-0 items-center gap-2">
        {onOpenPalette && (
          <Button size="sm" variant="ghost" onClick={onOpenPalette} title="Command palette (⌘K)">
            <span aria-hidden="true">⌘</span>K
          </Button>
        )}

        {onCaseChange && (
          <label className="hidden items-center gap-1 md:flex">
            <span className="mono-label !text-[8px]">CASE</span>
            <Select
              aria-label="Active case"
              value={caseId ?? ""}
              onChange={(event) => onCaseChange(event.target.value || null)}
              className="!w-[150px] !py-0.5 !text-[10px]"
            >
              <option value="">— none —</option>
              {cases.map((item) => (
                <option key={item.case_id} value={item.case_id}>
                  {item.name}
                </option>
              ))}
            </Select>
          </label>
        )}

        {agentCount !== undefined && agentCount > 0 && (
          <Badge tone="idle" title="Registered specialist agents">
            {agentCount} AGENTS
          </Badge>
        )}

        <Tooltip label={healthDetail ?? HEALTH_TEXT[health ?? "degraded"]} side="bottom">
          <StatusDot
            tone={health ? HEALTH_TONE[health] : "idle"}
            dotOnly
            pulse={health === "ok"}
            label={HEALTH_TEXT[health ?? "degraded"]}
          />
        </Tooltip>

        <span
          aria-hidden="true"
          className="hidden tabular-nums text-[9px] text-ink-muted 2xl:inline"
        >
          {now}
        </span>

        <ThemeToggle
          hideLabel
          onChange={onThemeChange}
          className="!px-1.5 !py-[3px]"
        />

        {onOpenShortcuts && (
          <Button
            size="sm"
            variant="ghost"
            onClick={onOpenShortcuts}
            iconLabel="Keyboard shortcuts (?)"
          >
            ?
          </Button>
        )}
      </div>
    </header>
  );
}
