"""
procedure/utils/build_chunk_ref.py
Python handler for the `chunky_internal_build_chunk_ref` stored procedure.

Converted from the original SQL procedure
(`procedure/sub/chunky_internal_build_chunk_ref.sql`).

Returns the canonical CHUNK_REF string:
  [Digital Copy](<url-encoded link>) | Doc Source: <path> | Page Num: <n>
or, when no link is supplied:
  Doc Source: <path> | Page Num: <n>

No Snowflake session is required — this is a pure function. It still
returns a dict so its shape matches the other handlers; the value lives
under the `chunk_ref` key.
"""
from __future__ import annotations
import urllib.parse
from typing import Dict


def _encode_link(link: str) -> str:
    """URL-encode a hyperlink for safe embedding in a Markdown URL."""
    if not link:
        return ""
    # Encode characters that break Markdown URL syntax or have special
    # meaning in URLs. Mirrors the REPLACE chain in the original SQL.
    return urllib.parse.quote(link, safe=":/?#&=@")


def build(rel_path: str, page_num: int, link: str = "") -> str:
    """Pure-function form — usable directly by other handlers."""
    base = f"Doc Source: {rel_path} | Page Num: {page_num}"
    if not link:
        return base
    return f"[Digital Copy]({_encode_link(link)}) | {base}"


def run(rel_path: str, page_num: int, link: str = "") -> Dict:
    """Handler form — matches the call signature of other utils."""
    return {"chunk_ref": build(rel_path, page_num, link)}
