"""
procedure/utils/_shared.py
Pure helpers shared across the handler modules.

Centralising these eliminates the duplicated `_qualify`, `clean_text_for_sql`,
`sanitize_nbsp`, and `build_chunk_ref` definitions that previously lived in
every handler file. The handlers re-export them for backwards compatibility
with existing call sites.
"""
from __future__ import annotations
import re
import urllib.parse
import difflib
from typing import Any, Optional

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,254}$")
_STAGE = re.compile(r'^@[A-Za-z0-9_$.\"]+(/[A-Za-z0-9_.\-/ ]*)?$')


def qualify(db: str, schema: str, table: str) -> str:
    """Return a fully-qualified, double-quoted Snowflake table identifier."""
    safe_db = (db or "").replace('"', '""')
    safe_sch = (schema or "").replace('"', '""')
    safe_tbl = (table or "").replace('"', '""')
    return f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'


def safe_identifier(name: Any, what: str = "identifier") -> str:
    value = str(name or "").strip()
    if not _IDENT.match(value):
        raise ValueError(f"Invalid {what}: {name!r}. Must match [A-Za-z_][A-Za-z0-9_$]*.")
    return value


def safe_stage_path(stage: Any) -> str:
    value = str(stage or "").strip()
    if not _STAGE.match(value):
        raise ValueError(f"Invalid stage path: {stage!r}. Use @DB.SCHEMA.STAGE[/prefix].")
    return value


def require(inst: dict, *keys) -> tuple:
    missing = [key for key in keys if not inst.get(key)]
    if missing:
        raise ValueError(
            f"Missing required instruction field(s): {', '.join(missing)}. "
            "Every Chunky command requires an explicit 'db' and 'schema'."
        )
    return tuple(inst[key] for key in keys)


def clean_text_for_sql(text: str) -> str:
    """Escape single quotes and strip non-printable chars for SQL embedding."""
    if not text:
        return ""
    safe = text.replace("'", "''")
    return "".join(
        ch for ch in safe
        if ch.isprintable() or ch in ("\n", "\r", "\t")
    )


def sanitize_nbsp(text: str) -> str:
    """Replace HTML non-breaking-space entities with regular spaces."""
    if not text:
        return text
    return re.sub(r"&nbsp;|&#160;|&#x[aA]0;", " ", text)


def build_chunk_ref(rel_path: str, page_num: int, link: str = "") -> str:
    """Build the canonical CHUNK_REF string."""
    base = f"Doc Source: {rel_path} | Page Num: {page_num}"
    if not link:
        return base
    safe_link = urllib.parse.quote(link, safe=":/?#&=@")
    return f"[Digital Copy]({safe_link}) | {base}"


def safe_role(r: Any) -> Optional[str]:
    """Return a quoted, uppercase Snowflake role name, or None if invalid."""
    if not r:
        return None
    s = str(r).strip()
    if not s or not _IDENT.match(s):
        return None
    return '"' + s.upper().replace('"', '""') + '"'


def _envelope(success, command, data, error, log=None, warnings=None,
              revert=None, run_id=None, remedy=None, next_steps=None, extra=None):
    from . import __version__
    values = list(warnings or [])
    out = {
        "success": success, "command": command, "run_id": run_id,
        "data": data, "error": error, "remedy": remedy,
        "next": next_steps or [], "warning": " | ".join(values) if values else None,
        "warnings": values, "revert": revert, "bundle_version": __version__,
        "query_ids": [], "timestamp_before": None, "timestamp_after": None,
        "query_count": 0,
    }
    if log is not None:
        out.update(log.to_dict())
    if extra:
        out.update(extra)
    return out


def ok(command, data=None, *, log=None, warnings=None, revert=None,
       run_id=None, remedy=None, next_steps=None, extra=None):
    return _envelope(True, command, data, None, log, warnings, revert,
                     run_id, remedy, next_steps, extra)


def err(command, error, *, remedy=None, data=None, log=None, warnings=None,
        run_id=None, next_steps=None, extra=None):
    return _envelope(False, command, data, str(error), log, warnings, None,
                     run_id, remedy, next_steps, extra)


def format_link_block(urls) -> str:
    """Render a markdown link block from a list of URLs."""
    if not urls:
        return ""
    lines = "\n".join(f"  - {u}" for u in urls)
    return f"\n\n[External links:\n{lines}\n]"


def make_revert_command(proc_name: str, db: str, schema: str,
                        table: str, timestamp_before: Optional[str],
                        query_ids=None, extra_fields: Optional[dict] = None) -> str:
    """
    Build a ready-to-run CALL string for the REVERT command.

    Centralised so every handler produces the same shape and the procedure
    name is parameterised (no hardcoded 'chunky_chunks' literals scattered
    around the codebase).
    """
    parts = [
        f"'db', '{db}'",
        f"'schema', '{schema}'",
        f"'table', '{table}'",
        f"'timestamp_before', '{timestamp_before or ''}'",
    ]
    if query_ids:
        # Render as ARRAY_CONSTRUCT so the SQL string stays valid
        ids = ", ".join(f"'{i}'" for i in query_ids)
        parts.append(f"'query_ids', ARRAY_CONSTRUCT({ids})")
    if extra_fields:
        for k, v in extra_fields.items():
            if isinstance(v, str):
                parts.append(f"'{k}', '{v}'")
            elif isinstance(v, list):
                items = ", ".join(f"'{i}'" for i in v)
                parts.append(f"'{k}', ARRAY_CONSTRUCT({items})")
    body = ", ".join(parts)
    return f"CALL {proc_name}('REVERT', OBJECT_CONSTRUCT({body}));"
