# views/admin.py
# Orchestrator for the Doc Refinery - imports all tab renderers from refinery package
import streamlit as st
from utils.snowflake_utils import get_snowpark_session

# Import all tab renderers from the refinery package
from views.refinery.tab_config import render_config_tab
from views.refinery.tab_ingestion import render_ingestion_tab
from views.refinery.tab_qa import render_qa_tab
from views.refinery.tab_deployment import render_deployment_tab
from views.refinery.tab_tools import render_tools_tab

def render_admin_view():
    """Main entry point for the Doc Refinery admin interface."""
    st.title("🏭 Doc Refinery")
    session = get_snowpark_session()
    
    t1, t2, t3, t4, t5 = st.tabs(["Config", "Ingestion", "QA Studio", "Deployment", "Tools"])
    
    with t1: render_config_tab(session)
    with t2: render_ingestion_tab(session)
    with t3: render_qa_tab(session)
    with t4: render_deployment_tab(session)
    with t5: render_tools_tab(session)