"""Deadline watch: digest computation and idempotent GitHub issue reconciliation.

Every case runs against a FakeTransport, so no test touches the network.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

import pytest

from src import notify_missing_titles as nmt
from src.newsletter import build_edition, load_newsletter_config

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_CONFIG = REPO_ROOT / "tests" / "fixtures" / "newsletter_config.test.json"
ET = ZoneInfo("America/New_York")
REPO = "pu-orfe/upcoming"
EDITION_ID = "2025-09-15"


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")


class FakeTransport:
    """Records requests and replays scripted responses."""

    def __init__(self, issues=None, events=None, release_body=""):
        self.issues = list(issues or [])
        self.events = events if events is not None else []
        self.release_body = release_body
        self.requests = []          # (method, url, payload)
        self.next_issue_number = 101
        self.fail_with = None

    # -- helpers ----------------------------------------------------------

    def calls(self, method=None, contains=None):
        return [
            r for r in self.requests
            if (method is None or r[0] == method) and (contains is None or contains in r[1])
        ]

    @property
    def writes(self):
        return [r for r in self.requests if r[0] != "GET"]

    # -- transport --------------------------------------------------------

    def __call__(self, request):
        payload = json.loads(request.data.decode()) if request.data else None
        self.requests.append((request.get_method(), request.full_url, payload))
        if self.fail_with is not None:
            raise self.fail_with
        return self._route(request.get_method(), request.full_url, payload)

    def _route(self, method, url, payload):
        if method == "GET" and "/releases/tags/" in url:
            return 200, json.dumps({
                "body": self.release_body,
                "assets": [{"name": "events.json",
                            "url": "https://api.github.com/assets/1"}],
            }).encode()
        if method == "GET" and "/assets/" in url:
            return 200, json.dumps(self.events).encode()
        if method == "GET" and "/issues?" in url:
            return 200, json.dumps(self.issues).encode()
        if method == "POST" and url.endswith("/labels"):
            return 201, b"{}"
        if method == "POST" and url.endswith("/issues"):
            number = self.next_issue_number
            self.next_issue_number += 1
            issue = {"number": number, "state": "open",
                     "title": payload["title"], "body": payload["body"]}
            self.issues.append(issue)
            return 201, json.dumps(issue).encode()
        if method == "POST" and url.endswith("/comments"):
            return 201, b"{}"
        if method == "PATCH" and "/issues/" in url:
            number = int(url.rsplit("/", 1)[-1])
            for issue in self.issues:
                if issue["number"] == number:
                    issue.update(payload)
            return 200, json.dumps({"number": number}).encode()
        raise AssertionError(f"unexpected request: {method} {url}")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def cfg():
    return load_newsletter_config(str(TEST_CONFIG), env={})


def edition():
    return build_edition(cfg(), datetime(2025, 9, 15).date())


def event(guid, start, *, placeholder, series="Optimization Seminar", speaker="Alice"):
    return {
        "guid": guid, "startTime": start, "endTime": start,
        "urlRef": f"https://example.org/{guid}",
        "location": {"name": "", "id": "", "detail": ""},
        "title": "An Optimization Seminar Talk" if placeholder else "A Real Title",
        "cancelled": "", "bannerImage": "", "itemType": "advertisement",
        "series": series, "speaker": speaker,
        "titleSource": "fallback-series" if placeholder else "enriched",
        "titleIsPlaceholder": placeholder,
    }


#: 7 events inside the 2025-09-15 window, 3 of them placeholders, plus 2 outside.
WINDOW_EVENTS = (
    [event(f"in-{i}", f"2025-09-{15 + i}T12:15:00", placeholder=i < 3) for i in range(7)]
    + [event("before", "2025-09-14T12:15:00", placeholder=True),
       event("after", "2025-09-22T12:15:00", placeholder=True)]
)


def run(argv_extra=(), transport=None, events=None, issues=None, release_body="", as_of=None):
    transport = transport or FakeTransport(
        issues=issues, events=events if events is not None else WINDOW_EVENTS,
        release_body=release_body,
    )
    argv = [
        "--repo", REPO, "--source", "release:latest:events.json",
        "--newsletter-config", str(TEST_CONFIG),
        "--as-of", as_of or "2025-09-08T12:00:00-04:00",
        "--run-url", "https://example.org/run/1",
        *argv_extra,
    ]
    code = nmt.main(argv, transport=transport)
    return code, transport


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------

def test_collects_only_placeholder_events_inside_window():
    e = edition()
    now = datetime(2025, 9, 8, 12, tzinfo=ET)
    digest = nmt.collect_missing(WINDOW_EVENTS, e, now=now, lead_hours=(72, 48, 24, 4))
    # Both numbers, so an over-broad filter fails too.
    assert digest.total_in_window == 7
    assert len(digest.missing) == 3
    assert {m.guid for m in digest.missing} == {"in-0", "in-1", "in-2"}


def test_hours_to_deadline_and_milestone_selection():
    e = edition()
    # Deadline is 2025-09-09T12:00 EDT.
    digest = nmt.collect_missing(
        WINDOW_EVENTS, e, now=datetime(2025, 9, 8, 12, tzinfo=ET), lead_hours=(72, 48, 24, 4)
    )
    assert round(digest.hours_to_deadline) == 24
    assert digest.due_milestone == "24"
    assert digest.past_deadline is False


def test_past_deadline_milestone():
    digest = nmt.collect_missing(
        WINDOW_EVENTS, edition(),
        now=datetime(2025, 9, 10, 12, tzinfo=ET), lead_hours=(72, 48, 24, 4),
    )
    assert digest.past_deadline is True
    assert digest.due_milestone == nmt.PAST_DEADLINE_MILESTONE


def test_no_milestone_when_deadline_is_far_out():
    digest = nmt.collect_missing(
        WINDOW_EVENTS, edition(),
        now=datetime(2025, 9, 1, 12, tzinfo=ET), lead_hours=(72, 48, 24, 4),
    )
    assert digest.due_milestone is None


# --------------------------------------------------------------------------
# Empty window guard
# --------------------------------------------------------------------------

def test_empty_coverage_window_exits_empty_window_code(capsys):
    code, transport = run(events=[])
    out = capsys.readouterr()
    assert code == nmt.EXIT_EMPTY_WINDOW
    assert "0" in out.out
    assert "Events in window | 0" in out.out
    assert transport.writes == [], "must not touch issues when the feed looks wedged"


def test_allow_empty_window_downgrades_to_ok(capsys):
    code, _ = run(["--allow-empty-window"], events=[])
    out = capsys.readouterr().out
    assert code == nmt.EXIT_OK
    # Still records the zero rather than reporting a clean bill of health.
    assert "Events in window | 0" in out


def test_no_placeholders_and_no_issue_is_noop_but_reports_counts(capsys):
    clean = [event(f"in-{i}", f"2025-09-{15 + i}T12:15:00", placeholder=False) for i in range(7)]
    code, transport = run(events=clean)
    out = capsys.readouterr().out
    assert code == nmt.EXIT_OK
    assert "Missing real titles | 0 of 7 in window" in out
    assert transport.writes == []


# --------------------------------------------------------------------------
# Issue lifecycle
# --------------------------------------------------------------------------

def test_creates_issue_once_when_placeholders_present(capsys):
    code, transport = run()
    capsys.readouterr()
    assert code == nmt.EXIT_OK
    creates = [r for r in transport.calls("POST") if r[1].endswith("/issues")]
    assert len(creates) == 1
    body = creates[0][2]["body"]
    parsed = nmt.parse_marker(body)
    assert parsed is not None and parsed[0] == EDITION_ID
    assert creates[0][2]["labels"] == [nmt.DEFAULT_LABEL]


def test_second_run_updates_existing_issue_instead_of_creating(capsys):
    _, first = run()
    capsys.readouterr()
    # Replay with the issue the first run created already present.
    _, second = run(issues=list(first.issues))
    capsys.readouterr()
    assert [r for r in second.calls("POST") if r[1].endswith("/issues")] == []
    assert len(second.calls("PATCH")) == 1


def test_matches_existing_issue_by_marker_not_title(capsys):
    existing = [{
        "number": 7, "state": "open", "title": "renamed by a human",
        "body": nmt.marker(EDITION_ID, {"24"}) + "\n\nstale text",
    }]
    code, transport = run(issues=existing)
    capsys.readouterr()
    assert code == nmt.EXIT_OK
    assert [r for r in transport.calls("POST") if r[1].endswith("/issues")] == []
    assert transport.calls("PATCH")[0][1].endswith("/issues/7")


def test_ignores_issue_for_a_different_edition(capsys):
    existing = [{
        "number": 7, "state": "open", "title": "Newsletter 2025-09-08: ...",
        "body": nmt.marker("2025-09-08", set()),
    }]
    _, transport = run(issues=existing)
    capsys.readouterr()
    assert len([r for r in transport.calls("POST") if r[1].endswith("/issues")]) == 1


def test_ignores_pull_requests_carrying_the_label(capsys):
    existing = [{
        "number": 7, "state": "open", "title": "a PR", "pull_request": {"url": "x"},
        "body": nmt.marker(EDITION_ID, set()),
    }]
    _, transport = run(issues=existing)
    capsys.readouterr()
    assert len([r for r in transport.calls("POST") if r[1].endswith("/issues")]) == 1


def test_reopens_closed_issue_when_placeholders_reappear(capsys):
    existing = [{
        "number": 7, "state": "closed", "title": "t",
        "body": nmt.marker(EDITION_ID, {"72"}),
    }]
    _, transport = run(issues=existing)
    capsys.readouterr()
    patches = transport.calls("PATCH")
    assert len(patches) == 1
    assert patches[0][2]["state"] == "open"
    assert transport.calls("POST", contains="/comments")


def test_closes_issue_when_all_titles_supplied(capsys):
    clean = [event(f"in-{i}", f"2025-09-{15 + i}T12:15:00", placeholder=False) for i in range(7)]
    existing = [{
        "number": 7, "state": "open", "title": "t",
        "body": nmt.marker(EDITION_ID, {"72", "24"}),
    }]
    code, transport = run(events=clean, issues=existing)
    capsys.readouterr()
    assert code == nmt.EXIT_OK
    # Comment first, then close, so the reason is visible above the state change.
    kinds = [(r[0], r[1].rsplit("/", 1)[-1]) for r in transport.writes]
    assert kinds == [("POST", "comments"), ("PATCH", "7")]
    assert transport.calls("PATCH")[0][2]["state"] == "closed"


def test_already_closed_and_clean_is_noop(capsys):
    clean = [event(f"in-{i}", f"2025-09-{15 + i}T12:15:00", placeholder=False) for i in range(7)]
    existing = [{"number": 7, "state": "closed", "title": "t",
                 "body": nmt.marker(EDITION_ID, {"72"})}]
    code, transport = run(events=clean, issues=existing)
    capsys.readouterr()
    assert code == nmt.EXIT_OK
    assert transport.writes == []


# --------------------------------------------------------------------------
# Milestone announcements
# --------------------------------------------------------------------------

def test_creation_records_the_milestone_without_a_separate_comment(capsys):
    """Opening an issue already notifies; a comment on top would be duplicate noise."""
    _, transport = run()
    capsys.readouterr()
    creates = [r for r in transport.calls("POST") if r[1].endswith("/issues")]
    assert len(creates) == 1
    assert transport.calls("POST", contains="/comments") == []
    _, announced = nmt.parse_marker(creates[0][2]["body"])
    assert announced == frozenset({"24"})


def test_comments_only_on_newly_crossed_milestone(capsys):
    """A later run inside the same milestone refreshes the body but must not re-notify."""
    _, first = run()
    capsys.readouterr()

    _, second = run(issues=list(first.issues), as_of="2025-09-08T15:00:00-04:00")
    capsys.readouterr()
    assert second.calls("POST", contains="/comments") == []
    assert len(second.calls("PATCH")) == 1, "the body is still refreshed"

    # T-3h crosses the 4h lead, which has not been announced yet.
    _, third = run(issues=list(second.issues), as_of="2025-09-09T09:00:00-04:00")
    capsys.readouterr()
    assert len(third.calls("POST", contains="/comments")) == 1


def test_each_milestone_announces_once_as_the_deadline_approaches(capsys):
    issues = None
    comments = []
    for as_of in ["2025-09-06T13:00:00-04:00",   # T-71h -> 72, opens the issue
                  "2025-09-07T13:00:00-04:00",   # T-47h -> 48
                  "2025-09-08T13:00:00-04:00",   # T-23h -> 24
                  "2025-09-09T09:00:00-04:00"]:  # T-3h  -> 4
        # Pinned: 'auto' would escalate to the edition publishing on 2025-09-08,
        # which also has an outstanding placeholder. That behavior has its own test.
        _, transport = run(["--target", "next-deadline"], issues=issues, as_of=as_of)
        capsys.readouterr()
        comments.append(len(transport.calls("POST", contains="/comments")))
        issues = list(transport.issues)
    # The first milestone rides on issue creation; the rest each comment exactly once.
    assert comments == [0, 1, 1, 1]
    _, announced = nmt.parse_marker(issues[0]["body"])
    assert announced == frozenset({"72", "48", "24", "4"})


def test_announced_milestones_round_trip_through_marker():
    text = nmt.marker("2025-09-15", {"72", "48"})
    assert nmt.parse_marker(text) == ("2025-09-15", frozenset({"72", "48"}))
    assert nmt.parse_marker(nmt.marker("2025-09-15", set())) == ("2025-09-15", frozenset())
    assert nmt.parse_marker("no marker here") is None


def test_marker_accepts_the_past_deadline_milestone():
    text = nmt.marker("2025-09-15", {"24", nmt.PAST_DEADLINE_MILESTONE})
    edition_id, announced = nmt.parse_marker(text)
    assert edition_id == "2025-09-15"
    assert nmt.PAST_DEADLINE_MILESTONE in announced


# --------------------------------------------------------------------------
# Exit codes
# --------------------------------------------------------------------------

def test_past_deadline_with_placeholders_exits_one(capsys):
    code, _ = run(["--target", "upcoming"], as_of="2025-09-10T12:00:00-04:00")
    assert code == nmt.EXIT_DEADLINE_MISSED
    assert "Deadline" in capsys.readouterr().out


def test_past_deadline_without_placeholders_exits_zero(capsys):
    clean = [event(f"in-{i}", f"2025-09-{15 + i}T12:15:00", placeholder=False) for i in range(7)]
    code, _ = run(["--target", "upcoming"], events=clean, as_of="2025-09-10T12:00:00-04:00")
    out = capsys.readouterr().out
    assert code == nmt.EXIT_OK
    assert "Missing real titles | 0 of 7 in window" in out


def test_auto_target_escalates_to_the_edition_about_to_publish(capsys):
    """Its deadline has passed, so a remaining placeholder needs a late addition."""
    code, _ = run(as_of="2025-09-10T12:00:00-04:00")
    out = capsys.readouterr().out
    assert code == nmt.EXIT_DEADLINE_MISSED
    assert "edition 2025-09-15" in out


def test_auto_target_chases_the_next_deadline_when_nothing_is_outstanding(capsys):
    clean = [event(f"in-{i}", f"2025-09-{15 + i}T12:15:00", placeholder=False) for i in range(7)]
    code, _ = run(["--allow-empty-window"], events=clean, as_of="2025-09-10T12:00:00-04:00")
    out = capsys.readouterr().out
    assert code == nmt.EXIT_OK
    assert "edition 2025-09-22" in out


def test_dry_run_degrades_when_the_issue_list_is_unreachable(capsys):
    """The digest is still worth having, but the gap must be visible."""
    transport = FakeTransport(events=WINDOW_EVENTS)
    real_route = transport._route

    def route(method, url, payload):
        if "/issues?" in url:
            raise HTTPError(url, 401, "Bad credentials", {}, None)
        return real_route(method, url, payload)

    transport._route = route
    code, transport = run(["--dry-run"], transport=transport)
    out = capsys.readouterr()
    assert code == nmt.EXIT_OK
    assert "::warning::could not read existing issues" in out.err
    assert "issue state unavailable" in out.out
    # The digest itself still landed.
    assert "Missing real titles | 3 of 7 in window" in out.out


def test_issue_list_failure_is_fatal_when_not_a_dry_run(capsys):
    transport = FakeTransport(events=WINDOW_EVENTS)
    real_route = transport._route

    def route(method, url, payload):
        if "/issues?" in url:
            raise HTTPError(url, 401, "Bad credentials", {}, None)
        return real_route(method, url, payload)

    transport._route = route
    code, _ = run(transport=transport)
    out = capsys.readouterr()
    assert code == nmt.EXIT_ERROR
    assert "HTTP 401" in out.err
    # The digest still computed, but the summary must not read as a clean bill of health.
    assert "| Failed |" in out.out


def test_api_failure_exits_error_and_still_emits_summary(capsys):
    transport = FakeTransport(events=WINDOW_EVENTS)
    transport.fail_with = HTTPError("https://api.github.com/x", 503, "boom", {}, None)
    code, _ = run(transport=transport)
    out = capsys.readouterr()
    assert code == nmt.EXIT_ERROR
    assert out.err.strip()
    assert "Newsletter deadline watch" in out.out and "Failed:" in out.out


def test_missing_token_without_dry_run_exits_error(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("NOTIFY_GITHUB_TOKEN", raising=False)
    code, transport = run()
    out = capsys.readouterr()
    assert code == nmt.EXIT_ERROR
    assert "GITHUB_TOKEN" in out.err
    assert transport.requests == [], "must not proceed unauthenticated"


def test_bad_config_exits_error(capsys, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code = nmt.main([
        "--repo", REPO, "--source", "file:/dev/null",
        "--newsletter-config", str(bad), "--dry-run",
    ], transport=FakeTransport())
    out = capsys.readouterr()
    assert code == nmt.EXIT_ERROR
    assert "Failed:" in out.out


# --------------------------------------------------------------------------
# Dry run and staleness
# --------------------------------------------------------------------------

def test_dry_run_makes_no_write_requests(capsys):
    code, transport = run(["--dry-run"])
    out = capsys.readouterr().out
    assert code == nmt.EXIT_OK
    assert transport.writes == []
    # But it still did the work and reported all three offending events.
    assert "Missing real titles | 3 of 7 in window" in out
    for guid in ("in-0", "in-1", "in-2"):
        assert guid in out or "Optimization Seminar" in out


def test_warns_when_published_variant_edition_is_stale(capsys):
    code, _ = run(release_body="ICS_SHA256:abc NEWSLETTER_EDITION:2025-09-08")
    out = capsys.readouterr()
    assert code == nmt.EXIT_OK
    assert "::warning::" in out.err and "stale" in out.err
    assert "out of sync" in out.out


def test_no_stale_warning_when_variant_edition_matches(capsys):
    code, _ = run(release_body=f"ICS_SHA256:abc NEWSLETTER_EDITION:{EDITION_ID}")
    out = capsys.readouterr()
    assert code == nmt.EXIT_OK
    assert "::warning::" not in out.err
    assert "in sync" in out.out


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs,expected_code",
    [
        ({}, nmt.EXIT_OK),
        ({"events": []}, nmt.EXIT_EMPTY_WINDOW),
        ({"as_of": "2025-09-10T12:00:00-04:00"}, nmt.EXIT_DEADLINE_MISSED),
    ],
)
def test_step_summary_always_non_empty(capsys, kwargs, expected_code):
    code, _ = run(**kwargs)
    out = capsys.readouterr().out
    assert code == expected_code
    assert out.strip()
    assert EDITION_ID in out


def test_step_summary_reports_the_schedule_exception(capsys):
    """The Labor Day edition should say why it moved."""
    code, _ = run(as_of="2025-08-25T13:00:00-04:00",
                  events=[event("x", "2025-09-02T12:15:00", placeholder=True)])
    out = capsys.readouterr().out
    assert code == nmt.EXIT_OK
    assert "2025-09-01" in out


def test_render_body_lists_every_missing_event():
    digest = nmt.collect_missing(
        WINDOW_EVENTS, edition(),
        now=datetime(2025, 9, 8, 12, tzinfo=ET), lead_hours=(72, 48, 24, 4),
    )
    body = nmt.render_body(digest, {"24"}, run_url="https://example.org/run/1")
    assert body.splitlines()[0].startswith("<!-- newsletter-missing-titles")
    for item in digest.missing:
        assert item.start_time in body
    assert "Awaiting a title | 3" in body
