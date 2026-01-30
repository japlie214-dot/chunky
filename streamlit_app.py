# streamlit_app.py
# Phase 5: Main Controller Integration - Single entry point for the RAG Ecosystem
import streamlit as st
import logging

# Local Imports
from logger_config import log_action
from views.home import render_home_view
from views.chat import render_chat_view
from views.admin import render_admin_view
from views.analytics_cost import render_cost_analytics
from views.analytics_quality import render_quality_analytics
from views.logs import render_logs_view

# -----------------------------------------------------------------------------
# 1. APP CONFIGURATION (Must be first)
# -----------------------------------------------------------------------------
st.set_page_config(layout="wide", page_title="RAG Ecosystem")

# -----------------------------------------------------------------------------
# 2. SESSION STATE INITIALIZATION
# -----------------------------------------------------------------------------
if "config" not in st.session_state:
    st.session_state.config = {
        "db": "SBOX_DB",
        "schema": "AI_SB",
        "services_cache": [],
        "user_id": "user_session_01",  # In prod, this might come from st.experimental_user
        "target_table": "SUS_CHUNKS"
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
    # --- Sidebar Navigation ---
    with st.sidebar:
        st.title("Navigation")
        
        # Determine default index based on state (allows shortcuts from Home to work in future)
        # For now, standard radio selection
        page_selection = st.radio("Go to:", [
            "Home",
            "RAG Playground",
            "Doc Refinery",
            "Cost Analytics",
            "Quality Analytics",
            "System Logs"
        ])

        st.markdown("---")
        st.caption("Active Context:")
        st.code(f"{st.session_state.config['db']}.{st.session_state.config['schema']}")
        
        # Optional: Quick Refresh for Services Cache
        if st.button("🔄 Refresh Session"):
            st.rerun()

    # --- Routing Logic ---
    try:
        if page_selection == "Home":
            render_home_view()
            
        elif page_selection == "RAG Playground":
            render_chat_view()
            
        elif page_selection == "Doc Refinery":
            render_admin_view()
            
        elif page_selection == "Cost Analytics":
            render_cost_analytics()
            
        elif page_selection == "Quality Analytics":
            render_quality_analytics()
            
        elif page_selection == "System Logs":
            render_logs_view()
            
    except Exception as e:
        st.error(f"Application Error: {e}")
        log_action("APP_CRASH", str(e))

if __name__ == "__main__":
    main()
