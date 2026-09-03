SHELL := /bin/bash

# Default output file
OUTPUT ?= events.json
SCHEMA := schema/events.schema.json
SITE_PORT ?= 8730

# Newsletter variant
NEWSLETTER_OUTPUT ?= events-newsletter.json
NEWSLETTER_SCHEMA := schema/events-newsletter.schema.json
NEWSLETTER_CONFIG ?= newsletter_config.json
TEST_NL_CONFIG := tests/fixtures/newsletter_config.test.json
# Pinned clock for reproducible example runs (a Friday; next edition is 2025-09-08)
AS_OF ?= 2025-09-05T12:00:00-04:00

.PHONY: help install gen gen-enriched gen-raw gen-enriched-raw validate validate-enriched example-gen example-validate example-validate-enriched example-validate-raw example-validate-enriched-raw test test-js docker-test docker-test-build docker-shell gen-newsletter newsletter-edition newsletter-example newsletter-validate serve-site

help:
	@echo "Targets:"
	@echo "  install                  - pip install -r requirements.txt"
	@echo "  gen                      - generate $(OUTPUT) from $$ICS_URL"
	@echo "  gen-enriched             - generate with --enrich-titles (includes fallback)"
	@echo "  gen-raw                  - generate with --enrich-raw-details"
	@echo "  gen-enriched-raw         - generate with both title and raw details enrichment"
	@echo "  validate                 - validate $(OUTPUT) against $(SCHEMA)"
	@echo "  validate-enriched        - gen-enriched then validate"
	@echo "  example-gen              - generate /tmp/events.json from examples/sample_input.example.ics"
	@echo "  example-validate         - validate /tmp/events.json"
	@echo "  example-validate-raw     - example-gen with raw details then validate"
	@echo "  example-validate-enriched-raw - example-gen with titles+raw details then validate"
	@echo "  test                     - run the pytest suite locally"
	@echo "  test-js                  - run the feed simulator's node suite"
	@echo "  docker-test              - run the pytest suite in a container"
	@echo "  docker-shell             - interactive shell in the test container"
	@echo "  gen-newsletter           - generate $(OUTPUT) plus $(NEWSLETTER_OUTPUT) from $$ICS_URL"
	@echo "  newsletter-edition       - print the currently resolved edition as JSON"
	@echo "  newsletter-example       - generate both files from the sample ICS at AS_OF"
	@echo "  newsletter-validate      - newsletter-example, then validate against both schemas"
	@echo "  serve-site               - preview site/ (and the simulator) on $(SITE_PORT)"

install:
	pip install -r requirements.txt

# Generate from ICS_URL into $(OUTPUT)
gen:
	@if [ -z "$$ICS_URL" ]; then echo "ICS_URL is not set" >&2; exit 1; fi
	python -m src.main --ics-url "$$ICS_URL" --output "$(OUTPUT)"

# Generate enriched (subtitle scraping + fallback) from ICS_URL into $(OUTPUT)
gen-enriched:
	@if [ -z "$$ICS_URL" ]; then echo "ICS_URL is not set" >&2; exit 1; fi
	python -m src.main --ics-url "$$ICS_URL" --output "$(OUTPUT)" --enrich-titles

gen-raw:
	@if [ -z "$$ICS_URL" ]; then echo "ICS_URL is not set" >&2; exit 1; fi
	python -m src.main --ics-url "$$ICS_URL" --output "$(OUTPUT)" --enrich-raw-details

gen-enriched-raw:
	@if [ -z "$$ICS_URL" ]; then echo "ICS_URL is not set" >&2; exit 1; fi
	python -m src.main --ics-url "$$ICS_URL" --output "$(OUTPUT)" --enrich-titles --enrich-raw-details

validate:
	python tools/validate_json.py --schema "$(SCHEMA)" --data "$(OUTPUT)"

validate-enriched: gen-enriched validate

example-gen:
	python -m src.main --ics-url "$(PWD)/examples/sample_input.example.ics" --output /tmp/events.json

example-validate:
	python tools/validate_json.py --schema "$(SCHEMA)" --data /tmp/events.json

example-validate-enriched:
	python -m src.main --ics-url "$(PWD)/examples/sample_input.example.ics" --output /tmp/events.json --enrich-titles
	python tools/validate_json.py --schema "$(SCHEMA)" --data /tmp/events.json

example-validate-raw:
	python -m src.main --ics-url "$(PWD)/examples/sample_input.example.ics" --output /tmp/events.json --enrich-raw-details
	python tools/validate_json.py --schema "$(SCHEMA)" --data /tmp/events.json

example-validate-enriched-raw:
	python -m src.main --ics-url "$(PWD)/examples/sample_input.example.ics" --output /tmp/events.json --enrich-titles --enrich-raw-details
	python tools/validate_json.py --schema "$(SCHEMA)" --data /tmp/events.json

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

test:
	pytest -q

# The simulator's core, exercised directly. tests/test_feed_simulator.py also runs
# this from pytest and cross-checks it against src/newsletter.py.
test-js:
	# Explicit files, not the directory: `node --test <dir>` needs Node 24+.
	node --test tests/js/*.test.js

docker-test-build:
	docker-compose build tests

docker-test: docker-test-build
	docker-compose run --rm tests

docker-shell:
	docker-compose run --rm --entrypoint /bin/bash tests

# ---------------------------------------------------------------------------
# Newsletter variant
# ---------------------------------------------------------------------------

# Generate the full feed and the next edition's slice in one pass.
gen-newsletter:
	@if [ -z "$$ICS_URL" ]; then echo "ICS_URL is not set" >&2; exit 1; fi
	python -m src.main --ics-url "$$ICS_URL" --output "$(OUTPUT)" \
	  --newsletter-output "$(NEWSLETTER_OUTPUT)" \
	  --newsletter-config "$(NEWSLETTER_CONFIG)" \
	  --newsletter-edition-output newsletter-edition.json

newsletter-edition:
	python -m src.newsletter --config "$(NEWSLETTER_CONFIG)" --json

# Reproducible end-to-end run against the checked-in sample ICS.
newsletter-example:
	python -m src.main --ics-url "$(PWD)/examples/sample_input.example.ics" \
	  --output /tmp/events.json \
	  --newsletter-output /tmp/events-newsletter.json \
	  --newsletter-config "$(TEST_NL_CONFIG)" \
	  --newsletter-edition-output /tmp/newsletter-edition.json \
	  --as-of "$(AS_OF)"

newsletter-validate: newsletter-example
	python tools/validate_json.py --schema "$(NEWSLETTER_SCHEMA)" --data /tmp/events-newsletter.json
	python tools/validate_json.py --schema "$(SCHEMA)" --data /tmp/events-newsletter.json
	python tools/validate_json.py --schema "$(SCHEMA)" --data /tmp/events.json

# ---------------------------------------------------------------------------
# Landing pages
# ---------------------------------------------------------------------------

# Preview site/ locally. The simulator fetches ./events.json relative to the page,
# so generate one first (make newsletter-example) and copy it in, or just point the
# feed selector at a file you have.
serve-site:
	@echo "Serving site/ at http://127.0.0.1:$(SITE_PORT)/  (Ctrl-C to stop)"
	@echo "Tip: cp /tmp/events.json site/events.json after 'make newsletter-example'"
	cd site && python3 -m http.server $(SITE_PORT)
