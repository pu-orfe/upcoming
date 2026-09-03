"""Report events still carrying a synthesized title as a newsletter deadline nears.

The pipeline guarantees a non-empty ``title``, so an event whose speaker has not
supplied one ships as something like "An ORFE Departmental Colloquia Talk". Those
listings are unpublishable, and an editor discovers them only by reading the feed.
This module finds them for the edition whose submission deadline comes next and
keeps a single GitHub issue per edition up to date.

Idempotency lives in the issue body, not in any local state: the first line
carries a marker naming the edition and the reminder milestones already
announced. Repeated cron runs rewrite the body (body edits do not notify) and
comment only when a new milestone is crossed (comments do notify).

Deliberately stdlib-only, matching src/verify_published_feed.py and
src/mirror_release.py, so the workflow needs no ``pip install``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .newsletter import (
    Edition,
    InvalidEventTime,
    NewsletterConfigError,
    NoEditionFound,
    filter_events_for_edition,
    load_newsletter_config,
    next_deadline_edition,
    parse_as_of,
    upcoming_edition,
)

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
REQUEST_TIMEOUT_SECONDS = 30

# orfe.princeton.edu and api.github.com both answer 403 to urllib's default
# ``Python-urllib/x.y``, so every request identifies itself explicitly.
USER_AGENT = "upcoming-newsletter-watch/1.0 (+https://github.com/pu-orfe/upcoming)"

DEFAULT_LABEL = "newsletter-titles"
DEFAULT_LABEL_COLOR = "fbca04"
DEFAULT_LABEL_DESCRIPTION = "Newsletter events still awaiting a real title"
DEFAULT_SOURCE = "release:latest:events.json"

EXIT_OK = 0
EXIT_DEADLINE_MISSED = 1
EXIT_ERROR = 2
EXIT_EMPTY_WINDOW = 3

PAST_DEADLINE_MILESTONE = "past-deadline"

MARKER_RE = re.compile(
    r"<!--\s*newsletter-missing-titles\s+edition=(?P<edition>[0-9-]+)"
    r"(?:\s+announced=(?P<announced>[0-9a-z,.\-]*))?\s*-->"
)

#: The pipeline stamps the edition it built for into the release body.
EDITION_MARKER_RE = re.compile(r"NEWSLETTER_EDITION:([0-9-]+)")


class NotifyError(RuntimeError):
    """Something went wrong that must not be reported as success."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

def _unescape(value: object) -> str:
    """Undo the ICS-style backslash escaping the transform applies."""
    return str(value or "").replace("\\,", ",").replace("\\;", ";")


@dataclass(frozen=True)
class MissingTitle:
    guid: str
    start_time: str
    title: str
    title_source: str
    series: str
    speaker: str
    url_ref: str

    @classmethod
    def from_event(cls, event: dict) -> "MissingTitle":
        return cls(
            guid=str(event.get("guid") or ""),
            start_time=str(event.get("startTime") or ""),
            # The feed escapes commas and semicolons for ICS round-tripping;
            # undo that so the digest reads as prose.
            title=_unescape(event.get("title")),
            title_source=str(event.get("titleSource") or "unknown"),
            series=str(event.get("series") or ""),
            speaker=_unescape(event.get("speaker")),
            url_ref=str(event.get("urlRef") or ""),
        )


@dataclass(frozen=True)
class Digest:
    edition: Edition
    total_in_window: int
    missing: tuple[MissingTitle, ...]
    hours_to_deadline: float
    past_deadline: bool
    due_milestone: str | None
    variant_edition: str | None = None

    @property
    def edition_id(self) -> str:
        return self.edition.id

    @property
    def variant_in_sync(self) -> bool | None:
        if self.variant_edition is None:
            return None
        return self.variant_edition == self.edition.id


@dataclass(frozen=True)
class Issue:
    number: int
    state: str
    title: str
    body: str
    announced: frozenset[str]


@dataclass(frozen=True)
class Action:
    kind: str          # created | updated | commented | reopened | closed | noop
    issue_number: int | None = None
    reason: str = ""
    milestone: str | None = None


# ---------------------------------------------------------------------------
# GitHub client
# ---------------------------------------------------------------------------

Transport = Callable[[Request], tuple[int, bytes]]


def _default_transport(request: Request) -> tuple[int, bytes]:
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.status, response.read()


def resolve_token() -> str | None:
    return os.getenv("GITHUB_TOKEN") or os.getenv("NOTIFY_GITHUB_TOKEN")


class GitHubClient:
    """Minimal GitHub REST client. `transport` is injectable so tests stay offline."""

    def __init__(
        self,
        repo: str,
        token: str | None,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.repo = repo
        self.token = token
        self._transport = transport or _default_transport
        self.calls: list[tuple[str, str]] = []

    def call(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        accept: str = "application/vnd.github+json",
        absolute: bool = False,
        tolerate: Sequence[int] = (),
    ) -> Any:
        url = path if absolute else f"{API_ROOT}{path}"
        headers = {
            "Accept": accept,
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": API_VERSION,
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(url, data=data, headers=headers, method=method)
        self.calls.append((method, url))
        try:
            status, body = self._transport(request)
        except HTTPError as exc:
            if exc.code in tolerate:
                return None
            detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
            raise NotifyError(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise NotifyError(f"{method} {url} failed: {exc.reason}") from exc
        if status in tolerate:
            return None
        if not body:
            return {}
        if accept == "application/octet-stream":
            return body
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise NotifyError(f"{method} {url} returned invalid JSON: {exc}") from exc

    # -- issues ------------------------------------------------------------

    def list_issues(self, label: str) -> list[dict]:
        """All issues carrying `label`, open or closed.

        Uses the list endpoint rather than issue search on purpose: GitHub's search
        index is eventually consistent, so a search-based lookup can miss an issue
        created moments earlier and produce a duplicate.
        """
        query = urlencode({"labels": label, "state": "all", "per_page": "100"})
        result = self.call("GET", f"/repos/{self.repo}/issues?{query}")
        return list(result or [])

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        return self.call(
            "POST", f"/repos/{self.repo}/issues",
            {"title": title, "body": body, "labels": labels},
        )

    def update_issue(self, number: int, **fields: Any) -> dict:
        return self.call("PATCH", f"/repos/{self.repo}/issues/{number}", dict(fields))

    def comment(self, number: int, body: str) -> dict:
        return self.call(
            "POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body}
        )

    def ensure_label(self, name: str, color: str, description: str) -> None:
        self.call(
            "POST", f"/repos/{self.repo}/labels",
            {"name": name, "color": color, "description": description},
            tolerate=(422,),
        )

    # -- releases ----------------------------------------------------------

    def release(self, tag: str) -> dict:
        return self.call("GET", f"/repos/{self.repo}/releases/tags/{quote(tag)}")

    def release_asset(self, tag: str, asset_name: str) -> tuple[bytes, str]:
        """Return the asset bytes and the release body."""
        release = self.release(tag)
        for asset in release.get("assets", []):
            if asset.get("name") == asset_name:
                content = self.call(
                    "GET", asset["url"], accept="application/octet-stream", absolute=True
                )
                return content, str(release.get("body") or "")
        raise NotifyError(f"release {self.repo}@{tag} has no asset named {asset_name}")


# ---------------------------------------------------------------------------
# Loading events
# ---------------------------------------------------------------------------

def load_events(source: str, *, client: GitHubClient | None = None) -> tuple[list[dict], str | None]:
    """Load the feed. Returns (events, release_body_edition_marker_or_None).

    ``source`` is one of ``release:<tag>:<asset>``, ``url:<https://...>`` or
    ``file:<path>``.
    """
    kind, _, rest = source.partition(":")
    if kind == "file":
        try:
            raw = Path(rest).read_text(encoding="utf-8")
        except OSError as exc:
            raise NotifyError(f"could not read {rest}: {exc}") from exc
        return _parse_events(raw, rest), None
    if kind == "url":
        request = Request(rest, headers={"User-Agent": USER_AGENT}, method="GET")
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
        except (HTTPError, URLError) as exc:
            raise NotifyError(f"could not fetch {rest}: {exc}") from exc
        return _parse_events(raw, rest), None
    if kind == "release":
        if client is None:
            raise NotifyError("a GitHub client is required for a release: source")
        tag, _, asset = rest.partition(":")
        if not tag or not asset:
            raise NotifyError(f"malformed source {source!r} (expected release:<tag>:<asset>)")
        content, body = client.release_asset(tag, asset)
        marker = EDITION_MARKER_RE.search(body or "")
        return _parse_events(content.decode("utf-8"), source), (marker.group(1) if marker else None)
    raise NotifyError(f"unknown source {source!r} (expected release:, url: or file:)")


def _parse_events(raw: str, where: str) -> list[dict]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NotifyError(f"{where}: invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise NotifyError(f"{where}: expected a JSON array of events")
    return data


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

def _format_lead(lead: float) -> str:
    return str(int(lead)) if float(lead).is_integer() else str(lead)


def collect_missing(
    events: list[dict],
    edition: Edition,
    *,
    now: datetime,
    lead_hours: Sequence[float],
    variant_edition: str | None = None,
) -> Digest:
    """Find events in the coverage window whose title is still a placeholder."""
    in_window = filter_events_for_edition(edition, events)
    missing = tuple(
        MissingTitle.from_event(ev) for ev in in_window if ev.get("titleIsPlaceholder")
    )
    remaining = (
        edition.deadline_at.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    ).total_seconds() / 3600.0
    past = remaining <= 0

    milestone: str | None = None
    if past:
        milestone = PAST_DEADLINE_MILESTONE
    else:
        # The tightest lead already crossed is the one worth announcing.
        crossed = [lead for lead in lead_hours if remaining <= lead]
        if crossed:
            milestone = _format_lead(min(crossed))

    return Digest(
        edition=edition,
        total_in_window=len(in_window),
        missing=missing,
        hours_to_deadline=remaining,
        past_deadline=past,
        due_milestone=milestone,
        variant_edition=variant_edition,
    )


def _digest_for(cfg, events, edition, *, now, variant_edition):
    return collect_missing(
        events, edition, now=now,
        lead_hours=cfg.rule_for(edition.week_start).reminder_lead_hours,
        variant_edition=variant_edition,
    )


def select_digest(cfg, events, *, now, target="auto", variant_edition=None) -> Digest:
    """Choose which edition to report on.

    An edition's deadline falls six days before its own publication, so at any
    moment two editions matter: the one about to publish (deadline already past --
    anything still missing needs a late addition emailed to the editor) and the one
    contributors are currently submitting for. 'auto' escalates to the former when
    it still has placeholders, because it publishes soonest.
    """
    if target == "next-deadline":
        return _digest_for(
            cfg, events, next_deadline_edition(cfg, now),
            now=now, variant_edition=variant_edition,
        )
    if target == "upcoming":
        return _digest_for(
            cfg, events, upcoming_edition(cfg, now),
            now=now, variant_edition=variant_edition,
        )

    publishing = _digest_for(
        cfg, events, upcoming_edition(cfg, now), now=now, variant_edition=variant_edition
    )
    if publishing.missing and publishing.past_deadline:
        return publishing
    return _digest_for(
        cfg, events, next_deadline_edition(cfg, now), now=now, variant_edition=variant_edition
    )


# ---------------------------------------------------------------------------
# Issue rendering and dedupe
# ---------------------------------------------------------------------------

def marker(edition_id: str, announced: Iterable[str]) -> str:
    ordered = ",".join(sorted(set(announced), key=_milestone_sort_key))
    suffix = f" announced={ordered}" if ordered else ""
    return f"<!-- newsletter-missing-titles edition={edition_id}{suffix} -->"


def _milestone_sort_key(value: str) -> tuple[int, float, str]:
    if value == PAST_DEADLINE_MILESTONE:
        return (1, 0.0, value)
    try:
        return (0, -float(value), value)
    except ValueError:
        return (2, 0.0, value)


def parse_marker(body: str) -> tuple[str, frozenset[str]] | None:
    match = MARKER_RE.search(body or "")
    if not match:
        return None
    raw = match.group("announced") or ""
    announced = frozenset(part for part in raw.split(",") if part)
    return match.group("edition"), announced


def issue_title(edition_id: str) -> str:
    return f"Newsletter {edition_id}: events still awaiting a title"


def _local(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M %Z")


def render_body(digest: Digest, announced: Iterable[str], *, run_url: str) -> str:
    e = digest.edition
    lines = [marker(digest.edition_id, announced), ""]
    lines.append(
        f"The submission deadline for the **{_local(e.publication_at)}** edition is "
        f"**{_local(e.deadline_at)}**."
    )
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Edition | `{digest.edition_id}` |")
    lines.append(f"| Publishes | {_local(e.publication_at)} |")
    deadline_note = (
        f"{abs(digest.hours_to_deadline):.0f}h ago"
        if digest.past_deadline
        else f"in {digest.hours_to_deadline:.0f}h"
    )
    lines.append(f"| Deadline | {_local(e.deadline_at)} ({deadline_note}) |")
    lines.append(
        f"| Coverage window | {_local(e.coverage_start)} .. {_local(e.coverage_end)} |"
    )
    lines.append(f"| Events in window | {digest.total_in_window} |")
    lines.append(f"| Awaiting a title | {len(digest.missing)} |")
    if e.exception_reason:
        lines.append(f"| Schedule exception | {e.exception_reason} |")
    if digest.variant_edition is not None:
        sync = "in sync" if digest.variant_in_sync else "**out of sync**"
        lines.append(f"| Published variant edition | `{digest.variant_edition}` ({sync}) |")
    lines.append("")

    if digest.missing:
        lines.append("### Still awaiting a title")
        lines.append("")
        lines.append("| Start | Series | Speaker | Current placeholder | Source |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in sorted(digest.missing, key=lambda m: m.start_time):
            link = f"[{item.title}]({item.url_ref})" if item.url_ref else item.title
            lines.append(
                f"| {item.start_time} | {item.series} | {item.speaker} | {link} | "
                f"`{item.title_source}` |"
            )
    else:
        lines.append(
            f"All {digest.total_in_window} events in this window now have a real title."
        )
    lines.append("")
    lines.append(f"_Updated by [the newsletter deadline watch]({run_url})._")
    return "\n".join(lines)


def render_comment(digest: Digest, milestone: str, *, run_url: str) -> str:
    count = len(digest.missing)
    noun = "event" if count == 1 else "events"
    if milestone == PAST_DEADLINE_MILESTONE:
        head = (
            f"**The deadline has passed** and {count} {noun} in the "
            f"`{digest.edition_id}` window still have no real title."
        )
    else:
        head = (
            f"**T-{milestone}h to the submission deadline** and {count} {noun} in the "
            f"`{digest.edition_id}` window still have no real title."
        )
    lines = [head, ""]
    for item in sorted(digest.missing, key=lambda m: m.start_time):
        speaker = f" — {item.speaker}" if item.speaker else ""
        lines.append(f"- `{item.start_time}` {item.series}{speaker}")
    lines.append("")
    lines.append(f"_See the issue body for the full table. [Run log]({run_url})._")
    return "\n".join(lines)


def find_issue(client: GitHubClient, *, label: str, edition_id: str) -> Issue | None:
    """Locate this edition's issue by its body marker.

    Matching on the marker rather than the title means a human can rename the
    issue without breaking idempotency.
    """
    for raw in client.list_issues(label):
        if "pull_request" in raw:
            continue
        parsed = parse_marker(str(raw.get("body") or ""))
        if parsed and parsed[0] == edition_id:
            return Issue(
                number=int(raw["number"]),
                state=str(raw.get("state") or "open"),
                title=str(raw.get("title") or ""),
                body=str(raw.get("body") or ""),
                announced=parsed[1],
            )
    return None


def reconcile(
    client: GitHubClient,
    digest: Digest,
    *,
    label: str = DEFAULT_LABEL,
    run_url: str = "",
    dry_run: bool = False,
) -> Action:
    """Bring this edition's issue in line with the digest."""
    try:
        existing = find_issue(client, label=label, edition_id=digest.edition_id)
    except NotifyError as exc:
        if not dry_run:
            raise
        # A dry run is a local inspection tool and the digest is the valuable part,
        # so an unreachable issue list degrades rather than aborting -- but it says
        # so, in the summary and on stderr, instead of reading as "nothing to do".
        print(f"::warning::could not read existing issues: {exc}", file=sys.stderr)
        return Action("unknown", None, f"issue state unavailable: {exc}")

    if not digest.missing:
        if existing is None or existing.state == "closed":
            return Action("noop", existing.number if existing else None, "nothing outstanding")
        body = render_body(digest, existing.announced, run_url=run_url)
        if dry_run:
            return Action("closed", existing.number, "dry-run", None)
        client.comment(
            existing.number,
            f"All {digest.total_in_window} events in the `{digest.edition_id}` window now "
            f"have a real title. Closing.",
        )
        client.update_issue(
            existing.number, body=body, state="closed", state_reason="completed"
        )
        return Action("closed", existing.number, "all titles supplied")

    milestone = digest.due_milestone
    announced = set(existing.announced) if existing else set()
    newly_due = milestone if (milestone and milestone not in announced) else None
    if newly_due:
        announced.add(newly_due)

    if existing is None:
        body = render_body(digest, announced, run_url=run_url)
        if dry_run:
            return Action("created", None, "dry-run", newly_due)
        client.ensure_label(label, DEFAULT_LABEL_COLOR, DEFAULT_LABEL_DESCRIPTION)
        created = client.create_issue(issue_title(digest.edition_id), body, [label])
        return Action("created", int(created.get("number", 0)), "issue opened", newly_due)

    body = render_body(digest, announced, run_url=run_url)
    if dry_run:
        kind = "reopened" if existing.state == "closed" else ("commented" if newly_due else "updated")
        return Action(kind, existing.number, "dry-run", newly_due)

    if existing.state == "closed":
        client.update_issue(existing.number, body=body, state="open")
        client.comment(existing.number, render_comment(digest, milestone or "", run_url=run_url))
        return Action("reopened", existing.number, "placeholders reappeared", newly_due)

    # A body edit does not notify anyone, so it is safe on every run.
    client.update_issue(existing.number, body=body)
    if newly_due:
        client.comment(existing.number, render_comment(digest, newly_due, run_url=run_url))
        return Action("commented", existing.number, f"milestone {newly_due}", newly_due)
    return Action("updated", existing.number, "body refreshed", None)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def render_step_summary(digest: Digest | None, action: Action | None, *, error: str = "") -> str:
    if digest is None:
        return "\n".join([
            "### Newsletter deadline watch",
            "",
            f"**Failed:** {error or 'unknown error'}",
            "",
        ])
    e = digest.edition
    deadline_note = (
        f"{abs(digest.hours_to_deadline):.0f}h ago"
        if digest.past_deadline
        else f"in {digest.hours_to_deadline:.0f}h"
    )
    lines = [
        f"### Newsletter deadline watch — edition {digest.edition_id}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Publishes | {_local(e.publication_at)} |",
        f"| Deadline | {_local(e.deadline_at)} ({deadline_note}) |",
        f"| Coverage window | {_local(e.coverage_start)} .. {_local(e.coverage_end)} |",
        f"| Events in window | {digest.total_in_window} |",
        f"| Missing real titles | {len(digest.missing)} of {digest.total_in_window} in window |",
        f"| Milestone | {digest.due_milestone or 'none due'} |",
    ]
    if action is not None:
        number = f"#{action.issue_number}" if action.issue_number else "—"
        lines.append(f"| Issue | {number} ({action.kind}: {action.reason}) |")
    if error:
        # The digest still computed, but something downstream did not. Say so here
        # as well as on stderr, so the summary is never a clean bill of health.
        lines.append(f"| Failed | {error} |")
    if digest.variant_edition is not None:
        sync = "in sync" if digest.variant_in_sync else "**out of sync**"
        lines.append(f"| Published variant edition | `{digest.variant_edition}` ({sync}) |")
    lines.append("")
    if digest.missing:
        lines.append("| Start | Series | Speaker | Placeholder title | titleSource |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in sorted(digest.missing, key=lambda m: m.start_time):
            lines.append(
                f"| {item.start_time} | {item.series} | {item.speaker} | {item.title} | "
                f"`{item.title_source}` |"
            )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--source", default=DEFAULT_SOURCE,
                   help="release:<tag>:<asset>, url:<https://...> or file:<path>")
    p.add_argument("--newsletter-config", default=None, help="Path to newsletter_config.json")
    p.add_argument("--label", default=DEFAULT_LABEL, help="Label carried by the tracking issue")
    p.add_argument(
        "--target", choices=["auto", "next-deadline", "upcoming"], default="auto",
        help=(
            "Which edition to report on. 'next-deadline' is the one contributors are "
            "currently submitting for; 'upcoming' is the one about to publish, whose "
            "deadline has already passed so anything still missing needs a late "
            "addition. 'auto' (default) escalates to 'upcoming' when that edition "
            "still has placeholders, and otherwise chases the next deadline."
        ),
    )
    p.add_argument("--as-of", default=None, help="ISO-8601 instant (testing only)")
    p.add_argument("--run-url", default="", help="Link back to the workflow run")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute and summarize without creating or editing issues")
    p.add_argument("--allow-empty-window", action="store_true",
                   help="Do not fail when the coverage window contains no events")
    return p.parse_args(argv)


def main(argv: list[str] | None = None, *, transport: Transport | None = None) -> int:
    ns = _parse_args(argv if argv is not None else sys.argv[1:])
    digest: Digest | None = None
    action: Action | None = None
    error = ""
    code = EXIT_OK

    try:
        cfg = load_newsletter_config(ns.newsletter_config)
        now = parse_as_of(ns.as_of, cfg)

        token = resolve_token()
        if token is None and not ns.dry_run:
            raise NotifyError(
                "no GITHUB_TOKEN in the environment; refusing to run as a silent no-op "
                "(pass --dry-run to compute without writing)"
            )
        client = GitHubClient(ns.repo, token, transport=transport)

        events, variant_edition = load_events(ns.source, client=client)
        digest = select_digest(
            cfg, events, now=now, target=ns.target, variant_edition=variant_edition
        )

        if digest.total_in_window == 0 and not ns.allow_empty_window:
            # A wedged pipeline, a broken window filter and a timezone misread all
            # present as "no placeholders, all good". Refuse to call that success.
            code = EXIT_EMPTY_WINDOW
            error = (
                f"no events at all fall inside the {digest.edition_id} coverage window "
                f"({digest.edition.coverage_start.isoformat()} .. "
                f"{digest.edition.coverage_end.isoformat()}); pass --allow-empty-window "
                "if this is a genuine recess"
            )
            print(error, file=sys.stderr)
        else:
            action = reconcile(
                client, digest, label=ns.label, run_url=ns.run_url, dry_run=ns.dry_run
            )
            if digest.past_deadline and digest.missing:
                code = EXIT_DEADLINE_MISSED

        if digest.variant_in_sync is False:
            print(
                f"::warning::the published variant was built for edition "
                f"{digest.variant_edition} but the next deadline belongs to "
                f"{digest.edition_id}; the variant is stale",
                file=sys.stderr,
            )
    except (NewsletterConfigError, NoEditionFound, InvalidEventTime, NotifyError) as exc:
        error = str(exc)
        code = EXIT_ERROR
        print(f"newsletter deadline watch failed: {error}", file=sys.stderr)

    # A summary is emitted on every path, including failures.
    print(render_step_summary(digest, action, error=error))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
