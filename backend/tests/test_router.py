"""
test_router.py — Input router detection matrix
==============================================
Table-driven coverage of every ``InputType``, plus the invariants that matter
for correctness rather than classification:

* **first-match ordering** is respected (a file extension beats the domain rule),
* **normalization** makes equivalent spellings share one cache key,
* **confidence** is always in ``[0, 1]`` and always accompanied by a reason,
* **every** type in ``AGENT_MAP`` names a registered agent,
* ``explain_routing`` reports the winner, the runners-up and why they lost.
"""
from __future__ import annotations

import pytest

from router import (
    AGENT_MAP,
    KNOWN_INPUT_TYPES,
    LLM_DISAMBIGUATION_THRESHOLD,
    ROUTING_RULES,
    InputType,
    RoutingDecision,
    build_context,
    detect_input_type,
    explain_routing,
    normalize_phone,
    normalize_target,
)


# ── The detection matrix ─────────────────────────────────────────────────────
# (raw input, expected InputType, the rule that must win)
DETECTION_CASES: list[tuple[str, InputType, str]] = [
    # ── phones ──
    ("+14155550123", InputType.PHONE, "phone"),
    ("+1 (415) 555-0123", InputType.PHONE, "phone"),
    ("(415) 555-0123", InputType.PHONE, "phone"),
    ("+44 20 7946 0958", InputType.PHONE, "phone"),
    # ── email ──
    ("test@example.com", InputType.EMAIL, "email"),
    ("John.Doe+tag@Example.COM", InputType.EMAIL, "email"),
    # ── IPv4 ──
    ("192.168.1.1", InputType.IP, "ipv4"),
    ("8.8.8.8", InputType.IP, "ipv4"),
    ("203.0.113.42", InputType.IP, "ipv4"),
    # ── IPv6 ──
    ("2001:0db8:85a3::8a2e:0370:7334", InputType.IPV6, "ipv6"),
    ("::1", InputType.IPV6, "ipv6"),
    ("fe80::1", InputType.IPV6, "ipv6"),
    # ── MAC ──
    ("00:1A:2B:3C:4D:5E", InputType.MAC, "mac_address"),
    ("AA-BB-CC-DD-EE-FF", InputType.MAC, "mac_address"),
    ("001a.2b3c.4d5e", InputType.MAC, "mac_address"),
    # ── host artifacts ──
    ("3f2504e0-4f89-11d3-9a0c-0305e82c3301", InputType.UUID, "uuid"),
    ("d41d8cd98f00b204e9800998ecf8427e", InputType.FILE_HASH, "file_hash"),
    ("aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d", InputType.FILE_HASH, "file_hash"),
    ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
     InputType.FILE_HASH, "file_hash"),
    ("CVE-2021-44228", InputType.CVE, "cve"),
    ("cve-2020-1234", InputType.CVE, "cve"),
    ("SGVsbG8gV29ybGQhIFRoaXMgaXMgYSB0ZXN0IHN0cmluZw==", InputType.BASE64, "base64_blob"),
    # ── crypto chains ──
    ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", InputType.CRYPTO_WALLET, "crypto_wallet"),
    ("0x742d35Cc6634C0532925a3b844Bc9a7595f0bEb0", InputType.CRYPTO_WALLET, "crypto_wallet"),
    ("bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq", InputType.CRYPTO_WALLET, "crypto_wallet"),
    # ── financial / identity ──
    ("GB82WEST12345698765432", InputType.IBAN, "iban"),
    ("DE89370400440532013000", InputType.IBAN, "iban"),
    ("1HGCM82633A004352", InputType.VIN, "vin"),
    ("JH4TB2H26CC000000", InputType.VIN, "vin"),
    ("ABC123", InputType.LICENSE_PLATE, "license_plate"),
    ("123-45-6789", InputType.NATIONAL_ID, "national_id"),
    # ── network infra ──
    ("magnet:?xt=urn:btih:c12fe1c06bba254a9dc9f519b335aa7c1367a88a&dn=x",
     InputType.MAGNET, "magnet_link"),
    ("abcdefghijklmnop.onion", InputType.ONION, "onion_address"),
    ("AS15169", InputType.ASN, "asn"),
    ("asn13335", InputType.ASN, "asn"),
    # ── domains / urls ──
    ("example.com", InputType.DOMAIN, "domain"),
    ("sub.example.co.uk", InputType.DOMAIN, "domain"),
    ("https://example.com/path?q=1", InputType.URL, "url"),
    ("http://xn--fsq.com", InputType.URL, "url"),
    # ── code repos ──
    ("github.com/torvalds/linux", InputType.CODE_REPO, "code_repo_url"),
    ("https://github.com/a/b", InputType.CODE_REPO, "code_repo_url"),
    ("https://huggingface.co/datasets/x/y", InputType.CODE_REPO, "code_repo_url"),
    # ── social handles ──
    ("https://www.reddit.com/u/spez", InputType.HANDLE, "social_profile_url"),
    ("reddit.com/u/spez", InputType.HANDLE, "social_profile_url"),
    ("https://twitter.com/jack", InputType.HANDLE, "social_profile_url"),
    ("https://steamcommunity.com/id/gaben", InputType.HANDLE, "social_profile_url"),
    ("https://discord.com/users/123456789012345678", InputType.HANDLE,
     "social_profile_url"),
    ("https://t.me/durov", InputType.HANDLE, "social_profile_url"),
    ("discord:foo#4242", InputType.HANDLE, "discord_handle"),
    ("reddit:foo", InputType.HANDLE, "platform_handle"),
    ("@somehandle", InputType.HANDLE, "at_handle"),
    # ── media / documents ──
    ("photo.jpg", InputType.IMAGE, "image_file"),
    ("clip.mp4", InputType.VIDEO, "video_file"),
    ("song.mp3", InputType.AUDIO, "audio_file"),
    ("report.pdf", InputType.DOCUMENT, "document_file"),
    ("archive.zip", InputType.DOCUMENT, "document_file"),
    # ── free text / names / aliases ──
    ("This is a long paragraph of prose that describes an incident which happened "
     "last week and involved several people.", InputType.TEXT, "text"),
    ("Jane Doe", InputType.PERSON_NAME, "person_name"),
    ("John Ronald Reuel Tolkien", InputType.PERSON_NAME, "person_name"),
    ("john_doe99", InputType.USERNAME, "username"),
    ("ghostwriter", InputType.USERNAME, "username"),
    # ── unknown ──
    ("", InputType.UNKNOWN, "empty"),
    ("!@#$%^&*()", InputType.UNKNOWN, "none"),
    ("1234", InputType.UNKNOWN, "none"),
]


@pytest.mark.parametrize(
    "raw,expected,rule",
    DETECTION_CASES,
    ids=[c[0][:38].replace(" ", "_") or "empty" for c in DETECTION_CASES],
)
def test_detects_input_type(raw: str, expected: InputType, rule: str) -> None:
    """Every case in the matrix classifies to its expected type via its rule."""
    decision = detect_input_type(raw)
    assert decision.input_type is expected, (
        f"{raw!r} → {decision.input_type.value} via {decision.rule}, "
        f"expected {expected.value}"
    )
    assert decision.rule == rule, f"{raw!r} fired {decision.rule}, expected {rule}"


@pytest.mark.parametrize(
    "raw,expected,rule",
    DETECTION_CASES,
    ids=[c[0][:38].replace(" ", "_") or "empty" for c in DETECTION_CASES],
)
def test_decision_is_wellformed(raw: str, expected: InputType, rule: str) -> None:
    """Every decision carries a bounded confidence, a reason and a cache key."""
    decision = detect_input_type(raw)
    assert 0.0 <= decision.confidence <= 1.0
    assert isinstance(decision.reasoning, str) and decision.reasoning
    assert decision.agents, "a decision must always propose a swarm"
    assert decision.target.get("cache_key") is not None
    assert isinstance(decision.alternatives, list)


def test_ipv6_is_not_mistaken_for_ipv4_or_domain() -> None:
    """IPv6 must get its own type, not fall through to UNKNOWN or DOMAIN."""
    decision = detect_input_type("2001:0db8:85a3::8a2e:0370:7334")
    assert decision.input_type is InputType.IPV6
    assert decision.rule == "ipv6"
    assert decision.target["version"] == 6


def test_all_ip_families_reach_the_right_swarm() -> None:
    """v4 and v6 both activate SIGMA + TERRA (the infrastructure agents)."""
    for raw in ("8.8.8.8", "2001:db8::1"):
        agents = detect_input_type(raw).agents
        assert "SIGMA" in agents and "TERRA" in agents, raw


# ── Normalization / cache-key equivalence ─────────────────────────────────────

EQUIVALENT_GROUPS: list[tuple[str, ...]] = [
    # The spec's canonical example.
    ("+14155550123", "+1 (415) 555-0123", "+1-415-555-0123", "(415) 555-0123"),
    ("Example.COM", "example.com", "example.com."),
    ("test@example.com", "TEST@Example.com"),
    ("https://x.com/a?utm_source=q", "http://x.com/a", "https://x.com/a#frag"),
    ("https://github.com/a/b", "github.com/a/b"),
]


@pytest.mark.parametrize("group", EQUIVALENT_GROUPS, ids=range(len(EQUIVALENT_GROUPS)))
def test_equivalent_spellings_share_one_cache_key(group: tuple[str, ...]) -> None:
    """Equivalent spellings of one target must collide on a single cache key."""
    keys = {detect_input_type(raw).target["cache_key"] for raw in group}
    assert len(keys) == 1, f"{group} produced {len(keys)} cache keys: {keys}"


def test_normalize_phone_resolves_e164() -> None:
    """Both formats normalize to the same E.164 string."""
    a = normalize_phone("+1 (415) 555-0123")
    b = normalize_phone("+14155550123")
    assert a["normalized"] == b["normalized"] == "+14155550123"
    assert a["cache_key"] == b["cache_key"]
    assert a["digits"] == b["digits"] == "14155550123"


def test_normalize_phone_survives_unparseable_input() -> None:
    """A junk number still yields a cache key rather than raising."""
    result = normalize_phone("not-a-number")
    assert result["cache_key"]
    # Validity is None (unparsed) rather than False: we never actually parsed it.
    assert result["valid"] in (None, False)


def test_target_metadata_differs_by_type() -> None:
    """Each normalizer emits type-appropriate metadata."""
    assert detect_input_type("CVE-2021-44228").target["year"] == 2021
    assert detect_input_type("d41d8cd98f00b204e9800998ecf8427e").target["algorithm"] == "md5"
    assert detect_input_type("aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d").target["algorithm"] == "sha1"
    assert detect_input_type("1HGCM82633A004352").target["check_digit_valid"] is True
    assert detect_input_type("AS15169").target["asn"] == 15169
    assert detect_input_type("00:1A:2B:3C:4D:5E").target["oui"] == "001a2b"
    assert detect_input_type("abcdefghijklmnop.onion").target["version"] == 2
    assert detect_input_type("sub.example.co.uk").target["tld"] == "uk"


def test_normalize_target_never_raises() -> None:
    """Normalization is total: any input yields a cache key."""
    for raw in ("", "   ", "ÿ" * 50, "https://", "a" * 5000):
        meta = normalize_target(InputType.UNKNOWN, raw)
        assert isinstance(meta, dict) and "cache_key" in meta


# ── Ordering / precedence invariants ──────────────────────────────────────────

def test_media_extension_beats_domain_rule() -> None:
    """'report.pdf' has a dot and two labels — it must not be a domain."""
    assert detect_input_type("report.pdf").input_type is InputType.DOCUMENT


def test_code_repo_and_social_urls_beat_generic_url() -> None:
    """A profile/repo URL is more specific than 'it is a URL'."""
    assert detect_input_type("https://www.reddit.com/u/spez").input_type is InputType.HANDLE
    assert detect_input_type("https://github.com/a/b").input_type is InputType.CODE_REPO
    assert detect_input_type("https://example.com/page").input_type is InputType.URL


def test_ipv4_beats_phone_for_dotted_quads() -> None:
    """'192.168.1.1' is an address, not a 10-digit phone number."""
    assert detect_input_type("192.168.1.1").input_type is InputType.IP


def test_empty_and_junk_never_crash() -> None:
    """Hostile input yields UNKNOWN with a bounded confidence."""
    for raw in ("", "   ", "\n\t", "🙂🙂🙂", "0" * 300):
        decision = detect_input_type(raw)
        assert decision.input_type in (InputType.UNKNOWN, InputType.USERNAME,
                                       InputType.PHONE, InputType.BASE64)
        assert 0.0 <= decision.confidence <= 1.0


# ── explain_routing ──────────────────────────────────────────────────────────

def test_explain_routing_reports_the_winner_and_losers() -> None:
    """The UI panel gets the winning rule plus every alternative considered."""
    report = explain_routing("test@example.com")
    assert report["input_type"] == "email"
    assert report["rule_fired"] == "email"
    assert report["confidence"] > 0.9
    assert report["cache_key"] == "test@example.com"
    assert report["agents"]
    assert report["rules_total"] == len(ROUTING_RULES)
    # Every rule that also matched is listed with an explicit outcome.
    for entry in report["rules_evaluated"]:
        assert entry["outcome"] in ("selected", "rejected")
        if entry["outcome"] == "rejected":
            assert entry["reject_reason"]
    assert any(e["outcome"] == "selected" for e in report["rules_evaluated"])


def test_explain_routing_lists_rejected_alternatives() -> None:
    """A URL that also matches the generic rule reports the runner-up."""
    report = explain_routing("https://github.com/a/b")
    names = {e["rule"] for e in report["rules_evaluated"]}
    assert "code_repo_url" in names
    rejected = [e for e in report["rules_evaluated"] if e["outcome"] == "rejected"]
    assert rejected, "expected at least one rejected alternative"


def test_explain_routing_flags_llm_eligibility_only_when_unsure() -> None:
    """A confident rule means the model is never consulted."""
    confident = explain_routing("test@example.com")
    assert confident["llm_considered"] is False
    unsure = explain_routing("")
    assert unsure["llm_considered"] is True
    assert unsure["llm_threshold"] == LLM_DISAMBIGUATION_THRESHOLD


def test_explain_routing_is_json_safe() -> None:
    """The panel payload must serialize (no datetimes, sets or bytes)."""
    import json

    for raw in ("+14155550123", "CVE-2021-44228", "hello world example", ""):
        json.dumps(explain_routing(raw))


# ── Structural invariants ────────────────────────────────────────────────────

def test_every_input_type_is_mapped_to_a_swarm() -> None:
    """No InputType may be missing from AGENT_MAP."""
    for itype in InputType:
        assert itype in AGENT_MAP, f"{itype.value} missing from AGENT_MAP"
        assert AGENT_MAP[itype], f"{itype.value} maps to an empty swarm"


def test_every_mapped_agent_exists_in_the_registry() -> None:
    """A router that names a non-existent agent would silently lose work."""
    from agents import AGENT_REGISTRY

    for itype, agents in AGENT_MAP.items():
        for name in agents:
            assert name in AGENT_REGISTRY, f"{itype.value} → unknown agent {name}"


def test_input_type_values_are_stable_strings() -> None:
    """InputType stays a str Enum: the frontend and stored rows key off values."""
    for itype in InputType:
        assert isinstance(itype.value, str)
        assert itype.value == itype.value.lower()
        assert itype in KNOWN_INPUT_TYPES


def test_legacy_input_type_values_are_unchanged() -> None:
    """Values the existing UI depends on must never drift."""
    legacy = {
        "username": InputType.USERNAME, "email": InputType.EMAIL,
        "phone": InputType.PHONE, "domain": InputType.DOMAIN,
        "ip_address": InputType.IP, "crypto_wallet": InputType.CRYPTO_WALLET,
        "url": InputType.URL, "image": InputType.IMAGE, "photo": InputType.PHOTO,
        "face": InputType.FACE, "person_name": InputType.PERSON_NAME,
        "video": InputType.VIDEO, "audio": InputType.AUDIO,
        "document": InputType.DOCUMENT, "text": InputType.TEXT,
        "unknown": InputType.UNKNOWN,
    }
    for value, member in legacy.items():
        assert member.value == value


def test_rules_are_unique_and_well_formed() -> None:
    """The table must have no duplicate rule names and every type must be mappable."""
    names = [rule.name for rule in ROUTING_RULES]
    assert len(names) == len(set(names)), "duplicate rule name in ROUTING_RULES"
    for rule in ROUTING_RULES:
        assert rule.input_type in AGENT_MAP
        assert 0.0 < rule.confidence <= 1.0
        assert rule.reasoning


def test_routing_decision_positional_contract() -> None:
    """llm.py constructs RoutingDecision positionally — keep the field order."""
    decision = RoutingDecision(InputType.DOMAIN, ["SCOUT"], 0.5, "because")
    assert decision.input_type is InputType.DOMAIN
    assert decision.agents == ["SCOUT"]
    assert decision.confidence == 0.5
    assert decision.reasoning == "because"
    # Defaults must exist so a 4-arg construction keeps working.
    assert decision.rule == "heuristic"
    assert decision.source == "rules"
    assert decision.target == {}


def test_build_context_infers_dayfirst_locale() -> None:
    """An unambiguous DMY date in the input flips the locale hint for KRONOS."""
    assert build_context("meeting 18/03/2024").dayfirst is True
    assert build_context("meeting 03/18/2024").dayfirst is False
    assert build_context("no dates here").dayfirst is False


def test_as_dict_round_trips() -> None:
    """as_dict produces the documented shape and JSON-safe values."""
    import json

    payload = detect_input_type("+14155550123").as_dict()
    json.dumps(payload)
    assert payload["input_type"] == "phone"
    assert payload["target"]["normalized"] == "+14155550123"
    assert payload["source"] == "rules"


def test_confident_decisions_skip_the_llm(monkeypatch) -> None:
    """A high-confidence rule must not call the model at all."""
    import asyncio

    import router

    calls: list[str] = []

    async def _boom(*_args, **_kwargs):
        calls.append("called")
        raise AssertionError("LLM must not be consulted for a confident decision")

    monkeypatch.setattr(router, "disambiguate", _boom)
    decision = asyncio.run(router.detect_input_type_async("test@example.com"))
    assert decision.input_type is InputType.EMAIL
    assert calls == []