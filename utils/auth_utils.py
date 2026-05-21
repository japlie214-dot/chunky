# utils/auth_utils.py
import streamlit as st
import time
import json
from snowflake.snowpark.context import get_active_session
from utils.constants import DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE

# -----------------------------------------------------------------------------
# 1. CONSTANTS & MAPPINGS
# -----------------------------------------------------------------------------
ADMIN_CONTACT = "ALVIN.LIE@JAPFA.COM"
APP_OWNER_ROLE = "IT_AI"
# Hardcoded App ID as requested
# App ID - using updated Streamlit ID for PROD
APP_ID_QUERY = f'execute streamlit "{DEFAULT_DB}"."{DEFAULT_SCHEMA}"."VOAKUWUPRAAUK1BU"()'

# Identity Map: Email -> [Potential Roles]
# Priority 1 for Identity Check
USER_ROLE_MAP = {
    "alvin.lie@japfa.com": ["IT_AI", "IT_DS", "IT_CSSWEB_AI"],
    "jordan.gani@japfa.com": ["IT_DS"],
    "evan.santosa@japfa.com": ["IT_AI", "IT_CSSWEB_AI"],
    "widji.nugroho@japfa.com": ["IT_CCP_AI"],
    "weny.dwijayanti@japfa.com": ["USER_CCP_DOC_AI"],
    "anastasia.irviana@japfa.com": ["USER_CCP_DOC_AI"],
    # Fallback/Admin
    "admin@japfa.com": ["ACCOUNTADMIN", "IT_AI"]
}

# Stage Access Map: "DB.SCHEMA.STAGE" -> [Allowed Roles]
# Priority 1 for Stage Verification
STAGE_ACCESS_MAP = {
    "SBOX_DB.AI_SB.DOCS": ["IT_AI", "IT_BI", "IT_DS", "IT_CSSWEB_AI", "IT_CCP_AI", "USER_CCP_DOC_AI"],
    f"{DEFAULT_DB}.{DEFAULT_SCHEMA}.{DEFAULT_STAGE}": ["IT_AI", "IT_BI", "IT_DS", "IT_CSSWEB_AI", "IT_CCP_AI", "USER_CCP_DOC_AI"]
}

# -----------------------------------------------------------------------------
# 2. CORE LOGIC
# -----------------------------------------------------------------------------
def get_current_user_email():
    """Safely retrieve user email from Streamlit context or secrets."""
    if st.user and st.user.get("email"):
        return st.user.get("email")
    if "user" in st.secrets and "email" in st.secrets["user"]:
        return st.secrets["user"]["email"]
    return None

def get_user_mapped_roles(email):
    """
    Returns the mapped roles for the user, or ['PUBLIC'] if none are found.
    
    Args:
        email: User email address
        
    Returns:
        List of uppercase role names
    """
    if not email:
        return ["PUBLIC"]
    roles = USER_ROLE_MAP.get(email.lower(), [])
    return [r.upper() for r in roles] if roles else ["PUBLIC"]

def get_authorized_roles_for_stage(session, db, schema, stage):
    """
    Step A: Check Hardcoded Map (Fastest)
    Step B: Call Stored Procedure (Dynamic)
    Returns (List[Roles], ErrorMessage)
    """
    key = f"{db}.{schema}.{stage}".upper()
    
    # 1. Map Shortcut
    if key in STAGE_ACCESS_MAP:
        return STAGE_ACCESS_MAP[key], None
        
    # 2. Dynamic Check via SP
    try:
        sql = "CALL SBOX_DB.AI_SB.GET_ROLES_WITH_STAGE_ACCESS(?, ?, ?)"
        res = session.sql(sql, params=[db, schema, stage]).collect()
        
        if not res or res[0][0] is None:
            return [], None

        # 1. Extract the raw JSON from the first column of the first row
        raw_json = res[0][0]
        
        # 2. Parse the JSON (handle if it comes as a string or already as a dict)
        if isinstance(raw_json, str):
            data_dict = json.loads(raw_json)
        else:
            data_dict = raw_json
        
        # 3. Navigate the specific structure: direct_grants -> data
        roles_set = set()
        if "direct_grants" in data_dict and "data" in data_dict["direct_grants"]:
            rows = data_dict["direct_grants"]["data"]
            for row in rows:
                # Based on your output, 'grantee_name' holds the role
                role = row.get("grantee_name")
                if role:
                    roles_set.add(role.upper())
        
        authorized_roles = list(roles_set)
        return authorized_roles, None
        
    except Exception as e:
        err_msg = str(e)
        # Catch common object errors
        if "does not exist" in err_msg or "authorized" in err_msg.lower():
            friendly_err = (
                f"❌ Object Not Found or Access Denied.\n"
                f"Please ensure `{db}.{schema}.{stage}` exists.\n"
                f"Also verify that the app owner role `{APP_OWNER_ROLE}` has usage rights on this object."
            )
            return [], friendly_err
        return [], f"System Error parsing stage access: {err_msg}"

def logout():
    """Clears authentication context."""
    if "auth_context" in st.session_state:
        del st.session_state["auth_context"]
    if "messages" in st.session_state:
        st.session_state.messages = []
    st.rerun()

def resolve_active_target_role(session, email):
    """
    Resolve the target role for grant execution based on user mapping.
    No longer uses QUERY_HISTORY scanning - relies solely on USER_ROLE_MAP.
    
    Returns: UPPERCASE role name (ready for SQL execution with double-quotes)
    """
    roles = get_user_mapped_roles(email)
    return roles[0]

# -----------------------------------------------------------------------------
# 3. UI COMPONENT
# -----------------------------------------------------------------------------
def render_login_screen(session):
    """Renders the blocking login form with animated status steps."""
    st.markdown("## 🛡️ Chunky Gatekeeper")
    st.info("Please connect to a secure Stage to proceed.")
    
    user_email = get_current_user_email()
    if user_email:
        st.caption(f"Identified as: `{user_email}`")
    else:
        st.warning("⚠️ Could not identify user email. (Local Dev?)")

    with st.form("gatekeeper_form"):
        c1, c2, c3 = st.columns(3)
        db_in = c1.text_input("Database", value=DEFAULT_DB, key="gk_db")
        sch_in = c2.text_input("Schema", value=DEFAULT_SCHEMA, key="gk_schema")
        stg_in = c3.text_input("Stage", value=DEFAULT_STAGE, key="gk_stage")
        
        submitted = st.form_submit_button("🔌 Connect & Verify")
    
    if submitted:
        # Normalize Inputs
        db = db_in.strip().upper()
        sch = sch_in.strip().upper()
        stg = stg_in.strip().upper()
        
        if not user_email:
            st.error("Authentication Impossible: User Email is missing.")
            return

        with st.status("🔐 Authenticating...", expanded=True) as status:
            # -------------------------------------------------
            # STAGE PHYSICAL EXISTENCE CHECK
            # Runs BEFORE get_authorized_roles_for_stage so the
            # hardcoded STAGE_ACCESS_MAP shortcut cannot mask a
            # physically deleted stage.
            # -------------------------------------------------
            status.write("🔍 Verifying Stage Existence...")
            time.sleep(0.3)
            try:
                # Use quoted identifiers to handle special characters, spaces,
                # or names starting with numbers (robustness/correctness).
                safe_db = db.replace('"', '""')
                safe_sch = sch.replace('"', '""')
                safe_stg = stg.replace('"', '""')
                session.sql(f'DESCRIBE STAGE "{safe_db}"."{safe_sch}"."{safe_stg}"').collect()
                status.write(f"✅ Stage `{db}.{sch}.{stg}` exists.")
            except Exception as e:
                status.update(label="❌ Stage Not Found", state="error")
                st.error(
                    f"Stage `{db}.{sch}.{stg}` not found or insufficient access.\n\n"
                    f"Details: {e}"
                )
                return

            # STAGE AUTHORIZATION CHECK
            status.write(f"📦 Retrieving authorized roles for `{db}.{sch}.{stg}`...")
            authorized_roles, err = get_authorized_roles_for_stage(session, db, sch, stg)

            if err:
                status.update(label="❌ Object Error", state="error")
                st.error(err)
                return

            if not authorized_roles:
                status.update(label="❌ Access Error", state="error")
                st.error(f"No authorized roles found for `{stg}`. Is the stage empty or restricted?")
                st.markdown(f"Ensure role **{APP_OWNER_ROLE}** has access to this stage.")
                return

            status.write(f"📋 Authorized Roles: {authorized_roles}")

            # IDENTITY VERIFICATION
            status.write("👤 Checking Identity Map...")
            time.sleep(0.3)

            my_roles = USER_ROLE_MAP.get(user_email.lower(), [])

            if my_roles:
                status.write(f"✅ Found in Map: {my_roles}")
            else:
                status.write("⚠️ Not in Map. Assigning PUBLIC role...")
                my_roles = ["PUBLIC"]

            # INTERSECTION CHECK
            status.write("🔗 Matching Permissions...")
            time.sleep(0.3)

            my_roles_upper = [r.upper() for r in my_roles]
            auth_roles_upper = [r.upper() for r in authorized_roles]

            common_roles = set(my_roles_upper).intersection(set(auth_roles_upper))

            if common_roles:
                status.update(label="✅ Authentication Successful!", state="complete")
                time.sleep(1)

                st.session_state.auth_context = {
                    "db": db,
                    "schema": sch,
                    "stage": stg,
                    "user": user_email,
                    "role": list(common_roles)[0]
                }

                if "config" not in st.session_state:
                    st.session_state.config = {}
                st.session_state.config["db"] = db
                st.session_state.config["schema"] = sch
                st.session_state.config["stage"] = stg
                st.session_state.config["user_id"] = user_email

                st.rerun()

            else:
                status.update(label="⛔ Insufficient Permissions", state="error")
                st.error(
                    "The stage exists, but you do not hold any of the roles "
                    "authorized to access it."
                )
                st.warning(f"Your Roles: {my_roles}")
                st.warning(f"Required (any of): {authorized_roles}")
                st.markdown(f"Contact **{ADMIN_CONTACT}** for assistance.")
                return
