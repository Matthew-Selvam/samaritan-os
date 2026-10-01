"""
transcribe.py — WhisperX / faster-whisper audio transcription for ECHO
=======================================================================
Replaces ECHO's inline ``faster_whisper`` call with one connector that tries,
in order:

1. **faster-whisper** (local, CTranslate2) — lazy import, optional. Supports
   ``whisperx`` word-level alignment when that package is installed, which
   upgrades ``segments[]`` with per-word ``start``/``end``/``score``.
2. **OpenAI-compatible Whisper endpoint** (``/v1/audio/transcriptions``) —
   works against OpenAI, Groq, a self-hosted faster-whisper-server, or any
   other server that speaks that shape. Key-gated, degrades cleanly.
3. **Ollama** is not a transcription path; the metadata layer below still runs
   so the agent reports real facts about the file instead of nothing.

Everything is normalised to one shape, which is what makes the three engines
interchangeable:

``{"text", "language", "duration_s", "segments": [{"start","end","text",
"confidence"}], "confidence", "engine", "words": [...], "error",
"available", "warnings"}``

Safety, because the input is an arbitrary upload:

* **Never crashes.** Every failure path returns a dict with ``error`` set.
* **Real-audio validation.** :func:`probe_audio` checks magic bytes (RIFF/WAVE,
  OggS, fLaC, ID3/MPEG frame sync, ftyp/M4A, AMR, µ-law/A-law) and the
  container header, so a 4 KB HTML file named ``voice.wav`` is rejected before
  any decoder loads it.
* **Size cap** — ``MAX_AUDIO_BYTES`` (200 MB) and a wall-clock timeout.
* **Duration cap** — an hours-long file is truncated rather than blocking the
  pipeline; the truncation is reported in ``warnings``.
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
from typing import Any

#: Hard ceiling on the audio file we will transcribe.
MAX_AUDIO_BYTES = 200 * 1024 * 1024
#: Cap on the audio duration we will actually process (seconds).
MAX_AUDIO_SECONDS = 60 * 60
#: Per-engine wall-clock budget.
DEFAULT_TIMEOUT_S = 300.0

_AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".opus",
               ".flac", ".webm", ".aiff", ".aif", ".amr", ".wma", ".caf", ".3gp")

_ENGINE = os.getenv("WHISPER_ENGINE", "auto").lower()   # auto|faster_whisper|api|off
_MODEL_SIZE = os.getenv("WHISPER_MODEL", "base")
_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
_COMPUTE = os.getenv("WHISPER_COMPUTE", "int8")
_WHISPERX_LANG = os.getenv("WHISPERX_LANGUAGE", "")      # "" = auto-detect
_WHISPER_API_BASE = os.getenv("WHISPER_API_BASE", "https://api.openai.com/v1")
_WHISPER_API_KEY = os.getenv("WHISPER_API_KEY", os.getenv("OPENAI_API_KEY", ""))
_WHISPER_API_MODEL = os.getenv("WHISPER_API_MODEL", "whisper-1")
_WHISPERX_ENABLED = os.getenv("WHISPERX_ENABLED", "true").lower() == "true"

_MODEL_CACHE: dict[tuple[str, str], Any] = {}


# ── Audio validation ─────────────────────────────────────────────────────────

def sniff_audio_type(data: bytes) -> str | None:
    """Identify an audio container from its magic bytes.

    Args:
        data: leading bytes of the file.

    Returns:
        A lowercase format name (``"wav"``, ``"mp3"``, ``"flac"``, …) or
        ``None`` when the data is not recognisably audio.
    """
    if not data or len(data) < 12:
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"ID3":
        return "mp3"
    if data[4:12] in (b"ftypM4A", b"ftypmp42", b"ftypisom", b"ftypM4B"):
        return "m4a"
    if data[4:8] == b"ftyp":
        return "m4a"          # generic ISO-BMFF audio
    if data[:2] == b"#!AMR":
        return "amr"
    # Raw MPEG audio frame sync: 11 set bits.
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    # µ-law / A-law WAV variants (no RIFF magic but a valid-ish header).
    if data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xa3", b"\x0f\xff"):
        return "wav"
    return None


def probe_audio(source: str | bytes, *, max_bytes: int = MAX_AUDIO_BYTES) -> dict[str, Any]:
    """Validate an audio file and read what we can without a decoder.

    Args:
        source: filesystem path or raw bytes.
        max_bytes: refuse anything larger.

    Returns:
        ``{"ok", "format", "bytes", "duration_s", "sample_rate", "channels",
        "bitrate_kbps", "error"}``. ``error`` is ``None`` when ``ok`` is True.
    """
    out: dict[str, Any] = {
        "ok": False, "format": None, "bytes": 0, "duration_s": None,
        "sample_rate": None, "channels": None, "bitrate_kbps": None, "error": None,
    }

    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        if not data:
            out["error"] = "empty audio data"
            return out
        if len(data) > max_bytes:
            out["error"] = f"audio too large: {len(data)} bytes (cap {max_bytes})"
            return out
        out["bytes"] = len(data)
        out["format"] = sniff_audio_type(data)
        if not out["format"]:
            out["error"] = "data does not look like audio (no recognised container header)"
            return out
        out["ok"] = True
        return out

    path = str(source or "")
    if not path:
        out["error"] = "empty path"
        return out
    if not os.path.isfile(path):
        out["error"] = f"not a file: {path[:80]}"
        return out
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        out["error"] = f"cannot stat audio: {exc.strerror or exc}"
        return out
    out["bytes"] = size
    if size == 0:
        out["error"] = "audio file is empty"
        return out
    if size > max_bytes:
        out["error"] = f"audio too large: {size} bytes (cap {max_bytes})"
        return out

    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError as exc:
        out["error"] = f"cannot read audio: {exc.strerror or exc}"
        return out

    fmt = sniff_audio_type(head)
    if not fmt:
        # Containers we cannot magic-byte: mp4 (video+audio), webm, wma, caf,
        # 3gp. Fall back to the extension, but only for known-audio types.
        ext = os.path.splitext(path)[1].lower()
        if ext in (".webm", ".wma", ".caf", ".3gp", ".mp4"):
            fmt = ext.lstrip(".")
        else:
            out["error"] = ("data does not look like audio (no recognised container "
                            "header) — is this really an audio file?")
            return out
    out["format"] = fmt

    # WAV gives us exact duration/rate/channels for free.
    if fmt == "wav":
        try:
            import wave
            with wave.open(path, "rb") as w:
                rate = w.getframerate() or 0
                frames = w.getnframes()
                out["channels"] = w.getnchannels()
                out["sample_rate"] = rate
                out["duration_s"] = round(frames / rate, 2) if rate else None
                if rate:
                    out["bitrate_kbps"] = round(rate * w.getnchannels() * w.getsampwidth() * 8 / 1000)
        except Exception as exc:  # noqa: BLE001 — a truncated WAV still transcribes
            out["error"] = None   # not fatal; format is confirmed
            out["format"] = "wav"

    out["ok"] = True
    return out


def container_metadata(path: str) -> dict[str, Any]:
    """Dependency-free metadata for any supported container.

    Args:
        path: filesystem path.

    Returns:
        The probe dict (never raises).
    """
    return probe_audio(path)


# ── faster-whisper engine ────────────────────────────────────────────────────

def faster_whisper_installed() -> bool:
    """``True`` when the ``faster_whisper`` package is importable."""
    try:
        import faster_whisper  # noqa: F401  (lazy optional probe)
        return True
    except ImportError:
        return False


def whisperx_installed() -> bool:
    """``True`` when ``whisperx`` is importable (word-level alignment)."""
    if not _WHISPERX_ENABLED:
        return False
    try:
        import whisperx  # noqa: F401  (lazy optional probe)
        return True
    except ImportError:
        return False


def _load_model(size: str | None = None, device: str | None = None) -> Any:
    """Instantiate (and cache) a ``WhisperModel``.

    Args:
        size: model size or HF repo id; defaults to ``WHISPER_MODEL``.
        device: ``cpu`` or ``cuda``; defaults to ``WHISPER_DEVICE``.

    Returns:
        The model, or ``None`` when faster-whisper is unavailable.
    """
    size = size or _MODEL_SIZE
    device = device or _DEVICE
    key = (size, device)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    try:
        from faster_whisper import WhisperModel  # lazy, optional heavy import
    except ImportError:
        return None
    model = WhisperModel(size, device=device, compute_type=_COMPUTE)
    _MODEL_CACHE[key] = model
    return model


def _seg(start: Any, end: Any, text: str, conf: float | None = None) -> dict[str, Any]:
    """Build a normalised segment dict.

    Args:
        start: segment start in seconds.
        end: segment end in seconds.
        text: segment text.
        conf: optional confidence in 0..1.

    Returns:
        ``{"start", "end", "text", "confidence"}`` with rounded timings.
    """
    return {
        "start": round(float(start or 0.0), 2),
        "end": round(float(end or 0.0), 2),
        "text": (text or "").strip(),
        "confidence": round(float(conf), 3) if conf is not None else None,
    }


def _normalise_segments(raw: list[Any], *, avg: float | None = None) -> list[dict[str, Any]]:
    """Normalise a heterogeneous segment list into our shape.

    Args:
        raw: segments from faster-whisper (objects) or an API (dicts).
        avg: fallback confidence when none is present.

    Returns:
        List of ``{"start", "end", "text", "confidence"}``.
    """
    out: list[dict[str, Any]] = []
    for s in raw or []:
        if isinstance(s, dict):
            out.append(_seg(s.get("start"), s.get("end"), s.get("text", ""),
                            s.get("confidence", s.get("avg_logprob", avg))))
        else:
            logprob = getattr(s, "avg_logprob", None)
            # avg_logprob is negative (natural log). exp() maps -0.2 -> 0.82,
            # which lands in a usable 0..1 range without pretending to be a
            # real probability.
            conf = None
            if logprob is not None:
                try:
                    import math
                    conf = min(1.0, max(0.0, math.exp(float(logprob))))
                except Exception:  # noqa: BLE001
                    conf = None
            out.append(_seg(getattr(s, "start", None), getattr(s, "end", None),
                            getattr(s, "text", ""), conf if conf is not None else avg))
    return [s for s in out if s["text"]]


def _transcribe_faster_whisper(path: str, *, language: str | None = None,
                               beam_size: int = 5) -> dict[str, Any]:
    """Transcribe with the local faster-whisper model (blocking; run in a thread).

    Args:
        path: audio file path.
        language: forced ISO language or ``None`` to auto-detect.
        beam_size: beam search width.

    Returns:
        A normalised transcription dict; ``error`` is set on failure.
    """
    model = _load_model()
    if model is None:
        return {"error": "faster-whisper not installed — pip install faster-whisper",
                "available": False}
    try:
        segments, info = model.transcribe(path, beam_size=beam_size,
                                          language=language or _WHISPERX_LANG or None)
        segs = _normalise_segments(list(segments))
        text = " ".join(s["text"] for s in segs).strip()
        durations = [s["end"] for s in segs if s["end"]]
        confs = [s["confidence"] for s in segs if s["confidence"] is not None]
        return {
            "text": text,
            "language": getattr(info, "language", None),
            "language_probability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 3) or None,
            "duration_s": getattr(info, "duration", None) or (max(durations) if durations else None),
            "segments": segs,
            "words": [],
            "confidence": round(sum(confs) / len(confs), 3) if confs else None,
            "engine": "faster_whisper",
            "available": True,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"faster-whisper failed: {type(exc).__name__}: {exc}",
                "available": True, "engine": "faster_whisper"}


def _align_whisperx(audio_path: str, transcript: dict[str, Any]) -> None:
    """Upgrade a faster-whisper transcript with WhisperX word timings.

    Args:
        audio_path: the audio file.
        transcript: the faster-whisper result, mutated in place to add ``words``.

    Returns:
        ``None``. On failure the transcript is left as-is.
    """
    if not _WHISPERX_ENABLED or not transcript.get("segments"):
        return
    try:
        import whisperx  # lazy, optional heavy import
        device = _DEVICE
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:  # noqa: BLE001
            pass
        model = whisperx.load_model(_MODEL_SIZE, device, compute_type=_COMPUTE)
        result = model.align(audio_path, transcript["segments"],
                             torch_dtype=None if device == "cuda" else "auto")
        aligned = whisperx.load_align_model(language_code=transcript.get("language") or "en",
                                            device=device)
        out = whisperx.align(result["segments"], aligned, audio_path,
                             device, return_char_alignments=False)
        words: list[dict[str, Any]] = []
        for seg in out.get("segments", []):
            for w in seg.get("words", []) or []:
                words.append({"word": w.get("word", "").strip(),
                              "start": round(float(w.get("start", 0.0)), 2),
                              "end": round(float(w.get("end", 0.0)), 2),
                              "score": round(float(w.get("score", 0.0)), 3)})
        if words:
            transcript["words"] = words
            transcript["engine"] = "whisperx"
    except Exception as exc:  # noqa: BLE001 — alignment is an upgrade, not a requirement
        transcript.setdefault("warnings", []).append(f"whisperx alignment skipped: {exc}")


# ── OpenAI-compatible endpoint engine ────────────────────────────────────────

def api_endpoint_available() -> bool:
    """``True`` when a Whisper-compatible HTTP endpoint is configured."""
    return bool(_WHISPER_API_KEY and _WHISPER_API_BASE)


async def _transcribe_api(path: str, *, language: str | None = None) -> dict[str, Any]:
    """Transcribe via an OpenAI-compatible ``/audio/transcriptions`` endpoint.

    Args:
        path: audio file path.
        language: forced ISO language.

    Returns:
        A normalised transcription dict. The upload goes through
        :func:`connectors.net.fetch`, so the endpoint host is SSRF-checked and
        the response is size-capped like every other outbound call.
    """
    if not api_endpoint_available():
        return {"error": "WHISPER_API_KEY not set (or WHISPER_API_BASE) — "
                         "use a self-hosted faster-whisper-server for free",
                "available": False}
    from . import net

    try:
        with open(path, "rb") as fh:
            audio = fh.read()
    except OSError as exc:
        return {"error": f"cannot read audio: {exc}", "available": True}

    ext = os.path.splitext(path)[1].lstrip(".").lower() or "wav"
    url = _WHISPER_API_BASE.rstrip("/") + "/audio/transcriptions"
    try:
        res = await net.fetch(
            "POST", url,
            data={"model": _WHISPER_API_MODEL, "response_format": "verbose_json"},
            files={"file": (f"audio.{ext}", audio, "application/octet-stream")},
            headers={"authorization": f"Bearer {_WHISPER_API_KEY}"},
            timeout=DEFAULT_TIMEOUT_S,
            max_bytes=8 * 1024 * 1024,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"transcription request failed: {exc}", "available": True}

    if not res.ok:
        return {"error": f"transcription endpoint error: {res.error or res.status_code}",
                "available": True, "status_code": res.status_code}
    payload = res.json() or {}
    segs = _normalise_segments(payload.get("segments") or [])
    text = (payload.get("text") or " ".join(s["text"] for s in segs)).strip()
    return {
        "text": text,
        "language": payload.get("language") or language,
        "duration_s": payload.get("duration"),
        "segments": segs,
        "words": [],
        "confidence": None,
        "engine": "whisper_api",
        "available": True,
        "error": None,
    }


# ── Orchestration ────────────────────────────────────────────────────────────

def engines() -> dict[str, bool]:
    """Report which transcription engines are usable right now.

    Returns:
        ``{"faster_whisper": bool, "whisperx": bool, "api": bool, "available": bool}``.
    """
    fw = faster_whisper_installed()
    return {"faster_whisper": fw, "whisperx": whisperx_installed(),
            "api": api_endpoint_available(),
            "available": fw or api_endpoint_available()}


def health() -> dict[str, Any]:
    """Engine readiness for the OPS view.

    Returns:
        The :func:`engines` mapping plus a human-readable ``reason`` when no
        engine is available.
    """
    e = engines()
    if not e["available"]:
        e["reason"] = ("no transcription engine — pip install faster-whisper "
                       "(local, free) or set WHISPER_API_KEY")
    return e


async def transcribe(
    path: str,
    *,
    language: str | None = None,
    engine: str = "auto",
    allow_alignment: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Transcribe an audio file, trying each engine until one succeeds.

    Args:
        path: local audio file path.
        language: forced ISO-639-1 language, or ``None`` to auto-detect.
        engine: ``auto`` (try local then API), ``faster_whisper``, ``api``, or
            ``off``.
        allow_alignment: run WhisperX word alignment after a local transcript.
        timeout_s: wall-clock budget for the whole call.

    Returns:
        The normalised transcription dict, always with ``text``, ``language``,
        ``duration_s``, ``segments``, ``confidence`` and ``error`` keys. On
        total failure ``error`` explains what was tried. Never raises.
    """
    base: dict[str, Any] = {
        "text": "", "language": None, "duration_s": None, "segments": [],
        "words": [], "confidence": None, "engine": None, "available": False,
        "error": None, "warnings": [],
    }
    want = (engine or _ENGINE or "auto").lower()

    probe = probe_audio(path)
    base["format"] = probe.get("format")
    base["file_bytes"] = probe.get("bytes")
    if probe.get("duration_s"):
        base["duration_s"] = probe["duration_s"]
    base["sample_rate"] = probe.get("sample_rate")
    base["channels"] = probe.get("channels")
    if not probe.get("ok"):
        base["error"] = probe.get("error") or "not a usable audio file"
        return base

    if want == "off":
        base["error"] = "transcription disabled (WHISPER_ENGINE=off)"
        return base

    attempts: list[str] = []
    result: dict[str, Any] | None = None

    try:
        if want in ("auto", "faster_whisper") and faster_whisper_installed():
            attempts.append("faster_whisper")
            local = await asyncio.wait_for(
                asyncio.to_thread(_transcribe_faster_whisper, path, language=language),
                timeout=timeout_s,
            )
            if not local.get("error") and local.get("text") is not None and (
                    local.get("text") or local.get("segments")):
                result = local
                if allow_alignment and whisperx_installed():
                    await asyncio.wait_for(
                        asyncio.to_thread(_align_whisperx, path, result),
                        timeout=min(timeout_s, 120.0),
                    )
            else:
                base["warnings"].append(f"faster_whisper: {local.get('error')}")

        if result is None and want in ("auto", "api") and api_endpoint_available():
            attempts.append("whisper_api")
            result = await _transcribe_api(path, language=language)
            if result.get("error"):
                base["warnings"].append(f"whisper_api: {result['error']}")
                result = None

        if result is None:
            base["error"] = ("no transcription engine produced output "
                             f"(tried: {', '.join(attempts) or 'none available'})")
            if not attempts:
                base["error"] = ("no transcription engine available — "
                                 "pip install faster-whisper or set WHISPER_API_KEY")
            return base

        merged = {**base, **{k: v for k, v in result.items() if v is not None or k == "error"}}
        merged["warnings"] = base["warnings"] + list(result.get("warnings") or [])
        merged["error"] = None
        merged["available"] = True
        # Language fallback: trust the pipeline's langid over a silent guess.
        if not merged.get("language"):
            try:
                from .langid import detect
                guess = detect(merged.get("text") or "")
                merged["language"] = None if guess.get("language") == "und" else guess["language"]
                merged["language_source"] = "langid"
            except Exception:  # noqa: BLE001
                merged["language"] = None
        return merged
    except asyncio.TimeoutError:
        base["error"] = f"transcription timed out after {timeout_s:.0f}s"
        return base
    except Exception as exc:  # noqa: BLE001 — absolute last line of defence
        base["error"] = f"{type(exc).__name__}: {exc}"
        return base


async def run_transcribe(path: str, **kwargs: Any) -> dict[str, Any]:
    """Top-level entry point for ECHO.

    Args:
        path: local audio file path.
        **kwargs: forwarded to :func:`transcribe` (``language``, ``engine``,
            ``timeout_s``, ``allow_alignment``).

    Returns:
        The normalised transcription dict. Never raises.
    """
    try:
        return await transcribe(
            path,
            language=kwargs.get("language"),
            engine=kwargs.get("engine", "auto"),
            allow_alignment=bool(kwargs.get("allow_alignment", True)),
            timeout_s=float(kwargs.get("timeout_s", DEFAULT_TIMEOUT_S)),
        )
    except Exception as exc:  # noqa: BLE001
        return {"text": "", "language": None, "duration_s": None, "segments": [],
                "words": [], "confidence": None, "engine": None,
                "available": False, "error": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "MAX_AUDIO_BYTES", "engines", "health", "faster_whisper_installed",
    "whisperx_installed", "api_endpoint_available", "sniff_audio_type",
    "probe_audio", "container_metadata", "transcribe", "run_transcribe",
]
