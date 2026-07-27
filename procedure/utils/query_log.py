"""
procedure/utils/query_log.py
Lightweight query-ID capture for revert support.

Every Snowflake SQL statement leaves behind a query ID. By collecting those
IDs as a procedure runs, we can hand the caller a `query_ids` list that they
can later pass to the REVERT command to undo the operation.

This module is intentionally Snowpark-only (no streamlit, no third-party
deps) so it works inside a Snowflake Python Stored Procedure.
"""
from __future__ import annotations
from typing import List, Optional, Iterable
import time


def capture_query_id(session) -> Optional[str]:
    """
    Return the query ID of the most recently executed statement on `session`,
    or None if it cannot be retrieved.

    Snowpark's `session.sql(...).collect()` runs the statement on the server.
    The server-side function `LAST_QUERY_ID()` returns the ID of the previous
    statement in the *session* — which is exactly what we want here.
    """
    try:
        rows = session.sql("SELECT LAST_QUERY_ID() AS QID").collect()
        if rows and rows[0]["QID"]:
            return str(rows[0]["QID"])
    except Exception:
        return None
    return None


class QueryLog:
    """
    Collects query IDs as a procedure executes, and snapshots the
    pre-operation timestamp that the REVERT command needs for TIME TRAVEL.
    """

    def __init__(self, session):
        self._session = session
        self._ids: List[str] = []
        # Snapshot the Snowflake current timestamp BEFORE any DML runs.
        # This is the value we hand back to the REVERT command.
        self.timestamp_before: Optional[str] = self._snapshot_timestamp()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _snapshot_timestamp(self) -> Optional[str]:
        try:
            rows = self._session.sql(
                "SELECT TO_VARCHAR(CURRENT_TIMESTAMP()) AS TS"
            ).collect()
            if rows and rows[0]["TS"]:
                return str(rows[0]["TS"])
        except Exception:
            return None
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def execute(self, sql: str, params: Optional[Iterable] = None):
        """
        Execute `sql` via Snowpark and record the resulting query ID.
        Returns the row collection so callers can chain `.as_dict()` etc.
        """
        if params is not None:
            res = self._session.sql(sql, params=list(params)).collect()
        else:
            res = self._session.sql(sql).collect()
        qid = capture_query_id(self._session)
        if qid:
            self._ids.append(qid)
        return res

    def snapshot_after(self) -> Optional[str]:
        """Return the current Snowflake timestamp (call after the work is done)."""
        try:
            rows = self._session.sql(
                "SELECT TO_VARCHAR(CURRENT_TIMESTAMP()) AS TS"
            ).collect()
            if rows and rows[0]["TS"]:
                return str(rows[0]["TS"])
        except Exception:
            return None
        return None

    def to_dict(self) -> dict:
        """Render the captured state for inclusion in the JSON response."""
        return {
            "query_ids": list(self._ids),
            "timestamp_before": self.timestamp_before,
            "timestamp_after": self.snapshot_after(),
            "query_count": len(self._ids),
        }

    @property
    def ids(self) -> List[str]:
        return list(self._ids)
