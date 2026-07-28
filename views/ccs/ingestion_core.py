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
from views.ccs.refinery_common import execute_sql_safe
from utils.table_migrator import LegacyTableMigrator


# -----------------------------------------------------------------------------
# Table Initialization
# -----------------------------------------------------------------------------

def _initialize_target_table(session, full_table, db, schema, table_name,
                               mode, tbl_exists, tbl_cols):
    if mode == 'OVERWRITE' or not tbl_exists:
        cmd = "CREATE OR REPLACE" if mode == 'OVERWRITE' else "CREATE"
        grants = " COPY GRANTS" if mode == 'OVERWRITE' else ""
        init_sql = (
            f"{cmd} TABLE {full_table} ("
            f"RELATIVE_PATH VARCHAR COMMENT 'Stage-relative path of the source PDF file', "
            f"PAGE_NUMBER NUMBER COMMENT 'PDF page number — directly reflects the original document page', "
            f"CHUNK VARCHAR COMMENT 'AI-extracted markdown content for this page segment', "
            f"CHUNK_ID VARCHAR COMMENT 'Globally unique identifier (CHK_<UUID>) for this chunk', "
            f"CHUNK_TYPE VARCHAR DEFAULT 'STANDARD' COMMENT 'Extraction type: STANDARD (layout), ENHANCED (vision-repaired), PLACEHOLDER (fallback)', "
            f"CHUNK_REF VARCHAR COMMENT 'Human-readable source reference: Doc Source + Page Num + optional digital copy link', "
            f"LINK_BLOCK VARCHAR COMMENT 'Markdown-formatted hyperlinks extracted from the PDF page', "
            f"CHUNK_METADATA VARIANT COMMENT 'JSON metadata: parser config, surgical mappings, timestamps'"
            f") CHANGE_TRACKING = TRUE{grants}"
        )
        ok, res = execute_sql_safe(session, init_sql)
        if not ok:
            raise Exception(f"Initialization Failed: {res}")
    else:
        LegacyTableMigrator.migrate_table(session, db, schema, table_name)


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


def _execute_surgical_delete_with_mappings(
    session, full_table, safe_file, source_range,
    page_mappings, job_queue, current_job_index
):
    start_pg, end_pg = source_range
    source_pages = list(range(start_pg, end_pg + 1))
    if not source_pages:
        return True, ""
        
    delete_sql = (
        f"DELETE FROM {full_table} "
        f"WHERE RELATIVE_PATH = '{safe_file}' "
        f"AND PAGE_NUMBER IN ({', '.join(map(str, source_pages))})"
    )
    try:
        ok, res = execute_sql_safe(session, delete_sql)
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
            st.warning(f"The following jobs targeting {target_table} were Cancelled due to this failure: {', '.join(cancelled_ids)}")
        return False, str(e)

# -----------------------------------------------------------------------------
# Surgical Delete with Shift (Range Mappings)
# -----------------------------------------------------------------------------

def _execute_surgical_delete_with_shift(
    session, full_table, safe_file, range_mappings,
    job_queue, current_job_index
):
    """
    DELETE source range pages only. No shifting, no delta, no CHUNK_REF rewrite.

    PAGE_NUMBER directly reflects the PDF page number. Replacement pages are
    inserted at their actual PDF page numbers by the ingestion strategy.

    Multi-range: sort bottom-up so deleting a higher range doesn't invalidate
    page numbers of a lower range.
    """
    from utils.page_mapping import RangeMapping, RangeMappingEngine

    rms = []
    for rm_dict in range_mappings:
        if isinstance(rm_dict, RangeMapping):
            rms.append(rm_dict)
        else:
            rms.append(RangeMapping(
                source_start=int(rm_dict['source_start']),
                source_end=int(rm_dict['source_end']),
                replacement_start=int(rm_dict['replacement_start']),
                replacement_end=int(rm_dict['replacement_end']),
            ))

    sorted_rms = RangeMappingEngine.sort_bottom_up(rms)

    # Wrap multi-range deletes in an explicit transaction so that a failure
    # on any range rolls back all prior deletes in this job.
    session.sql("BEGIN").collect()
    try:
        for rm in sorted_rms:
            delete_sql = (
                f"DELETE FROM {full_table} "
                f"WHERE RELATIVE_PATH = '{safe_file}' "
                f"AND PAGE_NUMBER BETWEEN {rm.source_start} AND {rm.source_end}"
            )
            ok, res = execute_sql_safe(session, delete_sql)
            if not ok:
                session.sql("ROLLBACK").collect()
                log_action("SURGICAL_DELETE_ERROR", str(res))
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
                return False, str(res)

            log_action("SURGICAL_RANGE_DELETE", {
                "table": full_table,
                "file": safe_file,
                "source_range": [rm.source_start, rm.source_end],
                "replacement_range": [rm.replacement_start, rm.replacement_end],
            })

        session.sql("COMMIT").collect()
    except Exception as e:
        session.sql("ROLLBACK").collect()
        log_action("SURGICAL_DELETE_TRANSACTION_ERROR", str(e))
        return False, str(e)

    return True, ""
