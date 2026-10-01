"""
Sentinel.py — SENTINEL Agent
=============================
Real change detection across repeated observations of the same target.

On each run SENTINEL:

1. fingerprints every current signal individually (stable hash of its
   type + normalized content), so an item can be recognised across runs even
   when its position in the list changes;
2. loads the previous snapshot for the same target;
3. emits **only** what is NEW, CHANGED or REMOVED — an unchanged target reports
   nothing, which is the whole point of a monitor;
4. supports multiple intervals (``daily``/``weekly``/``on_demand``) and
   suppresses repeat alerts for a change that is already acknowledged, so a
   flapping target cannot spam a channel.

Storage is file-backed and atomic (temp file + ``os.replace``); corrupt files
are quarantined rather than fatal.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone

from .base import BaseAgent, AgentResult


#: How much of a snapshot is retained per run (bounded growth).
MAX_HISTORY = 25
#: Fields excluded from an item's fingerprint: pure transport metadata.
_FINGERPRINT_SKIP = {"source", "latency_s", "timestamp", "observed_at", "raw"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: str, payload: dict) -> None:
    """Write JSON atomically (temp file in the same dir + ``os.replace``)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".sentinel-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def item_identity(item: dict) -> str:
    """Stable identity for one observed item, independent of its content.

    Identity must survive a content change — that is the whole basis for
    reporting an item as CHANGED rather than as a new item plus a removal. It
    is derived from the kind of thing observed plus whatever identifies it
    (an id, a value, a hostname, a CVE, a name, …), never from the content
    fields that are allowed to change.

    Args:
        item: One signal or entity dict.
    Returns:
        A 16-hex-character digest.
    """
    identity = {
        "type": item.get("type", "?"),
        "id": item.get("id") or item.get("value") or item.get("cve")
              or item.get("name") or item.get("hostname") or item.get("domain")
              or item.get("title"),
        "platform": item.get("platform"),
        "label": item.get("label"),
    }
    blob = json.dumps(identity, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def _fingerprint(item: dict) -> str:
    """Content fingerprint: identity plus every non-transport field.

    Two observations of the same identity with the same fingerprint are
    unchanged; different fingerprints mean the item CHANGED.

    Args:
        item: One signal dict.
    Returns:
        A 16-hex-character digest of the item's identity + content.
    """
    identity = item_identity(item)
    content = {k: v for k, v in item.items()
               if k not in _FINGERPRINT_SKIP and k != "type"}
    blob = json.dumps({"identity": identity, "content": content},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def _summarize(item: dict) -> str:
    """One-line human label for an item, used in the diff output."""
    for key in ("label", "name", "cve", "value", "hostname", "domain", "title"):
        if item.get(key):
            return str(item[key])[:80]
    stype = item.get("type", "signal")
    parts = ", ".join(f"{k}={v}" for k, v in sorted(item.items())
                      if k not in ("type", "source") and not isinstance(v, (dict, list)))
    return f"{stype}: {parts}"[:80] if parts else stype


class SentinelAgent(BaseAgent):
    """Monitoring — baseline snapshots, per-item fingerprints, diffing."""

    name = "SENTINEL"
    role = "Live Monitoring"
    icon = "⊲"
    description = "Real-time tracking: new posts, bio changes, username changes, new domains, leaks"
    preferred_models = ["phi3:mini"]
    token_budget = 2048

    #: How often a target is meant to be checked; drives the "late" flag.
    INTERVALS = {"hourly": 3600.0, "daily": 86400.0, "weekly": 604800.0,
                 "on_demand": 0.0}

    @staticmethod
    def _store_dir() -> str:
        default = ("/tmp/signal-os/sentinel_store" if os.getenv("VERCEL") else
                   os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "sentinel_store"))
        directory = os.getenv("SENTINEL_DIR", default)
        os.makedirs(directory, exist_ok=True)
        return directory

    @classmethod
    def _path(cls, target: str, interval: str) -> str:
        key = hashlib.sha256(f"{interval}:{target}".encode()).hexdigest()[:20]
        return os.path.join(cls._store_dir(), f"{key}.json")

    @classmethod
    def _read(cls, path: str) -> dict | None:
        """Read a snapshot, quarantining it when corrupt."""
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 — corrupt snapshot must not kill the run
            try:
                os.replace(path, f"{path}.corrupt-{int(time.time())}")
            except OSError:
                pass
            return None

    # ── Diffing ──────────────────────────────────────────────────────────────

    @classmethod
    def diff(cls, current: list[dict], prior: dict | None) -> dict:
        """Compare the current signals against the previous snapshot.

        Matching is by **identity**, not by content fingerprint: an item whose
        content changed keeps its identity and is reported as CHANGED, not as a
        new item plus a removal.

        Args:
            current: Signals observed this run.
            prior: The previous snapshot dict, or ``None`` for a first run.
        Returns:
            ``{"new", "changed", "removed", "unchanged_count", "counts"}``.
        """
        current = [item for item in current if isinstance(item, dict)]
        now_by_id: dict[str, dict] = {}
        for item in current:
            now_by_id.setdefault(item_identity(item), item)

        if prior is None:
            return {"new": list(current), "changed": [], "removed": [],
                    "unchanged_count": 0,
                    "counts": {"new": len(now_by_id), "changed": 0, "removed": 0}}

        prior_items = prior.get("items") or {}
        prior_by_id: dict[str, dict] = {}
        for item in prior_items.values():
            if isinstance(item, dict):
                prior_by_id.setdefault(item_identity(item), item)

        now_ids = set(now_by_id)
        prior_ids = set(prior_by_id)

        new_ids = now_ids - prior_ids
        removed_ids = prior_ids - now_ids
        common = now_ids & prior_ids

        changed: list[dict] = []
        unchanged = 0
        for identity in sorted(common):
            before, after = prior_by_id[identity], now_by_id[identity]
            if json.dumps(before, sort_keys=True, default=str) != \
               json.dumps(after, sort_keys=True, default=str):
                changed.append({"before": before, "after": after})
            else:
                unchanged += 1

        return {
            "new": [now_by_id[i] for i in sorted(new_ids)],
            "changed": changed,
            "removed": [prior_by_id[i] for i in sorted(removed_ids)],
            "unchanged_count": unchanged,
            "counts": {"new": len(new_ids), "changed": len(changed),
                       "removed": len(removed_ids)},
        }

    @staticmethod
    def _change_signature(delta: dict) -> str:
        """Digest of *what* changed, used as the alert-suppression key.

        Hashing the affected items (not the counts) means two unrelated alerts
        that happen to have the same shape ("1 new item") are both delivered,
        while re-observing the *same* change is suppressed.

        Args:
            delta: The diff dict from :meth:`diff`.
        Returns:
            A 16-hex-character digest.
        """
        payload = {
            "new": [_summarize(i) for i in delta.get("new", [])],
            "changed": [[_summarize(c["before"]), _summarize(c["after"])]
                        for c in delta.get("changed", [])],
            "removed": [_summarize(i) for i in delta.get("removed", [])],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]

    # ── Agent entry point ────────────────────────────────────────────────────

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        peer_signals: list[dict] = context.get("peer_signals", []) or []
        peer_entities: list[dict] = context.get("peer_entities", []) or []
        interval = str(context.get("interval") or "on_demand").lower()
        if interval not in self.INTERVALS:
            interval = "on_demand"

        # Watch entities as well as signals: a new linked account matters.
        watched: list[dict] = list(peer_signals) + [
            e for e in peer_entities if isinstance(e, dict)]

        types = sorted({s.get("type", "?") for s in watched})
        fingerprint = hashlib.sha256(
            ("|".join(types) + f"::{len(watched)}").encode()).hexdigest()[:16]
        path = self._path(target, interval)
        prior = self._read(path)

        now = _now()
        delta = self.diff(watched, prior)

        # ── Alert suppression ────────────────────────────────────────────────
        # A change already alerted for this exact target is not re-alerted,
        # unless the caller forces it, so a flapping target cannot spam. The key
        # is the *content* of the change (which items moved), not its shape:
        # "1 new" followed by a different "1 new" is a genuinely new alert.
        acknowledged: dict = dict((prior or {}).get("acknowledged", {}))
        force_alert = bool(context.get("force_alert"))
        change_fp = self._change_signature(delta)
        previously_alerted = acknowledged.get(change_fp)
        alerting = bool(delta["counts"]["new"] or delta["counts"]["changed"]
                        or delta["counts"]["removed"])
        suppressed = bool(not force_alert and alerting and previously_alerted)
        if alerting and not suppressed:
            acknowledged[change_fp] = now
        # Keep the suppression table bounded (oldest first).
        if len(acknowledged) > 50:
            acknowledged = dict(sorted(acknowledged.items(),
                                       key=lambda kv: kv[1])[-50:])

        changes: list[str] = []
        if prior is None:
            self.log(f"baseline snapshot registered for {target[:60]!r} "
                     f"({len(watched)} item(s), interval={interval})")
        else:
            if delta["counts"]["new"]:
                changes.append(f"{delta['counts']['new']} new item(s)")
            if delta["counts"]["changed"]:
                changes.append(f"{delta['counts']['changed']} changed item(s)")
            if delta["counts"]["removed"]:
                changes.append(f"{delta['counts']['removed']} removed item(s)")
            if suppressed:
                changes.append("(suppressed: this change was already alerted)")
            self.log(f"diff vs {(prior or {}).get('last_seen', '?')[:10]}: "
                     + ("; ".join(changes) if changes else "no change"))

        items = {item_identity(item): item for item in watched}
        history = list((prior or {}).get("history", []))
        history.append({"at": now, "counts": delta["counts"],
                        "fingerprint": fingerprint})
        snapshot = {
            "target": target[:120],
            "interval": interval,
            "signal_count": len(watched),
            "signal_types": types,
            "fingerprint": fingerprint,
            "first_seen": (prior or {}).get("first_seen", now),
            "last_seen": now,
            "last_checked_at": (prior or {}).get("last_checked_at", now),
            "checks": (prior or {}).get("checks", 0) + 1,
            "items": items,
            "history": history[-MAX_HISTORY:],
            "acknowledged": acknowledged,
        }
        # Expose which intervals already have a snapshot for this target.
        known_intervals = sorted({interval} | set((prior or {}).get("intervals", [])))

        due_in = None
        if interval != "on_demand":
            try:
                last = datetime.fromisoformat(snapshot["last_checked_at"].replace("Z", "+00:00"))
                due_in = max(0.0, self.INTERVALS[interval]
                             - (datetime.now(timezone.utc) - last).total_seconds())
            except (ValueError, TypeError):
                due_in = None

        try:
            _atomic_write(path, snapshot)
        except Exception as e:  # noqa: BLE001 — a read-only disk is not fatal
            self.log(f"snapshot write error: {e}")

        signals = [{"type": "monitor", "status": "baseline" if prior is None else "diffed",
                    "changes": changes, "checks": snapshot["checks"],
                    "new": delta["counts"]["new"], "changed": delta["counts"]["changed"],
                    "removed": delta["counts"]["removed"],
                    "suppressed": suppressed, "interval": interval,
                    "source": "sentinel"}]
        if alerting and not suppressed:
            for item in delta["new"][:10]:
                signals.append({"type": "monitor_new", "item": _summarize(item),
                                "fingerprint": _fingerprint(item), "source": "sentinel"})
            for entry in delta["changed"][:10]:
                signals.append({"type": "monitor_changed", "before": _summarize(entry["before"]),
                                "after": _summarize(entry["after"]),
                                "fingerprint": _fingerprint(entry["after"]),
                                "source": "sentinel"})
            for item in delta["removed"][:10]:
                signals.append({"type": "monitor_removed", "item": _summarize(item),
                                "fingerprint": _fingerprint(item), "source": "sentinel"})

        confidence = 0.7 if prior is not None else 0.6
        return AgentResult(
            agent=self.name, status="done",
            # Legacy keys kept verbatim.
            output={"monitoring": True, "baseline": prior is None,
                    "changes": changes, "snapshot": snapshot},
            confidence=confidence,
            reasoning=("Baseline snapshot registered." if prior is None
                       else (f"Detected {len(changes)} change(s)."
                             if changes else "No change since last check.")),
            signals=signals,
            latency_s=self._elapsed(t0),
        )


__all__ = ["SentinelAgent", "MAX_HISTORY"]