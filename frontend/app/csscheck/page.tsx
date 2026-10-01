// TEMPORARY build-verification route — deleted after `next build` succeeds.
import {
  ENTITY_TYPE_META,
  labelFor,
  colorFor,
  groupFor,
} from "@/lib/entityTypes";
import {
  formatBytes,
  formatDuration,
  formatRelativeTime,
  formatTimestamp,
  truncateMiddle,
  confidencePct,
  statusTone,
  toneClass,
  highlightTerms,
} from "@/lib/format";
import {
  API_BASE,
  wsUrl,
  apiFetch,
  listAgents,
  getHealth,
  submitInvestigation,
  createCase,
  getCaseStats,
  exportCase,
  newCircuit,
  downloadReport,
  submitPhotoSearch,
  submitNameSearch,
  updateCase,
  deleteCase,
  runAgent,
  getReport,
  listCases,
  getCase,
  getCaseEntities,
  getCaseTimeline,
  listInvestigations,
  getInvestigation,
  runSearch,
  submitInvestigationSync,
  getOpsecStatus,
  getMetrics,
} from "@/lib/api";
import type {
  AgentMeta,
  AgentNode,
  CaseStats,
  Entity,
  EntityType,
  HealthStatus,
  Investigation,
  MetricsSnapshot,
  OpsecStatus,
  Paged,
} from "@/lib/types";

export default function CssCheck() {
  const t: EntityType = "person";
  const label: string = labelFor(t);
  const color: string = colorFor("ip_address");
  const group = groupFor("wallet");
  const agent: AgentMeta = ENTITY_TYPE_META.person as unknown as AgentMeta;
  void [agent, color, group, label];
  void [
    API_BASE,
    wsUrl("inv_1"),
    apiFetch<HealthStatus>("/api/health"),
    listAgents(),
    getHealth(),
    submitInvestigation("x"),
    submitInvestigationSync("x"),
    getInvestigation("inv_1"),
    listInvestigations(),
    runSearch({ query: "q" }),
    submitPhotoSearch(new Blob()),
    submitNameSearch({ name: "n" }),
    listCases(),
    createCase({ name: "c" }),
    getCase("c"),
    updateCase("c", { name: "c2" }),
    deleteCase("c"),
    getCaseStats("c"),
    getCaseEntities("c"),
    getCaseTimeline("c"),
    exportCase("c"),
    runAgent("scout", { input: "x" }),
    getOpsecStatus(),
    getMetrics(),
    newCircuit(),
    getReport("inv_1"),
    downloadReport("inv_1"),
  ];
  void [
    formatBytes(1024),
    formatDuration(123456),
    formatRelativeTime(new Date().toISOString()),
    formatTimestamp(new Date().toISOString()),
    truncateMiddle("verylongemail@example.com", 20),
    confidencePct(0.87),
    statusTone("running"),
    toneClass(statusTone("error")),
    highlightTerms("hello world", ["world"]),
  ];
  const probe: Pick<CaseStats, "case_id"> = { case_id: "c" };
  const node: Pick<AgentNode, "name"> = { name: "scout" };
  const metrics: Pick<MetricsSnapshot, "counters"> = { counters: {} };
  const opsec: Pick<OpsecStatus, "active"> = { active: true };
  const paged: Paged<Entity> = { items: [], total: 0, limit: 10, offset: 0 };
  const inv: Pick<Investigation, "inv_id" | "status"> = {
    inv_id: "inv_1",
    status: "done",
  };
  void [probe, node, metrics, opsec, paged, inv];

  return (
    <main className="hairline-grid min-h-screen bg-surface-0 text-ink font-mono">
      <div className="panel p-4 bg-surface-2 border border-border-subtle">
        <header className="panel-header">
          <span className="mono-label">CSS CHECK</span>
          <span className="tone-ok tone-pulse">ONLINE</span>
        </header>
        <div className="skeleton h-4 w-40 my-3" />
        <span className="tone-warn">WARN</span>{" "}
        <span className="tone-err">ERR</span>{" "}
        <span className="tone-info">INFO</span>{" "}
        <span className="tone-idle">IDLE</span>
        <button className="focus-ring text-signal-ok text-signal-warn text-signal-err text-signal-info text-accent text-ink-muted border-accent rounded-md mt-4 px-2">
          focus target
        </button>
        <p className="text-signal-ok text-accent font-mono text-signal-alt">
          theme tokens resolve
        </p>
      </div>
    </main>
  );
}