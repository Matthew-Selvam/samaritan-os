"""
store.py — Async Persistence Layer
===================================
Durable storage for investigations, cases, reports, and the audit trail.

Backend selection (``config.STORE_BACKEND`` or auto-detected):

============  ==========================================================
``memory``    In-process dicts. Default when no ``DATABASE_URL`` is set.
``sqlite``    ``aiosqlite`` file at ``config.SQLITE_PATH``. Zero-config
              default for a single-node deployment.
``postgres``  ``psycopg``/``psycopg2`` when ``DATABASE_URL`` is a Postgres URL.
============  ==========================================================

**Nothing here raises on a database failure.** Every public method catches,
records the failure, and — on the first unrecoverable error — permanently
degrades the instance to an in-memory dict store with ``store.degraded =
True``. A dropped Postgres connection turns into "this process stops
persisting", never into a 500 on ``POST /api/investigate``.

Schema is created idempotently on :meth:`Store.start` via
``CREATE TABLE IF NOT EXISTS``, so start() is safe to call repeatedly and
safe to call concurrently from several workers.

Row shape: callers pass and receive plain dicts. Any dict/list value is stored
as a JSON text column and decoded on read, so the API never exposes driver
types.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

import config
from observability import get_logger, get_metrics, redact

__all__ = ["Store", "MemoryStore", "SQLiteStore", "PostgresStore", "get_store"]

log = get_logger("signal-os.store")

#: Top-level record keys promoted to real columns (indexed / filterable).
#: Everything else lives in the ``data`` JSON blob.
_SCALAR_COLUMNS = {
    "investigations": ("inv_id", "case_id", "input", "input_type", "status"),
    "cases": ("case_id", "name", "target"),
    "reports": ("report_id", "inv_id", "case_id", "format"),
}

_ISO = "%Y-%m-%dT%H:%M:%S.%f"


def _now() -> str:
    """Return the current UTC timestamp in ISO 8601."""
    return time.strftime(_ISO, time.gmtime()) + "Z"


def _dumps(value: Any) -> str:
    """JSON-encode a value for a text column, never raising.

    Args:
        value: Any JSON-serialisable structure.

    Returns:
        The JSON string, or ``"null"`` when the value cannot be encoded.
    """
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError, RecursionError):
        return "null"


def _loads(blob: Any, fallback: Any = None) -> Any:
    """JSON-decode a text column, never raising.

    Args:
        blob: Raw column value.
        fallback: Value to return when the column is missing or corrupt.

    Returns:
        The decoded value, or ``fallback``.
    """
    if blob is None or blob == "":
        return fallback
    if isinstance(blob, (dict, list, int, float, bool)):
        return blob
    try:
        return json.loads(blob)
    except (TypeError, ValueError):
        return fallback


# ── Protocol ─────────────────────────────────────────────────────────────────

@runtime_checkable
class Store(Protocol):
    """The persistence contract every backend implements (CONTRACTS §4)."""

    degraded: bool

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def save_investigation(self, rec: dict) -> None: ...
    async def update_investigation(self, inv_id: str, patch: dict) -> None: ...
    async def get_investigation(self, inv_id: str) -> dict | None: ...
    async def list_investigations(
        self, *, case_id: str | None = None, limit: int = 100
    ) -> list[dict]: ...
    async def append_step(self, inv_id: str, step: str) -> None: ...
    async def upsert_case(self, case: dict) -> dict: ...
    async def get_case(self, case_id: str) -> dict | None: ...
    async def list_cases(self, limit: int = 100) -> list[dict]: ...
    async def delete_case(self, case_id: str) -> bool: ...
    async def case_stats(self, case_id: str) -> dict: ...
    async def save_report(self, inv_id: str, md: str, meta: dict) -> str: ...
    async def audit(self, event: dict) -> None: ...


# ── In-memory backend (the degradation target) ───────────────────────────────

class MemoryStore:
    """Dict-backed store. Also the permanent fallback for the SQL backends.

    Bounded and lock-protected: each collection keeps at most
    ``config.STORE_MAX_ROWS`` records, evicting oldest-first, so a long-running
    process cannot grow without limit.

    Attributes:
        degraded: ``True`` when this instance replaced a failed SQL backend.
    """

    def __init__(self, max_rows: int = 5000, degraded: bool = False) -> None:
        """Create an empty store.

        Args:
            max_rows: Per-collection retention cap (oldest evicted first).
            degraded: Whether this store replaced a failed SQL backend.
        """
        self.degraded = degraded
        self.backend = "memory"
        self.max_rows = max(1, int(max_rows))
        self._lock = threading.RLock()
        self._investigations: dict[str, dict] = {}
        self._cases: dict[str, dict] = {}
        self._reports: dict[str, dict] = {}
        self._audit: list[dict] = []

    async def start(self) -> None:
        """No-op: the memory backend needs no initialisation."""
        return None

    async def close(self) -> None:
        """No-op: nothing to close."""
        return None

    # ── investigations ────────────────────────────────────────────────────────

    async def save_investigation(self, rec: dict) -> None:
        """Insert or replace an investigation record.

        Args:
            rec: Must carry ``inv_id``. ``steps`` are normalised to a list.
        """
        try:
            inv_id = str(rec.get("inv_id") or uuid.uuid4().hex[:8])
            record = dict(rec)
            record["inv_id"] = inv_id
            record.setdefault("created_at", _now())
            record["updated_at"] = _now()
            record["steps"] = list(record.get("steps") or [])
            with self._lock:
                self._investigations[inv_id] = record
                self._evict(self._investigations, "created_at")
            get_metrics().incr("store.investigation_saved")
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            log.error("memory save_investigation failed: %s", type(exc).__name__)

    async def update_investigation(self, inv_id: str, patch: dict) -> None:
        """Shallow-merge ``patch`` into an existing investigation.

        Args:
            inv_id: Target investigation id.
            patch: Fields to overwrite. Unknown keys are appended.
        """
        with self._lock:
            rec = self._investigations.get(str(inv_id))
            if rec is None:
                return
            rec.update(patch or {})
            rec["updated_at"] = _now()

    async def get_investigation(self, inv_id: str) -> dict | None:
        """Fetch one investigation, or ``None`` when absent."""
        with self._lock:
            rec = self._investigations.get(str(inv_id))
            return dict(rec) if rec is not None else None

    async def list_investigations(
        self, *, case_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        """List investigations newest-first, optionally filtered by case.

        Args:
            case_id: When set, return only this case's investigations.
            limit: Maximum rows returned.

        Returns:
            A list of investigation dicts.
        """
        with self._lock:
            rows = list(self._investigations.values())
        if case_id:
            rows = [r for r in rows if str(r.get("case_id")) == str(case_id)]
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return [dict(r) for r in rows[: max(0, int(limit))]]

    async def append_step(self, inv_id: str, step: str) -> None:
        """Append one pipeline step to an investigation's log.

        Args:
            inv_id: Target investigation id.
            step: The step text to append.
        """
        with self._lock:
            rec = self._investigations.get(str(inv_id))
            if rec is None:
                return
            steps = rec.setdefault("steps", [])
            if isinstance(steps, list):
                steps.append(str(step))
            rec["updated_at"] = _now()

    # ── cases ─────────────────────────────────────────────────────────────────

    async def upsert_case(self, case: dict) -> dict:
        """Create or update a case, returning the stored record.

        Args:
            case: Must carry ``case_id``; ``name`` defaults to the id. Existing
                fields are preserved unless overwritten.

        Returns:
            The stored case dict (a copy).
        """
        with self._lock:
            case_id = str(case.get("case_id") or uuid.uuid4().hex[:8])
            existing = self._cases.get(case_id, {})
            record = {**existing, **case, "case_id": case_id}
            record.setdefault("name", case_id)
            record.setdefault("created_at", _now())
            record["updated_at"] = _now()
            record["tags"] = list(record.get("tags") or [])
            self._cases[case_id] = record
            self._evict(self._cases, "created_at")
            return dict(record)

    async def get_case(self, case_id: str) -> dict | None:
        """Fetch one case, or ``None`` when absent."""
        with self._lock:
            rec = self._cases.get(str(case_id))
            return dict(rec) if rec is not None else None

    async def list_cases(self, limit: int = 100) -> list[dict]:
        """List cases newest-first.

        Args:
            limit: Maximum rows returned.

        Returns:
            A list of case dicts, each with an ``investigation_count``.
        """
        with self._lock:
            rows = list(self._cases.values())
            invs = list(self._investigations.values())
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        out: list[dict] = []
        for rec in rows[: max(0, int(limit))]:
            item = dict(rec)
            item["investigation_count"] = sum(
                1 for i in invs if str(i.get("case_id")) == item["case_id"]
            )
            out.append(item)
        return out

    async def delete_case(self, case_id: str) -> bool:
        """Delete a case and its investigations.

        Args:
            case_id: The case to remove.

        Returns:
            ``True`` when a case was actually removed.
        """
        cid = str(case_id)
        with self._lock:
            existed = self._cases.pop(cid, None) is not None
            for inv_id in [
                k for k, v in self._investigations.items() if str(v.get("case_id")) == cid
            ]:
                del self._investigations[inv_id]
        return existed

    async def case_stats(self, case_id: str) -> dict:
        """Aggregate counters for one case.

        Args:
            case_id: The case to summarise.

        Returns:
            ``{"case_id", "exists", "investigations", "total_steps",
            "entities", "signals", "confidence", "by_status"}``. Every key is
            present even when the case does not exist.
        """
        cid = str(case_id)
        with self._lock:
            case = self._cases.get(cid)
            invs = [i for i in self._investigations.values() if str(i.get("case_id")) == cid]
            by_status: dict[str, int] = {}
            total_steps = 0
            total_entities = 0
            total_signals = 0
            conf_sum = 0.0
            conf_n = 0
            for inv in invs:
                status = str(inv.get("status") or "unknown")
                by_status[status] = by_status.get(status, 0) + 1
                steps = inv.get("steps")
                total_steps += len(steps) if isinstance(steps, list) else 0
                report = inv.get("report") or {}
                entities = report.get("entities") if isinstance(report, dict) else None
                signals = report.get("signals") if isinstance(report, dict) else None
                total_entities += len(entities) if isinstance(entities, list) else 0
                total_signals += len(signals) if isinstance(signals, list) else 0
                conf = inv.get("confidence")
                if isinstance(conf, (int, float)) and conf > 0:
                    conf_sum += float(conf)
                    conf_n += 1
            return {
                "case_id": cid,
                "exists": case is not None,
                "name": (case or {}).get("name"),
                "investigations": len(invs),
                "total_steps": total_steps,
                "entities": total_entities,
                "signals": total_signals,
                "confidence": round(conf_sum / conf_n, 3) if conf_n else 0.0,
                "by_status": by_status,
            }

    # ── reports ───────────────────────────────────────────────────────────────

    async def save_report(self, inv_id: str, md: str, meta: dict) -> str:
        """Persist a generated report.

        Args:
            inv_id: Owning investigation id.
            md: Markdown body.
            meta: Format/target metadata; must be JSON-serialisable.

        Returns:
            The new ``report_id``.
        """
        report_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._reports[report_id] = {
                "report_id": report_id,
                "inv_id": str(inv_id),
                "case_id": str((meta or {}).get("case_id") or ""),
                "format": str((meta or {}).get("format") or "markdown"),
                "markdown": md or "",
                "meta": dict(meta or {}),
                "created_at": _now(),
            }
            self._evict(self._reports, "created_at")
        get_metrics().incr("store.report_saved")
        return report_id

    async def get_report(self, report_id: str) -> dict | None:
        """Fetch one report by id. Used by the export/report routes.

        Args:
            report_id: The report identifier.

        Returns:
            The report dict, or ``None``.
        """
        with self._lock:
            rec = self._reports.get(str(report_id))
            return dict(rec) if rec is not None else None

    async def get_latest_report(self, inv_id: str) -> dict | None:
        """Fetch the most recent report for an investigation.

        Args:
            inv_id: The investigation identifier.

        Returns:
            The newest report dict, or ``None``.
        """
        with self._lock:
            rows = [r for r in self._reports.values() if str(r.get("inv_id")) == str(inv_id)]
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return dict(rows[0]) if rows else None

    # ── audit ─────────────────────────────────────────────────────────────────

    async def audit(self, event: dict) -> None:
        """Append one audit event, redacted before storage.

        Args:
            event: Arbitrary event payload. Written as-is except that
                secret-shaped values are masked, so the audit trail is itself
                safe to export.
        """
        try:
            record = {
                "event_id": uuid.uuid4().hex[:12],
                "ts": _now(),
                "data": redact(event or {}),
            }
            with self._lock:
                self._audit.append(record)
                if len(self._audit) > self.max_rows:
                    del self._audit[: len(self._audit) - self.max_rows]
        except Exception as exc:  # noqa: BLE001
            log.error("memory audit failed: %s", type(exc).__name__)

    async def list_audit(self, limit: int = 100) -> list[dict]:
        """Return the most recent audit events, newest-first.

        Args:
            limit: Maximum rows returned.

        Returns:
            A list of audit event dicts.
        """
        with self._lock:
            rows = list(self._audit)
        rows.reverse()
        return [dict(r) for r in rows[: max(0, int(limit))]]

    # ── health ────────────────────────────────────────────────────────────────

    def health(self) -> dict:
        """Return a JSON-safe health summary for ``GET /api/health/deep``."""
        with self._lock:
            return {
                "backend": "memory",
                "degraded": self.degraded,
                "investigations": len(self._investigations),
                "cases": len(self._cases),
                "reports": len(self._reports),
                "audit_events": len(self._audit),
            }

    def _evict(self, table: dict, key: str) -> None:
        """Trim ``table`` to ``max_rows``, dropping the oldest rows first.

        Caller must hold :attr:`_lock`.
        """
        overflow = len(table) - self.max_rows
        if overflow <= 0:
            return
        for victim in sorted(table, key=lambda k: str(table[k].get(key) or ""))[:overflow]:
            del table[victim]


# ── SQLite backend ───────────────────────────────────────────────────────────

_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS investigations (
    inv_id      TEXT PRIMARY KEY,
    case_id     TEXT,
    input       TEXT,
    input_type  TEXT,
    status      TEXT,
    data        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_investigations_case
    ON investigations(case_id);
CREATE INDEX IF NOT EXISTS idx_investigations_created
    ON investigations(created_at);

CREATE TABLE IF NOT EXISTS cases (
    case_id     TEXT PRIMARY KEY,
    name        TEXT,
    target      TEXT,
    data        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    report_id   TEXT PRIMARY KEY,
    inv_id      TEXT,
    case_id     TEXT,
    format      TEXT,
    data        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_inv ON reports(inv_id);

CREATE TABLE IF NOT EXISTS audit_log (
    event_id    TEXT PRIMARY KEY,
    ts          TEXT,
    event       TEXT,
    data        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""


class SQLiteStore(MemoryStore):
    """``aiosqlite``-backed store — the zero-config default.

    Subclasses :class:`MemoryStore` so that :meth:`degrade` can hand back a
    fully working in-process store, and so ``isinstance(store, MemoryStore)``
    stays true for the degraded case.

    Args:
        path: Filesystem path to the SQLite file, or ``":memory:"``.
    """

    def __init__(self, path: str, max_rows: int = 5000) -> None:
        """Create a SQLite store. Call :meth:`start` before use.

        Args:
            path: Database file path; parent directories are created.
            max_rows: Retention cap used by the memory fallback.
        """
        super().__init__(max_rows=max_rows, degraded=False)
        self.backend = "sqlite"
        self.path = str(path)
        self._db: Any = None
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        """Open the connection and create the schema idempotently.

        On any failure, logs and degrades to the in-memory store rather than
        propagating — a missing/unwritable path must not stop the app.
        """
        try:
            import aiosqlite  # lazy: keeps the import graph light
        except ImportError as exc:
            log.error("aiosqlite missing (%s) — degrading to memory store", exc)
            self.degrade("aiosqlite not installed")
            return

        try:
            if self.path not in (":memory:", ""):
                parent = Path(self.path).expanduser().resolve().parent
                parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self.path, timeout=10.0)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA busy_timeout=5000")
            for statement in _SCHEMA_SQLITE.split(";"):
                if statement.strip():
                    await self._db.execute(statement)
            await self._db.commit()
            log.info("store ready: sqlite at %s", self.path)
            get_metrics().incr("store.backend_selected", backend="sqlite")
        except Exception as exc:  # noqa: BLE001
            log.error("sqlite start failed (%s) — degrading to memory store",
                      type(exc).__name__)
            await self._safe_close_db()
            self.degrade(f"sqlite unavailable: {type(exc).__name__}")

    async def close(self) -> None:
        """Close the SQLite connection. Never raises."""
        await self._safe_close_db()

    async def _safe_close_db(self) -> None:
        """Close the handle defensively, tolerating an already-broken conn."""
        db, self._db = self._db, None
        if db is None:
            return
        try:
            await db.close()
        except Exception:  # noqa: BLE001
            pass

    def degrade(self, reason: str) -> None:
        """Abandon SQL and serve from memory for the rest of the process.

        In-flight callers keep working because the memory tables are the same
        objects this subclass already inherits.

        Args:
            reason: Human-readable cause, logged (never a secret value).
        """
        if not self.degraded:
            log.error("store degraded to memory: %s", reason)
            get_metrics().incr("store.degraded", backend="sqlite")
        self.degraded = True
        self.backend = "memory"
        self._db = None

    # ── internals ─────────────────────────────────────────────────────────────

    def _usable(self) -> bool:
        """True when a live connection exists and the store is not degraded."""
        return self._db is not None and not self.degraded

    async def _execute(self, sql: str, params: Iterable = (), *, fetch: str | None = None) -> Any:
        """Run one statement, degrading the store if it fails.

        Args:
            sql: Parameterised SQL.
            params: Bound parameters.
            fetch: ``None`` for writes, ``"one"`` or ``"all"`` to return rows.

        Returns:
            Rows (``list[dict]``) for a fetch, otherwise the lastrowid/None.

        Raises:
            Nothing: any driver error degrades the store and returns empty.
        """
        if not self._usable():
            return [] if fetch else None
        try:
            async with self._write_lock:
                await self._db.execute(sql, tuple(params))
                if fetch:
                    cursor = await self._db.execute(sql, tuple(params))
                    rows = await cursor.fetchall() if fetch == "all" else await cursor.fetchone()
                    await cursor.close()
                    return [dict(r) for r in rows] if fetch == "all" else (
                        dict(rows) if rows else None
                    )
                await self._db.commit()
                return self._db.lastrowid
        except Exception as exc:  # noqa: BLE001
            log.error("sqlite error on %s (%s) — degrading", sql.split()[0], type(exc).__name__)
            self.degrade(f"{type(exc).__name__} during {sql.split()[0]}")
            return [] if fetch else None

    @staticmethod
    def _row_to_record(row: dict | None) -> dict | None:
        """Merge a row's JSON blob with its promoted scalar columns."""
        if not row:
            return None
        data = _loads(row.get("data"), {})
        if not isinstance(data, dict):
            data = {}
        merged = dict(data)
        for column in ("inv_id", "case_id", "input", "input_type", "status",
                       "name", "target", "report_id", "inv_id", "format",
                       "created_at", "updated_at"):
            if row.get(column) not in (None, ""):
                merged[column] = row[column]
        return merged

    # ── investigations ────────────────────────────────────────────────────────

    async def save_investigation(self, rec: dict) -> None:
        """Insert or replace an investigation row.

        Args:
            rec: Must carry ``inv_id``; the remaining fields are stored as JSON.
        """
        if not self._usable():
            await super().save_investigation(rec)
            return
        record = dict(rec)
        inv_id = str(record.get("inv_id") or uuid.uuid4().hex[:8])
        record["inv_id"] = inv_id
        record.setdefault("created_at", _now())
        record["updated_at"] = _now()
        record["steps"] = list(record.get("steps") or [])
        await self._execute(
            """INSERT INTO investigations
               (inv_id, case_id, input, input_type, status, data, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(inv_id) DO UPDATE SET
                   case_id=excluded.case_id, input=excluded.input,
                   input_type=excluded.input_type, status=excluded.status,
                   data=excluded.data, updated_at=excluded.updated_at""",
            (inv_id, record.get("case_id"), record.get("input"),
             record.get("input_type"), record.get("status"),
             _dumps(record), record["created_at"], record["updated_at"]),
        )
        if self.degraded:  # the write failed — keep memory in sync anyway
            await super().save_investigation(record)
        get_metrics().incr("store.investigation_saved")

    async def update_investigation(self, inv_id: str, patch: dict) -> None:
        """Shallow-merge ``patch`` into an investigation row.

        Args:
            inv_id: Target investigation id.
            patch: Fields to overwrite.
        """
        if not self._usable():
            await super().update_investigation(inv_id, patch)
            return
        existing = await self.get_investigation(inv_id)
        if existing is None:
            return
        merged = {**existing, **(patch or {}), "inv_id": str(inv_id)}
        merged["updated_at"] = _now()
        await self._execute(
            """UPDATE investigations SET
                   case_id=?, input=?, input_type=?, status=?, data=?, updated_at=?
               WHERE inv_id=?""",
            (merged.get("case_id"), merged.get("input"), merged.get("input_type"),
             merged.get("status"), _dumps(merged), merged["updated_at"], str(inv_id)),
        )

    async def get_investigation(self, inv_id: str) -> dict | None:
        """Fetch one investigation row, decoded."""
        if not self._usable():
            return await super().get_investigation(inv_id)
        row = await self._execute(
            "SELECT * FROM investigations WHERE inv_id=?", (str(inv_id),), fetch="one"
        )
        return self._row_to_record(row)

    async def list_investigations(
        self, *, case_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        """List investigations newest-first, optionally filtered by case."""
        if not self._usable():
            return await super().list_investigations(case_id=case_id, limit=limit)
        if case_id:
            rows = await self._execute(
                """SELECT * FROM investigations WHERE case_id=?
                   ORDER BY created_at DESC LIMIT ?""",
                (str(case_id), max(0, int(limit))), fetch="all",
            )
        else:
            rows = await self._execute(
                "SELECT * FROM investigations ORDER BY created_at DESC LIMIT ?",
                (max(0, int(limit)),), fetch="all",
            )
        return [rec for rec in (self._row_to_record(r) for r in rows) if rec is not None]

    async def append_step(self, inv_id: str, step: str) -> None:
        """Append one step to an investigation's stored ``steps`` list."""
        if not self._usable():
            await super().append_step(inv_id, step)
            return
        record = await self.get_investigation(inv_id)
        if record is None:
            return
        steps = record.get("steps")
        steps = list(steps) if isinstance(steps, list) else []
        steps.append(str(step))
        record["steps"] = steps
        record["updated_at"] = _now()
        await self._execute(
            "UPDATE investigations SET data=?, updated_at=? WHERE inv_id=?",
            (_dumps(record), record["updated_at"], str(inv_id)),
        )

    # ── cases ─────────────────────────────────────────────────────────────────

    async def upsert_case(self, case: dict) -> dict:
        """Create or update a case row, preserving fields not in ``case``."""
        if not self._usable():
            return await super().upsert_case(case)
        case_id = str(case.get("case_id") or uuid.uuid4().hex[:8])
        existing = await self.get_case(case_id) or {}
        record = {**existing, **case, "case_id": case_id}
        record.setdefault("name", case_id)
        record.setdefault("created_at", _now())
        record["updated_at"] = _now()
        record["tags"] = list(record.get("tags") or [])
        await self._execute(
            """INSERT INTO cases
                   (case_id, name, target, data, created_at, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(case_id) DO UPDATE SET
                   name=excluded.name, target=excluded.target,
                   data=excluded.data, updated_at=excluded.updated_at""",
            (case_id, record.get("name"), record.get("target"), _dumps(record),
             record["created_at"], record["updated_at"]),
        )
        if self.degraded:
            return await super().upsert_case(case)
        return record

    async def get_case(self, case_id: str) -> dict | None:
        """Fetch one case row, decoded."""
        if not self._usable():
            return await super().get_case(case_id)
        row = await self._execute(
            "SELECT * FROM cases WHERE case_id=?", (str(case_id),), fetch="one"
        )
        return self._row_to_record(row)

    async def list_cases(self, limit: int = 100) -> list[dict]:
        """List cases newest-first with an ``investigation_count`` each."""
        if not self._usable():
            return await super().list_cases(limit)
        rows = await self._execute(
            "SELECT * FROM cases ORDER BY created_at DESC LIMIT ?",
            (max(0, int(limit)),), fetch="all",
        )
        cases = [rec for rec in (self._row_to_record(r) for r in rows) if rec is not None]
        counts = await self._execute(
            "SELECT case_id, COUNT(*) AS n FROM investigations GROUP BY case_id",
            fetch="all",
        )
        by_case = {str(r.get("case_id")): int(r.get("n") or 0) for r in counts or []}
        for rec in cases:
            rec["investigation_count"] = by_case.get(str(rec.get("case_id")), 0)
        return cases

    async def delete_case(self, case_id: str) -> bool:
        """Delete a case and its investigations in one transaction."""
        if not self._usable():
            return await super().delete_case(case_id)
        cid = str(case_id)
        row = await self._execute(
            "SELECT 1 AS present FROM cases WHERE case_id=?", (cid,), fetch="one"
        )
        if not row:
            return False
        await self._execute("DELETE FROM investigations WHERE case_id=?", (cid,))
        await self._execute("DELETE FROM cases WHERE case_id=?", (cid,))
        return True

    async def case_stats(self, case_id: str) -> dict:
        """Aggregate counters for one case, computed from its investigations."""
        if not self._usable():
            return await super().case_stats(case_id)
        cid = str(case_id)
        case = await self.get_case(cid)
        invs = await self._execute(
            "SELECT * FROM investigations WHERE case_id=?", (cid,), fetch="all"
        )
        by_status: dict[str, int] = {}
        total_steps = total_entities = total_signals = 0
        conf_sum, conf_n = 0.0, 0
        for row in invs:
            rec = self._row_to_record(row) or {}
            status = str(rec.get("status") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
            steps = rec.get("steps")
            total_steps += len(steps) if isinstance(steps, list) else 0
            report = rec.get("report")
            if isinstance(report, dict):
                if isinstance(report.get("entities"), list):
                    total_entities += len(report["entities"])
                if isinstance(report.get("signals"), list):
                    total_signals += len(report["signals"])
            conf = rec.get("confidence")
            if isinstance(conf, (int, float)) and conf > 0:
                conf_sum += float(conf)
                conf_n += 1
        return {
            "case_id": cid,
            "exists": case is not None,
            "name": (case or {}).get("name"),
            "investigations": len(invs or []),
            "total_steps": total_steps,
            "entities": total_entities,
            "signals": total_signals,
            "confidence": round(conf_sum / conf_n, 3) if conf_n else 0.0,
            "by_status": by_status,
        }

    # ── reports ───────────────────────────────────────────────────────────────

    async def save_report(self, inv_id: str, md: str, meta: dict) -> str:
        """Persist a report row and return its ``report_id``."""
        if not self._usable():
            return await super().save_report(inv_id, md, meta)
        report_id = uuid.uuid4().hex[:12]
        record = {
            "report_id": report_id,
            "inv_id": str(inv_id),
            "case_id": str((meta or {}).get("case_id") or ""),
            "format": str((meta or {}).get("format") or "markdown"),
            "markdown": md or "",
            "meta": dict(meta or {}),
            "created_at": _now(),
        }
        await self._execute(
            """INSERT INTO reports
                   (report_id, inv_id, case_id, format, data, created_at)
               VALUES (?,?,?,?,?,?)""",
            (report_id, record["inv_id"], record["case_id"], record["format"],
             _dumps(record), record["created_at"]),
        )
        return report_id

    async def get_report(self, report_id: str) -> dict | None:
        """Fetch one report row by id."""
        if not self._usable():
            return await super().get_report(report_id)
        row = await self._execute(
            "SELECT * FROM reports WHERE report_id=?", (str(report_id),), fetch="one"
        )
        return self._row_to_record(row)

    async def get_latest_report(self, inv_id: str) -> dict | None:
        """Fetch the newest report for an investigation."""
        if not self._usable():
            return await super().get_latest_report(inv_id)
        row = await self._execute(
            "SELECT * FROM reports WHERE inv_id=? ORDER BY created_at DESC LIMIT 1",
            (str(inv_id),), fetch="one",
        )
        return self._row_to_record(row)

    # ── audit ─────────────────────────────────────────────────────────────────

    async def audit(self, event: dict) -> None:
        """Append one redacted audit event row."""
        if not self._usable():
            await super().audit(event)
            return
        safe = redact(event or {})
        await self._execute(
            """INSERT INTO audit_log (event_id, ts, event, data)
               VALUES (?,?,?,?)""",
            (uuid.uuid4().hex[:12], _now(), str((safe or {}).get("event", "")),
             _dumps(safe)),
        )

    async def list_audit(self, limit: int = 100) -> list[dict]:
        """Return the newest audit events."""
        if not self._usable():
            return await super().list_audit(limit)
        rows = await self._execute(
            "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?",
            (max(0, int(limit)),), fetch="all",
        )
        out: list[dict] = []
        for row in rows or []:
            rec = dict(row)
            rec["data"] = _loads(rec.get("data"), {})
            out.append(rec)
        return out

    def health(self) -> dict:
        """Return a health summary including the resolved SQLite path."""
        health = super().health()
        health.update({"backend": "memory" if self.degraded else "sqlite", "path": self.path})
        return health


# ── Postgres backend ─────────────────────────────────────────────────────────

_SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS investigations (
    inv_id      TEXT PRIMARY KEY,
    case_id     TEXT,
    input       TEXT,
    input_type  TEXT,
    status      TEXT,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_investigations_case ON investigations(case_id);
CREATE TABLE IF NOT EXISTS cases (
    case_id     TEXT PRIMARY KEY,
    name        TEXT,
    target      TEXT,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS reports (
    report_id   TEXT PRIMARY KEY,
    inv_id      TEXT,
    case_id     TEXT,
    format      TEXT,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS audit_log (
    event_id    TEXT PRIMARY KEY,
    ts          TIMESTAMPTZ DEFAULT NOW(),
    event       TEXT,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb
);
"""


class PostgresStore(SQLiteStore):
    """Postgres-backed store, used when ``DATABASE_URL`` is a Postgres URL.

    Reuses the SQL logic of :class:`SQLiteStore` but talks through a sync
    ``psycopg2``/``psycopg`` connection executed in a worker thread, so the
    event loop is never blocked on network I/O. ``JSONB`` columns accept the
    same ``_dumps`` JSON text.

    Args:
        dsn: Postgres connection string.
        max_rows: Retention cap used by the memory fallback.
    """

    #: ``%s`` is the psycopg placeholder; SQLite's ``?`` is translated on the way in.
    _SCHEMA = _SCHEMA_POSTGRES

    def __init__(self, dsn: str, max_rows: int = 5000) -> None:
        """Create a Postgres store. Call :meth:`start` before use.

        Args:
            dsn: ``postgresql://user:pass@host:5432/db`` connection string.
            max_rows: Retention cap used by the memory fallback.
        """
        # Bypass SQLiteStore.__init__'s aiosqlite-specific setup: reuse only
        # MemoryStore's tables, locks and bookkeeping.
        MemoryStore.__init__(self, max_rows=max_rows, degraded=False)
        self.backend = "postgres"
        self.dsn = str(dsn)
        self._conn: Any = None

    def _sql(self, sql: str) -> str:
        """Translate SQLite placeholders to Postgres ones."""
        return sql.replace("?", "%s")

    async def start(self) -> None:
        """Connect and create the schema, degrading to memory on failure."""
        try:
            import psycopg2  # lazy; psycopg (v3) is tried first by _import_pg
        except ImportError:
            try:
                import psycopg  # noqa: F401 — presence check only
            except ImportError as exc:
                log.error("no postgres driver installed (%s) — degrading", exc)
                self.degrade("psycopg/psycopg2 not installed")
                return
        try:
            self._conn = await asyncio.to_thread(self._connect_sync)
            await asyncio.to_thread(self._create_schema_sync)
            log.info("store ready: postgres")
            get_metrics().incr("store.backend_selected", backend="postgres")
        except Exception as exc:  # noqa: BLE001
            log.error("postgres start failed (%s) — degrading to memory store",
                      type(exc).__name__)
            self._conn = None
            self.degrade(f"postgres unavailable: {type(exc).__name__}")

    def _connect_sync(self) -> Any:
        """Open a synchronous connection (run in a thread)."""
        try:
            import psycopg  # psycopg 3
            return psycopg.connect(self.dsn, connect_timeout=5)
        except ImportError:
            import psycopg2
            return psycopg2.connect(self.dsn, connect_timeout=5)

    def _create_schema_sync(self) -> None:
        """Execute the idempotent DDL (run in a thread)."""
        with self._conn.cursor() as cur:
            for statement in self._SCHEMA.split(";"):
                if statement.strip():
                    cur.execute(statement)
            self._conn.commit()

    async def close(self) -> None:
        """Close the Postgres connection. Never raises."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await asyncio.to_thread(conn.close)
            except Exception:  # noqa: BLE001
                pass

    def _usable(self) -> bool:
        """True when a live connection exists and the store is not degraded."""
        return self._conn is not None and not self.degraded

    async def _execute(self, sql: str, params: Iterable = (), *, fetch: str | None = None) -> Any:
        """Run one statement on the thread pool, degrading on driver errors."""
        if not self._usable():
            return [] if fetch else None
        args = tuple(params)
        statement = self._sql(sql)
        try:
            return await asyncio.to_thread(self._run_sync, statement, args, fetch)
        except Exception as exc:  # noqa: BLE001
            log.error("postgres error on %s (%s) — degrading",
                      statement.split()[0], type(exc).__name__)
            try:
                await asyncio.to_thread(self._conn.rollback)
            except Exception:  # noqa: BLE001
                pass
            self.degrade(f"{type(exc).__name__} during {statement.split()[0]}")
            return [] if fetch else None

    def _run_sync(self, statement: str, args: tuple, fetch: str | None) -> Any:
        """Execute synchronously and commit; returns rows when fetching."""
        with self._conn.cursor() as cur:
            cur.execute(statement, args)
            if fetch == "all":
                columns = [d[0] for d in (cur.description or [])]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            elif fetch == "one":
                row = cur.fetchone()
                columns = [d[0] for d in (cur.description or [])]
                rows = dict(zip(columns, row)) if row else None
            else:
                rows = None
            self._conn.commit()
        if fetch:
            # JSONB comes back as an already-decoded dict; normalise anyway.
            if isinstance(rows, list):
                for r in rows:
                    if isinstance(r.get("data"), str):
                        r["data"] = _loads(r["data"], {})
            elif isinstance(rows, dict) and isinstance(rows.get("data"), str):
                rows["data"] = _loads(rows["data"], {})
            # Timestamps are datetime objects, not ISO strings.
            for r in (rows if isinstance(rows, list) else [rows] if rows else []):
                for col in ("created_at", "updated_at", "ts"):
                    if hasattr(r.get(col), "isoformat"):
                        r[col] = r[col].isoformat()
        return rows

    def health(self) -> dict:
        """Return a health summary; the DSN itself is never included (it holds
        a password)."""
        health = MemoryStore.health(self)
        health["backend"] = "memory" if self.degraded else "postgres"
        return health


# ── Factory ──────────────────────────────────────────────────────────────────

def _is_postgres_url(url: str) -> bool:
    """True when a DSN names Postgres rather than SQLite."""
    return str(url or "").startswith(("postgres://", "postgresql://", "postgresql+"))


def build_store(
    backend: str | None = None,
    *,
    sqlite_path: str | None = None,
    database_url: str | None = None,
) -> Store:
    """Construct (but do not start) the configured store.

    Selection order: explicit ``backend`` → auto-detect from ``DATABASE_URL``
    → ``sqlite``.

    Args:
        backend: ``"memory"``, ``"sqlite"``, ``"postgres"``, or ``None``.
        sqlite_path: Override for the SQLite file location.
        database_url: Override for the connection DSN.

    Returns:
        An unstarted :class:`Store`.
    """
    chosen = (backend or getattr(config, "STORE_BACKEND", "auto") or "auto").lower()
    dsn = database_url if database_url is not None else getattr(config, "DATABASE_URL", "")
    path = sqlite_path or getattr(config, "SQLITE_PATH", None)

    if chosen == "auto":
        if _is_postgres_url(dsn):
            chosen = "postgres"
        elif str(dsn or "").startswith("sqlite://"):
            chosen = "sqlite"
        else:
            chosen = "sqlite"  # zero-config default
    if chosen == "postgres" and not _is_postgres_url(dsn):
        log.warning("STORE_BACKEND=postgres but DATABASE_URL is not a postgres URL — using sqlite")
        chosen = "sqlite"
    if chosen == "sqlite" and not path:
        chosen = "memory"

    if chosen == "memory":
        return MemoryStore(max_rows=getattr(config, "STORE_MAX_ROWS", 5000))
    if chosen == "postgres":
        return PostgresStore(dsn, max_rows=getattr(config, "STORE_MAX_ROWS", 5000))
    return SQLiteStore(path, max_rows=getattr(config, "STORE_MAX_ROWS", 5000))


_store: Store | None = None
_store_lock = threading.Lock()


async def get_store() -> Store:
    """Return the process-wide store, starting it on first use.

    Returns:
        A started, ready :class:`Store`. The same instance is returned on
        every call; if ``start()`` failed the instance is already degraded and
        still fully functional in memory.
    """
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = build_store()
                await _store.start()
    return _store


async def set_store(store: Store) -> None:
    """Install a specific store (tests / custom deployments).

    Args:
        store: A started or startable :class:`Store` implementation.
    """
    global _store
    _store = store


async def reset_store() -> None:
    """Close and discard the singleton. Intended for tests."""
    global _store
    with _store_lock:
        store, _store = _store, None
    if store is not None:
        try:
            await store.close()
        except Exception:  # noqa: BLE001
            pass
