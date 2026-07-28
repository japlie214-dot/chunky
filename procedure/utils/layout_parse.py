"""
procedure/utils/layout_parse.py
Pure helpers for parsing AI_PARSE_DOCUMENT responses.

AI_PARSE_DOCUMENT returns one of two shapes depending on whether the
caller passed `page_filter` in the options:

  Shape A (page_filter supplied):
      {"pages": [{"index": 0, "content": "..."}, ...], "metadata": {...}}

  Shape B (no page_filter — Full Doc scope):
      {"content": "whole document markdown", "metadata": {"pageCount": N, ...}}

In Shape B the content uses the form-feed character (\\f) as a page
separator. This module centralises the shape detection and the
form-feed split so the ingestion handler can treat both shapes
uniformly.
"""
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional, Tuple

from .constants import LAYOUT_PAGE_SEPARATOR


def parse_ai_parse_document_response(raw: Any) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], bool]:
    """
    Normalise an AI_PARSE_DOCUMENT response into a list of page dicts.

    Returns:
        (pages, metadata, used_form_feed_split)

        pages: list of {"index": int, "content": str}
        metadata: the metadata dict if present, else None
        used_form_feed_split: True if the response was flat and we split
            by form-feed to recover per-page content.
    """
    if raw is None:
        return [], None, False

    if isinstance(raw, str):
        try:
            doc = json.loads(raw)
        except Exception:
            return [], None, False
    elif isinstance(raw, dict):
        doc = raw
    else:
        return [], None, False

    metadata = doc.get("metadata")
    if isinstance(metadata, dict):
        pass
    else:
        metadata = None

    # Shape A — explicit pages array
    pages = doc.get("pages")
    if isinstance(pages, list) and pages:
        # Normalise each entry to {"index": int, "content": str}
        normalised: List[Dict[str, Any]] = []
        for i, pg in enumerate(pages):
            if not isinstance(pg, dict):
                continue
            idx = pg.get("index", i)
            try:
                idx_int = int(idx)
            except Exception:
                idx_int = i
            content = pg.get("content", "") or ""
            normalised.append({"index": idx_int, "content": content})
        if normalised:
            return normalised, metadata, False

    # Shape B — flat content with form-feed page separators
    content = doc.get("content")
    if isinstance(content, str) and content:
        splits = content.split(LAYOUT_PAGE_SEPARATOR)
        # Keep empty splits so page indices align with PDF page numbers,
        # but only emit pages that have non-whitespace content. Caller
        # fills missing pages with PLACEHOLDER chunks.
        normalised = []
        for i, c in enumerate(splits):
            if c and c.strip():
                normalised.append({"index": i, "content": c})
        if normalised:
            return normalised, metadata, True

        # All splits were whitespace-only — treat as empty.
        return [], metadata, True

    return [], metadata, False


def expected_pages_for_range(replacement_range: Optional[Tuple[int, int]],
                             page_range: Optional[Tuple[int, int]],
                             total_pages: int) -> set:
    """Compute the set of expected page numbers given the ingest scope."""
    if replacement_range:
        return set(range(replacement_range[0], replacement_range[1] + 1))
    if page_range:
        return set(range(page_range[0], page_range[1] + 1))
    return set(range(1, total_pages + 1))
