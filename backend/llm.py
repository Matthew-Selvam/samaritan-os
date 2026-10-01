"""
llm.py — Single entry point for every model call
=================================================
Signal-OS talks to model providers *only* through this module. No agent,
connector or route may construct a provider client directly.

Providers (first configured one wins, automatic failover on error):
    openai     -> ``/v1/chat/completions``   (also serves DeepSeek + OpenRouter)
    anthropic  -> ``/v1/messages``           (native API)
    deepseek   -> OpenAI-compatible endpoint, own key
    openrouter -> OpenAI-compatible endpoint, own key
    ollama     -> local ``/api/chat``        (no key; reachability probe)

Design invariants
-----------------
1. **Never raise when no provider is configured.** ``complete()`` returns an
   ``LLMResult`` whose ``error`` is set and whose ``text`` is a clearly-marked
   deterministic (non-AI) answer. Agents branch on ``result.error`` and take
   their existing deterministic path. The platform must work with zero keys.
2. **Circuit-break** each provider after ``CIRCUIT_FAIL_THRESHOLD``
   consecutive failures for ``CIRCUIT_RESET_AFTER`` seconds, then probe again.
3. **Total timeout is enforced per call** across the whole failover walk.
4. **Never log prompt or response bodies** — model name + token counts only.
5. httpx is the only HTTP dependency; the openai/anthropic SDKs are *not*
   required. Provider modules are constructed lazily so import stays cheap.

Environment (all optional)
--------------------------
``LLM_PROVIDER_ORDER``   comma list, default openai,anthropic,deepseek,openrouter,ollama
``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``OPENAI_MODEL``
``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_MODEL``
``DEEPSEEK_API_KEY`` / ``DEEPSEEK_BASE_URL`` / ``DEEPSEEK_MODEL``
``OPENROUTER_API_KEY`` / ``OPENROUTER_BASE_URL`` / ``OPENROUTER_MODEL``
``OLLAMA_URL`` (default http://localhost:11434) / ``OLLAMA_MODEL``
``LLM_MAX_TOKENS`` (default 2048) / ``LLM_DEFAULT_TIMEOUT`` (default 45)
``CIRCUIT_FAIL_THRESHOLD`` (default 3) / ``CIRCUIT_RESET_AFTER`` (default 30)
``LLM_ROUTER_THRESHOLD`` (default 0.8) — heuristic routing confidence below
which the smart router asks the model for a second opinion.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable

__all__ = [
    "LLMUnavailable",
    "LLMResult",
    "LLM",
    "Provider",
    "OpenAIProvider",
    "AnthropicProvider",
    "OllamaProvider",
    "get_llm",
    "close_llm",
    "parse_json_loose",
    "classify_input",
    "detect_input_type_smart",
    "OFFLINE_MODEL",
    "OFFLINE_NOTICE",
    "IDENTITY_LIMITATION",
]


# ── Small helpers ─────────────────────────────────────────────────────────────

def _logger(name: str = "llm"):
    """Return the project logger, falling back to stdlib logging.

    WS-INFRA owns ``observability.py``; it may not exist yet (or may fail to
    import in a stripped environment), and this module must never break import.

    Args:
        name: Logger name.
    Returns:
        A ``logging.Logger``-compatible object.
    """
    try:
        from observability import get_logger  # type: ignore
        return get_logger(name)
    except Exception:  # noqa: BLE001 — any failure must fall back, never raise
        import logging
        return logging.getLogger(name)


def _log(name: str, msg: str, level: str = "info") -> None:
    """Log a message, swallowing every logging failure.

    Args:
        name: Logger name.
        msg: Message. Must never contain prompt or response bodies.
        level: One of debug/info/warning/error.
    """
    try:
        log = _logger(name)
        fn = getattr(log, level, None) or getattr(log, "info", None)
        if fn:
            fn(msg)
    except Exception:  # noqa: BLE001
        pass


def _env_str(name: str, default: str = "") -> str:
    """Read a stripped env string.

    Args:
        name: Env var name.
        default: Value when unset/blank.
    Returns:
        The env value or ``default``.
    """
    return (os.getenv(name) or "").strip() or default


def _env_int(name: str, default: int) -> int:
    """Read an int env var, tolerating junk.

    Args:
        name: Env var name.
        default: Fallback when unset or unparseable.
    Returns:
        Parsed int or ``default``.
    """
    try:
        return int(float(_env_str(name) or default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float env var, tolerating junk.

    Args:
        name: Env var name.
        default: Fallback when unset or unparseable.
    Returns:
        Parsed float or ``default``.
    """
    try:
        return float(_env_str(name) or default)
    except (TypeError, ValueError):
        return default


def _clip(text: Any, limit: int = 400) -> str:
    """Clamp any value to a single-line string for a body-free log.

    Args:
        text: Value to clamp (str, exception, or ``None``).
        limit: Max characters.
    Returns:
        A single-line string of at most ``limit`` characters.
    """
    if text is None:
        return ""
    return " ".join(str(text).split())[:limit]


# ── Errors + result ───────────────────────────────────────────────────────────

class LLMUnavailable(RuntimeError):
    """Raised only by the low-level provider call path (never by ``complete``).

    The public API converts this into an ``LLMResult`` with ``error`` set so a
    missing/failing provider can never crash a pipeline.
    """


OFFLINE_MODEL = "offline-deterministic"
OFFLINE_NOTICE = (
    "[offline / no-AI] No LLM provider is configured for this deployment, so AI "
    "reasoning is unavailable. Signal-OS fell back to its deterministic, "
    "rule-based path: every fact below was derived locally by pattern matching "
    "and public-source connectors, not by a language model. Treat this as "
    "machine-derived evidence, not analysis. To enable AI synthesis set "
    "OPENAI_API_KEY, ANTHROPIC_API_KEY, DEEPSEEK_API_KEY or OPENROUTER_API_KEY, "
    "or run Ollama locally (OLLAMA_URL)."
)

IDENTITY_LIMITATION = (
    "Identity attribution is out of scope for this pass: no attempt is made to "
    "identify, name, or match a real person from a face, and no biometric "
    "template is derived. Reverse image search, EXIF/GPS metadata and "
    "cross-corroborated account evidence are the legitimate attribution paths."
)


@dataclass
class LLMResult:
    """Outcome of one model call.

    Attributes:
        text: The assistant text (or a deterministic placeholder when offline).
        model: Model identifier used, or ``OFFLINE_MODEL``.
        provider: Provider name, or ``"offline"``.
        prompt_tokens: Prompt tokens as reported (or estimated).
        completion_tokens: Completion tokens as reported (or estimated).
        latency_s: Wall-clock seconds for the call.
        error: ``None`` on success; a human-readable reason otherwise.
    """
    text: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the result came from a real provider (no ``error``)."""
        return not self.error

    @property
    def total_tokens(self) -> int:
        """Sum of prompt + completion tokens."""
        return int(self.prompt_tokens) + int(self.completion_tokens)


# ── Circuit breaker (self-contained; WS-INFRA's circuit.py is not required) ──

class _Circuit:
    """Consecutive-failure breaker with a probe-after-reset window.

    Args:
        name: Circuit name (for logs).
        threshold: Consecutive failures that trip the circuit.
        reset_after: Seconds the circuit stays open before probing again.
    """

    __slots__ = ("name", "threshold", "reset_after", "fails", "opened_at")

    def __init__(self, name: str, threshold: int = 3, reset_after: float = 30.0) -> None:
        self.name = name
        self.threshold = max(1, int(threshold))
        self.reset_after = max(1.0, float(reset_after))
        self.fails = 0
        self.opened_at: float | None = None

    def allow(self) -> bool:
        """Return True when a request may be attempted (half-open allowed)."""
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.reset_after:
            # Reset window elapsed — allow a single probe to close the circuit.
            self.fails = 0
            self.opened_at = None
            return True
        return False

    def record_success(self) -> None:
        """Close the circuit and clear the failure count."""
        self.fails = 0
        self.opened_at = None

    def record_failure(self) -> None:
        """Count a failure and trip the circuit once the threshold is hit."""
        self.fails += 1
        if self.fails >= self.threshold and self.opened_at is None:
            self.opened_at = time.monotonic()
            _log("llm", f"circuit OPEN for provider {self.name} "
                        f"({self.fails} consecutive failures); "
                        f"probing again in {self.reset_after:.0f}s", "warning")

    @property
    def state(self) -> str:
        """``"open"`` while tripped, else ``"closed"``."""
        return "open" if self.opened_at is not None else "closed"

    def snapshot(self) -> dict:
        """Return breaker telemetry (no secrets)."""
        return {"state": self.state, "consecutive_failures": self.fails,
                "reset_after_s": self.reset_after}


# ── Providers ─────────────────────────────────────────────────────────────────

class Provider:
    """Base provider: static config, availability probe, one completion call.

    Subclasses implement ``_post`` (HTTP) plus payload/response shaping. A
    provider is *configured* when its key (or, for Ollama, a plausible URL)
    exists; reachability is confirmed lazily on first use.

    Args:
        name: Provider name as it appears in ``LLMResult.provider``.
        key_env: Env var holding the API key (empty for keyless providers).
        base_url: Default API base URL.
        default_model: Model used when the caller does not name one.
        model_env: Env var overriding ``default_model``.
        base_url_env: Env var overriding ``base_url``.
    """

    name = "provider"
    key_env = ""
    base_url = ""
    default_model = ""
    model_env = ""
    base_url_env = ""
    vision = False

    def __init__(self) -> None:
        self.circuit = _Circuit(
            self.name,
            threshold=_env_int("CIRCUIT_FAIL_THRESHOLD", 3),
            reset_after=_env_float("CIRCUIT_RESET_AFTER", 30.0),
        )
        self.key = _env_str(self.key_env) if self.key_env else ""
        self.base_url = (_env_str(self.base_url_env) or self.base_url).rstrip("/")
        self.model = _env_str(self.model_env) if self.model_env else ""
        self.model = self.model or self.default_model
        self._probe: bool | None = None   # None = not probed yet
        self._tags: list[dict] = []        # Ollama /api/tags snapshot

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        """True when the provider has the static config it needs."""
        return bool(self.key) if self.key_env else bool(self.base_url)

    def models(self) -> list[str]:
        """Model identifiers this provider is currently willing to serve."""
        return [self.model] if self.model else []

    def accepts_model(self, model: str | None) -> bool:
        """Return True when ``model`` can be served by this provider.

        A model hint that clearly belongs to a different vendor is refused so a
        caller (e.g. ``BaseAgent.preferred_models``) can never force a wrong
        model onto a provider; the provider default is used instead.

        Args:
            model: Model identifier or ``None``.
        Returns:
            True when compatible.
        """
        if not model:
            return True
        low = model.lower()
        if "/" in model:                      # openrouter-style "vendor/model"
            return low.split("/", 1)[0] in self.vendor_prefixes
        return any(k in low for k in self.vendor_markers)

    vendor_prefixes: tuple[str, ...] = ()
    vendor_markers: tuple[str, ...] = ()

    def health(self) -> dict:
        """Return provider telemetry (never includes the API key).

        ``configured`` is the static answer (key present, or a base URL for a
        keyless provider). ``reachable`` is the live answer for keyless
        providers and is ``None`` until the endpoint has actually been probed,
        so a sync caller is never misled into thinking a dead local daemon is
        usable.
        """
        return {
            "configured": self.configured,
            "ready": self.ready,
            "reachable": self.reachable,
            "model": self.model,
            "base_url": self.base_url,
            "vision": self.vision,
            "circuit": self.circuit.snapshot(),
        }

    @property
    def ready(self) -> bool:
        """True when this provider could plausibly serve a call right now.

        Key-based providers are ready as soon as a key exists. Keyless ones
        (Ollama) are only ready once their endpoint has been probed reachable.
        """
        if not self.configured:
            return False
        if self.key_env:
            return True
        return self._probe is True

    @property
    def reachable(self) -> bool | None:
        """Live reachability for keyless providers (``None`` = unprobed)."""
        if self.key_env:
            return bool(self.configured) or None
        return self._probe

    # ── Availability ──────────────────────────────────────────────────────────

    async def available(self) -> bool:
        """Return True when the provider is configured and not tripped.

        Keyless providers (Ollama) probe their endpoint once and cache the
        answer so a down local daemon costs one short timeout per process, not
        one per call.
        """
        if not self.configured:
            return False
        if not self.circuit.allow():
            return False
        if self.key_env:
            return True
        if self._probe is None:
            self._probe = await self._probe_endpoint()
        if not self._probe:
            # Count it against the breaker so repeated calls stop paying the probe.
            self.circuit.record_failure()
        return bool(self._probe)

    async def _probe_endpoint(self) -> bool:
        """Cheap reachability probe. Overridden by keyless providers."""
        return True

    # ── Completion ────────────────────────────────────────────────────────────

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None,
        model: str | None,
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> LLMResult:
        """Run one completion against this provider.

        Args:
            prompt: User prompt.
            system: Optional system prompt.
            model: Optional model override (ignored when incompatible).
            temperature: Sampling temperature.
            max_tokens: Completion budget.
            timeout: Seconds allowed for this single attempt.
        Returns:
            An ``LLMResult`` with ``error`` set on any failure.
        """
        t0 = time.monotonic()
        use_model = model if self.accepts_model(model) else None
        try:
            payload, headers = self._build(prompt, system, use_model or self.model,
                                           temperature, max_tokens)
            data = await self._post(self._completions_url(), payload, headers, timeout)
            text, pt, ct = self._parse(data)
            self.circuit.record_success()
            return LLMResult(
                text=text, model=use_model or self.model, provider=self.name,
                prompt_tokens=pt, completion_tokens=ct,
                latency_s=round(time.monotonic() - t0, 3), error=None,
            )
        except Exception as exc:  # noqa: BLE001 — provider isolation
            self.circuit.record_failure()
            return LLMResult(
                text="", model=self.model, provider=self.name,
                latency_s=round(time.monotonic() - t0, 3),
                error=f"{self.name}: {type(exc).__name__}: {_clip(exc, 200)}",
            )

    async def _post(self, url: str, payload: dict, headers: dict,
                    timeout: float) -> dict:
        """POST a request through the owning facade's shared HTTP client.

        Delegates to ``LLM._post_json`` so every provider shares one connection
        pool and one error-mapping path. Falls back to a throwaway client if no
        facade is registered (e.g. a provider constructed directly in a test).

        Args:
            url: Absolute endpoint URL.
            payload: JSON body.
            headers: Request headers.
            timeout: Seconds before aborting.
        Returns:
            The decoded JSON body.
        """
        facade = _FACADE[0] if _FACADE else None
        if facade is not None:
            return await facade._post_json(url, payload, headers, timeout)
        return await _standalone_post(url, payload, headers, timeout)

    async def stream(
        self,
        prompt: str,
        *,
        system: str | None,
        model: str | None,
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> AsyncIterator[str]:
        """Yield text chunks. Default: a single chunk of the full completion."""
        result = await self.complete(prompt, system=system, model=model,
                                     temperature=temperature,
                                     max_tokens=max_tokens, timeout=timeout)
        if result.error:
            raise LLMUnavailable(result.error)
        yield result.text

    # ── HTTP plumbing (httpx only) ────────────────────────────────────────────

    def _completions_url(self) -> str:
        """Return the HTTP endpoint for a non-streaming completion."""
        return f"{self.base_url}/chat/completions"

    def _build(
        self, prompt: str, system: str | None, model: str,
        temperature: float, max_tokens: int,
    ) -> tuple[dict, dict]:
        """Build the JSON body + headers for a completion.

        Args:
            prompt: User prompt.
            system: Optional system prompt.
            model: Resolved model id.
            temperature: Sampling temperature.
            max_tokens: Completion budget.
        Returns:
            ``(payload, headers)``.
        """
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        return payload, self._headers()

    def _headers(self) -> dict:
        """Return auth + content headers. Never logged."""
        return {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    def _parse(self, data: dict) -> tuple[str, int, int]:
        """Extract ``(text, prompt_tokens, completion_tokens)`` from a response.

        Args:
            data: Decoded JSON body.
        Returns:
            The text plus token counts.
        Raises:
            LLMUnavailable: When the body has no usable text.
        """
        choices = data.get("choices") or []
        if not choices:
            raise LLMUnavailable("empty choices")
        msg = choices[0].get("message") or {}
        text = msg.get("content") or choices[0].get("text") or ""
        if not isinstance(text, str) or not text.strip():
            raise LLMUnavailable("empty message content")
        usage = data.get("usage") or {}
        return text, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


class OpenAIProvider(Provider):
    """OpenAI ``/v1/chat/completions`` — also serves DeepSeek and OpenRouter.

    Args:
        name: Provider name (``openai``/``deepseek``/``openrouter``).
        key_env: API key env var.
        base_url: Default base URL.
        default_model: Default model id.
        model_env: Model override env var.
        base_url_env: Base URL override env var.
        vision: Whether the default model accepts images.
    """
    name = "openai"
    key_env = "OPENAI_API_KEY"
    base_url = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"
    model_env = "OPENAI_MODEL"
    base_url_env = "OPENAI_BASE_URL"
    vision = True
    vendor_prefixes = ("openai",)
    vendor_markers = ("gpt-", "gpt3", "gpt4", "o1-", "o3-", "o4-", "chatgpt")


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek — OpenAI-compatible endpoint with its own key."""

    name = "deepseek"
    key_env = "DEEPSEEK_API_KEY"
    base_url = "https://api.deepseek.com/v1"
    default_model = "deepseek-chat"
    model_env = "DEEPSEEK_MODEL"
    base_url_env = "DEEPSEEK_BASE_URL"
    vision = False
    vendor_prefixes = ("deepseek",)
    vendor_markers = ("deepseek",)


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter — OpenAI-compatible aggregator (``vendor/model`` ids)."""

    name = "openrouter"
    key_env = "OPENROUTER_API_KEY"
    base_url = "https://openrouter.ai/api/v1"
    default_model = "anthropic/claude-3.5-sonnet"
    model_env = "OPENROUTER_MODEL"
    base_url_env = "OPENROUTER_BASE_URL"
    vision = True
    vendor_prefixes = ()          # OpenRouter serves every vendor
    vendor_markers = ("/",)       # only the explicit "vendor/model" form matches

    def accepts_model(self, model: str | None) -> bool:
        """Accept any explicit model id, including ``vendor/model``."""
        return True

    def _headers(self) -> dict:
        """Add the optional OpenRouter attribution headers."""
        base = super()._headers()
        base["HTTP-Referer"] = "https://signal-os.local"
        base["X-Title"] = "Signal-OS"
        return base


class AnthropicProvider(Provider):
    """Anthropic native ``/v1/messages`` API.

    ``max_tokens`` is mandatory for this API, so a provider default is used
    when the caller omits it.
    """

    name = "anthropic"
    key_env = "ANTHROPIC_API_KEY"
    base_url = "https://api.anthropic.com"
    default_model = "claude-sonnet-4-5"
    model_env = "ANTHROPIC_MODEL"
    base_url_env = "ANTHROPIC_BASE_URL"
    vision = True
    vendor_prefixes = ("anthropic",)
    vendor_markers = ("claude",)

    def _completions_url(self) -> str:
        """Anthropic's native messages endpoint."""
        return f"{self.base_url}/v1/messages"

    def _headers(self) -> dict:
        """Anthropic uses ``x-api-key`` + a pinned API version, not Bearer."""
        return {
            "x-api-key": self.key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _build(
        self, prompt: str, system: str | None, model: str,
        temperature: float, max_tokens: int,
    ) -> tuple[dict, dict]:
        """Build a ``/v1/messages`` body (system is a top-level field)."""
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        return payload, self._headers()

    def _parse(self, data: dict) -> tuple[str, int, int]:
        """Concatenate ``content`` text blocks and read usage."""
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict) and b.get("type") == "text")
        if not text.strip():
            raise LLMUnavailable("empty content blocks")
        usage = data.get("usage") or {}
        return (text,
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0))


class OllamaProvider(Provider):
    """Local Ollama ``/api/chat`` — keyless, probed once per process.

    The configured ``OLLAMA_MODEL`` is not assumed to be installed: the probe
    reads ``/api/tags`` and picks a suitable installed model when the
    configured one is missing, so a developer machine with a different model
    set still gets a working local AI tier without configuration. Set
    ``OLLAMA_MODEL`` to pin a specific model and disable the auto-selection.
    """

    name = "ollama"
    key_env = ""
    base_url = "http://localhost:11434"
    default_model = "qwen2.5:7b"
    model_env = "OLLAMA_MODEL"
    base_url_env = "OLLAMA_URL"
    vision = True
    vendor_prefixes = ("ollama",)
    vendor_markers = (":",)   # "llama3.1:8b" style tags
    probe_timeout = 2.0

    # Preference order when auto-selecting an installed model: chat models
    # first, then anything with the completion capability, smallest first.
    preferred_family_prefixes: tuple[str, ...] = (
        "qwen2.5", "qwen3", "llama3.1", "llama3.2", "llama3", "mistral",
        "phi3", "gemma2", "gemma3", "hermes", "gpt-oss",
    )

    def _completions_url(self) -> str:
        """Ollama's native chat endpoint."""
        return f"{self.base_url}/api/chat"

    def _headers(self) -> dict:
        """No auth for a local daemon."""
        return {"Content-Type": "application/json"}

    async def _probe_endpoint(self) -> bool:
        """Read ``/api/tags`` and reconcile the configured model.

        Returns:
            True when the daemon answered and a usable chat model was found.
        """
        try:
            import httpx
            async with httpx.AsyncClient(timeout=self.probe_timeout,
                                         trust_env=False) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                if r.status_code >= 500:
                    return False
                tags = (r.json() or {}).get("models") or []
        except Exception as exc:  # noqa: BLE001
            _log("llm", f"ollama probe failed: {type(exc).__name__}", "debug")
            return False

        self._tags = [
            {**m, "_name": m.get("name") or m.get("model")}
            for m in tags if m.get("name") or m.get("model")
        ]
        chat = [m for m in self._tags
                if "embedding" not in (m.get("capabilities") or [])]
        if not chat:
            return False

        names = [m["_name"] for m in chat]
        pinned = _env_str(self.model_env) if self.model_env else ""
        if pinned and pinned in names:
            self.model = pinned
            return True
        if self.model in names:
            return True
        if pinned:
            # An explicit pin that isn't installed: honour the pin, let the
            # call fail loudly rather than silently using a different model.
            return True

        self.model = self._pick_model(names)
        _log("llm", f"ollama auto-selected installed model={self.model} "
                    f"(configured {self.default_model} is not installed; "
                    f"set {self.model_env} to pin)", "info")
        return True

    def _pick_model(self, names: list[str]) -> str:
        """Choose the best installed chat model.

        Args:
            names: Installed model names.
        Returns:
            The chosen model name (falls back to the first name).
        """
        for prefix in self.preferred_family_prefixes:
            for n in names:
                if n.lower().startswith(prefix):
                    return n
        return names[0]

    def vision_models(self) -> list[str]:
        """Return installed models that advertise the ``vision`` capability.

        Returns:
            Model names supporting image input (empty when none are installed).
        """
        return [m["_name"] for m in self._tags
                if "vision" in (m.get("capabilities") or [])]

    def pick_vision_model(self) -> str | None:
        """Return the installed model to use for image input.

        Falls back to the active chat model when Ollama reports no
        ``vision`` capability (older daemons omit the field), because some
        models accept images regardless of the reported metadata.

        Returns:
            A model name, or ``None`` when the daemon is unreachable.
        """
        if self._probe is not True:
            return None
        return self.vision_models()[0] if self.vision_models() else self.model or None

    def models(self) -> list[str]:
        """Ollama's active model, if the env did not blank it out."""
        return [self.model] if self.model else []

    def _build(
        self, prompt: str, system: str | None, model: str,
        temperature: float, max_tokens: int,
    ) -> tuple[dict, dict]:
        """Build an Ollama ``/api/chat`` body (``options`` holds sampling)."""
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        return payload, self._headers()

    def _parse(self, data: dict) -> tuple[str, int, int]:
        """Read ``message.content`` and the eval counters."""
        msg = data.get("message") or {}
        text = msg.get("content") or ""
        if not text.strip():
            raise LLMUnavailable("empty message content")
        return (text,
                int(data.get("prompt_eval_count") or 0),
                int(data.get("eval_count") or 0))


_PROVIDER_TYPES: dict[str, type[Provider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "deepseek": DeepSeekProvider,
    "openrouter": OpenRouterProvider,
    "ollama": OllamaProvider,
}
DEFAULT_PROVIDER_ORDER = "openai,anthropic,deepseek,openrouter,ollama"


# ── Loose JSON parsing ────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def _outermost_block(text: str) -> str | None:
    """Return the outermost balanced ``{...}`` or ``[...]`` block in ``text``.

    Scans with a bracket-depth counter and skips braces that appear inside a
    JSON string literal, so prose like ``"the value is {x}"`` is not mistaken
    for a payload.

    Args:
        text: Candidate text.
    Returns:
        The block substring, or ``None`` when there is none.
    """
    openers = {"{": "}", "[": "]"}
    best: str | None = None
    for start, ch in enumerate(text):
        if ch not in openers:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c in openers:
                depth += 1
            elif c in ("}", "]"):
                depth -= 1
                if depth == 0:
                    block = text[start:i + 1]
                    if best is None or len(block) > len(best):
                        best = block
                    break
    return best


def parse_json_loose(text: str) -> Any:
    """Parse JSON out of a model response, tolerating common decoration.

    Strips markdown fences and prose, then extracts the outermost JSON object
    or array. A trailing comma before a closing bracket is repaired because
    smaller models emit it often.

    Args:
        text: Raw model text.
    Returns:
        The decoded object/array, or ``None`` when nothing parsed.
    """
    if not text:
        return None
    candidates: list[str] = []
    for match in _FENCE_RE.finditer(text):
        block = match.group(1).strip()
        if block:
            candidates.append(block)
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    block = _outermost_block(text)
    if block:
        candidates.append(block)

    for cand in candidates:
        for attempt in (cand, re.sub(r",(\s*[}\]])", r"\1", cand)):
            try:
                return json.loads(attempt)
            except (ValueError, TypeError):
                continue
    return None


# ── The LLM facade ────────────────────────────────────────────────────────────

class LLM:
    """Provider-routing facade: failover, circuit breaking, timeouts, JSON.

    Args:
        order: Optional provider-name order override (defaults to
            ``LLM_PROVIDER_ORDER`` then the documented default chain).
    """

    def __init__(self, order: Iterable[str] | None = None) -> None:
        names = list(order) if order else [
            n.strip().lower() for n in _env_str(
                "LLM_PROVIDER_ORDER", DEFAULT_PROVIDER_ORDER).split(",") if n.strip()
        ]
        seen: set[str] = set()
        self.order: list[str] = []
        for n in names:
            if n in _PROVIDER_TYPES and n not in seen:
                seen.add(n)
                self.order.append(n)
        self.providers: dict[str, Provider] = {
            name: _PROVIDER_TYPES[name]() for name in self.order
        }
        self.default_max_tokens = max(256, _env_int("LLM_MAX_TOKENS", 2048))
        self.default_timeout = max(1.0, _env_float("LLM_DEFAULT_TIMEOUT", 45.0))
        self._client: Any = None
        self.calls: int = 0
        self.failovers: int = 0
        self.offline_calls: int = 0

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _http(self) -> Any:
        """Return (and lazily build) the shared httpx client.

        Returns:
            An ``httpx.AsyncClient``.
        """
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(timeout=None, trust_env=True)
        return self._client

    def _model_order(self, model: str | None) -> list[str]:
        """Reorder providers so a hinted model's vendor is tried first.

        Args:
            model: Optional model hint from the caller.
        Returns:
            A provider-name list, hint-matching provider first.
        """
        if not model:
            return list(self.order)
        for name in self.order:
            if self.providers[name].accepts_model(model) and (
                "/" in model or any(k in model.lower()
                                    for k in self.providers[name].vendor_markers)
            ):
                return [name] + [n for n in self.order if n != name]
        return list(self.order)

    async def _dispatch(
        self, prompt: str, *, system: str | None, model: str | None,
        temperature: float, max_tokens: int, timeout: float,
    ) -> LLMResult:
        """Walk the provider chain, honouring one total timeout.

        Args:
            prompt: User prompt.
            system: Optional system prompt.
            model: Optional model hint.
            temperature: Sampling temperature.
            max_tokens: Completion budget.
            timeout: Total seconds for the whole failover walk.
        Returns:
            The first successful ``LLMResult``, or an offline/error result.
        """
        t0 = time.monotonic()
        self.calls += 1
        order = self._model_order(model)
        errors: list[str] = []
        skipped: list[str] = []

        for idx, name in enumerate(order):
            provider = self.providers.get(name)
            if provider is None:
                continue
            remaining = timeout - (time.monotonic() - t0)
            if remaining <= 0.5:
                errors.append("total timeout exhausted before failover")
                break
            if not await provider.available():
                skipped.append(name)
                continue
            # Reserve enough budget for the providers still to be tried.
            share = remaining if idx == len(order) - 1 else max(
                3.0, remaining / max(1, len(order) - idx)
            )
            result = await provider.complete(
                prompt, system=system, model=model, temperature=temperature,
                max_tokens=max_tokens, timeout=min(remaining, share),
            )
            if not result.error:
                if idx:
                    self.failovers += idx
                # Body-free telemetry: model + token counts only.
                _log("llm", f"llm call ok provider={result.provider} model={result.model} "
                            f"prompt_tokens={result.prompt_tokens} "
                            f"completion_tokens={result.completion_tokens} "
                            f"latency_s={result.latency_s}")
                return result
            errors.append(result.error or "unknown provider error")
            _log("llm", f"llm provider failed: {result.error}", "warning")

        if not skipped and not errors:
            errors.append("no provider evaluated")

        return self._offline(
            prompt=prompt,
            reason="no LLM provider available — " + "; ".join(errors or skipped or ["none configured"]),
            latency_s=round(time.monotonic() - t0, 3),
            attempted=skipped + [c for c in errors],
        )

    def _offline(self, *, prompt: str, reason: str, latency_s: float,
                 attempted: list[str] | None = None) -> LLMResult:
        """Build the deterministic no-provider result (never raises).

        Args:
            prompt: The original prompt (only its length is used).
            reason: Human-readable failure reason.
            latency_s: Measured latency of the failed walk.
            attempted: Provider names / errors considered.
        Returns:
            An ``LLMResult`` with ``error`` set and templated text.
        """
        self.offline_calls += 1
        # Token estimate only — never the prompt body.
        est = max(1, len(prompt) // 4)
        _log("llm", f"llm offline fallback (reason={_clip(reason, 160)}); "
                    f"estimated_prompt_tokens={est}")
        return LLMResult(
            text=OFFLINE_NOTICE, model=OFFLINE_MODEL, provider="offline",
            prompt_tokens=est, completion_tokens=0, latency_s=latency_s,
            error=reason,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult:
        """Complete a prompt, failing over across providers. Never raises.

        Args:
            prompt: The user prompt.
            system: Optional system prompt.
            model: Optional model hint (vendor-routed when recognised).
            temperature: Sampling temperature.
            max_tokens: Completion budget; defaults to ``LLM_MAX_TOKENS``.
            timeout: Total seconds for the call; defaults to
                ``LLM_DEFAULT_TIMEOUT``.
        Returns:
            An ``LLMResult``. Check ``.error`` — it is set when no provider
            served the call, in which case ``.text`` is a deterministic
            placeholder rather than model output.
        """
        try:
            return await self._dispatch(
                prompt, system=system, model=model, temperature=temperature,
                max_tokens=max(1, int(max_tokens or self.default_max_tokens)),
                timeout=max(1.0, float(timeout or self.default_timeout)),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — absolute last line of defence
            return self._offline(prompt=prompt or "",
                                 reason=f"llm internal error: {type(exc).__name__}",
                                 latency_s=0.0)

    async def complete_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        schema_hint: str | None = None,
        timeout: float | None = None,
    ) -> dict | list:
        """Complete a prompt and parse the reply as JSON. Never raises.

        Asks for bare JSON, parses defensively (markdown fences, prose-wrapped
        blocks, trailing commas), retries once with a stricter instruction on
        a parse failure, and finally returns ``{"error": ..., "raw": ...}``
        rather than raising.

        Args:
            prompt: The user prompt; a JSON-only instruction is appended.
            system: Optional system prompt.
            model: Optional model hint.
            temperature: Sampling temperature.
            max_tokens: Completion budget.
            schema_hint: Example shape, e.g. ``'{"edges": [...]}'``.
            timeout: Total seconds for the call.
        Returns:
            The decoded object/array, or a dict carrying ``error`` and ``raw``.
        """
        instruction = (
            "Respond with a single valid JSON value and nothing else. "
            "No prose, no markdown fences, no comments, no trailing commas."
        )
        if schema_hint:
            instruction += f" The JSON must match this shape: {schema_hint}"
        asked = f"{prompt}\n\n{instruction}"

        first = await self.complete(asked, system=system, model=model,
                                    temperature=temperature,
                                    max_tokens=max_tokens, timeout=timeout)
        if first.error:
            return {"error": first.error, "raw": first.text,
                    "provider": first.provider, "model": first.model}
        parsed = parse_json_loose(first.text)
        if parsed is not None:
            return parsed

        _log("llm", f"json parse failed (model={first.model}, "
                    f"completion_tokens={first.completion_tokens}); retrying once")
        retry = await self.complete(
            f"{prompt}\n\nYour previous reply could not be parsed as JSON. "
            "Return ONLY the raw JSON value, starting with {{ or [ and ending "
            "with }} or ]. No code fence.",
            system=system, model=model, temperature=0.0,
            max_tokens=max_tokens, timeout=timeout,
        )
        if not retry.error:
            parsed = parse_json_loose(retry.text)
            if parsed is not None:
                return parsed
            return {"error": "json_parse_failed", "raw": retry.text,
                    "provider": retry.provider, "model": retry.model}
        return {"error": retry.error, "raw": retry.text}

    async def stream(self, prompt: str, **kw: Any) -> AsyncIterator[str]:
        """Yield text chunks for ``prompt``.

        Tries native streaming on the first available provider; if that path
        fails the call is satisfied from a normal completion and re-chunked, so
        the generator always terminates with the full text (or nothing at all
        when no provider exists — it never raises).

        Args:
            prompt: The user prompt.
            **kw: ``system``, ``model``, ``temperature``, ``max_tokens``,
                ``timeout``, ``chunk_size``.
        Yields:
            Text chunks in order.
        """
        system = kw.pop("system", None)
        model = kw.pop("model", None)
        temperature = float(kw.pop("temperature", 0.2))
        max_tokens = kw.pop("max_tokens", None) or self.default_max_tokens
        timeout = float(kw.pop("timeout", None) or self.default_timeout)
        chunk_size = int(kw.pop("chunk_size", 48) or 48)

        for name in self._model_order(model):
            provider = self.providers.get(name)
            if provider is None or not await provider.available():
                continue
            try:
                async for chunk in provider.stream(
                    prompt, system=system, model=model, temperature=temperature,
                    max_tokens=max_tokens, timeout=timeout,
                ):
                    if chunk:
                        yield chunk
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log("llm", f"stream failed on {name}: {type(exc).__name__}", "warning")

        result = await self.complete(prompt, system=system, model=model,
                                     temperature=temperature,
                                     max_tokens=max_tokens, timeout=timeout)
        if result.error:
            _log("llm", "stream fell through to offline fallback", "debug")
            return
        for i in range(0, len(result.text), chunk_size):
            yield result.text[i:i + chunk_size]

    async def describe_image(
        self,
        image_path: str,
        *,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        max_bytes: int = 6 * 1024 * 1024,
    ) -> LLMResult:
        """Describe a local image with a vision-capable provider.

        Images are inlined as a base64 data URI (no third-party upload, no
        persistence). The image digest is logged instead of the bytes.

        Args:
            image_path: Path to a local image file.
            prompt: What to ask about the image.
            system: Optional system prompt.
            model: Optional vision model hint.
            max_tokens: Completion budget.
            timeout: Total seconds for the call.
            max_bytes: Refuse to inline anything larger than this.
        Returns:
            An ``LLMResult``; ``error`` is set when no *vision* provider is
            configured or the file is unusable.
        """
        t0 = time.monotonic()
        try:
            from pathlib import Path
            path = Path(image_path)
            if not path.is_file():
                return LLMResult("", OFFLINE_MODEL, "offline", error=f"image not found: {path.name}")
            size = path.stat().st_size
            if size > max_bytes:
                return LLMResult("", OFFLINE_MODEL, "offline",
                                 error=f"image too large ({size} > {max_bytes} bytes)")
            raw = path.read_bytes()
        except Exception as exc:  # noqa: BLE001
            return LLMResult("", OFFLINE_MODEL, "offline",
                             error=f"image unreadable: {type(exc).__name__}")

        mime = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
        b64 = base64.b64encode(raw).decode("ascii")
        _log("llm", f"llm vision call image_sha256={hashlib.sha256(raw).hexdigest()[:16]} "
                    f"bytes={size} mime={mime}")

        for name in self._model_order(model):
            provider = self.providers.get(name)
            if provider is None or not getattr(provider, "vision", False):
                continue
            if not await provider.available():
                continue
            try:
                result = await self._vision_call(
                    provider, b64, mime, prompt, system, model,
                    max_tokens or self.default_max_tokens, timeout or self.default_timeout,
                )
                if not result.error:
                    _log("llm", f"llm vision ok provider={result.provider} "
                                f"model={result.model} "
                                f"completion_tokens={result.completion_tokens}")
                    return result
                _log("llm", f"llm vision provider failed: {result.error}", "warning")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log("llm", f"llm vision error on {name}: {type(exc).__name__}", "warning")

        return LLMResult(
            "", OFFLINE_MODEL, "offline", latency_s=round(time.monotonic() - t0, 3),
            error="no vision-capable LLM provider configured "
                  "(set OPENAI_API_KEY / ANTHROPIC_API_KEY / OPENROUTER_API_KEY, "
                  "or run a vision model in Ollama)",
        )

    async def _vision_call(
        self, provider: Provider, b64: str, mime: str, prompt: str,
        system: str | None, model: str | None, max_tokens: int, timeout: float,
    ) -> LLMResult:
        """Send an image + prompt to one vision provider.

        Args:
            provider: The vision provider.
            b64: Base64 image payload.
            mime: Image MIME type.
            prompt: The question about the image.
            system: Optional system prompt.
            model: Optional model hint.
            max_tokens: Completion budget.
            timeout: Seconds for this attempt.
        Returns:
            An ``LLMResult``; ``error`` is set on any failure.
        """
        t0 = time.monotonic()
        use_model = model if provider.accepts_model(model) else None
        if not use_model and provider.name == "ollama":
            # Prefer an installed model that actually advertises vision.
            use_model = getattr(provider, "pick_vision_model", lambda: None)()
        model_id = use_model or provider.model
        try:
            if provider.name == "anthropic":
                payload: dict[str, Any] = {
                    "model": model_id,
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                    "messages": [{"role": "user", "content": [
                        {"type": "image",
                         "source": {"type": "base64", "media_type": mime, "data": b64}},
                        {"type": "text", "text": prompt},
                    ]}],
                }
                if system:
                    payload["system"] = system
                headers = provider._headers()          # noqa: SLF001 — same module
                url = f"{provider.base_url}/v1/messages"
            elif provider.name == "ollama":
                payload = {
                    "model": model_id,
                    "stream": False,
                    "options": {"temperature": 0.2, "num_predict": max_tokens},
                    "messages": [
                        *([{"role": "system", "content": system}] if system else []),
                        {"role": "user", "content": prompt, "images": [b64]},
                    ],
                }
                headers = provider._headers()          # noqa: SLF001
                url = f"{provider.base_url}/api/chat"
            else:
                payload = {
                    "model": model_id,
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                    "messages": [
                        *([{"role": "system", "content": system}] if system else []),
                        {"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url",
                             "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        ]},
                    ],
                }
                headers = provider._headers()          # noqa: SLF001
                url = f"{provider.base_url}/chat/completions"

            data = await self._post_json(url, payload, headers, timeout)
            text, pt, ct = provider._parse(data)      # noqa: SLF001 — same module
            provider.circuit.record_success()
            return LLMResult(text=text, model=model_id, provider=provider.name,
                             prompt_tokens=pt, completion_tokens=ct,
                             latency_s=round(time.monotonic() - t0, 3))
        except Exception as exc:  # noqa: BLE001
            provider.circuit.record_failure()
            return LLMResult("", model_id, provider.name,
                             latency_s=round(time.monotonic() - t0, 3),
                             error=f"{provider.name}: {type(exc).__name__}: "
                                   f"{_clip(exc, 200)}")

    async def _post_json(self, url: str, payload: dict, headers: dict,
                         timeout: float) -> dict:
        """POST JSON with a hard timeout and return the decoded body.

        Args:
            url: Absolute endpoint URL.
            payload: JSON body.
            headers: Request headers.
            timeout: Seconds before aborting.
        Returns:
            The decoded JSON body.
        Raises:
            LLMUnavailable: On transport error, timeout, bad status or bad JSON.
        """
        client = await self._http()
        try:
            response = await client.post(
                url, json=payload, headers=headers,
                timeout=max(1.0, float(timeout)),
            )
        except asyncio.TimeoutError:
            raise LLMUnavailable(f"request timed out after {timeout:.0f}s") from None
        except Exception as exc:  # noqa: BLE001
            raise LLMUnavailable(f"transport error: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise LLMUnavailable(f"HTTP {response.status_code} from provider")
        try:
            return response.json()
        except ValueError:
            raise LLMUnavailable("non-JSON response body") from None

    def available_models(self) -> list[str]:
        """List models reachable with the current configuration.

        A keyless provider (Ollama) is only listed once its endpoint has been
        probed reachable, so an empty list genuinely means "no AI tier"
        rather than "a daemon that is not running".

        Returns:
            A de-duplicated list of ``provider:model`` strings. Empty when
            nothing is configured.
        """
        out: list[str] = []
        for name in self.order:
            provider = self.providers.get(name)
            if provider is None or not provider.ready:
                continue
            for model in provider.models():
                entry = f"{name}:{model}"
                if entry not in out:
                    out.append(entry)
        return out

    def health(self) -> dict:
        """Return a secret-free health snapshot of the AI tier.

        Returns:
            Dict with ``ok``, ``primary``, ``providers`` (per-provider config,
            readiness and circuit state), ``available_models``, call counters
            and the offline flag.
        """
        providers = {}
        configured = []
        ready = []
        for name in self.order:
            provider = self.providers.get(name)
            if provider is None:
                continue
            entry = provider.health()
            if entry["configured"]:
                configured.append(name)
            if entry["ready"]:
                ready.append(name)
            providers[name] = entry
        return {
            "ok": bool(ready),
            "primary": ready[0] if ready else None,
            "configured_providers": configured,
            "ready_providers": ready,
            "providers": providers,
            "available_models": self.available_models(),
            "provider_order": list(self.order),
            "offline_fallback": not ready,
            "vision_capable": [n for n in ready if self.providers[n].vision],
            "defaults": {
                "max_tokens": self.default_max_tokens,
                "timeout_s": self.default_timeout,
                "circuit_fail_threshold": _env_int("CIRCUIT_FAIL_THRESHOLD", 3),
                "circuit_reset_after_s": _env_float("CIRCUIT_RESET_AFTER", 30.0),
                "router_threshold": _env_float("LLM_ROUTER_THRESHOLD", 0.8),
            },
            "calls": self.calls,
            "failovers": self.failovers,
            "offline_calls": self.offline_calls,
        }

    async def close(self) -> None:
        """Close the shared HTTP client. Safe to call repeatedly."""
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001
                _log("llm", f"llm client close failed: {type(exc).__name__}", "debug")


# ── Singleton ─────────────────────────────────────────────────────────────────

# Indirection so Provider._post can reach the active facade's HTTP client
# without every provider holding its own copy (and its own connection pool).
_FACADE: list[LLM] = []
_llm: LLM | None = None


async def _standalone_post(url: str, payload: dict, headers: dict,
                           timeout: float) -> dict:
    """POST JSON with a throwaway client, for facadeless provider use.

    Args:
        url: Absolute endpoint URL.
        payload: JSON body.
        headers: Request headers.
        timeout: Seconds before aborting.
    Returns:
        The decoded JSON body.
    Raises:
        LLMUnavailable: On transport error, timeout, bad status or bad JSON.
    """
    import httpx
    try:
        async with httpx.AsyncClient(timeout=max(1.0, float(timeout)),
                                     trust_env=True) as client:
            response = await client.post(url, json=payload, headers=headers)
    except asyncio.TimeoutError:
        raise LLMUnavailable(f"request timed out after {timeout:.0f}s") from None
    except Exception as exc:  # noqa: BLE001
        raise LLMUnavailable(f"transport error: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise LLMUnavailable(f"HTTP {response.status_code} from provider")
    try:
        return response.json()
    except ValueError:
        raise LLMUnavailable("non-JSON response body") from None


def get_llm() -> LLM:
    """Return the process-wide ``LLM`` singleton, creating it on first use.

    Returns:
        The shared ``LLM`` instance.
    """
    global _llm
    if _llm is None:
        _llm = LLM()
        _FACADE.clear()
        _FACADE.append(_llm)
    return _llm


async def close_llm() -> None:
    """Close the singleton and drop it so the next call re-reads the env."""
    global _llm
    if _llm is not None:
        await _llm.close()
        _llm = None
    if _FACADE:
        _FACADE.clear()


# ── Smart input router (additive; the LLM is a second opinion only) ──────────

_ROUTER_SYSTEM = (
    "You classify an OSINT investigation target for a triage tool. Reply with "
    "one JSON object only: {\"input_type\": \"<type>\", \"reasoning\": \"<one "
    "sentence>\"}. Allowed input_type values are exactly: username, email, "
    "phone, domain, ip_address, crypto_wallet, url, image, photo, face, "
    "person_name, video, audio, document, text, unknown. Never return a type "
    "outside that list."
)


def _routing_agents(input_type: str) -> list[str]:
    """Return the agent list the heuristic router maps to ``input_type``.

    Args:
        input_type: An ``InputType`` value string.
    Returns:
        The mapped agent names, or an empty list when unmapped.
    """
    try:
        from router import AGENT_MAP, InputType
        return list(AGENT_MAP.get(InputType(input_type), []))
    except Exception:  # noqa: BLE001
        return []


async def classify_input(
    raw: str,
    *,
    heuristic_type: str | None = None,
    heuristic_confidence: float = 0.0,
) -> dict:
    """Ask the model to classify an investigation target as a second opinion.

    Returns a dict that always contains a usable ``input_type``; when the model
    is unavailable the heuristic answer is returned unchanged, so this is a
    safe drop-in for ``router.detect_input_type``.

    Args:
        raw: The raw user input.
        heuristic_type: Type already chosen by the deterministic router.
        heuristic_confidence: Confidence the heuristic reported.
    Returns:
        ``{"input_type", "reasoning", "confidence", "source", "agents"}``.
    """
    known = {
        "username", "email", "phone", "domain", "ip_address", "crypto_wallet",
        "url", "image", "photo", "face", "person_name", "video", "audio",
        "document", "text", "unknown",
    }
    base = {
        "input_type": heuristic_type or "unknown",
        "reasoning": "heuristic router (no model available)",
        "confidence": float(heuristic_confidence or 0.0),
        "source": "heuristic",
        "agents": _routing_agents(heuristic_type or "unknown"),
    }
    if not raw or not raw.strip():
        return base

    from llm import get_llm as _get   # self-import: keeps llm.py standalone
    data = await _get().complete_json(
        f"Target: {raw.strip()[:300]}\nClassify it.",
        system=_ROUTER_SYSTEM,
        max_tokens=256,
        timeout=12.0,
        schema_hint='{"input_type": "username", "reasoning": "short string"}',
    )
    if not isinstance(data, dict) or data.get("error"):
        return base
    proposed = str(data.get("input_type") or "").strip().lower()
    if proposed not in known:
        return base
    # A high-confidence deterministic match is not overridden by a guess.
    if float(heuristic_confidence or 0.0) >= 0.95 and proposed != base["input_type"]:
        return base
    return {
        "input_type": proposed,
        "reasoning": _clip(data.get("reasoning"), 240) or "model classification",
        "confidence": 0.85,
        "source": "llm",
        "agents": _routing_agents(proposed) or base["agents"],
    }


async def detect_input_type_smart(raw: str) -> Any:
    """Detect the input type, escalating to the model only when unsure.

    Runs the deterministic ``router.detect_input_type`` first. When its
    confidence is at or above ``LLM_ROUTER_THRESHOLD`` (default 0.8) the
    heuristic decision is returned untouched — no latency, no cost, no new
    failure mode. Otherwise the model is asked for a second opinion and its
    answer is returned only if it is a valid ``InputType``.

    Args:
        raw: The raw user input.
    Returns:
        A ``router.RoutingDecision`` (or the heuristic decision on any failure).
    """
    from router import AGENT_MAP, InputType, detect_input_type

    decision = detect_input_type(raw)
    threshold = _env_float("LLM_ROUTER_THRESHOLD", 0.8)
    if decision.confidence >= threshold:
        return decision

    try:
        refined = await classify_input(
            raw,
            heuristic_type=decision.input_type.value,
            heuristic_confidence=decision.confidence,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        _log("llm", f"smart router fell back to heuristic: {type(exc).__name__}", "debug")
        return decision

    if refined.get("source") != "llm":
        return decision
    itype = InputType(refined["input_type"])
    agents = list(AGENT_MAP.get(itype, decision.agents))
    _log("llm", f"smart router refined {decision.input_type.value}"
                f"({decision.confidence:.2f}) -> {itype.value}")
    return RoutingDecisionFactory(itype, agents, float(refined.get("confidence", 0.8)),
                                  refined.get("reasoning", decision.reasoning))


def RoutingDecisionFactory(input_type: Any, agents: list[str], confidence: float,
                           reasoning: str) -> Any:
    """Build a ``router.RoutingDecision`` without importing it at module scope.

    Args:
        input_type: An ``InputType`` member.
        agents: Agent names to activate.
        confidence: 0..1 confidence.
        reasoning: One-line explanation.
    Returns:
        A ``router.RoutingDecision`` instance (falls back to a local dataclass).
    """
    try:
        from router import RoutingDecision
        return RoutingDecision(input_type, agents, confidence, reasoning)
    except Exception:  # noqa: BLE001
        from dataclasses import dataclass

        @dataclass
        class _Fallback:
            input_type: Any
            agents: list[str]
            confidence: float
            reasoning: str

        return _Fallback(input_type, agents, confidence, reasoning)
