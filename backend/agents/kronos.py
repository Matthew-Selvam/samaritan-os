"""
Kronos.py — KRONOS Agent
========================
"""
from __future__ import annotations

import asyncio
from .base import BaseAgent, AgentResult

class KronosAgent(BaseAgent):
    """Timeline Reconstruction — chronology from timestamped signals.

    Extracts every dateable signal produced upstream (EXIF capture time, breach
    dates, account-creation dates, explicit event timestamps) via a tolerant
    multi-format parser, then orders them into a single event chain suitable for
    a vis.js timeline. No external date library required.
    """
    name = "KRONOS"; role = "Timeline Reconstruction"; icon = "⊕"
    description = "Chronology reconstruction: username changes, post history, account creation, location changes"
    preferred_models = ["gemma2:9b"]; token_budget = 6144

    _FORMATS = (
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y",
        "%B %d, %Y", "%m/%d/%Y", "%Y",
    )

    @classmethod
    def _parse_date(cls, value):
        from datetime import datetime
        if not value:
            return None
        s = str(value).strip().replace("Z", "")
        for fmt in cls._FORMATS:
            try:
                return datetime.strptime(s[:len(datetime.now().strftime(fmt)) + 4], fmt)
            except (ValueError, TypeError):
                continue
        # last resort: leading ISO date
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        context = context or {}
        peer_signals: list[dict] = context.get("peer_signals", []) or []
        self.log(f"reconstructing chronology from {len(peer_signals)} signals")

        events: list[dict] = []
        for s in peer_signals:
            stype = s.get("type")
            raw_date = None
            label = None
            if stype == "camera" or stype == "gps":
                raw_date = s.get("datetime")
            if stype == "breach":
                raw_date, label = s.get("date"), f"Data breach: {s.get('name', 'unknown')}"
            elif s.get("date"):
                raw_date = s.get("date")
            elif s.get("datetime"):
                raw_date = s.get("datetime")
            elif s.get("created_at"):
                raw_date = s.get("created_at")

            dt = self._parse_date(raw_date)
            if dt is None:
                continue
            if label is None:
                label = {
                    "account": f"Account seen: {s.get('platform', '?')}",
                    "person_record": f"Record: {s.get('full_name', '?')}",
                }.get(stype, f"{stype} event")
            events.append({
                "timestamp": dt.isoformat(),
                "year": dt.year,
                "label": label,
                "type": stype,
                "source": s.get("source", "unknown"),
            })

        events.sort(key=lambda e: e["timestamp"])
        span = None
        if len(events) >= 2:
            span = {"earliest": events[0]["timestamp"], "latest": events[-1]["timestamp"],
                    "years": events[-1]["year"] - events[0]["year"]}
            self.log(f"timeline spans {span['earliest'][:10]} → {span['latest'][:10]} "
                     f"({len(events)} events)")
        else:
            self.log(f"reconstructed {len(events)} dated event(s)")

        signals = [{"type": "timeline", "event_count": len(events),
                    "span": span, "source": "kronos"}]
        return AgentResult(
            agent=self.name, status="done",
            output={"events": events, "span": span, "event_count": len(events)},
            confidence=0.75 if len(events) >= 2 else (0.3 if events else 0.1),
            reasoning=f"Reconstructed {len(events)} dated event(s)"
                      + (f" spanning {span['years']} year(s)." if span else "."),
            signals=signals,
            latency_s=self._elapsed(t0),
        )
