# views/refinery/batch_processor.py
# Core batch processing orchestrator for the Doc Refinery package
import streamlit as st
import json
import time
import datetime
from logger_config import log_action
from observability import Accumulator, observe
from utils.core_utils import (
    PDFUtils, RAGAnalytics, clean_text_for_sql
)
from utils.snowflake_utils import (
    get_table_schema, execute_grant_with_retry
)

# Import from modularized ingestion components
from views.refinery.ingestion_core import (
    _initialize_target_table,
    _execute_surgical_delete,
    _execute_surgical_delete_with_mappings,
    _execute_surgical_delete_with_shift,
)
from views.refinery.ingestion_strategies import (
    _execute_layout_strategy,
    _execute_hybrid_repair_strategy,
    _execute_vision_strategy,
)


from views.refinery.batch_exceptions import BatchCancelledError


def _finalize_job_metrics(session, job, batch_metrics, job_start_time,
                           job_pages_count, full_table):
    """
    Reads job['metrics'] (already fully populated by strategy helpers) to compute
    credit costs, execute grants, set job status, stamp completion_ts, and
    aggregate into batch_metrics.
    """
    c_layout = (job['metrics'].get('layout_pages', 0) / 1000) * 3.33
    c_vision = 0.0

    vision_tokens = job['metrics'].get('vision_tokens', {})
    if vision_tokens:
        for model_name, usage in vision_tokens.items():
            pricing = RAGAnalytics.PRICING_REGISTRY.get(model_name, {'input': 0.60, 'output': 3.00})
            c_vision += (usage['in'] / 1_000_000 * pricing['input']) + (usage['out'] / 1_000_000 * pricing['output'])
    else:
        pricing  = RAGAnalytics.PRICING_REGISTRY.get('claude-haiku-4-5', {'input': 0.60, 'output': 3.00})
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
# _process_single_job — extracted from the original for-loop body
# -----------------------------------------------------------------------------

def _process_single_job(session, db, schema, stage_path, idx, total_jobs, batch_metrics):
    """
    Processes ONE job from st.session_state.job_queue at the given index.

    Decomposed into named Activities per the Activity-Driven Observability
    Convention. An Accumulator is created at the invocation boundary and
    threaded through every Activity. When observability is inactive, the
    Accumulator is a no-op with zero overhead.
    """
    job = st.session_state.job_queue[idx]

    if job['status'] in ['Completed', 'Completed with Warnings', 'Failed', 'Cancelled']:
        return

    # --- Accumulator at the invocation boundary (Rule 2) ---
    observability_active = st.session_state.get('observability_enabled', False)
    acc = Accumulator(active=observability_active)

    job_alert      = st.empty()
    job_start_time = time.time()
    job['metrics'] = {
        "start": job_start_time, "end": None, "duration": 0,
        "time_layout": 0.0,      "time_vision": 0.0,
        "pages": 0,              "layout_pages": 0,
        "vision_pages_list":     set(),
        "vision_tokens":         {},
        "vision_input_tokens":   0, "vision_output_tokens": 0,
        "standard_cnt": 0,       "enhanced_cnt": 0, "types": {},
        "defects_detail": [],
    }
    job['status'] = 'Running'

    green_completions = sum(
        1 for j in st.session_state.job_queue[:idx] if j.get('status') == 'Completed'
    )
    st.progress(
        green_completions / total_jobs if total_jobs > 0 else 0,
        text=f"Processing Job {idx+1} of {total_jobs}",
    )
    st.markdown(
        f"**🔄 Job {idx+1}/{total_jobs}:** `{job['file']}` → `{job['table']}`"
    )

    pdf_bytes = None
    def get_pdf_bytes():
        nonlocal pdf_bytes
        if pdf_bytes is None:
            pdf_bytes = session.file.get_stream(f"{stage_path}/{job['file']}").read()
        return pdf_bytes

    table_name = job['table'].split('.')[-1]
    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table_name.replace('"', '""')
    full_table = f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'
    chunk_sz, chunk_ov = job['params']

    # --- Activity: Scope Resolution ---
    def _activity_resolve_scope():
        """Activity: Resolve page scope, filters, and JSON options."""
        if job.get('surgical_range_mappings'):
            page_filters = []
            for rm in job['surgical_range_mappings']:
                page_filters.append({
                    'start': int(rm['replacement_start']) - 1,
                    'end': int(rm['replacement_end'])
                })
            json_opts = json.dumps({'mode': 'LAYOUT', 'page_filter': page_filters})
            job_pages_count = sum(
                rm['replacement_end'] - rm['replacement_start'] + 1
                for rm in job['surgical_range_mappings']
            )
            min_source = min(int(rm['source_start']) for rm in job['surgical_range_mappings'])
            max_target = max(
                int(rm['source_start']) + (int(rm['replacement_end']) - int(rm['replacement_start']))
                for rm in job['surgical_range_mappings']
            )
            pg_filter_sql = f"AND PAGE_NUMBER BETWEEN {min_source} AND {max_target}"
            s_pg = min(int(rm['replacement_start']) for rm in job['surgical_range_mappings'])
            e_pg = max(int(rm['replacement_end']) for rm in job['surgical_range_mappings'])
        elif job['scope'] == "Page Range":
            s_pg, e_pg     = job['range']
            job_pages_count = (e_pg - s_pg) + 1
            pg_filter_sql   = f"AND PAGE_NUMBER BETWEEN {s_pg} AND {e_pg}"
            json_opts       = json.dumps({'mode': 'LAYOUT', 'page_filter': [{'start': s_pg - 1, 'end': e_pg}]})
        else:
            s_pg            = 1
            job_pages_count = PDFUtils.get_page_count(get_pdf_bytes())
            e_pg            = job_pages_count
            pg_filter_sql   = ""
            json_opts       = json.dumps({'mode': 'LAYOUT'})
        return pg_filter_sql, json_opts, job_pages_count, s_pg, e_pg

    # --- Activity: Surgical Delete ---
    def _activity_surgical_delete(pg_filter_sql, s_pg, e_pg):
        """Activity: Execute surgical delete if mode is SURGICAL.
        
        Includes fallback for RELATIVE_PATH data migration edge case:
        If the first DELETE affects 0 rows and the path contains '/',
        retry with just the basename to handle data that was ingested
        with the old os.path.basename() logic.
        """
        if job['mode'] != 'SURGICAL':
            return None
        st.markdown(f"**✂️ Job {idx+1}/{total_jobs}:** Surgical Cleanup...")
        safe_file_surgical = clean_text_for_sql(job['file'])

        if job.get('surgical_range_mappings'):
            ok, err = _execute_surgical_delete_with_shift(
                session, full_table, safe_file_surgical,
                job['surgical_range_mappings'],
                st.session_state.job_queue, idx
            )
            # Fallback: if 0 rows deleted and path has '/', retry with basename
            if ok and '/' in job['file']:
                basename = job['file'].split('/')[-1]
                safe_basename = clean_text_for_sql(basename)
                if safe_basename != safe_file_surgical:
                    # Check if old data exists with basename
                    check_sql = f"SELECT COUNT(*) FROM {full_table} WHERE RELATIVE_PATH = '{safe_basename}'"
                    try:
                        check_res = session.sql(check_sql).collect()
                        if check_res and check_res[0][0] > 0:
                            log_action("SURGICAL_DELETE_FALLBACK_SHIFT", {
                                "original_path": job['file'],
                                "fallback_basename": basename,
                                "old_row_count": check_res[0][0]
                            })
                            _execute_surgical_delete_with_shift(
                                session, full_table, safe_basename,
                                job['surgical_range_mappings'],
                                st.session_state.job_queue, idx
                            )
                    except Exception:
                        pass  # Best-effort fallback
        elif job.get('surgical_page_mappings'):
            source_range = job.get('range', (s_pg, e_pg))
            ok, err = _execute_surgical_delete_with_mappings(
                session, full_table, safe_file_surgical, source_range,
                job['surgical_page_mappings'], st.session_state.job_queue, idx
            )
        else:
            surg_target_page = int(job.get('surgical_target_page', 0))
            ok, err = _execute_surgical_delete(
                session, full_table, safe_file_surgical, pg_filter_sql,
                st.session_state.job_queue, idx,
                target_page=surg_target_page
            )
            # Fallback for single-page delete
            if ok and '/' in job['file']:
                basename = job['file'].split('/')[-1]
                safe_basename = clean_text_for_sql(basename)
                if safe_basename != safe_file_surgical:
                    check_sql = f"SELECT COUNT(*) FROM {full_table} WHERE RELATIVE_PATH = '{safe_basename}'"
                    try:
                        check_res = session.sql(check_sql).collect()
                        if check_res and check_res[0][0] > 0:
                            log_action("SURGICAL_DELETE_FALLBACK", {
                                "original_path": job['file'],
                                "fallback_basename": basename,
                                "old_row_count": check_res[0][0]
                            })
                            _execute_surgical_delete(
                                session, full_table, safe_basename, pg_filter_sql,
                                st.session_state.job_queue, idx,
                                target_page=surg_target_page
                            )
                    except Exception:
                        pass

        if not ok:
            job_alert.error(f"Critical Failure in Surgical Delete: {err}")
            raise Exception(f"Surgical Delete Failed: {err}")
        return ok

    # --- Activity: Table Initialization & Grants ---
    def _activity_init_table():
        """Activity: Fetch schema, initialize table, apply RBAC grants."""
        tbl_exists, tbl_cols, _ = get_table_schema(session, db, schema, table_name)
        _initialize_target_table(
            session, full_table, db, schema, table_name,
            job['mode'], tbl_exists, tbl_cols,
        )
        grant_roles = job.get('grant_roles', [])

        if not tbl_exists or job['mode'] == 'OVERWRITE':
            user_email = st.session_state.auth_context.get('user', '')
            from utils.auth_utils import get_user_mapped_roles
            user_roles = get_user_mapped_roles(user_email)
            auto_role = next((r for r in user_roles if r.upper() != 'IT_AI'), None)
            if auto_role and auto_role not in grant_roles:
                grant_roles.append(auto_role)
        if grant_roles:
            import re
            ROLE_PATTERN = re.compile(r'^([A-Z_][A-Z0-9_$]*|"[^"]+")$', re.IGNORECASE)

            job['grant_status'] = {
                'attempted': True, 'success': False,
                'target_roles': grant_roles, 'failed_roles': []
            }
            user_email = st.session_state.auth_context.get('user', '')
            for role in grant_roles:
                if not ROLE_PATTERN.match(role):
                    log_action("GRANT_INVALID_ROLE_SKIPPED", {"job_id": job['id'], "invalid_role": role}, level="WARNING")
                    job['grant_status']['failed_roles'].append(f"{role} (Invalid Syntax)")
                    continue

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
        return tbl_exists

    # --- Activity: Layout Extraction ---
    def _activity_layout(json_opts, job_pages_count):
        """Activity: Run Layout Parser strategy."""
        if not job['layout']:
            return None
        st.markdown(f"**🔧 Job {idx+1}/{total_jobs}:** Running Layout Parser (SQL)...")
        safe_file = clean_text_for_sql(job['file'])
        _execute_layout_strategy(
            session, job, full_table, stage_path,
            db, schema, table_name,
            chunk_sz, chunk_ov, json_opts, safe_file, job_pages_count, get_pdf_bytes,
        )
        return True

    # --- Activity: Hybrid Repair ---
    def _activity_hybrid_repair(pg_filter_sql):
        """Activity: Run Hybrid Repair strategy for quality analysis."""
        if not (job['layout'] and job['vision']):
            return None
        st.markdown(f"**🔍 Job {idx+1}/{total_jobs}:** Analyzing Quality & Repairing Defects...")
        target_file_hybrid = clean_text_for_sql(job['file'])

        if job.get('surgical_range_mappings'):
            hybrid_pg_filter_sql = pg_filter_sql
        else:
            target_page_hybrid = int(job.get('surgical_target_page', 0))
            hybrid_pg_filter_sql = f"AND PAGE_NUMBER = {target_page_hybrid}" if target_page_hybrid > 0 else pg_filter_sql

        _execute_hybrid_repair_strategy(
            session, job, full_table, stage_path,
            target_file_hybrid, hybrid_pg_filter_sql, get_pdf_bytes, job_alert,
        )
        return True

    # --- Activity: Vision Extraction ---
    def _activity_vision(s_pg, e_pg):
        """Activity: Run Vision Parser strategy."""
        if not (job['vision'] and not job['layout']):
            return None
        st.markdown(f"**👁️ Job {idx+1}/{total_jobs}:** Running Vision Parser...")
        if job.get('surgical_range_mappings'):
            target_range = []
            for rm in job['surgical_range_mappings']:
                target_range.extend(
                    range(int(rm['replacement_start']), int(rm['replacement_end']) + 1)
                )
        else:
            target_range = range(s_pg, e_pg + 1)
        _execute_vision_strategy(
            session, job, full_table, stage_path,
            chunk_sz, chunk_ov, target_range, get_pdf_bytes,
        )
        return True

    # --- Activity: Metrics Finalization ---
    def _activity_finalize_metrics(job_pages_count):
        """Activity: Compute costs, set status, aggregate batch metrics."""
        _finalize_job_metrics(
            session, job, batch_metrics, job_start_time,
            job_pages_count, full_table,
        )
        return job['status']

    # --- Execute Activities (threading the Accumulator) ---
    try:
        pg_filter_sql, json_opts, job_pages_count, s_pg, e_pg = observe(
            "ScopeResolution", _activity_resolve_scope, acc
        )
        observe("SurgicalDelete", _activity_surgical_delete, acc, pg_filter_sql, s_pg, e_pg)
        observe("TableInitAndGrants", _activity_init_table, acc)
        observe("LayoutExtraction", _activity_layout, acc, json_opts, job_pages_count)
        observe("HybridRepair", _activity_hybrid_repair, acc, pg_filter_sql)
        observe("VisionExtraction", _activity_vision, acc, s_pg, e_pg)
        observe("MetricsFinalization", _activity_finalize_metrics, acc, job_pages_count)

        # If observability is active, attach lineage to job metrics
        if acc.active:
            job['metrics']['lineage'] = acc.to_lineage()

    except BatchCancelledError as e:
        job['status'] = 'Cancelled'
        job['metrics']['error'] = str(e)
        batch_metrics['jobs_cancelled'] = batch_metrics.get('jobs_cancelled', 0) + 1
        log_action("BATCH_CANCELLED", {"job_id": job['id'], "cancelled_at": str(e)})
        st.warning(f"Job {job['id']} Cancelled: {e}")
        job_alert.empty()
        if acc.active:
            job['metrics']['lineage'] = acc.to_lineage()

    except Exception as e:
        job['status'] = 'Failed'
        job['metrics']['error'] = str(e)
        batch_metrics['jobs_failed'] += 1
        log_action("JOB_FAILED", {"id": job['id'], "error": str(e)})
        st.error(f"Job {job['id']} Failed: {e}")
        job_alert.empty()
        if acc.active:
            job['metrics']['lineage'] = acc.to_lineage()

    finally:
        if "ingestion_history" not in st.session_state:
            st.session_state.ingestion_history = []
        st.session_state.ingestion_history = [
            j for j in st.session_state.ingestion_history if j['id'] != job['id']
        ]
        st.session_state.ingestion_history.append(job)


# -----------------------------------------------------------------------------
# run_batch_execution — One-Job-Per-Rerun Driver
# -----------------------------------------------------------------------------

def run_batch_execution(session, db, schema, stage_path):
    """
    One-job-per-rerun batch driver.

    Processes ONE job per invocation, then calls st.rerun() to yield control
    back to Streamlit. Between reruns, Streamlit processes queued UI events
    (including the Stop Batch button click). The next rerun checks
    st.session_state.cancel_batch and halts if True.

    This design is required because Streamlit's ScriptRunner is single-threaded
    per session. A blocking for-loop prevents ALL UI interactions. By yielding
    via st.rerun() between jobs, the Stop button becomes clickable.

    Ref: https://docs.streamlit.io/develop/api-reference/caching-and-state/st.session_state
    Ref: https://docs.streamlit.io/develop/api-reference/execution-flow/st.rerun

    Pre-condition: st.session_state.batch_in_progress must be True.
                   st.session_state.batch_metrics must be initialized.
                   st.session_state.batch_start_time must be set.
    These are set by the "Run Batch" button in tab_ingestion.py.
    """
    batch_metrics = st.session_state.batch_metrics
    batch_start_time = st.session_state.batch_start_time
    total_jobs = len(st.session_state.job_queue)

    st.markdown("### 📊 Batch Execution Progress")

    # Check cancel flag — set by the Stop button between reruns
    if st.session_state.get('cancel_batch', False):
        for j in st.session_state.job_queue:
            if j['status'] == 'Pending':
                j['status'] = 'Cancelled'
                batch_metrics['jobs_cancelled'] = batch_metrics.get('jobs_cancelled', 0) + 1
        # Finalize batch metrics
        batch_metrics['total_time'] = time.time() - batch_start_time
        batch_metrics['total_chunks'] = batch_metrics['standard_chunks'] + batch_metrics['enhanced_chunks']
        st.session_state.batch_audit = batch_metrics
        st.session_state.batch_in_progress = False
        st.session_state.cancel_batch = False
        st.warning("⚠️ Batch stopped. Remaining jobs marked as Cancelled.")
        return

    # Find next job to process (skip completed/failed/cancelled)
    next_idx = None
    for idx, j in enumerate(st.session_state.job_queue):
        if j['status'] not in ['Completed', 'Completed with Warnings', 'Failed', 'Cancelled']:
            next_idx = idx
            break

    if next_idx is None:
        # All jobs done — finalize
        batch_metrics['total_time'] = time.time() - batch_start_time
        batch_metrics['total_chunks'] = batch_metrics['standard_chunks'] + batch_metrics['enhanced_chunks']
        st.session_state.batch_audit = batch_metrics
        st.session_state.batch_in_progress = False
        st.success("🎉 Batch Execution Finished")
        return

    # Process ONE job — this blocks for the duration of one job only
    _process_single_job(session, db, schema, stage_path, next_idx, total_jobs, batch_metrics)

    # Yield control via st.rerun().
    # Streamlit's ScriptRunner processes queued UI events (like the Stop
    # button click) before re-executing the script. This is the ONLY way
    # to get a responsive cancel in Streamlit's single-threaded model.
    st.rerun()
