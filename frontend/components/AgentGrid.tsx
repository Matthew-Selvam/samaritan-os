"use client";

interface Agent {
  name: string;
  role: string;
  icon: string;
  color: string;
}

const AGENTS: Agent[] = [
  { name: "APEX",     role: "Supervisor",       icon: "⊕", color: "#00ff88" },
  { name: "SCOUT",    role: "Search Intel",      icon: "◎", color: "#00d4ff" },
  { name: "CRAWLER",  role: "Web Scraper",       icon: "⟨/⟩", color: "#00d4ff" },
  { name: "PRISM",    role: "Social Intel",      icon: "◈", color: "#9966ff" },
  { name: "IRIS",     role: "Vision Intel",      icon: "◉", color: "#ff66aa" },
  { name: "ECHO",     role: "Audio Intel",       icon: "~",  color: "#ffb020" },
  { name: "TERRA",    role: "GEOINT",            icon: "⊛", color: "#44dd88" },
  { name: "INK",      role: "Stylometry",        icon: "✦", color: "#aabbff" },
  { name: "NEXUS",    role: "Correlation",       icon: "∞", color: "#ff6644" },
  { name: "KRONOS",   role: "Timeline",          icon: "⊕", color: "#ffcc44" },
  { name: "VAULT",    role: "Memory",            icon: "□", color: "#88bbff" },
  { name: "SENTINEL", role: "Live Monitor",      icon: "⊲", color: "#ff4455" },
  { name: "QUILL",    role: "Reports",           icon: "⟁", color: "#ccddff" },
  { name: "SIGMA",    role: "Threat Intel",      icon: "⊗", color: "#ff8833" },
];

interface Props {
  activeAgents: Set<string>;
  activatedAgents: Set<string>;
}

export function AgentGrid({ activeAgents, activatedAgents }: Props) {
  return (
    <div className="flex flex-col gap-1">
      {AGENTS.map((agent) => {
        const isActive = activeAgents.has(agent.name);
        const isActivated = activatedAgents.has(agent.name);
        const highlight = isActive || isActivated;

        return (
          <div
            key={agent.name}
            className="flex items-center gap-2 px-2 py-1.5 rounded transition-all duration-200"
            style={{
              background: highlight ? `${agent.color}14` : "transparent",
              border: `1px solid ${highlight ? agent.color + "44" : "var(--border)"}`,
              animation: isActive ? "pulse-green 1.5s ease-in-out infinite" : undefined,
            }}
          >
            {/* Status dot */}
            <span
              className="rounded-full flex-shrink-0"
              style={{
                width: 5,
                height: 5,
                background: highlight ? agent.color : "var(--border-hi)",
                boxShadow: isActive ? `0 0 6px ${agent.color}` : undefined,
                display: "inline-block",
              }}
            />

            {/* Icon */}
            <span
              className="flex-shrink-0 w-5 text-center"
              style={{
                fontSize: 11,
                color: highlight ? agent.color : "var(--text-muted)",
                textShadow: highlight ? `0 0 8px ${agent.color}` : undefined,
              }}
            >
              {agent.icon}
            </span>

            {/* Name + role */}
            <div className="flex-1 min-w-0">
              <div
                className="font-bold tracking-wide truncate"
                style={{
                  fontSize: 10,
                  color: highlight ? agent.color : "var(--text)",
                  letterSpacing: "0.08em",
                }}
              >
                {agent.name}
              </div>
              <div className="label truncate" style={{ fontSize: 8 }}>
                {agent.role}
              </div>
            </div>

            {/* State badge */}
            {isActive && (
              <span
                className="flex-shrink-0 px-1 rounded"
                style={{
                  fontSize: 7,
                  background: `${agent.color}22`,
                  color: agent.color,
                  border: `1px solid ${agent.color}44`,
                  letterSpacing: "0.1em",
                }}
              >
                RUN
              </span>
            )}
            {!isActive && isActivated && (
              <span
                className="flex-shrink-0 px-1 rounded"
                style={{
                  fontSize: 7,
                  background: "rgba(0,255,136,0.08)",
                  color: "var(--green)",
                  border: "1px solid rgba(0,255,136,0.2)",
                  letterSpacing: "0.1em",
                }}
              >
                DONE
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
