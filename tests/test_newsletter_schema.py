"""The newsletter variant schema, and that it stays a superset of the base one."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from src import main as main_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ICS = REPO_ROOT / "examples" / "sample_input.example.ics"
TEST_CONFIG = REPO_ROOT / "tests" / "fixtures" / "newsletter_config.test.json"
BASE_SCHEMA_PATH = REPO_ROOT / "schema" / "events.schema.json"
NL_SCHEMA_PATH = REPO_ROOT / "schema" / "events-newsletter.schema.json"

AS_OF = "2025-09-05T12:00:00-04:00"


@pytest.fixture(scope="module")
def base_schema():
    return json.loads(BASE_SCHEMA_PATH.read_text())


@pytest.fixture(scope="module")
def nl_schema():
    return json.loads(NL_SCHEMA_PATH.read_text())


@pytest.fixture
def variant(tmp_path, capsys):
    out = tmp_path / "events.json"
    nl = tmp_path / "events-newsletter.json"
    rc = main_mod.main([
        "--ics-url", str(SAMPLE_ICS), "--output", str(out),
        "--newsletter-output", str(nl), "--newsletter-config", str(TEST_CONFIG),
        "--as-of", AS_OF,
    ])
    capsys.readouterr()
    assert rc == 0
    return json.loads(nl.read_text())


def _valid_item(**overrides):
    item = {
        "guid": "g", "startTime": "2025-09-08T12:15:00", "endTime": "2025-09-08T13:15:00",
        "urlRef": "https://example.org/1",
        "location": {"name": "Sherrerd Hall", "id": "", "detail": "101"},
        "title": "A Real Title", "cancelled": "", "bannerImage": "",
        "itemType": "advertisement", "titleSource": "enriched",
        "titleIsPlaceholder": False, "newsletterEdition": "2025-09-08",
    }
    item.update(overrides)
    return item


def test_variant_validates_against_newsletter_schema(variant, nl_schema):
    assert len(variant) > 0, "an empty array would validate trivially"
    errors = sorted(Draft7Validator(nl_schema).iter_errors(variant), key=str)
    assert errors == []


def test_variant_also_validates_against_base_schema(variant, base_schema):
    """The variant is a strict subset, so existing consumers stay safe."""
    assert len(variant) > 0
    assert sorted(Draft7Validator(base_schema).iter_errors(variant), key=str) == []


@pytest.mark.parametrize(
    "missing", ["titleSource", "titleIsPlaceholder", "newsletterEdition"]
)
def test_newsletter_schema_rejects_item_missing_required_field(nl_schema, missing):
    item = _valid_item()
    del item[missing]
    assert list(Draft7Validator(nl_schema).iter_errors([item])), (
        f"schema accepted an item with no {missing}"
    )


def test_newsletter_schema_rejects_unknown_title_source(nl_schema):
    item = _valid_item(titleSource="magic")
    assert list(Draft7Validator(nl_schema).iter_errors([item]))


def test_newsletter_schema_rejects_malformed_edition_id(nl_schema):
    item = _valid_item(newsletterEdition="week of Sept 8")
    assert list(Draft7Validator(nl_schema).iter_errors([item]))


def test_newsletter_schema_accepts_a_well_formed_item(nl_schema):
    """Proves the rejection tests above are discriminating, not blanket failures."""
    assert list(Draft7Validator(nl_schema).iter_errors([_valid_item()])) == []


def test_newsletter_schema_shares_base_property_definitions(base_schema, nl_schema):
    """Closes the drift hazard between the two hand-maintained schema files."""
    base_props = base_schema["items"]["properties"]
    nl_props = nl_schema["items"]["properties"]
    for name, definition in base_props.items():
        assert name in nl_props, f"newsletter schema is missing property {name!r}"
        assert nl_props[name] == definition, f"property {name!r} has drifted"


def test_newsletter_schema_required_is_a_superset_of_base(base_schema, nl_schema):
    base_required = set(base_schema["items"]["required"])
    nl_required = set(nl_schema["items"]["required"])
    assert base_required < nl_required
    assert nl_required - base_required == {
        "titleSource", "titleIsPlaceholder", "newsletterEdition",
    }


def _iter_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _iter_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_keys(item)


def test_both_schemas_are_self_contained(base_schema, nl_schema):
    """tools/validate_json.py has no RefResolver, so a $ref would fail at runtime."""
    for schema in (base_schema, nl_schema):
        assert "$ref" not in set(_iter_keys(schema))
