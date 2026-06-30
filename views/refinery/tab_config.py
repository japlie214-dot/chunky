# views/refinery/tab_config.py
# Config Tab - Job Management for the Doc Refinery package
import streamlit as st
import pandas as pd
import os
import json
from utils.core_utils import PDFUtils
from utils.snowflake_utils import get_table_schema
from utils.auth_utils import get_user_mapped_roles
from utils.page_mapping import PageMappingEngine, RangeMappingEngine
# render_page_mapping_section removed — it was dead code (never called, confirmed by repo grep)
from views.refinery.surgical_ui import render_range_mapping_section
from logger_config import log_action

# Preset definition: intent labels → (mode, scope) pairs.
# The order here determines the pill display order.
PRESET_OPTIONS = ["Add New Pages", "Replace Specific Pages", "Replace All Data"]
# Reverse lookup: (mode, scope) → preset label
_MODE_SCOPE_TO_PRESET = {
    ("APPEND", "Full Doc"): "Add New Pages",
    ("SURGICAL", "Page Range"): "Replace Specific Pages",
    ("OVERWRITE", "Full Doc"): "Replace All Data",
}

def _sync_preset_to_state(preset_label: str) -> None:
    """
    Write the preset's (mode, scope) into session_state keys that the
    existing jb_mode/jb_scope widgets read from.
    """
    mapping = {
        "Add New Pages": ("APPEND", "Full Doc"),
        "Replace Specific Pages": ("SURGICAL", "Page Range"),
        "Replace All Data": ("OVERWRITE", "Full Doc"),
    }
    mode, scope = mapping[preset_label]
    st.session_state["jb_mode"] = mode
    st.session_state["jb_scope"] = scope


def _derive_preset_label(mode: str, scope: str) -> str | None:
    """
    Derive the preset label from the current mode+scope. Returns None if
    the combination doesn't match any preset (user made a manual override
    that doesn't correspond to a preset intent).
    """
    return _MODE_SCOPE_TO_PRESET.get((mode, scope))


def render_config_tab(session):
    st.subheader("1. Job Management")

    # -----------------------------------------------------------------------
    # Persist Job Builder state across tab navigation.
    #
    # mode/scope: managed by Streamlit widget keys directly. Presets write
    # to st.session_state[jb_mode/jb_scope] BEFORE the widget renders
    # (Streamlit's "pre-setting" pattern). Non-preset fields use _jbv_*
    # helper keys as source of truth, passed as the widget's `value` param.
    # -----------------------------------------------------------------------
    # mode and scope use setdefault — Streamlit manages these via widget keys.
    # Presets write to the keys directly before widgets render.
    # mode and scope are managed by their radio widget keys.
    # Do NOT setdefault here — it forces a preset selection on first render.
    # Radio buttons self-initialize to their first option (APPEND / Full Doc)
    # when no key exists in session_state.
    _jb_defaults = {
        "file": "",
        "table_name": "SUS_CHUNKS",
        "link": "",
        "pstart": 1,
        "pend": 10,
        "grant_roles": "",
        "layout": True,
        "vision": True,
        "chunk": 8000,
        "overlap": 20,
    }
    for _field, _default in _jb_defaults.items():
        _helper_key = f"_jbv_{_field}"
        if _helper_key not in st.session_state:
            st.session_state[_helper_key] = _default

    def _jbv(field):
        """Read Job Builder value from helper key."""
        return st.session_state.get(f"_jbv_{field}", _jb_defaults.get(field))

    def _jbsync(field, new_value):
        """Sync widget value back to helper key."""
        st.session_state[f"_jbv_{field}"] = new_value

    # Context Retrieval
    ctx = st.session_state.auth_context
    db, schema, stage = ctx["db"], ctx["schema"], ctx["stage"]
    stage_path = f"@{db}.{schema}.{stage}"

    # Infrastructure Display (Read-Only)
    with st.expander(f"🔒 Active Context: {db}.{schema}", expanded=True):
        st.info(f"**Stage:** `{stage}` | **Path:** `{stage_path}`")
        
        # Wrap file listing in try/except to catch XP process errors
        pdf_files = []
        try:
            files = session.sql(f"LIST {stage_path} PATTERN='.*\\.pdf'").collect()
            # LIST returns name as "stage_name/folder/file.pdf" but
            # RELATIVE_PATH stores "folder/file.pdf". Strip the stage prefix
            # to preserve subdirectory paths while matching RELATIVE_PATH format.
            # Case-insensitive comparison because Snowflake may lowercase the stage name.
            prefix = f"{stage}/"
            pdf_files = []
            for f in files:
                fname = f['name']
                if fname.lower().startswith(prefix.lower()):
                    relative = fname[len(prefix):]
                    pdf_files.append(relative if relative else fname)
                else:
                    pdf_files.append(fname)
        except Exception as e:
            # Handle Snowflake XP/Session termination errors gracefully
            if "XP" in str(e) or "terminated" in str(e):
                st.error("⚠️ Connection unstable. Please refresh the page to reconnect.")
            else:
                st.warning(f"Could not list files: {e}")

    # =========================================================================
    # MVP FEATURE #1: Intent-Driven Preset Selector
    # =========================================================================
    # Ref: https://docs.streamlit.io/develop/api-reference/widgets/st.pills
    # st.pills was introduced in Streamlit 1.40.0.
    # Ref: https://discuss.streamlit.io/t/version-1-40-0/85145
    #
    # We use try/except as a defensive measure. Since requirements.txt now
    # enforces >=1.40.0, the except path should never execute in production.
    # If it does, it means the environment is misconfigured, and we log a warning.
    st.markdown("#### 📋 Job Builder")

    # Derive the current preset from existing state (before rendering the widget).
    # On first render, no preset is pre-selected (user must choose).
    # After the user picks a preset, _sync_preset_to_state sets jb_mode/jb_scope
    # and we derive the correct preset from those values.
    _current_mode = st.session_state.get("jb_mode")
    _current_scope = st.session_state.get("jb_scope")
    _active_preset = _derive_preset_label(_current_mode, _current_scope) if _current_mode and _current_scope else None

    preset_label = None
    try:
        # Ref: https://docs.streamlit.io/develop/api-reference/widgets/st.pills
        # selection_mode="single" (default) — user picks one intent.
        # default=_active_preset — pre-selects the pill matching current state.
        # If _active_preset is None (no matching preset), no pill is selected.
        preset_label = st.pills(
            "Job Intent",
            options=PRESET_OPTIONS,
            selection_mode="single",
            default=None,
            key="jb_preset",
            # Ref: https://docs.streamlit.io/develop/api-reference/widgets/st.pills
            # help parameter renders a tooltip (ℹ) next to the label.
            help="Select an intent to auto-configure the write mode and scope below."
        )
    except AttributeError:
        # st.pills unavailable — Streamlit <1.40.0 (should not happen after version bump).
        # Ref: https://docs.streamlit.io/develop/api-reference/widgets/st.radio
        # st.radio is the safe fallback — same selection semantics, different visual.
        log_action(
            "PRESET_FALLBACK",
            "st.pills unavailable, falling back to st.radio. Streamlit may be <1.40.0.",
            level="WARNING"
        )
        _radio_idx = PRESET_OPTIONS.index(_active_preset) if _active_preset in PRESET_OPTIONS else 0
        preset_label = st.radio(
            "Job Intent (fallback)",
            options=PRESET_OPTIONS,
            index=_radio_idx,
            horizontal=True,
            key="jb_preset_radio"
        )

    # When the user picks a preset, push the (mode, scope) into session state
    # BEFORE the downstream widgets render. This uses the "pre-setting" pattern
    # from Streamlit docs: setting session_state[widget_key] before the widget
    # call causes the widget to adopt that value.
    # Ref: https://docs.streamlit.io/develop/concepts/architecture/session-state
    if preset_label:
        _sync_preset_to_state(preset_label)

    # =========================================================================
    # Existing Job Builder UI (columns, widgets — mostly unchanged)
    # =========================================================================
    with st.container():
        jc1, jc2, jc3 = st.columns(3)
        
        with jc1:
            st.markdown("**📄 File & Scope**")
            # Persist selected PDF across tab navigation via _jbv pattern.
            _file_val = _jbv("file")
            _file_options = pdf_files if pdf_files else ["No files"]
            _file_idx = _file_options.index(_file_val) if _file_val in _file_options else 0
            sel_file = st.selectbox("Select PDF", _file_options, index=_file_idx, key="jb_file")
            if sel_file != _file_val and sel_file != "No files": _jbsync("file", sel_file)
            
            # PLAN-17: PDF Download Link moved above Scope selector with help text
            _link_val = _jbv("link")
            pdf_link = st.text_input(
                "PDF Download Link (Optional)",
                value=_link_val,
                key="jb_link",
                help="This will be used as reference as to where we could get the digital copy of the PDF."
            )
            if pdf_link != _link_val: _jbsync("link", pdf_link)
            
            scope = st.radio("Scope", ["Full Doc", "Page Range"], horizontal=True, key="jb_scope")
            
            # Metadata Caching
            page_count_est = 1
            if sel_file != "No files":
                if 'file_metadata_cache' not in st.session_state: st.session_state.file_metadata_cache = {}
                if sel_file in st.session_state.file_metadata_cache:
                    page_count_est = st.session_state.file_metadata_cache[sel_file]['page_count']
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
                            # Prefer metadata.pageCount (direct from API)
                            metadata = doc_json.get("metadata", {})
                            page_count_est = metadata.get("pageCount", len(doc_json.get("pages", []))) or 1
                            st.session_state.file_metadata_cache[sel_file] = {'page_count': page_count_est}
                        else:
                            log_action("PDF_PARSE_NULL", {"file": sel_file}, level="WARNING")
                    except Exception as e:
                        log_action("PDF_PAGE_COUNT_ERROR", {"file": sel_file, "error": str(e)}, level="WARNING")
                st.caption(f"Detected {page_count_est} pages")

            p_start, p_end = 1, page_count_est
            if scope == "Page Range":
                c_rng1, c_rng2 = st.columns(2)
                _ps_val = _jbv("pstart")
                _pe_val = _jbv("pend")
                p_start = c_rng1.number_input("Start", 1, max(1, page_count_est), value=_ps_val, key="jb_pstart")
                p_end = c_rng2.number_input("End", 1, max(1, page_count_est), value=min(_pe_val, page_count_est), key="jb_pend")
                if p_start != _ps_val: _jbsync("pstart", p_start)
                if p_end != _pe_val: _jbsync("pend", p_end)

        with jc2:
            st.markdown("**🎯 Target & Strategy**")
            # Locked to current schema context, but user can define Table Name
            # Read from helper key (source of truth, never a widget key)
            _tbl_val = _jbv("table_name")
            target_table_name = st.text_input("Target Table Name", value=_tbl_val, key="jb_table_name")
            if target_table_name != _tbl_val: _jbsync("table_name", target_table_name)
            target_table = target_table_name
            
            # Active Table Check
            target_table_base = target_table_name.split('.')[-1]
            tbl_exists, _, tbl_err = get_table_schema(session, db, schema, target_table_base)
            
            mode_help = (
                "**APPEND**: Adds new chunks to the end of the table.\n"
                "**OVERWRITE**: Drops and recreates the table.\n"
                "**SURGICAL**: Removes specific file/page entries before inserting new ones (Requires existing table)."
            )
            mode = st.radio("Write Mode", ["APPEND", "OVERWRITE", "SURGICAL"],
                           key="jb_mode", help=mode_help)
            
            # Display dynamic status messages & Block SURGICAL mode
            blocking_error = False
            grant_roles = []
            surgical_target_file = None
            surgical_target_page = 0
            
            if mode == "SURGICAL":
                if not tbl_exists:
                    st.error("❌ Table must exist for SURGICAL mode.")
                    blocking_error = True
                else:
                    st.success("✅ Target table confirmed.")
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

                    # MVP FEATURE #2: Inherited Scope Defaults.
                    # When the preset set scope to "Page Range", p_start/p_end are derived
                    # from the parent Job Builder scope (lines above). These values flow
                    # into render_range_mapping_section as source_start/source_end,
                    # which uses them as the default mapping range.
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
                    import re
                    avail_roles = get_user_mapped_roles(ctx.get("user", ""))
                    
                    # ARCHITECTURAL CONSTRAINT: IT_AI is excluded from standard grants intentionally.
                    # The Streamlit app runs with owner's rights (IT_AI), meaning any new tables created are
                    # automatically owned by IT_AI. Granting access to the owner is redundant and causes execution errors.
                    auto_roles = [r for r in avail_roles if r.upper() != "IT_AI"]
                    default_str = auto_roles[0] if auto_roles else ""
                    
                    grant_input = st.text_input(
                        "Grants for New Table",
                        value=_jbv("grant_roles") or default_str,
                        placeholder="e.g., IT_DS, IT_BI, \"CUSTOM-ROLE\"",
                        help="Comma or space-separated Snowflake role names. IT_AI is automatically the owner. Invalid roles are skipped.",
                        key="jb_grant_roles"
                    )
                    _jbsync("grant_roles", grant_input)
                    
                    # Robust parsing: extracts unquoted words OR double-quoted strings, preserving internal spaces
                    raw_splits = re.findall(r'[^,\s"]+|"[^"]*"', grant_input)
                    grant_roles = list(dict.fromkeys(
                        r.strip().upper() for r in raw_splits
                        if r.strip() and r.strip().upper() != "IT_AI"
                    ))
            
            _lay_val = _jbv("layout")
            _vis_val = _jbv("vision")
            use_layout = st.checkbox("Use Layout Parser (Structural)", _lay_val, key="jb_layout")
            use_vision = st.checkbox("Use Vision Parser (Charts/Images)", _vis_val, key="jb_vision")
            if use_layout != _lay_val: _jbsync("layout", use_layout)
            if use_vision != _vis_val: _jbsync("vision", use_vision)
            if not use_layout and not use_vision:
                st.error("Select at least one strategy.")
                blocking_error = True

        with jc3:
            st.markdown("**⚙️ Parameters**")
            chk_help = "Maximum characters per chunk. Chunks are strictly bounded by page; they do not cross page boundaries."
            _chk_val = _jbv("chunk")
            chk_sz = st.number_input("Chunk Size", 1000, 30000, _chk_val, step=500, key="jb_chunk", help=chk_help)
            if chk_sz != _chk_val: _jbsync("chunk", chk_sz)
            
            ov_help = "Characters repeated between adjacent chunks *on the same page only*."
            _ov_val = _jbv("overlap")
            overlap_pct = st.slider("Overlap %", 0, 50, _ov_val, key="jb_overlap", help=ov_help)
            if overlap_pct != _ov_val: _jbsync("overlap", overlap_pct)
            overlap = int(chk_sz * (overlap_pct / 100))
            
            # Validate Page Range inputs
            if scope == "Page Range" and p_start > p_end:
                st.error("❌ Start Page cannot be greater than End Page.")
                blocking_error = True
            
            if st.button("➕ Add Job", key="jb_add", type="primary", disabled=bool(blocking_error or not pdf_files)):
                mapping_res = st.session_state.get('surgical_range_result', {})
                if mode == "SURGICAL" and not mapping_res.get('is_valid', False):
                    st.error("Cannot add job: Invalid page mappings.")
                else:
                    est_pages = (p_end - p_start) + 1 if scope == "Page Range" else page_count_est
                    if 'job_queue' not in st.session_state: st.session_state.job_queue = []
                    new_id = max([j['id'] for j in st.session_state.job_queue] + [0]) + 1
                    
                    job_data = {
                        "id": new_id,
                        "file": sel_file,
                        "table": target_table,
                        "mode": mode,
                        "scope": scope,
                        "range": (p_start, p_end),
                        "estimated_pages": est_pages,
                        "layout": use_layout,
                        "vision": use_vision,
                        "params": (chk_sz, overlap),
                        "grant_roles": grant_roles,
                        "link": pdf_link,
                        "status": "Pending"
                    }
                    if mode == "SURGICAL":
                        job_data.update({
                            "surgical_file": sel_file,
                            "surgical_replacement_file": mapping_res.get('replacement_file'),
                            "surgical_range_mappings": mapping_res.get('range_mappings', [])
                        })
                    st.session_state.job_queue.append(job_data)
                    st.success("Job Added")
                    st.rerun()

    # Job Queue Display
    if 'job_queue' in st.session_state and st.session_state.job_queue:
        st.divider()
        st.markdown("#### 📊 Job Queue Workbench")
        
        # Helper to format scope for display/editing
        def fmt_scope(j):
            if j['scope'] == 'Full Doc': return "Full"
            s, e = j['range']
            return f"{s}-{e}"

        q_data = []
        for j in st.session_state.job_queue:
            q_data.append({
                "selected": j.get("selected", False),
                "id": j["id"],
                "file": j["file"],
                "table": j["table"],
                "Mode": j["mode"],
                "Scope Constraint": fmt_scope(j),
                "Target File": j.get("surgical_replacement_file", j["file"]) if j.get("mode") == "SURGICAL" else j.get("surgical_target_file", j["file"]),
                "Mappings": (
                    f"{len(j.get('surgical_range_mappings', []))} ranges"
                    if j.get("mode") == "SURGICAL" and j.get('surgical_range_mappings')
                    else f"{len(j.get('surgical_page_mappings', []))} pages"
                    if j.get("mode") == "SURGICAL" and j.get('surgical_page_mappings')
                    else "-"
                ),
                "PDF Link": j.get("link", ""),
                "Assigned Roles": ", ".join(j.get("grant_roles", [])),
                "L": j.get("layout", True),
                "V": j.get("vision", True),
                "pages": j.get("estimated_pages", 1),
                "status": j["status"]
            })
            
        edited_df = st.data_editor(
        pd.DataFrame(q_data),
        column_config={
            "selected": st.column_config.CheckboxColumn("Select", width="small"),
            "id": st.column_config.NumberColumn("ID", disabled=True, width="small"),
            "file": st.column_config.TextColumn("File", disabled=True, width="medium"),
            "Mode": st.column_config.SelectboxColumn("Mode", options=["APPEND", "OVERWRITE", "SURGICAL"], width="small"),
            "Scope Constraint": st.column_config.TextColumn("Scope", width="medium"),
            "Target File": st.column_config.TextColumn("Target File", width="medium", disabled=True),
            "Mappings": st.column_config.TextColumn("Mappings", width="small", disabled=True),
            "PDF Link": st.column_config.TextColumn("PDF Link", width="medium"),
            "Assigned Roles": st.column_config.TextColumn("Assigned Roles", width="medium"),
            "status": st.column_config.TextColumn("Status", disabled=True)
        },
        use_container_width=True,
        hide_index=True,
        key="config_job_editor_v4"
    )
        
        # Sync Logic with Validation
        if not edited_df.equals(pd.DataFrame(q_data)):
            for index, row in edited_df.iterrows():
                target_job = next((j for j in st.session_state.job_queue if j["id"] == row["id"]), None)
                if not target_job: continue
                
                # 1. Update Mode and Boolean Flags
                target_job["mode"] = row["Mode"]
                target_job["selected"] = row["selected"]
                target_job["layout"] = row["L"]
                target_job["vision"] = row["V"]
                target_job["surgical_target_file"] = str(row.get("Target File")) if pd.notna(row.get("Target File")) else target_job["file"]
                target_job["surgical_target_page"] = int(row.get("Target Page")) if pd.notna(row.get("Target Page")) else 0
                
                # PLAN-16: pd.notna() guards prevent the string "nan" from entering the
                # job dict when a user leaves a cell blank in the data editor.
                target_job["link"] = (
                    str(row["PDF Link"]) if pd.notna(row.get("PDF Link")) else ""
                )
                raw_roles = (
                    str(row["Assigned Roles"]) if pd.notna(row.get("Assigned Roles")) else ""
                )
                import re
                # ARCHITECTURAL CONSTRAINT: IT_AI is excluded to prevent redundant owner grants.
                # Findall pattern extracts unquoted words OR double-quoted strings, preserving internal spaces.
                target_job["grant_roles"] = list(dict.fromkeys(
                    r.strip().upper()
                    for r in re.findall(r'[^,\s"]+|"[^"]*"', raw_roles)
                    if r.strip() and r.strip().upper() != "IT_AI"
                ))
                
                # 2. Validate & Update Scope
                new_scope_str = str(row["Scope Constraint"]).strip().lower()
                
                # Get max pages from cache
                max_pg = 1
                if target_job['file'] in st.session_state.file_metadata_cache:
                    max_pg = st.session_state.file_metadata_cache[target_job['file']]['page_count']
                
                valid_update = False
                
                if new_scope_str in ["full", "full doc", "all"]:
                    target_job["scope"] = "Full Doc"
                    target_job["range"] = (1, max_pg)
                    target_job["estimated_pages"] = max_pg
                    valid_update = True
                elif "-" in new_scope_str:
                    try:
                        parts = new_scope_str.split("-")
                        if len(parts) == 2:
                            s, e = int(parts[0]), int(parts[1])
                            if 1 <= s <= e <= max_pg:
                                target_job["scope"] = "Page Range"
                                target_job["range"] = (s, e)
                                target_job["estimated_pages"] = e - s
                                valid_update = True
                            else:
                                st.toast(f"⚠️ Range {s}-{e} invalid for {target_job['file']} (Max {max_pg})", icon="❌")
                    except:
                        pass
                
                if not valid_update and new_scope_str != fmt_scope(target_job).lower():
                    st.toast(f"⚠️ Invalid format '{row['Scope Constraint']}'. Use 'Full' or 'Start-End'.", icon="❌")
            
            st.rerun()
        
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
