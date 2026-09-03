"""Provenance for placeholder-derived field values.

Records where a title came from so downstream consumers -- the WordPress
editorial ingester and the newsletter variant -- can tell a real, human-authored
title apart from one this pipeline synthesized.

The pipeline guarantees a non-empty ``title`` (``minLength: 1`` in
``schema/events.schema.json``), so an event whose speaker has not yet supplied a
title still ships with something like "An ORFE Departmental Colloquia Talk".
Without provenance that is indistinguishable from a real title, which is exactly
the listing editors want to reject.

Lives in its own module because both ``transform`` (titles that came straight
from the ICS feed) and ``enrich`` (the fallbacks) need it; importing nothing from
the package keeps that dependency edge acyclic.
"""
from __future__ import annotations

import os
from enum import Enum
from typing import Any, Iterable, Mapping, MutableMapping

TITLE_SOURCE_FIELD = "titleSource"
TITLE_PLACEHOLDER_FIELD = "titleIsPlaceholder"


class TitleSource(str, Enum):
    """Where an event's title came from."""

    ENRICHED = "enriched"                 # scraped from .event-subtitle
    ICS = "ics"                           # mapped straight from the feed
    FALLBACK_SPEAKER = "fallback-speaker"     # speaker name (with optional prefix)
    FALLBACK_TEMPLATE = "fallback-template"   # FALLBACK_PREPEND_TEXT alone
    FALLBACK_SERIES = "fallback-series"       # series-derived last resort


#: Sources that represent a title a human actually wrote.
REAL_TITLE_SOURCES: frozenset[TitleSource] = frozenset(
    {TitleSource.ENRICHED, TitleSource.ICS}
)
#: Sources this pipeline synthesized; editors should treat these as unpublishable.
PLACEHOLDER_TITLE_SOURCES: frozenset[TitleSource] = (
    frozenset(TitleSource) - REAL_TITLE_SOURCES
)
#: Wire values, in declaration order; mirrors the enum in the JSON schemas.
TITLE_SOURCE_VALUES: tuple[str, ...] = tuple(s.value for s in TitleSource)

#: Upstream writes these when a title is not yet known.
MISSING_TITLE_SENTINELS = frozenset({"tbd"})


def is_missing_title(value: object | None) -> bool:
    """True when a title is absent, blank, or a known sentinel such as 'TBD'."""
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    return text.lower() in MISSING_TITLE_SENTINELS


def mark_title_source(event: MutableMapping[str, Any], source: TitleSource) -> None:
    """Record provenance on ``event``.

    Writes a plain ``str`` and ``bool`` rather than the enum member so JSON
    serialization is unambiguous regardless of encoder behavior around
    ``str`` subclasses.
    """
    event[TITLE_SOURCE_FIELD] = str(source.value)
    event[TITLE_PLACEHOLDER_FIELD] = bool(source in PLACEHOLDER_TITLE_SOURCES)


def title_source(event: Mapping[str, Any]) -> str | None:
    """Return the recorded title source, or None when untagged."""
    value = event.get(TITLE_SOURCE_FIELD)
    return str(value) if value else None


def title_is_placeholder(event: Mapping[str, Any]) -> bool:
    """True when the event's title was synthesized by this pipeline."""
    return bool(event.get(TITLE_PLACEHOLDER_FIELD))


def ensure_title_provenance(events: Iterable[MutableMapping[str, Any]]) -> int:
    """Backfill any event that reached output without provenance.

    A non-missing untagged title can only have come from the ICS mapping, so it
    is tagged ``ics``; a still-missing title is tagged ``fallback-series`` (the
    fallback pass means that case should not arise in the normal pipeline).

    Returns the number of events backfilled.
    """
    count = 0
    for event in events:
        if event.get(TITLE_SOURCE_FIELD):
            continue
        source = (
            TitleSource.FALLBACK_SERIES
            if is_missing_title(event.get("title"))
            else TitleSource.ICS
        )
        mark_title_source(event, source)
        count += 1
    return count


def provenance_enabled(cli_flag: bool | None = None) -> bool:
    """Whether to record title provenance. Defaults to on.

    Set ``TITLE_PROVENANCE=0`` or pass ``--no-title-provenance`` to disable.
    """
    if cli_flag is not None:
        return cli_flag
    return os.getenv("TITLE_PROVENANCE", "1").lower() in {"1", "true", "yes", "on"}
