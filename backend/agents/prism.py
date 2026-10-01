"""
prism.py — PRISM Agent (Social Intelligence / Identity Resolution)
====================================================================

Turns scattered social traces into a defensible identity picture. The agent
keeps its three original connectors (Sherlock username scan, people search,
breach check) and adds the analytical layer that makes the results usable:

1. **Cross-platform account clustering** — a username found on several
   platforms is resolved into *person* clusters using the signals an analyst
   actually trusts: identical avatar hash, bio similarity, cross-referenced
   handles, follower-graph overlap, and shared contact/identifier. Each cluster
   carries an explicit confidence and the evidence behind it.
2. **Person-record assembly** — name variants, locations, employers and ages
   are folded into one profile with **per-field** confidence, plus an explicit
   ``unverified`` list. Nothing is invented: a field with no evidence is listed
   as unknown, not guessed.
3. **Person-name disambiguation** — for the ``/api/name-search`` path, a bare
   name returns RANKED candidates with the evidence that separated them
   (location, employer, age, username/email linkage), because "John Smith" is
   not a person, it is thousands of candidates.
4. **Optional LLM corroboration** — advisory only, guarded, and never able to
   invent an account or a field that the deterministic pass did not find.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import unicodedata
from collections import defaultdict
from typing import Any

from .base import BaseAgent, AgentResult

#: Similarity above which two accounts are considered the same person.
IDENTITY_THRESHOLD = 0.62
#: Above this a cluster is treated as a confirmed person rather than a lead.
STRONG_THRESHOLD = 0.82
#: Token overlap of follower/following sets that counts as graph overlap evidence.
FOLLOWER_OVERLAP_THRESHOLD = 0.25

try:  # pragma: no cover - env parsing
    LLM_TIMEOUT_S = float(os.environ.get("PRISM_LLM_TIMEOUT_S", "60"))
except (TypeError, ValueError):  # pragma: no cover
    LLM_TIMEOUT_S = 60.0

#: Master switch for the advisory LLM pass. Identity resolution is complete
#: without it — the model only comments on clusters already found. Operators
#: needing minimum latency (or running a slow local model) set this false and
#: pay nothing; the deterministic product is unchanged.
try:  # pragma: no cover - env parsing
    ADVISORY_LLM = os.environ.get("SIGNAL_OS_ADVISORY_LLM", "true").casefold() \
        not in ("0", "false", "no", "off")
except Exception:  # pragma: no cover
    ADVISORY_LLM = True

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "location": ("location", "city", "town", "place", "address", "region", "state"),
    "employer": ("employer", "company", "org", "organisation", "organization",
                 "workplace", "company_name"),
    "age": ("age", "years_old", "person_age"),
    "dob": ("dob", "date_of_birth", "birth_date", "birthdate"),
    "country": ("country", "country_code", "nation", "nationality"),
    "bio": ("bio", "about", "description", "tagline", "headline"),
    "avatar": ("avatar", "avatar_hash", "avatar_url_hash", "image_hash", "photo_hash"),
    "followers": ("followers", "follower_count", "followers_count"),
    "email": ("email", "email_address", "mail"),
    "phone": ("phone", "msisdn", "telephone", "mobile"),
}

_NAME_DROP = {"dr", "mr", "mrs", "ms", "prof", "sir", "jr", "sr", "ii", "iii",
              "iv", "phd", "md"}


def _norm(text: Any) -> str:
    """Casefold and strip all non-alphanumerics from a value.

    Args:
        text: Arbitrary value.

    Returns:
        str: Comparison key (``""`` when empty).
    """
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^a-z0-9]+", "", s)


def _words(text: Any) -> list[str]:
    """Tokenise a value into lowercase word tokens.

    Args:
        text: Arbitrary value.

    Returns:
        list[str]: Alphanumeric tokens.
    """
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return re.findall(r"[a-z0-9]+", s)


def _name_parts(text: Any) -> tuple[str, str]:
    """Split a person name into normalised (given, family) parts.

    Args:
        text: Person name, possibly with honorifics and a middle name.

    Returns:
        tuple[str, str]: ``(given_initials, family)``, both normalised.
    """
    toks = [t for t in _words(text) if t not in _NAME_DROP]
    if not toks:
        return "", ""
    family = toks[-1]
    given = "".join(t[0] for t in toks[:-1])
    return given, family


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two sets.

    Args:
        a: First set.
        b: Second set.

    Returns:
        float: Overlap in ``0.0``–``1.0``.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _field(attrs: dict, field: str) -> Any:
    """Read a canonical field from a dict using its alias list.

    Args:
        attrs: Attribute dict (may be empty).
        field: Canonical field name.

    Returns:
        Any: First non-empty alias value, or ``None``.
    """
    for key in _FIELD_ALIASES.get(field, (field,)):
        val = (attrs or {}).get(key)
        if val not in (None, "", [], {}):
            return val
    return None


def _clean(value: Any, limit: int = 120) -> str:
    """Coerce a value to a trimmed, length-capped string.

    Args:
        value: Arbitrary value.
        limit: Maximum length.

    Returns:
        str: Cleaned string (``""`` when empty).
    """
    if value is None:
        return ""
    s = str(value).strip()
    return s[:limit]


class PrismAgent(BaseAgent):
    """Social Intelligence — cross-platform identity resolution.

    Keeps the original Sherlock / people-search / breach connector results as
    its raw material and adds account clustering, person-record assembly and
    ranked name disambiguation on top.
    """

    name = "PRISM"
    role = "Social Intelligence"
    icon = "◈"
    description = ("Cross-platform identity resolution, account clustering, "
                   "person-record assembly, breach correlation, ranked "
                   "name disambiguation")
    preferred_models = ["qwen2.5vl:7b", "gemma2:9b", "qwen2.5:7b"]
    token_budget = 8192

    # ── Account clustering ──────────────────────────────────────────────────
    @staticmethod
    def _account_features(rec: dict) -> dict[str, Any]:
        """Extract comparable features from one account record.

        Args:
            rec: Account record (may carry ``attrs``).

        Returns:
            dict: Normalised handle, platform, avatar, bio tokens, follower set
            and identifier fragments.
        """
        attrs = rec.get("attrs") or {}
        value = str(rec.get("value") or rec.get("handle") or rec.get("username") or "")
        handle = _norm(value)
        platform = _clean(_field(attrs, "platform") or rec.get("platform")
                          or rec.get("site") or rec.get("url"), 60).casefold()
        bio = _clean(_field(attrs, "bio") or rec.get("bio"), 400).casefold()
        avatar = _norm(_field(attrs, "avatar"))
        followers_raw = _field(attrs, "followers")
        followers: set[str] = set()
        try:
            if followers_raw is not None and not isinstance(followers_raw, bool):
                if isinstance(followers_raw, (list, tuple, set)):
                    followers = {_norm(x) for x in followers_raw if str(x).strip()}
                else:
                    followers = {_norm(x)}  # single count, hashed for comparison
        except (TypeError, ValueError):
            followers = set()
        return {
            "handle": handle,
            "platform": platform,
            "avatar": avatar,
            "bio": bio,
            "bio_tokens": set(_words(bio)),
            "followers": followers,
            "email": _norm(_field(attrs, "email")),
            "phone": _norm(_field(attrs, "phone")),
            "location": _norm(_field(attrs, "location")),
            "employer": _norm(_field(attrs, "employer")),
            "source": str(rec.get("source") or "?"),
            "confidence": float(rec.get("confidence") or 0.5),
            "id": str(rec.get("id") or rec.get("value") or ""),
            "url": _clean(rec.get("url"), 200),
        }

    def _cluster_accounts(self, accounts: list[dict]) -> list[dict]:
        """Cluster accounts into plausible single-person groups.

        Uses a union-find over pairwise identity scores. A pair only merges on
        *hard* evidence (identical avatar hash, identical email/phone, or a
        cross-referenced handle); a shared handle across platforms or a similar
        bio produces a scored link that is reported either way, but only crosses
        the merge threshold with corroboration.

        Args:
            accounts: Account records to cluster.

        Returns:
            list[dict]: Clusters, each with ``id``, ``label``, ``confidence``,
            ``members``, ``evidence`` and ``alternatives`` (links considered but
            not merged).
        """
        feats = [self._account_features(a) for a in accounts]
        n = len(feats)
        parent = list(range(n))

        def find(i: int) -> int:
            """Resolve the cluster root of index ``i``.

            Args:
                i: Member index.

            Returns:
                int: Root index.
            """
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            """Merge two clusters, keeping the lower index as root.

            Args:
                i: First member index.
                j: Second member index.
            """
            ri, rj = find(i), find(j)
            if ri != rj:
                # Lower index wins so cluster membership is order-stable.
                lo, hi = sorted((ri, rj))
                parent[hi] = lo

        links: list[dict] = []
        for i in range(n):
            for j in range(i + 1, n):
                a, b = feats[i], feats[j]
                if a["handle"] and a["handle"] == b["handle"] \
                        and a["platform"] == b["platform"]:
                    continue  # same account seen twice
                score, evidence, hard = self._pair_identity(a, b)
                if score <= 0 and not evidence:
                    continue
                links.append({"i": i, "j": j, "score": round(score, 3),
                              "evidence": evidence, "hard": hard,
                              "merged": False})
                if hard or score >= STRONG_THRESHOLD:
                    union(i, j)
                    links[-1]["merged"] = True

        groups: dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            groups[find(i)].append(i)

        clusters: list[dict] = []
        for idx, (_, members) in enumerate(sorted(groups.items(),
                                                  key=lambda kv: (-len(kv[1]), kv[0]))):
            member_feats = [feats[m] for m in members]
            platforms = sorted({f["platform"] for f in member_feats if f["platform"]})
            internal = [l for l in links
                        if l["merged"] and l["i"] in members and l["j"] in members]
            scores = [l["score"] for l in internal] or [0.0]
            # The confidence of a cluster is the *weakest* link in it, not the
            # mean: one unproven hop makes the whole identity a lead, not a fact.
            conf = round(min(scores), 3) if internal else 0.4
            contested = [l for l in links
                         if not l["merged"] and {l["i"], l["j"]} <= set(members)]
            label = self._cluster_label(member_feats, platforms)
            clusters.append({
                "id": f"prism_cluster:{idx}:{_norm(label)[:24] or 'anon'}",
                "label": label,
                "size": len(members),
                "confidence": conf,
                "confidence_tier": ("strong" if conf >= STRONG_THRESHOLD
                                    else "probable" if conf >= IDENTITY_THRESHOLD
                                    else "unconfirmed"),
                "members": [accounts[m].get("id") or accounts[m].get("value")
                            for m in members],
                "member_details": [{
                    "id": accounts[m].get("id") or accounts[m].get("value"),
                    "handle": accounts[m].get("value") or accounts[m].get("handle"),
                    "platform": feats[m]["platform"],
                    "url": feats[m]["url"],
                    "source": feats[m]["source"],
                    "confidence": round(feats[m]["confidence"], 3),
                } for m in members],
                "platforms": platforms,
                "evidence": [f"{l['i']}~{l['j']}: {l['evidence']}" for l in internal],
                "alternatives": [{"i": l["i"], "j": l["j"], "score": l["score"],
                                  "evidence": l["evidence"], "reason": "not merged"}
                                 for l in contested],
                "single_source": len({f["source"] for f in member_feats}) == 1,
            })
        return clusters

    @staticmethod
    def _pair_identity(a: dict, b: dict) -> tuple[float, str, bool]:
        """Score whether two accounts belong to the same person.

        Args:
            a: Features of the first account.
            b: Features of the second account.

        Returns:
            tuple: ``(confidence, evidence, hard_merge)`` where ``hard_merge``
            marks evidence strong enough to merge unconditionally.
        """
        score, notes, hard = 0.0, [], False
        # Every key is read defensively: feature dicts also arrive from peer
        # agents, so a missing key must never raise inside the pipeline.
        a_avatar, b_avatar = a.get("avatar"), b.get("avatar")
        a_handle, b_handle = a.get("handle"), b.get("handle")
        a_bio, b_bio = a.get("bio") or "", b.get("bio") or ""
        a_tokens, b_tokens = a.get("bio_tokens") or set(), b.get("bio_tokens") or set()
        if a_avatar and a_avatar == b_avatar:
            score += 0.80
            notes.append("identical avatar hash")
            hard = True
        if a.get("email") and a.get("email") == b.get("email"):
            score += 0.85
            notes.append("identical email on two accounts")
            hard = True
        if a.get("phone") and a.get("phone") == b.get("phone"):
            score += 0.70
            notes.append("identical phone on two accounts")
            hard = True
        a_plat, b_plat = a.get("platform") or "", b.get("platform") or ""
        same_platform = bool(a_plat) and a_plat == b_plat
        if same_platform:
            # Two profiles on one platform with the same handle are one account.
            if a_handle and a_handle == b_handle:
                score += 0.90
                notes.append("same handle on the same platform")
                hard = True
        elif a_handle and a_handle == b_handle:
            score += 0.40
            notes.append(f"same handle on {a_plat or '?'} and {b_plat or '?'}")
        if a_tokens and b_tokens:
            sim = _jaccard(a_tokens, b_tokens)
            if sim >= 0.5:
                score += round(0.45 * sim, 3)
                notes.append(f"bios {sim:.0%} similar")
            elif sim >= 0.25:
                score += round(0.12 * sim, 3)
                notes.append(f"bios {sim:.0%} similar")
        # A handle named inside the other account's bio is explicit cross-referencing.
        if a_bio and b_handle and len(b_handle) > 3 and b_handle in a_bio:
            score += 0.45
            notes.append(f"bio of {a_plat or '?'} references @{b_handle}")
            hard = True
        if b_bio and a_handle and len(a_handle) > 3 and a_handle in b_bio:
            score += 0.45
            notes.append(f"bio of {b_plat or '?'} references @{a_handle}")
            hard = True
        a_followers, b_followers = a.get("followers") or set(), b.get("followers") or set()
        if a_followers and b_followers:
            overlap = _jaccard(a_followers, b_followers)
            if overlap >= FOLLOWER_OVERLAP_THRESHOLD:
                score += round(0.5 * overlap, 3)
                notes.append(f"follower-graph overlap {overlap:.0%}")
        if a.get("location") and a.get("location") == b.get("location"):
            score += 0.12
            notes.append("same listed location")
        if a.get("employer") and a.get("employer") == b.get("employer"):
            score += 0.15
            notes.append("same listed employer")
        if not notes:
            return 0.0, "", False
        return min(0.98, score), " + ".join(notes), hard

    @staticmethod
    def _cluster_label(feats: list[dict], platforms: list[str]) -> str:
        """Build a human label for an account cluster.

        Args:
            feats: Features of every member account.
            platforms: Sorted distinct platforms in the cluster.

        Returns:
            str: Label such as ``"john@twitter, github"``.
        """
        handles = sorted({f["handle"] for f in feats if f["handle"]})
        handle = handles[0] if handles else "unknown"
        if platforms:
            return f"{handle}@{', '.join(platforms[:4])}"
        return handle

    # ── Person-record assembly ──────────────────────────────────────────────
    @staticmethod
    def _assemble_person(name_variants: list[str], field_values: dict[str, list[tuple[str, str]]],
                         account_ids: list[str], breaches: list[dict]
                         ) -> dict[str, Any]:
        """Assemble one person profile with per-field confidence.

        Args:
            name_variants: Every name spelling observed for the person.
            field_values: ``field -> [(value, source), ...]`` from all sources.
            account_ids: Ids of accounts attributed to the person.
            breaches: Breach records affecting the person.

        Returns:
            dict: Profile with ``fields`` (per-field value/confidence/sources),
            ``unverified`` and an overall confidence.
        """
        # Confidence by agreement: more independent sources agreeing = stronger.
        field_weights = {"location": 0.3, "employer": 0.3, "age": 0.2,
                         "dob": 0.25, "country": 0.25, "email": 0.3,
                         "phone": 0.3, "bio": 0.15}
        fields: dict[str, Any] = {}
        unverified: list[str] = []
        for field, entries in field_values.items():
            if not entries:
                continue
            tally: dict[str, list[str]] = defaultdict(list)
            for value, source in entries:
                key = _norm(value)
                if key:
                    tally[key].append(source)
            if not tally:
                continue
            best_key, sources = max(tally.items(), key=lambda kv: (len(kv[1]), kv[0]))
            display = next(v for v, _ in entries if _norm(v) == best_key)
            independent = len(set(sources))
            agree = len(sources) / max(1, len(entries))
            base = field_weights.get(field, 0.2)
            conf = min(0.95, base + 0.2 * (independent - 1) + 0.3 * (agree - 0.5))
            fields[field] = {
                "value": _clean(display, 120),
                "confidence": round(max(0.05, conf), 3),
                "sources": sorted(set(sources)),
                "corroborating_sources": independent,
                "conflicting_values": [
                    {"value": _clean(next(v for v, _ in entries if _norm(v) == k), 80),
                     "sources": sorted(set(s))}
                    for k, s in tally.items() if k != best_key
                ],
            }
            if fields[field]["conflicting_values"]:
                fields[field]["status"] = "conflicting"
            elif independent < 2:
                fields[field]["status"] = "single_source"

        tracked = ("location", "employer", "age", "dob", "country", "email", "phone")
        for field in tracked:
            if field not in fields:
                unverified.append(field)
        for field in fields:
            if fields[field].get("status") == "single_source":
                unverified.append(f"{field} (single source)")

        if not name_variants:
            unverified.append("name")
        if not account_ids:
            unverified.append("accounts")

        # The canonical name is the fullest observed spelling, not the shortest:
        # "John Smith" is the canonical form, "J. Smith" is its abbreviation.
        clean_variants = sorted({_clean(v, 80) for v in name_variants if v})
        canonical = ""
        if clean_variants:
            canonical = max(clean_variants, key=lambda v: (len(v), v))

        # Overall confidence tracks how much of a profile is actually verified.
        if fields:
            mean_conf = sum(f["confidence"] for f in fields.values()) / len(fields)
        else:
            mean_conf = 0.0
        coverage = len([f for f in tracked if f in fields]) / len(tracked)
        overall = round(min(0.9, 0.25 * mean_conf + 0.55 * coverage), 3)
        return {
            "name_variants": clean_variants,
            "canonical_name": canonical,
            "fields": fields,
            "unverified": unverified,
            "accounts": account_ids,
            "breach_count": len(breaches),
            "breaches": breaches[:10],
            "confidence": overall,
            "evidence_sufficiency": ("thin — treat as a lead, not a profile"
                                     if overall < 0.45 else
                                     "adequate" if overall < 0.7 else "strong"),
        }

    # ── Name disambiguation ─────────────────────────────────────────────────
    def _rank_candidates(self, name: str, people: list[dict],
                         hints: dict[str, Any]) -> list[dict]:
        """Rank people records that share a name, with per-candidate evidence.

        A bare name matches many unrelated people, so the output is a ranked list
        with the *reason* each candidate is on it, not a single guess.

        Args:
            name: The searched name.
            people: Raw people-search records.
            hints: Caller hints (``location``, ``employer``, ``age``).

        Returns:
            list[dict]: Ranked candidates, each with ``score``, ``evidence`` and
            ``disqualifiers``.
        """
        want_given, want_family = _name_parts(name)
        out: list[dict] = []
        for rec in people or []:
            if not isinstance(rec, dict):
                continue
            full = _clean(rec.get("full_name") or rec.get("name") or "", 120)
            g, f = _name_parts(full)
            if not f:
                continue
            score, evidence, disqual = 0.0, [], []
            if f == want_family:
                score += 0.35
                evidence.append(f"same surname '{f}'")
            elif want_family and f[0] == want_family[0] and f != want_family:
                score += 0.05
                disqual.append(f"different surname ({f})")
            else:
                disqual.append("different surname")
            if want_given and g == want_given:
                score += 0.30
                evidence.append("same given-name initials")
            elif want_given and g and g[0] == want_given[0]:
                score += 0.12
                evidence.append("given-name initial matches")
            elif want_given:
                disqual.append("given-name initials differ")

            rec_loc = _norm(_field(rec, "location"))
            rec_emp = _norm(_field(rec, "employer"))
            rec_age = _field(rec, "age")
            hint_loc = _norm(hints.get("location"))
            hint_emp = _norm(hints.get("employer"))
            if hint_loc and rec_loc:
                if rec_loc == hint_loc:
                    score += 0.30
                    evidence.append(f"location matches request ({rec.get('location')})")
                else:
                    disqual.append(f"location differs ({rec.get('location')})")
            if hint_emp and rec_emp:
                if rec_emp == hint_emp:
                    score += 0.25
                    evidence.append(f"employer matches request ({rec.get('employer')})")
                else:
                    disqual.append(f"employer differs ({rec.get('employer')})")
            hint_age = hints.get("age")
            if hint_age is not None and rec_age is not None:
                try:
                    delta = abs(int(hint_age) - int(rec_age))
                    if delta == 0:
                        score += 0.20
                        evidence.append(f"age matches ({rec_age})")
                    elif delta <= 2:
                        score += 0.08
                        evidence.append(f"age within {delta} year(s)")
                    else:
                        disqual.append(f"age differs by {delta} years")
                except (TypeError, ValueError):
                    pass
            for link_key in ("username", "email", "handle"):
                val = rec.get(link_key)
                if val:
                    score += 0.10
                    evidence.append(f"has {link_key} linkage")
                    break
            rec_conf = rec.get("confidence")
            if isinstance(rec_conf, (int, float)) and 0 < float(rec_conf) <= 1:
                score = min(0.99, score * (0.6 + 0.4 * float(rec_conf)))
            out.append({
                "full_name": full,
                "score": round(min(0.99, score), 3),
                "location": _clean(rec.get("location"), 80),
                "employer": _clean(rec.get("employer") or rec.get("company"), 80),
                "age": rec_age,
                "source": _clean(rec.get("source"), 40),
                "evidence": evidence,
                "disqualifiers": disqual,
                "distinguishable": bool(evidence),
            })
        out.sort(key=lambda c: (-c["score"], c["full_name"]))
        # Explicit rank so the UI can show position, not just score.
        for i, c in enumerate(out, 1):
            c["rank"] = i
        return out

    async def _llm_corroboration(self, clusters: list[dict], candidates: list[dict],
                                 deep: bool = False) -> dict[str, Any]:
        """Optionally ask the LLM to judge cluster coherence — advisory only.

        The model may only *comment* on clusters that already exist. Any account,
        field or person it names that is not already in the deterministic output
        is discarded, so it can never invent an identity.

        Args:
            clusters: Deterministic account clusters.
            candidates: Ranked person candidates.
            deep: Force the pass on small inputs.

        Returns:
            dict: ``{"status", "notes", "corroborated", "disputed", "error"}``.
        """
        report: dict[str, Any] = {"status": "skipped", "notes": [], "corroborated": 0,
                                  "disputed": 0, "error": None}
        if not deep and (len(clusters) + len(candidates)) < 3:
            report["status"] = "skipped_insufficient_evidence"
            return report
        try:
            summary = json.dumps({
                "clusters": [{"id": c["id"], "platforms": c["platforms"],
                              "confidence": c["confidence"]}
                             for c in clusters[:8]],
                "candidates": [{"name": c["full_name"], "score": c["score"],
                                "location": c["location"]}
                               for c in candidates[:8]],
            }, ensure_ascii=False)
            prompt = (
                "You are reviewing an OSINT identity-resolution result.\n"
                f"Data:\n{summary}\n\n"
                "For each cluster id, say whether the accounts plausibly belong to "
                "one person. Be conservative. Do NOT propose new accounts, people "
                "or facts that are not in the data.\n"
                'Reply with JSON only: {"assessments":[{"cluster_id":"...",'
                '"verdict":"same_person|different_people|unsure",'
                '"note":"one short clause"}]}'
            )
            kwargs = {"max_tokens": 400, "temperature": 0.0,
                      "schema_hint": '{"assessments":[{"cluster_id":str,'
                                     '"verdict":str,"note":str}]}'}
            fn = getattr(self, "llm_json", None)
            data: Any = None
            if callable(fn):
                data = await asyncio.wait_for(fn(prompt, **kwargs),
                                              timeout=LLM_TIMEOUT_S)
            else:
                from llm import get_llm  # lazy: llm.py is a runtime dep
                data = await asyncio.wait_for(
                    get_llm().complete_json(prompt, **kwargs),
                    timeout=LLM_TIMEOUT_S)
            if isinstance(data, dict) and data.get("error"):
                report["status"] = "unavailable"
                report["error"] = _clean(data.get("error"), 200)
                return report
            items = []
            if isinstance(data, dict):
                items = data.get("assessments") or []
            elif isinstance(data, list):
                items = data
            known = {c["id"] for c in clusters}
            for item in items:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("cluster_id") or "")
                if cid not in known:      # hallucinated cluster id -> discard
                    continue
                verdict = str(item.get("verdict") or "unsure").casefold()
                report["notes"].append({
                    "cluster_id": cid,
                    "verdict": verdict if verdict in
                               ("same_person", "different_people", "unsure")
                               else "unsure",
                    "note": _clean(item.get("note"), 160),
                    "advisory": True,
                })
                if verdict == "same_person":
                    report["corroborated"] += 1
                elif verdict == "different_people":
                    report["disputed"] += 1
            report["status"] = "ok"
        except asyncio.TimeoutError:
            report["status"] = "timeout"
            report["error"] = "llm corroboration pass timed out"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            report["status"] = "error"
            report["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return report

    async def run(self, input_data, context=None):
        """Resolve accounts into person clusters and rank name candidates.

        Args:
            input_data: The target (username, email, phone or person name).
            context: Pipeline context. Uses ``input_type``, ``location``,
                ``employer``, ``age``, ``peer_entities``, ``peer_signals`` and
                ``deep``.

        Returns:
            AgentResult: ``output`` always contains the original ``sherlock``,
            ``people_search``, ``breach_check`` and ``input_type`` keys, plus
            ``account_clusters``, ``person_profile``, ``candidates`` and
            ``llm_corroboration``.
        """
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        input_type = str(context.get("input_type") or "username")
        signals: list[dict] = []
        entities: list[dict] = []
        errors: list[str] = []

        # ── 1. Sherlock username scan (unchanged behaviour) ──────────────────
        sherlock_data: dict = {}
        try:
            from connectors.sherlock import run_sherlock
            self.log(f"Sherlock username scan: {target}")
            sherlock_data = await asyncio.wait_for(run_sherlock(target), timeout=30.0)
            found = sherlock_data.get("count", 0)
            self.log(f"found {found} accounts across platforms")
            for r in sherlock_data.get("results", []) or []:
                site = _clean(r.get("site") or r.get("platform"), 40)
                url = _clean(r.get("url"), 300)
                handle = _clean(r.get("username") or target, 120)
                signals.append({
                    "type": "account", "platform": site, "url": url,
                    "source": "sherlock",
                    "confidence": float(r.get("confidence") or 0.7),
                })
                entities.append({
                    "id": f"account_{site.lower()}_{target}" if site
                    else f"account_{_norm(target)}",
                    "label": f"{handle}@{site}" if site else handle,
                    "type": "username",
                    "value": handle,
                    "confidence": float(r.get("confidence") or 0.7),
                    "source": "sherlock",
                    "attrs": {"platform": site, "url": url},
                })
        except FileNotFoundError:
            self.log("sherlock not installed — run: pip install sherlock-project")
            errors.append("sherlock_not_installed")
        except asyncio.TimeoutError:
            self.log("Sherlock timed out after 30s")
            errors.append("sherlock_timeout")
        except Exception as e:
            self.log(f"Sherlock error: {e}")
            errors.append(f"sherlock_error: {e}"[:200])

        # ── 2. People search (name inputs) ──────────────────────────────────
        people_data: dict = {}
        if input_type == "person_name":
            try:
                from connectors.people_search import run_people_search
                self.log(f"people search: {target}")
                people_data = await asyncio.wait_for(
                    run_people_search(target, location=context.get("location")),
                    timeout=25.0,
                )
                count = people_data.get("count", 0)
                self.log(f"found {count} people records")
                for r in people_data.get("results", []) or []:
                    full = _clean(r.get("full_name") or r.get("name") or target, 120)
                    signals.append({
                        "type": "person_record", "source": r.get("source"),
                        "full_name": full, "age": r.get("age"),
                        "confidence": float(r.get("confidence") or 0.6),
                    })
                    entities.append({
                        "id": f"person_{full.replace(' ', '_').lower()}" if full
                        else f"person_{_norm(target)}",
                        "label": full or target,
                        "type": "person",
                        "value": full or target,
                        "confidence": float(r.get("confidence") or 0.6),
                        "source": _clean(r.get("source"), 40) or "people_search",
                        "attrs": {k: v for k, v in {
                            "location": r.get("location"),
                            "employer": r.get("employer") or r.get("company"),
                            "age": r.get("age"),
                        }.items() if v not in (None, "")},
                    })
            except asyncio.TimeoutError:
                self.log("People search timed out after 25s")
                errors.append("people_search_timeout")
            except Exception as e:
                self.log(f"People search error: {e}")
                errors.append(f"people_search_error: {e}"[:200])

        # ── 3. Breach check (emails / usernames) ────────────────────────────
        breach_data: dict = {}
        if input_type in ("email", "username"):
            try:
                from connectors.breach_check import run_breach_check
                self.log(f"breach check: {target}")
                breach_data = await asyncio.wait_for(
                    run_breach_check(target), timeout=15.0,
                )
                breaches = breach_data.get("total_breaches", 0)
                self.log(f"found {breaches} breach(es)")
                for b in breach_data.get("breaches", []) or []:
                    signals.append({
                        "type": "breach", "name": b.get("name"),
                        "date": b.get("date"), "source": "hibp",
                        "confidence": 0.8,
                    })
            except asyncio.TimeoutError:
                self.log("Breach check timed out after 15s")
                errors.append("breach_check_timeout")
            except Exception as e:
                self.log(f"Breach check error: {e}")
                errors.append(f"breach_check_error: {e}"[:200])

        # ── 4. Cross-platform account clustering ────────────────────────────
        # Accounts come from Sherlock plus any account-shaped peer entity the
        # rest of the swarm already resolved.
        accounts: list[dict] = [e for e in entities
                                if e.get("type") in ("username", "account")]
        for e in (context.get("peer_entities") or []):
            if not isinstance(e, dict):
                continue
            if e.get("type") in ("username", "account") and e.get("value"):
                accounts.append(e)
        clusters: list[dict] = []
        try:
            clusters = self._cluster_accounts(accounts)
            self.log(f"clustered {len(accounts)} account(s) into {len(clusters)} "
                     f"identity cluster(s)")
        except Exception as exc:
            errors.append(f"clustering_error: {exc}"[:200])
            self.log(errors[-1])

        # ── 5. Ranked person-name candidates (the /api/name-search path) ────
        candidates: list[dict] = []
        if input_type == "person_name":
            try:
                candidates = self._rank_candidates(target, people_data.get("results", []), {
                    "location": context.get("location"),
                    "employer": context.get("employer"),
                    "age": context.get("age"),
                })
                self.log(f"ranked {len(candidates)} name candidate(s)")
            except Exception as exc:
                errors.append(f"disambiguation_error: {exc}"[:200])
                self.log(errors[-1])

        # ── 6. Person-record assembly (per-field confidence) ────────────────
        person_profile: dict = {}
        try:
            name_variants: list[str] = [target]
            field_values: dict[str, list[tuple[str, str]]] = defaultdict(list)
            for c in candidates:
                if not c.get("distinguishable"):
                    continue
                if c.get("full_name"):
                    name_variants.append(c["full_name"])
                for field in ("location", "employer", "age", "country"):
                    val = c.get(field if field != "country" else "location")
                    if val not in (None, ""):
                        field_values[field].append((str(val), c.get("source") or "people_search"))
            for e in entities:
                if e.get("type") == "person" and e.get("value"):
                    name_variants.append(str(e["value"]))
                for field in ("location", "employer", "age", "country", "email", "phone"):
                    val = _field(e.get("attrs") or {}, field)
                    if val not in (None, ""):
                        field_values[field].append((str(val), e.get("source") or "?"))
            for cl in clusters:
                if cl.get("single_source") and cl.get("platforms"):
                    continue
                for field in ("email", "phone", "location", "employer"):
                    for m in cl.get("member_details", []):
                        val = (m.get("attrs") or {}).get(field)
                        if val not in (None, ""):
                            field_values[field].append((str(val), m.get("source") or "?"))
            person_profile = self._assemble_person(
                name_variants, field_values,
                [a for c in clusters for a in c.get("members", [])],
                breach_data.get("breaches", []) or [],
            )
            self.log(f"person profile assembled — {len(person_profile.get('fields', {}))} "
                     f"verified field(s), {len(person_profile.get('unverified', []))} "
                     f"unverified, sufficiency: "
                     f"{person_profile.get('evidence_sufficiency')}")
        except Exception as exc:
            errors.append(f"profile_error: {exc}"[:200])
            self.log(errors[-1])

        # ── 7. Optional LLM corroboration (advisory, fully guarded) ─────────
        llm_report: dict[str, Any] = {"status": "disabled", "notes": [],
                                      "corroborated": 0, "disputed": 0, "error": None}
        if not context.get("disable_llm") and ADVISORY_LLM:
            try:
                llm_report = await self._llm_corroboration(
                    clusters, candidates, bool(context.get("deep")))
                self.log(f"llm corroboration: {llm_report.get('status')} "
                         f"(corroborated {llm_report.get('corroborated')}, "
                         f"disputed {llm_report.get('disputed')})")
            except Exception as exc:
                llm_report = {"status": "error", "notes": [], "corroborated": 0,
                              "disputed": 0, "error": f"{type(exc).__name__}: {exc}"[:200]}
                self.log(errors.append(f"llm_error: {exc}"[:200]) or errors[-1])

        has_live = bool(sherlock_data.get("count") or people_data.get("count")
                        or breach_data.get("total_breaches"))
        # Confidence reflects evidence, not optimism: a thin profile must not
        # look as good as a corroborated one just because agents ran.
        confidence = 0.3
        if has_live:
            confidence = 0.6
        strong = [c for c in clusters if c.get("confidence", 0) >= STRONG_THRESHOLD]
        if strong:
            confidence = min(0.92, confidence + 0.1 * len(strong))
        if len(candidates) > 1 and candidates[0]["score"] - candidates[1]["score"] < 0.1:
            confidence = min(confidence, 0.5)  # ambiguous name match
        if person_profile and person_profile.get("evidence_sufficiency") == "thin":
            confidence = min(confidence, 0.55)
        if errors and not has_live:
            confidence = min(confidence, 0.25)

        status = "done"
        if errors and not has_live and not accounts and not candidates:
            status = "partial"

        return AgentResult(
            agent=self.name,
            status=status,
            output={
                # ── keys preserved from the previous implementation ────────
                "sherlock": sherlock_data,
                "people_search": people_data,
                "breach_check": breach_data,
                "input_type": input_type,
                # ── new analytical product ─────────────────────────────────
                "account_clusters": clusters,
                "person_profile": person_profile,
                "candidates": candidates,
                "llm_corroboration": llm_report,
                "errors": errors,
                "stats": {
                    "accounts_examined": len(accounts),
                    "clusters": len(clusters),
                    "strong_clusters": len(strong),
                    "candidates": len(candidates),
                    "distinct_candidates": len([c for c in candidates
                                                if c.get("distinguishable")]),
                    "verified_fields": len(person_profile.get("fields", {})),
                    "unverified_fields": len(person_profile.get("unverified", [])),
                    "breaches": int(breach_data.get("total_breaches") or 0),
                },
            },
            confidence=round(confidence, 3),
            reasoning=(f"Identity resolution: Sherlock={sherlock_data.get('count', 0)} "
                       f"accounts, people={people_data.get('count', 0)}, "
                       f"breaches={breach_data.get('total_breaches', 0)}; "
                       f"{len(accounts)} account(s) clustered into {len(clusters)} "
                       f"identity group(s) ({len(strong)} strong); "
                       f"{len(candidates)} name candidate(s); profile sufficiency "
                       f"{person_profile.get('evidence_sufficiency', 'n/a')}"
                       + (f"; errors: {'; '.join(errors[:2])}" if errors else "")),
            signals=signals,
            entities_found=entities,
            latency_s=self._elapsed(t0),
        )
