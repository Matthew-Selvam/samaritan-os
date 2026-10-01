"""
darkweb.py — Marketplace mention monitoring for SENTINEL (defensive)
========================================================================
**Scope and intent.** This connector is a *defensive alerting* tool. It watches
operator-configured public index pages for text mentions of case entities and
raises a notification when a **new** mention appears. It deliberately cannot
and does not:

* place orders, contact sellers, hold escrow, or interact with any listing;
* search for illicit goods or services;
* send, negotiate, or transmit anything to a market participant.

It only reads public listing text that the operator has already allow-listed,
matches it against the operator's own case entity keywords, and reports a
diff. Nothing here facilitates purchasing — there is no purchase path in the
code at all, by design.

**Gating.** Disabled unless ``FEATURE_DARK_WEB_ALERTS=true`` (default off).
When disabled, every call returns ``{"enabled": False, "error": …}`` without
touching the network.

**Tor.** ``.onion`` targets are only ever fetched through the OPSEC proxy
(:func:`connectors.net.fetch` handles this). Clearnet targets work without Tor.

**Alerting.** Results are diffed against a persisted snapshot so only *new*
matches are surfaced. Duplicate matches inside ``SUPPRESS_WINDOW_S`` are
suppressed, and an optional allowlist (``DARKWEB_ALLOWLIST``) can mark certain
matches as expected/benign.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from typing import Any

from . import net

# ── Configuration ────────────────────────────────────────────────────────────

FEATURE = os.getenv("FEATURE_DARK_WEB_ALERTS", "false").lower() == "true"
#: URLs the operator has explicitly vetted and enabled. Nothing is fetched that
#: is not in this list — there is no crawl, no discovery, no seed generation.
INDEX_URLS: tuple[str, ...] = tuple(
    u.strip() for u in os.getenv("DARKWEB_INDEX_URLS", "").split(",") if u.strip()
)
#: Comma-separated hosts that are always permitted as *sources*.
ALLOWED_HOSTS: frozenset[str] = frozenset(
    h.strip().lower() for h in os.getenv("DARKWEB_ALLOWED_HOSTS", "").split(",") if h.strip()
)
#: Keywords whose presence in a match makes it *not* alert-worthy (a known
#: benign context, e.g. a news article about the case rather than a listing).
SUPPRESS_TERMS: tuple[str, ...] = tuple(
    t.strip().lower() for t in os.getenv("DARKWEB_SUPPRESS_TERMS", "").split(",") if t.strip()
)
#: Duplicate matches inside this window are not re-alerted.
SUPPRESS_WINDOW_S = int(os.getenv("DARKWEB_SUPPRESS_WINDOW_S", "21600"))
#: Snapshot retention on disk (per case).
SNAPSHOT_DIR = os.getenv("DARKWEB_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sentinel_store", "darkweb"))
#: Only allow ``.onion`` sources to be fetched when Tor is actually on.
_ALLOW_ONION = os.getenv("DARKWEB_ALLOW_ONION", "true").lower() == "true"

#: Words that are stripped from listing text before matching so a name does not
#: false-positive on every page ("the", "for", "sales", …).
_STOPWORDS = frozenset(
    "the a an and or for sale buy shop vendor of in on at to with new used "
    "listing listings market marketplace order orders shipping price prices "
    "contact whatsapp telegram signal email dm message best good cheap free".split()
)

#: Snippet context pulled around a hit.
SNIPPET_RADIUS = 90
MAX_MATCHES = 100
MAX_FETCH_BYTES = 4 * 1024 * 1024


def enabled() -> bool:
    """``True`` when dark-web monitoring is switched on."""
    return FEATURE


def health() -> dict[str, Any]:
    """Monitoring readiness for the OPS view.

    Returns:
        ``{"enabled", "indexes", "hosts", "allow_onion", "reason"}``.
    """
    out = {
        "enabled": FEATURE,
        "indexes": len(INDEX_URLS),
        "hosts": sorted(ALLOWED_HOSTS),
        "allow_onion": _ALLOW_ONION,
        "reason": None,
    }
    if not FEATURE:
        out["reason"] = "FEATURE_DARK_WEB_ALERTS is off — set it to true to enable"
    elif not INDEX_URLS:
        out["reason"] = "FEATURE_DARK_WEB_ALERTS is on but DARKWEB_INDEX_URLS is empty"
    return out


# ── Source allowlist ─────────────────────────────────────────────────────────

def source_allowed(url: str) -> tuple[bool, str | None]:
    """Check a source URL against the operator allowlist.

    Args:
        url: candidate index URL.

    Returns:
        ``(allowed, reason)``. ``reason`` is ``None`` when allowed.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url or "")
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()

    if scheme not in ("http", "https"):
        return False, f"unsupported scheme: {scheme or '(none)'}"
    if net.is_onion(host):
        if not _ALLOW_ONION:
            return False, "onion sources disabled (DARKWEB_ALLOW_ONION=false)"
        return True, None
    # Reject cloud-metadata / literal-private targets at the allowlist layer
    # too, so the reason is legible here rather than surfacing as a generic
    # fetch error later. net.fetch blocks these regardless.
    try:
        if not net.ip_is_public(host):
            return False, f"host '{host}' is not a public address"
    except ValueError:
        pass
    if host in ("metadata.google.internal", "metadata.goog", "metadata"):
        return False, f"cloud metadata host '{host}'"
    if ALLOWED_HOSTS and host not in ALLOWED_HOSTS:
        return False, f"host '{host}' is not in DARKWEB_ALLOWED_HOSTS"
    return True, None


# ── Keyword extraction ───────────────────────────────────────────────────────

def extract_keywords(entities: list[Any] | list[str], *,
                     extra: list[str] | None = None) -> list[str]:
    """Derive match keywords from case entities.

    Entities may be strings or the agents' ``{"id", "label", "type"}`` dicts.
    Short, generic and stopword-only terms are dropped so "John Smith" does not
    match every page containing "john".

    Args:
        entities: entity strings or dicts.
        extra: additional explicit keywords.

    Returns:
        A de-duplicated, length-sorted list of keywords (longest first, so a
        match on "john smith" wins over "john").
    """
    terms: list[str] = []
    for e in list(entities or []) + list(extra or []):
        if isinstance(e, dict):
            for key in ("label", "id", "name"):
                if e.get(key):
                    terms.append(str(e[key]))
                    break
        elif e:
            terms.append(str(e))

    out: set[str] = set()
    for raw in terms:
        cleaned = re.sub(r"[_\W]+", " ", raw, flags=re.UNICODE).strip().lower()
        if not cleaned or cleaned in _STOPWORDS:
            continue
        out.add(cleaned)
        # Also index the parts, minus stopwords and 1-2 char noise.
        for part in cleaned.split():
            if len(part) >= 4 and part not in _STOPWORDS:
                out.add(part)
    # Longer terms first so substring matching prefers the most specific.
    return sorted((t for t in out if len(t) >= 3 and t not in _STOPWORDS),
                  key=lambda t: (-len(t), t))


# ── Snapshot diffing ─────────────────────────────────────────────────────────

def _snapshot_path(case_id: str) -> str:
    """Resolve the on-disk snapshot path for a case.

    Args:
        case_id: the investigation case.

    Returns:
        An absolute path inside :data:`SNAPSHOT_DIR`.
    """
    safe = hashlib.sha256(str(case_id).encode()).hexdigest()[:16]
    return os.path.join(os.path.abspath(SNAPSHOT_DIR), f"{safe}.json")


def _load_snapshot(case_id: str) -> dict[str, Any]:
    """Load a case's previous snapshot.

    Args:
        case_id: the investigation case.

    Returns:
        The stored dict, or ``{}`` when there is none. Never raises.
    """
    path = _snapshot_path(case_id)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def _save_snapshot(case_id: str, data: dict[str, Any]) -> bool:
    """Persist a case snapshot.

    Args:
        case_id: the investigation case.
        data: the snapshot payload.

    Returns:
        ``True`` when written. Never raises.
    """
    try:
        os.makedirs(os.path.dirname(_snapshot_path(case_id)), exist_ok=True)
        with open(_snapshot_path(case_id), "w") as fh:
            json.dump(data, fh, indent=2)
        return True
    except OSError:
        return False


def _is_suppressed(match: dict[str, Any], snapshot: dict[str, Any],
                   now: float) -> tuple[bool, str | None]:
    """Decide whether a match should be withheld from an alert.

    Args:
        match: the current match.
        snapshot: the previous snapshot (for suppression history).
        now: current unix time.

    Returns:
        ``(suppress, reason)``.
    """
    for term in SUPPRESS_TERMS:
        if term and term in (match.get("context") or "").lower():
            return True, f"matched suppression term '{term}'"
    seen = (snapshot.get("seen") or {}).get(match["match_id"])
    if seen and (now - float(seen)) < SUPPRESS_WINDOW_S:
        return True, f"already alerted {int((now - float(seen)) / 60)}m ago (within {SUPPRESS_WINDOW_S}s window)"
    return False, None


# ── Fetch + match ────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    """Reduce HTML to searchable text.

    Args:
        html: raw HTML.

    Returns:
        Lowercased text with scripts/styles/entities removed.
    """
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&[a-z]+;|&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).lower()


def _match_text(text: str, keywords: list[str]) -> list[dict[str, Any]]:
    """Find keyword occurrences and build match records.

    Args:
        text: already-lowercased searchable text.
        keywords: keywords from :func:`extract_keywords`.

    Returns:
        A list of ``{"match_id", "keyword", "context"}`` dicts (capped).
    """
    found: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for kw in keywords:
        start = 0
        while True:
            idx = text.find(kw, start)
            if idx < 0:
                break
            start = idx + len(kw)
            lo, hi = max(0, idx - SNIPPET_RADIUS), min(len(text), idx + len(kw) + SNIPPET_RADIUS)
            match_id = hashlib.sha256(f"{kw}|{text[lo:hi]}".encode()).hexdigest()[:16]
            if match_id in seen_ids:
                continue
            seen_ids.add(match_id)
            found.append({"match_id": match_id, "keyword": kw,
                          "context": text[lo:hi].strip()})
            if len(found) >= MAX_MATCHES:
                return found
    return found


async def _fetch_source(url: str) -> dict[str, Any]:
    """Fetch one allow-listed index page.

    Args:
        url: the source URL (already allow-listed).

    Returns:
        ``{"ok", "text", "status", "error"}``. Never raises.
    """
    allowed, reason = source_allowed(url)
    if not allowed:
        return {"ok": False, "text": "", "status": 0, "error": reason}
    res = await net.fetch("GET", url, timeout=25.0, max_bytes=MAX_FETCH_BYTES)
    if not res.ok:
        return {"ok": False, "text": "", "status": res.status_code,
                "error": res.error or f"HTTP {res.status_code}"}
    return {"ok": True, "text": _strip_html(res.text), "status": res.status_code,
            "error": None, "final_url": res.final_url}


async def watch(case_id: str, entities: list[Any], *,
                urls: list[str] | None = None,
                keywords: list[str] | None = None) -> dict[str, Any]:
    """Watch configured sources for new mentions of the case entities.

    Args:
        case_id: the investigation case (scopes the snapshot).
        entities: case entities — strings or ``{"id", "label"}`` dicts.
        urls: sources to watch; defaults to :data:`INDEX_URLS`.
        keywords: explicit keywords; defaults to those derived from *entities*.

    Returns:
        ``{"enabled", "baseline", "new_matches", "seen_matches", "suppressed",
        "sources_checked", "sources_failed", "case_id", "available", "error"}``.
        On the first run ``baseline`` is ``True`` and ``new_matches`` is empty —
        a baseline run must never page anyone about pre-existing mentions.
        Never raises.
    """
    out: dict[str, Any] = {
        "enabled": FEATURE, "baseline": False, "new_matches": [],
        "seen_matches": [], "suppressed": [], "sources_checked": 0,
        "sources_failed": [], "case_id": case_id, "available": False,
        "error": None,
    }

    if not FEATURE:
        out["error"] = "dark-web monitoring disabled (FEATURE_DARK_WEB_ALERTS=false)"
        return out

    targets = [u for u in (urls or list(INDEX_URLS))]
    if not targets:
        out["error"] = "no DARKWEB_INDEX_URLS configured — nothing to watch"
        return out

    kw = keywords or extract_keywords(entities)
    if not kw:
        out["error"] = "no usable keywords derived from the case entities"
        return out

    now = time.time()
    prior = _load_snapshot(case_id)
    is_baseline = not prior

    # Only sources that pass the allowlist are ever fetched.
    permitted: list[str] = []
    for u in targets:
        ok, _ = source_allowed(u)
        if ok:
            permitted.append(u)
        else:
            out["sources_failed"].append({"url": u, "reason": "not allow-listed"})
    if not permitted:
        out["error"] = "no configured source passed the allowlist"
        return out

    results = await asyncio.gather(*(_fetch_source(u) for u in permitted),
                                   return_exceptions=True)

    all_matches: list[dict[str, Any]] = []
    for url, res in zip(permitted, results):
        if isinstance(res, BaseException):
            out["sources_failed"].append({"url": url, "reason": str(res)})
            continue
        if not res.get("ok"):
            out["sources_failed"].append({"url": url, "reason": res.get("error")})
            continue
        out["sources_checked"] += 1
        for m in _match_text(res["text"], kw):
            all_matches.append({**m, "source_url": url})

    known_ids = set((prior.get("matches") or {}).keys())
    seen_map: dict[str, float] = dict(prior.get("seen") or {})

    for m in all_matches:
        entry = {**m, "first_seen": now}
        if m["match_id"] in known_ids:
            out["seen_matches"].append(entry)
            seen_map.setdefault(m["match_id"], (prior["seen"].get(m["match_id"]) or now))
        else:
            suppress, reason = _is_suppressed(m, {"seen": seen_map}, now)
            if suppress:
                out["suppressed"].append({**entry, "suppressed_because": reason})
            else:
                out["new_matches"].append(entry)
            seen_map[m["match_id"]] = now

    # Persist: every match id we have now seen, plus the alert history.
    _save_snapshot(case_id, {
        "case_id": case_id,
        "matches": {m["match_id"]: m for m in all_matches},
        "seen": seen_map,
        "keywords": kw,
        "last_check": now,
        "runs": int(prior.get("runs", 0)) + 1,
    })

    out["baseline"] = is_baseline
    out["available"] = True
    if is_baseline:
        out["new_matches"] = []   # never alert on the first sighting
    if not out["sources_checked"] and out["sources_failed"]:
        out["error"] = "every configured source failed to fetch"
    return out


async def run_darkweb(case_id: str = "default", **kwargs: Any) -> dict[str, Any]:
    """Top-level entry point for SENTINEL.

    Args:
        case_id: the investigation case.
        **kwargs: ``entities``, ``urls``, ``keywords``.

    Returns:
        The :func:`watch` result dict. Never raises.
    """
    try:
        return await watch(case_id, kwargs.get("entities") or [],
                           urls=kwargs.get("urls"),
                           keywords=kwargs.get("keywords"))
    except Exception as exc:  # noqa: BLE001
        return {"enabled": FEATURE, "baseline": False, "new_matches": [],
                "seen_matches": [], "suppressed": [], "sources_checked": 0,
                "sources_failed": [], "case_id": case_id, "available": False,
                "error": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "enabled", "health", "source_allowed", "extract_keywords", "watch",
    "run_darkweb", "INDEX_URLS", "ALLOWED_HOSTS", "SUPPRESS_TERMS",
    "SUPPRESS_WINDOW_S",
]
