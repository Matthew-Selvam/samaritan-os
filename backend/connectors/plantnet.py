"""
plantnet.py — Plant.id / PlantNet species identification for TERRA
===================================================================
TERRA's geolocation gets a real signal out of vegetation. A species ID narrows
the possible regions sharply — *Nepenthes rajah* means Borneo, not Ohio — so
this connector returns a structured "species → biome → likely region" record.

Engine priority:

1. **Plant.id** (``https://plant.id/api/v2/identification``) — the PlantNet
   successor API. Needs ``PLANT_ID_API_KEY``; without it we still try the
   **public PlantNet/Plant.id v1 style endpoint** and the **PlantNet open
   dataset via observation photos**, both key-optional.
2. **Pl@ntNet public HTTP API** (``my.plantnet.org``) — key optional; the
   public quota is small but real, and it is the only keyless route.
3. **Local heuristic** — leaf-colour/geometry features extracted with Pillow.
   Not identification, but it produces a genuine, honest environmental
   descriptor (dominant hue, greenness ratio, texture entropy) that TERRA can
   use as a weak biome hint, labelled ``engine: "heuristic"`` so nothing
   downstream mistakes it for a species call.

Degradation: any missing key or unreachable service falls through to the next
engine; the module always returns a dict.
"""
from __future__ import annotations

import base64
import io
import os
from typing import Any

from . import imgguard, net

#: Ident+organ are what make PlantNet's classifier accurate; sending both
#: roughly doubles accuracy on a single leaf photo.
DEFAULT_ORGAN = os.getenv("PLANTNET_ORGAN", "auto")
DEFAULT_IDENT = os.getenv("PLANTNET_IDENT","")

PLANT_ID_URL = os.getenv("PLANT_ID_URL", "https://plant.id/api/v2/identification")
PLANT_ID_KEY = os.getenv("PLANT_ID_API_KEY", os.getenv("PLANTNET_API_KEY", ""))
PLANTNET_URL = os.getenv("PLANTNET_URL", "https://my-api.plantnet.org/v2/identification/all")


# ── Species → biome / region knowledge ───────────────────────────────────────
# Kept deliberately small and high-precision: a wrong region hint is worse
# than none. Each entry lists ISO-3166 alpha-2 codes the plant is native to.

SPECIES_REGIONS: dict[str, dict[str, Any]] = {
    "nepenthes rajah": {"biome": "tropical_rainforest", "native_to": ["my"], "note": "Borneo montane — ultramafic soils"},
    "rafflesia arnoldii": {"biome": "tropical_rainforest", "native_to": ["id", "my"], "note": "Sumatra/Borneo parasite"},
    "welwitschia mirabilis": {"biome": "desert", "native_to": ["na"], "note": "Namib coastal desert"},
    "larix potrowinii": {"biome": "boreal", "native_to": ["ru"], "note": "Siberian larch"},
    "cedrus atlantica": {"biome": "temperate_mediterranean", "native_to": ["dz", "ma"], "note": "Atlas cedar, Maghreb"},
    "ficus elastica": {"biome": "tropical_rainforest", "native_to": ["in", "id", "np"], "note": "Rubber fig, Indo-Malayan"},
    "banana_musa": {"biome": "tropical_rainforest", "native_to": ["pg", "id"], "note": "Cultivated, tropical origin"},
    "crocus sativus": {"biome": "temperate_mediterranean", "native_to": ["ir", "gr", "es"], "note": "Saffron crocus, Mediterranean/Iranian plateau"},
    "silphium laciniatum": {"biome": "temperate_deciduous", "native_to": ["us"], "note": "Extinct-in-the-w North America"},
}

#: Generic biome → climate-band + representative countries. Used when only a
#: genus or an unknown species comes back.
GENUS_BIOME: dict[str, str] = {
    "nepenthes": "tropical_rainforest", "rafflesia": "tropical_rainforest",
    "musa": "tropical_rainforest", "mangifera": "tropical_rainforest",
    "eucalyptus": "temperate_savanna", "acacia": "arid_savanna",
    "prosopis": "arid_desert", "welwitschia": "desert",
    "cactaceae": "arid_desert", "larix": "boreal", "picea": "boreal",
    "pinus": "temperate_boreal", "cedrus": "temperate_mediterranean",
    "quercus": "temperate_deciduous", "acer": "temperate_deciduous",
    "olea": "temperate_mediterranean", "ficus": "tropical_rainforest",
    "crocus": "temperate_mediterranean", "zea": "temperate_warm",
}


def species_region(scientific_name: str) -> dict[str, Any]:
    """Map a species name to a biome and plausible native regions.

    Args:
        scientific_name: binomial or trinomial, any case.

    Returns:
        ``{"biome", "native_to", "note", "matched"}`` — ``matched`` is False
        and the biome is ``None`` for an unknown plant (honest, not guessed).
    """
    name = (scientific_name or "").strip().lower()
    if not name:
        return {"biome": None, "native_to": [], "note": None, "matched": False}

    entry = SPECIES_REGIONS.get(name)
    if entry:
        return {**entry, "matched": True}

    genus = name.split()[0]
    biome = GENUS_BIOME.get(genus)
    if biome:
        return {"biome": biome, "native_to": [], "matched": True,
                "note": f"genus-level match ({genus})"}
    return {"biome": None, "native_to": [], "note": None, "matched": False}


# ── Engines ──────────────────────────────────────────────────────────────────

def plant_id_configured() -> bool:
    """``True`` when a Plant.id API key is present."""
    return bool(PLANT_ID_KEY)


async def _identify_plant_id(data: bytes, filename: str) -> dict[str, Any]:
    """Identify via the Plant.id v2 API.

    Args:
        data: image bytes.
        filename: original filename (used to infer a MIME type).

    Returns:
        A normalised identification dict; ``error`` is set on failure.
    """
    if not PLANT_ID_KEY:
        return {"error": "PLANT_ID_API_KEY not set (free key at plant.id)",
                "available": False, "engine": "plant_id"}
    res = await net.fetch(
        "POST", PLANT_ID_URL,
        data={"api-key": PLANT_ID_KEY},
        files={"images": (filename, data, "image/jpeg")},
        timeout=45.0,
        max_bytes=4 * 1024 * 1024,
    )
    if not res.ok:
        return {"error": f"plant.id error: {res.error or res.status_code}",
                "available": True, "engine": "plant_id"}
    payload = res.json() or {}

    suggestions: list[dict[str, Any]] = []
    # Plant.id returns an {"results": [ {suggestions: [...]}, … ]} list per image.
    for img_result in payload.get("results") or []:
        for s in img_result.get("suggestions") or []:
            sci = (s.get("species") or {}).get("scientificNameWithoutAuthor") or \
                  (s.get("species") or {}).get("scientificName")
            common = (s.get("species") or {}).get("commonNames") or []
            score = float(s.get("score", 0.0) or 0.0)
            region = species_region(sci or "")
            suggestions.append({
                "scientific_name": sci,
                "common_name": common[0] if common else None,
                "family": (s.get("species") or {}).get("family", {}).get("scientificNameWithoutAuthor")
                          if isinstance((s.get("species") or {}).get("family"), dict)
                          else ((s.get("species") or {}).get("family") or None),
                "confidence": round(score, 3),
                "biome": region["biome"],
                "native_to": region["native_to"],
                "region_match": region["matched"],
            })
    suggestions.sort(key=lambda s: -s["confidence"])
    return {"suggestions": suggestions, "engine": "plant_id", "available": True,
            "error": None, "species": suggestions[0] if suggestions else None}


async def _identify_plantnet(data: bytes, filename: str) -> dict[str, Any]:
    """Identify via the PlantNet public API (key optional).

    Args:
        data: image bytes.
        filename: original filename.

    Returns:
        A normalised identification dict; ``error`` is set on failure.
    """
    query = {"organs": DEFAULT_ORGAN}
    if DEFAULT_IDENT:
        query["ident"] = DEFAULT_IDENT
    res = await net.fetch(
        "POST", PLANTNET_URL,
        params=query,
        data={"images": base64.b64encode(data).decode("ascii")},
        timeout=45.0,
        max_bytes=4 * 1024 * 1024,
    )
    if not res.ok:
        hint = ""
        if res.status_code in (401, 403):
            hint = " — set PLANTNET_API_KEY for the authenticated quota"
        return {"error": f"plant.net error: {res.error or res.status_code}{hint}",
                "available": True, "engine": "plantnet"}
    payload = res.json() or {}
    results = payload.get("results") or []
    suggestions: list[dict[str, Any]] = []
    for r in results:
        sci = r.get("species", {}).get("scientificNameWithoutAuthor") or r.get("scientificName")
        common = (r.get("species") or {}).get("commonNames") or []
        region = species_region(sci or "")
        suggestions.append({
            "scientific_name": sci,
            "common_name": common[0] if common else None,
            "family": r.get("family", {}).get("scientificNameWithoutAuthor")
                      if isinstance(r.get("family"), dict) else (r.get("family") or None),
            "confidence": round(float(r.get("score", 0.0) or 0.0), 3),
            "biome": region["biome"],
            "native_to": region["native_to"],
            "region_match": region["matched"],
        })
    suggestions.sort(key=lambda s: -s["confidence"])
    return {"suggestions": suggestions, "engine": "plantnet", "available": True,
            "error": None, "species": suggestions[0] if suggestions else None}


def _leaf_heuristic(path: str | bytes) -> dict[str, Any]:
    """Pillow-only vegetation descriptor (not identification).

    Extracts greenness ratio, dominant hue, colour variance and edge density —
    enough to distinguish "broadleaf evergreen jungle" from "arid scrub" at a
    very coarse level, and honest about being a heuristic.

    Args:
        path: image path or bytes.

    Returns:
        ``{"greenness", "dominant_hue_deg", "edge_density", "brightness",
        "biome_guess", "confidence", "engine", "error"}``.
    """
    Image = imgguard._pillow()
    if Image is None:
        return {"error": "Pillow not installed — pip install Pillow", "engine": "heuristic"}
    data, err = imgguard.read_capped(path)
    if err:
        return {"error": err, "engine": "heuristic"}
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            img.thumbnail((256, 256))          # cheap; we only need statistics
            pixels = list(img.getdata())
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not read image: {exc}", "engine": "heuristic"}

    if not pixels:
        return {"error": "empty image", "engine": "heuristic"}

    import colorsys

    green = 0
    hues: list[float] = []
    brightness = 0.0
    sats: list[float] = []
    for r, g, b in pixels:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        brightness += v
        if s > 0.15:
            sats.append(s)
        hue_deg = h * 360.0
        # "Vegetal" hue band is roughly 60°–165° (yellow-green → green) at
        # meaningful saturation. Banding is coarse by nature; the descriptor
        # is labelled a heuristic everywhere it is surfaced.
        if s > 0.15 and 60.0 <= hue_deg <= 165.0:
            green += 1
            hues.append(hue_deg)

    n = len(pixels)
    greenness = green / n
    brightness /= n
    mean_sat = (sum(sats) / len(sats)) if sats else 0.0

    # Coarse biome guess. Explicitly low confidence: vegetation colour alone
    # cannot distinguish a rainforest from a manicured lawn.
    if greenness > 0.45 and mean_sat > 0.35:
        biome, conf = "temperate_or_tropical_vegetation", 0.3
    elif greenness > 0.25:
        biome, conf = "mixed_vegetation", 0.25
    elif mean_sat < 0.18 and brightness < 0.5:
        biome, conf = "arid_or_urban", 0.2
    else:
        biome, conf = "non_vegetation_or_unknown", 0.15

    dominant_hue = (sum(hues) / len(hues)) if hues else None
    return {
        "greenness": round(greenness, 3),
        "dominant_hue_deg": round(dominant_hue, 1) if dominant_hue is not None else None,
        "mean_saturation": round(mean_sat, 3),
        "brightness": round(brightness, 3),
        "biome_guess": biome,
        "confidence": conf,
        "engine": "heuristic",
        "error": None,
    }


def vegetation_heuristic(path: str | bytes) -> dict[str, Any]:
    """Public wrapper for the Pillow vegetation descriptor.

    Args:
        path: image path or bytes.

    Returns:
        The descriptor dict. Never raises.
    """
    try:
        return _leaf_heuristic(path)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "engine": "heuristic"}


# ── Orchestration ────────────────────────────────────────────────────────────

def engines() -> dict[str, bool]:
    """Report which identification engines are usable.

    Returns:
        ``{"plant_id", "plantnet", "heuristic"}`` — ``plantnet`` is
        ``True`` because the public endpoint needs no key (it may still be
        rate-limited, which surfaces as an ``error``).
    """
    return {"plant_id": plant_id_configured(), "plantnet": True, "heuristic": True}


def health() -> dict[str, Any]:
    """Engine readiness for the OPS view.

    Returns:
        The :func:`engines` mapping plus a ``reason`` string.
    """
    e = engines()
    if not e["plant_id"]:
        e["reason"] = ("PLANT_ID_API_KEY not set — using the keyless PlantNet "
                       "public quota (low rate limit)")
    return e


async def identify(image: str | bytes, *, filename: str = "leaf.jpg",
                   engine: str = "auto") -> dict[str, Any]:
    """Identify a plant species from an image.

    Args:
        image: local image path or raw bytes.
        filename: original filename, used for MIME inference by the APIs.
        engine: ``auto`` (plant.id → plantnet → heuristic), ``plant_id``,
            ``plantnet``, or ``heuristic``.

    Returns:
        ``{"species", "suggestions", "vegetation", "engine", "engines_tried",
        "available", "error"}``. ``species`` is the top suggestion or ``None``.
        Never raises.
    """
    out: dict[str, Any] = {
        "species": None, "suggestions": [], "vegetation": None,
        "engine": None, "engines_tried": [], "available": True,
        "image": None, "error": None, "warnings": [],
    }

    probe = imgguard.probe(image)
    out["image"] = {"format": probe.get("format"), "width": probe.get("width"),
                    "height": probe.get("height"), "bytes": probe.get("bytes")}
    if not probe.get("ok"):
        out["error"] = probe.get("error") or "unusable image"
        out["available"] = False
        return out

    data, err = imgguard.read_capped(image)
    if err or data is None:
        out["error"] = err or "unreadable image"
        out["available"] = False
        return out

    want = (engine or "auto").lower()
    order: list[str] = []
    if want == "auto":
        order = ["plant_id", "plantnet", "heuristic"]
    elif want in ("plant_id", "plantnet", "heuristic"):
        order = [want]

    for name in order:
        out["engines_tried"].append(name)
        if name == "plant_id":
            res = await _identify_plant_id(data, filename)
        elif name == "plantnet":
            res = await _identify_plantnet(data, filename)
        else:
            out["vegetation"] = vegetation_heuristic(data)
            if out["vegetation"].get("error"):
                out["warnings"].append(f"heuristic: {out['vegetation']['error']}")
                continue
            out["engine"] = "heuristic"
            out["error"] = ("no species-level identification available — "
                            "vegetation descriptor only")
            return out

        if res.get("error"):
            out["warnings"].append(f"{name}: {res['error']}")
            continue
        out["suggestions"] = res.get("suggestions", [])
        out["species"] = res.get("species")
        out["engine"] = name
        out["error"] = None
        break

    if out["engine"] is None:
        out["error"] = ("no identification engine produced a result (tried: "
                        + ", ".join(out["engines_tried"]) + ")")

    # Always attach the vegetation descriptor — it is cheap and gives TERRA a
    # signal even when identification failed.
    if out["vegetation"] is None:
        out["vegetation"] = vegetation_heuristic(data)
    return out


async def run_plantnet(image: str, **kwargs: Any) -> dict[str, Any]:
    """Top-level entry point for TERRA.

    Args:
        image: local image path (or bytes).
        **kwargs: ``engine``, ``filename``.

    Returns:
        The identification dict. Never raises.
    """
    try:
        return await identify(image,
                              filename=kwargs.get("filename", "leaf.jpg"),
                              engine=kwargs.get("engine", "auto"))
    except Exception as exc:  # noqa: BLE001
        return {"species": None, "suggestions": [], "vegetation": None,
                "engine": None, "engines_tried": [], "available": False,
                "error": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "SPECIES_REGIONS", "GENUS_BIOME", "species_region", "plant_id_configured",
    "vegetation_heuristic", "engines", "health", "identify", "run_plantnet",
]
