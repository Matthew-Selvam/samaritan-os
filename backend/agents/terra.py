"""
Terra.py — TERRA Agent
======================
"""
from __future__ import annotations

import asyncio
from .base import BaseAgent, AgentResult

class TerraAgent(BaseAgent):
    """GEOINT — geolocation from image metadata.

    Pulls GPS out of EXIF (its own extraction, so it does not depend on IRIS
    running first) and turns coordinates into map links, hemisphere, and a
    reverse-geocode hint. Environmental/vegetation inference (PlantNet/BioCLIP)
    can layer on later; this path is dependency-light and works today.
    """
    name = "TERRA"; role = "GEOINT"; icon = "⊛"
    description = "Geolocation from images, environmental inference, architecture/vegetation analysis"
    preferred_models = ["Florence-2", "gemma2:9b"]; token_budget = 6144

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        file_path = context.get("file_path", target)
        signals: list[dict] = []
        entities: list[dict] = []
        coords = None

        # Own EXIF extraction (independent of IRIS).
        try:
            from connectors.exif import run_exif
            self.log("extracting GPS from EXIF")
            exif = await asyncio.wait_for(run_exif(file_path), timeout=10.0)
            if exif.get("gps"):
                coords = (exif["gps"].get("lat"), exif["gps"].get("lon"))
        except Exception as e:
            self.log(f"EXIF error: {e}")

        # Fall back to any GPS signal collected upstream.
        if not coords:
            for s in context.get("peer_signals", []) or []:
                if s.get("type") == "gps" and s.get("lat") is not None:
                    coords = (s.get("lat"), s.get("lon"))
                    break

        if coords and coords[0] is not None:
            lat, lon = coords
            hemi_ns = "N" if lat >= 0 else "S"
            hemi_ew = "E" if lon >= 0 else "W"
            osm = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=16/{lat}/{lon}"
            gmaps = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
            self.log(f"geolocated: {lat}, {lon} ({hemi_ns}/{hemi_ew})")
            signals.append({"type": "geoint", "lat": lat, "lon": lon,
                            "hemisphere": f"{hemi_ns}{hemi_ew}",
                            "osm_url": osm, "gmaps_url": gmaps, "source": "terra"})
            entities.append({"id": f"loc_{round(lat, 4)}_{round(lon, 4)}",
                             "label": f"{lat:.4f}, {lon:.4f}", "type": "location"})
            confidence = 0.8
            reasoning = f"Geolocated to {lat:.4f}, {lon:.4f} ({hemi_ns}{hemi_ew})."
        else:
            self.log("no geolocation recoverable (no GPS EXIF)")
            confidence = 0.1
            reasoning = "No GPS metadata present; visual geolocation needs a vision model."

        return AgentResult(
            agent=self.name, status="done",
            output={"coordinates": coords, "signals": signals,
                    "note": None if coords else "no GPS EXIF; add PlantNet/Mapillary for visual geoint"},
            confidence=confidence, reasoning=reasoning,
            signals=signals, entities_found=entities,
            latency_s=self._elapsed(t0),
        )
