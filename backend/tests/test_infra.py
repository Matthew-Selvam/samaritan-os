"""
test_infra.py — Infrastructure Test Suite
==========================================
Covers the four WS-INFRA modules the platform cannot run without, plus the task
layer that sits on top of them:

* :mod:`circuit` — breaker state machine and the retry helper.
* :mod:`cache` — set/get, TTL expiry, prefix invalidation, backend resolution.
* :mod:`observability` — redaction (incl. nested sets/tuples/camelCase keys),
  counters, timings, gauges.
* :mod:`store` — investigation/case round trip, steps, listing, stats, and the
  degraded flag.
* :mod:`tasks` — ``run_investigation_local`` producing a *finished*
  investigation end to end.

Design constraints (see ``conftest.py`` for the isolation strategy):

* **Fast.** The one test that runs the real pipeline (~1–3s) is the only slow
  one; every other test is pure in-process logic.
* **No network.** Connectors point at a closed local port, so SCOUT's
  federation and every other outbound call fail in milliseconds instead of
  reaching the internet. An investigation therefore succeeds on its
  *deterministic* agents alone — which is precisely the property the task layer
  claims: the platform works with zero infrastructure.
* **No broker, no database required.** The store degrades to memory when SQLite
  is unavailable and the queue falls back to the local path; both are asserted
  rather than assumed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

import cache as cache_module
import circuit as circuit_module
import config
import observability as obs
import store as store_module
import tasks
from conftest import _pin_cache_backend

# ── Helpers ──────────────────────────────────────────────────────────────────


def _all_agents() -> list[str]:
    """Return every agent name in the registry.

    Used to assert ``get_timeout`` covers the *whole* registry rather than a
    hand-copied list that could drift from the real one.
    """
    from agents import AGENT_REGISTRY

    return sorted(AGENT_REGISTRY)


def _find_in_keys(keys, needle: str) -> bool:
    """True when any key in ``keys`` contains ``needle`` (case-insensitive)."""
    return any(needle in str(k).lower() for k in keys)


# ═══════════════════════════════════════════════════════════════════════════
# circuit.py
# ═══════════════════════════════════════════════════════════════════════════


class TestCircuitBreaker:
    """The three-state breaker: closed → open → half_open → closed."""

    def test_starts_closed_and_allows(self):
        breaker = circuit_module.CircuitBreaker("t-allow")
        assert breaker.state == circuit_module.CircuitBreaker.CLOSED
        assert breaker.allow() is True

    def test_opens_exactly_at_threshold(self):
        """The breaker trips on the Nth failure, not before."""
        breaker = circuit_module.CircuitBreaker("t-threshold", fail_threshold=3,
                                                reset_after=60.0)

        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == circuit_module.CircuitBreaker.CLOSED, \
            "must not open before the threshold"
        assert breaker.allow() is True

        breaker.record_failure()  # third consecutive failure
        assert breaker.state == circuit_module.CircuitBreaker.OPEN
        assert breaker.allow() is False, "an open breaker must reject calls"

    def test_success_resets_the_failure_run(self):
        """Intermittent failures must not accumulate into an open circuit."""
        breaker = circuit_module.CircuitBreaker("t-reset", fail_threshold=2,
                                                reset_after=60.0)
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        assert breaker.state == circuit_module.CircuitBreaker.CLOSED
        assert breaker.failures == 1

    def test_half_opens_after_reset_window(self):
        """After ``reset_after`` elapses the breaker admits one probe."""
        breaker = circuit_module.CircuitBreaker("t-halfopen", fail_threshold=1,
                                                reset_after=0.05)
        breaker.record_failure()
        assert breaker.state == circuit_module.CircuitBreaker.OPEN

        time.sleep(0.08)  # exceed the reset window

        # Reading state applies the lazy transition.
        assert breaker.state == circuit_module.CircuitBreaker.HALF_OPEN
        # The probe is admitted…
        assert breaker.allow() is True
        # …and a second concurrent caller is rejected while it is in flight,
        # so a slow dependency is never stampeded.
        assert breaker.allow() is False

    def test_half_open_probe_success_closes_the_circuit(self):
        breaker = circuit_module.CircuitBreaker("t-probe-ok", fail_threshold=1,
                                                reset_after=0.05)
        breaker.record_failure()
        time.sleep(0.08)
        assert breaker.allow() is True
        breaker.record_success()
        assert breaker.state == circuit_module.CircuitBreaker.CLOSED
        assert breaker.allow() is True

    def test_half_open_probe_failure_reopens_immediately(self):
        """A failed probe must not leave the breaker half-open forever."""
        breaker = circuit_module.CircuitBreaker("t-probe-bad", fail_threshold=1,
                                                reset_after=0.05)
        breaker.record_failure()
        time.sleep(0.08)
        assert breaker.allow() is True
        breaker.record_failure()
        assert breaker.state == circuit_module.CircuitBreaker.OPEN
        assert breaker.allow() is False

    def test_stats_is_json_safe_and_counts_rejections(self):
        breaker = circuit_module.CircuitBreaker("t-stats", fail_threshold=1,
                                                reset_after=60.0)
        breaker.record_failure()
        breaker.allow()  # rejected
        breaker.allow()  # rejected
        stats = breaker.stats()
        assert stats["name"] == "t-stats"
        assert stats["state"] == circuit_module.CircuitBreaker.OPEN
        assert stats["failures"] == 1
        assert stats["rejections"] == 2
        json.dumps(stats)  # must not raise

    def test_get_breaker_returns_a_shared_instance(self):
        a = circuit_module.get_breaker("shared-name")
        b = circuit_module.get_breaker("shared-name")
        assert a is b

    def test_get_breaker_uses_config_thresholds(self):
        breaker = circuit_module.get_breaker("cfg-baker")
        assert breaker.fail_threshold == config.CIRCUIT_FAIL_THRESHOLD
        assert breaker.reset_after == config.CIRCUIT_RESET_AFTER


class TestCircuitRetry:
    """``retry`` — transient failures must be absorbed, real ones propagated."""

    @pytest.mark.asyncio
    async def test_returns_first_success_without_retrying(self):
        calls = {"n": 0}

        async def _ok():
            calls["n"] += 1
            return "done"

        result = await circuit_module.retry(_ok, attempts=3, backoff=0.0)
        assert result == "done"
        assert calls["n"] == 1, "a success must not burn extra attempts"

    @pytest.mark.asyncio
    async def test_succeeds_after_transient_failures(self):
        """The headline retry case: fail, fail, then succeed."""
        calls = {"n": 0}

        async def _flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("upstream blip")
            return "recovered"

        result = await circuit_module.retry(_flaky, attempts=5, backoff=0.0)
        assert result == "recovered"
        assert calls["n"] == 3, "must retry until the dependency recovers"

    @pytest.mark.asyncio
    async def test_exhausted_attempts_raise_the_last_error(self):
        """When every attempt fails the caller must see the failure, not None."""
        calls = {"n": 0}

        async def _always_down():
            calls["n"] += 1
            raise ConnectionError("still down")

        with pytest.raises(ConnectionError):
            await circuit_module.retry(_always_down, attempts=3, backoff=0.0)
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_only_declared_exceptions_are_retried(self):
        """A ValueError is a bug, not a blip — retrying it just wastes time."""
        calls = {"n": 0}

        async def _bug():
            calls["n"] += 1
            raise ValueError("bad input")

        with pytest.raises(ValueError):
            await circuit_module.retry(
                _bug, attempts=4, backoff=0.0, exceptions=(ConnectionError,)
            )
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_open_circuit_short_circuits_the_call(self):
        """With a breaker, an open circuit must stop calling the dependency.

        ``retry`` checks ``allow()`` *before* the attempt loop, so an open
        circuit raises :class:`CircuitOpen` without invoking the callable at
        all — that is the whole point of pairing the two helpers.
        """
        breaker = circuit_module.CircuitBreaker("retry-cb", fail_threshold=1,
                                                reset_after=60.0)
        calls = {"n": 0}

        async def _down():
            calls["n"] += 1
            raise ConnectionError("down")

        # First round: the call is allowed, fails, and trips the breaker.
        with pytest.raises(ConnectionError):
            await circuit_module.retry(
                _down, attempts=3, backoff=0.0, breaker=breaker,
            )
        assert breaker.state == circuit_module.CircuitBreaker.OPEN

        # Second round: the circuit is open, so the callable is never invoked.
        calls["n"] = 0
        with pytest.raises(circuit_module.CircuitOpen):
            await circuit_module.retry(
                _down, attempts=3, backoff=0.0, breaker=breaker,
            )
        assert calls["n"] == 0, \
            f"an open breaker must fail fast, but the dependency was called {calls['n']}x"

    @pytest.mark.asyncio
    async def test_breaker_records_each_attempt(self):
        """Each retry attempt feeds the breaker, so the failure count tracks
        how hard the code actually tried."""
        breaker = circuit_module.CircuitBreaker("retry-count", fail_threshold=99,
                                                reset_after=60.0)

        async def _down():
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            await circuit_module.retry(_down, attempts=3, backoff=0.0, breaker=breaker)

        assert breaker.failures == 3
        assert breaker.stats()["total_failures"] == 3


# ═══════════════════════════════════════════════════════════════════════════
# cache.py
# ═══════════════════════════════════════════════════════════════════════════


class TestCache:
    """Cache behaviour, on whichever backend is active (here: in-process)."""

    @pytest.mark.asyncio
    async def test_set_then_get_round_trips(self):
        await cache_module.cache_set("k:roundtrip", {"a": 1, "b": [2, 3]})
        assert await cache_module.cache_get("k:roundtrip") == {"a": 1, "b": [2, 3]}

    @pytest.mark.asyncio
    async def test_miss_returns_none(self):
        assert await cache_module.cache_get("k:definitely-absent") is None

    @pytest.mark.asyncio
    async def test_empty_key_is_a_noop(self):
        await cache_module.cache_set("", "value")
        assert await cache_module.cache_get("") is None

    @pytest.mark.asyncio
    async def test_ttl_expiry_removes_the_entry(self):
        await cache_module.cache_set("k:ttl", "short-lived", ttl=0.05)
        assert await cache_module.cache_get("k:ttl") == "short-lived"
        time.sleep(0.12)
        assert await cache_module.cache_get("k:ttl") is None, \
            "an expired key must read as a miss"

    @pytest.mark.asyncio
    async def test_zero_ttl_means_no_expiry(self):
        await cache_module.cache_set("k:forever", "sticky", ttl=0)
        time.sleep(0.05)
        assert await cache_module.cache_get("k:forever") == "sticky"

    @pytest.mark.asyncio
    async def test_delete_removes_one_key(self):
        await cache_module.cache_set("k:del", 1)
        assert await cache_module.cache_delete("k:del") is True
        assert await cache_module.cache_get("k:del") is None
        assert await cache_module.cache_delete("k:del") is False

    @pytest.mark.asyncio
    async def test_delete_prefix_clears_a_namespace(self):
        """Namespace invalidation is how a stale case/report gets dropped."""
        await cache_module.cache_set("case:c1:a", 1)
        await cache_module.cache_set("case:c1:b", 2)
        await cache_module.cache_set("case:c2:a", 3)
        await cache_module.cache_set("other:keep", 4)

        removed = await cache_module.cache_delete_prefix("case:c1:")

        assert removed == 2
        assert await cache_module.cache_get("case:c1:a") is None
        assert await cache_module.cache_get("case:c1:b") is None
        assert await cache_module.cache_get("case:c2:a") == 3, \
            "a prefix delete must not touch a sibling namespace"
        assert await cache_module.cache_get("other:keep") == 4

    @pytest.mark.asyncio
    async def test_unserialisable_value_is_skipped_not_raised(self):
        """A value the backend cannot encode must not break the caller."""
        await cache_module.cache_set("k:bad", {1, 2, 3})  # set -> not JSON-safe
        # Whether it stored depends on the backend's encoder; the contract that
        # matters is that neither call raised.
        await cache_module.cache_get("k:bad")

    def test_stats_report_backend_and_counters(self):
        stats = cache_module.cache_stats()
        assert stats["backend"] in ("redis", "memory")
        for key in ("hits", "misses", "sets", "deletes", "errors", "max_size"):
            assert key in stats
        assert stats["max_size"] == config.CACHE_MAX_SIZE
        json.dumps(stats)

    @pytest.mark.asyncio
    async def test_stats_count_hits_and_misses(self):
        cache_module.reset_stats()
        await cache_module.cache_set("k:stats", "v")
        await cache_module.cache_get("k:stats")     # hit
        await cache_module.cache_get("k:stats:miss")  # miss
        stats = cache_module.cache_stats()
        assert stats["hits"] >= 1
        assert stats["misses"] >= 1
        assert stats["sets"] >= 1

    def test_invalidate_backend_forces_a_reprobe(self, monkeypatch):
        """The hook the cache's own failover path (and ops) depend on.

        ``invalidate_backend`` drops the resolved backend and the client so the
        next call re-probes. Asserted on the *state reset*, not on the re-probe
        outcome: the probe is a network call, and this suite must stay offline.
        (See ``conftest._pin_cache_backend`` for why the probe path is not
        exercised here at all.)
        """
        import cache as cache_module

        monkeypatch.setattr(cache_module, "_backend", "redis", raising=False)
        assert cache_module.cache_backend() == "redis"

        cache_module.invalidate_backend()

        assert cache_module._backend is None, \
            "invalidate_backend must force the next call to re-probe"
        assert cache_module._redis is None

        # Restore the pinned state for whatever runs next.
        _pin_cache_backend("memory")

    @pytest.mark.asyncio
    async def test_works_with_no_redis_at_all(self):
        """The zero-config guarantee: memory backend serves every operation."""
        assert cache_module.cache_backend() == "memory", \
            "the test env points Redis at a closed port, so it must demote"
        await cache_module.cache_set("k:noredis", [1, 2, 3])
        assert await cache_module.cache_get("k:noredis") == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_cached_decorator_hits_the_cache(self):
        calls = {"n": 0}

        @cache_module.cached(ttl=30.0, key="deco:fixed")
        async def _compute(value: int) -> int:
            calls["n"] += 1
            return value * 2

        assert await _compute(21) == 42
        assert await _compute(21) == 42
        assert calls["n"] == 1, "the second call must be served from cache"


# ═══════════════════════════════════════════════════════════════════════════
# observability.py
# ═══════════════════════════════════════════════════════════════════════════


class TestRedaction:
    """Secrets must not survive redaction at any depth or key style."""

    def test_redact_keys_covers_the_contract(self):
        """CONTRACTS.md §7 names the required set explicitly."""
        required = {
            "apikey", "api_key", "password", "secret", "token",
            "authorization", "cookie", "ssh_key", "sessionid", "session_id",
        }
        assert required <= set(obs.REDACT_KEYS), \
            f"missing: {sorted(required - set(obs.REDACT_KEYS))}"

    def test_masks_top_level_secret(self):
        out = obs.redact({"password": "hunter2", "user": "alice"})
        assert out["password"] == obs.REDACTED
        assert out["user"] == "alice", "non-secrets must pass through untouched"

    def test_masks_nested_secrets_deeply(self):
        payload = {
            "level1": {
                "level2": {
                    "level3": {"api_key": "sk-deep", "harmless": "keep"},
                },
            },
        }
        out = obs.redact(payload)
        assert out["level1"]["level2"]["level3"]["api_key"] == obs.REDACTED
        assert out["level1"]["level2"]["level3"]["harmless"] == "keep"

    def test_masks_camel_case_and_snake_case_keys(self):
        """Key matching is style-agnostic: apiKey, api_key, API-KEY all mask."""
        out = obs.redact({
            "apiKey": "a",
            "api_key": "b",
            "sessionId": "c",
            "accessToken": "d",
            "X-Api-Key": "e",
        })
        for key in ("apiKey", "api_key", "sessionId", "accessToken", "X-Api-Key"):
            assert out[key] == obs.REDACTED, f"{key} was not masked"

    def test_masks_secrets_inside_lists(self):
        out = obs.redact({"results": [{"token": "t1"}, {"token": "t2"}]})
        assert all(r["token"] == obs.REDACTED for r in out["results"])

    def test_masks_key_value_tuple_pairs(self):
        """A ``("password", "x")`` tuple carries the same secret as a mapping."""
        out = obs.redact({"creds": ("password", "hunter2")})
        assert obs.REDACTED in _flatten_values(out), \
            "the secret value inside a key-value tuple survived"

    def test_masks_key_value_pairs_inside_sets(self):
        """Sets hold values, not key-addressed fields — the key-value *tuple*
        is what carries the secret, and that is what must be masked."""
        out = obs.redact({"creds": {("password", "hunter2"), ("username", "alice")}})
        flat = _flatten_values(out)
        assert obs.REDACTED in flat, \
            "a (key, secret) tuple inside a set must have its value masked"
        assert "hunter2" not in flat, \
            "the secret value leaked out of a set member"
        assert "alice" in flat, "a non-secret member must pass through untouched"

    def test_survives_a_set_of_plain_values(self):
        """A set of opaque strings has no key to match on, so it is untouched
        — and, critically, must not raise (sets are unhashable after masking,
        so this is where a naive redaction implementation breaks)."""
        out = obs.redact({"tokens": {"tok_a", "tok_b"}})
        assert "tok_a" in _flatten_values(out)

    def test_masks_mixed_containers_without_raising(self):
        """Sets of tuples, dicts inside lists, etc. must not blow up."""
        out = obs.redact({
            "pairs": {("password", "p1"), ("username", "u1")},
            "deep": [{"inner": ({"secret": "s"},)}],
        })
        # The contract is "no secret value survives", not a specific shape.
        assert obs.REDACTED in _flatten_values(out)

    def test_survives_reference_cycles(self):
        """A self-referencing payload must not recurse forever."""
        payload: dict = {"name": "loop"}
        payload["self"] = payload
        out = obs.redact(payload)
        assert out["name"] == "loop"

    def test_redacted_output_is_json_safe(self):
        out = obs.redact({"api_key": "x", "items": [{"token": "y"}]})
        json.dumps(out)  # must not raise


class TestMetrics:
    """Counters, timings and gauges."""

    def test_incr_accumulates(self):
        m = obs.get_metrics()
        m.reset()
        m.incr("hits")
        m.incr("hits")
        m.incr("hits", value=3)
        assert m.snapshot()["counters"]["hits"] == 5.0

    def test_tags_split_series(self):
        m = obs.get_metrics()
        m.reset()
        m.incr("agent_runs", agent="SCOUT")
        m.incr("agent_runs", agent="NEXUS")
        counters = m.snapshot()["counters"]
        assert counters["agent_runs{agent=SCOUT}"] == 1.0
        assert counters["agent_runs{agent=NEXUS}"] == 1.0

    def test_timing_records_avg_and_bounds(self):
        m = obs.get_metrics()
        m.reset()
        m.timing("latency", 0.1)
        m.timing("latency", 0.3)
        entry = m.snapshot()["timings"]["latency"]
        assert entry["count"] == 2
        assert entry["min_s"] == pytest.approx(0.1)
        assert entry["max_s"] == pytest.approx(0.3)
        assert entry["avg_s"] == pytest.approx(0.2)

    def test_timing_ignores_negative_and_nan(self):
        """A bogus duration must not poison the averages."""
        m = obs.get_metrics()
        m.reset()
        m.timing("bad", -5.0)
        m.timing("bad", float("nan"))
        assert "bad" not in m.snapshot()["timings"]

    def test_gauge_keeps_last_value(self):
        m = obs.get_metrics()
        m.reset()
        m.gauge("inflight", 1)
        m.gauge("inflight", 7)
        assert m.snapshot()["gauges"]["inflight"] == 7.0

    def test_disabled_registry_records_nothing(self):
        """``METRICS_ENABLED=false`` must make recording a no-op."""
        m = obs.Metrics(enabled=False)
        m.incr("nope")
        m.timing("nope", 1.0)
        m.gauge("nope", 1.0)
        snap = m.snapshot()
        assert snap["counters"] == {}
        assert snap["timings"] == {}
        assert snap["gauges"] == {}
        assert snap["enabled"] is False

    def test_snapshot_shape_and_json_safety(self):
        m = obs.get_metrics()
        m.reset()
        m.incr("x")
        snap = m.snapshot()
        for key in ("counters", "timings", "gauges", "totals", "uptime_s", "enabled"):
            assert key in snap
        json.dumps(snap)

    def test_metrics_never_raise_on_hostile_tags(self):
        m = obs.get_metrics()
        m.reset()
        m.incr("hostile", value="not-a-number")  # coerced, must not raise
        m.incr("hostile2", tag=object())
        m.snapshot()

    def test_get_logger_returns_a_named_child_logger(self):
        """``get_logger`` must name the logger it hands back — the whole point
        is that log lines are attributable to a module.

        Note: ``logging.getLogger(name)`` is a *registry lookup*, so two calls
        with the same name legitimately return the same object. That is correct
        stdlib behaviour, not a defect; asserting otherwise would be testing
        the stdlib rather than this module.
        """
        log = obs.get_logger("signal-os.test")
        assert log.name == "signal-os.test"
        assert log is not logging.getLogger(), \
            "get_logger must never hand back the root logger"
        assert obs.get_logger("signal-os.test") is log, \
            "the stdlib logger registry should return the same handle per name"


def _flatten_values(value) -> list:
    """Collect every scalar inside a nested structure (test helper)."""
    out: list = []
    if isinstance(value, dict):
        for k, v in value.items():
            out.append(k)
            out.extend(_flatten_values(v))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            out.extend(_flatten_values(v))
    else:
        out.append(value)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# store.py
# ═══════════════════════════════════════════════════════════════════════════


class TestStore:
    """Investigations, cases, reports and the degraded flag."""

    @pytest.mark.asyncio
    async def test_investigation_round_trip(self):
        store = await store_module.get_store()
        await store.save_investigation({
            "inv_id": "inv-1",
            "case_id": "case-1",
            "input": "test@example.com",
            "input_type": "email",
            "status": "done",
        })
        got = await store.get_investigation("inv-1")
        assert got is not None
        assert got["inv_id"] == "inv-1"
        assert got["case_id"] == "case-1"
        assert got["input_type"] == "email"
        assert got["status"] == "done"

    @pytest.mark.asyncio
    async def test_missing_investigation_returns_none(self):
        store = await store_module.get_store()
        assert await store.get_investigation("nope") is None

    @pytest.mark.asyncio
    async def test_save_is_upsert_not_append(self):
        """Re-saving the same id must replace, never duplicate."""
        store = await store_module.get_store()
        await store.save_investigation({"inv_id": "dup", "case_id": "c", "status": "running"})
        await store.save_investigation({"inv_id": "dup", "case_id": "c", "status": "done"})
        got = await store.get_investigation("dup")
        assert got["status"] == "done"
        rows = await store.list_investigations(case_id="c")
        assert sum(1 for r in rows if r["inv_id"] == "dup") == 1

    @pytest.mark.asyncio
    async def test_update_merges_a_patch(self):
        store = await store_module.get_store()
        await store.save_investigation({"inv_id": "upd", "case_id": "c", "status": "running"})
        await store.update_investigation("upd", {"status": "done", "confidence": 0.8})
        got = await store.get_investigation("upd")
        assert got["status"] == "done"
        assert got["confidence"] == 0.8
        assert got["case_id"] == "c", "a patch must not drop untouched fields"

    @pytest.mark.asyncio
    async def test_update_unknown_id_is_a_noop(self):
        store = await store_module.get_store()
        await store.update_investigation("ghost", {"status": "done"})  # must not raise
        assert await store.get_investigation("ghost") is None

    @pytest.mark.asyncio
    async def test_append_step_accumulates_in_order(self):
        store = await store_module.get_store()
        await store.save_investigation({"inv_id": "steps", "case_id": "c"})
        for step in ("routed", "agents launched", "report ready"):
            await store.append_step("steps", step)
        got = await store.get_investigation("steps")
        assert got["steps"] == ["routed", "agents launched", "report ready"]

    @pytest.mark.asyncio
    async def test_append_step_on_unknown_id_is_a_noop(self):
        store = await store_module.get_store()
        await store.append_step("ghost", "step")  # must not raise

    @pytest.mark.asyncio
    async def test_list_investigations_filters_by_case(self):
        store = await store_module.get_store()
        await store.save_investigation({"inv_id": "a1", "case_id": "case-A"})
        await store.save_investigation({"inv_id": "a2", "case_id": "case-A"})
        await store.save_investigation({"inv_id": "b1", "case_id": "case-B"})

        only_a = await store.list_investigations(case_id="case-A")
        assert {r["inv_id"] for r in only_a} == {"a1", "a2"}

        everything = await store.list_investigations()
        assert {"a1", "a2", "b1"} <= {r["inv_id"] for r in everything}

    @pytest.mark.asyncio
    async def test_list_investigations_respects_limit(self):
        store = await store_module.get_store()
        for i in range(5):
            await store.save_investigation({"inv_id": f"lim-{i}", "case_id": "c"})
        rows = await store.list_investigations(case_id="c", limit=2)
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_list_investigations_is_newest_first(self):
        """Rows come back newest-first. Uses a private ``case_id`` so leftover
        rows from other tests cannot change the answer."""
        store = await store_module.get_store()
        case = "c-order-isolated"
        for i in range(3):
            await store.save_investigation({
                "inv_id": f"ord-{i}", "case_id": case,
                "created_at": f"2026-01-0{i + 1}T00:00:00.000",
            })
        rows = await store.list_investigations(case_id=case, limit=3)
        assert [r["inv_id"] for r in rows] == ["ord-2", "ord-1", "ord-0"], \
            "list_investigations must order newest-first"

    @pytest.mark.asyncio
    async def test_case_round_trip_and_delete(self):
        store = await store_module.get_store()
        await store.upsert_case({"case_id": "c-rt", "name": "Test Case",
                                 "target": "example.com"})
        got = await store.get_case("c-rt")
        assert got["name"] == "Test Case"

        await store.upsert_case({"case_id": "c-rt", "name": "Renamed"})
        assert (await store.get_case("c-rt"))["name"] == "Renamed"

        assert await store.delete_case("c-rt") is True
        assert await store.get_case("c-rt") is None
        assert await store.delete_case("c-rt") is False

    @pytest.mark.asyncio
    async def test_list_cases(self):
        store = await store_module.get_store()
        await store.upsert_case({"case_id": "c-list-1", "name": "One"})
        await store.upsert_case({"case_id": "c-list-2", "name": "Two"})
        ids = {c["case_id"] for c in await store.list_cases()}
        assert {"c-list-1", "c-list-2"} <= ids

    @pytest.mark.asyncio
    async def test_case_stats_aggregates_investigations(self):
        store = await store_module.get_store()
        await store.upsert_case({"case_id": "c-stats", "name": "Stats"})
        await store.save_investigation({
            "inv_id": "s1", "case_id": "c-stats", "status": "done", "confidence": 0.8,
            "steps": ["a", "b"],
            "report": {"entities": [{"id": "e1"}], "signals": [{"type": "account"}]},
        })
        await store.save_investigation({
            "inv_id": "s2", "case_id": "c-stats", "status": "done", "confidence": 0.6,
            "steps": ["a"],
            "report": {"entities": [], "signals": []},
        })

        stats = await store.case_stats("c-stats")
        assert stats["exists"] is True
        assert stats["investigations"] == 2
        assert stats["total_steps"] == 3
        assert stats["entities"] == 1
        assert stats["signals"] == 1
        assert stats["confidence"] == pytest.approx(0.7)
        assert stats["by_status"] == {"done": 2}
        json.dumps(stats)

    @pytest.mark.asyncio
    async def test_case_stats_for_unknown_case_is_well_formed(self):
        """Every key must exist even when the case does not — the API returns
        this dict directly."""
        store = await store_module.get_store()
        stats = await store.case_stats("never-existed")
        assert stats["exists"] is False
        assert stats["investigations"] == 0
        assert stats["confidence"] == 0.0
        json.dumps(stats)

    @pytest.mark.asyncio
    async def test_save_report_returns_an_id(self):
        store = await store_module.get_store()
        await store.save_investigation({"inv_id": "rep-1", "case_id": "c"})
        report_id = await store.save_report("rep-1", "# Report\n\nbody", {"format": "markdown"})
        assert report_id
        latest = await store.get_latest_report("rep-1")
        assert latest is not None
        assert "Report" in latest["markdown"]

    @pytest.mark.asyncio
    async def test_audit_records_an_event(self):
        """Audit events must be retrievable.

        The two store backends expose the payload differently — ``MemoryStore``
        nests it under ``data``, ``SQLiteStore`` promotes ``event`` to a real
        column and also keeps ``data`` — so the assertion looks in both places
        rather than pinning one backend's shape.
        """
        store = await store_module.get_store()
        await store.audit({"event": "investigation.created", "inv_id": "aud-1"})
        events = await store.list_audit(limit=10)
        assert events, "the audit trail recorded nothing"

        def _has_event(record: dict) -> bool:
            return (
                record.get("event") == "investigation.created"
                or (record.get("data") or {}).get("event") == "investigation.created"
            )

        assert any(_has_event(e) for e in events)

    @pytest.mark.asyncio
    async def test_audit_masks_secrets_before_storing(self):
        """The audit trail is exportable, so a credential must never land in it."""
        store = await store_module.get_store()
        # Assembled at runtime so this sentinel is not itself a secret-shaped
        # literal sitting in the repo.
        marker = "sk-" + "audit-canary"
        await store.audit({"event": "auth.attempt", "api_key": marker})
        events = await store.list_audit(limit=10)
        blob = json.dumps(events, default=str)
        assert marker not in blob, "a secret reached the audit trail"
        assert obs.REDACTED in blob, \
            "the secret should have been masked, not merely absent"

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "store.py bug: SQLiteStore._execute returns self._db.lastrowid, but "
            "lastrowid is a cursor attribute — aiosqlite.Connection has no such "
            "member — so the first INSERT degrades the store to memory. Fixing "
            "store.py makes this xfail turn into a pass (strict=True enforces it)."
        ),
    )
    async def test_sqlite_writes_do_not_degrade_the_store(self, tmp_path):
        """Regression guard for a real bug in ``store.py``.

        ``SQLiteStore._execute`` returned ``self._db.lastrowid`` after a write.
        ``lastrowid`` is a *cursor* attribute — ``aiosqlite.Connection`` has no
        such member — so every INSERT raised ``AttributeError``, which the
        method's own error handler converted into ``self.degrade(...)``. The
        first write against a perfectly healthy SQLite file therefore silently
        demoted the store to memory, and nothing written after it was durable.

        This test asserts writes work on the SQL backend. It currently fails;
        the fix belongs to whoever owns ``store.py``.
        """
        path = str(tmp_path / "audit.db")
        fresh = store_module.SQLiteStore(path)
        await fresh.start()
        try:
            assert fresh.degraded is False, "the store degraded on start"
            await fresh.audit({"event": "investigation.created", "inv_id": "aud-sql"})

            assert fresh.degraded is False, (
                "a write degraded the SQLite store — see the _execute/lastrowid "
                "bug documented in this test"
            )
            assert fresh.backend == "sqlite", (
                f"store fell back to {fresh.backend!r} after a successful write"
            )
            events = await fresh.list_audit(limit=10)
            assert events, "the audit event did not reach the audit_log table"
        finally:
            await fresh.close()

    @pytest.mark.asyncio
    async def test_health_reports_backend_and_degraded_flag(self):
        """``degraded`` is the flag ops watches: True means "running on memory"."""
        store = await store_module.get_store()
        health = store.health()
        assert "backend" in health
        assert isinstance(health["degraded"], bool)
        json.dumps(health)

    @pytest.mark.asyncio
    async def test_sqlite_backend_is_actually_used(self):
        """With SQLITE_PATH set the store must be the SQL backend, not memory."""
        store = await store_module.get_store()
        if store.backend != "sqlite":  # pragma: no cover - env-dependent
            pytest.skip(f"store backend is {store.backend}, not sqlite")
        assert store.degraded is False

    @pytest.mark.asyncio
    async def test_persists_across_a_reopen(self):
        """A real database keeps data; a memory store would lose it."""
        path = str(store_module.config.SQLITE_PATH)
        if not path:  # pragma: no cover - env-dependent
            pytest.skip("SQLITE_PATH is not configured")

        fresh = store_module.SQLiteStore(path)
        await fresh.start()
        try:
            await fresh.save_investigation({"inv_id": "persist-1", "case_id": "c",
                                            "input": "durable", "status": "done"})
            await fresh.close()

            reopened = store_module.SQLiteStore(path)
            await reopened.start()
            try:
                got = await reopened.get_investigation("persist-1")
                assert got is not None, "data did not survive a reopen"
                assert got["input"] == "durable"
            finally:
                await reopened.close()
        finally:
            await fresh.close()


# ═══════════════════════════════════════════════════════════════════════════
# config.py
# ═══════════════════════════════════════════════════════════════════════════


class TestConfig:
    """Settings and the per-agent timeout table."""

    def test_every_registry_agent_has_a_timeout(self):
        """The regression this guards: ``get_timeout`` used to cover 8 of 16.

        Asserted against the actual mapping rather than ``DEFAULT_TIMEOUT``:
        several agents legitimately share a 30s budget, so "equals the default"
        is not the same as "is missing from the table". Every name must have a
        ``TIMEOUT_<NAME>`` constant and that constant must be what is returned.
        """
        required = {
            "APEX", "CRAWLER", "NEXUS", "KRONOS", "VAULT", "SENTINEL",
            "QUILL", "PRISM", "TERRA", "INK", "ECHO", "SIGMA", "PHONOS",
            "EMAIL", "SCOUT", "IRIS",
        }
        missing = []
        for name in sorted(required):
            constant = f"TIMEOUT_{name}"
            if not hasattr(config, constant):
                missing.append(f"{name} (no {constant})")
                continue
            if config.get_timeout(name) != getattr(config, constant):
                missing.append(f"{name} (table entry disagrees with {constant})")
        assert not missing, f"agents not correctly wired: {missing}"

    def test_get_timeout_covers_the_live_registry(self):
        """Cross-check against AGENT_REGISTRY, not a hand-written list."""
        for agent in _all_agents():
            value = config.get_timeout(agent)
            assert isinstance(value, float)
            assert value > 0, f"{agent} has a non-positive timeout"

    def test_get_timeout_is_case_insensitive(self):
        assert config.get_timeout("kronos") == config.get_timeout("KRONOS")
        assert config.get_timeout("  QuIlL  ") == config.get_timeout("QUILL")

    def test_get_timeout_unknown_agent_falls_back(self):
        """An unknown name must degrade, never raise — a new agent is not a 500."""
        assert config.get_timeout("NOT_AN_AGENT") == config.DEFAULT_TIMEOUT
        assert config.get_timeout("") == config.DEFAULT_TIMEOUT
        assert config.get_timeout(None) == config.DEFAULT_TIMEOUT

    def test_get_timeout_env_override(self, monkeypatch):
        monkeypatch.setattr(config, "TIMEOUT_QUILL", 99.0)
        assert config.get_timeout("QUILL") == 99.0

    def test_new_settings_exist_and_have_safe_defaults(self):
        for name in (
            "CACHE_TTL_SECONDS", "CACHE_MAX_SIZE", "REDIS_REQUIRED",
            "CELERY_BROKER_URL", "CELERY_ENABLED", "SQLITE_PATH",
            "STORE_BACKEND", "CIRCUIT_FAIL_THRESHOLD", "CIRCUIT_RESET_AFTER",
            "MAX_CONCURRENT_INVESTIGATIONS", "REQUEST_TIMEOUT", "LLM_ENABLED",
            "LLM_TIMEOUT", "LLM_MAX_TOKENS", "LLM_TEMPERATURE", "OLLAMA_URL",
            "LLM_PROVIDER_ORDER", "AUDIT_LOG", "METRICS_ENABLED",
        ):
            assert hasattr(config, name), f"config.{name} is missing"

        assert config.CACHE_TTL_SECONDS > 0
        assert config.CACHE_MAX_SIZE > 0
        assert config.CIRCUIT_FAIL_THRESHOLD >= 1
        assert config.CIRCUIT_RESET_AFTER > 0
        assert config.MAX_CONCURRENT_INVESTIGATIONS >= 1
        assert config.REQUEST_TIMEOUT > 0
        assert config.LLM_MAX_TOKENS > 0
        assert config.STORE_BACKEND in ("auto", "memory", "sqlite", "postgres")
        assert isinstance(config.CELERY_ENABLED, bool)
        assert isinstance(config.LLM_ENABLED, bool)
        assert isinstance(config.METRICS_ENABLED, bool)
        assert isinstance(config.AUDIT_LOG, bool)
        assert isinstance(config.REDIS_REQUIRED, bool)

    def test_llm_provider_order_is_a_usable_chain(self):
        providers = [p.strip() for p in config.LLM_PROVIDER_ORDER.split(",") if p.strip()]
        assert providers, "the provider chain must never be empty"
        assert "ollama" in providers, \
            "the local provider is what makes the chain work with no API keys"

    def test_existing_feature_flag_helper_still_works(self):
        """Appending to config must not break the pre-existing helper."""
        assert config.is_feature_enabled("pdf_export") in (True, False)
        assert config.is_feature_enabled("no_such_feature") is False


# ═══════════════════════════════════════════════════════════════════════════
# tasks.py
# ═══════════════════════════════════════════════════════════════════════════


class TestTaskLayer:
    """The local fallback, the dispatcher, and the Celery wiring."""

    def test_celery_available_is_false_without_a_broker(self):
        """This environment has no broker; the platform must say so, not lie."""
        assert tasks.celery_available() is False

    def test_celery_available_never_raises(self):
        """Ops endpoints call this on every health check."""
        assert tasks.celery_available() in (True, False)

    def test_task_status_is_json_safe(self):
        status = tasks.task_status()
        assert "celery_available" in status
        assert "inflight" in status
        assert "limits" in status
        json.dumps(status)

    def test_celery_tasks_are_registered_or_wrapped(self):
        """The three CONTRACTS.md §9 names must all be resolvable."""
        for name in ("signal_os.investigate", "signal_os.sentinel_scan",
                     "signal_os.report"):
            assert name in tasks.celery_tasks
            assert callable(tasks.celery_tasks[name])

    def test_task_status_never_leaks_a_broker_password(self):
        """A DSN with credentials must be masked before it leaves the module."""
        import celery_app

        masked = celery_app._mask_dsn("redis://user:hunter2@localhost:6379/0")
        assert "hunter2" not in masked
        assert "user" in masked and "localhost" in masked
        json.dumps(tasks.task_status())

    def test_run_investigation_is_an_async_dispatcher(self):
        """It must stay the async dispatcher, not be shadowed by a task object."""
        assert asyncio.iscoroutinefunction(tasks.run_investigation)
        assert asyncio.iscoroutinefunction(tasks.run_investigation_local)

    def test_payload_slim_drops_callables_and_masks_secrets(self):
        """Only serialisable request params may reach the broker."""
        async def _emit(_msg: str) -> None:  # a live callback must be dropped
            return None

        slim = tasks._payload_slim({
            "input": "target@example.com",
            "emit": _emit,
            "api_key": "sk-should-not-travel",
        })
        assert "emit" not in slim
        assert slim["input"] == "target@example.com"
        assert "sk-should-not-travel" not in json.dumps(slim)

    def test_payload_slim_output_is_json_serialisable(self):
        slim = tasks._payload_slim({"input": "x", "case_id": "c", "depth": 2})
        json.dumps(slim)

    @pytest.mark.asyncio
    async def test_local_rejects_empty_input_without_running_agents(self):
        result = await tasks.run_investigation_local({"input": "   "})
        assert result["status"] == "error"
        assert result["error"]
        assert result["agent_results"] == []

    @pytest.mark.asyncio
    async def test_local_rejects_a_payload_with_no_input(self):
        result = await tasks.run_investigation_local({})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_dispatcher_falls_back_to_local_with_no_broker(self):
        """No broker configured => the request runs inline and returns a result."""
        result = await tasks.run_investigation({"input": "fallback@example.com"})
        assert result["status"] == "done"
        assert result["agent_results"], "the fallback must actually run agents"
        assert "task_id" not in result


class TestLocalInvestigation:
    """The end-to-end path the whole platform depends on."""

    @pytest.mark.asyncio
    async def test_produces_a_finished_investigation(self):
        """This is THE critical test: zero infrastructure, real pipeline.

        Runs APEX for real — routing, the primary swarm, the correlation tier,
        and QUILL's report — with no broker, no Postgres, and no network. If
        this passes, the platform runs on a bare ``uvicorn main:app``.
        """
        result = await tasks.run_investigation_local({"input": "test@example.com"})

        # ── contract shape ──────────────────────────────────────────────────
        assert result["status"] == "done", f"pipeline failed: {result.get('error')}"
        for key in ("status", "inv_id", "case_id", "input_type",
                    "agent_results", "report", "confidence", "latency_s"):
            assert key in result, f"missing key: {key}"

        assert result["inv_id"], "an investigation must carry an id"
        assert result["case_id"], "an investigation must carry a case id"
        assert result["input_type"] == "email", \
            f"router misclassified an email: {result['input_type']}"

        # ── agents actually ran ─────────────────────────────────────────────
        agents = result["agent_results"]
        assert len(agents) >= 5, f"only {len(agents)} agents ran"
        names = {a["agent"] for a in agents}
        assert {"EMAIL", "NEXUS", "KRONOS", "QUILL"} <= names, \
            f"correlation tier did not run: {sorted(names)}"
        for agent in agents:
            assert agent["status"] in ("done", "error", "partial", "stub"), \
                f"{agent['agent']} has status {agent['status']!r}"
            assert "confidence" in agent
            assert "latency_s" in agent

        # ── the report is real ─────────────────────────────────────────────
        report = result["report"]
        assert report is not None
        assert report["markdown"], "QUILL produced no markdown"
        assert "Intelligence Report" in report["markdown"]
        assert report["input_type"] == "email"
        assert 0.0 <= result["confidence"] <= 1.0
        assert result["latency_s"] > 0

    @pytest.mark.asyncio
    async def test_result_is_json_serialisable(self):
        """It must survive Celery's JSON serializer unchanged."""
        result = await tasks.run_investigation_local({"input": "json@example.com"})
        blob = json.dumps(result)  # must not raise
        assert json.loads(blob)["status"] == "done"

    @pytest.mark.asyncio
    async def test_result_is_persisted_to_the_store(self):
        """A finished investigation must be retrievable by its id."""
        result = await tasks.run_investigation_local({"input": "persist@example.com"})
        store = await store_module.get_store()
        record = await store.get_investigation(result["inv_id"])
        assert record is not None, "the investigation was not persisted"
        assert record["status"] == "done"
        assert record["case_id"] == result["case_id"]

    @pytest.mark.asyncio
    async def test_case_is_registered(self):
        result = await tasks.run_investigation_local({"input": "case@example.com"})
        store = await store_module.get_store()
        case = await store.get_case(result["case_id"])
        assert case is not None, "the case was not registered"

    @pytest.mark.asyncio
    async def test_caller_supplied_ids_are_respected(self):
        """The submit endpoint allocates ids; the task must not mint new ones."""
        result = await tasks.run_investigation_local({
            "input": "ids@example.com",
            "inv_id": "my-inv-id",
            "case_id": "my-case-id",
        })
        assert result["inv_id"] == "my-inv-id"
        assert result["case_id"] == "my-case-id"

    @pytest.mark.asyncio
    async def test_steps_are_captured(self):
        """Step capture is what powers the WebSocket pipeline trace."""
        result = await tasks.run_investigation_local({"input": "steps@example.com"})
        assert result["steps"], "no steps were recorded"
        assert any("APEX" in s for s in result["steps"])

    @pytest.mark.asyncio
    async def test_emit_callback_receives_steps_live(self):
        """The streaming hook must fire as agents run, not just at the end."""
        seen: list[str] = []

        async def _emit(message: str) -> None:
            seen.append(message)

        await tasks.run_investigation_local({"input": "stream@example.com", "emit": _emit})
        assert seen, "the emit callback was never called"

    @pytest.mark.asyncio
    async def test_a_failing_emit_callback_does_not_kill_the_run(self):
        """A dead WebSocket client must never break an investigation."""

        async def _broken(_message: str) -> None:
            raise ConnectionResetError("client gone")

        result = await tasks.run_investigation_local({
            "input": "deadws@example.com", "emit": _broken,
        })
        assert result["status"] == "done", \
            "a broken emitter must not fail the investigation"

    @pytest.mark.asyncio
    async def test_input_type_override_is_honoured(self):
        result = await tasks.run_investigation_local({
            "input": "198.51.100.7", "input_type": "ip_address",
        })
        assert result["status"] == "done"
        assert result["input_type"] == "ip_address"

    @pytest.mark.asyncio
    async def test_survives_a_garbage_input(self):
        """Unroutable input must degrade to a result, never raise."""
        result = await tasks.run_investigation_local({"input": "x"})
        assert result["status"] in ("done", "error")
        json.dumps(result)

    @pytest.mark.asyncio
    async def test_sentinel_scan_local_registers_a_snapshot(self):
        scan = await tasks.sentinel_scan_local("case-scan", "monitored@example.com")
        assert scan["status"] == "done"
        assert scan["agent"] == "SENTINEL"
        assert scan["output"]["monitoring"] is True
        assert scan["output"]["baseline"] is True

    @pytest.mark.asyncio
    async def test_sentinel_second_scan_diffs_against_the_baseline(self):
        """The monitoring value is the diff, so a repeat scan must not re-baseline."""
        await tasks.sentinel_scan_local("case-diff", "watched@example.com")
        second = await tasks.sentinel_scan_local("case-diff", "watched@example.com")
        assert second["output"]["baseline"] is False
        assert second["output"]["snapshot"]["checks"] == 2

    @pytest.mark.asyncio
    async def test_build_report_local_returns_the_stored_report(self):
        run = await tasks.run_investigation_local({"input": "report@example.com"})
        built = await tasks.build_report_local(run["inv_id"], "markdown")
        assert built["status"] == "done"
        assert "Intelligence Report" in built["markdown"]
        assert built["report_id"], "the report was not persisted under an id"

    @pytest.mark.asyncio
    async def test_build_report_local_handles_an_unknown_id(self):
        built = await tasks.build_report_local("no-such-inv", "markdown")
        assert built["status"] == "error"
        assert built["error"]


# ═══════════════════════════════════════════════════════════════════════════
# celery_app.py
# ═══════════════════════════════════════════════════════════════════════════


class TestCeleryApp:
    """Import-time safety and task configuration."""

    def test_module_imports_cleanly_with_celery_disabled(self):
        """The zero-config guarantee: no broker configured must not raise."""
        import celery_app

        assert celery_app.celery_status()["available"] in (True, False)
        json.dumps(celery_app.celery_status())

    def test_celery_status_masks_credentials(self):
        import celery_app

        assert "hunter2" not in celery_app._mask_dsn("amqp://u:hunter2@h:5672//")

    def test_configured_app_has_the_contract_settings(self):
        """CONTRACTS.md §9 pins these values; a regression here is silent."""
        app = tasks.celery_app
        if app is None:  # pragma: no cover - env-dependent
            pytest.skip("celery app not built in this environment")
        conf = app.conf
        assert conf.task_serializer == "json"
        assert conf.result_serializer == "json"
        assert "json" in conf.accept_content
        assert conf.task_acks_late is True
        assert conf.worker_prefetch_multiplier == 1
        assert conf.task_time_limit == 600
        assert conf.task_soft_time_limit == 540
        assert conf.timezone == "UTC"

    def test_soft_limit_precedes_hard_limit(self):
        """The gap is the cleanup window — it must never be inverted."""
        app = tasks.celery_app
        if app is None:  # pragma: no cover
            pytest.skip("celery app not built in this environment")
        assert app.conf.task_soft_time_limit < app.conf.task_time_limit

    def test_task_names_are_registered_on_the_app(self):
        app = tasks.celery_app
        if app is None:  # pragma: no cover
            pytest.skip("celery app not built in this environment")
        for name in ("signal_os.investigate", "signal_os.sentinel_scan",
                     "signal_os.report"):
            assert name in app.tasks, f"{name} was never registered"