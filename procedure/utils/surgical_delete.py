"""
procedure/utils/surgical_delete.py
Python handler for the `chunky_internal_surgical_delete` stored procedure.

Converted from the original SQL procedure (`procedure/sub/chunky_internal_surgical_delete.sql`).

Deletes rows in the target chunk table for each (source_start, source_end)
range, sorting bottom-up (highest source_end first) so multi-range shifts
do not invalidate each other. The entire batch is wrapped in an explicit
BEGIN / COMMIT / ROLLBACK transaction for atomicity.

Returns:
  {'success': True,  'deleted_ranges': [...], 'query_ids': [...]}
  {'success': False, 'error': '...',          'query_ids': [...]}
"""
from __future__ import annotations
from typing import Dict, Any, List

from .query_log import QueryLog


def _qualify(db: str, schema: str, table_name: str) -> str:
    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table_name.replace('"', '""')
    return f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'


def run(session, db: str, schema: str, table_name: str,
        file: str, range_mappings: Any) -> Dict:
    """
    Execute surgical deletes for the supplied range mappings.

    `range_mappings` may be a Snowflake ARRAY, a Python list of dicts, or a
    JSON string. Each entry must have integer keys `source_start` and
    `source_end`.
    """
    log = QueryLog(session)
    full_table = _qualify(db, schema, table_name)
    safe_file = (file or "").replace("'", "''")

    # Normalise range_mappings to a Python list of dicts
    if isinstance(range_mappings, str):
        import json
        try:
            range_mappings = json.loads(range_mappings)
        except Exception:
            range_mappings = []
    if not isinstance(range_mappings, (list, tuple)):
        try:
            range_mappings = list(range_mappings)
        except Exception:
            range_mappings = []

    # Sort bottom-up (highest source_end first)
    try:
        sorted_mappings = sorted(
            range_mappings,
            key=lambda m: int(m.get("source_end", 0)),
            reverse=True,
        )
    except Exception as e:
        return {
            "success": False,
            "error": f"Invalid range_mappings: {e}",
            **log.to_dict(),
        }

    deleted_ranges: List[Dict] = []
    log.execute("BEGIN")

    try:
        for rm in sorted_mappings:
            try:
                src_start = int(rm.get("source_start"))
                src_end = int(rm.get("source_end"))
            except Exception as e:
                raise ValueError(f"Bad range mapping {rm}: {e}")

            delete_sql = (
                f"DELETE FROM {full_table} "
                f"WHERE RELATIVE_PATH = '{safe_file}' "
                f"AND PAGE_NUMBER BETWEEN {src_start} AND {src_end}"
            )
            log.execute(delete_sql)
            deleted_ranges.append(
                {"source_start": src_start, "source_end": src_end}
            )

        log.execute("COMMIT")
        return {
            "success": True,
            "deleted_ranges": deleted_ranges,
            **log.to_dict(),
        }
    except Exception as e:
        try:
            log.execute("ROLLBACK")
        except Exception:
            pass
        return {
            "success": False,
            "error": str(e),
            "deleted_ranges_attempted": deleted_ranges,
            **log.to_dict(),
        }
