# views/refinery/tab_qa.py
# QA Tab - Quality Assurance and Refinement Studio for the Doc Refinery package
import streamlit as st
import pandas as pd
import os
import tempfile
import re
import json
import textwrap
from logger_config import log_action
from utils.core_utils import (
    PDFUtils, QualityInspector, Image, convert_from_bytes, save_optimized_image, clean_text_for_sql
)
from utils.snowflake_utils import (
    run_cortex, CORTEX_MODEL
)
import prompts

# Safe Import: mistletoe for hybrid Markdown rendering
try:
    import mistletoe
    MISTLETOE_AVAILABLE = True
except ImportError:
    MISTLETOE_AVAILABLE = False

# Safe Import: Snowpark
try:
    from snowflake.snowpark.functions import col
except Exception:
    col = None

def render_quality_inspector(session):
    """Context Locking"""
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
            
            # Use Snowpark table API with robust filtering to handle identifiers safely
            try:
                # Use column expression if available, otherwise safely escaped string
                if col is not None:
                    filter_expr = col("CHUNK_ID") == t_item['id']
                else:
                    safe_id = t_item['id'].replace("'", "''")
                    filter_expr = f"CHUNK_ID = '{safe_id}'"
                
                data = session.table([ctx['db'], ctx['schema'], t_tbl_base]) \
                              .filter(filter_expr) \
                              .select("CHUNK").collect()
            except Exception as e:
                t_item['status'] = f"Error: SQL Retrieval {e}"
                continue
            
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
                        
                        # UPDATED CALL (unpack 3, ignore tokens here)
                        res, _, _ = run_cortex(session, prompt, stage_root, rel_img_path, model=CORTEX_MODEL)
                        
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
    st.rerun()

def render_single_item_inspector(session, item, db, sch, stage_root):
    """Split screen inspector: Visual vs (Read-only Content + Editable Draft)."""
    # Context Prefixing - Enforce authenticated schema
    table_base = item.get('table', '').split('.')[-1]
    work_table = f'"{db}"."{sch}"."{table_base}"'
    
    try:
        # Use parameterized query for safety and to handle IDs with single quotes
        sql = f"SELECT CHUNK FROM {work_table} WHERE CHUNK_ID = ?"
        data = session.sql(sql, params=[item['id']]).collect()
        original_chunk = data[0]['CHUNK'] if data else "[Error: Chunk not found]"
    except Exception as e:
        original_chunk = f"[Error: {e}]"

    # Display Mode toggle at the top of the inspector
    _mode_options = ["Rendered", "Raw"]
    st.session_state.qa_display_mode = st.radio(
        "Display Mode",
        _mode_options,
        index=_mode_options.index(st.session_state.get("qa_display_mode", "Rendered")),
        horizontal=True,
        help="Rendered: high-fidelity Markdown preview. Raw: raw editable text. Original chunk is always read-only.",
    )

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
            
        draft_val = item.get('draft_text', "")
        mode = st.session_state.get("qa_display_mode", "Rendered")

        def unescape_chunk(text_content):
            """Unescape literal string representations and JSON-style quotes."""
            try:
                if text_content.startswith('"') and text_content.endswith('"'):
                    parsed_text = json.loads(text_content)
                    if isinstance(parsed_text, str):
                        text_content = parsed_text
            except Exception:
                pass
            if '\\n' in text_content or '\\"' in text_content or '\\t' in text_content:
                text_content = text_content.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"')
            if text_content.startswith('"') and text_content.endswith('"'):
                text_content = text_content[1:-1]
            return text_content

        def render_hybrid_markdown(text_content):
            """Two-layer pipeline: Markdown→HTML → Styled Container."""
            if MISTLETOE_AVAILABLE:
                html_content = mistletoe.markdown(text_content)
            else:
                html_content = f"<pre>{text_content}</pre>"

            # Layer 3: Joined string with pre-wrap on container for robust formatting
            wrapper_html = "".join([
                '<div class="rag-doc-panel" style="white-space: pre-wrap; background-color: rgba(128, 128, 128, 0.05); border: 1px solid rgba(128, 128, 128, 0.2); padding: 15px; border-radius: 6px; margin-bottom: 10px;">',
                html_content,
                '</div>'
            ])
            st.markdown(wrapper_html, unsafe_allow_html=True)

        # Pre-process original chunk for consistent display in both modes
        original_chunk_clean = unescape_chunk(original_chunk)

        st.markdown("##### 📄 Original Chunk")
        if mode == "Rendered":
            render_hybrid_markdown(original_chunk_clean)
        else:
            # st.code now provides the unescaped markdown for copying
            st.code(original_chunk_clean, language=None)
        
        st.markdown("##### 📄 Draft Preview")
        if mode == "Rendered":
            render_hybrid_markdown(unescape_chunk(draft_val) if draft_val else "*No draft generated yet.*")
        else:
            item['draft_text'] = st.text_area(
                "Draft", value=draft_val, key=f"draft_edit_{item['id']}"
            )
            if item['draft_text'] != draft_val:
                item['status'] = 'Modified'
        
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✨ Generate", key=f"gen_{item['id']}"):
                process_batch_generation(session, [item], stage_root)
        with c2:
            if st.button("💾 Commit", key=f"save_{item['id']}"):
                sql = f"UPDATE {work_table} SET CHUNK = ? WHERE CHUNK_ID = ?"
                try:
                    session.sql(sql, params=[item['draft_text'], item['id']]).collect()
                    item['status'] = 'Committed'
                    st.success("Saved")
                except Exception as e:
                    st.error(f"Commit failed: {e}")

def render_qa_tab(session):
    st.subheader("3. QA & Refinement Studio")
    
    # Centralized CSS for QA Panels
    st.markdown(textwrap.dedent("""
        <style>
        .rag-doc-panel p {
            white-space: pre-wrap;
        }
        </style>
    """), unsafe_allow_html=True)
    
    # Context Retrieval
    ctx = st.session_state.auth_context
    db, schema, stage = ctx["db"], ctx["schema"], ctx["stage"]
    stage_root = f"@{db}.{schema}.{stage}"

    if "admin_queue" not in st.session_state: st.session_state.admin_queue = []
    # Initialize display mode once per session; default is Rendered per UX spec.
    if "qa_display_mode" not in st.session_state: st.session_state.qa_display_mode = "Rendered"
    
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
            pg_input = st.text_input("Page Filter (e.g., '1-5, 8')", key="qa_pg_text")
            
            if st.button("Search", key="qa_search"):
                # Enforce authenticated schema
                tbl_base = current_search_table.split('.')[-1]
                full_tbl = f'"{db}"."{schema}"."{tbl_base}"'
                where = []
                
                if pg_input.strip():
                    try:
                        pages_to_query = set()
                        parts = pg_input.split(',')
                        for part in parts:
                            part = part.strip()
                            if '-' in part:
                                s, e = part.split('-')
                                pages_to_query.update(range(int(s), int(e) + 1))
                            elif part.isdigit():
                                pages_to_query.add(int(part))
                        
                        if pages_to_query:
                            pg_list = ", ".join(str(p) for p in sorted(list(pages_to_query)))
                            where.append(f"PAGE_NUMBER IN ({pg_list})")
                    except Exception:
                        st.toast("⚠️ Invalid page format. Ignoring page filter.", icon="⚠️")

                if current_search_file:
                    safe_f = clean_text_for_sql(current_search_file)
                    where.append(f"RELATIVE_PATH = '{safe_f}'")
                
                where_clause = f"WHERE {' AND '.join(where)}" if where else ""
                
                # Fetch RELATIVE_PATH from DB to ensure it's never empty in the workbench
                sql = f"SELECT CHUNK_ID, PAGE_NUMBER, RELATIVE_PATH, SUBSTR(CHUNK, 1, 80) as PREVIEW FROM {full_tbl} {where_clause} LIMIT 100"
                try:
                    res_df = session.sql(sql).to_pandas()
                    st.session_state.qa_results = res_df.sort_values(by="PAGE_NUMBER")
                except Exception as e:
                    st.error(f"Search failed: {e}")
                    
            if "qa_results" in st.session_state and not st.session_state.qa_results.empty:
                qa_df = st.session_state.qa_results
                
                def fmt_chunk_opt(cid):
                    try:
                        row = qa_df[qa_df['CHUNK_ID'] == cid].iloc[0]
                        return f"{cid} (Pg {row['PAGE_NUMBER']})"
                    except:
                        return cid

                sel_chunk = st.selectbox(
                    "Found",
                    qa_df["CHUNK_ID"].tolist(),
                    format_func=fmt_chunk_opt,
                    key="qa_chunk_sel"
                )
                if st.button("➕ Add to Workbench"):
                    existing_ids = [x['id'] for x in st.session_state.admin_queue]
                    if sel_chunk in existing_ids:
                        st.warning(f"Chunk `{sel_chunk}` is already in the workbench.")
                    else:
                        matches = st.session_state.qa_results[st.session_state.qa_results.CHUNK_ID == sel_chunk]
                        if not matches.empty:
                            row = matches.iloc[0]
                            st.session_state.admin_queue.append({
                            "id": sel_chunk, "status": "Pending",
                            "file": row['RELATIVE_PATH'], # Use path from DB, not user input
                            "table": current_search_table,
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
        
        # Ensure table column is visible and data is prepped
        if "table" not in df_queue.columns:
            df_queue["table"] = "Unknown"
            
        # Rename keys for display
        df_display = df_queue.rename(columns={
            "page_number": "Page Number",
            "context_instruction": "Instruction",
            "preview": "Original",
            "draft_text": "Draft",
            "table": "Target Table"
        })

        # Ensure 'Original' is treated as a read-only preview to avoid misleading the user
        edited_df = st.data_editor(
            # Added "Target Table" to the column list
            df_display[["selected", "id", "Target Table", "Page Number", "file", "Instruction", "Original", "Draft", "status"]],
            column_config={
                "selected": st.column_config.CheckboxColumn("Sel", width="small"),
                "id": st.column_config.TextColumn("ID", disabled=True),
                # Target Table disabled but always visible
                "Target Table": st.column_config.TextColumn("Target Table", disabled=True, width="medium"),
                "Page Number": st.column_config.NumberColumn("Pg", disabled=True, width="small"),
                "file": st.column_config.TextColumn("File", disabled=True),
                "Instruction": st.column_config.TextColumn("Instruction", width="medium"),
                "Original": st.column_config.TextColumn("Original", disabled=True, width="large"),
                "Draft": st.column_config.TextColumn("Draft", width="large"),
                "status": st.column_config.TextColumn("Status", disabled=True)
            },
            use_container_width=True, hide_index=True, key="qa_editor_v4"
        )
        
        # Sync changes back to session state
        for index, row in edited_df.iterrows():
             for item in st.session_state.admin_queue:
                 if item["id"] == row["id"]:
                     item["selected"] = row["selected"]
                     item["context_instruction"] = row["Instruction"]
                     # 'Original' is a truncated preview (SUBSTR 80) queried from the DB.
                     # Edits to it are ignored by the generation logic, so it is kept disabled.
                     item["draft_text"] = row["Draft"]

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
                        full_tbl = f'"{db}"."{schema}"."{tbl_base}"'
                        sql = f"UPDATE {full_tbl} SET CHUNK = ? WHERE CHUNK_ID = ?"
                        try:
                            session.sql(sql, params=[item['draft_text'], item['id']]).collect()
                            item['status'] = 'Committed'
                            count += 1
                        except Exception as e:
                            log_action("BATCH_COMMIT_ERROR", {"error": str(e)})
                st.success(f"Committed {count} items.")
                st.rerun()
        with b3:
             if st.button("🗑️ Remove (Selected)"):
                 st.session_state.admin_queue = [i for i in st.session_state.admin_queue if not i.get('selected')]
                 st.rerun()

        # Item Inspector
        st.divider()
        sel_idx = st.selectbox(
            "Inspect Item",
            range(len(st.session_state.admin_queue)),
            format_func=lambda x: f"{st.session_state.admin_queue[x]['id']} (Pg {st.session_state.admin_queue[x]['page_number']})",
            key="qa_inspect_sel"
        )
        item = st.session_state.admin_queue[sel_idx]
        # ORDERING CONSTRAINT (load-bearing): the edited_df sync loop above must
        # always execute before render_single_item_inspector so that item['draft_text']
        # in session state reflects the latest data_editor edits before the inspector
        # reads it. Do not reorder these call sites without updating the sync contract.
        render_single_item_inspector(session, item, db, schema, stage_root)

    st.divider()
    # render_quality_inspector(session)  # Disabled per requirement
