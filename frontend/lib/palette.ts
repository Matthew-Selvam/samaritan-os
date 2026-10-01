/**
 * palette.ts — the ⌘K command palette **engine**.
 *
 * Deliberately pure: scoring, ranking, recency and search composition all live
 * here with no React and no DOM, so they can be unit-tested in isolation
 * ({@link fuzzyScore} is the unit-testable core) and so the palette component
 * stays a thin view over this module.
 *
 * Ranking model, in priority order:
 *   1. Higher {@link fuzzyScore}.
 *   2. A shorter haystack (tighter match wins over a sprawling one).
 *   3. Recency — items in {@link RecentCommands} get a decaying bonus.
 *   4. Static commands before dynamic (backend) items, for exact ties.
 */

"use client";

// ── Scoring weights ─────────────────────────────────────────────────────────

/** Bonus applied to a match whose first character lands on a word boundary. */
export const SCORE_BOUNDARY_BONUS = 50;
/** Bonus when the query matches an uppercase acronym ("nk" → "NEXUS/KRONOS"). */
export const SCORE_ACRONYM_BONUS = 30;
/** Bonus when the match lands on a camelCase hump ("cl" → "…CommandList"). */
export const SCORE_CAMEL_BONUS = 24;
/**
 * Bonus per matched character beyond the first. Scaled so contiguity matters
 * less than *where* a match starts — otherwise "cmd" inside "acmd" (contiguous,
 * substring) outranks "cmd" at the head of "command".
 */
export const SCORE_CONSECUTIVE_BONUS = 6;
/** Penalty per skipped character between matched characters. */
export const SCORE_GAP_PENALTY = 2;
/** Bonus for matching the item's id/keywords as well as its label. */
export const SCORE_KEYWORD_BONUS = 8;
/** Bonus when the whole query appears as a substring (strong signal). */
export const SCORE_SUBSTRING_BONUS = 26;

/** Minimum score for an item to be considered a match at all. */
export const SCORE_MIN_THRESHOLD = 1;

/** Recency bonus for the most-recently-used item (decays by rank). */
export const RECENCY_BONUS_MAX = 45;
/** Divisor applied per position of recency rank (index 0 = newest). */
export const RECENCY_DECAY = 3;

/** Upper bound on persisted recents — keeps localStorage small and bounded. */
export const RECENT_LIMIT = 8;

/** How the palette reached an item, used for section headings. */
export type CommandSource = "static" | "dynamic";

/** Base shape of anything the palette can surface. */
export interface PaletteItem {
  /** Stable unique id; doubles as the recents key. */
  readonly id: string;
  /** Primary text. */
  readonly label: string;
  /** Optional secondary line shown under the label. */
  readonly hint?: string;
  /** Extra searchable terms (category, agent role, keywords…). */
  readonly keywords?: readonly string[];
  /** Glyph or icon key rendered before the label. */
  readonly icon?: string;
  /** Where it came from — dynamic items are fetched from the backend. */
  readonly source: CommandSource;
  /** Payload returned to the caller when the item is chosen. */
  readonly payload?: unknown;
  /** Optional right-aligned trailing metadata (e.g. "◉ 16 agents"). */
  readonly trailing?: string;
}

/** A scored, ranked search hit. */
export interface PaletteResult extends PaletteItem {
  /** Higher is better. */
  readonly score: number;
  /** Character ranges in `label` that matched, for highlighting. */
  readonly matches: readonly [number, number][];
}

/** Result of {@link scoreItem} — `null` when the item does not match at all. */
export interface ScoreResult {
  readonly score: number;
  readonly matches: readonly [number, number][];
}

/**
 * Classify a character position in a haystack.
 * `lower` | `upper` | `digit` | `space` | `separator`.
 */
type CharClass = "lower" | "upper" | "digit" | "space" | "separator";

function classify(ch: string): CharClass {
  if (ch === " " || ch === "\t" || ch === "\n") return "space";
  if (/[-_./\\:;,[\](){}<>|'"`~!@#$%^&*+=]/.test(ch)) return "separator";
  if (ch >= "0" && ch <= "9") return "digit";
  if (ch >= "A" && ch <= "Z") return "upper";
  return "lower";
}

/** Is the character at `index` a boundary — the first letter of a new word? */
function isBoundary(haystack: string, index: number): boolean {
  if (index === 0) return true;
  const here = classify(haystack[index] ?? "");
  if (here === "space" || here === "separator") return true;
  const prev = classify(haystack[index - 1] ?? "");
  // lower/digit → upper  == camelCase hump
  // upper → upper followed by lower == acronym start (e.g. "HTTPServer")
  if (prev === "lower" && here === "upper") return true;
  if (prev === "digit" && here === "upper") return true;
  if (
    prev === "upper" &&
    here === "upper" &&
    classify(haystack[index + 1] ?? "") === "lower"
  ) {
    return true;
  }
  return false;
}

/**
 * Score one candidate string against a query.
 *
 * Pure and synchronous. Returns `null` when every character of the query cannot
 * be found in order (a subsequence match is required).
 *
 * Bonuses: word-boundary starts, camelCase humps, acronym starts, contiguous
 * runs; penalties for gaps. Substring matches get a large flat bonus.
 */
export function fuzzyScore(
  query: string,
  haystack: string,
  options?: { readonly caseSensitive?: boolean },
): ScoreResult | null {
  const q = options?.caseSensitive ? query : query.toLowerCase();
  const h = options?.caseSensitive ? haystack : haystack.toLowerCase();
  const hRaw = haystack;

  if (q.length === 0) return { score: 0, matches: [] };
  if (h.length === 0) return null;
  if (q.length > h.length) return null;

  const indices: number[] = [];
  let hIndex = 0;
  for (let qIndex = 0; qIndex < q.length; qIndex++) {
    const needle = q[qIndex];
    let found = -1;
    while (hIndex < h.length) {
      if (h[hIndex] === needle) {
        found = hIndex;
        hIndex++;
        break;
      }
      hIndex++;
    }
    if (found === -1) return null;
    indices.push(found);
  }

  // Substring exactness. Weighted down because a substring match in the middle
  // of a long word (…acmd…) is a weaker signal than a clean boundary match at
  // the start of a shorter word (cmd → command palette).
  const substringHit = h.includes(q);
  let score = substringHit ? SCORE_SUBSTRING_BONUS : 0;
  if (hRaw === q) score += 60; // exact equality

  const matches: [number, number][] = [];
  for (let i = 0; i < indices.length; i++) {
    const at = indices[i];
    let bonus = 0;
    if (i === 0) {
      if (isBoundary(hRaw, at)) {
        bonus += SCORE_BOUNDARY_BONUS;
        // camelCase hump / acronym start — only meaningful at a boundary.
        const cls = classify(hRaw[at] ?? "");
        if (cls === "upper") {
          bonus += substringHit ? SCORE_ACRONYM_BONUS : SCORE_CAMEL_BONUS;
        }
      }
    } else {
      const gap = at - (indices[i - 1] ?? at) - 1;
      if (gap === 0) bonus += SCORE_CONSECUTIVE_BONUS;
      else bonus -= gap * SCORE_GAP_PENALTY;
    }
    score += bonus;

    // Merge adjacent matches into ranges for highlighting.
    const last = matches[matches.length - 1];
    if (last && last[1] === at) last[1] = at + 1;
    else matches.push([at, at + 1]);
  }

  // Prefer matches that finish early and densely, and that skip fewer
  // characters overall — a match must be able to explain its position.
  const span = (indices[indices.length - 1] ?? 0) - (indices[0] ?? 0) + 1;
  // Reward *density* (matched / span) so a short query landing entirely at a
  // boundary beats the same query landing mid-word, even when only the latter
  // is a literal substring.
  const density = q.length / span;
  score += Math.round(density * 18);
  score += Math.max(0, 12 - span);
  // Length penalty, gentle, so "graph" beats "graph of everything".
  score -= Math.floor(h.length / 24);

  return { score, matches };
}

/** Best score for an item across label, hint and keywords. */
export function scoreItem(item: PaletteItem, query: string): ScoreResult | null {
  if (query.length === 0) {
    return { score: 0, matches: [] };
  }
  const label = fuzzyScore(query, item.label);
  const keywordBest = (item.keywords ?? []).reduce<ScoreResult | null>((best, kw) => {
    const scored = fuzzyScore(query, kw);
    if (!scored) return best;
    if (!best || scored.score > best.score) return scored;
    return best;
  }, null);

  if (!label && !keywordBest) return null;

  if (label && keywordBest) {
    // Prefer label matches for highlight ranges, but reward a keyword hit.
    const score = Math.max(label.score, keywordBest.score + SCORE_KEYWORD_BONUS);
    return { score, matches: label.matches };
  }
  return label ?? keywordBest;
}

/** Recent-command record stored in localStorage. */
export interface RecentEntry {
  readonly id: string;
  /** Epoch ms of last use. */
  readonly usedAt: number;
}

/** Recent ids, newest first, as loaded from storage. */
export type RecentList = readonly RecentEntry[];

/**
 * Rank a set of items for a query. Pure; `recentIds` and `recencyIndexById`
 * are supplied by the caller so ranking stays testable.
 */
export function rankItems(
  items: readonly PaletteItem[],
  query: string,
  recencyIndexById?: ReadonlyMap<string, number>,
): readonly PaletteResult[] {
  const scored: PaletteResult[] = [];
  for (const item of items) {
    const result = scoreItem(item, query);
    if (!result) continue;
    if (query.length > 0 && result.score < SCORE_MIN_THRESHOLD) continue;
    let score = result.score;
    const recencyRank = recencyIndexById?.get(item.id);
    if (typeof recencyRank === "number") {
      score += Math.max(0, RECENCY_BONUS_MAX - recencyRank * RECENCY_DECAY);
    }
    scored.push({ ...item, score, matches: result.matches });
  }

  return scored.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    if (a.label.length !== b.label.length) return a.label.length - b.label.length;
    if (a.source !== b.source) return a.source === "static" ? -1 : 1;
    return a.id.localeCompare(b.id);
  });
}

/** Build a recency lookup map from a recent list (newest → oldest). */
export function recencyIndexMap(recents: RecentList): Map<string, number> {
  const map = new Map<string, number>();
  recents.forEach((entry, index) => {
    if (!map.has(entry.id)) map.set(entry.id, index);
  });
  return map;
}

// ── Persistence ─────────────────────────────────────────────────────────────

/** localStorage key for recents. */
export const RECENT_STORAGE_KEY = "signal-os:palette-recents";

/** Safely read recents; corrupt or unavailable storage yields an empty list. */
export function loadRecents(storageKey = RECENT_STORAGE_KEY): RecentList {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const valid: RecentEntry[] = [];
    for (const item of parsed) {
      if (
        typeof item === "object" &&
        item !== null &&
        typeof (item as RecentEntry).id === "string" &&
        typeof (item as RecentEntry).usedAt === "number"
      ) {
        valid.push({ id: (item as RecentEntry).id, usedAt: (item as RecentEntry).usedAt });
      }
    }
    return valid.slice(0, RECENT_LIMIT);
  } catch {
    return [];
  }
}

/** Persist recents, newest-first, capped at {@link RECENT_LIMIT}. */
export function saveRecents(
  recents: RecentList,
  storageKey = RECENT_STORAGE_KEY,
): void {
  if (typeof window === "undefined") return;
  try {
    const capped = recents.slice(0, RECENT_LIMIT);
    window.localStorage.setItem(storageKey, JSON.stringify(capped));
  } catch {
    // Storage may be full or blocked (private mode). Recents are a nicety;
    // failing to persist must never break the palette.
  }
}

/**
 * Move an id to the front of the recents list (pure — the caller persists).
 * Duplicate ids are collapsed; the cap is applied.
 */
export function touchRecent(
  recents: RecentList,
  id: string,
  now = Date.now(),
): RecentList {
  const filtered = recents.filter((entry) => entry.id !== id);
  return [{ id, usedAt: now }, ...filtered].slice(0, RECENT_LIMIT);
}

/** Clear recents (pure; returns an empty list). */
export function clearRecents(): RecentList {
  return [];
}

// ── Async command providers ─────────────────────────────────────────────────

/**
 * Supplies palette items from the backend. Implementations must resolve to a
 * (possibly empty) list and must never throw — the palette swallows errors and
 * keeps showing static commands.
 */
export interface PaletteProvider {
  /** Unique provider id, used in error logging only. */
  readonly id: string;
  /** Fetch items. Receives the current query; may debounce or short-circuit. */
  fetch(query: string, signal?: AbortSignal): Promise<readonly PaletteItem[]>;
}

/** Aggregate several providers, isolating failures. */
export async function collectDynamicItems(
  providers: readonly PaletteProvider[],
  query: string,
  signal?: AbortSignal,
): Promise<readonly PaletteItem[]> {
  const settled = await Promise.allSettled(
    providers.map(async (provider) => {
      try {
        return await provider.fetch(query, signal);
      } catch {
        return [] as readonly PaletteItem[];
      }
    }),
  );
  const out: PaletteItem[] = [];
  for (const result of settled) {
    if (result.status === "fulfilled") out.push(...result.value);
  }
  return out;
}

/** Deduplicate by id, static items winning ties. */
export function mergeItems(
  statics: readonly PaletteItem[],
  dynamics: readonly PaletteItem[],
): readonly PaletteItem[] {
  const seen = new Set<string>();
  const out: PaletteItem[] = [];
  for (const item of [...dynamics, ...statics]) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    out.push(item);
  }
  return out;
}

/**
 * Full palette search: score over the union of static and dynamic items, with
 * recency boosting. Pure — providers are resolved by the caller.
 */
export function searchPalette(
  statics: readonly PaletteItem[],
  dynamics: readonly PaletteItem[],
  query: string,
  recents: RecentList = [],
): readonly PaletteResult[] {
  const items = mergeItems(statics, dynamics);
  const withRecency = query.length === 0 ? recents.slice(0, RECENT_LIMIT) : [];
  return rankItems(items, query, recencyIndexMap(withRecency));
}

/** Split ranked results into recents section and full search results. */
export function splitResults(
  results: readonly PaletteResult[],
  recents: RecentList,
  query: string,
): { recent: readonly PaletteResult[]; rest: readonly PaletteResult[] } {
  if (query.length > 0) {
    return { recent: [], rest: results };
  }
  const recentIds = new Set(recents.slice(0, RECENT_LIMIT).map((r) => r.id));
  const recent: PaletteResult[] = [];
  const rest: PaletteResult[] = [];
  for (const result of results) {
    if (recentIds.has(result.id)) recent.push(result);
    else rest.push(result);
  }
  return { recent, rest };
}

/**
 * Move the selection by `delta`, wrapping at both ends — the behaviour VS Code
 * and fzf users expect from a listbox.
 */
export function moveSelection(
  length: number,
  current: number,
  delta: number,
): number {
  if (length <= 0) return 0;
  return (current + delta + length * Math.ceil(Math.abs(delta) / length)) % length;
}

/**
 * Group results into sections for rendering. Pure; preserves ranking within
 * each section.
 */
export function groupBySource(
  results: readonly PaletteResult[],
): ReadonlyArray<{ source: CommandSource; items: readonly PaletteResult[] }> {
  const statics = results.filter((r) => r.source === "static");
  const dynamics = results.filter((r) => r.source === "dynamic");
  const out: { source: CommandSource; items: readonly PaletteResult[] }[] = [];
  if (statics.length) out.push({ source: "static", items: statics });
  if (dynamics.length) out.push({ source: "dynamic", items: dynamics });
  return out;
}