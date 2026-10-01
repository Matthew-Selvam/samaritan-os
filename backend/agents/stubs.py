"""
stubs.py — Backwards-compatible re-export shim
===============================================
The agent implementations that used to live here now each live in their own
module (crawler.py, prism.py, iris.py, echo.py, terra.py, ink.py, nexus.py,
kronos.py, vault.py, sentinel.py, email.py, quill.py).

This module is kept so that existing imports
``from .stubs import PrismAgent, ...`` keep working unchanged.
"""

from __future__ import annotations

from .base import BaseAgent, AgentResult
from .crawler import CrawlerAgent
from .prism import PrismAgent
from .iris import IrisAgent
from .echo import EchoAgent
from .terra import TerraAgent
from .ink import InkAgent
from .nexus import NexusAgent
from .kronos import KronosAgent
from .vault import VaultAgent
from .sentinel import SentinelAgent
from .email import EmailAgent
from .quill import QuillAgent

__all__ = [
    "BaseAgent",
    "AgentResult",
    "CrawlerAgent",
    "PrismAgent",
    "IrisAgent",
    "EchoAgent",
    "TerraAgent",
    "InkAgent",
    "NexusAgent",
    "KronosAgent",
    "VaultAgent",
    "SentinelAgent",
    "EmailAgent",
    "QuillAgent",
]
