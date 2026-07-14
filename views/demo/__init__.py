# views/demo/__init__.py
# "Create Search Service" — 4-page wizard entry point.
# Routes to granular page modules.

import streamlit as st
from logger_config import log_action
from views.demo.common import jb_init, get_page, set_page


def render_demo_search_service():
    st.title("🌐 Demo: Create Search Service")
    log_action("NAVIGATE", "Visited Create Search Service Wizard")
    jb_init()
    st.session_state.setdefault("cssw_page", 1)
    st.session_state.setdefault("cssw_jobs", [])

    page = get_page()
    st.progress(page / 4, text=f"Step {page} of 4")

    from utils.snowflake_utils import get_snowpark_session
    session = get_snowpark_session()

    if page == 1:
        from views.demo.page1_setup import render
    elif page == 2:
        from views.demo.page2_builder import render
    elif page == 3:
        from views.demo.page3_execute import render
    elif page == 4:
        from views.demo.page4_complete import render
    else:
        set_page(1)
        st.rerun()
        return

    render(session)
