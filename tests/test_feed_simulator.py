"""Bridge that runs the browser simulator's node test suite from pytest.

The simulator reimplements the coverage-window arithmetic in JavaScript so the
Pages site can show editors what WordPress will ingest. That reimplementation can
drift from src/newsletter.py, so this file does two things: it runs the JS suite,
and it asserts the two implementations agree on the same cases.
"""
import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from src.newsletter import build_edition, is_in_coverage, load_newsletter_config

REPO_ROOT = Path(__file__).resolve().parents[1]
JS_TESTS = REPO_ROOT / "tests" / "js"
SIMULATOR = REPO_ROOT / "site" / "feed-simulator.js"
TEST_CONFIG = REPO_ROOT / "tests" / "fixtures" / "newsletter_config.test.json"

node = shutil.which("node")
requires_node = pytest.mark.skipif(node is None, reason="node is not installed")


def run_node(script: str) -> str:
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert result.returncode == 0, f"node failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout.strip()


def test_simulator_asset_exists():
    """Guards the skips below from hiding a deleted file."""
    assert SIMULATOR.is_file()
    assert JS_TESTS.is_dir() and list(JS_TESTS.glob("*.test.js"))


@requires_node
def test_javascript_suite_passes():
    result = subprocess.run(
        [node, "--test", str(JS_TESTS)], capture_output=True, text=True,
        cwd=REPO_ROOT, timeout=120,
    )
    assert result.returncode == 0, f"node --test failed:\n{result.stdout}\n{result.stderr}"
    # Assert it actually ran cases, so an empty suite cannot pass as success.
    assert "# pass 2" in result.stdout or "pass 2" in result.stdout, result.stdout


def _js_edition(publication_date: str) -> dict:
    payload = run_node(
        "const S=require('./site/feed-simulator.js');"
        f"console.log(JSON.stringify(S.resolveEdition({{publicationDate:'{publication_date}'}})));"
    )
    return json.loads(payload)


def _assert_agrees(edition, js: dict) -> None:
    # Python renders aware datetimes; compare the naive local wall clock the feed uses.
    assert js["id"] == edition.id
    assert js["coverageStart"] == edition.coverage_start.strftime("%Y-%m-%dT%H:%M:%S")
    assert js["coverageEnd"] == edition.coverage_end.strftime("%Y-%m-%dT%H:%M:%S")


@requires_node
@pytest.mark.parametrize(
    "week_start",
    [
        date(2026, 9, 21),   # a plain week
        date(2027, 3, 8),    # spring-forward week
        date(2026, 10, 26),  # fall-back week
        date(2026, 12, 28),  # year boundary
    ],
)
def test_javascript_and_python_agree_on_the_window(week_start):
    """The simulator must show exactly the window the pipeline filters on."""
    cfg = load_newsletter_config(str(TEST_CONFIG), env={})
    edition = build_edition(cfg, week_start)
    # Feed the simulator the date Python actually resolved, not an assumed weekday.
    publication_date = edition.publication_at.strftime("%Y-%m-%d")
    _assert_agrees(edition, _js_edition(publication_date))


@requires_node
def test_javascript_and_python_agree_on_a_shifted_publication(tmp_path):
    """Labor Day week: the id and the Sunday end must match on both sides."""
    config = tmp_path / "cfg.json"
    config.write_text(json.dumps({
        "exceptions": [
            {"week_of": "2026-09-07", "publication_date": "2026-09-08", "reason": "Labor Day"}
        ]
    }), encoding="utf-8")
    cfg = load_newsletter_config(str(config), env={})
    edition = build_edition(cfg, date(2026, 9, 7))
    assert edition.publication_at.strftime("%Y-%m-%d") == "2026-09-08", "fixture drifted"

    js = _js_edition("2026-09-08")
    _assert_agrees(edition, js)
    assert js["id"] == "2026-09-07", "the id follows the week anchor, not the publication date"
    assert js["shifted"] is True


@requires_node
def test_javascript_and_python_select_the_same_events():
    cfg = load_newsletter_config(str(TEST_CONFIG), env={})
    edition = build_edition(cfg, date(2026, 9, 21))
    events = [
        {"guid": "before", "startTime": "2026-09-20T23:59:59"},
        {"guid": "open-edge", "startTime": "2026-09-21T00:00:00"},
        {"guid": "middle", "startTime": "2026-09-24T16:15:00"},
        {"guid": "close-edge", "startTime": "2026-09-27T23:59:59"},
        {"guid": "after", "startTime": "2026-09-28T00:00:00"},
    ]
    expected = [e["guid"] for e in events if is_in_coverage(edition, e)]
    assert expected == ["open-edge", "middle", "close-edge"], "python side changed"

    payload = run_node(
        "const S=require('./site/feed-simulator.js');"
        "const e=S.resolveEdition({publicationDate:'2026-09-21'});"
        f"const events={json.dumps(events)};"
        "console.log(JSON.stringify(S.partition(events,e).included.map(i=>i.guid)));"
    )
    assert json.loads(payload) == expected


@requires_node
def test_simulator_is_immune_to_the_viewers_timezone():
    """An editor in California must see what WordPress sees."""
    script = (
        "const S=require('./site/feed-simulator.js');"
        "const e=S.resolveEdition({publicationDate:'2026-09-21'});"
        "console.log(JSON.stringify([e.coverageStart,e.coverageEnd,"
        "S.inWindow('2026-09-21T00:15:00',e),S.inWindow('2026-09-27T23:45:00',e)]));"
    )
    seen = set()
    for tz in ["America/New_York", "America/Los_Angeles", "Asia/Tokyo", "UTC"]:
        result = subprocess.run(
            [node, "-e", script], capture_output=True, text=True,
            cwd=REPO_ROOT, timeout=60, env={"TZ": tz, "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
        assert result.returncode == 0, result.stderr
        seen.add(result.stdout.strip())
    assert len(seen) == 1, f"results differ by viewer timezone: {seen}"
