# Copilot Instructions for `upcoming`

## Project Purpose
- Generate a normalized JSON events feed (`events.json`) from an upstream ICS calendar, with optional web-scraping enrichment for titles, content, and raw details.
- Intended for automation (GitHub Actions) and manual CLI use; schema compliance enforced via `schema/events.schema.json`.
- Canonical development and publishing both happen in `pu-orfe/upcoming`. Legacy-repository mirroring is retired; `src/mirror_release.py` is retained, unwired, for possible reintroduction.
- `pu-orfe/upcoming` is public so its release assets are directly shareable. Production refreshes run on a native every-30-minutes GitHub Actions schedule, a heartbeat workflow creates a tiny keepalive commit after 35 idle days so public-repo schedules do not age out, and the latest production payload is also deployed to GitHub Pages for `upcoming.orfe.princeton.edu/events.json`.

## Architecture Overview
- `src/main.py`: CLI entry point. Fetches ICS (`fetch_ics`), instantiates `ics.Calendar`, calls `transform_calendar`, and writes JSON. Handles optional enrichment based on CLI flags/env vars.
- `src/transform.py`: Defines `TransformConfig` dataclass and transformation helpers. Maps ICS event fields to output keys, normalizes dates with `arrow`, shapes location data, and inserts configured placeholders/copies.
- `src/enrich.py`: Networking + parsing layer for enrichment. Statically caches headers, applies optional Markdown conversion, and offers:
  - `enrich_titles`, `fill_title_fallback`
  - `enrich_content`
  - `enrich_raw_details`, `enrich_raw_extracts`, plus extraction helpers.
- `tools/validate_json.py`: JSON Schema validator using `jsonschema.Draft7Validator`.
- `examples/`: Reference ICS and expected JSON snapshot used in regression tests.
- Tests in `tests/` cover transformation, enrichment behaviors, and CLI flows (pytest + monkeypatch stubbing).

## Coding Guidelines
- Target Python 3.10+; keep type hints (`from __future__ import annotations`) and dataclasses consistent with current style.
- Avoid side effects in helpers. Functions like `transform_event`, `fetch_*`, and extractors should remain pure (depend only on args/env).
- For new enrichment logic, reuse the existing `requests.get` pattern (headers, `DEFAULT_TIMEOUT`) and make it monkeypatch-friendly (no global session state).
- Preserve JSON output formatting (indent=2). When writing files use UTF-8.
- ICS transformation: respect `TransformConfig` knobs. If adding new config fields, update defaults, loaders, and extend tests.
- Keep fallback rules intact: `fill_title_fallback` only overwrite empty/`TBD` titles unless explicitly told and enforces the 128-char prefix cap.
- Handle environment switches via small helpers (`enrichment_*_enabled`). Extend them if new flags are introduced to stay testable.

## Configuration & Environment
- Core env vars: `ICS_URL`, `OUTPUT_FILE`, `REPO_VARIABLE`, `TARGET_TZ`.
- Filtering: `EXCLUDE_SERIES` (comma-separated string or JSON array) removes events whose transformed `series` matches; CLI flag `--exclude-series` mirrors the env and can be repeated.
- Enrichment toggles: `ENRICH_TITLES`, `ENRICH_OVERWRITE`, `ENRICH_CONTENT`, `ENRICH_CONTENT_OVERWRITE`, `ENRICH_RAW_DETAILS`, `ENRICH_RAW_DETAILS_OVERWRITE`, `ENRICH_RAW_EXTRACTS`, `ENRICH_RAW_EXTRACTS_OVERWRITE`, `ENRICH_CONTENT_FORMAT`, `ENRICH_DEBUG`, `BOT_BYPASS_HEADER_VALUE`.
- Title fallback prefix via `FALLBACK_PREPEND_TEXT` (supports `{series}` etc., ignored when >=128 chars).
- CLI flags mirror env vars; prefer adding switches in `_parse_args` and associated env helpers together.

## Testing & Validation
- Use pytest: `pytest` or `pytest tests/test_transform.py::test_example_files_roundtrip -q` for focused checks.
- Tests expect network calls to be stubbed via `monkeypatch` and `DummyResp`; follow this pattern for new HTTP-dependent code.
- Validate JSON output against schema: `python tools/validate_json.py --schema schema/events.schema.json --data events.json`.
- Regression fixtures: update `examples/sample_output.expected.json` alongside logic changes; keep it small but representative.

## Development Workflow
- Install deps: `pip install -r requirements.txt` (see `Makefile install`).
- Common Make targets:
  - `make gen` / `make gen-enriched` / `make gen-raw` for local generation.
  - `make validate` / `make validate-enriched` for schema checks.
  - `make example-validate-enriched` for a quick sanity run using bundled fixtures.
- When adding CLI behavior, ensure `generate_events_json` and `main` stay aligned and update docs/README if user-facing semantics change.
- Keep GitHub Actions compatibility in mind (no interactive prompts, deterministic output ordering).

## Documentation & References
- README documents usage, env vars, and sample workflows; update it with any notable CLI or configuration changes.
- Schema updates should retain backward compatibility where possible and include validator test coverage.
- LICENSE: MIT. Honor existing contribution guidelines (see `CONTRIBUTING.md` if present).
