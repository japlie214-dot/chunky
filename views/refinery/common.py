"""
Shared primitive utilities for the Doc Refinery package.
CONSTRAINT: This module must have zero imports from batch_processor.py or
tab_deployment.py. All functions here must be stateless and pure so that any
refinery sub-module can import from here without risk of circular dependency.
"""
# views/refinery/common.py
# Common utilities for the Doc Refinery package
from logger_config import log_action

# -----------------------------------------------------------------------------
# PLAN-01: CHUNK_REF Builder Helper (relocated from batch_processor.py)
# -----------------------------------------------------------------------------

def _build_chunk_ref(rel_path: str, page_num, link: str = "") -> str:
    """
    Builds the canonical CHUNK_REF string.
    Uses the raw filename (not SQL-escaped) so single quotes are preserved.
    """
    base = f"Doc Source: {rel_path} | Page Num: {page_num}"
    return f"{base} | Link: {link}" if link else base


# -----------------------------------------------------------------------------
# execute_sql_safe - Robust SQL execution with error trapping
# -----------------------------------------------------------------------------

def execute_sql_safe(session, sql: str):
    """
    Executes SQL with robust error trapping and logging.
    Returns (success: bool, result: any)
    """
    try:
        res = session.sql(sql).collect()
        return True, res
    except Exception as e:
        err_msg = str(e)
        log_action("SQL_EXECUTION_ERROR", {
            "error": err_msg,
            "sql_snippet": sql[:500] if len(sql) > 500 else sql
        })
        return False, err_msg
