"""
router.py — AI Input Router
============================
Layered detector that classifies an investigation target and picks the swarm
that should work it.

Architecture
------------
1. **Deterministic rule table** (``ROUTING_RULES``) — ordered, first-match. Each
   rule carries a confidence and a human-readable reason, so every decision is
   explainable without a model. Rules are evaluated *in full* so
   :func:`explain_routing` can report the alternatives that lost.
2. **Optional LLM disambiguation** — only when the winning rule's confidence is
   below ``LLM_DISAMBIGUATION_THRESHOLD`` (0.6). Never consulted when a rule is
   confident: no latency, no cost, no new failure mode.
3. **Normalized target metadata** — every classification also produces a
   canonical form so ``'+1 (415) 555-0123'`` and ``'+14155550123'`` are the same
   target and therefore share one cache key / one case.

``InputType`` stays a ``str`` Enum (existing callers and the frontend depend on
the string values) and every pre-existing member keeps its original value.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote


# ── Input types ──────────────────────────────────────────────────────────────

class InputType(str, Enum):
    """Every target shape Signal-OS can route.

    The first block is the historical surface — values are frozen because the
    frontend, the stored investigations and ``llm.py`` all key off them.
    """

    # ── original surface (values frozen) ──
    USERNAME      = "username"
    EMAIL         = "email"
    PHONE         = "phone"
    DOMAIN        = "domain"
    IP            = "ip_address"
    CRYPTO_WALLET = "crypto_wallet"
    URL           = "url"
    IMAGE         = "image"
    PHOTO         = "photo"
    FACE          = "face"
    PERSON_NAME   = "person_name"
    VIDEO         = "video"
    AUDIO         = "audio"
    DOCUMENT      = "document"
    TEXT          = "text"
    UNKNOWN       = "unknown"

    # ── network / infrastructure ──
    IPV6          = "ipv6"
    MAC           = "mac_address"
    ASN           = "asn"
    ONION         = "onion_address"

    # ── host artifacts ──
    FILE_HASH     = "file_hash"
    UUID          = "uuid"
    CVE           = "cve"
    BASE64        = "base64_blob"

    # ── identity / financial ──
    IBAN          = "iban"
    VIN           = "vin"
    NATIONAL_ID   = "national_id"
    LICENSE_PLATE = "license_plate"

    # ── social / code ──
    HANDLE        = "social_handle"
    CODE_REPO     = "code_repo"
    MAGNET        = "magnet_link"


#: Types the LLM may propose. Kept in sync with ``InputType``; anything else is
#: rejected so a hallucinated type can never reach ``AGENT_MAP``.
KNOWN_INPUT_TYPES: frozenset[str] = frozenset(t.value for t in InputType)


#: Confidence at or above which the deterministic table is trusted outright.
LLM_DISAMBIGUATION_THRESHOLD = 0.6

#: Below this the decision is treated as "no idea" and the LLM is asked.
UNKNOWN_CONFIDENCE_CEILING = 0.45


# ── Which agents activate for each input type ────────────────────────────────
# APEX appends its universal correlation tier (NEXUS/KRONOS/VAULT/SENTINEL/QUILL)
# regardless of what is listed here, so these lists carry only the *primary*
# swarm. Every listed name must exist in ``agents.AGENT_REGISTRY``.

AGENT_MAP: dict[InputType, list[str]] = {
    # people / handles
    InputType.USERNAME:    ["SCOUT", "PRISM", "SIGMA"],
    InputType.HANDLE:      ["SCOUT", "PRISM", "SIGMA"],
    InputType.PERSON_NAME: ["SCOUT", "PRISM", "INK"],
    InputType.EMAIL:       ["EMAIL", "SCOUT", "SIGMA", "PRISM"],

    # telephony
    InputType.PHONE:       ["PHONOS", "SIGMA", "SCOUT"],

    # network surface
    InputType.DOMAIN:      ["SCOUT", "SIGMA", "CRAWLER", "PRISM"],
    InputType.URL:         ["CRAWLER", "IRIS", "SIGMA", "PRISM"],
    InputType.IP:          ["SIGMA", "TERRA", "CRAWLER"],
    InputType.IPV6:        ["SIGMA", "TERRA", "CRAWLER"],
    InputType.MAC:         ["SIGMA", "TERRA"],
    InputType.ASN:         ["SIGMA", "TERRA", "CRAWLER"],
    InputType.ONION:       ["SIGMA", "CRAWLER", "PRISM"],
    InputType.MAGNET:      ["SIGMA", "CRAWLER"],

    # host artifacts
    InputType.FILE_HASH:   ["SIGMA", "PRISM"],
    InputType.CVE:         ["SIGMA", "CRAWLER"],
    InputType.BASE64:      ["SIGMA", "PRISM"],
    InputType.UUID:        ["SIGMA", "PRISM"],
    InputType.CODE_REPO:   ["SCOUT", "SIGMA", "CRAWLER"],

    # financial / identity
    InputType.CRYPTO_WALLET: ["SIGMA", "PRISM", "TERRA"],
    InputType.IBAN:          ["SIGMA", "PRISM", "INK"],
    InputType.VIN:           ["SIGMA", "TERRA", "PRISM"],
    InputType.NATIONAL_ID:   ["SIGMA", "PRISM"],
    InputType.LICENSE_PLATE: ["SIGMA", "TERRA"],

    # media
    InputType.IMAGE:    ["IRIS", "TERRA", "SCOUT"],
    InputType.PHOTO:    ["IRIS", "TERRA", "SCOUT", "PRISM"],
    InputType.FACE:     ["IRIS", "PRISM", "SCOUT"],
    InputType.VIDEO:    ["IRIS", "ECHO", "TERRA"],
    InputType.AUDIO:    ["ECHO", "INK"],
    InputType.DOCUMENT: ["IRIS", "INK", "CRAWLER"],

    # free text
    InputType.TEXT:     ["INK", "PRISM", "SIGMA"],
    InputType.UNKNOWN:  ["SCOUT", "INK", "IRIS"],
}


# ── Rule plumbing ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Match:
    """What a rule found.

    Attributes:
        confidence: Rule confidence, optionally adjusted by ``bump``.
        metadata: Type-specific extras (platform, chain, tld, …).
        bump: Additive confidence adjustment for corroborating evidence.
    """

    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)
    bump: float = 0.0


@dataclass(frozen=True)
class Rule:
    """One ordered entry in the deterministic detection table.

    Attributes:
        name: Stable identifier surfaced by :func:`explain_routing`.
        input_type: The type this rule asserts.
        confidence: Baseline confidence when it matches.
        reasoning: Human-readable justification.
        matcher: ``(raw, ctx) -> Match | None``.
    """

    name: str
    input_type: InputType
    confidence: float
    reasoning: str
    matcher: Callable[[str, "Context"], Match | None]


@dataclass
class RoutingDecision:
    """The routing verdict.

    Field order is part of the contract: ``llm.py`` constructs this positionally
    as ``RoutingDecision(input_type, agents, confidence, reasoning)``.

    Attributes:
        input_type: The detected type.
        agents: Primary swarm to activate (APEX appends the correlation tier).
        confidence: 0.0–1.0.
        reasoning: One-line justification.
        rule: Name of the rule that fired (``"heuristic"`` when synthetic).
        target: Normalized target metadata (canonical value, cache key, extras).
        alternatives: Other rules that also matched but ranked lower.
        source: ``"rules"`` or ``"llm"``.
    """

    input_type: InputType
    agents: list[str]
    confidence: float
    reasoning: str
    rule: str = "heuristic"
    target: dict[str, Any] = field(default_factory=dict)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    source: str = "rules"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict of the decision."""
        return {
            "input_type": self.input_type.value,
            "agents": list(self.agents),
            "confidence": round(float(self.confidence), 3),
            "reasoning": self.reasoning,
            "rule": self.rule,
            "target": dict(self.target),
            "alternatives": list(self.alternatives),
            "source": self.source,
        }


@dataclass
class Context:
    """Corroborating evidence gathered once per detection.

    Attributes:
        dayfirst: ``True`` when the surrounding evidence points at a
            day-first (``DD/MM``) locale — consumed by KRONOS, but detected here
            so a single pass records it for every consumer.
        hints: Free-form flags contributed by callers.
    """

    dayfirst: bool = False
    hints: dict[str, Any] = field(default_factory=dict)


# ── Normalization helpers ────────────────────────────────────────────────────

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "referrer", "_ga", "igshid",
}
_DEFAULT_PORTS = {"http": "80", "https": "443", "ftp": "21"}

_MONTHS = {
    "jan", "january", "feb", "february", "mar", "march", "apr", "april",
    "may", "jun", "june", "jul", "july", "aug", "august", "sep", "sept",
    "september", "oct", "october", "nov", "november", "dec", "december",
}


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def normalize_phone(raw: str, ctx: Context | None = None) -> dict[str, Any]:
    """Canonicalize a phone number to E.164.

    ``'+1 (415) 555-0123'``, ``'+14155550123'`` and ``'415.555.0123'`` (with a
    US default region) all resolve to the same value, so they share a cache key.

    Args:
        raw: The raw input.
        ctx: Detection context; may carry a ``default_region`` hint.
    Returns:
        Dict with ``normalized`` (E.164 when resolvable), ``cache_key``,
        ``valid``, ``possible``, ``region`` and ``digits``.
    """
    ctx = ctx or Context()
    digits = _digits(raw)
    region_hint = ctx.hints.get("default_region") or ctx.hints.get("region")
    normalized, valid, possible, region = "", None, None, None
    try:
        import phonenumbers  # offline metadata; free, no key

        parse_region = region_hint or (None if raw.strip().startswith("+") else "US")
        try:
            parsed = phonenumbers.parse(raw.strip(), parse_region)
            possible = bool(phonenumbers.is_possible_number(parsed))
            valid = bool(phonenumbers.is_valid_number(parsed))
            region = phonenumbers.region_code_for_number(parsed)
            normalized = phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
        except Exception:  # noqa: BLE001 — unparseable is a normal outcome
            pass
    except ImportError:
        pass
    if not normalized:
        # Fallback: keep a leading '+' and otherwise just the digits, so
        # '+1 (415) 555-0123' and '+14155550123' still collide.
        normalized = ("+" + digits) if digits else ""
    return {
        "normalized": normalized or raw.strip(),
        "cache_key": normalized or raw.strip(),
        "digits": digits,
        "valid": valid,
        "possible": possible,
        "region": region,
        "default_region_used": region_hint,
    }


def _normalize_email(raw: str) -> dict[str, Any]:
    value = raw.strip()
    local, _, domain = value.rpartition("@")
    normalized = f"{local}@{domain.lower()}" if domain else value.lower()
    return {
        "normalized": normalized,
        "cache_key": normalized.lower(),
        "local_part": local,
        "domain": domain.lower(),
        "freemail": domain.lower() in {
            "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
            "protonmail.com", "proton.me", "icloud.com", "mail.com",
            "gmx.com", "zoho.com", "yandex.com", "tutanota.com",
        },
    }


def _normalize_domain(raw: str) -> dict[str, Any]:
    value = raw.strip().lower().rstrip(".")
    if "://" in value:
        value = urlsplit(value).hostname or value
    value = value.strip(".")
    labels = value.split(".")
    return {
        "normalized": value,
        "cache_key": value,
        "labels": labels,
        "registrable": ".".join(labels[-2:]) if len(labels) >= 2 else value,
        "tld": labels[-1] if len(labels) > 1 else "",
        "subdomain": ".".join(labels[:-2]) if len(labels) > 2 else "",
    }


def _normalize_url(raw: str) -> dict[str, Any]:
    value = raw.strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        return {"normalized": value, "cache_key": value}
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is not None and _DEFAULT_PORTS.get(scheme) == str(port):
        port = None
    netloc = f"{host}:{port}" if port else host
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if k.lower() not in _TRACKING_PARAMS])
    normalized = urlunsplit((scheme, netloc, parts.path.rstrip("/") or "/",
                             query, ""))  # fragment is client-side state
    return {
        "normalized": normalized,
        "cache_key": normalized.lower(),
        "scheme": scheme,
        "host": host,
        "path": parts.path,
        "query_keys": sorted({k for k, _ in parse_qsl(parts.query, keep_blank_values=True)}),
    }


def _normalize_handle(raw: str, platform: str, native: str) -> dict[str, Any]:
    key = f"{platform}:{native}".lower()
    return {"normalized": f"{platform}:{native}", "cache_key": key,
            "platform": platform, "handle": native}


def normalize_target(input_type: InputType, raw: str,
                     ctx: Context | None = None) -> dict[str, Any]:
    """Produce the canonical form + cache key for a classified target.

    Args:
        input_type: The detected type, which selects the normalizer.
        raw: The raw user input.
        ctx: Optional detection context.
    Returns:
        Dict with at least ``normalized`` and ``cache_key``.
    """
    ctx = ctx or Context()
    raw = (raw or "").strip()
    try:
        if input_type is InputType.PHONE:
            return normalize_phone(raw, ctx)
        if input_type is InputType.EMAIL:
            return _normalize_email(raw)
        if input_type is InputType.DOMAIN:
            return _normalize_domain(raw)
        if input_type is InputType.URL:
            return _normalize_url(raw)
        if input_type is InputType.HANDLE:
            host, _ = _host_of(raw)
            platform = _HANDLE_HOSTS.get(host)
            if not platform:
                meta = _HANDLE_BARE.match(raw)
                platform = (meta.group("platform").lower() if meta
                            else raw.strip().split(":")[0].split("/")[0].lstrip("@").lower()
                            if any(ch in raw for ch in ":/")
                            else "generic")
            native = (raw.strip().split("?")[0].split("#")[0]
                      .lstrip("@").split(":")[-1].split("/")[-1])
            return _normalize_handle(raw, platform, native)
        if input_type is InputType.IPV6:
            return {"normalized": _canon_ip(raw, version=6), "cache_key": _canon_ip(raw, 6)}
        if input_type is InputType.IP:
            return {"normalized": _canon_ip(raw, version=4), "cache_key": _canon_ip(raw, 4)}
        if input_type is InputType.MAC:
            value = raw.lower().replace("-", "").replace(":", "").replace(".", "")
            return {"normalized": value, "cache_key": value, "oui": value[:6],
                    "form": "eui48"}
        if input_type in (InputType.FILE_HASH, InputType.CVE, InputType.BASE64):
            value = raw.lower()
            extra: dict[str, Any] = {}
            if input_type is InputType.FILE_HASH:
                extra["algorithm"] = _hash_algorithm(value)
            elif input_type is InputType.CVE:
                extra["year"] = value.split("-")[1] if len(value.split("-")) > 1 else None
            else:
                extra["decoded_bytes"] = _b64_decode_len(value)
            return {"normalized": value, "cache_key": f"{input_type.value}:{value}", **extra}
        if input_type is InputType.CODE_REPO:
            host, path = _host_of(raw)
            # Strip any scheme and a leading "www." so 'github.com/a/b' and
            # 'https://github.com/a/b' are one target.
            canonical = f"{host}{path}".rstrip("/").lower()
            return {"normalized": canonical, "cache_key": canonical,
                    "platform": _REPO_HOSTS.get(host, "unknown"), "host": host}
        if input_type in (InputType.IBAN, InputType.VIN, InputType.NATIONAL_ID,
                          InputType.LICENSE_PLATE, InputType.UUID, InputType.ASN):
            value = re.sub(r"[\s\-]", "", raw).upper()
            return {"normalized": value, "cache_key": f"{input_type.value}:{value}"}
        if input_type is InputType.CRYPTO_WALLET:
            value = raw.strip()
            return {"normalized": value, "cache_key": value.lower()}
        # Everything else keys on its whitespace-collapsed form.
        value = _collapse_ws(unicodedata.normalize("NFKC", raw)).lower()
        return {"normalized": value, "cache_key": value[:512]}
    except Exception:  # noqa: BLE001 — normalization must never break routing
        return {"normalized": raw, "cache_key": raw[:512]}


def _canon_ip(raw: str, version: int | None = None) -> str:
    try:
        addr = ipaddress.ip_address(raw.strip().strip("[]"))
        return str(addr) if version is None or addr.version == version else raw.strip()
    except ValueError:
        return raw.strip().strip("[]").lower()


def _hash_algorithm(value: str) -> str:
    table = {32: "md5", 40: "sha1", 64: "sha256", 96: "sha96", 128: "sha512"}
    return table.get(len(value), "unknown")


def _b64_decode_len(value: str) -> int | None:
    try:
        return len(base64.b64decode(value, validate=True))
    except (binascii.Error, ValueError):
        return None


# ── Reusable matchers ────────────────────────────────────────────────────────

def _try_ip(value: str, version: int) -> Match | None:
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if addr.version != version:
        return None
    meta: dict[str, Any] = {
        "version": version,
        "is_private": addr.is_private,
        "is_loopback": addr.is_loopback,
        "is_multicast": addr.is_multicast,
        "is_reserved": addr.is_reserved,
        "is_global": addr.is_global,
    }
    if version == 4:
        packed = addr.packed
        meta["asn_hint"] = f"AS{packed[0] << 8 | packed[1]}"
    else:
        meta["scope"] = "link_local" if addr.is_link_local else "global"
    conf = 0.97 if addr.is_global else 0.93
    return Match(conf, meta)


def _looks_like_text(value: str) -> bool:
    """True when the input reads as prose rather than an identifier."""
    words = value.split()
    if len(value) >= 60 or len(words) >= 9:
        return True
    return bool(re.search(r"[.!?]\s+\S", value)) and len(words) >= 4


def _has_date_word(tokens: Iterable[str]) -> bool:
    return any(t.strip(".,").lower() in _MONTHS for t in tokens)


# ── Social / platform knowledge ──────────────────────────────────────────────

_SOCIAL_HOSTS: dict[str, str] = {
    "reddit.com": "reddit", "old.reddit.com": "reddit", "np.reddit.com": "reddit",
    "steamcommunity.com": "steam", "steampowered.com": "steam",
    "discord.com": "discord", "discordapp.com": "discord", "discord.gg": "discord",
    "t.me": "telegram", "telegram.me": "telegram", "telegram.org": "telegram",
    "x.com": "x", "twitter.com": "x", "mobile.twitter.com": "x",
    "instagram.com": "instagram", "facebook.com": "facebook", "fb.com": "facebook",
    "linkedin.com": "linkedin", "tiktok.com": "tiktok",
    "twitch.tv": "twitch", "pinterest.com": "pinterest",
    "bsky.app": "bluesky", "mastodon.social": "mastodon",
    "medium.com": "medium", "gitlab.com": "gitlab",
    "soundcloud.com": "soundcloud", "vk.com": "vk", "ok.ru": "odnoklassniki",
    "keybase.io": "keybase", "launchpad.net": "launchpad",
}

#: Hosts that host source code / models / datasets. ``facebook.com`` and
#: ``gitlab.com`` are deliberately absent here: they are social hosts first and
#: are classified by the higher-priority social rule.
_REPO_HOSTS: dict[str, str] = {
    "github.com": "github", "bitbucket.org": "bitbucket",
    "codeberg.org": "codeberg", "gitea.com": "gitea", "gitee.com": "gitee",
    "huggingface.co": "huggingface", "kaggle.com": "kaggle",
    "sourceforge.net": "sourceforge", "git.sr.ht": "sourcehut",
    "codeberg.io": "codeberg",
}

_HANDLE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"reddit\.com/(?:u|user)/(?P<handle>[A-Za-z0-9_\-]{2,32})", "reddit"),
    (r"steampowered\.com/id/(?P<handle>[A-Za-z0-9_\-]{2,64})", "steam"),
    (r"steamcommunity\.com/id/(?P<handle>[A-Za-z0-9_\-]{2,64})", "steam"),
    (r"discord\.com/users/(?P<handle>[A-Za-z0-9._\-]{2,64})", "discord"),
    (r"discord\.gg/(?P<handle>[A-Za-z0-9]{6,32})", "discord"),
    (r"t\.me/(?P<handle>[A-Za-z0-9_\-]{2,40})", "telegram"),
    (r"telegram\.me/(?P<handle>[A-Za-z0-9_\-]{3,40})", "telegram"),
    (r"(?:twitter|x)\.com/(?P<handle>[A-Za-z0-9_]{1,20})", "x"),
    (r"instagram\.com/(?P<handle>[A-Za-z0-9_.]{1,40})", "instagram"),
    (r"linkedin\.com/in/(?P<handle>[A-Za-z0-9\-%]{2,80})", "linkedin"),
    (r"twitch\.tv/(?P<handle>[A-Za-z0-9_]{2,30})", "twitch"),
    (r"tiktok\.com/@(?P<handle>[A-Za-z0-9_.]{1,30})", "tiktok"),
    (r"github\.com/(?P<handle>[A-Za-z0-9\-]{2,39})", "github"),
    (r"bsky\.app/profile/(?P<handle>[A-Za-z0-9._\-]{2,60})", "bluesky"),
    (r"medium\.com/@(?P<handle>[A-Za-z0-9._\-]{2,60})", "medium"),
    (r"soundcloud\.com/(?P<handle>[A-Za-z0-9\-]{2,40})", "soundcloud"),
    (r"vk\.com/(?P<handle>[A-Za-z0-9_.]{2,40})", "vk"),
)

#: Host -> platform, used by normalize_target to rebuild the platform label.
_HANDLE_HOSTS: dict[str, str] = {
    "reddit.com": "reddit", "steampowered.com": "steam",
    "steamcommunity.com": "steam", "discord.com": "discord", "discord.gg": "discord",
    "t.me": "telegram", "telegram.me": "telegram", "x.com": "x",
    "twitter.com": "x", "instagram.com": "instagram", "linkedin.com": "linkedin",
    "twitch.tv": "twitch", "tiktok.com": "tiktok", "github.com": "github",
    "bsky.app": "bluesky", "medium.com": "medium",
    "soundcloud.com": "soundcloud", "vk.com": "vk",
}

_HANDLE_BARE = re.compile(
    r"^(?P<platform>discord|telegram|reddit|x|twitter|github|instagram|steam)"
    r"\s*[:/]\s*(?P<handle>[A-Za-z0-9._#\-]{2,64})$",
    re.I,
)

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~\-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~\-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)

_HASH_RE = re.compile(r"^(?:[a-f0-9]{32}|[a-f0-9]{40}|[a-f0-9]{64}|[a-f0-9]{128})$")
_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,7}$", re.I)
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$|^(?:[0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}$")
_ASN_RE = re.compile(r"^(?:AS\s?\d{1,10}|asn\s?\d{1,10})$", re.I)
_ONION_RE = re.compile(r"^(?:[a-z2-7]{16}|[a-z2-7]{56})\.onion$", re.I)
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_MAGNET_RE = re.compile(r"^magnet:\?", re.I)
_AT_HANDLE_RE = re.compile(r"^@(?P<handle>[A-Za-z0-9][A-Za-z0-9_.]{1,49})$")
_DISCORD_LEGACY_RE = re.compile(r"^(?P<handle>[A-Za-z0-9_.]{2,32})#\d{4}$")
_DISCORD_MODERN_RE = re.compile(r"^(?:discord[.:])(?P<handle>[A-Za-z0-9_.]{2,32})[:/](\d{17,20})$")

# Crypto chains keyed by the value pattern they emit.
_CRYPTO_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bitcoin", re.compile(r"^(?:[13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[ac-hj-np-z02-9]{11,71})$")),
    ("ethereum", re.compile(r"^0x[a-fA-F0-9]{40}$")),
    ("litecoin", re.compile(r"^(?:[LM3][a-km-zA-HJ-NP-Z1-9]{26,33}|ltc1[a-z0-9]{20,70})$")),
    ("bitcoin_cash", re.compile(r"^(?:bitcoincash:)?[qp][a-z0-9]{41}$")),
    ("monero", re.compile(r"^4[0-9AB][1-9A-HJ-NP-Za-km-z]{93}$")),
    ("dash", re.compile(r"^X[1-9A-HJ-NP-Za-km-z]{33}$")),
    ("zcash", re.compile(r"^t1[a-km-zA-HJ-NP-Z1-9]{33}$")),
    ("tron", re.compile(r"^T[A-Za-z1-9]{33}$")),
    ("ripple", re.compile(r"^r[0-9a-zA-Z]{24,34}$")),
    ("dogecoin", re.compile(r"^D[5-9A-HJ-NP-U][1-9A-HJ-NP-Za-km-z]{32}$")),
    ("cardano", re.compile(r"^(?:addr1[0-9a-z]{50,}|DdzFF[1-9A-HJ-NP-Za-km-z]{50,})$")),
    ("stellar", re.compile(r"^G[A-Z2-7]{55}$")),
)

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
              ".heic", ".heif", ".avif", ".svg", ".ico")
_VIDEO_EXT = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv",
              ".mpg", ".mpeg", ".3gp")
_AUDIO_EXT = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".wma", ".opus",
              ".amr", ".aiff")
_DOC_EXT = (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt",
            ".rtf", ".odt", ".csv", ".md", ".json", ".xml", ".epub", ".log",
            ".eml", ".msg", ".zip", ".7z", ".rar", ".tar", ".gz")


# ── The rule table (ordered; first match wins) ───────────────────────────────

def _m_empty(raw: str, ctx: Context) -> Match | None:
    return Match(0.05, {"empty": True}) if not raw.strip() else None


def _m_uuid(raw: str, ctx: Context) -> Match | None:
    if not _UUID_RE.match(raw):
        return None
    version = raw[14]
    variant = "nil" if raw.lower() == "00000000-0000-0000-0000-000000000000" else "standard"
    return Match(0.95, {"version": int(version), "variant": variant})


def _m_ipv4(raw: str, ctx: Context) -> Match | None:
    return _try_ip(raw, 4)


def _m_ipv6(raw: str, ctx: Context) -> Match | None:
    if ":" not in raw:
        return None
    return _try_ip(raw, 6)


def _m_mac(raw: str, ctx: Context) -> Match | None:
    if not _MAC_RE.match(raw.strip()):
        return None
    value = raw.strip().lower().replace("-", "").replace(":", "").replace(".", "")
    # An EUI-48 whose last nibble is set is multicast, so it is never a NIC.
    multicast = int(value[-1], 16) & 1 == 1
    return Match(0.9 if multicast else 0.96,
                 {"unicast": not multicast, "oui": value[:6]})


def _m_cve(raw: str, ctx: Context) -> Match | None:
    if not _CVE_RE.match(raw.strip()):
        return None
    year = raw.strip().split("-")[1]
    if not (1999 <= int(year) <= 2100):
        return None
    return Match(0.98, {"year": int(year)})


def _m_hash(raw: str, ctx: Context) -> Match | None:
    value = raw.strip().lower()
    if not _HASH_RE.match(value):
        return None
    alg = _hash_algorithm(value)
    conf = {"md5": 0.96, "sha1": 0.97, "sha256": 0.98, "sha512": 0.97}.get(alg, 0.8)
    return Match(conf, {"algorithm": alg, "length": len(value)})


def _m_crypto(raw: str, ctx: Context) -> Match | None:
    value = raw.strip()
    for chain, pattern in _CRYPTO_PATTERNS:
        if pattern.match(value):
            conf = 0.97 if chain in ("bitcoin", "ethereum", "monero", "stellar") else 0.9
            return Match(conf, {"chain": chain, "address": value})
    return None


def _m_iban(raw: str, ctx: Context) -> Match | None:
    value = re.sub(r"\s", "", raw.strip()).upper()
    if not (15 <= len(value) <= 34) or not re.match(r"^[A-Z]{2}\d{2}[A-Z0-9]+$", value):
        return None
    # ISO 13616 mod-97 check.
    rearranged = value[4:] + value[:4]
    digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    try:
        if int(digits) % 97 != 1:
            return None
    except ValueError:
        return None
    return Match(0.99, {"country": value[:2], "check_digits": value[2:4],
                        "validated": "iso13616-mod97"})


def _m_magnet(raw: str, ctx: Context) -> Match | None:
    if not _MAGNET_RE.match(raw.strip()):
        return None
    try:
        query = dict(parse_qsl(urlsplit(raw.strip()).query.lstrip("?")))
    except ValueError:
        query = {}
    return Match(0.99, {"btih": query.get("xt"), "dn": query.get("dn"),
                        "tr_count": len(query.get("tr", "")) // 60})


def _m_onion(raw: str, ctx: Context) -> Match | None:
    if not _ONION_RE.match(raw.strip()):
        return None
    host = raw.strip().lower()
    return Match(0.98, {"version": 2 if len(host.split(".")[0]) == 16 else 3})


def _m_asn(raw: str, ctx: Context) -> Match | None:
    if not _ASN_RE.match(raw.strip()):
        return None
    digits = _digits(raw)
    if not (1 <= int(digits) <= 4294967295):
        return None
    return Match(0.94, {"asn": int(digits), "notation": raw.strip().upper()})


def _m_vin(raw: str, ctx: Context) -> Match | None:
    value = raw.strip().upper()
    if not _VIN_RE.match(value):
        return None
    transliterations = {
        **{str(d): d for d in range(10)},
        "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
        "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
        "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
    }
    weights = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
    try:
        total = sum(transliterations[c] * w for c, w in zip(value, weights))
    except KeyError:
        return None
    if total % 11 % 10 != int(value[8]):
        return None  # a random 17-char alnum string will fail the check digit
    wmi_year = {"1": "1981-85", "2": "1986", "3": "1987-89", "4": "1990", "5": "1991",
                "6": "1992-93", "7": "1994-95", "8": "1996", "9": "1997-99"}.get(value[0])
    return Match(0.95, {"check_digit_valid": True, "wmi": value[:3],
                        "model_year_hint": wmi_year, "country": "US"})


def _m_national_id(raw: str, ctx: Context) -> Match | None:
    """Recognise a few national-ID shapes, always validating the checksum."""
    value = re.sub(r"[\s\-]", "", raw.strip()).upper()

    # US SSN — area 000/666/9xx, group 00, serial 0000 are all invalid.
    if re.match(r"^\d{9}$", value) and re.match(r"^\d{3}-\d{2}-\d{4}$", raw.strip()):
        area, group, serial = value[:3], value[3:5], value[5:]
        if area not in {"000", "666"} and not area.startswith("9") \
                and group != "00" and serial != "0000":
            return Match(0.85, {"scheme": "US_SSN", "area": area,
                                "itin_candidate": area.startswith("9")})

    # Labelled forms are unambiguous regardless of shape.
    labelled = re.match(
        r"^(?:ssn|nino|national\s*insurance|nin|passport|aadhaar|aadhar|"
        r"sin|nric|tin|taxpayer)\s*[:#\-]?\s*([A-Z0-9]{6,20})$", value)
    if labelled:
        scheme = re.match(r"^(SSN|NINO|SSN|NIN|PASSPORT|AADHAAR|AADHAR|SIN|NRIC|TIN|TAXPAYER)",
                          value)
        return Match(0.9, {"scheme": (scheme.group(1) if scheme else "LABELLED"),
                           "value_len": len(labelled.group(1))})

    # Aadhaar — 12 digits passing the Verhoeff checksum.
    if re.match(r"^[2-9]\d{3}\d{4}\d{4}$", value):
        if _verhoeff_valid(value):
            return Match(0.9, {"scheme": "IN_AADHAAR", "verhoeff_valid": True})
    return None


_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6), (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4), (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2), (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_VERHOEFF_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def _verhoeff_valid(number: str) -> bool:
    if not number.isdigit() or len(number) < 1:
        return False
    c = 0
    inverted = list(reversed(number))
    for i, ch in enumerate(inverted):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return _VERHOEFF_INV[c] == 0


def _m_license_plate(raw: str, ctx: Context) -> Match | None:
    value = raw.strip().upper()
    if " " in value and not re.match(r"^[A-Z]{2}\s", value):
        return None
    compact = value.replace(" ", "")
    if not (5 <= len(compact) <= 9):
        return None
    if not re.match(r"^[A-Z0-9\-]+$", compact):
        return None
    # A plausible plate is mostly letters with a digit block; require one so a
    # bare uppercase word does not get mistaken for a plate.
    if not re.search(r"\d", compact) or not re.search(r"[A-Z]", compact):
        return None
    if re.match(r"^[A-Z]{1,3}\d{1,4}[A-Z]{0,2}$", compact) or \
       re.match(r"^[A-Z]{2}\d[A-Z]{2,3}$", compact):
        country = compact[:2] if re.match(r"^[A-Z]{2}\d", compact) else None
        return Match(0.75, {"format": "eu_compact", "country_code": country})
    if re.match(r"^\d[A-Z]{3}\d{2}$", compact):
        return Match(0.7, {"format": "us_state"})
    return None


def _m_base64(raw: str, ctx: Context) -> Match | None:
    value = raw.strip()
    if len(value) < 24 or len(value) % 4 or not re.match(r"^[A-Za-z0-9+/]+={0,2}$", value):
        return None
    decoded = _b64_decode_len(value)
    if decoded is None:
        return None
    if _hash_algorithm(value.lower()) != "unknown":
        return None  # a hex hash that happens to be base64-shaped
    print_ratio = value.count("=")
    if print_ratio > 2:
        return None
    return Match(0.7, {"decoded_bytes": decoded, "entropy_hint": "high"})


def _m_email(raw: str, ctx: Context) -> Match | None:
    if not _EMAIL_RE.match(raw.strip()):
        return None
    bump = 0.0
    if "." not in raw.split("@")[0]:
        bump += 0.01
    return Match(0.99, {"domain": raw.split("@")[-1].lower()}, bump)


def _m_discord_handle(raw: str, ctx: Context) -> Match | None:
    """Discord handles, with or without an explicit platform prefix.

    Accepts the legacy ``name#4242`` / ``discord:name#4242`` form and the new
    ``username:123456789012345678`` / ``discord:username:…`` form. The
    ``discord:``-prefixed forms matter because Discord migrated usernames to
    unique IDs, so a "platform:handle" string is now the common way to write one.
    """
    value = raw.strip()
    prefixed = re.match(r"^discord[:.]\s*(?P<rest>.+)$", value, re.I)
    if prefixed:
        value = prefixed.group("rest")
    modern = _DISCORD_MODERN_RE.match(value)
    legacy = _DISCORD_LEGACY_RE.match(value)
    if modern:
        return Match(0.94, {"platform": "discord", "handle": modern.group("handle"),
                            "id": modern.group(2), "style": "new_username"})
    if legacy:
        return Match(0.9, {"platform": "discord", "handle": legacy.group("handle"),
                           "style": "legacy_discriminator"})
    return None


def _m_at_handle(raw: str, ctx: Context) -> Match | None:
    match = _AT_HANDLE_RE.match(raw.strip())
    if not match:
        return None
    handle = match.group("handle")
    platform = "telegram" if ctx.hints.get("handle_platform") == "telegram" else "generic"
    conf = 0.9 if platform == "telegram" else 0.75
    return Match(conf, {"platform": platform, "handle": handle})


def _m_bare_handle(raw: str, ctx: Context) -> Match | None:
    match = _HANDLE_BARE.match(raw.strip())
    if not match:
        return None
    platform = match.group("platform").lower()
    return Match(0.85, {"platform": platform, "handle": match.group("handle")})


def _host_of(raw: str) -> tuple[str, str]:
    value = raw.strip()
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
    except ValueError:
        return "", ""
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host, parts.path or ""


def _m_social_url(raw: str, ctx: Context) -> Match | None:
    """A social profile URL, with or without an explicit scheme.

    ``https://www.reddit.com/u/spez``, ``reddit.com/u/spez`` and
    ``t.me/durov`` all resolve to a platform + handle.
    """
    value = raw.strip()
    if "/" not in value and "." not in value:
        return None
    host, path = _host_of(value)
    if host not in _SOCIAL_HOSTS:
        return None
    # Patterns are written host-first, so strip the scheme *and* the www. prefix.
    bare = re.sub(r"^https?://(?:www\.)?", "", value, flags=re.I)
    for pattern, platform in _HANDLE_PATTERNS:
        match = re.match(pattern, bare, re.I)
        if match and _SOCIAL_HOSTS.get(host) == platform:
            return Match(0.94, {"platform": platform, "handle": match.group("handle"),
                                "profile_path": path})
    return None


def _m_code_repo(raw: str, ctx: Context) -> Match | None:
    """A code / model / dataset host URL, with or without an explicit scheme."""
    value = raw.strip()
    if "/" not in value:
        return None
    host, path = _host_of(value)
    if host not in _REPO_HOSTS:
        return None
    platform = _REPO_HOSTS[host]
    segments = [p for p in path.split("/") if p]
    # GitHub honours a trailing .git and an extra path; keep the first two.
    return Match(0.95, {"platform": platform, "path": path.strip("/"),
                        "owner": segments[0] if segments else None,
                        "repo": (segments[1] if len(segments) > 1 else None),
                        "host": host})


def _m_url(raw: str, ctx: Context) -> Match | None:
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", raw.strip()):
        return None
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return None
    if not parts.hostname or "." not in parts.hostname:
        return None
    bump = 0.0
    if parts.query:
        bump += 0.01
    return Match(0.96, {"scheme": parts.scheme.lower(), "host": parts.hostname.lower()}, bump)


def _m_media_ext(raw: str) -> InputType | None:
    """Classify by file extension. Returns the type, or ``None``.

    Split from the :class:`Rule` matchers (which must return a :class:`Match`)
    because one extension table covers four different input types.
    """
    low = raw.strip().lower().split("?")[0].split("#")[0]
    if not re.search(r"\.[a-z0-9]{2,5}$", low):
        return None
    ext = low.rsplit(".", 1)[1]
    if low.endswith(_IMAGE_EXT):
        return InputType.IMAGE
    if low.endswith(_VIDEO_EXT):
        return InputType.VIDEO
    if low.endswith(_AUDIO_EXT):
        return InputType.AUDIO
    if low.endswith(_DOC_EXT):
        return InputType.DOCUMENT
    return None


def _media_matcher(input_type: InputType) -> "Callable[[str, Context], Match | None]":
    """Build a rule matcher that fires on one media extension family.

    Args:
        input_type: The type this matcher asserts.
    Returns:
        A matcher suitable for a :class:`Rule`.
    """

    def matcher(raw: str, ctx: Context) -> Match | None:
        if _m_media_ext(raw) is not input_type:
            return None
        low = raw.strip().lower()
        meta: dict[str, Any] = {"extension": low.rsplit(".", 1)[1]}
        if input_type is InputType.DOCUMENT:
            meta["is_archive"] = low.endswith((".zip", ".7z", ".rar", ".tar", ".gz"))
        return Match(0.99, meta)

    matcher.__name__ = f"m_{input_type.value}"
    return matcher


_MATCH_IMAGE = _media_matcher(InputType.IMAGE)
_MATCH_VIDEO = _media_matcher(InputType.VIDEO)
_MATCH_AUDIO = _media_matcher(InputType.AUDIO)
_MATCH_DOCUMENT = _media_matcher(InputType.DOCUMENT)


def _m_phone(raw: str, ctx: Context) -> Match | None:
    value = raw.strip()
    digits = _digits(value)
    if not digits or not (7 <= len(digits) <= 15):
        return None
    has_separator = bool(re.search(r"[\s().\-]", value))
    starts_plus = value.startswith("+")
    if not (starts_plus or has_separator):
        # A bare digit run is genuinely ambiguous (phone vs numeric alias).
        if not (ctx.hints.get("numeric_is_phone") or len(digits) >= 10):
            return None
        return Match(0.55, {"ambiguous": True, "format": "digits_only"},
                     bump=0.05 if len(digits) >= 11 else 0.0)
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", value):
        return None  # IPv4
    conf = 0.93 if starts_plus else 0.88
    if re.match(r"^\+\d{1,3}[\s.\-]?\(?\d", value) and not value[2:].startswith("(") is None:
        conf += 0.01
    return Match(min(conf, 0.96), {"format": "e164" if starts_plus else "formatted"})


def _m_domain(raw: str, ctx: Context) -> Match | None:
    value = raw.strip().rstrip(".")
    if " " in value or "/" in value:
        return None
    host = value
    if "://" in host:
        host = urlsplit(host).hostname or ""
    if "." not in host:
        return None
    try:
        host = host.encode("idna").decode("ascii")  # punycode-normalize unicode hosts
    except (UnicodeError, ValueError):
        pass
    labels = host.split(".")
    tld = labels[-1]
    if len(tld) < 2 or not re.match(r"^[a-z]{2,63}$", tld, re.I):
        return None
    if any(not l or len(l) > 63 or l.startswith("-") or l.endswith("-")
           for l in labels):
        return None
    if not all(re.match(r"^[a-z0-9\-]+$", l, re.I) for l in labels):
        return None
    # A single label that looks like a filename wins the DOCUMENT rule, but a
    # two-label string with a real TLD is a domain.
    conf = 0.93
    if len(labels) >= 3:
        conf = 0.9
    if tld.lower() in {"zip", "png", "jpg", "exe", "py", "js", "css", "html"}:
        conf -= 0.25
    if conf < 0.5:
        return None
    return Match(conf, {"tld": tld.lower(), "label_count": len(labels)})


def _m_text(raw: str, ctx: Context) -> Match | None:
    if not _looks_like_text(raw):
        return None
    conf = 0.9 if len(raw) > 120 else 0.8
    if _has_date_word(raw.split()[:6]):
        conf += 0.02  # a date-bearing blob is content, not a person
    return Match(conf, {"chars": len(raw), "words": len(raw.split())})


def _m_person_name(raw: str, ctx: Context) -> Match | None:
    words = raw.strip().split()
    if not (2 <= len(words) <= 5):
        return None
    if not all(re.match(r"^[A-Za-z][A-Za-z'.\-]{1,24}$", w) for w in words):
        return None
    if _has_date_word(words):
        return None  # "March 3 2024" is not a person
    if any(w.lower() in {"and", "the", "for", "with", "from", "that", "this"} for w in words):
        return None
    capitalized = sum(1 for w in words if w[0].isupper())
    if capitalized < 2:
        return None
    return Match(0.8, {"word_count": len(words)})


def _m_username(raw: str, ctx: Context) -> Match | None:
    value = raw.strip()
    if not re.match(r"^[A-Za-z0-9](?:[A-Za-z0-9_.\-]{1,29})$", value):
        return None
    if value.isdigit():
        return None  # all digits is a phone or an id, not a handle
    bump = 0.0
    if any(c.isdigit() for c in value):
        bump -= 0.08   # digits read more like an identifier than an alias
    if "." in value or "_" in value:
        bump += 0.02
    return Match(0.7, {"length": len(value)}, bump)


#: Ordered detection table. Everything above this line is a matcher; this is the
#: priority list. Order is load-bearing: e.g. the media-extension rule precedes
#: the domain rule so ``report.pdf`` is a DOCUMENT and not a domain, and the
#: phone rule precedes the username rule so ``4155550123`` is not an alias.
ROUTING_RULES: tuple[Rule, ...] = (
    Rule("empty", InputType.UNKNOWN, 0.05, "Input was empty or whitespace",
         _m_empty),
    Rule("uuid", InputType.UUID, 0.95,
         "RFC 4122 UUID (8-4-4-4-12 hex with version/variant nibbles)", _m_uuid),
    Rule("ipv4", InputType.IP, 0.97,
         "Valid IPv4 address (ipaddress parse)", _m_ipv4),
    Rule("ipv6", InputType.IPV6, 0.95,
         "Valid IPv6 address (ipaddress parse)", _m_ipv6),
    Rule("mac_address", InputType.MAC, 0.96,
         "IEEE 802 MAC-48 address (colon/dash/dotted hex)", _m_mac),
    Rule("cve", InputType.CVE, 0.98,
         "CVE identifier (CVE-YYYY-NNNN) with plausible year", _m_cve),
    Rule("file_hash", InputType.FILE_HASH, 0.97,
         "Fixed-length lowercase hex digest (MD5/SHA-1/SHA-256/SHA-512)", _m_hash),
    Rule("crypto_wallet", InputType.CRYPTO_WALLET, 0.95,
         "Cryptocurrency address matching a known chain's format", _m_crypto),
    Rule("iban", InputType.IBAN, 0.99,
         "IBAN with a valid ISO 13616 mod-97 check digit", _m_iban),
    Rule("magnet_link", InputType.MAGNET, 0.99,
         "magnet: URI carrying a BitTorrent info hash", _m_magnet),
    Rule("onion_address", InputType.ONION, 0.98,
         "Tor v2/v3 hidden-service .onion address", _m_onion),
    Rule("asn", InputType.ASN, 0.94, "Autonomous System Number (AS####)", _m_asn),
    Rule("vin", InputType.VIN, 0.95,
         "17-character VIN with a valid ISO 3779 check digit", _m_vin),
    Rule("national_id", InputType.NATIONAL_ID, 0.88,
         "National identifier shape with a valid checksum or explicit label",
         _m_national_id),
    Rule("license_plate", InputType.LICENSE_PLATE, 0.75,
         "Vehicle registration plate pattern", _m_license_plate),
    Rule("base64_blob", InputType.BASE64, 0.70,
         "Base64 blob: 4-aligned length, valid alphabet, decodes cleanly",
         _m_base64),
    Rule("email", InputType.EMAIL, 0.99, "RFC 5321 addr-spec with a dotted domain",
         _m_email),
    Rule("discord_handle", InputType.HANDLE, 0.92,
         "Discord handle (legacy name#discriminator or new username:id)",
         _m_discord_handle),
    Rule("code_repo_url", InputType.CODE_REPO, 0.95,
         "URL on a code/model/dataset host (GitHub, GitLab, HF, Kaggle…)",
         _m_code_repo),
    Rule("social_profile_url", InputType.HANDLE, 0.94,
         "URL on a social platform profile path", _m_social_url),
    Rule("platform_handle", InputType.HANDLE, 0.85,
         "platform:handle shorthand (reddit:foo, t.me/bar)", _m_bare_handle),
    Rule("at_handle", InputType.HANDLE, 0.75,
         "@handle form (generic unless a platform hint is supplied)", _m_at_handle),
    Rule("url", InputType.URL, 0.96, "Absolute URL with a scheme and dotted host",
         _m_url),
    Rule("image_file", InputType.IMAGE, 0.99, "Image file extension",
         _MATCH_IMAGE),
    Rule("video_file", InputType.VIDEO, 0.99, "Video file extension",
         _MATCH_VIDEO),
    Rule("audio_file", InputType.AUDIO, 0.99, "Audio file extension",
         _MATCH_AUDIO),
    Rule("document_file", InputType.DOCUMENT, 0.99,
         "Document / archive file extension", _MATCH_DOCUMENT),
    Rule("phone", InputType.PHONE, 0.93,
         "Phone number: E.164 prefix or 7–15 digits with separators",
         _m_phone),
    Rule("domain", InputType.DOMAIN, 0.93,
         "Registered domain name with a real TLD", _m_domain),
    Rule("text", InputType.TEXT, 0.85, "Free-form prose (multiple words/sentences)",
         _m_text),
    Rule("person_name", InputType.PERSON_NAME, 0.80,
         "2–5 capitalized name-shaped words, no date words", _m_person_name),
    Rule("username", InputType.USERNAME, 0.70,
         "Short alphanumeric handle with no spaces", _m_username),
)


# ── Detection entry points ───────────────────────────────────────────────────

def build_context(raw: str = "", hints: dict[str, Any] | None = None) -> Context:
    """Gather corroborating evidence once, for every rule to read.

    Args:
        raw: The raw input (used only for locale sniffing).
        hints: Caller-supplied flags, e.g. ``{"default_region": "GB"}``.
    Returns:
        A populated :class:`Context`.
    """
    ctx = Context(hints=dict(hints or {}))
    probe = (raw or "")[:400]
    # A day-first locale is inferred from an unambiguous DMY date anywhere in
    # the input; KRONOS reads the same hint so both agree.
    for match in re.finditer(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b", probe):
        a, b = int(match.group(1)), int(match.group(2))
        if a > 12 >= b:
            ctx.dayfirst = True
            break
        if b > 12 >= a:
            ctx.dayfirst = False
            break
    if not ctx.dayfirst and re.search(r"\b\d{4}-\d{2}-\d{2}\b", probe):
        ctx.dayfirst = False
    return ctx


def _agents_for(input_type: InputType) -> list[str]:
    return list(AGENT_MAP.get(input_type, AGENT_MAP[InputType.UNKNOWN]))


def detect_input_type(raw: str, *, hints: dict[str, Any] | None = None) -> RoutingDecision:
    """Classify ``raw`` with the deterministic rule table only.

    Every rule is evaluated (not just the winner) so the decision can report
    which lower-priority rules also matched. The first match wins.

    Args:
        raw: The raw user input.
        hints: Optional corroborating evidence, e.g. ``default_region``.
    Returns:
        A :class:`RoutingDecision`; never raises.
    """
    text = unicodedata.normalize("NFKC", (raw or "")).strip()
    ctx = build_context(text, hints)

    matches: list[tuple[int, Rule, Match]] = []
    for index, rule in enumerate(ROUTING_RULES):
        try:
            hit = rule.matcher(text, ctx)
        except Exception:  # noqa: BLE001 — one bad matcher must not blind the rest
            continue
        if hit is not None:
            matches.append((index, rule, hit))

    if not matches:
        decision = RoutingDecision(
            InputType.UNKNOWN, _agents_for(InputType.UNKNOWN),
            UNKNOWN_CONFIDENCE_CEILING, "No rule matched the input", "none",
        )
        decision.target = normalize_target(InputType.UNKNOWN, text, ctx)
        return decision

    index, rule, hit = matches[0]
    confidence = round(max(0.0, min(1.0, rule.confidence + hit.bump)), 3)
    alternatives = [
        {"rule": other_rule.name, "input_type": other_rule.input_type.value,
         "confidence": round(max(0.0, min(1.0, other_rule.confidence + other.bump)), 3),
         "reasoning": other_rule.reasoning}
        for _, other_rule, other in matches[1:]
    ]

    metadata = dict(hit.metadata)
    metadata["raw"] = text[:500]
    metadata["dayfirst"] = ctx.dayfirst
    decision = RoutingDecision(
        rule.input_type, _agents_for(rule.input_type), confidence, rule.reasoning,
        rule.name,
    )
    decision.target = {**normalize_target(rule.input_type, text, ctx), **metadata}
    decision.alternatives = alternatives
    return decision


def explain_routing(raw: str, *, hints: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the full routing story for a UI panel.

    Args:
        raw: The raw user input.
        hints: Optional corroborating evidence.
    Returns:
        Dict with the winning rule, its confidence, the normalized target and
        cache key, every alternative that matched and why it lost, the full rule
        inventory, and the agent swarm that would be activated.
    """
    decision = detect_input_type(raw, hints=hints)
    ctx = build_context((raw or "").strip(), hints)
    considered = []
    for index, rule in enumerate(ROUTING_RULES):
        try:
            hit = rule.matcher(unicodedata.normalize("NFKC", (raw or "")).strip(), ctx)
        except Exception:  # noqa: BLE001
            continue
        if hit is None:
            continue
        confidence = round(max(0.0, min(1.0, rule.confidence + hit.bump)), 3)
        considered.append({
            "rule": rule.name,
            "input_type": rule.input_type.value,
            "priority": index,
            "confidence": confidence,
            "matched": rule.name == decision.rule,
            "outcome": "selected" if rule.name == decision.rule else "rejected",
            "reject_reason": None if rule.name == decision.rule
                             else f"lower priority than '{decision.rule}'",
            "reasoning": rule.reasoning,
            "evidence": {k: v for k, v in hit.metadata.items() if k != "raw"},
        })

    return {
        "raw": (raw or "")[:500],
        "input_type": decision.input_type.value,
        "confidence": decision.confidence,
        "reasoning": decision.reasoning,
        "rule_fired": decision.rule,
        "source": decision.source,
        "agents": list(decision.agents),
        "target": decision.target,
        "cache_key": decision.target.get("cache_key"),
        "llm_considered": decision.confidence < LLM_DISAMBIGUATION_THRESHOLD,
        "llm_threshold": LLM_DISAMBIGUATION_THRESHOLD,
        "alternatives_considered": decision.alternatives,
        "rules_evaluated": considered,
        "rules_total": len(ROUTING_RULES),
        "context": {"dayfirst": ctx.dayfirst, "hints": ctx.hints},
    }


_LLM_SYSTEM = (
    "You triage OSINT investigation targets. Reply with one JSON object only: "
    '{"input_type": "<type>", "reasoning": "<one sentence>"}. Allowed input_type '
    "values are exactly: " + ", ".join(sorted(KNOWN_INPUT_TYPES)) + ". Choose the "
    "single best type; never invent a value outside that list."
)


async def disambiguate(decision: RoutingDecision, raw: str,
                       *, timeout: float = 8.0) -> RoutingDecision:
    """Ask the LLM for a second opinion on an uncertain decision.

    Never called when the rules are confident, never raises, and never returns a
    type outside :data:`KNOWN_INPUT_TYPES`.

    Args:
        decision: The heuristic decision to refine.
        raw: The raw user input.
        timeout: Seconds allowed for the model call.
    Returns:
        Either ``decision`` unchanged, or a refined copy with ``source='llm'``.
    """
    if decision.confidence >= LLM_DISAMBIGUATION_THRESHOLD:
        return decision
    try:
        import asyncio

        from llm import get_llm

        payload = await asyncio.wait_for(
            get_llm().complete_json(
                f"Target: {raw.strip()[:300]}\nClassify it.",
                system=_LLM_SYSTEM,
                max_tokens=256,
                timeout=timeout,
                schema_hint='{"input_type": "domain", "reasoning": "short string"}',
            ),
            timeout=timeout + 2.0,
        )
    except Exception:  # noqa: BLE001 — the model is strictly optional
        return decision
    if not isinstance(payload, dict) or payload.get("error"):
        return decision

    proposed = str(payload.get("input_type") or "").strip().lower()
    if proposed not in KNOWN_INPUT_TYPES:
        return decision
    try:
        refined_type = InputType(proposed)
    except ValueError:
        return decision
    if refined_type is decision.input_type:
        return decision

    ctx = Context()
    refined = RoutingDecision(
        refined_type, _agents_for(refined_type), 0.75,
        str(payload.get("reasoning") or "LLM disambiguation")[:240],
        decision.rule,
    )
    refined.source = "llm"
    refined.target = {**normalize_target(refined_type, raw, ctx),
                      "heuristic_type": decision.input_type.value,
                      "heuristic_confidence": decision.confidence}
    refined.alternatives = decision.alternatives
    return refined


async def detect_input_type_async(
    raw: str,
    *,
    hints: dict[str, Any] | None = None,
    use_llm: bool = True,
) -> RoutingDecision:
    """Classify ``raw``, escalating to the LLM only when the rules are unsure.

    Args:
        raw: The raw user input.
        hints: Optional corroborating evidence.
        use_llm: Set False to force the deterministic path.
    Returns:
        A :class:`RoutingDecision`.
    """
    decision = detect_input_type(raw, hints=hints)
    if not use_llm or decision.confidence >= LLM_DISAMBIGUATION_THRESHOLD:
        return decision
    return await disambiguate(decision, raw)


__all__ = [
    "AGENT_MAP",
    "KNOWN_INPUT_TYPES",
    "LLM_DISAMBIGUATION_THRESHOLD",
    "ROUTING_RULES",
    "Context",
    "InputType",
    "Match",
    "Rule",
    "RoutingDecision",
    "build_context",
    "detect_input_type",
    "detect_input_type_async",
    "disambiguate",
    "explain_routing",
    "normalize_target",
    "normalize_phone",
]