# views/refinery/tab_deployment.py
# Deployment Tab - Step-by-step Cortex Search Service creation guide
# Programmatic deployment code deprecated to views/refinery/deprecated/
import streamlit as st

def render_deployment_tab(session):
    st.subheader("4. Cortex Search Deployment")
    ctx = st.session_state.auth_context
    db, schema = ctx["db"], ctx["schema"]
    
    st.markdown("""
    ### What is Cortex Search?
    Cortex Search is a Snowflake-native service that provides low-latency,
    high-quality hybrid search over your text data.
    """)
    
    st.divider()
    st.markdown("### Step-by-Step: Create a Cortex Search Service via Snowsight")
    
    with st.container():
        st.markdown(f"""
        **Step 1 — Open the Search Service Page**
        Navigate to **AI & ML** → **Search** in the Snowsight left-hand navigation menu.

        **Step 2 — Start Creating a New Service**
        Click **Create**.

        **Step 3 — Configure the New Service**
        - **Service database and schema:** Select **`{db}`** and **`{schema}`** (Your current context).
        - **Warehouse:** Select a dedicated **X-SMALL** or **SMALL** warehouse.
        - **Service name:** e.g., `CSS_RAG_V1`.

        **Step 4 — Select Data to Be Indexed**
        Select the table that contains your ingested chunks (e.g., `SUS_CHUNKS`).

        **Step 5 — Select the Search Column**
        Choose **`CHUNK`**.

        **Step 6 — Select Attribute Columns**
        Select **`RELATIVE_PATH`** and **`PAGE_NUMBER`**.

        **Step 7 — Select Columns to Include in the Service**
        Select **`CHUNK_REF`** to enable tracing results back to the original document.

        **Step 8 — Configure the Search Service**
        - **Target Lag:** Based on data volatility (e.g. **365 days** for static files).
        - **Embedding Model:** Select **`snowflake-arctic-embed-m-v1.5`**.
        
        Click **Create** to build the search service.
        """)
    
    st.divider()
    with st.container():
        st.markdown(f"""
        ### After Creation
        Grant access so others can query the service:
        ```sql
        GRANT USAGE ON DATABASE {db} TO ROLE <role_name>;
        GRANT USAGE ON SCHEMA {schema} TO ROLE <role_name>;
        GRANT USAGE ON CORTEX SEARCH SERVICE <service_name> TO ROLE <role_name>;
        ```
        """)
