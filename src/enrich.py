"""Event enrichment utilities.

Currently supports fetching an event subtitle (<div class="event-subtitle">)
from each event's detail page URL and inserting it into the `title` field.

Opt-in via CLI flag --enrich-titles or environment variable ENRICH_TITLES=1.
Network failures or parsing misses leave the existing title unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Optional
import os
import requests
from bs4 import BeautifulSoup  # type: ignore
try:  # optional dependency for Markdown conversion
    from markdownify import markdownify as _md
except (ImportError, ModuleNotFoundError):  # pragma: no cover - optional
    _md = None  # type: ignore


DEFAULT_TIMEOUT = 15


@dataclass
class TitleEnrichmentStats:
    attempted: int = 0
    updated: int = 0
    skipped_missing_url: int = 0
    errors: int = 0


@dataclass
class ContentEnrichmentStats:
    attempted: int = 0
    updated: int = 0
    skipped_missing_url: int = 0
    errors: int = 0


@dataclass
class RawDetailsEnrichmentStats:
    attempted: int = 0
    updated: int = 0
    skipped_missing_url: int = 0
    errors: int = 0


@dataclass
class RawExtractEnrichmentStats:
    attempted: int = 0
    updated_abstract: int = 0
    updated_bio: int = 0
    skipped_missing_details: int = 0
    errors: int = 0


def fetch_subtitle(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Fetch a page and return normalized subtitle text.

    Adds a desktop User-Agent to avoid 403 responses and collapses internal
    whitespace/newlines to single spaces.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        # Site-provided bypass header for bot protection (value optional)
        "x-wdsoit-bot-bypass": os.getenv("BOT_BYPASS_HEADER_VALUE", "1"),
    }
    debug = os.getenv("ENRICH_DEBUG") in {"1", "true", "yes", "on"}
    try:
        resp = requests.get(url, timeout=timeout, headers=headers)
    except Exception as e:
        if debug:
            print(f"[enrich] request-error url={url} err={e}")
        return ""
    try:
        resp.raise_for_status()
    except Exception as e:
        if debug:
            print(f"[enrich] bad-status url={url} code={getattr(resp,'status_code',None)} err={e}")
        return ""
    soup = BeautifulSoup(resp.text, "html.parser")
    div = soup.find("div", class_="event-subtitle")
    if not div:
        if debug:
            print(f"[enrich] subtitle-missing url={url} length={len(resp.text)}")
        return ""
    # get_text with separator to retain spacing, then collapse any runs
    raw = div.get_text(separator=" ", strip=True)
    normalized = " ".join(raw.split())
    if debug:
        print(f"[enrich] subtitle-found url={url} len={len(normalized)}")
    return normalized


def fetch_content_body(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Fetch a page and return main content as text/markdown/html.

        Behavior is controlled by ENRICH_CONTENT_FORMAT env var:
            - "text" (default): plain text with paragraphs separated by blank lines
            - "markdown": basic Markdown converted from the HTML fragment
            - "html": sanitized inner HTML fragment of the details section

        Extraction targets a few structures, prioritizing a container matching
        `.events-detail-main` (or `.event-details-main`) that has a header `.details`.
        Falls back to generic containers otherwise.
        """
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "x-wdsoit-bot-bypass": os.getenv("BOT_BYPASS_HEADER_VALUE", "1"),
    }
    debug = os.getenv("ENRICH_DEBUG") in {"1", "true", "yes", "on"}
    try:
        resp = requests.get(url, timeout=timeout, headers=headers)
    except Exception as e:
        if debug:
            print(f"[enrich] content request-error url={url} err={e}")
        return ""
    try:
        resp.raise_for_status()
    except Exception as e:
        if debug:
            print(f"[enrich] content bad-status url={url} code={getattr(resp,'status_code',None)} err={e}")
        return ""
    soup = BeautifulSoup(resp.text, "html.parser")

    # Determine desired output format
    fmt = (os.getenv("ENRICH_CONTENT_FORMAT", "text") or "text").lower()
    if fmt not in {"text", "markdown", "html"}:
        fmt = "text"

    # Remove scripts/styles regardless of format
    for bad in soup(["script", "style"]):
        bad.decompose()

    # 1) Preferred structure: details within events-detail-main
    container = soup.select_one("div.events-detail-main") or soup.select_one(
        "div.event-details-main"
    )
    fragment = None
    if container:
        header = container.select_one(".details")
        # The actual body is typically within the next significant div
        if header:
            # Look for a specific content wrapper after the header
            specific = (
                header.find_next("div", class_="tex2jax_process")
                or header.find_next("div", class_="field__item")
                or header.find_next("div", class_="field--name-field-ps-body")
                or header.find_next("div")
            )
            fragment = specific
        else:
            # Fallback to container itself
            fragment = container

    # 2) Generic fallbacks
    if fragment is None:
        fragment = (
            soup.find("div", class_="event-description")
            or soup.find("div", class_="event-body")
            or soup.find("div", class_="field--name-body")
            or soup.find("div", id="content-body")
            or soup.find("article")
        )

    if fragment is None:
        if debug:
            print(f"[enrich] content-missing url={url}")
        return ""

    # Serializer
    def _to_text(el) -> str:
        # keep paragraph separation, then collapse excessive blank lines
        raw = el.get_text(separator="\n\n", strip=True)
        # Normalize newlines and spaces
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        # Collapse consecutive blank lines to single
        out_lines = []
        prev_blank = False
        for ln in lines:
            is_blank = (ln == "")
            if is_blank and prev_blank:
                continue
            out_lines.append(ln)
            prev_blank = is_blank
        return "\n".join(out_lines).strip()

    def _to_markdown(el) -> str:
        html = str(el)
        if _md is not None:
            try:
                md = _md(
                    html,
                    heading_style="ATX",
                    strip=[],
                    convert=["br", "p", "h1", "h2", "h3", "h4", "h5", "h6", "a", "em", "strong", "ul", "ol", "li"],
                )
            except Exception:
                md = _to_text(el)
        else:
            md = _to_text(el)
        # Tidy: collapse >2 blank lines
        parts = [p.strip() for p in md.splitlines()]
        cleaned = []
        blank = 0
        for p in parts:
            if p == "":
                blank += 1
                if blank > 1:
                    continue
            else:
                blank = 0
            cleaned.append(p)
        return "\n".join(cleaned).strip()

    def _to_html(el) -> str:
        # Return inner HTML of the fragment
        # Avoid returning the wrapper tag; focus on its contents
        try:
            return "".join(str(c) for c in el.contents).strip()
        except Exception:
            return str(el)

    if fmt == "html":
        body = _to_html(fragment)
    elif fmt == "markdown":
        body = _to_markdown(fragment)
    else:
        body = _to_text(fragment)

    if debug:
        print(f"[enrich] content-found url={url} fmt={fmt} len={len(body)}")
    return body


def fetch_raw_details_html(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Fetch a page and return inner HTML of <div class="events-detail-main">.

    Falls back to <div class="event-details-main"> if the primary class is not found.
    Returns an empty string if neither is present or on error.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "x-wdsoit-bot-bypass": os.getenv("BOT_BYPASS_HEADER_VALUE", "1"),
    }
    debug = os.getenv("ENRICH_DEBUG") in {"1", "true", "yes", "on"}
    try:
        resp = requests.get(url, timeout=timeout, headers=headers)
        resp.raise_for_status()
    except Exception as e:
        if debug:
            print(f"[enrich] raw-details request-error url={url} err={e}")
        return ""
    soup = BeautifulSoup(resp.text, "html.parser")
    container = soup.select_one("div.events-detail-main") or soup.select_one("div.event-details-main")
    if not container:
        if debug:
            print(f"[enrich] raw-details missing container url={url}")
        return ""
    try:
        html = "".join(str(c) for c in container.contents).strip()
    except Exception:
        html = str(container)
    if debug:
        print(f"[enrich] raw-details found url={url} len={len(html)}")
    return html


def extract_abstract_from_raw_details(raw_html: str) -> str:
    """Extract abstract content from raw event details HTML.

    Looks for content following "Abstract:" or <h*>Abstract</h*> headers,
    up to the end of the enclosing div or the next header.
    """
    if not raw_html or not raw_html.strip():
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")

    # Find abstract marker - try different patterns
    abstract_marker = None

    # Pattern 1: Look for "Abstract:" text (handles both "Abstract:" and "<strong>Abstract</strong>:")
    # First try to find exact "Abstract:" in text nodes
    for element in soup.find_all(string=lambda text: text and "Abstract:" in text):
        if "Abstract:" in element.strip():
            abstract_marker = element.parent
            break

    # If not found, look for "Abstract" followed by ":" (possibly separated by HTML tags)
    if not abstract_marker:
        for element in soup.find_all(string=lambda text: text and "Abstract" in text.strip()):
            # Check if this element contains "Abstract" (case insensitive)
            if "abstract" in element.strip().lower():
                # Find the closest block-level parent that might contain the full "Abstract:" text
                parent = element.parent
                while parent and parent.name not in ['p', 'div', 'section', 'article']:
                    parent = parent.parent
                
                if parent:
                    # Get all text from the parent element
                    parent_text = parent.get_text(strip=True)
                    if "abstract:" in parent_text.lower():
                        abstract_marker = parent
                        break

    # Pattern 2: Look for header tags containing "Abstract"
    if not abstract_marker:
        for header in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
            if header.get_text(strip=True).lower() == "abstract":
                abstract_marker = header
                break

    if not abstract_marker:
        return ""

    # Extract content from abstract marker until next header or end
    content_parts = []

    # Start from the element right after the marker
    if abstract_marker.name and abstract_marker.name.startswith('h'):
        # For header markers, start from next sibling
        current = abstract_marker.next_sibling
    else:
        # For text markers like "Abstract:", extract from the same element
        text_content = abstract_marker.get_text(strip=True)
        if "abstract:" in text_content.lower():
            # Extract text after "Abstract:"
            lower_text = text_content.lower()
            abstract_pos = lower_text.find("abstract:")
            if abstract_pos != -1:
                after_marker = text_content[abstract_pos + 9:].strip()  # 9 = len("abstract:")
                if after_marker:
                    content_parts.append(after_marker)
        # For colon markers, don't continue with siblings since content is in same element
        current = None

    # Collect content until we hit another header
    while current:
        # Stop if we hit any header
        if hasattr(current, 'name') and current.name and current.name.startswith('h'):
            break

        # Add text content
        if hasattr(current, 'get_text'):
            text = current.get_text(strip=True)
            if text:
                content_parts.append(text)

        current = current.next_sibling

    return " ".join(content_parts).strip()


def extract_bio_from_raw_details(raw_html: str) -> str:
    """Extract bio content from raw event details HTML.

    Looks for content following "Bio:" or <h*>Bio</h*> headers,
    up to the end of the enclosing div or the next header.
    """
    if not raw_html or not raw_html.strip():
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")

    # Find bio marker - try different patterns
    bio_marker = None

    # Pattern 1: Look for "Bio:" text (handles both "Bio:" and "<b>Bio</b>:")
    # First try to find exact "Bio:" in text nodes
    for element in soup.find_all(string=lambda text: text and "Bio:" in text):
        if "Bio:" in element.strip():
            bio_marker = element.parent
            break

    # If not found, look for "Bio" followed by ":" (possibly separated by HTML tags)
    if not bio_marker:
        for element in soup.find_all(string=lambda text: text and "Bio" in text.strip()):
            # Check if this element contains "Bio" (case insensitive)
            if "bio" in element.strip().lower():
                # Find the closest block-level parent that might contain the full "Bio:" text
                parent = element.parent
                while parent and parent.name not in ['p', 'div', 'section', 'article']:
                    parent = parent.parent
                
                if parent:
                    # Get all text from the parent element
                    parent_text = parent.get_text(strip=True)
                    if "bio:" in parent_text.lower():
                        bio_marker = parent
                        break

    # Pattern 2: Look for header tags containing "Bio"
    if not bio_marker:
        for header in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
            if header.get_text(strip=True).lower() == "bio":
                bio_marker = header
                break

    if not bio_marker:
        return ""

    # Extract content from bio marker until next header or end
    content_parts = []

    # Start from the element right after the marker
    if bio_marker.name and bio_marker.name.startswith('h'):
        # For header markers, start from next sibling
        current = bio_marker.next_sibling
    else:
        # For text markers like "Bio:", extract from the same element
        text_content = bio_marker.get_text(strip=True)
        if "bio:" in text_content.lower():
            # Extract text after "Bio:"
            lower_text = text_content.lower()
            bio_pos = lower_text.find("bio:")
            if bio_pos != -1:
                after_marker = text_content[bio_pos + 4:].strip()  # 4 = len("bio:")
                if after_marker:
                    content_parts.append(after_marker)
        # For colon markers, don't continue with siblings since content is in same element
        current = None

    # Collect content until we hit another header
    while current:
        # Stop if we hit any header
        if hasattr(current, 'name') and current.name and current.name.startswith('h'):
            break

        # Add text content
        if hasattr(current, 'get_text'):
            text = current.get_text(strip=True)
            if text:
                content_parts.append(text)

        current = current.next_sibling

    return " ".join(content_parts).strip()


def enrich_titles(events: List[Dict], enable: bool, session_cache: Optional[Dict[str, str]] = None, overwrite: bool = False) -> TitleEnrichmentStats:
    """Mutate events list in-place adding subtitle to 'title' when available.

    Debugging:
        Set ENRICH_DEBUG=1 to emit detailed skip/update logging to stdout.

    Args:
        events: list of event dicts with 'urlRef'.
        enable: if False, no-op.
        session_cache: optional dict for caching url->subtitle.
    Returns:
        TitleEnrichmentStats summarizing operation.
    """
    stats = TitleEnrichmentStats()
    if not enable:
        return stats
    cache = session_cache if session_cache is not None else {}
    debug = os.getenv("ENRICH_DEBUG") in {"1", "true", "yes", "on"}
    for idx, ev in enumerate(events):
        url = ev.get("urlRef") or ""
        if not url:
            stats.skipped_missing_url += 1
            if debug:
                print(f"[enrich] skip(no-url) event_index={idx}")
            continue
        stats.attempted += 1
        if url in cache:
            subtitle = cache[url]
            if debug:
                print(f"[enrich] cache-hit url={url} subtitle_len={len(subtitle)}")
        else:
            try:
                subtitle = fetch_subtitle(url)
            except Exception as e:
                stats.errors += 1
                cache[url] = ""
                if debug:
                    print(f"[enrich] error fetching url={url} err={e}")
                continue
            cache[url] = subtitle
            if debug:
                print(f"[enrich] fetched url={url} subtitle_len={len(subtitle)}")
        if not subtitle:
            if debug:
                print(f"[enrich] skip(no-subtitle) url={url}")
            continue
        existing = ev.get("title")
        # Decide whether to overwrite
        should_overwrite = overwrite or existing is None or str(existing).strip() == ""
        if should_overwrite:
            ev["title"] = subtitle
            stats.updated += 1
            if debug:
                action = "overwrote" if (existing and overwrite) else "updated"
                print(f"[enrich] {action} url={url} new_title_len={len(subtitle)}")
        else:
            if debug:
                snippet = str(existing)[:40].replace('\n', ' ')
                print(f"[enrich] skip(has-title) url={url} existing_snippet={snippet!r} overwrite={overwrite}")
    return stats


def enrichment_enabled(cli_flag: bool) -> bool:
    if cli_flag:
        return True
    return os.getenv("ENRICH_TITLES", "0") in {"1", "true", "yes", "on"}


def enrichment_overwrite_enabled(cli_flag: bool) -> bool:
    if cli_flag:
        return True
    return os.getenv("ENRICH_OVERWRITE", "0") in {"1", "true", "yes", "on"}


def fill_title_fallback(events: List[Dict], overwrite: bool = False, include_speaker: bool = True) -> int:
    """Fill missing/TBD titles using FALLBACK_PREPEND_TEXT and optionally the speaker field.

    Behavior:
    - Treats title values that are empty/whitespace or case-insensitive 'TBD' as missing.
    - When `overwrite` is False (default), only fills when missing as defined above.
    - When `overwrite` is True, replaces any existing title with the generated value.
    - When `include_speaker` is True (default), appends speaker to the prefix template.
    - When `include_speaker` is False, uses only the rendered FALLBACK_PREPEND_TEXT
      (e.g., "A {series} Talk" without the speaker name).
    - Guarantees a non-empty title: when neither the speaker nor a usable template
      is available, derives a last-resort title from the event's series
      (e.g., "An Optimization Seminar Talk"), or "A Seminar Talk" without a series.

    Returns the number of events whose title was set.
    """
    def _is_missing(title_val: object | None) -> bool:
        if title_val is None:
            return True
        s = str(title_val).strip()
        if not s:
            return True
        return s.lower() == "tbd"

    # Optional prefix for titles derived from speaker. Supports basic placeholders
    # like {series} sourced from the event dict. Missing keys render as empty string.
    # Special placeholder {a_an} auto-selects "A" or "An" based on next word.
    class _SafeDict(dict):
        def __missing__(self, key):  # noqa: D401
            return ""

    _A_AN_MARKER = "\x00A_AN\x00"

    def _resolve_a_an(text: str) -> str:
        """Replace {a_an} markers with 'A' or 'An' based on the following word."""
        import re
        def _replace(m: re.Match) -> str:
            rest = text[m.end():]
            # Find the next word character
            next_match = re.match(r"\s*(\w)", rest)
            if next_match:
                first_char = next_match.group(1).lower()
                # Use "An" for vowel sounds (simplified: just vowel letters)
                if first_char in "aeiou":
                    return "An"
            return "A"
        return re.sub(re.escape(_A_AN_MARKER), _replace, text)

    MAX_PREFIX_LEN = 128  # increased from 64 to allow richer templates
    raw_prefix_tmpl = os.getenv("FALLBACK_PREPEND_TEXT", "")
    raw_prefix_tmpl = raw_prefix_tmpl if isinstance(raw_prefix_tmpl, str) else ""

    def _last_resort_title(ev: Dict) -> str:
        """Series-derived title used when no speaker or template can produce one."""
        series = str(ev.get("series") or "").split(",")[0].strip()
        text = " ".join(f"{_A_AN_MARKER} {series} Talk".split()) if series else "A Seminar Talk"
        return _resolve_a_an(text)

    count = 0
    for ev in events:
        existing = ev.get("title")
        if not (overwrite or _is_missing(existing)):
            continue

        # Render prefix template with event fields (e.g., {series}) and
        # collapse whitespace so blanks (like empty series) don't leave doubles.
        # Handle {a_an} placeholder specially for A/An selection.
        prefix_rendered = ""
        if raw_prefix_tmpl:
            # Preserve {a_an} by replacing with marker before format_map
            tmpl = raw_prefix_tmpl.replace("{a_an}", _A_AN_MARKER)
            try:
                prefix_rendered = tmpl.format_map(_SafeDict(ev))
            except Exception:
                # On formatting error, fall back to raw literal
                prefix_rendered = tmpl
            prefix_rendered = " ".join(prefix_rendered.split())
            # Resolve A/An based on next word
            prefix_rendered = _resolve_a_an(prefix_rendered)

        speaker = ev.get("speaker") if include_speaker else None
        use_prefix = bool(prefix_rendered) and len(prefix_rendered) < MAX_PREFIX_LEN
        if speaker:
            speaker_str = str(speaker)
            ev["title"] = (
                f"{prefix_rendered} {speaker_str}" if use_prefix else speaker_str
            )
        elif use_prefix:
            # No speaker to append: use the template alone, stripping a trailing
            # "by" (case-insensitive) since no name follows.
            title = prefix_rendered.rstrip()
            if title.lower().endswith(" by"):
                title = title[:-3].rstrip()
            ev["title"] = title
        else:
            # No speaker and no usable template: never leave a title empty.
            ev["title"] = _last_resort_title(ev)
        count += 1
    return count


def fallback_include_speaker_enabled(cli_flag: bool | None = None) -> bool:
    """Check if fallback titles should include the speaker name.

    Default is True (include speaker) for backwards compatibility.
    Set FALLBACK_INCLUDE_SPEAKER=0 or --no-fallback-speaker to disable.
    """
    if cli_flag is not None:
        return cli_flag
    val = os.getenv("FALLBACK_INCLUDE_SPEAKER", "1")
    return val.lower() in {"1", "true", "yes", "on"}


def enrichment_content_enabled(cli_flag: bool) -> bool:
    if cli_flag:
        return True
    return os.getenv("ENRICH_CONTENT", "0") in {"1", "true", "yes", "on"}


def enrichment_content_overwrite_enabled(cli_flag: bool) -> bool:
    if cli_flag:
        return True
    return os.getenv("ENRICH_CONTENT_OVERWRITE", "0") in {"1", "true", "yes", "on"}


def enrichment_raw_details_enabled(cli_flag: bool) -> bool:
    if cli_flag:
        return True
    return os.getenv("ENRICH_RAW_DETAILS", "0") in {"1", "true", "yes", "on"}


def enrichment_raw_details_overwrite_enabled(cli_flag: bool) -> bool:
    if cli_flag:
        return True
    return os.getenv("ENRICH_RAW_DETAILS_OVERWRITE", "0") in {"1", "true", "yes", "on"}


def enrichment_raw_extracts_enabled(cli_flag: bool) -> bool:
    if cli_flag:
        return True
    return os.getenv("ENRICH_RAW_EXTRACTS", "1") in {"1", "true", "yes", "on"}  # enabled by default


def enrichment_raw_extracts_overwrite_enabled(cli_flag: bool) -> bool:
    if cli_flag:
        return True
    return os.getenv("ENRICH_RAW_EXTRACTS_OVERWRITE", "0") in {"1", "true", "yes", "on"}


def enrich_content(
    events: List[Dict],
    enable: bool,
    session_cache: Optional[Dict[str, str]] = None,
    overwrite: bool = False,
) -> ContentEnrichmentStats:
    """Optionally replace event 'content' with scraped page content.

    By default, does not overwrite non-empty content unless `overwrite=True`.
    """
    stats = ContentEnrichmentStats()
    if not enable:
        return stats
    cache = session_cache if session_cache is not None else {}
    debug = os.getenv("ENRICH_DEBUG") in {"1", "true", "yes", "on"}
    for idx, ev in enumerate(events):
        url = ev.get("urlRef") or ""
        if not url:
            stats.skipped_missing_url += 1
            if debug:
                print(f"[enrich] content skip(no-url) event_index={idx}")
            continue
        stats.attempted += 1
        if url in cache:
            body = cache[url]
            if debug:
                print(f"[enrich] content cache-hit url={url} body_len={len(body)}")
        else:
            try:
                body = fetch_content_body(url)
            except Exception as e:
                stats.errors += 1
                body = ""
                cache[url] = body
                if debug:
                    print(f"[enrich] content error fetching url={url} err={e}")
                continue
            cache[url] = body
            if debug:
                print(f"[enrich] content fetched url={url} body_len={len(body)}")
        if not body:
            if debug:
                print(f"[enrich] content skip(no-body) url={url}")
            continue
        existing = ev.get("content")
        should_overwrite = overwrite or existing is None or str(existing).strip() == ""
        if should_overwrite:
            ev["content"] = body
            stats.updated += 1
            if debug:
                action = "overwrote" if (existing and overwrite) else "updated"
                print(f"[enrich] content {action} url={url} new_len={len(body)}")
        else:
            if debug:
                print(f"[enrich] content skip(has-content) url={url} overwrite={overwrite}")
    return stats


def enrich_raw_details(
    events: List[Dict],
    enable: bool,
    session_cache: Optional[Dict[str, str]] = None,
    overwrite: bool = False,
) -> RawDetailsEnrichmentStats:
    """Optionally add 'rawEventDetails' containing inner HTML of events-detail-main.

    By default, does not overwrite non-empty values unless `overwrite=True`.
    """
    stats = RawDetailsEnrichmentStats()
    if not enable:
        return stats
    cache = session_cache if session_cache is not None else {}
    debug = os.getenv("ENRICH_DEBUG") in {"1", "true", "yes", "on"}
    for idx, ev in enumerate(events):
        url = ev.get("urlRef") or ""
        if not url:
            stats.skipped_missing_url += 1
            if debug:
                print(f"[enrich] raw-details skip(no-url) event_index={idx}")
            continue
        stats.attempted += 1
        if url in cache:
            html = cache[url]
            if debug:
                print(f"[enrich] raw-details cache-hit url={url} len={len(html)}")
        else:
            try:
                html = fetch_raw_details_html(url)
            except Exception as e:
                stats.errors += 1
                html = ""
                cache[url] = html
                if debug:
                    print(f"[enrich] raw-details error fetching url={url} err={e}")
                continue
            cache[url] = html
            if debug:
                print(f"[enrich] raw-details fetched url={url} len={len(html)}")
        if not html:
            if debug:
                print(f"[enrich] raw-details skip(no-html) url={url}")
            continue
        existing = ev.get("rawEventDetails")
        should_overwrite = overwrite or existing is None or str(existing).strip() == ""
        if should_overwrite:
            ev["rawEventDetails"] = html
            stats.updated += 1
            if debug:
                action = "overwrote" if (existing and overwrite) else "updated"
                print(f"[enrich] raw-details {action} url={url} new_len={len(html)}")
        else:
            if debug:
                print(f"[enrich] raw-details skip(has-value) url={url} overwrite={overwrite}")
    return stats


def enrich_raw_extracts(
    events: List[Dict],
    enable: bool,
    overwrite: bool = False,
) -> RawExtractEnrichmentStats:
    """Extract abstract and bio from rawEventDetails into separate fields.

    Adds 'rawExtractAbstract' and 'rawExtractBio' fields when rawEventDetails
    contains valid HTML with abstract/bio sections.

    By default, does not overwrite existing values unless `overwrite=True`.
    """
    stats = RawExtractEnrichmentStats()
    if not enable:
        return stats

    debug = os.getenv("ENRICH_DEBUG") in {"1", "true", "yes", "on"}

    for idx, ev in enumerate(events):
        raw_details = ev.get("rawEventDetails") or ""
        if not raw_details or not raw_details.strip():
            stats.skipped_missing_details += 1
            if debug:
                print(f"[enrich] raw-extract skip(no-details) event_index={idx}")
            continue

        stats.attempted += 1

        # Extract abstract
        existing_abstract = ev.get("rawExtractAbstract")
        should_overwrite_abstract = overwrite or existing_abstract is None or str(existing_abstract).strip() == ""
        if should_overwrite_abstract:
            try:
                abstract = extract_abstract_from_raw_details(raw_details)
                if abstract:
                    ev["rawExtractAbstract"] = abstract
                    stats.updated_abstract += 1
                    if debug:
                        print(f"[enrich] raw-extract abstract updated event_index={idx} len={len(abstract)}")
            except Exception as e:
                stats.errors += 1
                if debug:
                    print(f"[enrich] raw-extract abstract error event_index={idx} err={e}")

        # Extract bio
        existing_bio = ev.get("rawExtractBio")
        should_overwrite_bio = overwrite or existing_bio is None or str(existing_bio).strip() == ""
        if should_overwrite_bio:
            try:
                bio = extract_bio_from_raw_details(raw_details)
                if bio:
                    ev["rawExtractBio"] = bio
                    stats.updated_bio += 1
                    if debug:
                        print(f"[enrich] raw-extract bio updated event_index={idx} len={len(bio)}")
            except Exception as e:
                stats.errors += 1
                if debug:
                    print(f"[enrich] raw-extract bio error event_index={idx} err={e}")

    return stats
