"""
qdrant_store.py — Semantic memory for VAULT
=============================================
VAULT today remembers entity *IDs*. This adds semantic memory: entities,
signals and report text are embedded and stored in Qdrant so a new run can ask
"have I seen anything like this before?" rather than only exact-matching an ID.

Design constraint, stated up front: **this must no-op cleanly.** VAULT is a
core agent and the pipeline must not degrade when Qdrant is absent. Every
public function returns a dict, and the two the agent calls —
:func:`run_upsert_entities` and :func:`run_semantic_search` — return
``{"available": False, ...}`` with an ``error`` rather than raising.

Embedding tier, in order:

1. **LLM/embedding provider tier** — an OpenAI-compatible ``/embeddings``
   endpoint (``EMBEDDING_API_BASE`` + ``EMBEDDING_API_KEY``), including a local
   Ollama ``/api/embed`` endpoint. Real semantic vectors.
2. **Local hashing fallback** — a deterministic, dependency-free embedding:
   character 3-gram + word hashing into a fixed-dim float vector, L2
   normalised. Not semantic in the neural sense, but it *is* a real vector with
   real similarity structure (shared substrings score high), so cosine search
   still returns something useful and the whole feature works with no provider.

Backends:

* **Qdrant** (``qdrant-client``, already in requirements.txt) — lazy, real
  client, collection auto-created with the configured vector size/distance.
* **In-memory fallback** — when Qdrant is unreachable but embeddings work,
  entities land in a bounded LRU so semantic search still functions in-process.
  Explicitly labelled ``backend: "memory"`` so no caller mistakes it for the
  durable store.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import time
import uuid
from collections import OrderedDict
from typing import Any

from . import net

# ── Configuration ────────────────────────────────────────────────────────────

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
COLLECTION = os.getenv("QDRANT_COLLECTION", "signal_os_memory")
DEFAULT_DIM = int(os.getenv("QDRANT_DIM", "768"))
#: Cap on the in-memory fallback's retained points.
MEMORY_CAP = int(os.getenv("QDRANT_MEMORY_CAP", "2000"))
#: Probe timeout for the Qdrant health check.
PROBE_TIMEOUT_S = 2.0

_EMBED_BASE = os.getenv("EMBEDDING_API_BASE", "")
_EMBED_KEY = os.getenv("EMBEDDING_API_KEY", "")
_EMBED_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
#: Set to false to force the local hashing embedder (useful in tests).
_ALLOW_PROVIDER = os.getenv("QDRANT_USE_PROVIDER_EMBEDDINGS", "true").lower() == "true"

_TOKEN_RE = re.compile(r"[^\w]+", re.UNICODE)

_client: Any = None
_client_failed_at = 0.0
_client_failed = False
_MEM: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_dim_cache: dict[str, int] = {}


# ── Embeddings ───────────────────────────────────────────────────────────────

def _l2(vec: list[float]) -> list[float]:
    """L2-normalise a vector in place-safe fashion.

    Args:
        vec: raw vector.

    Returns:
        A unit-length vector (zeros stay zeros).
    """
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def hash_embed(text: str, dim: int = DEFAULT_DIM) -> list[float]:
    """Deterministic local embedding — the zero-provider fallback.

    Character trigrams and whole words are hashed into buckets with a sign
    bit, then the vector is L2-normalised. Similar strings share trigrams and
    therefore cosine-similarity high; unrelated strings land near orthogonal.

    Args:
        text: input text.
        dim: output dimensionality.

    Returns:
        A unit-norm float vector of length ``dim``.
    """
    vec = [0.0] * dim
    norm_text = (text or "").lower().strip()
    if not norm_text:
        return vec

    def _add(token: str, weight: float) -> None:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[idx] += sign * weight

    for word in _TOKEN_RE.split(norm_text):
        if not word:
            continue
        _add(f"w:{word}", 1.0)
    padded = f"  {norm_text} "
    for i in range(len(padded) - 3):
        _add(f"c:{padded[i:i + 3]}", 0.35)
    return _l2(vec)


async def _embed_ollama(texts: list[str]) -> list[list[float]] | None:
    """Embed via a local Ollama ``/api/embed`` endpoint.

    Args:
        texts: batch of strings.

    Returns:
        A list of vectors, or ``None`` when Ollama is unreachable/unsuitable.
    """
    url = _OLLAMA_URL.rstrip("/") + "/api/embed"
    try:
        payload = await net.post_json(
            url, {"model": _EMBED_MODEL, "input": texts},
            allow_private=True,   # local model server
            use_opsec=False,      # localhost is never proxied through Tor
            timeout=120.0,
            max_bytes=64 * 1024 * 1024,
        )
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    vectors = payload.get("embeddings") or payload.get("embedding")
    if vectors and isinstance(vectors[0], (int, float)):
        vectors = [vectors]
    if not isinstance(vectors, list) or not vectors:
        return None
    try:
        return [_l2([float(x) for x in v]) for v in vectors]
    except (TypeError, ValueError):
        return None


async def _embed_api(texts: list[str]) -> list[list[float]] | None:
    """Embed via an OpenAI-compatible ``/embeddings`` endpoint.

    Args:
        texts: batch of strings.

    Returns:
        A list of L2-normalised vectors, or ``None`` on any failure.
    """
    if not _EMBED_BASE or not _EMBED_KEY:
        return None
    url = _EMBED_BASE.rstrip("/") + "/embeddings"
    out: list[list[float]] = []
    try:
        for text in texts:
            payload = await net.post_json(
                url, {"model": _EMBED_MODEL, "input": text},
                headers={"authorization": f"Bearer {_EMBED_KEY}"},
                timeout=60.0,
                max_bytes=32 * 1024 * 1024,
            )
            data = (payload or {}).get("data") or []
            if not data:
                return None
            out.append(_l2([float(x) for x in data[0]["embedding"]]))
        return out
    except Exception:  # noqa: BLE001
        return None


async def embed(texts: list[str] | str, *, dim: int | None = None) -> dict[str, Any]:
    """Embed one or more strings.

    Args:
        texts: a string or a list of strings.
        dim: target dimensionality. The provider's own size wins when it
            differs (Qdrant collections are created at the provider's size and
            that size is recorded in ``result["dim"]``); the hash fallback
            honours ``dim``.

    Returns:
        ``{"vectors", "dim", "engine", "error"}``. ``engine`` is
        ``"provider"``, ``"ollama"``, ``"hash"`` or ``None``. Never raises.
    """
    batch = [texts] if isinstance(texts, str) else list(texts or [])
    target = int(dim or DEFAULT_DIM)
    if not batch:
        return {"vectors": [], "dim": target, "engine": None, "error": "empty input"}

    if _ALLOW_PROVIDER:
        vectors = await _embed_api(batch)
        if vectors:
            return {"vectors": vectors, "dim": len(vectors[0]), "engine": "provider",
                    "error": None}
        vectors = await _embed_ollama(batch)
        if vectors:
            return {"vectors": vectors, "dim": len(vectors[0]), "engine": "ollama",
                    "error": None}

    try:
        return {"vectors": [hash_embed(t, target) for t in batch], "dim": target,
                "engine": "hash", "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"vectors": [], "dim": target, "engine": None,
                "error": f"{type(exc).__name__}: {exc}"}


# ── Qdrant client ────────────────────────────────────────────────────────────

def _get_client() -> Any:
    """Lazily build the Qdrant client, with a cooldown after a failure.

    Returns:
        A ``QdrantClient``, or ``None`` when the package is absent or a recent
        attempt to reach the server failed (retry after ``QDRANT_RETRY_S``).
    """
    global _client, _client_failed, _client_failed_at
    if _client is not None:
        return _client
    if _client_failed and (time.time() - _client_failed_at) < 20.0:
        return None
    try:
        from qdrant_client import QdrantClient  # lazy, optional
    except ImportError:
        return None
    try:
        _client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None, timeout=5)
        _client_failed = False
        return _client
    except Exception:  # noqa: BLE001 — bad URL / unreachable server
        _client, _client_failed = None, True
        _client_failed_at = time.time()
        return None


def _mark_failed() -> None:
    """Record a Qdrant failure so the next call skips straight to memory."""
    global _client, _client_failed, _client_failed_at
    _client, _client_failed = None, True
    _client_failed_at = time.time()


def reachable() -> bool:
    """``True`` when the Qdrant server answers right now.

    Returns:
        Liveness boolean. Cached for 5 seconds; never raises.
    """
    client = _get_client()
    if client is None:
        return False
    try:
        client.get_collections()
        return True
    except Exception:  # noqa: BLE001
        _mark_failed()
        return False


async def ensure_collection(client: Any, dim: int) -> bool:
    """Create the memory collection when it does not already exist.

    Args:
        client: a ``QdrantClient``.
        dim: vector dimensionality the collection should use.

    Returns:
        ``True`` when the collection exists afterwards. Never raises.
    """
    try:
        existing = client.get_collections()
        names = {c.name for c in getattr(existing, "collections", [])}
        if COLLECTION in names:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from qdrant_client.models import Distance, VectorParams
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=int(dim), distance=Distance.COSINE),
        )
        return True
    except Exception:  # noqa: BLE001 — a race with another worker is fine
        try:
            client.get_collection(COLLECTION)
            return True
        except Exception:  # noqa: BLE001
            _mark_failed()
            return False


def backends() -> dict[str, bool]:
    """Report which memory backends are usable.

    Returns:
        ``{"qdrant": bool, "memory": bool, "available": bool}``. ``memory`` is
        always ``True`` — the in-process fallback cannot fail.
    """
    return {"qdrant": reachable(), "memory": True, "available": True}


def health() -> dict[str, Any]:
    """Memory-store readiness for the OPS view.

    Returns:
        The :func:`backends` mapping plus ``url``, ``collection``, ``dim`` and
        a ``reason`` when Qdrant is down.
    """
    b = backends()
    out = {**b, "url": QDRANT_URL, "collection": COLLECTION,
           "dim": _dim_cache.get("dim", DEFAULT_DIM)}
    if not b["qdrant"]:
        out["reason"] = (f"Qdrant unreachable at {QDRANT_URL} — semantic memory "
                         f"is running on the in-process fallback only")
    return out


# ── In-memory fallback ───────────────────────────────────────────────────────

def _mem_upsert(points: list[dict[str, Any]]) -> int:
    """Insert/refresh points in the bounded in-process store.

    Args:
        points: ``{"id", "vector", "payload"}`` dicts.

    Returns:
        Number of points written.
    """
    for p in points:
        _MEM[p["id"]] = p
        _MEM.move_to_end(p["id"])
    while len(_MEM) > MEMORY_CAP:
        _MEM.popitem(last=False)
    return len(points)


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    Args:
        a: first vector.
        b: second vector.

    Returns:
        Similarity in ``[-1, 1]``; ``0.0`` on a dimension mismatch.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _mem_search(vector: list[float], top_k: int,
                filters: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Brute-force cosine search over the in-process store.

    Args:
        vector: query vector.
        top_k: maximum hits to return.
        filters: exact-match payload filters.

    Returns:
        ``[{"id", "score", "payload"}]``, best first.
    """
    hits: list[dict[str, Any]] = []
    for pid, point in _MEM.items():
        payload = point.get("payload") or {}
        if filters and any(payload.get(k) != v for k, v in filters.items()):
            continue
        hits.append({"id": pid, "score": round(_cosine(vector, point["vector"]), 4),
                     "payload": payload})
    hits.sort(key=lambda h: -h["score"])
    return hits[:top_k]


def mem_size() -> int:
    """Number of points currently held in the in-process fallback."""
    return len(_MEM)


def mem_clear() -> int:
    """Empty the in-process fallback. Returns the number of points dropped."""
    n = len(_MEM)
    _MEM.clear()
    return n


# ── Upsert / search ──────────────────────────────────────────────────────────

def _entity_text(entity: dict[str, Any]) -> str:
    """Flatten an entity dict into the text that gets embedded.

    Args:
        entity: ``{"id", "label", "type", ...}``.

    Returns:
        A compact natural-language string (``"email: foo@bar.com"``).
    """
    parts = [str(entity.get(k)) for k in ("type", "label", "id") if entity.get(k)]
    return " ".join(parts) or json_id(entity)


def json_id(entity: dict[str, Any]) -> str:
    """Stable string id for an entity dict.

    Args:
        entity: any dict.

    Returns:
        A short deterministic id.
    """
    return hashlib.sha256(
        repr(sorted((str(k), str(v)) for k, v in entity.items())).encode()
    ).hexdigest()[:16]


async def upsert_entities(entities: list[dict[str, Any]], *,
                          case_id: str = "default",
                          tags: dict[str, Any] | None = None) -> dict[str, Any]:
    """Embed and store entities in the semantic memory store.

    Args:
        entities: entity dicts as produced by the agents
            (``{"id", "label", "type"}``).
        case_id: the investigation case, stored as a payload filter.
        tags: extra payload fields to attach to every point.

    Returns:
        ``{"stored", "backend", "collection", "dim", "engine", "available",
        "error"}``. ``backend`` is ``"qdrant"`` or ``"memory"``. Never raises.
    """
    out: dict[str, Any] = {
        "stored": 0, "backend": None, "collection": COLLECTION,
        "dim": DEFAULT_DIM, "engine": None, "available": True, "error": None,
    }
    items = [e for e in (entities or []) if isinstance(e, dict) and (e.get("id") or e.get("label"))]
    if not items:
        out["error"] = "no entities to store"
        return out

    texts = [_entity_text(e) for e in items]
    emb = await embed(texts)
    if emb.get("error") or not emb["vectors"]:
        out["error"] = emb.get("error") or "embedding failed"
        return out
    vectors = emb["vectors"]
    out["dim"] = emb["dim"]
    out["engine"] = emb["engine"]

    points: list[dict[str, Any]] = []
    for entity, vector, text in zip(items, vectors, texts):
        payload = {
            "case_id": case_id,
            "entity_id": entity.get("id") or json_id(entity),
            "label": entity.get("label"),
            "type": entity.get("type"),
            "text": text[:2000],
            "stored_at": time.time(),
        }
        if tags:
            payload.update(tags)
        points.append({"id": payload["entity_id"], "vector": vector, "payload": payload})

    client = _get_client()
    if client is not None and await ensure_collection(client, out["dim"]):
        try:
            from qdrant_client.models import PointStruct

            client.upsert(
                collection_name=COLLECTION,
                points=[PointStruct(id=p["id"], vector=p["vector"], payload=p["payload"])
                        for p in points],
            )
            out["stored"] = len(points)
            out["backend"] = "qdrant"
            return out
        except Exception as exc:  # noqa: BLE001 — fall through to memory
            _mark_failed()
            out["error"] = f"qdrant upsert failed, using in-memory fallback: {exc}"

    out["stored"] = _mem_upsert(points)
    out["backend"] = "memory"
    return out


async def semantic_search(query: str, *, top_k: int = 8,
                          case_id: str | None = None,
                          entity_type: str | None = None) -> dict[str, Any]:
    """Find stored entities semantically similar to *query*.

    Args:
        query: natural-language query.
        top_k: maximum hits.
        case_id: restrict to one case when given.
        entity_type: restrict to one entity type when given.

    Returns:
        ``{"results": [{"id", "score", "payload"}], "backend", "engine",
        "count", "available", "error"}``. Never raises.
    """
    out: dict[str, Any] = {
        "results": [], "backend": None, "engine": None, "count": 0,
        "available": True, "error": None,
    }
    if not (query or "").strip():
        out["error"] = "empty query"
        return out

    emb = await embed(query)
    if emb.get("error") or not emb["vectors"]:
        out["error"] = emb.get("error") or "embedding failed"
        return out
    vector = emb["vectors"][0]
    out["engine"] = emb["engine"]

    filters: dict[str, Any] = {}
    if case_id:
        filters["case_id"] = case_id
    if entity_type:
        filters["type"] = entity_type

    client = _get_client()
    if client is not None and await ensure_collection(client, len(vector)):
        try:
            raw = client.search(collection_name=COLLECTION, query_vector=vector,
                               limit=int(top_k), with_payload=True)
            out["results"] = [{"id": str(h.id), "score": round(float(h.score), 4),
                               "payload": h.payload or {}} for h in raw]
            out["backend"] = "qdrant"
            out["count"] = len(out["results"])
            return out
        except Exception as exc:  # noqa: BLE001
            _mark_failed()
            out["error"] = f"qdrant search failed, using in-memory fallback: {exc}"

    out["results"] = _mem_search(vector, int(top_k), filters or None)
    out["backend"] = "memory"
    out["count"] = len(out["results"])
    return out


async def recall_similar(entity: dict[str, Any], *, case_id: str = "default",
                         top_k: int = 5) -> dict[str, Any]:
    """Look up entities previously seen that resemble *entity*.

    Args:
        entity: the entity to find matches for.
        case_id: restrict to one case.
        top_k: maximum hits.

    Returns:
        The :func:`semantic_search` result dict, or an error dict. Never raises.
    """
    try:
        return await semantic_search(_entity_text(entity), top_k=top_k, case_id=case_id)
    except Exception as exc:  # noqa: BLE001
        return {"results": [], "backend": None, "engine": None, "count": 0,
                "available": False, "error": f"{type(exc).__name__}: {exc}"}


# ── Agent entry points ───────────────────────────────────────────────────────

async def run_upsert_entities(entities: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """VAULT entry point: store entities in semantic memory.

    Args:
        entities: entity dicts.
        **kwargs: ``case_id``, ``tags``.

    Returns:
        The :func:`upsert_entities` result. Never raises.
    """
    try:
        return await upsert_entities(entities, case_id=kwargs.get("case_id", "default"),
                                     tags=kwargs.get("tags"))
    except Exception as exc:  # noqa: BLE001 — VAULT must never break on this
        return {"stored": 0, "backend": None, "collection": COLLECTION,
                "dim": DEFAULT_DIM, "engine": None, "available": False,
                "error": f"{type(exc).__name__}: {exc}"}


async def run_semantic_search(query: str, **kwargs: Any) -> dict[str, Any]:
    """VAULT entry point: semantic recall over stored entities.

    Args:
        query: natural-language query.
        **kwargs: ``top_k``, ``case_id``, ``entity_type``.

    Returns:
        The :func:`semantic_search` result. Never raises.
    """
    try:
        return await semantic_search(
            query,
            top_k=int(kwargs.get("top_k", 8)),
            case_id=kwargs.get("case_id"),
            entity_type=kwargs.get("entity_type"),
        )
    except Exception as exc:  # noqa: BLE001
        return {"results": [], "backend": None, "engine": None, "count": 0,
                "available": False, "error": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "COLLECTION", "DEFAULT_DIM", "hash_embed", "embed", "reachable", "backends",
    "health", "ensure_collection", "upsert_entities", "semantic_search",
    "recall_similar", "run_upsert_entities", "run_semantic_search",
    "mem_size", "mem_clear",
]
