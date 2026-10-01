"""
base.py — Base Agent class
===========================
All Samaritan OS agents inherit from this. Defines the contract:
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
    """Base class for all Samaritan OS intelligence agents."""

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
