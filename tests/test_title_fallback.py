import os

import pytest

from src.enrich import (
    A_AN_MARKER,
    choose_article,
    fallback_include_speaker_enabled,
    fill_title_fallback,
    resolve_a_an,
)


def test_fill_title_fallback_blank_and_tbd(monkeypatch):
    # Ensure no prefix from environment interferes with baseline expectations
    monkeypatch.delenv("FALLBACK_PREPEND_TEXT", raising=False)
    events = [
        {"guid": "1", "speaker": "Alice", "title": ""},
        {"guid": "2", "speaker": "Bob", "title": "  TBD  "},
        {"guid": "3", "speaker": "Carol", "title": None},
        {"guid": "4", "speaker": "Dave", "title": "Existing"},
        {"guid": "5", "speaker": "", "title": ""},  # no speaker -> last-resort title
    ]
    filled = fill_title_fallback(events, overwrite=False)
    assert filled == 4
    assert events[0]["title"] == "Alice"
    assert events[1]["title"] == "Bob"
    assert events[2]["title"] == "Carol"
    assert events[3]["title"] == "Existing"
    assert events[4]["title"] == "A Seminar Talk"


def test_fill_title_fallback_with_prefix(monkeypatch):
    monkeypatch.setenv("FALLBACK_PREPEND_TEXT", "Seminar:")
    events = [
        {"guid": "1", "speaker": "Alice", "title": ""},
    ]
    filled = fill_title_fallback(events, overwrite=False)
    assert filled == 1
    assert events[0]["title"] == "Seminar: Alice"


def test_fill_title_fallback_with_overlong_prefix(monkeypatch):
    monkeypatch.setenv("FALLBACK_PREPEND_TEXT", "X" * 500)
    events = [
        {"guid": "1", "speaker": "Alice", "title": None},
    ]
    filled = fill_title_fallback(events, overwrite=False)
    assert filled == 1
    # Overlong prefix should be ignored
    assert events[0]["title"] == "Alice"


def test_fill_title_fallback_with_series_placeholder(monkeypatch):
    # Template includes {series}; should insert value and collapse spaces if missing
    monkeypatch.setenv("FALLBACK_PREPEND_TEXT", "A {series} Talk by")
    events = [
        {"guid": "1", "speaker": "Alice", "title": "", "series": "Optimization Seminar"},
        {"guid": "2", "speaker": "Bob", "title": "", "series": ""},
        {"guid": "3", "speaker": "Carol", "title": ""},  # no series key
    ]
    filled = fill_title_fallback(events, overwrite=False)
    assert filled == 3
    assert events[0]["title"] == "A Optimization Seminar Talk by Alice"
    # For missing/blank series, ensure we don't get double spaces
    assert events[1]["title"] == "A Talk by Bob"
    assert events[2]["title"] == "A Talk by Carol"


def test_fill_title_fallback_with_a_an_placeholder(monkeypatch):
    """The {a_an} placeholder should auto-select 'A' or 'An' based on next word."""
    monkeypatch.setenv("FALLBACK_PREPEND_TEXT", "{a_an} {series} Talk by")
    events = [
        {"guid": "1", "speaker": "Alice", "title": "", "series": "Optimization Seminar"},
        {"guid": "2", "speaker": "Bob", "title": "", "series": "ORFE Colloquium"},
        {"guid": "3", "speaker": "Carol", "title": "", "series": "Analysis Seminar"},
        {"guid": "4", "speaker": "Dave", "title": "", "series": ""},  # empty series
    ]
    filled = fill_title_fallback(events, overwrite=False)
    assert filled == 4
    assert events[0]["title"] == "An Optimization Seminar Talk by Alice"
    assert events[1]["title"] == "An ORFE Colloquium Talk by Bob"
    assert events[2]["title"] == "An Analysis Seminar Talk by Carol"
    # Empty series: {a_an} followed by "Talk" -> "A Talk"
    assert events[3]["title"] == "A Talk by Dave"


def test_fill_title_fallback_with_a_an_no_speaker(monkeypatch):
    """The {a_an} placeholder works with include_speaker=False."""
    monkeypatch.setenv("FALLBACK_PREPEND_TEXT", "{a_an} {series} Talk by")
    events = [
        {"guid": "1", "speaker": "Alice", "title": "", "series": "ORFE Colloquium"},
        {"guid": "2", "speaker": "Bob", "title": "", "series": "Statistics Seminar"},
    ]
    filled = fill_title_fallback(events, overwrite=False, include_speaker=False)
    assert filled == 2
    assert events[0]["title"] == "An ORFE Colloquium Talk"
    assert events[1]["title"] == "A Statistics Seminar Talk"


def test_fill_title_fallback_without_speaker(monkeypatch):
    """When include_speaker=False, use only the template without speaker name."""
    monkeypatch.setenv("FALLBACK_PREPEND_TEXT", "A {series} Talk")
    events = [
        {"guid": "1", "speaker": "Alice", "title": "", "series": "Optimization Seminar"},
        {"guid": "2", "speaker": "Bob", "title": "", "series": "Statistics"},
        {"guid": "3", "speaker": "Carol", "title": "Existing"},
    ]
    filled = fill_title_fallback(events, overwrite=False, include_speaker=False)
    assert filled == 2
    assert events[0]["title"] == "A Optimization Seminar Talk"
    assert events[1]["title"] == "A Statistics Talk"
    assert events[2]["title"] == "Existing"


def test_fill_title_fallback_without_speaker_no_template(monkeypatch):
    """When include_speaker=False and no template, fall back to a series-derived title."""
    monkeypatch.delenv("FALLBACK_PREPEND_TEXT", raising=False)
    events = [
        {"guid": "1", "speaker": "Alice", "title": "", "series": "Optimization Seminar"},
        {"guid": "2", "speaker": "Bob", "title": ""},  # no series either
    ]
    filled = fill_title_fallback(events, overwrite=False, include_speaker=False)
    assert filled == 2
    assert events[0]["title"] == "An Optimization Seminar Talk"
    assert events[1]["title"] == "A Seminar Talk"


def test_fill_title_fallback_last_resort_no_speaker_no_template(monkeypatch):
    """Events without speaker or template never keep an empty title."""
    monkeypatch.delenv("FALLBACK_PREPEND_TEXT", raising=False)
    events = [
        {"guid": "1", "speaker": "", "title": "", "series": "Optimization Seminar"},
        {"guid": "2", "speaker": None, "title": "TBD", "series": "S. S. Wilks Memorial Seminar in Statistics"},
        # Multi-series: use the first series only
        {"guid": "3", "speaker": "", "title": "", "series": "Optimization Seminar,ORFE Department Colloquia"},
        {"guid": "4", "speaker": "", "title": "", "series": ""},
        {"guid": "5", "speaker": "", "title": ""},  # no series key
    ]
    filled = fill_title_fallback(events, overwrite=False)
    assert filled == 5
    assert events[0]["title"] == "An Optimization Seminar Talk"
    # "S." is read "ess", so the article follows the sound, not the letter.
    assert events[1]["title"] == "An S. S. Wilks Memorial Seminar in Statistics Talk"
    assert events[2]["title"] == "An Optimization Seminar Talk"
    assert events[3]["title"] == "A Seminar Talk"
    assert events[4]["title"] == "A Seminar Talk"
    assert all(ev["title"].strip() for ev in events)


def test_fill_title_fallback_no_speaker_with_template(monkeypatch):
    """With a template but no speaker, use the template alone (strip trailing 'by')."""
    monkeypatch.setenv("FALLBACK_PREPEND_TEXT", "{a_an} {series} Talk by")
    events = [
        {"guid": "1", "speaker": "", "title": "", "series": "Optimization Seminar"},
    ]
    filled = fill_title_fallback(events, overwrite=False)
    assert filled == 1
    assert events[0]["title"] == "An Optimization Seminar Talk"


def test_fill_title_fallback_without_speaker_missing_series(monkeypatch):
    """When include_speaker=False and series is missing, collapse spaces properly."""
    monkeypatch.setenv("FALLBACK_PREPEND_TEXT", "A {series} Talk")
    events = [
        {"guid": "1", "speaker": "Alice", "title": ""},  # no series key
        {"guid": "2", "speaker": "Bob", "title": "", "series": ""},
    ]
    filled = fill_title_fallback(events, overwrite=False, include_speaker=False)
    assert filled == 2
    assert events[0]["title"] == "A Talk"
    assert events[1]["title"] == "A Talk"


def test_fill_title_fallback_without_speaker_strips_trailing_by(monkeypatch):
    """When include_speaker=False and template ends with 'by', strip it."""
    monkeypatch.setenv("FALLBACK_PREPEND_TEXT", "A {series} Talk by")
    events = [
        {"guid": "1", "speaker": "Alice", "title": "", "series": "Optimization Seminar"},
        {"guid": "2", "speaker": "Bob", "title": "", "series": ""},
    ]
    filled = fill_title_fallback(events, overwrite=False, include_speaker=False)
    assert filled == 2
    assert events[0]["title"] == "A Optimization Seminar Talk"
    assert events[1]["title"] == "A Talk"


def test_fallback_include_speaker_enabled_default(monkeypatch):
    """Default should be True (include speaker)."""
    monkeypatch.delenv("FALLBACK_INCLUDE_SPEAKER", raising=False)
    assert fallback_include_speaker_enabled() is True


def test_fallback_include_speaker_enabled_env_false(monkeypatch):
    """FALLBACK_INCLUDE_SPEAKER=0 should return False."""
    monkeypatch.setenv("FALLBACK_INCLUDE_SPEAKER", "0")
    assert fallback_include_speaker_enabled() is False


def test_fallback_include_speaker_enabled_cli_override(monkeypatch):
    """CLI flag should override env var."""
    monkeypatch.setenv("FALLBACK_INCLUDE_SPEAKER", "1")
    # CLI says no speaker
    assert fallback_include_speaker_enabled(cli_flag=False) is False
    # CLI says include speaker
    monkeypatch.setenv("FALLBACK_INCLUDE_SPEAKER", "0")
    assert fallback_include_speaker_enabled(cli_flag=True) is True


@pytest.mark.parametrize(
    "following,expected",
    [
        # Spelled-out initialisms follow the name of the first letter.
        ("S. S. Wilks Memorial Seminar", "An"),   # "ess"
        ("FPO", "An"),                            # "ef"
        ("MIT Colloquium", "An"),                 # "em"
        ("N. J. Section Meeting", "An"),          # "en"
        ("X. Y. Lecture", "An"),                  # "ex"
        ("B. B. Lecture", "A"),                   # "bee"
        ("CS Seminar", "A"),                      # "see"
        ("PDE Workshop", "A"),                    # "pee"
        ("U.S. Policy Seminar", "A"),             # "yoo"
        ("W. E. B. Lecture", "A"),                # "double-u"
        # Acronyms read as words fall through to the ordinary word rule.
        ("ORFE Department Colloquia", "An"),      # "or-fee"
        # Vowel letter, consonant sound.
        ("University Seminar", "A"),
        ("European Finance Seminar", "A"),
        ("Utility Theory Seminar", "A"),
        ("One-Day Workshop", "A"),
        # Consonant letter, vowel sound.
        ("Hour-Long Seminar", "An"),
        ("Honest Signals Seminar", "An"),
        # Ordinary spelling agrees with sound.
        ("Optimization Seminar", "An"),
        ("Analysis Seminar", "An"),
        ("Stochastic Analysis Seminar", "A"),
        ("Talk", "A"),
    ],
)
def test_choose_article_follows_sound_not_spelling(following, expected):
    assert choose_article(following) == expected


@pytest.mark.parametrize("following", ["", "   ", "123 Seminar", "!!!"])
def test_choose_article_defaults_to_a_without_letters(following):
    assert choose_article(following) == "A"


def test_resolve_a_an_replaces_every_marker():
    text = f"{A_AN_MARKER} Optimization Seminar and {A_AN_MARKER} S. S. Wilks Seminar"
    assert resolve_a_an(text) == "An Optimization Seminar and An S. S. Wilks Seminar"


def test_resolve_a_an_leaves_text_without_markers_untouched():
    assert resolve_a_an("A Stochastic Analysis Seminar Talk") == "A Stochastic Analysis Seminar Talk"


def test_live_series_names_get_correct_articles(monkeypatch):
    """The production template applied to the series seen in the real feed."""
    monkeypatch.setenv("FALLBACK_PREPEND_TEXT", "{a_an} {series} Talk by")
    events = [
        {"guid": "1", "title": "TBD", "series": "S. S. Wilks Distinguished Lecture Series"},
        {"guid": "2", "title": "TBD", "series": "S. S. Wilks Memorial Seminar in Statistics"},
        {"guid": "3", "title": "TBD", "series": "ORFE Department Colloquia"},
        {"guid": "4", "title": "TBD", "series": "Optimization Seminar"},
        {"guid": "5", "title": "TBD", "series": "Stochastic Analysis and Financial Mathematics Seminar"},
        {"guid": "6", "title": "TBD", "series": "FPO"},
    ]
    fill_title_fallback(events, overwrite=False, include_speaker=False)
    assert [e["title"] for e in events] == [
        "An S. S. Wilks Distinguished Lecture Series Talk",
        "An S. S. Wilks Memorial Seminar in Statistics Talk",
        "An ORFE Department Colloquia Talk",
        "An Optimization Seminar Talk",
        "A Stochastic Analysis and Financial Mathematics Seminar Talk",
        "An FPO Talk",
    ]
