"""
procedure/utils/revert.py
Native-Snowflake revert support.

Strategy: TIME TRAVEL
---------------------
Snowflake keeps every table change for the account's data-retention
window (default 24h on Standard Edition). We use that to "rewind" the
target table to a known-good timestamp.

Steps:
  1. Resolve the target timestamp. Callers may supply either:
       - `timestamp_before` (preferred — comes from the original
         operation's response), OR
       - `query_ids` — we look up START_TIME for each via
         INFORMATION_SCHEMA.QUERY_HISTORY() and take the MIN.
  2. Snapshot the current (corrupt) table to `<table>_revert_backup_<ts>`
     so the caller can inspect it if the revert goes wrong.
  3. Recreate the table from TIME TRAVEL:
       CREATE OR REPLACE TABLE <full_table> CLONE
         <full_table> AT(TIMESTAMP => '<ts>'::TIMESTAMP_LTZ)

Why CREATE OR REPLACE ... CLONE works:
  * CREATE OR REPLACE drops the existing table (snapshot kept by Time
    Travel) and creates a new one cloned from the time-travelled version.
  * The dropped table remains accessible via Time Travel for safety.

Returns a dict with the captured query_ids, the timestamp used, and the
backup table name.
"""
from __future__ import annotations
import time
from typing import Dict, Any, List, Optional

from .query_log import QueryLog
from .constants import TIME_TRAVEL_MAX_HOURS


def _qualify(db: str, schema: str, table_name: str) -> str:
    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table_name.replace('"', '""')
    return f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'


def _safe_ident(name: str) -> str:
    """Quote an identifier for use as a table name suffix."""
    return '"' + name.replace('"', '""') + '"'


def _resolve_timestamp(session, log: QueryLog,
                       timestamp_before: Optional[str],
                       query_ids: Optional[List[str]]) -> Optional[str]:
    """Return the earliest Snowflake timestamp for the revert operation."""
    if timestamp_before:
        return timestamp_before

    if not query_ids:
        return None

    # Query history for each query_id; take the earliest START_TIME.
    # INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER is preferred — it returns
    # recent queries without needing ACCOUNTADMIN.
    timestamps: List[str] = []
    for qid in query_ids:
        try:
            rows = log.execute(
                "SELECT START_TIME FROM TABLE("
                "INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER()) "
                "WHERE QUERY_ID = ?",
                params=[qid],
            )
            for r in rows:
                ts = r["START_TIME"]
                if ts:
                    timestamps.append(str(ts))
        except Exception:
            # Fall back to a simpler lookup if the by-user variant fails
            try:
                rows = log.execute(
                    "SELECT START_TIME FROM TABLE("
                    "INFORMATION_SCHEMA.QUERY_HISTORY()) "
                    "WHERE QUERY_ID = ?",
                    params=[qid],
                )
                for r in rows:
                    ts = r["START_TIME"]
                    if ts:
                        timestamps.append(str(ts))
            except Exception:
                continue

    if not timestamps:
        return None
    return min(timestamps)


def revert_table(session, db: str, schema: str, table_name: str,
                 timestamp_before: Optional[str] = None,
                 query_ids: Optional[List[str]] = None,
                 create_backup: bool = True) -> Dict:
    """
    Revert a chunk table to a previous state using TIME TRAVEL.

    Either `timestamp_before` or `query_ids` must be supplied.
    """
    log = QueryLog(session)
    full_table = _qualify(db, schema, table_name)

    ts = _resolve_timestamp(session, log, timestamp_before, query_ids)
    if not ts:
        return {
            "success": False,
            "error": (
                "Revert requires either `timestamp_before` or `query_ids`. "
                "Neither could be resolved."
            ),
            **log.to_dict(),
        }

    # Sanity-check the timestamp is within the retention window
    try:
        rows = log.execute(
            "SELECT DATEDIFF('hour', TRY_TO_TIMESTAMP_LTZ(?), CURRENT_TIMESTAMP()) AS HOURS_AGO",
            params=[ts],
        )
        if rows and rows[0]["HOURS_AGO"] is not None:
            hours_ago = int(rows[0]["HOURS_AGO"])
            if hours_ago >= TIME_TRAVEL_MAX_HOURS:
                return {
                    "success": False,
                    "error": (
                        f"Timestamp {ts} is {hours_ago}h old — beyond the "
                        f"{TIME_TRAVEL_MAX_HOURS}h Time Travel retention window."
                    ),
                    **log.to_dict(),
                }
    except Exception:
        # Don't fail the revert just because the sanity check failed —
        # the CLONE will fail loudly if the data has aged out.
        pass

    backup_table: Optional[str] = None
    if create_backup:
        # Snapshot the current (potentially corrupt) state to a timestamped
        # backup table so the caller can inspect/recover it if needed.
        backup_suffix = f"revert_backup_{int(time.time())}"
        backup_table = f"{table_name}_{backup_suffix}"
        try:
            log.execute(
                f"CREATE TABLE {_qualify(db, schema, backup_table)} "
                f"CLONE {full_table}"
            )
        except Exception:
            # Backup is best-effort — don't fail the revert if it errors.
            backup_table = None

    # Recreate the table from TIME TRAVEL.
    # CREATE OR REPLACE ... CLONE drops the existing table and recreates
    # it from the time-travelled snapshot. The dropped version remains
    # recoverable via UNDROP TABLE for additional safety.
    safe_ts = ts.replace("'", "''")
    clone_sql = (
        f"CREATE OR REPLACE TABLE {full_table} "
        f"CLONE {full_table} AT(TIMESTAMP => '{safe_ts}'::TIMESTAMP_LTZ)"
    )

    try:
        log.execute(clone_sql)
    except Exception as e:
        return {
            "success": False,
            "error": f"TIME TRAVEL CLONE failed: {e}",
            "timestamp_used": ts,
            "backup_table": backup_table,
            **log.to_dict(),
        }

    return {
        "success": True,
        "table": full_table,
        "timestamp_used": ts,
        "backup_table": backup_table,
        "strategy": "time_travel",
        "warning": (
            f"Table reverted to {ts}. "
            + (f"A backup was saved as {backup_table}. " if backup_table else "")
            + "The pre-revert table is also recoverable via UNDROP TABLE "
            "within the Time Travel window."
        ),
        **log.to_dict(),
    }


def revert_rows(session, db: str, schema: str, table_name: str,
                timestamp_before: str,
                file: Optional[str] = None,
                page_range: Optional[tuple] = None) -> Dict:
    """
    Restore only specific rows (filtered by file and/or page_range) to
    their state at `timestamp_before` using TIME TRAVEL.

    Useful when a single page-range update went wrong and the caller does
    not want to rewind the entire table.
    """
    log = QueryLog(session)
    full_table = _qualify(db, schema, table_name)
    safe_ts = timestamp_before.replace("'", "''")

    # Build WHERE clause for the rows we want to restore
    clauses = []
    if file:
        clauses.append(f"RELATIVE_PATH = '{file.replace(chr(39), chr(39)*2)}'")
    if page_range:
        clauses.append(
            f"PAGE_NUMBER BETWEEN {int(page_range[0])} AND {int(page_range[1])}"
        )
    where_clause = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    # Strategy: DELETE the current rows that match the filter, then
    # re-INSERT them from TIME TRAVEL.
    try:
        log.execute(f"DELETE FROM {full_table}{where_clause}")
        log.execute(
            f"INSERT INTO {full_table} "
            f"SELECT * FROM {full_table} "
            f"AT(TIMESTAMP => '{safe_ts}'::TIMESTAMP_LTZ){where_clause}"
        )
    except Exception as e:
        return {
            "success": False,
            "error": f"Row-level revert failed: {e}",
            "timestamp_used": timestamp_before,
            **log.to_dict(),
        }

    return {
        "success": True,
        "table": full_table,
        "timestamp_used": timestamp_before,
        "filter": {"file": file, "page_range": list(page_range) if page_range else None},
        "strategy": "time_travel_rows",
        "warning": (
            f"Rows matching the filter were reverted to {timestamp_before}. "
            "Other rows in the table are untouched."
        ),
        **log.to_dict(),
    }
