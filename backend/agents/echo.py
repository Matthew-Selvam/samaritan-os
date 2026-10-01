"""
Echo.py — ECHO Agent
====================
"""
from __future__ import annotations

import asyncio
from .base import BaseAgent, AgentResult

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
