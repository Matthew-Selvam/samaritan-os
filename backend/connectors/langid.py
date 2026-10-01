"""
langid.py — Language Detection (multi-language support)
========================================================
Every agent output that carries free text gets a detected language attached, so
QUILL can report "Russian transcript" instead of an unlabelled blob and so
NER/entity pipelines can pick the right model per locale.

Two engines, tried in order:

1. **fasttext** (``fasttext-lid176``) — the real deal, loaded lazily from a
   local ``.ftz`` model when present. Glibc-safe model loading (the wheel
   shadows the stdlib ``fasttext`` symbol). Skipped entirely when absent.
2. **Script + stopword heuristic** — pure stdlib, always available. Combines a
   Unicode-script vote (which settles CJK/Arabic/Devanagari/Cyrillic in a
   handful of characters) with character n-gram scoring against per-language
   stopword lists for the Latin languages that scripts alone cannot separate.

Covers Latin (en/es/fr/de/pt/it/nl/tr/id/vi), Cyrillic (ru/uk/bg),
CJK (zh/ja/ko), Arabic (ar/fa), Devanagari (hi/mr/ne) plus Greek (el), Hebrew
(he), Thai (th) and Korean.

Never raises: :func:`detect` always returns a result dict.
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

# ── Unicode script ranges ────────────────────────────────────────────────────

#: (name, predicate) style range tables. Kept as explicit code-point ranges so
#: no third-party dependency is needed to classify a script.
_SCRIPT_RANGES: tuple[tuple[str, int, int], ...] = (
    ("latin",        0x0041, 0x024F),
    ("latin_ext",    0x1E00, 0x1EFF),
    ("cyrillic",     0x0400, 0x04FF),
    ("cyrillic_ext", 0x0500, 0x052F),
    ("greek",        0x0370, 0x03FF),
    ("hebrew",       0x0590, 0x05FF),
    ("arabic",       0x0600, 0x06FF),
    ("arabic_ext",   0x0750, 0x077F),
    ("devanagari",   0x0900, 0x097F),
    ("thai",         0x0E00, 0x0E7F),
    ("cjk",          0x4E00, 0x9FFF),   # Han (unified)
    ("cjk",          0x3400, 0x4DBF),   # Han extension A
    ("hiragana",     0x3040, 0x309F),
    ("katakana",     0x30A0, 0x30FF),
    ("hangul",       0xAC00, 0xD7AF),
    ("hangul_jamo",  0x1100, 0x11FF),
)

#: Scripts that are unambiguous — one script maps to one language family.
_SCRIPT_LANG: dict[str, str] = {
    "cyrillic": "ru", "cyrillic_ext": "ru", "greek": "el",
    "hebrew": "he", "arabic": "ar", "arabic_ext": "ar",
    "devanagari": "hi", "thai": "th", "hangul": "ko",
    "hangul_jamo": "ko",
}

#: Kana present → Japanese (Han alone is Chinese).
_KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\uff66-\uff9d]")
#: Hangul syllables present → Korean.
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")

# ── Stopword evidence for the Latin / Han languages ──────────────────────────

STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset("the of and to in is that it for was with as on be at by this have from or an are not but they his her its you your we our".split()),
    "es": frozenset("el la los las de del y que en es un una con por para no se su sus como más pero sobre este esta son está están".split()),
    "fr": frozenset("le la les des de du et que en est un une avec pour pas sur se ce cette ces sont plus dans qui son ses nous vous".split()),
    "de": frozenset("der die das und in den von zu mit sich des auf für ist im dem nicht ein eine als auch es an werden aus er".split()),
    "pt": frozenset("o a os as de do da e em que um uma com para não se por mais como sobre está são ele ela dos das".split()),
    "it": frozenset("il lo la i gli le di del e che in un una con per non si ma come sono sullo questo questa degli delle".split()),
    "nl": frozenset("de het een en van in is dat op voor niet met te zijn er die ook als maar door voor naar uit".split()),
    "tr": frozenset("bir bu ve ile için de da ne olarak çok daha var olan gibi olarak ancak sonra kadar ise".split()),
    "id": frozenset("yang dan di dari untuk dengan pada ini itu tidak akan adalah dalam oleh atau juga sudah bisa".split()),
    "vi": frozenset("và của có là các được trong cho không người một này với đã như để về khi từ".split()),
    "ru": frozenset("и в не на что с он как а то все она так его но да ты к у же вы за бы по только ее мне было вот".split()),
    "uk": frozenset("і в на що з до як за це той але не або він вона вони ми ви так".split()),
    "pl": frozenset("nie się jest na w z do że o za po jak ale tym jego przez tylko".split()),
    "ja": frozenset("の に は を た が で て と し れ さ ある いる も する から な こと として".split()),
    "zh": frozenset("的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己 这".split()),
    "ko": frozenset("이 그 저 것 수 등 및 에서 그리고 하지만 또는 있는 있다 없는".split()),
}

#: Character n-grams that are strong hints when stopword hits are thin.
_NGRAM_HINTS: dict[str, frozenset[str]] = {
    "de": frozenset("ß ä ö ü".split()),
    "tr": frozenset("ğ ı ş İ".split()),
    "pl": frozenset("ł ę ą ć ź ż ń ś".split()),
    "pt": frozenset("ã õ ç".split()),
    "es": frozenset("ñ ¿ ¡".split()),
    "fr": frozenset("ç œ è ê à ù".split()),
    "vi": frozenset("ơ ư ạ ả ấ ầ ẩ ẫ ậ ắ ằ ẳ ẵ ặ ẹ ẻ ẽ ế ề ể ễ ệ ỉ ị ọ ỏ ố ồ ổ ỗ ộ ớ ờ ở ỡ ợ ụ ủ ứ ừ ử ữ ự ỳ ỵ ỷ ỹ".split()),
}

#: Cyrillic letters unique enough to separate ru / uk / bg.
_CYRILLIC_DISCRIMINATORS: dict[str, frozenset[str]] = {
    "ru": frozenset("ыэъё"),
    "uk": frozenset("іїєґ"),
    "bg": frozenset("ъщ"),
}

#: Arabic-script letters separating ar / fa / ur.
_ARABIC_DISCRIMINATORS: dict[str, frozenset[str]] = {
    "fa": frozenset("پچژگ"),
    "ur": frozenset("ٹڈڑںھے"),
}

_ISO_639_1 = frozenset(STOPWORDS)

# ── fasttext (lazy, optional) ────────────────────────────────────────────────

_MODEL: Any = None
_MODEL_TRIED = False


def _fasttext_model(model_path: str | None = None) -> Any:
    """Load the fasttext language-ID model once, lazily.

    Args:
        model_path: path to a ``lid176.ftz`` model. Defaults to
            ``FASTTEXT_LID_MODEL`` then a conventional ``models/`` path.

    Returns:
        The loaded model, or ``None`` when fasttext or the model file is absent.
    """
    global _MODEL, _MODEL_TRIED
    import os

    if _MODEL_TRIED and _MODEL is not None:
        return _MODEL
    _MODEL_TRIED = True
    path = model_path or os.getenv("FASTTEXT_LID_MODEL", "") or "models/lid176.ftz"
    if not os.path.isfile(path):
        return None
    try:
        import importlib.util

        # The `fasttext` PyPI wheel installs a top-level package that shadows
        # the C++ stdlib header name; load it by file location to sidestep any
        # half-installed/ABI-mismatched import.
        spec = importlib.util.find_spec("fasttext")
        if spec is None or not spec.origin:
            return None
        import importlib.machinery

        loader = importlib.machinery.SourceFileLoader("signal_fasttext", spec.origin)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _MODEL = mod.load_model(path)
        return _MODEL
    except Exception:  # noqa: BLE001 — any fasttext problem degrades to heuristic
        _MODEL = None
        return None


def fasttext_available() -> bool:
    """``True`` when the fasttext LID model can actually be loaded."""
    return _fasttext_model() is not None


def fasttext_reset() -> None:
    """Drop the cached model so the next call re-probes (used by tests)."""
    global _MODEL, _MODEL_TRIED
    _MODEL, _MODEL_TRIED = None, False


# ── Heuristic engine ─────────────────────────────────────────────────────────

def _script_votes(text: str) -> dict[str, int]:
    """Count characters per Unicode script.

    Args:
        text: input text.

    Returns:
        Mapping of script name → character count.
    """
    votes: dict[str, int] = {}
    for ch in text:
        cp = ord(ch)
        if ch.isspace() or not ch.isalpha():
            continue
        for name, lo, hi in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                votes[name] = votes.get(name, 0) + 1
                break
    return votes


def _words(text: str) -> list[str]:
    """Lowercased word tokens (letters only, any script)."""
    norm = unicodedata.normalize("NFKC", text).lower()
    return re.findall(r"[^\W\d_]{1,}", norm, re.UNICODE)


def _heuristic(text: str) -> dict[str, Any]:
    """Score languages by script + stopword + character evidence.

    Args:
        text: input text (must be non-empty).

    Returns:
        ``{"language", "confidence", "scores", "script", "engine",
        "alternatives"}``.
    """
    votes = _script_votes(text)
    total_alpha = sum(votes.values()) or 1
    script = max(votes, key=lambda k: votes[k]) if votes else "unknown"

    scores: dict[str, float] = {}

    # ── Script gate ──────────────────────────────────────────────────────────
    if script in ("hiragana", "katakana"):
        return {"language": "ja", "confidence": 0.97, "scores": {"ja": 0.97},
                "script": "kana", "engine": "heuristic", "alternatives": []}
    if _KANA_RE.search(text):
        return {"language": "ja", "confidence": 0.95, "scores": {"ja": 0.95},
                "script": "kana", "engine": "heuristic", "alternatives": []}
    if _HANGUL_RE.search(text):
        return {"language": "ko", "confidence": 0.95, "scores": {"ko": 0.95},
                "script": "hangul", "engine": "heuristic", "alternatives": []}

    # A script that is unique to one language wins outright, but we still let
    # stopwords add confidence (Arabic has fa/ur; Cyrillic has ru/uk/bg).
    for lang, chars in _CYRILLIC_DISCRIMINATORS.items():
        if any(c in text.lower() for c in chars):
            scores[lang] = scores.get(lang, 0.0) + 0.6
    for lang, chars in _ARABIC_DISCRIMINATORS.items():
        if any(c in text for c in chars):
            scores[lang] = scores.get(lang, 0.0) + 0.6

    if script == "cjk":
        # Han with no kana: Chinese. Split ja/zh by which stopword set hits —
        # Japanese uses kana heavily so its absence is strong evidence.
        zh_hits = len(set(_words(text)) & STOPWORDS["zh"])
        scores["zh"] = scores.get("zh", 0.0) + 0.5 + min(0.3, 0.1 * zh_hits)
        scores["ja"] = scores.get("ja", 0.0) + 0.05
    elif script in _SCRIPT_LANG and script not in ("cyrillic", "cyrillic_ext", "arabic", "arabic_ext", "devanagari"):
        scores[_SCRIPT_LANG[script]] = scores.get(_SCRIPT_LANG[script], 0.0) + 0.55
    elif script in ("cyrillic", "cyrillic_ext"):
        scores["ru"] = scores.get("ru", 0.0) + 0.4
    elif script in ("arabic", "arabic_ext"):
        scores["ar"] = scores.get("ar", 0.0) + 0.4
    elif script == "devanagari":
        scores["hi"] = scores.get("hi", 0.0) + 0.45
        scores["mr"] = scores.get("mr", 0.0) + 0.25
        scores["ne"] = scores.get("ne", 0.0) + 0.2

    # ── Stopword evidence ───────────────────────────────────────────────────
    tokens = _words(text)
    token_set = set(tokens)
    stop_scores: dict[str, int] = {}
    for lang, words in STOPWORDS.items():
        if lang not in _ISO_639_1:
            continue
        hits = len(token_set & words)
        if hits:
            stop_scores[lang] = hits
            scores[lang] = scores.get(lang, 0.0) + min(0.6, 0.22 * hits)

    # ── Character n-gram hints ──────────────────────────────────────────────
    lowered = text.lower()
    for lang, chars in _NGRAM_HINTS.items():
        n = sum(1 for c in chars if c in lowered)
        if n:
            scores[lang] = scores.get(lang, 0.0) + min(0.3, 0.08 * n)

    # Script coherence bonus: a language whose script matches the dominant one
    # is far more plausible than one that does not.
    script_lang_family = {
        "latin": STOPWORDS.keys(), "latin_ext": STOPWORDS.keys(),
        "cyrillic": ("ru", "uk", "bg"), "cyrillic_ext": ("ru", "uk", "bg"),
        "arabic": ("ar", "fa", "ur"), "arabic_ext": ("ar", "fa", "ur"),
        "devanagari": ("hi", "mr", "ne"),
    }
    family = script_lang_family.get(script)
    if family:
        share = votes.get(script, 0) / total_alpha
        for lang in family:
            if lang in scores:
                scores[lang] += 0.45 * share

    if not scores:
        return {"language": "und", "confidence": 0.0, "scores": {},
                "script": script, "engine": "heuristic", "alternatives": []}

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best_lang, best_score = ranked[0]
    runner = ranked[1][1] if len(ranked) > 1 else 0.0
    # Confidence: absolute strength, tempered when a runner-up is close.
    margin = (best_score - runner) / best_score if best_score else 0.0
    confidence = round(min(0.95, 0.35 + 0.45 * min(1.0, best_score / 1.2) + 0.2 * margin), 3)

    alternatives = [{"language": l, "score": round(s, 3)} for l, s in ranked[1:4]]
    return {"language": best_lang, "confidence": confidence,
            "scores": {l: round(s, 3) for l, s in ranked[:5]},
            "script": script, "engine": "heuristic", "alternatives": alternatives}


# ── Public API ───────────────────────────────────────────────────────────────

MIN_SAMPLE_CHARS = 12


def detect(text: str, *, min_chars: int = MIN_SAMPLE_CHARS,
           use_fasttext: bool = True) -> dict[str, Any]:
    """Detect the language of *text*.

    Args:
        text: the sample to classify.
        min_chars: below this length the verdict is ``und`` (too little signal).
        use_fasttext: attempt the fasttext engine first when its model is
            present. Disable to force the pure-stdlib heuristic.

    Returns:
        ``{"language", "confidence", "script", "engine", "scores",
        "alternatives", "chars", "error"}``. ``language`` is an ISO-639-1 code
        or ``"und"``. Never raises.
    """
    out: dict[str, Any] = {
        "language": "und", "confidence": 0.0, "script": "unknown",
        "engine": "none", "scores": {}, "alternatives": [],
        "chars": 0, "error": None,
    }
    if not isinstance(text, str) or not text.strip():
        out["error"] = "empty text"
        return out

    stripped = text.strip()
    out["chars"] = len(stripped)
    letters = [c for c in stripped if c.isalpha()]
    if len(letters) < 3:
        out["error"] = "no alphabetic content"
        return out

    if use_fasttext and len(stripped) >= min_chars:
        model = _fasttext_model()
        if model is not None:
            try:
                labels, probs = model.predict(stripped.replace("\n", " "))
                if labels:
                    lang = str(labels[0]).replace("__label__", "")[:2]
                    conf = float(probs[0]) if len(probs) else 0.0
                    votes = _script_votes(stripped)
                    out.update({"language": lang, "confidence": round(conf, 3),
                                "script": max(votes, key=lambda k: votes[k]) if votes else "unknown",
                                "engine": "fasttext", "scores": {lang: round(conf, 3)}})
                    return out
            except Exception as exc:  # noqa: BLE001
                out["error"] = f"fasttext failed: {exc}"

    heuristic = _heuristic(stripped)
    out.update({k: heuristic[k] for k in
                ("language", "confidence", "script", "engine", "scores", "alternatives")})
    return out


def detect_many(texts: list[str], **kw: Any) -> list[dict[str, Any]]:
    """Run :func:`detect` over a list, preserving order.

    Args:
        texts: samples to classify.
        **kw: forwarded to :func:`detect`.

    Returns:
        One result dict per input, in the same order.
    """
    return [detect(t, **kw) for t in texts]


def dominant_language(texts: list[str], **kw: Any) -> dict[str, Any]:
    """Aggregate language share across several samples.

    Args:
        texts: samples to classify (blank entries ignored).
        **kw: forwarded to :func:`detect`.

    Returns:
        ``{"language", "confidence", "distribution", "samples", "engine"}``.
    """
    results = [r for r in detect_many(texts, **kw) if r["language"] != "und"]
    if not results:
        return {"language": "und", "confidence": 0.0, "distribution": {},
                "samples": 0, "engine": "none"}
    dist: dict[str, float] = {}
    for r in results:
        dist[r["language"]] = dist.get(r["language"], 0.0) + r["confidence"]
    total = sum(dist.values()) or 1.0
    dist = {k: round(v / total, 3) for k, v in dist.items()}
    ranked = sorted(dist.items(), key=lambda kv: -kv[1])
    return {"language": ranked[0][0], "confidence": ranked[0][1],
            "distribution": dict(ranked), "samples": len(results),
            "engine": results[0]["engine"]}


def language_name(code: str) -> str:
    """Human-readable name for an ISO-639-1 code.

    Args:
        code: two-letter code (or ``"und"``).

    Returns:
        An English language name, or the code itself when unknown.
    """
    try:
        import pycountry  # optional; absent in the base venv
        found = pycountry.languages.get(alpha_2=(code or "")[:2])
        if found:
            return found.name
    except Exception:  # noqa: BLE001
        pass
    return {
        "en": "English", "es": "Spanish", "fr": "French", "de": "German",
        "pt": "Portuguese", "it": "Italian", "nl": "Dutch", "tr": "Turkish",
        "id": "Indonesian", "vi": "Vietnamese", "ru": "Russian", "uk": "Ukrainian",
        "bg": "Bulgarian", "pl": "Polish", "ja": "Japanese", "zh": "Chinese",
        "ko": "Korean", "ar": "Arabic", "fa": "Persian", "ur": "Urdu",
        "hi": "Hindi", "mr": "Marathi", "ne": "Nepali", "el": "Greek",
        "he": "Hebrew", "th": "Thai", "und": "undetermined",
    }.get((code or "")[:2], code or "und")


__all__ = [
    "STOPWORDS", "MIN_SAMPLE_CHARS", "detect", "detect_many",
    "dominant_language", "language_name", "fasttext_available",
    "fasttext_reset",
]
