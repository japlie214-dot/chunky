# views/refinery/tab_tools.py
# Tools Tab - Maintenance Tools for the Doc Refinery package
import streamlit as st

def render_tools_tab(session):
    st.subheader("5. Maintenance Tools")
    ctx = st.session_state.auth_context
    if st.button("🧹 Clear Temp Stages"):
        try:
            session.sql(f"REMOVE @{ctx['db']}.{ctx['schema']}.{ctx['stage']}/_temp_images").collect()
            st.success("Cleaned")
        except Exception as e: st.warning(f"Error: {e}")