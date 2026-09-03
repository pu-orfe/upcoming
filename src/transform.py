"""ICS event transformation utilities.

Provides configurable mapping, masking, placeholder injection, and
time/location normalization for calendar events.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set, Iterable, Any
import os
import re
import json
from pathlib import Path
import arrow  # type: ignore

from .placeholders import TitleSource, is_missing_title, mark_title_source


@dataclass
class TransformConfig:
    target_timezone: str = os.getenv("TARGET_TZ", "America/New_York")
    time_format: str = "YYYY-MM-DDTHH:mm:ss"
    field_mappings: Dict[str, str] = field(
        default_factory=lambda: {
            "uid": "guid",
            "begin": "startTime",
            "end": "endTime",
            "url": "urlRef",
            "categories": "series",
            "description": "content",
            "name": "speaker",
        }
    )
    masked_fields: Set[str] = field(
        default_factory=lambda: {"dtstamp", "sequence", "transp", "class"}
    )
    placeholders: Dict[str, str] = field(
        default_factory=lambda: {
            "title": "",
            "cancelled": "",
            "bannerImage": "",
            "itemType": "advertisement",
        }
    )
    copies: Dict[str, str] = field(default_factory=dict)
    # New configuration knobs
    join_categories: bool = True
    categories_delimiter: str = ","
    preserve_description_escapes: bool = True  # keep / add backslashes before , ;
    collapse_whitespace_in_description: bool = True
    # Control newline / fold representation in DESCRIPTION: space | literal_r | newline
    represent_newlines_as: str = "space"
    # Record titleSource/titleIsPlaceholder when the feed itself supplies a title
    mark_title_provenance: bool = True


def clean_text(value: str, collapse: bool = True) -> str:
    if not value:
        return ""
    value = value.replace("\r", "\n")
    if collapse:
        value = re.sub(r"\n+", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
    return value


def escape_commas(value: str) -> str:
    return re.sub(r"(?<!\\),", r"\\,", value)


def escape_semicolons(value: str) -> str:
    return re.sub(r"(?<!\\);", r"\\;", value)


def parse_location(raw: str | None) -> dict:
    if not raw:
        return {"name": "", "id": "", "detail": ""}
    parts = [p.strip() for p in raw.split("-", 1)]
    if len(parts) == 2:
        detail, name = parts
    else:
        detail = parts[0]
        name = ""
    return {"name": name, "id": "", "detail": detail}


def format_time(arrow_dt, cfg: TransformConfig) -> str:
    if arrow_dt is None:
        return ""
    try:
        localized = arrow_dt.to(cfg.target_timezone)
    except Exception:
        localized = arrow_dt
    return localized.format(cfg.time_format)


def transform_event(event, cfg: TransformConfig) -> dict:
    out: Dict[str, object] = {}

    # Map core fields
    for attr, target in cfg.field_mappings.items():
        if attr in cfg.masked_fields:
            continue
        val = getattr(event, attr, None)
        if val is None:
            continue
        if attr in {"begin", "end"}:
            out[target] = format_time(val, cfg)
        elif attr == "description":
            desc = str(val)
            if cfg.preserve_description_escapes:
                desc = escape_commas(escape_semicolons(desc))
            rep_mode = (cfg.represent_newlines_as or "space").lower()
            collapse = cfg.collapse_whitespace_in_description and rep_mode == "space"
            desc = clean_text(desc, collapse=collapse)
            if rep_mode == "literal_r":
                desc = desc.replace("\n", "\\r")
            elif rep_mode == "newline":
                # keep newlines
                pass
            elif rep_mode == "space":
                pass
            else:
                desc = clean_text(desc, collapse=True)
            out[target] = desc
        elif attr == "name":
            out[target] = escape_commas(str(val))
        elif attr == "categories":
            if isinstance(val, (set, list, tuple)):
                if cfg.join_categories:
                    out[target] = cfg.categories_delimiter.join(sorted(map(str, val)))
                else:
                    out[target] = next(iter(val), "")
            else:  # single string
                out[target] = str(val)
        else:
            out[target] = str(val)

    # Location parsing
    out["location"] = parse_location(getattr(event, "location", None))

    # Placeholders
    for k, v in cfg.placeholders.items():
        out.setdefault(k, v)

    # Copy fields
    for new_field, source_field in cfg.copies.items():
        if source_field in out:
            out[new_field] = out[source_field]

    # Title provenance: only a title the feed actually supplied counts as 'ics'.
    # Checked after `copies` so a copies entry that fills `title` is caught too;
    # the empty placeholder title seeded above is correctly left untagged.
    if cfg.mark_title_provenance and not is_missing_title(out.get("title")):
        mark_title_source(out, TitleSource.ICS)

    return out


def transform_calendar(calendar, cfg: TransformConfig | None = None) -> List[dict]:
    cfg = cfg or TransformConfig()
    events = [transform_event(ev, cfg) for ev in sorted(calendar.events, key=lambda e: e.begin or "")]
    return events


def load_config(path: str | os.PathLike | None) -> TransformConfig:
    """Load a TransformConfig from a JSON file if it exists; else defaults."""
    if not path:
        return TransformConfig()
    p = Path(path)
    if not p.exists():
        return TransformConfig()
    data: Dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    cfg = TransformConfig()
    for field_name in [
        "target_timezone",
        "time_format",
        "field_mappings",
        "masked_fields",
        "placeholders",
        "copies",
        "mark_title_provenance",
    ]:
        if field_name in data and data[field_name] is not None:
            setattr(cfg, field_name, data[field_name])
    if isinstance(cfg.masked_fields, list):  # type: ignore
        cfg.masked_fields = set(cfg.masked_fields)  # type: ignore
    return cfg
