from pathlib import Path

import pytest

from src.enrich import (
    ORFE_TOKEN,
    ZERO_WIDTH_SPACE,
    orfe_zwsp_enabled,
    split_orfe_in_titles,
)

SPLIT = "O​R​F​E"


def test_zero_width_space_is_u200b():
    assert ZERO_WIDTH_SPACE == "​"


def test_split_inserts_a_space_between_every_letter():
    events = [{"guid": "1", "title": "An ORFE Department Colloquia Talk"}]
    assert split_orfe_in_titles(events) == 1
    assert events[0]["title"] == f"An {SPLIT} Department Colloquia Talk"
    # Three separators for four letters, and the plain token is gone.
    assert events[0]["title"].count(ZERO_WIDTH_SPACE) == 3
    assert ORFE_TOKEN not in events[0]["title"]


def test_split_leaves_every_other_field_alone():
    """The downstream regex still needs to match series; only title is changed."""
    events = [
        {
            "guid": "1",
            "title": "An ORFE Department Colloquia Talk",
            "series": "ORFE Department Colloquia",
            "speaker": "A. Person, ORFE",
            "rawEventDetails": "<div>ORFE seminar</div>",
            "urlRef": "https://orfe.princeton.edu/events/x",
        }
    ]
    split_orfe_in_titles(events)
    assert events[0]["series"] == "ORFE Department Colloquia"
    assert events[0]["speaker"] == "A. Person, ORFE"
    assert events[0]["rawEventDetails"] == "<div>ORFE seminar</div>"
    assert events[0]["urlRef"] == "https://orfe.princeton.edu/events/x"


def test_split_is_idempotent():
    events = [{"guid": "1", "title": "An ORFE Department Colloquia Talk"}]
    split_orfe_in_titles(events)
    once = events[0]["title"]
    assert split_orfe_in_titles(events) == 0
    assert events[0]["title"] == once


def test_split_handles_multiple_occurrences_in_one_title():
    events = [{"guid": "1", "title": "ORFE and ORFE"}]
    assert split_orfe_in_titles(events) == 1
    assert events[0]["title"] == f"{SPLIT} and {SPLIT}"


def test_split_counts_only_changed_events():
    events = [
        {"guid": "1", "title": "An ORFE Department Colloquia Talk"},
        {"guid": "2", "title": "An Optimization Seminar Talk"},
        {"guid": "3", "title": "A Stochastic Analysis Seminar Talk"},
    ]
    assert split_orfe_in_titles(events) == 1
    assert events[1]["title"] == "An Optimization Seminar Talk"
    assert events[2]["title"] == "A Stochastic Analysis Seminar Talk"


def test_split_is_case_sensitive():
    """Only the exact uppercase token trips the consumer's regex."""
    events = [{"guid": "1", "title": "An orfe and Orfe talk"}]
    assert split_orfe_in_titles(events) == 0
    assert events[0]["title"] == "An orfe and Orfe talk"


@pytest.mark.parametrize("title", [None, "", 123, [], {}])
def test_split_tolerates_non_string_titles(title):
    events = [{"guid": "1", "title": title}]
    assert split_orfe_in_titles(events) == 0
    assert events[0]["title"] == title


def test_split_tolerates_a_missing_title_key():
    events = [{"guid": "1"}]
    assert split_orfe_in_titles(events) == 0
    assert "title" not in events[0]


def test_rendered_length_is_unchanged_ignoring_the_zero_width_spaces():
    original = "An ORFE Department Colloquia Talk"
    events = [{"guid": "1", "title": original}]
    split_orfe_in_titles(events)
    assert events[0]["title"].replace(ZERO_WIDTH_SPACE, "") == original


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TITLE_ORFE_ZWSP", raising=False)
    assert orfe_zwsp_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
def test_enabled_by_truthy_env_values(monkeypatch, value):
    monkeypatch.setenv("TITLE_ORFE_ZWSP", value)
    assert orfe_zwsp_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
def test_not_enabled_by_other_env_values(monkeypatch, value):
    monkeypatch.setenv("TITLE_ORFE_ZWSP", value)
    assert orfe_zwsp_enabled() is False


def test_cli_flag_overrides_a_disabled_env(monkeypatch):
    monkeypatch.setenv("TITLE_ORFE_ZWSP", "0")
    assert orfe_zwsp_enabled(cli_flag=True) is True


# --- End-to-end through main.py -------------------------------------------------

import json  # noqa: E402
from unittest.mock import patch  # noqa: E402

from src import main  # noqa: E402

ICS_ORFE = (
    "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Test//Test//EN\n"
    "BEGIN:VEVENT\nUID:uid-1\nSUMMARY:TBD\nDTSTART:20260915T120000Z\nDTEND:20260915T130000Z\n"
    "URL:https://orfe.princeton.edu/events/2026/example\n"
    "CATEGORIES:ORFE Department Colloquia\nEND:VEVENT\n"
    "END:VCALENDAR"
)


def _run_main(extra_args, monkeypatch, capsys):
    monkeypatch.setenv("FALLBACK_PREPEND_TEXT", "{a_an} {series} Talk by")
    monkeypatch.setenv("FALLBACK_INCLUDE_SPEAKER", "0")
    with patch("src.main.fetch_ics", return_value=ICS_ORFE):
        assert main.main(["--ics-url", "unused", "--print-only", *extra_args]) == 0
    out = capsys.readouterr().out
    return json.loads(out[out.find("[") :].strip())


def test_end_to_end_off_by_default(monkeypatch, capsys):
    monkeypatch.delenv("TITLE_ORFE_ZWSP", raising=False)
    data = _run_main([], monkeypatch, capsys)
    assert data[0]["title"] == "An ORFE Department Colloquia Talk"


def test_end_to_end_env_enables_the_split(monkeypatch, capsys):
    """The title is generated from series by the fallback, so the split must
    run after it to see the ORFE token at all."""
    monkeypatch.setenv("TITLE_ORFE_ZWSP", "1")
    data = _run_main([], monkeypatch, capsys)
    assert data[0]["title"] == f"An {SPLIT} Department Colloquia Talk"
    assert data[0]["series"] == "ORFE Department Colloquia"


def test_end_to_end_cli_flag_enables_the_split(monkeypatch, capsys):
    monkeypatch.delenv("TITLE_ORFE_ZWSP", raising=False)
    data = _run_main(["--title-orfe-zwsp"], monkeypatch, capsys)
    assert data[0]["title"] == f"An {SPLIT} Department Colloquia Talk"


def test_split_output_still_satisfies_the_schema(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("TITLE_ORFE_ZWSP", "1")
    data = _run_main([], monkeypatch, capsys)
    import jsonschema

    schema = json.loads(Path("schema/events.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(instance=data, schema=schema)
    assert ZERO_WIDTH_SPACE in data[0]["title"]
