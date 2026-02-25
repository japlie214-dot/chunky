# views/refinery/common.py
# Common utilities for the Doc Refinery package
from logger_config import log_action

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
