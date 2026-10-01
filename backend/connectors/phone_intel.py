"""
phone_intel.py — Phone Number Intelligence Connector
=====================================================
Given a phone number, extracts everything derivable from public / offline
sources. Two layers:

  1. OFFLINE (always works, no keys) — parse & validate via libphonenumber
     metadata: region, carrier name, line type, timezones, E.164 normalization,
     and cross-reference pivots (formatted variants + search dorks) so the number
     links up with signals other agents collected.

  2. LIVE (optional, key-gated, OPSEC-routed) — NumVerify carrier validation and
     breach-database exposure. Every live source degrades to a clear {"error": …}
     when its key/service is missing — never an exception.

Scope note: this is a COLLECTION connector for consented / authorized OSINT
(CTF targets, your own numbers, signed-scope engagements). It reads public
metadata about a number. It does NOT perform live-location tracking, cell-tower
triangulation, or account takeover — those require carrier access or targeting a
person who has not agreed, and are out of scope by design.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote_plus

NUMVERIFY_API_KEY = os.getenv("NUMVERIFY_API_KEY", "")
DEHASHED_EMAIL = os.getenv("DEHASHED_EMAIL", "")
DEHASHED_API_KEY = os.getenv("DEHASHED_API_KEY", "")


# ── Offline layer ─────────────────────────────────────────────────────────────

def parse_offline(number: str, default_region: str | None = None) -> dict[str, Any]:
    """Full offline parse via the phonenumbers (libphonenumber) metadata set.

    Returns validity, normalized formats, region, carrier, line type, and
    timezones — all without a single network call.
    """
    try:
        import phonenumbers
        from phonenumbers import carrier, geocoder, timezone
    except ImportError:
        return {"error": "phonenumbers not installed — run: pip install phonenumbers",
                "valid": False}

    # Numbers without a country code need a default region to parse.
    region = default_region or (None if number.strip().startswith("+") else "US")
    try:
        parsed = phonenumbers.parse(number, region)
    except phonenumbers.NumberParseException as e:
        return {"error": f"unparseable: {e}", "valid": False, "input": number}

    is_valid = phonenumbers.is_valid_number(parsed)
    is_possible = phonenumbers.is_possible_number(parsed)

    type_map = {
        phonenumbers.PhoneNumberType.MOBILE: "mobile",
        phonenumbers.PhoneNumberType.FIXED_LINE: "fixed_line",
        phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_or_mobile",
        phonenumbers.PhoneNumberType.VOIP: "voip",
        phonenumbers.PhoneNumberType.TOLL_FREE: "toll_free",
        phonenumbers.PhoneNumberType.PREMIUM_RATE: "premium_rate",
        phonenumbers.PhoneNumberType.SHARED_COST: "shared_cost",
        phonenumbers.PhoneNumberType.PAGER: "pager",
        phonenumbers.PhoneNumberType.PERSONAL_NUMBER: "personal",
        phonenumbers.PhoneNumberType.UAN: "uan",
        phonenumbers.PhoneNumberType.VOICEMAIL: "voicemail",
        phonenumbers.PhoneNumberType.UNKNOWN: "unknown",
    }
    line_type = type_map.get(phonenumbers.number_type(parsed), "unknown")

    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    intl = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    national = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)

    return {
        "valid": is_valid,
        "possible": is_possible,
        "input": number,
        "e164": e164,
        "international": intl,
        "national": national,
        "country_code": parsed.country_code,
        "national_number": parsed.national_number,
        "region": geocoder.region_code_for_number(parsed),
        "location": geocoder.description_for_number(parsed, "en") or None,
        "carrier": carrier.name_for_number(parsed, "en") or None,
        "line_type": line_type,
        "timezones": list(timezone.time_zones_for_number(parsed)),
    }


def pivot_variants(offline: dict[str, Any]) -> list[str]:
    """Normalized string variants to cross-reference against other signals."""
    variants: set[str] = set()
    for key in ("e164", "national", "international", "input"):
        v = offline.get(key)
        if v:
            variants.add(str(v))
            variants.add("".join(ch for ch in str(v) if ch.isdigit()))
    return sorted(v for v in variants if v)


def search_dorks(offline: dict[str, Any]) -> list[str]:
    """Public-web pivot queries for the number (fed to SCOUT / manual review)."""
    e164 = offline.get("e164")
    national = offline.get("national")
    digits = "".join(ch for ch in (e164 or "") if ch.isdigit())
    dorks: list[str] = []
    for token in filter(None, {e164, national, digits}):
        q = quote_plus(str(token))
        dorks.append(f'"{token}"')
        dorks.append(f'intext:"{token}"')
    return dorks


# ── Live layer (optional, key-gated) ──────────────────────────────────────────

async def numverify_lookup(e164: str) -> dict[str, Any]:
    """Live carrier validation via NumVerify. Requires NUMVERIFY_API_KEY."""
    if not NUMVERIFY_API_KEY:
        return {"source": "numverify",
                "error": "NUMVERIFY_API_KEY not set — free key at numverify.com"}
    try:
        from opsec import OpsecClient
        client = OpsecClient(timeout=15.0)
        resp = await client.get(
            "http://apilayer.net/api/validate",
            params={"access_key": NUMVERIFY_API_KEY,
                    "number": e164.lstrip("+"), "format": 1},
        )
        data = resp.json()
        if not data.get("valid", False) and data.get("error"):
            return {"source": "numverify", "error": data["error"].get("info", "lookup failed")}
        return {
            "source": "numverify",
            "valid": data.get("valid"),
            "carrier": data.get("carrier") or None,
            "line_type": data.get("line_type") or None,
            "location": data.get("location") or None,
            "country": data.get("country_name") or None,
        }
    except Exception as e:  # noqa: BLE001 — never break the pipeline on a connector
        return {"source": "numverify", "error": str(e)}


async def dehashed_lookup(e164: str) -> dict[str, Any]:
    """Breach-exposure lookup for a phone number via Dehashed. Key-gated."""
    if not (DEHASHED_EMAIL and DEHASHED_API_KEY):
        return {"source": "dehashed", "breaches": [],
                "error": "DEHASHED_EMAIL/DEHASHED_API_KEY not set — dehashed.com/api"}
    try:
        import base64
        from opsec import OpsecClient
        client = OpsecClient(timeout=20.0)
        token = base64.b64encode(f"{DEHASHED_EMAIL}:{DEHASHED_API_KEY}".encode()).decode()
        resp = await client.get(
            "https://api.dehashed.com/search",
            params={"query": f"phone:{e164}"},
            headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
        )
        if resp.status_code != 200:
            return {"source": "dehashed", "breaches": [], "error": f"HTTP {resp.status_code}"}
        entries = resp.json().get("entries", []) or []
        return {
            "source": "dehashed",
            "total": len(entries),
            "breaches": [
                {"database": e.get("database_name"), "email": e.get("email"),
                 "username": e.get("username"), "name": e.get("name")}
                for e in entries[:50]
            ],
        }
    except Exception as e:  # noqa: BLE001
        return {"source": "dehashed", "breaches": [], "error": str(e)}


# ── Public entry point ────────────────────────────────────────────────────────

async def run_phone_intel(number: str, default_region: str | None = None) -> dict[str, Any]:
    """Full phone-number workup. Offline always runs; live sources degrade cleanly."""
    import asyncio

    offline = parse_offline(number, default_region)
    result: dict[str, Any] = {
        "offline": offline,
        "pivots": pivot_variants(offline),
        "dorks": search_dorks(offline),
        "numverify": {},
        "dehashed": {},
    }

    e164 = offline.get("e164")
    if e164 and offline.get("valid"):
        result["numverify"], result["dehashed"] = await asyncio.gather(
            numverify_lookup(e164),
            dehashed_lookup(e164),
        )
    return result
