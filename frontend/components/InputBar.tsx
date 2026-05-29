"use client";
import { useState, useRef, useEffect } from "react";

interface Props {
  onSubmit: (value: string) => void;
  running: boolean;
}

const PLACEHOLDERS = [
  "target@example.com",
  "192.168.1.1",
  "john_doe_official",
  "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
  "suspicious-domain.io",
  "https://pastebin.com/xyz...",
  "upload image, video, or audio...",
];

export function InputBar({ onSubmit, running }: Props) {
  const [value, setValue] = useState("");
  const [placeholder, setPlaceholder] = useState(PLACEHOLDERS[0]);
  const inputRef = useRef<HTMLInputElement>(null);

  // cycle placeholder
  useEffect(() => {
    let i = 0;
    const id = setInterval(() => {
      i = (i + 1) % PLACEHOLDERS.length;
      setPlaceholder(PLACEHOLDERS[i]);
    }, 3000);
    return () => clearInterval(id);
  }, []);

  // focus on mount
  useEffect(() => { inputRef.current?.focus(); }, []);

  const submit = () => {
    if (running || !value.trim()) return;
    onSubmit(value.trim());
    setValue("");
  };

  return (
    <div className="flex flex-col gap-2">
      <p className="label" style={{ fontSize: 9 }}>
        INVESTIGATE &nbsp;·&nbsp; ANY INPUT TYPE ACCEPTED
      </p>
      <div
        className="flex items-center gap-2 rounded-md px-3 py-2"
        style={{
          background: "var(--bg-card)",
          border: `1px solid ${running ? "var(--green)" : "var(--border-hi)"}`,
          boxShadow: running ? "0 0 16px rgba(0,255,136,0.12)" : undefined,
          transition: "border-color 0.2s, box-shadow 0.2s",
        }}
      >
        {/* Prompt symbol */}
        <span style={{ color: "var(--green)", fontSize: 14, userSelect: "none" }}>›</span>

        <input
          ref={inputRef}
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
          placeholder={placeholder}
          disabled={running}
          className="flex-1 bg-transparent outline-none"
          style={{
            color: "var(--text)",
            fontSize: 13,
            fontFamily: "var(--font-mono)",
            caretColor: "var(--green)",
          }}
        />

        {/* Running spinner OR submit button */}
        {running ? (
          <div className="flex items-center gap-1.5" style={{ color: "var(--green)" }}>
            <Spinner />
            <span className="label" style={{ fontSize: 9 }}>ROUTING</span>
          </div>
        ) : (
          <button
            onClick={submit}
            disabled={!value.trim()}
            className="flex items-center gap-1.5 px-3 py-1 rounded transition-all"
            style={{
              background: value.trim() ? "rgba(0,255,136,0.12)" : "transparent",
              border: `1px solid ${value.trim() ? "var(--green)" : "var(--border)"}`,
              color: value.trim() ? "var(--green)" : "var(--text-muted)",
              fontSize: 10,
              letterSpacing: "0.1em",
              cursor: value.trim() ? "pointer" : "default",
              transition: "all 0.15s",
            }}
          >
            INVESTIGATE
          </button>
        )}
      </div>

      <div className="flex gap-4" style={{ color: "var(--text-muted)", fontSize: 10 }}>
        {["email","domain","username","IP","wallet","URL","image","⌘↵"].map((hint) => (
          <span key={hint} className="label" style={{ fontSize: 8 }}>{hint}</span>
        ))}
      </div>
    </div>
  );
}

function Spinner() {
  return (
    <svg
      width="12" height="12" viewBox="0 0 12 12"
      style={{ animation: "spin-slow 0.8s linear infinite" }}
    >
      <circle cx="6" cy="6" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeDasharray="20" strokeDashoffset="5" />
    </svg>
  );
}
