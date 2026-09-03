"""Title provenance across the real transform -> enrich -> fallback pipeline."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from src import main as main_mod
from src.enrich import enrich_titles, fill_title_fallback
from src.placeholders import TITLE_SOURCE_VALUES
from src.transform import TransformConfig, load_config, transform_event

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ICS = REPO_ROOT / "examples" / "sample_input.example.ics"
BASE_SCHEMA = REPO_ROOT / "schema" / "events.schema.json"


class DummyResp:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if not (200 <= self.status_code < 300):
            raise RuntimeError("http error")


def _subtitle_html(text: str) -> str:
    return f'<html><body><div class="event-subtitle">{text}</div></body></html>'


@pytest.fixture
def stub_subtitle(monkeypatch):
    """Patch requests.get to return a fixed subtitle."""

    def _install(text: str):
        def fake_get(url, timeout=15, headers=None):  # noqa: ARG001
            return DummyResp(_subtitle_html(text))

        monkeypatch.setattr("src.enrich.requests.get", fake_get)

    return _install


# --------------------------------------------------------------------------
# enrich_titles
# --------------------------------------------------------------------------

def test_enrich_titles_marks_enriched(stub_subtitle):
    stub_subtitle("A Real Human Title")
    events = [{"guid": "1", "urlRef": "https://example.org/1", "title": ""}]
    stats = enrich_titles(events, enable=True)
    assert stats.updated == 1
    assert events[0]["title"] == "A Real Human Title"
    assert events[0]["titleSource"] == "enriched"
    assert events[0]["titleIsPlaceholder"] is False


def test_enrich_titles_skip_leaves_provenance_untouched(stub_subtitle):
    """An event that already has a title is skipped, so it gains no provenance."""
    stub_subtitle("Scraped")
    events = [{"guid": "1", "urlRef": "https://example.org/1", "title": "Existing"}]
    enrich_titles(events, enable=True, overwrite=False)
    assert events[0]["title"] == "Existing"
    assert "titleSource" not in events[0]


def test_enrich_titles_overwrite_remarks_as_enriched(stub_subtitle):
    stub_subtitle("Scraped Title")
    events = [
        {
            "guid": "1",
            "urlRef": "https://example.org/1",
            "title": "A Seminar Talk",
            "titleSource": "fallback-series",
            "titleIsPlaceholder": True,
        }
    ]
    enrich_titles(events, enable=True, overwrite=True)
    assert events[0]["title"] == "Scraped Title"
    assert events[0]["titleSource"] == "enriched"
    assert events[0]["titleIsPlaceholder"] is False


def test_enrich_titles_mark_provenance_false_adds_no_keys(stub_subtitle):
    stub_subtitle("Scraped Title")
    events = [{"guid": "1", "urlRef": "https://example.org/1", "title": ""}]
    enrich_titles(events, enable=True, mark_provenance=False)
    assert events[0]["title"] == "Scraped Title"
    assert set(events[0]) == {"guid", "urlRef", "title"}


# --------------------------------------------------------------------------
# fill_title_fallback branches
# --------------------------------------------------------------------------

def test_fill_title_fallback_marks_fallback_speaker():
    events = [{"guid": "1", "speaker": "Alice", "title": ""}]
    assert fill_title_fallback(events) == 1
    assert events[0]["title"] == "Alice"
    assert events[0]["titleSource"] == "fallback-speaker"
    assert events[0]["titleIsPlaceholder"] is True


def test_fill_title_fallback_marks_fallback_speaker_with_prefix(monkeypatch):
    monkeypatch.setenv("FALLBACK_PREPEND_TEXT", "{a_an} {series} Talk by")
    events = [{"guid": "1", "speaker": "Alice", "series": "ORFE Colloquium", "title": ""}]
    assert fill_title_fallback(events) == 1
    assert events[0]["title"] == "An ORFE Colloquium Talk by Alice"
    # Prefix plus speaker is still speaker-derived.
    assert events[0]["titleSource"] == "fallback-speaker"
    assert events[0]["titleIsPlaceholder"] is True


def test_fill_title_fallback_marks_fallback_template(monkeypatch):
    monkeypatch.setenv("FALLBACK_PREPEND_TEXT", "{a_an} {series} Talk by")
    events = [{"guid": "1", "speaker": "Alice", "series": "ORFE Colloquium", "title": ""}]
    assert fill_title_fallback(events, include_speaker=False) == 1
    assert events[0]["title"] == "An ORFE Colloquium Talk"
    assert events[0]["titleSource"] == "fallback-template"
    assert events[0]["titleIsPlaceholder"] is True


def test_fill_title_fallback_marks_fallback_series():
    """The 'An ORFE Departmental Colloquia Talk' case editors want to reject."""
    events = [{"guid": "1", "speaker": "", "series": "ORFE Departmental Colloquia", "title": ""}]
    assert fill_title_fallback(events) == 1
    assert events[0]["title"] == "An ORFE Departmental Colloquia Talk"
    assert events[0]["titleSource"] == "fallback-series"
    assert events[0]["titleIsPlaceholder"] is True


def test_fallback_does_not_clobber_enriched_provenance(stub_subtitle):
    """Pipeline order: enrich then fallback. The enriched title must survive."""
    stub_subtitle("A Real Human Title")
    events = [{"guid": "1", "urlRef": "https://example.org/1", "title": "", "speaker": "Alice"}]
    enrich_titles(events, enable=True)
    filled = fill_title_fallback(events)
    assert filled == 0
    assert events[0]["title"] == "A Real Human Title"
    assert events[0]["titleSource"] == "enriched"
    assert events[0]["titleIsPlaceholder"] is False


def test_fallback_remarks_when_enrichment_returned_tbd(stub_subtitle):
    """Upstream 'TBD' is not a real title, so the fallback must take over."""
    stub_subtitle("TBD")
    events = [{"guid": "1", "urlRef": "https://example.org/1", "title": "", "speaker": "Alice"}]
    enrich_titles(events, enable=True)
    assert events[0]["titleSource"] == "enriched"
    assert fill_title_fallback(events) == 1
    assert events[0]["title"] == "Alice"
    assert events[0]["titleSource"] == "fallback-speaker"
    assert events[0]["titleIsPlaceholder"] is True


def test_fill_title_fallback_mark_provenance_false_adds_no_keys():
    events = [{"guid": "1", "speaker": "Alice", "title": ""}]
    fill_title_fallback(events, mark_provenance=False)
    assert set(events[0]) == {"guid", "speaker", "title"}


def test_fill_title_fallback_return_count_unchanged_with_provenance():
    """Re-runs the existing 5-event fixture: behavior must be byte-identical."""
    events = [
        {"guid": "1", "speaker": "Alice", "title": ""},
        {"guid": "2", "speaker": "Bob", "title": "  TBD  "},
        {"guid": "3", "speaker": "Carol", "title": None},
        {"guid": "4", "speaker": "Dave", "title": "Existing"},
        {"guid": "5", "speaker": "", "title": ""},
    ]
    assert fill_title_fallback(events, overwrite=False) == 4
    assert [e["title"] for e in events] == [
        "Alice", "Bob", "Carol", "Existing", "A Seminar Talk",
    ]
    # The untouched event stays untouched, provenance included.
    assert "titleSource" not in events[3]


# --------------------------------------------------------------------------
# transform
# --------------------------------------------------------------------------

class _FakeEvent:
    """Minimal stand-in for an ics.Event with only the attributes transform reads."""

    def __init__(self, **kwargs):
        self.uid = kwargs.get("uid", "u1")
        self.begin = None
        self.end = None
        self.url = kwargs.get("url", "https://example.org/1")
        self.categories = kwargs.get("categories", "ORFE Colloquium")
        self.description = kwargs.get("description", "")
        self.name = kwargs.get("name", "Alice")
        self.location = kwargs.get("location", "101 - Sherrerd Hall")


def test_transform_marks_ics_when_feed_supplies_a_title():
    cfg = TransformConfig(copies={"title": "speaker"})
    out = transform_event(_FakeEvent(), cfg)
    assert out["title"] == "Alice"
    assert out["titleSource"] == "ics"
    assert out["titleIsPlaceholder"] is False


def test_transform_does_not_mark_empty_placeholder_title():
    out = transform_event(_FakeEvent(), TransformConfig())
    assert out["title"] == ""
    assert "titleSource" not in out


def test_transform_provenance_can_be_disabled_via_config():
    cfg = TransformConfig(copies={"title": "speaker"}, mark_title_provenance=False)
    out = transform_event(_FakeEvent(), cfg)
    assert out["title"] == "Alice"
    assert "titleSource" not in out


def test_load_config_round_trips_provenance_flag(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"mark_title_provenance": False}), encoding="utf-8")
    assert load_config(path).mark_title_provenance is False
    assert load_config(None).mark_title_provenance is True


# --------------------------------------------------------------------------
# whole pipeline
# --------------------------------------------------------------------------

def test_main_pipeline_gives_every_event_provenance(tmp_path, capsys):
    out = tmp_path / "events.json"
    rc = main_mod.main(["--ics-url", str(SAMPLE_ICS), "--output", str(out)])
    capsys.readouterr()
    assert rc == 0
    data = json.loads(out.read_text())
    assert len(data) == 14
    for ev in data:
        assert ev["titleSource"] in TITLE_SOURCE_VALUES
        assert isinstance(ev["titleIsPlaceholder"], bool)


def test_generate_events_json_emits_nonempty_titles_and_provenance(tmp_path):
    """Regression: generate_events_json used to emit "" titles, violating minLength: 1."""
    out = tmp_path / "events.json"
    main_mod.generate_events_json(ics_url=str(SAMPLE_ICS), output_path=out)
    data = json.loads(out.read_text())
    assert len(data) == 14
    assert all(ev["title"].strip() for ev in data)
    assert all(ev["titleSource"] in TITLE_SOURCE_VALUES for ev in data)


def test_output_validates_against_schema_with_provenance(tmp_path):
    out = tmp_path / "events.json"
    main_mod.main(["--ics-url", str(SAMPLE_ICS), "--output", str(out)])
    data = json.loads(out.read_text())
    assert len(data) > 0, "an empty array would validate trivially"
    validator = Draft7Validator(json.loads(BASE_SCHEMA.read_text()))
    assert sorted(validator.iter_errors(data), key=str) == []


def test_schema_rejects_unknown_title_source():
    """Proves the enum is actually wired into the schema, not just documented."""
    validator = Draft7Validator(json.loads(BASE_SCHEMA.read_text()))
    item = {
        "guid": "g", "startTime": "2026-09-08T12:15:00", "endTime": "2026-09-08T13:15:00",
        "urlRef": "https://example.org/1",
        "location": {"name": "Sherrerd Hall", "id": "", "detail": "101"},
        "title": "T", "cancelled": "", "bannerImage": "", "itemType": "advertisement",
        "titleSource": "made-up",
    }
    assert list(validator.iter_errors([item])), "schema accepted an invalid titleSource"
