# streamlit_app.py
import streamlit as st
import traceback
import logging

# Local Imports
from logger_config import log_action
from views.home import render_home_view
from views.chat import render_chat_view
from views.admin import render_admin_view
from views.analytics_cost import render_cost_analytics
from views.analytics_quality import render_quality_analytics
from views.logs import render_logs_view
from views.demo.wizard import render_demo_search_service
from utils.snowflake_utils import get_snowpark_session
from utils import auth_utils
from utils.constants import DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE, DEFAULT_TARGET_TABLE

# -----------------------------------------------------------------------------
# 1. APP CONFIGURATION (Must be first)
# -----------------------------------------------------------------------------
st.set_page_config(layout="wide", page_title="RAG Ecosystem")

# -----------------------------------------------------------------------------
# 2. SESSION STATE INITIALIZATION
# -----------------------------------------------------------------------------
# PLAN-16: Initialize chunk_cache BEFORE any other session state (Golden Rule 2)
if "chunk_cache" not in st.session_state:
    st.session_state.chunk_cache = []

if "config" not in st.session_state:
    st.session_state.config = {
        "db": DEFAULT_DB,
        "schema": DEFAULT_SCHEMA,
        "stage": DEFAULT_STAGE,
        "services_cache": [],
        "user_id": "user_session_01",  # In prod, this might come from st.experimental_user
        "target_table": DEFAULT_TARGET_TABLE
    }

if "messages" not in st.session_state:
    st.session_state.messages = []

# Maintain only the last 30 messages
if len(st.session_state.messages) > 30:
    st.session_state.messages = st.session_state.messages[-30:]

# Initialize services_cache for backward compatibility
if "services_cache" not in st.session_state:
    st.session_state.services_cache = st.session_state.config.get("services_cache", [])

# Initialize active_config for chat configuration
if "active_config" not in st.session_state:
    st.session_state.active_config = {}

# Initialize monitoring_logs for analytics
if "monitoring_logs" not in st.session_state:
    st.session_state.monitoring_logs = []

# Initialize pending_batch for batch processing
if "pending_batch" not in st.session_state:
    st.session_state.pending_batch = []

# Initialize batch execution state for the one-job-per-rerun driver
# and cancel mechanism.
if "batch_in_progress" not in st.session_state:
    st.session_state.batch_in_progress = False

if "cancel_batch" not in st.session_state:
    st.session_state.cancel_batch = False

# -----------------------------------------------------------------------------
# 3. GLOBAL LOGGING STARTUP
# -----------------------------------------------------------------------------
if "app_started" not in st.session_state:
    log_action("APP_STARTUP", "Application initialized", st.session_state.config["user_id"])
    st.session_state.app_started = True

# -----------------------------------------------------------------------------
# 4. MAIN CONTROLLER
# -----------------------------------------------------------------------------
def main():
    session = get_snowpark_session()
    if not session:
        st.error("No active Snowflake session detected. Please run within Snowflake.")
        return

    # --- GATEKEEPER CHECK ---
    # If no auth context, show Login Screen and STOP.
    if "auth_context" not in st.session_state:
        auth_utils.render_login_screen(session)
        return
    
    if "query_tag_set" not in st.session_state:
        from utils.snowflake_utils import set_query_tag
        set_query_tag(session, st.session_state.auth_context)
        st.session_state.query_tag_set = True
    # ---------------------------------

    # --- Sidebar Navigation ---
    with st.sidebar:
        st.title("Navigation")
        
        # Determine default index based on state (allows shortcuts from Home to work in future)
        # For now, standard radio selection
        page_selection = st.radio("Go to:", [
            "Home",
            "Doc Refinery",
            "RAG Playground",
            "Create Search Service",
            "Cost Analytics",
            "Quality Analytics",
            "System Logs"
        ])

        st.markdown("---")
        
        # Context Display & Logout
        ctx = st.session_state.auth_context
        st.caption("🔒 Active Context:")
        st.code(f"{ctx['db']}.{ctx['schema']}\nStage: {ctx['stage']}")
        
        if st.button("❌ Disconnect / Change"):
            auth_utils.logout()
        
        st.markdown("---")
        
        # Optional: Quick Refresh for Services Cache
        if st.button("🔄 Refresh Session"):
            # Invalidate deployment caches so _fetch_and_validate_source_metadata and
            # _render_service_config_section re-query Snowflake on the next render.
            for _cache_key in ["deployment_tables_cache", "deployment_warehouses_cache"]:
                if _cache_key in st.session_state:
                    del st.session_state[_cache_key]
            st.rerun()

        # PLAN-16: In-memory chunk cache management (Golden Rules 6, 9)
        if st.button("🧹 Clear In-Memory Chunks"):
            # Purges raw chunk export cache ONLY — never touches job_queue or batch_audit
            st.session_state.chunk_cache = []
            st.rerun()

    # --- Routing Logic ---
    try:
        if page_selection == "Home":
            render_home_view()
            
        elif page_selection == "RAG Playground":
            render_chat_view()
            
        elif page_selection == "Doc Refinery":
            render_admin_view()
            
        elif page_selection == "Create Search Service":
            render_demo_search_service()
            
        elif page_selection == "Cost Analytics":
            render_cost_analytics()
            
        elif page_selection == "Quality Analytics":
            render_quality_analytics()
            
        elif page_selection == "System Logs":
            render_logs_view()
            
    except Exception as e:
        # Log full traceback to backend (never show raw traceback to user).
        # Using st.error() with a controlled message — NOT st.exception(),
        # which renders the full Python traceback in the UI.
        # Ref: https://docs.streamlit.io/develop/api-reference/execution-flow/st.error
        log_action("APP_CRASH", traceback.format_exc())
        st.error("An unexpected error occurred. Please refresh the page. "
                 "If the issue persists, contact support with the timestamp above.")

if __name__ == "__main__":
    main()
