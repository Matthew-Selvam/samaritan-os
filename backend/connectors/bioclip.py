"""
bioclip.py — BioCLIP / CLIP zero-shot ecological classification for TERRA
==========================================================================
The complementary signal to :mod:`plantnet`: instead of naming a species, this
labels the *scene* ecologically — which is often the more useful geolocation
input, because a streetview of Amazon canopy and one of a Parisian park both
read "temperate_or_tropical_vegetation" to a leaf classifier but differ wildly
to a scene classifier.

Engines, in order:

1. **BioCLIP via ``transformers``** — the real model
   (`` Perpetual/bio-vit-large-patch16`` or any local CLIP path). Lazy import,
   loaded once, CPU by default. Returns zero-shot probabilities over a
   fixed ecological label set.
2. **Ollama vision** — a live local Qwen2.5-VL style model at
   ``OLLAMA_URL`` classifies the scene against the same label set. This is the
   engine that works in this environment (a vision model is already running),
   so it is a real path, not a stub.
3. **Nothing** — returns ``{"error": ..., "available": False}``.

Degradation contract: with neither transformers nor a reachable Ollama, the
call returns a structured error and TERRA keeps whatever other signals it has.
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
from typing import Any

from . import imgguard, net

#: Ecological / scene label set. Deliberately chosen so each label implies a
#: climate band — the labels ARE the geolocation signal.
ECO_LABELS: tuple[str, ...] = (
    "tropical rainforest",
    "temperate forest",
    "boreal or taiga forest",
    "savanna or grassland",
    "desert or semi-arid scrub",
    "mangrove or wetland",
    "temperate grassland or farmland",
    "Mediterranean or chaparral",
    "urban street or city",
    "indoor or built interior",
    "coastal or beach",
    "alpine or highland",
)

#: label → (biome, ISO-3166 alpha-2 countries that plausibly match)
LABEL_GEO: dict[str, dict[str, Any]] = {
    "tropical rainforest": {"biome": "tropical_rainforest", "regions": ["br", "id", "my", "cd", "cg", "pe", "gh", "ci"]},
    "temperate forest": {"biome": "temperate_deciduous", "regions": ["de", "fr", "us", "gb", "ru", "jp", "kr", "ca"]},
    "boreal or taiga forest": {"biome": "boreal", "regions": ["ru", "ca", "se", "fi", "no", "kz"]},
    "savanna or grassland": {"biome": "savanna", "regions": ["ng", "ke", "za", "br", "au", "ar", "in"]},
    "desert or semi-arid scrub": {"biome": "arid_desert", "regions": ["sa", "eg", "ma", "au", "mx", "in", "za", "cl"]},
    "mangrove or wetland": {"biome": "mangrove_wetland", "regions": ["id", "my", "bd", "mz", "pg", "ec"]},
    "temperate grassland or farmland": {"biome": "temperate_grassland", "regions": ["us", "ca", "ua", "fr", "hu", "ar", "au"]},
    "Mediterranean or chaparral": {"biome": "temperate_mediterranean", "regions": ["es", "it", "gr", "tr", "il", "ma", "pt", "au"]},
    "urban street or city": {"biome": "urban", "regions": []},
    "indoor or built interior": {"biome": "indoor", "regions": []},
    "coastal or beach": {"biome": "coastal", "regions": ["id", "es", "gr", "th", "au", "br", "mx"]},
    "alpine or highland": {"biome": "alpine", "regions": ["ch", "no", "pe", "np", "ke", "ec", "at"]},
}

BIOCLIP_MODEL = os.getenv("BIOCLIP_MODEL", "Perpetual/bio-vit-large-patch16")
BIOCLIP_DEVICE = os.getenv("BIOCLIP_DEVICE", "cpu")
BIOCLIP_ENABLED = os.getenv("BIOCLIP_ENABLED", "true").lower() == "true"
_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
_OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "qwen2.5vl:7b")
_OLLAMA_ENABLED = os.getenv("BIOCLIP_OLLAMA_ENABLED", "true").lower() == "true"
#: Ollama is a local service; it is explicitly allowed to be on loopback.
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")

_PIPELINE: Any = None


# ── Availability ─────────────────────────────────────────────────────────────

def transformers_installed() -> bool:
    """``True`` when ``transformers`` is importable."""
    try:
        import transformers  # noqa: F401  (lazy optional probe)
        return True
    except ImportError:
        return False


def bioclip_installed() -> bool:
    """``True`` when the transformers CLIP path can be used."""
    return BIOCLIP_ENABLED and transformers_installed()


def ollama_enabled() -> bool:
    """``True`` when the Ollama vision fallback is enabled."""
    return _OLLAMA_ENABLED


def engines() -> dict[str, bool]:
    """Report which classification engines are *configured*.

    Returns:
        ``{"bioclip": bool, "ollama": bool, "available": bool}``. ``ollama``
        reflects configuration, not liveness — that is checked per call.
    """
    return {"bioclip": bioclip_installed(), "ollama": ollama_enabled(),
            "available": bioclip_installed() or ollama_enabled()}


def health() -> dict[str, Any]:
    """Engine readiness for the OPS view.

    Returns:
        The :func:`engines` mapping plus a ``reason`` string when nothing is
        configured.
    """
    e = engines()
    if not e["available"]:
        e["reason"] = ("neither transformers (BioCLIP) nor OLLAMA_VISION_MODEL is "
                       "configured — scene classification unavailable")
    return e


# ── BioCLIP / CLIP engine ────────────────────────────────────────────────────

def _get_pipeline() -> Any:
    """Load and cache the zero-shot image-classification pipeline.

    Returns:
        A ``transformers`` pipeline, or ``None`` when unavailable.
    """
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE
    if not bioclip_installed():
        return None
    try:
        from transformers import pipeline  # lazy, optional heavy import
        _PIPELINE = pipeline("zero-shot-image-classification",
                             model=BIOCLIP_MODEL, device=BIOCLIP_DEVICE)
        return _PIPELINE
    except Exception:  # noqa: BLE001 — model download/auth failure
        _PIPELINE = None
        return None


def _classify_local(image: Any) -> dict[str, Any]:
    """Run zero-shot classification on a Pillow image (blocking).

    Args:
        image: an open ``PIL.Image.Image``.

    Returns:
        ``{"scores": [...], "top_label", "confidence", "engine", "error"}``.
    """
    pipe = _get_pipeline()
    if pipe is None:
        return {"error": "BioCLIP/transformers pipeline unavailable", "engine": "bioclip"}
    try:
        raw = pipe(image, candidate_labels=list(ECO_LABELS))
        scores = [{"label": r["label"], "score": round(float(r["score"]), 4)} for r in raw]
        scores.sort(key=lambda s: -s["score"])
        top = scores[0] if scores else None
        return {"scores": scores, "top_label": top["label"] if top else None,
                "confidence": top["score"] if top else None,
                "engine": "bioclip", "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"bioclip failed: {type(exc).__name__}: {exc}", "engine": "bioclip"}


# ── Ollama vision engine ─────────────────────────────────────────────────────

async def ollama_reachable(timeout: float = 3.0) -> bool:
    """``True`` when the local Ollama server answers a tags request.

    Args:
        timeout: per-request timeout in seconds.

    Returns:
        Liveness boolean. Never raises.
    """
    url = _OLLAMA_URL.rstrip("/") + "/api/tags"
    try:
        res = await net.fetch("GET", url, timeout=timeout,
                              allow_private=True,   # Ollama is a local service
                              use_opsec=False,      # never route localhost via Tor
                              max_bytes=256 * 1024)
        return bool(res.ok)
    except Exception:  # noqa: BLE001
        return False


async def _classify_ollama(data: bytes, labels: tuple[str, ...]) -> dict[str, Any]:
    """Classify the scene with a local Ollama vision model.

    Args:
        data: image bytes.
        labels: the candidate label set to choose from.

    Returns:
        ``{"scores": [...], "top_label", "confidence", "engine", "error"}``.
    """
    if not data:
        return {"error": "no image data", "engine": "ollama"}
    b64 = base64.b64encode(data).decode("ascii")
    prompt = (
        "You are a geospatial image analyst. Look at this image and choose the "
        "single best label from this list:\n"
        + "\n".join(f"- {label}" for label in labels)
        + "\n\nReply with ONLY the chosen label, copied exactly. No explanation."
    )
    payload = {
        "model": _OLLAMA_VISION_MODEL,
        "prompt": prompt,
        "images": [b64],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 24},
    }
    try:
        res = await net.post_json(
            _OLLAMA_URL.rstrip("/") + "/api/generate", payload,
            allow_private=True,   # local service
            use_opsec=False,      # localhost is never proxied through Tor
            timeout=180.0,
            max_bytes=1024 * 1024,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"ollama request failed: {exc}", "engine": "ollama"}

    if not isinstance(res, dict):
        return {"error": "ollama returned no usable JSON", "engine": "ollama"}
    if res.get("error"):
        return {"error": f"ollama error: {res['error']}", "engine": "ollama"}

    text = str(res.get("response") or "").strip().strip('"').lower()
    if not text:
        return {"error": "ollama returned an empty response", "engine": "ollama"}

    # Map the model's free-text reply onto the label set by best overlap, so a
    # slightly-off answer still lands on a real label rather than being dropped.
    best, best_score = None, 0.0
    for label in labels:
        lab_tokens = set(label.lower().replace("or", " ").split())
        reply_tokens = set(text.replace("or", " ").split())
        overlap = len(lab_tokens & reply_tokens)
        if label.lower() in text:
            overlap += 2
        score = overlap / (len(lab_tokens) or 1)
        if score > best_score:
            best, best_score = label, score
    if best is None:
        return {"error": f"could not map model reply to a label: {text[:80]!r}",
                "engine": "ollama", "raw_reply": text[:200]}

    return {
        "scores": [{"label": best, "score": round(min(1.0, best_score), 4)}],
        "top_label": best,
        "confidence": round(min(1.0, best_score), 4),
        "engine": "ollama",
        "raw_reply": text[:200],
        "error": None,
    }


# ── Orchestration ────────────────────────────────────────────────────────────

async def classify(image: str | bytes, *, labels: tuple[str, ...] | None = None,
                   engine: str = "auto") -> dict[str, Any]:
    """Classify an image's ecological setting.

    Args:
        image: local image path or raw bytes.
        labels: candidate label set; defaults to :data:`ECO_LABELS`.
        engine: ``auto`` (bioclip → ollama), ``bioclip``, ``ollama``, or ``off``.

    Returns:
        ``{"top_label", "confidence", "scores", "biome", "candidate_regions",
        "engine", "engines_tried", "available", "error"}``. Never raises.
    """
    labels = tuple(labels or ECO_LABELS)
    out: dict[str, Any] = {
        "top_label": None, "confidence": None, "scores": [], "biome": None,
        "candidate_regions": [], "engine": None, "engines_tried": [],
        "available": False, "error": None, "warnings": [],
    }

    probe = imgguard.probe(image)
    if not probe.get("ok"):
        out["error"] = probe.get("error") or "unusable image"
        return out

    data, err = imgguard.read_capped(image)
    if err or data is None:
        out["error"] = err or "unreadable image"
        return out

    want = (engine or "auto").lower()
    order: list[str] = []
    if want == "auto":
        order = ["bioclip", "ollama"] if bioclip_installed() else ["ollama"]
    elif want in ("bioclip", "ollama"):
        order = [want]
    elif want == "off":
        out["error"] = "scene classification disabled"
        return out

    for name in order:
        out["engines_tried"].append(name)
        if name == "bioclip":
            Image = imgguard._pillow()
            if Image is None:
                out["warnings"].append("bioclip: Pillow not installed")
                continue
            try:
                with Image.open(io.BytesIO(data)) as raw_img:
                    res = await asyncio.wait_for(
                        asyncio.to_thread(_classify_local, raw_img.convert("RGB")),
                        timeout=180.0,
                    )
            except asyncio.TimeoutError:
                out["warnings"].append("bioclip: timed out after 180s")
                continue
        else:
            if not ollama_enabled():
                out["warnings"].append("ollama: disabled")
                continue
            res = await _classify_ollama(data, labels)

        if res.get("error"):
            out["warnings"].append(f"{name}: {res['error']}")
            continue
        out.update({
            "scores": res.get("scores", []),
            "top_label": res.get("top_label"),
            "confidence": res.get("confidence"),
            "engine": name,
            "available": True,
            "error": None,
        })
        break

    if out["top_label"]:
        geo = LABEL_GEO.get(out["top_label"], {})
        out["biome"] = geo.get("biome")
        out["candidate_regions"] = geo.get("regions", [])
    if out["engine"] is None:
        out["error"] = ("no scene-classification engine produced a result (tried: "
                        + ", ".join(out["engines_tried"] or ["none"]) + ")")
    return out


async def run_bioclip(image: str, **kwargs: Any) -> dict[str, Any]:
    """Top-level entry point for TERRA.

    Args:
        image: local image path (or bytes).
        **kwargs: ``engine``, ``labels``.

    Returns:
        The classification dict. Never raises.
    """
    try:
        return await classify(image, labels=kwargs.get("labels"),
                              engine=kwargs.get("engine", "auto"))
    except Exception as exc:  # noqa: BLE001
        return {"top_label": None, "confidence": None, "scores": [], "biome": None,
                "candidate_regions": [], "engine": None, "engines_tried": [],
                "available": False, "error": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "ECO_LABELS", "LABEL_GEO", "transformers_installed", "bioclip_installed",
    "ollama_enabled", "ollama_reachable", "engines", "health", "classify",
    "run_bioclip",
]
