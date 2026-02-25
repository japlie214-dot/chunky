# views/refinery/batch_processor.py
# Core batch processing logic for the Doc Refinery package
import streamlit as st
import pandas as pd
import json
import os
import time
import tempfile
from logger_config import log_action
from utils.core_utils import (
    PDFUtils, QualityInspector, RAGAnalytics, convert_from_bytes, save_optimized_image, clean_text_for_sql
)
from utils.snowflake_utils import (
    get_table_schema, run_cortex, execute_grant_with_retry
)
from utils.auth_utils import resolve_active_target_role
import prompts

# Safe Import: Snowpark
try:
    from snowflake.snowpark.functions import col
except Exception:
    col = None

from views.refinery.common import execute_sql_safe

# -----------------------------------------------------------------------------
# run_batch_execution - Core Execution Logic
# -----------------------------------------------------------------------------

def run_batch_execution(session, db, schema, stage_path):
    """
    Core Execution Logic with Deep Error Handling and Granular Progress Tracking.
    Implements Layout Only, Vision Only, and Hybrid strategies with nested visual feedback.
    """
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

            # 3. STRATEGY EXECUTION
            # --- STRATEGY A: LAYOUT (SQL) ---
            if job['layout']:
                t_layout_start = time.time()
                batch_status.markdown(f"**🔧 Job {idx+1}/{total_jobs}:** Running Layout Parser (SQL)...")
                safe_file = clean_text_for_sql(job['file'])
                
                # PLAN-01: Refactored to remove DIRECTORY() dependency
                # FIX: Ensure the stage path retains the '@' prefix for the TO_FILE function
                # We use the raw stage_path (e.g., '@DEV_DB.JPFA.STG_CSSWEB_DOCS')
                src_sql = f"""
                WITH PARSED AS (
                    SELECT '{safe_file}' AS RELATIVE_PATH,
                    SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(TO_FILE('{stage_path}', '{safe_file}'), PARSE_JSON('{json_opts}')) AS J
                )
                SELECT
                    P.RELATIVE_PATH::VARCHAR AS RELATIVE_PATH,
                    (pg.value:index::INT+1)::NUMBER AS PAGE_NUMBER,
                    ch.value::VARCHAR AS CHUNK,
                    CONCAT('CHK_', UUID_STRING())::VARCHAR AS CHUNK_ID,
                    'STANDARD'::VARCHAR AS CHUNK_TYPE
                FROM PARSED P, LATERAL FLATTEN(input => J:pages) pg,
                LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(pg.value:content::VARCHAR, 'markdown', {chunk_sz}, {chunk_ov})) ch
                """
                
                if job['mode'] == 'OVERWRITE':
                    final_sql = f"CREATE OR REPLACE TABLE {full_table} AS {src_sql}"
                    ok, res = execute_sql_safe(session, final_sql)
                else:
                    exists, cols, err = get_table_schema(session, db, schema, table_name)
                    if not exists:
                        final_sql = f"CREATE TABLE {full_table} AS {src_sql}"
                    else:
                        if 'CHUNK_TYPE' not in cols:
                            execute_sql_safe(session, f"ALTER TABLE {full_table} ADD COLUMN CHUNK_TYPE VARCHAR DEFAULT 'STANDARD'")
                        final_sql = f"INSERT INTO {full_table} (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE) {src_sql}"
                    ok, res = execute_sql_safe(session, final_sql)
                
                if not ok: raise Exception(f"Layout SQL Failed: {res}")
                
                # Metric Capture
                job['metrics']['layout_pages'] = job_pages_count  # Layout charges per page
                job['metrics']['time_layout'] += (time.time() - t_layout_start)
                batch_metrics['layout_pages_processed'] += job_pages_count  # Increment only when layout runs
                
                try:
                    # Snowflake CREATE TABLE AS returns a string status. INSERT returns a count.
                    # We query the count manually if a CREATE or REPLACE command was executed.
                    if job['mode'] == 'OVERWRITE' or 'CREATE TABLE' in final_sql:
                        count_sql = f"SELECT COUNT(*) FROM {full_table} WHERE RELATIVE_PATH = '{safe_file}' {pg_filter_sql}"
                        ok_c, res_c = execute_sql_safe(session, count_sql)
                        cnt = int(res_c[0][0]) if ok_c else 0
                    else:
                        try:
                            cnt = int(res[0][0])
                        except (ValueError, TypeError):
                            cnt = 0
                    job['metrics']['standard_cnt'] += cnt
                    batch_metrics['standard_chunks'] += cnt
                except Exception: pass

            # --- STRATEGY B: HYBRID REPAIR (Python Loop) ---
            if job['layout'] and job['vision']:
                batch_status.markdown(f"**🔍 Job {idx+1}/{total_jobs}:** Analyzing Quality & Repairing Defects...")
                safe_file = clean_text_for_sql(job['file'])
                q_sql = f"SELECT CHUNK_ID, PAGE_NUMBER, CHUNK FROM {full_table} WHERE RELATIVE_PATH = '{safe_file}' {pg_filter_sql}"
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
                                            # Use bind variables for large text chunks instead of string interpolation
                                            upd_sql = f"UPDATE {full_table} SET CHUNK = ?, CHUNK_TYPE = 'ENHANCED' WHERE CHUNK_ID = ?"
                                            try:
                                                session.sql(upd_sql, params=[res_txt, row['CHUNK_ID']]).collect()
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
                
                # Check Schema Cache
                if 'table_schema_cache' not in st.session_state: st.session_state.table_schema_cache = {}
                if job['table'] not in st.session_state.table_schema_cache:
                    exists, cols, err = get_table_schema(session, db, schema, job['table'])
                    st.session_state.table_schema_cache[job['table']] = (exists, cols)
                
                exists, cols = st.session_state.table_schema_cache[job['table']]
                if exists and 'CHUNK_TYPE' not in cols:
                    execute_sql_safe(session, f"ALTER TABLE {full_table} ADD COLUMN CHUNK_TYPE VARCHAR DEFAULT 'STANDARD'")
                
                safe_file = clean_text_for_sql(job['file'])
                try:
                    target_range = range(s_pg, e_pg + 1) if job['scope'] == "Page Range" else range(1, job_pages_count + 1)
                    vision_progress = st.progress(0, text="Initializing Vision...")
                    total_v_pgs = len(target_range)
                    
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
                                    # Use bind variables in the SELECT part of the INSERT to handle large strings safely
                                    ins_sql = f"""
                                    INSERT INTO {full_table} (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE)
                                    SELECT ?, ?, C.VALUE::VARCHAR, CONCAT('CHK_', UUID_STRING()), 'ENHANCED'
                                    FROM LATERAL FLATTEN(INPUT => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(?, 'markdown', ?, ?)) C
                                    """
                                    try:
                                        res_i = session.sql(ins_sql, params=[safe_file, pg, res_txt, chunk_sz, chunk_ov]).collect()
                                        inserted_cnt = int(res_i[0][0])
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
                                    except Exception as e:
                                        log_action("VISION_INSERT_ERROR", {"error": str(e)})
                    vision_progress.empty()
                except Exception as e:
                    log_action("VISION_ONLY_ERROR", {"job": job['id'], "error": str(e)})
                
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
            
            # PLAN-01: RBAC GRANT EXECUTION (Post-Job)
            user_email = st.session_state.auth_context.get('user', '') if 'auth_context' in st.session_state else ''
            resolved_role = resolve_active_target_role(session, user_email)
            grant_sql = f'GRANT ALL PRIVILEGES ON TABLE {full_table} TO ROLE "{resolved_role.upper()}"'
            grant_res = execute_grant_with_retry(session, grant_sql, user_email, resolved_role.upper())
            
            if grant_res == "Failed":
                job['status'] = 'Completed with Warnings'
                job['metrics']['access_granted'] = 'Failed'
                batch_metrics['jobs_warning'] = batch_metrics.get('jobs_warning', 0) + 1
                job_alert.warning(f"⚠️ Job completed but grant failed. Manual permission review required.")
            else:
                job['status'] = 'Completed'
                job['metrics']['access_granted'] = grant_res
                st.toast(f"Access granted to role: {grant_res}")
                batch_metrics['jobs_completed'] += 1
                
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
