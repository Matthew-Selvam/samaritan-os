"use client";
import { useState, useRef, useCallback, useEffect } from "react";

interface Props {
  onFileSelect: (file: File) => void;
  selectedFile: File | null;
  previewUrl: string | null;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}

export function PhotoUpload({ onFileSelect, selectedFile, previewUrl }: Props) {
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFile = useCallback(
    (file: File) => {
      if (file.type.startsWith("image/")) {
        onFileSelect(file);
      }
    },
    [onFileSelect],
  );

  /* ── drag events ── */
  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  }, []);

  const onDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  }, []);

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setIsDragging(false);
      const file = e.dataTransfer.files[0];
      if (file) handleFile(file);
    },
    [handleFile],
  );

  /* ── clipboard paste ── */
  useEffect(() => {
    const handler = (e: ClipboardEvent) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      for (const item of Array.from(items)) {
        if (item.type.startsWith("image/")) {
          const file = item.getAsFile();
          if (file) {
            handleFile(file);
            break;
          }
        }
      }
    };
    window.addEventListener("paste", handler);
    return () => window.removeEventListener("paste", handler);
  }, [handleFile]);

  /* ── input change ── */
  const onChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) handleFile(file);
  };

  /* ── clear ── */
  const clear = () => {
    if (inputRef.current) inputRef.current.value = "";
    // parent should set selectedFile / previewUrl to null
    onFileSelect(null as unknown as File); // signal clear
  };

  /* ── has preview ── */
  const hasFile = selectedFile && previewUrl;

  return (
    <div
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      onClick={() => !hasFile && inputRef.current?.click()}
      style={{
        position: "relative",
        border: `2px dashed ${isDragging ? "var(--green)" : "var(--border-hi)"}`,
        borderRadius: 10,
        background: isDragging
          ? "rgba(0,255,136,0.04)"
          : "var(--bg-card)",
        padding: hasFile ? 16 : 32,
        textAlign: "center",
        cursor: hasFile ? "default" : "pointer",
        transition: "border-color 0.25s ease, background 0.25s ease, box-shadow 0.25s ease",
        boxShadow: isDragging ? "0 0 24px rgba(0,255,136,0.08)" : "none",
      }}
    >
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        onChange={onChange}
        style={{ display: "none" }}
      />

      {!hasFile ? (
        /* ── empty state ── */
        <div className="flex flex-col items-center gap-3">
          {/* camera icon */}
          <svg
            width="40"
            height="40"
            viewBox="0 0 24 24"
            fill="none"
            stroke={isDragging ? "var(--green)" : "var(--text-muted)"}
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            style={{ transition: "stroke 0.25s ease" }}
          >
            <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z" />
            <circle cx="12" cy="13" r="4" />
          </svg>

          <span
            className="tracking-[0.2em]"
            style={{
              color: isDragging ? "var(--green)" : "var(--text-muted)",
              fontSize: 11,
              fontWeight: 600,
              letterSpacing: "0.2em",
              transition: "color 0.25s ease",
            }}
          >
            DROP IMAGE OR CLICK
          </span>

          <span
            style={{
              color: "var(--text-muted)",
              fontSize: 9,
              opacity: 0.6,
            }}
          >
            Ctrl+V to paste from clipboard
          </span>
        </div>
      ) : (
        /* ── preview state ── */
        <div className="flex flex-col items-center gap-3">
          <div
            style={{
              position: "relative",
              borderRadius: 8,
              overflow: "hidden",
              border: "1px solid var(--border-hi)",
              boxShadow: "0 0 20px rgba(0,212,255,0.06)",
            }}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={previewUrl}
              alt="Upload preview"
              style={{
                maxWidth: 300,
                maxHeight: 300,
                display: "block",
                objectFit: "contain",
              }}
            />
            {/* scanline overlay */}
            <div
              style={{
                position: "absolute",
                inset: 0,
                background:
                  "repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,255,136,0.015) 2px, rgba(0,255,136,0.015) 4px)",
                pointerEvents: "none",
              }}
            />
          </div>

          <div className="flex items-center gap-3" style={{ marginTop: 4 }}>
            <span
              style={{
                color: "var(--text)",
                fontSize: 11,
                fontFamily: "var(--font-mono)",
                maxWidth: 200,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {selectedFile.name}
            </span>
            <span
              style={{
                color: "var(--text-muted)",
                fontSize: 9,
                fontFamily: "var(--font-mono)",
              }}
            >
              {formatBytes(selectedFile.size)}
            </span>
          </div>

          {/* clear button */}
          <button
            onClick={(e) => {
              e.stopPropagation();
              clear();
            }}
            className="flex items-center gap-1.5 px-3 py-1 rounded"
            style={{
              background: "rgba(255,68,85,0.08)",
              border: "1px solid rgba(255,68,85,0.25)",
              color: "var(--red)",
              fontSize: 10,
              fontWeight: 600,
              letterSpacing: "0.15em",
              cursor: "pointer",
              transition: "background 0.2s ease, border-color 0.2s ease",
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = "rgba(255,68,85,0.15)";
              e.currentTarget.style.borderColor = "rgba(255,68,85,0.5)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = "rgba(255,68,85,0.08)";
              e.currentTarget.style.borderColor = "rgba(255,68,85,0.25)";
            }}
          >
            <svg
              width="10"
              height="10"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.5"
              strokeLinecap="round"
            >
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
            CLEAR
          </button>
        </div>
      )}
    </div>
  );
}
