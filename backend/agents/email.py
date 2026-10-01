"""
Email.py — EMAIL Agent
======================
"""
from __future__ import annotations

import asyncio
from .base import BaseAgent, AgentResult

class EmailAgent(BaseAgent):
    """Email Intelligence — account existence enumeration across providers.

    Given an email, checks which major providers (Gmail, Yahoo, Outlook, Proton,
    iCloud, Tutanota) have the address registered via password-reset flow
    inference. Works over OPSEC layer (Tor) to avoid IP-based rate limits.
    Degrades gracefully when services are unreachable.
    """
    name = "EMAIL"; role = "Email Intelligence"; icon = "✉"
    description = "Email account existence enumeration across 50+ providers (Gmail, Yahoo, Outlook, Proton…)"
    preferred_models = ["qwen2.5:7b"]; token_budget = 4096

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)

        signals: list[dict] = []
        entities: list[dict] = []

        if "@" not in target:
            self.log(f"not an email: {target}")
            return AgentResult(agent=self.name, status="error", output={"input": target},
                               error="not an email address", latency_s=self._elapsed(t0))

        try:
            from connectors.email_enum import run_email_enum
            self.log(f"email account enumeration: {target}")
            data = await asyncio.wait_for(run_email_enum(target), timeout=30.0)
        except asyncio.TimeoutError:
            self.log("email enum timed out after 30s")
            return AgentResult(agent=self.name, status="error", output={"email": target},
                               error="timeout", latency_s=self._elapsed(t0))
        except Exception as e:
            self.log(f"email enum error: {e}")
            return AgentResult(agent=self.name, status="error", output={"email": target},
                               error=str(e), latency_s=self._elapsed(t0))

        found = data.get("providers_with_account", [])
        if found:
            self.log(f"found accounts on {len(found)} provider(s): {', '.join(found)}")
            for provider in found:
                signals.append({"type": "email_account", "provider": provider,
                                "email": target, "source": "email_enum"})
        else:
            self.log("no accounts found on major providers")

        entities.append({"id": f"email_{target.lower()}", "label": target, "type": "email"})
        signals.append({"type": "email_profile", "address": target,
                        "providers_checked": data.get("total_providers_checked", 0),
                        "providers_found": len(found), "source": "email_enum"})

        confidence = 0.8 if found else 0.3
        return AgentResult(
            agent=self.name, status="done",
            output={
                "email": target,
                "accounts_found": found,
                "details": data.get("details", {}),
            },
            confidence=confidence,
            reasoning=f"Email {target}: found on {len(found)} provider(s)"
                      + (f" ({', '.join(found)})." if found else "."),
            signals=signals, entities_found=entities,
            latency_s=self._elapsed(t0),
        )
