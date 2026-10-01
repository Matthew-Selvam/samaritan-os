/**
 * onboarding.ts — first-run tour content and persistence.
 *
 * Kept apart from `Onboarding.tsx` so the copy, step metadata and
 * acknowledgement state can be unit-tested (and reused by a CLI help command)
 * without pulling in React.
 *
 * The tour is four steps. Step 1 and step 4a share a single screen; the
 * authorized-use acknowledgement is deliberately the *second* step rather than
 * a dismissible footer, because this platform performs OSINT on real people and
 * burying the warning would misrepresent what the tool is.
 */

"use client";

import type { AgentInputType } from "./agentCatalog";

// ── Steps ───────────────────────────────────────────────────────────────────

/** Glyph shown in the step rail. Mirrors the agent catalogue's icon language. */
export type OnboardingIcon = "◎" | "⬡" | "⊕" | "⌗";

/** One screen of the tour. */
export interface OnboardingStep {
  /** Stable id, also used in analytics/logging. */
  readonly id: string;
  /** Short eyebrow above the title. */
  readonly eyebrow: string;
  /** Headline. */
  readonly title: string;
  /** One-paragraph explanation, written for a first-time operator. */
  readonly body: string;
  /** 2–4 concrete points. */
  readonly bullets: readonly string[];
  readonly icon: OnboardingIcon;
  /** The single keyboard hint shown in the footer of this step. */
  readonly hint: string;
  /** Requires an explicit checkbox before "Continue" unlocks. */
  readonly requiresAcknowledgement?: boolean;
}

/** localStorage key recording completion. */
export const ONBOARDING_STORAGE_KEY = "signal-os:onboarding";

/** Version bump invalidates prior completions when the tour changes materially. */
export const ONBOARDING_VERSION = 1;

/** The four steps, in order. */
export const ONBOARDING_STEPS: readonly OnboardingStep[] = [
  {
    id: "what",
    eyebrow: "Step 1 — Orientation",
    title: "Signal-OS is a swarm, not a search box",
    body:
      "You give it one raw input — a phone number, a username, an email, a photo, a URL. " +
      "APEX classifies it, wakes only the agents that suit that input type, and fuses what " +
      "they return into a graph, a timeline and a written report. Nothing is collected " +
      "speculatively: every claim in the output traces back to an agent and a source.",
    bullets: [
      "16 specialised agents, grouped into primary producers, a correlation tier, and the APEX supervisor",
      "Only the agents relevant to the detected input type run — an email never triggers audio transcription",
      "Output is structured: knowledge graph, timeline, entity index, and a Markdown report",
    ],
    icon: "◎",
    hint: "Press → or ⏎ to continue",
  },
  {
    id: "authorization",
    eyebrow: "Step 2 — Authorized use",
    title: "This platform investigates real people",
    body:
      "Signal-OS aggregates publicly available information about identifiable individuals and " +
      "organisations, and assembles it into a dossier. Depending on how you use it, that can be " +
      "entirely lawful — or it can be harassment, doxxing, or a violation of data-protection law. " +
      "The determination is yours, and it does not change because the data was public.",
    bullets: [
      "Use it only for targets you are authorised to investigate: your own systems, a signed engagement, or explicit consent",
      "Do not use collected intelligence to harass, stalk, discriminate, or build profiles of private individuals",
      "Respect the law in your jurisdiction — GDPR, CCPA and equivalents impose duties that public data does not exempt you from",
      "Log what you do. An investigation you cannot justify in a log is one you should not have started",
    ],
    icon: "⬡",
    hint: "Acknowledge to unlock the rest of the tour",
    requiresAcknowledgement: true,
  },
  {
    id: "inputs",
    eyebrow: "Step 3 — The input bar",
    title: "One field, and it decides the whole swarm",
    body:
      "Type or paste a single identifier into the input bar and press ⌘↩. The router " +
      "classifies it — a bare word is usually a username, a dotted string a domain, a leading " +
      "+ a phone number — and shows you its reasoning with a confidence score before it commits. " +
      "If the classification is wrong, force the input type; APEX will then route by your " +
      "choice instead of its own.",
    bullets: [
      "Supported: phone, email, username, domain, URL, IP, crypto wallet, image, audio, video, document, free text",
      "⌘↩ runs the investigation · / returns to this bar from anywhere · ⌘K searches every command",
      "Watch the routing confidence — anything under ~70% is a guess worth correcting",
    ],
    icon: "⊕",
    hint: "The live pipeline trace shows every step as it happens",
  },
  {
    id: "graph",
    eyebrow: "Step 4 — Reading the output",
    title: "The graph is the finding; the report is the summary",
    body:
      "NEXUS links every entity into a knowledge graph and scores influence, so a cluster of " +
      "low-confidence edges pointing at one hub is usually where the real story sits. KRONOS " +
      "reconstructs chronology from eleven-plus date formats. QUILL writes the report. Read the " +
      "graph first, then use the timeline to check whether the correlations survive time.",
    bullets: [
      "Node confidence is the model's belief, not a fact — cross-check the high-weight claims against their sources",
      "Edges come from correlation; a thick edge is not a proof",
      "1–9 jump to a view, ⌘K opens the palette, ? lists every shortcut",
    ],
    icon: "⌗",
    hint: "Press ⇧? any time to reopen the shortcut reference",
  },
] as const;

/** Input types called out in the tour, for a quick-reference strip. */
export const ONBOARDING_EXAMPLE_INPUTS: ReadonlyArray<{
  readonly value: string;
  readonly type: AgentInputType;
  readonly hint: string;
}> = [
  { value: "+14155550123", type: "phone", hint: "PHONOS profiles the number" },
  { value: "j.doe", type: "username", hint: "PRISM resolves it across platforms" },
  { value: "example.com", type: "domain", hint: "CRAWLER fetches, SIGMA checks infrastructure" },
  { value: "https://example.org/profile", type: "url", hint: "SCOUT, CRAWLER, IRIS, SIGMA run" },
] as const;

// ── Persistence ─────────────────────────────────────────────────────────────

/** What we persist about the tour. */
export interface OnboardingRecord {
  /** Schema version — a mismatch re-shows the tour. */
  readonly version: number;
  /** Whether the tour finished, was skipped, or is in progress. */
  readonly status: "completed" | "skipped";
  /** True once the authorized-use acknowledgement was explicitly ticked. */
  readonly acknowledged: boolean;
  /** ISO 8601 timestamp of the last state change. */
  readonly updatedAt: string;
}

/** Has the tour already been dealt with for this visitor? */
export function loadOnboardingRecord(
  storageKey: string = ONBOARDING_STORAGE_KEY,
): OnboardingRecord | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    const candidate = parsed as Partial<OnboardingRecord>;
    if (typeof candidate.version !== "number") return null;
    if (candidate.status !== "completed" && candidate.status !== "skipped") return null;
    return {
      version: candidate.version,
      status: candidate.status,
      acknowledged: candidate.acknowledged === true,
      updatedAt:
        typeof candidate.updatedAt === "string"
          ? candidate.updatedAt
          : new Date(0).toISOString(),
    };
  } catch {
    return null;
  }
}

/** Persist tour state. Never throws — storage may be blocked or full. */
export function saveOnboardingRecord(
  record: OnboardingRecord,
  storageKey: string = ONBOARDING_STORAGE_KEY,
): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(record));
  } catch {
    // Non-fatal: the tour simply reappears next visit.
  }
}

/**
 * Should the tour appear? Only for a first-time visitor — or after the user
 * explicitly resets it, which writes a version bump.
 */
export function shouldShowOnboarding(
  storageKey: string = ONBOARDING_STORAGE_KEY,
  version: number = ONBOARDING_VERSION,
): boolean {
  const record = loadOnboardingRecord(storageKey);
  if (!record) return true;
  if (record.version !== version) return true;
  return false;
}

/** Wipe the record so the tour runs again. */
export function resetOnboarding(storageKey: string = ONBOARDING_STORAGE_KEY): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(storageKey);
  } catch {
    // Nothing to do.
  }
}

/** Clamp a step index into range — protects against keyboard overshoot. */
export function clampStep(index: number, length: number = ONBOARDING_STEPS.length): number {
  if (length <= 0) return 0;
  return Math.min(length - 1, Math.max(0, index));
}

/** Is this the final step? */
export function isLastStep(index: number, length: number = ONBOARDING_STEPS.length): boolean {
  return index >= length - 1;
}