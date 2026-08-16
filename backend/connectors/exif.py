"""
exif.py — EXIF Metadata Extraction Connector
==============================================
Extracts EXIF from images: GPS, camera, timestamps, software.
Reverse geocodes GPS coords through Tor. Strips EXIF from outbound images.
"""
from __future__ import annotations

import asyncio
import io
import os
from typing import Any

from opsec import OpsecClient


class ExifConnector:
    """Extract EXIF metadata, reverse geocode GPS, strip metadata."""

    def __init__(self) -> None:
        self._client = OpsecClient()

    async def extract(self, file_path_or_bytes: str | bytes) -> dict[str, Any]:
        """Extract EXIF from file path or raw bytes."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._extract_sync, file_path_or_bytes)

    def _extract_sync(self, source: str | bytes) -> dict[str, Any]:
        """Synchronous EXIF extraction — runs in executor."""
        result: dict[str, Any] = {
            "camera_make": None,
            "camera_model": None,
            "software": None,
            "datetime": None,
            "gps": None,
            "orientation": None,
            "flash": None,
            "iso": None,
            "exposure": None,
            "focal_length": None,
            "raw": {},
        }

        try:
            from PIL import Image
            from PIL.ExifTags import TAGS, GPSTAGS

            if isinstance(source, bytes):
                img = Image.open(io.BytesIO(source))
            else:
                img = Image.open(source)

            exif_data = img._getexif()
            if not exif_data:
                return result

            decoded: dict[str, Any] = {}
            for tag_id, value in exif_data.items():
                tag_name = TAGS.get(tag_id, str(tag_id))
                decoded[tag_name] = value

            result["camera_make"] = decoded.get("Make")
            result["camera_model"] = decoded.get("Model")
            result["software"] = decoded.get("Software")
            result["datetime"] = decoded.get("DateTimeOriginal") or decoded.get("DateTime")
            result["orientation"] = decoded.get("Orientation")
            result["flash"] = decoded.get("Flash")
            result["iso"] = decoded.get("ISOSpeedRatings")
            result["exposure"] = str(decoded.get("ExposureTime")) if decoded.get("ExposureTime") else None
            result["focal_length"] = str(decoded.get("FocalLength")) if decoded.get("FocalLength") else None

            # GPS extraction
            gps_info = decoded.get("GPSInfo")
            if gps_info:
                gps_decoded: dict[str, Any] = {}
                for gps_tag_id, gps_value in gps_info.items():
                    gps_tag_name = GPSTAGS.get(gps_tag_id, str(gps_tag_id))
                    gps_decoded[gps_tag_name] = gps_value

                lat = self._gps_to_decimal(
                    gps_decoded.get("GPSLatitude"),
                    gps_decoded.get("GPSLatitudeRef", "N"),
                )
                lon = self._gps_to_decimal(
                    gps_decoded.get("GPSLongitude"),
                    gps_decoded.get("GPSLongitudeRef", "E"),
                )
                if lat is not None and lon is not None:
                    result["gps"] = {"lat": lat, "lon": lon, "address": None}

            # Store safe raw subset (no binary blobs)
            safe_raw = {}
            for k, v in decoded.items():
                if isinstance(v, (str, int, float)):
                    safe_raw[k] = v
            result["raw"] = safe_raw

        except ImportError:
            result["error"] = "Pillow not installed — pip install Pillow"
        except Exception as e:
            result["error"] = str(e)

        return result

    @staticmethod
    def _gps_to_decimal(coords: Any, ref: str) -> float | None:
        """Convert EXIF GPS (degrees, minutes, seconds) to decimal."""
        if coords is None:
            return None
        try:
            # coords is a tuple of IFDRational or similar: ((d,1), (m,1), (s,100))
            d = float(coords[0])
            m = float(coords[1])
            s = float(coords[2])
            decimal = d + m / 60.0 + s / 3600.0
            if ref in ("S", "W"):
                decimal = -decimal
            return round(decimal, 7)
        except Exception:
            return None

    async def reverse_geocode(self, lat: float, lon: float) -> str | None:
        """Convert GPS coords to a human-readable address via Nominatim (through Tor)."""
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
        from PIL import Image
        img = Image.open(file_path)
        # Create a clean copy with no EXIF
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
        # Reverse geocode if GPS found
        gps = result.get("gps")
        if gps and gps.get("lat") is not None and gps.get("lon") is not None:
            address = await connector.reverse_geocode(gps["lat"], gps["lon"])
            result["gps"]["address"] = address
        return result
    finally:
        await connector.close()
