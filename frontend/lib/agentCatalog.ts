/**
 * agentCatalog.ts — the complete 16-agent Signal-OS catalogue for the UI.
 *
 * This is a *typed mirror* of three backend sources of truth, deliberately kept
 * in one place so no component re-derives agent metadata:
 *
 *   1. `backend/agents/__init__.py`  → `AGENT_REGISTRY` (the 16 names, and
 *      nothing else may be added without changing that dict).
 *   2. `backend/agents/<agent>.py`   → `name` / `role` / `icon` / `description` /
 *      `preferred_models` / `token_budget`, copied verbatim below.
 *   3. `backend/router.py`           → `AGENT_MAP`, the input-type → agent
 *      activation table, mirrored by {@link AGENT_ROUTING_MAP}.
 *
 * Type-only module (no React, no DOM, no side effects) so it can be imported
 * from server components, client components and plain node scripts alike.
 */

/** Union of every agent in `backend.agents.AGENT_REGISTRY`. */
export type AgentName =
  | "SCOUT"
  | "PHONOS"
  | "EMAIL"
  | "CRAWLER"
  | "IRIS"
  | "ECHO"
  | "PRISM"
  | "TERRA"
  | "INK"
  | "NEXUS"
  | "KRONOS"
  | "VAULT"
  | "SENTINEL"
  | "QUILL"
  | "SIGMA"
  | "APEX";

/**
 * Execution tier, mirroring README §"Modular Design":
 * `primary` agents run concurrently and produce signals; `correlation` agents
 * run afterwards over the aggregated output; `supervisor` routes everything.
 */
export type AgentTier = "primary" | "correlation" | "supervisor";

/**
 * Input types the router can detect (`backend/router.py::InputType`). Kept
 * structural rather than importing from `@/lib/types` because the canonical
 * union lives in the backend enum; this list must stay in lock-step with it.
 */
export type AgentInputType =
  | "username"
  | "email"
  | "phone"
  | "domain"
  | "ip_address"
  | "crypto_wallet"
  | "url"
  | "image"
  | "photo"
  | "face"
  | "person_name"
  | "video"
  | "audio"
  | "document"
  | "text"
  | "unknown";

/** Every input type, in router declaration order. */
export const AGENT_INPUT_TYPES: readonly AgentInputType[] = [
  "username",
  "email",
  "phone",
  "domain",
  "ip_address",
  "crypto_wallet",
  "url",
  "image",
  "photo",
  "face",
  "person_name",
  "video",
  "audio",
  "document",
  "text",
  "unknown",
] as const;

/** Human labels for the input-type union, used by routing pickers and legends. */
export const AGENT_INPUT_TYPE_LABELS: Readonly<Record<AgentInputType, string>> = {
  username: "Username",
  email: "Email address",
  phone: "Phone number",
  domain: "Domain",
  ip_address: "IP address",
  crypto_wallet: "Crypto wallet",
  url: "URL",
  image: "Image",
  photo: "Photo",
  face: "Face",
  person_name: "Person name",
  video: "Video",
  audio: "Audio",
  document: "Document",
  text: "Free text",
  unknown: "Unclassified",
};

/**
 * Model identifiers an agent prefers. `KnownAgentModel` covers every value that
 * actually appears in the backend today; the `string & {}` arm keeps the type
 * open for locally-added models without collapsing autocomplete.
 */
export type KnownAgentModel =
  | "qwen2.5:7b"
  | "gemma2:9b"
  | "claude-opus-4"
  | "phi3:mini"
  | "Florence-2"
  | "whisper";

export type AgentModelId = KnownAgentModel | (string & {});

/** OSINT connector modules under `backend/connectors/`. */
export type ConnectorName =
  | "searxng"
  | "theharvester"
  | "spiderfoot"
  | "phone_intel"
  | "email_enum"
  | "exif"
  | "face_embed"
  | "reverse_image"
  | "sherlock"
  | "people_search"
  | "breach_check"
  | "shodan"
  | "net";

/** Inclusive latency band, in milliseconds, for a single agent run. */
export interface LatencyRange {
  /** Lower bound in ms. */
  readonly min: number;
  /** Upper bound in ms. */
  readonly max: number;
}

/** Full UI descriptor for one agent. */
export interface AgentCatalogEntry {
  /** Registry key — must match `AGENT_REGISTRY` in the backend. */
  readonly name: AgentName;
  /** Short uppercase-friendly display label (equals `name` today). */
  readonly label: string;
  /** Single-glyph icon from the backend's `icon` class attribute. */
  readonly icon: string;
  /** Backend `role` attribute, verbatim. */
  readonly role: string;
  /** Condensed role for dense UI (mirrors the existing AgentGrid captions). */
  readonly shortRole: string;
  /** Backend `description`, verbatim. */
  readonly description: string;
  /** When the agent runs relative to the swarm. */
  readonly tier: AgentTier;
  /** Input types whose routing decision activates this agent. */
  readonly inputTypes: readonly AgentInputType[];
  /** Backend `preferred_models`, first available wins. */
  readonly preferredModels: readonly AgentModelId[];
  /** Backend `token_budget` — max tokens per run. */
  readonly tokenBudget: number;
  /** Typical wall-clock latency band for one run. */
  readonly latency: LatencyRange;
  /** Report sections / output fields this agent contributes to. */
  readonly contributesTo: readonly string[];
  /** Connector modules imported by the agent. Empty = pure-LLM / offline. */
  readonly connectors: readonly ConnectorName[];
  /** Env vars that unlock live enrichment; empty = works with no configuration. */
  readonly requiresEnv: readonly string[];
  /** Accent colour (hex), consistent with the existing AgentGrid palette. */
  readonly color: string;
}

/**
 * The catalogue, ordered the way APEX activates agents: primary producers
 * first (router order), then the correlation tier, then the supervisor.
 */
export const AGENT_CATALOG: readonly AgentCatalogEntry[] = [
  {
    name: "SCOUT",
    label: "Scout",
    icon: "◎",
    role: "Search Intelligence",
    shortRole: "Search Intel",
    description:
      "Runs dorks, search federation, query expansion across Google/Bing/Yandex/DuckDuckGo",
    tier: "primary",
    inputTypes: ["username", "email", "phone", "domain", "url", "person_name", "unknown"],
    preferredModels: ["qwen2.5:7b", "gemma2:9b"],
    tokenBudget: 4096,
    latency: { min: 2500, max: 9000 },
    contributesTo: ["dorks", "search results", "emails", "subdomains", "hosts"],
    connectors: ["searxng", "theharvester", "spiderfoot"],
    requiresEnv: ["SEARXNG_URL"],
    color: "#00d4ff",
  },
  {
    name: "PHONOS",
    label: "Phonos",
    icon: "☏",
    role: "Phone Intelligence",
    shortRole: "Phone Intel",
    description:
      "Phone number workup: carrier, line type, region, timezone, breach exposure, cross-reference pivots",
    tier: "primary",
    inputTypes: ["phone"],
    preferredModels: ["qwen2.5:7b", "gemma2:9b"],
    tokenBudget: 6144,
    latency: { min: 600, max: 2500 },
    contributesTo: ["carrier", "region", "line type", "timezones", "breach exposure"],
    connectors: ["phone_intel"],
    requiresEnv: ["NUMVERIFY_API_KEY", "DEHASHED_EMAIL", "DEHASHED_API_KEY"],
    color: "#4dd0ff",
  },
  {
    name: "EMAIL",
    label: "Email",
    icon: "✉",
    role: "Email Intelligence",
    shortRole: "Email Intel",
    description:
      "Email account existence enumeration across 50+ providers (Gmail, Yahoo, Outlook, Proton…)",
    tier: "primary",
    inputTypes: ["email"],
    preferredModels: ["qwen2.5:7b"],
    tokenBudget: 4096,
    latency: { min: 1500, max: 6000 },
    contributesTo: ["account existence", "provider", "gravatar", "breach exposure"],
    connectors: ["email_enum"],
    requiresEnv: [],
    color: "#7aa2ff",
  },
  {
    name: "CRAWLER",
    label: "Crawler",
    icon: "⟨/⟩",
    role: "Web Scraper",
    shortRole: "Web Scraper",
    description:
      "Structured data extraction, website parsing, browser automation (Playwright)",
    tier: "primary",
    inputTypes: ["domain", "url", "document"],
    preferredModels: ["qwen2.5:7b"],
    tokenBudget: 4096,
    latency: { min: 1200, max: 8000 },
    contributesTo: ["page text", "links", "metadata", "emails", "structured records"],
    connectors: [],
    requiresEnv: [],
    color: "#00d4ff",
  },
  {
    name: "IRIS",
    label: "Iris",
    icon: "◉",
    role: "Vision Intelligence",
    shortRole: "Vision Intel",
    description:
      "Face detection, EXIF extraction, reverse image search, face embedding cross-match",
    tier: "primary",
    inputTypes: ["url", "image", "photo", "face", "video", "document", "unknown"],
    preferredModels: ["Florence-2", "gemma2:9b"],
    tokenBudget: 8192,
    latency: { min: 3000, max: 12000 },
    contributesTo: ["EXIF", "faces", "reverse image matches", "camera metadata"],
    connectors: ["exif", "face_embed", "reverse_image"],
    requiresEnv: [],
    color: "#ff66aa",
  },
  {
    name: "ECHO",
    label: "Echo",
    icon: "~",
    role: "Audio Intelligence",
    shortRole: "Audio Intel",
    description:
      "Transcription (Whisper), accent detection, background environment analysis (YAMNet)",
    tier: "primary",
    inputTypes: ["video", "audio"],
    preferredModels: ["whisper"],
    tokenBudget: 4096,
    latency: { min: 2000, max: 30000 },
    contributesTo: ["transcript", "accent", "environmental cues"],
    connectors: [],
    requiresEnv: ["WHISPER_MODEL"],
    color: "#ffb020",
  },
  {
    name: "PRISM",
    label: "Prism",
    icon: "◈",
    role: "Social Intelligence",
    shortRole: "Social Intel",
    description:
      "Cross-platform identity resolution, account clustering, breach correlation",
    tier: "primary",
    inputTypes: [
      "username",
      "email",
      "photo",
      "face",
      "person_name",
      "text",
    ],
    preferredModels: ["gemma2:9b"],
    tokenBudget: 8192,
    latency: { min: 4000, max: 15000 },
    contributesTo: ["matched accounts", "profile bios", "breach records", "identity clusters"],
    connectors: ["sherlock", "people_search", "breach_check"],
    requiresEnv: ["HIBP_API_KEY", "DEHASHED_EMAIL", "DEHASHED_API_KEY"],
    color: "#9966ff",
  },
  {
    name: "TERRA",
    label: "Terra",
    icon: "⊛",
    role: "GEOINT",
    shortRole: "GEOINT",
    description:
      "Geolocation from images, environmental inference, architecture/vegetation analysis",
    tier: "primary",
    inputTypes: ["ip_address", "image", "photo", "video"],
    preferredModels: ["Florence-2", "gemma2:9b"],
    tokenBudget: 6144,
    latency: { min: 2500, max: 9000 },
    contributesTo: ["GPS coordinates", "city/country", "environmental context"],
    connectors: ["exif"],
    requiresEnv: [],
    color: "#44dd88",
  },
  {
    name: "INK",
    label: "Ink",
    icon: "✦",
    role: "Stylometry",
    shortRole: "Stylometry",
    description:
      "Writing fingerprinting, authorship analysis, cross-platform bio matching",
    tier: "primary",
    inputTypes: ["audio", "document", "person_name", "text", "unknown"],
    preferredModels: ["qwen2.5:7b"],
    tokenBudget: 8192,
    latency: { min: 1500, max: 6000 },
    contributesTo: ["authorship fingerprint", "vocabulary profile", "bio matches"],
    connectors: [],
    requiresEnv: [],
    color: "#aabbff",
  },
  {
    name: "SIGMA",
    label: "Sigma",
    icon: "⊗",
    role: "Threat Intelligence",
    shortRole: "Threat Intel",
    description:
      "IOC ingestion, phishing detection, breach correlation, infrastructure mapping",
    tier: "primary",
    inputTypes: ["email", "domain", "ip_address", "crypto_wallet", "url"],
    preferredModels: ["gemma2:9b"],
    tokenBudget: 8192,
    latency: { min: 1200, max: 6000 },
    contributesTo: ["open ports", "services", "CVEs", "host geolocation", "DNS records"],
    connectors: ["shodan"],
    requiresEnv: ["SHODAN_API_KEY"],
    color: "#ff8833",
  },
  {
    // ── Correlation tier — sequential, over aggregated output ──────────────
    name: "NEXUS",
    label: "Nexus",
    icon: "∞",
    role: "Correlation Engine",
    shortRole: "Correlation",
    description:
      "Hidden relationship detection, graph clustering, influence scoring, semantic linking",
    tier: "correlation",
    inputTypes: [
      "username",
      "email",
      "phone",
      "domain",
      "ip_address",
      "crypto_wallet",
      "image",
      "photo",
      "face",
      "person_name",
      "text",
    ],
    preferredModels: ["gemma2:9b"],
    tokenBudget: 12288,
    latency: { min: 1000, max: 5000 },
    contributesTo: ["knowledge graph", "clusters", "influence scores", "derived edges"],
    connectors: [],
    requiresEnv: [],
    color: "#ff6644",
  },
  {
    name: "KRONOS",
    label: "Kronos",
    icon: "⊕",
    role: "Timeline Reconstruction",
    shortRole: "Timeline",
    description:
      "Chronology reconstruction: username changes, post history, account creation, location changes",
    tier: "correlation",
    inputTypes: [],
    preferredModels: ["gemma2:9b"],
    tokenBudget: 6144,
    latency: { min: 800, max: 4000 },
    contributesTo: ["timeline events", "chronology", "date provenance"],
    connectors: [],
    requiresEnv: [],
    color: "#ffcc44",
  },
  {
    name: "VAULT",
    label: "Vault",
    icon: "□",
    role: "Memory Agent",
    shortRole: "Memory",
    description:
      "Persistent entity memory, semantic summarization, intelligence profile management",
    tier: "correlation",
    inputTypes: ["username", "email", "phone", "crypto_wallet", "photo"],
    preferredModels: ["gemma2:9b"],
    tokenBudget: 8192,
    latency: { min: 600, max: 3000 },
    contributesTo: ["persistent profiles", "entity memory", "cross-case recall"],
    connectors: [],
    requiresEnv: ["VAULT_DIR"],
    color: "#88bbff",
  },
  {
    name: "SENTINEL",
    label: "Sentinel",
    icon: "⊲",
    role: "Live Monitoring",
    shortRole: "Live Monitor",
    description:
      "Real-time tracking: new posts, bio changes, username changes, new domains, leaks",
    tier: "correlation",
    inputTypes: [],
    preferredModels: ["phi3:mini"],
    tokenBudget: 2048,
    latency: { min: 400, max: 2000 },
    contributesTo: ["baseline snapshots", "change events", "monitor alerts"],
    connectors: [],
    requiresEnv: ["SENTINEL_DIR"],
    color: "#ff4455",
  },
  {
    name: "QUILL",
    label: "Quill",
    icon: "⟁",
    role: "Report Generation",
    shortRole: "Reports",
    description:
      "AI-generated intelligence summaries, PDF reports, evidence bundles, graph exports",
    tier: "correlation",
    inputTypes: [],
    preferredModels: ["gemma2:9b", "claude-opus-4"],
    tokenBudget: 16384,
    latency: { min: 3000, max: 15000 },
    contributesTo: ["executive summary", "markdown report", "PDF export", "evidence bundle"],
    connectors: [],
    requiresEnv: [],
    color: "#ccddff",
  },
  {
    // ── Supervisor ────────────────────────────────────────────────────────
    name: "APEX",
    label: "Apex",
    icon: "⊕",
    role: "Master Supervisor",
    shortRole: "Supervisor",
    description:
      "Routes investigations, orchestrates agent swarms, synthesizes reports",
    tier: "supervisor",
    inputTypes: [...AGENT_INPUT_TYPES],
    preferredModels: ["qwen2.5:7b", "gemma2:9b", "claude-opus-4"],
    tokenBudget: 16384,
    latency: { min: 300, max: 2000 },
    contributesTo: ["routing decision", "swarm orchestration", "final synthesis"],
    connectors: [],
    requiresEnv: [],
    color: "#00ff88",
  },
] as const;

/** Name → entry lookup, keyed by {@link AgentName}. */
export const AGENTS_BY_NAME: Readonly<Record<AgentName, AgentCatalogEntry>> =
  AGENT_CATALOG.reduce(
    (acc, entry) => {
      acc[entry.name] = entry;
      return acc;
    },
    {} as Record<AgentName, AgentCatalogEntry>,
  );

/** Registry key order, as declared in `backend/agents/__init__.py`. */
export const AGENT_NAMES: readonly AgentName[] = [
  "SCOUT",
  "PHONOS",
  "EMAIL",
  "CRAWLER",
  "IRIS",
  "ECHO",
  "PRISM",
  "TERRA",
  "INK",
  "NEXUS",
  "KRONOS",
  "VAULT",
  "SENTINEL",
  "QUILL",
  "SIGMA",
  "APEX",
] as const;

/**
 * APEX's deferred correlation tier (`ApexAgent.CORRELATION_TIER`) — these five
 * run sequentially *after* the primary swarm, never concurrently with it.
 */
export const CORRELATION_TIER: readonly AgentName[] = [
  "NEXUS",
  "KRONOS",
  "VAULT",
  "SENTINEL",
  "QUILL",
] as const;

/**
 * Mirror of `backend/router.py::AGENT_MAP`. The correlation tier is deliberately
 * retained here even where the backend lists it, because APEX appends the full
 * tier to every investigation regardless of the routing map.
 */
export const AGENT_ROUTING_MAP: Readonly<Record<AgentInputType, readonly AgentName[]>> = {
  username: ["SCOUT", "PRISM", "NEXUS", "VAULT"],
  email: ["EMAIL", "SCOUT", "SIGMA", "PRISM", "NEXUS", "VAULT"],
  phone: ["PHONOS", "SCOUT", "NEXUS", "VAULT"],
  domain: ["SCOUT", "SIGMA", "CRAWLER", "NEXUS"],
  ip_address: ["SIGMA", "TERRA", "NEXUS"],
  crypto_wallet: ["SIGMA", "NEXUS", "VAULT"],
  url: ["SCOUT", "CRAWLER", "IRIS", "SIGMA"],
  image: ["IRIS", "TERRA", "NEXUS"],
  photo: ["IRIS", "TERRA", "NEXUS", "VAULT", "PRISM"],
  face: ["IRIS", "PRISM", "NEXUS"],
  person_name: ["SCOUT", "PRISM", "NEXUS", "VAULT", "INK"],
  video: ["IRIS", "ECHO", "TERRA"],
  audio: ["ECHO", "INK"],
  document: ["IRIS", "INK", "CRAWLER"],
  text: ["INK", "PRISM", "NEXUS"],
  unknown: ["SCOUT", "IRIS", "INK"],
} as const;

/**
 * Look up one agent. Accepts arbitrary strings because backend payloads
 * (`AgentResult.agent`, `AgentMeta.name`) are typed as plain strings; unknown
 * names return `undefined` rather than throwing, so live data can never crash a
 * render.
 */
export function agentByName(name: AgentName | string | null | undefined): AgentCatalogEntry | undefined {
  if (!name) return undefined;
  return AGENTS_BY_NAME[name as AgentName];
}

/** Like {@link agentByName} but for exhaustive/static contexts. */
export function requireAgent(name: AgentName): AgentCatalogEntry {
  return AGENTS_BY_NAME[name];
}

/**
 * Agents activated for a detected input type, in routing order.
 *
 * Pass `tier` to narrow to a single tier — e.g. `agentsForInputType("email",
 * "primary")` returns only the concurrent producers, excluding the correlation
 * tier that always runs afterwards.
 */
export function agentsForInputType(
  inputType: AgentInputType | string | null | undefined,
  tier?: AgentTier,
): readonly AgentCatalogEntry[] {
  const key = (inputType ?? "unknown") as AgentInputType;
  const names = AGENT_ROUTING_MAP[key] ?? AGENT_ROUTING_MAP.unknown;
  const entries: AgentCatalogEntry[] = [];
  for (const name of names) {
    const entry = AGENTS_BY_NAME[name];
    if (entry && (!tier || entry.tier === tier)) entries.push(entry);
  }
  return entries;
}

/** Every agent in a tier, in catalogue order. */
export function agentsByTier(tier: AgentTier): readonly AgentCatalogEntry[] {
  return AGENT_CATALOG.filter((entry) => entry.tier === tier);
}

/** Narrowing guard: is this string a registered agent name? */
export function isAgentName(value: string | null | undefined): value is AgentName {
  return typeof value === "string" && value in AGENTS_BY_NAME;
}

/** Total token budget if every agent in a tier ran once. */
export function totalTokenBudget(tier: AgentTier): number {
  return agentsByTier(tier).reduce((sum, entry) => sum + entry.tokenBudget, 0);
}