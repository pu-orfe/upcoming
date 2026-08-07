"""Verify that upcoming.orfe.princeton.edu serves the assets we published.

The pipeline can publish a release and still fail to deploy Pages, leaving the
site serving an older payload while every workflow run reports success. This
module fetches what consumers actually receive and compares it byte-for-byte
against the corresponding release asset.

Pages sits behind a CDN with ``max-age=600`` and per-edge caches, and a request
lands on whichever edge answers. Neither a query string nor ``Cache-Control:
no-cache`` forces a revalidation, so the check cannot defeat the cache; it
tolerates it instead:

* a mismatch inside the grace window after publication is ``pending``, not drift
* several samples are taken, likely hitting different edges, and a single
  matching sample proves the deploy reached the origin
* only when every sample drifts is the result reported as ``drift``

Callers are expected to require consecutive drifting runs before alerting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"

DEFAULT_ATTEMPTS = 3
DEFAULT_INTERVAL_SECONDS = 30
DEFAULT_GRACE_MINUTES = 20
REQUEST_TIMEOUT_SECONDS = 30

# orfe.princeton.edu answers 403 to urllib's default ``Python-urllib/x.y``, so
# every request identifies itself explicitly.
USER_AGENT = "upcoming-feed-verifier/1.0 (+https://github.com/pu-orfe/upcoming)"

STATUS_MATCH = "match"
STATUS_PENDING = "pending"
STATUS_DRIFT = "drift"
STATUS_STALE = "stale"
STATUS_ERROR = "error"

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2
EXIT_STALE = 3

# The pipeline records the hash of the ICS it built from in the release body.
ICS_SHA256_PATTERN = re.compile(r"ICS_SHA256:([0-9a-f]+)")


class VerificationError(RuntimeError):
    """Raised when the check itself cannot be carried out."""


@dataclass(frozen=True)
class Check:
    """One site path and the release asset it is supposed to mirror."""

    site_path: str
    tag: str
    asset_name: str

    @property
    def label(self) -> str:
        return f"/{self.site_path} vs {self.tag}:{self.asset_name}"


@dataclass
class Sample:
    attempt: int
    digest: str | None
    size: int | None
    error: str | None = None


@dataclass
class Result:
    label: str
    status: str
    expected_digest: str | None = None
    expected_size: int | None = None
    samples: list[Sample] | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_MATCH, STATUS_PENDING)


def parse_check(spec: str) -> Check:
    """Parse ``site/path=tag:asset`` into a :class:`Check`."""
    site_path, separator, source = spec.partition("=")
    if not separator or not site_path or not source:
        raise VerificationError(f"Malformed --check value: {spec!r} (expected site/path=tag:asset)")
    tag, separator, asset_name = source.partition(":")
    if not separator or not tag or not asset_name:
        raise VerificationError(f"Malformed --check source: {source!r} (expected tag:asset)")
    return Check(site_path=site_path.strip("/"), tag=tag, asset_name=asset_name)


def resolve_token() -> str | None:
    return os.getenv("GITHUB_TOKEN") or os.getenv("VERIFY_GITHUB_TOKEN")


def _api_request(url: str, token: str | None, accept: str) -> bytes:
    headers = {"Accept": accept, "User-Agent": USER_AGENT, "X-GitHub-Api-Version": API_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise VerificationError(f"GET {url} failed with HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise VerificationError(f"GET {url} failed: {exc.reason}") from exc


def fetch_release_asset(repo: str, tag: str, asset_name: str, token: str | None) -> tuple[bytes, datetime]:
    """Return the asset bytes and the time it was last updated."""
    raw = _api_request(
        f"{API_ROOT}/repos/{repo}/releases/tags/{quote(tag)}",
        token,
        "application/vnd.github+json",
    )
    release: dict[str, Any] = json.loads(raw)
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name:
            content = _api_request(asset["url"], token, "application/octet-stream")
            return content, _parse_timestamp(asset.get("updated_at"))
    raise VerificationError(f"Release {repo}@{tag} has no asset named {asset_name}")


def _parse_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_release_body(repo: str, tag: str, token: str | None) -> str:
    raw = _api_request(
        f"{API_ROOT}/repos/{repo}/releases/tags/{quote(tag)}",
        token,
        "application/vnd.github+json",
    )
    release: dict[str, Any] = json.loads(raw)
    return release.get("body") or ""


def extract_ics_digest(body: str) -> str:
    match = ICS_SHA256_PATTERN.search(body)
    if not match:
        raise VerificationError("Release body has no ICS_SHA256 marker")
    return match.group(1)


def fetch_ics(url: str) -> bytes:
    """Fetch the ICS feed the same way the pipeline does, so hashes compare."""
    request = Request(url, headers={"Accept": "text/calendar", "User-Agent": USER_AGENT}, method="GET")
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except HTTPError as exc:
        raise VerificationError(f"GET {url} failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise VerificationError(f"GET {url} failed: {exc.reason}") from exc


def verify_ics_freshness(
    *,
    ics_url: str,
    repo: str,
    tag: str,
    token: str | None,
    ics_fetcher: Callable[[str], bytes] | None = None,
) -> Result:
    """Compare the live ICS feed against the hash the release was built from.

    The drift check cannot see the site and the release going stale together: if
    generation stops, both agree and the comparison passes. This closes that gap
    by asking a different question — is the release built from the ICS that is
    live right now?

    A divergence is normal for one cycle: the feed changed and the pipeline has
    not fired yet. There is no way to know when the change happened, so age of
    the release is a poor signal (a feed that changed a minute ago would look
    identical to a wedged pipeline). Confirmation is therefore left to the
    caller, which requires the divergence to survive consecutive runs; by then
    the pipeline has had a full cycle to react.
    """
    ics_fetcher = ics_fetcher or fetch_ics
    label = f"ICS feed vs {tag}:ICS_SHA256"
    try:
        expected = extract_ics_digest(fetch_release_body(repo, tag, token))
    except VerificationError as exc:
        return Result(label=label, status=STATUS_ERROR, detail=str(exc))

    try:
        live_digest = digest(ics_fetcher(ics_url))
    except VerificationError as exc:
        return Result(label=label, status=STATUS_ERROR, expected_digest=expected, detail=str(exc))

    sample = Sample(attempt=1, digest=live_digest, size=None)
    if live_digest == expected:
        return Result(
            label=label,
            status=STATUS_MATCH,
            expected_digest=expected,
            samples=[sample],
            detail="release was built from the ICS that is live now",
        )
    return Result(
        label=label,
        status=STATUS_STALE,
        expected_digest=expected,
        samples=[sample],
        detail="live ICS differs from the hash the release was built from; pipeline may not be regenerating",
    )


def fetch_live(url: str) -> bytes:
    """Fetch the canonical URL exactly as a consumer would, cache and all."""
    request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}, method="GET")
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except HTTPError as exc:
        raise VerificationError(f"GET {url} failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise VerificationError(f"GET {url} failed: {exc.reason}") from exc


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def verify_check(
    check: Check,
    *,
    base_url: str,
    repo: str,
    token: str | None,
    attempts: int = DEFAULT_ATTEMPTS,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    grace: timedelta = timedelta(minutes=DEFAULT_GRACE_MINUTES),
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    live_fetcher: Callable[[str], bytes] | None = None,
) -> Result:
    # Resolved here rather than as default arguments so the module globals stay
    # substitutable, which keeps this reachable from main() under test.
    now = now or (lambda: datetime.now(timezone.utc))
    sleep = sleep or time.sleep
    live_fetcher = live_fetcher or fetch_live
    try:
        expected, published_at = fetch_release_asset(repo, check.tag, check.asset_name, token)
    except VerificationError as exc:
        return Result(label=check.label, status=STATUS_ERROR, detail=str(exc))

    expected_digest = digest(expected)
    url = urljoin(base_url.rstrip("/") + "/", check.site_path)
    samples: list[Sample] = []

    for attempt in range(1, max(1, attempts) + 1):
        if attempt > 1:
            sleep(interval)
        try:
            live = live_fetcher(url)
        except VerificationError as exc:
            samples.append(Sample(attempt=attempt, digest=None, size=None, error=str(exc)))
            continue
        live_digest = digest(live)
        samples.append(Sample(attempt=attempt, digest=live_digest, size=len(live)))
        # One matching sample is enough: it proves the deploy reached the origin
        # and any stale edge is just serving out its TTL.
        if live_digest == expected_digest:
            return Result(
                label=check.label,
                status=STATUS_MATCH,
                expected_digest=expected_digest,
                expected_size=len(expected),
                samples=samples,
                detail=f"matched on attempt {attempt}",
            )

    if all(sample.digest is None for sample in samples):
        return Result(
            label=check.label,
            status=STATUS_ERROR,
            expected_digest=expected_digest,
            expected_size=len(expected),
            samples=samples,
            detail="every attempt failed to fetch the live URL",
        )

    age = now() - published_at
    if age < grace:
        remaining = grace - age
        return Result(
            label=check.label,
            status=STATUS_PENDING,
            expected_digest=expected_digest,
            expected_size=len(expected),
            samples=samples,
            detail=(
                f"asset published {_minutes(age)} ago; still within the "
                f"{_minutes(grace)} grace window ({_minutes(remaining)} left)"
            ),
        )

    return Result(
        label=check.label,
        status=STATUS_DRIFT,
        expected_digest=expected_digest,
        expected_size=len(expected),
        samples=samples,
        detail=f"asset published {_minutes(age)} ago and no sample matched",
    )


def _minutes(delta: timedelta) -> str:
    total = int(delta.total_seconds() // 60)
    return f"{total}m"


def format_report(results: Iterable[Result]) -> str:
    lines = ["| Subject | Status | Expected sha256 | Observed sha256 | Detail |", "| --- | --- | --- | --- | --- |"]
    for result in results:
        served = "-"
        if result.samples:
            digests = {sample.digest for sample in result.samples if sample.digest}
            served = ", ".join(sorted(d[:12] for d in digests)) if digests else "unreachable"
        expected = result.expected_digest[:12] if result.expected_digest else "-"
        lines.append(
            f"| {result.label} | {result.status} | {expected} | {served} | {result.detail} |"
        )
    return "\n".join(lines)


def write_outputs(results: list[Result]) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    def flag(status: str) -> str:
        return "true" if any(result.status == status for result in results) else "false"

    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"drift={flag(STATUS_DRIFT)}\n")
        handle.write(f"stale={flag(STATUS_STALE)}\n")
        handle.write(f"error={flag(STATUS_ERROR)}\n")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Public site root, e.g. https://upcoming.orfe.princeton.edu")
    parser.add_argument("--repo", required=True, help="owner/name holding the release assets")
    parser.add_argument(
        "--check",
        dest="checks",
        action="append",
        required=True,
        metavar="SITE_PATH=TAG:ASSET",
        help="Site path and the release asset it mirrors; repeat for multiple paths",
    )
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS, help="Samples per path")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS, help="Seconds between samples")
    parser.add_argument(
        "--grace-minutes",
        type=int,
        default=DEFAULT_GRACE_MINUTES,
        help="Treat a mismatch as pending while the asset is younger than this",
    )
    parser.add_argument(
        "--ics-url",
        help="If set, also compare this live ICS feed against the release body's ICS_SHA256",
    )
    parser.add_argument(
        "--ics-release-tag",
        default="latest",
        help="Release tag whose body carries the ICS_SHA256 marker",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    checks = [parse_check(spec) for spec in args.checks]
    token = resolve_token()

    results = [
        verify_check(
            check,
            base_url=args.base_url,
            repo=args.repo,
            token=token,
            attempts=args.attempts,
            interval=args.interval,
            grace=timedelta(minutes=args.grace_minutes),
        )
        for check in checks
    ]

    if args.ics_url:
        results.append(
            verify_ics_freshness(
                ics_url=args.ics_url,
                repo=args.repo,
                tag=args.ics_release_tag,
                token=token,
            )
        )

    print(format_report(results))
    write_outputs(results)

    # Drift is the most actionable: the deploy is broken right now. Staleness
    # means generation stopped, which is reported separately so the alert can
    # name the right cause.
    if any(result.status == STATUS_DRIFT for result in results):
        return EXIT_DRIFT
    if any(result.status == STATUS_ERROR for result in results):
        return EXIT_ERROR
    if any(result.status == STATUS_STALE for result in results):
        return EXIT_STALE
    return EXIT_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(EXIT_ERROR) from exc
