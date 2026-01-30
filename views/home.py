# views/home.py
# Phase 3: Home Page View Module
# PLAN-10: Rebranded to "Chunky" with user documentation
import streamlit as st
from logger_config import log_action

def render_home_view():
    """Render the Home Page view"""
    st.title("🥥 Chunky")
    log_action("NAVIGATE", "Visited Home Page")
    
    st.markdown("### The RAG Ecosystem by DA COE")
    
    st.info(
        "Welcome to **Chunky**, your unstructured data refinement and RAG testing suite. "
        "Use this application to process documents, configure semantic search, and test retrieval quality."
    )

    # Documentation / How-To Section
    st.markdown("---")
    st.header("📖 User Documentation")
    
    with st.expander("🏭 How to use Doc Refinery", expanded=True):
        st.markdown("""
        **Objective:** Turn PDF documents into clean, searchable chunks.
        
        1. **Ingestion**: 
           - Ensure your PDFs are in the Snowflake Stage (`@DOCS`).
           - Go to the **Configuration** tab in Doc Refinery to map the database.
        2. **Job Builder**:
           - Select a file and choose a strategy:
             - **Layout Parser**: Good for standard text.
             - **Vision Parser**: Best for slides, charts, and complex tables.
           - Add the job to the queue and click **Run Batch**.
        3. **QA Studio**:
           - Use the **QA Studio** tab to inspect generated chunks.
           - Edit text to fix OCR errors and commit changes back to Snowflake.
        4. **Deployment**:
           - Once data is clean, go to **Deployment** to create a Cortex Search Service.
        """)

    with st.expander("🧠 How to use RAG Playground"):
        st.markdown("""
        **Objective:** Test your data with an LLM.
        
        1. **Configuration (Top of Page)**:
           - Define your Database and Schema.
           - Click **Scan for Services** to find your deployed Cortex Search Services.
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