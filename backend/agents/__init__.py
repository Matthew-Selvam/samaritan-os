"""
Signal-OS Agent Registry
========================
14 specialist agents, each with a defined role, tool set, and model assignment.
"""

from .base import BaseAgent, AgentResult
from .scout import ScoutAgent
from .sigma import SigmaAgent
from .phonos import PhonosAgent
from .stubs import (
    CrawlerAgent, IrisAgent, EchoAgent, PrismAgent,
    NexusAgent, KronosAgent, VaultAgent, SentinelAgent,
    EmailAgent, QuillAgent, TerraAgent, InkAgent,
)
from .apex import ApexAgent  # master supervisor

AGENT_REGISTRY: dict[str, type[BaseAgent]] = {
    "SCOUT":    ScoutAgent,
    "PHONOS":   PhonosAgent,
    "EMAIL":    EmailAgent,
    "CRAWLER":  CrawlerAgent,
    "IRIS":     IrisAgent,
    "ECHO":     EchoAgent,
    "PRISM":    PrismAgent,
    "NEXUS":    NexusAgent,
    "KRONOS":   KronosAgent,
    "VAULT":    VaultAgent,
    "SENTINEL": SentinelAgent,
    "QUILL":    QuillAgent,
    "SIGMA":    SigmaAgent,
    "TERRA":    TerraAgent,
    "INK":      InkAgent,
    "APEX":     ApexAgent,
}

__all__ = ["AGENT_REGISTRY", "BaseAgent", "AgentResult"]
