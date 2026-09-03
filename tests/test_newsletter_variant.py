"""Generation of events-newsletter.json alongside the untouched primary feed.

Every case pins --as-of 2025-09-05T12:00:00-04:00 (a Friday). The next edition is
then Monday 2025-09-08, covering Sep 8 00:00 - Sep 14 23:59:59 ET, and exactly two
of the 14 events in examples/sample_input.example.ics fall inside it.
"""
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src import main as main_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ICS = REPO_ROOT / "examples" / "sample_input.example.ics"
TEST_CONFIG = REPO_ROOT / "tests" / "fixtures" / "newsletter_config.test.json"

AS_OF = "2025-09-05T12:00:00-04:00"
EXPECTED_EDITION = "2025-09-08"
EXPECTED_GUIDS = {"ps_events:11876:delta:0", "ps_events:11856:delta:0"}
TOTAL_EVENTS = 14


def run(tmp_path, *extra, newsletter=True, as_of=AS_OF, config=TEST_CONFIG):
    """Run the CLI against the sample ICS; returns (rc, primary_path, variant_path)."""
    primary = tmp_path / "events.json"
    variant = tmp_path / "events-newsletter.json"
    argv = ["--ics-url", str(SAMPLE_ICS), "--output", str(primary)]
    if newsletter:
        argv += ["--newsletter-output", str(variant), "--newsletter-config", str(config)]
    if as_of:
        argv += ["--as-of", as_of]
    argv += list(extra)
    return main_mod.main(argv), primary, variant


def load(path):
    return json.loads(Path(path).read_text())


def test_variant_written_alongside_primary_output(tmp_path, capsys):
    rc, primary, variant = run(tmp_path)
    capsys.readouterr()
    assert rc == 0
    assert len(load(primary)) == TOTAL_EVENTS
    assert len(load(variant)) == 2


def test_primary_output_bytes_unchanged_by_newsletter_flag(tmp_path, capsys):
    """Backward-compatibility guard for the hourly WordPress ingest."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    run(a, newsletter=False)
    run(b, newsletter=True)
    capsys.readouterr()
    digest_a = hashlib.sha256((a / "events.json").read_bytes()).hexdigest()
    digest_b = hashlib.sha256((b / "events.json").read_bytes()).hexdigest()
    assert digest_a == digest_b
    # And the variant really was produced in the second run, so this is not vacuous.
    assert len(load(b / "events-newsletter.json")) == 2


def test_variant_contains_exactly_the_expected_guids(tmp_path, capsys):
    _, _, variant = run(tmp_path)
    capsys.readouterr()
    assert {ev["guid"] for ev in load(variant)} == EXPECTED_GUIDS


def test_variant_excludes_the_event_just_past_the_window(tmp_path, capsys):
    """ps_events:11861 starts 2025-09-15, one day past the window."""
    _, primary, variant = run(tmp_path)
    capsys.readouterr()
    assert any(ev["guid"] == "ps_events:11861:delta:0" for ev in load(primary))
    assert all(ev["guid"] != "ps_events:11861:delta:0" for ev in load(variant))


def test_variant_window_boundaries_use_target_timezone(tmp_path, monkeypatch, capsys):
    """Fails loudly if startTime is read as UTC instead of naive America/New_York."""
    edge_events = [
        {"guid": "edge-open", "startTime": "2025-09-08T00:15:00", "endTime": "2025-09-08T01:00:00",
         "urlRef": "https://example.org/1",
         "location": {"name": "", "id": "", "detail": ""},
         "title": "Early Monday", "cancelled": "", "bannerImage": "",
         "itemType": "advertisement"},
        {"guid": "edge-close", "startTime": "2025-09-14T23:45:00", "endTime": "2025-09-15T00:30:00",
         "urlRef": "https://example.org/2",
         "location": {"name": "", "id": "", "detail": ""},
         "title": "Late Sunday", "cancelled": "", "bannerImage": "",
         "itemType": "advertisement"},
    ]
    from src.newsletter import filter_events_for_edition, load_newsletter_config, parse_as_of, upcoming_edition

    cfg = load_newsletter_config(str(TEST_CONFIG), env={})
    edition = upcoming_edition(cfg, parse_as_of(AS_OF, cfg))
    kept = filter_events_for_edition(edition, edge_events)
    assert [ev["guid"] for ev in kept] == ["edge-open", "edge-close"]


def test_variant_keeps_placeholder_titled_events(tmp_path, capsys):
    """Placeholder titles are flagged, never dropped."""
    _, _, variant = run(tmp_path)
    capsys.readouterr()
    data = load(variant)
    flagged = [ev for ev in data if ev["titleIsPlaceholder"]]
    assert len(flagged) >= 1
    assert {ev["guid"] for ev in flagged} <= EXPECTED_GUIDS
    assert all(ev["titleSource"].startswith("fallback-") for ev in flagged)


def test_variant_inherits_series_exclusions(tmp_path, capsys):
    """Exclusions are applied upstream of the window filter, so the variant shrinks."""
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    _, _, before = run(before_dir)
    _, _, after = run(
        after_dir, "--exclude-series", "S. S. Wilks Memorial Seminar in Statistics"
    )
    capsys.readouterr()
    # A delta, not merely an absence: 2 -> 1.
    assert len(load(before)) == 2
    assert len(load(after)) == 1
    assert {ev["guid"] for ev in load(after)} == {"ps_events:11856:delta:0"}


def test_variant_not_truncated_by_limit(tmp_path, capsys):
    rc, primary, variant = run(tmp_path, "--limit", "1")
    capsys.readouterr()
    assert rc == 0
    assert len(load(primary)) == 1
    assert len(load(variant)) == 2


def test_print_only_skips_variant_write_but_reports_it(tmp_path, capsys):
    rc, _, variant = run(tmp_path, "--print-only")
    captured = capsys.readouterr()
    assert rc == 0
    assert not variant.exists()
    assert "skipping newsletter variant write" in captured.err


def test_as_of_produces_byte_identical_output(tmp_path, capsys):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    run(a)
    run(b)
    capsys.readouterr()
    assert (a / "events-newsletter.json").read_bytes() == (b / "events-newsletter.json").read_bytes()


def test_as_of_emits_pinned_edition_warning(tmp_path, capsys):
    run(tmp_path)
    assert "::warning::--as-of=" in capsys.readouterr().err


def test_no_warning_when_as_of_absent(tmp_path, capsys):
    """The warning must be about the pin, not noise on every run."""
    run(tmp_path, as_of=None)
    assert "::warning::--as-of" not in capsys.readouterr().err


def test_bad_newsletter_config_returns_4_after_writing_primary(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    rc, primary, variant = run(tmp_path, config=bad)
    captured = capsys.readouterr()
    assert rc == 4
    # The primary feed still landed, so the hourly ingest keeps working.
    assert len(load(primary)) == TOTAL_EVENTS
    assert not variant.exists()
    assert "Newsletter variant generation failed" in captured.err


def test_variant_stamps_edition_id_on_every_item(tmp_path, capsys):
    _, _, variant = run(tmp_path)
    capsys.readouterr()
    assert {ev["newsletterEdition"] for ev in load(variant)} == {EXPECTED_EDITION}


def test_edition_stamp_does_not_leak_into_primary_output(tmp_path, capsys):
    _, primary, _ = run(tmp_path)
    capsys.readouterr()
    assert all("newsletterEdition" not in ev for ev in load(primary))


def test_edition_sidecar_reports_counts(tmp_path, capsys):
    sidecar = tmp_path / "edition.json"
    _, _, variant = run(tmp_path, "--newsletter-edition-output", str(sidecar))
    capsys.readouterr()
    payload = load(sidecar)
    assert payload["editionId"] == EXPECTED_EDITION
    assert payload["eventCount"] == len(load(variant)) == 2
    assert payload["placeholderCount"] == 2
    assert payload["windowStart"] == "2025-09-08T00:00:00-04:00"
    assert payload["windowEnd"] == "2025-09-14T23:59:59-04:00"
    assert payload["deadlineAt"] == "2025-09-02T12:00:00-04:00"


def test_america_new_york_timezone_is_available():
    """Catches a container image without the IANA tz database at test time."""
    assert ZoneInfo("America/New_York").utcoffset(
        __import__("datetime").datetime(2025, 9, 8, 12)
    ) is not None
