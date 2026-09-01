"""
procedure/utils/revert.py
Native-Snowflake revert support.

Strategy: TIME TRAVEL via ALTER TABLE RENAME
--------------------------------------------
Snowflake keeps every table change for the account's data-retention
window (default 24h on Standard Edition). We use that to "rewind" the
target table to a known-good timestamp.

Why NOT `CREATE OR REPLACE TABLE X CLONE X AT(...)`:
  The Snowflake parser resolves the source reference before dropping the
  target. When source == target, the clone operation conflicts because
  the same object is being read and replaced in one statement. Even
  though the dropped table remains accessible via Time Travel, the
  statement fails at parse/plan time.

  (Ref: user-reported behaviour, reproduced against Snowflake Standard
  Edition. CREATE OR REPLACE ... CLONE ... works when source != target.)

Safe pattern (used here):
  1. Resolve the target timestamp (caller-supplied or via query_ids).
  2. ALTER TABLE <t> RENAME TO <t>_revert_backup_<epoch>
       — RENAME preserves the table's Time Travel history because it
         is the same physical table, just renamed.
  3. CREATE TABLE <t> CLONE <t>_revert_backup_<epoch>
         AT(TIMESTAMP => '<ts>'::TIMESTAMP_LTZ)
       — source (renamed) != target (original name), so the clone works.
  4. The renamed table is left in place as a backup. The caller can
     DROP it once they've verified the revert, or UNDROP the original
     if they want to undo the revert itself.

Returns a dict with the captured query_ids, the timestamp used, and the
backup table name.
"""
from __future__ import annotations
import time
from typing import Dict, Any, List, Optional

from .query_log import QueryLog
from .constants import TIME_TRAVEL_MAX_HOURS
from ._shared import qualify as _qualify


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

    # ------------------------------------------------------------------
    # SAFE REVERT PATTERN (rename + clone)
    # ------------------------------------------------------------------
    # 1. Rename the current (potentially corrupt) table to a backup name.
    #    RENAME preserves Time Travel history because the physical table
    #    is unchanged — only its identifier moves.
    #
    # 2. CREATE TABLE <original> CLONE <backup> AT(TIMESTAMP => '<ts>')
    #    Source (renamed) != target (original name), so this CLONE works.
    #
    # 3. Leave the backup in place so the caller can recover if needed.
    # ------------------------------------------------------------------
    backup_table: Optional[str] = None
    if create_backup:
        backup_suffix = f"revert_backup_{int(time.time())}"
        # Force the backup identifier to upper-case. `_qualify()` always
        # double-quotes identifiers, which makes Snowflake store the name
        # exactly as given. Snowflake's *unquoted* identifier folding also
        # upper-cases, so keeping this name upper-case ensures a plain,
        # unquoted `DROP TABLE IF EXISTS db.schema.<name>` (the natural
        # thing to type) resolves to the same physical object instead of
        # silently missing it because of a case mismatch.
        backup_table = f"{table_name}_{backup_suffix}".upper()
        backup_full = _qualify(db, schema, backup_table)
        try:
            log.execute(
                f"ALTER TABLE {full_table} RENAME TO {backup_full}"
            )
        except Exception as e:
            # If rename fails, fall back to CREATE TABLE backup CLONE
            # pattern (still preserves the original for recovery).
            try:
                log.execute(
                    f"CREATE TABLE {backup_full} CLONE {full_table}"
                )
            except Exception:
                # Last-resort: continue without a backup
                backup_table = None
                backup_full = None
    else:
        # No backup requested — drop the original (preserved by Time
        # Travel for the retention window) and recreate from Time Travel
        # using UNDROP. This is riskier; only used when caller explicitly
        # opts out of the backup.
        backup_full = None
        try:
            log.execute(f"DROP TABLE {full_table}")
        except Exception as e:
            return {
                "success": False,
                "error": f"DROP TABLE failed (cannot revert without backup): {e}",
                "timestamp_used": ts,
                **log.to_dict(),
            }

    # Recreate the original table from TIME TRAVEL.
    # Source = renamed backup (or the dropped original via Time Travel
    # when create_backup=False — Snowflake preserves dropped tables for
    # the retention window so CLONE AT() still resolves).
    safe_ts = ts.replace("'", "''")
    clone_source = backup_full if backup_full else full_table
    clone_sql = (
        f"CREATE TABLE {full_table} "
        f"CLONE {clone_source} AT(TIMESTAMP => '{safe_ts}'::TIMESTAMP_LTZ)"
    )

    try:
        log.execute(clone_sql)
    except Exception as e:
        # If clone fails, try to recover: UNDROP the original (if we
        # dropped it) or rename the backup back.
        if not create_backup:
            try:
                log.execute(f"UNDROP TABLE {full_table}")
            except Exception:
                pass
        elif backup_full:
            try:
                # Drop any half-created original, then rename backup back
                log.execute(f"DROP TABLE IF EXISTS {full_table}")
                log.execute(
                    f"ALTER TABLE {backup_full} RENAME TO {full_table}"
                )
            except Exception:
                pass
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
        safe_file = file.replace("'", "''")
        clauses.append(f"PDF_NAME = '{safe_file}'")
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
        "filter": {"file": file,
                   "page_range": list(page_range) if page_range else None},
        "strategy": "time_travel_rows",
        "warning": (
            f"Rows matching the filter were reverted to {timestamp_before}. "
            "Other rows in the table are untouched."
        ),
        **log.to_dict(),
    }
