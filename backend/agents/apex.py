"""
APEX — Master Supervisor
=========================
Routes every investigation. Detects the input type, activates the correct
swarm of specialist agents (from AGENT_MAP), runs them concurrently, and
synthesizes their outputs into a single investigation result.

Live streaming: when the caller passes an async ``emit`` callback via context,
every agent's log line is relayed in real time (APEX attaches a shared queue to
each agent and drains it to ``emit`` as lines arrive).
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from .base import BaseAgent, AgentResult
from router import detect_input_type  # absolute: backend/ is the path root (main:app)


class ApexAgent(BaseAgent):
    name = "APEX"
    role = "Master Supervisor"
    icon = "⊕"
    description = "Routes investigations, orchestrates agent swarms, synthesizes reports"
    preferred_models = ["qwen2.5:7b", "gemma2:9b", "claude-opus-4"]
    token_budget = 16384

    async def run(self, input_data: str | dict, context: dict | None = None) -> AgentResult:
        t0 = self._start_timer()
        context = context or {}
        emit: Callable[[str], Awaitable[None]] | None = context.get("emit")
        raw = input_data if isinstance(input_data, str) else str(input_data)

        # Shared live-stream queue: every agent's log() line flows through here.
        queue: asyncio.Queue = asyncio.Queue()
        self.attach_stream(queue)
        drain_task = asyncio.create_task(self._drain(queue, emit)) if emit else None

        try:
            # ── 1. Route ──────────────────────────────────────────────────────
            self.log(f"routing input: {raw[:80]}")
            decision = detect_input_type(raw)
            self.log(
                f"detected {decision.input_type.value} "
                f"(confidence={decision.confidence:.0%}) — {decision.reasoning}"
            )

            # ── 2. Activate the swarm from the routing decision ───────────────
            registry = self._registry()
            agents: list[BaseAgent] = []
            for agent_name in decision.agents:
                cls = registry.get(agent_name)
                if cls is None:
                    self.log(f"⚠ no implementation registered for {agent_name} — skipping")
                    continue
                agent = cls()
                agent.attach_stream(queue)   # so its logs stream live too
                agents.append(agent)

            self.log(
                f"activating {len(agents)} agents concurrently: "
                f"{', '.join(a.name for a in agents)}"
            )

            # ── 3. Run them all in parallel ───────────────────────────────────
            results: list[AgentResult] = await asyncio.gather(
                *(self._safe_run(a, raw, context) for a in agents)
            )

            # ── 4. Synthesize ─────────────────────────────────────────────────
            self.log("synthesizing agent outputs into investigation result…")
            agent_results = [self._serialize(a, r) for a, r in zip(agents, results)]
            entities = [e for r in results for e in (r.entities_found or [])]
            signals = [s for r in results for s in (r.signals or [])]

            confs = [r.confidence for r in results if r.confidence and r.confidence > 0]
            agg_conf = round(sum(confs) / len(confs), 3) if confs else decision.confidence

            errored = [r.agent for r in results if r.status == "error"]
            if errored:
                self.log(f"⚠ {len(errored)} agent(s) errored: {', '.join(errored)}")
            self.log(f"synthesis complete — {len(agent_results)} agents, confidence {agg_conf:.0%}")

            output = {
                "input": raw[:500],
                "input_type": decision.input_type.value,
                "routing_confidence": decision.confidence,
                "routing_reasoning": decision.reasoning,
                "agents_activated": decision.agents,
                "agent_results": agent_results,
                "entities": entities,
                "signals": signals,
            }

            return AgentResult(
                agent=self.name,
                status="done",
                output=output,
                confidence=agg_conf,
                reasoning=f"Routed to {decision.input_type.value}; ran {len(agents)} agents in parallel.",
                entities_found=entities,
                signals=signals,
                latency_s=self._elapsed(t0),
            )
        finally:
            # Flush the stream and stop the drain task cleanly.
            if drain_task is not None:
                await queue.put(None)   # sentinel
                await drain_task

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _drain(self, queue: asyncio.Queue, emit: Callable[[str], Awaitable[None]]) -> None:
        """Relay queued log lines to the emit callback until the sentinel arrives."""
        while True:
            line = await queue.get()
            if line is None:
                break
            try:
                await emit(line)
            except Exception:
                pass  # a dead WS client must not kill the pipeline

    async def _safe_run(self, agent: BaseAgent, raw: str, context: dict) -> AgentResult:
        """Run one agent, converting any exception into an error AgentResult."""
        t0 = self._start_timer()
        try:
            return await agent.run(raw, context)
        except Exception as e:  # noqa: BLE001 — isolate one agent's failure
            agent.log(f"error: {e}")
            return AgentResult(
                agent=agent.name,
                status="error",
                output=None,
                error=str(e),
                latency_s=self._elapsed(t0),
            )

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
