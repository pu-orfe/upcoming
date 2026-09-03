"""Core module to fetch an ICS feed, (optionally) manipulate it, and emit events.json.

Flask was removed; JSON delivery now happens by committing / publishing the generated
file (e.g. via GitHub Action artifact or GitHub Pages). A tiny helper to optionally
serve the file locally via `python -m http.server` can be used if desired.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlparse
import requests
from ics import Calendar
from .transform import transform_calendar, TransformConfig, load_config
from .enrich import (
    enrich_titles,
    enrichment_enabled,
    enrichment_overwrite_enabled,
    fill_title_fallback,
    fallback_include_speaker_enabled,
    enrich_content,
    enrichment_content_enabled,
    enrichment_content_overwrite_enabled,
    enrich_raw_details,
    enrichment_raw_details_enabled,
    enrichment_raw_details_overwrite_enabled,
    enrich_raw_extracts,
    enrichment_raw_extracts_enabled,
)
from .placeholders import ensure_title_provenance, provenance_enabled
from .newsletter import (
    Edition,
    NewsletterConfigError,
    NoEditionFound,
    InvalidEventTime,
    is_in_coverage,
    load_newsletter_config,
    parse_as_of,
    stamp_edition,
    upcoming_edition,
)

ICS_URL = os.getenv("ICS_URL", "https://example.com/calendar.ics")
REPO_VARIABLE = os.getenv("REPO_VARIABLE", "default")
OUTPUT_FILE_ENV = os.getenv("OUTPUT_FILE", "events.json")
SERIES_EXCLUDE_ENV_KEY = "EXCLUDE_SERIES"


def _split_series_value(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        values = parsed
    else:
        values = raw.split(",")
    cleaned: list[str] = []
    for item in values:
        text = str(item).strip()
        if text:
            cleaned.append(text)
    return cleaned


def _collect_series_exclusions(*sources: object) -> tuple[set[str], list[str]]:
    tokens: list[str] = []
    for source in sources:
        if source is None:
            continue
        if isinstance(source, str):
            tokens.extend(_split_series_value(source))
            continue
        if isinstance(source, Iterable):
            for item in source:
                if item is None:
                    continue
                tokens.extend(_split_series_value(str(item)))
    normalized = {token.casefold() for token in tokens if token}
    ordered_unique = sorted(dict.fromkeys(tokens), key=str.casefold)
    return normalized, ordered_unique


def _apply_series_exclusions(events: list[dict], exclusions: set[str]) -> tuple[list[dict], int]:
    if not exclusions:
        return events, 0
    filtered: list[dict] = []
    removed = 0
    for event in events:
        series_value = event.get("series")
        if isinstance(series_value, str):
            series_tokens = [s.strip() for s in series_value.split(",") if s.strip()]
        elif isinstance(series_value, Iterable):
            series_tokens = [str(s).strip() for s in series_value if str(s).strip()]
        elif series_value is None:
            series_tokens = []
        else:
            series_tokens = [str(series_value).strip()]
        if any(token.casefold() in exclusions for token in series_tokens):
            removed += 1
            continue
        filtered.append(event)
    return filtered, removed


def _apply_edition_window(events: list[dict], edition: Edition) -> tuple[list[dict], int]:
    """Keep only events inside the edition's coverage window.

    Mirrors _apply_series_exclusions: returns (kept, removed) and never mutates
    either the input list or the event dicts it contains.
    """
    kept: list[dict] = []
    removed = 0
    for event in events:
        if is_in_coverage(edition, event):
            kept.append(event)
        else:
            removed += 1
    return kept, removed


def _resolve_series_exclusions(
    cli_values: Iterable[str] | None = None,
    extra_values: Iterable[str] | None = None,
) -> tuple[set[str], list[str]]:
    env_value = os.getenv(SERIES_EXCLUDE_ENV_KEY)
    sources: tuple[object, ...]
    packed: list[object] = [env_value]
    if cli_values:
        packed.append(cli_values)
    if extra_values:
        packed.append(extra_values)
    sources = tuple(packed)
    return _collect_series_exclusions(*sources)


def fetch_ics(url: str) -> str:
    """Retrieve raw ICS text from URL or local file.

    Supports:
    - http(s) URLs via requests
    - file:// URLs by reading from the filesystem
    - bare local paths (absolute or relative)
    """
    # file:// scheme
    if url.startswith("file://"):
        parsed = urlparse(url)
        path = parsed.path
        return Path(path).read_text(encoding="utf-8")
    # bare local path
    if "://" not in url and Path(url).exists():
        return Path(url).read_text(encoding="utf-8")
    # http(s) fallback
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def manipulate_data(calendar: Calendar, variable: str) -> Calendar:
    """Placeholder for domain-specific event manipulation.

    Currently passes calendar through unchanged.
    """
    # TODO: Implement data manipulation logic
    _ = variable  # keep reference to show intended use
    return calendar


def calendar_to_json(calendar: Calendar) -> list[dict]:  # legacy fallback
    return transform_calendar(calendar)


def generate_events_json(
    ics_url: str = ICS_URL,
    repo_variable: str = REPO_VARIABLE,
    output_path: str | os.PathLike = "events.json",
    exclude_series: Iterable[str] | None = None,
) -> Path:
    """Fetch, manipulate and write events JSON.

    Returns the Path to the written file.
    """
    raw = fetch_ics(ics_url)
    calendar = Calendar(raw)
    manipulated = manipulate_data(calendar, repo_variable)
    # Apply transformation config (future: load custom config)
    cfg = TransformConfig()
    data = transform_calendar(manipulated, cfg)
    exclusions, _ = _resolve_series_exclusions(extra_values=exclude_series)
    if exclusions:
        data, _ = _apply_series_exclusions(data, exclusions)
    # The schema requires a non-empty title (minLength: 1) but transform seeds it
    # empty from TransformConfig.placeholders, so the fallback must run here too --
    # not only in main(), which is the path the CI workflow happens to use.
    fill_title_fallback(
        data, overwrite=False, include_speaker=fallback_include_speaker_enabled()
    )
    if provenance_enabled():
        ensure_title_provenance(data)
    out_path = Path(output_path)
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return out_path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate events.json from an ICS feed")
    p.add_argument("--ics-url", default=ICS_URL, help="ICS feed URL (env ICS_URL overrides)")
    p.add_argument(
        "--repo-variable",
        default=REPO_VARIABLE,
        help="Arbitrary repo variable used during manipulation",
    )
    p.add_argument(
        "--output",
        default=OUTPUT_FILE_ENV,
        help="Output JSON filepath (default comes from env OUTPUT_FILE or 'events.json')",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Optional path to JSON transform config (default: transform_config.json if present)",
    )
    p.add_argument(
        "--print-only",
        action="store_true",
        help="Print transformed JSON to stdout instead of writing file",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of events in output (for local iteration)",
    )
    p.add_argument(
        "--enrich-titles",
        action="store_true",
        help="Fetch each event page and populate the 'title' field from .event-subtitle (network heavy)",
    )
    p.add_argument(
        "--enrich-overwrite",
        action="store_true",
        help="When enriching titles, overwrite existing non-empty titles instead of only filling blanks",
    )
    p.add_argument(
        "--enrich-content",
        action="store_true",
        help="Fetch each event page and populate the 'content' field from main body (network heavy)",
    )
    p.add_argument(
        "--enrich-content-overwrite",
        action="store_true",
        help="When enriching content, overwrite existing non-empty content instead of only filling blanks",
    )
    p.add_argument(
        "--enrich-raw-details",
        action="store_true",
        help="Fetch each event page and populate 'rawEventDetails' with inner HTML of .events-detail-main",
    )
    p.add_argument(
        "--enrich-raw-details-overwrite",
        action="store_true",
        help="When enriching raw details, overwrite existing non-empty values instead of only filling blanks",
    )
    p.add_argument(
        "--enrich-raw-extracts",
        action="store_true",
        help="Extract abstract and bio from rawEventDetails into separate fields (requires raw details enrichment)",
    )
    p.add_argument(
        "--no-fallback-speaker",
        action="store_true",
        help="Don't include speaker name in fallback titles; use only FALLBACK_PREPEND_TEXT template (env FALLBACK_INCLUDE_SPEAKER=0)",
    )
    p.add_argument(
        "--newsletter-output",
        default=os.getenv("NEWSLETTER_OUTPUT_FILE") or None,
        help=(
            "Also write a newsletter variant holding only the events inside the next "
            "edition's coverage window (env NEWSLETTER_OUTPUT_FILE). Never affects --output."
        ),
    )
    p.add_argument(
        "--newsletter-config",
        default=os.getenv("NEWSLETTER_CONFIG_FILE") or None,
        help="Path to newsletter_config.json (default: newsletter_config.json if present)",
    )
    p.add_argument(
        "--newsletter-edition-output",
        default=None,
        help=(
            "Optional path for a small JSON sidecar describing the resolved edition "
            "(id, deadline, window, counts). Not published to Pages."
        ),
    )
    p.add_argument(
        "--as-of",
        default=None,
        help=(
            "ISO-8601 instant used to resolve the newsletter edition. Testing and "
            "backfill only; never set this in production."
        ),
    )
    p.add_argument(
        "--no-title-provenance",
        action="store_true",
        help="Don't record titleSource/titleIsPlaceholder on events (env TITLE_PROVENANCE=0)",
    )
    p.add_argument(
        "--exclude-series",
        action="append",
        default=None,
        help=(
            "Exclude events whose transformed 'series' value matches these names. "
            "Accepts comma-separated strings or repeated flags (env EXCLUDE_SERIES)."
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv or sys.argv[1:])
    # Determine config path fallback
    config_path = ns.config or ("transform_config.json" if os.path.exists("transform_config.json") else None)
    raw = fetch_ics(ns.ics_url)
    calendar = Calendar(raw)
    manipulated = manipulate_data(calendar, ns.repo_variable)
    cfg = load_config(config_path)
    data = transform_calendar(manipulated, cfg)
    exclusions, exclusion_labels = _resolve_series_exclusions(cli_values=ns.exclude_series)
    if exclusions:
        data, removed = _apply_series_exclusions(data, exclusions)
        if removed:
            label_text = ", ".join(exclusion_labels) if exclusion_labels else "n/a"
            print(
                "Applied series exclusion filter (%s) -> removed %d events"
                % (label_text, removed),
                file=sys.stderr,
            )
    # Optional enrichment (network I/O) - perform as late as possible just before output
    # Resolved before the first consumer below: enrich_titles takes it too.
    cli_no_prov = getattr(ns, 'no_title_provenance', False)
    mark_prov = provenance_enabled(cli_flag=False if cli_no_prov else None)

    do_enrich = enrichment_enabled(ns.enrich_titles)
    overwrite = enrichment_overwrite_enabled(ns.enrich_overwrite)
    if do_enrich:
        stats = enrich_titles(data, True, overwrite=overwrite, mark_provenance=mark_prov)
        print(
            f"Enriched titles: attempted={stats.attempted} updated={stats.updated} "
            f"errors={stats.errors} overwrite={'true' if overwrite else 'false'}"
        )
    # Post-process fallback: ensure no blank or 'TBD' titles remain, even when
    # enrichment is disabled. Fill from FALLBACK_PREPEND_TEXT template,
    # optionally with speaker; guarantees a series-derived title as last resort.
    # Only pass cli_flag when --no-fallback-speaker is explicitly used
    cli_no_speaker = getattr(ns, 'no_fallback_speaker', False)
    include_speaker = fallback_include_speaker_enabled(
        cli_flag=False if cli_no_speaker else None
    )
    filled = fill_title_fallback(
        data, overwrite=False, include_speaker=include_speaker, mark_provenance=mark_prov
    )
    if filled:
        source = "speaker field" if include_speaker else "template"
        print(f"Fallback populated {filled} titles from {source}")
    if mark_prov:
        backfilled = ensure_title_provenance(data)
        if backfilled:
            print(f"Backfilled title provenance for {backfilled} events")

    # Optional content enrichment (independent of title enrichment)
    do_content_enrich = enrichment_content_enabled(ns.enrich_content)
    content_overwrite = enrichment_content_overwrite_enabled(ns.enrich_content_overwrite)
    if do_content_enrich:
        cstats = enrich_content(data, True, overwrite=content_overwrite)
        print(
            f"Enriched content: attempted={cstats.attempted} updated={cstats.updated} "
            f"errors={cstats.errors} overwrite={'true' if content_overwrite else 'false'}"
        )
    # Optional raw details enrichment (independent)
    do_raw_enrich = enrichment_raw_details_enabled(ns.enrich_raw_details)
    raw_overwrite = enrichment_raw_details_overwrite_enabled(ns.enrich_raw_details_overwrite)
    if do_raw_enrich:
        rstats = enrich_raw_details(data, True, overwrite=raw_overwrite)
        print(
            f"Enriched raw details: attempted={rstats.attempted} updated={rstats.updated} "
            f"errors={rstats.errors} overwrite={'true' if raw_overwrite else 'false'}"
        )

    # Optional raw extracts enrichment (post-processes rawEventDetails)
    do_extract_enrich = enrichment_raw_extracts_enabled(getattr(ns, 'enrich_raw_extracts', False))
    if do_extract_enrich:
        xstats = enrich_raw_extracts(data, True, overwrite=False)  # extracts don't overwrite by default
        print(
            f"Enriched raw extracts: attempted={xstats.attempted} "
            f"abstract={xstats.updated_abstract} bio={xstats.updated_bio} "
            f"errors={xstats.errors}"
        )

    # --limit is a local-iteration convenience and must never truncate the variant.
    data_full = list(data)
    if ns.limit is not None:
        data = data[: ns.limit]
    if ns.print_only:
        print(json.dumps(data, indent=2))
        if ns.newsletter_output:
            print(
                "--print-only: skipping newsletter variant write to "
                f"{ns.newsletter_output}",
                file=sys.stderr,
            )
        return 0
    out_path = Path(ns.output)
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Wrote {out_path} ({len(data)} events)")

    # The newsletter variant is written last, and deliberately after the primary
    # output: if edition resolution fails, events.json is already on disk and the
    # non-zero exit stops the release step, so the hourly WordPress ingest keeps
    # consuming the last good feed rather than starving over a config typo.
    if ns.newsletter_output:
        rc = _write_newsletter_variant(ns, data_full)
        if rc:
            return rc
    return 0


def _write_newsletter_variant(ns: argparse.Namespace, events: list[dict]) -> int:
    """Write the next edition's slice of `events`. Returns 0, or 4 on config error."""
    try:
        config_path = ns.newsletter_config or (
            "newsletter_config.json" if os.path.exists("newsletter_config.json") else None
        )
        nl_cfg = load_newsletter_config(config_path)
        if ns.as_of:
            print(
                f"::warning::--as-of={ns.as_of} in use; the newsletter edition is "
                "pinned to that instant rather than the live clock",
                file=sys.stderr,
            )
        now = parse_as_of(ns.as_of, nl_cfg)
        edition = upcoming_edition(nl_cfg, now)
        in_window, dropped = _apply_edition_window(events, edition)
    except (NewsletterConfigError, NoEditionFound, InvalidEventTime) as exc:
        print(f"Newsletter variant generation failed: {exc}", file=sys.stderr)
        return 4

    # Copy before stamping: the dicts are shared with the primary output, and
    # _apply_edition_window promises not to mutate them.
    variant = [copy.copy(ev) for ev in in_window]
    stamp_edition(variant, edition)
    placeholders = sum(1 for ev in variant if ev.get("titleIsPlaceholder"))

    nl_path = Path(ns.newsletter_output)
    nl_path.write_text(json.dumps(variant, indent=2), encoding="utf-8")
    print(
        f"Wrote {nl_path} (edition {edition.id}: {len(variant)} events, "
        f"{dropped} outside window, {placeholders} placeholder titles)"
    )

    if ns.newsletter_edition_output:
        sidecar = {
            "editionId": edition.id,
            "publicationAt": edition.publication_at.isoformat(),
            "deadlineAt": edition.deadline_at.isoformat(),
            "windowStart": edition.coverage_start.isoformat(),
            "windowEnd": edition.coverage_end.isoformat(),
            "timezone": edition.timezone,
            "eventCount": len(variant),
            "placeholderCount": placeholders,
        }
        Path(ns.newsletter_edition_output).write_text(
            json.dumps(sidecar, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
