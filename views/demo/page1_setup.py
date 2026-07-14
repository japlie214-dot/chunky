# views/demo/page1_setup.py
# Page 1: Service Setup — role, database, schema, service name, privilege check.

import re
import streamlit as st
from views.demo.common import render_header, nav_buttons, ctx, jbv, jbsync
from utils.constants import DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE


def _check_create_css_privilege(session, db, schema):
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
        has_usage = any(
            str(r["privilege"] or "").upper() == "USAGE" and str(r["grantee_name"] or "").upper() == APP_OWNER_ROLE.upper()
            for r in res
        )
        if not has_usage:
            return False, f"**{APP_OWNER_ROLE}** does not have USAGE privilege on `{db}.{schema}`."
        return False, f"**{APP_OWNER_ROLE}** does not have the **CREATE CORTEX SEARCH SERVICE** privilege on `{db}.{schema}`."
    except Exception as e:
        return False, f"Error checking privileges: {e}"


def render(session):
    from utils.auth_utils import get_user_mapped_roles, get_current_user_email, APP_OWNER_ROLE
    render_header(1)

    c = ctx()
    db = c.get("db", DEFAULT_DB)
    schema = c.get("schema", DEFAULT_SCHEMA)
    user_email = c.get("user", "") or get_current_user_email() or ""

    # Role
    st.markdown("#### Select a role to create the service")
    user_roles = get_user_mapped_roles(user_email) or ["PUBLIC"]
    _role_val = jbv("role")
    # If no role saved yet, default to first available role
    if not _role_val or _role_val not in user_roles:
        _role_val = user_roles[0]
        jbsync("role", _role_val)
    _role_idx = user_roles.index(_role_val)
    role = st.selectbox("Role", user_roles, index=_role_idx, key="cssw_role_widget")
    # ALWAYS sync — not just on change
    jbsync("role", role)

    # DB / Schema
    st.markdown("#### Service database and schema")
    c1, c2 = st.columns(2)
    c1.text_input("Database", value=db, disabled=True)
    c2.text_input("Schema", value=schema, disabled=True)
    st.caption(f"🔒 Locked to the Gatekeeper context: `{db}.{schema}`")

    # Service Name
    st.markdown("#### Service name")
    _name_val = jbv("svc_name")
    svc_name = st.text_input("Service Name", value=_name_val, key="cssw_svc_name_widget",
                             help="Must start with CSS_ prefix.")
    # ALWAYS sync
    jbsync("svc_name", svc_name)

    # Validate
    can_next = True
    if svc_name and not svc_name.startswith("CSS_"):
        st.error("❌ Must start with `CSS_`."); can_next = False
    elif svc_name and not re.match(r'^[A-Z_][A-Z0-9_]*$', svc_name.upper()):
        st.error("❌ Invalid characters."); can_next = False
    elif len(svc_name) < 5:
        st.warning("⚠️ Needs at least one character after `CSS_`."); can_next = False

    if can_next and svc_name:
        with st.spinner(f"Checking {APP_OWNER_ROLE} privileges..."):
            ok, err = _check_create_css_privilege(session, db, schema)
        if ok:
            st.success(f"✅ **{APP_OWNER_ROLE}** has CREATE CORTEX SEARCH SERVICE privilege.")
        else:
            st.error(f"🚫 {err}"); can_next = False

    nav_buttons(can_next, show_back=False)
