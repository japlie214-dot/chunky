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
            f"{cmd} TABLE {full_table} "
            f"(RELATIVE_PATH VARCHAR, PAGE_NUMBER NUMBER, CHUNK VARCHAR, "
            f"CHUNK_ID VARCHAR, CHUNK_TYPE VARCHAR DEFAULT 'STANDARD', "
            f"CHUNK_REF VARCHAR, LINK_BLOCK VARCHAR, CHUNK_METADATA VARIANT) CHANGE_TRACKING = TRUE{grants}"
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
    DELETEs original pages in each range's source_start..source_end,
    then shifts all downstream pages (PAGE_NUMBER > source_end) by delta,
    then re-stamps CHUNK_REF to keep "Page Num: N" consistent.

    Applied bottom-up (descending source_end) to prevent collision
    when multiple ranges exist.

    Each range runs in its own explicit BEGIN TRANSACTION / COMMIT block.
    On any failure, explicit ROLLBACK is issued — execute_sql_safe swallows
    exceptions and returns (False, err), so the caller MUST manually
    ROLLBACK to avoid leaving the transaction open.

    Ref: https://docs.snowflake.com/en/sql-reference/transactions
    Ref: https://docs.snowflake.com/en/sql-reference/sql/update
    """
    from utils.page_mapping import RangeMapping, RangeMappingEngine

    # Convert dicts to RangeMapping objects if needed
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

    # Sort bottom-up to avoid collision
    # Ref: RangeMappingEngine.sort_bottom_up docstring
    sorted_rms = RangeMappingEngine.sort_bottom_up(rms)

    for rm in sorted_rms:
        delta = RangeMappingEngine.compute_delta(rm)

        # Begin explicit transaction
        # Ref: https://docs.snowflake.com/en/sql-reference/transactions
        # "A transaction can be started explicitly by executing a BEGIN statement.
        #  Snowflake recommends using BEGIN TRANSACTION."
        session.sql("BEGIN TRANSACTION").collect()

        try:
            # Step 1: DELETE original pages in the SOURCE range
            # (NOT the target/replacement range — the source range is what
            #  currently exists in the table and needs to be removed.)
            delete_sql = (
                f"DELETE FROM {full_table} "
                f"WHERE RELATIVE_PATH = '{safe_file}' "
                f"AND PAGE_NUMBER BETWEEN {rm.source_start} AND {rm.source_end}"
            )
            ok, res = execute_sql_safe(session, delete_sql)
            if not ok:
                raise Exception(f"DELETE failed: {res}")

            # Step 2: Shift downstream pages by delta (only if delta != 0)
            # The shift targets PAGE_NUMBER > source_end (NOT > target_end).
            # These are ORIGINAL pages that sit after the deleted block.
            # CHUNK_REF is re-stamped in the same UPDATE using REGEXP_REPLACE.
            # Both SET expressions reference the OLD row state per Snowflake
            # UPDATE semantics, so PAGE_NUMBER and CHUNK_REF compute against
            # the same pre-update value.
            # Ref: https://docs.snowflake.com/en/sql-reference/sql/update
            if delta != 0:
                shift_sql = (
                    f"UPDATE {full_table} "
                    f"SET PAGE_NUMBER = PAGE_NUMBER + {delta}, "
                    f"    CHUNK_REF = REGEXP_REPLACE("
                    f"        CHUNK_REF, "
                    f"        'Page Num: [0-9]+', "
                    f"        'Page Num: ' || (PAGE_NUMBER + {delta})"
                    f"    ) "
                    f"WHERE RELATIVE_PATH = '{safe_file}' "
                    f"AND PAGE_NUMBER > {rm.source_end}"
                )
                ok, res = execute_sql_safe(session, shift_sql)
                if not ok:
                    raise Exception(f"Shift UPDATE failed: {res}")

            # Commit
            session.sql("COMMIT").collect()

            log_action("SURGICAL_RANGE_SHIFT", {
                "table": full_table,
                "file": safe_file,
                "source_range": [rm.source_start, rm.source_end],
                "replacement_range": [rm.replacement_start, rm.replacement_end],
                "delta": delta,
            })

        except Exception as e:
            # CRITICAL: execute_sql_safe swallows the exception and returns
            # (False, err). The transaction is still open. We MUST explicitly
            # ROLLBACK here, otherwise the transaction stays active and
            # subsequent SQL calls will be inside a dangling transaction.
            # Ref: https://docs.snowflake.com/en/sql-reference/transactions
            # "When a DML statement in a transaction fails, the changes made
            #  by that failed statement are rolled back. However, the
            #  transaction stays active until the entire transaction is
            #  committed or rolled back."
            try:
                session.sql("ROLLBACK").collect()
            except Exception:
                pass  # Best-effort rollback; log the original error below

            log_action("SURGICAL_DELETE_ERROR", str(e))

            # Cascade-cancel downstream Pending jobs on the same table
            # (existing pattern from _execute_surgical_delete_with_mappings)
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

    return True, ""
