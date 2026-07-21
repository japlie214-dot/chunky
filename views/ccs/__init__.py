# views/ccs/__init__.py
# "Create Search Service" — 5-page wizard entry point.
# Routes to granular page modules.

import streamlit as st
from logger_config import log_action
from views.ccs.common import jb_init, get_page, set_page


def render_demo_search_service():
    st.title("🌐 Demo: Create Search Service")
    log_action("NAVIGATE", "Visited Create Search Service Wizard")
    jb_init()
    st.session_state.setdefault("cssw_page", 1)
    st.session_state.setdefault("cssw_jobs", [])

    page = get_page()
    st.progress(page / 5, text=f"Step {page} of 5")

    from utils.snowflake_utils import get_snowpark_session
    session = get_snowpark_session()

    try:
        if page == 1:
            from views.ccs.page1_setup import render
        elif page == 2:
            from views.ccs.page2_builder import render
        elif page == 3:
            from views.ccs.page3_execute import render
        elif page == 4:
            from views.ccs.page5_qa_tools import render as render_qa_tools
        elif page == 5:
            from views.ccs.page4_complete import render
        else:
            set_page(1)
            st.rerun()
            return
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log_action("WIZARD_IMPORT_ERROR", {"page": page, "error": str(e), "traceback": tb}, level="ERROR")
        st.error(f"❌ Failed to load Step {page}: `{e}`")
        with st.expander("🔧 Technical Details"):
            st.code(tb)
        return

    if page == 4:
        render_qa_tools(session)
    else:
        render(session)
