"""
stubs.py — Stub implementations for all remaining agents.
Each has a proper class with name/role/icon. Wire up real logic per agent.
"""
from __future__ import annotations
from dataclasses import asdict, is_dataclass
from .base import BaseAgent, AgentResult


def _dc(obj):
    """Serialize a dataclass to dict; pass through anything else."""
    return asdict(obj) if is_dataclass(obj) else obj


class CrawlerAgent(BaseAgent):
    name = "CRAWLER"; role = "Web Scraper"; icon = "⟨/⟩"
    description = "Structured data extraction, website parsing, browser automation (Playwright)"
    preferred_models = ["qwen2.5:7b"]; token_budget = 4096
    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        self.log("browser automation stub — connect Playwright")
        return AgentResult(agent=self.name, status="stub", output={"note": "TODO: Playwright integration"},
                           latency_s=self._elapsed(t0))


class PrismAgent(BaseAgent):
    name = "PRISM"; role = "Social Intelligence"; icon = "◈"
    description = "Cross-platform identity resolution, account clustering, bio comparison"
    preferred_models = ["gemma2:9b"]; token_budget = 8192

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        username = input_data if isinstance(input_data, str) else input_data.get("query", "")
        try:
            from connectors.sherlock import run_sherlock
            self.log(f"Sherlock username scan: {username}")
            data = await run_sherlock(username)
            found = data.get("count", 0)
            self.log(f"found {found} accounts across platforms")
            return AgentResult(agent=self.name, status="done", output=data,
                               confidence=0.9 if found else 0.4,
                               reasoning=f"Sherlock found {found} claimed accounts for '{username}'.",
                               latency_s=self._elapsed(t0))
        except FileNotFoundError:
            self.log("sherlock not installed — run: pip install sherlock-project")
        except Exception as e:
            self.log(f"Sherlock error: {e}")

        return AgentResult(agent=self.name, status="partial",
                           output={"note": "Install sherlock-project to enable username scanning"},
                           latency_s=self._elapsed(t0))


class IrisAgent(BaseAgent):
    name = "IRIS"; role = "Vision Intelligence"; icon = "◉"
    description = "Signage/OCR, scene cues, species & region inference for visual geolocation"
    preferred_models = ["Florence-2", "gemma2:9b"]; token_budget = 8192

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        image = input_data
        if isinstance(input_data, dict):
            image = input_data.get("image") or input_data.get("path") or input_data.get("query")
        if not image:
            return AgentResult(agent=self.name, status="error", output={},
                               error="IRIS expects an image path or bytes",
                               latency_s=self._elapsed(t0))

        from connectors import vision

        self.log("OCR signage")
        ocr = vision.ocr_image(image)
        self.log("scene cues (OpenCV)")
        scene = vision.scene_cues(image)
        self.log("visual region embedding (optional backend)")
        geoclip = vision.geoclip_estimate(image)

        # Build typed visual cues for TERRA's correlation scaffold
        cues = []
        if ocr.text and ocr.candidate_regions:
            cues.append({"kind": "signage", "value": "; ".join(ocr.candidate_regions),
                         "confidence": 0.55,
                         "evidence": f"OCR scripts {ocr.scripts} → langs {ocr.candidate_languages}"})
        if scene.biome_hint:
            cues.append({"kind": "biome", "value": scene.biome_hint, "confidence": 0.25,
                         "evidence": f"greenery_ratio={scene.greenery_ratio}, brightness={scene.brightness}"})

        # Confidence: signage with a script lock is the strongest single visual cue.
        if ocr.candidate_regions:
            conf = 0.55
            reasoning = (f"Signage OCR ({ocr.scripts}) narrows to {ocr.candidate_regions}. "
                         "Region-level only — not an address.")
        elif scene.biome_hint:
            conf = 0.25
            reasoning = f"No legible signage; scene cues suggest {scene.biome_hint}."
        else:
            conf = 0.1
            reasoning = "No legible signage or strong scene cues from a single frame."
        self.log(reasoning)

        return AgentResult(
            agent=self.name, status="done",
            output={"ocr": _dc(ocr), "scene": _dc(scene), "geoclip": geoclip,
                    "visual_cues": cues, "face_matching": "disabled_by_policy"},
            confidence=conf, reasoning=reasoning, signals=cues,
            latency_s=self._elapsed(t0),
        )


class EchoAgent(BaseAgent):
    name = "ECHO"; role = "Audio Intelligence"; icon = "~"
    description = "Transcription (Whisper), accent detection, background environment analysis (YAMNet)"
    preferred_models = ["whisper"]; token_budget = 4096
    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        self.log("audio stub — connect WhisperX/YAMNet")
        return AgentResult(agent=self.name, status="stub", output={"note": "TODO: WhisperX transcription"},
                           latency_s=self._elapsed(t0))


class TerraAgent(BaseAgent):
    name = "TERRA"; role = "GEOINT"; icon = "⊛"
    description = "Geolocation from images, environmental inference, architecture/vegetation analysis"
    preferred_models = ["Florence-2", "gemma2:9b"]; token_budget = 6144

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        # Accept a path/bytes directly, or a dict {"image": ...}
        image = input_data
        if isinstance(input_data, dict):
            image = input_data.get("image") or input_data.get("path") or input_data.get("query")
        if not image:
            return AgentResult(agent=self.name, status="error", output={},
                               error="TERRA expects an image path or bytes",
                               latency_s=self._elapsed(t0))

        from connectors import exif as exifmod

        self.log("extracting EXIF + GPS metadata")
        report = exifmod.extract(image)

        geo = report.geo
        online = bool((context or {}).get("online"))
        if geo and online:
            self.log("reverse-geocoding GPS via OSM/Nominatim")
            place = exifmod.reverse_geocode(geo, online=True)
            if place:
                geo["place"] = place

        # Confidence + reasoning: hard GPS is high-confidence; visual-cue-only is low.
        if geo:
            conf = 0.95
            place = (geo.get("place") or {}).get("display_name")
            where = place or f"{geo['lat']}, {geo['lon']}"
            self.log(f"GPS lock: {where} ({geo['climate_band']}, {geo['hemisphere_ns']} hemisphere)")
            reasoning = (f"EXIF GPS present → {where}. "
                         f"{geo['climate_band'].capitalize()} climate band, "
                         f"{geo['hemisphere_ns']} hemisphere.")
            if report.temporal.get("inferred_season"):
                reasoning += f" Shot in {report.temporal['inferred_season']} ({report.temporal.get('daypart','')})."
        elif report.has_exif:
            conf = 0.35
            reasoning = ("EXIF present but no GPS — hand to IRIS for visual geolocation "
                         "(signage/OCR, vegetation, architecture, biome).")
            self.log(reasoning)
        else:
            conf = 0.1
            reasoning = "No EXIF/GPS (likely social-media-stripped) — visual cues only."
            self.log(reasoning)

        entities = []
        if geo:
            entities.append({"type": "location", "lat": geo["lat"], "lon": geo["lon"],
                             "label": (geo.get("place") or {}).get("display_name"),
                             "maps": geo["maps"]})

        return AgentResult(
            agent=self.name, status="done", output=report.to_dict(),
            confidence=conf, reasoning=reasoning, entities_found=entities,
            latency_s=self._elapsed(t0),
        )


class InkAgent(BaseAgent):
    name = "INK"; role = "Stylometry"; icon = "✦"
    description = "Writing fingerprinting, authorship analysis, slang/dialect detection, ideology clustering"
    preferred_models = ["qwen2.5:7b"]; token_budget = 8192
    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        self.log("stylometry stub — transformer embedding pipeline")
        return AgentResult(agent=self.name, status="stub", output={"note": "TODO: Writeprints/JStylo bridge"},
                           latency_s=self._elapsed(t0))


class NexusAgent(BaseAgent):
    name = "NEXUS"; role = "Correlation Engine"; icon = "∞"
    description = "Hidden relationship detection, graph clustering, influence scoring, semantic linking"
    preferred_models = ["gemma2:9b"]; token_budget = 12288
    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        self.log("correlation stub — Neo4j graph analysis")
        return AgentResult(agent=self.name, status="stub", output={"note": "TODO: Neo4j entity linking"},
                           latency_s=self._elapsed(t0))


class KronosAgent(BaseAgent):
    name = "KRONOS"; role = "Timeline Reconstruction"; icon = "⊕"
    description = "Chronology reconstruction: username changes, post history, account creation, location changes"
    preferred_models = ["gemma2:9b"]; token_budget = 6144
    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        self.log("timeline stub — vis.js event chain")
        return AgentResult(agent=self.name, status="stub", output={"events": [], "note": "TODO: event pipeline"},
                           latency_s=self._elapsed(t0))


class VaultAgent(BaseAgent):
    name = "VAULT"; role = "Memory Agent"; icon = "□"
    description = "Persistent entity memory, semantic summarization, intelligence profile management"
    preferred_models = ["gemma2:9b"]; token_budget = 8192
    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        self.log("memory stub — Qdrant vector store")
        return AgentResult(agent=self.name, status="stub", output={"note": "TODO: Qdrant semantic search"},
                           latency_s=self._elapsed(t0))


class SentinelAgent(BaseAgent):
    name = "SENTINEL"; role = "Live Monitoring"; icon = "⊲"
    description = "Real-time tracking: new posts, bio changes, username changes, new domains, leaks"
    preferred_models = ["phi3:mini"]; token_budget = 2048
    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        self.log("monitoring stub — Redis pub/sub event bus")
        return AgentResult(agent=self.name, status="stub", output={"monitors": [], "note": "TODO: Redis event bus"},
                           latency_s=self._elapsed(t0))


class QuillAgent(BaseAgent):
    name = "QUILL"; role = "Report Generation"; icon = "⟁"
    description = "AI-generated intelligence summaries, PDF reports, evidence bundles, graph exports"
    preferred_models = ["gemma2:9b", "claude-opus-4"]; token_budget = 16384
    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        self.log("report stub — PDF generation pipeline")
        return AgentResult(agent=self.name, status="stub", output={"note": "TODO: ReportLab/WeasyPrint"},
                           latency_s=self._elapsed(t0))
