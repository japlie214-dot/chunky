# views/refinery/ingestion_core.py
# Core database operations for the Doc Refinery ingestion pipeline
"""
Core ingestion operations: table initialization and surgical deletion.
CONSTRAINT: This module must have zero imports from batch_processor.py or
tab_deployment.py. All functions here are stateless and receive all required
context via explicit parameters.
"""
import streamlit as st
from logger_config import log_action
from views.refinery.common import execute_sql_safe


# -----------------------------------------------------------------------------
# Table Initialization
# -----------------------------------------------------------------------------

def _initialize_target_table(session, full_table, db, schema, table_name,
                              mode, tbl_exists, tbl_cols):
    """
    Issues CREATE / CREATE OR REPLACE / ALTER TABLE as required.
    Receives pre-fetched (tbl_exists, tbl_cols) from the orchestrator;
    never issues its own DESCRIBE TABLE call.
    
    Args:
        session: Snowpark session
        full_table: Fully qualified table name with escaped identifiers
        db: Database name (raw, not escaped)
        schema: Schema name (raw, not escaped)
        table_name: Table name (raw, not escaped)
        mode: Operation mode ('OVERWRITE', 'APPEND', 'SURGICAL')
        tbl_exists: Boolean indicating if table exists
        tbl_cols: List of existing column names
    
    Raises:
        Exception: If table creation/alteration fails
    """
    if mode == 'OVERWRITE':
        init_sql = (
            f"CREATE OR REPLACE TABLE {full_table} "
            f"(RELATIVE_PATH VARCHAR, PAGE_NUMBER NUMBER, CHUNK VARCHAR, "
            f"CHUNK_ID VARCHAR, CHUNK_TYPE VARCHAR, CHUNK_REF VARCHAR, LINK_BLOCK VARCHAR) CHANGE_TRACKING = TRUE COPY GRANTS"
        )
        ok, res = execute_sql_safe(session, init_sql)
        if not ok:
            raise Exception(f"Overwrite Initialization Failed: {res}")
    elif not tbl_exists:
        init_sql = (
            f"CREATE TABLE {full_table} "
            f"(RELATIVE_PATH VARCHAR, PAGE_NUMBER NUMBER, CHUNK VARCHAR, "
            f"CHUNK_ID VARCHAR, CHUNK_TYPE VARCHAR, CHUNK_REF VARCHAR, LINK_BLOCK VARCHAR) CHANGE_TRACKING = TRUE"
        )
        ok, res = execute_sql_safe(session, init_sql)
        if not ok:
            raise Exception(f"Creation Initialization Failed: {res}")

    # Ensure CHUNK_TYPE exists for older tables
    if tbl_exists and tbl_cols and 'CHUNK_TYPE' not in [c.upper() for c in tbl_cols]:
        execute_sql_safe(session, f"ALTER TABLE {full_table} ADD COLUMN CHUNK_TYPE VARCHAR DEFAULT 'STANDARD'")
    # Ensure CHUNK_REF exists for older tables
    if tbl_exists and tbl_cols and 'CHUNK_REF' not in [c.upper() for c in tbl_cols]:
        execute_sql_safe(session, f"ALTER TABLE {full_table} ADD COLUMN CHUNK_REF VARCHAR DEFAULT NULL")
    # Ensure LINK_BLOCK exists for older tables
    if tbl_exists and tbl_cols and 'LINK_BLOCK' not in [c.upper() for c in tbl_cols]:
        execute_sql_safe(session, f"ALTER TABLE {full_table} ADD COLUMN LINK_BLOCK VARCHAR DEFAULT NULL")


# -----------------------------------------------------------------------------
# Surgical Delete
# -----------------------------------------------------------------------------

def _execute_surgical_delete(session, full_table, safe_file, pg_filter_sql,
                               job_queue, current_job_index, target_file=None, target_page=0):
    """
    Deletes existing rows for the file/range, then cascade-cancels all downstream
    Pending jobs targeting the same table on failure.
    
    Accepts the full job_queue list and current_job_index (never a pre-sliced copy)
    so that mutation of downstream job dicts is unambiguous and immune to
    shallow-copy slice semantics.
    
    Args:
        session: Snowpark session
        full_table: Fully qualified table name with escaped identifiers
        safe_file: SQL-escaped filename for the WHERE clause
        pg_filter_sql: Additional page range filter SQL
        job_queue: Full list of job dictionaries
        current_job_index: Index of the current job in the queue
    
    Returns:
        tuple: (success: bool, error_message: str)
    """
    delete_file = target_file if target_file else safe_file
    page_condition = f"AND PAGE_NUMBER = {int(target_page)}" if target_page and int(target_page) > 0 else pg_filter_sql
    del_sql = f"DELETE FROM {full_table} WHERE RELATIVE_PATH = '{delete_file}' {page_condition}"
    try:
        ok, res = execute_sql_safe(session, del_sql)
        if not ok:
            raise Exception(str(res))
        return True, ""
    except Exception as e:
        log_action("SURGICAL_DELETE_ERROR", str(e))
        target_table = job_queue[current_job_index]['table']
        cancelled_ids = []
        for i in range(current_job_index + 1, len(job_queue)):
            if job_queue[i]['table'] == target_table and job_queue[i]['status'] == 'Pending':
                job_queue[i]['status'] = 'Cancelled'
                cancelled_ids.append(str(job_queue[i]['id']))
        if cancelled_ids:
            st.warning(
                f"The following jobs targeting {target_table} were Cancelled "
                f"due to this failure: {', '.join(cancelled_ids)}"
            )
        return False, str(e)
