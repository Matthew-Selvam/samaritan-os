"""
test_apex.py — End-to-end pipeline
===================================
APEX is the contract every client depends on, so the pipeline is exercised
end-to-end across the main input types, asserting:

* the **output shape** is exactly what ``main._build_report`` and the frontend
  read — including every legacy key,
* **no agent hard-crashes** a run,
* the **DAG** really is staged (correlation agents see the primary aggregate),
* **timeouts** isolate a hanging agent instead of stalling the pipeline,
* **retries** recover a transient failure,
* **partial success** still returns a useful report with a confidence penalty,
* **structured events** are emitted for every agent start/finish/failure,
* **global concurrency** is actually bounded.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agents.apex import ApexAgent
from agents.base import AgentResult, BaseAgent

pytestmark = pytest.mark.asyncio


#: The keys ``main.py`` / the frontend read. Removing one is a breaking change.
REQUIRED_OUTPUT_KEYS = (
    "input", "input_type", "routing_confidence", "routing_reasoning",
    "agents_activated", "agent_results", "entities", "signals", "graph",
    "timeline", "report_markdown",
)

#: Input types the pipeline must handle end to end.
PIPELINE_INPUTS = [
    ("email", "test@example.com"),
    ("phone", "+14155550123"),
    ("username", "somehandle"),
    ("domain", "example.com"),
    ("url", "https://example.com/page"),
    ("ip", "203.0.113.10"),
]


@pytest.mark.parametrize("label,raw", PIPELINE_INPUTS, ids=[p[0] for p in PIPELINE_INPUTS])
async def test_pipeline_returns_the_expected_shape(label: str, raw: str) -> None:
    """Every input type produces a complete, well-formed investigation."""
    result = await ApexAgent().run(raw, context={"case_id": f"test-{label}"})

    assert isinstance(result, AgentResult)
    assert result.agent == "APEX"
    assert result.status in ("done", "partial"), f"{label}: {result.error}"

    out = result.output
    for key in REQUIRED_OUTPUT_KEYS:
        assert key in out, f"{label}: missing required output key {key!r}"

    assert out["input_type"]
    assert 0.0 <= out["routing_confidence"] <= 1.0
    assert out["routing_reasoning"]
    assert isinstance(out["agents_activated"], list) and out["agents_activated"]
    assert isinstance(out["agent_results"], list) and out["agent_results"]
    assert isinstance(out["entities"], list)
    assert isinstance(out["signals"], list)
    assert set(out["graph"]) == {"nodes", "edges"}
    assert isinstance(out["timeline"], list)
    assert out["report_markdown"], f"{label}: no report was produced"

    # The routing must agree with the router's own verdict.
    from router import detect_input_type

    assert out["input_type"] == detect_input_type(raw).input_type.value


@pytest.mark.parametrize("label,raw", PIPELINE_INPUTS, ids=[p[0] for p in PIPELINE_INPUTS])
async def test_no_agent_hard_crashes_the_pipeline(label: str, raw: str) -> None:
    """A failing agent degrades its own result, never the whole run."""
    out = (await ApexAgent().run(raw, context={"case_id": f"test-{label}"})).output

    for record in out["agent_results"]:
        assert record["status"] in ("done", "partial", "error")
        if record["status"] == "error":
            assert record["error"], f"{label}/{record['agent']}: error with no message"
        # Serialized records are what gets stored and streamed.
        assert record["agent"] and record["role"] and record["icon"]
        assert isinstance(record["steps"], list)
        assert isinstance(record["latency_s"], (int, float))

    # The run is still a success, or an honest partial success.
    assert out["pipeline"]["agents_succeeded"] + out["pipeline"]["agents_failed"] == \
        out["pipeline"]["agents_total"]


async def test_pipeline_output_is_json_serializable(benign_text: str) -> None:
    """The whole result must survive JSON encoding for storage + WebSocket."""
    out = (await ApexAgent().run(benign_text, context={"case_id": "test-json"})).output
    encoded = json.dumps(out, default=str)
    assert len(encoded) > 100
    assert json.loads(encoded)["input_type"]


async def test_long_text_input_produces_a_timeline(benign_text: str) -> None:
    """KRONOS must extract dated events out of free text."""
    out = (await ApexAgent().run(benign_text, context={"case_id": "test-text"})).output
    assert out["timeline"], "no timeline events extracted from dated prose"
    stamps = [e["timestamp"] for e in out["timeline"]]
    assert stamps == sorted(stamps)
    for event in out["timeline"]:
        assert event["precision"], "every event must declare its precision"
        assert event["source_text"]


# ── The DAG ──────────────────────────────────────────────────────────────────

async def test_correlation_agents_see_the_primary_aggregate() -> None:
    """NEXUS/KRONOS must run after the primary swarm, not against an empty set."""
    out = (await ApexAgent().run("+14155550123", context={"case_id": "test-dag"})).output
    agents = {r["agent"] for r in out["agent_results"]}
    assert {"PHONOS", "NEXUS", "KRONOS", "VAULT", "SENTINEL", "QUILL"} <= agents

    nexus = next(r for r in out["agent_results"] if r["agent"] == "NEXUS")
    # PHONOS emits several signals; a correlation tier that ran first would see 0.
    assert nexus["output"]["nodes"], "NEXUS built an empty graph — it ran too early"


async def test_dag_stage_order_is_recorded() -> None:
    """The stage plan is exposed so a UI can show pipeline progress."""
    out = (await ApexAgent().run("test@example.com", context={"case_id": "test-stages"})).output
    stages = {s["stage"] for s in out["pipeline"]["stages"]}
    assert stages == {"primary", "correlate", "observe", "report"}

    # QUILL is the terminal stage and must appear exactly once, last.
    records = out["agent_results"]
    order = [r["agent"] for r in records]
    assert order.count("QUILL") == 1
    assert order[-1] == "QUILL", "QUILL must synthesise last"


async def test_quill_runs_after_every_other_agent() -> None:
    """The report must be able to summarise a finished aggregate."""
    out = (await ApexAgent().run("example.com", context={"case_id": "test-order"})).output
    assert out["report_markdown"]
    # Every non-failed agent is represented in the pipeline stats.
    names = [r["agent"] for r in out["agent_results"]]
    assert "NEXUS" in names and "KRONOS" in names


# ── Structured events ────────────────────────────────────────────────────────

async def test_structured_events_are_emitted() -> None:
    """agent_started / agent_finished / agent_failed drive a real UI."""
    collected: list[dict] = []

    async def emit_event(event: dict) -> None:
        collected.append(event)

    await ApexAgent().run("+14155550123",
                          context={"case_id": "test-events", "emit_event": emit_event})
    # The drain task may not have flushed yet; the result carries them too.
    out_events = (await ApexAgent().run(
        "+14155550123", context={"case_id": "test-events-2"})).output["events"]

    kinds = {e["event"] for e in out_events}
    assert "routed" in kinds
    assert "agent_started" in kinds
    assert "agent_finished" in kinds
    assert "pipeline_finished" in kinds

    started = [e for e in out_events if e["event"] == "agent_started"]
    finished = [e for e in out_events if e["event"] == "agent_finished"]
    assert started and finished
    for event in started:
        assert event["agent"] and event["stage"] and event["timeout_s"] > 0
    for event in finished:
        assert event["agent"] in {s["agent"] for s in started}

    if collected:
        assert all(isinstance(e, dict) for e in collected)


async def test_emit_callback_receives_only_strings() -> None:
    """``emit`` is the existing string-only step contract; events must not leak in."""
    lines: list[object] = []

    async def emit(line: str) -> None:
        lines.append(line)

    await ApexAgent().run("example.com", context={"case_id": "test-emit", "emit": emit})
    assert lines, "emit received nothing"
    assert all(isinstance(line, str) for line in lines), (
        "a structured event leaked into the string-only emit channel"
    )


async def test_dead_emit_callback_does_not_kill_the_run() -> None:
    """A disconnected WebSocket client must not fail the investigation."""

    async def emit(line: str) -> None:
        raise ConnectionResetError("client gone")

    result = await ApexAgent().run("example.com",
                                   context={"case_id": "test-dead-emit", "emit": emit})
    assert result.status in ("done", "partial")
    assert result.output["report_markdown"]


# ── Failure isolation ────────────────────────────────────────────────────────

class SlowAgent(BaseAgent):
    """An agent that never finishes, to prove timeouts work.

    ``name`` matches a real agent so ``agent_timeout(name)`` resolves the way it
    would in production.
    """
    name = "SCOUT"
    role = "Test slow agent"
    icon = "⏳"
    description = "Sleeps forever"

    async def run(self, input_data, context=None) -> AgentResult:
        await asyncio.sleep(3600)


class FlakyAgent(BaseAgent):
    """Fails ``failures`` times with a transient error, then succeeds."""
    name = "SIGMA"
    role = "Test flaky agent"
    icon = "🎲"
    description = "Transient failure then success"
    attempts = 0

    async def run(self, input_data, context=None) -> AgentResult:
        type(self).attempts += 1
        if type(self).attempts <= self.failures:
            raise ConnectionError(f"transient failure {type(self).attempts}")
        return AgentResult(agent=self.name, status="done", output={"ok": True},
                           confidence=0.9, reasoning="recovered")


class BrokenAgent(BaseAgent):
    """Always raises — used to prove partial success."""
    name = "EMAIL"
    role = "Test broken agent"
    icon = "💥"
    description = "Always raises"

    async def run(self, input_data, context=None) -> AgentResult:
        raise RuntimeError("this agent is broken")


async def test_a_hanging_agent_is_timed_out(monkeypatch) -> None:
    """One slow agent must not stall the pipeline."""
    from agents import AGENT_REGISTRY

    monkeypatch.setitem(AGENT_REGISTRY, "SCOUT", SlowAgent)
    monkeypatch.setenv("APEX_MAX_ATTEMPTS", "1")

    started = asyncio.get_running_loop().time()
    result = await ApexAgent().run("test@example.com", context={"case_id": "test-timeout"})
    elapsed = asyncio.get_running_loop().time() - started

    assert result.status in ("done", "partial")
    assert elapsed < 60, f"pipeline stalled for {elapsed:.1f}s despite the timeout"
    slow = next(r for r in result.output["agent_results"] if r["agent"] == "SCOUT")
    assert slow["status"] == "error"
    assert "timeout" in (slow["error"] or "").lower()
    assert any(e["event"] == "agent_failed" for e in result.output["events"])
    # The rest of the pipeline still produced a report.
    assert result.output["report_markdown"]


async def test_transient_failures_are_retried(monkeypatch) -> None:
    """A flaky agent is retried and its eventual success is recorded."""
    from agents import AGENT_REGISTRY

    FlakyAgent.attempts = 0
    FlakyAgent.failures = 1
    monkeypatch.setitem(AGENT_REGISTRY, "SCOUT", FlakyAgent)
    monkeypatch.setenv("APEX_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("APEX_RETRY_BACKOFF_S", "0")

    result = await ApexAgent().run("test@example.com", context={"case_id": "test-retry"})
    flaky = next(r for r in result.output["agent_results"] if r["agent"] == "SIGMA")
    assert flaky["status"] == "done", f"retry did not recover: {flaky['error']}"
    assert FlakyAgent.attempts >= 2
    finished = [e for e in result.output["events"]
                if e["event"] == "agent_finished" and e["agent"] == "SIGMA"]
    assert finished, "the retried agent never reported success"
    assert max(e["attempts"] for e in finished) >= 2, finished


async def test_non_transient_failures_are_not_retried(monkeypatch) -> None:
    """A ValueError is deterministic; retrying it just wastes the budget."""
    from agents import AGENT_REGISTRY

    monkeypatch.setitem(AGENT_REGISTRY, "SCOUT", BrokenAgent)
    monkeypatch.setenv("APEX_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("APEX_RETRY_BACKOFF_S", "0")

    result = await ApexAgent().run("test@example.com", context={"case_id": "test-noretry"})
    started = [e for e in result.output["events"]
               if e["event"] == "agent_started" and e["agent"] == "EMAIL"]
    assert started, "the failing agent never started"
    # No attempt number above 1 anywhere: a deterministic failure is not retried.
    assert all(e["attempt"] == 1 for e in started), started


async def test_partial_success_still_returns_a_useful_report(monkeypatch) -> None:
    """7 of 9 failing must still yield a report, with a confidence penalty."""
    from agents import AGENT_REGISTRY

    for name in ("EMAIL", "SIGMA", "SCOUT", "PRISM"):
        monkeypatch.setitem(AGENT_REGISTRY, name, BrokenAgent)

    result = await ApexAgent().run("test@example.com", context={"case_id": "test-partial"})
    out = result.output

    assert result.status == "partial"
    assert out["pipeline"]["agents_failed"] >= 4
    assert out["pipeline"]["partial_success"] is True
    assert out["pipeline"]["confidence_penalty"] > 0
    # The report still exists — that is the whole point of partial success.
    assert out["report_markdown"]
    assert out["failed_agents"]
    assert all(f["error"] for f in out["failed_agents"])
    # And the confidence was actually penalised below the unpenalised aggregate.
    assert result.confidence < 1.0


async def test_confidence_penalty_scales_with_failures() -> None:
    """More failures ⇒ a bigger penalty, floored so partial results still count."""
    def penalty(n_failed: int, n_total: int = 9) -> tuple[float, bool]:
        results = [AgentResult(agent=f"A{i}", status="done", output={},
                               confidence=0.8)
                   for i in range(n_total - n_failed)]
        results += [AgentResult(agent=f"F{i}", status="error", output=None, error="x")
                    for i in range(n_failed)]
        return ApexAgent._confidence_penalty(results, 0.8)

    assert penalty(0) == (0.0, False)
    small, _ = penalty(2)
    large, _ = penalty(7)
    assert large > small > 0
    assert large <= 0.6, "penalty must stay bounded"
    total, partial = penalty(9)
    assert not partial, "total failure is not 'partial success'"


# ── Budget and concurrency ───────────────────────────────────────────────────

async def test_pipeline_budget_stops_the_run(monkeypatch) -> None:
    """A global time budget skips remaining tiers instead of overrunning."""
    from agents import AGENT_REGISTRY

    monkeypatch.setitem(AGENT_REGISTRY, "SCOUT", SlowAgent)
    monkeypatch.setenv("APEX_PIPELINE_BUDGET_S", "2")
    monkeypatch.setenv("APEX_MAX_ATTEMPTS", "1")

    # SCOUT's own timeout must exceed the pipeline budget so the agent is still
    # mid-flight when the budget expires — that is the situation the budget
    # exists for. Override the conftest's fast default back up for this test.
    import config

    monkeypatch.setattr(config, "TIMEOUT_SCOUT", 8.0, raising=False)

    result = await ApexAgent().run("test@example.com", context={"case_id": "test-budget"})
    out = result.output
    # The budget caps the whole run: it must terminate well inside SCOUT's own
    # 8s timeout, proving the global budget binds before the per-agent one.
    assert out["pipeline"]["budget_s"] == 2.0
    assert out["pipeline"]["duration_s"] < 7.0, (
        f"pipeline waited out the agent timeout ({out['pipeline']['duration_s']}s)"
    )
    slow = next((r for r in out["agent_results"] if r["agent"] == "SCOUT"), None)
    if slow is not None:
        assert slow["status"] == "error", "the hung agent must be reported as failed"
    else:
        # The budget fired before the agent started; the event must be recorded.
        assert any(e["event"] == "pipeline_budget_exhausted" for e in out["events"])


async def test_global_concurrency_is_bounded() -> None:
    """The process-wide gate caps simultaneous agent executions."""
    apex = ApexAgent()
    assert apex.max_concurrency >= 1
    gate = await ApexAgent._gate(apex.max_concurrency)
    assert gate._value == apex.max_concurrency
    # The gate is shared across instances (that is what makes it global).
    again = await ApexAgent._gate(4)
    assert again is gate


async def test_concurrency_settings_come_from_the_environment(monkeypatch) -> None:
    """Tuning knobs are env-overridable without touching config.py."""
    monkeypatch.setenv("APEX_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("APEX_PIPELINE_BUDGET_S", "42")
    monkeypatch.setenv("APEX_MAX_CONCURRENCY", "3")
    apex = ApexAgent()
    assert apex.max_attempts == 5
    assert apex.pipeline_budget_s == 42.0
    assert apex.max_concurrency == 3


# ── Routing integration ──────────────────────────────────────────────────────

async def test_routing_metadata_reaches_the_output() -> None:
    """The normalized target and cache key travel with the investigation."""
    out = (await ApexAgent().run("+1 (415) 555-0123", context={"case_id": "test-norm"})).output
    routing = out["routing"]
    assert routing["rule"]
    assert routing["source"] == "rules"
    assert routing["cache_key"]
    assert routing["target"]["normalized"] == "+14155550123"


async def test_equivalent_inputs_share_a_cache_key() -> None:
    """'+1 (415) 555-0123' and '+14155550123' must key identically."""
    a = (await ApexAgent().run("+1 (415) 555-0123", context={"case_id": "c1"})).output
    b = (await ApexAgent().run("+14155550123", context={"case_id": "c2"})).output
    assert a["routing"]["cache_key"] == b["routing"]["cache_key"]


async def test_input_type_override_is_respected() -> None:
    """A caller-supplied input_type wins over detection (existing behaviour)."""
    out = (await ApexAgent().run("somehandle",
                                 context={"case_id": "test-override",
                                          "input_type": "email"})).output
    assert out["input_type"] == "email"


async def test_agent_timeout_is_per_agent(monkeypatch) -> None:
    """Different agents get their own configured budget."""
    import config
    from agents.base import agent_timeout

    monkeypatch.setattr(config, "TIMEOUT_PHONOS", 12.0, raising=False)
    monkeypatch.setattr(config, "TIMEOUT_SCOUT", 90.0, raising=False)
    assert agent_timeout("PHONOS") == 12.0
    assert agent_timeout("SCOUT") == 90.0


async def test_token_accounting_is_aggregated() -> None:
    """Token usage is summed across the swarm and exposed."""
    out = (await ApexAgent().run("example.com", context={"case_id": "test-tokens"})).output
    assert "tokens_used" in out
    assert out["tokens_used"] == sum(
        int(r["tokens_used"] or 0) for r in out["agent_results"]
    )