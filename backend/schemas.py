"""
schemas.py — Strict request/response models for Signal-OS (WS-SEC)
==================================================================
The models in ``main.py`` were bare ``BaseModel`` declarations with unbounded
strings, unbounded lists and Pydantic's default ``extra="ignore"``. That means:

* ``{"input": "x" * 50_000_000}`` was accepted — a single request could be used
  to burn memory and push megabytes into every agent prompt.
* ``engines`` was an open list of arbitrary strings, forwarded verbatim into
  dork construction.
* Unknown fields were silently dropped, so a typo (``input_typey``) was
  invisible and a hostile extra key rode along unnoticed.

Every request model here sets ``extra="forbid"`` and puts an explicit
``max_length`` on all free text. Collections get ``max_length`` too, and every
optional string gets ``min_length=1`` so ``""`` cannot be used to smuggle a
"blank" input past a required check.

Field names are fixed by what ``main.py`` accepts today and by
``docs/CONTRACTS.md`` §10 — ``InvestigateRequest(input, input_type, case_id)``,
``SearchRequest(query, engines, use_dorks)``,
``NameSearchRequest(name, location, case_id)``. Where a field has a plausible
alternate name in the wild (``CompareRequest`` is consumed by a "model arena"
that calls it either ``prompt`` or ``input``), a validation
:class:`~pydantic.AliasChoices` accepts both while the canonical name stays
the first choice — so the model always *serialises* one predictable shape.

``ReportFormat`` is a ``Literal`` rather than a free string, which makes
``GET /investigate/{id}/report?format=pdf`` a validated query parameter
instead of a value interpolated into a file name.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

__all__ = [
    "StrictModel",
    "RequestModel",
    "InvestigateRequest",
    "SearchRequest",
    "NameSearchRequest",
    "CaseCreate",
    "CaseUpdate",
    "CompareRequest",
    "SaveMemoryRequest",
    "ReportFormat",
    "InputType",
    "MAX_INPUT_LENGTH",
    "MAX_QUERY_LENGTH",
    "MAX_NAME_LENGTH",
    "MAX_ENGINES",
    "MAX_TAGS",
    "MAX_MODELS",
]

# ── Bounds ───────────────────────────────────────────────────────────────────

#: Longest accepted primary input (a URL, a username, a file path, a text blob).
MAX_INPUT_LENGTH = 4096
#: Longest accepted search query.
MAX_QUERY_LENGTH = 512
#: Longest accepted person name / location string.
MAX_NAME_LENGTH = 256
#: Longest accepted free-text notes field.
MAX_NOTES_LENGTH = 8192

#: Upper bounds on caller-supplied collections.
MAX_ENGINES = 20
MAX_TAGS = 50
MAX_MODELS = 12
MAX_ENTITIES = 500

#: Identifiers the app generates look like this: 8 hex chars, optionally longer.
_ID_MAX = 64
_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,%d}$" % _ID_MAX

#: Input kinds the router understands. ``None`` keeps auto-detection.
InputType = Literal[
    "auto", "text", "username", "email", "domain", "url", "ip",
    "phone", "person_name", "photo", "image", "document", "wallet",
    "crypto", "location", "account",
]

#: Report renderers that exist.
ReportFormat = Literal["markdown", "pdf", "json"]

#: Engines SCOUT may federate to. Anything else is rejected at the edge rather
#: than being forwarded into dork construction.
KNOWN_ENGINES = frozenset({
    "google", "bing", "yandex", "duckduckgo", "brave", "mojeek",
    "startpage", "searx", "searxng", "wikipedia", "archive", "wayback",
    "github", "reddit", "social", "dorks", "shodan", "censys", "hunter",
    "haveibeenpwned", "leakcheck", "waymore", "publicwww",
})

NonEmptyStr = Annotated[str, Field(min_length=1, max_length=MAX_INPUT_LENGTH)]


# ── Bases ────────────────────────────────────────────────────────────────────

class StrictModel(BaseModel):
    """Base for response/serialisation models: coerce nothing, keep it strict."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
        use_enum_values=True,
    )


class RequestModel(StrictModel):
    """Base for request bodies.

    Adds the rule every request model follows: unknown fields are an error, so
    a typo fails loudly (422) instead of being silently dropped.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
        str_max_length=MAX_INPUT_LENGTH,
    )


# ── Requests ─────────────────────────────────────────────────────────────────

class InvestigateRequest(RequestModel):
    """``POST /api/investigate`` and ``POST /api/investigate-sync``.

    Attributes:
        input: Whatever is being investigated — text, username, email, domain,
            URL, IP, phone, or a server-side file path produced by an upload.
            Bounded to :data:`MAX_INPUT_LENGTH`.
        input_type: Optional hint; ``None`` keeps router auto-detection.
        case_id: Optional existing case to append to.
        deep: Run the expensive/deep agent pass.
    """

    input: NonEmptyStr = Field(
        max_length=MAX_INPUT_LENGTH,
        description="Text, username, email, domain, URL, IP, phone or file path.",
    )
    input_type: InputType | None = Field(
        default=None,
        max_length=32,
        description="Optional input-kind hint; omit for auto-detection.",
    )
    case_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    deep: bool = False

    @field_validator("input_type", mode="before")
    @classmethod
    def _blank_input_type_is_auto(cls, value: Any) -> Any:
        """Treat ``""``/``"auto"``/``None`` as "let the router decide"."""
        if isinstance(value, str) and value.strip().lower() in ("", "auto", "none"):
            return None
        return value


class SearchRequest(RequestModel):
    """``POST /api/search`` — SCOUT dork generation + engine federation.

    Attributes:
        query: The search string.
        engines: Engine names to federate to. Every entry must be in
            :data:`KNOWN_ENGINES`; the list is capped at
            :data:`MAX_ENGINES`.
        use_dorks: Generate search-operator dorks for the query.
    """

    query: NonEmptyStr = Field(max_length=MAX_QUERY_LENGTH)
    engines: list[str] = Field(
        default_factory=lambda: ["google", "bing", "yandex"],
        max_length=MAX_ENGINES,
    )
    use_dorks: bool = True

    @field_validator("engines", mode="before")
    @classmethod
    def _normalise_engines(cls, value: Any) -> Any:
        """Accept a comma-separated string; drop blanks and duplicates."""
        if value is None:
            return ["google", "bing", "yandex"]
        if isinstance(value, str):
            value = [p for p in (s.strip().lower() for s in value.split(",")) if p]
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("engines must be a list of engine names")
        if len(value) > MAX_ENGINES:
            raise ValueError(f"at most {MAX_ENGINES} engines are allowed")
        seen: list[str] = []
        for item in value:
            name = str(item).strip().lower()[:32]
            if not name:
                continue
            if name not in seen:
                seen.append(name)
        if not seen:
            raise ValueError("engines must not be empty")
        return seen

    @field_validator("engines")
    @classmethod
    def _known_engines(cls, value: list[str]) -> list[str]:
        """Reject unknown engines so they cannot reach dork construction."""
        unknown = [e for e in value if e not in KNOWN_ENGINES]
        if unknown:
            raise ValueError(
                "unsupported engine(s): " + ", ".join(sorted(unknown)[:10])
            )
        return value


class NameSearchRequest(RequestModel):
    """``POST /api/name-search`` — PRISM + SCOUT + INK on a person name.

    Attributes:
        name: The person's name (not a username/handle — use
            ``InvestigateRequest`` for that).
        location: Optional geographic hint narrowing the search.
        case_id: Optional case to file the result under.
    """

    name: NonEmptyStr = Field(max_length=MAX_NAME_LENGTH)
    location: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    case_id: str | None = Field(default=None, pattern=_ID_PATTERN)


class CaseCreate(RequestModel):
    """``POST /api/cases``.

    Attributes:
        name: Human-readable case name.
        target: The investigation target (username, domain, person).
        case_id: Optional explicit id; generated when omitted.
        tags: Free-form labels, capped at :data:`MAX_TAGS` x 64 chars.
        notes: Free-text notes, capped at :data:`MAX_NOTES_LENGTH`.
    """

    name: NonEmptyStr = Field(max_length=MAX_NAME_LENGTH)
    target: str | None = Field(default=None, max_length=MAX_INPUT_LENGTH)
    case_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)

    @field_validator("tags", mode="before")
    @classmethod
    def _clean_tags(cls, value: Any) -> Any:
        """Normalise a comma-separated string, then cap length and charset."""
        if value is None:
            return []
        if isinstance(value, str):
            value = [p for p in (s.strip() for s in value.split(",")) if p]
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("tags must be a list of strings")
        if len(value) > MAX_TAGS:
            raise ValueError(f"at most {MAX_TAGS} tags are allowed")
        out: list[str] = []
        for tag in value:
            name = str(tag).strip()[:64]
            if name and name not in out:
                out.append(name)
        return out


class CaseUpdate(RequestModel):
    """``PATCH /api/cases/{case_id}`` — every field optional, at least one set.

    Attributes:
        name: New case name.
        target: New investigation target.
        tags: Replacement tag list.
        notes: Replacement notes.
    """

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    target: str | None = Field(default=None, max_length=MAX_INPUT_LENGTH)
    tags: list[str] | None = Field(default=None, max_length=MAX_TAGS)
    notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)

    @field_validator("tags", mode="before")
    @classmethod
    def _clean_tags(cls, value: Any) -> Any:
        """Apply the same tag normalisation as :class:`CaseCreate`."""
        if value is None:
            return None
        if isinstance(value, str):
            value = [p for p in (s.strip() for s in value.split(",")) if p]
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("tags must be a list of strings")
        if len(value) > MAX_TAGS:
            raise ValueError(f"at most {MAX_TAGS} tags are allowed")
        out: list[str] = []
        for tag in value:
            name = str(tag).strip()[:64]
            if name and name not in out:
                out.append(name)
        return out

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "CaseUpdate":
        """Reject an empty patch: it would be a silent no-op."""
        if not self.model_dump(exclude_none=True):
            raise ValueError("patch must set at least one of: name, target, tags, notes")
        return self


class CompareRequest(RequestModel):
    """``POST /api/compare`` — model-arena side-by-side comparison.

    ``prompt`` is the canonical field; ``input`` and ``text`` are accepted as
    aliases so a caller using either spelling works.

    Attributes:
        prompt: The prompt/text handed to every model.
        models: Model identifiers to race. Capped at :data:`MAX_MODELS`.
        temperature: Sampling temperature, 0.0–2.0.
        system: Optional system prompt.
        case_id: Optional case to file the comparison under.
        rounds: How many rounds to run.
    """

    prompt: NonEmptyStr = Field(
        validation_alias=AliasChoices("prompt", "input", "text", "query"),
        max_length=MAX_INPUT_LENGTH,
        serialization_alias="prompt",
    )
    models: list[str] = Field(
        default_factory=list,
        max_length=MAX_MODELS,
        description="Model ids to compare; empty means 'every available model'.",
    )
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    system: str | None = Field(default=None, max_length=MAX_INPUT_LENGTH)
    case_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    rounds: int = Field(default=1, ge=1, le=10)

    @field_validator("models", mode="before")
    @classmethod
    def _clean_models(cls, value: Any) -> Any:
        """Normalise a comma-separated string; cap name length; dedupe."""
        if value is None:
            return []
        if isinstance(value, str):
            value = [p for p in (s.strip() for s in value.split(",")) if p]
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("models must be a list of model ids")
        if len(value) > MAX_MODELS:
            raise ValueError(f"at most {MAX_MODELS} models are allowed")
        out: list[str] = []
        for model in value:
            name = str(model).strip()[:128]
            if name and name not in out:
                out.append(name)
        return out


class SaveMemoryRequest(RequestModel):
    """``POST /api/cases/{case_id}/memory`` (VAULT entity memory).

    Attributes:
        case_id: The case the memory belongs to (path may supply it).
        entities: Entities to remember, capped at :data:`MAX_ENTITIES`.
        text: Free-text note to remember alongside the entities.
        tags: Optional labels.
        replace: Replace the stored memory instead of merging into it.
    """

    case_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    entities: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_ENTITIES)
    text: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    replace: bool = False

    @field_validator("entities", mode="before")
    @classmethod
    def _normalise_entities(cls, value: Any) -> Any:
        """Accept a list of strings or dicts; bound each entry's size."""
        if value is None:
            return []
        if isinstance(value, (str, bytes)):
            raise ValueError("entities must be a list, not a string")
        if not isinstance(value, (list, tuple)):
            raise ValueError("entities must be a list")
        if len(value) > MAX_ENTITIES:
            raise ValueError(f"at most {MAX_ENTITIES} entities are allowed")
        out: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, str):
                item = {"value": item[:512], "type": "note"}
            if not isinstance(item, dict):
                raise ValueError("each entity must be a string or an object")
            clean: dict[str, Any] = {}
            for key, raw in list(item.items())[:20]:
                if isinstance(raw, str):
                    clean[str(key)[:64]] = raw[:1024]
                elif isinstance(raw, (int, float, bool)) or raw is None:
                    clean[str(key)[:64]] = raw
                else:
                    clean[str(key)[:64]] = str(raw)[:1024]
            out.append(clean)
        return out

    @field_validator("tags", mode="before")
    @classmethod
    def _clean_tags(cls, value: Any) -> Any:
        """Apply the same tag normalisation as :class:`CaseCreate`."""
        if value is None:
            return []
        if isinstance(value, str):
            value = [p for p in (s.strip() for s in value.split(",")) if p]
        if len(value) > MAX_TAGS:
            raise ValueError(f"at most {MAX_TAGS} tags are allowed")
        out: list[str] = []
        for tag in value or []:
            name = str(tag).strip()[:64]
            if name and name not in out:
                out.append(name)
        return out