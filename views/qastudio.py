# views/qastudio.py
# Shared QA Studio — chunk inspection, draft editing, workbench.
# Used by both CCS wizard (Step 4) and standalone QA Studio page.
#
# Two modes controlled by the `mode` parameter:
#   mode="jobs"       — No Search Scope UI. Uses completed jobs from the
#                        `jobs` param (CCS) or st.session_state['job_queue']
#                        (Refinery). Always "From Completed Jobs" behavior.
#   mode="standalone"  — Shows Search Scope radio. Defaults to "Manual
#                        Search in Current Schema". User can switch to
#                        "From Completed Jobs" if jobs are available.

import streamlit as st
import pandas as pd
import os
import tempfile
import json
import textwrap
from logger_config import log_action
from utils.core_utils import (
    PDFUtils, Image, convert_from_bytes, save_optimized_image, clean_text_for_sql
)
from utils.snowflake_utils import run_cortex, CORTEX_MODEL
from utils.constants import QA_PDF_CACHE_PREFIX, CHUNK_PREVIEW_LENGTH, TEMP_IMAGE_PREFIX
from utils.display_safety import safe_markdown, safe_code, safe_data_editor
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_pdf_name(rel_path: str) -> str:
    """Extract just the PDF filename from a RELATIVE_PATH (strips folder prefix)."""
    if not rel_path:
        return "Unknown"
    return rel_path.split('/')[-1] if '/' in rel_path else rel_path


def _get_original_pdf_page(chunk_metadata, page_number: int) -> int:
    """Resolve the original PDF page number from chunk metadata.

    For surgical chunks, PAGE_NUMBER in the table may have been shifted
    from the original PDF page. The surgical metadata stores
    'original_pdf_page' per mapping entry so QA Studio can render the
    correct PDF page image.

    For non-surgical chunks, returns page_number as-is.
    """
    if not chunk_metadata:
        return page_number
    try:
        if isinstance(chunk_metadata, dict):
            meta = chunk_metadata
        elif isinstance(chunk_metadata, str):
            meta = json.loads(chunk_metadata)
        else:
            meta = json.loads(str(chunk_metadata))
        mappings = meta.get('surgical', {}).get('page_mappings', [])
        for pm in mappings:
            if pm.get('target') == page_number:
                return pm.get('original_pdf_page', pm.get('source', page_number))
    except Exception:
        pass
    return page_number


# ---------------------------------------------------------------------------
# Batch generation (shared by both CCS and Refinery)
# ---------------------------------------------------------------------------

def process_batch_generation(session, targets, stage_root):
    """Run Cortex for a list of items with hierarchical storage."""
    if not targets:
        st.info("No targets to process.")
        return

    progress = st.progress(0, "Starting batch generation...")
    ctx = st.session_state.auth_context

    for idx, t_item in enumerate(targets):
        progress.progress((idx + 1) / len(targets), f"Processing {t_item['id']}...")
        try:
            t_file = t_item['file']
            t_tbl = t_item['table']
            t_tbl_base = t_tbl.split('.')[-1]

            try:
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

            cache_key = f"{QA_PDF_CACHE_PREFIX}{t_file}"
            if cache_key not in st.session_state:
                try:
                    stream = session.file.get_stream(f"{stage_root}/{t_file}")
                    st.session_state[cache_key] = stream.read()
                except Exception as e:
                    t_item['status'] = f"Error: PDF Load {e}"
                    continue

            t_pdf_bytes = st.session_state[cache_key]

            if convert_from_bytes:
                t_images = convert_from_bytes(
                    t_pdf_bytes,
                    first_page=t_item['page_number'],
                    last_page=t_item['page_number']
                )
                if t_images:
                    with tempfile.TemporaryDirectory() as td:
                        img_name = f"p{t_item['page_number']}"
                        img_path_local = save_optimized_image(
                            t_images[0], td, img_name, sub_folder=t_file
                        )
                        if not img_path_local:
                            t_item['status'] = 'Error: Image Save Failed'
                            continue

                        safe_sub = PDFUtils.get_safe_folder(t_file)
                        full_stage_path = f"{stage_root}/{TEMP_IMAGE_PREFIX}/{safe_sub}"
                        session.file.put(
                            img_path_local, full_stage_path,
                            auto_compress=False, overwrite=True
                        )
                        rel_img_path = (
                            f"{TEMP_IMAGE_PREFIX}/{safe_sub}/"
                            f"{os.path.basename(img_path_local)}"
                        )

                        instruction = t_item.get('context_instruction', '')
                        prompt = prompts.get_silver_bullet_prompt(t_chunk_txt, instruction)

                        res, _, _, _ = run_cortex(
                            session, prompt, stage_root, rel_img_path,
                            model=CORTEX_MODEL
                        )

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


# ---------------------------------------------------------------------------
# Single item inspector (shared by both CCS and Refinery)
# ---------------------------------------------------------------------------

def render_single_item_inspector(session, item, db, sch, stage_root):
    """Split screen inspector: Visual vs (Read-only Content + Editable Draft)."""
    table_base = item.get('table', '').split('.')[-1]
    work_table = f'"{db}"."{sch}"."{table_base}"'

    try:
        sql = f"SELECT CHUNK, CHUNK_METADATA FROM {work_table} WHERE CHUNK_ID = ?"
        data = session.sql(sql, params=[item['id']]).collect()
        original_chunk = data[0]['CHUNK'] if data else "[Error: Chunk not found]"
        raw_metadata = data[0]['CHUNK_METADATA'] if data and len(data[0]) > 1 else None
    except Exception as e:
        original_chunk = f"[Error: {e}]"
        raw_metadata = None

    pdf_page = _get_original_pdf_page(raw_metadata, item['page_number'])

    _mode_options = ["Rendered", "Raw"]
    st.session_state.qa_display_mode = st.radio(
        "Display Mode", _mode_options,
        index=_mode_options.index(st.session_state.get("qa_display_mode", "Rendered")),
        horizontal=True,
        help="Rendered: high-fidelity Markdown preview. Raw: raw editable text. "
             "Original chunk is always read-only.",
    )

    col_vis, col_edit = st.columns(2)
    with col_vis:
        pdf_name = get_pdf_name(item['file'])
        st.caption(f"📄 {pdf_name} (Pg {pdf_page})")
        if convert_from_bytes and Image:
            try:
                cache_key = f"{QA_PDF_CACHE_PREFIX}{item['file']}"
                if cache_key not in st.session_state:
                    stream = session.file.get_stream(f"{stage_root}/{item['file']}")
                    st.session_state[cache_key] = stream.read()

                images = convert_from_bytes(
                    st.session_state[cache_key],
                    first_page=pdf_page, last_page=pdf_page
                )
                if images:
                    st.image(images[0], use_container_width=True)
            except Exception as e:
                st.error(f"Visual Error: {e}")
        else:
            st.warning("Install pdf2image for visuals.")

    with col_edit:
        st.caption(f"📝 Draft Editor (Status: {item['status']})")
        new_inst = st.text_area(
            "Instruction", value=item.get("context_instruction", ""),
            key=f"inst_{item['id']}"
        )
        if new_inst != item.get("context_instruction", ""):
            item["context_instruction"] = new_inst

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
                text_content = (
                    text_content.replace('\\n', '\n')
                    .replace('\\t', '\t')
                    .replace('\\"', '"')
                )
            if text_content.startswith('"') and text_content.endswith('"'):
                text_content = text_content[1:-1]
            return text_content

        def render_hybrid_markdown(text_content):
            """Two-layer pipeline: Markdown→HTML → Styled Container."""
            if MISTLETOE_AVAILABLE:
                html_content = mistletoe.markdown(text_content)
            else:
                html_content = f"<pre>{text_content}</pre>"

            wrapper_html = "".join([
                '<div class="rag-doc-panel" style="white-space: pre-wrap; '
                'background-color: rgba(128, 128, 128, 0.05); '
                'border: 1px solid rgba(128, 128, 128, 0.2); '
                'padding: 15px; border-radius: 6px; margin-bottom: 10px;">',
                html_content,
                '</div>'
            ])
            safe_markdown(
                wrapper_html, unsafe_allow_html=True, label="qa_chunk_rendered"
            )

        original_chunk_clean = unescape_chunk(original_chunk)

        st.markdown("##### 📄 Original Chunk")
        if mode == "Rendered":
            render_hybrid_markdown(original_chunk_clean)
        else:
            safe_code(original_chunk_clean, language=None, label="qa_chunk_raw")

        st.markdown("##### 📄 Draft Preview")
        if mode == "Rendered":
            render_hybrid_markdown(
                unescape_chunk(draft_val) if draft_val else "*No draft generated yet.*"
            )
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


# ---------------------------------------------------------------------------
# Source selection helpers (internal)
# ---------------------------------------------------------------------------

def _get_completed_jobs(jobs_param):
    """Extract completed jobs from explicit param or session state job_queue."""
    terminal = {"Completed", "Completed with Warnings", "Failed", "Cancelled"}
    if jobs_param is not None:
        return [j for j in jobs_param if j.get("status", "Pending") in terminal]
    return [
        j for j in st.session_state.get('job_queue', [])
        if j.get("status", "Pending") in terminal
    ]


def _render_source_from_jobs(completed_jobs, key_prefix="qa"):
    """Render table/file selection from completed jobs.

    Returns (current_search_table, current_search_file).
    """
    current_search_file = None
    current_search_table = None

    if completed_jobs:
        distinct_tables = sorted(set(j['table'] for j in completed_jobs))
        sel_table = st.selectbox(
            "Select Table", distinct_tables, key=f"{key_prefix}_tbl_sel"
        )
        if sel_table:
            current_search_table = sel_table
            table_files = sorted(set(
                j['file'] for j in completed_jobs if j['table'] == sel_table
            ))
            if len(table_files) > 1:
                current_search_file = st.multiselect(
                    "Filter by PDF Name",
                    options=table_files, default=[],
                    key=f"{key_prefix}_active_files",
                    help="Select one or more PDFs to filter. Leave empty to search all.",
                )
            elif table_files:
                current_search_file = table_files[0]
    else:
        st.info("No completed jobs yet. Run a batch first.")

    return current_search_table, current_search_file


def _render_source_manual(session, db, schema, key_prefix="qa"):
    """Render manual table/file search UI.

    Returns (current_search_table, current_search_file).
    """
    c1, c2 = st.columns(2)
    current_search_table = c1.text_input(
        "Table Name", "SUS_CHUNKS", key=f"{key_prefix}_manual_tbl"
    )

    available_files = []
    if current_search_table:
        try:
            tbl_base = current_search_table.split('.')[-1]
            full_tbl = f'"{db}"."{schema}"."{tbl_base}"'
            file_rows = session.sql(
                f"SELECT DISTINCT RELATIVE_PATH FROM {full_tbl} "
                f"ORDER BY RELATIVE_PATH"
            ).collect()
            available_files = [r[0] for r in file_rows if r[0]]
        except Exception:
            pass

    selected_files = c2.multiselect(
        "Filter by PDF Name",
        options=available_files, default=[],
        key=f"{key_prefix}_manual_files",
        help="Select one or more PDFs to filter. Leave empty to search all.",
    )
    return current_search_table, selected_files


# ---------------------------------------------------------------------------
# Search + workbench (internal, shared by both modes)
# ---------------------------------------------------------------------------

def _render_search_and_workbench(session, db, schema, stage_path,
                                  current_search_table, current_search_file,
                                  key_prefix="qa"):
    """Render the search and chunk selector, plus full workbench.

    Called after source selection resolves current_search_table and
    current_search_file. All widget keys use key_prefix to avoid
    collisions when multiple QA Studios exist in the same session.
    """
    # Search Logic
    if current_search_table:
        pg_input = st.text_input(
            "Page Filter (e.g., '1-5, 8')", key=f"{key_prefix}_pg_text"
        )

        if st.button("Search", key=f"{key_prefix}_search"):
            tbl_base = current_search_table.split('.')[-1]
            full_tbl = f'"{db}"."{schema}"."{tbl_base}"'
            where = []

            if pg_input.strip():
                try:
                    pages_to_query = set()
                    for part in pg_input.split(','):
                        part = part.strip()
                        if '-' in part:
                            s, e = part.split('-')
                            pages_to_query.update(range(int(s), int(e) + 1))
                        elif part.isdigit():
                            pages_to_query.add(int(part))
                    if pages_to_query:
                        pg_list = ", ".join(
                            str(p) for p in sorted(pages_to_query)
                        )
                        where.append(f"PAGE_NUMBER IN ({pg_list})")
                except Exception:
                    st.toast("⚠️ Invalid page format.", icon="⚠️")

            if current_search_file:
                if isinstance(current_search_file, list):
                    safe_files = [
                        clean_text_for_sql(f)
                        for f in current_search_file if f
                    ]
                    if safe_files:
                        in_list = ", ".join(f"'{sf}'" for sf in safe_files)
                        where.append(f"RELATIVE_PATH IN ({in_list})")
                else:
                    safe_f = clean_text_for_sql(current_search_file)
                    where.append(f"RELATIVE_PATH = '{safe_f}'")

            where_clause = f"WHERE {' AND '.join(where)}" if where else ""
            sql = (
                f"SELECT CHUNK_ID, PAGE_NUMBER, RELATIVE_PATH, "
                f"SUBSTR(CHUNK, 1, {CHUNK_PREVIEW_LENGTH}) as PREVIEW "
                f"FROM {full_tbl} {where_clause} LIMIT 100"
            )
            try:
                res_df = session.sql(sql).to_pandas()
                st.session_state[f"{key_prefix}_results"] = res_df.sort_values(
                    by="PAGE_NUMBER"
                )
            except Exception as e:
                st.error(f"Search failed: {e}")

        results_key = f"{key_prefix}_results"
        if (results_key in st.session_state
                and not st.session_state[results_key].empty):
            qa_df = st.session_state[results_key]

            def fmt_chunk_opt(cid):
                try:
                    row = qa_df[qa_df['CHUNK_ID'] == cid].iloc[0]
                    return (
                        f"{get_pdf_name(row['RELATIVE_PATH'])} — "
                        f"Pg {row['PAGE_NUMBER']}"
                    )
                except Exception:
                    return cid

            sel_chunk = st.selectbox(
                "Found", qa_df["CHUNK_ID"].tolist(),
                format_func=fmt_chunk_opt, key=f"{key_prefix}_chunk_sel"
            )
            if st.button("➕ Add to Workbench", key=f"{key_prefix}_add_btn"):
                existing_ids = [
                    x['id'] for x in st.session_state.admin_queue
                ]
                if sel_chunk in existing_ids:
                    st.warning(
                        f"Chunk `{sel_chunk}` is already in the workbench."
                    )
                else:
                    matches = st.session_state[results_key][
                        st.session_state[results_key].CHUNK_ID == sel_chunk
                    ]
                    if not matches.empty:
                        row = matches.iloc[0]
                        st.session_state.admin_queue.append({
                            "id": sel_chunk, "status": "Pending",
                            "file": row['RELATIVE_PATH'],
                            "table": current_search_table,
                            "page_number": int(row['PAGE_NUMBER']),
                            "selected": False, "draft_text": "",
                            "context_instruction": "",
                            "preview": row['PREVIEW']
                        })
                        st.success("Added")
                        st.rerun()

    # Workbench Logic
    if st.session_state.admin_queue:
        st.divider()
        st.markdown(
            f"### 🛠️ Workbench ({len(st.session_state.admin_queue)})"
        )

        df_queue = pd.DataFrame(st.session_state.admin_queue)
        if "table" not in df_queue.columns:
            df_queue["table"] = "Unknown"

        df_display = df_queue.rename(columns={
            "page_number": "Page Number",
            "context_instruction": "Instruction",
            "preview": "Original",
            "draft_text": "Draft",
            "table": "Target Table"
        })

        if 'file' in df_display.columns:
            df_display['file'] = df_display['file'].apply(get_pdf_name)

        edited_df = safe_data_editor(
            df_display[[
                "selected", "id", "Target Table", "Page Number", "file",
                "Instruction", "Original", "Draft", "status"
            ]],
            label=f"{key_prefix}_workbench",
            column_config={
                "selected": st.column_config.CheckboxColumn("Sel", width="small"),
                "id": st.column_config.TextColumn("ID", disabled=True),
                "Target Table": st.column_config.TextColumn(
                    "Target Table", disabled=True, width="medium"
                ),
                "Page Number": st.column_config.NumberColumn(
                    "Pg", disabled=True, width="small"
                ),
                "file": st.column_config.TextColumn("PDF Name", disabled=True),
                "Instruction": st.column_config.TextColumn(
                    "Instruction", width="medium"
                ),
                "Original": st.column_config.TextColumn(
                    "Original", disabled=True, width="large"
                ),
                "Draft": st.column_config.TextColumn("Draft", width="large"),
                "status": st.column_config.TextColumn("Status", disabled=True)
            },
            use_container_width=True, hide_index=True,
            key=f"{key_prefix}_editor"
        )

        # Sync changes back to session state
        for index, row in edited_df.iterrows():
            for item in st.session_state.admin_queue:
                if item["id"] == row["id"]:
                    item["selected"] = row["selected"]
                    item["context_instruction"] = row["Instruction"]
                    item["draft_text"] = row["Draft"]

        # Batch Actions
        b1, b2, b3, b4 = st.columns(4)
        with b1:
            if st.button("✨ Gen Drafts (Selected)", key=f"{key_prefix}_gen"):
                targets = [
                    i for i in st.session_state.admin_queue if i.get('selected')
                ]
                process_batch_generation(session, targets, stage_path)
        with b2:
            if st.button("💾 Commit (Selected)", key=f"{key_prefix}_commit"):
                targets = [
                    i for i in st.session_state.admin_queue if i.get('selected')
                ]
                count = 0
                for item in targets:
                    if item.get('draft_text'):
                        tbl = item.get('table') or current_search_table
                        tbl_base = tbl.split('.')[-1]
                        full_tbl = f'"{db}"."{schema}"."{tbl_base}"'
                        sql = f"UPDATE {full_tbl} SET CHUNK = ? WHERE CHUNK_ID = ?"
                        try:
                            session.sql(
                                sql,
                                params=[item['draft_text'], item['id']]
                            ).collect()
                            item['status'] = 'Committed'
                            count += 1
                        except Exception as e:
                            log_action(
                                "BATCH_COMMIT_ERROR", {"error": str(e)}
                            )
                if count:
                    st.success(f"Committed {count} items.")
                    st.rerun()
        with b3:
            if st.button("🗑️ Remove (Selected)", key=f"{key_prefix}_remove"):
                st.session_state.admin_queue = [
                    i for i in st.session_state.admin_queue
                    if not i.get('selected')
                ]
                st.rerun()
        with b4:
            if st.button("❌ Delete from Table (Selected)", key=f"{key_prefix}_delete"):
                selected = [
                    i for i in st.session_state.admin_queue if i.get('selected')
                ]
                if not selected:
                    st.toast("No items selected.", icon="⚠️")
                else:
                    st.session_state[f"{key_prefix}_delete_confirm"] = True
                    st.session_state[f"{key_prefix}_delete_targets"] = selected
                    st.rerun()

        confirm_key = f"{key_prefix}_delete_confirm"
        targets_key = f"{key_prefix}_delete_targets"
        if st.session_state.get(confirm_key, False):
            targets = st.session_state.get(targets_key, [])
            st.warning(
                f"⚠️ **Permanently delete {len(targets)} chunk(s) "
                f"from the Snowflake table?** This action cannot be undone."
            )
            c_confirm, c_cancel = st.columns(2)
            with c_confirm:
                if st.button(
                    "✅ Confirm Delete", type="primary",
                    key=f"{key_prefix}_confirm_del"
                ):
                    deleted = 0
                    for item in targets:
                        tbl = item.get('table') or current_search_table
                        tbl_base = tbl.split('.')[-1]
                        full_tbl = f'"{db}"."{schema}"."{tbl_base}"'
                        sql = f"DELETE FROM {full_tbl} WHERE CHUNK_ID = ?"
                        try:
                            session.sql(
                                sql, params=[item['id']]
                            ).collect()
                            deleted += 1
                            log_action("CHUNK_DELETED", {
                                "chunk_id": item['id'],
                                "table": full_tbl
                            })
                        except Exception as e:
                            log_action("CHUNK_DELETE_ERROR", {
                                "chunk_id": item['id'],
                                "error": str(e)
                            })
                            st.error(f"Delete failed for {item['id']}: {e}")

                    deleted_ids = {t['id'] for t in targets}
                    st.session_state.admin_queue = [
                        i for i in st.session_state.admin_queue
                        if i['id'] not in deleted_ids
                    ]
                    st.session_state[confirm_key] = False
                    st.session_state[targets_key] = []
                    st.toast(f"Deleted {deleted} chunk(s) from table.", icon="✅")
                    st.rerun()
            with c_cancel:
                if st.button("Cancel", key=f"{key_prefix}_cancel_del"):
                    st.session_state[confirm_key] = False
                    st.session_state[targets_key] = []
                    st.rerun()

        # Item Inspector
        st.divider()
        sel_idx = st.selectbox(
            "Inspect Item",
            range(len(st.session_state.admin_queue)),
            format_func=lambda x: (
                f"{get_pdf_name(st.session_state.admin_queue[x]['file'])} — "
                f"Pg {st.session_state.admin_queue[x]['page_number']} "
                f"({st.session_state.admin_queue[x]['id']})"
            ),
            key=f"{key_prefix}_inspect_sel"
        )
        item = st.session_state.admin_queue[sel_idx]
        render_single_item_inspector(session, item, db, schema, stage_path)


# ---------------------------------------------------------------------------
# Main QA Studio render (public API)
# ---------------------------------------------------------------------------

def render_qa_studio(session, db, schema, stage_path, jobs=None,
                     mode="jobs"):
    """Render the full QA Studio.

    Args:
        session: Snowpark session.
        db: Database name.
        schema: Schema name.
        stage_path: Stage path (e.g. @db.schema.stage).
        jobs: Optional list of jobs for "From Completed Jobs" source.
              When None, falls back to st.session_state['job_queue'].
        mode: Rendering mode.
              "jobs"       — No Search Scope UI. Uses completed jobs
                             (CCS wizard Step 4 behavior).
              "standalone" — Shows Search Scope radio. Defaults to
                             "Manual Search in Current Schema".
                             Falls back to "From Completed Jobs" if
                             no tables exist in the current schema.
    """
    st.markdown(textwrap.dedent("""
        <style>
        .rag-doc-panel p {
            white-space: pre-wrap;
        }
        </style>
    """), unsafe_allow_html=True)

    if "admin_queue" not in st.session_state:
        st.session_state.admin_queue = []
    if "qa_display_mode" not in st.session_state:
        st.session_state.qa_display_mode = "Rendered"

    # Determine key prefix to avoid widget key collisions between
    # the standalone page and the CCS wizard if both are in session.
    key_prefix = "qa_standalone" if mode == "standalone" else "qa"

    completed_jobs = _get_completed_jobs(jobs)

    if mode == "standalone":
        # Standalone page: Manual Search in Current Schema (no Search Scope radio)
        current_search_table, current_search_file = _render_source_manual(
            session, db, schema, key_prefix=key_prefix
        )
    else:
        # Jobs mode (CCS wizard): no Search Scope UI, always From Completed Jobs
        current_search_table, current_search_file = _render_source_from_jobs(
            completed_jobs, key_prefix=key_prefix
        )

    # Shared search + workbench
    _render_search_and_workbench(
        session, db, schema, stage_path,
        current_search_table, current_search_file,
        key_prefix=key_prefix,
    )
