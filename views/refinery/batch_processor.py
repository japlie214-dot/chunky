# views/refinery/batch_processor.py
# Core batch processing logic for the Doc Refinery package
import streamlit as st
import pandas as pd
import json
import os
import time
import tempfile
import datetime
from logger_config import log_action
from utils.core_utils import (
    PDFUtils, QualityInspector, RAGAnalytics, convert_from_bytes, save_optimized_image, clean_text_for_sql
)
from utils.snowflake_utils import (
    get_table_schema, run_cortex, execute_grant_with_retry
)
import prompts

# Safe Import: Snowpark
try:
    from snowflake.snowpark.functions import col
except Exception:
    col = None

from views.refinery.common import execute_sql_safe


# -----------------------------------------------------------------------------
# PLAN-16: CHUNK_REF Builder Helper
# -----------------------------------------------------------------------------

def _build_chunk_ref(rel_path: str, page_num, link: str = "") -> str:
    """
    Builds the canonical CHUNK_REF string per Golden Rule 4.
    Always uses the raw filename (not SQL-escaped) so single quotes are preserved.
    """
    base = f"Doc Source: {rel_path} | Page Num: {page_num}"
    return f"{base} | Link: {link}" if link else base


# -----------------------------------------------------------------------------
# run_batch_execution - Core Execution Logic
# -----------------------------------------------------------------------------

def run_batch_execution(session, db, schema, stage_path):
    """
    Core Execution Logic with Deep Error Handling and Granular Progress Tracking.
    Implements Layout Only, Vision Only, and Hybrid strategies with nested visual feedback.
    """
    # PLAN-16: Initialize chunk_cache if not yet present (safety net for direct invocations)
    if "chunk_cache" not in st.session_state:
        st.session_state.chunk_cache = []
    
    st.markdown("### 📊 Batch Execution Progress")
    batch_progress = st.progress(0, text="Initializing Batch...")
    batch_status = st.empty()
    
    total_jobs = len(st.session_state.job_queue)
    
    # Global Batch Metrics
    batch_metrics = {
        "jobs_completed": 0, "jobs_failed": 0, "jobs_warning": 0,
        "total_pages": 0, "total_chunks": 0,
        "layout_pages_processed": 0,  # Track pages specifically processed by Layout
        "vision_pages_processed": 0,  # Track unique pages touched by vision
        "standard_chunks": 0, "enhanced_chunks": 0,
        "total_time": 0.0,
        "time_layout": 0.0, "time_vision": 0.0,  # Time breakdown
        "credits_layout": 0.0, "credits_vision": 0.0,  # Cost breakdown
        "enhancement_breakdown": {}
    }
    
    batch_start_time = time.time()
    
    for idx, job in enumerate(st.session_state.job_queue):
        # Skip both completed and cancelled jobs (PLAN-01: Surgical Stop-Logic)
        if job['status'] in ['Completed', 'Cancelled']:
            continue
        
        job_alert = st.empty()
        job_start_time = time.time()
        job['metrics'] = {
            "start": job_start_time, "end": None, "duration": 0,
            "time_layout": 0.0, "time_vision": 0.0,  # Per job time
            "pages": 0, "layout_pages": 0,
            "vision_pages_list": set(),  # Track unique pages per job
            "vision_input_tokens": 0, "vision_output_tokens": 0,
            "standard_cnt": 0, "enhanced_cnt": 0, "types": {}
        }
        job['status'] = 'Running'
        
        # Progress Bar: Only count Green (Completed) jobs
        green_completions = sum(1 for j in st.session_state.job_queue[:idx] if j.get('status') == 'Completed')
        batch_progress.progress(green_completions / total_jobs if total_jobs > 0 else 0, text=f"Processing Job {idx+1} of {total_jobs}")
        batch_status.markdown(f"**🔄 Job {idx+1}/{total_jobs}:** `{job['file']}` → `{job['table']}`")
        
        pdf_bytes = None
        def get_pdf_bytes():
            nonlocal pdf_bytes
            if pdf_bytes is None:
                pdf_bytes = session.file.get_stream(f"{stage_path}/{job['file']}").read()
            return pdf_bytes
        
        # Resolve table path safely - Enforce Context by ignoring user-provided dots/prefixes
        table_name = job['table'].split('.')[-1]
        full_table = f'"{db}"."{schema}"."{table_name}"'
            
        chunk_sz, chunk_ov = job['params']
        
        try:
            # 1. SCOPE RESOLUTION & PAGE CALCULATION
            if job['scope'] == "Page Range":
                s_pg, e_pg = job['range']
                job_pages_count = (e_pg - s_pg) + 1
                pg_filter_sql = f"AND PAGE_NUMBER BETWEEN {s_pg} AND {e_pg}"
                json_opts = json.dumps({'mode': 'LAYOUT', 'page_filter': [{'start': s_pg, 'end': e_pg}]})
            else:
                s_pg, e_pg = 1, None
                real_pgs = PDFUtils.get_page_count(get_pdf_bytes())
                job_pages_count = real_pgs
                pg_filter_sql = ""
                json_opts = json.dumps({'mode': 'LAYOUT'})

            # 2. SURGICAL DELETE (with fault-tolerant error handling)
            if job['mode'] == 'SURGICAL':
                batch_status.markdown(f"**✂️ Job {idx+1}/{total_jobs}:** Surgical Cleanup...")
                safe_file_surgical = clean_text_for_sql(job['file'])
                del_sql = f"DELETE FROM {full_table} WHERE RELATIVE_PATH = '{safe_file_surgical}' {pg_filter_sql}"
                try:
                    ok, res = execute_sql_safe(session, del_sql)
                    if not ok:
                        raise Exception(str(res))
                except Exception as e:
                    log_action("SURGICAL_DELETE_ERROR", str(e))
                    job_alert.error(f"Critical Failure in Surgical Delete: {e}")
                    # Cancel subsequent jobs targeting the same table
                    cancelled_ids = []
                    for subsequent_job in st.session_state.job_queue[idx+1:]:
                        if subsequent_job['table'] == job['table'] and subsequent_job['status'] == 'Pending':
                            subsequent_job['status'] = 'Cancelled'
                            cancelled_ids.append(str(subsequent_job['id']))
                    if cancelled_ids:
                        st.warning(f"The following jobs targeting {job['table']} were Cancelled due to this failure: {', '.join(cancelled_ids)}")
                    raise Exception(f"Surgical Delete Failed: {e}")

            # 3. CENTRALIZED TABLE INITIALIZATION (PLAN-13 FIX)
            # Ensures table exists for ALL strategies (Layout, Vision, Hybrid)
            tbl_exists, tbl_cols, _ = get_table_schema(session, db, schema, table_name)
            
            if job['mode'] == 'OVERWRITE':
                batch_status.markdown(f"**🗑️ Job {idx+1}/{total_jobs}:** Recreating Table (OVERWRITE)...")
                init_sql = f'CREATE OR REPLACE TABLE {full_table} (RELATIVE_PATH VARCHAR, PAGE_NUMBER NUMBER, CHUNK VARCHAR, CHUNK_ID VARCHAR, CHUNK_TYPE VARCHAR, CHUNK_REF VARCHAR) COPY GRANTS'
                ok, res = execute_sql_safe(session, init_sql)
                if not ok:
                    raise Exception(f"Overwrite Initialization Failed: {res}")
            elif not tbl_exists:
                batch_status.markdown(f"**🆕 Job {idx+1}/{total_jobs}:** Creating New Table...")
                init_sql = f'CREATE TABLE {full_table} (RELATIVE_PATH VARCHAR, PAGE_NUMBER NUMBER, CHUNK VARCHAR, CHUNK_ID VARCHAR, CHUNK_TYPE VARCHAR, CHUNK_REF VARCHAR)'
                ok, res = execute_sql_safe(session, init_sql)
                if not ok:
                    raise Exception(f"Creation Initialization Failed: {res}")
            
            # Ensure CHUNK_TYPE exists for older tables
            if tbl_exists and tbl_cols and 'CHUNK_TYPE' not in [c.upper() for c in tbl_cols]:
                execute_sql_safe(session, f"ALTER TABLE {full_table} ADD COLUMN CHUNK_TYPE VARCHAR DEFAULT 'STANDARD'")
            # PLAN-16: Ensure CHUNK_REF exists for older tables (Golden Rule 13: no retrospective value updates)
            if tbl_exists and tbl_cols and 'CHUNK_REF' not in [c.upper() for c in tbl_cols]:
                execute_sql_safe(session, f"ALTER TABLE {full_table} ADD COLUMN CHUNK_REF VARCHAR DEFAULT NULL")

            # 4. STRATEGY EXECUTION
            # --- STRATEGY A: LAYOUT (SQL) ---
            if job['layout']:
                t_layout_start = time.time()
                batch_status.markdown(f"**🔧 Job {idx+1}/{total_jobs}:** Running Layout Parser (SQL)...")
                safe_file = clean_text_for_sql(job['file'])
                
                # PLAN-16: Strategy A refactored to SELECT→collect→augment→write_pandas
                # so that chunk data is available in the session backup (Flaw 1 fix).
                # The SELECT phase materialises all chunks in Python before any write.
                src_sql = f"""
                WITH PARSED AS (
                    SELECT '{safe_file}' AS RELATIVE_PATH,
                    SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(
                        TO_FILE('{stage_path}', '{safe_file}'), PARSE_JSON('{json_opts}')
                    ) AS J
                )
                SELECT
                    P.RELATIVE_PATH::VARCHAR           AS RELATIVE_PATH,
                    (pg.value:index::INT + 1)::NUMBER  AS PAGE_NUMBER,
                    ch.value::VARCHAR                  AS CHUNK,
                    CONCAT('CHK_', UUID_STRING())::VARCHAR AS CHUNK_ID,
                    'STANDARD'::VARCHAR                AS CHUNK_TYPE
                FROM PARSED P,
                     LATERAL FLATTEN(input => J:pages) pg,
                     LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(
                         pg.value:content::VARCHAR, 'markdown', {chunk_sz}, {chunk_ov}
                     )) ch
                """
                try:
                    collected_rows = session.sql(src_sql).collect()
                except Exception as e:
                    raise Exception(f"Layout SQL SELECT Failed: {e}")

                augmented_rows = []   # list of dicts for write_pandas
                link_val = job.get('link', '')

                # Helper to get value regardless of Snowflake's case-sensitivity (fixes truthy fallback bug)
                def get_val(row_dict, key, default):
                    if key.upper() in row_dict: return row_dict[key.upper()]
                    if key.lower() in row_dict: return row_dict[key.lower()]
                    return default

                for row in collected_rows:
                    r = row.as_dict()
                    # Normalise key casing (Snowpark may return lower or upper depending on driver)
                    rel  = get_val(r, 'RELATIVE_PATH', '')
                    pg_n = get_val(r, 'PAGE_NUMBER', 0)
                    chk  = get_val(r, 'CHUNK', '')
                    cid  = get_val(r, 'CHUNK_ID', '')
                    ctyp = get_val(r, 'CHUNK_TYPE', 'STANDARD')
                    c_ref = _build_chunk_ref(rel, pg_n, link_val)

                    augmented_rows.append({
                        'RELATIVE_PATH': rel, 'PAGE_NUMBER': pg_n, 'CHUNK': chk,
                        'CHUNK_ID': cid, 'CHUNK_TYPE': ctyp, 'CHUNK_REF': c_ref
                    })

                    # Session cache population (cap guard + deduplicated WARNING)
                    if len(st.session_state.chunk_cache) < 5000:
                        st.session_state.chunk_cache.append({
                            'job_id': job['id'], 'CHUNK_ID': cid, 'CHUNK': chk,
                            'CHUNK_TYPE': ctyp, 'PAGE_NUMBER': pg_n,
                            'RELATIVE_PATH': rel, 'CHUNK_REF': c_ref
                        })
                    elif not job.get('cache_limit_logged'):
                        log_action("CACHE_LIMIT_REACHED", {"file": job['file']}, level="WARNING")
                        job['cache_limit_logged'] = True

                # Write phase: commit augmented data to Snowflake
                if augmented_rows:
                    df_write = pd.DataFrame(augmented_rows)
                    try:
                        session.write_pandas(
                            df_write,
                            table_name=table_name,   # bare name (no db/schema prefix)
                            database=db,
                            schema=schema,
                            overwrite=False,
                            auto_create_table=False
                        )
                    except Exception as e:
                        raise Exception(f"Layout write_pandas Failed: {e}")

                cnt = len(augmented_rows)
                job['metrics']['layout_pages'] = job_pages_count
                job['metrics']['time_layout'] += (time.time() - t_layout_start)
                batch_metrics['layout_pages_processed'] += job_pages_count
                job['metrics']['standard_cnt'] += cnt
                batch_metrics['standard_chunks'] += cnt

            # --- STRATEGY B: HYBRID REPAIR (Python Loop) ---
            if job['layout'] and job['vision']:
                batch_status.markdown(f"**🔍 Job {idx+1}/{total_jobs}:** Analyzing Quality & Repairing Defects...")
                safe_file = clean_text_for_sql(job['file'])
                # PLAN-16: Include RELATIVE_PATH in SELECT for CHUNK_REF building
                q_sql = f"SELECT CHUNK_ID, PAGE_NUMBER, CHUNK, RELATIVE_PATH FROM {full_table} WHERE RELATIVE_PATH = '{safe_file}' {pg_filter_sql}"
                ok, rows = execute_sql_safe(session, q_sql)
                
                if ok and rows:
                    df = pd.DataFrame(rows)
                    df['STATUS'] = df['CHUNK'].apply(QualityInspector.inspect)
                    defects = df[df['STATUS'] != 'OK']
                    
                    if not defects.empty:
                        # Start timer specifically for AI operations
                        t_vis_start = time.time()
                        job_alert.warning(f"🛠️ Found {len(defects)} OCR defects in `{job['file']}`. Starting AI Repair...")
                        repair_progress = st.progress(0, text="Initializing Repairs...")
                        total_fix = len(defects)
                        processed_fix = 0
                        
                        try:
                            for pg_num in defects['PAGE_NUMBER'].unique():
                                imgs = convert_from_bytes(get_pdf_bytes(), first_page=pg_num, last_page=pg_num)
                                if not imgs: continue
                                
                                with tempfile.TemporaryDirectory() as td:
                                    img_name = f"repair_p{pg_num}"
                                    img_path = save_optimized_image(imgs[0], td, img_name, sub_folder=job['file'])
                                    if not img_path: continue
                                    
                                    safe_sub = PDFUtils.get_safe_folder(job['file'])
                                    full_stage_path = f"{stage_path}/_temp_images/{safe_sub}"
                                    session.file.put(img_path, full_stage_path, auto_compress=False, overwrite=True)
                                    rel_img_path = f"_temp_images/{safe_sub}/{os.path.basename(img_path)}"
                                    
                                    page_defects = defects[defects['PAGE_NUMBER'] == pg_num]
                                    for _, row in page_defects.iterrows():
                                        processed_fix += 1
                                        repair_progress.progress(processed_fix / total_fix, text=f"Repairing {processed_fix}/{total_fix}")
                                        
                                        prompt = prompts.get_silver_bullet_prompt(row['CHUNK'], f"Fix defect: {row['STATUS']}")
                                        # UPDATED CALL - unpack 3 values
                                        res_txt, p_tok, c_tok = run_cortex(session, prompt, stage_path, rel_img_path, model='claude-4-sonnet')
                                        
                                        if res_txt:
                                            # PLAN-16: Build CHUNK_REF and cache BEFORE the SQL write.
                                            # Use row['RELATIVE_PATH'] (original filename, not safe_file).
                                            c_ref = _build_chunk_ref(
                                                row['RELATIVE_PATH'], pg_num, job.get('link', '')
                                            )
                                            cache_entry = {
                                                'job_id': job['id'],
                                                'CHUNK_ID': row['CHUNK_ID'],
                                                'CHUNK': res_txt,
                                                'CHUNK_TYPE': 'ENHANCED',
                                                'PAGE_NUMBER': pg_num,
                                                'RELATIVE_PATH': row['RELATIVE_PATH'],
                                                'CHUNK_REF': c_ref
                                            }
                                            if len(st.session_state.chunk_cache) < 5000:
                                                st.session_state.chunk_cache.append(cache_entry)
                                            elif not job.get('cache_limit_logged'):
                                                log_action("CACHE_LIMIT_REACHED", {"file": job['file']}, level="WARNING")
                                                job['cache_limit_logged'] = True

                                            # PLAN-16: CHUNK_REF included in SET clause via bind parameter.
                                            upd_sql = f"UPDATE {full_table} SET CHUNK = ?, CHUNK_TYPE = 'ENHANCED', CHUNK_REF = ? WHERE CHUNK_ID = ?"
                                            try:
                                                session.sql(upd_sql, params=[res_txt, c_ref, row['CHUNK_ID']]).collect()
                                            except Exception as e:
                                                log_action("SQL_UPDATE_ERROR", {"error": str(e)})
                                            
                                            # Track unique pages processed by vision
                                            job['metrics']['vision_pages_list'].add(pg_num)
                                            
                                            # Token Capture
                                            job['metrics']['vision_input_tokens'] += p_tok
                                            job['metrics']['vision_output_tokens'] += c_tok
                                            
                                            etype = f"Repair: {row['STATUS']}"
                                            job['metrics']['enhanced_cnt'] += 1
                                            job['metrics']['types'][etype] = job['metrics']['types'].get(etype, 0) + 1
                                            batch_metrics['enhanced_chunks'] += 1
                                            batch_metrics['enhancement_breakdown'][etype] = batch_metrics['enhancement_breakdown'].get(etype, 0) + 1
                                            if job['metrics']['standard_cnt'] > 0:
                                                job['metrics']['standard_cnt'] -= 1
                                                batch_metrics['standard_chunks'] -= 1
                        except Exception as e:
                            log_action("REPAIR_ERROR", {"job": job['id'], "error": str(e)})
                        finally:
                            repair_progress.empty()
                            job_alert.empty()
                            job['metrics']['time_vision'] += (time.time() - t_vis_start)

            # --- STRATEGY C: VISION ONLY (Python Loop) ---
            if job['vision'] and not job['layout']:
                t_vis_start = time.time()
                batch_status.markdown(f"**👁️ Job {idx+1}/{total_jobs}:** Running Vision Parser...")
                
                # PLAN-16: Use raw_file for bind parameters (not safe_file which is SQL-escaped)
                raw_file = job['file']
                safe_file = clean_text_for_sql(job['file'])  # Still needed for SELECT-back WHERE clause
                target_range = range(s_pg, e_pg + 1) if job['scope'] == "Page Range" else range(1, job_pages_count + 1)
                vision_progress = st.progress(0, text="Initializing Vision...")
                total_v_pgs = len(target_range)
                
                # Helper to get value regardless of Snowflake's case-sensitivity (fixes truthy fallback bug)
                def get_val(row_dict, key, default):
                    if key.upper() in row_dict: return row_dict[key.upper()]
                    if key.lower() in row_dict: return row_dict[key.lower()]
                    return default
                
                for i, pg in enumerate(target_range):
                    vision_progress.progress((i + 1) / total_v_pgs, text=f"Processing Page {pg}")
                    imgs = convert_from_bytes(get_pdf_bytes(), first_page=pg, last_page=pg)
                    if imgs:
                        with tempfile.TemporaryDirectory() as td:
                            img_name = f"vis_{job['id']}_{pg}"
                            img_path = save_optimized_image(imgs[0], td, img_name, sub_folder=job['file'])
                            if not img_path: continue
                            
                            safe_sub = PDFUtils.get_safe_folder(job['file'])
                            full_stage_path = f"{stage_path}/_temp_images/{safe_sub}"
                            session.file.put(img_path, full_stage_path, auto_compress=False, overwrite=True)
                            rel_img_path = f"_temp_images/{safe_sub}/{os.path.basename(img_path)}"
                            
                            prompt = prompts.get_vision_extraction_prompt()
                            # UPDATED CALL - unpack 3 values
                            res_txt, p_tok, c_tok = run_cortex(session, prompt, stage_path, rel_img_path, model='claude-4-sonnet')
                            
                            if res_txt:
                                # PLAN-16: Build CHUNK_REF using raw_file (not safe_file)
                                c_ref = _build_chunk_ref(raw_file, pg, job.get('link', ''))

                                # PLAN-16: CHUNK_REF bound via parameter — never string-interpolated.
                                ins_sql = f"""
                                INSERT INTO {full_table}
                                    (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE, CHUNK_REF)
                                SELECT ?, ?, C.VALUE::VARCHAR,
                                       CONCAT('CHK_', UUID_STRING()), 'ENHANCED', ?
                                FROM LATERAL FLATTEN(
                                    INPUT => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(
                                        ?, 'markdown', ?, ?
                                    )
                                ) C
                                """
                                # FIX: Use raw_file for bind parameters, NOT safe_file (avoids double-escaping)
                                res_i = session.sql(
                                    ins_sql, params=[raw_file, pg, c_ref, res_txt, chunk_sz, chunk_ov]
                                ).collect()
                                inserted_cnt = int(res_i[0][0])

                                # PLAN-16: SELECT-back to retrieve actual CHUNK_IDs (generated by
                                # UUID_STRING() inside Snowflake) for session cache population.
                                sel_back = (
                                    f"SELECT CHUNK_ID, CHUNK, CHUNK_TYPE, PAGE_NUMBER, "
                                    f"RELATIVE_PATH, CHUNK_REF FROM {full_table} "
                                    f"WHERE RELATIVE_PATH = ? AND PAGE_NUMBER = ? ORDER BY CHUNK_ID"
                                )
                                # FIX: Use raw_file for bind parameter in SELECT-back
                                inserted_rows = session.sql(sel_back, params=[raw_file, pg]).collect()
                                for r in inserted_rows:
                                    rd = r.as_dict()
                                    cache_entry = {
                                        'job_id':       job['id'],
                                        'CHUNK_ID':     get_val(rd, 'CHUNK_ID', ''),
                                        'CHUNK':        get_val(rd, 'CHUNK', ''),
                                        'CHUNK_TYPE':   get_val(rd, 'CHUNK_TYPE', 'ENHANCED'),
                                        'PAGE_NUMBER':  get_val(rd, 'PAGE_NUMBER', 0),
                                        'RELATIVE_PATH':get_val(rd, 'RELATIVE_PATH', ''),
                                        'CHUNK_REF':    get_val(rd, 'CHUNK_REF', ''),
                                    }
                                    if len(st.session_state.chunk_cache) < 5000:
                                        st.session_state.chunk_cache.append(cache_entry)
                                    elif not job.get('cache_limit_logged'):
                                        log_action("CACHE_LIMIT_REACHED", {"file": job['file']}, level="WARNING")
                                        job['cache_limit_logged'] = True

                                # Track unique pages processed by vision
                                job['metrics']['vision_pages_list'].add(pg)
                                
                                # Token Capture
                                job['metrics']['vision_input_tokens'] += p_tok
                                job['metrics']['vision_output_tokens'] += c_tok
                                
                                job['metrics']['enhanced_cnt'] += inserted_cnt
                                etype = "Vision Extraction"
                                job['metrics']['types'][etype] = job['metrics']['types'].get(etype, 0) + inserted_cnt
                                batch_metrics['enhanced_chunks'] += inserted_cnt
                                batch_metrics['enhancement_breakdown'][etype] = batch_metrics['enhancement_breakdown'].get(etype, 0) + inserted_cnt
                vision_progress.empty()
                
                job['metrics']['time_vision'] += (time.time() - t_vis_start)

            # Job Finalization & Cost Calc
            # Layout Cost: 3.33 credits per 1000 pages
            c_layout = (job['metrics'].get('layout_pages', 0) / 1000) * 3.33
            
            # Vision Cost calculation using existing Registry
            pricing = RAGAnalytics.PRICING_REGISTRY.get('claude-4-sonnet', {'input': 1.50, 'output': 7.50})
            v_in = job['metrics']['vision_input_tokens']
            v_out = job['metrics']['vision_output_tokens']
            c_vision = (v_in / 1_000_000 * pricing['input']) + (v_out / 1_000_000 * pricing['output'])
            
            # Batch Aggregation
            batch_metrics['time_layout'] += job['metrics']['time_layout']
            batch_metrics['time_vision'] += job['metrics']['time_vision']
            batch_metrics['credits_layout'] += c_layout
            batch_metrics['credits_vision'] += c_vision
            
            # Track unique vision pages
            v_pgs_count = len(job['metrics']['vision_pages_list'])
            batch_metrics['vision_pages_processed'] += v_pgs_count
            
            # PLAN-13: Conditionally apply grants exclusively to newly created tables
            grant_roles = job.get('grant_roles', [])
            if grant_roles:
                user_email = st.session_state.auth_context.get('user', '') if 'auth_context' in st.session_state else ''
                grant_statuses = []
                for role in grant_roles:
                    grant_sql = f'GRANT ALL PRIVILEGES ON TABLE {full_table} TO ROLE "{role.upper()}"'
                    grant_res = execute_grant_with_retry(session, grant_sql, user_email, role.upper())
                    grant_statuses.append(grant_res)
                
                if "Failed" in grant_statuses:
                    job['status'] = 'Completed with Warnings'
                    job['metrics']['access_granted'] = 'Partial/Failed'
                    batch_metrics['jobs_warning'] = batch_metrics.get('jobs_warning', 0) + 1
                    job_alert.warning(f"⚠️ Job completed but some grants failed. Manual permission review required.")
                    # PLAN-16: Persist completion timestamp for CSV filename (overwritten on re-run).
                    job['metrics']['completion_ts'] = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                else:
                    job['status'] = 'Completed'
                    job['metrics']['access_granted'] = ", ".join(grant_roles)
                    st.toast(f"Access granted to: {', '.join(grant_roles)}")
                    batch_metrics['jobs_completed'] += 1
                    # PLAN-16: Persist completion timestamp for CSV filename (overwritten on re-run).
                    job['metrics']['completion_ts'] = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            else:
                job['status'] = 'Completed'
                job['metrics']['access_granted'] = 'Skipped (Existing Table)'
                batch_metrics['jobs_completed'] += 1
                # PLAN-16: Persist completion timestamp for CSV filename (overwritten on re-run).
                job['metrics']['completion_ts'] = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                
            job_end_time = time.time()
            job['metrics']['end'] = job_end_time
            job['metrics']['duration'] = job_end_time - job_start_time
            job['metrics']['pages'] = job_pages_count
            batch_metrics['total_pages'] += job_pages_count
            
        except Exception as e:
            job['status'] = 'Failed'
            job['metrics']['error'] = str(e)
            batch_metrics['jobs_failed'] += 1
            log_action("JOB_FAILED", {"id": job['id'], "error": str(e)})
            st.error(f"Job {job['id']} Failed: {e}")
            job_alert.empty()
        
        finally:
            # --- PERSISTENCE START ---
            if "ingestion_history" not in st.session_state:
                st.session_state.ingestion_history = []
            
            # Upsert logic based on ID to avoid duplicates ensuring failed jobs are also recorded
            st.session_state.ingestion_history = [
                j for j in st.session_state.ingestion_history if j['id'] != job['id']
            ]
            st.session_state.ingestion_history.append(job)
            # --- PERSISTENCE END ---

    batch_metrics['total_time'] = time.time() - batch_start_time
    batch_metrics['total_chunks'] = batch_metrics['standard_chunks'] + batch_metrics['enhanced_chunks']
    st.session_state.batch_audit = batch_metrics
    
    # PLAN-01: Progress bar shows only Green completions
    green_completions = sum(1 for j in st.session_state.job_queue if j.get('status') == 'Completed')
    batch_progress.progress(green_completions / total_jobs if total_jobs > 0 else 1.0, text="Batch Complete")
    time.sleep(1)
    batch_progress.empty()
    batch_status.empty()
    st.success("🎉 Batch Execution Finished")
