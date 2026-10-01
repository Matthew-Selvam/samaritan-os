"""
circuit.py — Circuit Breaker and Retry Helpers
===============================================
Resilience primitives shared by every outbound dependency (LLM providers,
connectors, Postgres, Redis). Two exports:

* :class:`CircuitBreaker` — closed → open → half_open state machine that stops
  hammering a dead dependency so the rest of the platform keeps serving.
* :func:`retry` — async retry with exponential backoff and jitter, optionally
  guarded by a breaker.

Both are dependency-free (stdlib only) and thread-safe. A breaker never
raises from its bookkeeping methods, so telemetry can't turn into an outage.

State machine::

    closed --(failures >= fail_threshold)--> open
    open --(now - opened_at >= reset_after)--> half_open
    half_open --(success)--> closed
    half_open --(failure)--> open   (timer restarts)
"""
from __future__ import annotations

import asyncio
import random
import threading
import time
from typing import Any, Awaitable, Callable

import config
from observability import get_logger, get_metrics

__all__ = ["CircuitOpen", "CircuitBreaker", "retry", "get_breaker", "reset_breakers"]


log = get_logger("signal-os.circuit")

#: Shared registry so a caller can inspect/reset every breaker by name.
_BREAKERS: dict[str, "CircuitBreaker"] = {}
_REGISTRY_LOCK = threading.Lock()


class CircuitOpen(Exception):
    """Raised when a call is rejected because its circuit is open.

    Attributes:
        breaker: The :class:`CircuitBreaker` that rejected the call, so a
            caller can inspect ``reset_after`` / ``failures``.
    """

    def __init__(self, message: str, breaker: "CircuitBreaker | None" = None) -> None:
        super().__init__(message)
        self.breaker = breaker


class CircuitBreaker:
    """A three-state circuit breaker guarding a single dependency.

    Args:
        name: Identifier used in logs and in :func:`get_breaker` lookups.
        fail_threshold: Consecutive failures that trip the breaker closed →
            open. Must be ≥ 1.
        reset_after: Seconds the breaker stays open before allowing a probe
            call through to half_open.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        name: str,
        fail_threshold: int = 5,
        reset_after: float = 30.0,
    ) -> None:
        self.name = str(name or "anonymous")
        self.fail_threshold = max(1, int(fail_threshold or 1))
        self.reset_after = max(0.0, float(reset_after or 0.0))

        self._lock = threading.RLock()
        self._state: str = self.CLOSED
        self._failures = 0
        self._successes = 0
        self._opened_at: float = 0.0
        self._total_calls = 0
        self._total_failures = 0
        self._total_rejections = 0
        # Guards the single half_open probe: while it is in flight the breaker
        # keeps rejecting, so a slow dependency is never stampeded.
        self._probe_in_flight = False

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        """Current state, after lazily applying any open → half_open expiry.

        Reading the state is enough to observe the transition; the probe
        itself only happens on the next :meth:`allow`.
        """
        with self._lock:
            self._maybe_half_open()
            return self._state

    @property
    def failures(self) -> int:
        """Consecutive failures in the current closed run (0 once open)."""
        with self._lock:
            return self._failures

    def _maybe_half_open(self) -> None:
        """Transition open → half_open once ``reset_after`` has elapsed.

        Caller must hold :attr:`_lock`.
        """
        if self._state == self.OPEN and (time.monotonic() - self._opened_at) >= self.reset_after:
            self._state = self.HALF_OPEN
            self._failures = 0
            log.info("circuit %s → half_open (probe allowed)", self.name)

    def allow(self) -> bool:
        """Test whether a call may proceed right now.

        Returns:
            ``True`` when the caller may make the call. A half-open circuit
            admits **one** probe at a time; while that probe is in flight the
            circuit is treated as open again so a slow dependency can't be
            stampeded by a thundering herd.
        """
        with self._lock:
            self._total_calls += 1
            if self._state == self.CLOSED:
                return True

            self._maybe_half_open()

            if self._state == self.OPEN:
                self._total_rejections += 1
                return False

            if self._state == self.HALF_OPEN:
                if self._probe_in_flight:
                    self._total_rejections += 1
                    return False
                self._probe_in_flight = True
                return True

            return True

    # ── outcome recording ────────────────────────────────────────────────────

    def record_success(self) -> None:
        """Record a successful call.

        From half_open this closes the circuit; from closed it resets the
        consecutive-failure counter. Emits a ``circuit.closed`` metric.
        """
        with self._lock:
            self._successes += 1
            if self._state == self.HALF_OPEN:
                self._state = self.CLOSED
                self._opened_at = 0.0
                self._failures = 0
                self._probe_in_flight = False
                log.info("circuit %s recovered → closed", self.name)
                get_metrics().incr("circuit.closed", breaker=self.name)
            elif self._state == self.CLOSED:
                self._failures = 0
            self._probe_in_flight = False

    def record_failure(self, exc: BaseException | None = None) -> None:
        """Record a failed call, tripping the breaker when the threshold is hit.

        Args:
            exc: The exception that caused the failure. Never logged with a
                traceback (callers own that) — only the type name is recorded
                to avoid leaking arguments that may contain secrets.
        """
        with self._lock:
            self._total_failures += 1
            self._probe_in_flight = False

            if self._state == self.HALF_OPEN:
                self._trip(exc)
                get_metrics().incr("circuit.open", breaker=self.name)
                return

            self._failures += 1
            if self._failures >= self.fail_threshold:
                self._trip(exc)
                get_metrics().incr("circuit.open", breaker=self.name)

    def _trip(self, exc: BaseException | None = None) -> None:
        """Move to the open state and (re)start the reset timer.

        Only the exception *type* is logged: an exception message can embed
        request payloads that hold credentials.

        Caller must hold :attr:`_lock`.

        Args:
            exc: Failure that triggered the trip, if available.
        """
        self._state = self.OPEN
        self._opened_at = time.monotonic()
        self._probe_in_flight = False
        log.warning(
            "circuit %s opened after %d failure(s) (last: %s) — rejecting for %.1fs",
            self.name, self._failures, type(exc).__name__ if exc else "unknown",
            self.reset_after,
        )

    def reset(self) -> None:
        """Force the breaker closed and clear all counters."""
        with self._lock:
            self._state = self.CLOSED
            self._failures = 0
            self._opened_at = 0.0
            self._probe_in_flight = False

    def stats(self) -> dict:
        """Return a JSON-safe snapshot of the breaker.

        Returns:
            ``{"name", "state", "failures", "fail_threshold", "reset_after",
            "successes", "total_calls", "total_failures", "rejections",
            "retry_in"}`` — consumed by ``GET /api/health/deep``.
        """
        with self._lock:
            self._maybe_half_open()
            return {
                "name": self.name,
                "state": self._state,
                "failures": self._failures,
                "fail_threshold": self.fail_threshold,
                "reset_after": self.reset_after,
                "successes": self._successes,
                "total_calls": self._total_calls,
                "total_failures": self._total_failures,
                "rejections": self._total_rejections,
            }

    def __repr__(self) -> str:
        return f"<CircuitBreaker {self.name} state={self._state} failures={self._failures}>"


def _resolve_delay(
    attempt: int,
    backoff: float,
    *,
    base_delay: float = 0.1,
    max_delay: float = 30.0,
) -> float:
    """Compute the sleep duration before a retry attempt.

    Full "equal jitter" backoff: a random value in
    ``[delay/2, delay]`` around an exponentially growing ``delay``, capped at
    ``max_delay``. Jitter is what stops N agents from retrying in lockstep
    against the same recovering dependency.

    Args:
        attempt: 0-based index of the attempt that just failed.
        backoff: Base multiplier in seconds.
        base_delay: Floor for the first attempt.
        max_delay: Upper bound for any single sleep.

    Returns:
        Seconds to sleep.
    """
    try:
        growth = min(float(base_delay) * (2.0 ** max(0, attempt)) * float(backoff), max_delay)
    except (TypeError, ValueError, OverflowError):
        return float(base_delay)
    return random.uniform(growth / 2.0, growth)


async def retry(
    fn: Callable[[], Awaitable[Any]] | Callable[..., Awaitable[Any]],
    *args: Any,
    attempts: int = 3,
    backoff: float = 0.5,
    exceptions: tuple = (Exception,),
    breaker: CircuitBreaker | str | None = None,
    label: str | None = None,
    **kwargs: Any,
) -> Any:
    """Call an async callable, retrying transient failures with backoff.

    Optionally guards the call with a :class:`CircuitBreaker`: while the
    circuit is open the call is rejected immediately (no sleep) with
    :class:`CircuitOpen`, and each attempt's outcome feeds the breaker.

    Args:
        fn: Zero-arg coroutine function, or a callable taking ``*args``.
        *args: Positional arguments forwarded to ``fn``.
        attempts: Maximum total attempts (≥ 1). The last failure is re-raised.
        backoff: Base backoff multiplier in seconds.
        exceptions: Exception types that trigger a retry. Anything not in this
            tuple propagates immediately.
        breaker: A :class:`CircuitBreaker` or a registry name to look one up.
        label: Name used in log lines; defaults to the function's name.
        **kwargs: Keyword arguments forwarded to ``fn``.

    Returns:
        Whatever ``fn`` returns.

    Raises:
        CircuitOpen: The breaker was open and rejected the call.
        Exception: The final attempt's exception, once ``attempts`` is
            exhausted (or immediately, for a non-retryable exception type).
    """
    cb: CircuitBreaker | None
    if isinstance(breaker, CircuitBreaker):
        cb = breaker
    elif isinstance(breaker, str) and breaker:
        cb = get_breaker(breaker)
    else:
        cb = None

    if cb is not None and not cb.allow():
        remaining = max(0.0, cb.reset_after - (time.monotonic() - cb._opened_at))
        get_metrics().incr("circuit.rejected", breaker=cb.name)
        raise CircuitOpen(f"circuit '{cb.name}' is open; retry in {remaining:.1f}s", cb)

    total = max(1, int(attempts or 1))
    name = label or getattr(fn, "__name__", "call")
    last_exc: BaseException | None = None

    for attempt in range(total):
        try:
            result = await (fn(*args, **kwargs) if (args or kwargs) else fn())
        except asyncio.CancelledError:
            raise  # cooperative cancellation must never be swallowed
        except exceptions as exc:  # noqa: PERF203 — retry loop is the point
            last_exc = exc
            if cb is not None:
                cb.record_failure(exc)
            get_metrics().incr("retry.attempt_failed", target=name)
            if attempt >= total - 1:
                break
            delay = _resolve_delay(attempt, backoff)
            log.warning(
                "retry %d/%d for %s after %s; sleeping %.2fs",
                attempt + 1, total, name, type(exc).__name__, delay,
            )
            await asyncio.sleep(delay)
        else:
            if cb is not None:
                cb.record_success()
            get_metrics().incr("retry.succeeded", target=name)
            return result

    assert last_exc is not None
    get_metrics().incr("retry.exhausted", target=name)
    raise last_exc


def get_breaker(name: str) -> CircuitBreaker:
    """Return (creating if needed) the shared breaker for ``name``.

    New breakers inherit ``config.CIRCUIT_FAIL_THRESHOLD`` and
    ``config.CIRCUIT_RESET_AFTER``.

    Args:
        name: Dependency name, e.g. ``"llm.openai"`` or ``"redis"``.

    Returns:
        The process-wide :class:`CircuitBreaker` for that name.
    """
    key = str(name or "anonymous")
    with _REGISTRY_LOCK:
        breaker = _BREAKERS.get(key)
        if breaker is None:
            breaker = CircuitBreaker(
                key,
                fail_threshold=getattr(config, "CIRCUIT_FAIL_THRESHOLD", 5),
                reset_after=getattr(config, "CIRCUIT_RESET_AFTER", 30.0),
            )
            _BREAKERS[key] = breaker
        return breaker


def reset_breakers() -> None:
    """Reset every registered breaker back to closed. Intended for tests."""
    with _REGISTRY_LOCK:
        breakers = list(_BREAKERS.values())
    for breaker in breakers:
        breaker.reset()
