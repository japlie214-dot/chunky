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
        if job['status'] == 'Completed':
            continue
        
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
        
        full_table = f"{db}.{schema}.{job['table']}"
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
                        st.warning(f"🛠️ Found {len(defects)} defects in `{job['file']}`. Starting AI Repair...")
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
                                    # Save page image once per page
                                    img_path = save_optimized_image(imgs[0], td, f"repair_p{pg_num}")
                                    # Upload temp
                                    session.file.put(img_path, f"{stage_path}/_temp_images", auto_compress=False, overwrite=True)
                                    rel_img_path = f"_temp_images/{os.path.basename(img_path)}"
                                    
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

            # --- STRATEGY C: VISION ONLY (Python Loop) ---
            if job['vision'] and not job['layout']:
                batch_status.markdown(f"**👁️ Job {idx+1}/{total_jobs}:** Running Vision Parser...")
                
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
                                img_path = save_optimized_image(imgs[0], td, f"vis_{job['id']}_{pg}")
                                session.file.put(img_path, f"{stage_path}/_temp_images", auto_compress=False, overwrite=True)
                                rel_img_path = f"_temp_images/{os.path.basename(img_path)}"
                                
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

    # Batch Finalization
    batch_metrics['total_time'] = time.time() - batch_start_time
    st.session_state.batch_audit = batch_metrics  # Store globally
    
    batch_progress.progress(1.0, text="Batch Complete")
    time.sleep(1)
    batch_progress.empty()
    st.success("🎉 Batch Execution Finished")
    st.rerun()

# -----------------------------------------------------------------------------
# SUB-RENDERERS (Tabs)
# -----------------------------------------------------------------------------

def render_ingestion_tab(session):
    """Render the Ingestion Pipeline tab with Multi-PDF Job Queue"""
    st.subheader("1. Ingestion Pipeline (Job Queue)")
    
    # Initialize State
    if 'job_queue' not in st.session_state:
        st.session_state.job_queue = []
    if 'file_metadata_cache' not in st.session_state:
        st.session_state.file_metadata_cache = {}
    if 'batch_audit' not in st.session_state:
        st.session_state.batch_audit = {}
    
    # Initialize legacy metrics for backward compatibility
    if 'batch_metrics' not in st.session_state:
        st.session_state.batch_metrics = {
            'chunks_created': 0,
            'chunks_enhanced': 0,
            'pages_processed': 0
        }
    
    # 1. Infrastructure (Source) with Save button
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

    # 2. Job Builder - 3-column layout
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

    # 3. Queue Workbench with data_editor
    if st.session_state.job_queue:
        st.divider()
        st.markdown("#### 📊 Job Queue Workbench")
        
        queue_df = pd.DataFrame([
            {
                "ID": j["id"],
                "File": j["file"],
                "Table": j["table"],
                "Mode": j["mode"],
                "Start": j["range"][0],
                "End": j["range"][1],
                "Pages": j.get("estimated_pages", 1),
                "Layout": j["layout"],
                "Vision": j["vision"],
                "Status": j["status"]
            }
            for j in st.session_state.job_queue
        ])
        
        edited_df = st.data_editor(queue_df, use_container_width=True, num_rows="dynamic", key="jb_editor")

        c_del, c_save, c_run = st.columns([1, 1, 2])
        
        with c_del:
            if st.button("🗑️ Sync Deletions", key="q_sync_del"):
                # Basic sync: if rows removed in editor, remove from session
                current_ids = edited_df["ID"].tolist()
                st.session_state.job_queue = [j for j in st.session_state.job_queue if j["id"] in current_ids]
                st.rerun()

        with c_save:
            if st.button("💾 Apply Changes", key="q_apply"):
                # Update session state from edited DF
                updated_queue = []
                for _, row in edited_df.iterrows():
                    # Find original or create new struct (simplified here to update existing)
                    orig = next((x for x in st.session_state.job_queue if x["id"] == row["ID"]), None)
                    if orig:
                        orig["file"] = row["File"]
                        orig["table"] = row["Table"]
                        orig["mode"] = row["Mode"]
                        orig["range"] = (int(row["Start"]), int(row["End"]))
                        # Recalculate pages
                        orig["estimated_pages"] = max(1, int(row["End"]) - int(row["Start"]) + 1)
                        orig["scope"] = "Page Range"  # Force custom range if edited
                        orig["layout"] = row["Layout"]
                        orig["vision"] = row["Vision"]
                        updated_queue.append(orig)
                st.session_state.job_queue = updated_queue
                st.success("Changes Applied")
                st.rerun()

        with c_run:
            if st.button("🚀 Run Batch", key="batch_run", type="primary"):
                try:
                    run_batch_execution(session, db, schema, stage_path)
                except Exception as e:
                    log_action("BATCH_RUN_ERROR", {"error": str(e)})
                    st.error(f"Batch runner failed to start: {e}")
                
        with c_save:
            if st.button("🗑️ Clear Queue", key="queue_clear"):
                st.session_state.job_queue = []
                st.session_state.batch_metrics = {
                    'chunks_created': 0,
                    'chunks_enhanced': 0,
                    'pages_processed': 0
                }
                st.success("Queue cleared")
                st.rerun()

    # 4. Enhanced Batch Report Dashboard
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

    # 5. Inspector / Auto-Fix
    st.divider()
    st.markdown("#### 🕵️ Quality Inspector")
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


def render_qa_tab(session):
    """Render the QA & Refinement Studio tab - Batch-Only Architecture"""
    st.subheader("2. QA & Refinement Studio (Batch-Only)")
    
    if "admin_queue" not in st.session_state:
        st.session_state.admin_queue = []
        
    # 1. Job Selection - Only from completed jobs in the session queue
    completed = [j for j in st.session_state.get('job_queue', []) if j['status'] == 'Completed']
    
    if not completed:
        st.info("No completed jobs to audit. Complete ingestion jobs first.")
        return
    
    selected_file = None
    selected_table = None
    
    sel_job = st.selectbox(
        "Select Job to Audit",
        completed,
        format_func=lambda x: f"Job #{x['id']} - {x['file']} (Table: {x['table']})",
        key="qa_job_sel"
    )
    selected_file = sel_job['file']
    selected_table = sel_job['table']
    
    st.info(f"📋 Auditing: {selected_file} → {selected_table}")
    
    # 2. Search & Queue Builder
    st.divider()
    c1, c2 = st.columns([3, 1])
    with c1:
        st.markdown(f"**Search in `{selected_table}`**")
        pg_filter = st.number_input("Page (0=All)", 0, key="qa_pg")
        
        if st.button("🔍 Search Chunks", key="qa_search"):
            db = st.session_state.config.get("db", "SBOX_DB")
            sch = st.session_state.config.get("schema", "AI_SB")
            
            # Ensure qualified table names are not corrupted by redundant prepending
            if "." in selected_table:
                full_tbl = selected_table
            else:
                full_tbl = f"{db}.{sch}.{selected_table}"
            
            where = []
            if selected_file: where.append(f"RELATIVE_PATH = '{selected_file}'")
            if pg_filter > 0: where.append(f"PAGE_NUMBER = {pg_filter}")
            
            where_clause = f"WHERE {' AND '.join(where)}" if where else ""
            sql = f"SELECT CHUNK_ID, PAGE_NUMBER, SUBSTR(CHUNK,0,80) as PREVIEW FROM {full_tbl} {where_clause} LIMIT 100"
            try:
                st.session_state.qa_results = session.sql(sql).to_pandas()
                log_action("QA_SEARCH", {"file": selected_file, "page": pg_filter, "results": len(st.session_state.qa_results)})
            except Exception as e:
                st.error(f"Search failed: {e}")
                log_action("QA_SEARCH_ERROR", {"error": str(e)})
                
        if "qa_results" in st.session_state and not st.session_state.qa_results.empty:
            sel_chunk = st.selectbox("Found Chunks", st.session_state.qa_results["CHUNK_ID"].tolist(),
                                     format_func=lambda x: f"{x} - Page {st.session_state.qa_results[st.session_state.qa_results.CHUNK_ID==x]['PAGE_NUMBER'].values[0]}",
                                     key="qa_chunk_sel")
            if st.button("➕ Add to Workbench", key="qa_add_queue"):
                # Add to local queue with page number for dropdown label
                if sel_chunk not in [x['id'] for x in st.session_state.admin_queue]:
                     pg_num = st.session_state.qa_results[st.session_state.qa_results.CHUNK_ID==sel_chunk]['PAGE_NUMBER'].values[0]
                     st.session_state.admin_queue.append({"id": sel_chunk, "status": "Pending", "file": selected_file, "table": selected_table, "page_number": pg_num})
                     st.success(f"Added {sel_chunk} to workbench.")
                     log_action("QA_QUEUE_ADD", {"chunk_id": sel_chunk})

    # 3. Workbench Interface
    st.divider()
    st.markdown(f"### 🛠️ Workbench ({len(st.session_state.admin_queue)} Items)")
    
    if st.session_state.admin_queue:
        # Item Selector (Dropdown instead of Number Input)
        queue_options = [
            f"{i}: {item['id']} (Pg {item.get('page_number', '?')})"
            for i, item in enumerate(st.session_state.admin_queue)
        ]
        
        sel_label = st.selectbox(
            "Select Item to Edit",
            options=queue_options,
            index=0,
            key="qa_item_sel"
        )
        
        # Parse index from label "0: CHK_..."
        curr_idx = int(sel_label.split(":")[0])
        item = st.session_state.admin_queue[curr_idx]
        
        # Load Full Data
        db = st.session_state.config.get("db", "SBOX_DB")
        sch = st.session_state.config.get("schema", "AI_SB")
        # Use table from item or selected_table
        work_table = item.get('table', selected_table)
        if "." in work_table:
            full_tbl = work_table
        else:
            full_tbl = f"{db}.{sch}.{work_table}"
        
        try:
            data = session.sql(f"SELECT CHUNK, PAGE_NUMBER, RELATIVE_PATH FROM {full_tbl} WHERE CHUNK_ID = '{item['id']}'").collect()[0]
            chunk_txt, pg_num, f_path = data['CHUNK'], data['PAGE_NUMBER'], data['RELATIVE_PATH']
            
            # Initialize images to ensure variable existence in edit column
            images = []
            
            col_vis, col_edit = st.columns(2)
            
            # Left: Visual Ground Truth Panel
            with col_vis:
                st.caption(f"📄 Page {pg_num} of {f_path}")
                st.markdown("**Visual Ground Truth**")
                
                if convert_from_bytes and Image:
                    try:
                        # PDF Byte Caching Strategy
                        cache_key = f"qa_pdf_{f_path}"
                        if cache_key not in st.session_state:
                            stage = st.session_state.config.get("stage", "DOCS")
                            stage_path_root = f"@{db}.{sch}.{stage}"
                            with st.spinner(f"Downloading {f_path}..."):
                                stream = session.file.get_stream(f"{stage_path_root}/{f_path}")
                                st.session_state[cache_key] = stream.read()
                        
                        pdf_bytes = st.session_state[cache_key]
                        
                        # Render specific page
                        images = convert_from_bytes(pdf_bytes, first_page=pg_num, last_page=pg_num)
                        if images:
                            st.image(images[0], use_container_width=True, caption=f"Page {pg_num}")
                        else:
                            st.warning("Rendered empty image.")
                            
                    except Exception as img_e:
                        st.error(f"Visual Error: {img_e}")
                        st.caption("Ensure pdf2image/poppler is installed and file exists in stage.")
                else:
                    st.warning("Visual preview requires pdf2image and PIL libraries.")
                
                # Display chunk metrics
                with st.expander("📊 Chunk Metrics"):
                    st.metric("Character Count", len(chunk_txt))
                    st.metric("Word Count", len(chunk_txt.split()))
                    st.metric("Lines", len(chunk_txt.split('\n')))
                    quality_status = QualityInspector.inspect(chunk_txt)
                    st.metric("Quality Status", quality_status)
            
            # Right: Edit Panel
            with col_edit:
                st.caption("📝 Edit Chunk")
                # Manual Edit Detection
                current_draft = item.get('draft_text', chunk_txt)
                manual_edit = st.text_area("Content", value=current_draft, height=400, key=f"edit_{item['id']}")
                
                if manual_edit != current_draft and manual_edit:
                    item['draft_text'] = manual_edit
                    item['status'] = 'Ready'
                    st.success("📝 Manual edit detected. Status: Ready.")
                
                # Context Instruction for AI Enhancement
                with st.expander("💡 AI Enhancement Options"):
                    st.info(prompts.get_instruction_tooltip())
                    context_inst = st.text_area("Context Instruction (optional)", placeholder="e.g., Convert the bar chart into a Markdown table...", key=f"ctx_{item['id']}")
                    
                    if st.button("✨ Generate Draft (AI)", key=f"gen_{item['id']}"):
                        if not images:
                            st.error("Cannot generate draft: Visual preview not available.")
                        else:
                            with st.spinner("Generating AI draft..."):
                                try:
                                    stage = st.session_state.config.get("stage", "DOCS")
                                    stage_path_root = f"@{db}.{sch}.{stage}"
                                    
                                    # FIX: Must upload the current image to stage before Cortex can process it
                                    with tempfile.TemporaryDirectory() as td:
                                        img_name = f"qa_gen_{f_path}_p{pg_num}".replace(".", "_").replace(" ", "_")
                                        img_path_local = save_optimized_image(images[0], td, img_name)
                                        session.file.put(img_path_local, f"{stage_path_root}/_temp_images", auto_compress=False, overwrite=True)
                                        rel_img_path = f"_temp_images/{os.path.basename(img_path_local)}"
                                        
                                        prompt = prompts.get_silver_bullet_prompt(chunk_txt, context_inst)
                                        res = run_cortex(session, prompt, stage_path_root, rel_img_path, model='claude-4-sonnet')
                                    
                                    if res:
                                        st.session_state[f"draft_{item['id']}"] = res
                                        st.success("Draft generated! Review in the editor above.")
                                        st.rerun()
                                except Exception as e:
                                    st.error(f"AI generation failed: {e}")
                
                # Action Buttons
                c_act1, c_act2 = st.columns(2)
                with c_act1:
                    if st.button("💾 Commit Change", key=f"save_{item['id']}"):
                         safe_txt = clean_text_for_sql(manual_edit)
                         session.sql(f"UPDATE {full_tbl} SET CHUNK = '{safe_txt}' WHERE CHUNK_ID = '{item['id']}'").collect()
                         st.session_state.admin_queue.pop(curr_idx)
                         st.success("Change committed and removed from queue.")
                         log_action("QA_COMMIT", {"chunk_id": item['id'], "table": work_table})
                         time.sleep(0.5)
                         st.rerun()
                with c_act2:
                    if st.button("⏭️ Remove from Queue", key=f"skip_{item['id']}"):
                        st.session_state.admin_queue.pop(curr_idx)
                        st.info("Removed from queue.")
                        log_action("QA_SKIP", {"chunk_id": item['id']})
                        time.sleep(0.3)
                        st.rerun()
            
            # Batch Actions
            st.divider()
            st.markdown("#### ⚡ Batch Operations")
            
            if st.button("🔥 Generate Missing Drafts"):
                targets = [i for i in st.session_state.admin_queue if not i.get('draft_text')]
                if not targets:
                    st.info("All items already have drafts.")
                else:
                    prog = st.progress(0, "Starting batch generation...")
                    db = st.session_state.config.get("db", "SBOX_DB")
                    sch = st.session_state.config.get("schema", "AI_SB")
                    stage = st.session_state.config.get("stage", "DOCS")
                    stage_root = f"@{db}.{sch}.{stage}"
                    
                    for idx, t_item in enumerate(targets):
                        prog.progress((idx+1)/len(targets), f"Processing {t_item['id']}...")
                        try:
                            # Load the chunk for this item
                            full_tbl = t_item.get('table', selected_table)
                            if "." not in full_tbl:
                                full_tbl = f"{db}.{sch}.{full_tbl}"
                            
                            data = session.sql(f"SELECT CHUNK, PAGE_NUMBER, RELATIVE_PATH FROM {full_tbl} WHERE CHUNK_ID = '{t_item['id']}'").collect()[0]
                            t_chunk_txt, t_pg_num, t_f_path = data['CHUNK'], data['PAGE_NUMBER'], data['RELATIVE_PATH']
                            
                            # Get PDF bytes with caching
                            cache_key = f"qa_pdf_{t_f_path}"
                            if cache_key not in st.session_state:
                                stream = session.file.get_stream(f"{stage_root}/{t_f_path}")
                                st.session_state[cache_key] = stream.read()
                            
                            t_pdf_bytes = st.session_state[cache_key]
                            
                            # Render page image
                            t_images = convert_from_bytes(t_pdf_bytes, first_page=t_pg_num, last_page=t_pg_num)
                            if t_images:
                                with tempfile.TemporaryDirectory() as td:
                                    img_name = f"qa_batch_{t_f_path}_p{t_pg_num}".replace(".", "_").replace(" ", "_")
                                    img_path_local = save_optimized_image(t_images[0], td, img_name)
                                    session.file.put(img_path_local, f"{stage_root}/_temp_images", auto_compress=False, overwrite=True)
                                    rel_img_path = f"_temp_images/{os.path.basename(img_path_local)}"
                                    
                                    prompt = prompts.get_silver_bullet_prompt(t_chunk_txt, "")
                                    res = run_cortex(session, prompt, stage_root, rel_img_path, model='claude-4-sonnet')
                                    
                                    if res:
                                        st.session_state[f"draft_{t_item['id']}"] = res
                                        t_item['draft_text'] = res
                                        t_item['status'] = 'Ready'
                        except Exception as e:
                            st.error(f"Error on {t_item['id']}: {e}")
                    
                    st.success("Batch Generation Complete")
                    st.rerun()
            
            st.divider()
            col_batch1, col_batch2 = st.columns(2)
            with col_batch1:
                if st.button("🔄 Commit All in Queue", key="qa_commit_all"):
                    st.info("Batch commit functionality - would iterate through all items")
            with col_batch2:
                if st.button("🗑️ Clear Queue", key="qa_clear_queue"):
                    st.session_state.admin_queue = []
                    st.success("Queue cleared.")
                    log_action("QA_QUEUE_CLEAR", {})
                    st.rerun()
                        
        except Exception as e:
            st.error(f"Error loading chunk: {e}")
            log_action("QA_LOAD_ERROR", {"chunk_id": item['id'], "error": str(e)})
            
    else:
        st.info("Workbench empty. Search and add chunks to begin QA.")


def render_deployment_tab(session):
    """Render the Cortex Search Deployment tab with Wizard and RBAC"""
    st.subheader("3. Cortex Search Deployment")
    
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
    st.subheader("4. Maintenance Tools")
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
    log_action("NAVIGATE", "Visited Admin Panel")
    
    session = get_snowpark_session()
    if not session:
        st.error("No active Snowflake session detected. Please run within Snowflake or check connection.")
        return

    tab1, tab2, tab3, tab4 = st.tabs(["Ingestion", "QA Studio", "Deployment", "Tools"])
    
    with tab1:
        render_ingestion_tab(session)
    with tab2:
        render_qa_tab(session)
    with tab3:
        render_deployment_tab(session)
    with tab4:
        render_tools_tab(session)