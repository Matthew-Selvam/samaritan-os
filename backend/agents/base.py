"""
base.py — Base Agent class
===========================
All Signal-OS agents inherit from this. Defines the contract:
  - name / role / icon
  - run(input, context) → AgentResult
  - tool call interface
  - confidence scoring
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentResult:
    agent: str
    status: str                    # done | error | partial
    output: Any                    # structured payload (depends on agent)
    confidence: float = 0.0        # 0.0 – 1.0
    reasoning: str = ""            # why this conclusion was reached
    entities_found: list[dict] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)
    latency_s: float = 0.0
    tokens_used: int = 0
    error: str | None = None


class BaseAgent(ABC):
    """Base class for all Signal-OS intelligence agents."""

    name: str = "BASE"
    role: str = "Generic agent"
    icon: str = "◎"
    description: str = ""

    # Model preferences (first available wins)
    preferred_models: list[str] = ["gemma2:9b", "qwen2.5:7b"]
    # Obsidian vault context folders to pull from
    context_folders: list[str] = []
    # Max tokens this agent should consume per run
    token_budget: int = 8192

    def __init__(self):
        self._steps: list[str] = []
        # Optional async-safe sink for live step streaming. When an orchestrator
        # (APEX) attaches a queue, every log line is also pushed to it so it can
        # be relayed to WebSocket clients in real time. Synchronous, non-blocking.
        self._stream: Any = None

    def attach_stream(self, queue: Any) -> None:
        """Attach an asyncio.Queue so log() lines stream out live."""
        self._stream = queue

    def log(self, msg: str):
        line = f"[{self.name}] {msg}"
        self._steps.append(line)
        print(f"  {line}")
        if self._stream is not None:
            try:
                self._stream.put_nowait(line)
            except Exception:
                pass  # never let telemetry break the agent

    @abstractmethod
    async def run(
        self,
        input_data: str | dict,
        context: dict | None = None,
    ) -> AgentResult:
        """Execute the agent's primary function."""
        ...

    def _start_timer(self) -> float:
        return time.time()

    def _elapsed(self, t0: float) -> float:
        return round(time.time() - t0, 2)


# ── Agent helpers (appended) ─────────────────────────────────────────────────
# Deliberately named ``call_llm`` / ``call_llm_json`` / ``agent_timeout`` rather
# than ``llm`` / ``llm_json`` / ``timeout`` so this block cannot collide with the
# LLM-helper block owned by another workstream. Both sets coexist.

async def call_llm(
    agent: BaseAgent,
    prompt: str,
    *,
    system: str | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
    model: str | None = None,
) -> str | None:
    """Run one completion through the shared LLM facade.

    Returns the model text, or ``None`` when no provider served the call — the
    caller then takes its deterministic path. Never raises.

    Args:
        agent: The calling agent (for the model preference + logging).
        prompt: The user prompt.
        system: Optional system prompt.
        timeout: Seconds; defaults to ``llm.LLM_DEFAULT_TIMEOUT``.
        max_tokens: Completion budget; defaults to the agent's token budget.
        model: Model hint; defaults to the agent's first preference.
    Returns:
        The completion text, or ``None``.
    """
    try:
        from llm import get_llm
    except Exception as exc:  # noqa: BLE001 — llm.py is optional
        agent.log(f"llm unavailable ({type(exc).__name__})")
        return None
    try:
        result = await get_llm().complete(
            prompt,
            system=system,
            model=model or next(iter(agent.preferred_models), None),
            max_tokens=max_tokens or agent.token_budget,
            timeout=timeout or 20.0,
        )
    except Exception as exc:  # noqa: BLE001 — the AI tier is never load-bearing
        agent.log(f"llm call failed ({type(exc).__name__})")
        return None
    if result.error:
        agent.log(f"llm offline — using deterministic path")
        return None
    return result.text


async def call_llm_json(
    agent: BaseAgent,
    prompt: str,
    *,
    system: str | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
    schema_hint: str | None = None,
) -> dict | list | None:
    """Run one completion and parse the reply as JSON.

    Returns:
        The decoded object/array, or ``None`` when the model is unavailable or
        the reply did not parse. Never raises.
    """
    try:
        from llm import get_llm
    except Exception as exc:  # noqa: BLE001
        agent.log(f"llm unavailable ({type(exc).__name__})")
        return None
    try:
        payload = await get_llm().complete_json(
            prompt,
            system=system,
            max_tokens=max_tokens or agent.token_budget,
            timeout=timeout or 20.0,
            schema_hint=schema_hint,
        )
    except Exception as exc:  # noqa: BLE001
        agent.log(f"llm json call failed ({type(exc).__name__})")
        return None
    if isinstance(payload, dict) and payload.get("error"):
        return None
    return payload if isinstance(payload, (dict, list)) else None


def agent_timeout(agent_name: str, default: float = 30.0) -> float:
    """Resolve an agent's configured timeout, defensively.

    ``config`` is being extended concurrently, so a missing attribute or a
    raising ``get_timeout`` must not take a pipeline down.

    Args:
        agent_name: Agent name, e.g. ``"PHONOS"``.
        default: Fallback when config cannot answer.
    Returns:
        Seconds allowed for the agent.
    """
    try:
        import config
        getter = getattr(config, "get_timeout", None)
        if callable(getter):
            value = getter(agent_name)
            if value:
                return float(value)
        raw = getattr(config, f"TIMEOUT_{agent_name}", None)
        if raw:
            return float(raw)
        return float(getattr(config, "TIMEOUT_DEFAULT", default))
    except Exception:  # noqa: BLE001 — never fail a run over a timeout lookup
        return default
