"""
connectors/__init__.py — the connector registry (WS-API)
========================================================
Signal-OS reaches outward through a set of small, single-purpose modules in this
package: one file per source or capability (``sherlock``, ``shodan``,
``searxng``, ``exif``, …). Each exposes a module-level ``async def run_*``
coroutine that an agent calls.

This module is the *registry*: one place that names every connector, binds a
logical name to its entry point, and can report whether that entry point can
actually run in this deployment.

Two public names
----------------
:data:`CONNECTORS`
    ``{logical_name: coroutine_function}``. Every value is a *lazy* callable —
    the connector module is not imported until the entry point is first called,
    so ``import connectors`` costs nothing and a missing optional dependency
    cannot break the package import.

:func:`connector_health`
    A per-connector readiness matrix for the OPS view. Import-probe only; it
    performs no network I/O and never raises.

Why lazy
--------
The connectors have wildly different dependency footprints. ``net``, ``imgguard``
and ``langid`` are stdlib-only; ``browser`` needs Playwright *and* a downloaded
Chromium; ``transcribe`` pulls in faster-whisper or torch; ``qdrant_store`` and
``face_embed`` need the Qdrant client. Importing any of them eagerly at package
import would make an optional dependency a hard requirement for the whole
platform. Instead every entry is a :class:`_LazyConnector` proxy that resolves
on first call and degrades with a clear error if the dependency is absent.

The rule this file is built on: **an entry is only listed because a ``run_*``
function exists in the corresponding module.** ``tests/test_smoke.py`` re-derives
the set of ``run_*`` functions straight from the source with :mod:`ast` and
fails if the registry and the tree ever disagree, so an entry cannot be
invented or go stale.

Naming
------
The logical name is the connector's module name, with the two VAULT semantic
memory entry points namespaced by their verb (``qdrant_upsert_entities``,
``qdrant_semantic_search``) because they share the ``qdrant_store`` module and a
bare name would collide with nothing and mislead everybody.

Args / Returns
--------------
CONNECTORS:
    Mapping of logical connector name to a callable that forwards to the real
    coroutine. Call it with the connector's own arguments; see each connector
    module's docstring for its signature.

connector_health():
    Returns a dict with ``connectors`` (name -> readiness record) and
    ``summary`` (counts). Each readiness record carries ``available``,
    ``installed``, ``requires`` and ``notes`` as described below.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

__all__ = [
    "CONNECTORS",
    "CONNECTOR_NAMES",
    "connector_health",
    "get_connector",
    "resolve",
]


# ── Requirement probing ──────────────────────────────────────────────────────
#
# A requirement is probed *without importing* the requirement itself wherever
# possible (find_spec for modules, shutil.which for binaries, os.environ for
# credentials) so the health matrix stays side-effect free.


@dataclass(frozen=True)
class _Requirement:
    """One thing a connector needs before it can produce real output.

    Attributes:
        name: Human-readable requirement, as it appears in ``requires``.
        kind: How to probe it — ``"module"``, ``"binary"``, ``"env"``,
            ``"path"`` or ``"callable"``. ``"callable"`` means "an optional
            probe function on the connector module itself" and is resolved by
            the registry, not by the generic probes below.
        target: What to probe: a module name, a binary name, an env var name,
            a filesystem path, or (for ``"callable"``) the connector's own
            module-level function name.
        optional: When true the requirement does not gate ``installed``. An
            optional requirement that is present is still reported as a
            satisfied capability.
    """

    name: str
    kind: str
    target: str
    optional: bool = False


def _module_present(name: str) -> bool:
    """``True`` when *name* is importable, without importing it.

    Args:
        name: Top-level module name.

    Returns:
        ``True`` if :func:`importlib.util.find_spec` resolves the module.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001 — a broken parent package raises, not returns
        return False


def _binary_present(name: str) -> bool:
    """``True`` when *name* is on ``PATH``.

    Args:
        name: Executable name.

    Returns:
        ``True`` when :func:`shutil.which` resolves it.
    """
    try:
        return shutil.which(name) is not None
    except Exception:  # noqa: BLE001
        return False


def _env_present(name: str) -> bool:
    """``True`` when environment variable *name* is set to a non-empty value.

    Args:
        name: Environment variable name.

    Returns:
        ``True`` when the variable exists and is not blank.
    """
    try:
        return bool((os.environ.get(name) or "").strip())
    except Exception:  # noqa: BLE001
        return False


def _path_present(path: str) -> bool:
    """``True`` when *path* exists on disk.

    Args:
        path: Filesystem path.

    Returns:
        ``True`` when the path exists.
    """
    try:
        return os.path.exists(path)
    except Exception:  # noqa: BLE001 — a malformed path raises on some platforms
        return False


_REQUIREMENT_PROBES: dict[str, Callable[[str], bool]] = {
    "module": _module_present,
    "binary": _binary_present,
    "env": _env_present,
    "path": _path_present,
}


def _probe(req: _Requirement, module: Any) -> bool | None:
    """Probe one requirement.

    Args:
        req: The requirement to test.
        module: The already-imported connector module, or ``None`` when it
            failed to import. Used for ``"callable"`` requirements.

    Returns:
        ``True``/``False`` when the requirement could be decided, or ``None``
        when it could not be decided (the connector module itself failed to
        import, so its own probe function is unavailable).
    """
    if req.kind == "callable":
        fn = getattr(module, req.target, None) if module is not None else None
        if not callable(fn):
            return None
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001 — a probe that throws reads as absent
            return False
    probe = _REQUIREMENT_PROBES.get(req.kind)
    if probe is None:
        return None
    return probe(req.target)


# ── Connector specs ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Spec:
    """One registry entry: where the entry point lives and what it needs.

    Attributes:
        name: Logical connector name — the key in :data:`CONNECTORS`.
        module: Importable module path.
        attr: The module-level ``run_*`` attribute to bind.
        summary: One line describing the source or capability.
        requirements: Everything that must hold for the connector to produce
            real output. Optional capabilities are marked as such.
    """

    name: str
    module: str
    attr: str
    summary: str
    requirements: tuple[_Requirement, ...] = field(default_factory=tuple)


_HTTPX = _Requirement("httpx", "module", "httpx")
_BS4 = _Requirement("beautifulsoup4", "module", "bs4")
_PIL = _Requirement("pillow", "module", "PIL")


_SPECS: tuple[_Spec, ...] = (
    _Spec(
        name="bioclip",
        module="connectors.bioclip",
        attr="run_bioclip",
        summary="Scene / biome classification for an image (TERRA).",
        requirements=(
            _Requirement("transformers", "module", "transformers", optional=True),
            _Requirement("open_clip_torch", "module", "open_clip", optional=True),
            _Requirement("BIOCLIP_ENABLED", "env", "BIOCLIP_ENABLED", optional=True),
            _Requirement("BIOCLIP_OLLAMA_ENABLED", "env", "BIOCLIP_OLLAMA_ENABLED", optional=True),
        ),
    ),
    _Spec(
        name="breach_check",
        module="connectors.breach_check",
        attr="run_breach_check",
        summary="Breach exposure for a target (HIBP / DeHashed).",
        requirements=(
            _Requirement("HIBP_API_KEY", "env", "HIBP_API_KEY", optional=True),
            _Requirement("DEHASHED_EMAIL", "env", "DEHASHED_EMAIL", optional=True),
            _Requirement("DEHASHED_API_KEY", "env", "DEHASHED_API_KEY", optional=True),
        ),
    ),
    _Spec(
        name="browser",
        module="connectors.browser",
        attr="run_browser",
        summary="Headless page render / screenshot / JS eval (CRAWLER).",
        requirements=(
            _Requirement("playwright", "module", "playwright"),
            _Requirement("chromium binary", "callable", "browsers_installed"),
        ),
    ),
    _Spec(
        name="darkweb",
        module="connectors.darkweb",
        attr="run_darkweb",
        summary="Dark-web / paste monitoring sweep (SENTINEL).",
        requirements=(
            _Requirement("FEATURE_DARK_WEB_ALERTS", "env", "FEATURE_DARK_WEB_ALERTS"),
            _Requirement("DARKWEB_INDEX_URLS", "env", "DARKWEB_INDEX_URLS", optional=True),
        ),
    ),
    _Spec(
        name="email_enum",
        module="connectors.email_enum",
        attr="run_email_enum",
        summary="Per-provider account existence check for an address (EMAIL).",
        requirements=(_HTTPX,),
    ),
    _Spec(
        name="exif",
        module="connectors.exif",
        attr="run_exif",
        summary="EXIF / GPS extraction from an image (IRIS, TERRA).",
        requirements=(_PIL,),
    ),
    _Spec(
        name="face_embed",
        module="connectors.face_embed",
        attr="run_face_embed",
        summary="Face detect, embed and match against stored vectors (IRIS).",
        requirements=(
            _Requirement("qdrant-client", "module", "qdrant_client"),
            _Requirement("QDRANT_URL", "env", "QDRANT_URL", optional=True),
        ),
    ),
    _Spec(
        name="people_search",
        module="connectors.people_search",
        attr="run_people_search",
        summary="Public-record sweep for a person name (PRISM).",
        requirements=(_BS4,),
    ),
    _Spec(
        name="phone_intel",
        module="connectors.phone_intel",
        attr="run_phone_intel",
        summary="Carrier / line-type / geocoding intel for a number (PHONOS).",
        requirements=(
            _Requirement("NUMVERIFY_API_KEY", "env", "NUMVERIFY_API_KEY", optional=True),
            _Requirement("DEHASHED_API_KEY", "env", "DEHASHED_API_KEY", optional=True),
        ),
    ),
    _Spec(
        name="plantnet",
        module="connectors.plantnet",
        attr="run_plantnet",
        summary="Plant species identification from a photo (TERRA).",
        requirements=(
            _Requirement("PLANT_ID_API_KEY", "env", "PLANT_ID_API_KEY", optional=True),
            _Requirement("PLANTNET_API_KEY", "env", "PLANTNET_API_KEY", optional=True),
        ),
    ),
    _Spec(
        name="qdrant_upsert_entities",
        module="connectors.qdrant_store",
        attr="run_upsert_entities",
        summary="Write entities into semantic memory (VAULT).",
        requirements=(_Requirement("qdrant-client", "module", "qdrant_client"),),
    ),
    _Spec(
        name="qdrant_semantic_search",
        module="connectors.qdrant_store",
        attr="run_semantic_search",
        summary="Semantic recall over stored entities (VAULT).",
        requirements=(_Requirement("qdrant-client", "module", "qdrant_client"),),
    ),
    _Spec(
        name="reverse_image",
        module="connectors.reverse_image",
        attr="run_reverse_image",
        summary="Reverse image search via a SearXNG instance (IRIS).",
        requirements=(_BS4, _HTTPX),
    ),
    _Spec(
        name="searxng",
        module="connectors.searxng",
        attr="run_searxng",
        summary="Metasearch federation through a SearXNG instance (SCOUT).",
        requirements=(_HTTPX,),
    ),
    _Spec(
        name="sherlock",
        module="connectors.sherlock",
        attr="run_sherlock",
        summary="Username enumeration via the Sherlock CLI (PRISM).",
        requirements=(_Requirement("sherlock CLI", "binary", "sherlock"),),
    ),
    _Spec(
        name="shodan",
        module="connectors.shodan",
        attr="run_shodan",
        summary="Shodan host intelligence: ports, services, vulns (SIGMA).",
        requirements=(
            _Requirement("shodan SDK", "module", "shodan"),
            _Requirement("SHODAN_API_KEY", "env", "SHODAN_API_KEY"),
        ),
    ),
    _Spec(
        name="spiderfoot",
        module="connectors.spiderfoot",
        attr="run_spiderfoot",
        summary="SpiderFoot module run against a target (SCOUT).",
        requirements=(_HTTPX,),
    ),
    _Spec(
        name="theharvester",
        module="connectors.theharvester",
        attr="run_theharvester",
        summary="Subdomain / email harvesting via theHarvester CLI (SCOUT).",
        requirements=(_Requirement("theHarvester CLI", "binary", "theHarvester"),),
    ),
    _Spec(
        name="transcribe",
        module="connectors.transcribe",
        attr="run_transcribe",
        summary="Speech-to-text for audio and video (ECHO, IRIS).",
        requirements=(
            _Requirement("faster-whisper", "module", "faster_whisper", optional=True),
            _Requirement("whisperx", "module", "whisperx", optional=True),
            _Requirement("WHISPER_API_KEY", "env", "WHISPER_API_KEY", optional=True),
        ),
    ),
)


# ── Lazy resolution ──────────────────────────────────────────────────────────


class _LazyConnector:
    """A deferred reference to a connector entry point.

    Constructing this is free — no import happens. The connector module is
    imported on the first :meth:`__call__`, or on demand via :meth:`resolve`,
    and the resolved coroutine is cached on the instance.

    The object is callable, so it drops straight into :data:`CONNECTORS` and
    satisfies ``dict[str, Callable]``. It also forwards ``__name__`` and
    ``__doc__`` from the real function once resolved, so error messages and logs
    name the connector they actually ran.
    """

    __slots__ = ("_error", "_fn", "_module", "_spec")

    def __init__(self, spec: _Spec) -> None:
        """Record the spec without touching the module.

        Args:
            spec: The registry entry this proxy stands in for.
        """
        self._spec = spec
        self._fn: Callable[..., Coroutine[Any, Any, dict[str, Any]]] | None = None
        self._module: Any = None
        self._error: str | None = None

    # -- introspection ------------------------------------------------------

    @property
    def spec(self) -> _Spec:
        """The :class:`_Spec` backing this proxy."""
        return self._spec

    @property
    def name(self) -> str:
        """The logical connector name."""
        return self._spec.name

    @property
    def __name__(self) -> str:  # noqa: A003 — mirrors the wrapped function
        """The wrapped coroutine's name, or the attribute name pre-resolution."""
        fn = self._fn
        return getattr(fn, "__name__", self._spec.attr) if fn else self._spec.attr

    @property
    def __doc__(self) -> str | None:  # type: ignore[override] # noqa: A003
        """The wrapped coroutine's docstring once resolved."""
        fn = self._fn
        return getattr(fn, "__doc__", None) if fn else None

    # -- resolution ---------------------------------------------------------

    def resolve(self) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
        """Import the connector module and bind its entry point.

        Returns:
            The connector's ``run_*`` coroutine function.

        Raises:
            RuntimeError: If the module cannot be imported (usually a missing
                optional dependency) or does not expose the expected attribute.
                The message names the module and the original error.
        """
        if self._fn is not None:
            return self._fn
        if self._error is not None:
            raise RuntimeError(f"connector {self._spec.name!r} is unavailable: {self._error}")
        try:
            self._module = importlib.import_module(self._spec.module)
        except Exception as exc:  # noqa: BLE001 — degrade, never break the import
            self._error = f"{type(exc).__name__}: {exc} (importing {self._spec.module})"
            raise RuntimeError(
                f"connector {self._spec.name!r} is unavailable: {self._error}"
            ) from exc
        fn = getattr(self._module, self._spec.attr, None)
        if not callable(fn):
            self._error = f"{self._spec.module} has no callable {self._spec.attr!r}"
            raise RuntimeError(f"connector {self._spec.name!r} is unavailable: {self._error}")
        self._fn = fn
        return fn

    @property
    def resolved(self) -> bool:
        """``True`` once the entry point has been imported and bound."""
        return self._fn is not None

    def __call__(self, *args: Any, **kwargs: Any) -> Coroutine[Any, Any, dict[str, Any]]:
        """Call the connector, resolving it on first use.

        Args:
            *args: Positional arguments for the connector's ``run_*`` function.
            **kwargs: Keyword arguments for the same.

        Returns:
            The connector's result coroutine.
        """
        return self.resolve()(*args, **kwargs)

    def __repr__(self) -> str:
        """Debug representation showing resolution state."""
        if self._fn is not None:
            state = "resolved"
        elif self._error is not None:
            state = f"error={self._error}"
        else:
            state = "unresolved"
        return f"<connector {self._spec.name} -> {self._spec.module}.{self._spec.attr} {state}>"


def _build_registry() -> dict[str, _LazyConnector]:
    """Construct the registry mapping from :data:`_SPECS`.

    Returns:
        ``{logical_name: _LazyConnector}`` in declaration order.
    """
    return {spec.name: _LazyConnector(spec) for spec in _SPECS}


#: ``{logical_name: callable}`` for every connector in the tree.
#:
#: Values are :class:`_LazyConnector` proxies, not the raw functions — they are
#: callable and forward everything, but they do not import the connector module
#: until first use. Use ``CONNECTORS[name].resolve()`` when you need the real
#: function object.
CONNECTORS: dict[str, Callable[..., Coroutine[Any, Any, dict[str, Any]]]] = _build_registry()

#: Sorted tuple of the registered logical names, for iteration and tests.
CONNECTOR_NAMES: tuple[str, ...] = tuple(sorted(CONNECTORS))


# ── Public helpers ───────────────────────────────────────────────────────────


def get_connector(name: str) -> _LazyConnector:
    """Look up one registry entry by logical name.

    Args:
        name: A key of :data:`CONNECTORS`.

    Returns:
        The :class:`_LazyConnector` proxy for *name*.

    Raises:
        KeyError: If *name* is not registered. The message lists the valid names.
    """
    try:
        return CONNECTORS[name]  # type: ignore[return-value]
    except KeyError:
        raise KeyError(
            f"unknown connector {name!r}; registered: {', '.join(CONNECTOR_NAMES)}"
        ) from None


def connector_health() -> dict[str, Any]:
    """Report per-connector readiness without touching the network.

    Probes only two things: whether the connector module imports and exposes its
    entry point (``available``), and whether its declared requirements are
    present (``installed``). No connector is executed, no HTTP request is made,
    no third-party service is contacted — this is safe to call from a health
    endpoint and from CI.

    ``available`` and ``installed`` are deliberately distinct. A connector can be
    importable but not installed (``browser`` without a downloaded Chromium;
    ``shodan`` without an API key), and a connector can be installed but have a
    feature flag switched off (``darkweb`` when
    ``FEATURE_DARK_WEB_ALERTS`` is false). Both states are healthy — they mean
    the platform degrades rather than breaks — so ``notes`` explains which.

    Args:
        None.

    Returns:
        A dict of the shape::

            {
              "connectors": {
                "<name>": {
                  "available": bool,     # module imports, entry point exists
                  "installed": bool,     # every required dependency present
                  "requires": [str],     # what it needs
                  "notes": str,          # human explanation of the state
                  "missing": [str],      # unsatisfied requirements
                }, ...
              },
              "summary": {
                "total": int, "available": int, "installed": int,
              },
            }
    """
    out: dict[str, Any] = {}
    counts = {"total": 0, "available": 0, "installed": 0}

    for spec in _SPECS:
        module: Any = None
        available = False
        import_error: str | None = None
        try:
            module = importlib.import_module(spec.module)
            available = callable(getattr(module, spec.attr, None))
            if not available:
                import_error = f"{spec.module} has no callable {spec.attr!r}"
        except Exception as exc:  # noqa: BLE001 — a missing dep is a state, not a crash
            import_error = f"{type(exc).__name__}: {exc}"

        missing: list[str] = []
        requires: list[str] = []
        satisfied_optional: list[str] = []
        required_ok = True

        for req in spec.requirements:
            requires.append(req.name)
            ok = _probe(req, module)
            if ok is True:
                if req.optional:
                    satisfied_optional.append(req.name)
                continue
            if req.optional:
                missing.append(f"{req.name} (optional, absent)")
                continue
            required_ok = False
            missing.append(req.name if ok is False else f"{req.name} (undetermined)")

        installed = bool(available and required_ok)
        notes = _health_notes(
            installed=installed,
            missing=missing,
            satisfied_optional=satisfied_optional,
        )
        if not available and import_error:
            notes = f"module import failed ({import_error})"

        out[spec.name] = {
            "available": available,
            "installed": installed,
            "requires": requires,
            "notes": notes,
            "missing": missing,
            "summary": spec.summary,
        }
        counts["total"] += 1
        counts["available"] += int(available)
        counts["installed"] += int(installed)

    return {"connectors": out, "summary": counts}


def _health_notes(
    *,
    installed: bool,
    missing: list[str],
    satisfied_optional: list[str],
) -> str:
    """Compose the one-line ``notes`` field for a health record.

    Args:
        installed: Whether required dependencies are present.
        missing: Unsatisfied requirement labels.
        satisfied_optional: Optional requirements that are present.

    Returns:
        A short human-readable sentence describing current readiness.
    """
    if installed:
        extra = f" (also present: {', '.join(satisfied_optional)})" if satisfied_optional else ""
        return f"ready{extra}"
    hard = [m for m in missing if "optional" not in m]
    if hard:
        return (
            f"degraded — needs {', '.join(hard)}; degrades to the deterministic "
            "offline path rather than failing"
        )
    return "degraded — optional capability unavailable; degrades"


#: Backwards-compatible alias for :func:`get_connector`.
resolve = get_connector
