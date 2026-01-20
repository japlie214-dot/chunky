# views/home.py
# Phase 3: Home Page View Module
import streamlit as st
from snowflake.snowpark.context import get_active_session
from logger_config import log_action
from utils.snowflake_utils import scan_for_services

def render_home_view():
    """Render the Home Page view"""
    st.title("🏠 RAG Ecosystem Home")
    log_action("NAVIGATE", "Visited Home Page")
    
    # Welcome Section
    st.markdown("""
    ### Welcome to the RAG Ecosystem
    
    This is your centralized hub for managing RAG (Retrieval-Augmented Generation) applications.
    Use the navigation sidebar to access different modules:
    
    - **Chat Playground**: Interact with your RAG models and test responses
    - **Knowledge Base (Admin)**: Manage document ingestion and deployment
    - **Cost Analytics**: Monitor and analyze LLM usage costs
    - **Quality Analytics**: Review safety and quality metrics
    
    """)
    
    # Configuration Section
    st.markdown("---")
    st.header("⚙️ Configuration")
    
    # Infrastructure Settings
    st.subheader("1. Infrastructure")
    
    # Wrap infrastructure settings in a form to prevent reruns on every input change
    with st.form("infra_setup"):
        target_db = st.text_input("Database", value=st.session_state.config.get("db", "SBOX_DB"))
        target_schema = st.text_input("Schema", value=st.session_state.config.get("schema", "AI_SB"))
        scan_submitted = st.form_submit_button("🔍 Scan for Services")
    
    if scan_submitted:
        session = get_active_session()
        try:
            services = scan_for_services(session, target_db, target_schema)
            st.session_state.services_cache = services
            st.session_state.config["db"] = target_db
            st.session_state.config["schema"] = target_schema
            st.success(f"Found {len(services)} services.")
        except Exception as e:
            st.error(f"Scan failed: {e}")
    
    # Configuration Form
    st.subheader("2. Configuration")
    with st.form("config_form"):
        # Service Selection (Multi-select)
        selected_services = st.multiselect(
            "Select Services",
            options=st.session_state.services_cache
        )
        
        # Proper default system prompt (persona + instructions)
        default_sys = (
            "You are an expert Document Research Assistant. "
            "Answer faithfully based on facts from the RAG context. "
            "Be aware of the chat history context but prioritize responding to the latest message. "
            "If the answer is not in the facts, state you do not know."
        )
        sys_prompt = st.text_area("System Prompt", value=default_sys, height=150)
        model = st.selectbox("LLM", options=["claude-4-sonnet", "claude-3-5-sonnet", "deepseek-r1", "openai-gpt-4.1", "openai-gpt-5"])
        limit = st.number_input("Limit per Service", min_value=1, value=5)
        temp = st.slider("Temperature", 0.0, 1.0, 0.5)
        top_p = st.slider("Top P", 0.0, 1.0, 0.5)
        
        if st.form_submit_button("✅ Apply Configuration"):
            st.session_state.active_config = {
                "services": selected_services, 
                "model": model, 
                "limit": limit,
                "db": target_db, 
                "schema": target_schema, 
                "sys_prompt": sys_prompt,
                "temperature": temp,
                "top_p": top_p
            }
            log_action("CONFIG_UPDATE", {
                "services": selected_services,
                "model": model,
                "limit": limit,
                "temperature": temp,
                "top_p": top_p
            })
            st.success("Configuration applied successfully!")
    
    # Current Status
    st.markdown("---")
    st.header("📊 Current Status")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("Database", st.session_state.config.get("db", "Not set"))
    
    with col2:
        st.metric("Schema", st.session_state.config.get("schema", "Not set"))
    
    with col3:
        st.metric("Services Available", len(st.session_state.services_cache))
    
    # Active Configuration Display
    if "active_config" in st.session_state and st.session_state.active_config:
        st.markdown("---")
        st.header("✅ Active Configuration")
        
        config = st.session_state.active_config
        
        st.write(f"**Model:** {config.get('model', 'Not set')}")
        st.write(f"**Services Selected:** {', '.join(config.get('services', [])) or 'None'}")
        st.write(f"**Limit per Service:** {config.get('limit', 'Not set')}")
        st.write(f"**Temperature:** {config.get('temperature', 'Not set')}")
        st.write(f"**Top P:** {config.get('top_p', 'Not set')}")
        
        with st.expander("View System Prompt"):
            st.code(config.get('sys_prompt', 'Not set'))
    
    # Quick Links
    st.markdown("---")
    st.header("🔗 Quick Links")
    
    st.markdown("""
    - [Snowflake Cortex Documentation](https://docs.snowflake.com/en/user-guide/snowflake-cortex-ml)
    - [Cortex Search Documentation](https://docs.snowflake.com/en/user-guide/snowflake-cortex-search)
    - [Streamlit Documentation](https://docs.streamlit.io)
    """)
    
    # System Information
    st.markdown("---")
    st.caption(f"Context: {st.session_state.config['db']}.{st.session_state.config['schema']}")
    st.caption(f"User Session: {st.session_state.config.get('user_id', 'anonymous')}")