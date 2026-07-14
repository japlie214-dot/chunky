# views/demo/wizard.py
# "Create Search Service" — 4-page wizard.
# All job builder logic copied directly from views/refinery/tab_config.py.
# Execution logic copied from views/refinery/tab_ingestion.py.
#
# Page 1: Service Setup (role, db, schema, service name, privilege check)
# Page 2: Job Builder (file, intent, scope, target, strategy, params, add job)
# Page 3: Job Queue & Execution (review, run batch, results)
# Page 4: Placeholder

import re
import json
import time
import streamlit as st
import pandas as pd
from logger_config import log_action
from utils.constants import DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE, PAGE_WARNING_THRESHOLD

# Lazy imports (snowflake not available in local mode)

# -----------------------------------------------------------------------------
# Styled header (st.html — hybrid approach, works in Snowflake)
# -----------------------------------------------------------------------------

_HEADER_HTML = """
<style>
  .wizard-hero {{
    background: linear-gradient(135deg, {grad_start} 0%, {grad_end} 100%);
    border-radius: 14px; padding: 24px 28px; color: white;
    box-shadow: 0 6px 24px rgba(0,0,0,0.18); margin-bottom: 20px;
  }}
  .wizard-hero h2 {{ margin: 0 0 4px 0; font-size: 1.35em; }}
  .wizard-hero p  {{ margin: 0; opacity: 0.88; font-size: 0.88em; }}
  .step-badge {{
    display: inline-block; background: rgba(255,255,255,0.25);
    border-radius: 10px; padding: 2px 10px; font-size: 0.72em;
    font-weight: 700; margin-left: 8px;
  }}
</style>
<div class="wizard-hero">
  <h2>{icon} {title} <span class="step-badge">Step {step} of 4</span></h2>
  <p>{subtitle}</p>
</div>
"""

_STEP_COLORS = {1: ("#667eea", "#764ba2"), 2: ("#f093fb", "#f5576c"),
                3: ("#4facfe", "#00f2fe"), 4: ("#43e97b", "#38f9d7")}
_STEP_CONTENT = {
    1: ("⚙️", "Service Setup", "Configure the role, database, schema, and name for your Cortex Search Service."),
    2: ("📂", "Job Builder", "Select files, configure intent, scope, strategy, and parameters. Add one or more jobs."),
    3: ("🚀", "Job Queue & Execution", "Review queued jobs, run the batch, and see results."),
    4: ("✅", "Complete", "Your Cortex Search Service is ready."),
}


def _render_header(step: int):
    icon, title, subtitle = _STEP_CONTENT[step]
    g1, g2 = _STEP_COLORS[step]
    st.html(_HEADER_HTML.format(icon=icon, title=title, subtitle=subtitle,
                                 step=step, grad_start=g1, grad_end=g2))


# -----------------------------------------------------------------------------
# Pagination
# -----------------------------------------------------------------------------

def _get_page():
    return st.session_state.get("cssw_page", 1)


def _set_page(p):
    st.session_state.cssw_page = p


def _nav(can_next, next_label="Next ➡️", show_back=True):
    c1, _, c3 = st.columns([1, 2, 1])
    with c1:
        if show_back and st.button("⬅️ Back"):
            _set_page(_get_page() - 1)
            st.rerun()
    with c3:
        if st.button(next_label, disabled=not can_next, type="primary"):
            _set_page(_get_page() + 1)
            st.rerun()


# -----------------------------------------------------------------------------
# Intent presets — COPIED from views/refinery/tab_config.py
# -----------------------------------------------------------------------------

PRESET_OPTIONS = ["Add New Pages", "Replace Specific Pages", "Replace All Data"]
_MODE_SCOPE_TO_PRESET = {
    ("APPEND", "Full Doc"): "Add New Pages",
    ("SURGICAL", "Page Range"): "Replace Specific Pages",
    ("OVERWRITE", "Full Doc"): "Replace All Data",
}


def _sync_preset_to_state(preset_label):
    """Write preset's (mode, scope) into session_state — COPIED from tab_config.py."""
    mapping = {
        "Add New Pages": ("APPEND", "Full Doc"),
        "Replace Specific Pages": ("SURGICAL", "Page Range"),
        "Replace All Data": ("OVERWRITE", "Full Doc"),
    }
    mode, scope = mapping[preset_label]
    st.session_state["cssw_mode"] = mode
    st.session_state["cssw_scope"] = scope


def _derive_preset_label(mode, scope):
    """Derive preset from mode+cope — COPIED from tab_config.py."""
    return _MODE_SCOPE_TO_PRESET.get((mode, scope))


# -----------------------------------------------------------------------------
# Job Builder helpers — COPIED from views/refinery/tab_config.py
# -----------------------------------------------------------------------------

_JB_DEFAULTS = {
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
    "group": "",
    "role": "",
    "svc_name": "CSS_",
    "file_page": 1,
}


def _jb_init():
    """Initialize _jbv helper keys — COPIED pattern from tab_config.py."""
    for field, default in _JB_DEFAULTS.items():
        key = f"_jbv_{field}"
        if key not in st.session_state:
            st.session_state[key] = default


def _jbv(field):
    """Read Job Builder value from helper key — COPIED from tab_config.py."""
    return st.session_state.get(f"_jbv_{field}", _JB_DEFAULTS.get(field))


def _jbsync(field, value):
    """Sync widget value back to helper key — COPIED from tab_config.py."""
    st.session_state[f"_jbv_{field}"] = value


# -----------------------------------------------------------------------------
# Privilege check
# -----------------------------------------------------------------------------

def _check_create_css_privilege(session, db, schema):
    """Check IT_AI has CREATE CORTEX SEARCH SERVICE on schema."""
    from utils.auth_utils import APP_OWNER_ROLE
    try:
        safe_db = db.replace('"', '""')
        safe_sch = schema.replace('"', '""')
        res = session.sql(f'SHOW GRANTS ON SCHEMA "{safe_db}"."{safe_sch}"').collect()
        for row in res:
            priv = str(row["privilege"] or "").upper()
            grantee = str(row["grantee_name"] or "").upper()
            granted_on = str(row["granted_on"] or "").upper()
            if priv == "CREATE CORTEX SEARCH SERVICE" and grantee == APP_OWNER_ROLE.upper() and granted_on == "SCHEMA":
                return True, ""
        has_usage = False
        for row in res:
            if str(row["privilege"] or "").upper() == "USAGE" and str(row["grantee_name"] or "").upper() == APP_OWNER_ROLE.upper():
                has_usage = True
                break
        if not has_usage:
            return False, f"**{APP_OWNER_ROLE}** does not have USAGE privilege on `{db}.{schema}`."
        return False, f"**{APP_OWNER_ROLE}** does not have the **CREATE CORTEX SEARCH SERVICE** privilege on `{db}.{schema}`. Please select a different schema or grant the privilege to the **{APP_OWNER_ROLE}** role."
    except Exception as e:
        return False, f"Error checking privileges: {e}"


# -----------------------------------------------------------------------------
# Stage file listing — COPIED from tab_config.py
# -----------------------------------------------------------------------------

def _list_stage_files(session, stage_path):
    """List PDF files — COPIED from tab_config.py LIST pattern."""
    try:
        files = session.sql(f"LIST {stage_path} PATTERN='.*\\.pdf'").collect()
        prefix = stage_path.lstrip("@").split(".")[-1] + "/"
        result = []
        for f in files:
            fname = f["name"]
            if fname.lower().startswith(prefix.lower()):
                relative = fname[len(prefix):]
                result.append(relative if relative else fname)
            else:
                result.append(fname)
        return sorted(result)
    except Exception as e:
        log_action("STAGE_LIST_ERROR", {"stage": stage_path, "error": str(e)}, level="WARNING")
        return []


def _group_by_directory(files):
    """Group files by immediate parent directory."""
    groups = {}
    for f in files:
        d = f.rsplit("/", 1)[0] if "/" in f else "(root)"
        groups.setdefault(d, []).append(f)
    return {k: sorted(v) for k, v in sorted(groups.items())}


# -----------------------------------------------------------------------------
# Page 1: Service Setup
# -----------------------------------------------------------------------------

def _render_page_1(session):
    from utils.auth_utils import get_user_mapped_roles, get_current_user_email, APP_OWNER_ROLE
    _render_header(1)

    ctx = st.session_state.get("auth_context", {})
    db = ctx.get("db", DEFAULT_DB)
    schema = ctx.get("schema", DEFAULT_SCHEMA)
    user_email = ctx.get("user", "") or get_current_user_email() or ""

    # Role — read from _jbv, sync on change
    st.markdown("#### Select a role to create the service")
    user_roles = get_user_mapped_roles(user_email) or ["PUBLIC"]
    _role_val = _jbv("role") or user_roles[0]
    _role_idx = user_roles.index(_role_val) if _role_val in user_roles else 0
    role = st.selectbox("Role", user_roles, index=_role_idx, key="cssw_role_widget")
    if role != _role_val:
        _jbsync("role", role)

    # DB / Schema (locked)
    st.markdown("#### Service database and schema")
    c1, c2 = st.columns(2)
    c1.text_input("Database", value=db, disabled=True)
    c2.text_input("Schema", value=schema, disabled=True)
    st.caption(f"🔒 Locked to the Gatekeeper context: `{db}.{schema}`")

    # Service Name — read from _jbv, sync on change
    st.markdown("#### Service name")
    _name_val = _jbv("svc_name")
    svc_name = st.text_input("Service Name", value=_name_val, key="cssw_svc_name_widget",
                             help="Must start with CSS_ prefix.")
    if svc_name != _name_val:
        _jbsync("svc_name", svc_name)

    # Validate
    can_next = True
    if svc_name and not svc_name.startswith("CSS_"):
        st.error("❌ Must start with `CSS_`.")
        can_next = False
    elif svc_name and not re.match(r'^[A-Z_][A-Z0-9_]*$', svc_name.upper()):
        st.error("❌ Invalid characters.")
        can_next = False
    elif len(svc_name) < 5:
        st.warning("⚠️ Needs at least one character after `CSS_`.")
        can_next = False

    if can_next and svc_name:
        with st.spinner(f"Checking {APP_OWNER_ROLE} privileges..."):
            ok, err = _check_create_css_privilege(session, db, schema)
        if ok:
            st.success(f"✅ **{APP_OWNER_ROLE}** has CREATE CORTEX SEARCH SERVICE privilege.")
        else:
            st.error(f"🚫 {err}")
            can_next = False

    _nav(can_next, show_back=False)


# -----------------------------------------------------------------------------
# Page 2: Job Builder — COPIED from tab_config.py
# -----------------------------------------------------------------------------

def _render_page_2(session):
    _render_header(2)

    ctx = st.session_state.auth_context
    db, schema, stage = ctx["db"], ctx["schema"], ctx["stage"]
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

    # Group dropdown + paginated file list
    if pdf_files:
        grouped = _group_by_directory(pdf_files)
        group_names = list(grouped.keys())
        _grp_val = _jbv("group")
        _grp_idx = group_names.index(_grp_val) if _grp_val in group_names else 0
        selected_group = st.selectbox("Directory", group_names, index=_grp_idx, key="cssw_group_widget")
        if selected_group != _grp_val:
            _jbsync("group", selected_group)
            _jbsync("file_page", 1)

        group_files = grouped.get(selected_group, [])
        PAGE_SIZE = 10
        total = len(group_files)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        fp = min(_jbv("file_page"), total_pages)

        if total_pages > 1:
            fp1, fp2, fp3 = st.columns([1, 3, 1])
            with fp1:
                if st.button("◀ Prev", disabled=(fp <= 1), key="cssw_fprev"):
                    _jbsync("file_page", fp - 1); st.rerun()
            with fp2:
                st.caption(f"Page {fp} of {total_pages} ({total} files)")
            with fp3:
                if st.button("Next ▶", disabled=(fp >= total_pages), key="cssw_fnext"):
                    _jbsync("file_page", fp + 1); st.rerun()

        page_files = group_files[(fp - 1) * PAGE_SIZE : fp * PAGE_SIZE]
        _file_val = _jbv("file")
        _file_options = pdf_files if pdf_files else ["No files"]
        _file_idx = _file_options.index(_file_val) if _file_val in _file_options else 0
        sel_file = st.selectbox("Select PDF", _file_options, index=_file_idx, key="cssw_file_widget")
        if sel_file != _file_val and sel_file != "No files":
            _jbsync("file", sel_file)
    else:
        st.warning("No PDF files found.")
        sel_file = "No files"

    st.divider()

    # --- Intent pills (COPIED from tab_config.py) ---
    st.markdown("#### 📋 Job Builder")

    _current_mode = st.session_state.get("cssw_mode")
    _current_scope = st.session_state.get("cssw_scope")
    _active_preset = _derive_preset_label(_current_mode, _current_scope) if _current_mode and _current_scope else None

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

    if preset_label:
        _sync_preset_to_state(preset_label)

    # --- Job Builder UI (COPIED structure from tab_config.py) ---
    with st.container():
        jc1, jc2, jc3 = st.columns(3)

        with jc1:
            st.markdown("**📄 File & Scope**")

            _link_val = _jbv("link")
            pdf_link = st.text_input("PDF Download Link (Optional)", value=_link_val,
                                     key="cssw_link_widget",
                                     help="Reference for where to get the digital copy.")
            if pdf_link != _link_val:
                _jbsync("link", pdf_link)

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
                _ps_val = _jbv("pstart")
                _pe_val = _jbv("pend")
                p_start = c_rng1.number_input("Start", 1, max(1, page_count_est), value=_ps_val, key="cssw_pstart_widget")
                p_end = c_rng2.number_input("End", 1, max(1, page_count_est), value=min(_pe_val, page_count_est), key="cssw_pend_widget")
                if p_start != _ps_val: _jbsync("pstart", p_start)
                if p_end != _pe_val: _jbsync("pend", p_end)

        with jc2:
            st.markdown("**🎯 Target & Strategy**")

            _tbl_val = _jbv("table_name")
            target_table_name = st.text_input("Target Table Name", value=_tbl_val, key="cssw_table_widget")
            if target_table_name != _tbl_val:
                _jbsync("table_name", target_table_name)
            target_table = target_table_name

            target_table_base = target_table_name.split(".")[-1]
            from utils.snowflake_utils import get_table_schema
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
            elif mode in ["APPEND", "OVERWRITE"]:
                if tbl_exists:
                    st.info(f"ℹ️ Table exists. Data will be {mode.lower()}ed.")
                else:
                    st.warning("🆕 Table does not exist. It will be created.")
                    avail_roles = get_user_mapped_roles(ctx.get("user", ""))
                    auto_roles = [r for r in avail_roles if r.upper() != "IT_AI"]
                    default_str = auto_roles[0] if auto_roles else ""
                    grant_input = st.text_input(
                        "Grants for New Table",
                        value=_jbv("grant_roles") or default_str,
                        placeholder="e.g., IT_DS, IT_BI",
                        help="Comma-separated role names. IT_AI is automatically the owner.",
                        key="cssw_grant_widget"
                    )
                    _jbsync("grant_roles", grant_input)
                    raw_splits = re.findall(r'[^,\s"]+|"[^"]*"', grant_input)
                    grant_roles = list(dict.fromkeys(
                        r.strip().upper() for r in raw_splits
                        if r.strip() and r.strip().upper() != "IT_AI"
                    ))

            _lay_val = _jbv("layout")
            _vis_val = _jbv("vision")
            use_layout = st.checkbox("Use Layout Parser (Structural)", _lay_val, key="cssw_layout_widget")
            use_vision = st.checkbox("Use Vision Parser (Charts/Images)", _vis_val, key="cssw_vision_widget")
            if use_layout != _lay_val: _jbsync("layout", use_layout)
            if use_vision != _vis_val: _jbsync("vision", use_vision)
            if not use_layout and not use_vision:
                st.error("Select at least one strategy.")
                blocking_error = True

        with jc3:
            st.markdown("**⚙️ Parameters**")

            _chk_val = _jbv("chunk")
            chk_sz = st.number_input("Chunk Size", 1000, 30000, _chk_val, step=500,
                                     key="cssw_chunk_widget",
                                     help="Maximum characters per chunk. Chunks do not cross page boundaries.")
            if chk_sz != _chk_val: _jbsync("chunk", chk_sz)

            _ov_val = _jbv("overlap")
            overlap_pct = st.slider("Overlap %", 0, 50, _ov_val, key="cssw_overlap_widget",
                                    help="Characters repeated between adjacent chunks on the same page.")
            if overlap_pct != _ov_val: _jbsync("overlap", overlap_pct)
            overlap = int(chk_sz * (overlap_pct / 100))

            if scope == "Page Range" and p_start > p_end:
                st.error("❌ Start Page cannot be greater than End Page.")
                blocking_error = True

            if st.button("➕ Add Job", key="cssw_add", type="primary",
                         disabled=bool(blocking_error or sel_file == "No files")):
                est_pages = (p_end - p_start) + 1 if scope == "Page Range" else page_count_est
                jobs = st.session_state.get("cssw_jobs", [])
                new_id = max([j["id"] for j in jobs] + [0]) + 1
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
                    "status": "Pending",
                }
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
        st.markdown(f"#### 📊 Job Queue Workbench ({len(jobs)} jobs)")

        def fmt_scope(j):
            if j["scope"] == "Full Doc":
                return "Full"
            s, e = j["range"]
            return f"{s}-{e}"

        q_data = []
        for j in jobs:
            q_data.append({
                "selected": j.get("selected", False),
                "id": j["id"],
                "file": j["file"],
                "table": j["table"],
                "Mode": j["mode"],
                "Scope Constraint": fmt_scope(j),
                "PDF Link": j.get("link", ""),
                "Assigned Roles": ", ".join(j.get("grant_roles", [])),
                "L": j.get("layout", True),
                "V": j.get("vision", True),
                "pages": j.get("estimated_pages", 1),
                "status": j["status"],
            })

        edited_df = st.data_editor(
            pd.DataFrame(q_data),
            column_config={
                "selected": st.column_config.CheckboxColumn("Select", width="small"),
                "id": st.column_config.NumberColumn("ID", disabled=True, width="small"),
                "file": st.column_config.TextColumn("File", disabled=True, width="medium"),
                "table": st.column_config.TextColumn("Table", disabled=True, width="medium"),
                "Mode": st.column_config.SelectboxColumn("Mode", options=["APPEND", "OVERWRITE", "SURGICAL"], width="small"),
                "Scope Constraint": st.column_config.TextColumn("Scope", width="medium"),
                "PDF Link": st.column_config.TextColumn("PDF Link", width="medium"),
                "Assigned Roles": st.column_config.TextColumn("Roles", width="medium"),
                "L": st.column_config.CheckboxColumn("L", width="small"),
                "V": st.column_config.CheckboxColumn("V", width="small"),
                "pages": st.column_config.NumberColumn("Pages", disabled=True, width="small"),
                "status": st.column_config.TextColumn("Status", disabled=True, width="small"),
            },
            use_container_width=True, hide_index=True, key="cssw_job_editor"
        )

        # Sync edits back (COPIED from tab_config.py)
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
                max_pg = 1
                if tgt["file"] in st.session_state.get("file_metadata_cache", {}):
                    max_pg = st.session_state.file_metadata_cache[tgt["file"]]["page_count"]
                if new_scope_str in ["full", "full doc", "all"]:
                    tgt["scope"] = "Full Doc"
                    tgt["range"] = (1, max_pg)
                    tgt["estimated_pages"] = max_pg
                elif "-" in new_scope_str:
                    try:
                        parts = new_scope_str.split("-")
                        s, e = int(parts[0]), int(parts[1])
                        if 1 <= s <= e <= max_pg:
                            tgt["scope"] = "Page Range"
                            tgt["range"] = (s, e)
                            tgt["estimated_pages"] = e - s
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
                st.session_state.cssw_jobs = []
                st.rerun()

    can_next = len(jobs) > 0
    _nav(can_next)


# -----------------------------------------------------------------------------
# Page 3: Job Queue & Execution — COPIED from tab_ingestion.py
# -----------------------------------------------------------------------------

def _render_page_3(session):
    _render_header(3)

    ctx = st.session_state.auth_context
    db, schema, stage = ctx["db"], ctx["schema"], ctx["stage"]
    stage_path = f"@{db}.{schema}.{stage}"

    svc_name = _jbv("svc_name")
    role = _jbv("role")
    jobs = st.session_state.get("cssw_jobs", [])

    if not jobs:
        st.warning("No jobs queued. Go back to Step 2 and add jobs.")
        _nav(can_next=False)
        return

    # --- Summary (reads from _jbv helper keys — always current) ---
    st.markdown("#### 📋 Configuration Summary")
    sc1, sc2 = st.columns(2)
    with sc1:
        st.markdown(f"- **Service Name:** `{svc_name}`")
        st.markdown(f"- **Owner Role:** `{role}`")
        st.markdown(f"- **Location:** `{db}.{schema}`")
    with sc2:
        total_pages = sum(j["estimated_pages"] for j in jobs)
        st.markdown(f"- **Total Jobs:** {len(jobs)}")
        st.markdown(f"- **Total Pages:** {total_pages}")
        files_str = ", ".join(f"`{j['file']}`" for j in jobs)
        st.markdown(f"- **Files:** {files_str}")

    st.divider()

    # Per-job detail
    st.markdown("#### 📦 Job Details")
    for j in jobs:
        s, e = j["range"]
        scope_str = j["scope"] if j["scope"] == "Full Doc" else f"Pages {s}–{e}"
        strat = []
        if j["layout"]: strat.append("Layout")
        if j["vision"]: strat.append("Vision")
        with st.expander(f"Job #{j['id']}: `{j['file']}` → `{j['table']}` ({j['status']})"):
            dc1, dc2, dc3 = st.columns(3)
            with dc1:
                st.markdown(f"**Mode:** {j['mode']}")
                st.markdown(f"**Scope:** {scope_str}")
                st.markdown(f"**Pages:** {j['estimated_pages']}")
            with dc2:
                st.markdown(f"**Strategy:** {' + '.join(strat)}")
                st.markdown(f"**Chunk Size:** {j['params'][0]:,}")
                st.markdown(f"**Overlap:** {j['params'][1]}")
            with dc3:
                st.markdown(f"**Status:** {j['status']}")
                if j.get("link"):
                    st.markdown(f"**Link:** {j['link']}")
                if j.get("grant_roles"):
                    st.markdown(f"**Roles:** {', '.join(j['grant_roles'])}")

    st.divider()

    # --- Execution (COPIED from tab_ingestion.py) ---
    st.markdown("#### 🚀 Execute")

    if "batch_in_progress" not in st.session_state:
        st.session_state.batch_in_progress = False
    if "cancel_batch" not in st.session_state:
        st.session_state.cancel_batch = False

    batch_started = st.session_state.get("cssw_batch_started", False)

    if not batch_started and not st.session_state.batch_in_progress:
        pending_pages = sum(j.get("estimated_pages", 0) for j in jobs if j.get("status") not in ["Completed", "Completed with Warnings", "Failed", "Cancelled"])
        if pending_pages > PAGE_WARNING_THRESHOLD:
            st.warning(f"⚠️ You have {pending_pages} pages queued. Large batches can overwhelm manual QA.")
        if st.button("🚀 Run Batch Execution", type="primary"):
            if "job_queue" not in st.session_state:
                st.session_state.job_queue = []
            existing_ids = {j["id"] for j in st.session_state.job_queue}
            for j in jobs:
                if j["id"] not in existing_ids:
                    st.session_state.job_queue.append(j)
            st.session_state.cssw_batch_started = True
            st.session_state.batch_in_progress = True
            st.session_state.cancel_batch = False
            st.session_state.batch_metrics = {
                "jobs_completed": 0, "jobs_failed": 0, "jobs_warning": 0, "jobs_cancelled": 0,
                "total_pages": 0, "total_chunks": 0,
                "layout_pages_processed": 0, "vision_pages_processed": 0,
                "layout_pages_list": set(), "vision_pages_list": set(),
                "standard_chunks": 0, "enhanced_chunks": 0,
                "total_time": 0.0, "time_layout": 0.0, "time_vision": 0.0,
                "credits_layout": 0.0, "credits_vision": 0.0, "enhancement_breakdown": {},
            }
            st.session_state.batch_start_time = time.time()
            st.rerun()

    if st.session_state.batch_in_progress:
        has_pending = any(j["status"] not in ["Completed", "Completed with Warnings", "Failed", "Cancelled"]
                         for j in st.session_state.get("job_queue", []))
        if has_pending:
            st.warning("⚠️ Batch in progress. Click Stop to halt after the current job.")
            if st.button("🛑 Stop Batch"):
                st.session_state.cancel_batch = True
                st.rerun()
        from views.refinery.batch_processor import run_batch_execution
        try:
            run_batch_execution(session, db, schema, stage_path)
        except Exception as e:
            st.error(f"Batch runner failed: {e}")
            st.session_state.batch_in_progress = False

    # Results
    if batch_started and not st.session_state.batch_in_progress:
        st.divider()
        st.markdown("#### 📊 Results")
        for wj in jobs:
            for gj in st.session_state.get("job_queue", []):
                if gj["id"] == wj["id"]:
                    wj["status"] = gj.get("status", wj["status"])
                    wj["metrics"] = gj.get("metrics", {})

        completed = sum(1 for j in jobs if j["status"] == "Completed")
        failed = sum(1 for j in jobs if j["status"] == "Failed")
        warnings = sum(1 for j in jobs if j["status"] == "Completed with Warnings")
        if failed > 0:
            st.error(f"⚠️ {failed} job(s) failed.")
        elif warnings > 0:
            st.warning(f"⚠️ {completed} completed, {warnings} with warnings.")
        else:
            st.success(f"🎉 All {completed} job(s) completed!")

        for j in jobs:
            jm = j.get("metrics", {})
            status = j["status"]
            icon = {"Completed": "✅", "Failed": "❌", "Completed with Warnings": "⚠️"}.get(status, "ℹ️")
            with st.expander(f"{icon} Job #{j['id']}: `{j['file']}` — {status}"):
                if jm:
                    rc1, rc2, rc3, rc4 = st.columns(4)
                    rc1.metric("Pages", jm.get("pages", 0))
                    rc2.metric("Chunks", jm.get("standard_cnt", 0) + jm.get("enhanced_cnt", 0))
                    rc3.metric("Duration", f"{jm.get('duration', 0):.1f}s")
                    rc4.metric("Status", status)
                    if jm.get("error"):
                        st.error(f"Error: {jm['error']}")

        _nav(can_next=True, next_label="Next ➡️")
    elif not batch_started:
        st.info("Click **Run Batch Execution** to start.")
        _nav(can_next=False)
    else:
        _nav(can_next=False)


# -----------------------------------------------------------------------------
# Page 4: Placeholder
# -----------------------------------------------------------------------------

def _render_page_4(session):
    _render_header(4)
    st.info("🚧 Reserved for future functionality.")
    if st.button("⬅️ Back to Start"):
        for key in list(st.session_state.keys()):
            if key.startswith("cssw_") or key.startswith("_jbv_"):
                del st.session_state[key]
        _set_page(1)
        st.rerun()


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------

def render_demo_search_service():
    """Main entry point for the 'Create Search Service' wizard."""
    st.title("🌐 Demo: Create Search Service")
    log_action("NAVIGATE", "Visited Create Search Service Wizard")

    _jb_init()

    if "cssw_page" not in st.session_state:
        st.session_state.cssw_page = 1
    if "cssw_jobs" not in st.session_state:
        st.session_state.cssw_jobs = []

    page = _get_page()
    st.progress(page / 4, text=f"Step {page} of 4")

    from utils.snowflake_utils import get_snowpark_session
    session = get_snowpark_session()

    if page == 1:
        _render_page_1(session)
    elif page == 2:
        _render_page_2(session)
    elif page == 3:
        _render_page_3(session)
    elif page == 4:
        _render_page_4(session)
    else:
        _set_page(1)
        st.rerun()
