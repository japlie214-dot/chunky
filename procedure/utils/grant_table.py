"""
procedure/utils/grant_table.py
Python helper for table grants used by the ingestion procedure.

The helper is bundled with the main procedure handlers.

Grants ALL PRIVILEGES on a table to the requested roles. Roles that fail
the Snowflake identifier regex are skipped. IT_AI is intentionally skipped
because the procedure creator already has that role.

Returns:
  {
    'success': bool,
    'granted': [...],
    'rejected': [...],
    'failed': [...],
    'query_ids': [...],
  }
"""
from __future__ import annotations
import re
from typing import Dict, List, Any

from .query_log import QueryLog

# Snowflake role identifier regex — same pattern as the original SQL SP.
_ROLE_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_$]*$", re.IGNORECASE)


def _qualify(db: str, schema: str, table_name: str) -> str:
    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table_name.replace('"', '""')
    return f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'


def _normalise_role(raw: str) -> str:
    """Return a Snowflake-safe quoted role name, or '' if invalid."""
    if not raw:
        return ""
    r = str(raw).strip()
    if not r:
        return ""
    if not _ROLE_PATTERN.match(r):
        return ""  # caller-visible rejection is assembled by run()
    return '"' + r.upper().replace('"', '""') + '"'


def run(session, db: str, schema: str, table_name: str, roles: Any) -> Dict:
    """
    Grant ALL PRIVILEGES on `table_name` to each role in `roles`.

    `roles` may be a Snowflake ARRAY, a Python list, or a JSON string.
    """
    log = QueryLog(session)
    full_table = _qualify(db, schema, table_name)

    # Normalise roles to a Python list of strings
    if isinstance(roles, str):
        import json
        try:
            roles = json.loads(roles)
        except Exception:
            roles = [roles]
    if not isinstance(roles, (list, tuple)):
        # Snowflake ARRAY comes through as a list-like; coerce defensively.
        try:
            roles = list(roles)
        except Exception:
            roles = []

    granted: List[str] = []
    rejected: List[dict] = []
    failed: List[dict] = []

    for raw in roles:
        safe_role = _normalise_role(raw)
        if not safe_role:
            if raw and str(raw).strip():
                rejected.append({"role": str(raw), "reason": "not a valid Snowflake identifier"})
            continue

        grant_sql = (
            f"GRANT ALL PRIVILEGES ON TABLE {full_table} TO ROLE {safe_role}"
        )
        ok = False
        # Two attempts (matches the original SQL retry loop)
        for _ in range(2):
            try:
                log.execute(grant_sql)
                ok = True
                break
            except Exception:
                continue

        if ok:
            granted.append(safe_role.strip('"'))
        else:
            failed.append({"role": safe_role.strip('"'),
                           "reason": "GRANT failed; check caller privileges on the table"})

    return {
        "success": not failed,
        "granted": granted,
        "rejected": rejected,
        "failed": failed,
        **log.to_dict(),
    }
