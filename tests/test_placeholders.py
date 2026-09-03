"""Tests for title provenance vocabulary and helpers."""
import json

import pytest

from src.placeholders import (
    PLACEHOLDER_TITLE_SOURCES,
    REAL_TITLE_SOURCES,
    TITLE_PLACEHOLDER_FIELD,
    TITLE_SOURCE_FIELD,
    TITLE_SOURCE_VALUES,
    TitleSource,
    ensure_title_provenance,
    is_missing_title,
    mark_title_source,
    provenance_enabled,
    title_is_placeholder,
    title_source,
)


def test_title_source_enum_values_match_contract():
    """The wire format is consumed by WordPress and both JSON schemas; lock it."""
    assert {s.value for s in TitleSource} == {
        "enriched",
        "ics",
        "fallback-speaker",
        "fallback-template",
        "fallback-series",
    }
    assert TITLE_SOURCE_VALUES == (
        "enriched",
        "ics",
        "fallback-speaker",
        "fallback-template",
        "fallback-series",
    )


def test_real_and_placeholder_sources_partition_the_enum():
    assert REAL_TITLE_SOURCES | PLACEHOLDER_TITLE_SOURCES == frozenset(TitleSource)
    assert not (REAL_TITLE_SOURCES & PLACEHOLDER_TITLE_SOURCES)
    assert len(PLACEHOLDER_TITLE_SOURCES) == 3


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n", "TBD", "tbd", " Tbd "])
def test_is_missing_title_true_cases(value):
    """Matches the closure previously inlined in enrich.fill_title_fallback."""
    assert is_missing_title(value) is True


@pytest.mark.parametrize(
    "value", ["Real Title", "TBD Talk", "A TBD-adjacent title", "0", "  x  "]
)
def test_is_missing_title_false_cases(value):
    assert is_missing_title(value) is False


def test_mark_title_source_sets_both_fields_as_plain_types():
    ev = {}
    mark_title_source(ev, TitleSource.ENRICHED)
    assert ev[TITLE_SOURCE_FIELD] == "enriched"
    assert ev[TITLE_PLACEHOLDER_FIELD] is False
    # Plain str/bool, not the str-subclass enum member, so json.dumps is boring.
    assert type(ev[TITLE_SOURCE_FIELD]) is str
    assert type(ev[TITLE_PLACEHOLDER_FIELD]) is bool


@pytest.mark.parametrize(
    "source",
    [
        TitleSource.FALLBACK_SPEAKER,
        TitleSource.FALLBACK_TEMPLATE,
        TitleSource.FALLBACK_SERIES,
    ],
)
def test_placeholder_flag_true_for_fallback_sources(source):
    ev = {}
    mark_title_source(ev, source)
    assert ev[TITLE_PLACEHOLDER_FIELD] is True
    assert title_is_placeholder(ev) is True


@pytest.mark.parametrize("source", [TitleSource.ENRICHED, TitleSource.ICS])
def test_placeholder_flag_false_for_real_sources(source):
    ev = {}
    mark_title_source(ev, source)
    assert ev[TITLE_PLACEHOLDER_FIELD] is False
    assert title_is_placeholder(ev) is False


def test_title_source_accessor_returns_none_when_untagged():
    assert title_source({}) is None
    assert title_source({"titleSource": "ics"}) == "ics"


def test_ensure_title_provenance_backfills_ics_for_untagged_real_title():
    events = [{"title": "Real"}]
    assert ensure_title_provenance(events) == 1
    assert events[0][TITLE_SOURCE_FIELD] == "ics"
    assert events[0][TITLE_PLACEHOLDER_FIELD] is False


def test_ensure_title_provenance_backfills_series_for_untagged_missing_title():
    events = [{"title": ""}]
    assert ensure_title_provenance(events) == 1
    assert events[0][TITLE_SOURCE_FIELD] == "fallback-series"
    assert events[0][TITLE_PLACEHOLDER_FIELD] is True


def test_ensure_title_provenance_leaves_existing_source_untouched():
    events = [
        {"title": "Real", TITLE_SOURCE_FIELD: "enriched", TITLE_PLACEHOLDER_FIELD: False},
        {"title": "Other"},
    ]
    # Only the untagged event is counted and rewritten.
    assert ensure_title_provenance(events) == 1
    assert events[0][TITLE_SOURCE_FIELD] == "enriched"
    assert events[1][TITLE_SOURCE_FIELD] == "ics"


def test_provenance_fields_are_json_round_trippable():
    ev = {"title": "A Seminar Talk"}
    mark_title_source(ev, TitleSource.FALLBACK_SERIES)
    assert json.loads(json.dumps(ev)) == ev


def test_provenance_enabled_default_true(monkeypatch):
    monkeypatch.delenv("TITLE_PROVENANCE", raising=False)
    assert provenance_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_provenance_enabled_env_off(monkeypatch, value):
    monkeypatch.setenv("TITLE_PROVENANCE", value)
    assert provenance_enabled() is False


def test_provenance_enabled_cli_override_beats_env(monkeypatch):
    monkeypatch.setenv("TITLE_PROVENANCE", "1")
    assert provenance_enabled(cli_flag=False) is False
