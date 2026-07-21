# views/ccs/page2_builder.py
# Page 2: Job Builder — file selection, intent, scope, target, strategy, params, add job.
# All patterns COPIED from views/refinery/tab_config.py.

import re
import json
import streamlit as st
import pandas as pd
from logger_config import log_action
from views.ccs.common import (
    render_header, nav_buttons, ctx, jbv, jbsync,
    PRESET_OPTIONS, sync_preset_to_state, derive_preset_label,
    list_stage_files, group_by_directory, normalize_pdf_to_table_name,
)
from views.ccs.surgical_ui import render_range_mapping_section
from utils.constants import DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE


def render(session):
    from utils.auth_utils import get_user_mapped_roles
    from utils.snowflake_utils import get_table_schema
    render_header(2)

    c = ctx()
    db = c.get("db", DEFAULT_DB)
    schema = c.get("schema", DEFAULT_SCHEMA)
    stage = c.get("stage", DEFAULT_STAGE)
    stage_path = f"@{db}.{schema}.{stage}"

    # --- File listing (COPIED from tab_config.py) ---
    with st.expander(f"🔒 Active Context: {db}.{schema}", expanded=True):
        st.info(f"**Stage:** `{stage}` | **Path:** `{stage_path}`")
        pdf_files = []
        try:
            files = session.sql(f"LIST {stage_path} PATTERN='.*\\.pdf'").collect()
            prefix = f"{stage}/"
            for f in files:
                fname = f["name"]
                if fname.lower().startswith(prefix.lower()):
                    relative = fname[len(prefix):]
                    pdf_files.append(relative if relative else fname)
                else:
                    pdf_files.append(fname)
        except Exception as e:
            if "XP" in str(e) or "terminated" in str(e):
                st.error("⚠️ Connection unstable. Please refresh.")
            else:
                st.warning(f"Could not list files: {e}")

    # Group dropdown + file selectbox
    sel_file = "No files"
    if pdf_files:
        grouped = group_by_directory(pdf_files)
        group_names = list(grouped.keys())
        _grp_val = jbv("group")
        _grp_idx = group_names.index(_grp_val) if _grp_val in group_names else 0
        selected_group = st.selectbox("Directory", group_names, index=_grp_idx, key="cssw_group_widget")
        if selected_group != _grp_val:
            jbsync("group", selected_group)

        group_files = grouped.get(selected_group, [])
        _file_val = jbv("file")
        _file_options = group_files if group_files else ["No files"]
        _file_idx = _file_options.index(_file_val) if _file_val in _file_options else 0
        sel_file = st.selectbox("Select PDF", _file_options, index=_file_idx, key="cssw_file_widget")
        if sel_file != "No files":
            if sel_file != _file_val:
                jbsync("file", sel_file)
                # Auto-fill table name from PDF: set the widget key directly
                # so the text_input reads the new value from session state on
                # the next rerun. Never combine value= AND key= on the widget
                # (Streamlit's "widget value already set" error + the locked-
                # display bug from HTML_lesson_learnt.md §6 both come from
                # combining the two). Initialize the widget via session_state
                # at render time instead.
                normalized = normalize_pdf_to_table_name(sel_file)
                jbsync("table_name", normalized)
                st.session_state["cssw_table_widget"] = normalized
    else:
        st.warning("No PDF files found.")

    st.divider()

    # --- Intent pills (COPIED from tab_config.py) ---
    st.markdown("#### 📋 Job Builder")

    _current_mode = st.session_state.get("cssw_mode")
    _current_scope = st.session_state.get("cssw_scope")
    _active_preset = derive_preset_label(_current_mode, _current_scope) if _current_mode and _current_scope else None

    preset_label = None
    try:
        preset_label = st.pills(
            "Job Intent", options=PRESET_OPTIONS, selection_mode="single",
            default=None, key="cssw_preset",
            help="Select an intent to auto-configure the write mode and scope below."
        )
    except AttributeError:
        log_action("PRESET_FALLBACK", "st.pills unavailable, falling back to st.radio.", level="WARNING")
        _radio_idx = PRESET_OPTIONS.index(_active_preset) if _active_preset in PRESET_OPTIONS else 0
        preset_label = st.radio("Job Intent (fallback)", options=PRESET_OPTIONS,
                                index=_radio_idx, horizontal=True, key="cssw_preset_radio")

    # Sync preset to mode/scope ONLY when the preset changes (not every rerun).
    # This allows the user to manually change mode/scope after selecting a preset
    # without the preset forcing them back on the next rerun.
    _last_applied = st.session_state.get("_cssw_last_applied_preset")
    if preset_label and preset_label != _last_applied:
        sync_preset_to_state(preset_label)
        st.session_state["_cssw_last_applied_preset"] = preset_label
        st.rerun()
    elif not preset_label and _last_applied:
        st.session_state.pop("_cssw_last_applied_preset", None)
    # If preset_label == _last_applied, do nothing — user's manual changes are preserved.

    # --- Job Builder UI (COPIED structure from tab_config.py) ---
    with st.container():
        jc1, jc2, jc3 = st.columns(3)

        with jc1:
            st.markdown("**📄 File & Scope**")

            _link_val = jbv("link")
            pdf_link = st.text_input("PDF Download Link (Optional)", value=_link_val,
                                     key="cssw_link_widget",
                                     help="Reference for where to get the digital copy.")
            if pdf_link != _link_val:
                jbsync("link", pdf_link)

            scope = st.radio("Scope", ["Full Doc", "Page Range"], horizontal=True, key="cssw_scope")

            # Page count detection (COPIED from tab_config.py)
            page_count_est = 1
            if sel_file != "No files":
                if "file_metadata_cache" not in st.session_state:
                    st.session_state.file_metadata_cache = {}
                if sel_file in st.session_state.file_metadata_cache:
                    page_count_est = st.session_state.file_metadata_cache[sel_file]["page_count"]
                else:
                    try:
                        safe_file_sql = sel_file.replace("'", "''")
                        safe_stage_sql = stage_path.replace("'", "''")
                        parse_opts = json.dumps({"mode": "LAYOUT", "page_split": True})
                        parse_sql = f"""
                            SELECT SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(
                                TO_FILE('{safe_stage_sql}', '{safe_file_sql}'),
                                PARSE_JSON('{parse_opts}')
                            ) AS J
                        """
                        parse_res = session.sql(parse_sql).collect()
                        if parse_res and parse_res[0]["J"]:
                            doc_json = json.loads(parse_res[0]["J"])
                            metadata = doc_json.get("metadata", {})
                            page_count_est = metadata.get("pageCount", len(doc_json.get("pages", []))) or 1
                            st.session_state.file_metadata_cache[sel_file] = {"page_count": page_count_est}
                        else:
                            log_action("PDF_PARSE_NULL", {"file": sel_file}, level="WARNING")
                    except Exception as e:
                        log_action("PDF_PAGE_COUNT_ERROR", {"file": sel_file, "error": str(e)}, level="WARNING")
                st.caption(f"Detected {page_count_est} pages")

            p_start, p_end = 1, page_count_est
            if scope == "Page Range":
                c_rng1, c_rng2 = st.columns(2)
                _ps_val = jbv("pstart")
                _pe_val = jbv("pend")
                p_start = c_rng1.number_input("Start", 1, max(1, page_count_est), value=_ps_val, key="cssw_pstart_widget")
                p_end = c_rng2.number_input("End", 1, max(1, page_count_est), value=min(_pe_val, page_count_est), key="cssw_pend_widget")
                if p_start != _ps_val: jbsync("pstart", p_start)
                if p_end != _pe_val: jbsync("pend", p_end)

        with jc2:
            st.markdown("**🎯 Target & Strategy**")

            _tbl_val = jbv("table_name")
            # Initialize widget key from the source-of-truth helper key, but
            # ONLY when the widget key is not yet in session_state. Overwriting
            # an existing widget key here would clobber user input mid-typing.
            # Never pass value= AND key= together (HTML_lesson_learnt.md §6):
            # value= is silently ignored once the widget key exists in
            # session_state, which breaks the PDF auto-fill scenario.
            st.session_state.setdefault("cssw_table_widget", _tbl_val)
            target_table_name = st.text_input("Target Table Name", key="cssw_table_widget")
            if target_table_name != _tbl_val:
                jbsync("table_name", target_table_name)
            target_table = target_table_name

            target_table_base = target_table_name.split(".")[-1]
            tbl_exists, _, _ = get_table_schema(session, db, schema, target_table_base)

            mode_help = (
                "**APPEND**: Adds new chunks to the end of the table.\n"
                "**OVERWRITE**: Drops and recreates the table.\n"
                "**SURGICAL**: Removes specific file/page entries before inserting new ones (Requires existing table)."
            )
            mode = st.radio("Write Mode", ["APPEND", "OVERWRITE", "SURGICAL"],
                            key="cssw_mode", help=mode_help)

            blocking_error = False
            grant_roles = []

            if mode == "SURGICAL":
                if not tbl_exists:
                    st.error("❌ Table must exist for SURGICAL mode.")
                    blocking_error = True
                else:
                    st.success("✅ Target table confirmed.")
                    # Fetch existing files and page counts from the target table
                    existing_files = []
                    page_count_map = {}
                    source_page_min_map = {}
                    try:
                        safe_target = target_table_base.replace('"', '""')
                        res = session.sql(f'''
                            SELECT RELATIVE_PATH, MIN(PAGE_NUMBER) as min_page, MAX(PAGE_NUMBER) as max_page
                            FROM "{db}"."{schema}"."{safe_target}"
                            GROUP BY RELATIVE_PATH
                        ''').collect()
                        for row in res:
                            path = row[0]
                            min_p = int(row[1]) if row[1] is not None else 1
                            max_p = int(row[2]) if row[2] is not None else 1
                            existing_files.append(path)
                            page_count_map[path] = max_p
                            source_page_min_map[path] = min_p
                        existing_files = sorted(list(set(existing_files)))
                    except Exception as e:
                        st.warning(f"Could not fetch existing files: {e}")
                        existing_files = [sel_file]

                    if sel_file not in existing_files:
                        existing_files.append(sel_file)
                        page_count_map[sel_file] = page_count_est
                        source_page_min_map[sel_file] = 1

                    with st.expander("📑 Configure Page Mappings", expanded=True):
                        render_range_mapping_section(
                            source_file=sel_file,
                            source_start=p_start,
                            source_end=p_end,
                            source_page_min=source_page_min_map.get(sel_file, 1),
                            source_page_max=page_count_map.get(sel_file, page_count_est),
                            replacement_files=existing_files,
                            replacement_pages_map=page_count_map,
                            key_prefix="surg_range"
                        )
                        mapping_result = st.session_state.get('surgical_range_result', {})
                        if not mapping_result.get('is_valid', False):
                            st.error("❌ Fix mapping errors to proceed.")
                            blocking_error = True
            elif mode in ["APPEND", "OVERWRITE"]:
                if tbl_exists:
                    st.info(f"ℹ️ Table exists. Data will be {mode.lower()}ed.")
                    # Duplicate page detection for APPEND mode
                    if mode == "APPEND":
                        try:
                            safe_target = target_table_base.replace('"', '""')
                            page_condition = f"AND PAGE_NUMBER BETWEEN {p_start} AND {p_end}" if scope == "Page Range" else ""
                            safe_file_check = sel_file.replace("'", "''")
                            dup_sql = f'''
                                SELECT RELATIVE_PATH, PAGE_NUMBER
                                FROM "{db}"."{schema}"."{safe_target}"
                                WHERE RELATIVE_PATH = '{safe_file_check}'
                                {page_condition}
                                ORDER BY PAGE_NUMBER
                            '''
                            dup_res = session.sql(dup_sql).collect()
                            if dup_res:
                                dup_pages = [int(r[1]) for r in dup_res]
                                total_dup = len(dup_pages)
                                display_pages = dup_pages[:6]
                                if total_dup <= 6:
                                    page_list = ", ".join(str(p) for p in display_pages)
                                    st.warning(f"⚠️ **Possible Duplicate Pages Detected** ({total_dup} total): Pages {page_list} already exist in `{target_table_name}` for `{sel_file}`. Appending will create duplicate content with different CHUNK_IDs.")
                                else:
                                    page_list = ", ".join(str(p) for p in display_pages)
                                    st.warning(f"⚠️ **Possible Duplicate Pages Detected** ({total_dup} total): Pages {page_list}, ... and {total_dup - 6} more already exist in `{target_table_name}` for `{sel_file}`. Appending will create duplicate content with different CHUNK_IDs.")
                        except Exception:
                            pass
                else:
                    st.warning("🆕 Table does not exist. It will be created.")
                    avail_roles = get_user_mapped_roles(c.get("user", ""))
                    auto_roles = [r for r in avail_roles if r.upper() != "IT_AI"]
                    default_str = auto_roles[0] if auto_roles else ""
                    grant_input = st.text_input(
                        "Grants for New Table",
                        value=jbv("grant_roles") or default_str,
                        placeholder="e.g., IT_DS, IT_BI",
                        help="Comma-separated role names. IT_AI is automatically the owner.",
                        key="cssw_grant_widget"
                    )
                    jbsync("grant_roles", grant_input)
                    raw_splits = re.findall(r'[^,\s"]+|"[^"]*"', grant_input)
                    grant_roles = list(dict.fromkeys(
                        r.strip().upper() for r in raw_splits
                        if r.strip() and r.strip().upper() != "IT_AI"
                    ))

            _lay_val = jbv("layout")
            _vis_val = jbv("vision")
            use_layout = st.checkbox("Use Layout Parser (Structural)", _lay_val, key="cssw_layout_widget")
            use_vision = st.checkbox("Use Vision Parser (Charts/Images)", _vis_val, key="cssw_vision_widget")
            if use_layout != _lay_val: jbsync("layout", use_layout)
            if use_vision != _vis_val: jbsync("vision", use_vision)
            if not use_layout and not use_vision:
                st.error("Select at least one strategy.")
                blocking_error = True

        with jc3:
            st.markdown("**⚙️ Parameters**")

            _chk_val = jbv("chunk")
            chk_sz = st.number_input("Chunk Size", 1000, 30000, _chk_val, step=500,
                                     key="cssw_chunk_widget",
                                     help="Maximum characters per chunk. Chunks do not cross page boundaries.")
            if chk_sz != _chk_val: jbsync("chunk", chk_sz)

            _ov_val = jbv("overlap")
            overlap_pct = st.slider("Overlap %", 0, 50, _ov_val, key="cssw_overlap_widget",
                                    help="Characters repeated between adjacent chunks on the same page.")
            if overlap_pct != _ov_val: jbsync("overlap", overlap_pct)
            overlap = int(chk_sz * (overlap_pct / 100))

            if scope == "Page Range" and p_start > p_end:
                st.error("❌ Start Page cannot be greater than End Page.")
                blocking_error = True

            if st.button("➕ Add Job", key="cssw_add", type="primary",
                         disabled=bool(blocking_error or sel_file == "No files")):
                mapping_res = st.session_state.get('surgical_range_result', {})
                if mode == "SURGICAL" and not mapping_res.get('is_valid', False):
                    st.error("Cannot add job: Invalid page mappings.")
                else:
                    est_pages = (p_end - p_start) + 1 if scope == "Page Range" else page_count_est
                    jobs = st.session_state.get("cssw_jobs", [])
                    new_id = max([j["id"] for j in jobs] + [0]) + 1
                    job_data = {
                        "id": new_id, "file": sel_file, "table": target_table,
                        "mode": mode, "scope": scope, "range": (p_start, p_end),
                        "estimated_pages": est_pages, "layout": use_layout, "vision": use_vision,
                        "params": (chk_sz, overlap), "grant_roles": grant_roles,
                        "link": pdf_link, "status": "Pending",
                    }
                    if mode == "SURGICAL":
                        job_data.update({
                            "surgical_file": sel_file,
                            "surgical_replacement_file": mapping_res.get('replacement_file'),
                            "surgical_range_mappings": mapping_res.get('range_mappings', [])
                        })
                    if "cssw_jobs" not in st.session_state:
                        st.session_state.cssw_jobs = []
                    st.session_state.cssw_jobs.append(job_data)
                    st.success(f"✅ Job #{new_id} added")
                    log_action("CSSW_JOB_ADDED", {"id": new_id, "file": sel_file, "table": target_table})
                    st.rerun()

    # --- Job Queue Workbench (COPIED from tab_config.py) ---
    jobs = st.session_state.get("cssw_jobs", [])
    if jobs:
        st.divider()
        st.markdown(f"#### 📊 Job Workbench ({len(jobs)} jobs)")

        def fmt_scope(j):
            if j["scope"] == "Full Doc":
                return "Full"
            s, e = j["range"]
            return f"{s}-{e}"

        q_data = [{
            "selected": j.get("selected", False), "id": j["id"], "file": j["file"],
            "table": j["table"], "Mode": j["mode"], "Scope Constraint": fmt_scope(j),
            "Target File": j.get("surgical_replacement_file", j["file"]) if j.get("mode") == "SURGICAL" else j.get("surgical_target_file", j["file"]),
            "Mappings": (
                f"{len(j.get('surgical_range_mappings', []))} ranges"
                if j.get("mode") == "SURGICAL" and j.get('surgical_range_mappings')
                else f"{len(j.get('surgical_page_mappings', []))} pages"
                if j.get("mode") == "SURGICAL" and j.get('surgical_page_mappings')
                else "-"
            ),
            "PDF Link": j.get("link", ""), "Assigned Roles": ", ".join(j.get("grant_roles", [])),
            "L": j.get("layout", True), "V": j.get("vision", True),
            "pages": j.get("estimated_pages", 1), "status": j["status"],
        } for j in jobs]

        edited_df = st.data_editor(
            pd.DataFrame(q_data),
            column_config={
                "selected": st.column_config.CheckboxColumn("Select", width="small"),
                "id": st.column_config.NumberColumn("ID", disabled=True, width="small"),
                "file": st.column_config.TextColumn("File", disabled=True, width="medium"),
                "table": st.column_config.TextColumn("Table", disabled=True, width="medium"),
                "Mode": st.column_config.SelectboxColumn("Mode", options=["APPEND", "OVERWRITE", "SURGICAL"], width="small"),
                "Scope Constraint": st.column_config.TextColumn("Scope", width="medium"),
                "Target File": st.column_config.TextColumn("Target File", width="medium", disabled=True),
                "Mappings": st.column_config.TextColumn("Mappings", width="small", disabled=True),
                "PDF Link": st.column_config.TextColumn("PDF Link", width="medium"),
                "Assigned Roles": st.column_config.TextColumn("Roles", width="medium"),
                "L": st.column_config.CheckboxColumn("L", width="small"),
                "V": st.column_config.CheckboxColumn("V", width="small"),
                "pages": st.column_config.NumberColumn("Pages", disabled=True, width="small"),
                "status": st.column_config.TextColumn("Status", disabled=True, width="small"),
            },
            use_container_width=True, hide_index=True, key="cssw_job_editor"
        )

        if not edited_df.equals(pd.DataFrame(q_data)):
            for _, row in edited_df.iterrows():
                tgt = next((j for j in jobs if j["id"] == row["id"]), None)
                if not tgt:
                    continue
                tgt["mode"] = row["Mode"]
                tgt["selected"] = row["selected"]
                tgt["layout"] = row["L"]
                tgt["vision"] = row["V"]
                tgt["link"] = str(row["PDF Link"]) if pd.notna(row.get("PDF Link")) else ""
                raw_roles = str(row["Assigned Roles"]) if pd.notna(row.get("Assigned Roles")) else ""
                tgt["grant_roles"] = list(dict.fromkeys(
                    r.strip().upper() for r in re.findall(r'[^,\s"]+|"[^"]*"', raw_roles)
                    if r.strip() and r.strip().upper() != "IT_AI"
                ))
                new_scope_str = str(row["Scope Constraint"]).strip().lower()
                max_pg = st.session_state.get("file_metadata_cache", {}).get(tgt["file"], {}).get("page_count", 1)
                if new_scope_str in ["full", "full doc", "all"]:
                    tgt["scope"] = "Full Doc"; tgt["range"] = (1, max_pg); tgt["estimated_pages"] = max_pg
                elif "-" in new_scope_str:
                    try:
                        parts = new_scope_str.split("-")
                        s, e = int(parts[0]), int(parts[1])
                        if 1 <= s <= e <= max_pg:
                            tgt["scope"] = "Page Range"; tgt["range"] = (s, e); tgt["estimated_pages"] = e - s
                    except Exception:
                        pass
            st.rerun()

        bc1, bc2 = st.columns(2)
        with bc1:
            if st.button("🗑️ Delete Selected Jobs"):
                st.session_state.cssw_jobs = [j for j in jobs if not j.get("selected")]
                st.rerun()
        with bc2:
            if st.button("💥 Clear Queue"):
                st.session_state.cssw_jobs = []; st.rerun()

    nav_buttons(len(jobs) > 0)
