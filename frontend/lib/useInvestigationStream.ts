"use client";

/**
 * useInvestigationStream — live pipeline subscription over
 * `WS /ws/pipeline/{inv_id}`.
 *
 * Behaviour (CONTRACTS.md §11):
 *  - subscribes when `invId` changes, tears the socket down on unmount or when
 *    the id changes, so no socket or timer is ever leaked;
 *  - reconnects with exponential backoff from 1s to a 30s cap, resubscribing
 *    on every reconnect (the backend replays `done` for finished runs, so a
 *    reconnect converges on the final state rather than replaying every step);
 *  - emits a heartbeat ping every 25s to keep intermediaries from culling the
 *    connection, and stops once the run is finished;
 *  - never throws into the render tree: consumer errors are caught and logged,
 *    transport failures surface through `status` + the returned controls.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { wsUrl } from "./api";
import type { PipelineEvent } from "./types";

/** Backoff schedule for reconnect attempts. */
const BACKOFF_BASE_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;
const BACKOFF_FACTOR = 2;
/** Keepalive ping interval; comfortably under the typical 60s idle timeout. */
const HEARTBEAT_MS = 25_000;
/** How long to wait for `open` before treating the attempt as failed. */
const CONNECT_TIMEOUT_MS = 15_000;

/** Socket lifecycle state, suitable for driving a status indicator. */
export type StreamStatus =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "closed"
  | "error";

export interface UseInvestigationStreamResult {
  /** Close the socket and cancel any pending reconnect. Idempotent. */
  close: () => void;
  /** Force an immediate reconnect, resetting the backoff. */
  reconnect: () => void;
  /** Current lifecycle state. */
  status: StreamStatus;
  /** Reconnect attempts since the last successful open. */
  attempts: number;
  /** Delay in ms before the next scheduled reconnect attempt. */
  nextRetryInMs: number;
}

export type InvestigationStreamHandler = (event: PipelineEvent) => void;

/** Exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s (capped). */
export function backoffDelayMs(attempt: number): number {
  if (attempt <= 0) return 0;
  const delay = BACKOFF_BASE_MS * Math.pow(BACKOFF_FACTOR, attempt - 1);
  return Math.min(delay, BACKOFF_MAX_MS);
}

/**
 * Normalise a raw socket frame into a {@link PipelineEvent}.
 *
 * Non-JSON frames are surfaced as a synthetic `error` event rather than being
 * dropped, so a malformed backend frame can never silently stall the UI.
 */
export function parsePipelineEvent(data: unknown): PipelineEvent | null {
  let payload: unknown = data;
  if (typeof data === "string") {
    try {
      payload = JSON.parse(data);
    } catch {
      return {
        type: "error",
        message: `Unparseable pipeline frame: ${data.slice(0, 200)}`,
      };
    }
  }
  if (!payload || typeof payload !== "object") return null;
  const record = payload as Record<string, unknown>;
  const type = typeof record.type === "string" ? record.type : "";
  if (!type) return null;
  // Cast through unknown: the union is discriminated by `type`, and any extra
  // fields the backend adds survive untouched on the returned object.
  return { ...record, type } as unknown as PipelineEvent;
}

/**
 * Subscribe to an investigation's pipeline WebSocket.
 *
 * @param invId - Investigation id; `null`/`""` leaves the hook idle.
 * @param onEvent - Called for every decoded event. Exceptions are caught so a
 *                  faulty consumer cannot break the stream or React's tree.
 * @returns Controls and connection state: `{ close, reconnect, status }`.
 */
export function useInvestigationStream(
  invId: string | null | undefined,
  onEvent: InvestigationStreamHandler,
): UseInvestigationStreamResult {
  const [status, setStatus] = useState<StreamStatus>("idle");
  const [attempts, setAttempts] = useState(0);
  const [nextRetryInMs, setNextRetryInMs] = useState(0);

  // Latest handler without forcing a resubscribe when the identity changes.
  const handlerRef = useRef<InvestigationStreamHandler>(onEvent);
  useEffect(() => {
    handlerRef.current = onEvent;
  }, [onEvent]);

  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const heartbeatTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const connectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptsRef = useRef(0);
  const closedRef = useRef(false);
  /** Set when the run reached a terminal state — no reconnect after that. */
  const finishedRef = useRef(false);
  /** Id the socket was opened for, so stale callbacks are ignored. */
  const openedForRef = useRef<string | null>(null);

  const clearTimers = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (heartbeatTimerRef.current !== null) {
      clearInterval(heartbeatTimerRef.current);
      heartbeatTimerRef.current = null;
    }
    if (connectTimerRef.current !== null) {
      clearTimeout(connectTimerRef.current);
      connectTimerRef.current = null;
    }
  }, []);

  const hardClose = useCallback(() => {
    clearTimers();
    const socket = socketRef.current;
    socketRef.current = null;
    openedForRef.current = null;
    if (socket) {
      // Detach first: a close() we initiated must not schedule a reconnect.
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      try {
        socket.close();
      } catch {
        /* already closing */
      }
    }
  }, [clearTimers]);

  const emit = useCallback((event: PipelineEvent) => {
    try {
      handlerRef.current(event);
    } catch (err) {
      console.error("[signal-os] investigation stream handler threw:", err);
    }
  }, []);

  useEffect(() => {
    if (!invId) {
      hardClose();
      attemptsRef.current = 0;
      finishedRef.current = false;
      setStatus("idle");
      setAttempts(0);
      setNextRetryInMs(0);
      return;
    }

    closedRef.current = false;
    finishedRef.current = false;
    openedForRef.current = null;

    const scheduleReconnect = (immediate = false) => {
      if (closedRef.current || finishedRef.current) return;
      const attempt = attemptsRef.current;
      const delay = immediate ? 0 : backoffDelayMs(attempt);
      attemptsRef.current = attempt + 1;
      setAttempts(attemptsRef.current);
      setNextRetryInMs(delay);
      setStatus("reconnecting");
      clearTimers();
      reconnectTimerRef.current = setTimeout(() => {
        reconnectTimerRef.current = null;
        if (closedRef.current || finishedRef.current) return;
        setStatus((prev) => (prev === "reconnecting" ? "connecting" : prev));
        // Re-derive the URL each attempt: a proxy-less dev origin may change.
        openSocket();
      }, delay);
    };

    const startHeartbeat = (socket: WebSocket) => {
      heartbeatTimerRef.current = setInterval(() => {
        if (socket.readyState !== WebSocket.OPEN) return;
        try {
          socket.send(JSON.stringify({ type: "ping", ts: new Date().toISOString() }));
        } catch {
          /* socket died mid-send; onclose will handle it */
        }
      }, HEARTBEAT_MS);
    };

    function openSocket() {
      if (closedRef.current || finishedRef.current) return;
      const url = wsUrl(invId as string);
      let socket: WebSocket;
      try {
        socket = new WebSocket(url);
      } catch (err) {
        setStatus("error");
        console.warn("[signal-os] could not open pipeline socket:", err);
        scheduleReconnect();
        return;
      }

      socketRef.current = socket;
      openedForRef.current = invId as string;
      setStatus("connecting");

      // If the handshake never completes, treat it like a failed attempt.
      connectTimerRef.current = setTimeout(() => {
        if (socket.readyState === WebSocket.CONNECTING) {
          try {
            socket.close();
          } catch {
            /* ignore */
          }
        }
      }, CONNECT_TIMEOUT_MS);

      socket.onopen = () => {
        if (socketRef.current !== socket || closedRef.current) return;
        if (connectTimerRef.current !== null) {
          clearTimeout(connectTimerRef.current);
          connectTimerRef.current = null;
        }
        attemptsRef.current = 0;
        setAttempts(0);
        setNextRetryInMs(0);
        setStatus("open");
        startHeartbeat(socket);
      };

      socket.onmessage = (message: MessageEvent<unknown>) => {
        if (socketRef.current !== socket || closedRef.current) return;
        const event = parsePipelineEvent(message.data);
        if (!event) return;

        if (event.type === "ping") {
          try {
            if (socket.readyState === WebSocket.OPEN) {
              socket.send(JSON.stringify({ type: "pong", ts: new Date().toISOString() }));
            }
          } catch {
            /* ignore */
          }
          return;
        }

        emit(event);

        if (event.type === "done" || event.type === "error") {
          // Terminal: replay state has been delivered, stop reconnecting.
          finishedRef.current = true;
          clearTimers();
          setStatus(event.type === "done" ? "closed" : "error");
          hardClose();
        }
      };

      socket.onerror = () => {
        if (socketRef.current !== socket || closedRef.current) return;
        setStatus("error");
      };

      socket.onclose = (closeEvent: CloseEvent) => {
        if (connectTimerRef.current !== null) {
          clearTimeout(connectTimerRef.current);
          connectTimerRef.current = null;
        }
        if (socketRef.current === socket) socketRef.current = null;
        if (openedForRef.current === invId) openedForRef.current = null;
        if (closedRef.current || finishedRef.current) {
          setStatus((prev) => (prev === "error" ? prev : "closed"));
          return;
        }
        // 1000 = normal closure by the backend after a terminal event.
        if (closeEvent.code === 1000 && closeEvent.reason === "terminal") {
          setStatus("closed");
          return;
        }
        scheduleReconnect();
      };
    }

    openRef.current = openSocket;
    openSocket();

    return () => {
      closedRef.current = true;
      openRef.current = null;
      hardClose();
      attemptsRef.current = 0;
      finishedRef.current = false;
      setStatus("closed");
    };
    // `emit` and `hardClose` are stable; only the id drives resubscription.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [invId, emit, hardClose]);

  const close = useCallback(() => {
    attemptsRef.current = 0;
    finishedRef.current = true; // suppress any in-flight reconnect
    hardClose();
    setStatus("closed");
  }, [hardClose]);

  // The effect installs its own `openSocket`; `reconnect` reuses it so the
  // manual path and the automatic path cannot drift apart.
  const openRef = useRef<(() => void) | null>(null);

  const reconnect = useCallback(() => {
    if (!invId || !openRef.current) return;
    hardClose();
    attemptsRef.current = 0;
    finishedRef.current = false;
    closedRef.current = false;
    setAttempts(0);
    setNextRetryInMs(0);
    openRef.current();
  }, [invId, hardClose]);

  return { close, reconnect, status, attempts, nextRetryInMs };
}

export default useInvestigationStream;