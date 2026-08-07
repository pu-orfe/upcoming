"""Core module to fetch an ICS feed, (optionally) manipulate it, and emit events.json.

Flask was removed; JSON delivery now happens by committing / publishing the generated
file (e.g. via GitHub Action artifact or GitHub Pages). A tiny helper to optionally
serve the file locally via `python -m http.server` can be used if desired.
"""

from __future__ import annotations

import argparse
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
    do_enrich = enrichment_enabled(ns.enrich_titles)
    overwrite = enrichment_overwrite_enabled(ns.enrich_overwrite)
    if do_enrich:
        stats = enrich_titles(data, True, overwrite=overwrite)
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
    filled = fill_title_fallback(data, overwrite=False, include_speaker=include_speaker)
    if filled:
        source = "speaker field" if include_speaker else "template"
        print(f"Fallback populated {filled} titles from {source}")

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

    if ns.limit is not None:
        data = data[: ns.limit]
    if ns.print_only:
        print(json.dumps(data, indent=2))
        return 0
    out_path = Path(ns.output)
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Wrote {out_path} ({len(data)} events)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
