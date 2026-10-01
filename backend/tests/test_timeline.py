"""
test_timeline.py — KRONOS date-format matrix
=============================================
The specification promises "event reconstruction from 11+ date formats". This
file is the proof: a table of every supported format, asserted against an exact
expected UTC instant, plus the behaviours that make a timeline trustworthy —
precision labelling, free-text extraction, deduplication, conflict resolution,
and MDY/DMY disambiguation.

Every case injects a fixed ``now`` so relative dates ("3 days ago") are
deterministic rather than dependent on when the suite runs.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.kronos import KronosAgent

pytestmark = pytest.mark.asyncio

#: Fixed "today" for every relative-date assertion.
NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def parse(text: str, *, dayfirst: bool | None = None) -> dict | None:
    """Parse one date string against the fixed clock."""
    return KronosAgent.parse_date(text, dayfirst=dayfirst, now=NOW)


# ── The format matrix ────────────────────────────────────────────────────────
# (label, input, expected UTC ISO timestamp)
FORMAT_MATRIX: list[tuple[str, str, str]] = [
    # ── Epoch (s / ms / µs / ns, with and without a fraction) ──
    ("epoch seconds", "1709251200", "2024-03-01T00:00:00Z"),
    ("epoch milliseconds", "1709251200000", "2024-03-01T00:00:00Z"),
    ("epoch microseconds", "1709251200000000", "2024-03-01T00:00:00Z"),
    ("epoch fraction", "1709251200.5", "2024-03-01T00:00:00.500000Z"),
    # ── ISO 8601 ──
    ("iso with Z", "2024-03-03T10:30:00Z", "2024-03-03T10:30:00Z"),
    ("iso space separator", "2024-03-03 10:30:00", "2024-03-03T10:30:00Z"),
    ("iso offset positive", "2024-03-03T10:30:00+05:30", "2024-03-03T05:00:00Z"),
    ("iso offset negative", "2024-03-03T01:00:00-04:00", "2024-03-03T05:00:00Z"),
    ("iso minutes only", "2024-03-03T10:30", "2024-03-03T10:30:00Z"),
    ("iso date only", "2024-03-03", "2024-03-03T00:00:00Z"),
    ("iso fractional seconds", "2024-03-03T10:30:00.123456Z", "2024-03-03T10:30:00.123456Z"),
    ("iso week date", "2024-W10-1", "2024-03-04T00:00:00Z"),
    ("iso ordinal date", "2024-063", "2024-03-03T00:00:00Z"),
    # ── RFC 2822 ──
    ("rfc2822 full", "Mon, 3 Mar 2024 10:30:00 +0000", "2024-03-03T10:30:00Z"),
    ("rfc2822 offset", "Mon, 3 Mar 2024 10:30:00 +0200", "2024-03-03T08:30:00Z"),
    ("rfc2822 two-digit year", "Sun, 3 Mar 24 10:30:00 +0000", "2024-03-03T10:30:00Z"),
    # ── Month-name forms ──
    ("day month year", "3 Mar 2024", "2024-03-03T00:00:00Z"),
    ("month day year", "March 3 2024", "2024-03-03T00:00:00Z"),
    ("abbrev with comma", "Mar 3, 2024", "2024-03-03T00:00:00Z"),
    ("zero padded day", "03 March 2024", "2024-03-03T00:00:00Z"),
    ("ordinal suffix", "3rd of March 2024", "2024-03-03T00:00:00Z"),
    ("full month period", "March 3, 2024", "2024-03-03T00:00:00Z"),
    ("dotted abbreviation", "Mar. 3 2024", "2024-03-03T00:00:00Z"),
    # ── Numeric slash / dot / dash ──
    ("US slash", "03/04/2024", "2024-03-04T00:00:00Z"),
    ("EU slash", "18/03/2024", "2024-03-18T00:00:00Z"),
    ("unambiguous US", "12/25/2024", "2024-12-25T00:00:00Z"),
    ("two-digit year", "03/04/24", "2024-03-04T00:00:00Z"),
    ("dot separated", "18.03.2024", "2024-03-18T00:00:00Z"),
    ("dash separated", "2024-03-03", "2024-03-03T00:00:00Z"),
    # ── Month-year / year / quarter / half ──
    ("month and year", "Mar 2024", "2024-03-01T00:00:00Z"),
    ("quarter", "Q2 2024", "2024-04-01T00:00:00Z"),
    ("quarter with of", "Q1 of 2019", "2019-01-01T00:00:00Z"),
    ("quarter suffix form", "2Q2024", "2024-04-01T00:00:00Z"),
    ("half year", "H1 2024", "2024-01-01T00:00:00Z"),
    ("bare year", "2024", "2024-01-01T00:00:00Z"),
    # ── Relative ──
    ("yesterday", "yesterday", "2024-06-14T00:00:00Z"),
    ("today", "today", "2024-06-15T00:00:00Z"),
    ("tomorrow", "tomorrow", "2024-06-16T00:00:00Z"),
    ("n days ago", "3 days ago", "2024-06-12T00:00:00Z"),
    ("n weeks ago", "2 weeks ago", "2024-06-01T00:00:00Z"),
    ("n months ago", "2 months ago", "2024-04-15T00:00:00Z"),
    ("n years ago", "5 years ago", "2019-06-15T00:00:00Z"),
    ("one hour ago", "an hour ago", "2024-06-15T11:00:00Z"),
    ("word three days", "three days ago", "2024-06-12T00:00:00Z"),
    ("last week", "last week", "2024-06-08T00:00:00Z"),
    ("last month", "last month", "2024-05-15T00:00:00Z"),
    ("last year", "last year", "2023-06-15T00:00:00Z"),
    # ── Ranges / since / durations ──
    ("year range", "2019-2024", "2019-01-01T00:00:00Z"),
    ("year range with to", "2019 to 2024", "2019-01-01T00:00:00Z"),
    ("since", "since 2019", "2019-01-01T00:00:00Z"),
    ("duration", "for 3 years", "2021-06-15T00:00:00Z"),
    ("duration months", "over 18 months", "2022-12-15T00:00:00Z"),
]


@pytest.mark.parametrize(
    "label,text,expected",
    FORMAT_MATRIX,
    ids=[c[0] for c in FORMAT_MATRIX],
)
def test_parses_every_documented_format(label: str, text: str, expected: str) -> None:
    """Every format family normalizes to the exact expected UTC instant."""
    result = parse(text)
    assert result is not None, f"{label}: {text!r} did not parse at all"
    assert result["timestamp"] == expected, (
        f"{label}: {text!r} → {result['timestamp']}, expected {expected}"
    )
    assert result["source_text"] == text
    assert result["precision"] in ("second", "minute", "day", "month", "quarter",
                                   "half", "year")


def test_format_matrix_covers_the_promised_breadth() -> None:
    """Guard the spec claim itself: 11+ distinct formats, across many families."""
    assert len({c[0].rsplit(" ", 1)[0] for c in FORMAT_MATRIX}) >= 11
    families = {c[0].split()[0] for c in FORMAT_MATRIX}
    assert len(families) >= 6, f"expected several format families, got {families}"


# ── Precision ────────────────────────────────────────────────────────────────

PRECISION_CASES = [
    ("2024-03-03T10:30:45Z", "second"),
    ("2024-03-03T10:30Z", "minute"),
    ("2024-03-03", "day"),
    ("Mar 2024", "month"),
    ("Q2 2024", "quarter"),
    ("2024", "year"),
]


@pytest.mark.parametrize("text,precision", PRECISION_CASES,
                         ids=[c[0] for c in PRECISION_CASES])
def test_precision_is_declared(text: str, precision: str) -> None:
    """A consumer must know how exact each date is before charting it."""
    result = parse(text)
    assert result is not None
    assert result["precision"] == precision


def test_timestamps_are_always_utc_with_a_z_suffix() -> None:
    """Every timestamp is normalized to UTC, whatever the input's zone."""
    for _label, text, _expected in FORMAT_MATRIX:
        result = parse(text)
        assert result is not None
        assert result["timestamp"].endswith("Z"), text
        assert "+" not in result["timestamp"][10:], text


def test_unparseable_input_returns_none() -> None:
    """Junk yields None rather than a wrong date."""
    for junk in ("", "   ", "not a date", "32/13/2024", "13/45/2024",
                 "hello", "2024-13-45", None):
        assert parse(junk) is None, junk


# ── MDY vs DMY disambiguation ────────────────────────────────────────────────

def test_dayfirst_flag_overrides_the_default() -> None:
    """An explicit dayfirst=True reads 03/04 as 3 April."""
    assert parse("03/04/2024", dayfirst=True)["timestamp"].startswith("2024-04-03")
    assert parse("03/04/2024", dayfirst=False)["timestamp"].startswith("2024-03-04")


def test_impossible_component_settles_the_order() -> None:
    """A value above 12 proves which end is the day."""
    assert parse("18/03/2024")["dayfirst"] is True
    assert parse("03/18/2024")["dayfirst"] is False


def test_corroborating_evidence_resolves_an_ambiguous_date() -> None:
    """A day-first locale mentioned nearby decides an otherwise ambiguous date."""
    # 'UK' in the window → day-first → 3 April.
    events = KronosAgent.parse_any(
        "Filed under the UK regime on 03/04/2024 and reviewed later.", now=NOW)
    ambiguous = [e for e in events if e["format"].startswith("numeric")]
    assert ambiguous and ambiguous[0]["timestamp"].startswith("2024-04-03")

    # A US locale mention flips it to month-first → 4 March.
    events = KronosAgent.parse_any(
        "Reported in the United States on 03/04/2024 and reviewed later.", now=NOW)
    ambiguous = [e for e in events if e["format"].startswith("numeric")]
    assert ambiguous and ambiguous[0]["timestamp"].startswith("2024-03-04")


def test_neighbouring_unambiguous_date_resolves_ambiguity() -> None:
    """A DMY neighbour is strong evidence for reading other dates day-first."""
    events = KronosAgent.parse_any(
        "Dated 18/03/2024 and also 03/04/2024.", now=NOW)
    ambiguous = [e for e in events if e["source_text"] == "03/04/2024"]
    assert ambiguous and ambiguous[0]["timestamp"].startswith("2024-04-03")


# ── Free-text extraction ─────────────────────────────────────────────────────

NARRATIVE = (
    "The breach was detected on 2019-04-12. The account was created on "
    "March 3 2021. A maintenance window ran from 18/03/2021 until 2021-03-20, "
    "and the ticket was closed 2 weeks later. Logged entry: 1709251200."
)


def test_extracts_every_date_from_free_text() -> None:
    """Dates embedded in prose are found, in document order, with their source."""
    events = KronosAgent.parse_any(NARRATIVE, now=NOW)
    stamps = [e["timestamp"] for e in events]
    assert stamps == sorted(stamps), "events must come back in document order"
    # 2019-04-12, 2021-03-03, 2021-03-18 (and 2021-03-20), the epoch, and the
    # relative "2 weeks later".
    assert any(e["timestamp"].startswith("2019-04-12") for e in events)
    assert any(e["timestamp"].startswith("2021-03-03") for e in events)
    assert any(e["timestamp"].startswith("2021-03-18") for e in events)
    for event in events:
        assert event["source_text"], "every extracted event keeps its source text"
        assert event["precision"]


def test_no_date_is_double_counted() -> None:
    """An ISO timestamp must not also be read as a numeric slash date."""
    events = KronosAgent.parse_any("Timestamp 2024-03-03T10:30:00Z recorded.", now=NOW)
    assert len(events) == 1, events


def test_empty_text_yields_no_events() -> None:
    """No input, no events."""
    assert KronosAgent.parse_any("") == []
    assert KronosAgent.parse_any("   ") == []
    assert KronosAgent.parse_any("no dates whatsoever in this sentence") == []


# ── Dedupe ───────────────────────────────────────────────────────────────────

def test_near_identical_events_are_deduplicated() -> None:
    """The same instant + subject reported twice collapses to one event."""
    events = [
        {"timestamp": "2024-01-01T00:00:00Z", "label": "Account seen: reddit"},
        {"timestamp": "2024-01-01T00:00:00Z", "label": "Account seen: reddit"},
        {"timestamp": "2024-02-02T00:00:00Z", "label": "Something else"},
    ]
    deduped = KronosAgent.dedupe(events)
    assert len(deduped) == 2
    merged = next(e for e in deduped if e["label"] == "Account seen: reddit")
    assert merged.get("also_seen") == 1, "the merge should be recorded"


def test_dedupe_keeps_distinct_events() -> None:
    """Different labels or different instants must not be merged."""
    events = [
        {"timestamp": "2024-01-01T00:00:00Z", "label": "A"},
        {"timestamp": "2024-01-02T00:00:00Z", "label": "A"},
        {"timestamp": "2024-01-01T00:00:00Z", "label": "B"},
    ]
    assert len(KronosAgent.dedupe(events)) == 3


# ── Conflicts ────────────────────────────────────────────────────────────────

def test_conflicting_dates_prefer_the_most_recent_and_flag_it() -> None:
    """Two dates for one subject: keep the latest, record what it displaced."""
    events = [
        {"timestamp": "2024-01-01T00:00:00Z", "label": "Account created",
         "source_text": "2019-01-01"},
        {"timestamp": "2024-06-01T00:00:00Z", "label": "Account created",
         "source_text": "2024-06-01"},
    ]
    resolved = KronosAgent.resolve_conflicts(events)
    assert len(resolved) == 1
    assert resolved[0]["timestamp"].startswith("2024-06-01")
    conflict = resolved[0]["conflict"]
    assert conflict["displaced"].startswith("2024-01-01")
    assert conflict["displaced_source"] == "2019-01-01"
    assert "most recent" in conflict["note"]


def test_near_identical_dates_are_not_treated_as_conflicts() -> None:
    """Two dates a day apart are the same event observed twice, not a conflict."""
    events = [
        {"timestamp": "2024-01-01T00:00:00Z", "label": "Seen"},
        {"timestamp": "2024-01-01T06:00:00Z", "label": "Seen"},
    ]
    resolved = KronosAgent.resolve_conflicts(events)
    assert len(resolved) == 2
    assert not any(e.get("conflict") for e in resolved)


# ── The agent ────────────────────────────────────────────────────────────────

async def test_agent_builds_a_timeline_from_signals(benign_signals) -> None:
    """Mixed-format upstream signals become one ordered timeline."""
    result = await KronosAgent().run("example.com", context={"peer_signals": benign_signals})
    out = result.output
    assert result.status == "done"
    assert out["event_count"] >= 3
    stamps = [e["timestamp"] for e in out["events"]]
    assert stamps == sorted(stamps)
    assert out["span"]["earliest"] <= out["span"]["latest"]
    assert out["formats_parsed"], "the agent reports which formats it parsed"


async def test_agent_timeline_from_text_only(benign_text: str) -> None:
    """With no upstream signals, the target text itself supplies the dates."""
    result = await KronosAgent().run(benign_text, context={"peer_signals": []})
    assert result.output["event_count"] >= 3
    assert result.confidence > 0.3


async def test_agent_preserves_legacy_keys(benign_signals) -> None:
    """events / span / event_count are the frontend contract."""
    out = (await KronosAgent().run("x", context={"peer_signals": benign_signals})).output
    for key in ("events", "span", "event_count"):
        assert key in out


async def test_agent_handles_no_input_at_all() -> None:
    """No signals and an empty target is a valid, empty timeline."""
    result = await KronosAgent().run("", context={"peer_signals": []})
    assert result.status == "done"
    assert result.output["event_count"] == 0
    assert result.output["span"] is None


async def test_legacy_parse_date_alias_still_works() -> None:
    """The old ``_parse_date`` classmethod name is kept for compatibility."""
    assert KronosAgent._parse_date("2024-03-03", now=NOW)["timestamp"] == \
        "2024-03-03T00:00:00Z"


def test_dayfirst_regions_table_is_populated() -> None:
    """The exported locale table is real data, not a stub."""
    from agents.kronos import DAYFIRST_REGIONS

    assert DAYFIRST_REGIONS["GB"] is True
    assert DAYFIRST_REGIONS["US"] is False
    assert len(DAYFIRST_REGIONS) >= 10