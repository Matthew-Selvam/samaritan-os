"use client";

/**
 * PhotoUpload.tsx — drag-drop / paste / pick control for the PHOTO tab.
 *
 * The original drag-and-drop, clipboard-paste and preview behaviour is kept and
 * extended: any media type is accepted (IRIS vision, ECHO audio, PRISM video),
 * the clear button reports a real `null` instead of a cast, and the whole zone is
 * keyboard reachable via a labelled button.
 */

import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import clsx from "clsx";
import { Badge, Button } from "./ui";
import { formatBytes } from "@/lib/format";

export interface PhotoUploadProps {
  /** `null` clears the selection. */
  onFileSelect: (file: File | null) => void;
  selectedFile: File | null;
  /** Object URL for the preview; `null` for audio/video. */
  previewUrl: string | null;
  /** Rejected the last file — rendered inline. */
  error?: string | null;
  /** Maximum accepted size in bytes; 0 disables the check. */
  maxBytes?: number;
  className?: string;
}

const ACCEPT = "image/*,video/*,audio/*";
/** Guard rail matching the backend upload limit. */
const DEFAULT_MAX_BYTES = 100 * 1024 * 1024;

/** Media class for a MIME type. */
function mediaKind(file: File): "image" | "video" | "audio" | null {
  if (file.type.startsWith("image/")) return "image";
  if (file.type.startsWith("video/")) return "video";
  if (file.type.startsWith("audio/")) return "audio";
  return null;
}

/**
 * Upload zone.
 *
 * Accepts a drop, a paste, a click or Enter/Space on the focusable zone, and
 * always labels what it expects so the control is understandable without sight
 * of the glyph.
 */
export function PhotoUpload({
  onFileSelect,
  selectedFile,
  previewUrl,
  error,
  maxBytes = DEFAULT_MAX_BYTES,
  className,
}: PhotoUploadProps) {
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const hasFile = Boolean(selectedFile);

  const handleFile = useCallback(
    (file: File | null | undefined) => {
      if (!file) {
        onFileSelect(null);
        return;
      }
      if (!mediaKind(file)) return; // silently ignore a directory or a PDF
      if (maxBytes > 0 && file.size > maxBytes) return;
      onFileSelect(file);
    },
    [maxBytes, onFileSelect],
  );

  /* ── drag events ── */
  const onDragOver = useCallback((event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    setIsDragging(true);
  }, []);

  const onDragLeave = useCallback((event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    setIsDragging(false);
  }, []);

  const onDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      event.stopPropagation();
      setIsDragging(false);
      handleFile(event.dataTransfer.files[0]);
    },
    [handleFile],
  );

  /* ── clipboard paste ── */
  useEffect(() => {
    const handler = (event: ClipboardEvent) => {
      const items = event.clipboardData?.items;
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

  const onChange = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      handleFile(event.target.files?.[0]);
    },
    [handleFile],
  );

  const clear = useCallback(() => {
    if (inputRef.current) inputRef.current.value = "";
    handleFile(null);
  }, [handleFile]);

  const kind = selectedFile ? mediaKind(selectedFile) : null;

  return (
    <div
      className={className}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      style={{
        position: "relative",
        border: `2px dashed ${isDragging ? "var(--green)" : "var(--border-hi)"}`,
        borderRadius: 10,
        background: isDragging ? "rgba(0,255,136,0.04)" : "var(--bg-card)",
        padding: hasFile ? 16 : 32,
        textAlign: "center",
        transition: "border-color 0.25s ease, background 0.25s ease, box-shadow 0.25s ease",
        boxShadow: isDragging ? "0 0 24px rgba(0,255,136,0.08)" : "none",
      }}
    >
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        onChange={onChange}
        aria-label="Choose an image, video or audio file to analyse"
        className="sr-only"
      />

      {!hasFile ? (
        <div className="flex flex-col items-center gap-3">
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            className="focus-ring flex flex-col items-center gap-3 rounded px-4 py-2"
          >
            <svg
              width="40"
              height="40"
              viewBox="0 0 24 24"
              fill="none"
              stroke={isDragging ? "var(--green)" : "var(--text-muted)"}
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
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
                transition: "color 0.25s ease",
              }}
            >
              DROP MEDIA OR PRESS ENTER
            </span>
          </button>

          <span className="text-[9px] text-ink-muted">
            Image, video or audio · also paste from the clipboard with ⌘V
          </span>
          <span className="flex flex-wrap justify-center gap-1.5">
            <Badge tone="idle">IRIS · VISION</Badge>
            <Badge tone="idle">ECHO · AUDIO</Badge>
            <Badge tone="idle">PRISM · VIDEO</Badge>
          </span>
        </div>
      ) : (
        <div className="flex flex-col items-center gap-3">
          {selectedFile && (
            <MediaPreview file={selectedFile} previewUrl={previewUrl} kind={kind} />
          )}

          <div className="flex flex-wrap items-center justify-center gap-3">
            {selectedFile && (
              <>
            <span className="max-w-[240px] truncate text-[11px] text-ink">{selectedFile.name}</span>
            <span className="text-[9px] text-ink-muted">{formatBytes(selectedFile.size)}</span>
              </>
            )}
            {kind && <Badge tone="info">{kind.toUpperCase()}</Badge>}
          </div>

          <Button variant="danger" size="sm" onClick={clear}>
            Clear
          </Button>
        </div>
      )}

      {error && (
        <p role="alert" className="mt-2 text-[10px] text-signal-err">
          {error}
        </p>
      )}
    </div>
  );
}

/**
 * Renders the inline preview for the selected media.
 *
 * Split out so the `File` non-null narrowing happens once, at the boundary,
 * rather than being re-asserted at each usage site.
 */
function MediaPreview({
  file,
  previewUrl,
  kind,
}: {
  file: File;
  previewUrl: string | null;
  kind: "image" | "video" | "audio" | null;
}) {
  if (!previewUrl) {
    return <span className="mono-label">NO PREVIEW FOR FILE</span>;
  }
  if (kind === "video") {
    return (
      <video
        src={previewUrl}
        controls
        aria-label={`Preview of ${file.name}`}
        style={{ maxWidth: 320, maxHeight: 300 }}
      />
    );
  }
  if (kind === "audio") {
    return <audio src={previewUrl} controls aria-label={`Preview of ${file.name}`} />;
  }
  if (kind !== "image") {
    return <span className="mono-label">NO PREVIEW FOR FILE</span>;
  }
  return (
    <div
      className="relative overflow-hidden rounded-lg border border-border-strong"
      style={{ boxShadow: "0 0 20px rgba(0,212,255,0.06)" }}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={previewUrl}
        alt={`Preview of ${file.name}`}
        style={{ maxWidth: 300, maxHeight: 300, display: "block", objectFit: "contain" }}
      />
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,255,136,0.015) 2px, rgba(0,255,136,0.015) 4px)",
        }}
      />
    </div>
  );
}
