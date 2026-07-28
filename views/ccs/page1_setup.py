# views/ccs/page1_setup.py
# Page 1: Service Setup — role, database, schema, service name, privilege check.
#
# IMPORTANT: Widget keys (cssw_role, cssw_svc_name) are NOT reliable across
# page navigation — Streamlit clears them when the widget isn't rendered.
# Use _wiz_* storage keys that persist independently, and ALWAYS sync after
# every widget interaction.

import re
import streamlit as st
from views.ccs.common import render_header, nav_buttons, ctx
from utils.constants import DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE


def _wiz_get(key, default=""):
    """Read from persistent wizard storage key."""
    return st.session_state.get(f"_wiz_{key}", default)


def _wiz_set(key, value):
    """Write to persistent wizard storage key."""
    st.session_state[f"_wiz_{key}"] = value


def _check_privileges(session, db, schema, stage):
    """
    Validate all privileges IT_AI needs for the full ingestion pipeline.

    SQL operations performed during ingestion:
      LIST @stage                   → READ on internal stage, USAGE on external stage
      AI_PARSE_DOCUMENT(TO_FILE(@stage/...)) → READ on internal stage, USAGE on external stage
      session.file.put()            → WRITE on internal stage (QA image uploads)
      CREATE TABLE ... CHANGE_TRACKING=TRUE → CREATE TABLE on schema
      INSERT/SELECT/DELETE/UPDATE/TRUNCATE/DROP → table owner (automatic)
      GRANT ALL ON TABLE ... TO ROLE → table owner (automatic)
      BEGIN/COMMIT/ROLLBACK         → no special privilege

    Stage privilege rules (per Snowflake docs):
      - USAGE: only applies to external stages
      - READ: only applies to internal stages (required for LIST, GET, TO_FILE)
      - WRITE: only applies to internal stages (required for PUT)
      - OWNERSHIP: grants full access to any stage type

    Returns (ok: bool, error_message: str).
    """
    from utils.auth_utils import APP_OWNER_ROLE
    try:
        safe_db = db.replace('"', '""')
        safe_sch = schema.replace('"', '""')
        safe_stg = stage.replace('"', '""')

        # --- Schema privileges ---
        schema_res = session.sql(f'SHOW GRANTS ON SCHEMA "{safe_db}"."{safe_sch}"').collect()
        schema_privs = set()
        for row in schema_res:
            if str(row["grantee_name"] or "").upper() == APP_OWNER_ROLE.upper():
                schema_privs.add(str(row["privilege"] or "").upper())

        required_schema = {"USAGE", "CREATE TABLE", "CREATE CORTEX SEARCH SERVICE"}
        missing_schema = required_schema - schema_privs

        # --- Stage privileges ---
        stage_privs = set()
        try:
            stage_res = session.sql(f'SHOW GRANTS ON STAGE "{safe_db}"."{safe_sch}"."{safe_stg}"').collect()
            for row in stage_res:
                if str(row["grantee_name"] or "").upper() == APP_OWNER_ROLE.upper():
                    stage_privs.add(str(row["privilege"] or "").upper())
        except Exception:
            pass

        missing_stage = set()
        # Per Snowflake docs: USAGE only applies to external stages,
        # READ/WRITE only applies to internal stages. OWNERSHIP covers all.
        has_stage_access = (
            "OWNERSHIP" in stage_privs
            or "READ" in stage_privs
            or "USAGE" in stage_privs
        )
        if not has_stage_access:
            missing_stage.add("READ on stage (internal) or USAGE on stage (external)")

        # --- Report ---
        all_missing = sorted(missing_schema | missing_stage)
        if not schema_privs:
            return False, (
                f"**{APP_OWNER_ROLE}** has no privileges on `{db}.{schema}`. "
                f"Please grant USAGE, CREATE TABLE, and CREATE CORTEX SEARCH SERVICE."
            )
        if all_missing:
            return False, (
                f"**{APP_OWNER_ROLE}** is missing: **{', '.join(all_missing)}** "
                f"on `{db}.{schema}`. Please grant these privileges."
            )
        return True, ""
    except Exception as e:
        return False, f"Error checking privileges: {e}"


def render(session):
    from utils.auth_utils import get_user_mapped_roles, get_current_user_email, APP_OWNER_ROLE
    render_header(1)

    c = ctx()
    db = c.get("db", DEFAULT_DB)
    schema = c.get("schema", DEFAULT_SCHEMA)
    stage = c.get("stage", DEFAULT_STAGE)
    user_email = c.get("user", "") or get_current_user_email() or ""

    # --- Role ---
    # Storage key: _wiz_role (persists across page navigation)
    # Widget key: cssw_role_select (only alive when this page renders)
    st.markdown("#### Select a role to create the service")
    user_roles = get_user_mapped_roles(user_email) or ["PUBLIC"]

    saved_role = _wiz_get("role")
    if not saved_role or saved_role not in user_roles:
        saved_role = user_roles[0]
    _role_idx = user_roles.index(saved_role)

    selected_role = st.selectbox("Role", user_roles, index=_role_idx, key="cssw_role_select")
    # ALWAYS sync to persistent storage — not just on change
    _wiz_set("role", selected_role)

    # --- DB / Schema ---
    st.markdown("#### Service database and schema")
    c1, c2 = st.columns(2)
    c1.text_input("Database", value=db, disabled=True)
    c2.text_input("Schema", value=schema, disabled=True)
    st.caption(f"🔒 Locked to the Gatekeeper context: `{db}.{schema}`")

    # --- Service Name ---
    # Storage key: _wiz_svc_name (persists across page navigation)
    # Widget key: cssw_svc_name_input (only alive when this page renders)
    st.markdown("#### Service name")
    saved_name = _wiz_get("svc_name", "CSS_")
    entered_name = st.text_input("Service Name", value=saved_name, key="cssw_svc_name_input",
                                 help="Must start with CSS_ prefix.")
    # ALWAYS sync to persistent storage
    _wiz_set("svc_name", entered_name)

    # --- Validate ---
    can_next = True
    if entered_name and not entered_name.startswith("CSS_"):
        st.error("❌ Must start with `CSS_`."); can_next = False
    elif entered_name and not re.match(r'^[A-Z_][A-Z0-9_]*$', entered_name.upper()):
        st.error("❌ Invalid characters."); can_next = False
    elif len(entered_name) < 5:
        st.warning("⚠️ Needs at least one character after `CSS_`."); can_next = False

    # --- Privilege check ---
    if can_next and entered_name:
        with st.spinner(f"Checking {APP_OWNER_ROLE} privileges..."):
            ok, err = _check_privileges(session, db, schema, stage)
        if ok:
            st.success(f"✅ **{APP_OWNER_ROLE}** has all required privileges (USAGE, CREATE TABLE, CREATE CORTEX SEARCH SERVICE, stage access via READ/USAGE/OWNERSHIP).")
        else:
            st.error(f"🚫 {err}"); can_next = False

    nav_buttons(can_next, show_back=False)
