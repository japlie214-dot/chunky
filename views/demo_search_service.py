# views/demo_search_service.py
# "Create Search Service" — guided 4-page wizard for creating a Cortex Search Service.
# Uses hybrid approach: st.html() for styled headers + native widgets for input.
#
# Page 1: Service Setup (role, database, schema, service name)
# Page 2: Job Builder (file selection, intent, scope, strategy, params, add jobs)
# Page 3: Job Queue & Execution (review all jobs, run batch, see results)
# Page 4: Placeholder (empty for now)

import re
import json
import time
import streamlit as st
import pandas as pd
from logger_config import log_action
from utils.constants import DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE, PAGE_WARNING_THRESHOLD

# Lazy imports: auth_utils and snowflake_utils import snowflake.snowpark at module
# level, which fails in local mode. Import them inside functions instead.

# -----------------------------------------------------------------------------
# Styled headers (st.html — the hybrid approach that works in Snowflake)
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

_STEP_COLORS = {
    1: ("#667eea", "#764ba2"),
    2: ("#f093fb", "#f5576c"),
    3: ("#4facfe", "#00f2fe"),
    4: ("#43e97b", "#38f9d7"),
}

_STEP_CONTENT = {
    1: ("⚙️", "Service Setup",
        "Configure the role, database, schema, and name for your new Cortex Search Service."),
    2: ("📂", "Job Builder",
        "Select files, configure intent, scope, strategy, and parameters. Add one or more jobs."),
    3: ("🚀", "Job Queue & Execution",
        "Review queued jobs, run the batch, and see results."),
    4: ("✅", "Complete",
        "Your Cortex Search Service is ready."),
}


def _render_header(step: int):
    """Render the styled step header via st.html()."""
    icon, title, subtitle = _STEP_CONTENT[step]
    grad_start, grad_end = _STEP_COLORS[step]
    st.html(_HEADER_HTML.format(
        icon=icon, title=title, subtitle=subtitle,
        step=step, grad_start=grad_start, grad_end=grad_end,
    ))


# -----------------------------------------------------------------------------
# Session state initialization (call ONCE, before any widget renders)
# -----------------------------------------------------------------------------

def _init_defaults():
    """
    Initialize all wizard session state keys ONCE using setdefault.
    After this, widget keys are the source of truth — never overwrite them.
    """
    defaults = {
        "cssw_page": 1,
        # Page 1
        "cssw_role": "",
        "cssw_svc_name": "CSS_",
        # Page 2 — Job Builder
        "cssw_selected_group": "",
        "cssw_selected_file": "",
        "cssw_job_intent": "Add New Pages",
        "cssw_scope": "Full Doc",
        "cssw_target_table": "SUS_CHUNKS",
        "cssw_write_mode": "APPEND",
        "cssw_layout": True,
        "cssw_vision": True,
        "cssw_chunk_size": 8000,
        "cssw_overlap_pct": 20,
        "cssw_pstart": 1,
        "cssw_pend": 1,
        "cssw_link": "",
        # Page 2 — Job queue (wizard-local, separate from global job_queue)
        "cssw_jobs": [],
        # Page 3
        "cssw_batch_started": False,
    }
    for key, val in defaults.items():
        st.session_state.setdefault(key, val)


# -----------------------------------------------------------------------------
# Pagination helpers
# -----------------------------------------------------------------------------

def _get_page() -> int:
    return st.session_state.get("cssw_page", 1)


def _set_page(page: int):
    st.session_state.cssw_page = page


def _nav_buttons(can_next: bool, next_label: str = "Next ➡️", show_back: bool = True):
    """Render Back / Next buttons in a row."""
    c1, c2, c3 = st.columns([1, 2, 1])
    with c1:
        if show_back and st.button("⬅️ Back"):
            _set_page(_get_page() - 1)
            st.rerun()
    with c3:
        if st.button(next_label, disabled=not can_next, type="primary"):
            _set_page(_get_page() + 1)
            st.rerun()


# -----------------------------------------------------------------------------
# Privilege check
# -----------------------------------------------------------------------------

def _check_create_css_privilege(session, db: str, schema: str) -> tuple[bool, str]:
    """
    Check if IT_AI (the app owner role) has CREATE CORTEX SEARCH SERVICE
    privilege on the given schema.

    Returns (ok, error_message).
    """
    from utils.auth_utils import APP_OWNER_ROLE
    try:
        safe_db = db.replace('"', '""')
        safe_sch = schema.replace('"', '""')
        sql = f'SHOW GRANTS ON SCHEMA "{safe_db}"."{safe_sch}"'
        res = session.sql(sql).collect()
        for row in res:
            privilege = str(row["privilege"] or "").upper()
            grantee = str(row["grantee_name"] or "").upper()
            granted_on = str(row["granted_on"] or "").upper()
            if (
                privilege == "CREATE CORTEX SEARCH SERVICE"
                and grantee == APP_OWNER_ROLE.upper()
                and granted_on == "SCHEMA"
            ):
                return True, ""
        has_usage = False
        for row in res:
            privilege = str(row["privilege"] or "").upper()
            grantee = str(row["grantee_name"] or "").upper()
            if privilege == "USAGE" and grantee == APP_OWNER_ROLE.upper():
                has_usage = True
                break
        if not has_usage:
            return False, (
                f"**{APP_OWNER_ROLE}** does not have USAGE privilege on "
                f"`{db}.{schema}`. Please select a different schema or "
                f"grant USAGE to the **{APP_OWNER_ROLE}** role."
            )
        return False, (
            f"**{APP_OWNER_ROLE}** does not have the **CREATE CORTEX SEARCH SERVICE** "
            f"privilege on `{db}.{schema}`. Please select a different schema or grant "
            f"the privilege to the **{APP_OWNER_ROLE}** role."
        )
    except Exception as e:
        err = str(e)
        if "does not exist" in err.lower() or "not found" in err.lower():
            return False, f"Schema `{db}.{schema}` not found. Please verify the database and schema."
        return False, f"Error checking privileges: {err}"


# -----------------------------------------------------------------------------
# Stage file listing
# -----------------------------------------------------------------------------

def _list_stage_files(session, stage_path: str) -> list[str]:
    """List PDF files in the stage, returning relative paths."""
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


def _group_by_directory(files: list[str]) -> dict[str, list[str]]:
    """Group files by their immediate parent directory."""
    groups: dict[str, list[str]] = {}
    for f in files:
        if "/" in f:
            dir_name = f.rsplit("/", 1)[0]
        else:
            dir_name = "(root)"
        groups.setdefault(dir_name, []).append(f)
    return {k: sorted(v) for k, v in sorted(groups.items())}


# -----------------------------------------------------------------------------
# Intent presets (mirrors tab_config.py)
# -----------------------------------------------------------------------------

_PRESET_OPTIONS = ["Add New Pages", "Replace Specific Pages", "Replace All Data"]
_PRESET_TO_MODE_SCOPE = {
    "Add New Pages": ("APPEND", "Full Doc"),
    "Replace Specific Pages": ("SURGICAL", "Page Range"),
    "Replace All Data": ("OVERWRITE", "Full Doc"),
}


# -----------------------------------------------------------------------------
# Page 1: Service Setup
# -----------------------------------------------------------------------------

def _render_page_1(session):
    """Page 1: Role, Database, Schema, Service Name."""
    from utils.auth_utils import get_user_mapped_roles, get_current_user_email, APP_OWNER_ROLE
    _render_header(1)

    ctx = st.session_state.get("auth_context", {})
    db = ctx.get("db", DEFAULT_DB)
    schema = ctx.get("schema", DEFAULT_SCHEMA)
    user_email = ctx.get("user", "") or get_current_user_email() or ""

    # --- Role selection ---
    st.markdown("#### Select a role to create the service")
    user_roles = get_user_mapped_roles(user_email)
    if not user_roles:
        user_roles = ["PUBLIC"]

    # Initialize default role once
    if not st.session_state.cssw_role:
        st.session_state.cssw_role = user_roles[0]

    # Find current index from persisted state
    current_role = st.session_state.cssw_role
    role_idx = user_roles.index(current_role) if current_role in user_roles else 0

    role = st.selectbox(
        "Role",
        options=user_roles,
        index=role_idx,
        key="cssw_role",
        help="The role that will own and manage the Cortex Search Service.",
    )

    # --- Database & Schema (locked to gate context) ---
    st.markdown("#### Service database and schema")
    c1, c2 = st.columns(2)
    with c1:
        st.text_input("Database", value=db, disabled=True, key="cssw_db_display")
    with c2:
        st.text_input("Schema", value=schema, disabled=True, key="cssw_schema_display")

    st.caption(f"🔒 Locked to the Gatekeeper context: `{db}.{schema}`")

    # --- Service Name ---
    st.markdown("#### Service name")
    svc_name = st.text_input(
        "Service Name",
        key="cssw_svc_name",
        help="Must start with CSS_ prefix. This will be the Cortex Search Service identifier.",
    )

    # Validate
    can_next = True
    if svc_name and not svc_name.startswith("CSS_"):
        st.error("❌ Service name must start with the `CSS_` prefix.")
        can_next = False
    elif svc_name and not re.match(r'^[A-Z_][A-Z0-9_]*$', svc_name.upper()):
        st.error("❌ Service name contains invalid characters. Use only letters, numbers, and underscores.")
        can_next = False
    elif len(svc_name) < 5:
        st.warning("⚠️ Service name needs at least one character after `CSS_`.")
        can_next = False

    # --- Privilege check ---
    if can_next and svc_name:
        with st.spinner(f"Checking {APP_OWNER_ROLE} privileges on `{db}.{schema}`..."):
            ok, err_msg = _check_create_css_privilege(session, db, schema)
        if ok:
            st.success(f"✅ **{APP_OWNER_ROLE}** has CREATE CORTEX SEARCH SERVICE privilege on `{db}.{schema}`.")
        else:
            st.error(f"🚫 {err_msg}")
            can_next = False

    _nav_buttons(can_next, show_back=False)


# -----------------------------------------------------------------------------
# Page 2: Job Builder
# -----------------------------------------------------------------------------

def _render_page_2(session):
    """Page 2: Full job builder — file selection, intent, scope, strategy, params, add job."""
    _render_header(2)

    ctx = st.session_state.get("auth_context", {})
    db = ctx.get("db", DEFAULT_DB)
    schema = ctx.get("schema", DEFAULT_SCHEMA)
    stage = ctx.get("stage", DEFAULT_STAGE)
    stage_path = f"@{db}.{schema}.{stage}"

    # --- File Selection with group dropdown + pagination ---
    st.markdown("#### 📂 Select data to be indexed")
    st.caption(f"Stage: `{stage_path}`")

    pdf_files = _list_stage_files(session, stage_path)
    if not pdf_files:
        st.warning("No PDF files found in the stage.")
        _nav_buttons(can_next=False)
        return

    grouped = _group_by_directory(pdf_files)
    group_names = list(grouped.keys())

    # Group dropdown
    current_group = st.session_state.cssw_selected_group
    if current_group not in group_names:
        current_group = group_names[0]
        st.session_state.cssw_selected_group = current_group

    selected_group = st.selectbox(
        "Directory",
        options=group_names,
        index=group_names.index(current_group),
        key="cssw_selected_group",
        help="Filter files by directory.",
    )

    # Files in selected group with pagination
    group_files = grouped.get(selected_group, [])
    PAGE_SIZE = 10
    total_files = len(group_files)
    total_pages = max(1, (total_files + PAGE_SIZE - 1) // PAGE_SIZE)

    if "cssw_file_page" not in st.session_state:
        st.session_state.cssw_file_page = 1
    file_page = st.session_state.cssw_file_page
    if file_page > total_pages:
        file_page = total_pages
        st.session_state.cssw_file_page = file_page

    # Pagination controls
    if total_pages > 1:
        fp1, fp2, fp3 = st.columns([1, 3, 1])
        with fp1:
            if st.button("◀ Prev", disabled=(file_page <= 1), key="cssw_fp_prev"):
                st.session_state.cssw_file_page = file_page - 1
                st.rerun()
        with fp2:
            st.caption(f"Page {file_page} of {total_pages} ({total_files} files)")
        with fp3:
            if st.button("Next ▶", disabled=(file_page >= total_pages), key="cssw_fp_next"):
                st.session_state.cssw_file_page = file_page + 1
                st.rerun()

    # Show files for current page
    start_idx = (file_page - 1) * PAGE_SIZE
    end_idx = min(start_idx + PAGE_SIZE, total_files)
    page_files = group_files[start_idx:end_idx]

    # Build display labels
    file_display_map = {}
    for f in page_files:
        fname = f.split("/")[-1]
        label = f"📄 {fname}"
        file_display_map[label] = f

    display_labels = list(file_display_map.keys())
    current_file = st.session_state.cssw_selected_file
    current_file_label = None
    for label, path in file_display_map.items():
        if path == current_file:
            current_file_label = label
            break

    radio_idx = display_labels.index(current_file_label) if current_file_label in display_labels else 0

    chosen_label = st.radio(
        "Select a PDF",
        options=display_labels,
        index=radio_idx,
        key="cssw_file_radio",
    )
    chosen_file = file_display_map.get(chosen_label, "")
    st.session_state.cssw_selected_file = chosen_file

    st.markdown("---")

    # --- Job Intent ---
    st.markdown("#### 📋 Job Intent")
    intent = st.pills(
        "Intent",
        options=_PRESET_OPTIONS,
        selection_mode="single",
        default=st.session_state.cssw_job_intent,
        key="cssw_job_intent",
        help="Select an intent to auto-configure write mode and scope.",
    )

    # Sync intent → mode + scope
    if intent and intent in _PRESET_TO_MODE_SCOPE:
        mode, scope = _PRESET_TO_MODE_SCOPE[intent]
        st.session_state.cssw_write_mode = mode
        st.session_state.cssw_scope = scope
    else:
        mode = st.session_state.cssw_write_mode
        scope = st.session_state.cssw_scope

    st.markdown("---")

    # --- Scope ---
    st.markdown("#### 📐 Scope")
    scope = st.radio(
        "Scope",
        options=["Full Doc", "Page Range"],
        horizontal=True,
        key="cssw_scope",
    )

    # Page count detection
    page_count = 1
    if chosen_file:
        cache_key = f"cssw_pc_{chosen_file}"
        if cache_key in st.session_state:
            page_count = st.session_state[cache_key]
        else:
            try:
                safe_file = chosen_file.replace("'", "''")
                safe_stage = stage_path.replace("'", "''")
                parse_opts = json.dumps({"mode": "LAYOUT", "page_split": True})
                parse_sql = f"""
                    SELECT SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(
                        TO_FILE('{safe_stage}', '{safe_file}'),
                        PARSE_JSON('{parse_opts}')
                    ) AS J
                """
                res = session.sql(parse_sql).collect()
                if res and res[0]["J"]:
                    doc = json.loads(res[0]["J"])
                    page_count = doc.get("metadata", {}).get("pageCount", len(doc.get("pages", []))) or 1
                st.session_state[cache_key] = page_count
            except Exception as e:
                log_action("CSS_PAGE_COUNT_ERROR", {"file": chosen_file, "error": str(e)}, level="WARNING")
                page_count = st.session_state.get(cache_key, 1)

    st.caption(f"📄 Detected **{page_count}** pages")

    p_start, p_end = 1, page_count
    if scope == "Page Range":
        sp1, sp2 = st.columns(2)
        p_start = sp1.number_input("Start Page", 1, max(1, page_count), key="cssw_pstart")
        p_end = sp2.number_input("End Page", 1, max(1, page_count), key="cssw_pend")
        if p_start > p_end:
            st.error("❌ Start page cannot exceed end page.")

    st.markdown("---")

    # --- Target Table ---
    st.markdown("#### 🗄️ Target")
    target_table = st.text_input("Target Table Name", key="cssw_target_table")

    st.markdown("---")

    # --- Strategy ---
    st.markdown("#### 🎯 Strategy")
    st1, st2 = st.columns(2)
    use_layout = st1.checkbox("Layout Parser (structural)", key="cssw_layout")
    use_vision = st2.checkbox("Vision Parser (charts/images)", key="cssw_vision")
    if not use_layout and not use_vision:
        st.error("Select at least one strategy.")

    st.markdown("---")

    # --- Parameters ---
    st.markdown("#### ⚙️ Parameters")
    pr1, pr2 = st.columns(2)
    chunk_size = pr1.number_input(
        "Chunk Size (chars)", 1000, 30000, step=500,
        key="cssw_chunk_size",
        help="Maximum characters per chunk. Chunks do not cross page boundaries.",
    )
    overlap_pct = pr2.slider(
        "Overlap %", 0, 50,
        key="cssw_overlap_pct",
        help="Characters repeated between adjacent chunks on the same page.",
    )

    st.markdown("---")

    # --- PDF Link ---
    link = st.text_input(
        "PDF Download Link (Optional)",
        key="cssw_link",
        help="Reference for where to get the digital copy of the PDF.",
    )

    st.markdown("---")

    # --- Add Job ---
    blocking = (not use_layout and not use_vision) or (not chosen_file) or (not target_table)
    if scope == "Page Range" and p_start > p_end:
        blocking = True

    if st.button("➕ Add Job", type="primary", disabled=blocking):
        est_pages = (p_end - p_start + 1) if scope == "Page Range" else page_count
        new_id = max([j["id"] for j in st.session_state.cssw_jobs] + [0]) + 1

        job_data = {
            "id": new_id,
            "file": chosen_file,
            "table": target_table.upper(),
            "mode": mode,
            "scope": scope,
            "range": (p_start, p_end),
            "estimated_pages": est_pages,
            "layout": use_layout,
            "vision": use_vision,
            "params": (chunk_size, int(chunk_size * overlap_pct / 100)),
            "link": link,
            "status": "Pending",
        }
        st.session_state.cssw_jobs.append(job_data)
        st.success(f"✅ Job #{new_id} added: `{chosen_file}` → `{target_table.upper()}`")
        log_action("CSSW_JOB_ADDED", {"id": new_id, "file": chosen_file, "table": target_table})
        st.rerun()

    # --- Job Queue Workbench ---
    if st.session_state.cssw_jobs:
        st.markdown("---")
        st.markdown(f"#### 📊 Job Queue ({len(st.session_state.cssw_jobs)} jobs)")

        q_data = []
        for j in st.session_state.cssw_jobs:
            s, e = j["range"]
            q_data.append({
                "ID": j["id"],
                "File": j["file"],
                "Table": j["table"],
                "Mode": j["mode"],
                "Scope": j["scope"] if j["scope"] == "Full Doc" else f"{s}-{e}",
                "L": j["layout"],
                "V": j["vision"],
                "Pages": j["estimated_pages"],
                "Status": j["status"],
            })

        st.dataframe(pd.DataFrame(q_data), use_container_width=True, hide_index=True)

        jc1, jc2 = st.columns(2)
        with jc1:
            if st.button("🗑️ Clear All Jobs"):
                st.session_state.cssw_jobs = []
                st.rerun()

    # Navigation — can proceed if at least one job is queued
    can_next = len(st.session_state.cssw_jobs) > 0
    _nav_buttons(can_next)


# -----------------------------------------------------------------------------
# Page 3: Job Queue & Execution
# -----------------------------------------------------------------------------

def _render_page_3(session):
    """Page 3: Review all jobs, run batch, see results."""
    from utils.snowflake_utils import get_table_schema
    _render_header(3)

    ctx = st.session_state.get("auth_context", {})
    db = ctx.get("db", DEFAULT_DB)
    schema = ctx.get("schema", DEFAULT_SCHEMA)
    stage = ctx.get("stage", DEFAULT_STAGE)
    stage_path = f"@{db}.{schema}.{stage}"

    svc_name = st.session_state.cssw_svc_name
    role = st.session_state.cssw_role
    jobs = st.session_state.cssw_jobs

    if not jobs:
        st.warning("No jobs queued. Go back to Step 2 and add jobs.")
        _nav_buttons(can_next=False)
        return

    # --- Summary ---
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
        files = ", ".join(f"`{j['file']}`" for j in jobs)
        st.markdown(f"- **Files:** {files}")

    st.markdown("---")

    # Per-job detail
    st.markdown("#### 📦 Job Details")
    for j in jobs:
        s, e = j["range"]
        scope_str = j["scope"] if j["scope"] == "Full Doc" else f"Pages {s}–{e}"
        strat = []
        if j["layout"]: strat.append("Layout")
        if j["vision"]: strat.append("Vision")
        overlap = j["params"][1]
        with st.expander(f"Job #{j['id']}: `{j['file']}` → `{j['table']}` ({j['status']})"):
            jc1, jc2, jc3 = st.columns(3)
            with jc1:
                st.markdown(f"**Mode:** {j['mode']}")
                st.markdown(f"**Scope:** {scope_str}")
                st.markdown(f"**Pages:** {j['estimated_pages']}")
            with jc2:
                st.markdown(f"**Strategy:** {' + '.join(strat)}")
                st.markdown(f"**Chunk Size:** {j['params'][0]:,}")
                st.markdown(f"**Overlap:** {overlap}")
            with jc3:
                st.markdown(f"**Status:** {j['status']}")
                if j.get("link"):
                    st.markdown(f"**Link:** {j['link']}")

    st.markdown("---")

    # --- Execution ---
    st.markdown("#### 🚀 Execute")

    if "batch_in_progress" not in st.session_state:
        st.session_state.batch_in_progress = False
    if "cancel_batch" not in st.session_state:
        st.session_state.cancel_batch = False

    # Merge wizard jobs into global job_queue for the batch processor
    if not st.session_state.cssw_batch_started and not st.session_state.batch_in_progress:
        if st.button("🚀 Run Batch Execution", type="primary"):
            # Push wizard jobs into global job_queue
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
                "jobs_completed": 0, "jobs_failed": 0, "jobs_warning": 0,
                "jobs_cancelled": 0,
                "total_pages": 0, "total_chunks": 0,
                "layout_pages_processed": 0, "vision_pages_processed": 0,
                "layout_pages_list": set(), "vision_pages_list": set(),
                "standard_chunks": 0, "enhanced_chunks": 0,
                "total_time": 0.0, "time_layout": 0.0, "time_vision": 0.0,
                "credits_layout": 0.0, "credits_vision": 0.0,
                "enhancement_breakdown": {},
            }
            st.session_state.batch_start_time = time.time()
            st.rerun()

    # Run batch (one-job-per-rerun driver)
    if st.session_state.batch_in_progress:
        st.warning("⚠️ Batch in progress. Click Stop to halt after the current job.")
        if st.button("🛑 Stop Batch"):
            st.session_state.cancel_batch = True
            st.rerun()

        from views.refinery.batch_processor import run_batch_execution
        try:
            run_batch_execution(session, db, schema, stage_path)
        except Exception as e:
            st.error(f"Execution failed: {e}")
            st.session_state.batch_in_progress = False

    # Show results
    if st.session_state.cssw_batch_started and not st.session_state.batch_in_progress:
        st.markdown("---")
        st.markdown("#### 📊 Results")

        # Update wizard jobs from global job_queue
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
            st.success(f"🎉 All {completed} job(s) completed successfully!")

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
                else:
                    st.caption("No metrics available.")

        _nav_buttons(can_next=True, next_label="Next ➡️")
    elif not st.session_state.cssw_batch_started:
        st.info("Click **Run Batch Execution** to start processing.")
        _nav_buttons(can_next=False)
    else:
        _nav_buttons(can_next=False)


# -----------------------------------------------------------------------------
# Page 4: Placeholder
# -----------------------------------------------------------------------------

def _render_page_4(session):
    """Page 4: Empty placeholder for future functionality."""
    _render_header(4)
    st.info("🚧 This page is reserved for future functionality.")
    st.markdown("Coming soon: service status monitoring, query testing, and more.")

    if st.button("⬅️ Back to Start"):
        for key in list(st.session_state.keys()):
            if key.startswith("cssw_"):
                del st.session_state[key]
        _set_page(1)
        st.rerun()


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------

def render_demo_search_service():
    """
    Main entry point for the 'Create Search Service' wizard.
    4-page guided flow using hybrid approach (st.html + native widgets).
    """
    st.title("🌐 Demo: Create Search Service")
    log_action("NAVIGATE", "Visited Create Search Service Wizard")

    # Initialize all defaults ONCE
    _init_defaults()

    # Progress bar
    page = _get_page()
    st.progress(page / 4, text=f"Step {page} of 4")

    # Get session (lazy — works in both Snowflake and local mode)
    from utils.snowflake_utils import get_snowpark_session
    session = get_snowpark_session()

    # Route to current page
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
