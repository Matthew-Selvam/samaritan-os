"""
SIGMA — Threat Intelligence Agent
=================================
IOC correlation, breach analysis, infrastructure mapping.

For IP and domain inputs, SIGMA queries Shodan (via connectors/shodan.py) for
host / infrastructure intelligence — open ports, services, CVEs, geolocation,
DNS records — and normalizes the result into IOC signals. For other input types
it records that Shodan is IP/domain-scoped and returns an inert result.

The Shodan connector hard-imports the ``shodan`` SDK (an optional dependency not
pinned in requirements.txt), so SIGMA imports it lazily and degrades gracefully
when the SDK or an API key is missing — the pipeline never breaks.
"""
from __future__ import annotations

import asyncio
import os

from .base import BaseAgent, AgentResult
from router import detect_input_type  # absolute: backend/ is the path root (main:app)


class SigmaAgent(BaseAgent):
    name = "SIGMA"
    role = "Threat Intelligence"
    icon = "⊗"
    description = "IOC ingestion, phishing detection, breach correlation, infrastructure mapping"
    preferred_models = ["gemma2:9b"]
    token_budget = 8192

    # Shodan lookups only make sense for these input types.
    SHODAN_TYPES = {"ip_address", "domain"}
    SHODAN_TIMEOUT = 10.0

    async def run(self, input_data: str | dict, context: dict | None = None) -> AgentResult:
        t0 = self._start_timer()
        context = context or {}
        target = (input_data if isinstance(input_data, str) else str(input_data)).strip()

        # Resolve the input type: prefer what APEX injected into context; else self-detect.
        input_type = context.get("input_type") or detect_input_type(target).input_type.value

        if input_type not in self.SHODAN_TYPES:
            self.log(f"input_type={input_type!r} is not ip_address/domain — Shodan lookup skipped")
            return AgentResult(
                agent=self.name, status="done",
                output={"iocs": [], "input_type": input_type, "shodan": None,
                        "note": "Shodan applies to ip_address/domain inputs only."},
                confidence=0.1,
                reasoning="No IP/domain target — Shodan not applicable.",
                latency_s=self._elapsed(t0),
            )

        # Lazy import: the connector pulls in the `shodan` SDK, which may be absent.
        try:
            from connectors.shodan import run_shodan
        except Exception as e:  # noqa: BLE001 — ImportError (SDK missing) or worse
            self.log(f"Shodan connector unavailable: {e}")
            return self._inactive(t0, input_type,
                                  note="shodan SDK not installed — `pip install shodan` to enable.",
                                  error=str(e))

        api_key = os.getenv("SHODAN_API_KEY")
        self.log(f"querying Shodan for {input_type} {target[:60]}"
                 + ("" if api_key else " (no SHODAN_API_KEY set)"))
        try:
            res = await asyncio.wait_for(run_shodan(target, api_key=api_key), timeout=self.SHODAN_TIMEOUT)
        except asyncio.TimeoutError:
            self.log(f"Shodan timed out after {self.SHODAN_TIMEOUT:.0f}s")
            return self._inactive(t0, input_type, note=f"Shodan timed out after {self.SHODAN_TIMEOUT:.0f}s",
                                  error="timeout")
        except Exception as e:  # noqa: BLE001 — never let a connector kill the agent
            self.log(f"Shodan call failed: {e}")
            return self._inactive(t0, input_type, note="Shodan call failed", error=str(e))

        # The connector returns a raw dict; an "error" key means it could not run live.
        if "error" in res:
            self.log(f"Shodan inactive — {res['error']}")
            return self._inactive(t0, input_type, note=res["error"], error=res["error"])

        signals = self._to_iocs(input_type, target, res)
        self.log(f"Shodan live — {len(signals)} IOC signal(s)")
        return AgentResult(
            agent=self.name, status="done",
            output={"iocs": signals, "input_type": input_type, "shodan": res,
                    "note": f"Shodan returned {len(signals)} IOC signal(s)."},
            confidence=0.6 if signals else 0.4,
            reasoning="Shodan host/infrastructure intelligence retrieved.",
            signals=signals,
            latency_s=self._elapsed(t0),
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _inactive(self, t0: float, input_type: str, *, note: str, error: str | None) -> AgentResult:
        """Wired-but-not-live result. Status stays 'done' (low confidence), not 'error',
        so a missing key/SDK doesn't inflate the pipeline's error count."""
        return AgentResult(
            agent=self.name, status="done",
            output={"iocs": [], "input_type": input_type, "shodan": None, "note": note},
            confidence=0.2,
            reasoning="Shodan connector wired but not live (missing SDK/API key or request failed).",
            error=error,
            latency_s=self._elapsed(t0),
        )

    @staticmethod
    def _to_iocs(input_type: str, target: str, res: dict) -> list[dict]:
        """Normalize the Shodan host payload into IOC signals."""
        signals: list[dict] = []
        ip = res.get("ip") or target
        for port in (res.get("ports") or []):
            signals.append({"type": "open_port", "ip": ip, "port": port})
        for host in (res.get("hostnames") or []):
            signals.append({"type": "hostname", "ip": ip, "hostname": host})
        for vuln in (res.get("vulns") or []):
            signals.append({"type": "vuln", "ip": ip, "cve": vuln})
        for svc in (res.get("services") or []):
            signals.append({"type": "service", "ip": ip, "port": svc.get("port"),
                            "product": svc.get("product"), "version": svc.get("version")})
        if res.get("org"):
            signals.append({"type": "org", "ip": ip, "org": res.get("org")})
        if res.get("country") or res.get("city"):
            signals.append({"type": "geo", "ip": ip, "city": res.get("city"),
                            "country": res.get("country"),
                            "lat": res.get("latitude"), "lon": res.get("longitude")})
        return signals
