"""
vision.py — Visual geolocation + scene connector
================================================
Powers the IRIS (Vision) agent. Scoped to *where was this taken and what is
in the frame* — NOT to identifying private individuals.

Capabilities (each degrades gracefully if its backend isn't installed):
  1. OCR signage         — tesseract binary → text blocks in the image
  2. Script/region hint  — Unicode-range analysis of OCR text → candidate
                           writing systems, languages, and world regions
  3. Scene cues          — OpenCV: brightness/daypart, dominant colors,
                           greenery ratio (weak biome signal, flagged as such)
  4. Geo embedding        — optional StreetCLIP/GeoCLIP adapter (torch/transformers);
                           returns a structured "backend unavailable" note if absent

POLICY: face recognition / face-to-identity matching is intentionally NOT
implemented here. `recognize_faces()` is a hard no-op by design — identifying
a private individual against their will is out of scope for this tool.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import io
from dataclasses import dataclass, asdict
from typing import Any

import numpy as np

try:
    import cv2
    _HAS_CV2 = True
except Exception:  # pragma: no cover
    _HAS_CV2 = False


# --------------------------------------------------------------------------- #
#  Unicode script -> (writing system, example languages, world regions)
# --------------------------------------------------------------------------- #
_SCRIPT_RANGES: list[tuple[int, int, str, list[str], list[str]]] = [
    (0x0590, 0x05FF, "Hebrew",      ["Hebrew"],                 ["Israel"]),
    (0x0600, 0x06FF, "Arabic",      ["Arabic", "Farsi", "Urdu"],["MENA", "Gulf", "Pakistan"]),
    (0x0900, 0x097F, "Devanagari",  ["Hindi", "Marathi"],       ["India", "Nepal"]),
    (0x0B80, 0x0BFF, "Tamil",       ["Tamil"],                  ["South India", "Sri Lanka", "Singapore"]),
    (0x0E00, 0x0E7F, "Thai",        ["Thai"],                   ["Thailand"]),
    (0x1100, 0x11FF, "Hangul",      ["Korean"],                 ["Korea"]),
    (0xAC00, 0xD7AF, "Hangul",      ["Korean"],                 ["Korea"]),
    (0x3040, 0x309F, "Hiragana",    ["Japanese"],               ["Japan"]),
    (0x30A0, 0x30FF, "Katakana",    ["Japanese"],               ["Japan"]),
    (0x4E00, 0x9FFF, "Han",         ["Chinese", "Japanese"],    ["China", "Taiwan", "Japan", "Singapore"]),
    (0x0400, 0x04FF, "Cyrillic",    ["Russian", "Ukrainian"],   ["Russia", "Eastern Europe", "Central Asia"]),
    (0x0370, 0x03FF, "Greek",       ["Greek"],                  ["Greece", "Cyprus"]),
]


@dataclass
class OCRResult:
    text: str
    n_chars: int
    scripts: list[str]
    candidate_languages: list[str]
    candidate_regions: list[str]
    backend: str


@dataclass
class SceneCues:
    brightness: float | None = None          # 0–255 mean luma
    daypart_hint: str | None = None          # bright/dim — weak
    greenery_ratio: float | None = None      # 0–1 vegetation-ish pixels
    dominant_rgb: list[int] | None = None
    biome_hint: str | None = None            # heuristic, low confidence
    backend: str = "opencv" if _HAS_CV2 else "unavailable"


# --------------------------------------------------------------------------- #
#  Image loading
# --------------------------------------------------------------------------- #
def _to_tempfile(image: str | bytes) -> tuple[str, bool]:
    """Return (path, is_temp). Accepts a path or raw bytes."""
    if isinstance(image, (bytes, bytearray)):
        fd, path = tempfile.mkstemp(suffix=".img")
        with os.fdopen(fd, "wb") as f:
            f.write(image)
        return path, True
    return image, False


def _load_bgr(image: str | bytes):
    if not _HAS_CV2:
        return None
    if isinstance(image, (bytes, bytearray)):
        arr = np.frombuffer(image, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return cv2.imread(image, cv2.IMREAD_COLOR)


# --------------------------------------------------------------------------- #
#  1 + 2. OCR + script/region inference
# --------------------------------------------------------------------------- #
def detect_scripts(text: str) -> tuple[list[str], list[str], list[str]]:
    scripts, langs, regions = {}, [], []
    for ch in text:
        cp = ord(ch)
        for lo, hi, name, ls, rs in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                scripts[name] = scripts.get(name, 0) + 1
                for x in ls:
                    if x not in langs:
                        langs.append(x)
                for x in rs:
                    if x not in regions:
                        regions.append(x)
                break
    if any(c.isascii() and c.isalpha() for c in text) and not scripts:
        scripts["Latin"] = 1
    ordered = [s for s, _ in sorted(scripts.items(), key=lambda kv: -kv[1])]
    return ordered, langs, regions


def ocr_image(image: str | bytes, lang: str = "eng") -> OCRResult:
    """Run the tesseract binary directly (no pytesseract dependency)."""
    binary = shutil.which("tesseract")
    if not binary:
        return OCRResult("", 0, [], [], [], backend="unavailable: install tesseract")

    path, is_temp = _to_tempfile(image)
    try:
        proc = subprocess.run(
            [binary, path, "stdout", "-l", lang, "--psm", "11"],
            capture_output=True, timeout=30,
        )
        text = proc.stdout.decode("utf-8", "replace").strip()
    except (subprocess.TimeoutExpired, Exception) as e:  # noqa
        return OCRResult("", 0, [], [], [], backend=f"error: {e}")
    finally:
        if is_temp and os.path.exists(path):
            os.remove(path)

    scripts, langs, regions = detect_scripts(text)
    return OCRResult(text=text, n_chars=len(text), scripts=scripts,
                     candidate_languages=langs, candidate_regions=regions,
                     backend="tesseract")


# --------------------------------------------------------------------------- #
#  3. Scene cues (OpenCV) — honest, low-confidence environmental signal
# --------------------------------------------------------------------------- #
def scene_cues(image: str | bytes) -> SceneCues:
    bgr = _load_bgr(image)
    if bgr is None:
        return SceneCues(backend="unavailable")

    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    luma = float(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).mean())

    # greenery: hue in green band with reasonable saturation/value
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    green = ((h >= 35) & (h <= 85) & (s >= 40) & (v >= 40))
    green_ratio = float(green.mean())

    mean_bgr = bgr.reshape(-1, 3).mean(axis=0)
    dom_rgb = [int(mean_bgr[2]), int(mean_bgr[1]), int(mean_bgr[0])]

    biome = None
    if green_ratio > 0.35:
        biome = "vegetated (forest/park/rural) — low confidence"
    elif green_ratio < 0.05 and luma > 150:
        biome = "arid/built/snow — low confidence"

    return SceneCues(
        brightness=round(luma, 1),
        daypart_hint="bright" if luma > 110 else "dim",
        greenery_ratio=round(green_ratio, 3),
        dominant_rgb=dom_rgb,
        biome_hint=biome,
    )


# --------------------------------------------------------------------------- #
#  4. Geo-embedding adapter (optional heavy backend)
# --------------------------------------------------------------------------- #
def geoclip_estimate(image: str | bytes, *, model: str = "geolocal/StreetCLIP") -> dict:
    """
    Region estimate from a visual geolocation embedding model.
    Requires torch + transformers + the model weights. If unavailable,
    returns a structured note instead of raising (codebase convention).
    """
    try:
        import torch  # noqa
        from transformers import CLIPModel, CLIPProcessor  # noqa
    except Exception:
        return {"backend": "unavailable",
                "note": f"install torch+transformers and pull {model} to enable "
                        "visual region estimation on EXIF-stripped photos"}
    # Intentionally not auto-downloading multi-GB weights here. Wire the actual
    # zero-shot country/region scoring once the model is provisioned.
    return {"backend": model, "note": "model provisioned hook — implement zero-shot scoring"}


# --------------------------------------------------------------------------- #
#  POLICY no-op
# --------------------------------------------------------------------------- #
def recognize_faces(*_args, **_kwargs) -> dict:
    """
    Disabled by design. This connector geolocates scenes; it does not match
    faces to the identities of private individuals.
    """
    return {"backend": "disabled_by_policy",
            "note": "face-to-identity matching is intentionally not implemented"}
