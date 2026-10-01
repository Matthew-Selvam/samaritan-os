"""
Ink.py — INK Agent
==================
"""
from __future__ import annotations

import asyncio
from .base import BaseAgent, AgentResult

class InkAgent(BaseAgent):
    """Stylometry — basic bio fingerprinting across platforms.

    Analyzes writing patterns, slang, vocabulary to link accounts.
    """
    name = "INK"; role = "Stylometry"; icon = "✦"
    description = "Writing fingerprinting, authorship analysis, cross-platform bio matching"
    preferred_models = ["qwen2.5:7b"]; token_budget = 8192

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        target = input_data if isinstance(input_data, str) else str(input_data)

        self.log("analyzing text patterns for stylometric fingerprinting")

        # Basic text analysis — word frequency, avg word length, punctuation patterns
        words = target.split()
        word_count = len(words)
        avg_word_len = sum(len(w) for w in words) / max(word_count, 1)
        unique_ratio = len(set(w.lower() for w in words)) / max(word_count, 1)

        # Character-level features
        char_count = len(target)
        upper_ratio = sum(1 for c in target if c.isupper()) / max(char_count, 1)
        punct_count = sum(1 for c in target if c in "!?.,;:—–-")
        emoji_like = sum(1 for c in target if ord(c) > 0x1F600)

        profile = {
            "word_count": word_count,
            "avg_word_length": round(avg_word_len, 2),
            "vocabulary_richness": round(unique_ratio, 3),
            "uppercase_ratio": round(upper_ratio, 3),
            "punctuation_density": round(punct_count / max(word_count, 1), 3),
            "emoji_count": emoji_like,
            "char_count": char_count,
        }

        self.log(f"stylometric profile: {word_count} words, vocab richness={unique_ratio:.2%}")

        signals = [{"type": "stylometry", "profile": profile, "source": "ink_analysis"}]
        return AgentResult(
            agent=self.name, status="done",
            output={"profile": profile, "text_preview": target[:200]},
            confidence=0.5 if word_count > 20 else 0.2,
            reasoning=f"Stylometric analysis: {word_count} words, richness={unique_ratio:.2%}",
            signals=signals,
            latency_s=self._elapsed(t0),
        )
