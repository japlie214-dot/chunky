# views/admin.py
# Phase 4: Knowledge Base (Admin) View Module
import streamlit as st
import pandas as pd
import json
import os
import time
from logger_config import log_action
from utils.core_utils import (
    PDFUtils, PromptEngine, QualityInspector, Image, convert_from_bytes
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
    """Render the Ingestion Pipeline tab"""
    st.subheader("1. Ingestion Pipeline")
    
    # 1. Config Section (Defaults from Global)
    with st.expander("⚙️ Pipeline Configuration", expanded=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            db = st.text_input("Database", value=st.session_state.config.get("db", "SBOX_DB"), key="adm_db")
            schema = st.text_input("Schema", value=st.session_state.config.get("schema", "AI_SB"), key="adm_sch")
            stage = st.text_input("Stage", value="DOCS", key="adm_stg")
        
        with c2:
            target_table = st.text_input("Target Table", value="SUS_CHUNKS", key="adm_tbl")
            write_mode = st.selectbox("Write Mode", ["APPEND", "OVERWRITE"], key="adm_mode")
            
        with c3:
            chunk_size = st.number_input("Chunk Size", value=8000, step=500, key="adm_sz")
            overlap = st.number_input("Overlap", value=2000, step=100, key="adm_ov")

    # 2. File Selection
    stage_path = f"@{db}.{schema}.{stage}"
    full_table_path = f"{db}.{schema}.{target_table}"
    
    try:
        # Snowflake LIST uses regex; use a safe regex to match .pdf files
        files = session.sql(f"LIST {stage_path} PATTERN='.*\\.pdf'").collect()
        filenames = [os.path.basename(f['name']) for f in files]
        selected_file = st.selectbox("Select PDF", filenames, key="adm_file_sel")
    except Exception as e:
        st.error(f"Could not list files in {stage_path}: {e}")
        return

    # 3. Execution Logic
    if st.button("🚀 Start Ingestion"):
        if not selected_file: return
        
        log_action("INGEST_START", {"file": selected_file, "mode": write_mode})
        with st.spinner("Parsing and Chunking..."):
            try:
                # Basic Parsing SQL (Simplified from Notebook)
                # Note: AI_PARSE_DOCUMENT expects a config object; provide mode=LAYOUT
                src_sql = f"""
                SELECT
                    RELATIVE_PATH,
                    pages.value:index::INTEGER + 1 as PAGE_NUMBER,
                    chunks.value::VARCHAR as CHUNK,
                    CONCAT('CHK_', UUID_STRING()) as CHUNK_ID
                FROM
                    DIRECTORY({stage_path}),
                    LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(TO_FILE('{stage_path}', RELATIVE_PATH), PARSE_JSON('{{"mode": "LAYOUT"}}')):pages) pages,
                    LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(pages.value:content::VARCHAR, 'markdown', {chunk_size}, {overlap})) chunks
                WHERE RELATIVE_PATH = '{selected_file}'
                """
                
                if write_mode == "OVERWRITE":
                    session.sql(f"CREATE OR REPLACE TABLE {full_table_path} AS {src_sql}").collect()
                else:
                    # Append handling
                    check = session.sql(f"SHOW TABLES LIKE '{target_table}' IN SCHEMA {db}.{schema}").collect()
                    if not check:
                        session.sql(f"CREATE TABLE {full_table_path} AS {src_sql}").collect()
                    else:
                        session.sql(f"INSERT INTO {full_table_path} {src_sql}").collect()
                
                st.success(f"Ingestion complete for {selected_file}")
                log_action("INGEST_COMPLETE", {"file": selected_file})
                
            except Exception as e:
                st.error(f"Ingestion failed: {e}")
                log_action("INGEST_ERROR", str(e))

    # 4. Inspector / Auto-Fix (Mini Version)
    if st.button("🕵️ Run Quality Inspector"):
        with st.spinner("Analyzing Chunks..."):
            try:
                if col is not None:
                    df = session.table(full_table_path).filter(col("RELATIVE_PATH") == selected_file).to_pandas()
                else:
                    df = session.sql(f"SELECT * FROM {full_table_path} WHERE RELATIVE_PATH = '{selected_file}'").to_pandas()
                df["STATUS"] = df["CHUNK"].apply(QualityInspector.inspect)
                defects = df[df["STATUS"] != "OK"]
                
                st.info(f"Found {len(defects)} potential issues out of {len(df)} chunks.")
                if not defects.empty:
                    st.dataframe(defects[["PAGE_NUMBER", "STATUS", "CHUNK_ID"]])
                    log_action("INSPECT_RUN", {"defects": len(defects)})
            except Exception as e:
                st.error(f"Inspector failed: {e}")


def render_qa_tab(session):
    """Render the QA & Refinement Studio tab"""
    st.subheader("2. QA & Refinement Studio")
    
    # Initialize Admin Queue
    if "admin_queue" not in st.session_state: st.session_state.admin_queue = []
    
    c1, c2 = st.columns([3, 1])
    with c1:
        # Search for chunks
        table = st.session_state.config.get("target_table", "SUS_CHUNKS")
        db = st.session_state.config.get("db", "SBOX_DB")
        schema = st.session_state.config.get("schema", "AI_SB")
        full_table = f"{db}.{schema}.{table}"
        
        search_pg = st.number_input("Filter by Page (0=All)", 0)
        if st.button("🔍 Search Chunks"):
            where = f"WHERE PAGE_NUMBER = {search_pg}" if search_pg > 0 else ""
            try:
                df = session.sql(f"SELECT CHUNK_ID, PAGE_NUMBER, SUBSTR(CHUNK,0,50) as P FROM {full_table} {where} LIMIT 50").to_pandas()
                st.session_state.admin_search_results = df
            except Exception:
                st.warning("Table not found or empty.")
    
    if "admin_search_results" in st.session_state and not st.session_state.admin_search_results.empty:
        sel_id = st.selectbox("Select Chunk", st.session_state.admin_search_results["CHUNK_ID"].tolist())
        if st.button("Add to Queue"):
            st.session_state.admin_queue.append({"id": sel_id, "status": "Pending"})
            st.success(f"Added {sel_id}")

    st.divider()
    st.markdown("### Workbench")
    
    if st.session_state.admin_queue:
        # Simple queue processor
        item = st.session_state.admin_queue[0]
        st.write(f"Editing: {item['id']}")
        
        # Load actual chunk text
        try:
            curr_text = session.sql(f"SELECT CHUNK FROM {full_table} WHERE CHUNK_ID = '{item['id']}'").collect()[0][0]
            new_text = st.text_area("Edit Chunk Content", value=curr_text, height=300)
            
            if st.button("💾 Commit Change"):
                # Use parameterized query to avoid fragile string interpolation
                session.sql(f"UPDATE {full_table} SET CHUNK = ? WHERE CHUNK_ID = ?", params=[new_text, item['id']]).collect()
                st.session_state.admin_queue.pop(0)
                st.success("Updated and removed from queue.")
                log_action("QA_COMMIT", {"chunk_id": item['id']})
                time.sleep(1)
                st.rerun()
        except Exception as e:
            st.error(f"Error loading chunk: {e}")
    else:
        st.info("Queue is empty.")


def render_deployment_tab(session):
    """Render the Cortex Search Deployment tab"""
    st.subheader("3. Cortex Search Deployment")
    
    with st.form("deploy_form"):
        svc_name = st.text_input("Service Name", "CS_RAG_V1")
        target_table = st.text_input("Source Table", f"{st.session_state.config.get('db')}.{st.session_state.config.get('schema')}.SUS_CHUNKS")
        wh = st.selectbox("Warehouse", ["COMPUTE_WH", "SBOX_WH"])
        
        if st.form_submit_button("Deploy Service"):
            try:
                sql = f"""
                CREATE OR REPLACE CORTEX SEARCH SERVICE {st.session_state.config.get('db')}.{st.session_state.config.get('schema')}.{svc_name}
                ON CHUNK
                ATTRIBUTES PAGE_NUMBER, RELATIVE_PATH
                WAREHOUSE = {wh}
                TARGET_LAG = '1 minute'
                AS (
                    SELECT CHUNK, RELATIVE_PATH, PAGE_NUMBER, CHUNK_ID
                    FROM {target_table}
                )
                """
                session.sql(sql).collect()
                st.success(f"Deployed {svc_name} successfully!")
                log_action("DEPLOY_SUCCESS", {"service": svc_name})
            except Exception as e:
                st.error(f"Deployment failed: {e}")
                log_action("DEPLOY_ERROR", str(e))


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