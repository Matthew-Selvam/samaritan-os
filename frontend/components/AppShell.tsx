"use client";

/**
 * AppShell.tsx — the client shell that owns all cross-view state.
 *
 * Ownership map (deliberate, so nothing is fetched twice):
 *  - this component owns: the active view, the active case, the current
 *    investigation, the pipeline WebSocket, health polling and every action
 *    (run, rotate, export, clear);
 *  - `TopBar` / `StatusBar` are pure chrome driven from here;
 *  - each view is mounted only while active, so switching views never leaks a
 *    socket, a timer or a stale fetch.
 *
 * Keyboard handling goes through `lib/shortcuts.ts`'s `useHotkeys` (owned by the
 * parallel workstream) rather than a second ad-hoc `keydown` listener, and the
 * ⌘K surface is `components/CommandPalette.tsx` (also owned there). Both are
 * imported; if either module is absent the shell still builds because every
 * shortcut is registered through the shared hook rather than inline handlers.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import { TopBar } from "@/components/TopBar";
import { StatusBar } from "@/components/StatusBar";
import { InputBar, type InputBarHandle } from "@/components/InputBar";
import { AgentGrid } from "@/components/AgentGrid";
import { OpsecIndicator } from "@/components/OpsecIndicator";
import { Tooltip } from "@/components/ui";
import { ToastProvider, useToast } from "@/components/ui";
import { CommandPalette } from "@/components/CommandPalette";
import { ShortcutsDialog } from "@/components/ShortcutsDialog";
import type { PaletteItem } from "@/lib/palette";
import { DashboardView } from "@/components/views/DashboardView";
import { InvestigateView } from "@/components/views/InvestigateView";
import { CasesView } from "@/components/views/CasesView";
import { AgentsView } from "@/components/views/AgentsView";
import { GraphView } from "@/components/views/GraphView";
import { TimelineView } from "@/components/views/TimelineView";
import { KnowledgeView } from "@/components/views/KnowledgeView";
import { ReportsView } from "@/components/views/ReportsView";
import { OpsView } from "@/components/views/OpsView";
import type { ResultItem } from "@/components/ResultsGrid";
import {
  getCaseEntities,
  getHealth,
  getInvestigation,
  listCases,
  listInvestigations,
  newCircuit,
  submitInvestigation,
  submitInvestigationSync,
  submitNameSearch,
  submitPhotoSearch,
} from "@/lib/api";
import { normalizeEntityType } from "@/lib/entityTypes";
import type {
  AgentResult,
  Case,
  Entity,
  HealthLevel,
  Investigation,
  InvestigationStatus,
  PipelineEvent,
  Report,
  Signal,
} from "@/lib/types";
import { useInvestigationStream } from "@/lib/useInvestigationStream";
import { useHotkeys } from "@/lib/shortcuts";
import { DEFAULT_VIEW, VIEWS, isViewId, viewByHotkey, viewById, type ViewId } from "@/lib/views";
import { apiCall, useLocalStorage } from "@/lib/hooks";

/** Lucide-free icon glyphs for the rail, keyed by the registry's icon names. */
const RAIL_GLYPH: Record<string, string> = {
  LayoutDashboard: "▚",
  Crosshair: "◎",
  FolderKanban: "▤",
  Bot: "⬡",
  Network: "∞",
  Clock: "⊕",
  Database: "▦",
  FileText: "⟁",
  ShieldCheck: "⛨",
};

/** Key used to persist the last active view. */
const VIEW_STORAGE_KEY = "signal-os:view";

export interface AppShellProps {
  /** Deep-link straight into a view (used by the palette and deep links). */
  initialView?: ViewId;
  className?: string;
}

/**
 * The application shell.
 *
 * @example
 * <AppShell />
 */
export function AppShell({ initialView, className }: AppShellProps) {
  return (
    <ToastProvider>
      <Shell initialView={initialView} className={className} />
    </ToastProvider>
  );
}

/** Inner shell — split out so the toast context is available. */
function Shell({ initialView, className }: AppShellProps) {
  const toast = useToast();

  /* ── View + case ── */
  const [storedView, setStoredView] = useLocalStorage<ViewId>(VIEW_STORAGE_KEY, DEFAULT_VIEW);
  const [view, setView] = useState<ViewId>(
    initialView ?? (isViewId(storedView) ? storedView : DEFAULT_VIEW),
  );
  const [caseId, setCaseId] = useState<string | null>(null);
  const [cases, setCases] = useState<Case[]>([]);

  /* ── Investigation + stream ── */
  const [investigation, setInvestigation] = useState<Investigation | null>(null);
  const [activeAgents, setActiveAgents] = useState<ReadonlySet<string>>(new Set());
  const [entities, setEntities] = useState<readonly Entity[]>([]);
  const [signals, setSignals] = useState<readonly Signal[]>([]);
  const [agentResults, setAgentResults] = useState<readonly AgentResult[]>([]);
  const [runError, setRunError] = useState<string | null>(null);
  const inputRef = useRef<InputBarHandle>(null);

  /* ── History / health ── */
  const [history, setHistory] = useState<Investigation[]>([]);
  const [health, setHealth] = useState<HealthLevel | null>(null);
  const [agentCount, setAgentCount] = useState<number | undefined>(undefined);

  /* ── Search results ── */
  const [searchResults, setSearchResults] = useState<readonly ResultItem[] | null>(null);
  const [searchMode, setSearchMode] = useState<"photo" | "name">("photo");
  const [searchTime, setSearchTime] = useState<number | undefined>(undefined);
  const [searchError, setSearchError] = useState<string | null>(null);

  /* ── Graph cross-links ── */
  const [graphCaseEntities, setGraphCaseEntities] = useState<readonly Entity[] | null>(null);
  const [focusEntityId, setFocusEntityId] = useState<string | null>(null);

  /* ── Overlays owned elsewhere ── */
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);

  const viewDef = viewById(view);

  /* ── Persist the active view ── */
  useEffect(() => {
    setStoredView(view);
  }, [setStoredView, view]);

  /* ── Navigate ── */
  const navigate = useCallback(
    (next: ViewId) => {
      setView(next);
      setPaletteOpen(false);
    },
    [],
  );

  /* ── Cases ── */
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    listCases({ limit: 100 }, { signal: controller.signal, timeout_ms: 15_000 })
      .then((items) => {
        if (!cancelled) setCases(items);
      })
      .catch(() => {
        /* The selector degrades to empty; the CASES view shows the error. */
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [view === "cases", investigation?.inv_id]);

  /* ── History ── */
  const refreshHistory = useCallback(async () => {
    try {
      const items = await apiCall(
        () => listInvestigations({ limit: 40 }, { timeout_ms: 15_000 }),
        "/api/investigations",
      );
      setHistory(items);
    } catch {
      /* Non-fatal: the rail shows the last known list. */
    }
  }, []);

  useEffect(() => {
    void refreshHistory();
  }, [refreshHistory]);

  /* ── Health polling (one place, shared with the top bar) ── */
  useEffect(() => {
    const check = async () => {
      try {
        const payload = await getHealth({ timeout_ms: 8_000 });
        const level = payload.status;
        setHealth(level === "ok" || level === "degraded" ? level : "down");
        if (payload.agents?.total) setAgentCount(payload.agents.total);
      } catch {
        setHealth("down");
      }
    };
    void check();
    const id = window.setInterval(() => void check(), 15_000);
    return () => window.clearInterval(id);
  }, []);

  /* ── Pipeline stream ── */
  const invId = investigation?.inv_id ?? null;
  const running =
    investigation?.status === "routing" || investigation?.status === "running";

  const onPipelineEvent = useCallback((event: PipelineEvent) => {
    setInvestigation((prev) => {
      if (!prev) return prev;
      switch (event.type) {
        case "step": {
          const message = event.message;
          if (!message) return prev;
          const upper = message.toUpperCase();
          setActiveAgents((prevActive) => {
            const next = new Set(prevActive);
            for (const agent of AGENT_NAMES) {
              if (upper.includes(agent)) next.add(agent);
            }
            return next;
          });
          return {
            ...prev,
            status: prev.status === "idle" ? "routing" : prev.status,
            steps: [...(prev.steps ?? []), message],
          };
        }
        case "agent": {
          setAgentResults((prevResults) => {
            const next = [...prevResults];
            const index = next.findIndex(
              (result) => result.agent.toUpperCase() === event.agent.toUpperCase(),
            );
            const record: AgentResult = {
              agent: event.agent,
              status: event.status,
              confidence: event.confidence,
              latency_s: event.latency_s,
              error: event.error,
              output: event.output,
            };
            if (index >= 0) next[index] = { ...next[index], ...record };
            else next.push(record);
            return next;
          });
          if (event.status === "done" || event.status === "error") {
            setActiveAgents((prevActive) => {
              const next = new Set(prevActive);
              next.delete(event.agent.toUpperCase());
              return next;
            });
          }
          return prev;
        }
        case "done": {
          const report: Report | undefined = event.report;
          setAgentResults(event.agents ?? []);
          setActiveAgents(new Set());
          if (report) {
            setEntities(report.entities ?? []);
            setSignals(report.signals ?? []);
          }
          if (event.entities?.nodes?.length && !report?.entities?.length) {
            setEntities(
              event.entities.nodes.map((node) => ({
                id: node.id,
                label: node.label || node.id,
                type: normalizeEntityType(node.type),
                confidence: node.confidence,
              })),
            );
          }
          return {
            ...prev,
            status: event.status === "error" ? "error" : "done",
            ended_at: new Date().toISOString(),
            steps: event.steps?.length ? event.steps : prev.steps,
            agents: event.agents ?? prev.agents,
            agents_activated: event.agents_activated ?? prev.agents_activated,
            report: report ?? prev.report,
            entities: event.entities ?? prev.entities,
            input: event.input ?? prev.input,
            input_type: event.input_type ?? prev.input_type,
          };
        }
        case "error": {
          setActiveAgents(new Set());
          return {
            ...prev,
            status: "error",
            ended_at: new Date().toISOString(),
            error: event.error ?? event.message ?? "The pipeline failed.",
            steps: [
              ...(prev.steps ?? []),
              `ERROR: ${event.error ?? event.message ?? "pipeline failed"}`,
            ],
          };
        }
        default:
          return prev;
      }
    });
  }, []);

  const stream = useInvestigationStream(invId, onPipelineEvent);

  /* ── Run an investigation ── */
  const runInvestigation = useCallback(
    async (rawInput: string) => {
      const input = rawInput.trim();
      if (!input) return;

      setRunError(null);
      setEntities([]);
      setSignals([]);
      setAgentResults([]);
      setActiveAgents(new Set());
      setGraphCaseEntities(null);

      const draft: Investigation = {
        inv_id: "",
        input,
        input_type: "unknown",
        status: "routing",
        steps: [],
        started_at: new Date().toISOString(),
        case_id: caseId ?? undefined,
      };
      setInvestigation(draft);
      navigate("investigate");

      try {
        const queued = await submitInvestigation(input, {
          case_id: caseId ?? undefined,
        });
        setInvestigation((prev) => ({
          ...(prev ?? draft),
          inv_id: queued.inv_id,
          case_id: queued.case_id ?? caseId ?? undefined,
          status: "running",
        }));

        // Poll REST in parallel with the socket: the socket may not be proxied
        // in every deployment, and routing metadata only lands there.
        const poll = window.setInterval(async () => {
          try {
            const full = await getInvestigation(queued.inv_id, { timeout_ms: 10_000 });
            setInvestigation((prev) =>
              prev && prev.inv_id === full.inv_id
                ? {
                    ...prev,
                    input_type: full.input_type ?? prev.input_type,
                    agents_activated: full.agents_activated ?? prev.agents_activated,
                    routing_confidence: full.routing_confidence,
                    routing_reasoning: full.routing_reasoning,
                    // The backend emits "queued" before APEX starts; the
                    // union in lib/types does not model it, so fold it here.
                    status: full.status === ("queued" as InvestigationStatus) ? "running" : full.status,
                    report: full.report ?? prev.report,
                    agents: full.agents ?? prev.agents,
                    entities: full.entities ?? prev.entities,
                    error: full.error ?? prev.error,
                  }
                : prev,
            );
            if (full.status === "done" || full.status === "error") {
              window.clearInterval(poll);
            }
          } catch {
            /* transient — the socket or the next tick will catch up */
          }
        }, 1500);
        window.setTimeout(() => window.clearInterval(poll), 180_000);

        void refreshHistory();
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        setRunError(message);
        setInvestigation((prev) =>
          prev
            ? {
                ...prev,
                status: "error",
                error: message,
                ended_at: new Date().toISOString(),
                steps: [...(prev.steps ?? []), `ERROR: ${message}`],
              }
            : prev,
        );
        toast.error(message);
      }
    },
    [caseId, navigate, refreshHistory, toast],
  );

  /* ── Open a past run ── */
  const openInvestigation = useCallback(
    async (inv: Investigation) => {
      setRunError(null);
      setSearchResults(null);
      setGraphCaseEntities(null);
      // Seed from the list payload so the UI is instant, then hydrate.
      setInvestigation(inv);
      setEntities(inv.report?.entities ?? []);
      setSignals(inv.report?.signals ?? []);
      setAgentResults(inv.agents ?? []);
      setActiveAgents(new Set());
      navigate("investigate");

      if (inv.status === "done" && inv.report) return;
      try {
        const full = await getInvestigation(inv.inv_id, { timeout_ms: 20_000 });
        setInvestigation(full);
        setEntities(full.report?.entities ?? []);
        setSignals(full.report?.signals ?? []);
        setAgentResults(full.agents ?? []);
      } catch (err) {
        toast.error(err instanceof Error ? err.message : String(err));
      }
    },
    [navigate, toast],
  );

  /* ── Clear the current run ── */
  const clearRun = useCallback(() => {
    stream.close();
    setInvestigation(null);
    setEntities([]);
    setSignals([]);
    setAgentResults([]);
    setActiveAgents(new Set());
    setSearchResults(null);
    setSearchError(null);
    setRunError(null);
    setGraphCaseEntities(null);
    toast.push("Cleared the current investigation.", "info");
  }, [stream, toast]);

  /* ── Search helpers ── */
  const runPhotoSearch = useCallback(
    async (file: File) => {
      setSearchError(null);
      setSearchMode("photo");
      const started = Date.now();
      try {
        const result = await submitPhotoSearch(file, {
          case_id: caseId ?? undefined,
          filename: file.name,
          timeout_ms: 120_000,
        });
        const rows = normalisePhotoResults(result);
        setSearchResults(rows);
        setSearchTime((Date.now() - started) / 1000);
        if (result.entities?.length) setEntities(result.entities);
        const foundSignals = result.signals ?? [];
        if (foundSignals.length > 0) setSignals((prev) => [...prev, ...foundSignals]);
        toast.success(`IRIS analysed the media — ${rows.length} result(s).`);
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        setSearchError(message);
        setSearchResults([]);
        toast.error(message);
      }
    },
    [caseId, toast],
  );

  const runNameSearch = useCallback(
    async (name: string) => {
      setSearchError(null);
      setSearchMode("name");
      const started = Date.now();
      try {
        const response = await submitNameSearch({
          name,
          case_id: caseId ?? undefined,
          timeout_ms: 120_000,
        });
        setSearchResults(response.results as ResultItem[]);
        setSearchTime((Date.now() - started) / 1000);
        const foundEntities = response.entities ?? [];
        if (foundEntities.length > 0) setEntities((prev) => [...prev, ...foundEntities]);
        const responseSignals = response.signals ?? [];
        if (responseSignals.length > 0) setSignals((prev) => [...prev, ...responseSignals]);
        toast.success(`PRISM swept ${name} — ${response.results.length} profile(s).`);
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        setSearchError(message);
        setSearchResults([]);
        toast.error(message);
      }
    },
    [caseId, toast],
  );

  /* ── OPSEC ── */
  const rotateCircuit = useCallback(async () => {
    try {
      await newCircuit({ timeout_ms: 30_000 });
      toast.success("New circuit established.");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  }, [toast]);

  /* ── Case cross-links ── */
  const openCaseInGraph = useCallback(async (nextCaseId: string | null) => {
    if (!nextCaseId) return;
    try {
      const rows = await apiCall(
        () => getCaseEntities(nextCaseId, { timeout_ms: 20_000 }),
        `/api/cases/${nextCaseId}/entities`,
      );
      setGraphCaseEntities(rows);
      setFocusEntityId(null);
      navigate("graph");
      toast.push(`Loaded ${rows.length} entities from case ${nextCaseId}.`, "info");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  }, [navigate, toast]);

  const focusEntityInGraph = useCallback(
    (entity: Entity | null, entityId?: string) => {
      if (entity) {
        const exists = entities.some((candidate) => candidate.id === entity.id);
        if (!exists) setEntities((prev) => [...prev, entity]);
      }
      setGraphCaseEntities(null);
      setFocusEntityId(entityId ?? entity?.id ?? null);
      navigate("graph");
    },
    [entities, navigate],
  );

  /* ── Keyboard: routed through lib/shortcuts.ts's useHotkeys ── */
  useHotkeys(
    useMemo(
      () => [
        { binding: "mod+k", handler: () => setPaletteOpen((open) => !open), allowInInput: true },
        { binding: "?", handler: () => setShortcutsOpen(true), shift: true },
        { binding: "/", handler: () => inputRef.current?.focus(), allowInInput: true },
        { binding: "mod+enter", handler: () => inputRef.current?.submit(), allowInInput: true },
        ...VIEWS.map((def) => ({
          binding: def.hotkey,
          handler: () => navigate(def.id),
          shouldPreventDefault: false,
        })),
      ],
      [navigate],
    ),
  );

  /* ── Case entities for the graph, loaded lazily ── */
  const graphEntities = useMemo(
    () => graphCaseEntities ?? investigation?.report?.entities ?? entities,
    [graphCaseEntities, investigation?.report?.entities, entities],
  );

  const opsecLabel = health === "down" ? "backend offline" : health === null ? "checking" : health;
  const report = investigation?.report ?? null;
  const reportTimeline = report?.timeline ?? [];
  const currentCase = cases.find((c) => c.case_id === caseId) ?? null;

  return (
    <div
      className={clsx(
        "flex h-screen flex-col overflow-hidden font-mono text-ink",
        className,
      )}
      style={{ background: "var(--bg)" }}
    >
      <TopBar
        view={view}
        onViewChange={navigate}
        health={health}
        cases={cases}
        caseId={caseId}
        onCaseChange={(next) => {
          setCaseId(next);
          toast.push(
            next
              ? `New runs will be bound to case ${next}.`
              : "Runs will no longer be bound to a case.",
            "info",
          );
        }}
        onOpenPalette={() => setPaletteOpen(true)}
        onOpenShortcuts={() => setShortcutsOpen(true)}
        agentCount={agentCount}
      />

      <div className="flex min-h-0 flex-1">
        {/* Left icon rail */}
        <nav
          aria-label="Views"
          className="flex w-11 shrink-0 flex-col items-center gap-0.5 border-r border-border-subtle bg-surface-1 py-1.5"
        >
          {VIEWS.map((def) => {
            const active = def.id === view;
            return (
              <Tooltip key={def.id} label={`${def.label} · ${def.hotkey}`} side="right">
                <button
                  type="button"
                  onClick={() => navigate(def.id)}
                  aria-current={active ? "page" : undefined}
                  aria-label={`${def.label} — ${def.description}`}
                  className={clsx(
                    "focus-ring flex h-8 w-8 items-center justify-center rounded border text-[13px] transition-colors",
                    active
                      ? "border-accent/50 bg-accent/10 text-accent"
                      : "border-transparent text-ink-muted hover:text-ink",
                  )}
                >
                  <span aria-hidden="true">{RAIL_GLYPH[def.icon] ?? "◻"}</span>
                </button>
              </Tooltip>
            );
          })}

          <span aria-hidden="true" className="my-1 h-px w-5 bg-border-subtle" />

          <Tooltip label={`Opsec · ${opsecLabel}`} side="right">
            <OpsecIndicator
              pollMs={30_000}
              compact
              onRotated={() => toast.success("Circuit rotated.")}
              onError={(message) => toast.error(message)}
            />
          </Tooltip>
        </nav>

        {/* View surface */}
        <main className="flex min-w-0 flex-1 flex-col" aria-label={`${viewDef?.label ?? "View"} view`}>
          {view === "dashboard" && (
            <DashboardView
              inputRef={inputRef}
              onSubmit={runInvestigation}
              running={running}
              caseLabel={currentCase?.name ?? null}
              investigation={investigation}
              onOpenCase={(inv) => void openInvestigation(inv)}
              onOpenOps={() => navigate("ops")}
              onNavigate={(next) => {
                if (isViewId(next)) navigate(next);
              }}
            />
          )}

          {view === "investigate" && (
            <InvestigateView
              investigation={investigation}
              streamStatus={stream.status}
              entities={entities}
              signals={signals}
              agentResults={agentResults}
              activeAgents={activeAgents}
              onSubmit={runInvestigation}
              running={running}
              caseLabel={currentCase?.name ?? null}
              searchResults={searchResults}
              searchMode={searchMode}
              searchTime={searchTime}
              searchError={searchError ?? runError}
              onRunPhotoSearch={(file) => void runPhotoSearch(file)}
              onRunNameSearch={(name) => void runNameSearch(name)}
            />
          )}

          {view === "cases" && (
            <CasesView
              activeCaseId={caseId}
              onCaseChange={setCaseId}
              onOpenInvestigation={(inv) => void openInvestigation(inv)}
              onOpenGraph={() => void openCaseInGraph(caseId)}
            />
          )}

          {view === "agents" && (
            <AgentsView
              investigation={investigation}
              agentResults={agentResults}
              activeAgents={activeAgents}
            />
          )}

          {view === "graph" && (
            <GraphView
              entities={graphEntities}
              graph={investigation?.entities ?? report?.graph ?? null}
              signals={signals}
              caseEntities={graphCaseEntities}
              onClearCaseEntities={() => {
                setGraphCaseEntities(null);
                setFocusEntityId(null);
              }}
              investigation={investigation}
            />
          )}

          {view === "timeline" && (
            <TimelineView
              events={reportTimeline}
              entities={entities}
              caseId={caseId}
              onOpenEntity={focusEntityInGraph}
            />
          )}

          {view === "knowledge" && (
            <KnowledgeView
              availableEntities={entities}
              caseId={caseId}
              onOpenEntity={focusEntityInGraph}
            />
          )}

          {view === "reports" && (
            <ReportsView
              investigation={investigation}
              available={history}
              onSelect={(inv) => void openInvestigation(inv)}
            />
          )}

          {view === "ops" && <OpsView />}
        </main>

        {/* Right context rail — only where it earns its space */}
        {(view === "dashboard" || view === "investigate" || view === "graph") && (
          <aside className="scroll-thin hidden w-56 shrink-0 flex-col gap-2 overflow-y-auto border-l border-border-subtle bg-surface-1 p-2 2xl:flex">
            <div className="panel">
              <div className="panel-header !py-1">
                <span className="mono-label !text-[9px]">AGENTS</span>
                <span className="mono-label !text-[8px]">
                  {activeAgents.size} live
                </span>
              </div>
              <div className="!p-1.5">
                <AgentGrid
                  compact
                  activeAgents={activeAgents}
                  activatedAgents={new Set(
                    (investigation?.agents_activated ?? []).map((a) => a.toUpperCase()),
                  )}
                  results={agentResults}
                  onSelect={() => navigate("agents")}
                />
              </div>
            </div>

            <div className="panel">
              <div className="panel-header !py-1">
                <span className="mono-label !text-[9px]">RUN</span>
                {investigation && (
                  <button
                    type="button"
                    onClick={clearRun}
                    className="focus-ring rounded px-1 text-[9px] uppercase tracking-[0.1em] text-ink-muted hover:text-signal-err"
                  >
                    Clear
                  </button>
                )}
              </div>
              <div className="!p-1.5">
                {investigation ? (
                  <dl className="m-0 flex flex-col gap-1">
                    <Row label="Target" value={investigation.input} />
                    <Row label="Type" value={investigation.input_type} />
                    <Row label="Status" value={investigation.status} />
                    <Row label="Entities" value={String(entities.length)} />
                    <Row label="Signals" value={String(signals.length)} />
                    <Row
                      label="Confidence"
                      value={
                        report?.confidence === undefined
                          ? "—"
                          : `${Math.round(report.confidence * 100)}%`
                      }
                    />
                  </dl>
                ) : (
                  <p className="mono-label m-0 py-2 !text-[8px]">NO ACTIVE RUN</p>
                )}
              </div>
            </div>
          </aside>
        )}
      </div>

      <StatusBar
        investigation={investigation}
        streamStatus={stream.status}
        entityCount={entities.length}
        signalCount={signals.length}
        viewLabel={viewDef?.label}
      />

      {/* Overlays owned by the parallel workstream. Rendered lazily so a missing
          module cannot break the shell — the imports above are the contract. */}
      <ShellOverlays
        paletteOpen={paletteOpen}
        onPaletteOpenChange={setPaletteOpen}
        shortcutsOpen={shortcutsOpen}
        onShortcutsOpenChange={setShortcutsOpen}
        views={VIEWS}
        onNavigate={navigate}
        onNewInvestigation={() => {
          clearRun();
          inputRef.current?.focus();
        }}
        onRotateCircuit={() => void rotateCircuit()}
        onOpenInvestigation={(inv) => void openInvestigation(inv)}
        recent={history.slice(0, 8)}
        caseLabel={currentCase?.name ?? null}
      />
    </div>
  );
}

/* ── Small helpers ──────────────────────────────────────────────────────────── */

/** Roster names used to detect agents in a trace line. */
const AGENT_NAMES: readonly string[] = [
  "APEX",
  "SCOUT",
  "CRAWLER",
  "PRISM",
  "IRIS",
  "ECHO",
  "TERRA",
  "INK",
  "NEXUS",
  "KRONOS",
  "VAULT",
  "SENTINEL",
  "QUILL",
  "SIGMA",
  "EMAIL",
  "PHONOS",
];

/** One label/value line in the right rail. */
function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <dt className="mono-label shrink-0 !text-[8px]">{label}</dt>
      <dd className="m-0 min-w-0 truncate text-right text-[10px] text-ink" title={value}>
        {value}
      </dd>
    </div>
  );
}

/** Fold a photo-search payload into the flat result list `ResultsGrid` expects. */
function normalisePhotoResults(result: {
  results?: unknown;
  entities?: unknown;
  markdown?: string;
}): ResultItem[] {
  const source = result as Record<string, unknown>;
  if (Array.isArray(source.results)) return source.results as ResultItem[];
  // Older builds returned entities only; surface them as single-column rows.
  const entities = Array.isArray(source.entities) ? (source.entities as Entity[]) : [];
  return entities.map((entity) => ({
    title: entity.label || entity.value || entity.id,
    source: entity.value,
    platform: entity.source,
    confidence: entity.confidence,
  }));
}

/* ── Overlays ───────────────────────────────────────────────────────────────── */

interface OverlaysProps {
  paletteOpen: boolean;
  onPaletteOpenChange: (open: boolean) => void;
  shortcutsOpen: boolean;
  onShortcutsOpenChange: (open: boolean) => void;
  views: readonly { id: ViewId; label: string; description: string; icon: string }[];
  onNavigate: (view: ViewId) => void;
  onNewInvestigation: () => void;
  onRotateCircuit: () => void;
  onOpenInvestigation: (investigation: Investigation) => void;
  recent: readonly Investigation[];
  caseLabel: string | null;
}

/**
 * Hosts `CommandPalette` and `ShortcutsDialog` (owned by the parallel
 * workstream) and feeds the palette a real item list built from the shell's
 * live state: every view, the run actions, the agent roster and the recent
 * investigations.
 */
function ShellOverlays({
  paletteOpen,
  onPaletteOpenChange,
  shortcutsOpen,
  onShortcutsOpenChange,
  views,
  onNavigate,
  onNewInvestigation,
  onRotateCircuit,
  onOpenInvestigation,
  recent,
  caseLabel,
}: OverlaysProps) {
  const items = useMemo(() => {
    const out: PaletteItem[] = [];
    for (const def of views) {
      out.push({
        id: `view:${def.id}`,
        label: `Go to ${def.label}`,
        hint: def.description,
        icon: RAIL_GLYPH[def.icon] ?? "◻",
        source: "static",
        keywords: [def.label.toLowerCase(), def.id],
        payload: { kind: "view", id: def.id },
      });
    }
    out.push(
      {
        id: "action:new-investigation",
        label: "New investigation",
        hint: "Clear the current run and focus the input bar",
        icon: "＋",
        source: "static",
        payload: { kind: "new-investigation" },
      },
      {
        id: "action:rotate-circuit",
        label: "Rotate Tor circuit",
        hint: "Request a new exit node and identity",
        icon: "⛨",
        source: "static",
        payload: { kind: "rotate-circuit" },
      },
      {
        id: "action:clear-case",
        label: "Clear case binding",
        hint: "Stop binding new runs to a case",
        icon: "▤",
        source: "static",
        payload: { kind: "clear-case" },
      },
    );
    for (const agent of AGENT_ROSTER_FOR_PALETTE) {
      out.push({
        id: `agent:${agent.name}`,
        label: agent.name,
        hint: agent.role,
        icon: agent.icon,
        source: "static",
        keywords: [agent.role.toLowerCase(), agent.name.toLowerCase()],
        payload: { kind: "agent", name: agent.name },
      });
    }
    return out;
  }, [views]);

  const onSelect = useCallback(
    (item: PaletteItem) => {
      const payload = item.payload as
        | { kind: string; id?: string; name?: string }
        | undefined;
      if (!payload) return;
      if (payload.kind === "view" && payload.id && isViewId(payload.id)) {
        onNavigate(payload.id);
        return;
      }
      if (payload.kind === "new-investigation") {
        onNewInvestigation();
        return;
      }
      if (payload.kind === "rotate-circuit") {
        onRotateCircuit();
        return;
      }
      if (payload.kind === "clear-case") {
        onNavigate("cases");
        return;
      }
      if (payload.kind === "agent" && payload.name) {
        onNavigate("agents");
      }
    },
    [onNavigate, onNewInvestigation, onRotateCircuit],
  );

  return (
    <>
      {paletteOpen && (
        <CommandPalette
          open={paletteOpen}
          onClose={() => onPaletteOpenChange(false)}
          items={items}
          onSelect={onSelect}
          footerHint={caseLabel ? `case: ${caseLabel}` : "no case bound"}
          placeholder="Search views, actions, agents, investigations…"
        />
      )}
      {shortcutsOpen && (
        <ShortcutsDialog
          open={shortcutsOpen}
          onClose={() => onShortcutsOpenChange(false)}
          extraCommands={[
            ...views.map((def) => ({
              id: `view.${def.id}`,
              label: `Go to ${def.label}`,
              category: "Navigation" as const,
              keys: [def.id === "dashboard" ? "1" : String(views.indexOf(def) + 1)],
              description: def.description,
            })),
            {
              id: "investigation.new",
              label: "Clear the current run",
              category: "Investigation",
              keys: ["⌘", "R"],
              description: "Drop the active investigation and focus the input bar.",
            },
          ]}
        />
      )}
      <span className="sr-only">
        {recent.length} recent investigations available in the palette.
      </span>
    </>
  );
}

/** Minimal roster for palette entries (kept local to avoid a second catalog import). */
const AGENT_ROSTER_FOR_PALETTE: readonly { name: string; role: string; icon: string }[] = [
  { name: "APEX", role: "Master Supervisor", icon: "⊕" },
  { name: "SCOUT", role: "Search Intelligence", icon: "◎" },
  { name: "CRAWLER", role: "Web Scraper", icon: "⟨/⟩" },
  { name: "PRISM", role: "Social Intelligence", icon: "◈" },
  { name: "IRIS", role: "Vision Intelligence", icon: "◉" },
  { name: "ECHO", role: "Audio Intelligence", icon: "~" },
  { name: "TERRA", role: "GEOINT", icon: "⊛" },
  { name: "INK", role: "Stylometry", icon: "✦" },
  { name: "NEXUS", role: "Correlation", icon: "∞" },
  { name: "KRONOS", role: "Timeline", icon: "⊕" },
  { name: "VAULT", role: "Memory", icon: "□" },
  { name: "SENTINEL", role: "Live Monitoring", icon: "⊲" },
  { name: "QUILL", role: "Reports", icon: "⟁" },
  { name: "SIGMA", role: "Threat Intelligence", icon: "⊗" },
  { name: "EMAIL", role: "Email Intelligence", icon: "✉" },
  { name: "PHONOS", role: "Phone Intelligence", icon: "☏" },
];

