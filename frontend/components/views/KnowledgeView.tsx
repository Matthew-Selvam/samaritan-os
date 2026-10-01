"use client";

/**
 * KnowledgeView.tsx — the cross-case vault.
 *
 * ONE MEMORY SYSTEM: pinned entities and analyst notes persist across cases so
 * a fact surfaced in one investigation is recallable in the next. Storage is
 * `lib/vaultStore.ts` (localStorage behind a typed API) because the backend has
 * no vault routes yet — the view never talks to storage directly, so swapping
 * in an HTTP backend is a change to that one module.
 *
 * The GDPR purge control is deliberately loud: it states exactly what it
 * deletes and requires a confirmation.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import {
  Badge,
  Button,
  DefRow,
  EmptyState,
  Field,
  Modal,
  Panel,
  Segmented,
  StatTile,
  Table,
  Tabs,
  TextArea,
  TextInput,
  useToast,
  type Column,
} from "@/components/ui";
import { EntityBadge } from "@/components/EntityBadge";
import { colorFor, labelFor } from "@/lib/entityTypes";
import {
  confidencePct,
  formatRelativeTime,
  truncate,
} from "@/lib/format";
import type { Entity, EntityType } from "@/lib/types";
import { normalizeEntityType } from "@/lib/entityTypes";
import {
  addVaultEntity,
  loadVaultEntities,
  loadVaultNotes,
  purgeVault,
  removeVaultEntity,
  removeVaultNote,
  saveVaultEntities,
  saveVaultNotes,
  upsertVaultNote,
  vaultScore,
  type VaultEntity,
  type VaultNote,
} from "@/lib/vaultStore";
import { downloadBlob, toJson } from "@/lib/hooks";

type VaultTab = "entities" | "notes";

export interface KnowledgeViewProps {
  /** Entities of the current run, offered for pinning. */
  availableEntities?: readonly Entity[];
  /** Case the next pin will be attributed to. */
  caseId?: string | null;
  /** Open an entity in the graph. */
  onOpenEntity?: (entity: Entity) => void;
  className?: string;
}

/** Prompt-local entity type picker options. */
const PIN_TYPE_HINTS: EntityType[] = [
  "person",
  "email",
  "username",
  "domain",
  "ip",
  "phone",
  "url",
  "wallet",
  "crypto",
  "location",
  "org",
  "document",
  "note",
  "other",
];

/**
 * The vault.
 *
 * @example
 * <KnowledgeView availableEntities={entities} caseId={caseId} onOpenEntity={focus} />
 */
export function KnowledgeView({
  availableEntities = [],
  caseId = null,
  onOpenEntity,
  className,
}: KnowledgeViewProps) {
  const toast = useToast();
  const [tab, setTab] = useState<VaultTab>("entities");
  const [items, setItems] = useState<VaultEntity[]>([]);
  const [notes, setNotes] = useState<VaultNote[]>([]);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [pinOpen, setPinOpen] = useState(false);
  const [noteOpen, setNoteOpen] = useState(false);
  const [purgeOpen, setPurgeOpen] = useState(false);

  const [pinValue, setPinValue] = useState("");
  const [pinType, setPinType] = useState<EntityType>("other");
  const [noteTitle, setNoteTitle] = useState("");
  const [noteBody, setNoteBody] = useState("");
  const [noteTags, setNoteTags] = useState("");
  const [editingNoteId, setEditingNoteId] = useState<string | null>(null);

  /* ── Load on mount; re-read after every mutation ── */
  const reload = useCallback(() => {
    setItems(loadVaultEntities());
    setNotes(loadVaultNotes());
  }, []);

  useEffect(reload, [reload]);

  const commitItems = useCallback((next: VaultEntity[]) => {
    setItems(next);
    saveVaultEntities(next);
  }, []);

  const commitNotes = useCallback((next: VaultNote[]) => {
    setNotes(next);
    saveVaultNotes(next);
  }, []);

  /* ── Recall ── */
  const filteredItems = useMemo(() => {
    return items
      .map((item) => ({
        item,
        score: vaultScore(`${item.label} ${item.value ?? ""} ${item.type} ${item.source ?? ""}`, query),
      }))
      .filter((row) => row.score > 0 && (!typeFilter || row.item.type === typeFilter))
      .sort((a, b) => b.score - a.score || b.item.savedAt.localeCompare(a.item.savedAt))
      .map((row) => row.item);
  }, [items, query, typeFilter]);

  const filteredNotes = useMemo(() => {
    return notes
      .map((note) => ({
        note,
        score: vaultScore(`${note.title} ${note.body} ${(note.tags ?? []).join(" ")}`, query),
      }))
      .filter((row) => row.score > 0)
      .sort((a, b) => b.score - a.score || b.note.savedAt.localeCompare(a.note.savedAt))
      .map((row) => row.note);
  }, [notes, query]);

  /* ── Actions ── */
  const submitPin = useCallback(() => {
    const value = pinValue.trim();
    if (!value) {
      toast.error("Enter something worth remembering.");
      return;
    }
    const next = addVaultEntity(items, {
      id: `${normalizeEntityType(pinType)}:${value.toLowerCase()}`,
      label: value,
      type: normalizeEntityType(pinType),
      value,
      caseId: caseId ?? undefined,
    });
    commitItems(next);
    setPinOpen(false);
    setPinValue("");
    toast.success(`Pinned “${truncate(value, 32)}” to the vault.`);
  }, [caseId, commitItems, items, pinType, pinValue, toast]);

  const pinFromRun = useCallback(
    (entity: Entity) => {
      const next = addVaultEntity(items, {
        id: entity.id,
        label: entity.label || entity.value || entity.id,
        type: normalizeEntityType(entity.type),
        value: entity.value,
        confidence: entity.confidence,
        source: entity.source,
        caseId: caseId ?? undefined,
      });
      commitItems(next);
      toast.success(`Pinned ${truncate(entity.label || entity.id, 32)}.`);
    },
    [caseId, commitItems, items, toast],
  );

  const unpin = useCallback(
    (id: string) => {
      commitItems(removeVaultEntity(items, id));
      toast.push("Entity removed from the vault.", "info");
    },
    [commitItems, items, toast],
  );

  const openNote = useCallback(
    (note: VaultNote | null) => {
      setEditingNoteId(note?.id ?? null);
      setNoteTitle(note?.title ?? "");
      setNoteBody(note?.body ?? "");
      setNoteTags((note?.tags ?? []).join(", "));
      setNoteOpen(true);
    },
    [],
  );

  const submitNote = useCallback(() => {
    const title = noteTitle.trim();
    if (!title) {
      toast.error("A note needs a title.");
      return;
    }
    const next = upsertVaultNote(notes, {
      id: editingNoteId ?? `note-${Date.now()}`,
      title,
      body: noteBody.trim(),
      tags: noteTags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean),
      caseId: caseId ?? undefined,
    });
    commitNotes(next);
    setNoteOpen(false);
    toast.success(editingNoteId ? "Note updated." : "Note saved.");
  }, [caseId, commitNotes, editingNoteId, noteBody, noteTags, noteTitle, notes, toast]);

  const deleteNote = useCallback(
    (id: string) => {
      commitNotes(removeVaultNote(notes, id));
      toast.push("Note deleted.", "info");
    },
    [commitNotes, notes, toast],
  );

  const doPurge = useCallback(() => {
    purgeVault();
    reload();
    setPurgeOpen(false);
    toast.success("Vault purged: pinned entities, notes and palette recents erased.");
  }, [reload, toast]);

  const exportVault = useCallback(() => {
    downloadBlob(
      toJson({ exported_at: new Date().toISOString(), entities: items, notes }),
      "signal-os-vault.json",
      "application/json",
    );
    toast.success("Vault exported.");
  }, [items, notes, toast]);

  const typeCounts = useMemo(() => {
    const counts = new Map<EntityType, number>();
    for (const item of items) counts.set(item.type, (counts.get(item.type) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [items]);

  const itemColumns: Column<VaultEntity>[] = useMemo(
    () => [
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
          <span className="block min-w-0 truncate" title={row.label}>
            {row.label}
          </span>
        ),
      },
      {
        key: "confidence",
        header: "Conf.",
        width: "70px",
        align: "right",
        hideBelow: "md",
        sortValue: (row) => row.confidence ?? -1,
        render: (row) => (row.confidence === undefined ? "—" : confidencePct(row.confidence)),
      },
      {
        key: "case",
        header: "Case",
        width: "110px",
        hideBelow: "lg",
        sortValue: (row) => row.caseId ?? "",
        render: (row) => (
          <span className="mono-label !text-[9px]">{row.caseId ?? "—"}</span>
        ),
      },
      {
        key: "savedAt",
        header: "Saved",
        width: "110px",
        hideBelow: "lg",
        sortValue: (row) => row.savedAt,
        render: (row) => (
          <span className="mono-label !text-[9px]">{formatRelativeTime(row.savedAt)}</span>
        ),
      },
      {
        key: "actions",
        header: "",
        width: "112px",
        align: "right",
        render: (row) => (
          <span className="flex justify-end gap-1">
            {onOpenEntity && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() =>
                  onOpenEntity({
                    id: row.id,
                    label: row.label,
                    type: row.type,
                    value: row.value,
                    confidence: row.confidence,
                  })
                }
              >
                Graph
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={() => unpin(row.id)}>
              Remove
            </Button>
          </span>
        ),
      },
    ],
    [onOpenEntity, unpin],
  );

  return (
    <div className={clsx("scroll-thin min-h-0 flex-1 overflow-y-auto", className)}>
      <div className="flex flex-col gap-3 p-3">
        <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
          <StatTile label="Pinned entities" value={items.length} tone="info" icon="◉" />
          <StatTile label="Types" value={typeCounts.length} tone="ok" icon="◈" />
          <StatTile label="Notes" value={notes.length} tone="warn" icon="✎" />
          <StatTile
            label="Storage"
            value="LOCAL"
            tone="idle"
            hint="Client-side until the vault API lands"
            icon="□"
          />
        </div>

        <Panel
          eyebrow="VAULT"
          title="Persistent memory"
          actions={
            <>
              <Button size="sm" variant="ghost" onClick={exportVault} disabled={items.length + notes.length === 0}>
                Export
              </Button>
              <Button size="sm" variant="outline" onClick={() => openNote(null)}>
                New note
              </Button>
              <Button size="sm" variant="solid" onClick={() => setPinOpen(true)}>
                Pin entity
              </Button>
              <Button size="sm" variant="danger" onClick={() => setPurgeOpen(true)}>
                GDPR purge
              </Button>
            </>
          }
        >
          <div className="flex flex-wrap items-center gap-2">
            <div className="min-w-[220px] flex-1">
              <TextInput
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Recall across cases…"
                aria-label="Search the vault"
                className="!py-1 !text-[11px]"
              />
            </div>
            <Segmented
              label="Vault section"
              value={tab}
              onChange={setTab}
              options={[
                { value: "entities", label: `Entities ${items.length}` },
                { value: "notes", label: `Notes ${notes.length}` },
              ]}
            />
          </div>

          {typeCounts.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              <button
                type="button"
                onClick={() => setTypeFilter("")}
                aria-pressed={typeFilter === ""}
                className={clsx(
                  "focus-ring rounded border px-1.5 py-[1px] font-mono text-[9px] uppercase",
                  typeFilter === ""
                    ? "border-accent/50 bg-accent/10 text-accent"
                    : "border-border-subtle text-ink-muted",
                )}
              >
                All
              </button>
              {typeCounts.map(([type, count]) => (
                <button
                  key={type}
                  type="button"
                  onClick={() => setTypeFilter(typeFilter === type ? "" : type)}
                  aria-pressed={typeFilter === type}
                  className={clsx(
                    "focus-ring rounded border px-1.5 py-[1px] font-mono text-[9px] uppercase",
                    typeFilter === type ? "border-accent/50 bg-accent/10 text-accent" : "",
                  )}
                  style={
                    typeFilter === type
                      ? undefined
                      : { color: colorFor(type), borderColor: `${colorFor(type)}4d` }
                  }
                >
                  {labelFor(type)} {count}
                </button>
              ))}
            </div>
          )}
        </Panel>

        <Panel bodyClassName="!p-0">
          <Tabs
            label="Vault contents"
            active={tab}
            onChange={setTab}
            items={[
              { id: "entities", label: "Pinned entities", count: filteredItems.length },
              { id: "notes", label: "Analyst notes", count: filteredNotes.length },
            ]}
          />

          <div className="p-2">
            {tab === "entities" &&
              (filteredItems.length === 0 ? (
                <EmptyState
                  glyph="◉"
                  title={items.length === 0 ? "VAULT IS EMPTY" : "NO MATCHES"}
                  description={
                    items.length === 0
                      ? "Pin an entity from this screen or from the ENTITIES tab of a run, and it stays recallable across every future case."
                      : "Nothing matches the current recall query or type filter."
                  }
                  action={
                    <Button variant="solid" onClick={() => setPinOpen(true)}>
                      Pin the first entity
                    </Button>
                  }
                />
              ) : (
                <Table
                  columns={itemColumns}
                  rows={filteredItems}
                  rowKey={(row) => row.id}
                  caption="Pinned entities"
                />
              ))}

            {tab === "notes" &&
              (filteredNotes.length === 0 ? (
                <EmptyState
                  glyph="✎"
                  title={notes.length === 0 ? "NO NOTES" : "NO MATCHES"}
                  description={
                    notes.length === 0
                      ? "Record hypotheses, dead ends and next steps so they survive the session."
                      : "No note matches the recall query."
                  }
                  action={
                    <Button variant="solid" onClick={() => openNote(null)}>
                      Write the first note
                    </Button>
                  }
                />
              ) : (
                <ul className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                  {filteredNotes.map((note) => (
                    <li
                      key={note.id}
                      className="panel flex flex-col gap-1.5 p-2.5 transition-colors hover:border-border-strong"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <h3 className="m-0 min-w-0 truncate text-[12px] text-ink">{note.title}</h3>
                        <Badge tone="idle">{formatRelativeTime(note.savedAt)}</Badge>
                      </div>
                      {note.body && (
                        <p className="m-0 line-clamp-4 text-[10px] leading-relaxed text-ink-muted">
                          {note.body}
                        </p>
                      )}
                      <div className="mt-auto flex items-center justify-between gap-2">
                        <span className="flex flex-wrap gap-1">
                          {(note.tags ?? []).map((tag) => (
                            <Badge key={tag} tone="info">
                              {tag}
                            </Badge>
                          ))}
                        </span>
                        <span className="flex shrink-0 gap-1">
                          <Button size="sm" variant="ghost" onClick={() => openNote(note)}>
                            Edit
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => deleteNote(note.id)}>
                            Delete
                          </Button>
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
              ))}
          </div>
        </Panel>

        {/* Quick-pin from the current run */}
        {availableEntities.length > 0 && (
          <Panel
            eyebrow="CURRENT RUN"
            title="Pin from this investigation"
            actions={<Badge tone="idle">{availableEntities.length} available</Badge>}
            bodyClassName="!p-0"
          >
            <div className="scroll-thin max-h-[220px] overflow-y-auto">
              <ul className="flex flex-col">
                {availableEntities.slice(0, 200).map((entity) => {
                  const already = items.some((item) => item.id === entity.id);
                  return (
                    <li
                      key={entity.id}
                      className="flex items-center gap-2 border-b border-border-subtle/60 px-2 py-1"
                    >
                      <EntityBadge type={entity.type} showLabel={false} />
                      <span className="min-w-0 flex-1 truncate text-[11px] text-ink">
                        {truncate(entity.label || entity.value || entity.id, 60)}
                      </span>
                      <span className="mono-label shrink-0 !text-[8px]">
                        {entity.source ?? "—"}
                      </span>
                      <Button
                        size="sm"
                        variant={already ? "ghost" : "outline"}
                        disabled={already}
                        onClick={() => pinFromRun(entity)}
                      >
                        {already ? "Pinned" : "Pin"}
                      </Button>
                    </li>
                  );
                })}
              </ul>
            </div>
          </Panel>
        )}
      </div>

      {/* Pin dialog */}
      <Modal
        open={pinOpen}
        onClose={() => setPinOpen(false)}
        title="Pin entity to vault"
        width={440}
        footer={
          <>
            <Button size="sm" variant="ghost" onClick={() => setPinOpen(false)}>
              Cancel
            </Button>
            <Button size="sm" variant="solid" onClick={submitPin}>
              Pin
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-2 p-3">
          <Field label="Value" htmlFor="pin-value">
            <TextInput
              id="pin-value"
              value={pinValue}
              onChange={(event) => setPinValue(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  submitPin();
                }
              }}
              placeholder="attacker@protonmail.com"
            />
          </Field>
          <Field label="Type" htmlFor="pin-type" hint="Determines the colour used everywhere else.">
            <select
              id="pin-type"
              value={pinType}
              onChange={(event) => setPinType(event.target.value as EntityType)}
              className="focus-ring rounded-md border border-border-strong bg-surface-0 px-2 py-1.5 text-[12px] text-ink"
            >
              {PIN_TYPE_HINTS.map((type) => (
                <option key={type} value={type}>
                  {labelFor(type)}
                </option>
              ))}
            </select>
          </Field>
          {caseId && <DefRow label="Attributed case" mono>{caseId}</DefRow>}
        </div>
      </Modal>

      {/* Note editor */}
      <Modal
        open={noteOpen}
        onClose={() => setNoteOpen(false)}
        title={editingNoteId ? "Edit note" : "New note"}
        width={560}
        footer={
          <>
            <Button size="sm" variant="ghost" onClick={() => setNoteOpen(false)}>
              Cancel
            </Button>
            <Button size="sm" variant="solid" onClick={submitNote}>
              Save
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-2 p-3">
          <Field label="Title" htmlFor="note-title">
            <TextInput
              id="note-title"
              value={noteTitle}
              onChange={(event) => setNoteTitle(event.target.value)}
              placeholder="Pivot on the wallet cluster"
            />
          </Field>
          <Field label="Body" htmlFor="note-body">
            <TextArea
              id="note-body"
              rows={6}
              value={noteBody}
              onChange={(event) => setNoteBody(event.target.value)}
              placeholder="What we know, what we ruled out, what to try next."
            />
          </Field>
          <Field label="Tags" htmlFor="note-tags" hint="Comma separated.">
            <TextInput
              id="note-tags"
              value={noteTags}
              onChange={(event) => setNoteTags(event.target.value)}
              placeholder="hypothesis, follow-up"
            />
          </Field>
        </div>
      </Modal>

      {/* GDPR purge */}
      <Modal
        open={purgeOpen}
        onClose={() => setPurgeOpen(false)}
        title="Erase the vault?"
        width={480}
        footer={
          <>
            <Button size="sm" variant="ghost" onClick={() => setPurgeOpen(false)}>
              Keep data
            </Button>
            <Button size="sm" variant="danger" onClick={doPurge}>
              Erase everything
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-2 p-3">
          <p className="m-0 text-[11px] leading-relaxed text-ink-muted">
            This permanently removes everything Signal-OS has retained in this browser:
          </p>
          <div>
            <DefRow label="Pinned entities">{items.length}</DefRow>
            <DefRow label="Analyst notes">{notes.length}</DefRow>
            <DefRow label="Palette recents">Erased</DefRow>
            <DefRow label="Recoverable">No</DefRow>
          </div>
          <p className="m-0 text-[10px] text-ink-muted">
            Export the vault first if you need an audit copy. Server-side records are untouched.
          </p>
        </div>
      </Modal>
    </div>
  );
}
