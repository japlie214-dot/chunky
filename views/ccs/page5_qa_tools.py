# views/ccs/page5_qa_tools.py
# Page 4 (CCS): QA Studio — chunk inspection and editing.
# Imports from the shared views/qastudio.py module.
# No Tools tab. No Search Scope. User can skip this step freely.

import streamlit as st
from views.ccs.common import render_header, nav_buttons, ctx
from utils.constants import DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE


def render(session):
    render_header(4)

    c = ctx()
    db = c.get("db", DEFAULT_DB)
    schema = c.get("schema", DEFAULT_SCHEMA)
    stage = c.get("stage", DEFAULT_STAGE)
    stage_path = f"@{db}.{schema}.{stage}"

    jobs = st.session_state.get("cssw_jobs", [])

    # Import and render the shared QA Studio
    from views.qastudio import render_qa_studio
    render_qa_studio(session, db, schema, stage_path, jobs=jobs)

    # User can always proceed to Step 5 without doing anything here
    nav_buttons(can_next=True, show_back=True)
