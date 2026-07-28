# views/refinery/tab_qa.py
# QA Tab - Quality Assurance and Refinement Studio for the Doc Refinery package
# Uses shared functions from views/qastudio.py for the inspector and batch generation.
import streamlit as st
import pandas as pd
import json
import textwrap
from logger_config import log_action
from utils.core_utils import (
    QualityInspector, clean_text_for_sql
)
from utils.constants import CHUNK_PREVIEW_LENGTH
from utils.display_safety import safe_dataframe, safe_data_editor

# Import shared QA functions from the primary QA Studio module
from views.qastudio import (
    get_pdf_name as _get_pdf_name,
    _get_original_pdf_page,
    process_batch_generation,
    render_single_item_inspector,
)


def render_quality_inspector(session):
    """Context Locking"""
    ctx = st.session_state.auth_context
    db, schema = ctx["db"], ctx["schema"]

    st.markdown("#### 🕵️ Quality Inspector")
    inspect_table_input = st.text_input(
        "Target Table (Current Schema)", "SUS_CHUNKS", key="insp_tbl"
    )

    if st.button("🔍 Run Quality Inspector", key="insp_run"):
        tbl_base = inspect_table_input.split('.')[-1]
        full_table_path = f"{db}.{schema}.{tbl_base}"
        with st.spinner("Analyzing..."):
            try:
                df = session.sql(
                    f"SELECT CHUNK_ID, PAGE_NUMBER, RELATIVE_PATH, CHUNK "
                    f"FROM {full_table_path} LIMIT 100"
                ).to_pandas()
                df["STATUS"] = df["CHUNK"].apply(QualityInspector.inspect)
                defects = df[df["STATUS"] != "OK"]

                if not defects.empty:
                    st.warning(f"Found {len(defects)} issues.")
                    safe_dataframe(
                        defects[["PAGE_NUMBER", "STATUS", "CHUNK_ID"]],
                        use_container_width=True, label="qa_defects"
                    )
                else:
                    st.success("No obvious defects in sample.")
            except Exception as e:
                st.error(f"Inspector failed: {e}")


def render_qa_tab(session):
    st.subheader("3. QA & Refinement Studio")

    st.markdown(textwrap.dedent("""
        <style>
        .rag-doc-panel p {
            white-space: pre-wrap;
        }
        </style>
    """), unsafe_allow_html=True)

    ctx = st.session_state.auth_context
    db, schema, stage = ctx["db"], ctx["schema"], ctx["stage"]
    stage_root = f"@{db}.{schema}.{stage}"

    if "admin_queue" not in st.session_state:
        st.session_state.admin_queue = []
    if "qa_display_mode" not in st.session_state:
        st.session_state.qa_display_mode = "Rendered"

    # QA Source Selection
    qa_source = st.radio(
        "Search Scope",
        ["Active Job Queue", "Manual Search in Current Schema"],
        horizontal=True, key="qa_source"
    )

    current_search_file = None
    current_search_table = None

    if qa_source == "Active Job Queue":
        jobs = st.session_state.get('job_queue', [])
        if jobs:
            distinct_tables = sorted(set(j['table'] for j in jobs))
            sel_table = st.selectbox(
                "Select Table", distinct_tables, key="qa_tbl_sel"
            )
            if sel_table:
                current_search_table = sel_table
                table_files = sorted(set(
                    j['file'] for j in jobs if j['table'] == sel_table
                ))
                if len(table_files) > 1:
                    current_search_file = st.multiselect(
                        "Filter by PDF Name",
                        options=table_files, default=[],
                        key="qa_active_files",
                        help="Select one or more PDFs to filter. Leave empty to search all.",
                    )
                elif table_files:
                    current_search_file = table_files[0]
    else:
        c1, c2 = st.columns(2)
        current_search_table = c1.text_input(
            "Table Name", "SUS_CHUNKS", key="qa_manual_tbl"
        )

        _available_files = []
        if current_search_table:
            try:
                _tbl_base = current_search_table.split('.')[-1]
                _full_tbl = f'"{db}"."{schema}"."{_tbl_base}"'
                _file_rows = session.sql(
                    f"SELECT DISTINCT RELATIVE_PATH FROM {_full_tbl} "
                    f"ORDER BY RELATIVE_PATH"
                ).collect()
                _available_files = [r[0] for r in _file_rows if r[0]]
            except Exception:
                pass

        selected_files = c2.multiselect(
            "Filter by PDF Name",
            options=_available_files, default=[],
            key="qa_manual_files",
            help="Select one or more PDFs to filter. Leave empty to search all.",
        )
        current_search_file = selected_files

    # Search Logic
    if current_search_table:
        with st.expander("🔍 Search Chunks", expanded=False):
            pg_input = st.text_input(
                "Page Filter (e.g., '1-5, 8')", key="qa_pg_text"
            )

            if st.button("Search", key="qa_search"):
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
                        st.toast(
                            "⚠️ Invalid page format. Ignoring page filter.",
                            icon="⚠️"
                        )

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

                where_clause = (
                    f"WHERE {' AND '.join(where)}" if where else ""
                )
                sql = (
                    f"SELECT CHUNK_ID, PAGE_NUMBER, RELATIVE_PATH, "
                    f"SUBSTR(CHUNK, 1, {CHUNK_PREVIEW_LENGTH}) as PREVIEW "
                    f"FROM {full_tbl} {where_clause} LIMIT 100"
                )
                try:
                    res_df = session.sql(sql).to_pandas()
                    st.session_state.qa_results = res_df.sort_values(
                        by="PAGE_NUMBER"
                    )
                except Exception as e:
                    st.error(f"Search failed: {e}")

            if ("qa_results" in st.session_state
                    and not st.session_state.qa_results.empty):
                qa_df = st.session_state.qa_results

                def fmt_chunk_opt(cid):
                    try:
                        row = qa_df[qa_df['CHUNK_ID'] == cid].iloc[0]
                        return (
                            f"{_get_pdf_name(row['RELATIVE_PATH'])} — "
                            f"Pg {row['PAGE_NUMBER']}"
                        )
                    except Exception:
                        return cid

                sel_chunk = st.selectbox(
                    "Found", qa_df["CHUNK_ID"].tolist(),
                    format_func=fmt_chunk_opt, key="qa_chunk_sel"
                )
                if st.button("➕ Add to Workbench"):
                    existing_ids = [
                        x['id'] for x in st.session_state.admin_queue
                    ]
                    if sel_chunk in existing_ids:
                        st.warning(
                            f"Chunk `{sel_chunk}` is already in the workbench."
                        )
                    else:
                        matches = st.session_state.qa_results[
                            st.session_state.qa_results.CHUNK_ID == sel_chunk
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
            df_display['file'] = df_display['file'].apply(_get_pdf_name)

        edited_df = safe_data_editor(
            df_display[[
                "selected", "id", "Target Table", "Page Number", "file",
                "Instruction", "Original", "Draft", "status"
            ]],
            label="qa_workbench",
            column_config={
                "selected": st.column_config.CheckboxColumn(
                    "Sel", width="small"
                ),
                "id": st.column_config.TextColumn("ID", disabled=True),
                "Target Table": st.column_config.TextColumn(
                    "Target Table", disabled=True, width="medium"
                ),
                "Page Number": st.column_config.NumberColumn(
                    "Pg", disabled=True, width="small"
                ),
                "file": st.column_config.TextColumn(
                    "PDF Name", disabled=True
                ),
                "Instruction": st.column_config.TextColumn(
                    "Instruction", width="medium"
                ),
                "Original": st.column_config.TextColumn(
                    "Original", disabled=True, width="large"
                ),
                "Draft": st.column_config.TextColumn("Draft", width="large"),
                "status": st.column_config.TextColumn(
                    "Status", disabled=True
                )
            },
            use_container_width=True, hide_index=True, key="qa_editor_v4"
        )

        for index, row in edited_df.iterrows():
            for item in st.session_state.admin_queue:
                if item["id"] == row["id"]:
                    item["selected"] = row["selected"]
                    item["context_instruction"] = row["Instruction"]
                    item["draft_text"] = row["Draft"]

        b1, b2, b3, b4 = st.columns(4)
        with b1:
            if st.button("✨ Gen Drafts (Selected)"):
                targets = [
                    i for i in st.session_state.admin_queue
                    if i.get('selected')
                ]
                process_batch_generation(session, targets, stage_root)
        with b2:
            if st.button("💾 Commit (Selected)"):
                targets = [
                    i for i in st.session_state.admin_queue
                    if i.get('selected')
                ]
                count = 0
                for item in targets:
                    if item.get('draft_text'):
                        tbl = item.get('table') or current_search_table
                        tbl_base = tbl.split('.')[-1]
                        full_tbl = f'"{db}"."{schema}"."{tbl_base}"'
                        sql = (
                            f"UPDATE {full_tbl} SET CHUNK = ? "
                            f"WHERE CHUNK_ID = ?"
                        )
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
                st.success(f"Committed {count} items.")
                st.rerun()
        with b3:
            if st.button("🗑️ Remove (Selected)"):
                st.session_state.admin_queue = [
                    i for i in st.session_state.admin_queue
                    if not i.get('selected')
                ]
                st.rerun()
        with b4:
            if st.button("❌ Delete from Table (Selected)"):
                selected = [
                    i for i in st.session_state.admin_queue
                    if i.get('selected')
                ]
                if not selected:
                    st.toast("No items selected.", icon="⚠️")
                else:
                    st.session_state.qa_delete_confirm = True
                    st.session_state.qa_delete_targets = selected
                    st.rerun()

        if st.session_state.get('qa_delete_confirm', False):
            targets = st.session_state.get('qa_delete_targets', [])
            st.warning(
                f"⚠️ **Permanently delete {len(targets)} chunk(s) "
                f"from the Snowflake table?** "
                f"This action cannot be undone."
            )
            c_confirm, c_cancel = st.columns(2)
            with c_confirm:
                if st.button(
                    "✅ Confirm Delete", type="primary"
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
                            st.error(
                                f"Delete failed for {item['id']}: {e}"
                            )

                    deleted_ids = {t['id'] for t in targets}
                    st.session_state.admin_queue = [
                        i for i in st.session_state.admin_queue
                        if i['id'] not in deleted_ids
                    ]
                    st.session_state.qa_delete_confirm = False
                    st.session_state.qa_delete_targets = []
                    st.toast(
                        f"Deleted {deleted} chunk(s) from table.",
                        icon="✅"
                    )
                    st.rerun()
            with c_cancel:
                if st.button("Cancel"):
                    st.session_state.qa_delete_confirm = False
                    st.session_state.qa_delete_targets = []
                    st.rerun()

        st.divider()
        sel_idx = st.selectbox(
            "Inspect Item",
            range(len(st.session_state.admin_queue)),
            format_func=lambda x: (
                f"{_get_pdf_name(st.session_state.admin_queue[x]['file'])}"
                f" — Pg {st.session_state.admin_queue[x]['page_number']}"
                f" ({st.session_state.admin_queue[x]['id']})"
            ),
            key="qa_inspect_sel"
        )
        item = st.session_state.admin_queue[sel_idx]
        render_single_item_inspector(session, item, db, schema, stage_root)

    st.divider()
    # render_quality_inspector(session)  # Disabled per requirement
