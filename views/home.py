# views/home.py
# Phase 3: Home Page View Module
# PLAN-12: Updated Documentation for Gatekeeper Workflow
import streamlit as st
from logger_config import log_action

def render_home_view():
    """Render the Home Page view"""
    st.title("🥥 Chunky")
    log_action("NAVIGATE", "Visited Home Page")
    
    st.markdown("### The RAG Ecosystem by DA COE")
    
    # Context Display
    if "auth_context" in st.session_state:
        ctx = st.session_state.auth_context
        st.success(f"🔒 **Securely Connected to:** `{ctx['db']}.{ctx['schema']}` (Stage: `{ctx['stage']}`)")
    
    st.info(
        "Welcome to **Chunky**, your unstructured data refinement and RAG testing suite. "
        "Use this application to process documents, configure semantic search, and test retrieval quality."
    )

    # Documentation / How-To Section
    st.markdown("---")
    st.header("📖 User Documentation")
    
    with st.expander("🛡️ Authentication & Security", expanded=True):
        st.markdown("""
        **Gatekeeper Protocol:**
        - This application uses a strict **Stage-Based Authentication**.
        - To access the app, you must connect to a Snowflake Stage where your Role has explicit permissions.
        - Once connected, your session is **Locked** to that Database, Schema, and Stage.
        - To switch projects (e.g., from Finance to HR), use the **Disconnect** button in the sidebar.
        """)
    
    with st.expander("🏭 How to use Doc Refinery"):
        st.markdown("""
        **Objective:** Turn PDF documents into clean, searchable chunks.
        
        1. **Job Builder**:
           - Select a PDF from the **Connected Stage**.
           - Choose a strategy:
             - **Layout Parser**: Good for standard text.
             - **Vision Parser**: Best for slides, charts, and complex tables.
           - Add the job to the queue and click **Run Batch**.
        2. **QA Studio**:
           - Use the **QA Studio** tab to inspect generated chunks.
           - Edit text to fix OCR errors and commit changes back to Snowflake.
        3. **Deployment**:
           - Once data is clean, go to **Deployment** to create a Cortex Search Service.
           - Services are deployed directly into your **Connected Schema**.
        """)

    with st.expander("🧠 How to use RAG Playground"):
        st.markdown("""
        **Objective:** Test your data with an LLM.
        
        1. **Configuration**:
           - Click **Scan for Services** to find Cortex Search Services in your **Connected Schema**.
           - Select the service(s) you want to query against.
        2. **Chat**:
           - Ask questions in the chat bar.
           - The bot will use the selected services to find context.
        3. **Retrieval Inspector**:
           - Look below the chat to see exactly what chunks were sent to the LLM.
        """)

    with st.expander("📊 How to interpret Analytics"):
        st.markdown("""
        **Quality Analytics**:
        - Detects hallucinations, bias, and safety issues.
        - **Note**: This is an R&D feature for playground testing only.
        
        **Cost Analytics**:
        - Tracks credit consumption for LLM generation.
        """)

    # Quick Links
    st.markdown("---")
    st.caption("Developed by DA COE • R&D Initiative")