"""
Iris.py — IRIS Agent
====================
"""
from __future__ import annotations

import asyncio
from .base import BaseAgent, AgentResult

class IrisAgent(BaseAgent):
    """Vision Intelligence — face detection, EXIF, reverse image search.

    The deanonymization photo pipeline. Upload a face → find them everywhere.
    """
    name = "IRIS"; role = "Vision Intelligence"; icon = "◉"
    description = "Face detection, EXIF extraction, reverse image search, face embedding cross-match"
    preferred_models = ["Florence-2", "gemma2:9b"]; token_budget = 8192

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        file_path = context.get("file_path", target)
        signals: list[dict] = []
        entities: list[dict] = []

        # ── EXIF extraction ───────────────────────────────────────────────
        exif_data = {}
        try:
            from connectors.exif import run_exif
            self.log(f"extracting EXIF metadata")
            exif_data = await asyncio.wait_for(run_exif(file_path), timeout=10.0)
            if exif_data.get("gps"):
                gps = exif_data["gps"]
                self.log(f"GPS: {gps.get('lat')}, {gps.get('lon')} — {gps.get('address', 'no address')}")
                signals.append({"type": "gps", "lat": gps["lat"], "lon": gps["lon"],
                                "address": gps.get("address"), "source": "exif"})
            if exif_data.get("camera_make"):
                self.log(f"camera: {exif_data['camera_make']} {exif_data.get('camera_model', '')}")
                signals.append({"type": "camera", "make": exif_data["camera_make"],
                                "model": exif_data.get("camera_model"), "source": "exif"})
            if exif_data.get("datetime"):
                self.log(f"datetime: {exif_data['datetime']}")
        except Exception as e:
            self.log(f"EXIF extraction error: {e}")

        # ── Face detection + embedding ────────────────────────────────────
        face_data: dict = {}
        try:
            from connectors.face_embed import run_face_embed
            self.log("running face detection + embedding")
            face_data = await asyncio.wait_for(
                run_face_embed(file_path, investigation_id=context.get("case_id")),
                timeout=30.0,
            )
            faces = face_data.get("faces_detected", 0)
            matches = len(face_data.get("matches", []))
            self.log(f"detected {faces} face(s), {matches} cross-match(es) in vector DB")
            for m in face_data.get("matches", []):
                signals.append({"type": "face_match", "score": m.get("score"),
                                "metadata": m.get("metadata"), "source": "qdrant"})
                entities.append({
                    "id": f"face_{m.get('id', 'unknown')}",
                    "label": m.get("metadata", {}).get("person_name", "Unknown face"),
                    "type": "person",
                })
        except Exception as e:
            self.log(f"Face detection error: {e}")

        # ── Reverse image search ──────────────────────────────────────────
        reverse_data: dict = {}
        try:
            from connectors.reverse_image import run_reverse_image
            self.log("running reverse image search (Google/Yandex/TinEye through Tor)")
            reverse_data = await asyncio.wait_for(
                run_reverse_image(file_path, is_file=True), timeout=45.0,
            )
            count = reverse_data.get("count", 0)
            self.log(f"reverse image: {count} result(s) across engines")
            for r in reverse_data.get("results", [])[:20]:
                signals.append({"type": "image_match", "url": r.get("url"),
                                "title": r.get("title"), "engine": r.get("source_engine"),
                                "similarity": r.get("similarity"), "source": "reverse_image"})
        except Exception as e:
            self.log(f"Reverse image search error: {e}")

        has_live = bool(face_data.get("faces_detected") or reverse_data.get("count") or exif_data.get("gps"))
        return AgentResult(
            agent=self.name, status="done",
            output={
                "exif": exif_data,
                "faces": face_data,
                "reverse_image": reverse_data,
            },
            confidence=0.8 if has_live else 0.2,
            reasoning=f"Vision pipeline: EXIF={'yes' if exif_data.get('camera_make') else 'no'}, "
                      f"faces={face_data.get('faces_detected', 0)}, "
                      f"reverse={reverse_data.get('count', 0)} results",
            signals=signals,
            entities_found=entities,
            latency_s=self._elapsed(t0),
        )
