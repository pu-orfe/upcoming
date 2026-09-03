"""Contract tests over the CI wiring.

These catch the class of bug unit tests structurally cannot: a workflow that
publishes a file nobody verifies, a caller that forgets a required action input,
or a cron that silently desyncs from newsletter_config.json.
"""
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
PAGES_ACTION = REPO_ROOT / "actions" / "prepare-pages-artifact" / "action.yml"
WATCH_WORKFLOW = WORKFLOW_DIR / "newsletter_deadline_watch.yml"
VERIFY_WORKFLOW = WORKFLOW_DIR / "verify_published_feed.yml"
ICS_WORKFLOW = WORKFLOW_DIR / "ics_to_json.yml"

NEWSLETTER_ASSET = "events-newsletter.json"


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def workflow_paths():
    return sorted(WORKFLOW_DIR.glob("*.yml"))


def iter_steps(workflow):
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            yield step


def pages_action_callers():
    """Every (workflow_path, step) that invokes the Pages composite action."""
    found = []
    for path in workflow_paths():
        for step in iter_steps(load_yaml(path)):
            if "prepare-pages-artifact" in str(step.get("uses") or ""):
                found.append((path, step))
    return found


# --------------------------------------------------------------------------
# All workflows parse
# --------------------------------------------------------------------------

def test_every_workflow_is_valid_yaml():
    paths = workflow_paths()
    assert len(paths) >= 6, "expected the full set of workflows"
    for path in paths:
        assert load_yaml(path) is not None, f"{path.name} parsed as empty"


def test_pages_action_is_valid_yaml():
    assert load_yaml(PAGES_ACTION)["inputs"]


# --------------------------------------------------------------------------
# The composite action and its callers
# --------------------------------------------------------------------------

def test_action_declares_newsletter_inputs():
    inputs = load_yaml(PAGES_ACTION)["inputs"]
    assert "prod-events-newsletter-file" in inputs
    assert "dev-events-newsletter-file" in inputs


def test_at_least_one_caller_uses_each_local_source():
    """Keeps the two tests below from passing vacuously."""
    sources = [
        (step.get("with") or {}).get("prod-events-source")
        for _, step in pages_action_callers()
    ]
    assert "local" in sources
    dev_sources = [
        (step.get("with") or {}).get("dev-events-source")
        for _, step in pages_action_callers()
    ]
    assert "local" in dev_sources


def test_every_caller_using_local_prod_source_passes_newsletter_file():
    for path, step in pages_action_callers():
        with_ = step.get("with") or {}
        if with_.get("prod-events-source") == "local":
            assert with_.get("prod-events-newsletter-file"), (
                f"{path.name} publishes a local prod feed without the newsletter file; "
                "the Pages tree would be missing /events-newsletter.json"
            )


def test_every_caller_using_local_dev_source_passes_newsletter_file():
    for path, step in pages_action_callers():
        with_ = step.get("with") or {}
        if with_.get("dev-events-source") == "local":
            assert with_.get("dev-events-newsletter-file"), (
                f"{path.name} publishes a local dev feed without the newsletter file"
            )


def test_release_branch_downloads_newsletter_asset():
    """The Pages tree is rebuilt from scratch on every deploy.

    publish_landing_pages.yml assembles it from release assets alone, so without
    this download a landing-page-only run would delete /events-newsletter.json
    from the live site and the verifier would then report drift.
    """
    script = PAGES_ACTION.read_text(encoding="utf-8")
    downloads = re.findall(
        r"gh release download (\w+) --pattern '([^']+)'[^\n]*--dir (\S+)", script
    )
    prod = [(tag, pattern, d) for tag, pattern, d in downloads if tag == "latest"]
    dev = [(tag, pattern, d) for tag, pattern, d in downloads if tag == "dev"]
    assert any(NEWSLETTER_ASSET in pattern for _, pattern, _ in prod), (
        "the latest-release branch never downloads the newsletter asset"
    )
    assert any(NEWSLETTER_ASSET in pattern for _, pattern, _ in dev), (
        "the dev-release branch never downloads the newsletter asset"
    )


def test_local_branches_require_the_newsletter_file():
    """A caller that forgets the input should hard-fail, not ship a partial tree."""
    script = PAGES_ACTION.read_text(encoding="utf-8")
    assert "prod-events-source=local requires prod-events-file and prod-events-newsletter-file" in script
    assert "dev-events-source=local requires dev-events-file, dev-events-nofpo-file and dev-events-newsletter-file" in script


# --------------------------------------------------------------------------
# Published paths are verified
# --------------------------------------------------------------------------

def published_pages_paths():
    """Site-relative JSON paths the composite action writes under pages/.

    Two ways a file lands there: `cp <src> pages/<path>` in the local branches, and
    `gh release download --pattern <asset> --dir pages[/dev]` in the release ones.
    """
    script = PAGES_ACTION.read_text(encoding="utf-8")
    paths = set()

    for target in re.findall(r"^\s*cp \S+ (pages/\S+\.json)\s*$", script, re.MULTILINE):
        paths.add(target[len("pages/"):])

    for line in script.splitlines():
        if "gh release download" not in line:
            continue
        directory = re.search(r"--dir (\S+)", line)
        if not directory:
            continue
        prefix = directory.group(1)[len("pages"):].strip("/")
        for pattern in re.findall(r"--pattern '([^']+)'", line):
            paths.add(f"{prefix}/{pattern}" if prefix else pattern)

    assert paths, "found no published paths; the extraction is broken, not the wiring"
    # dev/test.json is a static fixture copied from examples/, not a release asset.
    return {p for p in paths if p.endswith(".json") and not p.endswith("test.json")}


def verify_checks():
    text = VERIFY_WORKFLOW.read_text(encoding="utf-8")
    return {m.split("=")[0] for m in re.findall(r'--check "([^"]+)"', text)}


def test_verify_workflow_declares_checks():
    checks = verify_checks()
    assert len(checks) >= 5, f"expected the full check list, got {checks}"


def test_every_published_pages_path_has_a_verify_check():
    """Publishing a file nobody verifies is how drift goes unnoticed."""
    missing = published_pages_paths() - verify_checks()
    assert not missing, f"published but never verified: {sorted(missing)}"


@pytest.mark.parametrize(
    "path", ["events.json", "events-newsletter.json",
             "dev/events.json", "dev/events-nofpo.json", "dev/events-newsletter.json"],
)
def test_expected_paths_are_verified(path):
    assert path in verify_checks()


# --------------------------------------------------------------------------
# Static assets the pages reference
# --------------------------------------------------------------------------

SITE_DIR = REPO_ROOT / "site"


def referenced_assets():
    """Local script/style/anchor targets the two pages load, as site-relative paths."""
    wanted = set()
    for page, prefix in ((SITE_DIR / "index.html", ""), (SITE_DIR / "dev" / "index.html", "dev/")):
        html = page.read_text(encoding="utf-8")
        for src in re.findall(r'<script[^>]+src="([^"]+)"', html):
            if src.startswith(("http://", "https://", "//")):
                continue
            if src.startswith("../"):
                wanted.add(src[3:])
            else:
                wanted.add(prefix + src.lstrip("./"))
    return wanted


def test_pages_reference_the_simulator():
    """Keeps the deployment test below from passing because the tag was removed."""
    assert "feed-simulator.js" in referenced_assets()


def test_every_referenced_asset_is_deployed_by_the_action():
    script = PAGES_ACTION.read_text(encoding="utf-8")
    copied = set(re.findall(r"^\s*cp \S+ pages/(\S+)\s*$", script, re.MULTILINE))
    missing = referenced_assets() - copied
    assert not missing, (
        f"the pages load {sorted(missing)} but the Pages action never copies it, "
        "so the deployed site would 404 on it"
    )


def test_referenced_assets_exist_in_the_repo():
    for asset in referenced_assets():
        assert (SITE_DIR / asset).is_file(), f"site/{asset} is referenced but missing"


def test_simulator_asset_is_self_contained():
    """A strict-ish Pages deploy plus no bundler means no external imports."""
    js = (SITE_DIR / "feed-simulator.js").read_text(encoding="utf-8")
    assert "import(" not in js and "require(" not in js.replace("module.exports", "")
    assert "http://" not in js and "https://" not in js.replace("orfe.princeton.edu", "")


# --------------------------------------------------------------------------
# The ICS pipeline
# --------------------------------------------------------------------------

def test_ics_workflow_forces_rebuild_on_edition_rollover():
    """The window is time-driven; the skip gate must not be ICS-only."""
    text = ICS_WORKFLOW.read_text(encoding="utf-8")
    assert "NEWSLETTER_EDITION" in text
    assert "NEWSLETTER_CONFIG_SHA256" in text
    assert "--print-edition-id" in text


def test_ics_workflow_publishes_and_validates_the_newsletter_asset():
    text = ICS_WORKFLOW.read_text(encoding="utf-8")
    assert "events-newsletter.schema.json" in text
    assert "$NEWSLETTER_ASSET" in text


def test_no_workflow_pins_as_of_outside_manual_dispatch():
    """A pinned clock in production would freeze the edition forever."""
    for path in workflow_paths():
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "--as-of" not in line:
                continue
            assert "${AS_OF" in line or "inputs.as_of" in line, (
                f"{path.name} hard-codes --as-of: {line.strip()}"
            )
        assert "NEWSLETTER_AS_OF" not in text


# --------------------------------------------------------------------------
# The deadline watch
# --------------------------------------------------------------------------

def test_newsletter_watch_cron_is_hourly_and_offset():
    workflow = load_yaml(WATCH_WORKFLOW)
    # PyYAML parses the bare key `on` as the boolean True.
    triggers = workflow.get("on") or workflow.get(True)
    crons = [entry["cron"] for entry in triggers["schedule"]]
    assert crons == ["5 * * * *"]
    minute, hour, dom, month, dow = crons[0].split()
    # A weekday- or hour-pinned cron would duplicate newsletter_config.json in a
    # place the config cannot reach, and GitHub cron is UTC-only.
    assert hour == "*" and dow == "*"
    assert minute != "0", "offset from the pipeline and verifier to avoid contention"


def test_newsletter_watch_does_not_touch_failure_streak():
    """A red run means editors missed a deadline, not that CI broke.

    Checks the parsed steps rather than the raw text, so the explanatory comment
    in the workflow does not read as a usage.
    """
    used = [str(step.get("uses") or "") for step in iter_steps(load_yaml(WATCH_WORKFLOW))]
    assert used, "the watch workflow has no steps"
    assert not any("update-failure-streak" in u for u in used)


def test_ics_workflow_does_use_the_failure_streak_action():
    """Guards the test above from passing because the action was renamed."""
    used = [str(step.get("uses") or "") for step in iter_steps(load_yaml(ICS_WORKFLOW))]
    assert any("update-failure-streak" in u for u in used)


def test_newsletter_watch_has_issue_write_and_a_concurrency_group():
    workflow = load_yaml(WATCH_WORKFLOW)
    assert workflow["permissions"]["issues"] == "write"
    assert workflow["permissions"]["contents"] == "read"
    assert workflow["concurrency"]["group"] == "newsletter-deadline-watch"
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_newsletter_watch_defaults_dispatch_to_dry_run():
    workflow = load_yaml(WATCH_WORKFLOW)
    triggers = workflow.get("on") or workflow.get(True)
    assert triggers["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True


# --------------------------------------------------------------------------
# Pull-request test signal
# --------------------------------------------------------------------------

def test_a_workflow_runs_pytest_on_pull_requests():
    for path in workflow_paths():
        workflow = load_yaml(path)
        triggers = workflow.get("on") or workflow.get(True)
        if not isinstance(triggers, dict) or "pull_request" not in triggers:
            continue
        if "pytest" in path.read_text(encoding="utf-8"):
            return
    pytest.fail("no workflow runs pytest on pull requests")


def test_container_test_path_is_exercised_in_ci():
    text = (WORKFLOW_DIR / "tests.yml").read_text(encoding="utf-8")
    assert "docker compose" in text and "tests" in text
