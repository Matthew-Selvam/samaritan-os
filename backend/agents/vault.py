"""
Vault.py — VAULT Agent
=======================
Persistent, cross-case entity memory.

The point of memory is *recall*: on a new investigation VAULT returns the
entities it has already seen — from this case **and** from every other case —
so the report can say "known entity, first observed 14 Mar, seen in 3 prior
cases" instead of re-deriving it.

Design:

* **Stable ids** — an entity's id is derived from its type + normalized label,
  so the same person/handle/phone collapses to one record across cases.
* **Per-case + global index** — each case file holds its own entities; a global
  index maps every id to the cases it appeared in. That is what makes
  cross-case recall possible with plain files.
* **first_seen / last_seen / run_count** tracked per entity and per case.
* **Atomic writes** — temp file + ``os.replace``, so a crash mid-write can never
  leave a truncated store.
* **Safe filenames** — case ids are sanitized; hostile ids cannot escape the
  store directory.
* **Bounded** — per-case entity cap and total-store cap, with least-recently-seen
  eviction, so the store cannot grow without limit.
* **Corrupt-file recovery** — an unparseable file is moved aside, not deleted,
  and the run continues.

Runs in the correlation tier so it sees the full aggregated entity set.
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


#: Hard caps. Exceeding either prunes oldest-first.
MAX_ENTITIES_PER_CASE = 5000
MAX_TOTAL_CASES = 500
#: One entity record may not exceed this many characters of label/attribute text.
MAX_LABEL_CHARS = 300
MAX_ATTRIBUTES = 25


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(name: str, fallback: str = "default") -> str:
    """Reduce an arbitrary id to a safe, collision-resistant filename.

    Keeps a readable prefix for operators and appends a digest of the full
    value so two ids that share a prefix never collide.

    Args:
        name: Untrusted case id.
        fallback: Used when ``name`` is empty.
    Returns:
        A filename-safe stem.
    """
    raw = str(name or "").strip()
    if not raw:
        raw = fallback
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12]
    prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:48].strip("._-") or fallback
    return f"{prefix}-{digest}"


def _atomic_write(path: str, payload: dict) -> None:
    """Write JSON atomically (temp file in the same dir + ``os.replace``)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".vault-", suffix=".tmp")
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


def stable_entity_id(entity: dict) -> str:
    """Derive a stable id for an entity so it dedupes across runs and cases.

    An id supplied by the producing agent is preferred (they are already
    normalized); otherwise one is synthesized from type + label.

    Args:
        entity: An entity dict with at least ``label`` or ``id``.
    Returns:
        A stable id string.
    """
    supplied = entity.get("id")
    if supplied:
        return str(supplied)[:200]
    label = str(entity.get("label") or "").strip().lower()
    kind = str(entity.get("type") or "entity").strip().lower()
    if not label:
        # Fall back to a content digest so anonymous entities still dedupe.
        blob = json.dumps(entity, sort_keys=True, default=str)
        return f"{kind}_{hashlib.sha256(blob.encode('utf-8', 'replace')).hexdigest()[:16]}"
    slug = re.sub(r"[^a-z0-9]+", "_", label).strip("_")[:60] or "unnamed"
    return f"{kind}_{slug}"


class VaultAgent(BaseAgent):
    """Memory — persistent entity store across cases and investigations."""

    name = "VAULT"
    role = "Memory Agent"
    icon = "□"
    description = "Persistent entity memory, semantic summarization, intelligence profile management"
    preferred_models = ["gemma2:9b"]
    token_budget = 8192

    @staticmethod
    def _store_dir() -> str:
        default = ("/tmp/signal-os/vault_store" if os.getenv("VERCEL") else
                   os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "vault_store"))
        directory = os.getenv("VAULT_DIR", default)
        os.makedirs(directory, exist_ok=True)
        return directory

    @classmethod
    def _case_path(cls, case_id: str) -> str:
        return os.path.join(cls._store_dir(), f"{_safe_name(case_id)}.json")

    @classmethod
    def _index_path(cls) -> str:
        return os.path.join(cls._store_dir(), "_index.json")

    # ── Store I/O ────────────────────────────────────────────────────────────

    @classmethod
    def _read_json(cls, path: str) -> dict | None:
        """Read a store file, quarantining it when corrupt.

        Args:
            path: File to read.
        Returns:
            The decoded dict, or ``None`` when missing or unusable.
        """
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 — corrupt store must not kill the run
            try:
                os.replace(path, f"{path}.corrupt-{int(time.time())}")
            except OSError:
                pass
            return None

    @classmethod
    def _load_index(cls) -> dict:
        """Load the global entity → cases index."""
        index = cls._read_json(cls._index_path()) or {}
        index.setdefault("entities", {})
        index.setdefault("cases", {})
        return index

    @classmethod
    def _save_index(cls, index: dict) -> None:
        try:
            _atomic_write(cls._index_path(), index)
        except Exception:  # noqa: BLE001 — the index is a cache, not the truth
            pass

    @classmethod
    def _prune(cls, directory: str) -> None:
        """Enforce the total-case cap by dropping the least recently used cases."""
        index = cls._load_index()
        cases = index.get("cases", {})
        if len(cases) <= MAX_TOTAL_CASES:
            return
        ordered = sorted(cases.items(), key=lambda kv: kv[1].get("last_seen", ""))
        for case_id, _ in ordered[:len(cases) - MAX_TOTAL_CASES]:
            path = cls._case_path(case_id)
            try:
                os.unlink(path)
            except OSError:
                pass
            cases.pop(case_id, None)
        cls._save_index(index)

    # ── Agent entry point ────────────────────────────────────────────────────

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        context = context or {}
        case_id = str(context.get("case_id") or "default")
        peer_entities: list[dict] = context.get("peer_entities", []) or []
        target = input_data if isinstance(input_data, str) else str(input_data)
        path = self._case_path(case_id)

        memory = self._read_json(path) or {
            "case_id": case_id, "entities": {}, "first_seen": None, "runs": 0,
        }
        memory.setdefault("entities", {})
        memory.setdefault("runs", 0)
        if memory.get("case_id") != case_id:
            # Filename digest collision or a hand-edited file — realign it.
            memory["case_id"] = case_id

        index = self._load_index()
        index.setdefault("entities", {})
        index.setdefault("cases", {})

        now = _now()
        known_ids = set(memory["entities"].keys())
        new_ids: list[str] = []
        seen_ids: list[str] = []

        for entity in peer_entities:
            if not isinstance(entity, dict):
                continue
            eid = stable_entity_id(entity)
            if not eid:
                continue
            label = str(entity.get("label") or "")[:MAX_LABEL_CHARS]
            record = memory["entities"].get(eid)

            if eid in known_ids:
                seen_ids.append(eid)
                record["last_seen"] = now
                record["run_count"] = int(record.get("run_count", 1)) + 1
                if label and not record.get("label"):
                    record["label"] = label
            else:
                new_ids.append(eid)
                memory["entities"][eid] = {
                    "label": label,
                    "type": entity.get("type"),
                    "first_seen": now,
                    "last_seen": now,
                    "run_count": 1,
                    "attributes": {k: v for k, v in list(entity.items())[:MAX_ATTRIBUTES]
                                   if k not in ("id", "label")},
                }

            # Global index: this is what makes cross-case recall work.
            entry = index["entities"].setdefault(
                eid, {"label": label, "type": entity.get("type"),
                      "first_seen": now, "last_seen": now,
                      "run_count": 1, "cases": []})
            entry["last_seen"] = now
            entry["run_count"] = int(entry.get("run_count", 0)) + 1
            if case_id not in entry["cases"]:
                entry["cases"].append(case_id)
            if label and not entry.get("label"):
                entry["label"] = label

        memory["first_seen"] = memory.get("first_seen") or now
        memory["last_seen"] = now
        memory["runs"] = int(memory.get("runs", 0)) + 1
        memory["target"] = target[:200]

        # Bound the per-case entity map.
        if len(memory["entities"]) > MAX_ENTITIES_PER_CASE:
            ordered = sorted(memory["entities"].items(),
                             key=lambda kv: kv[1].get("last_seen", ""))
            for eid, _ in ordered[:len(memory["entities"]) - MAX_ENTITIES_PER_CASE]:
                memory["entities"].pop(eid, None)

        case_entry = index["cases"].setdefault(
            case_id, {"first_seen": now, "last_seen": now, "runs": 0,
                      "entity_count": 0})
        case_entry["last_seen"] = now
        case_entry["runs"] = memory["runs"]
        case_entry["entity_count"] = len(memory["entities"])

        try:
            _atomic_write(path, memory)
            self._save_index(index)
        except Exception as e:  # noqa: BLE001 — a read-only disk is not fatal
            self.log(f"memory write error: {e}")

        # ── Cross-case recall ────────────────────────────────────────────────
        # Entities this investigation touched that were first seen in ANOTHER
        # case — the actual product of "memory".
        cross_case: list[dict] = []
        for eid in new_ids:
            entry = index["entities"].get(eid) or {}
            other_cases = [c for c in entry.get("cases", []) if c != case_id]
            if other_cases:
                cross_case.append({
                    "id": eid, "label": entry.get("label"), "type": entry.get("type"),
                    "first_seen": entry.get("first_seen"),
                    "last_seen": entry.get("last_seen"),
                    "run_count": entry.get("run_count"),
                    "other_cases": other_cases[:10],
                    "other_case_count": len(other_cases),
                })
        prior_cases = len(index["cases"])
        self.log(f"memory: {len(new_ids)} new, {len(seen_ids)} previously-seen, "
                 f"{len(memory['entities'])} in this case "
                 f"(run #{memory['runs']}, {prior_cases} case(s) indexed)")
        if cross_case:
            self.log(f"cross-case recall: {len(cross_case)} entity(ies) seen in "
                     f"{len({c['id'] for c in cross_case})} prior case(s)")

        signals = [{
            "type": "memory",
            "new_entities": len(new_ids),
            "known_entities": len(seen_ids),
            "total": len(memory["entities"]),
            "run_count": memory["runs"],
            "global_entities": len(index["entities"]),
            "indexed_cases": prior_cases,
            "cross_case_hits": len(cross_case),
            "source": "vault",
        }]
        if seen_ids:
            signals.append({"type": "recurrence", "entity_ids": seen_ids[:20],
                            "note": "entity seen in a prior run of this case",
                            "source": "vault"})
        if cross_case:
            signals.append({"type": "memory_recall", "entities": cross_case[:20],
                            "note": "entity previously observed in another case",
                            "source": "vault"})

        confidence = 0.9 if (peer_entities or prior_cases > 1) else 0.5
        return AgentResult(
            agent=self.name, status="done",
            # Legacy keys kept verbatim for the frontend + stored reports.
            output={
                "total_entities": len(memory["entities"]),
                "new": new_ids,
                "previously_seen": seen_ids,
                "run_count": memory["runs"],
                "store": path,
                # New: cross-case recall + index stats.
                "previously_seen_other_cases": cross_case,
                "global_entity_count": len(index["entities"]),
                "indexed_cases": prior_cases,
                "case_first_seen": memory.get("first_seen"),
            },
            confidence=confidence,
            reasoning=(f"Memory for case {case_id}: {len(new_ids)} new entities, "
                       f"{len(seen_ids)} recurring, {len(memory['entities'])} in this "
                       f"case; {len(cross_case)} recalled from other cases."),
            signals=signals,
            latency_s=self._elapsed(t0),
        )


__all__ = ["VaultAgent", "stable_entity_id", "MAX_ENTITIES_PER_CASE", "MAX_TOTAL_CASES"]