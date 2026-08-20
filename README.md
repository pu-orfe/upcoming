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
* Unit tests and regression testing

## Usage

### Release Assets

**Production** (`latest`)
- Canonical public URL: `https://github.com/pu-orfe/upcoming/releases/download/latest/events.json`
- Landing page: `https://upcoming.orfe.princeton.edu/`
- Custom-domain URL: `https://upcoming.orfe.princeton.edu/events.json`
- Published from `pu-orfe/upcoming`
- Triggers: Scheduled (every 30 minutes via native GitHub Actions cron), manual
- Purpose: Stable production feed

**Landing page**
- URL: `https://upcoming.orfe.princeton.edu/`
- Style: a lightweight, Paper Tiger–inspired page that explains the feed endpoints and links back to the repository
- Purpose: human-readable documentation for production, development, and test asset consumers

**Development** (`dev`)
- Canonical public URL: `https://github.com/pu-orfe/upcoming/releases/download/dev/events.json`
- Custom-domain landing page: `https://upcoming.orfe.princeton.edu/dev/`
- Custom-domain asset URLs:
  - `https://upcoming.orfe.princeton.edu/dev/events.json`
  - `https://upcoming.orfe.princeton.edu/dev/events-nofpo.json`
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
| `TITLE_ORFE_ZWSP` | CLI/CI | bool | `false` | Insert a zero-width space (U+200B) between the letters of `ORFE` in the `title` field only. Workaround for a downstream consumer whose regex trips on the token; see [ORFE token in titles](#orfe-token-in-titles). CLI: `--title-orfe-zwsp`. |
| `BOT_BYPASS_HEADER_VALUE` | CLI/CI | string | `1` | Value sent as `x-wdsoit-bot-bypass` header during enrichment requests. |
| `ENRICH_CONTENT` | CLI/CI | bool | `false` | Enable content scraping from the event page into `content` (fallback stays as ICS `DESCRIPTION` if not overwritten). |
| `ENRICH_CONTENT_OVERWRITE` | CLI/CI | bool | `false` | Overwrite non-empty `content` when enriching. |
| `ENRICH_CONTENT_FORMAT` | CLI/CI | enum | `text` | Output format for scraped content: `text` (plain), `markdown` (requires `markdownify`), or `html` (inner fragment). |
| `ENRICH_RAW_DETAILS` | CLI/CI | bool | `false` | Enable raw HTML scraping from the event page into `rawEventDetails` (inner HTML of `.events-detail-main` container). |
| `ENRICH_RAW_DETAILS_OVERWRITE` | CLI/CI | bool | `false` | Overwrite non-empty `rawEventDetails` when enriching. |
| `ENRICH_RAW_EXTRACTS` | CLI/CI | bool | `true` | Enable automatic extraction of `rawExtractAbstract` and `rawExtractBio` from `rawEventDetails` (requires raw details enrichment). |
| `ENRICH_RAW_EXTRACTS_OVERWRITE` | CLI/CI | bool | `false` | Overwrite existing `rawExtractAbstract`/`rawExtractBio` values when extracting. |

Boolean envs accept: `1,true,yes,on` (case-insensitive) for true.

Titles are guaranteed non-empty (enforced by `minLength: 1` in the schema). The fill order is: enriched subtitle → `FALLBACK_PREPEND_TEXT` template (+ speaker unless disabled) → a series-derived last resort such as `An Optimization Seminar Talk` (`A Seminar Talk` when the event has no series). The fallback pass runs even when title enrichment is disabled.

### Transform parameters

| Name | Scope | Type | Default | Purpose |
|------|-------|------|---------|---------|
| `TARGET_TZ` | CLI/CI | string | `America/New_York` | Target timezone for datetime normalization. |
| `EXCLUDE_SERIES` | CLI/CI | string or JSON array | — | Comma-separated list or JSON array of series names to drop after transformation. |

You can also provide a JSON config file via `--config` (copy from `transform_config.example.json`) to override mappings, placeholders, masks, etc.

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

### ORFE token in titles

A downstream consumer's regex trips on the literal string `ORFE` in `title` — most often via the generated fallback `An ORFE Department Colloquia Talk`. Setting `TITLE_ORFE_ZWSP=1` inserts U+200B ZERO WIDTH SPACE between each letter, so the title still *reads* identically but no longer contains the token:

```
An ORFE Department Colloquia Talk      ->  An O<U+200B>R<U+200B>F<U+200B>E Department Colloquia Talk
```

Deliberately narrow, so the workaround does not spread further than it must:

- **`title` only.** `series`, `speaker`, `rawEventDetails` and `urlRef` keep the plain token and stay matchable.
- **Case-sensitive.** Only the exact uppercase `ORFE` is split; `orfe` in URLs is untouched.
- **Applied last**, after enrichment and the title fallback, so it catches the token whatever produced it.
- **Idempotent.** Once split, the literal token is gone, so re-running changes nothing.
- **Off by default**, so behaviour is unchanged until the repo variable opts in.

This is a stopgap for a consumer-side bug, not a feature. Once that regex is fixed, set `TITLE_ORFE_ZWSP=0` (or delete the variable) and the step becomes a no-op; the code can then be removed with `src/enrich.py`'s `split_orfe_in_titles`, its `tests/test_orfe_zwsp.py`, the CLI flag, and the workflow `env` entries.

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
