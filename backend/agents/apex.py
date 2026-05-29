"""
APEX — Master Supervisor
=========================
Routes every investigation. Decides which agents to activate,
in what order, and how to synthesize their outputs.
Uses LangGraph for stateful multi-agent orchestration.
"""
from __future__ import annotations

from .base import BaseAgent, AgentResult
from ..router import detect_input_type, AGENT_MAP


class ApexAgent(BaseAgent):
    name = "APEX"
    role = "Master Supervisor"
    icon = "⊕"
    description = "Routes investigations, orchestrates agent swarms, synthesizes reports"
    preferred_models = ["qwen2.5:7b", "gemma2:9b", "claude-opus-4"]
    token_budget = 16384

    async def run(self, input_data: str | dict, context: dict | None = None) -> AgentResult:
        t0 = self._start_timer()
        raw = input_data if isinstance(input_data, str) else str(input_data)

        self.log(f"routing: {raw[:80]}")
        decision = detect_input_type(raw)
        self.log(f"detected {decision.input_type} (confidence={decision.confidence:.0%}) — {decision.reasoning}")
        self.log(f"activating agents: {', '.join(decision.agents)}")

        # TODO: LangGraph StateGraph execution here
        # For now: return routing plan
        return AgentResult(
            agent=self.name,
            status="done",
            output={
                "input_type": decision.input_type,
                "agents_activated": decision.agents,
                "routing_confidence": decision.confidence,
                "routing_reasoning": decision.reasoning,
                "pipeline": "stubbed — wire LangGraph in orchestrator.py",
            },
            confidence=decision.confidence,
            reasoning=decision.reasoning,
            latency_s=self._elapsed(t0),
        )
