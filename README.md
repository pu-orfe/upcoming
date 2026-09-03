# ORFE Upcoming

Automated pipeline that fetches a department ICS feed, applies configurable transformation, and publishes a stable JSON file as a GitHub Release asset.

Canonical development and publishing both happen in `pu-orfe/upcoming`. Release-asset mirroring to a legacy repository is currently retired (see [Legacy mirror](#legacy-mirror)). The old app/Azure dispatcher is no longer required; production refreshes now run on a native GitHub Actions schedule, a small heartbeat workflow keeps the public repo's schedules from aging out, and the latest production payload is also deployed to GitHub Pages for `upcoming.orfe.princeton.edu`.

## Features

* Every-30-minutes + manual workflow (cron + `workflow_dispatch`)
* Daily heartbeat check that writes a tiny keepalive commit only after 35 days without a `main` branch commit
* GitHub Pages deployment of the latest production `events.json` for `https://upcoming.orfe.princeton.edu/events.json`
* ICS fetching with SHA256 change detection
* Configurable field mapping and transformation
* Title enrichment from event pages
* Content enrichment (optional)
* Raw details extraction (optional)
* Failure streak tracking with issue creation
* JSON schema validation
* Title provenance (`titleSource` / `titleIsPlaceholder`) so consumers can tell a real title from a synthesized one
* A newsletter variant feed scoped to the next edition's coverage window, on a configurable publication schedule
* Hourly deadline watch that tracks events still awaiting a title in a single GitHub issue per edition
* Unit tests and regression testing, runnable locally or in a container

## Usage

### Release Assets

**Production** (`latest`)
- Canonical public URL: `https://github.com/pu-orfe/upcoming/releases/download/latest/events.json`
- Landing page: `https://upcoming.orfe.princeton.edu/`
- Custom-domain URLs:
  - `https://upcoming.orfe.princeton.edu/events.json` — the full feed
  - `https://upcoming.orfe.princeton.edu/events-newsletter.json` — [newsletter variant](#newsletter-variant): only the next edition's events
- Published from `pu-orfe/upcoming`
- Triggers: Scheduled (every 30 minutes via native GitHub Actions cron), manual
- Purpose: Stable production feed

**Landing page**
- URL: `https://upcoming.orfe.princeton.edu/`
- Style: a lightweight, Paper Tiger–inspired page that explains the feed endpoints and links back to the repository
- Purpose: human-readable documentation for production, development, and test asset consumers
- Carries a **What each record contains** field reference and an inline
  [feed view simulator](#feed-view-simulator); `/dev/` carries the simulator too, pointed at the dev assets

**Development** (`dev`)
- Canonical public URL: `https://github.com/pu-orfe/upcoming/releases/download/dev/events.json`
- Custom-domain landing page: `https://upcoming.orfe.princeton.edu/dev/`
- Custom-domain asset URLs:
  - `https://upcoming.orfe.princeton.edu/dev/events.json`
  - `https://upcoming.orfe.princeton.edu/dev/events-nofpo.json`
  - `https://upcoming.orfe.princeton.edu/dev/events-newsletter.json`
  - `https://upcoming.orfe.princeton.edu/dev/test.json`
- Published from `pu-orfe/upcoming`
- Triggers: Manual (`workflow_dispatch` on the development branch you want to test)
- Purpose: Testing environment

**Development test fixture** (`test.json`)
- URL: `https://upcoming.orfe.princeton.edu/dev/test.json`
- Contents: a static, realistic dummy feed based on the shape and style of previously served ORFE Upcoming assets
- Intended use: remote ingest and downstream integration testing when the live production or development feeds are empty or otherwise unsuitable as test input

### Landing pages

The Pages site has two hand-written HTML pages:

| Path | Serves | Edit this file |
|------|--------|----------------|
| `/` | `https://upcoming.orfe.princeton.edu/` | `site/index.html` |
| `/dev/` | `https://upcoming.orfe.princeton.edu/dev/` | `site/dev/index.html` |

How to ship a change:

- **`site/index.html`** (production landing): merge to `main`, then dispatch **`Publish Landing Pages`**. This workflow only rebuilds the Pages artifact (no ICS fetch, no JSON regeneration) and reuses the current `latest`/`dev` release assets for the JSON endpoints. Restricted to `main`.
- **`site/dev/index.html`** (dev landing): dispatch **`ICS to JSON (Development)`** from the branch with your edits, with `force: true`. The dev workflow now publishes `site/index.html` from the branch too, so you can preview both landing pages together.

The Pages tree is assembled by the local composite action `actions/prepare-pages-artifact`, which all three workflows share.

### Publish verification

**`Verify Published Feed`** (`.github/workflows/verify_published_feed.yml`) runs every 30 minutes and answers two questions the pipeline cannot answer about itself:

| Check | Catches |
|-------|---------|
| Served bytes vs the release asset, per path | A release was published but Pages never deployed, so the site serves an older payload |
| Live ICS hash vs `ICS_SHA256` in the `latest` release body | Generation stopped, so the site and the release are stale *together* and agree with each other |

It runs on its own schedule rather than as a step in the publish job on purpose. When the publish job fails early, its remaining steps are *skipped*, not failed — a check living there would be skipped alongside the deploy it was meant to verify.

Both checks tolerate normal transients rather than paging on them:

- Pages sits behind a CDN with `max-age=600` and per-edge caches, and neither a query string nor `Cache-Control: no-cache` forces revalidation. A mismatch within a 20-minute grace window after publication is `pending`, not a fault.
- Each path is sampled several times, likely landing on different edges. **Any one matching sample passes** — it proves the deploy reached the origin, so a stale sibling edge is just serving out its TTL.
- An unreachable host is reported as `error`, never as a content problem.
- Alerting requires two consecutive failing runs, so a single blip is never actionable.

Run it locally with:
```bash
GITHUB_TOKEN=$(gh auth token) python -m src.verify_published_feed \
  --base-url https://upcoming.orfe.princeton.edu \
  --repo pu-orfe/upcoming \
  --check "events.json=latest:events.json" \
  --check "events-newsletter.json=latest:events-newsletter.json" \
  --ics-url "$ICS_URL"
```
Exit codes: `0` ok, `1` drift, `2` error, `3` ICS stale.

### Legacy mirror

Mirroring to a legacy repository is **retired**. Nothing in CI writes to another repository any more:

- the release-mirror step was removed from `ICS to JSON` and `ICS to JSON (Development)`
- the `Mirror legacy repository` workflow, which force-pushed `main` and all tags on every push, was deleted

`src/mirror_release.py` and its tests are deliberately **kept but unwired**, so reintroduction is a workflow change rather than a rewrite. The module resolves a target repository's canonical name before mutating it and follows redirects on all HTTP methods, which matters because a renamed target otherwise fails `DELETE` with HTTP 307.

To reintroduce it, add a step like this *after* the canonical release is published, and keep `continue-on-error` so a mirror problem can never block the Pages deploy:

```yaml
- name: Mirror to legacy repository
  if: steps.check_change.outputs.skip != 'true'
  continue-on-error: true
  env:
    TARGET_GITHUB_TOKEN: ${{ secrets.LEGACY_REPOSITORY_TOKEN }}
  run: |
    python -m src.mirror_release \
      --target-repo "<owner>/<name>" --target-commitish main \
      --tag latest --title "Latest Events" --notes "..." \
      --asset events.json --latest
```

The `LEGACY_REPOSITORY_TOKEN` secret is left in place for that purpose.

### Local Development

Generate JSON locally:
```bash
python -m src.main --ics-url "https://example.com/calendar.ics" --output events.json
```

With enrichment:
```bash
ENRICH_TITLES=1 python -m src.main --ics-url "$ICS_URL" --limit 2 --print-only
```

Validate output:
```bash
Update examples after changes:
```bash
python -m src.main --ics-url file://$PWD/examples/sample_input.example.ics --print-only > /tmp/new.json
# Edit /tmp/new.json to keep representative subset
mv /tmp/new.json examples/sample_output.expected.json
pytest tests/test_transform.py::test_example_files_roundtrip -q
```

One-liners:
- Generate and validate from your ICS_URL
	```bash
	make install
	ICS_URL="https://example.com/calendar.ics" make gen-enriched validate
	```
- Validate a previously generated file
	```bash
	make validate
	```
- Use the example ICS and validate (with enrichment and fallback applied)
	```bash
	make example-validate-enriched
	```

Alternatively invoke the validator directly:
```bash
python tools/validate_json.py --schema schema/events.schema.json --data events.json
```

## Newsletter variant

The Engineering events newsletter publishes each Monday around noon — except Labor Day
week, when it publishes Tuesday — and the deadline to submit an event is the Tuesday
preceding publication at noon. Editors ingesting the full feed see every future event,
including ones whose speaker has not yet supplied a title.

Two things address that, and both are configuration rather than code, because the
schedule changes between semesters.

### Title provenance

Every event carries two extra fields:

| Field | Meaning |
|-------|---------|
| `titleSource` | `enriched` (scraped subtitle) · `ics` (supplied by the feed) · `fallback-speaker` · `fallback-template` · `fallback-series` |
| `titleIsPlaceholder` | `true` for the three `fallback-*` sources — the title was synthesized here, not written by a person |

The pipeline guarantees a non-empty `title` (`minLength: 1` in the schema), so an event
awaiting a title still ships as something like `An ORFE Departmental Colloquia Talk`.
These fields are what let an editor tell that apart from a real title and filter it out.
Disable with `TITLE_PROVENANCE=0` or `--no-title-provenance`.

### The variant feed

`events-newsletter.json` holds only the events inside the next edition's coverage
window — publication day 00:00 through that week's Sunday 23:59:59, local. `events.json`
is untouched, so existing consumers are unaffected. Placeholder-titled events are kept
and flagged, never dropped: an editor should see that a listing needs chasing, not find
it silently missing.

Each item additionally carries `newsletterEdition`, the stable id of the edition it was
built for. That id is the **week-anchor Monday** even when publication shifts, so adding
a Labor Day exception after the fact does not rename an edition or re-notify about it.

```bash
python -m src.main --ics-url "$ICS_URL" \
  --output events.json \
  --newsletter-output events-newsletter.json \
  --newsletter-config newsletter_config.json
```

Inspect the resolved schedule at any instant:

```bash
python -m src.newsletter --json                       # the next edition
python -m src.newsletter --which next-deadline --json # the one being submitted for
python -m src.newsletter --as-of 2026-09-02T09:00:00 --json   # Labor Day week
```

### Schedule configuration

`newsletter_config.json` (template: `newsletter_config.example.json`). Every bound is
`anchor + offset_days @ time`, where the anchor is the week-start Monday or the
publication date:

| Bound | Anchor | Offset | Time | Normal week | Labor Day week 2026-09-07 |
|-------|--------|--------|------|-------------|---------------------------|
| publication | `week_start` | 0 | 12:00:00 | Mon | **Tue 09-08** (exception) |
| deadline | `week_start` | −6 | 12:00:00 | preceding Tue | Tue 09-01 |
| coverage start | `publication` | 0 | 00:00:00 | Mon | **Tue 09-08** |
| coverage end | `week_start` | +6 | 23:59:59 | Sun | Sun 09-13 |

Coverage *start* follows a publication shift; coverage *end* stays pinned to the week.
Anchoring the deadline to the week start means moving publication does not drag the
submission deadline with it — set `deadline_date` on the exception if you want it to.

`schedules[]` entries are partial overrides merged onto `defaults`, selected by
`effective_from`/`effective_to`, which is how a semester changes the deadline.
`exceptions[]` are keyed by the week-anchor Monday. `blackouts[]` skip recess weeks.

### Feed view simulator

The landing pages embed a simulator so editors can see an edition before it exists. Pick a
**week** and it filters the live feed in the browser, rendering exactly what the editorial
system would ingest: the events in the window, which of them still carry a synthesised
title, how long remains before the deadline, what falls outside the window and why, and the
resulting JSON.

One control covers the normal case. Choosing a week fills in the standard schedule —
published that Monday at noon, deadline the Tuesday six days before — and an **Advanced**
panel holds the exact publication date/time, deadline date/time and an as-of clock for
anything that departs from it, such as a week that publishes on the Tuesday. Editing any of
those marks the panel *customised* and stops the week from overwriting them; changing the
week again resets them.

Two properties matter for trusting what it shows:

- **It filters the way the pipeline does.** Feed timestamps are naive Eastern wall clock, and
  because every timestamp shares one format and one zone, the simulator compares them as
  plain strings. That is not a shortcut &mdash; it is what makes the result independent of the
  viewer's own timezone, so a reader in California sees exactly what WordPress sees.
  `tests/test_feed_simulator.py` asserts the JavaScript and `src/newsletter.py` agree on the
  window bounds and on which events fall inside them.
- **It never writes anything.** It fetches a published feed and computes in the page.

The view is deep-linkable, so a specific edition can be sent to someone:

```
https://upcoming.orfe.princeton.edu/?pub=2026-09-21&deadline=2026-09-15&now=2026-09-14T13:00#feed-simulator
```

Query parameters: `pub`, `pubtime`, `deadline`, `deadlinetime`, `now` (and `feed` on
`/dev/`, which offers a choice of dev feeds).

**It always reads a full feed, never `events-newsletter.json`.** The variant is the
finished artifact for one edition; the simulator exists to preview editions that do not
exist yet. Pointing it at the variant makes every other edition come back empty, which
reads as "nothing is scheduled", so the variant is simply not offered as a source. The
production page reads `events.json` and has no selector at all.

The logic lives in `site/feed-simulator.js`, shared by both pages and deployed to the Pages
root by `actions/prepare-pages-artifact`. Its core is exercised by `tests/js/` under
`node --test`; `tests/test_pipeline_contract.py` asserts that any asset the pages reference is
actually deployed, so the page cannot ship a 404.

### Deadline watch

`Newsletter Deadline Watch` (`.github/workflows/newsletter_deadline_watch.yml`) runs
hourly and keeps **one GitHub issue per edition** listing events still carrying a
placeholder title, labelled `newsletter-titles`. It closes the issue when every title
has been supplied.

The cron is deliberately coarse. The deadline lives in `newsletter_config.json`, and
pinning a weekday and hour in cron would duplicate that schedule where the config cannot
reach it; GitHub cron is UTC-only, so a fixed hour is wrong for half the year. The
*script* decides whether a run falls inside a reminder lead window
(`reminders.lead_hours`, default 72/48/24/4).

Idempotency lives in the issue body, not in local state: the first line carries a marker
naming the edition and the milestones already announced. Every run rewrites the body
(which does not notify) and comments only when a new milestone is crossed (which does).
Matching is on that marker, so renaming the issue does not break dedupe.

`--target` picks the edition to report on. A deadline falls six days before its own
publication, so two editions matter at once: the one about to publish, whose deadline has
passed and whose gaps now need a late addition emailed to the editor, and the one
contributors are currently submitting for. `auto` (the default) escalates to the former
when it still has placeholders.

```bash
GITHUB_TOKEN=$(gh auth token) python -m src.notify_missing_titles \
  --repo pu-orfe/upcoming \
  --source release:latest:events.json \
  --newsletter-config newsletter_config.json \
  --dry-run
```

Exit codes: `0` reconciled · `1` deadline passed with placeholders remaining · `2` error
· `3` **no events at all in the coverage window**. That last one exists because a wedged
pipeline, a broken window filter and a timezone misread all otherwise present as "no
placeholders, all good"; pass `--allow-empty-window` for a genuine recess.

## Tests

```bash
make test                 # pytest locally
make docker-test          # the same suite in a container
make newsletter-validate  # end-to-end variant generation against the sample ICS
make test-js              # the simulator's node suite on its own
make serve-site           # preview the landing pages and simulator locally
```

The simulator's JavaScript is covered twice: `tests/js/feed-simulator.test.js` runs under
`node --test`, and `tests/test_feed_simulator.py` runs that suite from pytest *and*
cross-checks the JavaScript against `src/newsletter.py` on the same editions. Those pytest
cases skip when `node` is absent (the slim container has none) but run on CI's Ubuntu
runners; `test_simulator_asset_exists` fails rather than skips if the files go missing, so a
deletion cannot hide behind a skip.

`docker-compose` builds from `Dockerfile` and bind-mounts the working tree, so iterating
needs no rebuild. `requirements.txt` carries `tzdata` as a `zoneinfo` fallback: the
schedule arithmetic needs an IANA time zone database, Windows ships none, and a slim
base image is not guaranteed to keep one across revisions. A test asserts the
`America/New_York` lookup resolves, so a base image that drops it fails loudly.

Tests point at `tests/fixtures/newsletter_config.test.json`, never the live schedule — an
editor changing a deadline must not break CI in a way that looks like a code regression.

## Configuration reference (env vars and inputs)

These environment variables and workflow inputs control behavior at runtime.

### Core runtime

| Name | Scope | Type | Default | Purpose |
|------|-------|------|---------|---------|
| `ICS_URL` | CLI/CI | string | — | Upstream ICS feed URL. Supports http(s), `file://`, or local paths. |
| `OUTPUT_FILE` | CLI/CI | string | `events.json` | Output JSON filename. |
| `REPO_VARIABLE` | CLI/CI | string | `default` | Arbitrary variable passed to `manipulate_data` (currently unused). |

### Enrichment and fallback

| Name | Scope | Type | Default | Purpose |
|------|-------|------|---------|---------|
| `ENRICH_TITLES` | CLI/CI | bool | `false` (manual CLI), `true` (scheduled CI, manual workflow default) | Enable subtitle scraping to populate `title` from each event detail page. |
| `ENRICH_OVERWRITE` | CLI/CI | bool | `false` | When enriching, overwrite non-empty `title` values instead of only filling blanks. |
| `ENRICH_DEBUG` | CLI/CI | bool | `false` | Verbose enrichment logging (fetch/skip/overwrite decisions). |
| `FALLBACK_PREPEND_TEXT` | CLI/CI | string | — | Prefix template for titles filled from `speaker`. Supports `{series}` placeholder and `{a_an}` for automatic A/An selection based on how the next word is *pronounced*; missing keys render empty and whitespace is collapsed. Max length: 128 chars. Example: `{a_an} {series} Talk by` → `An ORFE Colloquium Talk by Alice`. |
| `FALLBACK_INCLUDE_SPEAKER` | CLI/CI | bool | `true` | Include speaker name in fallback titles. Set to `0` to use only `FALLBACK_PREPEND_TEXT` template (e.g., `A {series} Talk` without speaker). CLI: `--no-fallback-speaker`. |
| `BOT_BYPASS_HEADER_VALUE` | CLI/CI | string | `1` | Value sent as `x-wdsoit-bot-bypass` header during enrichment requests. |
| `ENRICH_CONTENT` | CLI/CI | bool | `false` | Enable content scraping from the event page into `content` (fallback stays as ICS `DESCRIPTION` if not overwritten). |
| `ENRICH_CONTENT_OVERWRITE` | CLI/CI | bool | `false` | Overwrite non-empty `content` when enriching. |
| `ENRICH_CONTENT_FORMAT` | CLI/CI | enum | `text` | Output format for scraped content: `text` (plain), `markdown` (requires `markdownify`), or `html` (inner fragment). |
| `ENRICH_RAW_DETAILS` | CLI/CI | bool | `false` | Enable raw HTML scraping from the event page into `rawEventDetails` (inner HTML of `.events-detail-main` container). |
| `ENRICH_RAW_DETAILS_OVERWRITE` | CLI/CI | bool | `false` | Overwrite non-empty `rawEventDetails` when enriching. |
| `ENRICH_RAW_EXTRACTS` | CLI/CI | bool | `true` | Enable automatic extraction of `rawExtractAbstract` and `rawExtractBio` from `rawEventDetails` (requires raw details enrichment). |
| `ENRICH_RAW_EXTRACTS_OVERWRITE` | CLI/CI | bool | `false` | Overwrite existing `rawExtractAbstract`/`rawExtractBio` values when extracting. |

Boolean envs accept: `1,true,yes,on` (case-insensitive) for true.

Each fill site records where the title came from in `titleSource`, and sets `titleIsPlaceholder` for the `fallback-*` ones — see [Title provenance](#title-provenance).

Titles are guaranteed non-empty (enforced by `minLength: 1` in the schema). The fill order is: enriched subtitle → `FALLBACK_PREPEND_TEXT` template (+ speaker unless disabled) → a series-derived last resort such as `An Optimization Seminar Talk` (`A Seminar Talk` when the event has no series). The fallback pass runs even when title enrichment is disabled.

### Transform parameters

| Name | Scope | Type | Default | Purpose |
|------|-------|------|---------|---------|
| `TARGET_TZ` | CLI/CI | string | `America/New_York` | Target timezone for datetime normalization. |
| `EXCLUDE_SERIES` | CLI/CI | string or JSON array | — | Comma-separated list or JSON array of series names to drop after transformation. |

You can also provide a JSON config file via `--config` (copy from `transform_config.example.json`) to override mappings, placeholders, masks, etc.

### Newsletter schedule and variant

See [Newsletter variant](#newsletter-variant) for what these do.

| Name | Scope | Type | Default | Purpose |
|------|-------|------|---------|---------|
| `NEWSLETTER_OUTPUT_FILE` | CLI/CI | string | — | Path for the newsletter variant. Unset means no variant is written. CLI: `--newsletter-output`. |
| `NEWSLETTER_CONFIG_FILE` | CLI/CI | string | `newsletter_config.json` | Schedule config consumed by `src.main`. CLI: `--newsletter-config`. |
| `NEWSLETTER_CONFIG` | CLI | string | `newsletter_config.json` | Schedule config consumed by `src.newsletter` and `src.notify_missing_titles`. |
| `NEWSLETTER_TZ` | CLI/CI | string | `TARGET_TZ`, else `America/New_York` | Timezone for all schedule arithmetic. |
| `NEWSLETTER_PUBLISH_WEEKDAY` | CLI/CI | `MON`..`SUN` or `0`..`6` | `MON` | Publication weekday. Monday is `0`; ISO `7` is rejected. |
| `NEWSLETTER_PUBLISH_TIME` | CLI/CI | `HH:MM[:SS]` | `12:00:00` | Publication time of day. |
| `NEWSLETTER_DEADLINE_WEEKDAY` | CLI/CI | `MON`..`SUN` or `0`..`6` | `TUE` (via offset −6) | Resolved to the most recent such weekday *strictly before* publication. |
| `NEWSLETTER_DEADLINE_OFFSET_DAYS` | CLI/CI | integer | `-6` | Raw offset from the week-start Monday. Wins over `NEWSLETTER_DEADLINE_WEEKDAY`. |
| `NEWSLETTER_DEADLINE_TIME` | CLI/CI | `HH:MM[:SS]` | `12:00:00` | Submission deadline time of day. |
| `NEWSLETTER_REMINDER_LEAD_HOURS` | CLI/CI | comma-separated numbers | `72,48,24,4` | Hours before the deadline at which the watch announces a milestone. |
| `TITLE_PROVENANCE` | CLI/CI | bool | `true` | Record `titleSource` / `titleIsPlaceholder`. CLI: `--no-title-provenance`. |

Environment wins over the config file. `--as-of` (on `src.main`, `src.newsletter` and
`src.notify_missing_titles`) pins the clock for testing and backfill; it deliberately has
**no** environment default, since a stray repo variable would freeze the edition
indefinitely, and `src.main` emits a `::warning::` whenever it is used.

### GitHub Actions inputs (manual/scheduled)

| Name | Workflow | Type | Default | Purpose |
|------|----------|------|---------|---------|
| `force` | `ICS to JSON` | input | `false` | Force regeneration even if ICS content hash is unchanged. |
| `enrich_titles` | `ICS to JSON` | input | `true` | Toggle enrichment on manual runs (scheduled runs always enrich). |
| `enrich_raw_details` | `ICS to JSON` | input | `true` | Capture raw event details HTML on manual runs. |
| `replace_latest` | `ICS to JSON` | input | `false` | Replace the Latest Events release instead of creating a separate manual release. |

CLI flags mirror the envs: `--enrich-titles`, `--enrich-overwrite`, `--enrich-content`, `--enrich-content-overwrite`, `--enrich-raw-details`, `--enrich-raw-details-overwrite`, `--enrich-raw-extracts`.
`--exclude-series` accepts comma-separated names and can be repeated; it mirrors `EXCLUDE_SERIES`.
`--no-fallback-speaker` disables including speaker in fallback titles; mirrors `FALLBACK_INCLUDE_SPEAKER=0`.
`--newsletter-output`, `--newsletter-config`, `--newsletter-edition-output` and `--as-of` control the newsletter variant; `--no-title-provenance` mirrors `TITLE_PROVENANCE=0`.

`FALLBACK_PREPEND_TEXT` supports two placeholders: `{series}` inserts the event series name, and `{a_an}` auto-selects "A" or "An" (e.g., `{a_an} {series} Talk by` → "An ORFE Colloquium Talk by Alice").

`{a_an}` follows pronunciation rather than spelling, because spelling alone is wrong in both directions:

| Series starts with | Article | Why |
|---|---|---|
| `S. S. Wilks Memorial Seminar` | **An** | Spelled out, "ess" |
| `FPO` | **An** | Spelled out, "ef" |
| `ORFE Department Colloquia` | **An** | Read as a word, "or-fee" |
| `University Seminar` | **A** | Vowel letter, "yoo" sound |
| `Hour-Long Seminar` | **An** | Consonant letter, vowel sound |
| `PDE Workshop` | **A** | Spelled out, "pee" |

A single letter, or an all-caps run, is treated as spelled out and judged by the name of its first letter. All-caps acronyms that are read as words instead live in `_WORD_ACRONYMS` in `src/enrich.py` — add to that set if a new one appears in the feed.
