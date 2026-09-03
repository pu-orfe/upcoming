"""Loading and overriding the newsletter schedule configuration."""
import json
from datetime import date, time
from pathlib import Path

import pytest

from src.newsletter import (
    Anchor,
    NewsletterConfigError,
    ScheduleRule,
    load_newsletter_config,
    parse_time,
    parse_weekday,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "newsletter_config.example.json"
LIVE_CONFIG = REPO_ROOT / "newsletter_config.json"


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    """Run where ./newsletter_config.json does not exist, so defaults really apply."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(tmp_path, payload) -> str:
    path = tmp_path / "newsletter_config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

def test_defaults_match_documented_orfe_schedule(isolated_cwd):
    cfg = load_newsletter_config(None, env={})
    assert cfg.timezone == "America/New_York"
    rule = cfg.defaults
    assert rule.publish_weekday == 0                    # Monday
    assert rule.publish_time == time(12, 0, 0)          # noon
    assert rule.deadline.anchor is Anchor.WEEK_START
    assert rule.deadline.offset_days == -6              # the preceding Tuesday
    assert rule.deadline.at == time(12, 0, 0)
    assert rule.coverage_start.anchor is Anchor.PUBLICATION
    assert rule.coverage_start.offset_days == 0
    assert rule.coverage_start.at == time(0, 0, 0)
    assert rule.coverage_end.anchor is Anchor.WEEK_START
    assert rule.coverage_end.offset_days == 6           # through Sunday
    assert rule.coverage_end.at == time(23, 59, 59)


def test_missing_config_file_falls_back_to_defaults(isolated_cwd):
    """A nonexistent explicit path is not an error; mirrors transform.load_config."""
    cfg = load_newsletter_config(str(isolated_cwd / "nope.json"), env={})
    assert cfg.defaults == ScheduleRule()
    assert cfg.source_path is None


def test_example_config_file_loads_and_carries_labor_day_exception():
    """Keeps the shipped template from rotting."""
    cfg = load_newsletter_config(EXAMPLE_CONFIG, env={})
    assert cfg.defaults.publish_weekday == 0
    assert cfg.defaults.publish_time == time(12, 0, 0)
    reasons = {e.reason for e in cfg.exceptions}
    assert "Labor Day" in reasons
    assert any(e.week_of == date(2026, 9, 7) for e in cfg.exceptions)
    assert cfg.blackouts, "example should demonstrate blackouts"
    assert any(r.label == "spring-2027" for r in cfg.schedules)


def test_live_config_file_loads_and_covers_this_labor_day():
    cfg = load_newsletter_config(LIVE_CONFIG, env={})
    exc = next(e for e in cfg.exceptions if e.week_of == date(2026, 9, 7))
    assert exc.publication_date == date(2026, 9, 8)
    assert exc.reason == "Labor Day"


def test_config_fingerprint_is_stable_and_sensitive(tmp_path, isolated_cwd):
    a = load_newsletter_config(LIVE_CONFIG, env={})
    b = load_newsletter_config(LIVE_CONFIG, env={})
    assert a.fingerprint() == b.fingerprint()
    changed = load_newsletter_config(
        _write(tmp_path, {"defaults": {"publication": {"time": "09:00"}}}), env={}
    )
    assert changed.fingerprint() != a.fingerprint()


def test_config_fingerprint_reflects_env_overrides(isolated_cwd):
    base = load_newsletter_config(LIVE_CONFIG, env={})
    overridden = load_newsletter_config(LIVE_CONFIG, env={"NEWSLETTER_PUBLISH_WEEKDAY": "WED"})
    assert base.fingerprint() != overridden.fingerprint()


# --------------------------------------------------------------------------
# Environment overrides
# --------------------------------------------------------------------------

def test_env_overrides_publish_weekday_and_time(isolated_cwd):
    cfg = load_newsletter_config(
        None, env={"NEWSLETTER_PUBLISH_WEEKDAY": "TUE", "NEWSLETTER_PUBLISH_TIME": "09:30"}
    )
    assert cfg.defaults.publish_weekday == 1
    assert cfg.defaults.publish_time == time(9, 30, 0)


def test_env_overrides_beat_config_file(tmp_path, isolated_cwd):
    path = _write(tmp_path, {"defaults": {"publication": {"weekday": "MON", "time": "12:00"}}})
    cfg = load_newsletter_config(
        path, env={"NEWSLETTER_PUBLISH_WEEKDAY": "WED", "NEWSLETTER_PUBLISH_TIME": "08:00"}
    )
    assert cfg.defaults.publish_weekday == 2
    assert cfg.defaults.publish_time == time(8, 0, 0)


def test_env_is_read_from_os_environ_by_default(isolated_cwd, monkeypatch):
    """Guards against reading env as a dataclass field default at import time."""
    monkeypatch.setenv("NEWSLETTER_PUBLISH_WEEKDAY", "FRI")
    assert load_newsletter_config(None).defaults.publish_weekday == 4


def test_newsletter_tz_falls_back_to_target_tz(isolated_cwd):
    cfg = load_newsletter_config(None, env={"TARGET_TZ": "America/Chicago"})
    assert cfg.timezone == "America/Chicago"


def test_newsletter_tz_beats_target_tz(isolated_cwd):
    cfg = load_newsletter_config(
        None, env={"NEWSLETTER_TZ": "America/Denver", "TARGET_TZ": "America/Chicago"}
    )
    assert cfg.timezone == "America/Denver"


def test_deadline_weekday_env_resolves_to_offset_before_publication(isolated_cwd):
    """TUE with Monday publication is the *previous* Tuesday: -6, never +1."""
    cfg = load_newsletter_config(None, env={"NEWSLETTER_DEADLINE_WEEKDAY": "TUE"})
    assert cfg.defaults.deadline.offset_days == -6
    assert cfg.defaults.deadline.anchor is Anchor.WEEK_START


def test_deadline_weekday_env_with_shifted_publication(isolated_cwd):
    """With Wednesday publication, the previous Tuesday is one day back."""
    cfg = load_newsletter_config(
        None,
        env={"NEWSLETTER_PUBLISH_WEEKDAY": "WED", "NEWSLETTER_DEADLINE_WEEKDAY": "TUE"},
    )
    assert cfg.defaults.deadline.offset_days == 1  # week_start + 1 == Tuesday


def test_deadline_weekday_same_as_publication_goes_a_full_week_back(isolated_cwd):
    cfg = load_newsletter_config(None, env={"NEWSLETTER_DEADLINE_WEEKDAY": "MON"})
    assert cfg.defaults.deadline.offset_days == -7


def test_deadline_offset_days_env_wins_over_weekday(isolated_cwd):
    cfg = load_newsletter_config(
        None,
        env={"NEWSLETTER_DEADLINE_WEEKDAY": "TUE", "NEWSLETTER_DEADLINE_OFFSET_DAYS": "-3"},
    )
    assert cfg.defaults.deadline.offset_days == -3


def test_deadline_time_env_override(isolated_cwd):
    cfg = load_newsletter_config(None, env={"NEWSLETTER_DEADLINE_TIME": "17:30:00"})
    assert cfg.defaults.deadline.at == time(17, 30, 0)


def test_reminder_lead_hours_env_parses_csv(isolated_cwd):
    cfg = load_newsletter_config(None, env={"NEWSLETTER_REMINDER_LEAD_HOURS": "72,24,2"})
    assert cfg.defaults.reminder_lead_hours == (72.0, 24.0, 2.0)


def test_newsletter_config_env_selects_the_file(isolated_cwd, tmp_path):
    path = _write(tmp_path, {"defaults": {"publication": {"weekday": "THU"}}})
    cfg = load_newsletter_config(None, env={"NEWSLETTER_CONFIG": path})
    assert cfg.defaults.publish_weekday == 3


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def test_invalid_timezone_raises_config_error(isolated_cwd):
    with pytest.raises(NewsletterConfigError) as excinfo:
        load_newsletter_config(None, env={"NEWSLETTER_TZ": "Not/AZone"})
    assert "tzdata" in str(excinfo.value)


def test_weekday_seven_is_rejected():
    """Guards the ISO (1..7) vs Python (0..6) off-by-one."""
    with pytest.raises(NewsletterConfigError) as excinfo:
        parse_weekday(7)
    assert "ISO" in str(excinfo.value)


@pytest.mark.parametrize("value,expected", [("MON", 0), ("monday", 0), ("Mon", 0), (0, 0), ("6", 6)])
def test_weekday_accepts_name_and_integer_forms(value, expected):
    assert parse_weekday(value) == expected


def test_unknown_weekday_name_raises():
    with pytest.raises(NewsletterConfigError):
        parse_weekday("Funday")


def test_time_accepts_hh_mm_and_hh_mm_ss():
    assert parse_time("12:00") == parse_time("12:00:00") == time(12, 0, 0)


def test_bad_time_raises():
    with pytest.raises(NewsletterConfigError):
        parse_time("noon")


def test_malformed_json_raises_config_error(tmp_path, isolated_cwd):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(NewsletterConfigError):
        load_newsletter_config(str(path), env={})


def test_exception_week_of_must_be_a_monday(tmp_path, isolated_cwd):
    path = _write(tmp_path, {"exceptions": [{"week_of": "2026-09-08"}]})
    with pytest.raises(NewsletterConfigError) as excinfo:
        load_newsletter_config(path, env={})
    assert "Monday" in str(excinfo.value)


def test_blackout_end_before_start_raises(tmp_path, isolated_cwd):
    path = _write(tmp_path, {"blackouts": [{"start": "2027-01-11", "end": "2026-12-21"}]})
    with pytest.raises(NewsletterConfigError):
        load_newsletter_config(path, env={})


# --------------------------------------------------------------------------
# Schedule rule selection
# --------------------------------------------------------------------------

def test_schedule_rules_merge_onto_defaults(tmp_path, isolated_cwd):
    path = _write(tmp_path, {
        "defaults": {"publication": {"weekday": "MON", "time": "12:00"}},
        "schedules": [{"label": "spring", "effective_from": "2027-01-25",
                       "deadline": {"offset_days": -4, "time": "17:00"}}],
    })
    cfg = load_newsletter_config(path, env={})
    rule = cfg.rule_for(date(2027, 2, 1))
    assert rule.label == "spring"
    assert rule.deadline.offset_days == -4
    # Inherited from defaults, not reset.
    assert rule.publish_weekday == 0
    assert rule.publish_time == time(12, 0, 0)
    assert rule.coverage_end.offset_days == 6


def test_rule_selection_picks_latest_effective_from(tmp_path, isolated_cwd):
    path = _write(tmp_path, {"schedules": [
        {"label": "early", "effective_from": "2027-01-01"},
        {"label": "late", "effective_from": "2027-02-01"},
    ]})
    cfg = load_newsletter_config(path, env={})
    assert cfg.rule_for(date(2027, 1, 11)).label == "early"
    assert cfg.rule_for(date(2027, 3, 1)).label == "late"


def test_rule_effective_to_expires_back_to_defaults(tmp_path, isolated_cwd):
    path = _write(tmp_path, {"schedules": [
        {"label": "spring", "effective_from": "2027-01-25", "effective_to": "2027-05-17"},
    ]})
    cfg = load_newsletter_config(path, env={})
    assert cfg.rule_for(date(2027, 3, 1)).label == "spring"
    assert cfg.rule_for(date(2027, 6, 7)).label == "default"
