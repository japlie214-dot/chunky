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
    get_snowpark_session, clean_text_for_sql, get_table_schema
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
    
    # 1. Infrastructure (Source)
    with st.expander("🏛️ Infrastructure & Source", expanded=True):
        c1, c2, c3 = st.columns(3)
        db = c1.text_input("Database", value=st.session_state.config.get("db", "SBOX_DB"), key="ing_db")
        schema = c2.text_input("Schema", value=st.session_state.config.get("schema", "AI_SB"), key="ing_sch")
        stage = c3.text_input("Stage", value="DOCS", key="ing_stg")
        stage_path = f"@{db}.{schema}.{stage}"
        
        # Dynamic File List
        pdf_files = []
        try:
            files = session.sql(f"LIST {stage_path} PATTERN='.*\\.pdf'").collect()
            pdf_files = [os.path.basename(f['name']) for f in files]
        except Exception as e:
            st.warning(f"Could not list files: {e}")

    # 2. Job Builder
    st.markdown("#### 📋 Job Builder")
    with st.container():
        jc1, jc2, jc3 = st.columns(3)
        
        # Column 1: File & Scope
        with jc1:
            sel_file = st.selectbox("Select PDF", pdf_files, key="jb_file") if pdf_files else st.selectbox("Select PDF", ["No files found"], key="jb_file")
            scope = st.radio("Scope", ["Full Doc", "Page Range"], horizontal=True, key="jb_scope")
            p_start, p_end = 1, 1
            if scope == "Page Range":
                p_start = st.number_input("Start", 1, value=1, key="jb_pstart")
                p_end = st.number_input("End", 1, value=10, key="jb_pend")

        # Column 2: Strategy
        with jc2:
            target_table = st.text_input("Target Table", "SUS_CHUNKS", key="jb_table")
            mode = st.radio("Write Mode", ["APPEND", "OVERWRITE", "SURGICAL"], index=0, key="jb_mode")
            use_layout = st.checkbox("Use Layout Parser (Uncheck for Text-only)", True, key="jb_layout")

        # Column 3: Add Action
        with jc3:
            st.write("Parameters")
            chk_sz = st.number_input("Chunk Size", 5000, 30000, 8000, key="jb_chunk")
            overlap = st.number_input("Overlap", 0, 5000, 2000, key="jb_overlap")
            if st.button("➕ Add Job", key="jb_add"):
                if pdf_files and sel_file != "No files found":
                    st.session_state.job_queue.append({
                        "id": len(st.session_state.job_queue)+1,
                        "file": sel_file,
                        "table": target_table,
                        "mode": mode,
                        "scope": scope,
                        "range": (p_start, p_end),
                        "strategy": use_layout,
                        "params": (chk_sz, overlap),
                        "status": "Pending"
                    })
                    st.success("Job Added")
                    log_action("JOB_ADDED", {"file": sel_file, "id": len(st.session_state.job_queue)})

    # 3. Execution Engine
    if st.session_state.job_queue:
        st.divider()
        st.write(f"**Queue ({len(st.session_state.job_queue)} Jobs)**")
        # Display queue as dataframe
        queue_df = pd.DataFrame([
            {
                "ID": j["id"],
                "File": j["file"],
                "Table": j["table"],
                "Mode": j["mode"],
                "Scope": j["scope"],
                "Range": f"{j['range'][0]}-{j['range'][1]}",
                "Layout": j["strategy"],
                "Status": j["status"]
            }
            for j in st.session_state.job_queue
        ])
        st.dataframe(queue_df, use_container_width=True)
        
        col_run, col_clear = st.columns(2)
        with col_run:
            if st.button("🚀 Run Batch", key="batch_run"):
                progress_bar = st.progress(0)
                completed_count = 0
                
                for idx, job in enumerate(st.session_state.job_queue):
                    if job['status'] != 'Pending':
                        continue
                    
                    try:
                        full_table_path = f"{db}.{schema}.{job['table']}"
                        
                        # Build WHERE clause for page range
                        page_filter = ""
                        if job['scope'] == "Page Range":
                            page_filter = f"AND PAGE_NUMBER BETWEEN {job['range'][0]} AND {job['range'][1]}"
                        
                        # Layout Parser Mode
                        parse_mode = "LAYOUT" if job['strategy'] else "TEXT"
                        
                        # Build SQL query
                        src_sql = f"""
                        SELECT
                            RELATIVE_PATH,
                            pages.value:index::INTEGER + 1 as PAGE_NUMBER,
                            chunks.value::VARCHAR as CHUNK,
                            CONCAT('CHK_', UUID_STRING()) as CHUNK_ID
                        FROM
                            DIRECTORY({stage_path}),
                            LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(TO_FILE('{stage_path}', RELATIVE_PATH), PARSE_JSON('{{"mode": "{parse_mode}"}}')):pages) pages,
                            LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(pages.value:content::VARCHAR, 'markdown', {job['params'][0]}, {job['params'][1]})) chunks
                        WHERE RELATIVE_PATH = '{job['file']}'
                        {page_filter}
                        """
                        
                        # Handle Write Modes
                        if job['mode'] == "OVERWRITE":
                            session.sql(f"CREATE OR REPLACE TABLE {full_table_path} AS {src_sql}").collect()
                        elif job['mode'] == "SURGICAL":
                            # Delete existing chunks for this file first
                            # Apply page range filter to DELETE if applicable
                            delete_filter = f"WHERE RELATIVE_PATH = '{job['file']}'"
                            if job['scope'] == "Page Range":
                                delete_filter += f" AND PAGE_NUMBER BETWEEN {job['range'][0]} AND {job['range'][1]}"
                            
                            session.sql(f"DELETE FROM {full_table_path} {delete_filter}").collect()
                            # Then insert
                            session.sql(f"INSERT INTO {full_table_path} {src_sql}").collect()
                        else:  # APPEND
                            # Check if table exists
                            check = session.sql(f"SHOW TABLES LIKE '{job['table']}' IN SCHEMA {db}.{schema}").collect()
                            if not check:
                                session.sql(f"CREATE TABLE {full_table_path} AS {src_sql}").collect()
                            else:
                                session.sql(f"INSERT INTO {full_table_path} {src_sql}").collect()
                        
                        job['status'] = 'Completed'
                        completed_count += 1
                        log_action("JOB_COMPLETED", {"file": job['file'], "id": job['id']})
                        
                    except Exception as e:
                        job['status'] = f'Failed: {str(e)[:50]}'
                        log_action("JOB_FAILED", {"file": job['file'], "error": str(e)})
                    
                    progress_bar.progress((idx + 1) / len(st.session_state.job_queue))
                
                st.success(f"Batch Complete: {completed_count} jobs processed")
                
        with col_clear:
            if st.button("🗑️ Clear Queue", key="queue_clear"):
                st.session_state.job_queue = []
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
    """Render the QA & Refinement Studio tab with Batch Selection"""
    st.subheader("2. QA & Refinement Studio")
    
    # Initialize Admin Queue
    if "admin_queue" not in st.session_state:
        st.session_state.admin_queue = []
    
    # Mode Selection
    mode = st.radio("Mode", ["Legacy (Single File)", "Batch Job"], horizontal=True, key="qa_mode")
    
    # Get completed jobs from queue
    completed_jobs = [j for j in st.session_state.get('job_queue', []) if j['status'] == 'Completed']
    
    c1, c2 = st.columns([3, 1])
    with c1:
        # Search for chunks
        table = st.session_state.config.get("target_table", "SUS_CHUNKS")
        db = st.session_state.config.get("db", "SBOX_DB")
        schema = st.session_state.config.get("schema", "AI_SB")
        full_table = f"{db}.{schema}.{table}"
        
        if mode == "Batch Job" and completed_jobs:
            sel_job = st.selectbox(
                "Select Job",
                completed_jobs,
                format_func=lambda x: f"Job #{x['id']} - {x['file']}",
                key="qa_job_sel"
            )
            # Filter by selected job's file
            job_file_filter = f"AND RELATIVE_PATH = '{sel_job['file']}'"
        else:
            job_file_filter = ""
        
        search_pg = st.number_input("Filter by Page (0=All)", 0, key="qa_page")
        if st.button("🔍 Search Chunks", key="qa_search"):
            where_clauses = []
            if search_pg > 0:
                where_clauses.append(f"PAGE_NUMBER = {search_pg}")
            if job_file_filter:
                where_clauses.append(f"RELATIVE_PATH = '{sel_job['file']}'")
            
            where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            try:
                df = session.sql(f"SELECT CHUNK_ID, PAGE_NUMBER, RELATIVE_PATH, SUBSTR(CHUNK,0,50) as P FROM {full_table} {where} LIMIT 50").to_pandas()
                st.session_state.admin_search_results = df
            except Exception:
                st.warning("Table not found or empty.")
    
    if "admin_search_results" in st.session_state and not st.session_state.admin_search_results.empty:
        sel_id = st.selectbox("Select Chunk", st.session_state.admin_search_results["CHUNK_ID"].tolist(), key="qa_chunk_sel")
        if st.button("Add to Queue", key="qa_add_queue"):
            st.session_state.admin_queue.append({"id": sel_id, "status": "Pending"})
            st.success(f"Added {sel_id}")

    st.divider()
    st.markdown("### Workbench")
    
    if st.session_state.admin_queue:
        # Simple queue processor
        item = st.session_state.admin_queue[0]
        st.write(f"**Editing:** {item['id']}")
        
        # Load actual chunk text
        try:
            curr_text = session.sql(f"SELECT CHUNK, RELATIVE_PATH, PAGE_NUMBER FROM {full_table} WHERE CHUNK_ID = '{item['id']}'").collect()[0]
            chunk_text, rel_path, page_num = curr_text
            
            # Display metadata
            c_meta, c_img = st.columns([1, 2])
            with c_meta:
                st.info(f"**File:** {rel_path}\n**Page:** {page_num}")
            
            with c_img:
                # Side-by-side Image vs Text logic (placeholder for Vision integration)
                if convert_from_bytes and Image:
                    st.markdown("**Vision Preview** (if available)")
                    # Future: Load and display page image for comparison
                    st.caption("Image preview requires Vision-enabled processing")
            
            new_text = st.text_area("Edit Chunk Content", value=chunk_text, height=300, key="qa_edit")
            
            col_commit, col_skip = st.columns(2)
            with col_commit:
                if st.button("💾 Commit Change", key="qa_commit"):
                    # Use parameterized query to avoid fragile string interpolation
                    session.sql(f"UPDATE {full_table} SET CHUNK = ? WHERE CHUNK_ID = ?", params=[new_text, item['id']]).collect()
                    st.session_state.admin_queue.pop(0)
                    st.success("Updated and removed from queue.")
                    log_action("QA_COMMIT", {"chunk_id": item['id']})
                    time.sleep(1)
                    st.rerun()
            with col_skip:
                if st.button("⏭️ Skip", key="qa_skip"):
                    st.session_state.admin_queue.pop(0)
                    st.info("Skipped this chunk.")
                    time.sleep(0.5)
                    st.rerun()
                    
        except Exception as e:
            st.error(f"Error loading chunk: {e}")
    else:
        st.info("Queue is empty. Search and add chunks to begin QA.")


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