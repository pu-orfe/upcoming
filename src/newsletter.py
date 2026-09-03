"""Newsletter edition scheduling for the ORFE weekly events newsletter.

The Engineering events newsletter publishes each Monday around noon -- except
Labor Day week, when it publishes Tuesday -- and the deadline to submit an event
is the Tuesday preceding publication at noon. Both can change between semesters,
so the schedule lives in ``newsletter_config.json`` rather than in code.

Every edition derives from a *week anchor*: ``week_start``, the Monday of the ISO
week. Publication, deadline and the coverage window are each expressed as
``anchor + offset_days @ time_of_day``, where the anchor is either the week start
or the (possibly shifted) publication date::

    bound            anchor        offset  time       Labor Day week 2026-09-07
    publication      week_start    0       12:00:00   Tue 09-08  (exception)
    deadline         week_start    -6      12:00:00   Tue 09-01
    coverage start   publication   0       00:00:00   Tue 09-08
    coverage end     week_start    +6      23:59:59   Sun 09-13

The mixed anchoring is deliberate: coverage *start* follows a publication shift
("publication day 00:00") while coverage *end* stays pinned to the week ("through
that Sunday"). Anchoring the deadline to the week start means moving publication
does not drag the submission deadline with it.

Deliberately stdlib-only. It is imported by a CI step that runs before
``pip install``, and the arithmetic here is calendar-*date* arithmetic where
``date + timedelta`` is DST-exact by construction; ``zoneinfo`` is then used for
the single localization step, with PEP 495 ``fold`` pinned so results reproduce.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_CONFIG_FILENAME = "newsletter_config.json"

ENV_CONFIG = "NEWSLETTER_CONFIG"
ENV_TZ = "NEWSLETTER_TZ"
ENV_PUBLISH_WEEKDAY = "NEWSLETTER_PUBLISH_WEEKDAY"
ENV_PUBLISH_TIME = "NEWSLETTER_PUBLISH_TIME"
ENV_DEADLINE_WEEKDAY = "NEWSLETTER_DEADLINE_WEEKDAY"
ENV_DEADLINE_OFFSET_DAYS = "NEWSLETTER_DEADLINE_OFFSET_DAYS"
ENV_DEADLINE_TIME = "NEWSLETTER_DEADLINE_TIME"
ENV_REMINDER_LEAD_HOURS = "NEWSLETTER_REMINDER_LEAD_HOURS"

#: date.weekday() semantics: Monday is 0, Sunday is 6. 7 is rejected on purpose,
#: because accepting both conventions is how an off-by-one gets shipped.
WEEKDAY_NAMES: dict[str, int] = {
    "MON": 0, "MONDAY": 0,
    "TUE": 1, "TUES": 1, "TUESDAY": 1,
    "WED": 2, "WEDS": 2, "WEDNESDAY": 2,
    "THU": 3, "THUR": 3, "THURS": 3, "THURSDAY": 3,
    "FRI": 4, "FRIDAY": 4,
    "SAT": 5, "SATURDAY": 5,
    "SUN": 6, "SUNDAY": 6,
}

#: The feed writes naive local wall clock; see schema/events.schema.json.
EVENT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"

EVENT_START_FIELD = "startTime"
EDITION_FIELD = "newsletterEdition"


class NewsletterConfigError(ValueError):
    """The newsletter configuration is malformed or unusable."""


class InvalidEventTime(ValueError):
    """An event timestamp is not the naive-local format the feed promises."""


class NoEditionFound(LookupError):
    """No edition exists within the configured lookahead horizon."""


class Anchor(str, Enum):
    WEEK_START = "week_start"
    PUBLICATION = "publication"


class EditionPhase(str, Enum):
    OPEN = "open"            # now < deadline_at: submissions still accepted
    CLOSED = "closed"        # deadline passed, not yet published
    PUBLISHED = "published"  # publication_at <= now


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_weekday(value: object, *, field_name: str = "weekday") -> int:
    """Parse a weekday as Mon=0..Sun=6. Accepts 'MON', 'Monday', or 0."""
    if isinstance(value, bool):
        raise NewsletterConfigError(f"{field_name}: expected a weekday, got {value!r}")
    if isinstance(value, int):
        number = value
    else:
        text = str(value).strip()
        if not text:
            raise NewsletterConfigError(f"{field_name}: empty weekday")
        upper = text.upper()
        if upper in WEEKDAY_NAMES:
            return WEEKDAY_NAMES[upper]
        try:
            number = int(text)
        except ValueError:
            raise NewsletterConfigError(
                f"{field_name}: unknown weekday {value!r}; use MON..SUN or 0..6"
            ) from None
    if not 0 <= number <= 6:
        raise NewsletterConfigError(
            f"{field_name}: weekday must be 0 (Monday) through 6 (Sunday), got {number}. "
            "ISO numbering (1..7) is not accepted."
        )
    return number


def parse_time(value: object, *, field_name: str = "time") -> time:
    """Parse 'HH:MM' or 'HH:MM:SS'."""
    if isinstance(value, time):
        return value
    text = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise NewsletterConfigError(f"{field_name}: expected HH:MM or HH:MM:SS, got {value!r}")


def parse_date(value: object, *, field_name: str = "date") -> date:
    """Parse an ISO 'YYYY-MM-DD' date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise NewsletterConfigError(
            f"{field_name}: expected YYYY-MM-DD, got {value!r}"
        ) from None


def _parse_lead_hours(value: object, *, field_name: str) -> tuple[float, ...]:
    if isinstance(value, str):
        items: Sequence[Any] = [p for p in value.split(",") if p.strip()]
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise NewsletterConfigError(f"{field_name}: expected a list or comma-separated string")
    leads: list[float] = []
    for item in items:
        try:
            leads.append(float(str(item).strip()))
        except ValueError:
            raise NewsletterConfigError(f"{field_name}: {item!r} is not a number") from None
    if any(lead < 0 for lead in leads):
        raise NewsletterConfigError(f"{field_name}: lead hours must not be negative")
    return tuple(sorted(set(leads), reverse=True))


def _format_lead(lead: float) -> str:
    """Render a lead as a stable key fragment: 72.0 -> '72', 1.5 -> '1.5'."""
    return str(int(lead)) if float(lead).is_integer() else str(lead)


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Bound:
    """One schedule bound, as an offset from an anchor date at a wall-clock time."""

    anchor: Anchor = Anchor.WEEK_START
    offset_days: int = 0
    at: time = time(0, 0, 0)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, default: "Bound", field_name: str) -> "Bound":
        anchor = default.anchor
        if "anchor" in data:
            try:
                anchor = Anchor(str(data["anchor"]))
            except ValueError:
                raise NewsletterConfigError(
                    f"{field_name}.anchor: expected 'week_start' or 'publication', "
                    f"got {data['anchor']!r}"
                ) from None
        offset = default.offset_days
        if "offset_days" in data:
            try:
                offset = int(data["offset_days"])
            except (TypeError, ValueError):
                raise NewsletterConfigError(
                    f"{field_name}.offset_days: expected an integer"
                ) from None
        at = parse_time(data["time"], field_name=f"{field_name}.time") if "time" in data else default.at
        return cls(anchor=anchor, offset_days=offset, at=at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor": self.anchor.value,
            "offset_days": self.offset_days,
            "time": self.at.isoformat(),
        }


DEFAULT_DEADLINE = Bound(Anchor.WEEK_START, -6, time(12, 0, 0))
DEFAULT_COVERAGE_START = Bound(Anchor.PUBLICATION, 0, time(0, 0, 0))
DEFAULT_COVERAGE_END = Bound(Anchor.WEEK_START, 6, time(23, 59, 59))


@dataclass(frozen=True)
class ScheduleRule:
    """A schedule in force over a (possibly open-ended) range of weeks."""

    label: str = "default"
    effective_from: date | None = None   # inclusive, compared against week_start
    effective_to: date | None = None     # inclusive
    publish_weekday: int = 0
    publish_time: time = time(12, 0, 0)
    deadline: Bound = DEFAULT_DEADLINE
    coverage_start: Bound = DEFAULT_COVERAGE_START
    coverage_end: Bound = DEFAULT_COVERAGE_END
    reminder_lead_hours: tuple[float, ...] = (72.0, 48.0, 24.0, 4.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "publication": {"weekday": self.publish_weekday, "time": self.publish_time.isoformat()},
            "deadline": self.deadline.to_dict(),
            "coverage": {"start": self.coverage_start.to_dict(), "end": self.coverage_end.to_dict()},
            "reminders": {"lead_hours": list(self.reminder_lead_hours)},
        }


@dataclass(frozen=True)
class EditionException:
    """A one-off override for a single week, keyed by its week-start Monday."""

    week_of: date
    publication_date: date | None = None
    publication_time: time | None = None
    deadline_date: date | None = None
    deadline_time: time | None = None
    coverage_start_date: date | None = None
    coverage_end_date: date | None = None
    skip: bool = False
    reason: str = ""


@dataclass(frozen=True)
class Blackout:
    """A range of weeks with no edition (recess, break)."""

    start: date
    end: date
    reason: str = ""

    def covers(self, week_start: date) -> bool:
        return self.start <= week_start <= self.end


@dataclass(frozen=True)
class Reminder:
    """One deadline reminder: when it fires, and a key for deduping notifications."""

    lead_hours: float
    fire_at: datetime
    key: str


@dataclass(frozen=True)
class NewsletterConfig:
    timezone: str = DEFAULT_TIMEZONE
    defaults: ScheduleRule = field(default_factory=ScheduleRule)
    schedules: tuple[ScheduleRule, ...] = ()
    exceptions: tuple[EditionException, ...] = ()
    blackouts: tuple[Blackout, ...] = ()
    max_weeks_lookahead: int = 60
    source_path: Path | None = None

    def tzinfo(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise NewsletterConfigError(
                f"unknown timezone {self.timezone!r}: {exc}. On a system without the IANA "
                "time zone database (notably the python:*-slim images and Windows), "
                "install the 'tzdata' package."
            ) from exc

    def rule_for(self, week_start: date) -> ScheduleRule:
        """The schedule in force for a week: latest matching effective_from wins."""
        best: ScheduleRule | None = None
        for rule in self.schedules:
            if rule.effective_from is not None and rule.effective_from > week_start:
                continue
            if rule.effective_to is not None and rule.effective_to < week_start:
                continue
            if (
                best is None
                or (rule.effective_from or date.min) >= (best.effective_from or date.min)
            ):
                best = rule
        return best or self.defaults

    def exception_for(self, week_start: date) -> EditionException | None:
        for exc in self.exceptions:
            if exc.week_of == week_start:
                return exc
        return None

    def is_blacked_out(self, week_start: date) -> bool:
        return any(b.covers(week_start) for b in self.blackouts)

    def fingerprint(self) -> str:
        """Stable SHA256 over the resolved config, env overrides included.

        CI compares this against the previous run so an edited schedule forces a
        rebuild even when the upstream ICS has not changed.
        """
        payload = {
            "timezone": self.timezone,
            "defaults": self.defaults.to_dict(),
            "schedules": [r.to_dict() for r in self.schedules],
            "exceptions": [
                {
                    "week_of": e.week_of.isoformat(),
                    "publication_date": e.publication_date.isoformat() if e.publication_date else None,
                    "publication_time": e.publication_time.isoformat() if e.publication_time else None,
                    "deadline_date": e.deadline_date.isoformat() if e.deadline_date else None,
                    "deadline_time": e.deadline_time.isoformat() if e.deadline_time else None,
                    "coverage_start_date": e.coverage_start_date.isoformat() if e.coverage_start_date else None,
                    "coverage_end_date": e.coverage_end_date.isoformat() if e.coverage_end_date else None,
                    "skip": e.skip,
                }
                for e in self.exceptions
            ],
            "blackouts": [
                {"start": b.start.isoformat(), "end": b.end.isoformat()} for b in self.blackouts
            ],
            "max_weeks_lookahead": self.max_weeks_lookahead,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Edition:
    """One published edition of the newsletter."""

    id: str
    week_start: date
    publication_at: datetime
    deadline_at: datetime
    coverage_start: datetime
    coverage_end: datetime
    timezone: str
    rule_label: str = "default"
    exception_reason: str | None = None
    reminders: tuple[Reminder, ...] = ()

    def covers(self, moment: datetime) -> bool:
        """True when an aware instant falls inside the coverage window (inclusive)."""
        return self.coverage_start <= moment <= self.coverage_end

    def phase(self, now: datetime) -> EditionPhase:
        if now >= self.publication_at:
            return EditionPhase.PUBLISHED
        if now >= self.deadline_at:
            return EditionPhase.CLOSED
        return EditionPhase.OPEN

    def notification_key(self, kind: str) -> str:
        return f"{self.id}:{kind}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "weekStart": self.week_start.isoformat(),
            "publicationAt": self.publication_at.isoformat(),
            "deadlineAt": self.deadline_at.isoformat(),
            "coverageStart": self.coverage_start.isoformat(),
            "coverageEnd": self.coverage_end.isoformat(),
            "timezone": self.timezone,
            "ruleLabel": self.rule_label,
            "exceptionReason": self.exception_reason,
            "reminders": [
                {"leadHours": r.lead_hours, "fireAt": r.fire_at.isoformat(), "key": r.key}
                for r in self.reminders
            ],
        }


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _rule_from_dict(
    data: Mapping[str, Any], *, base: ScheduleRule, field_name: str
) -> ScheduleRule:
    """Build a rule as a partial override merged onto `base`."""
    label = str(data.get("label", base.label))
    effective_from = (
        parse_date(data["effective_from"], field_name=f"{field_name}.effective_from")
        if data.get("effective_from")
        else base.effective_from
    )
    effective_to = (
        parse_date(data["effective_to"], field_name=f"{field_name}.effective_to")
        if data.get("effective_to")
        else base.effective_to
    )

    publication = data.get("publication") or {}
    if not isinstance(publication, Mapping):
        raise NewsletterConfigError(f"{field_name}.publication: expected an object")
    publish_weekday = (
        parse_weekday(publication["weekday"], field_name=f"{field_name}.publication.weekday")
        if "weekday" in publication
        else base.publish_weekday
    )
    publish_time = (
        parse_time(publication["time"], field_name=f"{field_name}.publication.time")
        if "time" in publication
        else base.publish_time
    )

    deadline = base.deadline
    if "deadline" in data:
        if not isinstance(data["deadline"], Mapping):
            raise NewsletterConfigError(f"{field_name}.deadline: expected an object")
        deadline = Bound.from_dict(
            data["deadline"], default=base.deadline, field_name=f"{field_name}.deadline"
        )

    coverage = data.get("coverage") or {}
    if not isinstance(coverage, Mapping):
        raise NewsletterConfigError(f"{field_name}.coverage: expected an object")
    coverage_start = base.coverage_start
    if "start" in coverage:
        coverage_start = Bound.from_dict(
            coverage["start"], default=base.coverage_start,
            field_name=f"{field_name}.coverage.start",
        )
    coverage_end = base.coverage_end
    if "end" in coverage:
        coverage_end = Bound.from_dict(
            coverage["end"], default=base.coverage_end,
            field_name=f"{field_name}.coverage.end",
        )

    reminders = data.get("reminders") or {}
    if not isinstance(reminders, Mapping):
        raise NewsletterConfigError(f"{field_name}.reminders: expected an object")
    lead_hours = (
        _parse_lead_hours(reminders["lead_hours"], field_name=f"{field_name}.reminders.lead_hours")
        if "lead_hours" in reminders
        else base.reminder_lead_hours
    )

    return ScheduleRule(
        label=label,
        effective_from=effective_from,
        effective_to=effective_to,
        publish_weekday=publish_weekday,
        publish_time=publish_time,
        deadline=deadline,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        reminder_lead_hours=lead_hours,
    )


def _exception_from_dict(data: Mapping[str, Any], index: int) -> EditionException:
    where = f"exceptions[{index}]"
    if "week_of" not in data:
        raise NewsletterConfigError(f"{where}: 'week_of' is required")
    week_of = parse_date(data["week_of"], field_name=f"{where}.week_of")
    if week_of.weekday() != 0:
        raise NewsletterConfigError(
            f"{where}.week_of: {week_of.isoformat()} is not a Monday; exceptions are "
            "keyed by the week-anchor Monday so the edition id stays stable."
        )
    def _opt_date(key: str) -> date | None:
        return parse_date(data[key], field_name=f"{where}.{key}") if data.get(key) else None

    def _opt_time(key: str) -> time | None:
        return parse_time(data[key], field_name=f"{where}.{key}") if data.get(key) else None

    return EditionException(
        week_of=week_of,
        publication_date=_opt_date("publication_date"),
        publication_time=_opt_time("publication_time"),
        deadline_date=_opt_date("deadline_date"),
        deadline_time=_opt_time("deadline_time"),
        coverage_start_date=_opt_date("coverage_start_date"),
        coverage_end_date=_opt_date("coverage_end_date"),
        skip=bool(data.get("skip", False)),
        reason=str(data.get("reason", "")),
    )


def _blackout_from_dict(data: Mapping[str, Any], index: int) -> Blackout:
    where = f"blackouts[{index}]"
    if "week_of" in data:
        one = parse_date(data["week_of"], field_name=f"{where}.week_of")
        return Blackout(start=one, end=one, reason=str(data.get("reason", "")))
    if "start" not in data or "end" not in data:
        raise NewsletterConfigError(f"{where}: needs either 'week_of' or both 'start' and 'end'")
    start = parse_date(data["start"], field_name=f"{where}.start")
    end = parse_date(data["end"], field_name=f"{where}.end")
    if end < start:
        raise NewsletterConfigError(f"{where}: end {end} precedes start {start}")
    return Blackout(start=start, end=end, reason=str(data.get("reason", "")))


def _deadline_offset_for_weekday(deadline_weekday: int, publish_weekday: int) -> int:
    """Offset from week_start for the most recent `deadline_weekday` before publication."""
    days_back = (publish_weekday - deadline_weekday) % 7 or 7
    return publish_weekday - days_back


def _apply_env_overrides(rule: ScheduleRule, env: Mapping[str, str]) -> ScheduleRule:
    """Env wins over the file, so it is applied to every rule uniformly."""
    updates: dict[str, Any] = {}
    if ENV_PUBLISH_WEEKDAY in env:
        updates["publish_weekday"] = parse_weekday(
            env[ENV_PUBLISH_WEEKDAY], field_name=ENV_PUBLISH_WEEKDAY
        )
    if ENV_PUBLISH_TIME in env:
        updates["publish_time"] = parse_time(env[ENV_PUBLISH_TIME], field_name=ENV_PUBLISH_TIME)
    if ENV_REMINDER_LEAD_HOURS in env:
        updates["reminder_lead_hours"] = _parse_lead_hours(
            env[ENV_REMINDER_LEAD_HOURS], field_name=ENV_REMINDER_LEAD_HOURS
        )

    publish_weekday = updates.get("publish_weekday", rule.publish_weekday)
    deadline = rule.deadline
    # A raw offset is unambiguous, so it wins over a weekday that must be resolved.
    if ENV_DEADLINE_OFFSET_DAYS in env:
        try:
            offset = int(str(env[ENV_DEADLINE_OFFSET_DAYS]).strip())
        except ValueError:
            raise NewsletterConfigError(
                f"{ENV_DEADLINE_OFFSET_DAYS}: expected an integer, "
                f"got {env[ENV_DEADLINE_OFFSET_DAYS]!r}"
            ) from None
        deadline = replace(deadline, anchor=Anchor.WEEK_START, offset_days=offset)
    elif ENV_DEADLINE_WEEKDAY in env:
        weekday = parse_weekday(env[ENV_DEADLINE_WEEKDAY], field_name=ENV_DEADLINE_WEEKDAY)
        deadline = replace(
            deadline,
            anchor=Anchor.WEEK_START,
            offset_days=_deadline_offset_for_weekday(weekday, publish_weekday),
        )
    if ENV_DEADLINE_TIME in env:
        deadline = replace(
            deadline, at=parse_time(env[ENV_DEADLINE_TIME], field_name=ENV_DEADLINE_TIME)
        )
    if deadline != rule.deadline:
        updates["deadline"] = deadline

    return replace(rule, **updates) if updates else rule


def load_newsletter_config(
    path: str | os.PathLike | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> NewsletterConfig:
    """Load the newsletter schedule from JSON, then apply environment overrides.

    Resolution order for the file, mirroring ``transform.load_config``: the
    explicit ``path`` argument, then ``$NEWSLETTER_CONFIG``, then
    ``./newsletter_config.json`` if it exists, else built-in defaults. A path that
    does not exist is not an error.

    Environment is read here rather than as dataclass field defaults, so tests can
    monkeypatch it.
    """
    env = os.environ if env is None else env

    chosen = path or env.get(ENV_CONFIG) or None
    if chosen is None and Path(DEFAULT_CONFIG_FILENAME).exists():
        chosen = DEFAULT_CONFIG_FILENAME

    data: dict[str, Any] = {}
    source_path: Path | None = None
    if chosen:
        candidate = Path(chosen)
        if candidate.exists():
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise NewsletterConfigError(f"{candidate}: invalid JSON: {exc}") from exc
            if not isinstance(loaded, Mapping):
                raise NewsletterConfigError(f"{candidate}: top level must be a JSON object")
            data = dict(loaded)
            source_path = candidate

    tz_name = (
        env.get(ENV_TZ)
        or data.get("timezone")
        or env.get("TARGET_TZ")
        or DEFAULT_TIMEZONE
    )

    defaults_data = data.get("defaults") or {}
    if not isinstance(defaults_data, Mapping):
        raise NewsletterConfigError("defaults: expected an object")
    defaults = _rule_from_dict(defaults_data, base=ScheduleRule(), field_name="defaults")
    defaults = _apply_env_overrides(defaults, env)

    schedules_data = data.get("schedules") or []
    if not isinstance(schedules_data, (list, tuple)):
        raise NewsletterConfigError("schedules: expected a list")
    schedules: list[ScheduleRule] = []
    for index, entry in enumerate(schedules_data):
        if not isinstance(entry, Mapping):
            raise NewsletterConfigError(f"schedules[{index}]: expected an object")
        rule = _rule_from_dict(entry, base=defaults, field_name=f"schedules[{index}]")
        schedules.append(_apply_env_overrides(rule, env))

    exceptions_data = data.get("exceptions") or []
    if not isinstance(exceptions_data, (list, tuple)):
        raise NewsletterConfigError("exceptions: expected a list")
    exceptions = tuple(
        _exception_from_dict(entry, index) for index, entry in enumerate(exceptions_data)
    )

    blackouts_data = data.get("blackouts") or []
    if not isinstance(blackouts_data, (list, tuple)):
        raise NewsletterConfigError("blackouts: expected a list")
    blackouts = tuple(
        _blackout_from_dict(entry, index) for index, entry in enumerate(blackouts_data)
    )

    try:
        lookahead = int(data.get("max_weeks_lookahead", 60))
    except (TypeError, ValueError):
        raise NewsletterConfigError("max_weeks_lookahead: expected an integer") from None
    if lookahead < 1:
        raise NewsletterConfigError("max_weeks_lookahead must be at least 1")

    cfg = NewsletterConfig(
        timezone=str(tz_name),
        defaults=defaults,
        schedules=tuple(schedules),
        exceptions=exceptions,
        blackouts=blackouts,
        max_weeks_lookahead=lookahead,
        source_path=source_path,
    )
    cfg.tzinfo()  # fail fast on an unknown zone rather than at first use
    return cfg


# ---------------------------------------------------------------------------
# Edition arithmetic
# ---------------------------------------------------------------------------

def week_start_for(moment: datetime | date) -> date:
    """The Monday of the week containing `moment`."""
    day = moment.date() if isinstance(moment, datetime) else moment
    return day - timedelta(days=day.weekday())


def _localize(day: date, at: time, tz: ZoneInfo) -> datetime:
    """Combine a date and wall-clock time into an aware instant.

    ``fold=0`` pins the first occurrence of an ambiguous wall clock (the repeated
    hour when DST ends) and the pre-transition offset for a nonexistent one, so
    results are reproducible.
    """
    return datetime.combine(day, at).replace(tzinfo=tz, fold=0)


def now_in_tz(cfg: NewsletterConfig, now: datetime | None = None) -> datetime:
    """Current time in the newsletter timezone, or `now` converted into it."""
    tz = cfg.tzinfo()
    if now is None:
        return datetime.now(tz)
    return now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz, fold=0)


def parse_as_of(value: str | None, cfg: NewsletterConfig) -> datetime:
    """Parse an ``--as-of`` value, defaulting to the real clock."""
    if not value:
        return now_in_tz(cfg)
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise NewsletterConfigError(
            f"--as-of: expected an ISO-8601 datetime, got {value!r}"
        ) from None
    return now_in_tz(cfg, parsed)


def _bound_datetime(
    bound: Bound, *, week_start: date, publication_date: date, tz: ZoneInfo,
    override_date: date | None = None, override_time: time | None = None,
) -> datetime:
    if override_date is not None:
        anchor_date = override_date
    else:
        base = week_start if bound.anchor is Anchor.WEEK_START else publication_date
        anchor_date = base + timedelta(days=bound.offset_days)
    return _localize(anchor_date, override_time or bound.at, tz)


def _build_reminders(
    edition_id: str, deadline_at: datetime, leads: Sequence[float], tz: ZoneInfo
) -> tuple[Reminder, ...]:
    """Reminder instants, computed through UTC.

    Subtracting a timedelta from an aware datetime is wall-clock arithmetic, not
    elapsed time; across a DST transition that is off by an hour. Going through
    UTC makes the lead a true elapsed duration.
    """
    deadline_utc = deadline_at.astimezone(timezone.utc)
    reminders = []
    for lead in leads:
        fire_at = (deadline_utc - timedelta(hours=lead)).astimezone(tz)
        reminders.append(
            Reminder(
                lead_hours=lead,
                fire_at=fire_at,
                key=f"{edition_id}:deadline-{_format_lead(lead)}h",
            )
        )
    return tuple(reminders)


def build_edition(cfg: NewsletterConfig, week_start: date) -> Edition | None:
    """Build the edition anchored to `week_start`, or None when there is none."""
    if week_start.weekday() != 0:
        week_start = week_start_for(week_start)
    if cfg.is_blacked_out(week_start):
        return None
    exc = cfg.exception_for(week_start)
    if exc is not None and exc.skip:
        return None

    tz = cfg.tzinfo()
    rule = cfg.rule_for(week_start)

    publication_date = week_start + timedelta(days=rule.publish_weekday)
    if exc is not None and exc.publication_date is not None:
        publication_date = exc.publication_date
    publication_time = rule.publish_time
    if exc is not None and exc.publication_time is not None:
        publication_time = exc.publication_time
    publication_at = _localize(publication_date, publication_time, tz)

    deadline_at = _bound_datetime(
        rule.deadline, week_start=week_start, publication_date=publication_date, tz=tz,
        override_date=exc.deadline_date if exc else None,
        override_time=exc.deadline_time if exc else None,
    )
    coverage_start = _bound_datetime(
        rule.coverage_start, week_start=week_start, publication_date=publication_date, tz=tz,
        override_date=exc.coverage_start_date if exc else None,
    )
    coverage_end = _bound_datetime(
        rule.coverage_end, week_start=week_start, publication_date=publication_date, tz=tz,
        override_date=exc.coverage_end_date if exc else None,
    )
    if coverage_end < coverage_start:
        raise NewsletterConfigError(
            f"edition {week_start.isoformat()}: coverage ends ({coverage_end.isoformat()}) "
            f"before it starts ({coverage_start.isoformat()})"
        )

    edition_id = week_start.isoformat()
    return Edition(
        id=edition_id,
        week_start=week_start,
        publication_at=publication_at,
        deadline_at=deadline_at,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        timezone=cfg.timezone,
        rule_label=rule.label,
        exception_reason=(exc.reason or None) if exc else None,
        reminders=_build_reminders(edition_id, deadline_at, rule.reminder_lead_hours, tz),
    )


def iter_editions(cfg: NewsletterConfig, start: date, count: int) -> Iterator[Edition]:
    """Yield up to `count` editions from the week containing `start` forward."""
    week = week_start_for(start)
    produced = 0
    scanned = 0
    limit = cfg.max_weeks_lookahead + count
    while produced < count and scanned < limit:
        edition = build_edition(cfg, week)
        if edition is not None:
            yield edition
            produced += 1
        week += timedelta(days=7)
        scanned += 1


def upcoming_edition(cfg: NewsletterConfig, now: datetime | None = None) -> Edition:
    """The next edition that has not yet published.

    ``publication_at <= now`` counts as published, so at exactly noon on
    publication day this returns the *following* edition.
    """
    moment = now_in_tz(cfg, now)
    # Start a week back: an edition whose publication was shifted later (Labor Day)
    # can still be pending while `now` has already moved into the following week.
    week = week_start_for(moment) - timedelta(days=7)
    for _ in range(cfg.max_weeks_lookahead + 1):
        edition = build_edition(cfg, week)
        if edition is not None and edition.publication_at > moment:
            return edition
        week += timedelta(days=7)
    raise NoEditionFound(
        f"no edition publishes after {moment.isoformat()} within "
        f"{cfg.max_weeks_lookahead} weeks (check blackouts and max_weeks_lookahead)"
    )


def next_deadline_edition(cfg: NewsletterConfig, now: datetime | None = None) -> Edition:
    """The edition whose submission deadline comes next.

    This is *not* the same as :func:`upcoming_edition`. With the ORFE schedule a
    deadline falls six days before its own publication, so by the time an edition
    is the next to publish its deadline is already days past. Contributors chasing
    a deadline are always working on the edition *after* the next one, which is
    what the deadline watch must report on.
    """
    moment = now_in_tz(cfg, now)
    week = week_start_for(moment) - timedelta(days=7)
    for _ in range(cfg.max_weeks_lookahead + 1):
        edition = build_edition(cfg, week)
        if edition is not None and edition.deadline_at > moment:
            return edition
        week += timedelta(days=7)
    raise NoEditionFound(
        f"no edition has a deadline after {moment.isoformat()} within "
        f"{cfg.max_weeks_lookahead} weeks (check blackouts and max_weeks_lookahead)"
    )


def current_edition(cfg: NewsletterConfig, now: datetime | None = None) -> Edition | None:
    """The most recently published edition, or None within the lookback horizon."""
    moment = now_in_tz(cfg, now)
    week = week_start_for(moment) + timedelta(days=7)
    for _ in range(cfg.max_weeks_lookahead + 1):
        edition = build_edition(cfg, week)
        if edition is not None and edition.publication_at <= moment:
            return edition
        week -= timedelta(days=7)
    return None


# ---------------------------------------------------------------------------
# Event membership
# ---------------------------------------------------------------------------

def event_start_to_aware(value: str | datetime, tz: ZoneInfo) -> datetime:
    """Lift a feed timestamp into `tz`.

    The feed writes naive local wall clock in TARGET_TZ, e.g.
    ``"2026-09-21T12:15:00"`` (see schema/events.schema.json). A ``Z`` or
    ``+00:00`` suffix is rejected rather than parsed: the schema pattern forbids
    it, so a suffix means the producer is broken, and quietly reading it as UTC
    would shift the window by four or five hours.
    """
    if isinstance(value, datetime):
        return value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz, fold=0)
    if value is None:
        raise InvalidEventTime("event start time is missing")
    text = str(value).strip()
    try:
        naive = datetime.strptime(text, EVENT_TIME_FORMAT)
    except (TypeError, ValueError):
        raise InvalidEventTime(
            f"expected naive local {EVENT_TIME_FORMAT} (no timezone suffix), got {value!r}"
        ) from None
    return naive.replace(tzinfo=tz, fold=0)


def is_in_coverage(
    edition: Edition,
    event: Mapping[str, Any] | str | datetime,
    *,
    field: str = EVENT_START_FIELD,
) -> bool:
    """True when the event starts inside the edition's coverage window."""
    if isinstance(event, Mapping):
        raw = event.get(field)
    else:
        raw = event
    tz = ZoneInfo(edition.timezone)
    return edition.covers(event_start_to_aware(raw, tz))


def filter_events_for_edition(
    edition: Edition,
    events: Iterable[Mapping[str, Any]],
    *,
    field: str = EVENT_START_FIELD,
) -> list[Mapping[str, Any]]:
    """Events inside the coverage window, in input order."""
    return [ev for ev in events if is_in_coverage(edition, ev, field=field)]


def edition_for_event(
    cfg: NewsletterConfig,
    event: Mapping[str, Any] | str | datetime,
    *,
    field: str = EVENT_START_FIELD,
    horizon_weeks: int | None = None,
) -> Edition | None:
    """The edition whose coverage window contains this event, if any."""
    tz = cfg.tzinfo()
    raw = event.get(field) if isinstance(event, Mapping) else event
    moment = event_start_to_aware(raw, tz)
    horizon = horizon_weeks if horizon_weeks is not None else cfg.max_weeks_lookahead
    week = week_start_for(moment) - timedelta(days=7)
    for _ in range(min(horizon, 4) + 2):
        edition = build_edition(cfg, week)
        if edition is not None and edition.covers(moment):
            return edition
        week += timedelta(days=7)
    return None


def stamp_edition(events: Iterable[dict], edition: Edition) -> int:
    """Record the edition id on each event. Returns the number stamped."""
    count = 0
    for ev in events:
        ev[EDITION_FIELD] = edition.id
        count += 1
    return count


def due_reminders(edition: Edition, now: datetime) -> list[Reminder]:
    """Reminders whose lead has passed but whose deadline has not.

    A skipped cron run therefore fires late rather than never, and repeated runs
    return the same keys so the caller can dedupe.
    """
    if now >= edition.deadline_at:
        return []
    return [r for r in edition.reminders if r.fire_at <= now]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Resolve the newsletter publication schedule and coverage window."
    )
    p.add_argument("--config", default=None, help=f"Path to {DEFAULT_CONFIG_FILENAME}")
    p.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 instant to resolve against. Testing and backfill only.",
    )
    p.add_argument(
        "--which", choices=["upcoming", "current", "next-deadline"], default="upcoming",
        help=(
            "Resolve the next unpublished edition (default), the last published one, "
            "or the one whose submission deadline comes next"
        ),
    )
    p.add_argument("--events", default=None, help="events.json to filter against the window")
    p.add_argument("--reminders", action="store_true", help="List reminders due as of --as-of")
    p.add_argument("--print-edition-id", action="store_true", help="Print only the edition id")
    p.add_argument(
        "--print-config-sha256", action="store_true",
        help="Print only the resolved-config fingerprint (used by CI change detection)",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON (default for full output)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        cfg = load_newsletter_config(ns.config)
        now = parse_as_of(ns.as_of, cfg)
    except NewsletterConfigError as exc:
        print(f"newsletter config error: {exc}", file=sys.stderr)
        return 2

    if ns.print_config_sha256:
        print(cfg.fingerprint())
        return 0

    try:
        if ns.which == "current":
            edition = current_edition(cfg, now)
            if edition is None:
                print("no published edition within the lookback horizon", file=sys.stderr)
                return 3
        elif ns.which == "next-deadline":
            edition = next_deadline_edition(cfg, now)
        else:
            edition = upcoming_edition(cfg, now)
    except NoEditionFound as exc:
        print(f"{exc}", file=sys.stderr)
        return 3
    except NewsletterConfigError as exc:
        print(f"newsletter config error: {exc}", file=sys.stderr)
        return 2

    if ns.print_edition_id:
        print(edition.id)
        return 0

    payload: dict[str, Any] = {"asOf": now.isoformat(), "edition": edition.to_dict()}

    if ns.reminders:
        payload["dueReminders"] = [
            {"leadHours": r.lead_hours, "fireAt": r.fire_at.isoformat(), "key": r.key}
            for r in due_reminders(edition, now)
        ]

    if ns.events:
        try:
            events = json.loads(Path(ns.events).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"could not read {ns.events}: {exc}", file=sys.stderr)
            return 2
        try:
            covered = filter_events_for_edition(edition, events)
        except InvalidEventTime as exc:
            print(f"{ns.events}: {exc}", file=sys.stderr)
            return 2
        payload["eventCount"] = len(covered)
        payload["events"] = [
            {
                "guid": ev.get("guid"),
                "startTime": ev.get(EVENT_START_FIELD),
                "title": ev.get("title"),
                "titleIsPlaceholder": bool(ev.get("titleIsPlaceholder")),
            }
            for ev in covered
        ]

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
