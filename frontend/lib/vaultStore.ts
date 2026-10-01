/**
 * vaultStore.ts — the client-side half of the KNOWLEDGE view.
 *
 * The backend has no vault routes yet, so persistent cross-case memory is kept
 * in `localStorage` behind one tiny typed module. Every consumer goes through
 * these helpers, so swapping the storage to an HTTP call later is a change to
 * this file alone — and the GDPR purge control has exactly one implementation.
 *
 * This module is intentionally NOT `use client`: it is pure data access that
 * only touches `window` inside a guarded branch, so it can also be imported by
 * tests or a future server component.
 */

import type { EntityType } from "./types";

const ENTITIES_KEY = "signal-os:vault:entities";
const NOTES_KEY = "signal-os:vault:notes";

/** An entity the analyst pinned into the vault. */
export interface VaultEntity {
  /** Stable key: `${type}:${value}`. */
  id: string;
  label: string;
  type: EntityType;
  value?: string;
  confidence?: number;
  source?: string;
  caseId?: string;
  savedAt: string;
}

/** A free-text analyst note. */
export interface VaultNote {
  id: string;
  title: string;
  body: string;
  tags?: string[];
  caseId?: string;
  savedAt: string;
}

/** Build the canonical vault id for an entity. */
export function vaultEntityId(type: EntityType | string, value: string): string {
  return `${type}:${value.trim().toLowerCase()}`;
}

function read<T>(key: string, fallback: T): T {
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as unknown;
    return Array.isArray(parsed) ? (parsed as T) : fallback;
  } catch {
    return fallback;
  }
}

function write(key: string, value: unknown): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* quota exceeded or private mode — surfaced by the view's toast */
  }
}

/** Every pinned entity, newest first. */
export function loadVaultEntities(): VaultEntity[] {
  return read<VaultEntity[]>(ENTITIES_KEY, []).slice().sort((a, b) =>
    b.savedAt.localeCompare(a.savedAt),
  );
}

/** Persist the pinned-entity list. */
export function saveVaultEntities(items: readonly VaultEntity[]): void {
  write(ENTITIES_KEY, items);
}

/**
 * Pin an entity, de-duplicating by id and keeping the newest save time.
 * @returns The next list — callers should re-render from the return value.
 */
export function addVaultEntity(
  list: readonly VaultEntity[],
  entity: Omit<VaultEntity, "id" | "savedAt"> & { id?: string; savedAt?: string },
): VaultEntity[] {
  const id = entity.id ?? vaultEntityId(entity.type, entity.value ?? entity.label);
  const next: VaultEntity = {
    id,
    label: entity.label,
    type: entity.type,
    value: entity.value,
    confidence: entity.confidence,
    source: entity.source,
    caseId: entity.caseId,
    savedAt: entity.savedAt ?? new Date().toISOString(),
  };
  return [next, ...list.filter((item) => item.id !== id)];
}

/** Remove one pinned entity by id. */
export function removeVaultEntity(
  list: readonly VaultEntity[],
  id: string,
): VaultEntity[] {
  return list.filter((item) => item.id !== id);
}

/** Every note, newest first. */
export function loadVaultNotes(): VaultNote[] {
  return read<VaultNote[]>(NOTES_KEY, []).slice().sort((a, b) =>
    b.savedAt.localeCompare(a.savedAt),
  );
}

/** Persist the note list. */
export function saveVaultNotes(items: readonly VaultNote[]): void {
  write(NOTES_KEY, items);
}

/** Insert or update a note keyed by id. */
export function upsertVaultNote(
  list: readonly VaultNote[],
  note: Omit<VaultNote, "savedAt"> & { savedAt?: string },
): VaultNote[] {
  const next: VaultNote = { ...note, savedAt: note.savedAt ?? new Date().toISOString() };
  const rest = list.filter((item) => item.id !== next.id);
  return [next, ...rest].sort((a, b) => b.savedAt.localeCompare(a.savedAt));
}

/** Remove one note by id. */
export function removeVaultNote(list: readonly VaultNote[], id: string): VaultNote[] {
  return list.filter((item) => item.id !== id);
}

/** Count of everything the purge control will delete. */
export function vaultFootprint(): { entities: number; notes: number } {
  return { entities: loadVaultEntities().length, notes: loadVaultNotes().length };
}

/**
 * GDPR purge: erase every locally retained analyst artefact.
 *
 * Clears both vault keys plus the palette recents owned by `lib/palette.ts`,
 * which is why this is a single function rather than two calls in the view.
 */
export function purgeVault(): void {
  if (typeof window === "undefined") return;
  for (const key of [ENTITIES_KEY, NOTES_KEY, "signal-os:palette-recents"]) {
    try {
      window.localStorage.removeItem(key);
    } catch {
      /* nothing else we can do */
    }
  }
}

/** A lexical relevance score for vault search (label/value/note body). */
export function vaultScore(haystack: string, query: string): number {
  const needle = query.trim().toLowerCase();
  if (!needle) return 1;
  const hay = haystack.toLowerCase();
  if (hay === needle) return 1000;
  if (hay.startsWith(needle)) return 500;
  const index = hay.indexOf(needle);
  if (index >= 0) return 300 - Math.min(index, 100);
  // Subsequence match, so "jdk" still finds "john.doe.k".
  let cursor = 0;
  for (const ch of needle) {
    const found = hay.indexOf(ch, cursor);
    if (found < 0) return 0;
    cursor = found + 1;
  }
  return 50;
}
