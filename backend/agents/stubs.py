"""
stubs.py — Agent implementations (upgraded + stubs)
=====================================================
IRIS, PRISM, INK are real implementations now.
Remaining agents are wired stubs ready for activation.
"""
from __future__ import annotations

import asyncio
from .base import BaseAgent, AgentResult


class CrawlerAgent(BaseAgent):
    """Web Scraper — static fetch + structured extraction.

    Fetches a URL/domain over the OPSEC layer (falls back to plain httpx) and
    extracts title, outbound links, emails, and phone numbers from the HTML. This
    is dependency-light static scraping; a Playwright browser-automation path can
    layer on later for JS-rendered targets without changing this contract.
    """
    name = "CRAWLER"; role = "Web Scraper"; icon = "⟨/⟩"
    description = "Structured data extraction, website parsing, browser automation (Playwright)"
    preferred_models = ["qwen2.5:7b"]; token_budget = 4096

    async def run(self, input_data, context=None):
        import re
        t0 = self._start_timer()
        target = input_data if isinstance(input_data, str) else str(input_data)
        url = target if target.startswith(("http://", "https://")) else f"https://{target}"
        signals: list[dict] = []
        entities: list[dict] = []

        html = ""
        status_code = None
        try:
            try:
                from opsec import OpsecClient
                client = OpsecClient(timeout=20.0)
                resp = await client.get(url)
            except Exception:
                import httpx
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
                    resp = await c.get(url, headers={"User-Agent": "Signal-OS-Crawler"})
            status_code = resp.status_code
            html = resp.text or ""
            self.log(f"fetched {url} — HTTP {status_code}, {len(html)} bytes")
        except Exception as e:
            self.log(f"fetch failed: {e}")
            return AgentResult(agent=self.name, status="error", output={"url": url},
                               error=str(e), latency_s=self._elapsed(t0))

        title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = title_m.group(1).strip()[:200] if title_m else None
        links = sorted(set(re.findall(r'href=["\'](https?://[^"\'>\s]+)', html, re.I)))[:100]
        emails = sorted(set(re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", html)))
        phones = sorted(set(re.findall(r"\+?\d[\d\s().\-]{7,17}\d", html)))[:20]

        self.log(f"extracted: title={'yes' if title else 'no'}, {len(links)} links, "
                 f"{len(emails)} emails, {len(phones)} phones")
        for e in emails[:30]:
            signals.append({"type": "email_found", "value": e, "source": "crawler"})
            entities.append({"id": f"email_{e.lower()}", "label": e, "type": "email"})
        for p in phones:
            signals.append({"type": "phone_found", "value": p, "source": "crawler"})
        if links:
            signals.append({"type": "outbound_links", "count": len(links),
                            "sample": links[:10], "source": "crawler"})

        return AgentResult(
            agent=self.name, status="done",
            output={"url": url, "http_status": status_code, "title": title,
                    "links": links, "emails": emails, "phones": phones},
            confidence=0.7 if (status_code == 200 and (emails or links)) else 0.3,
            reasoning=f"Scraped {url}: {len(links)} links, {len(emails)} emails, "
                      f"{len(phones)} phone(s).",
            signals=signals, entities_found=entities,
            latency_s=self._elapsed(t0),
        )


class PrismAgent(BaseAgent):
    """Social Intelligence — full identity resolution pipeline.

    Sherlock username scan + people search + breach check + profile photo harvesting.
    """
    name = "PRISM"; role = "Social Intelligence"; icon = "◈"
    description = "Cross-platform identity resolution, account clustering, breach correlation"
    preferred_models = ["gemma2:9b"]; token_budget = 8192

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        input_type = context.get("input_type", "username")
        signals: list[dict] = []
        entities: list[dict] = []

        # ── Sherlock username scan ────────────────────────────────────────
        sherlock_data = {}
        try:
            from connectors.sherlock import run_sherlock
            self.log(f"Sherlock username scan: {target}")
            sherlock_data = await asyncio.wait_for(run_sherlock(target), timeout=30.0)
            found = sherlock_data.get("count", 0)
            self.log(f"found {found} accounts across platforms")
            for r in sherlock_data.get("results", []):
                signals.append({
                    "type": "account", "platform": r.get("site"),
                    "url": r.get("url"), "source": "sherlock",
                })
                entities.append({
                    "id": f"account_{r.get('site', '').lower()}_{target}",
                    "label": f"{target}@{r.get('site')}",
                    "type": "username",
                })
        except FileNotFoundError:
            self.log("sherlock not installed — run: pip install sherlock-project")
        except asyncio.TimeoutError:
            self.log("Sherlock timed out after 30s")
        except Exception as e:
            self.log(f"Sherlock error: {e}")

        # ── People search (for name inputs) ───────────────────────────────
        people_data = {}
        if input_type == "person_name":
            try:
                from connectors.people_search import run_people_search
                self.log(f"people search: {target}")
                people_data = await asyncio.wait_for(
                    run_people_search(target, location=context.get("location")),
                    timeout=25.0,
                )
                count = people_data.get("count", 0)
                self.log(f"found {count} people records")
                for r in people_data.get("results", []):
                    signals.append({
                        "type": "person_record", "source": r.get("source"),
                        "full_name": r.get("full_name"), "age": r.get("age"),
                    })
                    entities.append({
                        "id": f"person_{r.get('full_name', '').replace(' ', '_').lower()}",
                        "label": r.get("full_name", target),
                        "type": "person",
                    })
            except Exception as e:
                self.log(f"People search error: {e}")

        # ── Breach check (for emails) ─────────────────────────────────────
        breach_data = {}
        if input_type in ("email", "username"):
            try:
                from connectors.breach_check import run_breach_check
                self.log(f"breach check: {target}")
                breach_data = await asyncio.wait_for(
                    run_breach_check(target), timeout=15.0,
                )
                breaches = breach_data.get("total_breaches", 0)
                self.log(f"found {breaches} breach(es)")
                for b in breach_data.get("breaches", []):
                    signals.append({
                        "type": "breach", "name": b.get("name"),
                        "date": b.get("date"), "source": "hibp",
                    })
            except Exception as e:
                self.log(f"Breach check error: {e}")

        has_live = bool(sherlock_data.get("count") or people_data.get("count") or breach_data.get("total_breaches"))
        return AgentResult(
            agent=self.name, status="done",
            output={
                "sherlock": sherlock_data,
                "people_search": people_data,
                "breach_check": breach_data,
                "input_type": input_type,
            },
            confidence=0.85 if has_live else 0.3,
            reasoning=f"Identity resolution: Sherlock={sherlock_data.get('count', 0)} accounts, "
                      f"people={people_data.get('count', 0)}, breaches={breach_data.get('total_breaches', 0)}",
            signals=signals,
            entities_found=entities,
            latency_s=self._elapsed(t0),
        )


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


class EchoAgent(BaseAgent):
    """Audio Intelligence — file metadata now, transcription when Whisper is present.

    Extracts container metadata (duration/channels/sample-rate for WAV, size/format
    otherwise) with no dependencies. If ``faster_whisper`` is installed it also
    transcribes; otherwise it reports a clear, honest note rather than pretending.
    """
    name = "ECHO"; role = "Audio Intelligence"; icon = "~"
    description = "Transcription (Whisper), accent detection, background environment analysis (YAMNet)"
    preferred_models = ["whisper"]; token_budget = 4096

    async def run(self, input_data, context=None):
        import os
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        path = context.get("file_path", target)
        signals: list[dict] = []
        meta: dict = {}

        if not os.path.isfile(path):
            self.log(f"no audio file at {path}")
            return AgentResult(agent=self.name, status="error", output={"path": path},
                               error="file not found", latency_s=self._elapsed(t0))

        meta["size_bytes"] = os.path.getsize(path)
        meta["format"] = os.path.splitext(path)[1].lstrip(".").lower()
        if meta["format"] == "wav":
            try:
                import wave
                with wave.open(path, "rb") as w:
                    frames, rate = w.getnframes(), w.getframerate()
                    meta.update({"channels": w.getnchannels(), "sample_rate": rate,
                                 "duration_s": round(frames / rate, 2) if rate else None})
                self.log(f"WAV: {meta['duration_s']}s, {meta['channels']}ch, {meta['sample_rate']}Hz")
            except Exception as e:
                self.log(f"WAV parse error: {e}")
        signals.append({"type": "audio_meta", **meta, "source": "echo"})

        transcript = None
        try:
            from faster_whisper import WhisperModel  # optional heavy dep
            self.log("transcribing with faster-whisper…")
            model = WhisperModel(os.getenv("WHISPER_MODEL", "base"), device="cpu",
                                 compute_type="int8")
            segments, info = model.transcribe(path)
            transcript = " ".join(s.text for s in segments).strip()
            self.log(f"transcribed {len(transcript)} chars (lang={info.language})")
            signals.append({"type": "transcript", "text": transcript[:500],
                            "language": info.language, "source": "faster_whisper"})
            status = "done"
        except ImportError:
            self.log("transcription skipped — install faster-whisper to enable")
            status = "partial"

        return AgentResult(
            agent=self.name, status=status,
            output={"metadata": meta, "transcript": transcript,
                    "note": None if transcript else "install faster-whisper for transcription"},
            confidence=0.7 if transcript else 0.3,
            reasoning=f"Audio {meta.get('format')}, {meta.get('duration_s', '?')}s"
                      + (f"; transcribed {len(transcript)} chars." if transcript else "; metadata only."),
            signals=signals, latency_s=self._elapsed(t0),
        )


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


class InkAgent(BaseAgent):
    """Stylometry — basic bio fingerprinting across platforms.

    Analyzes writing patterns, slang, vocabulary to link accounts.
    """
    name = "INK"; role = "Stylometry"; icon = "✦"
    description = "Writing fingerprinting, authorship analysis, cross-platform bio matching"
    preferred_models = ["qwen2.5:7b"]; token_budget = 8192

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        target = input_data if isinstance(input_data, str) else str(input_data)

        self.log("analyzing text patterns for stylometric fingerprinting")

        # Basic text analysis — word frequency, avg word length, punctuation patterns
        words = target.split()
        word_count = len(words)
        avg_word_len = sum(len(w) for w in words) / max(word_count, 1)
        unique_ratio = len(set(w.lower() for w in words)) / max(word_count, 1)

        # Character-level features
        char_count = len(target)
        upper_ratio = sum(1 for c in target if c.isupper()) / max(char_count, 1)
        punct_count = sum(1 for c in target if c in "!?.,;:—–-")
        emoji_like = sum(1 for c in target if ord(c) > 0x1F600)

        profile = {
            "word_count": word_count,
            "avg_word_length": round(avg_word_len, 2),
            "vocabulary_richness": round(unique_ratio, 3),
            "uppercase_ratio": round(upper_ratio, 3),
            "punctuation_density": round(punct_count / max(word_count, 1), 3),
            "emoji_count": emoji_like,
            "char_count": char_count,
        }

        self.log(f"stylometric profile: {word_count} words, vocab richness={unique_ratio:.2%}")

        signals = [{"type": "stylometry", "profile": profile, "source": "ink_analysis"}]
        return AgentResult(
            agent=self.name, status="done",
            output={"profile": profile, "text_preview": target[:200]},
            confidence=0.5 if word_count > 20 else 0.2,
            reasoning=f"Stylometric analysis: {word_count} words, richness={unique_ratio:.2%}",
            signals=signals,
            latency_s=self._elapsed(t0),
        )


class NexusAgent(BaseAgent):
    """Correlation Engine — in-memory entity graph + relationship inference.

    Consumes the aggregated signals/entities of the primary swarm (injected by
    APEX as ``context['peer_signals']`` / ``context['peer_entities']``) and builds
    a de-duplicated knowledge graph: nodes, weighted edges, connected-component
    clusters, and degree-centrality influence scoring. Pure Python — no Neo4j
    required (a Neo4j sink can be layered on later without touching this logic).
    """
    name = "NEXUS"; role = "Correlation Engine"; icon = "∞"
    description = "Hidden relationship detection, graph clustering, influence scoring, semantic linking"
    preferred_models = ["gemma2:9b"]; token_budget = 12288

    @staticmethod
    def _slug(text: str) -> str:
        return "".join(c if c.isalnum() else "_" for c in str(text).lower()).strip("_") or "unknown"

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        peer_signals: list[dict] = context.get("peer_signals", []) or []
        peer_entities: list[dict] = context.get("peer_entities", []) or []

        self.log(f"correlating {len(peer_entities)} entities + {len(peer_signals)} signals")

        # ── Nodes: root target + de-duplicated peer entities ──────────────────
        root_id = f"target_{self._slug(target)}"
        nodes: dict[str, dict] = {
            root_id: {"id": root_id, "label": target[:60], "type": "target", "degree": 0}
        }
        for e in peer_entities:
            eid = e.get("id") or f"entity_{self._slug(e.get('label', ''))}"
            if eid not in nodes:
                nodes[eid] = {"id": eid, "label": e.get("label", eid),
                              "type": e.get("type", "entity"), "degree": 0}

        # ── Edges: connect every discovered entity back to the root target ────
        edges: list[dict] = []
        seen_edges: set[tuple] = set()

        def add_edge(a: str, b: str, relation: str, weight: float = 1.0, source: str = "nexus"):
            if a == b or a not in nodes or b not in nodes:
                return
            key = (a, b, relation)
            if key in seen_edges:
                return
            seen_edges.add(key)
            edges.append({"source": a, "target": b, "relation": relation,
                          "weight": weight, "provenance": source})
            nodes[a]["degree"] += 1
            nodes[b]["degree"] += 1

        for eid, node in list(nodes.items()):
            if eid == root_id:
                continue
            relation = {
                "username": "has_account", "person": "resolves_to",
                "location": "located_at",
            }.get(node["type"], "linked_to")
            add_edge(root_id, eid, relation)

        # ── Relationship inference from signals (co-location, breach, matches) ─
        gps_signals = [s for s in peer_signals if s.get("type") == "gps"]
        for i, g in enumerate(gps_signals):
            loc_id = f"loc_{round(g.get('lat', 0), 4)}_{round(g.get('lon', 0), 4)}"
            nodes.setdefault(loc_id, {"id": loc_id,
                                      "label": g.get("address") or f"{g.get('lat')}, {g.get('lon')}",
                                      "type": "location", "degree": 0})
            add_edge(root_id, loc_id, "located_at", weight=1.5, source="exif_gps")

        breach_count = sum(1 for s in peer_signals if s.get("type") == "breach")
        match_count = sum(1 for s in peer_signals if s.get("type") in ("face_match", "image_match"))
        account_count = sum(1 for s in peer_signals if s.get("type") == "account")

        # ── Connected-component clustering ────────────────────────────────────
        adjacency: dict[str, set[str]] = {n: set() for n in nodes}
        for e in edges:
            adjacency[e["source"]].add(e["target"])
            adjacency[e["target"]].add(e["source"])
        clusters: list[list[str]] = []
        unvisited = set(nodes)
        while unvisited:
            start = unvisited.pop()
            stack, comp = [start], [start]
            while stack:
                cur = stack.pop()
                for nb in adjacency[cur]:
                    if nb in unvisited:
                        unvisited.remove(nb)
                        stack.append(nb)
                        comp.append(nb)
            clusters.append(comp)

        # ── Influence scoring: normalized degree centrality ───────────────────
        max_deg = max((n["degree"] for n in nodes.values()), default=0) or 1
        ranked = sorted(nodes.values(), key=lambda n: n["degree"], reverse=True)
        for n in nodes.values():
            n["influence"] = round(n["degree"] / max_deg, 3)

        self.log(f"graph: {len(nodes)} nodes, {len(edges)} edges, "
                 f"{len(clusters)} cluster(s), top node '{ranked[0]['label']}'")

        signals = [{
            "type": "correlation_summary",
            "nodes": len(nodes), "edges": len(edges), "clusters": len(clusters),
            "accounts": account_count, "breaches": breach_count, "media_matches": match_count,
            "source": "nexus",
        }]
        density = len(edges) / max(len(nodes), 1)
        confidence = min(0.95, 0.3 + 0.1 * len(edges)) if edges else 0.1
        return AgentResult(
            agent=self.name, status="done",
            output={
                "nodes": list(nodes.values()),
                "edges": edges,
                "clusters": [{"size": len(c), "members": c} for c in clusters],
                "top_entities": [{"label": n["label"], "type": n["type"],
                                  "degree": n["degree"], "influence": n["influence"]}
                                 for n in ranked[:5]],
                "graph_density": round(density, 3),
            },
            confidence=round(confidence, 3),
            reasoning=f"Built graph of {len(nodes)} nodes / {len(edges)} edges across "
                      f"{len(clusters)} cluster(s); {account_count} accounts, "
                      f"{breach_count} breaches, {match_count} media matches correlated.",
            signals=signals,
            latency_s=self._elapsed(t0),
        )


class KronosAgent(BaseAgent):
    """Timeline Reconstruction — chronology from timestamped signals.

    Extracts every dateable signal produced upstream (EXIF capture time, breach
    dates, account-creation dates, explicit event timestamps) via a tolerant
    multi-format parser, then orders them into a single event chain suitable for
    a vis.js timeline. No external date library required.
    """
    name = "KRONOS"; role = "Timeline Reconstruction"; icon = "⊕"
    description = "Chronology reconstruction: username changes, post history, account creation, location changes"
    preferred_models = ["gemma2:9b"]; token_budget = 6144

    _FORMATS = (
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y",
        "%B %d, %Y", "%m/%d/%Y", "%Y",
    )

    @classmethod
    def _parse_date(cls, value):
        from datetime import datetime
        if not value:
            return None
        s = str(value).strip().replace("Z", "")
        for fmt in cls._FORMATS:
            try:
                return datetime.strptime(s[:len(datetime.now().strftime(fmt)) + 4], fmt)
            except (ValueError, TypeError):
                continue
        # last resort: leading ISO date
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        context = context or {}
        peer_signals: list[dict] = context.get("peer_signals", []) or []
        self.log(f"reconstructing chronology from {len(peer_signals)} signals")

        events: list[dict] = []
        for s in peer_signals:
            stype = s.get("type")
            raw_date = None
            label = None
            if stype == "camera" or stype == "gps":
                raw_date = s.get("datetime")
            if stype == "breach":
                raw_date, label = s.get("date"), f"Data breach: {s.get('name', 'unknown')}"
            elif s.get("date"):
                raw_date = s.get("date")
            elif s.get("datetime"):
                raw_date = s.get("datetime")
            elif s.get("created_at"):
                raw_date = s.get("created_at")

            dt = self._parse_date(raw_date)
            if dt is None:
                continue
            if label is None:
                label = {
                    "account": f"Account seen: {s.get('platform', '?')}",
                    "person_record": f"Record: {s.get('full_name', '?')}",
                }.get(stype, f"{stype} event")
            events.append({
                "timestamp": dt.isoformat(),
                "year": dt.year,
                "label": label,
                "type": stype,
                "source": s.get("source", "unknown"),
            })

        events.sort(key=lambda e: e["timestamp"])
        span = None
        if len(events) >= 2:
            span = {"earliest": events[0]["timestamp"], "latest": events[-1]["timestamp"],
                    "years": events[-1]["year"] - events[0]["year"]}
            self.log(f"timeline spans {span['earliest'][:10]} → {span['latest'][:10]} "
                     f"({len(events)} events)")
        else:
            self.log(f"reconstructed {len(events)} dated event(s)")

        signals = [{"type": "timeline", "event_count": len(events),
                    "span": span, "source": "kronos"}]
        return AgentResult(
            agent=self.name, status="done",
            output={"events": events, "span": span, "event_count": len(events)},
            confidence=0.75 if len(events) >= 2 else (0.3 if events else 0.1),
            reasoning=f"Reconstructed {len(events)} dated event(s)"
                      + (f" spanning {span['years']} year(s)." if span else "."),
            signals=signals,
            latency_s=self._elapsed(t0),
        )


class VaultAgent(BaseAgent):
    """Memory — persistent entity store across investigations.

    File-backed JSON memory keyed by case (a Qdrant/Postgres backend can replace
    the store without changing the agent). On each run it loads what's already
    known for the case, merges the entities the swarm just produced, flags which
    are new vs. previously-seen, and persists the union. Runs in the correlation
    tier so it sees the full aggregated entity set.
    """
    name = "VAULT"; role = "Memory Agent"; icon = "□"
    description = "Persistent entity memory, semantic summarization, intelligence profile management"
    preferred_models = ["gemma2:9b"]; token_budget = 8192

    @staticmethod
    def _store_dir() -> str:
        import os
        d = os.getenv("VAULT_DIR",
                      os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "vault_store"))
        os.makedirs(d, exist_ok=True)
        return d

    async def run(self, input_data, context=None):
        import os, json
        from datetime import datetime, timezone
        t0 = self._start_timer()
        context = context or {}
        case_id = context.get("case_id", "default")
        peer_entities = context.get("peer_entities", []) or []
        path = os.path.join(self._store_dir(), f"{case_id}.json")

        memory = {"case_id": case_id, "entities": {}, "first_seen": None, "runs": 0}
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    memory = json.load(f)
            except Exception as e:
                self.log(f"memory read error: {e}")

        known_ids = set(memory.get("entities", {}).keys())
        new_ids, seen_ids = [], []
        for e in peer_entities:
            eid = e.get("id")
            if not eid:
                continue
            if eid in known_ids:
                seen_ids.append(eid)
            else:
                new_ids.append(eid)
                memory["entities"][eid] = {"label": e.get("label"), "type": e.get("type")}

        now = datetime.now(timezone.utc).isoformat()
        memory["first_seen"] = memory.get("first_seen") or now
        memory["last_seen"] = now
        memory["runs"] = memory.get("runs", 0) + 1
        try:
            with open(path, "w") as f:
                json.dump(memory, f, indent=2)
        except Exception as e:
            self.log(f"memory write error: {e}")

        self.log(f"memory: {len(new_ids)} new, {len(seen_ids)} previously-seen, "
                 f"{len(memory['entities'])} total (run #{memory['runs']})")
        signals = [{"type": "memory", "new_entities": len(new_ids),
                    "known_entities": len(seen_ids), "total": len(memory["entities"]),
                    "run_count": memory["runs"], "source": "vault"}]
        if seen_ids:
            signals.append({"type": "recurrence", "entity_ids": seen_ids[:20],
                            "note": "entity seen in a prior investigation of this case",
                            "source": "vault"})
        return AgentResult(
            agent=self.name, status="done",
            output={"total_entities": len(memory["entities"]), "new": new_ids,
                    "previously_seen": seen_ids, "run_count": memory["runs"],
                    "store": path},
            confidence=0.9 if peer_entities else 0.5,
            reasoning=f"Memory for case {case_id}: {len(new_ids)} new entities, "
                      f"{len(seen_ids)} recurring, {len(memory['entities'])} total.",
            signals=signals, latency_s=self._elapsed(t0),
        )


class SentinelAgent(BaseAgent):
    """Monitoring — baseline snapshots + change detection.

    Registers a target on a file-backed watchlist and snapshots its signal
    fingerprint. On a later run it diffs against the last snapshot and reports
    what changed (new signal types, count deltas) — the core of monitoring without
    a live event bus. A Redis pub/sub scheduler can drive re-runs later; the diff
    logic here is what it would call.
    """
    name = "SENTINEL"; role = "Live Monitoring"; icon = "⊲"
    description = "Real-time tracking: new posts, bio changes, username changes, new domains, leaks"
    preferred_models = ["phi3:mini"]; token_budget = 2048

    @staticmethod
    def _store_dir() -> str:
        import os
        d = os.getenv("SENTINEL_DIR",
                      os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "sentinel_store"))
        os.makedirs(d, exist_ok=True)
        return d

    async def run(self, input_data, context=None):
        import os, json, hashlib
        from datetime import datetime, timezone
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        peer_signals = context.get("peer_signals", []) or []

        types = sorted({s.get("type", "?") for s in peer_signals})
        fingerprint = hashlib.sha256(
            ("|".join(types) + f"::{len(peer_signals)}").encode()
        ).hexdigest()[:16]
        key = hashlib.sha256(target.encode()).hexdigest()[:16]
        path = os.path.join(self._store_dir(), f"{key}.json")

        prior = None
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    prior = json.load(f)
            except Exception as e:
                self.log(f"snapshot read error: {e}")

        now = datetime.now(timezone.utc).isoformat()
        changes: list[str] = []
        if prior is None:
            self.log(f"new monitor registered for target (baseline: {len(peer_signals)} signals)")
        else:
            new_types = set(types) - set(prior.get("signal_types", []))
            gone_types = set(prior.get("signal_types", [])) - set(types)
            delta = len(peer_signals) - prior.get("signal_count", 0)
            if new_types:
                changes.append(f"new signal types: {', '.join(sorted(new_types))}")
            if gone_types:
                changes.append(f"dropped signal types: {', '.join(sorted(gone_types))}")
            if delta:
                changes.append(f"signal count {'+' if delta > 0 else ''}{delta}")
            self.log(f"diff vs {prior.get('last_seen', '?')[:10]}: "
                     + ("; ".join(changes) if changes else "no change"))

        snapshot = {"target": target[:120], "signal_count": len(peer_signals),
                    "signal_types": types, "fingerprint": fingerprint,
                    "first_seen": (prior or {}).get("first_seen", now), "last_seen": now,
                    "checks": (prior or {}).get("checks", 0) + 1}
        try:
            with open(path, "w") as f:
                json.dump(snapshot, f, indent=2)
        except Exception as e:
            self.log(f"snapshot write error: {e}")

        signals = [{"type": "monitor", "status": "baseline" if prior is None else "diffed",
                    "changes": changes, "checks": snapshot["checks"], "source": "sentinel"}]
        return AgentResult(
            agent=self.name, status="done",
            output={"monitoring": True, "baseline": prior is None,
                    "changes": changes, "snapshot": snapshot},
            confidence=0.6,
            reasoning=("Baseline snapshot registered." if prior is None
                       else (f"Detected {len(changes)} change(s)." if changes
                             else "No change since last check.")),
            signals=signals, latency_s=self._elapsed(t0),
        )


class EmailAgent(BaseAgent):
    """Email Intelligence — account existence enumeration across providers.

    Given an email, checks which major providers (Gmail, Yahoo, Outlook, Proton,
    iCloud, Tutanota) have the address registered via password-reset flow
    inference. Works over OPSEC layer (Tor) to avoid IP-based rate limits.
    Degrades gracefully when services are unreachable.
    """
    name = "EMAIL"; role = "Email Intelligence"; icon = "✉"
    description = "Email account existence enumeration across 50+ providers (Gmail, Yahoo, Outlook, Proton…)"
    preferred_models = ["qwen2.5:7b"]; token_budget = 4096

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)

        signals: list[dict] = []
        entities: list[dict] = []

        if "@" not in target:
            self.log(f"not an email: {target}")
            return AgentResult(agent=self.name, status="error", output={"input": target},
                               error="not an email address", latency_s=self._elapsed(t0))

        try:
            from connectors.email_enum import run_email_enum
            self.log(f"email account enumeration: {target}")
            data = await asyncio.wait_for(run_email_enum(target), timeout=30.0)
        except asyncio.TimeoutError:
            self.log("email enum timed out after 30s")
            return AgentResult(agent=self.name, status="error", output={"email": target},
                               error="timeout", latency_s=self._elapsed(t0))
        except Exception as e:
            self.log(f"email enum error: {e}")
            return AgentResult(agent=self.name, status="error", output={"email": target},
                               error=str(e), latency_s=self._elapsed(t0))

        found = data.get("providers_with_account", [])
        if found:
            self.log(f"found accounts on {len(found)} provider(s): {', '.join(found)}")
            for provider in found:
                signals.append({"type": "email_account", "provider": provider,
                                "email": target, "source": "email_enum"})
        else:
            self.log("no accounts found on major providers")

        entities.append({"id": f"email_{target.lower()}", "label": target, "type": "email"})
        signals.append({"type": "email_profile", "address": target,
                        "providers_checked": data.get("total_providers_checked", 0),
                        "providers_found": len(found), "source": "email_enum"})

        confidence = 0.8 if found else 0.3
        return AgentResult(
            agent=self.name, status="done",
            output={
                "email": target,
                "accounts_found": found,
                "details": data.get("details", {}),
            },
            confidence=confidence,
            reasoning=f"Email {target}: found on {len(found)} provider(s)"
                      + (f" ({', '.join(found)})." if found else "."),
            signals=signals, entities_found=entities,
            latency_s=self._elapsed(t0),
        )


class QuillAgent(BaseAgent):
    """Report Generation — deterministic intelligence brief.

    Synthesizes the full investigation (routing, peer agent results, correlation
    graph, timeline) into a structured Markdown intelligence report and a compact
    machine-readable summary. Fully deterministic — no LLM call required, so it
    always produces a report even fully offline. If ReportLab is installed a PDF
    is rendered to ``context['report_dir']`` as well; otherwise the Markdown stands
    on its own.
    """
    name = "QUILL"; role = "Report Generation"; icon = "⟁"
    description = "AI-generated intelligence summaries, PDF reports, evidence bundles, graph exports"
    preferred_models = ["gemma2:9b", "claude-opus-4"]; token_budget = 16384

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        peer_signals: list[dict] = context.get("peer_signals", []) or []
        peer_entities: list[dict] = context.get("peer_entities", []) or []
        agent_results: list[dict] = context.get("agent_results", []) or []
        input_type = context.get("input_type", "unknown")
        case_id = context.get("case_id", "—")

        self.log(f"generating intelligence report for {input_type} target")

        from datetime import datetime, timezone
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # ── Signal tallies ────────────────────────────────────────────────────
        by_type: dict[str, int] = {}
        for s in peer_signals:
            by_type[s.get("type", "unknown")] = by_type.get(s.get("type", "unknown"), 0) + 1

        graph = next((r.get("output", {}) for r in agent_results if r.get("agent") == "NEXUS"), {})
        timeline = next((r.get("output", {}) for r in agent_results if r.get("agent") == "KRONOS"), {})

        completed = [r for r in agent_results if r.get("status") == "done"]
        errored = [r for r in agent_results if r.get("status") == "error"]

        # ── Markdown report ───────────────────────────────────────────────────
        lines: list[str] = [
            f"# Intelligence Report — {target[:80]}",
            "",
            f"- **Case ID:** `{case_id}`",
            f"- **Input type:** {input_type}",
            f"- **Generated:** {generated}",
            f"- **Agents completed:** {len(completed)}"
            + (f" · **errored:** {len(errored)}" if errored else ""),
            "",
            "## Executive Summary",
            "",
            self._summary(target, input_type, by_type, graph, timeline, agent_results),
            "",
            "## Signals Collected",
            "",
        ]
        if by_type:
            lines += [f"- **{count}× {stype}**" for stype, count in
                      sorted(by_type.items(), key=lambda kv: -kv[1])]
        else:
            lines.append("- _No signals extracted._")

        lines += ["", "## Entities", ""]
        if peer_entities:
            seen = set()
            for e in peer_entities:
                key = e.get("id") or e.get("label")
                if key in seen:
                    continue
                seen.add(key)
                lines.append(f"- **{e.get('label', '?')}** _({e.get('type', 'entity')})_")
        else:
            lines.append("- _No entities resolved._")

        if graph.get("top_entities"):
            lines += ["", "## Correlation — Most Connected", ""]
            for n in graph["top_entities"]:
                lines.append(f"- **{n['label']}** — degree {n['degree']}, "
                             f"influence {n['influence']}")
            lines.append("")
            lines.append(f"_Graph: {len(graph.get('nodes', []))} nodes, "
                         f"{len(graph.get('edges', []))} edges, "
                         f"{len(graph.get('clusters', []))} cluster(s), "
                         f"density {graph.get('graph_density', 0)}._")

        events = timeline.get("events", [])
        if events:
            lines += ["", "## Timeline", ""]
            for ev in events:
                lines.append(f"- `{ev['timestamp'][:10]}` — {ev['label']} "
                             f"_(via {ev['source']})_")

        lines += ["", "## Agent Activity", ""]
        for r in agent_results:
            status = r.get("status", "?")
            mark = {"done": "✓", "error": "✗", "stub": "◌"}.get(status, "•")
            lines.append(f"- {mark} **{r.get('agent')}** ({r.get('role', '')}) — "
                         f"{status}, {r.get('confidence', 0):.0%} conf, "
                         f"{r.get('latency_s', 0)}s")

        lines += ["", "---", "_Generated by Signal-OS · QUILL · deterministic brief._"]
        markdown = "\n".join(lines)

        # ── Optional PDF render (best-effort) ─────────────────────────────────
        pdf_path = None
        report_dir = context.get("report_dir")
        if report_dir:
            try:
                pdf_path = self._render_pdf(markdown, report_dir, case_id)
                if pdf_path:
                    self.log(f"PDF written: {pdf_path}")
            except Exception as e:
                self.log(f"PDF render skipped: {e}")

        self.log(f"report ready — {len(peer_signals)} signals, {len(events)} events, "
                 f"{len(markdown)} chars")
        return AgentResult(
            agent=self.name, status="done",
            output={
                "markdown": markdown,
                "signal_tally": by_type,
                "event_count": len(events),
                "entity_count": len(peer_entities),
                "pdf_path": pdf_path,
                "generated_at": generated,
            },
            confidence=0.9 if peer_signals else 0.4,
            reasoning=f"Compiled brief covering {len(agent_results)} agents, "
                      f"{len(peer_signals)} signals, {len(events)} timeline events.",
            signals=[{"type": "report", "format": "markdown",
                      "length": len(markdown), "pdf": bool(pdf_path), "source": "quill"}],
            latency_s=self._elapsed(t0),
        )

    @staticmethod
    def _summary(target, input_type, by_type, graph, timeline, agent_results=None) -> str:
        parts = [f"Investigation of `{target[:60]}` classified as **{input_type}**."]
        # Phone profile (from PHONOS), if present.
        phone = None
        for r in (agent_results or []):
            if r.get("agent") == "PHONOS":
                phone = (r.get("output") or {}).get("offline", {})
                break
        if phone and phone.get("valid"):
            bits = [b for b in (phone.get("carrier"), phone.get("line_type"),
                                phone.get("location") or phone.get("region")) if b]
            parts.append(f"Number is valid ({', '.join(bits)}).")
        accounts = by_type.get("account", 0)
        breaches = by_type.get("breach", 0)
        gps = by_type.get("gps", 0)
        matches = by_type.get("face_match", 0) + by_type.get("image_match", 0)
        if accounts:
            parts.append(f"{accounts} linked account(s) identified across platforms.")
        if breaches:
            parts.append(f"Exposure in {breaches} known data breach(es).")
        if gps:
            parts.append(f"{gps} geolocation signal(s) recovered from media metadata.")
        if matches:
            parts.append(f"{matches} media/face match(es) found via reverse search.")
        if graph.get("clusters"):
            parts.append(f"Correlation produced {len(graph['clusters'])} entity cluster(s).")
        span = timeline.get("span")
        if span:
            parts.append(f"Activity timeline spans {span['earliest'][:10]} → "
                         f"{span['latest'][:10]}.")
        if len(parts) == 1:
            parts.append("No corroborating signals were recovered from public sources.")
        return " ".join(parts)

    @staticmethod
    def _render_pdf(markdown: str, report_dir: str, case_id: str):
        """Render Markdown to a simple PDF if ReportLab is available."""
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.lib.units import inch
            from reportlab.pdfgen import canvas
        except ImportError:
            return None
        import os
        os.makedirs(report_dir, exist_ok=True)
        path = os.path.join(report_dir, f"signal-os-report-{case_id}.pdf")
        c = canvas.Canvas(path, pagesize=letter)
        width, height = letter
        y = height - inch
        for raw in markdown.splitlines():
            line = raw.replace("**", "").replace("`", "").replace("_", "")
            if y < inch:
                c.showPage()
                y = height - inch
            font, size = ("Helvetica", 10)
            if line.startswith("# "):
                font, size, line = "Helvetica-Bold", 16, line[2:]
            elif line.startswith("## "):
                font, size, line = "Helvetica-Bold", 13, line[3:]
            c.setFont(font, size)
            c.drawString(inch, y, line[:110])
            y -= size + 5
        c.save()
        return path
