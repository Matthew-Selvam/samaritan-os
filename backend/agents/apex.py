"""
APEX — Master Supervisor
========================
Routes every investigation, activates the right swarm, schedules it as a DAG,
and synthesizes the results into one investigation record.

Execution model
---------------
::

    tier 1   PRIMARY swarm          (all in parallel, global semaphore)
              │  e.g. PHONOS, SCOUT, SIGMA …
    tier 2   NEXUS ‖ KRONOS         (parallel — both read the tier-1 aggregate)
              │  VAULT ‖ SENTINEL   (parallel — both read everything so far)
    tier 3   QUILL                  (last — needs the finished aggregate)

Resilience guarantees:

* **Per-agent timeout** — every agent is wrapped in ``asyncio.wait_for`` using
  ``config.get_timeout()``, so one slow agent can never stall the pipeline.
* **Retry with backoff** — a transient failure is retried up to
  ``AGENT_MAX_ATTEMPTS`` times with exponential backoff and jitter.
* **Global concurrency limit** — a process-wide semaphore bounds simultaneous
  agent executions, so 50 concurrent investigations do not melt the box.
* **Graceful partial success** — 7 of 9 agents failing still returns a useful
  report, with a confidence penalty proportional to what was lost.
* **Global time budget** — the whole run is bounded; tiers that blow their
  share are skipped rather than allowed to overrun.
* **Structured events** — ``agent_started`` / ``agent_finished`` /
  ``agent_failed`` / ``pipeline_budget_exhausted`` are pushed to the caller's
  ``emit`` callback alongside the plain log lines.

Live streaming: when the caller passes an async ``emit`` callback via context,
every agent's log line is relayed in real time (APEX attaches a shared queue to
each agent and drains it to ``emit`` as lines arrive).
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable

from .base import BaseAgent, AgentResult, agent_timeout
from router import detect_input_type  # absolute: backend/ is the path root (main:app)


class _EventLog(list):
    """An append-only event list that mirrors every entry to an async sink.

    ``emit_event`` is optional, so the pipeline behaves identically without a
    streaming consumer — the events are still collected and returned in
    ``output["events"]`` for polling clients.

    Args:
        sink: Optional async callback invoked with each event dict.
    """

    def __init__(self, sink: Callable[[dict], Awaitable[None]] | None = None) -> None:
        super().__init__()
        self._sink = sink

    def append(self, event: dict) -> None:  # type: ignore[override]
        super().append(event)
        if self._sink is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # Schedule rather than await: append is called from sync context.
        loop.create_task(self._deliver(event))

    async def _deliver(self, event: dict) -> None:
        """Push one event, swallowing any sink failure."""
        try:
            assert self._sink is not None
            await self._sink(event)
        except Exception:  # noqa: BLE001 — a dead WS client must not kill a run
            pass


class ApexAgent(BaseAgent):
    name = "APEX"
    role = "Master Supervisor"
    icon = "⊕"
    description = "Routes investigations, orchestrates agent swarms, synthesizes reports"
    preferred_models = ["qwen2.5:7b", "gemma2:9b", "claude-opus-4"]
    token_budget = 16384

    #: Agents that reason over the *aggregated* output of the tiers above them.
    #: They must run AFTER collection, or they would correlate an empty set.
    CORRELATION_TIER: list[str] = ["NEXUS", "KRONOS", "VAULT", "SENTINEL", "QUILL"]

    #: Dependency DAG. Each entry is a stage: every agent inside a stage may run
    #: concurrently; stages run in order.
    DAG: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("correlate", ("NEXUS", "KRONOS")),   # both read only the primary swarm
        ("observe", ("VAULT", "SENTINEL")),   # both read everything so far
        ("report", ("QUILL",)),                # needs the finished aggregate
    )

    #: Last agent in the DAG must never run — it is the synthesiser.
    _TERMINAL = "QUILL"

    #: Transient failures worth retrying (everything else fails fast).
    RETRYABLE = (asyncio.TimeoutError, ConnectionError, OSError)

    # ── Class-level knobs (env-overridable, no config.py dependency) ─────────

    @classmethod
    def _setting(cls, attr: str, default: float) -> float:
        """Read a tuning knob defensively (config.py is owned elsewhere)."""
        import os
        raw = os.getenv(f"APEX_{attr}")
        if raw:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
        try:
            import config
            value = getattr(config, f"APEX_{attr}", None)
            if value:
                return float(value)
        except Exception:  # noqa: BLE001
            pass
        return default

    @property
    def max_attempts(self) -> int:
        """Total attempts per agent, including the first."""
        return max(1, int(self._setting("MAX_ATTEMPTS", 2)))

    @property
    def pipeline_budget_s(self) -> float:
        """Wall-clock ceiling for one whole investigation.

        The floor is 1s, not something larger: a caller (or a test) that
        deliberately sets a tight budget must get exactly that budget, or the
        setting becomes a lie. The *default* when nothing is configured is the
        generous 180s.
        """
        return max(1.0, self._setting("PIPELINE_BUDGET_S", 180.0))

    @property
    def max_concurrency(self) -> int:
        """Process-wide ceiling on simultaneously running agents."""
        return max(1, int(self._setting("MAX_CONCURRENCY", 12)))

    @property
    def retry_backoff_s(self) -> float:
        """Base backoff between retries."""
        return max(0.0, self._setting("RETRY_BACKOFF_S", 0.5))

    # A single process-wide gate shared by every APEX instance, so 50
    # investigations each fanning out to 9 agents still cannot saturate the box.
    _global_semaphore: asyncio.Semaphore | None = None
    _semaphore_lock = asyncio.Lock()

    @classmethod
    async def _gate(cls, limit: int) -> asyncio.Semaphore:
        """Return (creating once) the process-wide concurrency gate."""
        if cls._global_semaphore is None:
            async with cls._semaphore_lock:
                if cls._global_semaphore is None:
                    cls._global_semaphore = asyncio.Semaphore(limit)
        return cls._global_semaphore

    # ── Main entry point ─────────────────────────────────────────────────────

    async def run(self, input_data: str | dict, context: dict | None = None) -> AgentResult:
        t0 = self._start_timer()
        context = context or {}
        emit: Callable[[Any], Awaitable[None]] | None = context.get("emit")
        raw = input_data if isinstance(input_data, str) else str(input_data)

        # Shared live-stream queue: every agent's log line flows through here.
        # It carries *strings only* — ``emit`` is an existing public contract
        # (``main._run_pipeline`` appends each item to a list of step strings and
        # broadcasts it as {"type": "step"}).
        queue: asyncio.Queue = asyncio.Queue()
        self.attach_stream(queue)
        drain_task = asyncio.create_task(self._drain(queue, emit)) if emit else None

        # Structured events use their own sink so the string contract holds.
        emit_event: Callable[[dict], Awaitable[None]] | None = context.get("emit_event")
        context = {**context, "_queue": queue, "_events": []}

        deadline = t0 + self.pipeline_budget_s
        events = _EventLog(emit_event)
        started = time.monotonic()
        tokens_total = 0
        timeout_for = agent_timeout

        try:
            # ── 1. Route ──────────────────────────────────────────────────────
            self.log(f"routing input: {raw[:80]}")
            decision = detect_input_type(raw)
            self.log(
                f"detected {decision.input_type.value} "
                f"(confidence={decision.confidence:.0%}, rule={decision.rule}) — "
                f"{decision.reasoning}"
            )
            events.append({
                "event": "routed", "input_type": decision.input_type.value,
                "confidence": decision.confidence, "rule": decision.rule,
                "agents": list(decision.agents),
            })

            context["input_type"] = context.get("input_type") or decision.input_type.value
            # A caller-supplied override wins over detection for the *reported*
            # type too, not just for the agents that receive it.
            reported_type = context["input_type"]
            # Normalized target so equivalent spellings share one cache/case key.
            context.setdefault("target", decision.target)
            context["cache_key"] = decision.target.get("cache_key") or raw[:200]

            registry = self._registry()
            gate = await self._gate(self.max_concurrency)

            all_results: list[AgentResult] = []
            all_agents: list[BaseAgent] = []
            entities: list[dict] = []
            signals: list[dict] = []

            # ── 2. Primary swarm (one parallel stage) ────────────────────────
            primary_names = [n for n in decision.agents if n not in self.CORRELATION_TIER]
            budget_hit = time.monotonic() >= deadline
            if budget_hit and primary_names:
                self.log(f"⚠ pipeline budget exhausted before the primary swarm — "
                         f"skipping {', '.join(primary_names)}")
                events.append({"event": "pipeline_budget_exhausted",
                               "stage": "primary", "skipped": primary_names})
                primary_names = []
            primary = await self._run_stage(
                primary_names, registry, raw, context, gate, events, timeout_for, deadline,
                stage="primary",
            )
            all_agents += [a for a, _ in primary]
            all_results += [r for _, r in primary]
            entities += [e for _, r in primary for e in (r.entities_found or [])]
            signals += [s for _, r in primary for s in (r.signals or [])]

            # ── 3. Correlation DAG ────────────────────────────────────────────
            agent_results_so_far = [self._serialize(a, r) for a, r in
                                    zip(all_agents, all_results)]

            for stage_name, names in self.DAG:
                # QUILL is the synthesiser: it must run even on an exhausted
                # budget, because a report of partial findings is exactly what
                # partial success means. Only its *dependencies* are skipped.
                if time.monotonic() >= deadline and stage_name != "report":
                    remaining = [n for n in names if n not in
                                 {r.agent for r in all_results}]
                    if remaining:
                        self.log(f"⚠ pipeline budget exhausted — skipping {', '.join(remaining)}")
                        events.append({"event": "pipeline_budget_exhausted",
                                       "stage": stage_name, "skipped": remaining})
                    break

                present = [n for n in names if n not in {r.agent for r in all_results}]
                if not present:
                    continue
                self.log(f"stage '{stage_name}': {', '.join(present)}")
                stage_ctx = {
                    **context,
                    "peer_signals": signals,
                    "peer_entities": entities,
                    "agent_results": agent_results_so_far,
                }
                stage = await self._run_stage(
                    present, registry, raw, stage_ctx, gate, events, timeout_for,
                    deadline, stage=stage_name,
                )
                for agent, result in stage:
                    all_agents.append(agent)
                    all_results.append(result)
                    entities += result.entities_found or []
                    signals += result.signals or []
                    # Let later stages see this stage's discoveries.
                    stage_ctx["peer_signals"] = signals
                    stage_ctx["peer_entities"] = entities
                    stage_ctx["agent_results"].append(self._serialize(agent, result))

            # ── 4. Synthesize ────────────────────────────────────────────────
            self.log("synthesizing agent outputs into investigation result…")
            agent_results = [self._serialize(a, r) for a, r in zip(all_agents, all_results)]
            tokens_total = sum(int(r.tokens_used or 0) for r in all_results)

            done = [r for r in all_results if r.status == "done"]
            errored = [r for r in all_results if r.status == "error"]
            if errored:
                self.log(f"⚠ {len(errored)} agent(s) errored: "
                         f"{', '.join(r.agent for r in errored)}")

            raw_conf = self._aggregate_confidence(all_results)
            penalty, partial = self._confidence_penalty(all_results, raw_conf)
            agg_conf = round(max(0.0, raw_conf - penalty), 3)
            elapsed = time.monotonic() - started
            self.log(f"synthesis complete — {len(agent_results)} agents "
                     f"({len(done)} ok / {len(errored)} failed), "
                     f"confidence {agg_conf:.0%}, {elapsed:.1f}s")

            def _agent_output(name: str) -> dict:
                return next((r.get("output") or {} for r in agent_results
                             if r.get("agent") == name), {})
            graph = _agent_output("NEXUS")
            timeline = _agent_output("KRONOS")
            report_md = _agent_output("QUILL").get("markdown")

            agents_activated = (primary_names
                                + [n for _, names in self.DAG for n in names])

            events.append({
                "event": "pipeline_finished", "agents": len(agent_results),
                "failed": len(errored), "confidence": agg_conf,
                "tokens": tokens_total,
            })

            output = {
                # ── legacy keys the frontend depends on ──
                "input": raw[:500],
                "input_type": reported_type,
                "detected_input_type": decision.input_type.value,
                "routing_confidence": decision.confidence,
                "routing_reasoning": decision.reasoning,
                "agents_activated": agents_activated,
                "agent_results": agent_results,
                "entities": entities,
                "signals": signals,
                "graph": {"nodes": graph.get("nodes", []), "edges": graph.get("edges", [])},
                "timeline": timeline.get("events", []),
                "report_markdown": report_md,
                # ── new keys ──
                "routing": {
                    "rule": decision.rule,
                    "source": decision.source,
                    "alternatives": decision.alternatives,
                    "target": decision.target,
                    "cache_key": context["cache_key"],
                },
                "pipeline": {
                    "stages": [
                        {"stage": "primary", "agents": primary_names},
                        *[{"stage": name, "agents": list(names)} for name, names in self.DAG],
                    ],
                    "duration_s": round(elapsed, 2),
                    "budget_s": self.pipeline_budget_s,
                    "budget_exhausted": time.monotonic() >= deadline,
                    "agents_total": len(agent_results),
                    "agents_succeeded": len(done),
                    "agents_failed": len(errored),
                    "partial_success": partial,
                    "confidence_penalty": penalty,
                    "max_concurrency": self.max_concurrency,
                    "max_attempts": self.max_attempts,
                },
                "tokens_used": tokens_total,
                "events": events,
                "failed_agents": [
                    {"agent": r.agent, "error": r.error, "status": r.status}
                    for r in all_results if r.status == "error"
                ],
            }

            return AgentResult(
                agent=self.name,
                status="partial" if errored else "done",
                output=output,
                confidence=agg_conf,
                reasoning=(f"Routed to {decision.input_type.value}; ran "
                           f"{len(all_results)} agents across "
                           f"{1 + len(self.DAG)} stages in {elapsed:.1f}s"
                           + (f"; {len(errored)} failed, confidence penalised "
                              f"by {penalty:.2f}." if errored else ".")),
                entities_found=entities,
                signals=signals,
                latency_s=self._elapsed(t0),
                tokens_used=tokens_total,
            )
        finally:
            if drain_task is not None:
                await queue.put(None)   # sentinel
                await drain_task

    # ── Stage execution ──────────────────────────────────────────────────────

    async def _run_stage(self, names, registry, raw, context, gate, events,
                         timeout_for, deadline, *, stage) -> list[tuple[BaseAgent, AgentResult]]:
        """Run one DAG stage: every named agent concurrently.

        Missing implementations are skipped with a note rather than failing the
        stage. Returns ``(agent, result)`` pairs in the order of ``names``.
        """
        agents: list[BaseAgent] = []
        for agent_name in names:
            cls = registry.get(agent_name)
            if cls is None:
                self.log(f"⚠ no implementation registered for {agent_name} — skipping")
                events.append({"event": "agent_missing", "agent": agent_name,
                               "stage": stage})
                continue
            agent = cls()
            agent.attach_stream(context.get("_queue"))
            agents.append(agent)

        if not agents:
            return []

        results = await asyncio.gather(*(
            self._safe_run(a, raw, context, gate, events, timeout_for, deadline,
                           stage=stage)
            for a in agents
        ))
        return list(zip(agents, results))

    async def _safe_run(self, agent: BaseAgent, raw: str, context: dict, gate,
                        events, timeout_for, deadline, *, stage: str) -> AgentResult:
        """Run one agent with timeout + retry, converting failure into a result."""
        t0 = self._start_timer()
        # The pipeline budget is a *hard* ceiling for the whole run, so an
        # agent may never be given more time than remains before the deadline.
        # Clamping up (rather than down) here would let one agent overrun the
        # entire budget, which is the exact failure the budget exists to stop.
        remaining = deadline - time.monotonic()
        if remaining <= 0.25:
            return AgentResult(
                agent=agent.name, status="error", output=None,
                error="pipeline time budget exhausted before this agent started",
                latency_s=self._elapsed(t0),
            )
        budget = max(0.25, remaining)
        limit = min(timeout_for(agent.name), budget)
        attempts = self.max_attempts
        last_error: str | None = None

        for attempt in range(1, attempts + 1):
            events.append({"event": "agent_started", "agent": agent.name,
                           "stage": stage, "attempt": attempt,
                           "timeout_s": round(limit, 2)})
            try:
                async with gate:
                    result = await asyncio.wait_for(
                        agent.run(raw, context), timeout=limit)
            except asyncio.TimeoutError:
                last_error = f"timeout after {limit:.0f}s"
                agent.log(f"⚠ timeout after {limit:.0f}s (attempt {attempt}/{attempts})")
                if attempt < attempts:
                    await self._sleep_backoff(attempt)
                    continue
                break
            except self.RETRYABLE as exc:  # noqa: PERF203 — retry loop
                last_error = f"{type(exc).__name__}: {exc}"
                agent.log(f"transient failure {last_error} "
                          f"(attempt {attempt}/{attempts})")
                if attempt < attempts:
                    await self._sleep_backoff(attempt)
                    continue
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — isolate one agent's failure
                last_error = f"{type(exc).__name__}: {exc}"
                agent.log(f"error: {exc}")
                break

            # Success (any non-error status counts as a result).
            events.append({"event": "agent_finished", "agent": agent.name,
                           "stage": stage, "status": result.status,
                           "confidence": result.confidence,
                           "latency_s": result.latency_s,
                           "attempts": attempt, "tokens": result.tokens_used})
            return result

        events.append({"event": "agent_failed", "agent": agent.name,
                       "stage": stage, "error": last_error,
                       "attempts": attempts})
        return AgentResult(
            agent=agent.name, status="error", output=None, error=last_error,
            latency_s=self._elapsed(t0),
        )

    async def _sleep_backoff(self, attempt: int) -> None:
        """Exponential backoff with jitter, so retries do not synchronise."""
        delay = self.retry_backoff_s * (2 ** (attempt - 1))
        await asyncio.sleep(delay * (0.5 + random.random() / 2))

    # ── Scoring helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _aggregate_confidence(results: list[AgentResult]) -> float:
        """Mean of the agents that actually reported a confidence."""
        confs = [r.confidence for r in results if r.confidence and r.confidence > 0]
        return round(sum(confs) / len(confs), 3) if confs else 0.3

    @staticmethod
    def _confidence_penalty(results: list[AgentResult],
                            base: float) -> tuple[float, bool]:
        """Penalise the aggregate confidence for what did not complete.

        A run where most agents failed should never look confident. The penalty
        scales with the failure rate and is floored so a partial success still
        reports *something* useful rather than collapsing to zero.

        Args:
            results: Every agent result.
            base: The pre-penalty confidence.
        Returns:
            ``(penalty, partial_success_flag)``.
        """
        total = len(results)
        if not total:
            return 0.0, False
        failed = [r for r in results if r.status == "error"]
        if not failed:
            return 0.0, False
        rate = len(failed) / total
        penalty = round(min(0.6, 0.1 * len(failed) + 0.4 * rate * rate), 3)
        partial = len(failed) < total
        return penalty, partial

    # ── Helpers ──────────────────────────────────────────────────────────────

    async def _drain(self, queue: asyncio.Queue,
                     emit: Callable[[Any], Awaitable[None]]) -> None:
        """Relay queued log lines and structured events to ``emit``."""
        while True:
            item = await queue.get()
            if item is None:
                break
            try:
                if isinstance(item, dict):
                    await emit(item)
                else:
                    await emit(item)
            except Exception:  # noqa: BLE001 — a dead WS client must not kill the run
                pass

    @staticmethod
    def _serialize(agent: BaseAgent, r: AgentResult) -> dict:
        """Flatten an AgentResult (+ its live steps) for storage / transport."""
        return {
            "agent": r.agent,
            "role": agent.role,
            "icon": agent.icon,
            "status": r.status,
            "output": r.output,
            "confidence": r.confidence,
            "reasoning": r.reasoning,
            "entities_found": r.entities_found,
            "signals": r.signals,
            "latency_s": r.latency_s,
            "tokens_used": r.tokens_used,
            "error": r.error,
            "steps": agent._steps,
        }

    @staticmethod
    def _registry() -> dict[str, type[BaseAgent]]:
        """Lazy import the canonical registry to avoid an import-time cycle
        (agents/__init__.py imports this module)."""
        from . import AGENT_REGISTRY
        return AGENT_REGISTRY