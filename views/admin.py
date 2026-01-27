# views/admin.py
import streamlit as st
import pandas as pd
import json
import os
import time
import tempfile
from logger_config import log_action
from utils.core_utils import (
    PDFUtils, QualityInspector, RAGAnalytics, Image, convert_from_bytes, save_optimized_image
)
from utils.snowflake_utils import (
    get_snowpark_session, clean_text_for_sql, get_table_schema, run_cortex
)
import prompts

# Safe Import: Snowpark
try:
    from snowflake.snowpark.functions import col
except Exception:
    col = None

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
        # Log with SQL snippet for debugging
        log_action("SQL_EXECUTION_ERROR", {
            "error": err_msg,
            "sql_snippet": sql[:500] if len(sql) > 500 else sql
        })
        return False, err_msg

# -----------------------------------------------------------------------------
# run_batch_execution - Core Execution Logic with Deep Error Handling
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
        "jobs": 0, "pages": 0, "standard": 0, "enhanced": 0,
        "total_time": 0.0, "enhancement_breakdown": {}
    }
    
    batch_start_time = time.time()
    
    for idx, job in enumerate(st.session_state.job_queue):
        # SKIP LOGIC: Ignore already completed jobs to prevent re-execution
        if job['status'] == 'Completed':
            continue
        
        # PLAN-13: Placeholder for per-job alerts to prevent notification stacking
        job_alert = st.empty()
        
        job_start_time = time.time()
        job['metrics'] = {
            "start": job_start_time,
            "end": None,
            "duration": 0,
            "pages": 0,
            "standard_cnt": 0,
            "enhanced_cnt": 0,
            "types": {}  # Tracks types of enhancements (e.g., "Vision", "Repair_Table")
        }
        
        job['status'] = 'Running'
        
        # Global Status Update
        batch_status.markdown(f"**🔄 Job {idx+1}/{total_jobs}:** `{job['file']}` → `{job['table']}`")
        batch_progress.progress(idx / total_jobs, text=f"Processing Job {idx+1} of {total_jobs}")
        
        job_pages = 0
        pdf_bytes = None
        
        def get_pdf_bytes():
            """Lazy-load and cache PDF bytes to avoid redundant streaming."""
            nonlocal pdf_bytes
            if pdf_bytes is None:
                pdf_bytes = session.file.get_stream(f"{stage_path}/{job['file']}").read()
            return pdf_bytes
        
        # Resolve table path safely to prevent double-prefixing
        if "." not in job['table']:
            full_table = f"{db}.{schema}.{job['table']}"
        else:
            full_table = job['table']
            
        chunk_sz, chunk_ov = job['params']
        
        try:
            # 1. SCOPE RESOLUTION
            if job['scope'] == "Page Range":
                s_pg, e_pg = job['range']
                pg_filter_sql = f"AND PAGE_NUMBER BETWEEN {s_pg} AND {e_pg}"
                json_opts = json.dumps({'mode': 'LAYOUT', 'page_filter': [{'start': s_pg, 'end': e_pg}]})
            else:
                s_pg, e_pg = 1, None
                pg_filter_sql = ""
                json_opts = json.dumps({'mode': 'LAYOUT'})

            # 2. SURGICAL DELETE
            if job['mode'] == 'SURGICAL':
                batch_status.markdown(f"**✂️ Job {idx+1}/{total_jobs}:** Surgical Cleanup...")
                del_sql = f"DELETE FROM {full_table} WHERE RELATIVE_PATH = '{job['file']}' {pg_filter_sql}"
                ok, res = execute_sql_safe(session, del_sql)
                if not ok:
                    raise Exception(f"Surgical Delete Failed: {res}")

            # 3. STRATEGY EXECUTION
            
            # --- STRATEGY A: LAYOUT (SQL) ---
            if job['layout']:
                batch_status.markdown(f"**🔧 Job {idx+1}/{total_jobs}:** Running Layout Parser (SQL)...")
                
                # Base Query with clean_text_for_sql applied to file path
                safe_file = clean_text_for_sql(job['file'])
                
                src_sql = f"""
                WITH PDF AS (SELECT RELATIVE_PATH, TO_FILE('{stage_path}', RELATIVE_PATH) AS F FROM DIRECTORY({stage_path}) WHERE RELATIVE_PATH = '{safe_file}'),
                PARSED AS (SELECT RELATIVE_PATH, SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(F, PARSE_JSON('{json_opts}')) AS J FROM PDF)
                SELECT
                    P.RELATIVE_PATH,
                    pg.value:index::INT+1 AS PAGE_NUMBER,
                    ch.value::VARCHAR AS CHUNK,
                    CONCAT('CHK_', UUID_STRING()) AS CHUNK_ID,
                    'STANDARD' AS CHUNK_TYPE
                FROM PARSED P, LATERAL FLATTEN(input => J:pages) pg,
                LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(pg.value:content::VARCHAR, 'markdown', {chunk_sz}, {chunk_ov})) ch
                """
                
                if job['mode'] == 'OVERWRITE':
                    final_sql = f"CREATE OR REPLACE TABLE {full_table} AS {src_sql}"
                    ok, res = execute_sql_safe(session, final_sql)
                else:
                    # Append/Surgical
                    exists, cols, err = get_table_schema(session, db, schema, job['table'])
                    if not exists:
                        final_sql = f"CREATE TABLE {full_table} AS {src_sql}"
                    else:
                        # Ensure CHUNK_TYPE exists
                        if 'CHUNK_TYPE' not in cols:
                            execute_sql_safe(session, f"ALTER TABLE {full_table} ADD COLUMN CHUNK_TYPE VARCHAR DEFAULT 'STANDARD'")
                        final_sql = f"INSERT INTO {full_table} (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE) {src_sql}"
                    ok, res = execute_sql_safe(session, final_sql)
                
                if not ok:
                    raise Exception(f"Layout SQL Failed: {res}")
                
                # METRICS CAPTURE: Standard Chunks
                try:
                    if job['mode'] == 'OVERWRITE':
                        # For CREATE AS, fetch the count from the table
                        count_sql = f"SELECT COUNT(*) FROM {full_table} WHERE RELATIVE_PATH = '{safe_file}' {pg_filter_sql}"
                        ok_c, res_c = execute_sql_safe(session, count_sql)
                        cnt = res_c[0][0] if ok_c else 0
                    else:
                        # For INSERT, res[0][0] is the integer count of rows inserted
                        cnt = int(res[0][0])
                    
                    job['metrics']['standard_cnt'] += cnt
                    batch_metrics['standard'] += cnt
                except Exception:
                    pass

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
                        # PLAN-13: Use placeholder and clarify wording (OCR defects)
                        job_alert.warning(f"🛠️ Found {len(defects)} OCR defects in `{job['file']}`. Starting AI Repair...")
                        repair_progress = st.progress(0, text="Initializing Repairs...")
                        
                        total_fix = len(defects)
                        processed_fix = 0
                        
                        try:
                            for pg_num in defects['PAGE_NUMBER'].unique():
                                # Render page image once per page, even if multiple defects exist
                                imgs = convert_from_bytes(get_pdf_bytes(), first_page=pg_num, last_page=pg_num)
                                if not imgs:
                                    continue
                                
                                with tempfile.TemporaryDirectory() as td:
                                    # HIERARCHICAL STORAGE: _temp_images/<filename>/...
                                    img_name = f"repair_p{pg_num}"
                                    # Use sub_folder to prevent filename collisions
                                    img_path = save_optimized_image(imgs[0], td, img_name, sub_folder=job['file'])
                                    if not img_path:
                                        log_action("BATCH_REPAIR_ERROR", {"job": job['id'], "error": f"Failed to save image for page {pg_num}"})
                                        continue
                                    # Upload to hierarchical stage path
                                    safe_sub = PDFUtils.get_safe_folder(job['file'])
                                    full_stage_path = f"{stage_path}/_temp_images/{safe_sub}"
                                    session.file.put(img_path, full_stage_path, auto_compress=False, overwrite=True)
                                    rel_img_path = f"_temp_images/{safe_sub}/{os.path.basename(img_path)}"
                                    
                                    # Process all defects for this page
                                    page_defects = defects[defects['PAGE_NUMBER'] == pg_num]
                                    for _, row in page_defects.iterrows():
                                        processed_fix += 1
                                        repair_progress.progress(processed_fix / total_fix, text=f"Repairing {processed_fix}/{total_fix}: Page {pg_num} ({row['STATUS']})")
                                        
                                        # Run Cortex
                                        prompt = prompts.get_silver_bullet_prompt(row['CHUNK'], f"Fix defect: {row['STATUS']}")
                                        res_txt = run_cortex(session, prompt, stage_path, rel_img_path)
                                        
                                        if res_txt:
                                            clean_chunk = clean_text_for_sql(res_txt)
                                            upd_sql = f"UPDATE {full_table} SET CHUNK = '{clean_chunk}', CHUNK_TYPE = 'ENHANCED' WHERE CHUNK_ID = '{row['CHUNK_ID']}'"
                                            execute_sql_safe(session, upd_sql)
                                            
                                            # METRIC TRACKING
                                            etype = f"Repair: {row['STATUS']}"
                                            job['metrics']['enhanced_cnt'] += 1
                                            job['metrics']['types'][etype] = job['metrics']['types'].get(etype, 0) + 1
                                            
                                            batch_metrics['enhanced'] += 1
                                            batch_metrics['enhancement_breakdown'][etype] = batch_metrics['enhancement_breakdown'].get(etype, 0) + 1
                                            
                                            if job['metrics']['standard_cnt'] > 0:
                                                job['metrics']['standard_cnt'] -= 1
                                                batch_metrics['standard'] -= 1
                        except Exception as e:
                            log_action("REPAIR_ERROR", {"job": job['id'], "error": str(e)})
                        finally:
                            repair_progress.empty()
                            job_alert.empty()  # PLAN-13: Clear alert placeholder after repair completes

            # --- STRATEGY C: VISION ONLY (Python Loop) ---
            if job['vision'] and not job['layout']:
                batch_status.markdown(f"**👁️ Job {idx+1}/{total_jobs}:** Running Vision Parser...")
                
                # Verify column existence as Strategy A (Layout) was skipped
                exists, cols, err = get_table_schema(session, db, schema, job['table'])
                if exists and 'CHUNK_TYPE' not in cols:
                    execute_sql_safe(session, f"ALTER TABLE {full_table} ADD COLUMN CHUNK_TYPE VARCHAR DEFAULT 'STANDARD'")
                
                # Define safe_file for this block to prevent SQL injection
                safe_file = clean_text_for_sql(job['file'])
                
                try:
                    real_pgs = PDFUtils.get_page_count(get_pdf_bytes())
                    job_pages = real_pgs
                    effective_end = e_pg if e_pg is not None else real_pgs
                    target_range = range(s_pg, min(effective_end, real_pgs) + 1)
                    
                    vision_progress = st.progress(0, text="Initializing Vision...")
                    total_v_pgs = len(target_range)
                    
                    for i, pg in enumerate(target_range):
                        vision_progress.progress((i + 1) / total_v_pgs, text=f"Processing Page {pg} of {effective_end}")
                        imgs = convert_from_bytes(get_pdf_bytes(), first_page=pg, last_page=pg)
                        if imgs:
                            with tempfile.TemporaryDirectory() as td:
                                # HIERARCHICAL STORAGE: _temp_images/<filename>/...
                                img_name = f"vis_{job['id']}_{pg}"
                                # Use sub_folder to prevent filename collisions
                                img_path = save_optimized_image(imgs[0], td, img_name, sub_folder=job['file'])
                                if not img_path:
                                    log_action("VISION_ONLY_ERROR", {"job": job['id'], "error": f"Failed to save image for page {pg}"})
                                    continue
                                # Upload to hierarchical stage path
                                safe_sub = PDFUtils.get_safe_folder(job['file'])
                                full_stage_path = f"{stage_path}/_temp_images/{safe_sub}"
                                session.file.put(img_path, full_stage_path, auto_compress=False, overwrite=True)
                                rel_img_path = f"_temp_images/{safe_sub}/{os.path.basename(img_path)}"
                                
                                prompt = prompts.get_vision_extraction_prompt()
                                res_txt = run_cortex(session, prompt, stage_path, rel_img_path)
                                
                                if res_txt:
                                    clean_chunk = clean_text_for_sql(res_txt)
                                    # Recursive split via SQL helper
                                    ins_sql = f"""
                                    INSERT INTO {full_table} (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE)
                                    SELECT '{safe_file}', {pg}, C.VALUE::VARCHAR, CONCAT('CHK_', UUID_STRING()), 'ENHANCED'
                                    FROM TABLE(SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER('{clean_chunk}', 'markdown', {chunk_sz}, {chunk_ov})) C
                                    """
                                    ok_i, res_i = execute_sql_safe(session, ins_sql)
                                    if ok_i:
                                        # METRIC TRACKING: Get actual chunk count from the INSERT result
                                        inserted_cnt = int(res_i[0][0])
                                        job['metrics']['enhanced_cnt'] += inserted_cnt
                                        etype = "Vision Extraction"
                                        job['metrics']['types'][etype] = job['metrics']['types'].get(etype, 0) + inserted_cnt
                                        
                                        batch_metrics['enhanced'] += inserted_cnt
                                        batch_metrics['enhancement_breakdown'][etype] = batch_metrics['enhancement_breakdown'].get(etype, 0) + inserted_cnt
                    vision_progress.empty()
                except Exception as e:
                    log_action("VISION_ONLY_ERROR", {"job": job['id'], "error": str(e)})

            if job_pages == 0:
                try:
                    job_pages = PDFUtils.get_page_count(get_pdf_bytes()) if pdf_bytes else 1
                except:
                    job_pages = 1

            # Job Finalization
            job_alert.empty()  # PLAN-13: Clear alert placeholder on successful job completion
            job['status'] = 'Completed'
            job_end_time = time.time()
            job['metrics']['end'] = job_end_time
            job['metrics']['duration'] = job_end_time - job_start_time
            
            job['metrics']['pages'] = job_pages
            batch_metrics['pages'] += job_pages
            
            total_chunks = job['metrics']['standard_cnt'] + job['metrics']['enhanced_cnt']
            job['metrics']['throughput_cps'] = total_chunks / job['metrics']['duration'] if job['metrics']['duration'] > 0 else 0
            
            batch_metrics['jobs'] += 1
            
        except Exception as e:
            job['status'] = 'Failed'
            job['metrics']['error'] = str(e)
            log_action("JOB_FAILED", {"id": job['id'], "error": str(e)})
            st.error(f"Job {job['id']} Failed. See System Logs for details.")
            job_alert.empty()  # PLAN-13: Clear alert placeholder on job failure

    # Batch Finalization
    batch_metrics['total_time'] = time.time() - batch_start_time
    st.session_state.batch_audit = batch_metrics  # Store globally
    
    batch_progress.progress(1.0, text="Batch Complete")
    time.sleep(1)
    batch_progress.empty()
    st.success("🎉 Batch Execution Finished")
    
    # PLAN-12: Display summary instead of restarting app
    # This prevents the queue from disappearing visually due to lack of refresh
    if st.session_state.job_queue:
        st.markdown("### 🏁 Execution Summary")
        summary_data = [{
            "File": j["file"],
            "Status": j["status"],
            "Pages": j.get("metrics", {}).get("pages", 0),
            "Chunks": j.get("metrics", {}).get("standard_cnt", 0) + j.get("metrics", {}).get("enhanced_cnt", 0)
        } for j in st.session_state.job_queue]
        st.dataframe(pd.DataFrame(summary_data), use_container_width=True)
    
    # st.rerun()  <-- REMOVED per PLAN-12

# -----------------------------------------------------------------------------
# SUB-RENDERERS (Tabs)
# -----------------------------------------------------------------------------

def render_config_tab(session):
    """
    Render the Configuration Tab.
    Handles Infrastructure, Job Building, and Queue Management.
    """
    st.subheader("1. Configuration & Job Management")

    # Initialize State
    if 'job_queue' not in st.session_state:
        st.session_state.job_queue = []
    if 'file_metadata_cache' not in st.session_state:
        st.session_state.file_metadata_cache = {}

    # sub-checklist: Move "Infrastructure & Source" section
    with st.expander("🏛️ Infrastructure & Source", expanded=True):
        c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
        db = c1.text_input("Database", value=st.session_state.config.get("db", "SBOX_DB"), key="ing_db")
        schema = c2.text_input("Schema", value=st.session_state.config.get("schema", "AI_SB"), key="ing_sch")
        stage = c3.text_input("Stage", value="DOCS", key="ing_stg")
        
        stage_path = f"@{db}.{schema}.{stage}"
        pdf_files = []
        try:
            files = session.sql(f"LIST {stage_path} PATTERN='.*\\.pdf'").collect()
            pdf_files = [os.path.basename(f['name']) for f in files]
        except Exception as e:
            st.warning(f"Could not list files: {e}")
            
        with c4:
            st.write("")  # spacer
            if st.button("💾 Save Config", key="ing_save_infra"):
                st.session_state.config["db"] = db
                st.session_state.config["schema"] = schema
                st.session_state.config["stage"] = stage
                st.success("Config Saved")
                log_action("INFRA_CONFIG_SAVED", {"db": db, "schema": schema, "stage": stage})

    # sub-checklist: Move "Job Builder" section
    st.markdown("#### 📋 Job Builder")
    with st.container():
        jc1, jc2, jc3 = st.columns(3)
        
        # Column 1: File & Scope
        with jc1:
            st.markdown("**📄 File & Scope**")
            sel_file = st.selectbox("Select PDF", pdf_files if pdf_files else ["No files"], key="jb_file")
            scope = st.radio("Scope", ["Full Doc", "Page Range"], horizontal=True, key="jb_scope")
            
            # Metadata Caching Logic
            page_count_est = 1
            if sel_file != "No files":
                if sel_file in st.session_state.file_metadata_cache:
                    page_count_est = st.session_state.file_metadata_cache[sel_file]['page_count']
                else:
                    try:
                        stream = session.file.get_stream(f"{stage_path}/{sel_file}")
                        pdf_bytes = stream.read()
                        page_count_est = PDFUtils.get_page_count(pdf_bytes)
                        st.session_state.file_metadata_cache[sel_file] = {'page_count': page_count_est}
                    except:
                        page_count_est = 1
                st.caption(f"Detected {page_count_est} pages")

            p_start, p_end = 1, page_count_est
            if scope == "Page Range":
                c_rng1, c_rng2 = st.columns(2)
                p_start = c_rng1.number_input("Start", 1, value=1, key="jb_pstart")
                p_end = c_rng2.number_input("End", 1, value=min(10, page_count_est), key="jb_pend")

        # Column 2: Target & Mode
        with jc2:
            st.markdown("**🎯 Target & Strategy**")
            target_table = st.text_input("Target Table", "SUS_CHUNKS", key="jb_table")
            mode = st.radio("Write Mode", ["APPEND", "OVERWRITE", "SURGICAL"], index=0, key="jb_mode",
                          help="Surgical replaces specific pages in the target table.")
            
            use_layout = st.checkbox("Use Layout Parser (Structural)", True, key="jb_layout")
            use_vision = st.checkbox("Use Vision Parser (Charts/Images)", True, key="jb_vision")
            if not use_layout and not use_vision:
                st.error("Select at least one strategy.")

        # Column 3: Params & Add
        with jc3:
            st.markdown("**⚙️ Parameters**")
            chk_sz = st.number_input("Chunk Size", 1000, 30000, 8000, step=500, key="jb_chunk")
            overlap_pct = st.slider("Overlap %", 0, 50, 20, key="jb_overlap")
            overlap = int(chk_sz * (overlap_pct / 100))
            
            # Validation Logic
            validation_errors = []
            if scope == "Page Range" and p_end < p_start:
                validation_errors.append("❌ End Page < Start Page")
            if not use_layout and not use_vision:
                validation_errors.append("❌ Select at least one strategy")
            
            # Auto-default target file for surgical
            target_file_param = sel_file if mode == "SURGICAL" else None
            
            st.write("")
            if validation_errors:
                for err in validation_errors: st.error(err)
            
            if st.button("➕ Add Job", key="jb_add", type="primary", disabled=bool(validation_errors or not pdf_files)):
                est_pages = (p_end - p_start + 1) if scope == "Page Range" else page_count_est
                st.session_state.job_queue.append({
                    "id": len(st.session_state.job_queue)+1,
                    "file": sel_file,
                    "table": target_table,
                    "mode": mode,
                    "scope": scope,
                    "range": (p_start, p_end),
                    "estimated_pages": est_pages,
                    "layout": use_layout,
                    "vision": use_vision,
                    "params": (chk_sz, overlap),
                    "surgical_file": target_file_param,
                    "status": "Pending"
                })
                st.success("Job Added")
                log_action("JOB_ADDED", {"file": sel_file, "id": len(st.session_state.job_queue)})
                
                # Conflict Warning
                from collections import Counter
                overwrites = [j['table'] for j in st.session_state.job_queue if j['mode'] == 'OVERWRITE']
                for tbl, count in Counter(overwrites).items():
                    if count > 1: st.warning(f"⚠️ Multiple OVERWRITE jobs for `{tbl}`.")

    # sub-checklist: Move "Job Queue Workbench" to this tab
    # Ensure st.session_state.job_queue items have 'selected'
    for j in st.session_state.job_queue:
        if 'selected' not in j: j['selected'] = False

    if st.session_state.job_queue:
        st.divider()
        st.markdown("#### 📊 Job Queue Workbench")
        
        # Create display DF with full metadata
        q_data = []
        for j in st.session_state.job_queue:
            q_data.append({
                "selected": j.get("selected", False),
                "id": j["id"],
                "file": j["file"],
                "table": j["table"],
                "scope": j.get("scope", "Full"),
                "mode": j["mode"],
                "L": j.get("layout", True),
                "V": j.get("vision", True),
                "pages": j.get("estimated_pages", 1),
                "status": j["status"]
            })
            
        df_jobs = pd.DataFrame(q_data)
        
        edited_jobs = st.data_editor(
            df_jobs,
            column_config={
                "selected": st.column_config.CheckboxColumn("Select", width="small"),
                "id": st.column_config.NumberColumn("ID", disabled=True, width="small"),
                "file": st.column_config.TextColumn("File", disabled=True, width="medium"),
                "scope": st.column_config.TextColumn("Scope", disabled=True, width="small"),
                "mode": st.column_config.TextColumn("Mode", disabled=True, width="small"),
                "L": st.column_config.CheckboxColumn("Layout", width="small"),
                "V": st.column_config.CheckboxColumn("Vision", width="small"),
                "status": st.column_config.TextColumn("Status", disabled=True)
            },
            use_container_width=True,
            hide_index=True,
            key="config_job_editor_v2"
        )
        
        # Sync Selection and Strategy toggles
        for index, row in edited_jobs.iterrows():
            for j in st.session_state.job_queue:
                if j["id"] == row["id"]:
                    j["selected"] = row["selected"]
                    j["layout"] = row["L"]
                    j["vision"] = row["V"]
                    break
        
        # Batch Actions
        bc1, bc2 = st.columns(2)
        with bc1:
            if st.button("🗑️ Delete Selected Jobs"):
                st.session_state.job_queue = [j for j in st.session_state.job_queue if not j.get("selected")]
                # st.rerun() removed per PLAN-12 - user can use Refresh UI button
        with bc2:
             if st.button("💥 Clear Queue"):
                st.session_state.job_queue = []
                st.session_state.batch_audit = {}  # Reset metrics as well
                st.session_state.file_metadata_cache = {}  # Optional cleanup
                st.success("Queue cleared")
                # st.rerun() removed per PLAN-12 - user can use Refresh UI button


def render_ingestion_tab(session):
    """
    Render the Ingestion & Execution Tab.
    Focuses on running batches and monitoring progress.
    """
    st.subheader("2. Ingestion Execution")
    
    # Check for empty queue
    if not st.session_state.get('job_queue'):
        st.info("ℹ️ No jobs queued. Go to the **Configuration** tab to add jobs.")
        # sub-checklist: Keep "Quality Inspector" here
        st.divider()
        st.markdown("#### 🕵️ Quality Inspector")
        render_quality_inspector(session)
        return

    # sub-checklist: Display summary of job queue
    st.markdown("#### 📋 Execution Queue")
    
    q_data = [{
        "ID": j["id"],
        "File": j["file"],
        "Table": j["table"],
        "Status": j["status"]
    } for j in st.session_state.job_queue]
    
    st.dataframe(pd.DataFrame(q_data), use_container_width=True)

    # sub-checklist: Run Batch button
    if st.button("🚀 Run Batch Execution", key="batch_run", type="primary"):
        # Fetch current config
        db = st.session_state.config.get("db", "SBOX_DB")
        schema = st.session_state.config.get("schema", "AI_SB")
        stage = st.session_state.config.get("stage", "DOCS")
        stage_path = f"@{db}.{schema}.{stage}"
        
        try:
            run_batch_execution(session, db, schema, stage_path)
        except Exception as e:
            log_action("BATCH_RUN_ERROR", {"error": str(e)})
            st.error(f"Batch runner failed to start: {e}")

    # sub-checklist: Enhanced Batch Report Dashboard
    if 'batch_audit' in st.session_state and st.session_state.batch_audit:
        st.divider()
        st.subheader("📊 Enhanced Batch Report")
        
        bm = st.session_state.batch_audit
        
        # Summary Metrics
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Jobs", bm.get('jobs', 0))
        m2.metric("Total Pages", bm.get('pages', 0))
        m3.metric("Standard Chunks", bm.get('standard', 0))
        m4.metric("Enhanced Chunks", bm.get('enhanced', 0))
        
        # Strategy Analysis
        st.markdown("#### 🤖 Strategy Analysis")
        strat_c1, strat_c2 = st.columns(2)
        with strat_c1:
            if bm.get('enhancement_breakdown'):
                st.dataframe(pd.DataFrame(list(bm['enhancement_breakdown'].items()), columns=["Type", "Count"]), use_container_width=True)
            else:
                st.info("No enhancements performed.")
        
        # Table Overview
        with strat_c2:
            if st.session_state.job_queue:
                tbl_stats = {}
                for j in st.session_state.job_queue:
                    if j['status'] == 'Completed':
                        t = j['table']
                        if t not in tbl_stats: tbl_stats[t] = {'Files': 0, 'Chunks': 0}
                        tbl_stats[t]['Files'] += 1
                        metrics = j.get('metrics', {})
                        tbl_stats[t]['Chunks'] += metrics.get('standard_cnt', 0) + metrics.get('enhanced_cnt', 0)
                
                if tbl_stats:
                    st.dataframe(pd.DataFrame.from_dict(tbl_stats, orient='index').reset_index().rename(columns={'index': 'Table'}), use_container_width=True)

        # Export Options
        st.markdown("#### 📥 Export Data")
        ec1, ec2 = st.columns(2)
        with ec1:
            st.download_button("⬇️ Download Batch JSON", json.dumps(bm, indent=2), "batch_report.json", "application/json")
        with ec2:
            # Job metrics CSV
            job_data = []
            for j in st.session_state.job_queue:
                if j['status'] == 'Completed':
                     m = j.get('metrics', {})
                     job_data.append({
                         "ID": j['id'], "File": j['file'], "Duration": m.get('duration',0),
                         "Standard": m.get('standard_cnt',0), "Enhanced": m.get('enhanced_cnt',0)
                     })
            if job_data:
                st.download_button("⬇️ Download Metrics CSV", pd.DataFrame(job_data).to_csv(index=False), "batch_metrics.csv", "text/csv")

    # Inspector / Auto-Fix (Shared Helper Logic)
    st.divider()
    st.markdown("#### 🕵️ Quality Inspector")
    render_quality_inspector(session)


def render_quality_inspector(session):
    """Helper to render Quality Inspector controls"""
    db = st.session_state.config.get("db", "SBOX_DB")
    schema = st.session_state.config.get("schema", "AI_SB")
    
    inspect_table = st.text_input("Target Table for Inspection", "SUS_CHUNKS", key="insp_tbl")
    if st.button("🔍 Run Quality Inspector", key="insp_run"):
        full_table_path = f"{db}.{schema}.{inspect_table}"
        with st.spinner("Analyzing Chunks..."):
            try:
                df = session.sql(f"SELECT CHUNK_ID, PAGE_NUMBER, RELATIVE_PATH, CHUNK FROM {full_table_path} LIMIT 100").to_pandas()
                df["STATUS"] = df["CHUNK"].apply(QualityInspector.inspect)
                defects = df[df["STATUS"] != "OK"]
                
                st.info(f"Found {len(defects)} potential issues out of {len(df)} chunks.")
                if not defects.empty:
                    st.dataframe(defects[["PAGE_NUMBER", "RELATIVE_PATH", "STATUS", "CHUNK_ID"]], use_container_width=True)
                    log_action("INSPECT_RUN", {"defects": len(defects)})
            except Exception as e:
                st.error(f"Inspector failed: {e}")


def process_batch_generation(session, targets, stage_root):
    """Helper to run Cortex for a list of items with hierarchical storage."""
    if not targets:
        st.info("No targets to process.")
        return

    progress = st.progress(0, "Starting batch generation...")
    
    for idx, t_item in enumerate(targets):
        progress.progress((idx+1)/len(targets), f"Processing {t_item['id']}...")
        try:
            # 1. Resolve File & Table
            t_file = t_item['file']
            t_tbl = t_item['table']
            if "." not in t_tbl:
                 # Attempt to resolve from config if incomplete
                 db = st.session_state.config.get("db", "SBOX_DB")
                 sch = st.session_state.config.get("schema", "AI_SB")
                 t_tbl = f"{db}.{sch}.{t_tbl}"
            
            # 2. Fetch Chunk Text if missing
            data = session.sql(f"SELECT CHUNK FROM {t_tbl} WHERE CHUNK_ID = '{t_item['id']}'").collect()
            if not data:
                t_item['status'] = 'Error: ID not found'
                continue
            
            t_chunk_txt = data[0]['CHUNK']
            
            # 3. PDF Cache & Image Logic with Hierarchical Storage
            cache_key = f"qa_pdf_{t_file}"
            if cache_key not in st.session_state:
                try:
                    stream = session.file.get_stream(f"{stage_root}/{t_file}")
                    st.session_state[cache_key] = stream.read()
                except Exception as e:
                    t_item['status'] = f"Error: PDF Load {e}"
                    continue
            
            t_pdf_bytes = st.session_state[cache_key]
            
            # Render Page
            if convert_from_bytes:
                t_images = convert_from_bytes(t_pdf_bytes, first_page=t_item['page_number'], last_page=t_item['page_number'])
                if t_images:
                    with tempfile.TemporaryDirectory() as td:
                        # HIERARCHICAL STORAGE: _temp_images/<filename>/...
                        img_name = f"p{t_item['page_number']}"
                        # Pass sanitized filename as subfolder
                        img_path_local = save_optimized_image(t_images[0], td, img_name, sub_folder=t_file)
                        if not img_path_local:
                            t_item['status'] = 'Error: Image Save Failed'
                            continue
                        
                        # Upload to hierarchical stage path
                        # target stage path: @DOCS/_temp_images/<sanitized_file>/<img_name>.jpg
                        safe_sub = PDFUtils.get_safe_folder(t_file)
                        full_stage_path = f"{stage_root}/_temp_images/{safe_sub}"
                        
                        session.file.put(img_path_local, full_stage_path, auto_compress=False, overwrite=True)
                        
                        # Relative path for Cortex
                        rel_img_path = f"_temp_images/{safe_sub}/{os.path.basename(img_path_local)}"
                        
                        instruction = t_item.get('context_instruction', '')
                        prompt = prompts.get_silver_bullet_prompt(t_chunk_txt, instruction)
                        
                        res = run_cortex(session, prompt, stage_root, rel_img_path, model='claude-4-sonnet')
                        
                        if res:
                            t_item['draft_text'] = res
                            t_item['status'] = 'Ready'
                else:
                    t_item['status'] = 'Error: Render Failed'
            else:
                t_item['status'] = 'Error: No PDF Lib'
                
        except Exception as e:
            t_item['status'] = f"Error: {str(e)}"
    
    progress.empty()
    st.success("Batch Processing Complete")
    # st.rerun() removed per PLAN-12 - user can use Refresh UI button


def render_single_item_inspector(session, item, db, sch, stage_root):
    """Split screen inspector: Visual vs (Read-only Content + Editable Draft)."""
    
    # 1. Resolve Table and Fetch Fresh Data
    work_table = item.get('table')
    if "." not in work_table:
        work_table = f"{db}.{sch}.{work_table}"
    
    try:
        data = session.sql(f"SELECT CHUNK FROM {work_table} WHERE CHUNK_ID = '{item['id']}'").collect()
        original_chunk = data[0]['CHUNK'] if data else "[Error: Chunk not found in table]"
    except Exception as e:
        original_chunk = f"[Error fetching chunk: {e}]"

    col_vis, col_edit = st.columns(2)
    
    # --- Visual Ground Truth ---
    with col_vis:
        st.caption(f"📄 Source: {item['file']} (Pg {item['page_number']})")
        
        if convert_from_bytes and Image:
            try:
                # Retrieve from cache or load
                cache_key = f"qa_pdf_{item['file']}"
                if cache_key not in st.session_state:
                    with st.spinner("Loading PDF..."):
                        stream = session.file.get_stream(f"{stage_root}/{item['file']}")
                        st.session_state[cache_key] = stream.read()
                
                pdf_bytes = st.session_state[cache_key]
                images = convert_from_bytes(pdf_bytes, first_page=item['page_number'], last_page=item['page_number'])
                
                if images:
                    st.image(images[0], use_container_width=True)
                else:
                    st.error("Failed to render page image.")
            except Exception as e:
                st.error(f"Visual Error: {e}")
        else:
            st.warning("Install pdf2image for visuals.")

        # Display chunk metrics
        with st.expander("📊 Chunk Metrics"):
            st.metric("Character Count", len(original_chunk))
            st.metric("Word Count", len(original_chunk.split()))
            st.metric("Lines", len(original_chunk.split('\n')))
            quality_status = QualityInspector.inspect(original_chunk)
            st.metric("Quality Status", quality_status)

    # --- Edit Panel ---
    with col_edit:
        st.caption(f"📝 Draft Editor (Status: {item['status']})")
        
        # Instruction field (synced with table)
        new_inst = st.text_area("Context Instruction", value=item.get("context_instruction", ""), key=f"inst_{item['id']}")
        if new_inst != item.get("context_instruction", ""):
            item["context_instruction"] = new_inst
            
        # Original Content (Read-Only)
        st.markdown("**Original Content (Read-Only)**")
        st.text_area("Original", value=original_chunk, height=200, disabled=True, key=f"orig_{item['id']}")
        
        # Draft Content (Editable)
        st.markdown("**Draft Content (Editable)**")
        # If draft is empty, don't auto-fill with original to keep it clean
        draft_val = item.get('draft_text', "")
        item['draft_text'] = st.text_area("Draft", value=draft_val, height=300, key=f"draft_edit_{item['id']}")
        
        if item['draft_text'] != draft_val:
            item['status'] = 'Modified'
        
        # Single Item Actions
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✨ Generate (Single)", key=f"gen_{item['id']}"):
                process_batch_generation(session, [item], stage_root)
        with c2:
            if st.button("💾 Commit (Single)", key=f"save_{item['id']}"):
                safe_txt = clean_text_for_sql(item['draft_text'])
                sql = f"UPDATE {work_table} SET CHUNK = '{safe_txt}' WHERE CHUNK_ID = '{item['id']}'"
                execute_sql_safe(session, sql)
                item['status'] = 'Committed'
                st.success("Committed to Snowflake.")
                # st.rerun() removed per PLAN-12 - user can use Refresh UI button


def render_qa_tab(session):
    """
    Render the QA & Refinement Studio.
    Table-based workbench with batch operations and persistent context.
    """
    st.subheader("3. QA & Refinement Studio")
    
    if "admin_queue" not in st.session_state:
        st.session_state.admin_queue = []
    
    # Ensure all items have required keys
    for item in st.session_state.admin_queue:
        if "selected" not in item:
            item["selected"] = False
        if "context_instruction" not in item:
            item["context_instruction"] = ""
            
    # 1. Context Source Selection (for Search)
    qa_source = st.radio(
        "Context Source (for Search)",
        ["Active Job Queue", "Manual Configuration"],
        horizontal=True,
        key="qa_source_radio"
    )
    
    current_search_file = None
    current_search_table = None
    
    if qa_source == "Active Job Queue":
        jobs = st.session_state.get('job_queue', [])
        if not jobs:
            st.warning("⚠️ Job Queue is empty.")
        else:
            sel_job = st.selectbox(
                "Select Job from Queue",
                jobs,
                format_func=lambda x: f"[{x['status']}] {x['file']} (Table: {x['table']})",
                key="qa_job_sel"
            )
            if sel_job:
                current_search_file = sel_job['file']
                current_search_table = sel_job['table']
                st.info(f"📋 Context: `{current_search_file}` in `{current_search_table}`")
                
    else: # Manual Configuration
        c_m1, c_m2 = st.columns(2)
        current_search_table = c_m1.text_input("Target Table", "SUS_CHUNKS", key="qa_manual_table")
        current_search_file = c_m2.text_input("File Name (Optional)", key="qa_manual_file")
        st.info(f"📋 Context: Table `{current_search_table}`" + (f", File `{current_search_file}`" if current_search_file else ""))

    # 2. Search & Queue Builder
    if current_search_table:
        with st.expander("🔍 Search & Add Chunks", expanded=False):
            c1, c2 = st.columns([3, 1])
            with c1:
                pg_filter = st.number_input("Page (0=All)", 0, key="qa_pg")
                
                if st.button("Search Chunks", key="qa_search"):
                    db = st.session_state.config.get("db", "SBOX_DB")
                    sch = st.session_state.config.get("schema", "AI_SB")
                    
                    if "." in current_search_table: full_tbl = current_search_table
                    else: full_tbl = f"{db}.{sch}.{current_search_table}"
                    
                    where = []
                    if current_search_file: where.append(f"RELATIVE_PATH = '{current_search_file}'")
                    if pg_filter > 0: where.append(f"PAGE_NUMBER = {pg_filter}")
                    
                    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
                    # Use 1-based indexing for SUBSTR in Snowflake
                    sql = f"SELECT CHUNK_ID, PAGE_NUMBER, SUBSTR(CHUNK, 1, 80) as PREVIEW FROM {full_tbl} {where_clause} LIMIT 100"
                    try:
                        st.session_state.qa_results = session.sql(sql).to_pandas()
                        log_action("QA_SEARCH", {"file": current_search_file, "results": len(st.session_state.qa_results)})
                    except Exception as e:
                        st.error(f"Search failed: {e}")
                        
                if "qa_results" in st.session_state and not st.session_state.qa_results.empty:
                    sel_chunk = st.selectbox("Found Chunks", st.session_state.qa_results["CHUNK_ID"].tolist(),
                                             format_func=lambda x: f"{x} - Page {st.session_state.qa_results[st.session_state.qa_results.CHUNK_ID==x]['PAGE_NUMBER'].values[0]}",
                                             key="qa_chunk_sel")
                    
                    if st.button("➕ Add to Workbench", key="qa_add_queue"):
                        # Check duplicates
                        if sel_chunk not in [x['id'] for x in st.session_state.admin_queue]:
                             row = st.session_state.qa_results[st.session_state.qa_results.CHUNK_ID==sel_chunk].iloc[0]
                             st.session_state.admin_queue.append({
                                 "id": sel_chunk,
                                 "status": "Pending",
                                 "file": current_search_file, # Persist Source File
                                 "table": current_search_table, # Capture the specific table from search
                                 "page_number": int(row['PAGE_NUMBER']),
                                 "selected": False,
                                 "draft_text": "",
                                 "context_instruction": "",
                                 "preview": row['PREVIEW']  # Capture preview from search result
                             })
                             st.success(f"Added {sel_chunk} to workbench.")
                             # st.rerun() removed per PLAN-12 - user can use Refresh UI button

    # 3. Workbench Interface (Table Based)
    st.divider()
    st.markdown(f"### 🛠️ Workbench ({len(st.session_state.admin_queue)} Items)")
    
    if st.session_state.admin_queue:
        # Prepare Data for Editor
        df_queue = pd.DataFrame(st.session_state.admin_queue)
        
        # Display Columns
        cols_config = {
            "selected": st.column_config.CheckboxColumn("Select", width="small"),
            "id": st.column_config.TextColumn("Chunk ID", disabled=True, width="medium"),
            "page_number": st.column_config.NumberColumn("Pg", disabled=True, width="small"),
            "file": st.column_config.TextColumn("Source File", disabled=True, width="medium"),
            "table": st.column_config.TextColumn("Table", disabled=True, width="small"), # Visible metadata
            "status": st.column_config.TextColumn("Status", disabled=True, width="small"),
            "context_instruction": st.column_config.TextColumn("Instruction", width="medium"),
            "draft_text": st.column_config.TextColumn("Draft", disabled=True, width="small"), # Visual indicator only
            "preview": st.column_config.TextColumn("Content Preview", disabled=True, width="large"),
        }
        
        # EDITOR
        edited_df = st.data_editor(
            df_queue[["selected", "id", "page_number", "file", "table", "status", "context_instruction", "draft_text", "preview"]],
            column_config=cols_config,
            use_container_width=True,
            hide_index=True,
            key="qa_editor_v3"
        )
        
        # Sync changes back to session state (Selection & Instructions)
        for index, row in edited_df.iterrows():
            # Find matching item in queue by ID
            for item in st.session_state.admin_queue:
                if item["id"] == row["id"]:
                    item["selected"] = row["selected"]
                    item["context_instruction"] = row["context_instruction"]
                    break

        # --- Batch Actions ---
        st.caption("Batch Operations")
        b1, b2, b3, b4 = st.columns(4)
        
        db = st.session_state.config.get("db", "SBOX_DB")
        sch = st.session_state.config.get("schema", "AI_SB")
        stage = st.session_state.config.get("stage", "DOCS")
        stage_root = f"@{db}.{sch}.{stage}"
        
        with b1:
            if st.button("✨ Gen Missing Drafts"):
                # Filter: Draft is empty
                targets = [i for i in st.session_state.admin_queue if not i.get('draft_text')]
                process_batch_generation(session, targets, stage_root)
                
        with b2:
            if st.button("⚡ Gen Selected Drafts"):
                # Filter: Selected is True
                targets = [i for i in st.session_state.admin_queue if i.get('selected')]
                if not targets:
                    st.warning("No items selected.")
                else:
                    process_batch_generation(session, targets, stage_root)

        with b3:
            if st.button("💾 Commit Selected"):
                targets = [i for i in st.session_state.admin_queue if i.get('selected')]
                if not targets:
                    st.warning("No items selected.")
                else:
                    count = 0
                    progress = st.progress(0, "Committing...")
                    for idx, item in enumerate(targets):
                        if item.get('draft_text'):
                            # Resolve Table per item with fallback to search context
                            tbl = item.get('table') or current_search_table
                            if not tbl:
                                st.error(f"Cannot resolve table for {item['id']}")
                                continue
                            
                            # Dynamic DB/Schema prefixing
                            if "." not in tbl:
                                db = st.session_state.config.get("db", "SBOX_DB")
                                sch = st.session_state.config.get("schema", "AI_SB")
                                full_tbl = f"{db}.{sch}.{tbl}"
                            else:
                                full_tbl = tbl
                            
                            safe_txt = clean_text_for_sql(item['draft_text'])
                            sql = f"UPDATE {full_tbl} SET CHUNK = '{safe_txt}' WHERE CHUNK_ID = '{item['id']}'"
                            execute_sql_safe(session, sql)
                            item['status'] = 'Committed'
                            count += 1
                        progress.progress((idx+1)/len(targets))
                    progress.empty()
                    st.success(f"Committed {count} items.")
                    # st.rerun() removed per PLAN-12 - user can use Refresh UI button

        with b4:
              if st.button("🗑️ Remove Selected"):
                  before = len(st.session_state.admin_queue)
                  st.session_state.admin_queue = [i for i in st.session_state.admin_queue if not i.get('selected')]
                  st.success(f"Removed {before - len(st.session_state.admin_queue)} items.")
                  # st.rerun() removed per PLAN-12 - user can use Refresh UI button

        # --- Inspector Panel ---
        st.divider()
        st.subheader("🧐 Item Inspector")
        
        # Dropdown to select item to inspect
        inspect_options = [
            f"{i}: {item['id']} ({item['file']} - Pg {item['page_number']})"
            for i, item in enumerate(st.session_state.admin_queue)
        ]
        
        sel_idx_label = st.selectbox("Inspect Item", options=inspect_options, index=0)
        curr_idx = int(sel_idx_label.split(":")[0])
        item = st.session_state.admin_queue[curr_idx]
        
        # RENDER SINGLE ITEM EDITOR
        render_single_item_inspector(session, item, db, sch, stage_root)
        
    else:
        st.info("Workbench empty. Search and add chunks to begin QA.")


def render_deployment_tab(session):
    """Render the Cortex Search Deployment tab with Wizard and RBAC"""
    st.subheader("4. Cortex Search Deployment")
    
    db = st.session_state.config.get("db", "SBOX_DB")
    schema = st.session_state.config.get("schema", "AI_SB")
    
    # Wizard Step 1: Source Config
    st.markdown("#### 📁 1. Source Config")
    tgt_table = st.text_input("Source Table", value=f"{db}.{schema}.SUS_CHUNKS", key="dep_src_tbl")
    
    # Parse target table to correctly identify DB/Schema/Table
    parts = tgt_table.split('.')
    if len(parts) == 3:
        t_db, t_sch, t_tbl = parts
    elif len(parts) == 2:
        t_db, t_sch, t_tbl = db, parts[0], parts[1]
    else:
        t_db, t_sch, t_tbl = db, schema, parts[0]
    
    # Fetch columns from table for attribute selection
    cols = []
    try:
        cols = get_table_schema(session, t_db, t_sch, t_tbl)[1]
    except Exception:
        cols = ["CHUNK", "PAGE_NUMBER", "RELATIVE_PATH", "CHUNK_ID"]
    
    # Wizard Step 2: Service Config
    st.markdown("#### ⚙️ 2. Service Config")
    c1, c2 = st.columns(2)
    svc_name = c1.text_input("Service Name (CS_...)", "CS_RAG_V1", key="dep_svc_name")
    wh = c2.selectbox("Warehouse", ["COMPUTE_WH", "SBOX_WH"], key="dep_wh")
    
    # Wizard Step 3: Attributes
    st.markdown("#### 🏷️ 3. Attributes")
    atts = st.multiselect("Filter Attributes", cols, default=["PAGE_NUMBER", "RELATIVE_PATH"], key="dep_atts")
    
    # Wizard Step 4: Advanced Options
    with st.expander("🔧 Advanced Options"):
        target_lag = st.selectbox(
            "Target Lag (Sync Latency)",
            ["1 minute", "5 minutes", "15 minutes", "1 hour"],
            index=0,
            key="dep_lag"
        )
        embedding_model = st.selectbox(
            "Embedding Model",
            ["snowflake-arctic-embed-l", "e5-base-v2", "voyage-2"],
            index=0,
            key="dep_embed"
        )
    
    # Deploy Button with SQL Preview
    col_deploy, col_preview = st.columns(2)
    
    with col_preview:
        if st.button("👁️ Preview SQL", key="dep_preview"):
            att_list = ", ".join(atts) if atts else "PAGE_NUMBER, RELATIVE_PATH"
            # Ensure all attributes are included in the SELECT list
            base_cols = ["CHUNK", "RELATIVE_PATH", "PAGE_NUMBER", "CHUNK_ID"]
            # Combine base cols with selected attributes, removing duplicates
            select_cols = list(set(base_cols + atts))
            select_str = ", ".join(select_cols)
            
            sql_preview = f"""
            CREATE OR REPLACE CORTEX SEARCH SERVICE {db}.{schema}.{svc_name}
            ON CHUNK
            ATTRIBUTES {att_list}
            WAREHOUSE = {wh}
            TARGET_LAG = '{target_lag}'
            EMBEDDING_MODEL = '{embedding_model}'
            AS (
                SELECT {select_str}
                FROM {tgt_table}
            )
            """
            st.code(sql_preview, language="sql")
    
    with col_deploy:
        if st.button("🚀 Deploy Service", key="dep_deploy"):
            try:
                att_list = ", ".join(atts) if atts else "PAGE_NUMBER, RELATIVE_PATH"
                # Ensure all attributes are included in the SELECT list
                base_cols = ["CHUNK", "RELATIVE_PATH", "PAGE_NUMBER", "CHUNK_ID"]
                # Combine base cols with selected attributes, removing duplicates
                select_cols = list(set(base_cols + atts))
                select_str = ", ".join(select_cols)
                
                sql = f"""
                CREATE OR REPLACE CORTEX SEARCH SERVICE {db}.{schema}.{svc_name}
                ON CHUNK
                ATTRIBUTES {att_list}
                WAREHOUSE = {wh}
                TARGET_LAG = '{target_lag}'
                EMBEDDING_MODEL = '{embedding_model}'
                AS (
                    SELECT {select_str}
                    FROM {tgt_table}
                )
                """
                session.sql(sql).collect()
                st.success(f"✅ Deployed {svc_name} successfully!")
                log_action("DEPLOY_SUCCESS", {"service": svc_name, "table": tgt_table})
            except Exception as e:
                st.error(f"❌ Deployment failed: {e}")
                log_action("DEPLOY_ERROR", {"service": svc_name, "error": str(e)})
    
    # RBAC Section
    st.divider()
    st.subheader("🔐 Access Control (RBAC)")
    
    col_role, col_grant = st.columns(2)
    
    with col_role:
        target_role = st.text_input("Target Role", "ACCOUNTADMIN", key="rbac_role")
        st.caption("Grant this role access to the Cortex Search Service")
    
    with col_grant:
        privilege = st.selectbox(
            "Privilege Level",
            ["USAGE", "OWNERSHIP"],
            index=0,
            key="rbac_priv"
        )
    
    col_grant_btn, col_revoke_btn = st.columns(2)
    
    with col_grant_btn:
        if st.button("🔑 Grant Access", key="rbac_grant"):
            try:
                full_svc_name = f"{db}.{schema}.{svc_name}"
                # Grant USAGE on service
                grant_sql = f"GRANT {privilege} ON CORTEX SEARCH SERVICE {full_svc_name} TO ROLE {target_role}"
                session.sql(grant_sql).collect()
                
                # Grant USAGE on schema (required)
                schema_grant = f"GRANT USAGE ON SCHEMA {db}.{schema} TO ROLE {target_role}"
                session.sql(schema_grant).collect()
                
                # Grant SELECT on source table
                table_grant = f"GRANT SELECT ON TABLE {tgt_table} TO ROLE {target_role}"
                session.sql(table_grant).collect()
                
                st.success(f"✅ Granted {privilege} on {svc_name} to role {target_role}")
                log_action("RBAC_GRANT", {"service": svc_name, "role": target_role, "privilege": privilege})
            except Exception as e:
                st.error(f"❌ Grant failed: {e}")
                log_action("RBAC_ERROR", {"error": str(e)})
    
    with col_revoke_btn:
        if st.button("🔒 Revoke Access", key="rbac_revoke"):
            try:
                full_svc_name = f"{db}.{schema}.{svc_name}"
                revoke_sql = f"REVOKE {privilege} ON CORTEX SEARCH SERVICE {full_svc_name} FROM ROLE {target_role}"
                session.sql(revoke_sql).collect()
                
                st.success(f"✅ Revoked {privilege} from role {target_role}")
                log_action("RBAC_REVOKE", {"service": svc_name, "role": target_role})
            except Exception as e:
                st.error(f"❌ Revoke failed: {e}")
                log_action("RBAC_ERROR", {"error": str(e)})
    
    # Existing Services List
    st.divider()
    st.markdown("#### 📋 Existing Cortex Search Services")
    try:
        services = session.sql(f"SHOW CORTEX SEARCH SERVICES IN SCHEMA {db}.{schema}").collect()
        if services:
            svc_df = pd.DataFrame([{"Name": s["name"], "Status": s["status"], "Warehouse": s["warehouse"]} for s in services])
            
            # Filter Toggle
            show_active = st.toggle("Show Active Services Only", value=True)
            if show_active:
                svc_df = svc_df[svc_df["Status"] == "ACTIVE"]
                
            st.dataframe(svc_df, use_container_width=True)
        else:
            st.info("No Cortex Search Services found in this schema.")
    except Exception as e:
        st.warning(f"Could not list services: {e}")


def render_tools_tab(session):
    """Render the Maintenance Tools tab"""
    st.subheader("5. Maintenance Tools")
    if st.button("🧹 Clear Temp Stages"):
        stage = f"@{st.session_state.config.get('db')}.{st.session_state.config.get('schema')}.DOCS"
        try:
            session.sql(f"REMOVE {stage}/_temp_images").collect()
            st.success("Cleaned temp images.")
            log_action("MAINTENANCE", "Cleared temp files")
        except Exception as e:
            st.warning(f"Cleanup warning: {e}")


# -----------------------------------------------------------------------------
# MAIN VIEW
# -----------------------------------------------------------------------------

def render_admin_view():
    """Render the Knowledge Base (Admin) view"""
    st.title("🛠️ Knowledge Base Admin")
    
    # PLAN-12: Add manual refresh button to avoid auto-rerun dependency
    if st.button("🔄 Refresh UI"):
        st.rerun()
        
    log_action("NAVIGATE", "Visited Admin Panel")
    
    session = get_snowpark_session()
    if not session:
        st.error("No active Snowflake session detected. Please run within Snowflake or check connection.")
        return

    # Update: New Tab Structure
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "Configuration", 
        "Ingestion", 
        "QA Studio", 
        "Deployment", 
        "Tools"
    ])
    
    with tab1:
        render_config_tab(session)
    with tab2:
        render_ingestion_tab(session)
    with tab3:
        render_qa_tab(session)
    with tab4:
        render_deployment_tab(session)
    with tab5:
        render_tools_tab(session)