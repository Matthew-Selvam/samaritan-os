"use client";

/**
 * InvestigateView.tsx — the deep-work screen.
 *
 * Owns nothing but presentation: the shell owns the investigation object and
 * the live WebSocket (via `useInvestigationStream`), so switching views never
 * drops the stream. Tabs cover TRACE / AGENTS / ENTITIES / SIGNALS / RESULTS /
 * PHOTO, and each one degrades on its own — one agent erroring shows a red badge
 * in the AGENTS tab and leaves the other five untouched.
 */

import { useCallback, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import {
  Badge,
  Button,
  DefRow,
  Drawer,
  EmptyState,
  ErrorState,
  Meter,
  Panel,
  SectionTitle,
  Tabs,
  Table,
  TextInput,
  useToast,
  type Column,
} from "@/components/ui";
import { InputBar, type InputBarHandle } from "@/components/InputBar";
import { PipelineTrace } from "@/components/PipelineTrace";
import { AgentGrid } from "@/components/AgentGrid";
import { EntityBadge } from "@/components/EntityBadge";
import { PhotoUpload } from "@/components/PhotoUpload";
import { ResultsGrid, type ResultItem } from "@/components/ResultsGrid";
import { colorFor, ENTITY_TYPE_META, labelFor, normalizeEntityType } from "@/lib/entityTypes";
import {
  agentTone,
  confidencePct,
  formatRelativeTime,
  formatSeconds,
  statusLabel,
  statusTone,
  truncate,
  type Tone,
} from "@/lib/format";
import type {
  AgentResult,
  AgentStatus,
  Entity,
  Investigation,
  Signal,
} from "@/lib/types";
import { useLocalStorage } from "@/lib/hooks";
import type { StreamStatus } from "@/lib/useInvestigationStream";

/** Tab identifiers. */
export type InvestigateTab =
  | "trace"
  | "agents"
  | "entities"
  | "signals"
  | "results"
  | "photo";

export interface InvestigateViewProps {
  /**
   * Ref for the input bar. The shell owns it so `/` and ⌘↵ work from anywhere,
   * which means this view must forward it rather than keeping a local ref the
   * shell cannot reach.
   */
  inputRef?: React.Ref<InputBarHandle>;
  investigation: Investigation | null;
  /** Current WebSocket state, shown in the header. */
  streamStatus: StreamStatus;
  /** Entities for the current run, from the report or the live graph. */
  entities: readonly Entity[];
  /** Signals for the current run. */
  signals: readonly Signal[];
  /** Per-agent results, for the AGENTS tab and the detail drawer. */
  agentResults: readonly AgentResult[];
  /** Agents APEX activated, uppercased. */
  activeAgents: ReadonlySet<string>;
  onSubmit: (value: string) => void;
  running: boolean;
  caseLabel?: string | null;
  /** Search / name-search results shown on the RESULTS tab. */
  searchResults?: readonly ResultItem[] | null;
  searchMode?: "photo" | "name";
  searchTime?: number;
  /** Non-null when a search failed. */
  searchError?: string | null;
  onRunPhotoSearch?: (file: File) => void;
  onRunNameSearch?: (name: string) => void;
  className?: string;
}

/** Column set for the entity table: type, label, value, confidence, source. */
const ENTITY_COLUMNS: Column<Entity>[] = [
  {
    key: "type",
    header: "Type",
    width: "128px",
    sortValue: (row: Entity) => labelFor(row.type),
    render: (row: Entity) => <EntityBadge type={row.type} />,
  },
  {
    key: "label",
    header: "Entity",
    sortValue: (row: Entity) => (row.label || row.value || row.id).toLowerCase(),
    render: (row: Entity) => (
      <span className="block min-w-0 truncate" title={row.label || row.value || row.id}>
        {row.label || row.value || row.id}
      </span>
    ),
  },
  {
    key: "value",
    header: "Value",
    hideBelow: "md",
    sortValue: (row: Entity) => (row.value ?? "").toLowerCase(),
    render: (row: Entity) => (
      <span className="block min-w-0 truncate text-ink-muted" title={row.value}>
        {row.value ?? "—"}
      </span>
    ),
  },
  {
    key: "confidence",
    header: "Conf.",
    width: "84px",
    align: "right",
    sortValue: (row: Entity) => row.confidence ?? -1,
    render: (row: Entity) =>
      row.confidence === undefined ? (
        "—"
      ) : (
        <span className="inline-flex items-center justify-end gap-1.5 tabular-nums">
          <Meter
            value={row.confidence}
            tone={row.confidence >= 0.7 ? "ok" : row.confidence >= 0.4 ? "warn" : "err"}
            height={3}
            className="w-8"
            label="Entity confidence"
          />
          {confidencePct(row.confidence)}
        </span>
      ),
  },
  {
    key: "source",
    header: "Source",
    width: "96px",
    hideBelow: "lg",
    sortValue: (row: Entity) => (row.source ?? "").toLowerCase(),
    render: (row: Entity) => (
      <span className="mono-label !text-[9px]">{row.source ?? "—"}</span>
    ),
  },
];

/** Tone for a terminal agent status, including live states. */
function agentToneFor(status: AgentStatus | "running" | "idle"): Tone {
  if (status === "running") return "info";
  if (status === "idle") return "idle";
  return agentTone(status);
}

/** Short status label for an agent. */
function agentStatusText(status: AgentStatus | "running" | "idle"): string {
  if (status === "running") return "RUNNING";
  if (status === "idle") return "IDLE";
  return status.toUpperCase();
}

/**
 * The deep-work screen.
 *
 * @example
 * <InvestigateView investigation={inv} entities={entities} onSubmit={run} running={busy} />
 */
export function InvestigateView({
  inputRef,
  investigation,
  streamStatus,
  entities,
  signals,
  agentResults,
  activeAgents,
  onSubmit,
  running,
  caseLabel = null,
  searchResults = null,
  searchMode = "photo",
  searchTime,
  searchError = null,
  onRunPhotoSearch,
  onRunNameSearch,
  className,
}: InvestigateViewProps) {
  const [tab, setTab] = useLocalStorage<InvestigateTab>("signal-os:investigate-tab", "trace");
  const [entityQuery, setEntityQuery] = useState("");
  const [signalQuery, setSignalQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<string>("");
  const [sourceFilter, setSourceFilter] = useState<string>("");
  const [selectedAgent, setSelectedAgent] = useState<AgentResult | null>(null);
  const [selectedEntity, setSelectedEntity] = useState<Entity | null>(null);
  const [photoFile, setPhotoFile] = useState<File | null>(null);
  const [photoPreview, setPhotoPreview] = useState<string | null>(null);
  const [nameQuery, setNameQuery] = useState("");
  const toast = useToast();

  /* ── Derived entity/signal lists ── */
  const typeCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const entity of entities) {
      const type = normalizeEntityType(entity.type);
      counts.set(type, (counts.get(type) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [entities]);

  const sourceCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const signal of signals) {
      const key = signal.source || "unknown";
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [signals]);

  const filteredEntities = useMemo(() => {
    const needle = entityQuery.trim().toLowerCase();
    return entities.filter((entity) => {
      if (typeFilter && normalizeEntityType(entity.type) !== typeFilter) return false;
      if (!needle) return true;
      return (
        entity.label.toLowerCase().includes(needle) ||
        (entity.value ?? "").toLowerCase().includes(needle) ||
        (entity.source ?? "").toLowerCase().includes(needle)
      );
    });
  }, [entities, entityQuery, typeFilter]);

  const filteredSignals = useMemo(() => {
    const needle = signalQuery.trim().toLowerCase();
    return signals.filter((signal) => {
      if (sourceFilter && signal.source !== sourceFilter) return false;
      if (!needle) return true;
      return (
        signal.type.toLowerCase().includes(needle) ||
        (signal.source ?? "").toLowerCase().includes(needle) ||
        JSON.stringify(signal.value ?? "").toLowerCase().includes(needle)
      );
    });
  }, [signals, signalQuery, sourceFilter]);

  const failedAgents = useMemo(
    () => agentResults.filter((agent) => agent.status === "error"),
    [agentResults],
  );
  const partialAgents = useMemo(
    () => agentResults.filter((agent) => agent.status === "partial"),
    [agentResults],
  );

  const tabs: { id: InvestigateTab; label: string; count?: number }[] = [
    { id: "trace", label: "Trace", count: investigation?.steps?.length },
    { id: "agents", label: "Agents", count: agentResults.length || investigation?.agents_activated?.length },
    { id: "entities", label: "Entities", count: entities.length },
    { id: "signals", label: "Signals", count: signals.length },
    { id: "results", label: "Results", count: searchResults?.length },
    { id: "photo", label: "Photo" },
  ];

  const onPhotoFile = useCallback((file: File | null) => {
    setPhotoFile(file);
    if (photoPreview) {
      URL.revokeObjectURL(photoPreview);
      setPhotoPreview(null);
    }
    if (file) {
      setPhotoPreview(URL.createObjectURL(file));
    }
  }, [photoPreview]);

  const runPhoto = useCallback(() => {
    if (!photoFile) {
      toast.error("Select a file first.");
      return;
    }
    onRunPhotoSearch?.(photoFile);
  }, [onRunPhotoSearch, photoFile, toast]);

  const runName = useCallback(() => {
    const name = nameQuery.trim();
    if (!name) {
      toast.error("Enter a name to search.");
      return;
    }
    onRunNameSearch?.(name);
  }, [nameQuery, onRunNameSearch, toast]);

  return (
    <div className={clsx("flex min-h-0 flex-1 flex-col", className)}>
      {/* ── Input ── */}
      <div className="border-b border-border-subtle bg-surface-1 px-3 py-2">
        <InputBar ref={inputRef} onSubmit={onSubmit} running={running} caseLabel={caseLabel} />
      </div>

      {/* ── Tabs ── */}
      <Tabs items={tabs} active={tab} onChange={setTab} label="Investigation views" actions={
        <>
          {failedAgents.length > 0 && (
            <Badge tone="err" title="These agents failed; the rest of the run is unaffected">
              {failedAgents.length} FAILED
            </Badge>
          )}
          {partialAgents.length > 0 && (
            <Badge tone="warn">{partialAgents.length} PARTIAL</Badge>
          )}
          <Badge tone={streamStatus === "open" ? "ok" : streamStatus === "error" ? "err" : "idle"}>
            {streamStatus.toUpperCase()}
          </Badge>
        </>
      } />

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto p-3">
        {!investigation ? (
          <EmptyState
            glyph="⊕"
            title="NO ACTIVE INVESTIGATION"
            description="Enter a target above — email, username, domain, IP, wallet, URL, image, or a document — and press ⌘↵."
          />
        ) : (
          <>
            {tab === "trace" && (
              <div className="flex flex-col gap-3">
                {investigation.error && (
                  <Panel className="border-signal-err/40">
                    <p role="alert" className="m-0 break-words text-[11px] text-signal-err">
                      <span className="mono-label mr-1 !text-[9px]">RUN ERROR</span>
                      {investigation.error}
                    </p>
                  </Panel>
                )}
                <PipelineTrace investigation={investigation} />
              </div>
            )}

            {tab === "agents" && (
              <div className="flex flex-col gap-3">
                <Panel
                  eyebrow="ROSTER"
                  title={`${investigation.agents_activated?.length ?? 0} activated`}
                  actions={
                    <Badge tone="idle" title="Agents reported by the backend">
                      {agentResults.length} reported
                    </Badge>
                  }
                >
                  <AgentGrid
                    activeAgents={activeAgents}
                    activatedAgents={new Set(
                      (investigation.agents_activated ?? []).map((a) => a.toUpperCase()),
                    )}
                    results={agentResults}
                    onSelect={(name) => {
                      const match = agentResults.find(
                        (a) => a.agent.toUpperCase() === name.toUpperCase(),
                      );
                      if (match) setSelectedAgent(match);
                      else
                        toast.push(
                          `${name} has not reported yet — it may not have been activated for this input.`,
                          "idle",
                        );
                    }}
                  />
                </Panel>

                {agentResults.length > 0 && (
                  <Panel eyebrow="RESULTS" title="Agent results" bodyClassName="!p-0">
                    <Table
                      columns={[
                        {
                          key: "agent",
                          header: "Agent",
                          width: "110px",
                          sortValue: (row) => row.agent.toUpperCase(),
                          render: (row) => (
                            <span className="font-bold tracking-wide">{row.agent}</span>
                          ),
                        },
                        {
                          key: "status",
                          header: "Status",
                          width: "92px",
                          sortValue: (row) => row.status,
                          render: (row) => (
                            <Badge
                              tone={agentToneFor(row.status)}
                              pulse={row.status === "partial"}
                            >
                              {agentStatusText(row.status)}
                            </Badge>
                          ),
                        },
                        {
                          key: "confidence",
                          header: "Conf.",
                          width: "70px",
                          align: "right",
                          sortValue: (row) => row.confidence ?? -1,
                          render: (row) => confidencePct(row.confidence),
                        },
                        {
                          key: "latency",
                          header: "Latency",
                          width: "80px",
                          align: "right",
                          hideBelow: "md",
                          sortValue: (row) => row.latency_s ?? -1,
                          render: (row) => formatSeconds(row.latency_s),
                        },
                        {
                          key: "entities",
                          header: "Ent",
                          width: "56px",
                          align: "right",
                          hideBelow: "lg",
                          sortValue: (row) => row.entities_found?.length ?? 0,
                          render: (row) => row.entities_found?.length ?? 0,
                        },
                        {
                          key: "detail",
                          header: "",
                          width: "80px",
                          align: "right",
                          render: (row) => (
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => setSelectedAgent(row)}
                            >
                              Inspect
                            </Button>
                          ),
                        },
                      ]}
                      rows={agentResults}
                      rowKey={(row) => row.agent}
                      defaultSort={{ key: "confidence", direction: "desc" }}
                      onRowClick={setSelectedAgent}
                      caption="Per-agent results"
                    />
                  </Panel>
                )}

                {agentResults.length === 0 && (
                  <Panel eyebrow="RESULTS" title="Agent results">
                    <EmptyState
                      compact
                      glyph="◌"
                      title={running ? "WAITING FOR AGENTS" : "NO AGENT RESULTS"}
                      description={
                        running
                          ? "Agents report as they finish; the stream is still open."
                          : "This run finished without per-agent results."
                      }
                    />
                  </Panel>
                )}
              </div>
            )}

            {tab === "entities" && (
              <Panel
                eyebrow="GRAPH"
                title={`${filteredEntities.length} of ${entities.length} entities`}
                bodyClassName="!p-0"
                actions={
                  <>
                    {selectedEntity && (
                      <Button size="sm" variant="ghost" onClick={() => setSelectedEntity(null)}>
                        Clear selection
                      </Button>
                    )}
                    <Badge tone="idle">{typeCounts.length} types</Badge>
                  </>
                }
              >
                <div className="flex flex-wrap items-center gap-2 border-b border-border-subtle bg-surface-1 px-2 py-1.5">
                  <div className="min-w-[200px] flex-1">
                    <TextInput
                      value={entityQuery}
                      onChange={(event) => setEntityQuery(event.target.value)}
                      placeholder="Filter entities…"
                      aria-label="Filter entities"
                      className="!py-1 !text-[11px]"
                    />
                  </div>
                  <label className="flex items-center gap-1">
                    <span className="mono-label !text-[8px]">TYPE</span>
                    <select
                      value={typeFilter}
                      onChange={(event) => setTypeFilter(event.target.value)}
                      aria-label="Filter by entity type"
                      className="focus-ring rounded border border-border-strong bg-surface-0 px-1.5 py-1 text-[10px] text-ink"
                    >
                      <option value="">ALL</option>
                      {typeCounts.map(([type, count]) => (
                        <option key={type} value={type}>
                          {labelFor(type)} ({count})
                        </option>
                      ))}
                    </select>
                  </label>
                </div>

                {entities.length === 0 ? (
                  <EmptyState
                    glyph="◉"
                    title={running ? "NO ENTITIES YET" : "NO ENTITIES FOUND"}
                    description={
                      running
                        ? "Entities stream in as agents report."
                        : "This run produced no entities. Try a different input or a deeper sweep."
                    }
                  />
                ) : (
                  <Table
                    columns={ENTITY_COLUMNS}
                    rows={filteredEntities}
                    rowKey={(row) => row.id}
                    onRowClick={setSelectedEntity}
                    isRowActive={(row) => row.id === selectedEntity?.id}
                    maxHeight="60vh"
                    caption="Discovered entities"
                    empty={
                      <EmptyState
                        compact
                        title="NO MATCHES"
                        description="No entity matches the current filter."
                      />
                    }
                  />
                )}
              </Panel>
            )}

            {tab === "signals" && (
              <Panel
                eyebrow="SIGNALS"
                title={`${filteredSignals.length} of ${signals.length} signals`}
                bodyClassName="!p-0"
                actions={<Badge tone="idle">{sourceCounts.length} sources</Badge>}
              >
                <div className="flex flex-wrap items-center gap-2 border-b border-border-subtle bg-surface-1 px-2 py-1.5">
                  <div className="min-w-[200px] flex-1">
                    <TextInput
                      value={signalQuery}
                      onChange={(event) => setSignalQuery(event.target.value)}
                      placeholder="Filter signals…"
                      aria-label="Filter signals"
                      className="!py-1 !text-[11px]"
                    />
                  </div>
                  <label className="flex items-center gap-1">
                    <span className="mono-label !text-[8px]">SOURCE</span>
                    <select
                      value={sourceFilter}
                      onChange={(event) => setSourceFilter(event.target.value)}
                      aria-label="Filter by signal source"
                      className="focus-ring rounded border border-border-strong bg-surface-0 px-1.5 py-1 text-[10px] text-ink"
                    >
                      <option value="">ALL</option>
                      {sourceCounts.map(([source, count]) => (
                        <option key={source} value={source}>
                          {source} ({count})
                        </option>
                      ))}
                    </select>
                  </label>
                </div>

                {signals.length === 0 ? (
                  <EmptyState
                    glyph="≋"
                    title={running ? "NO SIGNALS YET" : "NO SIGNALS COLLECTED"}
                    description={
                      running
                        ? "Signals arrive from connectors as agents run."
                        : "No connector returned a signal for this target."
                    }
                  />
                ) : (
                  <Table
                    columns={[
                      {
                        key: "type",
                        header: "Type",
                        width: "150px",
                        sortValue: (row) => row.type,
                        render: (row) => (
                          <span className="mono-label !text-[9px]">{row.type}</span>
                        ),
                      },
                      {
                        key: "value",
                        header: "Value",
                        sortValue: (row) => JSON.stringify(row.value ?? ""),
                        render: (row) => (
                          <span className="block min-w-0 truncate" title={JSON.stringify(row.value)}>
                            {truncate(
                              typeof row.value === "string"
                                ? row.value
                                : JSON.stringify(row.value ?? ""),
                              160,
                            )}
                          </span>
                        ),
                      },
                      {
                        key: "source",
                        header: "Source",
                        width: "104px",
                        sortValue: (row) => row.source,
                        render: (row) => (
                          <span className="mono-label !text-[9px]">{row.source}</span>
                        ),
                      },
                      {
                        key: "confidence",
                        header: "Conf.",
                        width: "70px",
                        align: "right",
                        sortValue: (row) => row.confidence,
                        render: (row) => confidencePct(row.confidence),
                      },
                      {
                        key: "ts",
                        header: "When",
                        width: "110px",
                        hideBelow: "md",
                        sortValue: (row) => row.ts ?? "",
                        render: (row) => (
                          <span className="mono-label !text-[9px]">
                            {row.ts ? formatRelativeTime(row.ts) : "—"}
                          </span>
                        ),
                      },
                    ]}
                    rows={filteredSignals}
                    rowKey={(row, index) => `${row.type}-${row.source}-${index}`}
                    maxHeight="60vh"
                    caption="Collected signals"
                    empty={
                      <EmptyState compact title="NO MATCHES" description="No signal matches the filter." />
                    }
                  />
                )}
              </Panel>
            )}

            {tab === "results" && (
              <div className="flex flex-col gap-3">
                {searchError ? (
                  <Panel>
                    <ErrorState message={searchError} compact />
                  </Panel>
                ) : searchResults && searchResults.length > 0 ? (
                  <ResultsGrid results={searchResults} mode={searchMode} searchTime={searchTime} />
                ) : (
                  <Panel eyebrow="SEARCH" title="Cross-surface results">
                    <EmptyState
                      glyph="⌕"
                      title={searchResults ? "NO RESULTS" : "NO SEARCH RUN"}
                      description={
                        searchResults
                          ? "The search returned nothing. Broaden the query or add surfaces."
                          : "Use the Photo tab for a reverse image search, or run a name sweep below."
                      }
                    />
                    <div className="mt-2 flex flex-wrap items-end gap-2 border-t border-border-subtle pt-2">
                      <label className="flex min-w-[200px] flex-1 flex-col gap-1">
                        <span className="mono-label !text-[8px]">NAME SWEEP</span>
                        <TextInput
                          value={nameQuery}
                          onChange={(event) => setNameQuery(event.target.value)}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") {
                              event.preventDefault();
                              runName();
                            }
                          }}
                          placeholder="Jane Doe"
                          aria-label="Name to search"
                        />
                      </label>
                      <Button variant="solid" size="md" onClick={runName} disabled={!onRunNameSearch}>
                        Run PRISM sweep
                      </Button>
                    </div>
                  </Panel>
                )}
              </div>
            )}

            {tab === "photo" && (
              <div className="flex flex-col gap-3">
                <Panel eyebrow="IRIS" title="Reverse image / media search">
                  <PhotoUpload onFileSelect={onPhotoFile} selectedFile={photoFile} previewUrl={photoPreview} />
                  <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-border-subtle pt-2">
                    <Button
                      variant="solid"
                      onClick={runPhoto}
                      disabled={!photoFile || !onRunPhotoSearch}
                      loading={running && Boolean(photoFile)}
                    >
                      Analyse media
                    </Button>
                    <span className="mono-label !text-[8px]">
                      DRAG · CLICK · OR PASTE WITH ⌘V
                    </span>
                  </div>
                </Panel>

                {searchResults && searchResults.length > 0 && searchMode === "photo" && (
                  <ResultsGrid results={searchResults} mode="photo" searchTime={searchTime} />
                )}
              </div>
            )}
          </>
        )}
      </div>

      {/* ── Agent detail drawer ── */}
      <Drawer
        open={selectedAgent !== null}
        onClose={() => setSelectedAgent(null)}
        title={selectedAgent ? `${selectedAgent.agent} · ${selectedAgent.role ?? "agent"}` : ""}
        width={420}
      >
        {selectedAgent && (
          <div className="flex flex-col gap-3 p-3">
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone={agentToneFor(selectedAgent.status)} pulse={selectedAgent.status === "partial"}>
                {agentStatusText(selectedAgent.status)}
              </Badge>
              {selectedAgent.icon && <Badge tone="idle">{selectedAgent.icon}</Badge>}
              {selectedAgent.confidence !== undefined && (
                <Badge tone="ok">{confidencePct(selectedAgent.confidence)} confidence</Badge>
              )}
              {selectedAgent.latency_s !== undefined && (
                <Badge tone="idle">{formatSeconds(selectedAgent.latency_s)}</Badge>
              )}
              {selectedAgent.tokens_used !== undefined && (
                <Badge tone="idle">{selectedAgent.tokens_used} tokens</Badge>
              )}
            </div>

            {selectedAgent.error && (
              <p role="alert" className="m-0 break-words text-[10px] text-signal-err">
                {selectedAgent.error}
              </p>
            )}

            {selectedAgent.reasoning && (
              <div>
                <SectionTitle>Reasoning</SectionTitle>
                <p className="m-0 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-ink-muted">
                  {selectedAgent.reasoning}
                </p>
              </div>
            )}

            {selectedAgent.steps && selectedAgent.steps.length > 0 && (
              <div>
                <SectionTitle>{`Steps (${selectedAgent.steps.length})`}</SectionTitle>
                <ol className="m-0 flex list-none flex-col gap-1 p-0">
                  {selectedAgent.steps.map((step, index) => (
                    <li key={index} className="flex gap-2 text-[10px]">
                      <span className="w-5 shrink-0 text-ink-muted tabular-nums">
                        {String(index + 1).padStart(2, "0")}
                      </span>
                      <span className="min-w-0 break-words text-ink-muted">{step}</span>
                    </li>
                  ))}
                </ol>
              </div>
            )}

            {selectedAgent.entities_found && selectedAgent.entities_found.length > 0 && (
              <div>
                <SectionTitle>{`Entities (${selectedAgent.entities_found.length})`}</SectionTitle>
                <ul className="m-0 flex list-none flex-col gap-1 p-0">
                  {selectedAgent.entities_found.map((entity) => (
                    <li key={entity.id} className="flex items-center gap-2">
                      <EntityBadge type={entity.type} showLabel={false} />
                      <span className="min-w-0 flex-1 truncate text-[10px] text-ink">
                        {entity.label || entity.value || entity.id}
                      </span>
                      <span className="mono-label shrink-0 !text-[8px]">
                        {confidencePct(entity.confidence)}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {selectedAgent.signals && selectedAgent.signals.length > 0 && (
              <div>
                <SectionTitle>{`Signals (${selectedAgent.signals.length})`}</SectionTitle>
                <ul className="m-0 flex list-none flex-col gap-1 p-0">
                  {selectedAgent.signals.slice(0, 40).map((signal, index) => (
                    <li key={index} className="flex items-center gap-2">
                      <Badge tone="info">{signal.type}</Badge>
                      <span className="min-w-0 flex-1 truncate text-[10px] text-ink-muted">
                        {truncate(JSON.stringify(signal.value ?? ""), 60)}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div>
              <SectionTitle>Raw output</SectionTitle>
              <pre className="scroll-thin m-0 max-h-[240px] overflow-auto rounded border border-border-subtle bg-surface-0 p-2 text-[9px] leading-relaxed text-ink-muted">
                {safeStringify(selectedAgent.output)}
              </pre>
            </div>
          </div>
        )}
      </Drawer>

      {/* ── Entity detail drawer ── */}
      <Drawer
        open={selectedEntity !== null}
        onClose={() => setSelectedEntity(null)}
        title={selectedEntity ? truncate(selectedEntity.label || selectedEntity.id, 40) : ""}
        width={380}
      >
        {selectedEntity && (
          <div className="flex flex-col gap-3 p-3">
            <EntityBadge type={selectedEntity.type} size="md" />
            <div>
              <DefRow label="Label">{selectedEntity.label || "—"}</DefRow>
              {selectedEntity.value && (
                <DefRow label="Value" mono>
                  {selectedEntity.value}
                </DefRow>
              )}
              <DefRow label="Id" mono>
                {selectedEntity.id}
              </DefRow>
              {selectedEntity.confidence !== undefined && (
                <DefRow label="Confidence">
                  <Meter
                    value={selectedEntity.confidence}
                    tone={selectedEntity.confidence >= 0.7 ? "ok" : "warn"}
                    className="ml-auto w-16"
                    label="Entity confidence"
                  />
                  {confidencePct(selectedEntity.confidence)}
                </DefRow>
              )}
              {selectedEntity.source && <DefRow label="Source">{selectedEntity.source}</DefRow>}
              {selectedEntity.first_seen && (
                <DefRow label="First seen">{formatRelativeTime(selectedEntity.first_seen)}</DefRow>
              )}
              {selectedEntity.last_seen && (
                <DefRow label="Last seen">{formatRelativeTime(selectedEntity.last_seen)}</DefRow>
              )}
            </div>

            {selectedEntity.attrs && Object.keys(selectedEntity.attrs).length > 0 && (
              <div>
                <SectionTitle>Attributes</SectionTitle>
                <div>
                  {Object.entries(selectedEntity.attrs).map(([key, value]) => (
                    <DefRow key={key} label={key} mono>
                      {truncate(String(value), 80)}
                    </DefRow>
                  ))}
                </div>
              </div>
            )}

            <div className="flex items-center gap-2">
              <Badge color={colorFor(selectedEntity.type)}>{labelFor(selectedEntity.type)}</Badge>
              {investigation && (
                <Badge tone={statusTone(investigation.status)}>
                  {statusLabel(investigation.status)}
                </Badge>
              )}
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
}

/** JSON stringify that never throws on cycles. */
function safeStringify(value: unknown): string {
  if (value === undefined) return "undefined";
  if (value === null) return "null";
  if (typeof value === "string") return value;
  try {
    const seen = new WeakSet<object>();
    return JSON.stringify(
      value,
      (_key, item: unknown) => {
        if (typeof item === "object" && item !== null) {
          if (seen.has(item)) return "[circular]";
          seen.add(item);
        }
        return item;
      },
      2,
    ) ?? String(value);
  } catch {
    return String(value);
  }
}

export { ENTITY_TYPE_META };
