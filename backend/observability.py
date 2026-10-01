"""
observability.py — Logging, Metrics, and Secret Redaction
=========================================================
Single observability entry point for Signal-OS. Every module logs through
:func:`get_logger`; nothing calls ``print()`` and nothing uses the root logger
directly.

Three concerns live here:

* **Logging** — one configured stream handler, level/format from ``config``,
  plus a :class:`RedactingFilter` that scrubs secrets out of every formatted
  record so an accidental ``logger.debug(payload)`` can never leak a key.
* **Redaction** — :func:`redact` walks arbitrarily nested structures and masks
  the *values* of any key in :data:`REDACT_KEYS`, at any depth, including
  inside lists/tuples/sets and inside objects with a ``__dict__``.
* **Metrics** — an in-process, thread-safe counter/timer/gauge registry that
  ``GET /api/metrics`` can serve without any external exporter.

Privacy rules baked in here:
  - Prompt and response *bodies* are never logged. Log token counts and model
    names only (see :func:`log_llm_call`).
  - Secret-shaped key values are replaced with :data:`REDACTED` before they can
    reach a handler, a log file, or a metrics label.

Everything degrades gracefully: an unusable config value falls back to a
sane default rather than raising at import time.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sys
import threading
import time
from collections import OrderedDict
from typing import Any, Iterable, Mapping

import config

__all__ = [
    "REDACT_KEYS",
    "REDACTED",
    "Metrics",
    "RedactingFilter",
    "audit_log",
    "get_logger",
    "get_metrics",
    "log_llm_call",
    "redact",
]


# ── Redaction ────────────────────────────────────────────────────────────────

#: The value substituted for every masked secret. Fixed string so logs stay
#: greppable and never leak a hint of the original length.
REDACTED = "***REDACTED***"

#: Key names whose *values* must never be logged or exported. Matched after
#: camelCase/underscore/hyphen normalisation, so ``X-API-Key``,
#: ``x_api_key`` and ``apiKey`` all hit ``api_key``.
REDACT_KEYS: frozenset[str] = frozenset({
    # ── required by docs/CONTRACTS.md §7 ──
    "apikey",
    "api_key",
    "password",
    "secret",
    "token",
    "authorization",
    "cookie",
    "ssh_key",
    "sessionid",
    "session_id",
    # ── common aliases of the above ──
    "passwd",
    "pwd",
    "api_token",
    "auth_token",
    "access_token",
    "refresh_token",
    "bearer",
    "credentials",
    "secret_key",
    "client_secret",
    "private_key",
    "x-api-key",
    "set-cookie",
    "jwt",
    "otp",
    "x-auth-token",
})

# Split ``fooBarBaz`` / ``foo-bar_baz`` into lowercase word tokens so that a
# redact key can be matched as a contiguous run of words.
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_KEY_SPLIT = re.compile(r"[^a-z0-9]+")

# Inline ``api_key=hunter2`` / ``"token": "abc"`` inside a free-form string.
# The value is matched by one of three alternatives (double-quoted,
# single-quoted, or bare) rather than by a backreference to an *optional*
# group — Python's `re` fails a backreference whose group never participated,
# which would silently skip every unquoted secret.
_INLINE_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|apikey|password|passwd|pwd|secret|token|"
    r"authorization|cookie|session[_-]?id)\b(\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s\"',;)\]}]+)"
)

# ``Authorization: Bearer eyJhbG...`` is the one shape where the secret is a
# *pair* of words. The generic pattern above would mask only "Bearer" and leave
# the JWT in the clear, so this one consumes to the end of the quoted string or
# line.
_INLINE_AUTH = re.compile(
    r"(?i)\b(authorization|proxy[_-]?authorization)\b(\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\r\n]+)"
)

_MAX_STRING_SCAN = 4096  # don't regex-scan megabyte payloads


def _key_tokens(key: Any) -> list[str]:
    """Split a key name into normalised lowercase word tokens.

    Args:
        key: Any object; coerced with ``str()``.

    Returns:
        List of lowercase alphanumeric word tokens (possibly empty).
    """
    try:
        text = str(key)
    except Exception:  # noqa: BLE001 — a hostile __str__ must not break redaction
        return []
    return [t for t in _KEY_SPLIT.split(_CAMEL_SPLIT.sub("_", text).lower()) if t]


def _precompute_tokens() -> list[list[str]]:
    """Tokenise every :data:`REDACT_KEYS` entry once, at import time."""
    out: list[list[str]] = []
    for rk in REDACT_KEYS:
        toks = _key_tokens(rk)
        if toks:
            out.append(toks)
            # Also allow a separator-insensitive match ("apiKey" vs "api_key").
            joined = "".join(toks)
            if joined != toks:
                out.append([joined])
    return out


_REDACT_TOKENS: list[list[str]] = _precompute_tokens()


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    """True when ``needle`` appears as a contiguous run inside ``haystack``."""
    n, m = len(haystack), len(needle)
    if m == 0 or m > n:
        return False
    first = needle[0]
    for i in range(n - m + 1):
        if haystack[i] == first and haystack[i:i + m] == needle:
            return True
    return False


def is_secret_key(key: Any) -> bool:
    """Report whether a dict key is secret-shaped and its value must be masked.

    Matching is separator- and case-insensitive and tolerates compound keys
    such as ``github_token`` or ``Set-Cookie``, while deliberately *not*
    matching unrelated keys that merely contain a secret word as a prefix
    (``tokens_used`` stays loggable — it is a metric, not a token).

    Args:
        key: Candidate mapping key.

    Returns:
        ``True`` when the associated value must be redacted.
    """
    tokens = _key_tokens(key)
    if not tokens:
        return False
    for needle in _REDACT_TOKENS:
        if _contains_run(tokens, needle):
            return True
    return False


def _scrub_string(text: str) -> str:
    """Mask ``key=value`` style secrets embedded in a free-form string."""
    if len(text) > _MAX_STRING_SCAN:
        return text
    if "=" not in text and ":" not in text:
        return text
    try:
        scrubbed = _INLINE_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
        # Run the auth pass second so a "Bearer <jwt>" tail is not left behind.
        return _INLINE_AUTH.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", scrubbed)
    except Exception:  # noqa: BLE001 — never fail a log call on a bad regex run
        return text


def _is_key_value_pair(value: tuple) -> bool:
    """True when a tuple looks like a literal ``(key, value)`` mapping entry.

    Only used for containers where the mapping relationship is positional
    (sets, and tuples used as dict items). A bare ``('apiKey', 'x')`` carries
    the same secret as ``{'apiKey': 'x'}`` and must be masked the same way.

    Args:
        value: A tuple of length ≥ 2 whose first element is a string.

    Returns:
        ``True`` when element 0 is a secret-shaped key name.
    """
    try:
        return len(value) >= 2 and isinstance(value[0], str) and is_secret_key(value[0])
    except Exception:  # noqa: BLE001
        return False


def _redact_mapping(obj: Mapping, seen: set[int]) -> dict:
    out: dict[Any, Any] = {}
    for key, value in obj.items():
        try:
            is_secret = is_secret_key(key)
        except Exception:  # noqa: BLE001
            is_secret = False
        out[key] = REDACTED if is_secret else _redact_value(value, seen)
    return out


def _redact_value(value: Any, seen: set[int]) -> Any:
    """Recursively redact one value, guarding against reference cycles."""
    try:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return _scrub_string(value)
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"

        ident = id(value)
        if ident in seen:            # cycle — emit a marker, not a recursion error
            return "<recursion>"
        seen.add(ident)
        try:
            if isinstance(value, Mapping):
                return _redact_mapping(value, seen)
            if isinstance(value, (list, tuple)):
                items = [_redact_value(v, seen) for v in value]
                if isinstance(value, tuple) and _is_key_value_pair(value):
                    # A (key, value) tuple — e.g. inside a set, where the
                    # pair-ness is positional rather than structural. Mask the
                    # *value* (elements 1..n), keeping the key name visible so
                    # the reader can still see which field was secret.
                    return type(value)([items[0], REDACTED])
                return type(value)(items) if isinstance(value, tuple) else items
            if isinstance(value, (set, frozenset)):
                # Redact members *first*, then rebuild. A set member that
                # redacts to an unhashable/colliding value (or a REDACTED
                # collision) would raise, so fall back to a sorted list, which
                # is still a faithful, JSON-safe rendering of the same data.
                members = [_redact_value(v, seen) for v in value]
                try:
                    return type(value)(members)
                except TypeError:
                    return sorted(members, key=lambda m: repr(m))

            # Objects with a __dict__ (e.g. an AgentResult) are flattened to
            # their public attribute mapping so nested secrets are still masked.
            attrs = getattr(value, "__dict__", None)
            if isinstance(attrs, Mapping):
                red = _redact_mapping(attrs, seen)
                slots = getattr(type(value), "__slots__", None)
                if slots:
                    for slot in slots:
                        try:
                            red[slot] = _redact_value(getattr(value, slot), seen)
                        except AttributeError:
                            continue
                return red

            to_dict = getattr(value, "to_dict", None)
            if callable(to_dict):
                return _redact_value(to_dict(), seen)

            # Last resort: strings of unknown types still get scrubbed.
            return _scrub_string(str(value))
        finally:
            seen.discard(ident)
    except Exception:  # noqa: BLE001 — redaction must never raise
        return "<unloggable>"


def redact(obj: Any) -> Any:
    """Deep-copy ``obj`` with every secret-shaped value masked.

    Recurses through dicts, lists, tuples, sets and plain objects at any depth
    and replaces the value of any key matching :data:`REDACT_KEYS` with
    :data:`REDACTED`. Cycle-safe and exception-proof: an object that cannot be
    introspected becomes a short placeholder rather than propagating an error.

    Args:
        obj: Any value — scalars pass through, containers are walked.

    Returns:
        A redacted structure of the same broad shape as ``obj``. Safe to
        ``json.dumps`` and safe to log.
    """
    try:
        return _redact_value(obj, set())
    except Exception:  # noqa: BLE001
        return "<unloggable>"


# ── Logging ──────────────────────────────────────────────────────────────────

_CONFIGURED = False
_CONFIG_LOCK = threading.Lock()


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs secrets from messages and arguments.

    Applied to the Signal-OS stream handler so that *every* record — including
    one logged by third-party code through the same handler — has inline
    ``key=value`` secrets masked.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        try:
            if isinstance(record.msg, str):
                record.msg = _scrub_string(record.msg)
            if record.args:
                if isinstance(record.args, Mapping):
                    record.args = {
                        k: (REDACTED if is_secret_key(k) else v)
                        for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        _scrub_string(a) if isinstance(a, str) else a
                        for a in record.args
                    )
        except Exception:  # noqa: BLE001 — a filter must never drop the record
            pass
        return True


def _resolve_level(level: Any) -> int:
    """Coerce a configured level name/number into a logging level int."""
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.INFO)


def configure_logging(force: bool = False) -> None:
    """Install the Signal-OS stream handler exactly once.

    Idempotent by default. A pre-existing ``logging.basicConfig`` (the legacy
    ``main.py`` calls it at import) is respected unless ``force`` is set — we
    never add a second handler to a logger that already has one.

    Args:
        force: Reconfigure even if handlers were already installed.
    """
    global _CONFIGURED
    with _CONFIG_LOCK:
        if _CONFIGURED and not force:
            return
        root = logging.getLogger()
        if not force and root.handlers:
            # Someone already configured logging (legacy main.py) — respect it
            # but still make sure our redacting filter is present.
            for handler in root.handlers:
                if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                    handler.addFilter(RedactingFilter())
            _CONFIGURED = True
            return

        level = _resolve_level(getattr(config, "LOG_LEVEL", "INFO"))
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            getattr(config, "LOG_FORMAT", "%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        ))
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
        root.setLevel(level)
        _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger for a module.

    Args:
        name: Usually ``__name__``; dotted names create child loggers that
            inherit the single root handler.

    Returns:
        A :class:`logging.Logger` with the Signal-OS handler and redaction
        filter already installed.
    """
    configure_logging()
    return logging.getLogger(name or "signal-os")


def log_llm_call(
    logger: logging.Logger,
    *,
    model: str,
    provider: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_s: float = 0.0,
    error: str | None = None,
) -> None:
    """Log an LLM call by *shape* only — never the prompt or response body.

    Args:
        logger: Destination logger.
        model: Model identifier (safe to log).
        provider: Provider name, when known.
        prompt_tokens: Prompt token count.
        completion_tokens: Completion token count.
        latency_s: Wall-clock duration of the call.
        error: Error message, if the call failed.
    """
    try:
        logger.info(
            "llm call model=%s provider=%s prompt_tokens=%d completion_tokens=%d "
            "latency_s=%.2f%s",
            model, provider or "-", int(prompt_tokens), int(completion_tokens),
            float(latency_s), f" error={error}" if error else "",
        )
    except Exception:  # noqa: BLE001
        pass


def audit_log(event: str, **fields: Any) -> None:
    """Emit a structured, redacted audit line when ``config.AUDIT_LOG`` is on.

    Args:
        event: Short event name, e.g. ``"investigation.started"``.
        **fields: Arbitrary context; redacted before being written.
    """
    if not getattr(config, "AUDIT_LOG", True):
        return
    try:
        payload = ", ".join(f"{k}={redact(v)!r}" for k, v in fields.items())
        get_logger("signal-os.audit").info("AUDIT %s %s", event, payload)
    except Exception:  # noqa: BLE001 — auditing must never break a request
        pass


# ── Metrics ──────────────────────────────────────────────────────────────────

_MAX_SERIES = 2000  # cap distinct series so a bad tag can't grow unbounded


def _series_key(name: str, tags: Mapping[str, Any] | None) -> str:
    """Build a stable ``name{k=v,...}`` series key from a name plus tags."""
    if not tags:
        return name
    try:
        parts = ",".join(f"{k}={redact(tags[k])}" for k in sorted(tags))
    except Exception:  # noqa: BLE001
        parts = "tags=unavailable"
    return f"{name}{{{parts}}}"


class Metrics:
    """Thread-safe in-process metric registry.

    Holds three series families:

    * counters — monotonically increasing values (:meth:`incr`)
    * timings — count/total/min/max/avg of durations in seconds (:meth:`timing`)
    * gauges — last value wins (:meth:`gauge`)

    No external exporter is required; :meth:`snapshot` returns everything for
    ``GET /api/metrics``. Series count is capped at :data:`_MAX_SERIES` so an
    accidentally high-cardinality tag cannot exhaust memory.
    """

    def __init__(self, enabled: bool = True, max_series: int = _MAX_SERIES) -> None:
        """Create a registry.

        Args:
            enabled: When ``False``, all recording calls become no-ops.
            max_series: Maximum distinct series per family.
        """
        self.enabled = bool(enabled)
        self.max_series = int(max_series)
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        self._timings: dict[str, dict[str, float]] = {}
        self._gauges: dict[str, float] = {}
        self._started = time.time()

    # ── recording ────────────────────────────────────────────────────────────

    def incr(self, name: str, value: int = 1, **tags: Any) -> None:
        """Increment a counter.

        Args:
            name: Series name, e.g. ``"agent_runs"``.
            value: Amount to add (default 1).
            **tags: Low-cardinality dimensions; values are redacted.
        """
        if not self.enabled:
            return
        key = _series_key(name, tags)
        try:
            with self._lock:
                if key not in self._counters and len(self._counters) >= self.max_series:
                    return
                self._counters[key] = self._counters.get(key, 0.0) + float(value)
        except Exception:  # noqa: BLE001 — metrics must never break a caller
            pass

    def timing(self, name: str, seconds: float, **tags: Any) -> None:
        """Record a duration sample.

        Args:
            name: Series name, e.g. ``"agent_latency"``.
            seconds: Duration in seconds; non-positive or non-finite values
                are ignored.
            **tags: Low-cardinality dimensions.
        """
        if not self.enabled:
            return
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            return
        if value < 0 or value != value or value in (float("inf"), float("-inf")):
            return
        key = _series_key(name, tags)
        try:
            with self._lock:
                entry = self._timings.get(key)
                if entry is None:
                    if len(self._timings) >= self.max_series:
                        return
                    entry = {"count": 0.0, "total": 0.0, "min": value, "max": value}
                    self._timings[key] = entry
                entry["count"] += 1.0
                entry["total"] += value
                entry["min"] = min(entry["min"], value)
                entry["max"] = max(entry["max"], value)
        except Exception:  # noqa: BLE001
            pass

    def gauge(self, name: str, value: float, **tags: Any) -> None:
        """Set a gauge to the latest observed value.

        Args:
            name: Series name, e.g. ``"investigations_active"``.
            value: Current value.
            **tags: Low-cardinality dimensions.
        """
        if not self.enabled:
            return
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        key = _series_key(name, tags)
        try:
            with self._lock:
                if key not in self._gauges and len(self._gauges) >= self.max_series:
                    return
                self._gauges[key] = numeric
        except Exception:  # noqa: BLE001
            pass

    # ── helpers ──────────────────────────────────────────────────────────────

    def timer(self, name: str, **tags: Any) -> "_Timer":
        """Return a context manager that records elapsed time into ``name``.

        Args:
            name: Timing series name.
            **tags: Low-cardinality dimensions.

        Returns:
            A context manager usable as ``with metrics.timer("x"): ...``.
        """
        return _Timer(self, name, tags)

    def reset(self) -> None:
        """Drop every series. Intended for tests."""
        with self._lock:
            self._counters.clear()
            self._timings.clear()
            self._gauges.clear()

    # ── export ───────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Return a JSON-safe view of every recorded series.

        Returns:
            ``{"counters": {...}, "timings": {...}, "gauges": {...},
            "totals": {...}, "uptime_s": float, "enabled": bool}``. Timing
            entries additionally carry ``avg_s``.
        """
        try:
            with self._lock:
                counters = dict(self._counters)
                gauges = dict(self._gauges)
                timings: dict[str, dict[str, float]] = {}
                for key, entry in self._timings.items():
                    count = entry["count"] or 1.0
                    timings[key] = {
                        "count": entry["count"],
                        "total_s": round(entry["total"], 6),
                        "avg_s": round(entry["total"] / count, 6),
                        "min_s": round(entry["min"], 6),
                        "max_s": round(entry["max"], 6),
                    }
                return {
                    "counters": counters,
                    "timings": timings,
                    "gauges": gauges,
                    "totals": {
                        "counters": len(counters),
                        "timings": len(timings),
                        "gauges": len(gauges),
                    },
                    "uptime_s": round(time.time() - self._started, 3),
                    "enabled": self.enabled,
                }
        except Exception:  # noqa: BLE001
            return {
                "counters": {}, "timings": {}, "gauges": {},
                "totals": {"counters": 0, "timings": 0, "gauges": 0},
                "uptime_s": 0.0, "enabled": self.enabled,
            }

    def fingerprint(self, name: str) -> str:
        """Return a short stable hash of a series name (for log correlation).

        Args:
            name: Series or value name.

        Returns:
            8-character hex digest.
        """
        return hashlib.sha256(str(name).encode("utf-8", "replace")).hexdigest()[:8]


class _Timer:
    """Context manager that feeds :meth:`Metrics.timing` on exit."""

    __slots__ = ("_metrics", "_name", "_tags", "_t0")

    def __init__(self, metrics: Metrics, name: str, tags: Mapping[str, Any]) -> None:
        self._metrics = metrics
        self._name = name
        self._tags = dict(tags)
        self._t0 = 0.0

    def __enter__(self) -> "_Timer":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self._metrics.timing(self._name, time.perf_counter() - self._t0, **self._tags)
        return False  # never swallow an exception


_METRICS: Metrics | None = None
_METRICS_LOCK = threading.Lock()


def get_metrics() -> Metrics:
    """Return the process-wide :class:`Metrics` singleton.

    Honours ``config.METRICS_ENABLED`` at first construction; the returned
    object stays mutable so ops can flip ``metrics.enabled`` at runtime.

    Returns:
        The shared :class:`Metrics` instance.
    """
    global _METRICS
    if _METRICS is None:
        with _METRICS_LOCK:
            if _METRICS is None:
                _METRICS = Metrics(enabled=getattr(config, "METRICS_ENABLED", True))
    return _METRICS
