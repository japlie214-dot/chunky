# utils/auth_utils.py
import streamlit as st
import time
from snowflake.snowpark.context import get_active_session

# -----------------------------------------------------------------------------
# 1. CONSTANTS & MAPPINGS
# -----------------------------------------------------------------------------
ADMIN_CONTACT = "ALVIN.LIE@JAPFA.COM"
APP_OWNER_ROLE = "IT_AI"
# Hardcoded App ID as requested
APP_ID_QUERY = 'execute streamlit "SBOX_DB"."AI_SB"."FH0KFJX9MLH_RZBK"()'

# Identity Map: Email -> [Potential Roles]
# Priority 1 for Identity Check
USER_ROLE_MAP = {
    "alvin.lie@japfa.com": ["IT_AI", "IT_DS"],
    "jordan.gani@japfa.com": ["IT_DS"],
    # Fallback/Admin
    "admin@japfa.com": ["ACCOUNTADMIN", "IT_AI"]
}

# Stage Access Map: "DB.SCHEMA.STAGE" -> [Allowed Roles]
# Priority 1 for Stage Verification
STAGE_ACCESS_MAP = {
    "SBOX_DB.AI_SB.DOCS": ["IT_AI", "IT_BI", "IT_DS", "IT_CSSWEB_AI"]
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

def get_active_role_from_history(session, user_email):
    """
    Fallback: Check INFORMATION_SCHEMA.QUERY_HISTORY for the specific running app query.
    Returns a list containing the single active role if found, else empty list.
    """
    try:
        # Strict hardcoded query text to identify the active user session
        sql = """
        SELECT USER_NAME, ROLE_NAME
        FROM INFORMATION_SCHEMA.QUERY_HISTORY
        WHERE QUERY_TEXT = ?
          AND EXECUTION_STATUS = 'RUNNING'
        ORDER BY START_TIME DESC
        LIMIT 100
        """
        rows = session.sql(sql, params=[APP_ID_QUERY]).collect()
        
        # Match USER_NAME to email (Case insensitive matching)
        for row in rows:
            if row['USER_NAME'] and row['USER_NAME'].upper() == user_email.upper():
                return [row['ROLE_NAME']]
                
        return []
    except Exception as e:
        # If permission issues or local dev (where history might be empty/inaccessible)
        # return empty to trigger the rejection flow
        return []

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
        # Assumes GET_ROLES_WITH_STAGE_ACCESS exists in the path or fully qualified
        # returns TABLE(ROLE_NAME, PRIVILEGES)
        sql = "CALL GET_ROLES_WITH_STAGE_ACCESS(?, ?, ?)"
        res = session.sql(sql, params=[db, schema, stage]).collect()
        
        authorized_roles = [row['ROLE_NAME'].upper() for row in res]
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
        return [], f"System Error verifying stage: {err_msg}"

def logout():
    """Clears authentication context."""
    if "auth_context" in st.session_state:
        del st.session_state["auth_context"]
    if "messages" in st.session_state:
        st.session_state.messages = []
    st.rerun()

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
        db_in = c1.text_input("Database", value="SBOX_DB")
        sch_in = c2.text_input("Schema", value="AI_SB")
        stg_in = c3.text_input("Stage", value="DOCS")
        
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
            # STEP 1: IDENTITY VERIFICATION
            # -------------------------------------------------
            status.write("👤 Checking Identity Map...")
            time.sleep(0.3) # Small UX delay
            
            my_roles = USER_ROLE_MAP.get(user_email, [])
            
            if my_roles:
                status.write(f"✅ Found in Map: {my_roles}")
            else:
                status.write("⚠️ Not in Map. Checking Query History...")
                my_roles = get_active_role_from_history(session, user_email)
                if my_roles:
                     status.write(f"✅ Active Role Detected: {my_roles}")
                else:
                    status.update(label="❌ Access Denied", state="error")
                    st.error(f"⛔ Authorization Failed for `{user_email}`.")
                    st.markdown(f"Please contact **{ADMIN_CONTACT}** to be granted access.")
                    return

            # -------------------------------------------------
            # STEP 2: STAGE VERIFICATION
            # -------------------------------------------------
            status.write(f"📦 Verifying Access to `{db}.{sch}.{stg}`...")
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

            # -------------------------------------------------
            # STEP 3: INTERSECTION CHECK
            # -------------------------------------------------
            status.write("🔗 Matching Permissions...")
            time.sleep(0.3)
            
            my_roles_upper = [r.upper() for r in my_roles]
            auth_roles_upper = [r.upper() for r in authorized_roles]
            
            common_roles = set(my_roles_upper).intersection(set(auth_roles_upper))
            
            if common_roles:
                status.update(label="✅ Authentication Successful!", state="complete")
                time.sleep(1)
                
                # SET SESSION STATE
                st.session_state.auth_context = {
                    "db": db,
                    "schema": sch,
                    "stage": stg,
                    "user": user_email,
                    "role": list(common_roles)[0]
                }
                
                # Sync Legacy Config
                if "config" not in st.session_state: st.session_state.config = {}
                st.session_state.config["db"] = db
                st.session_state.config["schema"] = sch
                st.session_state.config["stage"] = stg
                st.session_state.config["user_id"] = user_email
                
                st.rerun()
                
            else:
                status.update(label="⛔ Access Denied", state="error")
                st.error("You do not hold any roles authorized for this stage.")
                st.warning(f"Your Roles: {my_roles}")
                st.warning(f"Required: {authorized_roles}")
                st.markdown(f"Contact **{ADMIN_CONTACT}** for assistance.")