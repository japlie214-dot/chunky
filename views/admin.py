# views/admin.py
# PLAN-12: Refactored for Gatekeeper Authentication (Context Locking)
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
    get_snowpark_session, clean_text_for_sql, get_table_schema, run_cortex, scan_for_services
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
        log_action("SQL_EXECUTION_ERROR", {
            "error": err_msg,
            "sql_snippet": sql[:500] if len(sql) > 500 else sql
        })
        return False, err_msg

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
        "jobs_completed": 0, "jobs_failed": 0,
        "total_pages": 0, "total_chunks": 0,
        "standard_chunks": 0, "enhanced_chunks": 0,
        "total_time": 0.0, "enhancement_breakdown": {}
    }
    
    batch_start_time = time.time()
    
    for idx, job in enumerate(st.session_state.job_queue):
        if job['status'] == 'Completed':
            continue
        
        job_alert = st.empty()
        job_start_time = time.time()
        job['metrics'] = {
            "start": job_start_time, "end": None, "duration": 0,
            "pages": 0, "standard_cnt": 0, "enhanced_cnt": 0, "types": {}
        }
        job['status'] = 'Running'
        
        batch_status.markdown(f"**🔄 Job {idx+1}/{total_jobs}:** `{job['file']}` → `{job['table']}`")
        batch_progress.progress(idx / total_jobs, text=f"Processing Job {idx+1} of {total_jobs}")
        
        pdf_bytes = None
        def get_pdf_bytes():
            nonlocal pdf_bytes
            if pdf_bytes is None:
                pdf_bytes = session.file.get_stream(f"{stage_path}/{job['file']}").read()
            return pdf_bytes
        
        # Resolve table path safely - Enforce Context by ignoring user-provided dots/prefixes
        table_name = job['table'].split('.')[-1]
        full_table = f"{db}.{schema}.{table_name}"
            
        chunk_sz, chunk_ov = job['params']
        
        try:
            # 1. SCOPE RESOLUTION & PAGE CALCULATION
            if job['scope'] == "Page Range":
                s_pg, e_pg = job['range']
                job_pages_count = max(1, e_pg - s_pg)
                pg_filter_sql = f"AND PAGE_NUMBER BETWEEN {s_pg} AND {e_pg}"
                json_opts = json.dumps({'mode': 'LAYOUT', 'page_filter': [{'start': s_pg, 'end': e_pg}]})
            else:
                s_pg, e_pg = 1, None
                real_pgs = PDFUtils.get_page_count(get_pdf_bytes())
                job_pages_count = real_pgs
                pg_filter_sql = ""
                json_opts = json.dumps({'mode': 'LAYOUT'})

            # 2. SURGICAL DELETE
            if job['mode'] == 'SURGICAL':
                batch_status.markdown(f"**✂️ Job {idx+1}/{total_jobs}:** Surgical Cleanup...")
                del_sql = f"DELETE FROM {full_table} WHERE RELATIVE_PATH = '{job['file']}' {pg_filter_sql}"
                ok, res = execute_sql_safe(session, del_sql)
                if not ok: raise Exception(f"Surgical Delete Failed: {res}")

            # 3. STRATEGY EXECUTION
            # --- STRATEGY A: LAYOUT (SQL) ---
            if job['layout']:
                batch_status.markdown(f"**🔧 Job {idx+1}/{total_jobs}:** Running Layout Parser (SQL)...")
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
                    exists, cols, err = get_table_schema(session, db, schema, job['table'])
                    if not exists:
                        final_sql = f"CREATE TABLE {full_table} AS {src_sql}"
                    else:
                        if 'CHUNK_TYPE' not in cols:
                            execute_sql_safe(session, f"ALTER TABLE {full_table} ADD COLUMN CHUNK_TYPE VARCHAR DEFAULT 'STANDARD'")
                        final_sql = f"INSERT INTO {full_table} (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE) {src_sql}"
                    ok, res = execute_sql_safe(session, final_sql)
                
                if not ok: raise Exception(f"Layout SQL Failed: {res}")
                
                try:
                    if job['mode'] == 'OVERWRITE':
                        count_sql = f"SELECT COUNT(*) FROM {full_table} WHERE RELATIVE_PATH = '{safe_file}' {pg_filter_sql}"
                        ok_c, res_c = execute_sql_safe(session, count_sql)
                        cnt = res_c[0][0] if ok_c else 0
                    else:
                        cnt = int(res[0][0])
                    job['metrics']['standard_cnt'] += cnt
                    batch_metrics['standard_chunks'] += cnt
                except Exception: pass

            # --- STRATEGY B: HYBRID REPAIR ---
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
                                        res_txt = run_cortex(session, prompt, stage_path, rel_img_path, model='claude-4-sonnet')
                                        
                                        if res_txt:
                                            clean_chunk = clean_text_for_sql(res_txt)
                                            upd_sql = f"UPDATE {full_table} SET CHUNK = '{clean_chunk}', CHUNK_TYPE = 'ENHANCED' WHERE CHUNK_ID = '{row['CHUNK_ID']}'"
                                            execute_sql_safe(session, upd_sql)
                                            
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

            # --- STRATEGY C: VISION ONLY ---
            if job['vision'] and not job['layout']:
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
                                res_txt = run_cortex(session, prompt, stage_path, rel_img_path, model='claude-4-sonnet')
                                
                                if res_txt:
                                    clean_chunk = clean_text_for_sql(res_txt)
                                    ins_sql = f"""
                                    INSERT INTO {full_table} (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE)
                                    SELECT '{safe_file}', {pg}, C.VALUE::VARCHAR, CONCAT('CHK_', UUID_STRING()), 'ENHANCED'
                                    FROM TABLE(SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER('{clean_chunk}', 'markdown', {chunk_sz}, {chunk_ov})) C
                                    """
                                    ok_i, res_i = execute_sql_safe(session, ins_sql)
                                    if ok_i:
                                        inserted_cnt = int(res_i[0][0])
                                        job['metrics']['enhanced_cnt'] += inserted_cnt
                                        etype = "Vision Extraction"
                                        job['metrics']['types'][etype] = job['metrics']['types'].get(etype, 0) + inserted_cnt
                                        batch_metrics['enhanced_chunks'] += inserted_cnt
                                        batch_metrics['enhancement_breakdown'][etype] = batch_metrics['enhancement_breakdown'].get(etype, 0) + inserted_cnt
                    vision_progress.empty()
                except Exception as e:
                    log_action("VISION_ONLY_ERROR", {"job": job['id'], "error": str(e)})

            job['status'] = 'Completed'
            job_end_time = time.time()
            job['metrics']['end'] = job_end_time
            job['metrics']['duration'] = job_end_time - job_start_time
            job['metrics']['pages'] = job_pages_count
            batch_metrics['jobs_completed'] += 1
            batch_metrics['total_pages'] += job_pages_count
            
        except Exception as e:
            job['status'] = 'Failed'
            job['metrics']['error'] = str(e)
            batch_metrics['jobs_failed'] += 1
            log_action("JOB_FAILED", {"id": job['id'], "error": str(e)})
            st.error(f"Job {job['id']} Failed: {e}")
            job_alert.empty()

    batch_metrics['total_time'] = time.time() - batch_start_time
    batch_metrics['total_chunks'] = batch_metrics['standard_chunks'] + batch_metrics['enhanced_chunks']
    st.session_state.batch_audit = batch_metrics
    
    batch_progress.progress(1.0, text="Batch Complete")
    time.sleep(1)
    batch_progress.empty()
    batch_status.empty()
    st.success("🎉 Batch Execution Finished")

# -----------------------------------------------------------------------------
# SUB-RENDERERS (Tabs)
# -----------------------------------------------------------------------------

def render_config_tab(session):
    """Refactored for PLAN-12 Context Locking"""
    st.subheader("1. Job Management")
    
    # Context Retrieval
    ctx = st.session_state.auth_context
    db, schema, stage = ctx["db"], ctx["schema"], ctx["stage"]
    stage_path = f"@{db}.{schema}.{stage}"

    # Infrastructure Display (Read-Only)
    with st.expander(f"🔒 Active Context: {db}.{schema}", expanded=True):
        st.info(f"**Stage:** `{stage}` | **Path:** `{stage_path}`")
        
        pdf_files = []
        try:
            files = session.sql(f"LIST {stage_path} PATTERN='.*\\.pdf'").collect()
            pdf_files = [os.path.basename(f['name']) for f in files]
        except Exception as e:
            st.warning(f"Could not list files: {e}")

    # Job Builder
    st.markdown("#### 📋 Job Builder")
    with st.container():
        jc1, jc2, jc3 = st.columns(3)
        
        with jc1:
            st.markdown("**📄 File & Scope**")
            sel_file = st.selectbox("Select PDF", pdf_files if pdf_files else ["No files"], key="jb_file")
            scope = st.radio("Scope", ["Full Doc", "Page Range"], horizontal=True, key="jb_scope")
            
            # Metadata Caching
            page_count_est = 1
            if sel_file != "No files":
                if 'file_metadata_cache' not in st.session_state: st.session_state.file_metadata_cache = {}
                if sel_file in st.session_state.file_metadata_cache:
                    page_count_est = st.session_state.file_metadata_cache[sel_file]['page_count']
                else:
                    try:
                        stream = session.file.get_stream(f"{stage_path}/{sel_file}")
                        pdf_bytes = stream.read()
                        page_count_est = PDFUtils.get_page_count(pdf_bytes)
                        st.session_state.file_metadata_cache[sel_file] = {'page_count': page_count_est}
                    except: pass
                st.caption(f"Detected {page_count_est} pages")

            p_start, p_end = 1, page_count_est
            if scope == "Page Range":
                c_rng1, c_rng2 = st.columns(2)
                p_start = c_rng1.number_input("Start", 1, value=1, key="jb_pstart")
                p_end = c_rng2.number_input("End", 1, value=min(10, page_count_est), key="jb_pend")

        with jc2:
            st.markdown("**🎯 Target & Strategy**")
            # Locked to current schema context, but user can define Table Name
            target_table_name = st.text_input("Target Table Name", "SUS_CHUNKS", key="jb_table_name")
            target_table = target_table_name # Will be prefixed with ctx later
            
            mode = st.radio("Write Mode", ["APPEND", "OVERWRITE", "SURGICAL"], index=0, key="jb_mode",
                          help="Surgical replaces specific pages in the target table.")
            
            use_layout = st.checkbox("Use Layout Parser (Structural)", True, key="jb_layout")
            use_vision = st.checkbox("Use Vision Parser (Charts/Images)", True, key="jb_vision")
            if not use_layout and not use_vision: st.error("Select at least one strategy.")

        with jc3:
            st.markdown("**⚙️ Parameters**")
            chk_sz = st.number_input("Chunk Size", 1000, 30000, 8000, step=500, key="jb_chunk")
            overlap_pct = st.slider("Overlap %", 0, 50, 20, key="jb_overlap")
            overlap = int(chk_sz * (overlap_pct / 100))
            
            validation_errors = []
            if scope == "Page Range" and p_end < p_start: validation_errors.append("❌ End Page < Start Page")
            if not use_layout and not use_vision: validation_errors.append("❌ Select at least one strategy")
            
            if st.button("➕ Add Job", key="jb_add", type="primary", disabled=bool(validation_errors or not pdf_files)):
                est_pages = max(1, (p_end - p_start)) if scope == "Page Range" else page_count_est
                if 'job_queue' not in st.session_state: st.session_state.job_queue = []
                
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
                    "surgical_file": sel_file if mode == "SURGICAL" else None,
                    "status": "Pending"
                })
                st.success("Job Added")
                st.rerun()

    # Job Queue Display
    if 'job_queue' in st.session_state and st.session_state.job_queue:
        st.divider()
        st.markdown("#### 📊 Job Queue Workbench")
        
        q_data = []
        for j in st.session_state.job_queue:
            q_data.append({
                "selected": j.get("selected", False), "id": j["id"], "file": j["file"],
                "table": j["table"], "scope": j.get("scope", "Full"), "mode": j["mode"],
                "L": j.get("layout", True), "V": j.get("vision", True),
                "pages": j.get("estimated_pages", 1), "status": j["status"]
            })
            
        edited_jobs = st.data_editor(
            pd.DataFrame(q_data),
            column_config={
                "selected": st.column_config.CheckboxColumn("Select", width="small"),
                "id": st.column_config.NumberColumn("ID", disabled=True, width="small"),
                "file": st.column_config.TextColumn("File", disabled=True, width="medium"),
                "status": st.column_config.TextColumn("Status", disabled=True)
            },
            use_container_width=True, hide_index=True, key="config_job_editor_v2"
        )
        
        for index, row in edited_jobs.iterrows():
            for j in st.session_state.job_queue:
                if j["id"] == row["id"]:
                    j["selected"] = row["selected"]
                    j["layout"] = row["L"]
                    j["vision"] = row["V"]
                    break
        
        bc1, bc2 = st.columns(2)
        with bc1:
            if st.button("🗑️ Delete Selected Jobs"):
                st.session_state.job_queue = [j for j in st.session_state.job_queue if not j.get("selected")]
                st.rerun()
        with bc2:
             if st.button("💥 Clear Queue"):
                st.session_state.job_queue = []
                st.session_state.batch_audit = {}
                st.rerun()

def render_ingestion_tab(session):
    """Refactored for PLAN-12 Context Locking"""
    st.subheader("2. Ingestion Execution")
    
    # Context Retrieval
    ctx = st.session_state.auth_context
    db, schema, stage = ctx["db"], ctx["schema"], ctx["stage"]
    stage_path = f"@{db}.{schema}.{stage}"
    
    if not st.session_state.get('job_queue'):
        st.info("ℹ️ No jobs queued.")
        render_quality_inspector(session)
        return

    if 'batch_audit' not in st.session_state or not st.session_state.batch_audit:
        st.markdown("#### 📋 Pending Execution Queue")
        q_data = [{"ID": j["id"], "File": j["file"], "Table": j["table"], "Status": j["status"]} for j in st.session_state.job_queue]
        st.dataframe(pd.DataFrame(q_data), use_container_width=True)

    if st.button("🚀 Run Batch Execution", key="batch_run", type="primary"):
        try:
            # Enforce Context
            run_batch_execution(session, db, schema, stage_path)
        except Exception as e:
            st.error(f"Batch runner failed: {e}")

    # Report Dashboard
    if 'batch_audit' in st.session_state and st.session_state.batch_audit:
        st.divider()
        bm = st.session_state.batch_audit
        rpt_tab1, rpt_tab2 = st.tabs(["📊 Overview", "📋 Details"])
        
        with rpt_tab1:
            m1, m2, m3 = st.columns(3)
            m1.metric("✅ Success", bm['jobs_completed'])
            m2.metric("📄 Pages", bm['total_pages'])
            m3.metric("⏱️ Time", f"{bm['total_time']:.1f}s")
            
            c1, c2, c3 = st.columns(3)
            c1.metric("Total Chunks", bm['total_chunks'])
            c2.metric("Enhanced", bm['enhanced_chunks'])
            if bm['total_chunks'] > 0:
                st.progress(bm['enhanced_chunks'] / bm['total_chunks'])

        with rpt_tab2:
            job_rows = [{"ID": j['id'], "File": j['file'], "Status": j['status']} for j in st.session_state.job_queue]
            st.dataframe(pd.DataFrame(job_rows), use_container_width=True)

    st.divider()
    render_quality_inspector(session)

def render_quality_inspector(session):
    """Refactored for PLAN-12 Context Locking"""
    ctx = st.session_state.auth_context
    db, schema = ctx["db"], ctx["schema"]
    
    st.markdown("#### 🕵️ Quality Inspector")
    inspect_table_input = st.text_input("Target Table (Current Schema)", "SUS_CHUNKS", key="insp_tbl")
    
    if st.button("🔍 Run Quality Inspector", key="insp_run"):
        # Enforce authenticated schema
        tbl_base = inspect_table_input.split('.')[-1]
        full_table_path = f"{db}.{schema}.{tbl_base}"
        with st.spinner("Analyzing..."):
            try:
                df = session.sql(f"SELECT CHUNK_ID, PAGE_NUMBER, RELATIVE_PATH, CHUNK FROM {full_table_path} LIMIT 100").to_pandas()
                df["STATUS"] = df["CHUNK"].apply(QualityInspector.inspect)
                defects = df[df["STATUS"] != "OK"]
                
                if not defects.empty:
                    st.warning(f"Found {len(defects)} issues.")
                    st.dataframe(defects[["PAGE_NUMBER", "STATUS", "CHUNK_ID"]], use_container_width=True)
                else:
                    st.success("No obvious defects in sample.")
            except Exception as e:
                st.error(f"Inspector failed: {e}")

def process_batch_generation(session, targets, stage_root):
    """Helper to run Cortex for a list of items with hierarchical storage."""
    if not targets:
        st.info("No targets to process.")
        return

    progress = st.progress(0, "Starting batch generation...")
    
    # Retrieve Context for resolving tables if needed
    ctx = st.session_state.auth_context
    
    for idx, t_item in enumerate(targets):
        progress.progress((idx+1)/len(targets), f"Processing {t_item['id']}...")
        try:
            t_file = t_item['file']
            t_tbl = t_item['table']
            
            # Context Enforcement - Enforce authenticated schema
            t_tbl_base = t_tbl.split('.')[-1]
            t_tbl = f"{ctx['db']}.{ctx['schema']}.{t_tbl_base}"
            
            data = session.sql(f"SELECT CHUNK FROM {t_tbl} WHERE CHUNK_ID = '{t_item['id']}'").collect()
            if not data:
                t_item['status'] = 'Error: ID not found'
                continue
            
            t_chunk_txt = data[0]['CHUNK']
            
            cache_key = f"qa_pdf_{t_file}"
            if cache_key not in st.session_state:
                try:
                    stream = session.file.get_stream(f"{stage_root}/{t_file}")
                    st.session_state[cache_key] = stream.read()
                except Exception as e:
                    t_item['status'] = f"Error: PDF Load {e}"
                    continue
            
            t_pdf_bytes = st.session_state[cache_key]
            
            if convert_from_bytes:
                t_images = convert_from_bytes(t_pdf_bytes, first_page=t_item['page_number'], last_page=t_item['page_number'])
                if t_images:
                    with tempfile.TemporaryDirectory() as td:
                        img_name = f"p{t_item['page_number']}"
                        img_path_local = save_optimized_image(t_images[0], td, img_name, sub_folder=t_file)
                        if not img_path_local:
                            t_item['status'] = 'Error: Image Save Failed'
                            continue
                        
                        safe_sub = PDFUtils.get_safe_folder(t_file)
                        full_stage_path = f"{stage_root}/_temp_images/{safe_sub}"
                        session.file.put(img_path_local, full_stage_path, auto_compress=False, overwrite=True)
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

def render_single_item_inspector(session, item, db, sch, stage_root):
    """Split screen inspector: Visual vs (Read-only Content + Editable Draft)."""
    # Context Prefixing - Enforce authenticated schema
    table_base = item.get('table', '').split('.')[-1]
    work_table = f"{db}.{sch}.{table_base}"
    
    try:
        data = session.sql(f"SELECT CHUNK FROM {work_table} WHERE CHUNK_ID = '{item['id']}'").collect()
        original_chunk = data[0]['CHUNK'] if data else "[Error: Chunk not found]"
    except Exception as e:
        original_chunk = f"[Error: {e}]"

    col_vis, col_edit = st.columns(2)
    with col_vis:
        st.caption(f"📄 Source: {item['file']} (Pg {item['page_number']})")
        if convert_from_bytes and Image:
            try:
                cache_key = f"qa_pdf_{item['file']}"
                if cache_key not in st.session_state:
                    stream = session.file.get_stream(f"{stage_root}/{item['file']}")
                    st.session_state[cache_key] = stream.read()
                
                images = convert_from_bytes(st.session_state[cache_key], first_page=item['page_number'], last_page=item['page_number'])
                if images: st.image(images[0], use_container_width=True)
            except Exception as e: st.error(f"Visual Error: {e}")
        else:
            st.warning("Install pdf2image for visuals.")

    with col_edit:
        st.caption(f"📝 Draft Editor (Status: {item['status']})")
        new_inst = st.text_area("Instruction", value=item.get("context_instruction", ""), key=f"inst_{item['id']}")
        if new_inst != item.get("context_instruction", ""): item["context_instruction"] = new_inst
            
        st.text_area("Original", value=original_chunk, height=150, disabled=True, key=f"orig_{item['id']}")
        
        draft_val = item.get('draft_text', "")
        item['draft_text'] = st.text_area("Draft", value=draft_val, height=200, key=f"draft_edit_{item['id']}")
        if item['draft_text'] != draft_val: item['status'] = 'Modified'
        
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✨ Generate", key=f"gen_{item['id']}"):
                process_batch_generation(session, [item], stage_root)
        with c2:
            if st.button("💾 Commit", key=f"save_{item['id']}"):
                safe_txt = clean_text_for_sql(item['draft_text'])
                execute_sql_safe(session, f"UPDATE {work_table} SET CHUNK = '{safe_txt}' WHERE CHUNK_ID = '{item['id']}'")
                item['status'] = 'Committed'
                st.success("Saved")

def render_qa_tab(session):
    """Refactored for PLAN-12 Context Locking"""
    st.subheader("3. QA & Refinement Studio")
    
    # Context Retrieval
    ctx = st.session_state.auth_context
    db, schema, stage = ctx["db"], ctx["schema"], ctx["stage"]
    stage_root = f"@{db}.{schema}.{stage}"

    if "admin_queue" not in st.session_state: st.session_state.admin_queue = []
    
    # QA Source Selection - Simplified to Context
    qa_source = st.radio("Search Scope", ["Active Job Queue", "Manual Search in Current Schema"], horizontal=True, key="qa_source")
    
    current_search_file = None
    current_search_table = None
    
    if qa_source == "Active Job Queue":
        jobs = st.session_state.get('job_queue', [])
        if jobs:
            sel_job = st.selectbox("Select Job", jobs, format_func=lambda x: f"{x['file']} -> {x['table']}", key="qa_job_sel")
            if sel_job:
                current_search_file = sel_job['file']
                current_search_table = sel_job['table']
    else:
        c1, c2 = st.columns(2)
        current_search_table = c1.text_input("Table Name", "SUS_CHUNKS", key="qa_manual_tbl")
        current_search_file = c2.text_input("File Filter (Optional)", key="qa_manual_file")

    # Search Logic
    if current_search_table:
        with st.expander("🔍 Search Chunks", expanded=False):
            pg_filter = st.number_input("Page (0=All)", 0, key="qa_pg")
            if st.button("Search", key="qa_search"):
                # Enforce authenticated schema
                tbl_base = current_search_table.split('.')[-1]
                full_tbl = f"{db}.{schema}.{tbl_base}"
                where = []
                if current_search_file: where.append(f"RELATIVE_PATH = '{current_search_file}'")
                if pg_filter > 0: where.append(f"PAGE_NUMBER = {pg_filter}")
                where_clause = f"WHERE {' AND '.join(where)}" if where else ""
                
                sql = f"SELECT CHUNK_ID, PAGE_NUMBER, SUBSTR(CHUNK, 1, 80) as PREVIEW FROM {full_tbl} {where_clause} LIMIT 100"
                try:
                    st.session_state.qa_results = session.sql(sql).to_pandas()
                except Exception as e:
                    st.error(f"Search failed: {e}")
                    
            if "qa_results" in st.session_state and not st.session_state.qa_results.empty:
                sel_chunk = st.selectbox("Found", st.session_state.qa_results["CHUNK_ID"].tolist(), key="qa_chunk_sel")
                if st.button("➕ Add to Workbench"):
                    if sel_chunk not in [x['id'] for x in st.session_state.admin_queue]:
                        row = st.session_state.qa_results[st.session_state.qa_results.CHUNK_ID==sel_chunk].iloc[0]
                        st.session_state.admin_queue.append({
                            "id": sel_chunk, "status": "Pending",
                            "file": current_search_file, "table": current_search_table,
                            "page_number": int(row['PAGE_NUMBER']),
                            "selected": False, "draft_text": "", "context_instruction": "",
                            "preview": row['PREVIEW']
                        })
                        st.success("Added")
                        st.rerun()

    # Workbench Logic
    if st.session_state.admin_queue:
        st.divider()
        st.markdown(f"### 🛠️ Workbench ({len(st.session_state.admin_queue)})")
        
        # Display Editor
        df_queue = pd.DataFrame(st.session_state.admin_queue)
        edited_df = st.data_editor(
            df_queue[["selected", "id", "page_number", "file", "status"]],
            column_config={"selected": st.column_config.CheckboxColumn("Sel", width="small")},
            use_container_width=True, hide_index=True, key="qa_editor_v3"
        )
        for index, row in edited_df.iterrows():
             for item in st.session_state.admin_queue:
                 if item["id"] == row["id"]: item["selected"] = row["selected"]

        # Batch Actions
        b1, b2, b3 = st.columns(3)
        with b1:
             if st.button("✨ Gen Drafts (Selected)"):
                 targets = [i for i in st.session_state.admin_queue if i.get('selected')]
                 process_batch_generation(session, targets, stage_root)
        with b2:
            if st.button("💾 Commit (Selected)"):
                targets = [i for i in st.session_state.admin_queue if i.get('selected')]
                count = 0
                for item in targets:
                    if item.get('draft_text'):
                        tbl = item.get('table') or current_search_table
                        # Enforce authenticated schema
                        tbl_base = tbl.split('.')[-1]
                        full_tbl = f"{db}.{schema}.{tbl_base}"
                        safe_txt = clean_text_for_sql(item['draft_text'])
                        execute_sql_safe(session, f"UPDATE {full_tbl} SET CHUNK = '{safe_txt}' WHERE CHUNK_ID = '{item['id']}'")
                        item['status'] = 'Committed'
                        count += 1
                st.success(f"Committed {count} items.")
                st.rerun()
        with b3:
             if st.button("🗑️ Remove (Selected)"):
                 st.session_state.admin_queue = [i for i in st.session_state.admin_queue if not i.get('selected')]
                 st.rerun()

        # Item Inspector
        st.divider()
        sel_idx = st.selectbox("Inspect Item", range(len(st.session_state.admin_queue)), format_func=lambda x: f"{st.session_state.admin_queue[x]['id']}")
        item = st.session_state.admin_queue[sel_idx]
        render_single_item_inspector(session, item, db, schema, stage_root)

def render_deployment_tab(session):
    """Refactored for PLAN-12 Context Locking"""
    st.subheader("4. Cortex Search Deployment")
    ctx = st.session_state.auth_context
    db, schema = ctx["db"], ctx["schema"]
    
    st.markdown("#### 📁 Source")
    # Locked Schema Context
    tgt_table_name_input = st.text_input("Source Table Name", "SUS_CHUNKS", key="dep_src_tbl")
    tgt_table_base = tgt_table_name_input.split('.')[-1]
    tgt_table_full = f"{db}.{schema}.{tgt_table_base}"
    
    if st.button("✅ Validate Table"):
        exists, _, err = get_table_schema(session, db, schema, tgt_table_base)
        if exists: st.toast("Table found!")
        else: st.toast(f"Table missing: {err}")

    # Service Config
    st.markdown("#### ⚙️ Service")
    svc_suffix = st.text_input("Service Suffix", "RAG_V1", key="dep_svc").strip()
    svc_name = f"CSS_{svc_suffix}" if svc_suffix else None
    
    # Attributes
    cols = []
    try: cols = get_table_schema(session, db, schema, tgt_table_base)[1]
    except: cols = ["PAGE_NUMBER", "RELATIVE_PATH"]
    atts = st.multiselect("Attributes", cols, default=["PAGE_NUMBER", "RELATIVE_PATH"], key="dep_atts")
    
    if st.button("🚀 Deploy Service"):
        if not svc_name: st.error("Name required")
        else:
            try:
                # Basic columns required
                select_cols = list(set(["CHUNK", "RELATIVE_PATH", "PAGE_NUMBER", "CHUNK_ID"] + atts))
                sql = f"""
                CREATE OR REPLACE CORTEX SEARCH SERVICE {db}.{schema}.{svc_name}
                ON CHUNK ATTRIBUTES {', '.join(atts)}
                WAREHOUSE = COMPUTE_WH TARGET_LAG = '1 minutes'
                AS (SELECT {', '.join(select_cols)} FROM {tgt_table_full})
                """
                session.sql(sql).collect()
                st.success(f"Deployed {svc_name}")
            except Exception as e: st.error(f"Deploy failed: {e}")

    # RBAC - Locked to Context
    st.divider()
    st.markdown("#### 🔐 RBAC (Active Schema)")
    if st.button("🔄 Scan Services"):
        st.session_state.admin_service_cache = scan_for_services(session, db, schema)
    
    svc_list = st.session_state.get('admin_service_cache', [])
    target_svc = st.selectbox("Service", svc_list, key="rbac_svc")
    target_role = st.text_input("Role", "ACCOUNTADMIN", key="rbac_role")
    
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Grant Access"):
             session.sql(f"GRANT USAGE ON CORTEX SEARCH SERVICE {db}.{schema}.{target_svc} TO ROLE {target_role}").collect()
             session.sql(f"GRANT USAGE ON SCHEMA {db}.{schema} TO ROLE {target_role}").collect()
             st.success("Granted")
    with c2:
        if st.button("Revoke Access"):
             session.sql(f"REVOKE USAGE ON CORTEX SEARCH SERVICE {db}.{schema}.{target_svc} FROM ROLE {target_role}").collect()
             st.success("Revoked")

def render_tools_tab(session):
    st.subheader("5. Maintenance Tools")
    ctx = st.session_state.auth_context
    if st.button("🧹 Clear Temp Stages"):
        try:
            session.sql(f"REMOVE @{ctx['db']}.{ctx['schema']}.{ctx['stage']}/_temp_images").collect()
            st.success("Cleaned")
        except Exception as e: st.warning(f"Error: {e}")

def render_admin_view():
    st.title("🏭 Doc Refinery")
    session = get_snowpark_session()
    
    t1, t2, t3, t4, t5 = st.tabs(["Config", "Ingestion", "QA Studio", "Deployment", "Tools"])
    
    with t1: render_config_tab(session)
    with t2: render_ingestion_tab(session)
    with t3: render_qa_tab(session)
    with t4: render_deployment_tab(session)
    with t5: render_tools_tab(session)