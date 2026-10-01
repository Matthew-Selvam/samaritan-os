"use client";

/**
 * CasesView.tsx — full case management.
 *
 * List, create, rename, tag, annotate, inspect per-case stats and drill into a
 * case's investigations / entities / timeline, plus export. Every mutation
 * reports success or failure through the shared toast, so a rejected PATCH never
 * fails silently.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import {
  Badge,
  Button,
  DefRow,
  EmptyState,
  ErrorState,
  Field,
  Modal,
  Panel,
  Select,
  SkeletonRows,
  StatTile,
  Table,
  Tabs,
  TextArea,
  TextInput,
  useToast,
  type Column,
} from "@/components/ui";
import { EntityBadge } from "@/components/EntityBadge";
import {
  createCase,
  deleteCase,
  exportCase,
  getCaseEntities,
  getCaseStats,
  getCaseTimeline,
  listCases,
  listInvestigations,
  updateCase,
} from "@/lib/api";
import {
  formatRelativeTime,
  statusLabel,
  statusTone,
  truncate,
} from "@/lib/format";
import { labelFor } from "@/lib/entityTypes";
import type { Case, CaseStats, Entity, Investigation, TimelineEvent } from "@/lib/types";
import { apiCall, downloadBlob, toJson, useAsyncResource } from "@/lib/hooks";

type CasesTab = "investigations" | "entities" | "timeline";

export interface CasesViewProps {
  /** Case currently bound in the top-bar selector. */
  activeCaseId?: string | null;
  onCaseChange?: (caseId: string | null) => void;
  onOpenInvestigation?: (investigation: Investigation) => void;
  /** Cross-link target: jump to the graph with these entities loaded. */
  onOpenGraph?: (entities: Entity[]) => void;
  className?: string;
}

/** Tag input: comma-separated, normalised and de-duplicated. */
function parseTags(raw: string): string[] {
  return [...new Set(raw.split(",").map((t) => t.trim()).filter(Boolean))];
}

function formatTags(tags: readonly string[] | undefined): string {
  return (tags ?? []).join(", ");
}

/**
 * Case management screen.
 *
 * @example
 * <CasesView activeCaseId={caseId} onCaseChange={setCaseId} />
 */
export function CasesView({
  activeCaseId = null,
  onCaseChange,
  onOpenInvestigation,
  onOpenGraph,
  className,
}: CasesViewProps) {
  const toast = useToast();
  const cases = useAsyncResource<Case[]>(
    (signal) =>
      apiCall(() => listCases({ limit: 100 }, { signal, timeout_ms: 15_000 }), "/api/cases"),
    { pollMs: 20_000 },
  );

  const [selectedId, setSelectedId] = useState<string | null>(activeCaseId);
  const [tab, setTab] = useState<CasesTab>("investigations");
  const [createOpen, setCreateOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);

  const [draftName, setDraftName] = useState("");
  const [draftTarget, setDraftTarget] = useState("");
  const [draftTags, setDraftTags] = useState("");
  const [draftNotes, setDraftNotes] = useState("");

  // Follow the top-bar selection until the analyst picks a row here.
  useEffect(() => {
    if (activeCaseId) setSelectedId(activeCaseId);
  }, [activeCaseId]);

  const selected: Case | null = useMemo(
    () => cases.data?.find((c) => c.case_id === selectedId) ?? null,
    [cases.data, selectedId],
  );

  const stats = useAsyncResource<CaseStats>(
    (signal) =>
      apiCall(
        () => getCaseStats(selectedId as string, { signal, timeout_ms: 15_000 }),
        `/api/cases/${selectedId}/stats`,
      ),
    { enabled: Boolean(selectedId), deps: [selectedId] },
  );
  const investigations = useAsyncResource<Investigation[]>(
    (signal) =>
      apiCall(
        () => listInvestigations({ case_id: selectedId ?? undefined, limit: 100 }, { signal }),
        "/api/investigations",
      ),
    { enabled: Boolean(selectedId), deps: [selectedId] },
  );
  const entities = useAsyncResource<Entity[]>(
    (signal) =>
      apiCall(
        () => getCaseEntities(selectedId as string, { signal, timeout_ms: 20_000 }),
        `/api/cases/${selectedId}/entities`,
      ),
    { enabled: Boolean(selectedId), deps: [selectedId] },
  );
  const timeline = useAsyncResource<TimelineEvent[]>(
    (signal) =>
      apiCall(
        () => getCaseTimeline(selectedId as string, { signal, timeout_ms: 20_000 }),
        `/api/cases/${selectedId}/timeline`,
      ),
    { enabled: Boolean(selectedId), deps: [selectedId] },
  );

  const openCreate = useCallback(() => {
    setDraftName("");
    setDraftTarget("");
    setDraftTags("");
    setDraftNotes("");
    setCreateOpen(true);
  }, []);

  const openEdit = useCallback(() => {
    if (!selected) return;
    setDraftName(selected.name);
    setDraftTarget(selected.target ?? "");
    setDraftTags(formatTags(selected.tags));
    setDraftNotes(selected.notes ?? "");
    setEditOpen(true);
  }, [selected]);

  const submitCreate = useCallback(async () => {
    const name = draftName.trim();
    if (!name) {
      toast.error("A case needs a name.");
      return;
    }
    setBusy(true);
    try {
      const created = await createCase({
        name,
        target: draftTarget.trim() || undefined,
        tags: parseTags(draftTags),
        notes: draftNotes.trim() || undefined,
      });
      cases.reload();
      setSelectedId(created.case_id);
      onCaseChange?.(created.case_id);
      setCreateOpen(false);
      toast.success(`Case “${created.name}” created.`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [cases, createOpen, draftName, draftNotes, draftTags, draftTarget, onCaseChange, toast]);

  const submitEdit = useCallback(async () => {
    if (!selected) return;
    const name = draftName.trim();
    if (!name) {
      toast.error("A case needs a name.");
      return;
    }
    setBusy(true);
    try {
      const updated = await updateCase(selected.case_id, {
        name,
        target: draftTarget.trim() || undefined,
        tags: parseTags(draftTags),
        notes: draftNotes.trim() || undefined,
      });
      cases.set((cases.data ?? []).map((c) => (c.case_id === updated.case_id ? updated : c)));
      setEditOpen(false);
      toast.success("Case updated.");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [cases, draftName, draftNotes, draftTags, draftTarget, selected, toast]);

  const doDelete = useCallback(async () => {
    if (!selected) return;
    setBusy(true);
    try {
      await deleteCase(selected.case_id);
      cases.set((cases.data ?? []).filter((c) => c.case_id !== selected.case_id));
      setSelectedId(null);
      if (activeCaseId === selected.case_id) onCaseChange?.(null);
      setConfirmDelete(false);
      toast.success(`Case “${selected.name}” deleted.`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [activeCaseId, cases, onCaseChange, selected, toast]);

  const doExport = useCallback(async () => {
    if (!selected) return;
    setBusy(true);
    try {
      const payload = await exportCase(selected.case_id, { timeout_ms: 60_000 });
      downloadBlob(
        toJson(payload),
        `signal-os-case-${selected.case_id}.json`,
        "application/json",
      );
      toast.success("Case bundle downloaded.");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [selected, toast]);

  const caseColumns: Column<Case>[] = useMemo(
    () => [
      {
        key: "name",
        header: "Case",
        sortValue: (row) => row.name.toLowerCase(),
        render: (row) => (
          <span className="flex min-w-0 flex-col">
            <span className="truncate text-ink">{row.name}</span>
            {row.target && (
              <span className="mono-label truncate !text-[8px]">{truncate(row.target, 48)}</span>
            )}
          </span>
        ),
      },
      {
        key: "tags",
        header: "Tags",
        hideBelow: "lg",
        render: (row) =>
          (row.tags ?? []).length === 0 ? (
            <span className="mono-label !text-[8px]">—</span>
          ) : (
            <span className="flex flex-wrap gap-1">
              {(row.tags ?? []).slice(0, 4).map((tag) => (
                <Badge key={tag} tone="idle">
                  {tag}
                </Badge>
              ))}
              {(row.tags ?? []).length > 4 && (
                <Badge tone="idle">+{(row.tags ?? []).length - 4}</Badge>
              )}
            </span>
          ),
      },
      {
        key: "investigations",
        header: "Runs",
        width: "72px",
        align: "right",
        sortValue: (row) => row.investigation_count ?? 0,
        render: (row) => row.investigation_count ?? "—",
      },
      {
        key: "updated",
        header: "Updated",
        width: "120px",
        hideBelow: "md",
        sortValue: (row) => row.updated_at ?? "",
        render: (row) => (
          <span className="mono-label !text-[9px]">
            {row.updated_at ? formatRelativeTime(row.updated_at) : "—"}
          </span>
        ),
      },
    ],
    [],
  );

  return (
    <div className={clsx("scroll-thin min-h-0 flex-1 overflow-y-auto", className)}>
      <div className="flex flex-col gap-3 p-3">
        <Panel
          eyebrow="WORKSPACE"
          title="Cases"
          actions={
            <>
              <Badge tone="idle">{cases.data?.length ?? 0}</Badge>
              <Button size="sm" variant="ghost" iconLabel="Refresh cases" onClick={cases.reload}>
                ⟳
              </Button>
              <Button size="sm" variant="solid" onClick={openCreate}>
                New case
              </Button>
            </>
          }
          bodyClassName="!p-0"
        >
          {cases.state === "loading" && !cases.data ? (
            <div className="p-3">
              <SkeletonRows rows={4} height={40} />
            </div>
          ) : cases.state === "error" ? (
            <ErrorState message={cases.error} onRetry={cases.reload} />
          ) : (cases.data?.length ?? 0) === 0 ? (
            <EmptyState
              glyph="◫"
              title="NO CASES YET"
              description="A case groups investigations, entities and a timeline. Create one to start building a dossier."
              action={
                <Button variant="solid" onClick={openCreate}>
                  Create the first case
                </Button>
              }
            />
          ) : (
            <Table
              columns={caseColumns}
              rows={cases.data ?? []}
              rowKey={(row) => row.case_id}
              onRowClick={(row) => {
                setSelectedId(row.case_id);
                onCaseChange?.(row.case_id);
              }}
              isRowActive={(row) => row.case_id === selectedId}
              caption="All cases"
              defaultSort={{ key: "updated", direction: "desc" }}
            />
          )}
        </Panel>

        {selected && (
          <>
            <Panel
              eyebrow="SELECTED"
              title={selected.name}
              actions={
                <>
                  <Select
                    aria-label="Select case"
                    value={selected.case_id}
                    onChange={(event) => {
                      setSelectedId(event.target.value || null);
                      onCaseChange?.(event.target.value || null);
                    }}
                    className="!w-[160px] !py-0.5 !text-[10px]"
                  >
                    {(cases.data ?? []).map((c) => (
                      <option key={c.case_id} value={c.case_id}>
                        {c.name}
                      </option>
                    ))}
                  </Select>
                  <Button size="sm" variant="outline" onClick={openEdit}>
                    Edit
                  </Button>
                  <Button size="sm" variant="outline" loading={busy} onClick={() => void doExport()}>
                    Export
                  </Button>
                  <Button size="sm" variant="danger" onClick={() => setConfirmDelete(true)}>
                    Delete
                  </Button>
                </>
              }
            >
              <div className="flex flex-col gap-3">
                <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                  <StatTile
                    label="Investigations"
                    value={stats.data?.investigation_count ?? "—"}
                    tone="info"
                    loading={stats.state === "loading"}
                    icon="◎"
                  />
                  <StatTile
                    label="Entities"
                    value={stats.data?.entity_count ?? "—"}
                    tone="ok"
                    loading={stats.state === "loading"}
                    icon="◉"
                  />
                  <StatTile
                    label="Signals"
                    value={stats.data?.signal_count ?? "—"}
                    tone="warn"
                    loading={stats.state === "loading"}
                    icon="≋"
                  />
                  <StatTile
                    label="Agents"
                    value={stats.data?.agent_count ?? "—"}
                    tone="info"
                    loading={stats.state === "loading"}
                    icon="⬡"
                  />
                </div>

                <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
                  <div>
                    <div className="mono-label mb-1 !text-[9px]">METADATA</div>
                    <DefRow label="Case id" mono>
                      {selected.case_id}
                    </DefRow>
                    {selected.target && <DefRow label="Target" mono>{selected.target}</DefRow>}
                    <DefRow label="Created">
                      {selected.created_at ? formatRelativeTime(selected.created_at) : "—"}
                    </DefRow>
                    <DefRow label="Updated">
                      {selected.updated_at ? formatRelativeTime(selected.updated_at) : "—"}
                    </DefRow>
                    {stats.data?.first_seen && (
                      <DefRow label="First signal">{formatRelativeTime(stats.data.first_seen)}</DefRow>
                    )}
                    {stats.data?.last_seen && (
                      <DefRow label="Last signal">{formatRelativeTime(stats.data.last_seen)}</DefRow>
                    )}
                    <DefRow label="Tags">
                      {(selected.tags ?? []).length === 0 ? (
                        <span className="mono-label !text-[8px]">none</span>
                      ) : (
                        <span className="flex flex-wrap justify-end gap-1">
                          {(selected.tags ?? []).map((tag) => (
                            <Badge key={tag} tone="info">
                              {tag}
                            </Badge>
                          ))}
                        </span>
                      )}
                    </DefRow>
                    {stats.data?.entity_types && (
                      <DefRow label="Types">
                        {Object.keys(stats.data.entity_types).length}
                      </DefRow>
                    )}
                  </div>

                  <div className="flex min-w-0 flex-col">
                    <div className="mono-label mb-1 !text-[9px]">ANALYST NOTES</div>
                    {selected.notes ? (
                      <p className="m-0 whitespace-pre-wrap rounded border border-border-subtle bg-surface-0 p-2 text-[11px] leading-relaxed text-ink">
                        {selected.notes}
                      </p>
                    ) : (
                      <EmptyState
                        compact
                        glyph="✎"
                        title="NO NOTES"
                        description="Use Edit to record hypotheses, dead ends and next steps."
                      />
                    )}
                  </div>
                </div>

                <div>
                  <div className="mono-label mb-1 !text-[9px]">TYPE BREAKDOWN</div>
                  {stats.data?.entity_types ? (
                    <div className="flex flex-wrap gap-1">
                      {Object.entries(stats.data.entity_types).map(([type, count]) => (
                        <EntityBadge
                          key={type}
                          type={type}
                          label={`${labelFor(type)} ${count ?? 0}`}
                        />
                      ))}
                    </div>
                  ) : stats.state === "loading" ? (
                    <SkeletonRows rows={1} height={22} />
                  ) : (
                    <EmptyState compact title="NO TYPE DATA" description="This backend reports no per-case entity counts." />
                  )}
                </div>
              </div>
            </Panel>

            <Panel bodyClassName="!p-0">
              <Tabs
                label="Case contents"
                active={tab}
                onChange={setTab}
                items={[
                  { id: "investigations", label: "Investigations", count: investigations.data?.length },
                  { id: "entities", label: "Entities", count: entities.data?.length },
                  { id: "timeline", label: "Timeline", count: timeline.data?.length },
                ]}
                actions={
                  tab === "entities" && onOpenGraph && (entities.data?.length ?? 0) > 0 ? (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => onOpenGraph(entities.data ?? [])}
                    >
                      Open in graph ↗
                    </Button>
                  ) : undefined
                }
              />

              <div className="p-2">
                {tab === "investigations" &&
                  (investigations.state === "loading" && !investigations.data ? (
                    <SkeletonRows rows={3} height={44} />
                  ) : investigations.state === "error" ? (
                    <ErrorState
                      compact
                      message={investigations.error}
                      onRetry={investigations.reload}
                    />
                  ) : (investigations.data?.length ?? 0) === 0 ? (
                    <EmptyState
                      compact
                      glyph="◎"
                      title="NO INVESTIGATIONS IN THIS CASE"
                      description="Run an investigation from the INVESTIGATE view with this case selected."
                    />
                  ) : (
                    <Table
                      columns={[
                        {
                          key: "input",
                          header: "Target",
                          sortValue: (row) => row.input.toLowerCase(),
                          render: (row) => (
                            <span className="block truncate" title={row.input}>
                              {truncate(row.input, 60)}
                            </span>
                          ),
                        },
                        {
                          key: "type",
                          header: "Type",
                          width: "120px",
                          sortValue: (row) => row.input_type,
                          render: (row) => (
                            <Badge color={colorForInput(row.input_type)}>{row.input_type}</Badge>
                          ),
                        },
                        {
                          key: "status",
                          header: "Status",
                          width: "100px",
                          sortValue: (row) => row.status,
                          render: (row) => (
                            <Badge tone={statusTone(row.status)}>{statusLabel(row.status)}</Badge>
                          ),
                        },
                        {
                          key: "agents",
                          header: "Agents",
                          width: "76px",
                          align: "right",
                          hideBelow: "md",
                          sortValue: (row) => row.agents_activated?.length ?? 0,
                          render: (row) => row.agents_activated?.length ?? 0,
                        },
                        {
                          key: "started",
                          header: "Started",
                          width: "120px",
                          hideBelow: "lg",
                          sortValue: (row) => row.started_at ?? "",
                          render: (row) => (
                            <span className="mono-label !text-[9px]">
                              {row.started_at ? formatRelativeTime(row.started_at) : "—"}
                            </span>
                          ),
                        },
                      ]}
                      rows={investigations.data ?? []}
                      rowKey={(row) => row.inv_id}
                      onRowClick={onOpenInvestigation}
                      caption="Investigations in this case"
                    />
                  ))}

                {tab === "entities" &&
                  (entities.state === "loading" && !entities.data ? (
                    <SkeletonRows rows={5} height={26} />
                  ) : entities.state === "error" ? (
                    <ErrorState compact message={entities.error} onRetry={entities.reload} />
                  ) : (entities.data?.length ?? 0) === 0 ? (
                    <EmptyState
                      compact
                      glyph="◉"
                      title="NO ENTITIES YET"
                      description="Entities appear once an investigation in this case completes."
                    />
                  ) : (
                    <Table
                      columns={[
                        {
                          key: "type",
                          header: "Type",
                          width: "130px",
                          sortValue: (row) => labelFor(row.type),
                          render: (row) => <EntityBadge type={row.type} />,
                        },
                        {
                          key: "label",
                          header: "Entity",
                          sortValue: (row) => row.label.toLowerCase(),
                          render: (row) => (
                            <span className="block truncate">{row.label || row.value || row.id}</span>
                          ),
                        },
                        {
                          key: "confidence",
                          header: "Conf.",
                          width: "70px",
                          align: "right",
                          hideBelow: "md",
                          sortValue: (row) => row.confidence ?? -1,
                          render: (row) =>
                            row.confidence === undefined ? "—" : `${Math.round(row.confidence * 100)}%`,
                        },
                        {
                          key: "source",
                          header: "Source",
                          width: "100px",
                          hideBelow: "lg",
                          sortValue: (row) => row.source ?? "",
                          render: (row) => (
                            <span className="mono-label !text-[9px]">{row.source ?? "—"}</span>
                          ),
                        },
                      ]}
                      rows={entities.data ?? []}
                      rowKey={(row) => row.id}
                      maxHeight="50vh"
                      caption="Entities in this case"
                    />
                  ))}

                {tab === "timeline" &&
                  (timeline.state === "loading" && !timeline.data ? (
                    <SkeletonRows rows={4} height={40} />
                  ) : timeline.state === "error" ? (
                    <ErrorState compact message={timeline.error} onRetry={timeline.reload} />
                  ) : (timeline.data?.length ?? 0) === 0 ? (
                    <EmptyState
                      compact
                      glyph="⊕"
                      title="NO TIMELINE EVENTS"
                      description="KRONOS reconstructs a chronology once case investigations have produced dated signals."
                    />
                  ) : (
                    <Table
                      columns={[
                        {
                          key: "date",
                          header: "When",
                          width: "170px",
                          sortValue: (row) => row.ts ?? row.date,
                          render: (row) => (
                            <span className="mono-label !text-[9px]">{row.ts ?? row.date}</span>
                          ),
                        },
                        {
                          key: "label",
                          header: "Event",
                          sortValue: (row) => row.label.toLowerCase(),
                          render: (row) => (
                            <span className="block min-w-0 truncate" title={row.label}>
                              {row.label}
                            </span>
                          ),
                        },
                        {
                          key: "source",
                          header: "Source",
                          width: "110px",
                          hideBelow: "md",
                          sortValue: (row) => row.source ?? "",
                          render: (row) => (
                            <span className="mono-label !text-[9px]">{row.source ?? "—"}</span>
                          ),
                        },
                        {
                          key: "confidence",
                          header: "Conf.",
                          width: "70px",
                          align: "right",
                          hideBelow: "lg",
                          sortValue: (row) => row.confidence ?? -1,
                          render: (row) =>
                            row.confidence === undefined ? "—" : `${Math.round(row.confidence * 100)}%`,
                        },
                      ]}
                      rows={timeline.data ?? []}
                      rowKey={(row, index) => `${row.id}-${index}`}
                      maxHeight="50vh"
                      caption="Case timeline"
                    />
                  ))}
              </div>
            </Panel>
          </>
        )}
      </div>

      {/* Create / edit dialog */}
      <Modal
        open={createOpen || editOpen}
        onClose={() => {
          setCreateOpen(false);
          setEditOpen(false);
        }}
        title={createOpen ? "New case" : "Edit case"}
        width={520}
        footer={
          <>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setCreateOpen(false);
                setEditOpen(false);
              }}
            >
              Cancel
            </Button>
            <Button
              size="sm"
              variant="solid"
              loading={busy}
              onClick={() => void (createOpen ? submitCreate() : submitEdit())}
            >
              {createOpen ? "Create" : "Save"}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-2 p-3">
          <Field label="Name" htmlFor="case-name">
            <TextInput
              id="case-name"
              value={draftName}
              onChange={(event) => setDraftName(event.target.value)}
              placeholder="Operation Nightfall"
            />
          </Field>
          <Field label="Target" htmlFor="case-target" hint="The primary subject or indicator.">
            <TextInput
              id="case-target"
              value={draftTarget}
              onChange={(event) => setDraftTarget(event.target.value)}
              placeholder="example.com"
            />
          </Field>
          <Field label="Tags" htmlFor="case-tags" hint="Comma separated.">
            <TextInput
              id="case-tags"
              value={draftTags}
              onChange={(event) => setDraftTags(event.target.value)}
              placeholder="phishing, infra, eu"
            />
          </Field>
          <Field label="Notes" htmlFor="case-notes">
            <TextArea
              id="case-notes"
              rows={4}
              value={draftNotes}
              onChange={(event) => setDraftNotes(event.target.value)}
              placeholder="Hypotheses, dead ends, next steps…"
            />
          </Field>
        </div>
      </Modal>

      <Modal
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        title="Delete case?"
        width={400}
        footer={
          <>
            <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
            <Button size="sm" variant="danger" loading={busy} onClick={() => void doDelete()}>
              Delete permanently
            </Button>
          </>
        }
      >
        <p className="m-0 p-3 text-[11px] leading-relaxed text-ink-muted">
          Deleting “{selected?.name}” removes the case and its bindings. Investigations already run
          are not erased, but they will no longer be grouped under it.
        </p>
      </Modal>
    </div>
  );
}

/** Accent colour for a raw input-type string. */
function colorForInput(inputType: string | null | undefined): string {
  const map: Record<string, string> = {
    email: "#00d4ff",
    domain: "#00ff88",
    ip_address: "#ffb020",
    username: "#9966ff",
    crypto_wallet: "#ff8833",
    url: "#44dd88",
  };
  return map[inputType ?? ""] ?? "#5a7a9a";
}
