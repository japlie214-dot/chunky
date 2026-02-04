# views/refinery/tab_deployment.py
# Deployment Tab - Cortex Search Deployment for the Doc Refinery package
import streamlit as st
import time
from utils.snowflake_utils import get_table_schema, clean_text_for_sql, scan_for_services

def render_deployment_tab(session):
    st.subheader("4. Cortex Search Deployment")
    ctx = st.session_state.auth_context
    db, schema = ctx["db"], ctx["schema"]
    
    st.markdown("#### 📁 Context & Source")
    c_ctx1, c_ctx2 = st.columns(2)
    with c_ctx1:
        st.text_input("Active Database", value=db, disabled=True)
    with c_ctx2:
        st.text_input("Active Schema", value=schema, disabled=True)

    # Fetch tables for dropdown
    try:
        tables_res = session.sql(f"SHOW TABLES IN SCHEMA {db}.{schema}").collect()
        table_list = [r['name'] for r in tables_res]
    except:
        table_list = ["SUS_CHUNKS"]
    
    tgt_table_base = st.selectbox("Source Table Name", options=table_list, index=0 if "SUS_CHUNKS" not in table_list else table_list.index("SUS_CHUNKS"))
    tgt_table_full = f'"{db}"."{schema}"."{tgt_table_base}"'

    # Service Config
    st.markdown("#### ⚙️ Service Configuration")
    
    c_pfx1, c_pfx2 = st.columns([1, 4])
    with c_pfx1:
        st.text_input("Prefix", value="CSS_", disabled=True, help="Standardized CSS Prefix")
    with c_pfx2:
        svc_user_name = st.text_input("Service Name", "RAG_V1", key="dep_svc_name").strip()
    
    # Standardized full name with prefix
    full_svc_identifier = f"CSS_{svc_user_name}"
    
    c_infra1, c_infra2, c_infra3 = st.columns(3)
    with c_infra1:
        # Fetch warehouses dynamically
        try:
            wh_data = session.sql("SHOW WAREHOUSES").collect()
            wh_list = [r['name'] for r in wh_data]
        except:
            wh_list = ["COMPUTE_WH"]
        warehouse_sel = st.selectbox("Warehouse", wh_list, index=0)
    
    with c_infra2:
        lag_val = st.number_input("Target Lag", min_value=1, value=365)
    with c_infra3:
        lag_unit = st.selectbox("Lag Unit", ["days", "hours", "minutes"], index=0)
    
    svc_comment = st.text_area("Comment (Optional)", placeholder="Describe this search service...")

    # Attributes Selection
    cols = []
    try:
        _, cols, _ = get_table_schema(session, db, schema, tgt_table_base)
    except:
        cols = ["PAGE_NUMBER", "RELATIVE_PATH", "CHUNK"]  # Fallback
    
    preferred_defaults = ["PAGE_NUMBER", "RELATIVE_PATH"]
    safe_defaults = [c for c in preferred_defaults if c in cols]
    atts = st.multiselect("Filter Attributes", cols, default=safe_defaults, key="dep_atts")

    # SQL Generation
    if "cortex_sql_preview" not in st.session_state:
        st.session_state.cortex_sql_preview = ""

    if st.button("📝 Generate SQL Preview"):
        if not svc_user_name: st.error("Service Name required")
        else:
            try:
                select_cols = list(set(["CHUNK", "RELATIVE_PATH", "PAGE_NUMBER", "CHUNK_ID"] + atts))
                # Wrap attributes in double quotes
                quoted_atts = [f'"{a}"' for a in atts]
                attr_clause = f"ATTRIBUTES ({', '.join(quoted_atts)})" if atts else ""
                comment_clause = f"\nCOMMENT = '{clean_text_for_sql(svc_comment)}'" if svc_comment else ""
                
                # Robust Quoting for Identifiers to handle spaces/special chars
                st.session_state.cortex_sql_preview = f"""CREATE OR REPLACE CORTEX SEARCH SERVICE "{db}"."{schema}"."{full_svc_identifier}"
ON CHUNK {attr_clause}
WAREHOUSE = "{warehouse_sel}"
TARGET_LAG = '{lag_val} {lag_unit}'{comment_clause}
AS (
    SELECT {', '.join([f'"{c}"' for c in select_cols])}
    FROM {tgt_table_full}
)"""
            except Exception as e:
                st.error(f"Generation failed: {e}")

    # Render Preview Area if Content Exists
    if st.session_state.cortex_sql_preview:
        st.markdown("#### 📜 SQL Preview & Edit")
        st.session_state.cortex_sql_preview = st.text_area("Review DDL", value=st.session_state.cortex_sql_preview, height=300, key="cortex_ddl_editor")
        
        c_exec, c_cancel = st.columns([1, 4])
        with c_exec:
            if st.button("🚀 Execute & Deploy", type="primary"):
                final_sql = st.session_state.cortex_sql_preview
                
                # Strict Target Validation (Prevents bypass via FROM clause inclusion)
                required_target_prefix = f'CREATE OR REPLACE CORTEX SEARCH SERVICE "{db}"."{schema}"."CSS_'.upper()
                if required_target_prefix not in final_sql.upper():
                    st.error(f"⛔ **Security Violation!**")
                    st.markdown(f"""
                    The SQL target must be in your secured context and follow naming standards: `{db}.{schema}.CSS_...`
                    
                    **Options:**
                    1. Update the SQL above to use the correct Database/Schema and CSS_ prefix.
                    2. Click **'❌ Disconnect / Change'** in the Sidebar to authenticate to a different project.
                    3. Copy this SQL and execute it manually in a **Snowflake Worksheet**.
                    """)
                    st.stop()

                # Basic Malicious Query Check
                forbidden = ["DROP TABLE", "DELETE FROM", "TRUNCATE", "ALTER TABLE"]
                if any(k in final_sql.upper() for k in forbidden):
                    st.error("⛔ Security Block: Destructive keywords detected.")
                    st.stop()

                try:
                    with st.spinner("Deploying..."):
                        session.sql(final_sql).collect()
                        # Use st.toast for success
                        st.toast(f"🚀 Service '{full_svc_identifier}' deployed successfully!", icon="✅")
                        st.session_state.cortex_sql_preview = ""
                        time.sleep(1)
                        st.rerun()
                except Exception as e:
                    st.error(f"Deployment Failed: {e}")
        
        with c_cancel:
            if st.button("❌ Cancel"):
                st.session_state.cortex_sql_preview = ""
                st.rerun()

    # RBAC - Locked to Context
    st.divider()
    st.markdown("#### 🔐 RBAC (Active Schema)")
    if st.button("🔄 Scan Services"):
        st.session_state.admin_service_cache = scan_for_services(session, db, schema)
    
    svc_list = st.session_state.get('admin_service_cache', [])
    target_svc = st.selectbox("Service", svc_list, key="rbac_svc")
    target_role = st.text_input("Role", "ACCOUNTADMIN", key="rbac_role")
    
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Grant Access"):
             # Uppercase the role to ensure it matches standard Snowflake identifier behavior
             safe_role = f'"{target_role.upper()}"'
             session.sql(f'GRANT USAGE ON CORTEX SEARCH SERVICE "{db}"."{schema}"."{target_svc}" TO ROLE {safe_role}').collect()
             session.sql(f'GRANT USAGE ON SCHEMA "{db}"."{schema}" TO ROLE {safe_role}').collect()
             st.success("Granted")
    with c2:
        if st.button("Revoke Access"):
             safe_role = f'"{target_role.upper()}"'
             session.sql(f'REVOKE USAGE ON CORTEX SEARCH SERVICE "{db}"."{schema}"."{target_svc}" FROM ROLE {safe_role}').collect()
             st.success("Revoked")