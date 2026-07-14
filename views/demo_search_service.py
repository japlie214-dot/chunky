# views/demo_search_service.py
# "Create Search Service" — guided 4-page wizard for creating a Cortex Search Service.
# Uses hybrid approach: st.html() for styled headers + native widgets for input.
#
# Page 1: Service Setup (role, database, schema, service name)
# Page 2: Data Source & Configuration (stage files, refinery params)
# Page 3: Confirmation & Execution (review, run batch)
# Page 4: Placeholder (empty for now)

import re
import json
import time
import streamlit as st
from logger_config import log_action
from utils.constants import DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE

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
    2: ("📂", "Data Source & Configuration",
        "Select a PDF from the stage and configure ingestion parameters."),
    3: ("🚀", "Confirm & Execute",
        "Review your configuration and run the ingestion job."),
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
        # Query the grants on the schema to check for CREATE CORTEX SEARCH SERVICE
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
        # Also check USAGE at minimum
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
    """List PDF files in the stage, returning relative paths (stripped stage prefix)."""
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
    """Group files by their directory path."""
    groups: dict[str, list[str]] = {}
    for f in files:
        if "/" in f:
            dir_name = f.rsplit("/", 1)[0]
        else:
            dir_name = "(root)"
        groups.setdefault(dir_name, []).append(f)
    # Sort groups and files within groups
    return {k: sorted(v) for k, v in sorted(groups.items())}


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

    role = st.selectbox(
        "Role",
        options=user_roles,
        index=0,
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
        value=st.session_state.get("cssw_svc_name", "CSS_"),
        key="cssw_svc_name",
        help="Must start with CSS_ prefix. This will be the Cortex Search Service identifier.",
    )
    # Validate CSS_ prefix
    if svc_name and not svc_name.startswith("CSS_"):
        st.error("❌ Service name must start with the `CSS_` prefix.")
        can_next = False
    elif svc_name and not re.match(r'^[A-Z_][A-Z0-9_]*$', svc_name.upper()):
        st.error("❌ Service name contains invalid characters. Use only letters, numbers, and underscores.")
        can_next = False
    elif len(svc_name) < 5:  # CSS_ + at least 1 char
        st.warning("⚠️ Service name needs at least one character after `CSS_`.")
        can_next = False
    else:
        can_next = True

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
# Page 2: Data Source & Configuration
# -----------------------------------------------------------------------------

def _render_page_2(session):
    """Page 2: Select PDF from stage, configure ingestion parameters."""
    _render_header(2)

    ctx = st.session_state.get("auth_context", {})
    db = ctx.get("db", DEFAULT_DB)
    schema = ctx.get("schema", DEFAULT_SCHEMA)
    stage = ctx.get("stage", DEFAULT_STAGE)
    stage_path = f"@{db}.{schema}.{stage}"

    # --- Stage content display ---
    st.markdown("#### 📂 Select data to be indexed")
    st.caption(f"Stage: `{stage_path}`")

    pdf_files = _list_stage_files(session, stage_path)
    if not pdf_files:
        st.warning("No PDF files found in the stage. Please add PDFs and try again.")
        _nav_buttons(can_next=False)
        return

    # Group by directory and display as checklist
    grouped = _group_by_directory(pdf_files)
    selected_file = st.session_state.get("cssw_selected_file", "")

    # Use radio for single selection (user can select one PDF only)
    all_files_flat = []
    for dir_name, files in grouped.items():
        all_files_flat.extend(files)

    # Build display options with directory grouping
    file_options = []
    for dir_name, files in grouped.items():
        for f in files:
            display = f"📁 {dir_name}/  →  📄 {f.split('/')[-1]}" if dir_name != "(root)" else f"📄 {f}"
            file_options.append((display, f))

    display_labels = [o[0] for o in file_options]
    file_values = [o[1] for o in file_options]

    # Find current index
    current_idx = 0
    if selected_file in file_values:
        current_idx = file_values.index(selected_file)

    chosen_display = st.radio(
        "Select a PDF to ingest",
        options=display_labels,
        index=current_idx,
        key="cssw_file_radio",
    )
    chosen_file = file_values[display_labels.index(chosen_display)]
    st.session_state.cssw_selected_file = chosen_file

    st.markdown("---")

    # --- Doc Refinery config (translated to wizard UI) ---
    st.markdown("#### ⚙️ Ingestion Configuration")

    # Scope
    c1, c2 = st.columns(2)
    with c1:
        scope = st.radio(
            "Scope",
            options=["Full Doc", "Page Range"],
            horizontal=True,
            key="cssw_scope",
        )

    # Detect page count for the selected file
    page_count = st.session_state.get("cssw_page_count", 1)
    if chosen_file:
        cache_key = f"cssw_pc_{chosen_file}"
        if cache_key not in st.session_state:
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
        else:
            page_count = st.session_state[cache_key]

    st.caption(f"📄 Detected **{page_count}** pages in `{chosen_file}`")

    p_start, p_end = 1, page_count
    if scope == "Page Range":
        with c2:
            cr1, cr2 = st.columns(2)
            p_start = cr1.number_input("Start Page", 1, max(1, page_count), value=1, key="cssw_pstart")
            p_end = cr2.number_input("End Page", 1, max(1, page_count), value=page_count, key="cssw_pend")
            if p_start > p_end:
                st.error("❌ Start page cannot exceed end page.")

    st.markdown("---")

    # Strategy
    st.markdown("#### 🎯 Extraction Strategy")
    s1, s2 = st.columns(2)
    use_layout = s1.checkbox("Layout Parser (structural)", value=True, key="cssw_layout")
    use_vision = s2.checkbox("Vision Parser (charts/images)", value=True, key="cssw_vision")
    if not use_layout and not use_vision:
        st.error("Select at least one strategy.")

    st.markdown("---")

    # Parameters
    st.markdown("#### ⚙️ Chunk Parameters")
    p1, p2 = st.columns(2)
    chunk_size = p1.number_input(
        "Chunk Size (chars)", 1000, 30000,
        value=st.session_state.get("cssw_chunk_size", 8000),
        step=500, key="cssw_chunk_size",
        help="Maximum characters per chunk. Chunks do not cross page boundaries.",
    )
    overlap_pct = p2.slider(
        "Overlap %", 0, 50,
        value=st.session_state.get("cssw_overlap_pct", 20),
        key="cssw_overlap_pct",
        help="Characters repeated between adjacent chunks on the same page.",
    )

    # Can proceed?
    can_next = bool(chosen_file) and (use_layout or use_vision)
    if scope == "Page Range" and p_start > p_end:
        can_next = False

    _nav_buttons(can_next)


# -----------------------------------------------------------------------------
# Page 3: Confirmation & Execution
# -----------------------------------------------------------------------------

def _render_page_3(session):
    """Page 3: Review configuration, execute ingestion."""
    from utils.snowflake_utils import get_table_schema
    _render_header(3)

    ctx = st.session_state.get("auth_context", {})
    db = ctx.get("db", DEFAULT_DB)
    schema = ctx.get("schema", DEFAULT_SCHEMA)
    stage = ctx.get("stage", DEFAULT_STAGE)
    stage_path = f"@{db}.{schema}.{stage}"

    svc_name = st.session_state.get("cssw_svc_name", "CSS_")
    role = st.session_state.get("cssw_role", "")
    chosen_file = st.session_state.get("cssw_selected_file", "")
    scope = st.session_state.get("cssw_scope", "Full Doc")
    use_layout = st.session_state.get("cssw_layout", True)
    use_vision = st.session_state.get("cssw_vision", True)
    chunk_size = st.session_state.get("cssw_chunk_size", 8000)
    overlap_pct = st.session_state.get("cssw_overlap_pct", 20)
    p_start = st.session_state.get("cssw_pstart", 1)
    p_end = st.session_state.get("cssw_pend", 1)
    page_count = st.session_state.get(f"cssw_pc_{chosen_file}", 1)

    # --- Summary ---
    st.markdown("#### 📋 Configuration Summary")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Service**")
        st.markdown(f"- **Name:** `{svc_name}`")
        st.markdown(f"- **Owner Role:** `{role}`")
        st.markdown(f"- **Location:** `{db}.{schema}`")

    with c2:
        st.markdown("**Data Source**")
        st.markdown(f"- **File:** `{chosen_file}`")
        st.markdown(f"- **Scope:** {scope}" + (f" (pages {p_start}–{p_end})" if scope == "Page Range" else ""))
        st.markdown(f"- **Pages:** {page_count}")

    st.markdown("---")

    c3, c4 = st.columns(2)
    with c3:
        st.markdown("**Strategy**")
        strategies = []
        if use_layout: strategies.append("✅ Layout Parser")
        if use_vision: strategies.append("✅ Vision Parser")
        st.markdown("\n".join(f"- {s}" for s in strategies) if strategies else "- ❌ None selected")

    with c4:
        st.markdown("**Parameters**")
        overlap = int(chunk_size * (overlap_pct / 100))
        st.markdown(f"- **Chunk Size:** {chunk_size:,} chars")
        st.markdown(f"- **Overlap:** {overlap_pct}% ({overlap} chars)")

    st.markdown("---")

    # --- Target table info ---
    target_table = svc_name.upper()  # Use service name as table name
    st.markdown(f"#### 🗄️ Target Table: `{target_table}`")

    tbl_exists, _, _ = get_table_schema(session, db, schema, target_table)
    if tbl_exists:
        st.info(f"ℹ️ Table `{target_table}` already exists. Data will be **appended**.")
    else:
        st.info(f"🆕 Table `{target_table}` will be created.")

    # --- Execution ---
    st.markdown("---")
    st.markdown("#### 🚀 Execute")

    # Initialize batch state if not present
    if "batch_in_progress" not in st.session_state:
        st.session_state.batch_in_progress = False
    if "cancel_batch" not in st.session_state:
        st.session_state.cancel_batch = False
    if "job_queue" not in st.session_state:
        st.session_state.job_queue = []

    # Check if job already added for this wizard run
    wizard_job_added = st.session_state.get("cssw_job_added", False)

    if not st.session_state.batch_in_progress and not wizard_job_added:
        if st.button("🚀 Create & Ingest", type="primary", disabled=not chosen_file):
            # Build the job
            est_pages = (p_end - p_start + 1) if scope == "Page Range" else page_count
            new_id = max([j["id"] for j in st.session_state.job_queue] + [0]) + 1

            job_data = {
                "id": new_id,
                "file": chosen_file,
                "table": target_table,
                "mode": "APPEND",
                "scope": scope,
                "range": (p_start, p_end),
                "estimated_pages": est_pages,
                "layout": use_layout,
                "vision": use_vision,
                "params": (chunk_size, overlap),
                "grant_roles": [role] if role and role.upper() != "IT_AI" else [],
                "link": "",
                "status": "Pending",
            }

            st.session_state.job_queue.append(job_data)
            st.session_state.cssw_job_added = True
            log_action("CSSW_JOB_ADDED", {
                "service": svc_name, "file": chosen_file,
                "table": target_table, "pages": est_pages,
            })

            # Start batch
            st.session_state.batch_in_progress = True
            st.session_state.cancel_batch = False
            from utils.constants import LAYOUT_COST_PER_1K_PAGES
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
        from views.refinery.batch_processor import run_batch_execution
        try:
            run_batch_execution(session, db, schema, stage_path)
        except Exception as e:
            st.error(f"Execution failed: {e}")
            st.session_state.batch_in_progress = False

    # Show results if batch finished
    if wizard_job_added and not st.session_state.batch_in_progress:
        # Check the job status
        wizard_job = None
        for j in st.session_state.job_queue:
            if j.get("table", "").upper() == target_table and j.get("file") == chosen_file:
                wizard_job = j
                break

        if wizard_job:
            status = wizard_job.get("status", "Unknown")
            if status == "Completed":
                st.success(f"🎉 Ingestion completed successfully! Table `{target_table}` is ready.")
            elif status == "Completed with Warnings":
                st.warning(f"⚠️ Ingestion completed with warnings. Check the details below.")
            elif status == "Failed":
                st.error(f"❌ Ingestion failed. Check the error details.")
            else:
                st.info(f"ℹ️ Job status: {status}")

            # Show metrics
            jm = wizard_job.get("metrics", {})
            if jm:
                mc1, mc2, mc3, mc4 = st.columns(4)
                mc1.metric("Pages", jm.get("pages", 0))
                mc2.metric("Chunks", jm.get("standard_cnt", 0) + jm.get("enhanced_cnt", 0))
                duration = jm.get("duration", 0)
                mc3.metric("Duration", f"{duration:.1f}s")
                mc4.metric("Status", status)

        # Next button (enabled after execution)
        _nav_buttons(can_next=True, next_label="Next ➡️")
    elif not wizard_job_added:
        st.info("Click **Create & Ingest** to start the ingestion process.")
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
        # Reset wizard state
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

    # Initialize session state
    if "cssw_page" not in st.session_state:
        st.session_state.cssw_page = 1

    # Progress bar
    page = _get_page()
    st.progress(page / 4, text=f"Step {page} of 4")

    # Get session (works for both Snowflake and local mode)
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
