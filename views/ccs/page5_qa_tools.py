# views/ccs/page5_qa_tools.py
# Page 5: QA Studio & Tools — chunk inspection, draft editing, and maintenance tools.
# Combines QA Studio and Tools from Doc Refinery into a single wizard step.

import streamlit as st
from views.ccs.common import render_header, nav_buttons, ctx
from utils.constants import DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE


def render(session):
    render_header(5)

    c = ctx()
    db = c.get("db", DEFAULT_DB)
    schema = c.get("schema", DEFAULT_SCHEMA)
    stage = c.get("stage", DEFAULT_STAGE)
    stage_path = f"@{db}.{schema}.{stage}"

    jobs = st.session_state.get("cssw_jobs", [])
    terminal = {"Completed", "Completed with Warnings", "Failed", "Cancelled"}
    completed_jobs = [j for j in jobs if j.get("status", "Pending") in terminal]

    qa_tab, tools_tab = st.tabs(["🕵️ QA Studio", "🛠️ Tools"])

    # --- QA Studio Tab ---
    with qa_tab:
        _render_qa_studio(session, db, schema, stage_path, completed_jobs)

    # --- Tools Tab ---
    with tools_tab:
        _render_tools(session, db, schema)

    nav_buttons(can_next=False, show_back=True)


def _render_qa_studio(session, db, schema, stage_path, completed_jobs):
    """Render QA Studio for chunk inspection and editing."""
    from views.ccs.qa import (
        render_single_item_inspector, _get_pdf_name,
    )
    from utils.core_utils import clean_text_for_sql

    st.markdown("#### 🕵️ QA Studio — Chunk Inspection & Editing")
    st.caption("Inspect, edit, and repair chunks from your completed ingestion jobs.")

    if "admin_queue" not in st.session_state:
        st.session_state.admin_queue = []
    if "qa_display_mode" not in st.session_state:
        st.session_state.qa_display_mode = "Rendered"

    # Source selection
    qa_source = st.radio(
        "Search Scope",
        ["From Completed Jobs", "Manual Search in Current Schema"],
        horizontal=True, key="cssw_qa_source"
    )

    current_search_file = None
    current_search_table = None

    if qa_source == "From Completed Jobs":
        if completed_jobs:
            distinct_tables = sorted(set(j["table"] for j in completed_jobs))
            sel_table = st.selectbox("Select Table", distinct_tables, key="cssw_qa_tbl_sel")
            if sel_table:
                current_search_table = sel_table
                table_files = sorted(set(j["file"] for j in completed_jobs if j["table"] == sel_table))
                if len(table_files) > 1:
                    current_search_file = st.multiselect(
                        "Filter by PDF Name", options=table_files, default=[],
                        key="cssw_qa_active_files"
                    )
                elif table_files:
                    current_search_file = table_files[0]
        else:
            st.info("No completed jobs yet. Run a batch first.")
    else:
        c1, c2 = st.columns(2)
        current_search_table = c1.text_input("Table Name", "SUS_CHUNKS", key="cssw_qa_manual_tbl")
        _available_files = []
        if current_search_table:
            try:
                _tbl_base = current_search_table.split(".")[-1]
                _full_tbl = f'"{db}"."{schema}"."{_tbl_base}"'
                _file_rows = session.sql(
                    f"SELECT DISTINCT RELATIVE_PATH FROM {_full_tbl} ORDER BY RELATIVE_PATH"
                ).collect()
                _available_files = [r[0] for r in _file_rows if r[0]]
            except Exception:
                pass
        selected_files = c2.multiselect(
            "Filter by PDF Name", options=_available_files, default=[],
            key="cssw_qa_manual_files"
        )
        current_search_file = selected_files

    # Search
    if current_search_table:
        from utils.constants import CHUNK_PREVIEW_LENGTH
        with st.expander("🔍 Search Chunks", expanded=False):
            pg_input = st.text_input("Page Filter (e.g., '1-5, 8')", key="cssw_qa_pg_text")
            if st.button("Search", key="cssw_qa_search"):
                tbl_base = current_search_table.split(".")[-1]
                full_tbl = f'"{db}"."{schema}"."{tbl_base}"'
                where = []
                if pg_input.strip():
                    try:
                        pages_to_query = set()
                        for part in pg_input.split(","):
                            part = part.strip()
                            if "-" in part:
                                s, e = part.split("-")
                                pages_to_query.update(range(int(s), int(e) + 1))
                            elif part.isdigit():
                                pages_to_query.add(int(part))
                        if pages_to_query:
                            pg_list = ", ".join(str(p) for p in sorted(pages_to_query))
                            where.append(f"PAGE_NUMBER IN ({pg_list})")
                    except Exception:
                        st.toast("⚠️ Invalid page format.", icon="⚠️")
                if current_search_file:
                    if isinstance(current_search_file, list):
                        safe_files = [clean_text_for_sql(f) for f in current_search_file if f]
                        if safe_files:
                            in_list = ", ".join(f"'{sf}'" for sf in safe_files)
                            where.append(f"RELATIVE_PATH IN ({in_list})")
                    else:
                        safe_f = clean_text_for_sql(current_search_file)
                        where.append(f"RELATIVE_PATH = '{safe_f}'")
                where_clause = f"WHERE {' AND '.join(where)}" if where else ""
                sql = f"SELECT CHUNK_ID, PAGE_NUMBER, RELATIVE_PATH, SUBSTR(CHUNK, 1, {CHUNK_PREVIEW_LENGTH}) as PREVIEW FROM {full_tbl} {where_clause} LIMIT 100"
                try:
                    res_df = session.sql(sql).to_pandas()
                    st.session_state.qa_results = res_df.sort_values(by="PAGE_NUMBER")
                except Exception as e:
                    st.error(f"Search failed: {e}")

            if "qa_results" in st.session_state and not st.session_state.qa_results.empty:
                qa_df = st.session_state.qa_results
                def fmt_chunk_opt(cid):
                    try:
                        row = qa_df[qa_df["CHUNK_ID"] == cid].iloc[0]
                        return f"{_get_pdf_name(row['RELATIVE_PATH'])} — Pg {row['PAGE_NUMBER']}"
                    except Exception:
                        return cid
                sel_chunk = st.selectbox("Found", qa_df["CHUNK_ID"].tolist(), format_func=fmt_chunk_opt, key="cssw_qa_chunk_sel")
                if st.button("➕ Add to Workbench", key="cssw_qa_add"):
                    existing_ids = [x["id"] for x in st.session_state.admin_queue]
                    if sel_chunk in existing_ids:
                        st.warning(f"Chunk `{sel_chunk}` is already in the workbench.")
                    else:
                        matches = st.session_state.qa_results[st.session_state.qa_results.CHUNK_ID == sel_chunk]
                        if not matches.empty:
                            row = matches.iloc[0]
                            st.session_state.admin_queue.append({
                                "id": sel_chunk, "status": "Pending",
                                "file": row["RELATIVE_PATH"],
                                "table": current_search_table,
                                "page_number": int(row["PAGE_NUMBER"]),
                                "selected": False, "draft_text": "", "context_instruction": "",
                                "preview": row["PREVIEW"]
                            })
                            st.success("Added")
                            st.rerun()

    # Workbench
    if st.session_state.admin_queue:
        st.divider()
        st.markdown(f"### 🛠️ Workbench ({len(st.session_state.admin_queue)})")

        import pandas as pd
        df_queue = pd.DataFrame(st.session_state.admin_queue)
        if "table" not in df_queue.columns:
            df_queue["table"] = "Unknown"
        df_display = df_queue.rename(columns={
            "page_number": "Page Number", "context_instruction": "Instruction",
            "preview": "Original", "draft_text": "Draft", "table": "Target Table"
        })
        if "file" in df_display.columns:
            df_display["file"] = df_display["file"].apply(_get_pdf_name)

        from utils.display_safety import safe_data_editor
        edited_df = safe_data_editor(
            df_display[["selected", "id", "Target Table", "Page Number", "file", "Instruction", "Original", "Draft", "status"]],
            label="cssw_qa_workbench",
            column_config={
                "selected": st.column_config.CheckboxColumn("Sel", width="small"),
                "id": st.column_config.TextColumn("ID", disabled=True),
                "Target Table": st.column_config.TextColumn("Target Table", disabled=True, width="medium"),
                "Page Number": st.column_config.NumberColumn("Pg", disabled=True, width="small"),
                "file": st.column_config.TextColumn("PDF Name", disabled=True),
                "Instruction": st.column_config.TextColumn("Instruction", width="medium"),
                "Original": st.column_config.TextColumn("Original", disabled=True, width="large"),
                "Draft": st.column_config.TextColumn("Draft", width="large"),
                "status": st.column_config.TextColumn("Status", disabled=True)
            },
            use_container_width=True, hide_index=True, key="cssw_qa_editor"
        )

        for index, row in edited_df.iterrows():
            for item in st.session_state.admin_queue:
                if item["id"] == row["id"]:
                    item["selected"] = row["selected"]
                    item["context_instruction"] = row["Instruction"]
                    item["draft_text"] = row["Draft"]

        b1, b2, b3, b4 = st.columns(4)
        with b1:
            if st.button("✨ Gen Drafts (Selected)", key="cssw_qa_gen"):
                from views.ccs.qa import process_batch_generation
                targets = [i for i in st.session_state.admin_queue if i.get("selected")]
                process_batch_generation(session, targets, stage_path)
        with b2:
            if st.button("💾 Commit (Selected)", key="cssw_qa_commit"):
                targets = [i for i in st.session_state.admin_queue if i.get("selected")]
                count = 0
                for item in targets:
                    if item.get("draft_text"):
                        tbl = item.get("table") or current_search_table
                        tbl_base = tbl.split(".")[-1]
                        full_tbl = f'"{db}"."{schema}"."{tbl_base}"'
                        sql = f"UPDATE {full_tbl} SET CHUNK = ? WHERE CHUNK_ID = ?"
                        try:
                            session.sql(sql, params=[item["draft_text"], item["id"]]).collect()
                            item["status"] = "Committed"
                            count += 1
                        except Exception as e:
                            st.error(f"Commit failed for {item['id']}: {e}")
                if count:
                    st.success(f"Committed {count} items.")
                    st.rerun()
        with b3:
            if st.button("🗑️ Remove (Selected)", key="cssw_qa_remove"):
                st.session_state.admin_queue = [i for i in st.session_state.admin_queue if not i.get("selected")]
                st.rerun()
        with b4:
            if st.button("❌ Delete from Table (Selected)", key="cssw_qa_delete"):
                selected = [i for i in st.session_state.admin_queue if i.get("selected")]
                if not selected:
                    st.toast("No items selected.", icon="⚠️")
                else:
                    st.session_state.qa_delete_confirm = True
                    st.session_state.qa_delete_targets = selected
                    st.rerun()

        if st.session_state.get("qa_delete_confirm", False):
            targets = st.session_state.get("qa_delete_targets", [])
            st.warning(f"⚠️ **Permanently delete {len(targets)} chunk(s) from the Snowflake table?**")
            c_confirm, c_cancel = st.columns(2)
            with c_confirm:
                if st.button("✅ Confirm Delete", key="cssw_qa_confirm_del", type="primary"):
                    deleted = 0
                    for item in targets:
                        tbl = item.get("table") or current_search_table
                        tbl_base = tbl.split(".")[-1]
                        full_tbl = f'"{db}"."{schema}"."{tbl_base}"'
                        try:
                            session.sql(f"DELETE FROM {full_tbl} WHERE CHUNK_ID = ?", params=[item["id"]]).collect()
                            deleted += 1
                        except Exception as e:
                            st.error(f"Delete failed for {item['id']}: {e}")
                    deleted_ids = {t["id"] for t in targets}
                    st.session_state.admin_queue = [i for i in st.session_state.admin_queue if i["id"] not in deleted_ids]
                    st.session_state.qa_delete_confirm = False
                    st.session_state.qa_delete_targets = []
                    st.toast(f"Deleted {deleted} chunk(s).", icon="✅")
                    st.rerun()
            with c_cancel:
                if st.button("Cancel", key="cssw_qa_cancel_del"):
                    st.session_state.qa_delete_confirm = False
                    st.session_state.qa_delete_targets = []
                    st.rerun()

        # Item Inspector
        st.divider()
        sel_idx = st.selectbox(
            "Inspect Item", range(len(st.session_state.admin_queue)),
            format_func=lambda x: f"{_get_pdf_name(st.session_state.admin_queue[x]['file'])} — Pg {st.session_state.admin_queue[x]['page_number']} ({st.session_state.admin_queue[x]['id']})",
            key="cssw_qa_inspect_sel"
        )
        item = st.session_state.admin_queue[sel_idx]
        render_single_item_inspector(session, item, db, schema, stage_path)


def _render_tools(session, db, schema):
    """Render maintenance tools."""
    from views.ccs.tools import render_tools_tab
    render_tools_tab(session)
