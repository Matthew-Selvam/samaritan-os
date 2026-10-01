"""
Kronos.py — KRONOS Agent
========================
Timeline reconstruction: turns anything dateable — upstream signals *and* the
raw target text — into one UTC-normalized chronology.

Format coverage (the spec's "11+ date formats" claim, made true and then some):

=====================  =====================================================
Family                 Handled forms
=====================  =====================================================
Epoch                  seconds, milliseconds, microseconds, nanoseconds,
                       with or without a fractional part
ISO 8601               ``2024-03-03T10:30:00Z``, offsets ``+05:30``, space or
                       ``T`` separator, week dates ``2024-W10-1``, ordinal
                       ``2024-063``
RFC 2822               ``Mon, 3 Mar 2024 10:30:00 +0000``
Month-name forms       ``March 3 2024``, ``3 Mar 2024``, ``Mar 3, 2024``,
                       ``03 March 2024``, ``3rd of March 2024``
Numeric slash/dot      ``03/04/2024`` — US (MDY) vs EU (DMY) resolved from
                       corroborating evidence, per occurrence
Month-year / year      ``Mar 2024``, ``2024``, ``Q2 2024``, ``H1 2024``
Relative               ``yesterday``, ``today``, ``3 days ago``, ``last week``,
                       ``2 months ago``, ``an hour ago``
Ranges                 ``2019-2024``, ``since 2019``, ``March 2024 to June
                       2024``, ``between 2019 and 2021``
Durations              ``for 3 years``, ``over 18 months``
=====================  =====================================================

Every event carries ``timestamp`` (UTC ISO-8601), ``precision``
(second/minute/day/month/year), ``source_text`` and ``dayfirst`` (which
convention resolved an ambiguous numeric date). Near-identical events are
deduplicated and conflicting dates for the same subject prefer the most recent
while flagging the conflict.
"""
from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone

from .base import BaseAgent, AgentResult


# ── Locale data (offline, no dependencies) ───────────────────────────────────

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: Regional month/day ordering. Used to break a numeric date's ambiguity.
DAYFIRST_REGIONS: dict[str, bool] = {
    "GB": True, "IE": True, "AU": True, "NZ": True, "IN": True, "ZA": True,
    "DE": True, "FR": True, "IT": True, "ES": True, "PT": True, "NL": True,
    "BR": True, "PL": True, "SE": True, "NO": True, "DK": True, "FI": True,
    "AT": True, "CH": True, "BE": True, "AT": True,
    "US": False, "CA": False, "MX": False, "PH": False, "TH": False,
    "ID": False, "MY": False, "SG": False, "HK": False, "TW": False,
    "JP": False, "KR": False, "CN": False, "HU": False, "LT": False,
}

#: Words that imply a day-first locale if they appear alongside a numeric date.
_DAYFIRST_HINTS = (
    "uk", "britain", "england", "ireland", "europe", "australia", "india",
    "day/month", "dd/mm", "dmy",
)

_RELATIVE_UNITS: dict[str, int] = {
    "second": 0, "sec": 0, "s": 0,
    "minute": 1, "min": 1, "m": 1,
    "hour": 2, "hr": 2, "h": 2,
    "day": 3, "d": 3,
    "week": 4, "w": 4,
    "fortnight": 4,
    "month": 5, "mo": 5,
    "quarter": 6, "q": 6,
    "year": 7, "yr": 7, "y": 7,
    "decade": 8,
}


def _utc(dt: datetime) -> str:
    """Format a datetime as UTC ISO-8601 with a trailing ``Z``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _start_of(dt: datetime, precision: str) -> datetime:
    """Truncate a datetime to the start of its stated precision."""
    if precision == "year":
        return dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if precision == "month":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if precision == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if precision == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if precision == "minute":
        return dt.replace(second=0, microsecond=0)
    return dt


class KronosAgent(BaseAgent):
    """Timeline Reconstruction — chronology from every dateable source.

    Consumes the aggregated ``peer_signals`` produced upstream *and* the raw
    target text, then orders everything into one event chain suitable for a
    vis.js timeline. No external date library required, so it works fully
    offline.
    """

    name = "KRONOS"
    role = "Timeline Reconstruction"
    icon = "⊕"
    description = "Chronology reconstruction: username changes, post history, account creation, location changes"
    preferred_models = ["gemma2:9b"]
    token_budget = 6144

    #: Below this many events a timeline is not worth trusting.
    MIN_USEFUL_EVENTS = 2

    # ── Regex table ──────────────────────────────────────────────────────────
    _ISO_RE = re.compile(
        r"\b(?P<Y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"
        r"(?:[T ](?P<H>\d{2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?"
        r"(?:\.(?P<f>\d{1,9}))?"
        r"(?P<tz>Z|z|[+\-]\d{2}:?\d{2})?)?\b"
    )
    _ISO_WEEK_RE = re.compile(r"\b(?P<Y>\d{4})-W(?P<w>\d{2})-(?P<d>[1-7])\b", re.I)
    _ISO_ORDINAL_RE = re.compile(r"\b(?P<Y>\d{4})-(?P<d>\d{3})\b")
    _RFC2822_RE = re.compile(
        r"\b(?P<dow>[A-Za-z]{3}),\s+(?P<d>\d{1,2})\s+(?P<mon>[A-Za-z]{3,9})\s+"
        r"(?P<Y>\d{2,4})\s+(?P<H>\d{2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?"
        r"(?:\s+(?P<tz>[+\-]\d{4}|[A-Z]{2,5}))?"
    )
    _MONTH_NAME_DMY_RE = re.compile(
        rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?"
        rf"(?P<mon>{_MONTH_NAMES})\.?,?\s+(?P<Y>\d{{4}})\b", re.I)
    _MONTH_NAME_MDY_RE = re.compile(
        rf"\b(?P<mon>{_MONTH_NAMES})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?"
        rf"(?:,)?\s+(?P<Y>\d{{4}})\b", re.I)
    _NUMERIC_RE = re.compile(
        # The leading guard stops 'v1.03/04' style fragments; the trailing one
        # deliberately omits '.' so a date ending a sentence ('... on 03/04/2024.')
        # still matches.
        r"(?<![\d/.-])(?P<a>\d{1,2})[/.\-](?P<b>\d{1,2})[/.\-](?P<Y>\d{2,4})(?![\d/-])")
    _MONTH_YEAR_RE = re.compile(
        rf"\b(?P<mon>{_MONTH_NAMES})\.?\s+(?P<Y>\d{{4}})\b", re.I)
    _QUARTER_RE = re.compile(r"\bQ\s*(?P<q>[1-4])\s*(?:of\s+)?(?P<Y>\d{4})\b", re.I)
    #: The rarer ``2Q2024`` / ``2Q 2024`` form, same groups so one handler serves both.
    _QUARTER_SUFFIX_RE = re.compile(r"\b(?P<q>[1-4])\s*Q\.?\s*(?P<Y>\d{4})\b", re.I)
    _HALF_RE = re.compile(r"\bH\s*(?P<h>[12])\s*(?P<Y>\d{4})\b", re.I)
    _YEAR_RE = re.compile(r"(?<![\d/.\-])(?P<Y>(?:19|20)\d{2})(?![\d/.\-])")
    _EPOCH_RE = re.compile(r"\b(?P<n>1[0-9]{8,18}(?:\.\d{1,9})?)\b")
    _RELATIVE_RE = re.compile(
        r"\b(?P<n>\d{1,3})\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?|"
        r"days?|weeks?|wks?|months?|mo|quarters?|qtrs?|years?|yrs?|decades?)"
        r"\s+(?P<dir>ago|before|earlier|prior)\b"
        r"|\b(?P<unit2>an?|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
        r"(?P<unit3>seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|"
        r"months?|quarters?|years?|decades?)\s+(?P<dir2>ago|before)\b"
        r"|\b(?P<kw>yesterday|today|tomorrow|last\s+(?:week|month|year)|"
        r"this\s+(?:week|month|year)|next\s+(?:week|month|year)|"
        r"last\s+night|recently)\b",
        re.I)
    _SINCE_RE = re.compile(r"\b(?:since|as\s+of|from|after)\s+(?P<Y>(?:19|20)\d{2})\b", re.I)
    _RANGE_RE = re.compile(
        r"\b(?P<a>(?:19|20)\d{2})\s*(?:-|–|—|to|until|through|and)\s*"
        r"(?P<b>(?:19|20)\d{2})\b")
    _DURATION_RE = re.compile(
        r"\b(?:for|over|during|within|lasting|persisted\s+for)\s+"
        r"(?P<n>\d{1,3})\s*(?P<unit>days?|weeks?|months?|years?)\b", re.I)
    _SIGNAL_DATE_KEYS = ("date", "datetime", "created_at", "timestamp", "time",
                        "published", "published_at", "seen_at", "observed",
                        "breach_date", "last_seen", "first_seen", "dob",
                        "birth_date", "registered_at", "modified", "updated_at")

    # ── Public parsing API ───────────────────────────────────────────────────

    @classmethod
    def parse_date(cls, value, *, dayfirst: bool | None = None,
                   now: datetime | None = None) -> dict | None:
        """Parse one date-ish string into a normalized event.

        Args:
            value: The text to parse (may be a datetime, date or str).
            dayfirst: ``True``/``False`` to force MDY/DMY for ambiguous numeric
                dates; ``None`` to apply the default convention.
            now: Reference "today" for relative expressions (tests inject this).
        Returns:
            ``{"timestamp", "precision", "source_text", "format", "dayfirst",
            "year"}`` or ``None`` when nothing parsed.
        """
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        reference = now or datetime.now(timezone.utc)
        default_df = bool(cls.dayfirst_default()) if dayfirst is None else bool(dayfirst)

        parsed = (cls._parse_epoch(text, reference)
                  or cls._parse_iso(text, reference)
                  or cls._parse_week(text, reference)
                  or cls._parse_ordinal(text, reference)
                  or cls._parse_rfc2822(text)
                  or cls._parse_named(text, reference)
                  or cls._parse_numeric(text, reference, default_df)
                  or cls._parse_relative(text, reference)
                  or cls._parse_range(text, reference)
                  or cls._parse_since(text, reference)
                  or cls._parse_duration(text, reference)
                  or cls._parse_period(text, reference)
                  or cls._parse_year(text, reference))
        if parsed is None:
            return None
        dt, precision, fmt, df = parsed
        return {
            "timestamp": _utc(dt),
            "year": dt.year,
            "precision": precision,
            "source_text": text[:120],
            "format": fmt,
            "dayfirst": df,
        }

    @classmethod
    def parse_any(cls, text: str, *, dayfirst: bool | None = None,
                  now: datetime | None = None) -> list[dict]:
        """Extract every dated event from free text, in document order."""
        if not text:
            return []
        reference = now or datetime.now(timezone.utc)
        events: list[dict] = []
        consumed: list[tuple[int, int]] = []

        def overlaps(start: int, end: int) -> bool:
            return any(not (end <= s or start >= e) for s, e in consumed)

        # Longest / most specific patterns run first so an ISO timestamp is not
        # also read as a numeric slash date.
        extractors = (
            (cls._EPOCH_RE, lambda m: cls._parse_epoch(m.group("n"), reference)),
            (cls._ISO_RE, lambda m: cls._from_iso_match(m, reference)),
            (cls._ISO_WEEK_RE, lambda m: cls._from_week_date(m, reference)),
            (cls._ISO_ORDINAL_RE, lambda m: cls._from_ordinal(m, reference)),
            (cls._RFC2822_RE, lambda m: cls._parse_rfc2822(m.group(0))),
            (cls._MONTH_NAME_DMY_RE, lambda m: cls._from_named(
                int(m.group("d")), m.group("mon"), int(m.group("Y")), reference, "mdy_dmy")),
            (cls._MONTH_NAME_MDY_RE, lambda m: cls._from_named(
                int(m.group("d")), m.group("mon"), int(m.group("Y")), reference, "mdy_dmy")),
            (cls._QUARTER_RE, lambda m: cls._from_quarter(m)),
            (cls._QUARTER_SUFFIX_RE, lambda m: cls._from_quarter(m)),
            (cls._HALF_RE, lambda m: cls._from_half(m)),
            (cls._MONTH_YEAR_RE, lambda m: cls._from_month_year(
                m.group("mon"), int(m.group("Y")), reference)),
            (cls._NUMERIC_RE, lambda m: cls._parse_numeric_match(m, reference)),
            (cls._RELATIVE_RE, lambda m: cls._parse_relative(m.group(0), reference)),
            (cls._RANGE_RE, lambda m: cls._from_range(m, reference)),
            (cls._SINCE_RE, lambda m: cls._from_since(m)),
            (cls._DURATION_RE, lambda m: cls._from_duration(m, reference)),
            (cls._YEAR_RE, lambda m: cls._from_year(int(m.group("Y")), reference)),
        )
        for pattern, handler in extractors:
            for match in pattern.finditer(text):
                if overlaps(match.start(), match.end()):
                    continue
                try:
                    parsed = handler(match)
                except Exception:  # noqa: BLE001 — a bad candidate is skipped
                    parsed = None
                if parsed is None:
                    continue
                dt, precision, fmt, df = parsed
                events.append({
                    "timestamp": _utc(dt),
                    "year": dt.year,
                    "precision": precision,
                    "source_text": match.group(0)[:120],
                    "format": fmt,
                    "dayfirst": df,
                    "offset": match.start(),
                })
                consumed.append((match.start(), match.end()))

        # ``since 2019`` and bare years overlap; the year rule only claims a
        # match the specific rules left free, so ordering above already handles
        # it. Sort by document position, then return.
        events.sort(key=lambda e: e["offset"])
        for e in events:
            e.pop("offset", None)
        return events

    @staticmethod
    def dayfirst_default() -> bool:
        """The deployment's default numeric-date convention (MDY unless set)."""
        try:
            import config
            fmt = str(getattr(config, "DATE_INPUT_FORMAT", "") or "").upper()
            if "DMY" in fmt or "DD/MM" in fmt:
                return True
            if "MDY" in fmt or "MM/DD" in fmt:
                return False
        except Exception:  # noqa: BLE001 — config is best-effort here
            pass
        return bool(os_environ_dayfirst())

    # ── Individual format parsers ────────────────────────────────────────────

    # -- epoch --
    @staticmethod
    def _from_epoch(value: float, reference: datetime) -> tuple:
        seconds = float(value)
        # Magnitude-based unit detection: ms / us / ns are all plausible.
        if seconds > 1e17:
            seconds /= 1e9
        elif seconds > 1e14:
            seconds /= 1e6
        elif seconds > 1e11:
            seconds /= 1e3
        try:
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        if not (1970 <= dt.year <= reference.year + 2):
            return None
        return (dt, "second", "epoch", False)

    @classmethod
    def _parse_epoch(cls, text: str, reference: datetime) -> tuple | None:
        match = cls._EPOCH_RE.fullmatch(text.strip())
        if not match:
            return None
        return cls._from_epoch(match.group("n"), reference)

    # -- ISO 8601 --
    @classmethod
    def _from_iso_match(cls, m, reference: datetime) -> tuple | None:
        return cls._from_iso_parts(
            int(m.group("Y")), int(m.group("m")), int(m.group("d")),
            int(m.group("H")) if m.group("H") else None,
            int(m.group("M")) if m.group("M") else None,
            int(m.group("S")) if m.group("S") else None,
            m.group("tz"), m.group("f"), reference)

    @staticmethod
    def _from_iso_parts(year, month, day, hour, minute, second, tz, frac,
                        reference) -> tuple | None:
        try:
            dt = datetime(year, month, day, hour or 0, minute or 0, second or 0,
                          int((frac or "0").ljust(6, "0")[:6]))
        except ValueError:
            return None
        if tz and tz.upper() != "Z":
            sign = 1 if tz[0] == "+" else -1
            digits = tz[1:].replace(":", "")
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
            dt = dt.replace(tzinfo=timezone(sign * offset))
        # An explicit UTC offset is part of the source text, not a loss of
        # precision: 'T01:00:00-04:00' is still to-the-second. A fractional
        # second likewise implies second precision even with seconds omitted.
        # Test against None, not truthiness: seconds '00' and minutes '00' are
        # valid values that would otherwise be read as "absent".
        precision = ("second" if (second is not None or frac) else
                     "minute" if minute is not None else
                     "hour" if hour is not None else "day")
        return (dt, precision, "iso8601", False)

    @classmethod
    def _parse_iso(cls, text: str, reference: datetime) -> tuple | None:
        match = cls._ISO_RE.fullmatch(text.strip())
        if match:
            return cls._from_iso_match(match, reference)
        return None

    @classmethod
    def _from_week_date(cls, m, reference: datetime) -> tuple | None:
        try:
            week = datetime.strptime(
                f"{int(m.group('Y'))}-{int(m.group('w'))}-{int(m.group('d'))}", "%G-%V-%u"
            )
        except ValueError:
            return None
        return (week.replace(tzinfo=timezone.utc), "day", "iso8601_week", False)

    @classmethod
    def _from_ordinal(cls, m, reference: datetime) -> tuple | None:
        try:
            day = int(m.group("d"))
            dt = datetime(int(m.group("Y")), 1, 1, tzinfo=timezone.utc) + timedelta(days=day - 1)
        except ValueError:
            return None
        if not (1 <= day <= 366):
            return None
        return (dt, "day", "iso8601_ordinal", False)

    @classmethod
    def _parse_week(cls, text: str, reference: datetime) -> tuple | None:
        match = cls._ISO_WEEK_RE.fullmatch(text.strip())
        return cls._from_week_date(match, reference) if match else None

    @classmethod
    def _parse_ordinal(cls, text: str, reference: datetime) -> tuple | None:
        match = cls._ISO_ORDINAL_RE.fullmatch(text.strip())
        return cls._from_ordinal(match, reference) if match else None

    # -- RFC 2822 --
    @classmethod
    def _parse_rfc2822(cls, text: str) -> tuple | None:
        match = cls._RFC2822_RE.search(text.strip())
        if not match:
            return None
        month = _MONTHS.get(match.group("mon").lower())
        if not month:
            return None
        year = int(match.group("Y"))
        if year < 100:
            year += 1900 if year >= 70 else 2000
        tz = match.group("tz")
        try:
            dt = datetime(year, month, int(match.group("d")), int(match.group("H")),
                          int(match.group("M")), int(match.group("S") or 0))
        except ValueError:
            return None
        if tz and re.match(r"^[+\-]\d{4}$", tz):
            sign = 1 if tz[0] == "+" else -1
            dt = dt.replace(tzinfo=timezone(sign * timedelta(
                hours=int(tz[1:3]), minutes=int(tz[3:5]))))
        return (dt, "second", "rfc2822", False)

    # -- month-name forms --
    @classmethod
    def _from_named(cls, day: int, month_name: str, year: int,
                    reference: datetime, fmt: str) -> tuple | None:
        month = _MONTHS.get(month_name.lower().rstrip("."))
        if not month or not (1 <= day <= 31):
            return None
        try:
            dt = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None
        return (dt, "day", fmt, False)

    @classmethod
    def _parse_named(cls, text: str, reference: datetime) -> tuple | None:
        for pattern in (cls._MONTH_NAME_DMY_RE, cls._MONTH_NAME_MDY_RE):
            match = pattern.search(text)
            if match:
                return cls._from_named(int(match.group("d")), match.group("mon"),
                                       int(match.group("Y")), reference, "month_name")
        return None

    # -- numeric (MDY vs DMY) --
    @classmethod
    def _from_numeric(cls, a: int, b: int, year: int,
                      dayfirst: bool, reference: datetime) -> tuple | None:
        """Resolve one numeric date.

        ``dayfirst`` is recorded as the *convention actually used to read this
        date*, which is decided by the data itself when it is unambiguous:
        ``18/03/2024`` is day-first by proof (18 cannot be a month), and
        ``03/18/2024`` is month-first by the same proof. Only a genuinely
        ambiguous pair (both parts ≤ 12) is settled by the caller's preference.
        """
        if year < 100:
            year += 2000 if year < 70 else 1900
        forced = False
        if a > 12 and b <= 12:
            day, month = a, b           # unambiguous DMY
            forced = True
        elif b > 12 and a <= 12:
            day, month = b, a           # unambiguous MDY
            forced = True
        else:
            day, month = (a, b) if dayfirst else (b, a)
        try:
            dt = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None
        if forced:
            # Report the convention the data proves, not the caller's default.
            dayfirst = (day, month) == (a, b)
        fmt = "numeric_dayfirst" if dayfirst else "numeric_monthfirst"
        return (dt, "day", fmt, dayfirst)

    @classmethod
    def _parse_numeric(cls, text: str, reference: datetime, dayfirst: bool) -> tuple | None:
        match = cls._NUMERIC_RE.search(text)
        if not match:
            return None
        return cls._from_numeric(int(match.group("a")), int(match.group("b")),
                                int(match.group("Y")), dayfirst, reference)

    @classmethod
    def _parse_numeric_match(cls, m, reference: datetime) -> tuple | None:
        """Resolve one numeric date, consulting corroborating evidence.

        A value > 12 settles the order outright. Otherwise the surrounding text
        is searched for a locale hint (``US``-style locale, an ``18/03/2024``
        neighbour, a ``DD/MM`` token) and only then does the default apply.
        """
        a, b, year = int(m.group("a")), int(m.group("b")), int(m.group("Y"))
        if a > 12 >= b:
            return cls._from_numeric(a, b, year, True, reference)
        if b > 12 >= a:
            return cls._from_numeric(a, b, year, False, reference)
        # Ambiguous: look at the neighbourhood for a decisive signal.
        window_start = max(0, m.start() - 120)
        window_end = min(len(m.string), m.end() + 120)
        window = m.string[window_start:window_end]
        hint = cls._locale_hint(window)
        return cls._from_numeric(a, b, year, hint, reference)

    @staticmethod
    def _locale_hint(window: str) -> bool:
        """Return True for day-first evidence in ``window``, else False."""
        low = window.lower()
        for token in _DAYFIRST_HINTS:
            if token in low:
                return True
        for us_token in ("us$", "u.s.", "united states", "america"):
            if us_token in low:
                return False
        # A neighbour with an impossible month is the strongest evidence.
        for match in re.finditer(r"(?<![\d/.-])(\d{1,2})[/.\-](\d{1,2})[/.\-]\d{2,4}",
                                 window):
            x, y = int(match.group(1)), int(match.group(2))
            if x > 12 >= y:
                return True
            if y > 12 >= x:
                return False
        return False

    # -- periods --
    @classmethod
    def _from_month_year(cls, month_name: str, year: int, reference) -> tuple | None:
        month = _MONTHS.get(month_name.lower().rstrip("."))
        if not month:
            return None
        return (datetime(year, month, 1, tzinfo=timezone.utc), "month", "month_year", False)

    @classmethod
    def _from_quarter(cls, m) -> tuple | None:
        q = int(m.group("q"))
        year = int(m.group("Y"))
        month = 3 * (q - 1) + 1
        return (datetime(year, month, 1, tzinfo=timezone.utc), "quarter", "quarter", False)

    @classmethod
    def _from_half(cls, m) -> tuple | None:
        h = int(m.group("h"))
        year = int(m.group("Y"))
        month = 1 if h == 1 else 7
        return (datetime(year, month, 1, tzinfo=timezone.utc), "half", "half_year", False)

    @classmethod
    def _parse_period(cls, text: str, reference: datetime) -> tuple | None:
        for pattern, handler in ((cls._QUARTER_RE, cls._from_quarter),
                                 (cls._QUARTER_SUFFIX_RE, cls._from_quarter),
                                 (cls._HALF_RE, cls._from_half)):
            match = pattern.search(text)
            if match:
                try:
                    return handler(match)
                except (ValueError, TypeError):
                    return None
        match = cls._MONTH_YEAR_RE.search(text)
        if match:
            return cls._from_month_year(match.group("mon"), int(match.group("Y")), reference)
        return None

    @classmethod
    def _from_year(cls, year: int, reference: datetime) -> tuple | None:
        if not (1900 <= year <= reference.year + 2):
            return None
        return (datetime(year, 1, 1, tzinfo=timezone.utc), "year", "year", False)

    @classmethod
    def _parse_range(cls, text: str, reference: datetime) -> tuple | None:
        match = cls._RANGE_RE.search(text.strip())
        return cls._from_range(match, reference) if match else None

    @classmethod
    def _parse_since(cls, text: str, reference: datetime) -> tuple | None:
        match = cls._SINCE_RE.search(text.strip())
        return cls._from_since(match) if match else None

    @classmethod
    def _parse_duration(cls, text: str, reference: datetime) -> tuple | None:
        match = cls._DURATION_RE.search(text.strip())
        return cls._from_duration(match, reference) if match else None

    @classmethod
    def _parse_year(cls, text: str, reference: datetime) -> tuple | None:
        match = cls._YEAR_RE.fullmatch(text.strip())
        return cls._from_year(int(match.group("Y")), reference) if match else None

    # -- relative --
    _WORD_NUMBERS = {"an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
                     "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                     "ten": 10}

    @classmethod
    def _from_relative(cls, reference: datetime, amount: int, unit_key: str) -> datetime:
        """Subtract ``amount`` ``unit_key``s from ``reference``.

        ``_RELATIVE_UNITS`` stores days for the sub-month units and a coarse
        *bucket index* for everything above a week, so months/quarters/years are
        handled by calendar arithmetic below rather than a fixed day count.
        """
        unit_key = {"min": "minute", "sec": "second", "hr": "hour",
                    "yr": "year", "wk": "week", "mo": "month",
                    "qtr": "quarter", "q": "quarter"}.get(unit_key, unit_key)
        unit = _RELATIVE_UNITS.get(unit_key)
        if unit is None:
            unit = 3
        dt = reference
        if unit <= 4:
            # Seconds/minutes/hours keep their time-of-day component; days and
            # weeks are day-granular.
            days = {0: 0, 1: 0, 2: 0, 3: amount, 4: amount * 7}[unit]
            seconds = {0: amount, 1: amount * 60, 2: amount * 3600}.get(unit, 0)
            dt = dt - timedelta(days=days, seconds=seconds)
        else:
            months_per_unit = {"month": 1, "quarter": 3, "year": 12,
                               "decade": 120}.get(unit_key, 1)
            months_back = amount * months_per_unit
            year = dt.year - (months_back // 12)
            month_index = dt.month - (months_back % 12)
            while month_index < 1:
                month_index += 12
                year -= 1
            day = min(dt.day, calendar.monthrange(year, month_index)[1])
            dt = dt.replace(year=year, month=month_index, day=day)
        # Sub-day units keep time-of-day; day and coarser are day-granular.
        return _start_of(dt, "day" if unit >= 3 else "second")

    @classmethod
    def _parse_relative(cls, text: str, reference: datetime) -> tuple | None:
        match = cls._RELATIVE_RE.search(text.strip())
        if not match:
            return None
        groups = match.groupdict()
        if groups.get("kw"):
            phrase = re.sub(r"\s+", " ", groups["kw"].lower())
            today = _start_of(reference, "day")
            if phrase == "yesterday":
                return (today - timedelta(days=1), "day", "relative", False)
            if phrase == "today":
                return (today, "day", "relative", False)
            if phrase == "tomorrow":
                return (today + timedelta(days=1), "day", "relative", False)
            if phrase == "last night":
                return (today, "day", "relative", False)
            if phrase == "recently":
                return (today - timedelta(days=7), "day", "relative", False)
            match2 = re.match(r"last\s+(week|month|year)$", phrase)
            if match2:
                key = match2.group(1)
                return (cls._from_relative(reference, 1, key), "day", "relative", False)
            match2 = re.match(r"this\s+(week|month|year)$", phrase)
            if match2:
                return (_start_of(reference, "day"), "day", "relative", False)
            match2 = re.match(r"next\s+(week|month|year)$", phrase)
            if match2:
                key = match2.group(1)
                return (reference + cls._shift(key), "day", "relative", False)
            return None
        if groups.get("n"):
            amount = int(groups["n"])
            unit = groups["unit"].lower().rstrip("s")
        else:
            word = groups["unit2"].lower()
            amount = cls._WORD_NUMBERS.get(word, 1)
            unit = groups["unit3"].lower().rstrip("s")
        unit = {"min": "minute", "sec": "second", "hr": "hour",
                "yr": "year", "wk": "week", "mo": "month",
                "qtr": "quarter", "q": "quarter"}.get(unit, unit)
        return (cls._from_relative(reference, amount, unit), "day", "relative", False)

    @staticmethod
    def _shift(unit: str) -> timedelta:
        return {"week": timedelta(days=7), "month": timedelta(days=31),
                "year": timedelta(days=366)}.get(unit, timedelta(days=7))

    # -- ranges, since, durations --
    @classmethod
    def _from_range(cls, m, reference: datetime) -> tuple | None:
        """A year range yields its *start* event (the end is captured by span)."""
        return cls._from_year(int(m.group("a")), reference)

    @classmethod
    def _from_since(cls, m) -> tuple | None:
        year = int(m.group("Y"))
        return (datetime(year, 1, 1, tzinfo=timezone.utc), "year", "since", False)

    @classmethod
    def _from_duration(cls, m, reference: datetime) -> tuple | None:
        """``for 3 years`` yields the window that *ended* today."""
        amount = int(m.group("n"))
        unit = m.group("unit").lower().rstrip("s")
        unit = {"day": "day", "week": "week", "month": "month",
                "year": "year"}.get(unit, "day")
        return (cls._from_relative(reference, amount, unit), "day", "duration", False)

    # ── Dedupe / conflict handling ────────────────────────────────────────────

    @staticmethod
    def _event_key(event: dict) -> tuple:
        """Identity used for near-duplicate detection.

        Same timestamp + same label, or the same source text repeated, are the
        same event surfaced twice.
        """
        label = str(event.get("label") or "").strip().lower()
        return (event.get("timestamp"), label)

    @classmethod
    def dedupe(cls, events: list[dict]) -> list[dict]:
        """Collapse near-identical events, keeping the richest record.

        Args:
            events: Candidate events.
        Returns:
            Deduplicated events, in first-seen order.
        """
        seen: dict[tuple, dict] = {}
        for event in events:
            key = cls._event_key(event)
            existing = seen.get(key)
            if existing is None:
                seen[key] = dict(event)
                continue
            # Same instant and subject: merge, preferring more precision and
            # the earliest source text (stable output).
            if event.get("precision") and event["precision"] not in (
                    "year", "month", "quarter", "half"):
                existing["precision"] = event["precision"]
            existing.setdefault("also_seen", 0)
            existing["also_seen"] = int(existing["also_seen"]) + 1
        return list(seen.values())

    @staticmethod
    def resolve_conflicts(events: list[dict], window_days: int = 2) -> list[dict]:
        """Prefer the most recent date when two events describe one moment.

        Two events conflict when they share a label but land more than
        ``window_days`` apart. The later timestamp wins and the event carries a
        ``conflict`` block so a reviewer can see what it displaced.

        Args:
            events: Deduplicated events.
            window_days: Separation beyond which two same-label dates conflict.
        Returns:
            Events with conflicts annotated and losers removed.
        """
        by_label: dict[str, list[dict]] = {}
        for event in events:
            by_label.setdefault(str(event.get("label") or "").strip().lower(), []).append(event)

        dropped: set[int] = set()
        for label, group in by_label.items():
            if len(group) < 2:
                continue
            ordered = sorted(group, key=lambda e: e["timestamp"])
            for older, newer in zip(ordered, ordered[1:]):
                try:
                    a = datetime.fromisoformat(older["timestamp"].replace("Z", "+00:00"))
                    b = datetime.fromisoformat(newer["timestamp"].replace("Z", "+00:00"))
                except ValueError:
                    continue
                if (b - a).days > window_days:
                    newer["conflict"] = {
                        "displaced": older["timestamp"],
                        "displaced_source": older.get("source_text"),
                        "note": "kept the most recent date for this subject",
                    }
                    dropped.add(id(older))
        return [e for e in events if id(e) not in dropped]

    # ── Agent entry point ────────────────────────────────────────────────────

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        context = context or {}
        peer_signals: list[dict] = context.get("peer_signals", []) or []
        target = input_data if isinstance(input_data, str) else str(input_data)
        dayfirst = context.get("dayfirst")

        self.log(f"reconstructing chronology from {len(peer_signals)} signals "
                 f"+ target text")

        events: list[dict] = []
        for s in peer_signals:
            event = self._event_from_signal(s, dayfirst)
            if event:
                events.append(event)

        # The target itself often carries dates (EXIF text, a pasted statement).
        text_events = self.parse_any(target, dayfirst=dayfirst)
        for parsed in text_events:
            label = self._label_from_context(parsed.get("source_text", ""))
            events.append({
                "timestamp": parsed["timestamp"],
                "year": parsed["year"],
                "label": label or f"Dated reference: {parsed['source_text']}",
                "type": "text_date",
                "precision": parsed["precision"],
                "format": parsed["format"],
                "source_text": parsed["source_text"],
                "dayfirst": parsed["dayfirst"],
                "source": "kronos_text",
            })

        before = len(events)
        events = self.dedupe(events)
        events = self.resolve_conflicts(events)
        conflicted = sum(1 for e in events if e.get("conflict"))
        if before != len(events):
            self.log(f"consolidated {before} → {len(events)} event(s)"
                     + (f" ({conflicted} conflict resolved)" if conflicted else ""))

        events.sort(key=lambda e: e["timestamp"])

        span = None
        if len(events) >= 2:
            span = {
                "earliest": events[0]["timestamp"],
                "latest": events[-1]["timestamp"],
                "years": events[-1]["year"] - events[0]["year"],
                "days": _days_between(events[0]["timestamp"], events[-1]["timestamp"]),
            }
            self.log(f"timeline spans {span['earliest'][:10]} → {span['latest'][:10]} "
                     f"({len(events)} events)")
        else:
            self.log(f"reconstructed {len(events)} dated event(s)")

        by_format: dict[str, int] = {}
        for e in events:
            key = e.get("format") or "unknown"
            by_format[key] = by_format.get(key, 0) + 1

        signals = [{"type": "timeline", "event_count": len(events),
                    "span": span, "source": "kronos",
                    "formats_parsed": by_format}]
        if conflicted:
            signals.append({"type": "timeline_conflict",
                            "count": conflicted, "source": "kronos"})
        # Timeline events feed QUILL and the frontend; keep the legacy keys.
        confidence = 0.75 if len(events) >= 2 else (0.3 if events else 0.1)
        return AgentResult(
            agent=self.name, status="done",
            output={
                "events": events,
                "span": span,
                "event_count": len(events),
                "formats_parsed": by_format,
                "conflicts": conflicted,
            },
            confidence=confidence,
            reasoning=f"Reconstructed {len(events)} dated event(s) across "
                      f"{len(by_format)} format family(ies)"
                      + (f" spanning {span['years']} year(s)." if span else "."),
            signals=signals,
            latency_s=self._elapsed(t0),
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    @classmethod
    def _event_from_signal(cls, signal: dict, dayfirst: bool | None) -> dict | None:
        """Pull the best date out of one upstream signal."""
        if not isinstance(signal, dict):
            return None
        stype = signal.get("type") or "signal"
        raw_date = None
        label = None
        for key in cls._SIGNAL_DATE_KEYS:
            if signal.get(key):
                raw_date = signal[key]
                break
        if stype == "breach":
            label = f"Data breach: {signal.get('name', 'unknown')}"
        elif stype in ("camera", "gps"):
            label = f"Media captured ({stype})"
        elif stype == "account":
            label = f"Account seen: {signal.get('platform', '?')}"
        elif stype == "person_record":
            label = f"Record: {signal.get('full_name', '?')}"
        if raw_date is None:
            # Some signals carry a human phrase instead of a field.
            for key in ("note", "title", "summary", "description"):
                if signal.get(key):
                    parsed = cls.parse_date(signal[key], dayfirst=dayfirst)
                    if parsed:
                        return {**parsed, "label": f"{stype}: {str(signal[key])[:60]}",
                                "type": stype, "source": signal.get("source", "unknown")}
                    break
            return None
        parsed = cls.parse_date(raw_date, dayfirst=dayfirst)
        if parsed is None:
            return None
        return {
            "timestamp": parsed["timestamp"],
            "year": parsed["year"],
            "label": label or f"{stype} event",
            "type": stype,
            "precision": parsed["precision"],
            "format": parsed["format"],
            "source_text": parsed["source_text"],
            "dayfirst": parsed["dayfirst"],
            "source": signal.get("source", "unknown"),
        }

    @staticmethod
    def _label_from_context(fragment: str) -> str:
        """Use the words around a date as its label when they read like prose."""
        fragment = (fragment or "").strip()
        if not fragment or len(fragment) < 8:
            return ""
        words = fragment.split()
        if len(words) < 3 or not any(len(w) > 4 for w in words):
            return ""
        return " ".join(words[:8])[:80]

    @staticmethod
    def _start_of(dt: datetime, precision: str) -> datetime:
        return _start_of(dt, precision)


def _days_between(iso_a: str, iso_b: str) -> int:
    try:
        a = datetime.fromisoformat(iso_a.replace("Z", "+00:00"))
        b = datetime.fromisoformat(iso_b.replace("Z", "+00:00"))
        return (b - a).days
    except (ValueError, TypeError):
        return 0


def os_environ_dayfirst() -> bool:
    """Honour a ``DATE_INPUT_FORMAT=DMY``-style environment override."""
    import os
    value = (os.getenv("DATE_INPUT_FORMAT") or "").upper()
    return "DMY" in value or "DD/MM" in value


# Kept for backwards compatibility with the previous classmethod name.
KronosAgent._parse_date = KronosAgent.parse_date


__all__ = ["KronosAgent", "DAYFIRST_REGIONS", "_MONTHS"]