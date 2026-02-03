# utils/auth_utils.py
# PLAN-12: Centralized Authentication & Context Management
import streamlit as st
from snowflake.snowpark.context import get_active_session

# -----------------------------------------------------------------------------
# 1. HARDCODED IDENTITY MAP
# -----------------------------------------------------------------------------
# Source of Truth: Mapping Emails to Snowflake Roles
USER_ROLE_MAP = {
    "alvin.lie@japfa.com": ["IT_AI", "IT_DS"],
    "jordan.gani@japfa.com": ["IT_DS"],
    # Fallback for dev/testing if needed
    "admin@japfa.com": ["ACCOUNTADMIN", "IT_AI"]
}

# -----------------------------------------------------------------------------
# 2. CORE AUTHENTICATION LOGIC
# -----------------------------------------------------------------------------
def get_current_user_email():
    """Safely retrieve user email from Streamlit context or secrets."""
    # 1. Try native Streamlit user (Deployed)
    if st.user and st.user.get("email"):
        return st.user.get("email")
    
    # 2. Try Secrets (Local Dev)
    if "user" in st.secrets and "email" in st.secrets["user"]:
        return st.secrets["user"]["email"]
    
    return None

def authenticate_stage_access(session, db, schema, stage):
    """
    Verifies if the current user holds a role that is granted access 
    to the specific Snowflake Stage.
    """
    user_email = get_current_user_email()
    
    if not user_email:
        st.error("❌ Authentication Failed: Could not identify user.")
        return False

    # 1. Get User's Claimed Roles
    my_roles = USER_ROLE_MAP.get(user_email, [])
    if not my_roles:
        st.error(f"❌ Access Denied: User '{user_email}' is not registered.")
        return False

    # 2. Get Snowflake's Actual Grants on the Stage
    full_stage_path = f"{db}.{schema}.{stage}"
    try:
        # App Owner (IT_AI) must have privileges to run SHOW GRANTS
        grants = session.sql(f"SHOW GRANTS ON STAGE {full_stage_path}").collect()
    except Exception as e:
        st.error(f"❌ Stage Verification Failed. Does '{full_stage_path}' exist?")
        st.error(f"System Error: {e}")
        return False

    # 3. Intersection Check - Normalized for case-insensitive comparison
    # Filter for role grants only and normalize to uppercase
    allowed_roles = [row['grantee_name'].upper() for row in grants if row['granted_to'] == 'ROLE']
    my_roles_upper = [r.upper() for r in my_roles]
    
    # Check intersection
    common_roles = set(my_roles_upper).intersection(set(allowed_roles))
    
    if common_roles:
        st.toast(f"✅ Verified via role(s): {', '.join(common_roles)}")
        return True
    else:
        st.error("⛔ Access Denied")
        st.warning(f"Your Roles: {my_roles}")
        st.warning(f"Stage Allowed Roles: {allowed_roles}")
        return False

def logout():
    """Clears authentication context."""
    if "auth_context" in st.session_state:
        del st.session_state["auth_context"]
    # Optional: Clear other session artifacts
    if "messages" in st.session_state:
        st.session_state.messages = []
    st.rerun()

# -----------------------------------------------------------------------------
# 3. UI COMPONENT: LOGIN SCREEN
# -----------------------------------------------------------------------------
def render_login_screen(session):
    """Renders the blocking login form."""
    st.markdown("## 🛡️ Chunky Gatekeeper")
    st.info("Please connect to a secure Stage to proceed.")
    
    # Debug info for dev transparency
    user = get_current_user_email()
    if user:
        st.caption(f"Identified as: `{user}`")
    else:
        st.caption("⚠️ User identity not found (Local Dev?)")

    with st.form("gatekeeper_form"):
        c1, c2, c3 = st.columns(3)
        db_in = c1.text_input("Database", value="SBOX_DB")
        sch_in = c2.text_input("Schema", value="AI_SB")
        stg_in = c3.text_input("Stage", value="DOCS")
        
        submitted = st.form_submit_button("🔌 Connect & Verify")
    
    if submitted:
        # Normalize
        db = db_in.strip().upper()
        sch = sch_in.strip().upper()
        stg = stg_in.strip().upper()
        
        if authenticate_stage_access(session, db, sch, stg):
            # Set Context
            st.session_state.auth_context = {
                "db": db,
                "schema": sch,
                "stage": stg,
                "user": user
            }
            
            # Sync to Legacy Config and update user_id for logging
            if "config" not in st.session_state:
                st.session_state.config = {}
            st.session_state.config["db"] = db
            st.session_state.config["schema"] = sch
            st.session_state.config["stage"] = stg
            st.session_state.config["user_id"] = user
            
            st.success("Authentication Successful. Redirecting...")
            st.rerun()