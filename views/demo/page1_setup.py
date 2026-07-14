# views/demo/page1_setup.py
# Page 1: Service Setup — role, database, schema, service name, privilege check.

import re
import streamlit as st
from views.demo.common import render_header, nav_buttons, ctx
from utils.constants import DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE


def _check_privileges(session, db, schema, stage):
    """
    Validate all privileges IT_AI needs for the full ingestion pipeline.

    SQL operations performed during ingestion:
      LIST @stage                   → USAGE on stage
      SHOW GRANTS ON SCHEMA         → USAGE on schema
      AI_PARSE_DOCUMENT(TO_FILE(@stage/...)) → USAGE on stage
      CREATE TABLE ... CHANGE_TRACKING=TRUE → CREATE TABLE on schema
      INSERT/SELECT/DELETE/UPDATE/TRUNCATE/DROP → table owner (automatic)
      GRANT ALL ON TABLE ... TO ROLE → table owner (automatic)
      BEGIN/COMMIT/ROLLBACK         → no special privilege

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
            # If stage doesn't exist or can't query, we'll catch it below
            pass

        missing_stage = set()
        if "USAGE" not in stage_privs and "OWNERSHIP" not in stage_privs:
            missing_stage.add("USAGE on stage")

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

    # Role — widget key is the source of truth
    st.markdown("#### Select a role to create the service")
    user_roles = get_user_mapped_roles(user_email) or ["PUBLIC"]

    # Initialize widget key once (not _jbv — widget key is the source of truth)
    if "cssw_role" not in st.session_state:
        st.session_state.cssw_role = user_roles[0]

    # If current value not in available roles, reset
    if st.session_state.cssw_role not in user_roles:
        st.session_state.cssw_role = user_roles[0]

    _role_idx = user_roles.index(st.session_state.cssw_role)
    role = st.selectbox("Role", user_roles, index=_role_idx, key="cssw_role")

    # DB / Schema
    st.markdown("#### Service database and schema")
    c1, c2 = st.columns(2)
    c1.text_input("Database", value=db, disabled=True)
    c2.text_input("Schema", value=schema, disabled=True)
    st.caption(f"🔒 Locked to the Gatekeeper context: `{db}.{schema}`")

    # Service Name — widget key is the source of truth
    st.markdown("#### Service name")
    if "cssw_svc_name" not in st.session_state:
        st.session_state.cssw_svc_name = "CSS_"

    svc_name = st.text_input("Service Name", key="cssw_svc_name",
                             help="Must start with CSS_ prefix.")

    # Validate
    can_next = True
    if svc_name and not svc_name.startswith("CSS_"):
        st.error("❌ Must start with `CSS_`."); can_next = False
    elif svc_name and not re.match(r'^[A-Z_][A-Z0-9_]*$', svc_name.upper()):
        st.error("❌ Invalid characters."); can_next = False
    elif len(svc_name) < 5:
        st.warning("⚠️ Needs at least one character after `CSS_`."); can_next = False

    # Privilege check
    if can_next and svc_name:
        with st.spinner(f"Checking {APP_OWNER_ROLE} privileges..."):
            ok, err = _check_privileges(session, db, schema, stage)
        if ok:
            st.success(f"✅ **{APP_OWNER_ROLE}** has all required privileges (USAGE, CREATE TABLE, CREATE CORTEX SEARCH SERVICE, stage access).")
        else:
            st.error(f"🚫 {err}"); can_next = False

    nav_buttons(can_next, show_back=False)
