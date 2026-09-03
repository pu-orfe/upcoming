"""Edition arithmetic: windows, exceptions, DST, reminders.

Calendar facts these tests rely on (all verified):
  2026-08-31, 2026-09-07, 2026-09-21 are Mondays; Labor Day 2026 is Mon 2026-09-07.
  US DST 2026: Mar 8 - Nov 1.  US DST 2027: Mar 14 - Nov 7.
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src import newsletter as nl
from src.newsletter import (
    EditionPhase,
    InvalidEventTime,
    NoEditionFound,
    build_edition,
    current_edition,
    due_reminders,
    edition_for_event,
    event_start_to_aware,
    filter_events_for_edition,
    is_in_coverage,
    iter_editions,
    load_newsletter_config,
    next_deadline_edition,
    stamp_edition,
    upcoming_edition,
    week_start_for,
)

ET = ZoneInfo("America/New_York")
LABOR_DAY = {"week_of": "2026-09-07", "publication_date": "2026-09-08", "reason": "Labor Day"}


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Never pick up the repo's live newsletter_config.json."""
    monkeypatch.chdir(tmp_path)


def cfg_from(payload=None):
    import tempfile
    path = Path(tempfile.mkdtemp()) / "cfg.json"
    path.write_text(json.dumps(payload or {}), encoding="utf-8")
    return load_newsletter_config(str(path), env={})


def et(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=ET)


# --------------------------------------------------------------------------
# Identity and the plain week
# --------------------------------------------------------------------------

def test_edition_id_is_week_start_monday():
    cfg = cfg_from()
    monday = date(2026, 1, 5)
    for _ in range(52):
        assert monday.weekday() == 0
        assert build_edition(cfg, monday).id == monday.isoformat()
        monday += timedelta(days=7)


def test_week_start_for_returns_monday():
    assert week_start_for(date(2026, 9, 13)) == date(2026, 9, 7)   # a Sunday
    assert week_start_for(et(2026, 9, 7, 23, 59)) == date(2026, 9, 7)


def test_build_edition_normalizes_a_non_monday_anchor():
    cfg = cfg_from()
    assert build_edition(cfg, date(2026, 9, 24)).id == "2026-09-21"


def test_plain_week_publication_deadline_and_coverage():
    e = build_edition(cfg_from(), date(2026, 9, 21))
    assert e.publication_at == et(2026, 9, 21, 12)
    assert e.deadline_at == et(2026, 9, 15, 12)      # the preceding Tuesday
    assert e.coverage_start == et(2026, 9, 21, 0, 0, 0)
    assert e.coverage_end == et(2026, 9, 27, 23, 59, 59)


def test_edition_to_dict_is_json_serializable():
    e = build_edition(cfg_from(), date(2026, 9, 21))
    payload = json.loads(json.dumps(e.to_dict()))
    assert payload["id"] == "2026-09-21"
    assert payload["publicationAt"].endswith("-04:00")
    assert payload["reminders"], "reminders should be present by default"


# --------------------------------------------------------------------------
# Roll-over around publication
# --------------------------------------------------------------------------

def test_now_before_publication_returns_that_edition():
    assert upcoming_edition(cfg_from(), et(2026, 9, 21, 11, 59, 59)).id == "2026-09-21"


def test_now_exactly_at_publication_rolls_to_next():
    """publication_at <= now counts as published."""
    assert upcoming_edition(cfg_from(), et(2026, 9, 21, 12, 0, 0)).id == "2026-09-28"


def test_now_after_publication_rolls_to_next_edition():
    cfg = cfg_from()
    now = et(2026, 9, 21, 13)
    assert upcoming_edition(cfg, now).id == "2026-09-28"
    assert current_edition(cfg, now).id == "2026-09-21"


def test_now_mid_week_returns_next_monday_edition():
    assert upcoming_edition(cfg_from(), et(2026, 9, 23, 9)).id == "2026-09-28"


def test_edition_phase_transitions():
    e = build_edition(cfg_from(), date(2026, 9, 21))
    assert e.phase(et(2026, 9, 15, 11, 59)) is EditionPhase.OPEN
    assert e.phase(et(2026, 9, 15, 12, 0)) is EditionPhase.CLOSED
    assert e.phase(et(2026, 9, 20, 9)) is EditionPhase.CLOSED
    assert e.phase(et(2026, 9, 21, 12, 0)) is EditionPhase.PUBLISHED


# --------------------------------------------------------------------------
# Labor Day exception
# --------------------------------------------------------------------------

def test_labor_day_exception_shifts_publication_to_tuesday():
    cfg = cfg_from({"exceptions": [LABOR_DAY]})
    e = upcoming_edition(cfg, et(2026, 9, 2, 9))
    assert e.id == "2026-09-07"
    assert e.publication_at == et(2026, 9, 8, 12)
    assert e.exception_reason == "Labor Day"


def test_labor_day_exception_does_not_change_edition_id():
    """Dedup stability: adding the exception later must not rename the edition."""
    without = build_edition(cfg_from(), date(2026, 9, 7))
    with_exc = build_edition(cfg_from({"exceptions": [LABOR_DAY]}), date(2026, 9, 7))
    assert without.id == with_exc.id == "2026-09-07"
    assert without.notification_key("published") == with_exc.notification_key("published")


def test_labor_day_coverage_starts_tuesday_and_still_ends_sunday():
    e = build_edition(cfg_from({"exceptions": [LABOR_DAY]}), date(2026, 9, 7))
    assert e.coverage_start == et(2026, 9, 8, 0, 0, 0)
    assert e.coverage_end == et(2026, 9, 13, 23, 59, 59)
    assert is_in_coverage(e, "2026-09-07T15:00:00") is False   # Labor Day itself
    assert is_in_coverage(e, "2026-09-08T00:00:00") is True


def test_labor_day_exception_leaves_deadline_on_the_normal_tuesday():
    """The deadline is anchored to the week, so a publication shift does not drag it."""
    e = build_edition(cfg_from({"exceptions": [LABOR_DAY]}), date(2026, 9, 7))
    assert e.deadline_at == et(2026, 9, 1, 12)


def test_exception_can_override_deadline_explicitly():
    exc = dict(LABOR_DAY, deadline_date="2026-09-02")
    e = build_edition(cfg_from({"exceptions": [exc]}), date(2026, 9, 7))
    assert e.deadline_at == et(2026, 9, 2, 12)


def test_exception_skip_true_produces_no_edition():
    cfg = cfg_from({"exceptions": [{"week_of": "2026-09-07", "skip": True}]})
    assert build_edition(cfg, date(2026, 9, 7)) is None
    assert upcoming_edition(cfg, et(2026, 9, 2, 9)).id == "2026-09-14"


# --------------------------------------------------------------------------
# Coverage-window edges
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "start,expected",
    [
        ("2026-09-20T23:59:59", False),
        ("2026-09-21T00:00:00", True),
        ("2026-09-24T16:15:00", True),
        ("2026-09-27T23:59:59", True),
        ("2026-09-28T00:00:00", False),
    ],
)
def test_coverage_window_edges(start, expected):
    e = build_edition(cfg_from(), date(2026, 9, 21))
    assert is_in_coverage(e, {"startTime": start}) is expected


def test_filter_events_for_edition_selects_only_covered_and_preserves_order():
    e = build_edition(cfg_from(), date(2026, 9, 21))
    events = [
        {"guid": "before", "startTime": "2026-09-20T12:00:00"},
        {"guid": "first", "startTime": "2026-09-21T09:00:00"},
        {"guid": "after", "startTime": "2026-10-05T12:00:00"},
        {"guid": "second", "startTime": "2026-09-25T16:30:00"},
    ]
    assert [ev["guid"] for ev in filter_events_for_edition(e, events)] == ["first", "second"]


def test_edition_for_event_finds_the_owning_edition():
    cfg = cfg_from()
    found = edition_for_event(cfg, {"startTime": "2026-10-07T16:15:00"})
    assert found is not None and found.id == "2026-10-05"


def test_stamp_edition_marks_every_event():
    e = build_edition(cfg_from(), date(2026, 9, 21))
    events = [{"guid": "a"}, {"guid": "b"}]
    assert stamp_edition(events, e) == 2
    assert {ev["newsletterEdition"] for ev in events} == {"2026-09-21"}


# --------------------------------------------------------------------------
# Naive-local vs aware
# --------------------------------------------------------------------------

def test_naive_feed_string_is_localized_not_treated_as_utc():
    got = event_start_to_aware("2026-09-21T12:15:00", ET)
    assert got == datetime(2026, 9, 21, 12, 15, tzinfo=ET)
    assert got.utcoffset() == timedelta(hours=-4)
    assert got != datetime(2026, 9, 21, 12, 15, tzinfo=timezone.utc)


def test_event_string_with_utc_suffix_is_rejected():
    """The schema pattern forbids a suffix, so one means the producer is broken."""
    with pytest.raises(InvalidEventTime):
        event_start_to_aware("2026-09-21T12:15:00Z", ET)
    with pytest.raises(InvalidEventTime):
        event_start_to_aware("2026-09-21T12:15:00+00:00", ET)


@pytest.mark.parametrize("value", ["", "   ", "2026-09-21 12:15", "not a date", None])
def test_malformed_event_start_raises_invalid_event_time(value):
    with pytest.raises(InvalidEventTime):
        event_start_to_aware(value, ET)


def test_aware_datetime_input_is_converted_not_relabelled():
    aware_utc = datetime(2026, 9, 21, 16, 15, tzinfo=timezone.utc)
    assert event_start_to_aware(aware_utc, ET) == datetime(2026, 9, 21, 12, 15, tzinfo=ET)


# --------------------------------------------------------------------------
# DST
# --------------------------------------------------------------------------

def test_spring_forward_week_is_167_hours_long():
    """DST starts Sun 2027-03-14, so that Mon..Sun week is an hour short."""
    e = build_edition(cfg_from(), date(2027, 3, 8))
    assert e.coverage_start.utcoffset() == timedelta(hours=-5)
    assert e.coverage_end.utcoffset() == timedelta(hours=-4)
    # Subtracting two aware datetimes in the same zone yields the wall-clock
    # difference, so measure real elapsed time through UTC.
    elapsed = e.coverage_end.astimezone(timezone.utc) - e.coverage_start.astimezone(timezone.utc)
    assert elapsed == timedelta(days=6, hours=22, minutes=59, seconds=59)
    assert e.coverage_end - e.coverage_start == timedelta(days=6, hours=23, minutes=59, seconds=59)
    assert is_in_coverage(e, "2027-03-14T23:00:00") is True


def test_fall_back_week_is_169_hours_long():
    """DST ends Sun 2026-11-01, so that week is an hour long."""
    e = build_edition(cfg_from(), date(2026, 10, 26))
    assert e.coverage_start.utcoffset() == timedelta(hours=-4)
    assert e.coverage_end.utcoffset() == timedelta(hours=-5)
    elapsed = e.coverage_end.astimezone(timezone.utc) - e.coverage_start.astimezone(timezone.utc)
    assert elapsed == timedelta(days=7, minutes=59, seconds=59)


def test_ambiguous_local_time_in_fall_back_hour_is_covered():
    e = build_edition(cfg_from(), date(2026, 10, 26))
    moment = event_start_to_aware("2026-11-01T01:30:00", ET)
    assert moment.fold == 0
    assert e.covers(moment) is True


def test_nonexistent_local_time_in_spring_gap_does_not_crash():
    e = build_edition(cfg_from(), date(2027, 3, 8))
    assert is_in_coverage(e, "2027-03-14T02:30:00") is True


@pytest.mark.parametrize("week", [date(2027, 3, 8), date(2026, 10, 26), date(2026, 11, 9)])
def test_publication_and_deadline_are_wall_clock_stable_across_dst(week):
    e = build_edition(cfg_from(), week)
    assert e.publication_at.hour == 12 and e.publication_at.minute == 0
    assert e.deadline_at.hour == 12 and e.deadline_at.minute == 0


# --------------------------------------------------------------------------
# Blackouts
# --------------------------------------------------------------------------

def test_blackout_range_is_skipped_and_next_edition_returned():
    cfg = cfg_from({"blackouts": [{"start": "2026-12-21", "end": "2027-01-11"}]})
    assert upcoming_edition(cfg, et(2026, 12, 16, 9)).id == "2027-01-18"


def test_blackout_week_of_shorthand_skips_one_week():
    cfg = cfg_from({"blackouts": [{"week_of": "2026-11-23"}]})
    assert build_edition(cfg, date(2026, 11, 23)) is None
    assert build_edition(cfg, date(2026, 11, 30)) is not None


def test_blackout_boundaries_are_inclusive():
    cfg = cfg_from({"blackouts": [{"start": "2026-12-21", "end": "2027-01-11"}]})
    assert build_edition(cfg, date(2026, 12, 21)) is None
    assert build_edition(cfg, date(2027, 1, 11)) is None
    assert build_edition(cfg, date(2027, 1, 18)) is not None


def test_no_edition_within_lookahead_raises():
    cfg = cfg_from({
        "blackouts": [{"start": "2026-09-07", "end": "2028-09-07"}],
        "max_weeks_lookahead": 4,
    })
    with pytest.raises(NoEditionFound):
        upcoming_edition(cfg, et(2026, 9, 8, 9))


def test_current_edition_returns_none_when_nothing_published_in_lookback():
    cfg = cfg_from({
        "blackouts": [{"start": "2024-01-01", "end": "2028-09-07"}],
        "max_weeks_lookahead": 4,
    })
    assert current_edition(cfg, et(2026, 9, 8, 9)) is None


# --------------------------------------------------------------------------
# Semester rule changes
# --------------------------------------------------------------------------

SPRING = {
    "schedules": [{
        "label": "spring-2027",
        "effective_from": "2027-01-25",
        "effective_to": "2027-05-17",
        "deadline": {"anchor": "week_start", "offset_days": -4, "time": "17:00:00"},
        "reminders": {"lead_hours": [48, 12]},
    }]
}


def test_semester_rule_change_moves_deadline():
    cfg = cfg_from(SPRING)
    before = build_edition(cfg, date(2027, 1, 18))
    after = build_edition(cfg, date(2027, 1, 25))
    assert before.deadline_at == et(2027, 1, 12, 12)     # Tuesday noon, default rule
    assert before.rule_label == "default"
    assert after.deadline_at == et(2027, 1, 21, 17)      # Thursday 5pm, spring rule
    assert after.rule_label == "spring-2027"


def test_semester_rule_change_can_move_publication_weekday():
    cfg = cfg_from({"schedules": [
        {"label": "tue", "effective_from": "2027-01-25", "publication": {"weekday": "TUE"}}
    ]})
    e = build_edition(cfg, date(2027, 1, 25))
    assert e.publication_at == et(2027, 1, 26, 12)
    assert e.coverage_start == et(2027, 1, 26, 0, 0, 0)
    assert e.coverage_end == et(2027, 1, 31, 23, 59, 59)   # still that Sunday


def test_semester_rule_change_moves_reminder_leads():
    cfg = cfg_from(SPRING)
    assert len(build_edition(cfg, date(2027, 1, 25)).reminders) == 2
    assert len(build_edition(cfg, date(2026, 9, 21)).reminders) == 4


def test_editions_straddling_a_semester_boundary_use_their_own_rules():
    cfg = cfg_from(SPRING)
    labels = [e.rule_label for e in iter_editions(cfg, date(2027, 1, 11), 4)]
    assert labels == ["default", "default", "spring-2027", "spring-2027"]


# --------------------------------------------------------------------------
# Reminders and cron dedup
# --------------------------------------------------------------------------

def test_reminders_are_lead_hours_before_deadline():
    e = build_edition(cfg_from(), date(2026, 9, 21))
    assert [r.lead_hours for r in e.reminders] == [72.0, 48.0, 24.0, 4.0]
    assert e.reminders[-1].fire_at == e.deadline_at - timedelta(hours=4)


def test_reminder_lead_is_absolute_hours_across_dst():
    """72h before a 2026-11-03 12:00 EST deadline is 2026-10-31 13:00 EDT, not 12:00."""
    e = build_edition(cfg_from(), date(2026, 11, 9))
    assert e.deadline_at == et(2026, 11, 3, 12)
    assert e.deadline_at.utcoffset() == timedelta(hours=-5)
    lead72 = next(r for r in e.reminders if r.lead_hours == 72.0)
    assert lead72.fire_at == et(2026, 10, 31, 13)
    assert lead72.fire_at.utcoffset() == timedelta(hours=-4)


def test_due_reminders_returns_only_passed_leads():
    e = build_edition(cfg_from({"defaults": {"reminders": {"lead_hours": [72, 24]}}}),
                      date(2026, 9, 21))
    now = e.deadline_at - timedelta(hours=48)
    assert [r.lead_hours for r in due_reminders(e, now)] == [72.0]


def test_due_reminders_is_empty_after_deadline():
    e = build_edition(cfg_from(), date(2026, 9, 21))
    assert due_reminders(e, e.deadline_at) == []
    assert due_reminders(e, e.deadline_at + timedelta(hours=1)) == []


def test_due_reminders_is_idempotent_across_repeated_cron_runs():
    e = build_edition(cfg_from(), date(2026, 9, 21))
    now = e.deadline_at - timedelta(hours=30)
    assert [r.key for r in due_reminders(e, now)] == [r.key for r in due_reminders(e, now)]


def test_missed_cron_run_still_fires_the_lead_late():
    e = build_edition(cfg_from({"defaults": {"reminders": {"lead_hours": [72, 24, 2]}}}),
                      date(2026, 9, 21))
    now = e.deadline_at - timedelta(hours=1)
    assert [r.lead_hours for r in due_reminders(e, now)] == [72.0, 24.0, 2.0]


def test_reminder_key_is_unique_per_edition_and_lead():
    cfg = cfg_from({"defaults": {"reminders": {"lead_hours": [72, 24, 2]}}})
    keys = [r.key for e in iter_editions(cfg, date(2026, 9, 21), 3) for r in e.reminders]
    assert len(keys) == 9
    assert len(set(keys)) == 9
    assert keys[0] == "2026-09-21:deadline-72h"


def test_notification_key_is_stable_under_publication_shift():
    a = build_edition(cfg_from(), date(2026, 9, 7))
    b = build_edition(cfg_from({"exceptions": [LABOR_DAY]}), date(2026, 9, 7))
    assert a.notification_key("missing-titles") == b.notification_key("missing-titles")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_prints_upcoming_edition_json(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"exceptions": [LABOR_DAY]}), encoding="utf-8")
    rc = nl.main(["--config", str(path), "--as-of", "2026-09-02T09:00:00", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["edition"]["id"] == "2026-09-07"
    assert out["edition"]["publicationAt"].startswith("2026-09-08T12:00")


def test_cli_print_edition_id_prints_only_the_id(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    path.write_text("{}", encoding="utf-8")
    rc = nl.main(["--config", str(path), "--as-of", "2026-09-02T09:00:00", "--print-edition-id"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "2026-09-07"


def test_cli_print_config_sha256_is_a_hex_digest(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    path.write_text("{}", encoding="utf-8")
    rc = nl.main(["--config", str(path), "--print-config-sha256"])
    digest = capsys.readouterr().out.strip()
    assert rc == 0
    assert len(digest) == 64 and int(digest, 16) >= 0


def test_cli_filters_events_file(tmp_path, capsys):
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text("{}", encoding="utf-8")
    events_path = tmp_path / "events.json"
    events_path.write_text(json.dumps([
        {"guid": "in", "startTime": "2026-09-22T16:15:00", "title": "T"},
        {"guid": "out", "startTime": "2026-10-22T16:15:00", "title": "T"},
    ]), encoding="utf-8")
    rc = nl.main([
        "--config", str(cfg_path), "--as-of", "2026-09-16T09:00:00",
        "--events", str(events_path),
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["eventCount"] == 1
    assert [e["guid"] for e in out["events"]] == ["in"]


def test_cli_reports_due_reminders(tmp_path, capsys):
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text("{}", encoding="utf-8")
    rc = nl.main([
        "--config", str(cfg_path), "--as-of", "2026-09-13T09:00:00",
        "--which", "next-deadline", "--reminders",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    # Edition 2026-09-21 has deadline 2026-09-15T12:00; 09-13 09:00 is 51h out,
    # so the 72h lead has fired but the 48h one has not.
    assert out["edition"]["id"] == "2026-09-21"
    assert [r["leadHours"] for r in out["dueReminders"]] == [72.0]


def test_next_deadline_edition_is_not_the_next_to_publish():
    """A deadline falls six days before its own publication, so the edition being
    chased is always the one after the next to publish."""
    cfg = cfg_from()
    now = et(2026, 9, 11, 9)      # Friday
    assert upcoming_edition(cfg, now).id == "2026-09-14"
    chased = next_deadline_edition(cfg, now)
    assert chased.id == "2026-09-21"
    assert chased.deadline_at == et(2026, 9, 15, 12)


def test_next_deadline_edition_rolls_at_the_deadline():
    cfg = cfg_from()
    assert next_deadline_edition(cfg, et(2026, 9, 15, 11, 59, 59)).id == "2026-09-21"
    assert next_deadline_edition(cfg, et(2026, 9, 15, 12, 0, 0)).id == "2026-09-28"


def test_next_deadline_edition_skips_blacked_out_weeks():
    cfg = cfg_from({"blackouts": [{"week_of": "2026-09-21"}]})
    assert next_deadline_edition(cfg, et(2026, 9, 11, 9)).id == "2026-09-28"


def test_next_deadline_edition_raises_past_the_horizon():
    cfg = cfg_from({
        "blackouts": [{"start": "2024-01-01", "end": "2030-01-01"}],
        "max_weeks_lookahead": 3,
    })
    with pytest.raises(NoEditionFound):
        next_deadline_edition(cfg, et(2026, 9, 11, 9))


def test_cli_exits_2_on_bad_config(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    path.write_text("{not json", encoding="utf-8")
    rc = nl.main(["--config", str(path)])
    assert rc == 2
    assert "config error" in capsys.readouterr().err


def test_cli_exits_3_when_no_edition_found(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({
        "blackouts": [{"start": "2024-01-01", "end": "2030-01-01"}],
        "max_weeks_lookahead": 3,
    }), encoding="utf-8")
    rc = nl.main(["--config", str(path), "--as-of", "2026-09-02T09:00:00"])
    assert rc == 3
    assert capsys.readouterr().err.strip()
