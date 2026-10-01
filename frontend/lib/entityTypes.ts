/**
 * entityTypes.ts — the canonical entity taxonomy registry.
 *
 * Per CONTRACTS.md §3, this file is the single source of truth for entity type
 * colours and the type list; the backend `router.py` may only emit types from
 * here. Colours are carried over verbatim from `components/EntityGraph.tsx`
 * (`TYPE_COLOR`) so the Cytoscape graph, badges, legends and tables all agree.
 *
 * Runtime-safe: pure data + tiny helpers, no React, no browser globals.
 */

import type { EntityType } from "./types";

export type { EntityType };

/** Coarse family used for grouping filters and legend sections. */
export type EntityGroup = "Identity" | "Infrastructure" | "Media" | "Financial" | "Content";

export interface EntityTypeMeta {
  /** Short human label, e.g. "Username". */
  label: string;
  /** Hex colour shared by the graph, badges and legends. */
  color: string;
  /** Single-glyph icon for dense rows. */
  icon: string;
  /** Family used for grouping. */
  group: EntityGroup;
}

/** Every entity type, in display order. */
export const ENTITY_TYPES: readonly EntityType[] = [
  "person",
  "org",
  "account",
  "username",
  "email",
  "phone",
  "domain",
  "url",
  "ip",
  "location",
  "wallet",
  "crypto",
  "image",
  "face",
  "document",
  "note",
  "other",
] as const;

/** Fallback colour for types outside the taxonomy (defensive, never crashes). */
export const DEFAULT_ENTITY_COLOR = "#5a7a9a";

/**
 * Colour + label + icon + group for every canonical entity type.
 *
 * The first nine colours match `TYPE_COLOR` in `components/EntityGraph.tsx`
 * exactly so the graph and the rest of the UI never disagree.
 */
export const ENTITY_TYPE_META: Record<EntityType, EntityTypeMeta> = {
  person:   { label: "Person",     color: "#00d4ff", icon: "◉", group: "Identity" },
  email:    { label: "Email",      color: "#9966ff", icon: "@", group: "Identity" },
  username: { label: "Username",   color: "#ccddff", icon: "@", group: "Identity" },
  account:  { label: "Account",    color: "#7f8cff", icon: "▣", group: "Identity" },
  phone:    { label: "Phone",      color: "#ffcc44", icon: "☎", group: "Identity" },

  domain:   { label: "Domain",     color: "#00ff88", icon: "◈", group: "Infrastructure" },
  url:      { label: "URL",        color: "#44dd88", icon: "↗", group: "Infrastructure" },
  ip:       { label: "IP Address", color: "#ffb020", icon: "◆", group: "Infrastructure" },
  location: { label: "Location",   color: "#35d0d8", icon: "⌖", group: "Infrastructure" },

  image:    { label: "Image",      color: "#ff66aa", icon: "▣", group: "Media" },
  face:     { label: "Face",       color: "#ff8fc0", icon: "☺", group: "Media" },

  wallet:   { label: "Wallet",     color: "#ff8833", icon: "⬢", group: "Financial" },
  crypto:   { label: "Crypto",     color: "#ffaa33", icon: "⬡", group: "Financial" },

  org:      { label: "Org",        color: "#00c8b4", icon: "▤", group: "Identity" },

  document: { label: "Document",   color: "#aabbff", icon: "▤", group: "Content" },
  note:     { label: "Note",       color: "#9fb4c7", icon: "✎", group: "Content" },
  other:    { label: "Other",      color: DEFAULT_ENTITY_COLOR, icon: "○", group: "Content" },
};

/** Groups in presentation order, with their member types. */
export const ENTITY_GROUPS: Readonly<Record<EntityGroup, readonly EntityType[]>> = {
  Identity: ["person", "org", "account", "username", "email", "phone"],
  Infrastructure: ["domain", "url", "ip", "location"],
  Media: ["image", "face"],
  Financial: ["wallet", "crypto"],
  Content: ["document", "note", "other"],
} as const;

/** Group display order for filters and legends. */
export const ENTITY_GROUP_ORDER: readonly EntityGroup[] = [
  "Identity",
  "Infrastructure",
  "Media",
  "Financial",
  "Content",
] as const;

function isEntityType(value: string): value is EntityType {
  return Object.prototype.hasOwnProperty.call(ENTITY_TYPE_META, value);
}

/**
 * Normalise an arbitrary string from the backend into an {@link EntityType}.
 *
 * Aliases seen in the wild (`ip_address`, `crypto_wallet`, `twitter`, ...) are
 * folded onto canonical types; anything unrecognised becomes `"other"` so a
 * rogue type can never crash a render.
 *
 * @param value - Raw type string from the API.
 * @returns The canonical entity type.
 */
export function normalizeEntityType(value: string | null | undefined): EntityType {
  if (!value) return "other";
  const raw = value.trim().toLowerCase().replace(/[\s-]+/g, "_");
  if (isEntityType(raw)) return raw;
  switch (raw) {
    case "ip_address":
    case "ipv4":
    case "ipv6":
      return "ip";
    case "crypto_wallet":
    case "bitcoin":
    case "btc":
    case "ethereum":
    case "eth":
      return "crypto";
    case "face_match":
    case "facial":
      return "face";
    case "company":
    case "organisation":
    case "organization":
      return "org";
    case "handle":
    case "social":
    case "screen_name":
      return "username";
    case "site":
    case "hostname":
    case "domain_name":
      return "domain";
    case "picture":
    case "photo":
    case "media":
      return "image";
    case "text":
    case "comment":
      return "note";
    default:
      return "other";
  }
}

/**
 * Look up the full metadata for a type.
 * @param type - Canonical or raw type string.
 * @returns The metadata record; falls back to the `other` entry.
 */
export function metaFor(type: EntityType | string): EntityTypeMeta {
  const canonical = normalizeEntityType(type);
  return ENTITY_TYPE_META[canonical] ?? ENTITY_TYPE_META.other;
}

/**
 * Display label for an entity type.
 * @param type - Canonical or raw type string.
 * @returns e.g. `"IP Address"` for `"ip"`, `"Other"` for anything unknown.
 */
export function labelFor(type: EntityType | string): string {
  return metaFor(type).label;
}

/**
 * Hex colour for an entity type.
 * @param type - Canonical or raw type string.
 * @returns The shared hex colour, e.g. `"#00d4ff"` for `"person"`.
 */
export function colorFor(type: EntityType | string): string {
  return metaFor(type).color;
}

/**
 * Icon glyph for an entity type.
 * @param type - Canonical or raw type string.
 * @returns A single-character glyph safe to render in a mono font.
 */
export function iconFor(type: EntityType | string): string {
  return metaFor(type).icon;
}

/**
 * Group family for an entity type.
 * @param type - Canonical or raw type string.
 * @returns One of Identity / Infrastructure / Media / Financial / Content.
 */
export function groupFor(type: EntityType | string): EntityGroup {
  return metaFor(type).group;
}

/**
 * Types belonging to one group, in registry order.
 * @param group - The family to enumerate.
 * @returns Its member types.
 */
export function typesInGroup(group: EntityGroup): readonly EntityType[] {
  return ENTITY_GROUPS[group] ?? [];
}

export default ENTITY_TYPE_META;