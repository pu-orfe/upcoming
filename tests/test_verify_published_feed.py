import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from src import verify_published_feed as verify

REPO = "pu-orfe/upcoming"
BASE_URL = "https://upcoming.orfe.princeton.edu"
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

CURRENT = b'[{"guid": "fresh"}]'
STALE = b"[]"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture
def check():
    return verify.Check(site_path="events.json", tag="latest", asset_name="events.json")


def _release(monkeypatch, payload: bytes, published_minutes_ago: int):
    """Stub the release lookup with a payload of a given age."""
    published_at = NOW - timedelta(minutes=published_minutes_ago)
    monkeypatch.setattr(
        verify,
        "fetch_release_asset",
        lambda repo, tag, asset, token: (payload, published_at),
    )


def _run(check, live_responses, **overrides):
    """Drive verify_check with a scripted sequence of live responses."""
    calls: list[str] = []

    def live_fetcher(url):
        calls.append(url)
        response = live_responses[min(len(calls) - 1, len(live_responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response

    kwargs = dict(
        base_url=BASE_URL,
        repo=REPO,
        token=None,
        attempts=3,
        interval=0,
        grace=timedelta(minutes=20),
        now=lambda: NOW,
        sleep=lambda _seconds: None,
        live_fetcher=live_fetcher,
    )
    kwargs.update(overrides)
    return verify.verify_check(check, **kwargs), calls


def test_parse_check_splits_site_path_and_source():
    parsed = verify.parse_check("dev/events-nofpo.json=dev:events-nofpo.json")
    assert parsed.site_path == "dev/events-nofpo.json"
    assert parsed.tag == "dev"
    assert parsed.asset_name == "events-nofpo.json"


@pytest.mark.parametrize("spec", ["events.json", "events.json=latest", "=latest:events.json", "events.json=:x"])
def test_parse_check_rejects_malformed_specs(spec):
    with pytest.raises(verify.VerificationError):
        verify.parse_check(spec)


def test_matching_content_passes_on_first_sample(monkeypatch, check):
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    result, calls = _run(check, [CURRENT])
    assert result.status == verify.STATUS_MATCH
    assert result.ok
    # A match short-circuits; no reason to keep sampling.
    assert calls == [f"{BASE_URL}/events.json"]


def test_one_matching_edge_is_enough(monkeypatch, check):
    """Independent edge caches mean a stale sample alone proves nothing."""
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    result, calls = _run(check, [STALE, STALE, CURRENT])
    assert result.status == verify.STATUS_MATCH
    assert len(calls) == 3


def test_persistent_stale_content_is_drift(monkeypatch, check):
    """The failure mode from the wedged pipeline: fresh release, stale site."""
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    result, _ = _run(check, [STALE, STALE, STALE])
    assert result.status == verify.STATUS_DRIFT
    assert not result.ok
    assert result.expected_digest == _sha(CURRENT)
    assert {sample.digest for sample in result.samples} == {_sha(STALE)}


def test_recent_publish_is_pending_not_drift(monkeypatch, check):
    """Inside the CDN TTL a mismatch is expected, not a fault."""
    _release(monkeypatch, CURRENT, published_minutes_ago=5)
    result, _ = _run(check, [STALE, STALE, STALE])
    assert result.status == verify.STATUS_PENDING
    assert result.ok
    assert "grace window" in result.detail


def test_grace_boundary_is_exclusive(monkeypatch, check):
    _release(monkeypatch, CURRENT, published_minutes_ago=20)
    result, _ = _run(check, [STALE])
    assert result.status == verify.STATUS_DRIFT


def test_transient_fetch_failure_does_not_mask_a_match(monkeypatch, check):
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    result, _ = _run(check, [verify.VerificationError("HTTP 503"), CURRENT])
    assert result.status == verify.STATUS_MATCH


def test_site_unreachable_is_error_not_drift(monkeypatch, check):
    """A dead network must not be reported as a content problem."""
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    result, _ = _run(check, [verify.VerificationError("HTTP 503")])
    assert result.status == verify.STATUS_ERROR
    assert "failed to fetch" in result.detail


def test_missing_release_asset_is_error(monkeypatch, check):
    def boom(repo, tag, asset, token):
        raise verify.VerificationError("no asset named events.json")

    monkeypatch.setattr(verify, "fetch_release_asset", boom)
    result, _ = _run(check, [CURRENT])
    assert result.status == verify.STATUS_ERROR


def test_sleep_is_skipped_before_the_first_attempt(monkeypatch, check):
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    slept: list[float] = []
    _run(check, [STALE, STALE, CURRENT], sleep=slept.append, interval=30)
    assert slept == [30, 30]


def test_dev_paths_resolve_against_the_base_url(monkeypatch):
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    check = verify.Check(site_path="dev/events-nofpo.json", tag="dev", asset_name="events-nofpo.json")
    _, calls = _run(check, [CURRENT])
    assert calls == [f"{BASE_URL}/dev/events-nofpo.json"]


def test_main_returns_drift_exit_code(monkeypatch):
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    monkeypatch.setattr(verify, "fetch_live", lambda url: STALE)
    exit_code = verify.main(
        [
            "--base-url", BASE_URL,
            "--repo", REPO,
            "--check", "events.json=latest:events.json",
            "--attempts", "2",
            "--interval", "0",
        ]
    )
    assert exit_code == verify.EXIT_DRIFT


def test_main_returns_ok_when_content_matches(monkeypatch):
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    monkeypatch.setattr(verify, "fetch_live", lambda url: CURRENT)
    exit_code = verify.main(
        ["--base-url", BASE_URL, "--repo", REPO, "--check", "events.json=latest:events.json", "--interval", "0"]
    )
    assert exit_code == verify.EXIT_OK


def test_main_writes_step_outputs(monkeypatch, tmp_path):
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    monkeypatch.setattr(verify, "fetch_live", lambda url: STALE)
    verify.main(
        ["--base-url", BASE_URL, "--repo", REPO, "--check", "events.json=latest:events.json", "--interval", "0"]
    )
    assert "drift=true" in output.read_text(encoding="utf-8")


def test_report_shows_both_digests(monkeypatch, check):
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    result, _ = _run(check, [STALE, STALE, STALE])
    report = verify.format_report([result])
    assert _sha(CURRENT)[:12] in report
    assert _sha(STALE)[:12] in report
    assert "drift" in report


ICS_LIVE = b"BEGIN:VCALENDAR\r\nX:new\r\nEND:VCALENDAR\r\n"
ICS_URL = "https://orfe.princeton.edu/feeds/events/upcoming.ics"


def _body(digest_hex: str) -> str:
    return f"Automated update of events feed. ICS_SHA256:{digest_hex}"


def test_extract_ics_digest_reads_the_marker():
    assert verify.extract_ics_digest(_body("abc123")) == "abc123"


def test_extract_ics_digest_requires_the_marker():
    with pytest.raises(verify.VerificationError, match="ICS_SHA256"):
        verify.extract_ics_digest("Automated update of events feed.")


def _ics_run(monkeypatch, release_body, live_ics):
    monkeypatch.setattr(verify, "fetch_release_body", lambda repo, tag, token: release_body)

    def ics_fetcher(url):
        if isinstance(live_ics, Exception):
            raise live_ics
        return live_ics

    return verify.verify_ics_freshness(
        ics_url=ICS_URL, repo=REPO, tag="latest", token=None, ics_fetcher=ics_fetcher
    )


def test_release_built_from_current_ics_matches(monkeypatch):
    result = _ics_run(monkeypatch, _body(_sha(ICS_LIVE)), ICS_LIVE)
    assert result.status == verify.STATUS_MATCH
    assert result.ok


def test_release_built_from_older_ics_is_stale(monkeypatch):
    """The wedge: the feed moved on but the pipeline stopped regenerating."""
    result = _ics_run(monkeypatch, _body(_sha(b"old ics")), ICS_LIVE)
    assert result.status == verify.STATUS_STALE
    assert not result.ok
    assert result.expected_digest == _sha(b"old ics")
    assert result.samples[0].digest == _sha(ICS_LIVE)


def test_unreachable_ics_is_error_not_stale(monkeypatch):
    result = _ics_run(monkeypatch, _body(_sha(ICS_LIVE)), verify.VerificationError("HTTP 502"))
    assert result.status == verify.STATUS_ERROR


def test_missing_ics_marker_is_error_not_stale(monkeypatch):
    result = _ics_run(monkeypatch, "no marker here", ICS_LIVE)
    assert result.status == verify.STATUS_ERROR


def test_ics_check_is_skipped_without_the_flag(monkeypatch):
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    monkeypatch.setattr(verify, "fetch_live", lambda url: CURRENT)

    def unexpected(*args, **kwargs):
        raise AssertionError("ICS freshness must not run unless --ics-url is given")

    monkeypatch.setattr(verify, "verify_ics_freshness", unexpected)
    assert verify.main(
        ["--base-url", BASE_URL, "--repo", REPO, "--check", "events.json=latest:events.json", "--interval", "0"]
    ) == verify.EXIT_OK


def test_main_returns_stale_exit_code_when_only_ics_diverges(monkeypatch):
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    monkeypatch.setattr(verify, "fetch_live", lambda url: CURRENT)
    monkeypatch.setattr(verify, "fetch_release_body", lambda repo, tag, token: _body(_sha(b"old ics")))
    monkeypatch.setattr(verify, "fetch_ics", lambda url: ICS_LIVE)
    assert verify.main(
        [
            "--base-url", BASE_URL,
            "--repo", REPO,
            "--check", "events.json=latest:events.json",
            "--interval", "0",
            "--ics-url", ICS_URL,
        ]
    ) == verify.EXIT_STALE


def test_drift_outranks_stale_in_the_exit_code(monkeypatch):
    """Both wrong at once: a broken deploy is the more actionable cause."""
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    monkeypatch.setattr(verify, "fetch_live", lambda url: STALE)
    monkeypatch.setattr(verify, "fetch_release_body", lambda repo, tag, token: _body(_sha(b"old ics")))
    monkeypatch.setattr(verify, "fetch_ics", lambda url: ICS_LIVE)
    assert verify.main(
        [
            "--base-url", BASE_URL,
            "--repo", REPO,
            "--check", "events.json=latest:events.json",
            "--attempts", "1",
            "--interval", "0",
            "--ics-url", ICS_URL,
        ]
    ) == verify.EXIT_DRIFT


def test_main_writes_stale_output_flag(monkeypatch, tmp_path):
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    _release(monkeypatch, CURRENT, published_minutes_ago=120)
    monkeypatch.setattr(verify, "fetch_live", lambda url: CURRENT)
    monkeypatch.setattr(verify, "fetch_release_body", lambda repo, tag, token: _body(_sha(b"old ics")))
    monkeypatch.setattr(verify, "fetch_ics", lambda url: ICS_LIVE)
    verify.main(
        [
            "--base-url", BASE_URL, "--repo", REPO,
            "--check", "events.json=latest:events.json",
            "--interval", "0", "--ics-url", ICS_URL,
        ]
    )
    written = output.read_text(encoding="utf-8")
    assert "stale=true" in written
    assert "drift=false" in written


@pytest.mark.parametrize(
    "fetch,url",
    [
        (lambda: verify.fetch_ics(ICS_URL), ICS_URL),
        (lambda: verify.fetch_live(f"{BASE_URL}/events.json"), f"{BASE_URL}/events.json"),
    ],
)
def test_requests_send_an_explicit_user_agent(monkeypatch, fetch, url):
    """orfe.princeton.edu answers 403 to urllib's default Python-urllib UA."""
    seen = {}

    class _Response:
        def read(self):
            return b"payload"

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def fake_urlopen(request, *args, **kwargs):
        seen["ua"] = request.get_header("User-agent")
        return _Response()

    monkeypatch.setattr(verify, "urlopen", fake_urlopen)
    fetch()
    assert seen["ua"] == verify.USER_AGENT
    assert "Python-urllib" not in seen["ua"]
