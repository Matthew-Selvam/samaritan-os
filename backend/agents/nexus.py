"""
nexus.py — NEXUS Agent (Correlation Engine)
===========================================

Turns the aggregated signals/entities of the primary swarm (injected by APEX as
``context['peer_signals']`` / ``context['peer_entities']``) into a real
analytical product rather than a fan-out stub:

1. **Entity normalisation & resolution** — a confidence-aware union-find over
   blocking keys. Alias variants ("John Smith", "john smith", "J. Smith",
   "john.smith@x.com") collapse toward one canonical node carrying a stable
   ``f"{type}:{normalized}"`` id and an explicit alias set. Provably-distinct
   entities (two different emails, two conflicting countries for one person)
   are **never** hard-merged; they are reported as merge candidates or as
   contradictions instead.
2. **Weighted relationship extraction** — co-occurrence in a signal, shared
   attributes (email domain, city, handle, avatar hash, employer), temporal
   proximity and source co-authoring, each edge typed and weighted with a
   confidence.
3. **Community detection** — deterministic weighted label propagation in pure
   Python (no scipy / networkx), emitting CONTRACTS.md §3 cluster records.
4. **Centrality** — weighted degree + Brandes betweenness approximation +
   PageRank power iteration, combined into a single influence score.
5. **Contradiction detection** — two sources disagreeing about a country,
   carrier, employer, city or age for the same identity. High-value analyst
   output.
6. **Anomaly detection** — single-source entities, isolated nodes, and outliers
   in numeric / categorical attribute distributions.
7. **LLM-assisted relationship inference** — optional, advisory only. Every
   proposed edge is filtered against the real node-id set so the model can
   *propose* links but never *invent* entities. Fully guarded: with no provider
   available every deterministic result is still produced.

Pure Python and dependency-free so it runs in every deployment, including
zero-infrastructure local mode.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .base import BaseAgent, AgentResult

# ── Tunables ───────────────────────────────────────────────────────────────
HARD_MERGE = 0.80          # union-find threshold: above this two records are ONE identity
SOFT_LINK = 0.55           # below this an edge is still emitted but never collapses a cluster
MAX_LLM_NODES = 24         # node list handed to the LLM (keeps prompts inside token_budget)
MIN_LLM_NODES = 8          # below this the LLM pass is not worth its latency
MAX_BETWEENNESS_NODES = 400  # above this Brandes runs on a degree-sampled pivot set
PAGERANK_ITERS = 30
PAGERANK_DAMPING = 0.85

#: Wall-clock ceiling for the advisory LLM pass. NEXUS runs inside APEX's
#: sequential correlation tier, so an unbounded model call would stall the whole
#: pipeline. Overridable for deliberately slow local models.
try:  # pragma: no cover - env parsing
    LLM_TIMEOUT_S = float(os.environ.get("NEXUS_LLM_TIMEOUT_S", "60"))
except (TypeError, ValueError):  # pragma: no cover
    LLM_TIMEOUT_S = 60.0

#: Master switch for the *advisory* LLM passes across the correlation tier.
#: The deterministic product (entities, edges, clusters, centrality,
#: contradictions, anomalies) is complete without it; the model only adds
#: advisory edges. Operators who need minimum latency — or are running a slow
#: local model — disable it and pay nothing.
try:  # pragma: no cover - env parsing
    ADVISORY_LLM = os.environ.get("SIGNAL_OS_ADVISORY_LLM", "true").casefold() \
        not in ("0", "false", "no", "off")
except Exception:  # pragma: no cover
    ADVISORY_LLM = True

#: Evidence weights, per relation type. Deliberately conservative: a bare name
#: match is never enough to hard-merge, corroboration is required.
EDGE_BASE_WEIGHT: dict[str, float] = {
    "alias_of": 0.95,
    "identical_identifier": 0.90,
    "shared_avatar": 0.88,
    "shared_handle": 0.90,
    "llm_inferred": 0.55,
    "cross_platform_handle": 0.55,
    "same_email_domain": 0.50,
    "shared_location": 0.48,
    "shared_employer": 0.42,
    "name_match": 0.45,
    "same_signal": 0.30,
    "source_coauthor": 0.25,
    "temporal_proximity": 0.20,
    "linked_to": 0.20,
}

#: Attributes that are *contradictory* when two sources disagree, with severity.
TRACKED_FIELDS: dict[str, str] = {
    "country": "high", "nationality": "high", "employer": "high", "company": "high",
    "carrier": "high", "org": "high", "city": "medium", "location": "medium",
    "region": "low", "age": "low", "dob": "medium", "line_type": "low",
}

#: Aliases accepted for each tracked attribute, so heterogeneous agents
#: (``carrier`` vs ``mobile_carrier``) still land in the same bucket. Aliases
#: must never collide with structural keys (``type``, ``source``, ``value``) —
#: doing so makes every signal's ``type`` look like a contradicting line_type.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "country": ("country", "country_code", "nation", "nationality"),
    "employer": ("employer", "company", "org", "organisation", "organization",
                 "workplace", "company_name"),
    "city": ("city", "town", "locality", "municipality"),
    "location": ("location", "address", "place", "geo", "gps_location"),
    "region": ("region", "state", "province", "area", "territory"),
    "carrier": ("carrier", "mobile_carrier", "network", "mnc", "operator"),
    "age": ("age", "years_old", "person_age"),
    "dob": ("dob", "date_of_birth", "birth_date", "birthdate"),
    "line_type": ("line_type", "number_type", "sim_type"),
    "platform": ("platform", "site", "network_name", "service"),
    "handle": ("handle", "username", "user_name", "screen_name"),
    "avatar": ("avatar", "avatar_hash", "avatar_url_hash", "image_hash", "photo_hash"),
    "bio": ("bio", "about", "description", "tagline"),
    "followers": ("followers", "follower_count", "followers_count"),
}

_TYPE_ALIASES = {
    "username": "username", "handle": "username", "user": "username",
    "account": "account", "profile": "account", "social": "account",
    "email": "email", "mail": "email", "e-mail": "email",
    "person": "person", "people": "person", "name": "person", "individual": "person",
    "phone": "phone", "msisdn": "phone", "telephone": "phone", "mobile": "phone",
    "domain": "domain", "hostname": "domain", "host": "domain",
    "url": "url", "link": "url", "uri": "url",
    "ip": "ip", "ip_address": "ip",
    "location": "location", "place": "location", "geo": "location", "address": "location",
    "org": "org", "company": "org", "organisation": "org", "organization": "org",
    "image": "image", "photo": "image", "face": "face", "media": "image",
    "wallet": "wallet", "crypto": "crypto", "address_crypto": "crypto",
    "document": "document", "note": "note", "breach": "other", "other": "other",
}

_ENTITY_ID_ATTRS = ("entity_id", "entity_ids", "entities", "entity",
                    "subject", "subject_id", "target", "target_id")

_LOGGER = None


def _log():
    """Return the NEXUS logger, importing observability lazily.

    Returns:
        logging.Logger: Observability logger, or a stdlib logger if
            ``observability`` is unavailable.
    """
    global _LOGGER
    if _LOGGER is None:
        try:
            from observability import get_logger  # lazy: never at module scope
            _LOGGER = get_logger("agents.nexus")
        except Exception:  # pragma: no cover - stdlib fallback
            import logging
            _LOGGER = logging.getLogger("agents.nexus")
    return _LOGGER


# ── Normalisation primitives ────────────────────────────────────────────────
def _strip_accents(text: str) -> str:
    """Remove diacritics so "Müller" and "Muller" collapse to one token.

    Args:
        text: Input string.

    Returns:
        str: NFKD-normalised string with combining marks removed.
    """
    nfkd = unicodedata.normalize("NFKD", str(text))
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _norm_key(text: Any) -> str:
    """Casefold, de-accent and collapse a value into a comparison key.

    Args:
        text: Arbitrary value.

    Returns:
        str: Lowercase alphanumeric key (``""`` for empty input).
    """
    s = _strip_accents(text or "").casefold()
    return re.sub(r"[^a-z0-9]+", "", s)


def _norm_words(text: Any) -> list[str]:
    """Tokenise a value into lowercase word tokens.

    Args:
        text: Arbitrary value (may be ``None``).

    Returns:
        list[str]: Alphanumeric tokens, order preserved.
    """
    s = _strip_accents(text or "").casefold()
    return re.findall(r"[a-z0-9]+", s)


def _name_tokens(text: Any) -> list[str]:
    """Tokenise a person name, dropping honorifics and generational suffixes.

    Args:
        text: Person name, e.g. ``"Dr. John Q. Smith III"``.

    Returns:
        list[str]: Meaningful name tokens.
    """
    drop = {"dr", "mr", "mrs", "ms", "prof", "sir", "madam", "jr", "sr",
            "ii", "iii", "iv", "phd", "md", "mister", "miss", "mx"}
    return [t for t in _norm_words(text) if t not in drop]


def _email_parts(value: Any) -> tuple[str, str]:
    """Split an email into (local, domain), lowercased.

    Args:
        value: Candidate email string.

    Returns:
        tuple[str, str]: ``(local, domain)``; ``("", "")`` when not an email.
    """
    s = str(value or "").strip().casefold()
    if "@" not in s:
        return "", ""
    local, _, domain = s.partition("@")
    domain = re.sub(r"^www\.", "", domain)
    return local, domain


def _local_name_tokens(value: Any) -> list[str]:
    """Derive person-name tokens from an identifier (email local part, handle).

    Args:
        value: Identifier such as ``john.smith@x.com`` or ``john_smith``.

    Returns:
        list[str]: Tokens found in the identifier.
    """
    local, _ = _email_parts(value)
    if not local:
        local = str(value or "")
    toks = re.split(r"[^A-Za-z0-9]+", local)
    return [t.casefold() for t in toks if t]


def _canonical_id(etype: str, value: Any) -> str:
    """Build the stable, deterministic entity id required by CONTRACTS §3.

    Args:
        etype: Normalised entity type.
        value: Raw entity value.

    Returns:
        str: ``f"{type}:{normalized_value}"``; stable across runs and inputs.
    """
    return f"{etype}:{_norm_key(value) or _norm_key(str(value))}"


def _norm_type(raw: Any) -> str:
    """Map a heterogeneous agent-supplied type onto the canonical vocabulary.

    Args:
        raw: Reported entity/signal type.

    Returns:
        str: Canonical type string (falls back to ``"other"``).
    """
    key = str(raw or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return _TYPE_ALIASES.get(key, key or "other")


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Jaccard similarity of two token sets.

    Args:
        a: Iterable of tokens.
        b: Iterable of tokens.

    Returns:
        float: ``0.0``–``1.0`` overlap.
    """
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _parse_ts(value: Any) -> Optional[float]:
    """Tolerantly parse a timestamp into epoch seconds.

    Handles ISO-8601 (with ``Z`` or offset), ``YYYY-MM-DD``, ``MMM YYYY``,
    ``YYYY``, epoch seconds/milliseconds and simple relative phrases.

    Args:
        value: Raw timestamp from a signal or entity.

    Returns:
        float | None: Epoch seconds, or ``None`` when unparseable.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{9,14}", s):
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    rel = re.fullmatch(r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s*ago",
                       s.casefold())
    if rel:
        mult = {"second": 1, "minute": 60, "hour": 3600, "day": 86400,
                "week": 604800, "month": 2592000, "year": 31536000}
        now = datetime.now(timezone.utc).timestamp()
        return now - int(rel.group(1)) * mult[rel.group(2)]
    iso = s.replace("Z", "+00:00").replace("z", "+00:00")
    for candidate in (iso, iso.replace(" ", "T")):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    for fmt in ("%b %Y", "%B %Y", "%b %d %Y", "%B %d %Y", "%d %b %Y",
                "%Y-%m", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _fmt_ts(epoch: Optional[float]) -> str:
    """Format epoch seconds as an ISO-8601 UTC string.

    Args:
        epoch: Epoch seconds, or ``None``.

    Returns:
        str: ISO-8601 string, or ``""`` when ``epoch`` is ``None``.
    """
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return ""


def _hash(value: Any) -> str:
    """Stable short hash of any value (used for opaque id fragments).

    Args:
        value: Arbitrary value.

    Returns:
        str: 10-char hex digest of the repr.
    """
    return hashlib.sha1(str(value).encode("utf-8", "replace")).hexdigest()[:10]


# ── Confidence-aware union-find ────────────────────────────────────────────
class _UnionFind:
    """Union-find whose merges are gated by a confidence threshold.

    Records every *attempted* merge with the evidence that justified (or
    failed to justify) it, so the merge log is auditable rather than opaque.

    Args:
        threshold: Minimum confidence required to actually union two roots.
    """

    def __init__(self, threshold: float = HARD_MERGE) -> None:
        """Initialise an empty partition.

        Args:
            threshold: Minimum confidence required to merge two roots.
        """
        self.threshold = threshold
        self._parent: dict[str, str] = {}
        self.merged: list[dict] = []
        self.rejected: list[dict] = []

    def add(self, key: str) -> None:
        """Register a node id (idempotent).

        Args:
            key: Node id to register.
        """
        self._parent.setdefault(key, key)

    def find(self, key: str) -> str:
        """Return the root of ``key``'s set (path-compressed).

        Args:
            key: Node id.

        Returns:
            str: Root id.
        """
        self.add(key)
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:  # path compression
            self._parent[key], key = root, self._parent[key]
        return root

    def union(self, a: str, b: str, confidence: float,
              evidence: str = "", blocked: bool = False) -> bool:
        """Merge two sets if the evidence is strong enough.

        Args:
            a: First node id.
            b: Second node id.
            confidence: Evidence confidence in ``0.0``–``1.0``.
            evidence: Human-readable justification.
            blocked: When True the pair is provably distinct and is never merged
                regardless of confidence.

        Returns:
            bool: True when the two sets were merged.
        """
        self.add(a)
        self.add(b)
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        if blocked or confidence < self.threshold:
            self.rejected.append({
                "a": a, "b": b, "confidence": round(confidence, 3),
                "evidence": evidence or "insufficient evidence",
                "reason": "provably_distinct" if blocked else "below_threshold",
            })
            return False
        # Deterministic direction: the lexicographically smaller root wins, so
        # cluster ids are stable regardless of entity ordering.
        lo, hi = sorted((ra, rb))
        self._parent[hi] = lo
        self.merged.append({"a": a, "b": b, "confidence": round(confidence, 3),
                            "evidence": evidence})
        return True

    def groups(self) -> dict[str, list[str]]:
        """Materialise the current partition.

        Returns:
            dict[str, list[str]]: Root id -> sorted member ids.
        """
        out: dict[str, list[str]] = defaultdict(list)
        for key in self._parent:
            out[self.find(key)].append(key)
        return {k: sorted(v) for k, v in sorted(out.items())}


# ── Relationship + clustering algorithms (pure Python) ─────────────────────
def _label_propagation(adj: dict[str, dict[str, float]], max_iter: int = 12
                       ) -> dict[str, str]:
    """Deterministic weighted label propagation.

    Nodes are processed in sorted order and ties break towards the
    lexicographically smaller label, which makes the output reproducible — the
    default label-propagation implementation is order-dependent and would make
    reports unstable between runs.

    Args:
        adj: Weighted undirected adjacency ``node -> {neighbour: weight}``.
        max_iter: Maximum synchronous update rounds.

    Returns:
        dict[str, str]: Node id -> community label.
    """
    labels = {n: n for n in adj}
    if not labels:
        return labels
    order = sorted(adj)
    for _ in range(max_iter):
        changed = False
        for node in order:
            neighbours = adj.get(node) or {}
            if not neighbours:
                continue
            score: dict[str, float] = defaultdict(float)
            for nb, w in neighbours.items():
                if nb in labels:
                    score[labels[nb]] += max(w, 0.0)
            if not score:
                continue
            best = min(score.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if best != labels[node]:
                labels[node] = best
                changed = True
        if not changed:
            break
    return labels


def _pagerank(adj: dict[str, dict[str, float]], damping: float = PAGERANK_DAMPING,
              iters: int = PAGERANK_ITERS) -> dict[str, float]:
    """PageRank by power iteration over a weighted undirected graph.

    Args:
        adj: Weighted undirected adjacency.
        damping: Damping factor.
        iters: Iteration count.

    Returns:
        dict[str, float]: Node id -> PageRank, summing to ~1.0.
    """
    n = len(adj)
    if not n:
        return {}
    base = 1.0 / n
    rank = {node: base for node in adj}
    for _ in range(iters):
        nxt: dict[str, float] = {}
        dangling = 0.0
        for node, neighbours in adj.items():
            out_w = sum(neighbours.values())
            if out_w <= 0:
                dangling += rank[node]
            else:
                for nb, w in neighbours.items():
                    nxt[nb] = nxt.get(nb, 0.0) + rank[node] * w / out_w
        total = sum(nxt.values()) + dangling
        if total <= 0:
            break
        # Dangling rank is redistributed uniformly across all nodes.
        updated = {
            node: (1.0 - damping) / n
            + damping * (nxt.get(node, 0.0) + dangling) / total
            for node in adj
        }
        delta = sum(abs(updated[node] - rank[node]) for node in adj)
        rank = updated
        if delta < 1e-6:
            break
    return rank


def _betweenness(adj: dict[str, dict[str, float]],
                 max_nodes: int = MAX_BETWEENNESS_NODES) -> dict[str, float]:
    """Brandes betweenness centrality with pivoting for large graphs.

    For graphs above ``max_nodes`` the algorithm runs from a degree-sampled
    pivot set rather than every node — a standard, documented approximation.

    Args:
        adj: Weighted undirected adjacency (weights are used for traversal
            strength only, not path counting).
        max_nodes: Pivot-set budget.

    Returns:
        dict[str, float]: Raw (unnormalised) betweenness per node.
    """
    if not adj:
        return {}
    nodes = sorted(adj)
    if len(nodes) <= max_nodes:
        pivots = nodes
    else:
        ranked = sorted(nodes, key=lambda x: (-len(adj[x]), x))[:max_nodes]
        pivots = sorted(ranked)
    score: dict[str, float] = {n: 0.0 for n in nodes}
    for s in pivots:
        stack: list[str] = []
        preds: dict[str, list[str]] = {n: [] for n in nodes}
        sigma = {n: 0.0 for n in nodes}
        dist = {n: -1 for n in nodes}
        sigma[s] = 1.0
        dist[s] = 0
        queue: deque[str] = deque([s])
        while queue:
            v = queue.popleft()
            stack.append(v)
            for w in (adj.get(v) or {}):
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    preds[w].append(v)
        delta = {n: 0.0 for n in nodes}
        while stack:
            w = stack.pop()
            for v in preds[w]:
                if sigma[w]:
                    delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != s:
                score[w] += delta[w]
    return score


def _norm_scores(scores: dict[str, float]) -> dict[str, float]:
    """Scale a score map into ``0.0``–``1.0`` by its own maximum.

    Args:
        scores: Raw scores.

    Returns:
        dict[str, float]: Normalised scores.
    """
    if not scores:
        return {}
    top = max(scores.values())
    if top <= 0:
        return {k: 0.0 for k in scores}
    return {k: round(v / top, 4) for k, v in scores.items()}


class NexusAgent(BaseAgent):
    """Correlation Engine — entity resolution + weighted graph + analytics.

    Consumes the aggregated signals/entities of the primary swarm and returns a
    de-duplicated knowledge graph with resolved identities, weighted typed
    edges, communities, centrality rankings, contradictions and anomalies.
    """

    name = "NEXUS"
    role = "Correlation Engine"
    icon = "∞"
    description = ("Hidden relationship detection, entity resolution, graph "
                   "clustering, influence scoring, contradiction & anomaly "
                   "detection, semantic linking")
    preferred_models = ["qwen2.5vl:7b", "gemma2:9b", "qwen2.5:7b"]
    token_budget = 12288

    # ── Small helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _slug(text: str) -> str:
        """Slugify a label for use in an id fragment.

        Args:
            text: Arbitrary text.

        Returns:
            str: Lowercase slug (``"unknown"`` when empty).
        """
        return "".join(c if c.isalnum() else "_" for c in str(text).lower()).strip("_") or "unknown"

    @staticmethod
    def _as_float(value: Any, default: float = 0.0) -> float:
        """Coerce a value to float within ``0.0``–``1.0``.

        Args:
            value: Candidate number.
            default: Value used when coercion fails.

        Returns:
            float: Clamped float.
        """
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
        if v != v:  # NaN
            return default
        return max(0.0, min(1.0, v))

    @staticmethod
    def _collect_fields(record: dict, out: dict[str, list[tuple[str, Any]]]) -> None:
        """Harvest tracked attributes from a record and its ``attrs`` sub-dict.

        Args:
            record: Entity or signal dict.
            out: Accumulator mapping field -> ``[(source, value), ...]``.
        """
        bags = [record]
        nested = record.get("attrs")
        if isinstance(nested, dict):
            bags.append(nested)
        for bag in bags:
            for field, keys in FIELD_ALIASES.items():
                for key in keys:
                    if key not in bag:
                        continue
                    val = bag.get(key)
                    if val in (None, "", [], {}):
                        continue
                    if isinstance(val, (list, tuple, set)):
                        for item in val:
                            if item not in (None, ""):
                                out.setdefault(field, []).append(
                                    (str(bag.get("source", record.get("source", "?"))), item))
                    else:
                        out.setdefault(field, []).append(
                            (str(bag.get("source", record.get("source", "?"))), val))

    @staticmethod
    def _signal_entities(sig: dict, value_index: dict[str, list[str]],
                         id_index: dict[str, str]) -> list[str]:
        """Map a signal onto the entity ids it references.

        Tries, in order: explicit entity-id attributes, the signal's own
        value, and any attribute value that matches a known entity.

        Args:
            sig: Signal dict.
            value_index: Normalised value -> node ids.
            id_index: Raw entity id -> node id.

        Returns:
            list[str]: Sorted node ids referenced by the signal.
        """
        found: set[str] = set()
        bags = [sig]
        nested = sig.get("attrs")
        if isinstance(nested, dict):
            bags.append(nested)
        for bag in bags:
            for key in _ENTITY_ID_ATTRS:
                raw = bag.get(key)
                if not raw:
                    continue
                items = raw if isinstance(raw, (list, tuple, set)) else [raw]
                for item in items:
                    if not isinstance(item, str):
                        item = str(getattr(item, "id", "") or "")
                    if not item:
                        continue
                    hit = id_index.get(item)
                    if hit:
                        found.add(hit)
                    else:
                        found.update(value_index.get(_norm_key(item), ()))
        for bag in bags:
            for val in (bag.get("value"), bag.get("url"), bag.get("platform")):
                if isinstance(val, (str, int, float)):
                    found.update(value_index.get(_norm_key(val), ()))
        return sorted(found)

    # ── Entity resolution ───────────────────────────────────────────────────
    def _resolve(self, entities: list[dict], signals: list[dict], log,
                 uf: _UnionFind) -> tuple[dict[str, dict], list[dict]]:
        """Normalise, de-duplicate and resolve peer entities into identities.

        Merges are written directly into the caller-supplied union-find so the
        reported partition, the merge log and the contradiction groups are all
        derived from exactly the same structure.

        Args:
            entities: Raw peer entity dicts.
            signals: Raw peer signals, used for value→entity linkage.
            log: Callable for progress logging.
            uf: Union-find to merge into.

        Returns:
            tuple: ``(nodes, candidates)`` where ``nodes`` maps node id ->
            canonical record with ``aliases``, and ``candidates`` are soft links
            that were deliberately NOT merged.
        """
        nodes: dict[str, dict] = {}
        raw_by_key: dict[str, list[str]] = defaultdict(list)
        candidates: list[dict] = []

        # 1. Normalise every raw entity into a canonical record.
        for idx, ent in enumerate(entities or []):
            if not isinstance(ent, dict):
                continue
            etype = _norm_type(ent.get("type") or "other")
            raw_value = ent.get("value") or ent.get("label") or ent.get("id") or ""
            label = str(ent.get("label") or raw_value or "unknown")[:120]
            conf = self._as_float(ent.get("confidence"), 0.5) or 0.5
            source = str(ent.get("source") or "unknown")
            nid = _canonical_id(etype, raw_value)
            if not _norm_key(raw_value):
                # Nothing identifying to key on — keep it addressable but isolated.
                nid = f"{etype}:unknown-{_hash((idx, label))}"
            rec = nodes.get(nid)
            if rec is None:
                rec = {
                    "id": nid,
                    "canonical_id": nid,
                    "label": label,
                    "type": etype,
                    "value": str(raw_value)[:200],
                    "confidence": conf,
                    "sources": [source],
                    "aliases": [],
                    "attrs": {},
                    "first_seen": ent.get("first_seen") or "",
                    "last_seen": ent.get("last_seen") or "",
                    "entity_ids": [],
                    "raw_count": 1,   # total raw records folded into this node
                }
                nodes[nid] = rec
            else:
                rec["raw_count"] = rec.get("raw_count", 1) + 1
                if conf > rec["confidence"]:
                    rec["confidence"] = conf
                if source not in rec["sources"]:
                    rec["sources"].append(source)
            if ent.get("id") and ent["id"] not in rec["entity_ids"]:
                rec["entity_ids"].append(str(ent["id"]))
            for alias in (ent.get("aliases") if isinstance(ent.get("aliases"), list) else []):
                if str(alias) not in rec["aliases"]:
                    rec["aliases"].append(str(alias)[:120])
            if label != rec["label"] and label not in rec["aliases"]:
                rec["aliases"].append(label)
            if str(raw_value) != rec["value"] and str(raw_value) not in rec["aliases"]:
                rec["aliases"].append(str(raw_value)[:200])
            # Merge attrs (first writer wins per key, lists accumulate) while
            # recording *every* value with its source — conflicting values are
            # the raw material for contradiction detection, so they must be
            # preserved rather than overwritten.
            for bag in (ent, ent.get("attrs") if isinstance(ent.get("attrs"), dict) else {}):
                if not isinstance(bag, dict):
                    continue
                bag_source = str(bag.get("source", source))
                for k, v in bag.items():
                    if k in ("id", "label", "type", "value", "confidence", "source",
                             "aliases", "first_seen", "last_seen"):
                        continue
                    if v in (None, "", [], {}):
                        continue
                    rec.setdefault("attr_provenance", {}).setdefault(k, []).append(
                        (bag_source, v))
                    if k not in rec["attrs"]:
                        rec["attrs"][k] = v
            ts = _parse_ts(ent.get("first_seen"))
            if ts and (not rec["first_seen"] or ts < (_parse_ts(rec["first_seen"]) or ts)):
                rec["first_seen"] = ent.get("first_seen")
            ts = _parse_ts(ent.get("last_seen"))
            if ts and (not rec["last_seen"] or ts > (_parse_ts(rec["last_seen"]) or ts)):
                rec["last_seen"] = ent.get("last_seen")
            raw_by_key[_norm_key(raw_value)].append(nid)
            uf.add(nid)

        log(f"normalised {len(entities or [])} raw entities → {len(nodes)} unique node(s)")

        # 2. Deterministic blocking-key merges.
        #    (a) byte-identical normalised values of the same type.
        for key, ids in raw_by_key.items():
            if not key or len(ids) < 2:
                continue
            head, rest = ids[0], ids[1:]
            for other in rest:
                uf.union(head, other, 0.95, f"identical normalized value '{key}'")

        #    (b) person-name blocking: same surname + first initial.
        person_ids = [n for n, r in nodes.items() if r["type"] == "person"]
        blocks: dict[str, list[str]] = defaultdict(list)
        for nid in person_ids:
            toks = _name_tokens(nodes[nid]["value"]) or _name_tokens(nodes[nid]["label"])
            if len(toks) >= 2:
                blocks[f"{toks[-1]}|{toks[0][0]}"].append(nid)
            elif toks:
                blocks[f"{toks[0]}|"].append(nid)
        for block, ids in sorted(blocks.items()):
            if len(ids) < 2:
                continue
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    score, ev, blocked = self._name_match(nodes[a], nodes[b])
                    if score >= SOFT_LINK:
                        candidates.append({"a": a, "b": b, "type": "name_match",
                                           "confidence": score, "evidence": ev,
                                           "merged": uf.union(a, b, score, ev, blocked)})

        #    (c) identifier-derived person linkage (email local part / handle).
        for nid, rec in list(nodes.items()):
            if rec["type"] in ("email", "username", "account"):
                continue
            idents = self._identifiers(rec)
            for other, rec2 in nodes.items():
                if other == nid or rec2["type"] not in ("email", "username", "account"):
                    continue
                local_toks = _local_name_tokens(rec2["value"])
                if not local_toks:
                    continue
                base = idents.get("name_tokens") or []
                overlap = _jaccard(local_toks, base)
                contains = bool(base) and len(local_toks) <= 2 and all(
                    t in base for t in local_toks)
                if not (contains or overlap >= 0.66):
                    continue
                score, ev, blocked = self._identifier_match(
                    rec, rec2, overlap, contains, local_toks)
                if score >= SOFT_LINK:
                    candidates.append({"a": nid, "b": other, "type": "name_to_identifier",
                                       "confidence": score, "evidence": ev,
                                       "merged": uf.union(nid, other, score, ev, blocked)})

        #    (d) hard prohibition: two different emails are two accounts.
        emails = [n for n, r in nodes.items() if r["type"] == "email"]
        for i, a in enumerate(emails):
            for b in emails[i + 1:]:
                if uf.find(a) == uf.find(b):
                    continue
                uf.union(a, b, 0.0,
                         "distinct email addresses are distinct accounts until "
                         "evidence says otherwise", blocked=True)

        merged = sum(1 for c in candidates if c["merged"])
        log(f"entity resolution: {len(uf.merged)} identity merge(s) above threshold, "
            f"{len(candidates) - merged} candidate link(s) held back, "
            f"{len(uf.rejected)} rejected pair(s)")
        return nodes, candidates

    def _collapse(self, nodes: dict[str, dict], uf: _UnionFind,
                  log) -> tuple[dict[str, dict], list[dict]]:
        """Collapse union-find groups into single canonical nodes.

        This is what turns "several records that look like one person" into
        *one* person node carrying a stable canonical id, the full alias set and
        every source that reported it. A group whose members were already the
        same id is recorded as a ``dedup`` merge so the count reflects work done.

        Args:
            nodes: Canonical node records keyed by node id.
            uf: Union-find holding the resolved partition.
            log: Callable for progress logging.

        Returns:
            tuple: ``(collapsed_nodes, identity_records)``. Identity records
            describe every collapsed group, including single-member dedups.
        """
        groups = uf.groups()
        member_of: dict[str, str] = {}
        for root, members in groups.items():
            for m in members:
                member_of[m] = root
        # Re-root: a group's canonical id is the min member id, so the id is
        # stable regardless of the order entities arrived in.
        root_canonical: dict[str, str] = {}
        for root, members in groups.items():
            present = sorted(m for m in members if m in nodes)
            root_canonical[root] = present[0] if present else root

        collapsed: dict[str, dict] = {}
        identities: list[dict] = []
        for root, members in sorted(groups.items()):
            present = [m for m in members if m in nodes]
            if not present:
                continue
            cid = root_canonical[root]
            primary = nodes[cid]
            aliases: list[str] = []
            sources: list[str] = []
            attrs: dict[str, Any] = {}
            provenance: dict[str, list] = {}
            values: list[str] = []
            types: list[str] = []
            platforms: list[str] = []
            first_seen, last_seen = "", ""
            confidence = primary.get("confidence", 0.5)
            for m in present:
                rec = nodes[m]
                for a in rec.get("aliases", []):
                    if a not in aliases and _norm_key(a) != _norm_key(rec.get("value")):
                        aliases.append(str(a)[:120])
                if str(rec.get("value")) != str(primary.get("value")) \
                        and str(rec.get("value")) not in values:
                    values.append(str(rec.get("value"))[:200])
                for s in rec.get("sources", []):
                    if s not in sources:
                        sources.append(s)
                for k, v in (rec.get("attrs") or {}).items():
                    if k not in attrs:
                        attrs[k] = v
                for k, entries in (rec.get("attr_provenance") or {}).items():
                    provenance.setdefault(k, []).extend(entries)
                if rec.get("type") not in types:
                    types.append(str(rec.get("type")))
                for key in ("platform", "site"):
                    for prov_key, entries in (rec.get("attr_provenance") or {}).items():
                        if prov_key not in key and not (key == "site"
                                                       and prov_key == "platform"):
                            continue
                        for _, pv in entries:
                            if pv not in (None, "") and str(pv) not in platforms:
                                platforms.append(str(pv))
                    pv = (rec.get("attrs") or {}).get(key)
                    if pv and str(pv) not in platforms:
                        platforms.append(str(pv))
                ts = _parse_ts(rec.get("first_seen"))
                if ts and (not first_seen or ts < (_parse_ts(first_seen) or ts)):
                    first_seen = str(rec.get("first_seen"))
                ts = _parse_ts(rec.get("last_seen"))
                if ts and (not last_seen or ts > (_parse_ts(last_seen) or ts)):
                    last_seen = str(rec.get("last_seen"))
                confidence = max(confidence, rec.get("confidence", 0.5))
                for rid in rec.get("entity_ids", ()):
                    if rid not in primary["entity_ids"]:
                        primary["entity_ids"].append(rid)
            aliases = sorted(set(aliases) | set(values))
            rec_out = dict(primary)
            rec_out.update({
                "id": cid, "canonical_id": cid, "aliases": aliases,
                "sources": sources, "attrs": attrs,
                "attr_provenance": provenance,
                "first_seen": first_seen, "last_seen": last_seen,
                "confidence": round(confidence, 3),
                "merged_from": present,
                "variant_types": types,
            })
            if platforms:
                rec_out.setdefault("attrs", {}).setdefault("platforms", platforms)
            collapsed[cid] = rec_out
            if len(present) > 1:
                identities.append({
                    "canonical_id": cid,
                    "label": primary.get("label", cid),
                    "type": primary.get("type", "other"),
                    "members": present,
                    "merged_from": [m for m in present if m != cid],
                    "aliases": aliases,
                    "size": len(present),
                    "sources": sources,
                    "platforms": platforms,
                    "merge_kind": "identity",
                })
        # A single-member group that arrived more than once is a pure dedup —
        # no cross-entity evidence was needed, the records were identical.
        collapsed_ids = {i["canonical_id"] for i in identities}
        for nid, rec in nodes.items():
            if nid in collapsed_ids or rec.get("raw_count", 1) <= 1:
                continue
            identities.append({
                "canonical_id": nid, "label": rec.get("label", nid),
                "type": rec.get("type", "other"), "members": [nid],
                "merged_from": [], "aliases": rec.get("aliases", []),
                "size": 1, "sources": rec.get("sources", []),
                "platforms": [], "merge_kind": "dedup",
                "duplicates_collapsed": rec.get("raw_count", 1),
            })
        log(f"collapsed {len(nodes)} node(s) into {len(collapsed)} identity node(s) "
            f"({len(identities)} identity group(s))")
        return collapsed, identities

    @staticmethod
    def _identifiers(rec: dict) -> dict[str, Any]:
        """Extract comparable identifier features from a node record.

        Args:
            rec: Canonical node record.

        Returns:
            dict: ``{"name_tokens", "email_domain", "handle", "city", ...}``.
        """
        attrs = rec.get("attrs") or {}
        local, domain = _email_parts(rec.get("value"))
        name_tokens = _name_tokens(rec.get("value")) or _name_tokens(rec.get("label"))
        if not name_tokens and local:
            name_tokens = _local_name_tokens(local)
        city = ""
        for key in ("city", "location", "place", "address", "region", "country"):
            val = attrs.get(key)
            if val:
                city = _norm_key(val)
                break
        employer = ""
        for key in ("employer", "company", "org", "organisation", "organization"):
            val = attrs.get(key)
            if val:
                employer = _norm_key(val)
                break
        return {"name_tokens": name_tokens, "email_domain": domain, "handle": local,
                "local": local, "city": city, "employer": employer,
                "label": _norm_key(rec.get("label")), "type": rec.get("type"),
                "platform": str(attrs.get("platform") or attrs.get("site") or "").casefold(),
                "avatar": _norm_key(attrs.get("avatar") or attrs.get("avatar_hash")
                                    or attrs.get("image_hash") or ""),
                "bio": str(attrs.get("bio") or attrs.get("about")
                           or attrs.get("description") or "").strip().casefold()}

    @staticmethod
    def _initial_match(ta: list[str], tb: list[str]) -> bool:
        """Detect an abbreviated given name against a full one.

        ``["j", "smith"]`` matches ``["john", "smith"]`` but ``["jo", "smith"]``
        does not match ``["jan", "smith"]`` — a two-letter stem must be a prefix.

        Args:
            ta: First name token list.
            tb: Second name token list.

        Returns:
            bool: True when one side is a compatible abbreviation of the other.
        """
        if len(ta) < 2 or len(tb) < 2:
            return False
        if ta[-1] != tb[-1]:
            return False
        ga = [t for t in ta[:-1] if t]
        gb = [t for t in tb[:-1] if t]
        if not ga or not gb:
            return False

        def abbrev(short: str, long: str) -> bool:
            """Is ``short`` a usable abbreviation of ``long``?

            Args:
                short: Candidate abbreviation.
                long: Full name token.

            Returns:
                bool: True when the abbreviation can stand for the full token.
            """
            return len(short) == 1 or (len(short) <= 3 and long.startswith(short))

        return any(abbrev(a, b) or abbrev(b, a) for a in ga for b in gb)

    def _name_match(self, a: dict, b: dict) -> tuple[float, str, bool]:
        """Score two person records for identity, and flag hard contradictions.

        Args:
            a: First person record.
            b: Second person record.

        Returns:
            tuple: ``(confidence, evidence, blocked)`` where ``blocked`` marks a
            provable contradiction that must never be merged.
        """
        ia, ib = self._identifiers(a), self._identifiers(b)
        ta, tb = ia["name_tokens"], ib["name_tokens"]
        if not ta or not tb:
            return 0.0, "no name tokens", True
        if ta == tb:
            score, ev = 0.92, f"identical full name ({' '.join(ta)})"
        elif len(ta) >= 2 and len(tb) >= 2 and ta[0] == tb[0] and ta[-1] == tb[-1]:
            score, ev = 0.62, f"same first+surname, differing middle name ({' '.join(ta)})"
        elif ta[-1] == tb[-1] and self._initial_match(ta, tb):
            # "J. Smith" vs "John Smith": an initial that agrees with the first
            # letter of the other given name. Suggestive on its own, decisive
            # once a location or employer corroborates.
            score = 0.70
            ev = (f"same surname '{ta[-1]}' and matching given-name initial "
                  f"({' '.join(ta)} / {' '.join(tb)})")
        elif len(ta) == 1 or len(tb) == 1:
            score, ev = 0.40, f"partial name match ({' '.join(ta)} / {' '.join(tb)})"
        else:
            score, ev = 0.0, f"different names ({' '.join(ta)} / {' '.join(tb)})"
            if ta[-1] == tb[-1]:
                return score, ev, False   # same surname, different given name
            return score, ev, True        # different surnames: do not merge
        if score <= 0:
            return score, ev, False
        # Corroboration lifts the score; contradiction vetoes it.
        bonus, notes = 0.0, []
        if ia["city"] and ia["city"] == ib["city"]:
            bonus += 0.22
            notes.append("same location")
        if ia["employer"] and ia["employer"] == ib["employer"]:
            bonus += 0.20
            notes.append("same employer")
        if ia["email_domain"] and ia["email_domain"] == ib["email_domain"]:
            bonus += 0.12
            notes.append(f"same email domain ({ia['email_domain']})")
        if a.get("attrs", {}).get("avatar") and a["attrs"]["avatar"] == b.get("attrs", {}).get("avatar"):
            bonus += 0.25
            notes.append("identical avatar hash")
        _, veto = self._check_contradiction(a, b, ia, ib)
        if veto:
            return 0.0, ev + "; " + veto, True
        final = max(0.0, min(0.99, score + bonus))
        if notes:
            ev += " + " + ", ".join(notes)
        return final, ev, False

    @staticmethod
    def _cross_platform_identity(a: dict, b: dict, fa: dict, fb: dict) -> tuple[float, str]:
        """Score whether two social accounts belong to the same person.

        Weights the signals an analyst actually uses: a shared avatar hash is
        near-decisive, a near-identical bio is strong support, and a
        cross-referenced handle (the same handle on a different platform) is a
        weak prior on its own.

        Args:
            a: First account record.
            b: Second account record.
            fa: Identifier features of ``a``.
            fb: Identifier features of ``b``.

        Returns:
            tuple: ``(confidence, evidence)``; ``(0.0, reason)`` when the two
            accounts are on the same platform or nothing links them.
        """
        pa, pb = fa.get("platform"), fb.get("platform")
        if pa and pb and pa == pb:
            return 0.0, "same platform — not evidence of a single person"
        score, notes = 0.0, []
        if fa.get("avatar") and fa["avatar"] == fb.get("avatar"):
            score += 0.75
            notes.append("identical avatar hash")
        if fa.get("bio") and fb.get("bio"):
            sim = _jaccard(_norm_words(fa["bio"]), _norm_words(fb["bio"]))
            if sim >= 0.5:
                score += round(0.45 * sim, 3)
                notes.append(f"bios {sim:.0%} similar")
        fa_h = fa.get("handle") or ""
        fb_h = fb.get("handle") or ""
        if fa_h and fb_h and fa_h == fb_h:
            score += 0.3
            notes.append(f"same handle '{fa_h}' on {pa or '?'} and {pb or '?'}")
        elif fa_h and fb_h and (fa_h in fb_h or fb_h in fa_h):
            score += 0.18
            notes.append(f"handle variant '{fa_h}'/'{fb_h}'")
        # A cross-reference in one bio naming the other's handle is decisive.
        if fa.get("bio") and fb_h and fb_h in fa["bio"] and len(fb_h) > 3:
            score += 0.4
            notes.append(f"bio cross-references @{fb_h}")
        if fb.get("bio") and fa_h and fa_h in fb["bio"] and len(fa_h) > 3:
            score += 0.4
            notes.append(f"bio cross-references @{fa_h}")
        if not notes:
            return 0.0, "no cross-platform evidence"
        return min(0.97, score), " + ".join(notes)

    def _identifier_match(self, person: dict, ident: dict, overlap: float,
                          contains: bool, local_toks: list[str]
                          ) -> tuple[float, str, bool]:
        """Score a person↔identifier (email/handle) link.

        Args:
            person: Candidate person record.
            ident: Email/username/account record.
            overlap: Token overlap between identifier and name.
            contains: True when every identifier token appears in the name.
            local_toks: Tokens derived from the identifier.

        Returns:
            tuple: ``(confidence, evidence, blocked)``.
        """
        ip = self._identifiers(person)
        ii = self._identifiers(ident)
        score = (0.66 if contains else 0.45 * overlap)
        notes = [f"identifier '{ident.get('value')}' matches name tokens {local_toks}"]
        if ip["email_domain"] and ip["email_domain"] == ii["email_domain"]:
            score += 0.20
            notes.append("same email domain")
        if ip["city"] and ip["city"] == ii["city"]:
            score += 0.15
            notes.append("same city")
        if ip["employer"] and ip["employer"] == ii["employer"]:
            score += 0.15
            notes.append("same employer")
        _, veto = self._check_contradiction(person, ident, ip, ii)
        if veto:
            return 0.0, "; ".join(notes) + "; " + veto, True
        return max(0.0, min(0.98, score)), "; ".join(notes), False

    def _check_contradiction(self, a: dict, b: dict, ia: dict,
                             ib: dict) -> tuple[bool, str]:
        """Detect provable identity conflicts between two records.

        Args:
            a: First record.
            b: Second record.
            ia: Identifier features of ``a``.
            ib: Identifier features of ``b``.

        Returns:
            tuple: ``(contradicts, reason)``.
        """
        if a.get("type") == "email" and b.get("type") == "email":
            if _norm_key(a.get("value")) != _norm_key(b.get("value")):
                return True, "two different email addresses"
        aa, ba = a.get("attrs") or {}, b.get("attrs") or {}
        for field in ("country", "employer", "dob"):
            va, vb = self._field(aa, field), self._field(ba, field)
            if va and vb and _norm_key(va) != _norm_key(vb):
                return True, f"conflicting {field} ({va!r} vs {vb!r})"
        if ia["type"] == "phone" and ib["type"] == "phone":
            da = re.sub(r"\D", "", str(a.get("value", "")))
            db = re.sub(r"\D", "", str(b.get("value", "")))
            if da and db and da[-7:] != db[-7:]:
                return True, f"different phone numbers ({da} / {db})"
        return False, ""

    @staticmethod
    def _field(attrs: dict, field: str) -> Any:
        """Read a tracked field from an attrs dict using its alias list.

        Args:
            attrs: Attribute dict.
            field: Canonical field name.

        Returns:
            Any: First non-empty alias value, or ``None``.
        """
        for key in FIELD_ALIASES.get(field, (field,)):
            val = attrs.get(key)
            if val not in (None, "", [], {}):
                return val
        return None

    # ── Edges ───────────────────────────────────────────────────────────────
    def _edges(self, nodes: dict[str, dict], uf: _UnionFind, candidates: list[dict],
               signals: list[dict], log) -> list[dict]:
        """Build the weighted, typed, evidence-bearing edge set.

        Args:
            nodes: Canonical node records.
            uf: Union-find holding the identity partition.
            candidates: Soft links produced during resolution.
            signals: Raw peer signals.
            log: Callable for progress logging.

        Returns:
            list[dict]: Edge records (CONTRACTS §3 shape plus ``relation``,
            ``provenance`` and ``evidence``).
        """
        edges: dict[tuple[str, str, str], dict] = {}
        id_index: dict[str, str] = {}
        value_index: dict[str, list[str]] = defaultdict(list)
        for nid, rec in nodes.items():
            value_index[_norm_key(rec.get("value"))].append(nid)
            for rid in rec.get("entity_ids", ()):  # map upstream ids → canonical
                id_index[rid] = nid

        def add(a: str, b: str, rel: str, weight: float, confidence: float,
                evidence: str, provenance: str = "nexus", origin: str = "deterministic") -> None:
            """Record one weighted edge, merging duplicate relations.

            Repeated evidence for the same pair/relation reinforces the existing
            edge (confidence saturates) rather than duplicating it, so the graph
            stays small and the edge count reflects distinct relationships.

            Args:
                a: First node id.
                b: Second node id.
                rel: Relation type.
                weight: Edge weight.
                confidence: Evidence confidence in ``0.0``–``1.0``.
                evidence: Human-readable justification.
                provenance: Which agent or pass produced the evidence.
                origin: ``"deterministic"`` or ``"llm"``.
            """
            if a == b or a not in nodes or b not in nodes:
                return
            lo, hi = sorted((a, b))
            key = (lo, hi, rel)
            existing = edges.get(key)
            if existing:
                # Independent evidence reinforces; confidence saturates.
                if evidence not in existing["evidence"]:
                    existing["evidence"].append(evidence)
                existing["confidence"] = round(min(0.99, max(existing["confidence"],
                                                            confidence) + 0.05), 3)
                existing["weight"] = round(max(existing["weight"], weight), 3)
                return
            edges[key] = {"source": lo, "target": hi, "relation": rel, "label": rel,
                          "type": rel, "weight": round(weight, 3),
                          "confidence": round(max(0.0, min(0.99, confidence)), 3),
                          "provenance": provenance, "origin": origin,
                          "evidence": [evidence]}

        # (a) Identity merges → alias_of edges.
        for m in uf.merged:
            add(m["a"], m["b"], "alias_of", EDGE_BASE_WEIGHT["alias_of"],
                m["confidence"], m["evidence"], provenance="entity_resolution")
        # (b) Held-back soft links → candidate edges (explicitly not merged).
        for c in candidates:
            if not c.get("merged"):
                add(c["a"], c["b"], c["type"], EDGE_BASE_WEIGHT.get(c["type"], 0.35),
                    c["confidence"], c["evidence"] + " [candidate link, not merged]",
                    provenance="entity_resolution")

        # (c) Co-occurrence: entities referenced by the same signal.
        sig_refs: list[tuple[str, str, list[str]]] = []
        for sig in signals or []:
            if not isinstance(sig, dict):
                continue
            refs = self._signal_entities(sig, value_index, id_index)
            if len(refs) < 2:
                continue
            src = str(sig.get("source") or sig.get("type") or "signal")
            for i, a in enumerate(refs):
                for b in refs[i + 1:]:
                    add(a, b, "same_signal", EDGE_BASE_WEIGHT["same_signal"],
                        0.42, f"both referenced by {src} signal "
                              f"'{sig.get('type')}'", provenance=src)
                    sig_refs.append((a, b, [src]))

        # (d) Shared attributes.
        feats = {nid: self._identifiers(rec) for nid, rec in nodes.items()}
        ids = sorted(nodes)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                fa, fb = feats[a], feats[b]
                if fa["email_domain"] and fa["email_domain"] == fb["email_domain"] \
                        and nodes[a]["type"] != nodes[b]["type"]:
                    add(a, b, "same_email_domain", EDGE_BASE_WEIGHT["same_email_domain"],
                        0.55, f"shared email domain {fa['email_domain']}")
                if fa["city"] and fa["city"] == fb["city"]:
                    add(a, b, "shared_location", EDGE_BASE_WEIGHT["shared_location"],
                        0.5, f"shared location {fa['city']}")
                if fa["employer"] and fa["employer"] == fb["employer"]:
                    add(a, b, "shared_employer", EDGE_BASE_WEIGHT["shared_employer"],
                        0.45, f"shared employer {fa['employer']}")
                ha = _norm_key(nodes[a].get("attrs", {}).get("avatar"))
                hb = _norm_key(nodes[b].get("attrs", {}).get("avatar"))
                if ha and ha == hb:
                    add(a, b, "shared_avatar", EDGE_BASE_WEIGHT["shared_avatar"],
                        0.9, "identical avatar hash")
                if (nodes[a]["type"] in ("username", "account")
                        and nodes[b]["type"] in ("username", "account")):
                    if fa["handle"] and fa["handle"] == fb["handle"]:
                        pa, pb = fa.get("platform"), fb.get("platform")
                        if pa and pb and pa != pb:
                            add(a, b, "cross_platform_handle",
                                EDGE_BASE_WEIGHT["cross_platform_handle"], 0.55,
                                f"same handle '{fa['handle']}' on {pa} and {pb}")
                        else:
                            add(a, b, "shared_handle", EDGE_BASE_WEIGHT["shared_handle"],
                                0.9, f"identical handle on {pa or pb or 'unknown'}")
                    # Same handle on two platforms is suggestive; an identical
                    # avatar hash or a near-identical bio is what actually
                    # resolves a cross-platform identity.
                    xscore, xev = self._cross_platform_identity(nodes[a], nodes[b], fa, fb)
                    if xscore >= SOFT_LINK:
                        add(a, b, "cross_platform_identity",
                            round(min(0.95, xscore), 3), round(xscore, 3), xev,
                            provenance="identity_resolution")
                if nodes[a]["type"] == "person" and nodes[b]["type"] == "person":
                    score, ev, blocked = self._name_match(nodes[a], nodes[b])
                    if score >= SOFT_LINK and not blocked:
                        add(a, b, "name_match", EDGE_BASE_WEIGHT["name_match"],
                            score, ev, provenance="entity_resolution")

        # (e) Source co-authoring: two entities reported by the same agent.
        # Only meaningful when the source reported a *small* set — a source that
        # returned twenty entities in one response co-authors everything and the
        # edge carries no information, so it is skipped above the cap.
        COAUTHOR_MAX = 8
        by_source: dict[str, set[str]] = defaultdict(set)
        for nid, rec in nodes.items():
            for src in rec.get("sources", ()):
                by_source[src].add(nid)
        for src, members in sorted(by_source.items()):
            members = sorted(members)
            if len(members) < 2 or len(members) > COAUTHOR_MAX:
                continue
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    add(a, b, "source_coauthor", EDGE_BASE_WEIGHT["source_coauthor"],
                        0.3, f"both reported by {src}", provenance=src)

        # (f) Temporal proximity: same source, two signals close in time.
        stamps: dict[tuple[str, str], list[float]] = defaultdict(list)
        for sig in signals or []:
            if not isinstance(sig, dict):
                continue
            ts = _parse_ts(sig.get("ts"))
            if ts is None:
                continue
            src = str(sig.get("source") or "signal")
            for ref in self._signal_entities(sig, value_index, id_index):
                stamps[(src, ref)].append(ts)
        keys = sorted(stamps)
        for i, (src_a, a) in enumerate(keys):
            for src_b, b in keys[i + 1:]:
                if src_a != src_b or a == b:
                    continue
                ta, tb = stamps[(src_a, a)], stamps[(src_b, b)]
                gap = min(abs(x - y) for x in ta for y in tb)
                if gap > 90 * 86400:  # 90 days
                    continue
                prox = 1.0 - (gap / (90 * 86400))
                add(a, b, "temporal_proximity",
                    EDGE_BASE_WEIGHT["temporal_proximity"] + 0.25 * prox,
                    0.25 + 0.3 * prox,
                    f"signals from {src_a} within {gap / 86400:.1f} day(s)",
                    provenance=src_a)

        log(f"extracted {len(edges)} weighted edge(s) from {len(signals or [])} signal(s)")
        return list(edges.values())

    # ── Graph analytics ─────────────────────────────────────────────────────
    @staticmethod
    def _adjacency(nodes: dict[str, dict], edges: list[dict],
                   floor: float = SOFT_LINK) -> dict[str, dict[str, float]]:
        """Build a weighted undirected adjacency with an evidence floor.

        Args:
            nodes: Canonical node records.
            edges: Edge list.
            floor: Minimum edge confidence to include.

        Returns:
            dict: ``node -> {neighbour: weight}``.
        """
        adj: dict[str, dict[str, float]] = {n: {} for n in nodes}
        for e in edges:
            a, b = e["source"], e["target"]
            if a not in adj or b not in adj:
                continue
            w = float(e.get("weight", 0.0)) * max(0.2, float(e.get("confidence", 0.5)))
            adj[a][b] = max(adj[a].get(b, 0.0), w)
            adj[b][a] = max(adj[b].get(a, 0.0), w)
        return {n: {k: v for k, v in nb.items() if v >= floor * 0.2} for n, nb in adj.items()}

    def _communities(self, adj: dict[str, dict[str, float]],
                     nodes: dict[str, dict]) -> tuple[list[dict], dict[str, str]]:
        """Cluster the graph with weighted label propagation.

        Args:
            adj: Weighted adjacency.
            nodes: Canonical node records.

        Returns:
            tuple: ``(clusters, node_to_cluster)`` where clusters follow
            CONTRACTS §3 (``id``, ``label``, ``size``, ``members``) plus
            ``cohesion`` and ``method``.
        """
        labels = _label_propagation(adj)
        buckets: dict[str, list[str]] = defaultdict(list)
        for nid, lab in labels.items():
            buckets[lab].append(nid)
        clusters: list[dict] = []
        node_to_cluster: dict[str, str] = {}
        for idx, (lab, members) in enumerate(
                sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))):
            members = sorted(members)
            cid = f"cluster:{idx}:{self._slug(lab)}"
            member_set = set(members)
            internal = sum(1 for m in members
                           for b in (adj.get(m) or {}) if b in member_set)
            cohesion = round(2 * internal / (len(members) * max(1, len(members) - 1)), 3) \
                if len(members) > 1 else 0.0
            lead = max(members, key=lambda m: (nodes.get(m, {}).get("confidence", 0),
                                               -len(m)))
            for m in members:
                node_to_cluster[m] = cid
            clusters.append({
                "id": cid,
                "label": str(nodes.get(lead, {}).get("label", lab))[:80],
                "size": len(members),
                "members": members,
                "cohesion": cohesion,
                "method": "label_propagation",
            })
        return clusters, node_to_cluster

    @staticmethod
    def _centrality(adj: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        """Compute degree / betweenness / PageRank and a blended influence.

        Args:
            adj: Weighted adjacency.

        Returns:
            dict: ``node -> {"degree", "degree_norm", "betweenness",
            "pagerank", "influence"}``.
        """
        degree = {n: len(nb) for n, nb in adj.items()}
        wdegree = {n: round(sum(nb.values()), 3) for n, nb in adj.items()}
        btw = _norm_scores(_betweenness(adj))
        pr = _norm_scores(_pagerank(adj))
        dn = _norm_scores(degree)
        wn = _norm_scores(wdegree)
        out: dict[str, dict[str, float]] = {}
        for n in adj:
            influence = round(0.30 * wn.get(n, 0.0) + 0.40 * btw.get(n, 0.0)
                              + 0.30 * pr.get(n, 0.0), 4)
            out[n] = {
                "degree": degree[n],
                "weighted_degree": wdegree[n],
                "degree_norm": dn.get(n, 0.0),
                "betweenness": btw.get(n, 0.0),
                "pagerank": pr.get(n, 0.0),
                "influence": influence,
            }
        return out

    def _contradictions(self, nodes: dict[str, dict], uf: _UnionFind,
                        signals: list[dict]) -> list[dict]:
        """Flag fields where two sources disagree about the same identity.

        Args:
            nodes: Canonical node records.
            uf: Union-find partition (contradictions are reported per resolved
                identity, so a split name variant is caught too).
            signals: Raw peer signals, mined for field values.

        Returns:
            list[dict]: Contradiction records sorted by severity.
        """
        found: list[dict] = []
        groups = uf.groups()
        # Attribute harvest: per node, per field, per source. The recorded
        # provenance (not the merged attrs) is used, so a value that a later
        # source overwrote is still available as a contradicting observation.
        per_node: dict[str, dict[str, list[tuple[str, Any]]]] = defaultdict(
            lambda: defaultdict(list))
        for nid, rec in nodes.items():
            prov = rec.get("attr_provenance") or {}
            rec_fields: dict[str, list[tuple[str, Any]]] = {}
            for key, entries in prov.items():
                for field, keys in FIELD_ALIASES.items():
                    if key in keys and field in TRACKED_FIELDS:
                        rec_fields.setdefault(field, []).extend(entries)
            if not prov:  # provenance-free record: fall back to merged attrs
                self._collect_fields(rec, rec_fields)
            for field, pairs in rec_fields.items():
                if field in TRACKED_FIELDS:
                    per_node[nid][field].extend(pairs)
        # Signals contribute values to the entities they reference.
        value_index: dict[str, list[str]] = defaultdict(list)
        id_index: dict[str, str] = {}
        for nid, rec in nodes.items():
            value_index[_norm_key(rec.get("value"))].append(nid)
            for rid in rec.get("entity_ids", ()):
                id_index[rid] = nid
        for sig in signals or []:
            if not isinstance(sig, dict):
                continue
            refs = self._signal_entities(sig, value_index, id_index)
            if not refs:
                continue
            sig_fields: dict[str, list[tuple[str, Any]]] = {}
            self._collect_fields(sig, sig_fields)
            src = str(sig.get("source") or "signal")
            for ref in refs:
                for field, pairs in sig_fields.items():
                    if field in TRACKED_FIELDS:
                        per_node[ref][field].extend((src, v) for _, v in pairs)

        for root, members in groups.items():
            # Collect field values across every member of the identity.
            field_values: dict[str, list[tuple[str, str, Any]]] = defaultdict(list)
            for m in members:
                for field, pairs in per_node.get(m, {}).items():
                    for src, val in pairs:
                        field_values[field].append((m, src, val))
            for field, entries in field_values.items():
                distinct: dict[str, tuple[str, str, Any]] = {}
                per_value_sources: dict[str, set[str]] = defaultdict(set)
                for nid, src, val in entries:
                    key = _norm_key(val)
                    if not key:
                        continue
                    per_value_sources[key].add(str(src))
                    if key not in distinct or (len(str(src)) < len(str(distinct[key][1]))):
                        distinct[key] = (nid, src, val)
                if len(distinct) < 2:
                    continue
                severity = TRACKED_FIELDS[field]
                sources = sorted({str(s) for _, s, _ in entries})
                # A real contradiction needs disagreement between *independent*
                # sources. One source emitting two values is self-inconsistent
                # noise, not a cross-source conflict.
                value_sources = list(per_value_sources.values())
                independent = any(a.isdisjoint(b)
                                  for i, a in enumerate(value_sources)
                                  for b in value_sources[i + 1:])
                if not independent and len(members) < 2:
                    continue
                found.append({
                    "id": f"contradiction:{self._slug(field)}:{_hash((root, field))[:6]}",
                    "entity_id": root,
                    "entity_label": str(nodes.get(members[0], {}).get("label", root))[:80],
                    "field": field,
                    "severity": severity,
                    "values": [
                        {"value": str(v)[:80], "source": str(s), "entity_id": n}
                        for n, s, v in sorted(distinct.values(), key=lambda t: str(t[2]))
                    ],
                    "sources": sources,
                    "confidence": round(min(0.9, 0.55 + 0.15 * (len(distinct) - 1)), 3),
                    "detail": (f"sources disagree on {field} for "
                               f"{nodes.get(members[0], {}).get('label', root)}: "
                               + " vs ".join(f"{v} [{s}]" for _, s, v in distinct.values())),
                })
        order = {"high": 0, "medium": 1, "low": 2}
        found.sort(key=lambda c: (order.get(c["severity"], 3), -c["confidence"]))
        return found

    def _anomalies(self, nodes: dict[str, dict], centrality: dict[str, dict],
                   contradictions: list[dict]) -> list[dict]:
        """Detect single-source entities, isolated nodes and attribute outliers.

        Args:
            nodes: Canonical node records.
            centrality: Centrality scores.
            contradictions: Contradiction records (flagged as anomalies too).

        Returns:
            list[dict]: Anomaly records.
        """
        out: list[dict] = []
        for nid, rec in sorted(nodes.items()):
            srcs = rec.get("sources") or []
            cent = centrality.get(nid, {})
            if len(srcs) == 1:
                out.append({
                    "type": "single_source", "entity_id": nid,
                    "entity_label": str(rec.get("label"))[:80],
                    "source": srcs[0], "severity": "medium",
                    "detail": f"only observed by {srcs[0]}; not corroborated",
                })
            if not cent.get("degree") and len(nodes) > 1:
                out.append({
                    "type": "isolated", "entity_id": nid,
                    "entity_label": str(rec.get("label"))[:80],
                    "source": ",".join(srcs[:3]), "severity": "low",
                    "detail": "no relational evidence attached to this entity",
                })
        # Numeric outliers (z-score) over well-populated numeric attributes.
        numeric_fields = ("age", "followers", "follower_count", "connections",
                          "posts", "photos", "videos")
        for field in numeric_fields:
            values: list[tuple[str, float]] = []
            for nid, rec in nodes.items():
                raw = self._field(rec.get("attrs") or {}, field)
                if raw is None and field == "followers":
                    raw = (rec.get("attrs") or {}).get("follower_count")
                try:
                    if raw is None or isinstance(raw, bool):
                        continue
                    values.append((nid, float(raw)))
                except (TypeError, ValueError):
                    continue
            if len(values) < 4:
                continue
            nums = [v for _, v in values]
            mean = sum(nums) / len(nums)
            var = sum((v - mean) ** 2 for v in nums) / len(nums)
            sd = math.sqrt(var)
            if sd <= 0:
                continue
            for nid, val in values:
                z = (val - mean) / sd
                if abs(z) >= 2.5:
                    out.append({
                        "type": "attribute_outlier", "entity_id": nid,
                        "entity_label": str(nodes.get(nid, {}).get("label"))[:80],
                        "field": field, "value": val,
                        "z_score": round(z, 2), "severity": "medium" if z > 0 else "low",
                        "detail": f"{field}={val:g} is {abs(z):.1f}σ from the mean ({mean:.1f})",
                    })
        for c in contradictions:
            out.append({"type": "contradiction", "entity_id": c["entity_id"],
                        "entity_label": c["entity_label"], "severity": c["severity"],
                        "detail": c["detail"]})
        return out

    # ── LLM-assisted relationship inference ─────────────────────────────────
    async def _llm_edges(self, nodes: dict[str, dict], adjacency: dict[str, dict],
                         existing: list[dict], deep: bool = False) -> dict[str, Any]:
        """Ask the LLM to propose extra edges, filtered against real node ids.

        The model is strictly advisory: every returned edge must reference two
        ids that already exist in the graph, or it is discarded. Nothing the LLM
        says can create an entity.

        Args:
            nodes: Canonical node records.
            adjacency: Current weighted adjacency (used to bound the prompt).
            existing: Edges already found deterministically.
            deep: When True the LLM pass is attempted even for small graphs.

        Returns:
            dict: ``{"status", "proposed", "accepted", "rejected", "edges",
            "error", "model"}``.
        """
        report: dict[str, Any] = {"status": "skipped", "proposed": 0, "accepted": 0,
                                  "rejected": 0, "edges": [], "error": None,
                                  "model": None}
        try:
            items = sorted(nodes.items(), key=lambda kv: kv[0])
            # The model can only add value on a graph rich enough to contain
            # non-obvious links; below the floor we skip the call entirely so a
            # small investigation is not slowed by a slow local model.
            if len(items) < 2:
                report["status"] = "skipped_no_edges_possible"
                return report
            if len(items) < MIN_LLM_NODES and not deep:
                report["status"] = "skipped_graph_too_small"
                return report
            items = items[:MAX_LLM_NODES]
            id_list = "\n".join(
                f"- {nid} | {nodes[nid].get('type')} | {str(nodes[nid].get('label'))[:60]}"
                for nid, _ in items)
            known = {nid for nid, _ in items}
            linked = {f"{min(e['source'], e['target'])}|{max(e['source'], e['target'])}"
                      for e in existing}
            prompt = (
                "You are an OSINT analyst reviewing an entity graph.\n"
                "Here are existing nodes (id | type | label):\n"
                f"{id_list}\n\n"
                "Propose up to 5 relationships that are NOT already obvious, and only "
                "between node ids listed above. Never invent a new node id. Give a "
                "relation label (max 3 words) and a confidence between 0 and 1.\n"
                'Reply with JSON only: {"edges":[{"source":"<node id>",'
                '"target":"<node id>","relation":"...","confidence":0.0,'
                '"reason":"one short clause"}]}'
            )
            # Local models run at a few tokens/second, so the budget is sized for
            # ~5 short edges rather than the full agent token_budget.
            kwargs = {"max_tokens": 400,
                      "temperature": 0.0,
                      "schema_hint": '{"edges":[{"source":str,"target":str,'
                                     '"relation":str,"confidence":float,"reason":str}]}'}
            data: Any = None
            fn = getattr(self, "llm_json", None)  # WS-MAIN may add this to BaseAgent
            if callable(fn):
                data = await asyncio.wait_for(fn(prompt, **kwargs),
                                              timeout=LLM_TIMEOUT_S)
            else:
                from llm import get_llm  # lazy import: llm.py is a runtime dep
                data = await asyncio.wait_for(
                    get_llm().complete_json(prompt, **kwargs),
                    timeout=LLM_TIMEOUT_S)
            if isinstance(data, dict) and data.get("error"):
                report["status"] = "unavailable"
                report["error"] = str(data.get("error"))[:200]
                return report
            raw_edges = []
            if isinstance(data, dict):
                raw_edges = data.get("edges") or []
            elif isinstance(data, list):
                raw_edges = data
            report["proposed"] = len(raw_edges)
            accepted: list[dict] = []
            for e in raw_edges:
                if not isinstance(e, dict):
                    report["rejected"] += 1
                    continue
                a, b = str(e.get("source") or ""), str(e.get("target") or "")
                if a not in known or b not in known or a == b:
                    report["rejected"] += 1   # hallucinated / unknown node id
                    continue
                pair = f"{min(a, b)}|{max(a, b)}"
                conf = self._as_float(e.get("confidence"), 0.4)
                conf = min(conf, 0.75)      # LLM-proposed links never outrank verified ones
                if pair in linked and conf <= 0.5:
                    report["rejected"] += 1
                    continue
                rel = re.sub(r"[^a-z0-9_]+", "_", str(e.get("relation") or "related_to")
                             .casefold())[:24] or "related_to"
                accepted.append({
                    "source": min(a, b), "target": max(a, b),
                    "relation": f"llm_{rel}", "label": f"llm_{rel}",
                    "type": "llm_inferred", "weight": round(EDGE_BASE_WEIGHT["llm_inferred"]
                                                             * conf, 3),
                    "confidence": round(conf, 3), "provenance": "llm",
                    "origin": "llm",
                    "evidence": [f"LLM-proposed: {str(e.get('reason') or '')[:120]}"],
                    "advisory": True,
                })
                linked.add(pair)
            report["edges"] = accepted
            report["accepted"] = len(accepted)
            report["status"] = "ok" if accepted or not raw_edges else "no_valid_edges"
            if raw_edges and not accepted:
                report["error"] = "all proposed edges referenced unknown node ids"
        except asyncio.TimeoutError:
            report["status"] = "timeout"
            report["error"] = "llm relationship pass timed out"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let an optional pass break correlation
            report["status"] = "error"
            report["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return report

    # ── Entry point ─────────────────────────────────────────────────────────
    async def run(self, input_data, context=None):
        """Resolve identities, build the weighted graph, and analyse it.

        Args:
            input_data: Investigation target (the seed of the graph).
            context: Pipeline context. Uses ``peer_signals``, ``peer_entities``,
                ``agent_results`` and ``deep``.

        Returns:
            AgentResult: ``output`` always contains ``nodes``, ``edges``,
            ``clusters``, ``top_entities`` and ``graph_density``; ``partial``
            carries any degraded sub-result instead of raising.
        """
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        peer_signals: list[dict] = [s for s in (context.get("peer_signals") or [])
                                   if isinstance(s, dict)]
        peer_entities: list[dict] = [e for e in (context.get("peer_entities") or [])
                                     if isinstance(e, dict)]
        log = self.log

        self.log(f"correlating {len(peer_entities)} entities + {len(peer_signals)} signals")

        # Root target node (kept for backwards compatibility with the graph view).
        root_id = f"target_{self._slug(target)}"
        warnings: list[str] = []
        nodes: dict[str, dict] = {}
        uf = _UnionFind(HARD_MERGE)
        candidates: list[dict] = []
        edges: list[dict] = []
        centrality: dict[str, dict] = {}
        clusters: list[dict] = []
        contradictions: list[dict] = []
        anomalies: list[dict] = []
        llm_report: dict[str, Any] = {"status": "skipped", "accepted": 0}

        # ── Nodes: root target + de-duplicated peer entities ─────────────────
        nodes[root_id] = {
            "id": root_id, "canonical_id": root_id, "label": target[:60],
            "type": "target", "value": str(target)[:200], "confidence": 1.0,
            "sources": ["nexus"], "aliases": [], "attrs": {}, "first_seen": "",
            "last_seen": "", "entity_ids": [], "raw_count": 0,
        }
        uf.add(root_id)

        try:
            resolved, candidates = self._resolve(peer_entities, peer_signals, log, uf)
            for nid, rec in resolved.items():
                nodes.setdefault(nid, rec)
        except Exception as exc:
            warnings.append(f"entity resolution degraded: {type(exc).__name__}: {exc}")
            log(warnings[-1])

        # ── Collapse resolved identities into canonical nodes ───────────────
        # After this point a person is ONE node carrying every alias and source.
        identities: list[dict] = []
        try:
            nodes, identities = self._collapse(nodes, uf, log)
        except Exception as exc:
            warnings.append(f"identity collapse degraded: {type(exc).__name__}: {exc}")
            log(warnings[-1])

        # Fold resolved aliases back into the node set.
        try:
            edges = self._edges(nodes, uf, candidates, peer_signals, log)
        except Exception as exc:
            warnings.append(f"relationship extraction degraded: {type(exc).__name__}: {exc}")
            log(warnings[-1])

        adjacency = self._adjacency(nodes, edges)

        # ── LLM-assisted relationship inference (advisory, fully guarded) ────
        # The model call is fired off as a task so the deterministic analytics
        # below run *while* it waits — NEXUS is on APEX's sequential critical
        # path, so overlapping keeps the LLM pass close to free in wall time.
        deep = bool(context.get("deep"))
        llm_task: Optional[asyncio.Task] = None
        if context.get("disable_llm") or not ADVISORY_LLM:
            llm_report = {"status": "disabled", "proposed": 0, "accepted": 0,
                          "rejected": 0, "edges": [], "error": None, "model": None}
        else:
            llm_task = asyncio.ensure_future(
                self._llm_edges(nodes, adjacency, edges, deep))

        try:
            centrality = self._centrality(adjacency)
        except Exception as exc:
            warnings.append(f"centrality degraded: {type(exc).__name__}: {exc}")
            centrality = {n: {"degree": len(nb), "weighted_degree": 0.0,
                              "degree_norm": 0.0, "betweenness": 0.0,
                              "pagerank": 0.0, "influence": 0.0}
                          for n, nb in adjacency.items()}
            log(warnings[-1])

        try:
            contradictions = self._contradictions(nodes, uf, peer_signals)
        except Exception as exc:
            warnings.append(f"contradiction detection degraded: {type(exc).__name__}: {exc}")
            contradictions = []
            log(warnings[-1])

        # ── Collect the LLM pass, then fold it in before clustering ──────────
        if llm_task is not None:
            try:
                llm_report = await llm_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # _llm_edges already guards internally
                llm_report = {"status": "error", "proposed": 0, "accepted": 0,
                              "rejected": 0, "edges": [],
                              "error": f"{type(exc).__name__}: {exc}"[:200],
                              "model": None}
            log(f"llm relationship pass: {llm_report.get('status')} "
                f"(accepted {llm_report.get('accepted', 0)}/"
                f"{llm_report.get('proposed', 0)})")
            if llm_report.get("edges"):
                edges.extend(llm_report["edges"])
                adjacency = self._adjacency(nodes, edges)

        try:
            clusters, node_to_cluster = self._communities(adjacency, nodes)
        except Exception as exc:
            warnings.append(f"clustering degraded: {type(exc).__name__}: {exc}")
            clusters, node_to_cluster = [], {}
            log(warnings[-1])

        try:
            anomalies = self._anomalies(nodes, centrality, contradictions)
        except Exception as exc:
            warnings.append(f"anomaly detection degraded: {type(exc).__name__}: {exc}")
            anomalies = []
            log(warnings[-1])

        # ── Root edges: the seed is linked to what it produced ───────────────
        root_type = _norm_type(context.get("input_type") or "")
        relation = {
            "username": "has_account", "person": "resolves_to",
            "location": "located_at", "account": "has_account",
        }.get(root_type, "linked_to")
        for e in list(edges):
            if e["source"] == root_id or e["target"] == root_id:
                continue
            edges.append({"source": root_id, "target": e["source"], "relation": relation,
                          "label": relation, "type": relation,
                          "weight": EDGE_BASE_WEIGHT["linked_to"], "confidence": 0.5,
                          "provenance": "nexus", "origin": "deterministic",
                          "evidence": [f"discovered while investigating the seed target"]})
            break  # seed fan-out stays minimal; the real structure is above

        # ── Finalise nodes with cluster + centrality + alias metadata ────────
        out_nodes: list[dict] = []
        for nid, rec in nodes.items():
            cent = centrality.get(nid, {"degree": 0, "weighted_degree": 0.0,
                                        "degree_norm": 0.0, "betweenness": 0.0,
                                        "pagerank": 0.0, "influence": 0.0})
            cluster = node_to_cluster.get(nid)
            merged_aliases = sorted({
                str(a) for a in rec.get("aliases", [])
                if _norm_key(a) != _norm_key(rec.get("value"))
            })
            out_nodes.append({
                "id": nid,
                "canonical_id": rec.get("canonical_id", nid),
                "label": rec.get("label", nid),
                "type": rec.get("type", "other"),
                "value": rec.get("value", ""),
                "weight": cent.get("weighted_degree", 0.0),
                "confidence": round(rec.get("confidence", 0.5), 3),
                "group": cluster or "",
                "degree": cent.get("degree", 0),
                "weighted_degree": cent.get("weighted_degree", 0.0),
                "betweenness": cent.get("betweenness", 0.0),
                "pagerank": cent.get("pagerank", 0.0),
                "influence": cent.get("influence", 0.0),
                "aliases": merged_aliases,
                "alias_count": len(merged_aliases),
                "merged_from": [m for m in (rec.get("merged_from") or []) if m != nid],
                "variant_types": rec.get("variant_types", []),
                "sources": rec.get("sources", []),
                "source_count": len(rec.get("sources", [])),
                "first_seen": rec.get("first_seen", ""),
                "last_seen": rec.get("last_seen", ""),
                "cluster": cluster or "",
                "attrs": rec.get("attrs", {}),
            })

        ranked = sorted(out_nodes, key=lambda n: (-n["influence"], -n["degree"], n["id"]))
        top_entities = [
            {"id": n["id"], "label": n["label"], "type": n["type"],
             "degree": n["degree"], "influence": n["influence"],
             "betweenness": n["betweenness"], "pagerank": n["pagerank"],
             "cluster": n["cluster"], "confidence": n["confidence"]}
            for n in ranked[:8]
        ]

        breach_count = sum(1 for s in peer_signals if s.get("type") == "breach")
        match_count = sum(1 for s in peer_signals
                          if s.get("type") in ("face_match", "image_match"))
        account_count = sum(1 for s in peer_signals if s.get("type") == "account")

        resolved_identities = list(uf.merged)
        log(f"graph: {len(out_nodes)} nodes, {len(edges)} edges, {len(clusters)} "
            f"cluster(s), {len(resolved_identities)} identity merge(s), "
            f"{len(contradictions)} contradiction(s), {len(anomalies)} anomaly(ies); "
            f"top node '{ranked[0]['label']}'" if ranked else "graph empty")

        signals = [{
            "type": "correlation_summary",
            "nodes": len(out_nodes), "edges": len(edges), "clusters": len(clusters),
            "accounts": account_count, "breaches": breach_count,
            "media_matches": match_count,
            "identity_merges": len(resolved_identities),
            "contradictions": len(contradictions),
            "anomalies": len(anomalies),
            "llm_edges_accepted": llm_report.get("accepted", 0),
            "confidence": 0.5,
            "source": "nexus",
        }]
        for c in contradictions[:10]:
            signals.append({
                "type": "contradiction", "entity_id": c["entity_id"],
                "field": c["field"], "severity": c["severity"],
                "value": c["detail"][:200], "confidence": c["confidence"],
                "source": "nexus",
            })
        for a in anomalies[:10]:
            signals.append({
                "type": "anomaly", "entity_id": a.get("entity_id", ""),
                "anomaly": a.get("type", ""), "value": a.get("detail", "")[:200],
                "confidence": 0.45, "source": "nexus",
            })

        density = round(len(edges) / max(len(out_nodes), 1), 3)
        evidence = sum(1 for e in edges if e.get("origin") == "deterministic")
        confidence = 0.0
        if edges:
            confidence = min(0.95, 0.25 + 0.05 * len(edges) + 0.05 * evidence)
        if contradictions:
            confidence = max(confidence, 0.5)  # contradictions are themselves a finding
        entities_found = [
            {"id": n["id"], "label": n["label"], "type": n["type"],
             "value": n["value"], "confidence": n["confidence"],
             "source": "nexus", "attrs": {"aliases": n["aliases"],
                                          "cluster": n["cluster"],
                                          "influence": n["influence"]}}
            for n in ranked if n["type"] not in ("target",)
        ]

        return AgentResult(
            agent=self.name,
            status="partial" if warnings else "done",
            output={
                # ── keys preserved from the previous implementation ────────
                "nodes": out_nodes,
                "edges": edges,
                "clusters": clusters,
                "top_entities": top_entities,
                "graph_density": density,
                # ── new analytical product ─────────────────────────────────
                "centrality": {n["id"]: {
                    "degree": n["degree"], "weighted_degree": n["weighted_degree"],
                    "betweenness": n["betweenness"], "pagerank": n["pagerank"],
                    "influence": n["influence"]} for n in ranked},
                "identities": identities,
                "merges": resolved_identities[:50],
                "merge_candidates": candidates[:50],
                "rejected_merges": uf.rejected[:25],
                "contradictions": contradictions,
                "anomalies": anomalies,
                "llm_relationships": llm_report,
                "stats": {
                    "raw_entities": len(peer_entities),
                    "unique_nodes": len(out_nodes),
                    "identity_merges": len(resolved_identities),
                    "edges": len(edges),
                    "deterministic_edges": evidence,
                    "llm_edges": len(edges) - evidence,
                    "clusters": len(clusters),
                    "largest_cluster": max((c["size"] for c in clusters), default=0),
                    "contradictions": len(contradictions),
                    "anomalies": len(anomalies),
                    "signals_analysed": len(peer_signals),
                },
                "warnings": warnings,
                "method": {
                    "resolution": f"confidence-aware union-find (hard>{HARD_MERGE})",
                    "clustering": "weighted label propagation (deterministic)",
                    "centrality": "weighted degree + Brandes betweenness + PageRank",
                },
            },
            confidence=round(confidence, 3),
            reasoning=(f"Resolved {len(peer_entities)} raw entities into {len(out_nodes)} "
                       f"nodes ({len(resolved_identities)} identity merges), built "
                       f"{len(edges)} weighted edges and {len(clusters)} cluster(s); "
                       f"{len(contradictions)} contradiction(s) and {len(anomalies)} "
                       f"anomaly(ies) flagged; LLM pass {llm_report.get('status')}"
                       + (f" with {warnings[0]}" if warnings else "")),
            entities_found=entities_found,
            signals=signals,
            latency_s=self._elapsed(t0),
        )

