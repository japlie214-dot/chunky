"""
procedure/utils/init_table.py
Python helper for initializing the ingestion table.

Converted to a Python helper so the main procedure keeps its logic in the
same deployment story (single IMPORTS zip, consistent error handling, and
query-ID capture for revert support).

Behaviour is identical to the SQL original:
- OVERWRITE mode  -> CREATE OR REPLACE TABLE ... COPY GRANTS
- APPEND/SURGICAL -> CREATE TABLE IF NOT EXISTS (no-op when the table exists)

Returns one of:
  {'status': 'CREATED', 'table': '<full_table>'}
  {'status': 'EXISTS',  'table': '<full_table>'}
  {'status': 'ERROR',   'error': '...'}
"""
from __future__ import annotations
from typing import Dict

from .query_log import QueryLog


def _qualify(db: str, schema: str, table_name: str) -> str:
    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table_name.replace('"', '""')
    return f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'


def _columns_ddl() -> str:
    """Single source of truth for the chunk table column list."""
    return (
        "RELATIVE_PATH VARCHAR, "
        "PAGE_NUMBER NUMBER, "
        "CHUNK VARCHAR, "
        "CHUNK_ID VARCHAR, "
        "CHUNK_TYPE VARCHAR DEFAULT 'STANDARD', "
        "CHUNK_REF VARCHAR, "
        "LINK_BLOCK VARCHAR, "
        "CHUNK_METADATA VARIANT"
    )


def run(session, db: str, schema: str, table_name: str, mode: str) -> Dict:
    """
    Create (or replace) the target chunk table.

    Parameters mirror the original SQL procedure signature so existing
    CALL statements continue to work unchanged.
    """
    log = QueryLog(session)
    full_table = _qualify(db, schema, table_name)
    mode_upper = (mode or "APPEND").upper()

    # Check existence
    exists = False
    try:
        rows = log.execute(
            "SELECT COUNT(*) AS CNT FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_CATALOG = ? AND TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            params=[db, schema, table_name],
        )
        exists = bool(rows and int(rows[0]["CNT"]) > 0)
    except Exception:
        exists = False

    if mode_upper == "OVERWRITE" or not exists:
        cmd = "CREATE OR REPLACE" if mode_upper == "OVERWRITE" else "CREATE"
        grants = " COPY GRANTS" if mode_upper == "OVERWRITE" else ""
        init_sql = (
            f"{cmd} TABLE {full_table} ({_columns_ddl()}) "
            f"CHANGE_TRACKING = TRUE{grants}"
        )
        try:
            log.execute(init_sql)
            return {
                "status": "CREATED",
                "table": full_table,
                "mode": mode_upper,
                **log.to_dict(),
            }
        except Exception as e:
            return {"status": "ERROR", "error": str(e), **log.to_dict()}

    # Table exists and mode is APPEND/SURGICAL — no-op
    return {
        "status": "EXISTS",
        "table": full_table,
        "mode": mode_upper,
        **log.to_dict(),
    }
