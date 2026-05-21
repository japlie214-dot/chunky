# views/refinery/batch_processor.py
# Core batch processing orchestrator for the Doc Refinery package
import streamlit as st
import json
import time
import datetime
from logger_config import log_action
from utils.core_utils import (
    PDFUtils, RAGAnalytics, clean_text_for_sql
)
from utils.snowflake_utils import (
    get_table_schema, execute_grant_with_retry
)

# PLAN-02: Import from modularized ingestion components
from views.refinery.ingestion_core import (
    _initialize_target_table,
    _execute_surgical_delete,
)
from views.refinery.ingestion_strategies import (
    _execute_layout_strategy,
    _execute_hybrid_repair_strategy,
    _execute_vision_strategy,
)


def _finalize_job_metrics(session, job, batch_metrics, job_start_time,
                           job_pages_count, full_table):
    """
    Reads job['metrics'] (already fully populated by strategy helpers) to compute
    credit costs, execute grants, set job status, stamp completion_ts, and
    aggregate into batch_metrics.
    Receives full_table explicitly — never reads st.session_state.auth_context.
    Called only from the try block; never from finally, so it does not overwrite
    partial-success metrics on failure.
    """
    c_layout = (job['metrics'].get('layout_pages', 0) / 1000) * 3.33
    pricing  = RAGAnalytics.PRICING_REGISTRY.get('claude-sonnet-4-6', {'input': 1.50, 'output': 7.50})
    v_in     = job['metrics']['vision_input_tokens']
    v_out    = job['metrics']['vision_output_tokens']
    c_vision = (v_in / 1_000_000 * pricing['input']) + (v_out / 1_000_000 * pricing['output'])

    batch_metrics['time_layout']   += job['metrics']['time_layout']
    batch_metrics['time_vision']   += job['metrics']['time_vision']
    batch_metrics['credits_layout'] += c_layout
    batch_metrics['credits_vision'] += c_vision
    batch_metrics['vision_pages_processed'] += len(job['metrics']['vision_pages_list'])
    batch_metrics['layout_pages_processed'] += job['metrics'].get('layout_pages', 0)
    batch_metrics['standard_chunks']        += job['metrics']['standard_cnt']
    batch_metrics['enhanced_chunks']        += job['metrics']['enhanced_cnt']
    for etype, count in job['metrics']['types'].items():
        batch_metrics['enhancement_breakdown'][etype] = (
            batch_metrics['enhancement_breakdown'].get(etype, 0) + count
        )

    # Status Determination based ONLY on batch processing
    skipped = job.get('skipped_page_ranges', [])
    total_batches = job.get('metrics', {}).get('total_batches', 1)
    failed_batches = len(skipped)
    successful_batches = total_batches - failed_batches

    if successful_batches == 0 and total_batches > 0:
        job['status'] = 'Failed'
    elif failed_batches > 0 and successful_batches > 0:
        job['status'] = 'Completed with Warnings'
        batch_metrics['jobs_warning'] = batch_metrics.get('jobs_warning', 0) + 1
    elif successful_batches > 0:
        job['status'] = 'Completed'
        batch_metrics['jobs_completed'] += 1
    else:
        job['status'] = 'Failed'

    # Grant-failure override: only escalates 'Completed' → 'Completed with Warnings'.
    # 'Failed' and 'Completed with Warnings' (from batch failures) are left unchanged.
    if job.get('grant_warning') and job['status'] == 'Completed':
        job['status'] = 'Completed with Warnings'
        batch_metrics['jobs_completed'] -= 1
        batch_metrics['jobs_warning'] = batch_metrics.get('jobs_warning', 0) + 1

    job['metrics']['completion_ts'] = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    job_end_time = time.time()
    job['metrics']['end']      = job_end_time
    job['metrics']['duration'] = job_end_time - job_start_time
    job['metrics']['pages']    = job_pages_count
    batch_metrics['total_pages'] += job_pages_count


# -----------------------------------------------------------------------------
# run_batch_execution - Core Execution Logic (Orchestrator)
# -----------------------------------------------------------------------------

def run_batch_execution(session, db, schema, stage_path):
    """
    High-level orchestrator. Iterates the job queue and delegates all work
    to single-responsibility helpers. The try/except/finally structure and
    ingestion_history persistence live exclusively here.
    """
    if "chunk_cache" not in st.session_state:
        st.session_state.chunk_cache = []

    st.markdown("### 📊 Batch Execution Progress")
    batch_progress = st.progress(0, text="Initializing Batch...")
    batch_status   = st.empty()
    total_jobs     = len(st.session_state.job_queue)

    batch_metrics = {
        "jobs_completed": 0, "jobs_failed": 0, "jobs_warning": 0,
        "total_pages": 0,    "total_chunks": 0,
        "layout_pages_processed": 0, "vision_pages_processed": 0,
        "standard_chunks": 0,        "enhanced_chunks": 0,
        "total_time": 0.0,  "time_layout": 0.0, "time_vision": 0.0,
        "credits_layout": 0.0,       "credits_vision": 0.0,
        "enhancement_breakdown": {},
    }
    batch_start_time = time.time()

    for idx, job in enumerate(st.session_state.job_queue):
        if job['status'] in ['Completed', 'Cancelled']:
            continue

        job_alert      = st.empty()
        job_start_time = time.time()
        job['metrics'] = {
            "start": job_start_time, "end": None, "duration": 0,
            "time_layout": 0.0,      "time_vision": 0.0,
            "pages": 0,              "layout_pages": 0,
            "vision_pages_list":     set(),
            "vision_input_tokens":   0, "vision_output_tokens": 0,
            "standard_cnt": 0,       "enhanced_cnt": 0, "types": {},
            "defects_detail": [],
        }
        job['status'] = 'Running'

        green_completions = sum(
            1 for j in st.session_state.job_queue[:idx] if j.get('status') == 'Completed'
        )
        batch_progress.progress(
            green_completions / total_jobs if total_jobs > 0 else 0,
            text=f"Processing Job {idx+1} of {total_jobs}",
        )
        batch_status.markdown(
            f"**🔄 Job {idx+1}/{total_jobs}:** `{job['file']}` → `{job['table']}`"
        )

        pdf_bytes = None
        def get_pdf_bytes():
            nonlocal pdf_bytes
            if pdf_bytes is None:
                pdf_bytes = session.file.get_stream(f"{stage_path}/{job['file']}").read()
            return pdf_bytes

        table_name = job['table'].split('.')[-1]
        # PLAN-01: Ensure identifiers are escaped to handle special characters or mixed case
        safe_db = db.replace('"', '""')
        safe_sch = schema.replace('"', '""')
        safe_tbl = table_name.replace('"', '""')
        full_table = f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'
        chunk_sz, chunk_ov = job['params']

        try:
            # 1. Scope resolution — e_pg is always an int after this block
            # 1. Scope resolution — e_pg is always an int after this block
            # Coordinate system fix: Snowflake page_filter end parameters are exclusive.
            # 1-based inclusive e_pg is equivalent to 0-based exclusive end parameter.
            if job['scope'] == "Page Range":
                s_pg, e_pg     = job['range']
                job_pages_count = (e_pg - s_pg) + 1
                pg_filter_sql   = f"AND PAGE_NUMBER BETWEEN {s_pg} AND {e_pg}"
                json_opts       = json.dumps({'mode': 'LAYOUT', 'page_filter': [{'start': s_pg - 1, 'end': e_pg}]})
            else:
                s_pg            = 1
                job_pages_count = PDFUtils.get_page_count(get_pdf_bytes())
                e_pg            = job_pages_count   # ensures range(s_pg, e_pg+1) is valid
                pg_filter_sql   = ""
                json_opts       = json.dumps({'mode': 'LAYOUT'})

            # 2. Surgical delete (preserves original execution order: delete before schema fetch)
            if job['mode'] == 'SURGICAL':
                batch_status.markdown(f"**✂️ Job {idx+1}/{total_jobs}:** Surgical Cleanup...")
                safe_file_surgical = clean_text_for_sql(job['file'])
                ok, err = _execute_surgical_delete(
                    session, full_table, safe_file_surgical, pg_filter_sql,
                    st.session_state.job_queue, idx,
                )
                if not ok:
                    job_alert.error(f"Critical Failure in Surgical Delete: {err}")
                    raise Exception(f"Surgical Delete Failed: {err}")

            # 3. Schema fetch (once; result passed to init helper — no double-DESCRIBE)
            tbl_exists, tbl_cols, _ = get_table_schema(session, db, schema, table_name)

            # 4. Table initialization
            _initialize_target_table(
                session, full_table, db, schema, table_name,
                job['mode'], tbl_exists, tbl_cols,
            )

            # 4b. Immediate RBAC Grants — unconditional when grant_roles is non-empty.
            #     Idempotent: re-granting existing privileges is a no-op in Snowflake.
            grant_roles = job.get('grant_roles', [])
            
            # Auto-grant logic for newly created or overwritten tables to ensure the creator retains access.
            # In OVERWRITE mode, the table is dropped and recreated, which requires fresh grants.
            if not tbl_exists or job['mode'] == 'OVERWRITE':
                user_email = st.session_state.auth_context.get('user', '')
                from utils.auth_utils import get_user_mapped_roles
                user_roles = get_user_mapped_roles(user_email)
                auto_role = next((r for r in user_roles if r.upper() != 'IT_AI'), None)
                if auto_role and auto_role not in grant_roles:
                    grant_roles.append(auto_role)
            if grant_roles:
                import re
                # Pattern supports standard unquoted identifiers OR double-quoted identifiers containing special chars
                ROLE_PATTERN = re.compile(r'^([A-Z_][A-Z0-9_$]*|"[^"]+")$', re.IGNORECASE)
                
                job['grant_status'] = {
                    'attempted': True, 'success': False,
                    'target_roles': grant_roles, 'failed_roles': []
                }
                user_email = st.session_state.auth_context.get('user', '')
                for role in grant_roles:
                    # Identifier syntax validation
                    if not ROLE_PATTERN.match(role):
                        log_action("GRANT_INVALID_ROLE_SKIPPED", {"job_id": job['id'], "invalid_role": role}, level="WARNING")
                        # Emits error to the Job Details dashboard without breaking chunking
                        job['grant_status']['failed_roles'].append(f"{role} (Invalid Syntax)")
                        continue
                        
                    # Handle quoted vs unquoted identifiers safely
                    if role.startswith('"') and role.endswith('"'):
                        grant_sql = f'GRANT ALL PRIVILEGES ON TABLE {full_table} TO ROLE {role}'
                    else:
                        safe_role = role.upper().replace('"', '""')
                        grant_sql = f'GRANT ALL PRIVILEGES ON TABLE {full_table} TO ROLE "{safe_role}"'
                        
                    grant_res = execute_grant_with_retry(
                        session, grant_sql, user_email, role.upper()
                    )
                    if grant_res == "Failed":
                        job['grant_status']['failed_roles'].append(role.upper())

                if job['grant_status']['failed_roles']:
                    log_action(
                        "GRANT_INIT_FAILURE",
                        {"file": job['file'], "table": full_table,
                         "failed_roles": job['grant_status']['failed_roles']},
                        level="WARNING"
                    )
                    st.warning(
                        f"⚠️ Grants failed for: "
                        f"{', '.join(job['grant_status']['failed_roles'])}"
                    )
                    job['grant_warning'] = True
                else:
                    job['grant_status']['success'] = True
                    st.toast(f"Access granted to: {', '.join(grant_roles)}")

            # 5a. Strategy A: Layout
            if job['layout']:
                batch_status.markdown(f"**🔧 Job {idx+1}/{total_jobs}:** Running Layout Parser (SQL)...")
                safe_file = clean_text_for_sql(job['file'])
                _execute_layout_strategy(
                    session, job, full_table, stage_path,
                    db, schema, table_name,
                    chunk_sz, chunk_ov, json_opts, safe_file, job_pages_count, get_pdf_bytes,
                )

            # 5b. Strategy B: Hybrid Repair
            if job['layout'] and job['vision']:
                batch_status.markdown(f"**🔍 Job {idx+1}/{total_jobs}:** Analyzing Quality & Repairing Defects...")
                safe_file = clean_text_for_sql(job['file'])
                _execute_hybrid_repair_strategy(
                    session, job, full_table, stage_path,
                    safe_file, pg_filter_sql, get_pdf_bytes, job_alert,
                )

            # 5c. Strategy C: Vision Only
            if job['vision'] and not job['layout']:
                batch_status.markdown(f"**👁️ Job {idx+1}/{total_jobs}:** Running Vision Parser...")
                target_range = range(s_pg, e_pg + 1)
                _execute_vision_strategy(
                    session, job, full_table, stage_path,
                    chunk_sz, chunk_ov, target_range, get_pdf_bytes,
                )

            # 6. Cost, grant, status finalization — called only on success path
            _finalize_job_metrics(
                session, job, batch_metrics, job_start_time,
                job_pages_count, full_table,
            )

        except Exception as e:
            job['status'] = 'Failed'
            job['metrics']['error'] = str(e)
            batch_metrics['jobs_failed'] += 1
            log_action("JOB_FAILED", {"id": job['id'], "error": str(e)})
            st.error(f"Job {job['id']} Failed: {e}")
            job_alert.empty()

        finally:
            # Persistence block: reads job['metrics'] as-mutated (incremental pattern)
            # guarantees accurate partial-success records regardless of which helper raised.
            if "ingestion_history" not in st.session_state:
                st.session_state.ingestion_history = []
            st.session_state.ingestion_history = [
                j for j in st.session_state.ingestion_history if j['id'] != job['id']
            ]
            st.session_state.ingestion_history.append(job)

    batch_metrics['total_time']   = time.time() - batch_start_time
    batch_metrics['total_chunks'] = batch_metrics['standard_chunks'] + batch_metrics['enhanced_chunks']
    st.session_state.batch_audit  = batch_metrics

    green_completions = sum(1 for j in st.session_state.job_queue if j.get('status') == 'Completed')
    batch_progress.progress(
        green_completions / total_jobs if total_jobs > 0 else 1.0,
        text="Batch Complete",
    )
    time.sleep(1)
    batch_progress.empty()
    batch_status.empty()
    st.success("🎉 Batch Execution Finished")
