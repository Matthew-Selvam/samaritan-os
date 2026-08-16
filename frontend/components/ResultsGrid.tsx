"use client";

interface ResultItem {
  url?: string;
  title?: string;
  similarity?: number;
  platform?: string;
  username?: string;
  bio?: string;
  avatar_url?: string;
  source?: string;
}

interface Props {
  results: ResultItem[];
  mode: "photo" | "name";
  searchTime?: number;
}

function similarityColor(pct: number): string {
  if (pct >= 80) return "var(--green)";
  if (pct >= 50) return "var(--amber)";
  return "var(--red)";
}

function similarityGlow(pct: number): string {
  if (pct >= 80) return "0 0 8px rgba(0,255,136,0.3)";
  if (pct >= 50) return "0 0 8px rgba(255,176,32,0.25)";
  return "0 0 8px rgba(255,68,85,0.25)";
}

function initials(name?: string): string {
  if (!name) return "??";
  return name
    .split(/[\s._-]+/)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? "")
    .join("");
}

/* ── platform badge ── */
function PlatformBadge({ platform }: { platform?: string }) {
  if (!platform) return null;
  return (
    <span
      style={{
        display: "inline-block",
        padding: "1px 6px",
        borderRadius: 3,
        fontSize: 8,
        fontWeight: 700,
        letterSpacing: "0.12em",
        textTransform: "uppercase",
        color: "var(--cyan)",
        background: "rgba(0,212,255,0.08)",
        border: "1px solid rgba(0,212,255,0.2)",
      }}
    >
      {platform}
    </span>
  );
}

/* ═══════════════ PHOTO CARD ═══════════════ */
function PhotoCard({ item }: { item: ResultItem }) {
  const sim = item.similarity ?? 0;
  return (
    <div
      className="flex gap-3 p-3 rounded-lg"
      style={{
        background: "var(--bg-card)",
        border: "1px solid var(--border)",
        transition: "border-color 0.2s ease, box-shadow 0.2s ease",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "var(--border-hi)";
        e.currentTarget.style.boxShadow = "0 0 16px rgba(0,212,255,0.04)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "var(--border)";
        e.currentTarget.style.boxShadow = "none";
      }}
    >
      {/* thumbnail */}
      <div
        style={{
          width: 80,
          height: 80,
          borderRadius: 6,
          overflow: "hidden",
          flexShrink: 0,
          background: "var(--bg-panel)",
          border: "1px solid var(--border)",
        }}
      >
        {item.url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={item.url}
            alt={item.title ?? "result"}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        ) : (
          <div
            className="flex items-center justify-center"
            style={{ width: "100%", height: "100%", color: "var(--text-muted)", fontSize: 20 }}
          >
            ◻
          </div>
        )}
      </div>

      {/* info */}
      <div className="flex flex-col justify-between flex-1" style={{ minWidth: 0 }}>
        <div>
          {item.source && (
            <a
              href={item.source}
              target="_blank"
              rel="noopener noreferrer"
              style={{
                color: "var(--cyan)",
                fontSize: 10,
                fontFamily: "var(--font-mono)",
                textDecoration: "none",
                display: "block",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
              onMouseEnter={(e) => (e.currentTarget.style.textDecoration = "underline")}
              onMouseLeave={(e) => (e.currentTarget.style.textDecoration = "none")}
            >
              {item.source}
            </a>
          )}
          {item.title && (
            <span
              style={{
                color: "var(--text)",
                fontSize: 11,
                display: "block",
                marginTop: 2,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {item.title}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2" style={{ marginTop: 4 }}>
          {/* similarity */}
          <span
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: 12,
              fontWeight: 700,
              color: similarityColor(sim),
              textShadow: similarityGlow(sim),
            }}
          >
            {sim.toFixed(1)}%
          </span>
          {/* mini bar */}
          <div
            style={{
              flex: 1,
              height: 3,
              borderRadius: 2,
              background: "var(--border)",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                width: `${Math.min(sim, 100)}%`,
                height: "100%",
                borderRadius: 2,
                background: similarityColor(sim),
                transition: "width 0.4s ease",
              }}
            />
          </div>
          <PlatformBadge platform={item.platform} />
        </div>
      </div>
    </div>
  );
}

/* ═══════════════ NAME CARD ═══════════════ */
function NameCard({ item }: { item: ResultItem }) {
  return (
    <div
      className="flex gap-3 p-3 rounded-lg"
      style={{
        background: "var(--bg-card)",
        border: "1px solid var(--border)",
        transition: "border-color 0.2s ease, box-shadow 0.2s ease",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "var(--border-hi)";
        e.currentTarget.style.boxShadow = "0 0 16px rgba(0,212,255,0.04)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "var(--border)";
        e.currentTarget.style.boxShadow = "none";
      }}
    >
      {/* avatar */}
      <div
        className="flex items-center justify-center flex-shrink-0"
        style={{
          width: 44,
          height: 44,
          borderRadius: "50%",
          background: item.avatar_url
            ? `url(${item.avatar_url}) center/cover no-repeat`
            : "linear-gradient(135deg, rgba(0,212,255,0.15), rgba(153,102,255,0.15))",
          border: "1px solid var(--border-hi)",
          color: "var(--cyan)",
          fontSize: 13,
          fontWeight: 700,
          fontFamily: "var(--font-mono)",
        }}
      >
        {!item.avatar_url && initials(item.username)}
      </div>

      {/* details */}
      <div className="flex flex-col flex-1" style={{ minWidth: 0 }}>
        <div className="flex items-center gap-2">
          <span
            style={{
              color: "var(--text)",
              fontSize: 12,
              fontWeight: 600,
            }}
          >
            {item.username ?? "Unknown"}
          </span>
          <PlatformBadge platform={item.platform} />
        </div>

        {item.bio && (
          <span
            style={{
              color: "var(--text-muted)",
              fontSize: 10,
              marginTop: 3,
              lineHeight: 1.4,
              display: "-webkit-box",
              WebkitLineClamp: 2,
              WebkitBoxOrient: "vertical",
              overflow: "hidden",
            }}
          >
            {item.bio}
          </span>
        )}

        {item.source && (
          <a
            href={item.source}
            target="_blank"
            rel="noopener noreferrer"
            style={{
              color: "var(--cyan)",
              fontSize: 9,
              fontFamily: "var(--font-mono)",
              textDecoration: "none",
              marginTop: 3,
              opacity: 0.7,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              display: "block",
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.opacity = "1";
              e.currentTarget.style.textDecoration = "underline";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.opacity = "0.7";
              e.currentTarget.style.textDecoration = "none";
            }}
          >
            {item.source}
          </a>
        )}
      </div>
    </div>
  );
}

/* ═══════════════ MAIN GRID ═══════════════ */
export function ResultsGrid({ results, mode, searchTime }: Props) {
  /* ── empty state ── */
  if (!results || results.length === 0) {
    return (
      <div
        className="flex flex-col items-center justify-center gap-3"
        style={{ padding: 48, opacity: 0.5 }}
      >
        <svg
          width="36"
          height="36"
          viewBox="0 0 24 24"
          fill="none"
          stroke="var(--text-muted)"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <circle cx="12" cy="12" r="10" />
          <line x1="4.93" y1="4.93" x2="19.07" y2="19.07" />
        </svg>
        <span
          style={{
            color: "var(--text-muted)",
            fontSize: 11,
            letterSpacing: "0.15em",
            fontWeight: 600,
          }}
        >
          NO RESULTS
        </span>
      </div>
    );
  }

  return (
    <div>
      {/* ── header ── */}
      <div
        className="flex items-center justify-between"
        style={{ marginBottom: 12, padding: "0 2px" }}
      >
        <div className="flex items-center gap-2">
          <span
            style={{
              color: "var(--green)",
              fontSize: 11,
              fontWeight: 700,
              fontFamily: "var(--font-mono)",
            }}
          >
            {results.length}
          </span>
          <span
            style={{
              color: "var(--text-muted)",
              fontSize: 10,
              letterSpacing: "0.12em",
              fontWeight: 600,
            }}
          >
            {mode === "photo" ? "IMAGE MATCHES" : "PROFILE MATCHES"}
          </span>
        </div>

        {searchTime !== undefined && (
          <span
            style={{
              color: "var(--text-muted)",
              fontSize: 9,
              fontFamily: "var(--font-mono)",
            }}
          >
            {searchTime.toFixed(2)}s
          </span>
        )}
      </div>

      {/* ── grid ── */}
      <div
        className="grid gap-2"
        style={{
          gridTemplateColumns: mode === "photo"
            ? "repeat(auto-fill, minmax(320px, 1fr))"
            : "repeat(auto-fill, minmax(280px, 1fr))",
        }}
      >
        {results.map((item, i) =>
          mode === "photo" ? (
            <PhotoCard key={`photo-${i}`} item={item} />
          ) : (
            <NameCard key={`name-${i}`} item={item} />
          ),
        )}
      </div>
    </div>
  );
}
