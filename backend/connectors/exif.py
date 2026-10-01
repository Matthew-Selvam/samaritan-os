"""
exif.py — Image metadata + geolocation connector
=================================================
Self-contained, no API keys required. Powers the TERRA (GEOINT) agent.

What it does (legitimately scoped — identifies *a place and what's in frame*):
  1. EXIF extraction      — camera make/model, lens, timestamp, software, orientation
  2. GPS → coordinates    — parses EXIF GPS IFD into decimal lat/lon + altitude
  3. Geo inference        — hemisphere, rough climate band, maps deep-links
  4. Temporal inference   — local season from timestamp + hemisphere, day/night hint
  5. Visual-cue scaffold  — typed fields a downstream vision model (IRIS) fills in:
                            signage/OCR text, plant species, animal species, biome,
                            architecture style — each with confidence + evidence.

Optional online reverse-geocode (Nominatim/OSM) is gated behind `online=True`
and degrades gracefully to offline-only output if unavailable.

This module also keeps the async `ExifConnector` / `run_exif` surface the
IRIS and TERRA agents call. Both paths share the single offline parser below,
so a GPS fix lands in one place.
"""
from __future__ import annotations

import asyncio
import io
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from PIL import Image, ExifTags

from opsec import OpsecClient

# Reverse maps: tag name -> id
_TAG_BY_NAME = {v: k for k, v in ExifTags.TAGS.items()}
_GPS_TAG_BY_NAME = {v: k for k, v in ExifTags.GPSTAGS.items()}


# --------------------------------------------------------------------------- #
#  Data model
# --------------------------------------------------------------------------- #
@dataclass
class GeoPoint:
    lat: float
    lon: float
    altitude_m: float | None = None
    source: str = "exif_gps"

    @property
    def hemisphere_ns(self) -> str:
        return "Northern" if self.lat >= 0 else "Southern"

    @property
    def hemisphere_ew(self) -> str:
        return "Eastern" if self.lon >= 0 else "Western"

    def climate_band(self) -> str:
        a = abs(self.lat)
        if a < 23.5:
            return "tropical"
        if a < 35:
            return "subtropical"
        if a < 55:
            return "temperate"
        if a < 66.5:
            return "subpolar"
        return "polar"

    def maps_links(self) -> dict[str, str]:
        return {
            "google": f"https://www.google.com/maps/search/?api=1&query={self.lat},{self.lon}",
            "osm": f"https://www.openstreetmap.org/?mlat={self.lat}&mlon={self.lon}#map=15/{self.lat}/{self.lon}",
            "bing": f"https://www.bing.com/maps?cp={self.lat}~{self.lon}&lvl=15",
        }


@dataclass
class VisualCue:
    """A single inference a vision model (IRIS) can attach to the scene."""
    kind: str                      # signage | plant | animal | biome | architecture | text
    value: str                     # e.g. "Quercus robur", "Arabic shop signage"
    confidence: float = 0.0        # 0.0–1.0
    evidence: str = ""             # why — what in the frame supports it


@dataclass
class GeoReport:
    has_exif: bool = False
    camera: dict[str, Any] = field(default_factory=dict)
    timestamp: str | None = None
    timestamp_source: str | None = None
    geo: dict[str, Any] | None = None          # serialized GeoPoint + derived
    temporal: dict[str, Any] = field(default_factory=dict)
    visual_cues: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
#  EXIF parsing
# --------------------------------------------------------------------------- #
def _to_float(x: Any) -> float | None:
    try:
        # Pillow IFDRational, tuples, ints, floats
        if isinstance(x, tuple) and len(x) == 2:
            return x[0] / x[1] if x[1] else None
        return float(x)
    except (TypeError, ZeroDivisionError, ValueError):
        return None


def _dms_to_decimal(dms, ref: str | None) -> float | None:
    """Convert EXIF (deg, min, sec) + ref (N/S/E/W) to signed decimal degrees."""
    try:
        d = _to_float(dms[0]) or 0.0
        m = _to_float(dms[1]) or 0.0
        s = _to_float(dms[2]) or 0.0
    except (TypeError, IndexError):
        return None
    dec = d + m / 60.0 + s / 3600.0
    if ref and str(ref).upper() in ("S", "W"):
        dec = -dec
    return round(dec, 6)


def _parse_gps(gps_ifd: dict) -> GeoPoint | None:
    named = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
    lat = _dms_to_decimal(named.get("GPSLatitude"), named.get("GPSLatitudeRef"))
    lon = _dms_to_decimal(named.get("GPSLongitude"), named.get("GPSLongitudeRef"))
    if lat is None or lon is None:
        return None
    alt = _to_float(named.get("GPSAltitude"))
    if alt is not None and _to_float(named.get("GPSAltitudeRef")) == 1:
        alt = -alt
    return GeoPoint(lat=lat, lon=lon, altitude_m=alt)


def _season(month: int, hemisphere: str) -> str:
    north = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring",
             5: "spring", 6: "summer", 7: "summer", 8: "summer", 9: "autumn",
             10: "autumn", 11: "autumn"}
    s = north.get(month, "unknown")
    if hemisphere == "Southern":
        flip = {"winter": "summer", "summer": "winter",
                "spring": "autumn", "autumn": "spring"}
        s = flip.get(s, s)
    return s


# --------------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------------- #
def extract(image_path_or_bytes: str | bytes) -> GeoReport:
    """
    Parse an image (path or raw bytes) into a GeoReport.
    Pure-local, never touches the network, never raises on bad input.
    """
    report = GeoReport()

    try:
        if isinstance(image_path_or_bytes, (bytes, bytearray)):
            img = Image.open(io.BytesIO(image_path_or_bytes))
        else:
            if not os.path.exists(image_path_or_bytes):
                report.notes.append(f"file not found: {image_path_or_bytes}")
                return report
            img = Image.open(image_path_or_bytes)

        exif = img.getexif()
    except Exception as exc:
        report.notes.append(f"unreadable image: {type(exc).__name__}: {exc}")
        return report

    if not exif:
        report.notes.append("no EXIF block — likely stripped (common on social platforms)")
        return report
    report.has_exif = True

    # Camera / device
    for name in ("Make", "Model", "LensModel", "Software", "Orientation"):
        tid = _TAG_BY_NAME.get(name)
        if tid and tid in exif:
            report.camera[name] = str(exif[tid]).strip("\x00 ")

    # Timestamp — prefer the EXIF SubIFD DateTimeOriginal, fall back to DateTime
    sub = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else {}
    dt_raw, dt_src = None, None
    if sub:
        for name, src in (("DateTimeOriginal", "exif_original"),
                          ("DateTimeDigitized", "exif_digitized")):
            tid = _TAG_BY_NAME.get(name)
            if tid and tid in sub:
                dt_raw, dt_src = str(sub[tid]), src
                break
    if not dt_raw:
        tid = _TAG_BY_NAME.get("DateTime")
        if tid and tid in exif:
            dt_raw, dt_src = str(exif[tid]), "exif_filemodify"

    parsed_dt = None
    if dt_raw:
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed_dt = datetime.strptime(dt_raw.strip(), fmt)
                break
            except ValueError:
                continue
        report.timestamp = parsed_dt.isoformat() if parsed_dt else dt_raw
        report.timestamp_source = dt_src

    # GPS
    gps_ifd = exif.get_ifd(0x8825) if hasattr(exif, "get_ifd") else {}
    geo = _parse_gps(gps_ifd) if gps_ifd else None
    if geo:
        report.geo = {
            **asdict(geo),
            "hemisphere_ns": geo.hemisphere_ns,
            "hemisphere_ew": geo.hemisphere_ew,
            "climate_band": geo.climate_band(),
            "maps": geo.maps_links(),
        }
        # Temporal inference now that we know the hemisphere
        if parsed_dt:
            report.temporal = {
                "local_month": parsed_dt.month,
                "inferred_season": _season(parsed_dt.month, geo.hemisphere_ns),
                "hour": parsed_dt.hour,
                "daypart": _daypart(parsed_dt.hour),
            }
    else:
        report.notes.append("EXIF present but no GPS tags — geolocate from visual cues (IRIS)")
        if parsed_dt:
            report.temporal = {"local_month": parsed_dt.month, "hour": parsed_dt.hour,
                               "daypart": _daypart(parsed_dt.hour)}

    return report


def _daypart(hour: int) -> str:
    if 5 <= hour < 8:
        return "dawn"
    if 8 <= hour < 17:
        return "day"
    if 17 <= hour < 20:
        return "dusk"
    return "night"


def attach_visual_cues(report: GeoReport, cues: list[VisualCue]) -> GeoReport:
    """Merge vision-model inferences (signage, species, biome) into the report."""
    report.visual_cues.extend(asdict(c) for c in cues)
    return report


def reverse_geocode(geo: dict, *, online: bool = False, timeout: float = 6.0) -> dict | None:
    """
    Optional OSM/Nominatim reverse geocode. Off by default.
    Returns {display_name, country, ...} or None. Never raises.
    """
    if not online or not geo:
        return None
    try:
        import json
        import urllib.parse
        import urllib.request
        q = urllib.parse.urlencode({"lat": geo["lat"], "lon": geo["lon"],
                                    "format": "jsonv2", "zoom": "14"})
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/reverse?{q}",
            headers={"User-Agent": "samaritan-os-terra/1.0 (osint-geoint)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        return {
            "display_name": data.get("display_name"),
            "country": data.get("address", {}).get("country"),
            "country_code": data.get("address", {}).get("country_code"),
            "state": data.get("address", {}).get("state"),
            "city": data.get("address", {}).get("city")
                    or data.get("address", {}).get("town"),
        }
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Async connector surface (IRIS / TERRA agents, OPSEC-routed geocoding)
# --------------------------------------------------------------------------- #
class ExifConnector:
    """Async wrapper over the offline parser, with OPSEC-routed geocoding.

    GPS parsing is delegated to `extract()` so the sync and async paths can
    never drift apart. Reverse geocoding goes through the OPSEC layer, which
    is the only permitted egress path for this connector.
    """

    def __init__(self) -> None:
        self._client = OpsecClient()

    async def extract(self, file_path_or_bytes: str | bytes) -> dict[str, Any]:
        """Extract EXIF from a file path or raw bytes."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._extract_sync, file_path_or_bytes)

    def _extract_sync(self, source: str | bytes) -> dict[str, Any]:
        """Synchronous EXIF extraction — runs in an executor."""
        report = extract(source)
        return {
            "camera_make": report.camera.get("Make"),
            "camera_model": report.camera.get("Model"),
            "software": report.camera.get("Software"),
            "datetime": report.timestamp,
            "orientation": report.camera.get("Orientation"),
            "gps": (
                {"lat": report.geo["lat"], "lon": report.geo["lon"],
                 "altitude": report.geo.get("altitude_m"),
                 "maps": report.geo.get("maps"), "address": None}
                if report.geo else None
            ),
            "raw": dict(report.camera),
            "notes": report.notes,
        }

    @staticmethod
    def _gps_to_decimal(coords: Any, ref: str) -> float | None:
        """Convert EXIF GPS (degrees, minutes, seconds) to decimal."""
        return _dms_to_decimal(coords, ref)

    async def reverse_geocode(self, lat: float, lon: float) -> str | None:
        """Convert GPS coords to a human-readable address via Nominatim (through OPSEC)."""
        try:
            resp = await self._client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={
                    "format": "jsonv2",
                    "lat": str(lat),
                    "lon": str(lon),
                    "zoom": 18,
                    "addressdetails": 1,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("display_name")
        except Exception:
            return None

    @staticmethod
    def strip_exif(file_path: str, output_path: str | None = None) -> str:
        """Remove ALL EXIF from an image file. Returns output path."""
        img = Image.open(file_path)
        data = list(img.getdata())
        clean = Image.new(img.mode, img.size)
        clean.putdata(data)
        out = output_path or file_path
        clean.save(out)
        return out

    async def close(self) -> None:
        await self._client.close()


async def run_exif(file_path: str, **kwargs: Any) -> dict[str, Any]:
    """Top-level entry point for EXIF extraction + optional geocoding."""
    connector = ExifConnector()
    try:
        result = await connector.extract(file_path)
        gps = result.get("gps")
        if gps and gps.get("lat") is not None and gps.get("lon") is not None:
            gps["address"] = await connector.reverse_geocode(gps["lat"], gps["lon"])
        return result
    finally:
        await connector.close()
