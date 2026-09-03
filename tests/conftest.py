# Ensure project root is on sys.path for imports when running pytest directly.
import os
import sys
from pathlib import Path

import pytest

root = Path(__file__).resolve().parents[1]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

#: Environment variables that steer transform, enrichment, provenance and the
#: newsletter schedule. A developer's exported shell value would otherwise leak
#: into assertions and make failures look like code regressions.
_ISOLATED_ENV_PREFIXES = ("NEWSLETTER_",)
_ISOLATED_ENV_NAMES = (
    "TARGET_TZ",
    "TITLE_PROVENANCE",
    "FALLBACK_PREPEND_TEXT",
    "FALLBACK_INCLUDE_SPEAKER",
    "EXCLUDE_SERIES",
    "ENRICH_TITLES",
    "ENRICH_OVERWRITE",
    "ENRICH_DEBUG",
    "OUTPUT_FILE",
)


@pytest.fixture(autouse=True)
def isolate_pipeline_env(monkeypatch):
    """Clear pipeline-steering env vars for the duration of each test."""
    for name in list(os.environ):
        if name.startswith(_ISOLATED_ENV_PREFIXES) or name in _ISOLATED_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
