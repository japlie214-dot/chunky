# views/admin.py
# Phase 4: Knowledge Base (Admin) View Module
import streamlit as st
import pandas as pd
import json
import os
import time
from logger_config import log_action
from utils.core_utils import (
    PDFUtils, PromptEngine, QualityInspector, RAGAnalytics, Image, convert_from_bytes
)
from utils.snowflake_utils import (
    get_snowpark_session, clean_text_for_sql, get_table_schema, run_cortex
)

# Safe Import: Snowpark
try:
    from snowflake.snowpark.functions import col
except Exception:
    col = None

# -----------------------------------------------------------------------------
# SUB-RENDERERS (Tabs)
# -----------------------------------------------------------------------------

def render_ingestion_tab(session):
    """Render the Ingestion Pipeline tab with Multi-PDF Job Queue"""
    st.subheader("1. Ingestion Pipeline (Job Queue)")
    
    # Initialize Job Queue
    if 'job_queue' not in st.session_state:
        st.session_state.job_queue = []
    
    # Initialize metrics
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
            
            # Load metadata if possible (page count)
            page_count_est = 1
            if sel_file != "No files":
                # Check cache or simple heuristic
                pass
            
            p_start, p_end = 1, 10
            if scope == "Page Range":
                c_rng1, c_rng2 = st.columns(2)
                p_start = c_rng1.number_input("Start", 1, value=1, key="jb_pstart")
                p_end = c_rng2.number_input("End", 1, value=10, key="jb_pend")

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
            
            # Auto-default target file for surgical
            target_file_param = sel_file if mode == "SURGICAL" else None
            
            st.write("")
            if st.button("➕ Add Job", key="jb_add", type="primary", disabled=(not pdf_files)):
                if sel_file != "No files":
                    st.session_state.job_queue.append({
                        "id": len(st.session_state.job_queue)+1,
                        "file": sel_file,
                        "table": target_table,
                        "mode": mode,
                        "scope": scope,
                        "range": (p_start, p_end),
                        "layout": use_layout,
                        "vision": use_vision,
                        "params": (chk_sz, overlap),
                        "surgical_file": target_file_param,
                        "status": "Pending"
                    })
                    st.success("Job Added")
                    log_action("JOB_ADDED", {"file": sel_file, "id": len(st.session_state.job_queue)})
                    
                    # OVERWRITE Conflict Detection Warning
                    from collections import Counter
                    overwrites = [j['table'] for j in st.session_state.job_queue if j['mode'] == 'OVERWRITE']
                    for tbl, count in Counter(overwrites).items():
                        if count > 1:
                            st.warning(f"⚠️ Multiple OVERWRITE jobs detected for table `{tbl}`. Previous OVERWRITE jobs will be overwritten by later ones.")

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
                "Pages": f"{j['range'][0]}-{j['range'][1]}" if j["scope"] == "Page Range" else "All",
                "Strategy": f"{'L' if j['layout'] else ''}{'+' if j['layout'] and j['vision'] else ''}{'V' if j['vision'] else ''}",
                "Status": j["status"]
            }
            for j in st.session_state.job_queue
        ])
        
        edited_df = st.data_editor(queue_df, use_container_width=True, num_rows="dynamic", key="jb_editor")
        
        # Sync deletions if needed (basic implementation)
        if len(edited_df) < len(st.session_state.job_queue):
            # Logic to remove deleted rows from session state would go here
            pass

        col_run, col_clear = st.columns(2)
        with col_run:
            if st.button("🚀 Run Batch", key="batch_run", type="primary"):
                # DUAL PROGRESS BARS
                global_bar = st.progress(0, text="Starting Batch...")
                local_bar = st.progress(0, text="Waiting...")
                
                total_jobs = len(st.session_state.job_queue)
                completed_count = 0
                total_pages = 0
                total_chunks = 0
                
                for idx, job in enumerate(st.session_state.job_queue):
                    if job['status'] == 'Completed': continue
                    
                    job['status'] = 'Running'
                    global_bar.progress((idx) / total_jobs, text=f"Processing Job {job['id']}/{total_jobs}: {job['file']}")
                    
                    try:
                        full_table = f"{db}.{schema}.{job['table']}"
                        
                        # 1. Surgical Cleanup
                        if job['mode'] == 'SURGICAL':
                            del_file = job['surgical_file'] or job['file']
                            pg_filter = ""
                            if job['scope'] == "Page Range":
                                pg_filter = f"AND PAGE_NUMBER BETWEEN {job['range'][0]} AND {job['range'][1]}"
                            session.sql(f"DELETE FROM {full_table} WHERE RELATIVE_PATH = '{del_file}' {pg_filter}").collect()
                        
                        # 2. Execution Strategy
                        # Determine Page Range
                        if job['scope'] == "Page Range":
                            start_p, end_p = job['range']
                            pg_filter = f" AND pages.value:index::INTEGER + 1 BETWEEN {start_p} AND {end_p}"
                        else:
                            start_p, end_p = 1, None  # Logic for full doc
                            pg_filter = ""
                        
                        # Strategy: Layout Parser
                        if job['layout']:
                            local_bar.progress(0.2, text=f"Job {job['id']}: Running Layout Parser...")
                            parse_mode = "LAYOUT"
                            
                            # Build SQL query for Layout Parser
                            src_sql = f"""
                            SELECT
                                RELATIVE_PATH,
                                pages.value:index::INTEGER + 1 as PAGE_NUMBER,
                                chunks.value::VARCHAR as CHUNK,
                                CONCAT('CHK_', UUID_STRING()) as CHUNK_ID,
                                'STANDARD' as CHUNK_TYPE
                            FROM
                                DIRECTORY({stage_path}),
                                LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(TO_FILE('{stage_path}', RELATIVE_PATH), PARSE_JSON('{{"mode": "{parse_mode}"}}')):pages) pages,
                                LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(pages.value:content::VARCHAR, 'markdown', {job['params'][0]}, {job['params'][1]})) chunks
                            WHERE RELATIVE_PATH = '{job['file']}'
                            {pg_filter}
                            """
                            
                            # Handle Write Modes for Layout
                            # User-Driven Write Modes: Execute OVERWRITE as requested by the user
                            if job['mode'] == "OVERWRITE":
                                session.sql(f"CREATE OR REPLACE TABLE {full_table} AS {src_sql}").collect()
                            else:  # APPEND or SURGICAL
                                check = session.sql(f"SHOW TABLES LIKE '{job['table']}' IN SCHEMA {db}.{schema}").collect()
                                if not check:
                                    session.sql(f"CREATE TABLE {full_table} AS {src_sql}").collect()
                                else:
                                    session.sql(f"INSERT INTO {full_table} {src_sql}").collect()
                            
                            # Count chunks created specifically for this job's scope
                            chunk_count = session.sql(f"SELECT COUNT(*) as C FROM {full_table} WHERE RELATIVE_PATH = '{job['file']}' {pg_filter}").collect()[0]['C']
                            total_chunks += chunk_count
                            
                            # Resolve actual page count for accurate reporting
                            if not end_p:
                                try:
                                    pdf_bytes = session.file.get_stream(f"{stage_path}/{job['file']}").read()
                                    actual_end = PDFUtils.get_page_count(pdf_bytes)
                                except: actual_end = 1
                            else: actual_end = end_p
                            total_pages += (actual_end - start_p + 1)
                        
                        # Strategy: Vision Parser (Loop)
                        if job['vision']:
                            # Ensure end_p is resolved for Full Doc scope
                            if end_p is None:
                                try:
                                    pdf_bytes = session.file.get_stream(f"{stage_path}/{job['file']}").read()
                                    end_p = PDFUtils.get_page_count(pdf_bytes)
                                except: end_p = 1
                            
                            for pg in range(start_p, end_p + 1):
                                progress_val = 0.5 + (0.5 * (pg - start_p + 1) / (end_p - start_p + 1))
                                local_bar.progress(min(progress_val, 1.0), text=f"Job {job['id']}: Vision Analyzing Page {pg}...")
                                
                                try:
                                    # Fetch chunks created by Layout parser to enhance them
                                    page_chunks = session.sql(f"SELECT CHUNK_ID, CHUNK FROM {full_table} WHERE RELATIVE_PATH = '{job['file']}' AND PAGE_NUMBER = {pg}").collect()
                                    for c_row in page_chunks:
                                        prompt = PromptEngine.get_prompt(c_row['CHUNK'], "High-fidelity reconstruction required for charts and tables.")
                                        # Assumes image naming convention from extraction process
                                        img_path = f"_temp_images/{job['file']}_p{pg}.png"
                                        res = run_cortex(session, prompt, stage_path, img_path, model='claude-4-sonnet')
                                        if res:
                                            safe_res = clean_text_for_sql(res)
                                            session.sql(f"UPDATE {full_table} SET CHUNK = '{safe_res}', CHUNK_TYPE = 'ENHANCED' WHERE CHUNK_ID = '{c_row['CHUNK_ID']}'").collect()
                                except Exception as vision_e:
                                    log_action("VISION_ERROR", {"id": job['id'], "page": pg, "error": str(vision_e)})
                        
                        job['status'] = 'Completed'
                        completed_count += 1
                        log_action("JOB_COMPLETE", {"id": job['id'], "file": job['file']})
                        
                    except Exception as e:
                        job['status'] = f"Failed: {str(e)[:20]}"
                        log_action("JOB_FAILED", {"id": job['id'], "error": str(e)})
                    
                    global_bar.progress((idx + 1) / total_jobs)
                
                # Update metrics
                st.session_state.batch_metrics['chunks_created'] = total_chunks
                st.session_state.batch_metrics['pages_processed'] = total_pages
                
                global_bar.progress(1.0, text="Batch Complete")
                local_bar.empty()
                
                # Display metrics
                st.success(f"Batch Execution Finished: {completed_count} jobs processed")
                col_m1, col_m2, col_m3 = st.columns(3)
                with col_m1:
                    st.metric("Chunks Created", total_chunks)
                with col_m2:
                    st.metric("Pages Processed", total_pages)
                with col_m3:
                    st.metric("Jobs Completed", completed_count)
                
        with col_clear:
            if st.button("🗑️ Clear Queue", key="queue_clear"):
                st.session_state.job_queue = []
                st.session_state.batch_metrics = {
                    'chunks_created': 0,
                    'chunks_enhanced': 0,
                    'pages_processed': 0
                }
                st.success("Queue cleared")
                st.rerun()

    # 4. Inspector / Auto-Fix
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
                # Add to local queue
                if sel_chunk not in [x['id'] for x in st.session_state.admin_queue]:
                     st.session_state.admin_queue.append({"id": sel_chunk, "status": "Pending", "file": selected_file, "table": selected_table})
                     st.success(f"Added {sel_chunk} to workbench.")
                     log_action("QA_QUEUE_ADD", {"chunk_id": sel_chunk})

    # 3. Workbench Interface
    st.divider()
    st.markdown(f"### 🛠️ Workbench ({len(st.session_state.admin_queue)} Items)")
    
    if st.session_state.admin_queue:
        # Item Selector
        curr_idx = st.number_input("Queue Index", 0, len(st.session_state.admin_queue)-1, 0, key="qa_idx")
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
            
            col_vis, col_edit = st.columns(2)
            
            # Left: Visual Ground Truth Panel
            with col_vis:
                st.caption(f"📄 Page {pg_num} of {f_path}")
                st.markdown("**Visual Ground Truth**")
                if convert_from_bytes and Image:
                    try:
                        # Attempt to retrieve and display the page image from stage
                        stage = st.session_state.config.get("stage", "DOCS")
                        stage_path = f"@{db}.{sch}.{stage}"
                        
                        # Get image file path (assuming PDF pages are stored as images)
                        image_rel_path = f"{f_path}_page_{pg_num}.png"
                        
                        # Try to load image (placeholder for actual implementation)
                        st.info("Visual Preview: Image loading from stage (requires image extraction)")
                        st.caption("In production, this would display the actual PDF page image for comparison")
                        
                    except Exception as img_e:
                        st.warning(f"Could not load visual preview: {img_e}")
                else:
                    st.info("Visual preview requires pdf2image and PIL libraries")
                
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
                new_text = st.text_area("Content", value=chunk_txt, height=400, key=f"edit_{item['id']}")
                
                # Context Instruction for AI Enhancement
                with st.expander("💡 AI Enhancement Options"):
                    st.info(PromptEngine.get_instruction_tooltip())
                    context_inst = st.text_area("Context Instruction (optional)", placeholder="e.g., Convert the bar chart into a Markdown table...", key=f"ctx_{item['id']}")
                    
                    if st.button("✨ Generate Draft (AI)", key=f"gen_{item['id']}"):
                        with st.spinner("Generating AI draft..."):
                            try:
                                stage = st.session_state.config.get("stage", "DOCS")
                                stage_path_root = f"@{db}.{sch}.{stage}"
                                img_path = f"_temp_images/{f_path}_p{pg_num}.png"
                                
                                prompt = PromptEngine.get_prompt(chunk_txt, context_inst)
                                res = run_cortex(session, prompt, stage_path_root, img_path, model='claude-4-sonnet')
                                
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
                         safe_txt = clean_text_for_sql(new_text)
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