# views/refinery/tab_deployment.py
# Deployment Tab - Cortex Search Deployment for the Doc Refinery package
import streamlit as st
import time
import json
import uuid
from logger_config import log_action
from utils.snowflake_utils import get_table_schema, scan_for_services, execute_grant_with_retry
from utils.core_utils import clean_text_for_sql
from utils.auth_utils import resolve_active_target_role
from utils.constants import (
    EMBEDDING_MODELS, EMBEDDING_PRICING, TARGET_LAG_UNITS,
    CREDIT_TO_USD, CREDIT_TO_IDR
)

def check_lag_warning(val, unit):
    """
    Calculates total minutes and returns a warning string if below 5-day threshold.
    
    Args:
        val: Numeric lag value
        unit: Unit of lag ('minutes', 'hours', or 'days')
    
    Returns:
        Warning message string if lag is < 5 days, None otherwise
    """
    total_min = val
    if unit == "hours": total_min *= 60
    elif unit == "days": total_min *= 1440
    
    if total_min < 7200: # Less than 5 days
        return (
            "⚠️ **Cost Optimization Warning:** Target Lag is set to less than 5 days. "
            "Smaller lags trigger more frequent re-indexing, which increases credit consumption. "
            "Ensure the lag is set wisely relative to how often your source table is updated."
        )
    return None

def render_deployment_tab(session):
    st.subheader("4. Cortex Search Deployment")
    ctx = st.session_state.auth_context
    db, schema = ctx["db"], ctx["schema"]
    
    # Post-Deployment User Guidance (Sticky State)
    if "last_deployed_service" in st.session_state:
        deployed_svc = st.session_state.last_deployed_service
        
        c_warn, c_dismiss = st.columns([4, 1])
        with c_warn:
            st.warning(
                f"🚀 **Service '{deployed_svc}' Deployed Successfully!**\n\n"
                "⚠️ **Action Required:** Grant permissions in the **RBAC** section below."
            )
        with c_dismiss:
            if st.button("Dismiss Notification"):
                del st.session_state.last_deployed_service
                st.rerun()

        st.toast(f"✅ Service '{deployed_svc}' is ready for RBAC configuration.", icon="🛡️")
    
    st.markdown("#### 📁 Context & Source")
    c_ctx1, c_ctx2 = st.columns(2)
    help_msg = "Context is locked by Gatekeeper. To change project, click 'Disconnect / Change' in the sidebar."
    with c_ctx1:
        st.text_input("Active Database", value=db, disabled=True, help=help_msg)
    with c_ctx2:
        st.text_input("Active Schema", value=schema, disabled=True, help=help_msg)

    # Proper Tagging: Capture setup metadata lookups with Trace IDs to connect them to session startup errors.
    tid_setup = uuid.uuid4().hex
    try:
        sql_list = f"SHOW TABLES IN SCHEMA \"{db}\".\"{schema}\""
        log_action("METADATA_FETCH_START", {"sql": sql_list}, user_id=ctx.get("user", "anonymous"), trace_id=tid_setup)
        tables_res = session.sql(sql_list).collect()
        log_action("METADATA_FETCH_SUCCESS", {"table_count": len(tables_res)}, user_id=ctx.get("user", "anonymous"), trace_id=tid_setup)
        
        table_list = []
        for r in tables_res:
            row_dict = r.as_dict()
            name = row_dict.get('name') or row_dict.get('NAME')
            if name: table_list.append(name)
    except Exception as e:
        log_action("METADATA_FETCH_ERROR", {"error": str(e)}, user_id=ctx.get("user", "anonymous"), level="ERROR", trace_id=tid_setup)
        table_list = ["SUS_CHUNKS"]
    
    # Safe index handling to prevent crash on empty list (FIX: Potential Crash)
    table_idx = 0
    if table_list and "SUS_CHUNKS" in table_list:
        table_idx = table_list.index("SUS_CHUNKS")
    tgt_table_base = st.selectbox(
        "Source Table Name",
        options=table_list if table_list else ["SUS_CHUNKS"],
        index=table_idx if table_list else 0
    )
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
        # Fetch warehouses dynamically with resilient key access
        try:
            wh_data = session.sql("SHOW WAREHOUSES").collect()
            wh_list = []
            for r in wh_data:
                row_dict = r.as_dict()
                name = row_dict.get('name') or row_dict.get('NAME')
                if name: wh_list.append(name)
        except:
            wh_list = ["COMPUTE_WH"]
        warehouse_sel = st.selectbox("Warehouse", wh_list, index=0)
    
    with c_infra2:
        # Default is 365 days for new service creation as intended
        lag_val = st.number_input("Target Lag", min_value=1, value=365)
    with c_infra3:
        lag_unit = st.selectbox("Lag Unit", TARGET_LAG_UNITS, index=2)
    
    svc_comment = st.text_area("Comment (Optional)", placeholder="Describe this search service...")

    # Performance & Cost Warning
    lag_warn = check_lag_warning(lag_val, lag_unit)
    if lag_warn:
        st.warning(lag_warn)

    # 1. Fetch Schema first to populate dependent UI components (FIX: Execution Order)
    exists, cols, err = get_table_schema(session, db, schema, tgt_table_base)
    
    if not exists:
        st.error(f"❌ Table '{tgt_table_base}' is empty or does not exist. Cannot configure service.")
        return
    
    if not cols:
        st.error(f"❌ Table '{tgt_table_base}' has no columns. Cannot configure service.")
        return

    # 2. Embedding & Search Strategy
    st.markdown("#### 🧠 Embedding & Search Strategy")
    model_options = list(EMBEDDING_MODELS.keys())
    selected_model = st.selectbox("Embedding Model", options=model_options, index=0)
    
    model_meta = EMBEDDING_MODELS[selected_model]
    if model_meta["warning"]:
        st.warning(model_meta["warning"])
    else:
        st.info(f"✅ Recommended: {model_meta['lang']} | {model_meta['context']} Context | {model_meta['dim']} Dim")

    # Search Target Column (Now safe to use 'cols')
    target_col = st.selectbox("Search Target Column (ON)", options=cols, index=cols.index("CHUNK") if "CHUNK" in cols else 0)

    # 3. Attributes Selection & SELECT Configuration
    st.markdown("#### ⚙️ Column Configuration")

    # Content Columns (SELECT)
    default_sel = [c for c in ["CHUNK", "RELATIVE_PATH", "PAGE_NUMBER", "CHUNK_ID"] if c in cols]
    select_cols_list = st.multiselect(
        "Result Columns (SELECT)",
        cols,
        default=default_sel,
        key="dep_select_cols",
        help="Columns returned by the search service."
    )

    # Filter Attributes (ATTRIBUTES)
    safe_defaults = [c for c in ["PAGE_NUMBER", "RELATIVE_PATH"] if c in cols]
    atts = st.multiselect(
        "Filter Attributes (ATTRIBUTES)",
        cols,
        default=safe_defaults,
        key="dep_atts",
        help="Columns used for filtering (WHERE clause)."
    )

    # SQL Generation
    if "cortex_sql_preview" not in st.session_state:
        st.session_state.cortex_sql_preview = ""

    if st.button("📝 Generate SQL Preview"):
        # Reset stale cost estimation
        if "last_est" in st.session_state:
            del st.session_state.last_est
        
        if not svc_user_name:
            st.error("❌ Service Name cannot be empty.")
        elif not select_cols_list:
            st.error("❌ You must select at least one Result Column (SELECT).")
        elif target_col not in select_cols_list:
            st.error(f"❌ Search Target '{target_col}' must be included in Result Columns (SELECT).")
        else:
            try:
                # Wrap attributes in double quotes
                quoted_atts = [f'"{a}"' for a in atts]
                attr_clause = f"ATTRIBUTES ({', '.join(quoted_atts)})" if atts else ""
                comment_clause = f"\nCOMMENT = '{clean_text_for_sql(svc_comment)}'" if svc_comment else ""
                
                # Robust Quoting for Identifiers
                # Use user-defined select_cols_list instead of hardcoded merge
                quoted_selects = [f'"{c}"' for c in select_cols_list]
                
                st.session_state.cortex_sql_preview = f"""CREATE OR REPLACE CORTEX SEARCH SERVICE "{db}"."{schema}"."{full_svc_identifier}"
ON "{target_col}" {attr_clause}
WAREHOUSE = "{warehouse_sel}"
TARGET_LAG = '{lag_val} {lag_unit}'
EMBEDDING_MODEL = '{selected_model}'{comment_clause}
AS (
    SELECT {', '.join(quoted_selects)}
    FROM {tgt_table_full}
)"""
            except Exception as e:
                st.error(f"Generation failed: {e}")

    # Render Preview Area if Content Exists
    if st.session_state.cortex_sql_preview:
        st.markdown("#### 📜 SQL Preview & Edit")
        st.session_state.cortex_sql_preview = st.text_area("Review DDL", value=st.session_state.cortex_sql_preview, height=300, key="cortex_ddl_editor")

        # --- NESTED COST ESTIMATION START ---
        st.markdown("##### 💰 Embedding Cost Estimation")
        if st.button("🔌 Calculate Estimated Embedding Cost", key="calc_est_btn"):
            with st.spinner("Sampling rows and counting tokens..."):
                tid_est = uuid.uuid4().hex
                user = ctx.get("user", "anonymous")
                try:
                    # 1. Row Count
                    sql_cnt = f"SELECT COUNT(*) FROM {tgt_table_full}"
                    log_action("COST_EST_ROW_COUNT_START", {"sql": sql_cnt}, user_id=user, trace_id=tid_est)
                    row_count_res = session.sql(sql_cnt).collect()
                    total_rows = row_count_res[0][0]
                    log_action("COST_EST_ROW_COUNT_SUCCESS", {"total_rows": total_rows}, user_id=user, trace_id=tid_est)
                    
                    # 2. Sample 100 rows
                    sql_sample = f'SELECT "{target_col}" FROM {tgt_table_full} SAMPLE (100 ROWS)'
                    log_action("COST_EST_SAMPLE_START", {"sql": sql_sample}, user_id=user, trace_id=tid_est)
                    sample_rows = session.sql(sql_sample).collect()
                    log_action("COST_EST_SAMPLE_SUCCESS", {"retrieved_count": len(sample_rows)}, user_id=user, trace_id=tid_est)
                    
                    accumulated_text = ""
                    sampled_rows_count = 0
                    BATCH_LIMIT = 600000

                    # Accumulate until limit is reached
                    for row in sample_rows:
                        val = str(row[0])
                        accumulated_text += val + " "
                        sampled_rows_count += 1
                        
                        if len(accumulated_text) >= BATCH_LIMIT:
                            break

                    # Count tokens on the accumulated text
                    total_sampled_tokens = 0
                    if accumulated_text:
                        # Map model names that COUNT_TOKENS doesn't recognize
                        token_model_map = {"snowflake-arctic-embed-l-v2.0-8k": "snowflake-arctic-embed-l-v2.0"}
                        token_model = token_model_map.get(selected_model, selected_model)
                        
                        t_sql = "SELECT SNOWFLAKE.CORTEX.COUNT_TOKENS(?, ?)"
                        log_action("COST_EST_TOKENS_START", {"sql": t_sql, "model": token_model, "chars": len(accumulated_text)}, user_id=user, trace_id=tid_est)
                        t_res = session.sql(t_sql, params=[token_model, accumulated_text]).collect()
                        total_sampled_tokens = t_res[0][0]
                        log_action("COST_EST_TOKENS_SUCCESS", {"tokens": total_sampled_tokens}, user_id=user, trace_id=tid_est)

                    if sampled_rows_count > 0:
                        avg_tokens_per_row = total_sampled_tokens / sampled_rows_count
                        total_est_tokens = avg_tokens_per_row * total_rows
                        price_per_1m = EMBEDDING_PRICING.get(selected_model, 0.05)
                        est_credits = (total_est_tokens / 1000000) * price_per_1m
                        
                        st.session_state.last_est = {
                            "credits": est_credits,
                            "usd": est_credits * CREDIT_TO_USD,
                            "idr": est_credits * CREDIT_TO_IDR,
                            "total_rows": total_rows,
                            "sampled_rows": sampled_rows_count,
                            "intended_sample": len(sample_rows),
                            "avg_tokens": avg_tokens_per_row,
                            "price_rate": price_per_1m,
                            "lag": f"{lag_val} {lag_unit}",
                            "model": selected_model
                        }
                except Exception as e:
                    log_action("COST_EST_ERROR", {"error": str(e)}, user_id=user, level="ERROR", trace_id=tid_est)
                    st.error(f"Estimation failed: {e}")

        # Display formula transparency and sampling warnings
        if "last_est" in st.session_state:
            est = st.session_state.last_est
            
            # Sampling Warnings
            if est['total_rows'] > 100:
                st.warning(f"⚠️ **Sampling Active:** Calculation is an estimate based on {est['sampled_rows']} random rows out of {est['total_rows']:,} total.")
            if est['sampled_rows'] < est['intended_sample']:
                st.caption(f"ℹ️ *Sample was capped at {est['sampled_rows']} rows to stay within processing character limits.*")

            # Formula Transparency (Enhanced with all variables and conversion rate)
            with st.expander("📝 View Calculation Variables & Formula", expanded=True):
                st.markdown(f"**Formula:** `(Avg Tokens/Row * Total Rows / 1,000,000) * Credit Rate` (@ {est['model']})")
                
                v1, v2, v3, v4 = st.columns(4)
                v1.metric("Total Rows", f"{est['total_rows']:,}")
                v2.metric("Sampled Rows", est['sampled_rows'])
                v3.metric("Avg Tokens/Row", f"{est['avg_tokens']:.2f}")
                # Clarity: Provide both the standard 1M token rate and the granular per-token rate as requested.
                per_token = est['price_rate'] / 1_000_000
                v4.metric("Credit Rate (Cr / 1M Tokens)", f"{est['price_rate']:.2f}",
                          help=f"Granular Cost: {per_token:.10f} Cr / Token")

                st.divider()
                
                c1, c2, c3 = st.columns(3)
                # Increased precision to :.6f for Credits and :.4f for USD
                c1.metric("Estimated Credits", f"{est['credits']:.6f} Cr")
                c2.metric("Estimated USD", f"${est['usd']:.4f}")
                c3.metric("Estimated IDR", f"Rp {est['idr']:,.0f}")
                
                # Conversion Rate moved to caption to prevent truncation
                st.caption(f"**Conversion Rate:** 1 Cr = ${CREDIT_TO_USD:.2f} = Rp {CREDIT_TO_IDR:,.0f}")
            
            # Recurrence text updated with higher precision for credit value
            st.info(f"💡 **Recurrence:** You will pay approx. **{est['credits']:.6f} Credits** every **{est['lag']}** if Indexing is active, as Cortex re-indexes to maintain the Target Lag.")
        # --- NESTED COST ESTIMATION END ---

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
                    return # Gracefully stop rendering this tab's logic (FIX: UX Regression)

                # Basic Malicious Query Check
                forbidden = ["DROP TABLE", "DELETE FROM", "TRUNCATE", "ALTER TABLE"]
                if any(k in final_sql.upper() for k in forbidden):
                    st.error("⛔ Security Block: Destructive keywords detected.")
                    return # Gracefully stop rendering this tab's logic (FIX: UX Regression)

                tid_deploy = uuid.uuid4().hex
                user = ctx.get("user", "anonymous")
                try:
                    with st.spinner("Deploying..."):
                        log_action("DEPLOY_SERVICE_START", {"sql": final_sql}, user_id=user, trace_id=tid_deploy)
                        session.sql(final_sql).collect()
                        log_action("DEPLOY_SERVICE_SUCCESS", {"service": full_svc_identifier}, user_id=user, trace_id=tid_deploy)
                        
                        # PLAN-01: Automated Grant Execution for deployed service
                        resolved_role = resolve_active_target_role(session, user)
                        grant_sql = f'GRANT USAGE ON CORTEX SEARCH SERVICE "{db}"."{schema}"."{full_svc_identifier}" TO ROLE "{resolved_role.upper()}"'
                        grant_res = execute_grant_with_retry(session, grant_sql, user, resolved_role.upper())
                        
                        if grant_res != "Failed":
                            st.toast(f"Access granted to role: {grant_res}")
                        
                        # Set Sticky Success State (Overwrites previous if any)
                        st.session_state.last_deployed_service = full_svc_identifier
                        st.session_state.cortex_sql_preview = ""
                        # Invalidate the RBAC cache to force a fresh scan on the next render
                        if "admin_service_cache" in st.session_state:
                            del st.session_state.admin_service_cache
                        
                        # Rerun to refresh the UI and populate the RBAC list automatically
                        st.rerun()
                except Exception as e:
                    log_action("DEPLOY_SERVICE_ERROR", {"error": str(e)}, user_id=user, level="ERROR", trace_id=tid_deploy)
                    st.error(f"Deployment Failed: {e}")
        
        with c_cancel:
            if st.button("❌ Cancel"):
                st.session_state.cortex_sql_preview = ""
                # Clear estimation state on cancel
                if "last_est" in st.session_state:
                    del st.session_state.last_est
                st.rerun()

    # Service Management & ALTER Suite
    st.divider()
    st.header("🔧 Service Management")
    
    m_svc_list = scan_for_services(session, db, schema)
    selected_m_svc = st.selectbox("Select Service to Manage", m_svc_list, key="m_svc_sel")
    
    if selected_m_svc:
        m_full_name = f'"{db}"."{schema}"."{selected_m_svc}"'
        
        # Fetch current metadata for display
        svc_meta = {}
        tid = uuid.uuid4().hex
        d_sql = f"DESCRIBE CORTEX SEARCH SERVICE {m_full_name}"
        log_action("DESCRIBE_SERVICE_START", {"sql": d_sql}, user_id=ctx.get("user", "anonymous"), trace_id=tid)
        try:
            desc_rows = session.sql(d_sql).collect()
            log_action("DESCRIBE_SERVICE_SUCCESS", {"rows": [r.as_dict() for r in desc_rows]}, user_id=ctx.get("user", "anonymous"), trace_id=tid)
            
            # Logic: Support both Horizontal and Vertical DESCRIBE outputs (varies by region/driver)
            # and strip quotes from keys often added by Snowpark/Snowflake identifiers.
            for r in desc_rows:
                row_dict_raw = r.as_dict()
                # Normalize keys: Upper case and strip double-quotes
                row_dict = {k.upper().strip('"'): v for k, v in row_dict_raw.items()}
                
                # Check for Vertical format (columns 'PROPERTY' and 'VALUE')
                if "PROPERTY" in row_dict and "VALUE" in row_dict:
                    prop = str(row_dict["PROPERTY"]).upper()
                    val = row_dict["VALUE"]
                    svc_meta[prop.lower()] = val
                # Check for Horizontal format (one row with many columns)
                else:
                    for k, v in row_dict.items():
                        svc_meta[k.lower()] = v

            # Standardize internal keys for the UI metrics
            svc_meta["indexing_status"] = svc_meta.get("indexing_state")
            svc_meta["serving_status"] = svc_meta.get("serving_state")
        except Exception as e:
            log_action("DESCRIBE_SERVICE_ERROR", {"error": str(e)}, level="ERROR", user_id=ctx.get("user", "anonymous"), trace_id=tid)
            st.warning(f"Could not fetch metadata: {e}")

        # Display live service status
        with st.container():
            st.markdown("##### 📡 Live Service Status")
            s_idx, s_srv, s_lag, s_wh = st.columns(4)
            s_idx.metric("Indexing", svc_meta.get("indexing_status", "Unknown"))
            s_srv.metric("Serving", svc_meta.get("serving_status", "Unknown"))
            s_lag.metric("Current Lag", svc_meta.get("target_lag", "N/A"))
            s_wh.metric("Warehouse", svc_meta.get("warehouse", "N/A"))

        # Lifecycle Tabs (Removed Scoring Profiles)
        user = ctx.get("user", "anonymous")
        m_tab1, m_tab2 = st.tabs(["⚡ Status & Refresh", "⚙️ Configuration"])

        with m_tab1:
            st.markdown("##### ⚙️ Indexing Control")
            c1, c2, c3 = st.columns(3)
            if c1.button("▶️ Resume Indexing"):
                tid = uuid.uuid4().hex
                sql = f"ALTER CORTEX SEARCH SERVICE {m_full_name} RESUME INDEXING"
                log_action("SERVICE_RESUME_INDEX_START", {"sql": sql}, user_id=user, trace_id=tid)
                try:
                    session.sql(sql).collect()
                    log_action("SERVICE_RESUME_INDEX_SUCCESS", {"service": m_full_name}, user_id=user, trace_id=tid)
                    st.success("Indexing Resumed")
                except Exception as e:
                    log_action("SERVICE_RESUME_INDEX_ERROR", {"error": str(e)}, user_id=user, level="ERROR", trace_id=tid)
                    st.error(f"Action failed: {e}")
            if c2.button("⏸️ Suspend Indexing"):
                tid = uuid.uuid4().hex
                sql = f"ALTER CORTEX SEARCH SERVICE {m_full_name} SUSPEND INDEXING"
                log_action("SERVICE_SUSPEND_INDEX_START", {"sql": sql}, user_id=user, trace_id=tid)
                try:
                    session.sql(sql).collect()
                    log_action("SERVICE_SUSPEND_INDEX_SUCCESS", {"service": m_full_name}, user_id=user, trace_id=tid)
                    st.warning("Indexing Suspended")
                except Exception as e:
                    log_action("SERVICE_SUSPEND_INDEX_ERROR", {"error": str(e)}, user_id=user, level="ERROR", trace_id=tid)
                    st.error(f"Action failed: {e}")
            if c3.button("🔄 Trigger Refresh"):
                tid = uuid.uuid4().hex
                sql = f"ALTER CORTEX SEARCH SERVICE {m_full_name} REFRESH"
                log_action("SERVICE_REFRESH_START", {"sql": sql}, user_id=user, trace_id=tid)
                try:
                    session.sql(sql).collect()
                    log_action("SERVICE_REFRESH_SUCCESS", {"service": m_full_name}, user_id=user, trace_id=tid)
                    st.info("Manual Refresh Triggered")
                except Exception as e:
                    log_action("SERVICE_REFRESH_ERROR", {"error": str(e)}, user_id=user, level="ERROR", trace_id=tid)
                    st.error(f"Action failed: {e}")

            st.markdown("##### 🌐 Serving Control")
            s1, s2 = st.columns(2)
            if s1.button("▶️ Resume Serving"):
                tid = uuid.uuid4().hex
                sql = f"ALTER CORTEX SEARCH SERVICE {m_full_name} RESUME SERVING"
                log_action("SERVICE_RESUME_SERVING_START", {"sql": sql}, user_id=user, trace_id=tid)
                try:
                    session.sql(sql).collect()
                    log_action("SERVICE_RESUME_SERVING_SUCCESS", {"service": m_full_name}, user_id=user, trace_id=tid)
                    st.success("Serving Resumed")
                except Exception as e:
                    log_action("SERVICE_RESUME_SERVING_ERROR", {"error": str(e)}, user_id=user, level="ERROR", trace_id=tid)
                    st.error(f"Action failed: {e}")
            if s2.button("⏸️ Suspend Serving"):
                tid = uuid.uuid4().hex
                sql = f"ALTER CORTEX SEARCH SERVICE {m_full_name} SUSPEND SERVING"
                log_action("SERVICE_SUSPEND_SERVING_START", {"sql": sql}, user_id=user, trace_id=tid)
                try:
                    session.sql(sql).collect()
                    log_action("SERVICE_SUSPEND_SERVING_SUCCESS", {"service": m_full_name}, user_id=user, trace_id=tid)
                    st.warning("Serving Suspended")
                except Exception as e:
                    log_action("SERVICE_SUSPEND_SERVING_ERROR", {"error": str(e)}, user_id=user, level="ERROR", trace_id=tid)
                    st.error(f"Action failed: {e}")

        with m_tab2:
            st.markdown("#### Update Parameters")
            st.caption(f"Current: `{svc_meta.get('target_lag')}` on `{svc_meta.get('warehouse')}`")
            
            # Default Lag 30, Default Unit Days (index 2)
            new_lag_val = st.number_input("New Target Lag", 1, 365, value=30, key="m_lag_val")
            new_lag_unit = st.selectbox("New Unit", TARGET_LAG_UNITS, index=2, key="m_lag_unit")
            
            # Reuse logic for Service Management updates
            m_lag_warn = check_lag_warning(new_lag_val, new_lag_unit)
            if m_lag_warn:
                st.warning(m_lag_warn)
            
            new_wh = st.text_input("New Warehouse", key="m_wh")
            
            if st.button("💾 Apply SET Changes"):
                tid = uuid.uuid4().hex
                sql_set = f"ALTER CORTEX SEARCH SERVICE {m_full_name} SET "
                params = []
                if new_lag_val: params.append(f"TARGET_LAG = '{new_lag_val} {new_lag_unit}'")
                # Identifiers should be double-quoted with internal quotes escaped for security
                if new_wh.strip():
                    safe_wh = new_wh.strip().upper().replace('"', '""')
                    params.append(f'WAREHOUSE = "{safe_wh}"')
                if params:
                    final_sql = sql_set + ", ".join(params)
                    # Pass the active user ID from the context for better trace correlation
                    log_action("ALTER_SERVICE_START", {"sql": final_sql}, user_id=ctx.get("user", "anonymous"), trace_id=tid)
                    try:
                        res = session.sql(final_sql).collect()
                        log_action("ALTER_SERVICE_SUCCESS", {"result": [r.as_dict() for r in res]}, user_id=ctx.get("user", "anonymous"), trace_id=tid)
                        st.success("Parameters Updated")
                    except Exception as e:
                        log_action("ALTER_SERVICE_ERROR", {"error": str(e)}, user_id=ctx.get("user", "anonymous"), level="ERROR", trace_id=tid)
                        st.error(f"Update failed: {e}")

    # -------------------------------------------------------------------------
    # Automated RBAC & Multi-Role Grant
    # -------------------------------------------------------------------------
    st.divider()
    st.markdown("#### 🔐 RBAC (Active Schema)")
    
    # Auto-scan logic - Optimized to run only when needed
    if "admin_service_cache" not in st.session_state:
        tid_scan = uuid.uuid4().hex
        sql_scan = f"SHOW CORTEX SEARCH SERVICES IN SCHEMA \"{db}\".\"{schema}\""
        log_action("RBAC_AUTOSCAN_START", {"sql": sql_scan}, user_id=ctx.get("user", "anonymous"), trace_id=tid_scan)
        try:
            raw_svcs = session.sql(sql_scan).collect()
            active_svcs = []
            for s in raw_svcs:
                row_dict = s.as_dict()
                name = row_dict.get('name') or row_dict.get('NAME')
                status_raw = row_dict.get('status') or row_dict.get('STATUS')
                # Filter for active serving
                if status_raw and name:
                    if "active" in str(status_raw).lower():
                        active_svcs.append(name)
            st.session_state.admin_service_cache = sorted(list(set(active_svcs))) # Dedup and sort
            log_action("RBAC_AUTOSCAN_SUCCESS", {"count": len(active_svcs)}, user_id=ctx.get("user", "anonymous"), trace_id=tid_scan)
        except Exception as e:
            st.session_state.admin_service_cache = []
            log_action("RBAC_AUTOSCAN_ERROR", {"error": str(e)}, user_id=ctx.get("user", "anonymous"), level="ERROR", trace_id=tid_scan)
    
    svc_list = st.session_state.admin_service_cache
    
    # Auto-selection logic
    default_ix = 0
    if "last_deployed_service" in st.session_state:
        last_svc = st.session_state.last_deployed_service
        # Handle prefix/name matching safely
        for i, s in enumerate(svc_list):
            if s == last_svc or f"CSS_{s}" == last_svc:
                default_ix = i
                break
    
    target_svc = st.selectbox("Service", svc_list, index=default_ix if svc_list else None, key="rbac_svc")
    
    # Multi-Role Input
    target_role_input = st.text_input(
        "Roles (Comma Separated)", 
        placeholder="e.g. ACCOUNTADMIN, IT_AI, ANALYST", 
        key="rbac_role",
        help="Enter multiple roles separated by commas to bulk-grant permissions."
    )
    
    c1, c2 = st.columns(2)
    user_id = ctx.get("user", "anonymous")
    
    with c1:
        if st.button("Grant Access"):
             if not target_svc or not target_role_input.strip():
                 st.error("Service and Role(s) are required.")
             else:
                 roles = [r.strip().upper() for r in target_role_input.split(',') if r.strip()]
                 success_list = []
                 err_list = []
                 
                 # Generate one Trace ID for the entire logical transaction (the batch grant)
                 tid_batch = uuid.uuid4().hex
                 log_action("RBAC_GRANT_BATCH_START", {"service": target_svc, "roles": roles}, user_id=user_id, trace_id=tid_batch)
                 
                 for role in roles:
                     # Escape double quotes to prevent SQL injection in identifiers
                     safe_role = '"' + role.replace('"', '""') + '"'
                     safe_svc = target_svc.replace('"', '""')
                     safe_db = db.replace('"', '""')
                     safe_sch = schema.replace('"', '""')
                     try:
                         session.sql(f'GRANT USAGE ON CORTEX SEARCH SERVICE "{safe_db}"."{safe_sch}"."{safe_svc}" TO ROLE {safe_role}').collect()
                         session.sql(f'GRANT USAGE ON SCHEMA "{safe_db}"."{safe_sch}" TO ROLE {safe_role}').collect()
                         success_list.append(role)
                         log_action("RBAC_GRANT_SUCCESS", {"service": target_svc, "role": role}, user_id=user_id, trace_id=tid_batch)
                     except Exception as e:
                         err_msg = str(e)
                         err_list.append(f"{role}: {err_msg}")
                         log_action("RBAC_GRANT_ERROR", {"service": target_svc, "role": role, "error": err_msg}, user_id=user_id, level="ERROR", trace_id=tid_batch)
                 
                 if success_list:
                     st.success(f"Granted access to: {', '.join(success_list)}")
                     # Automatically clear the sticky notification once action is taken
                     if "last_deployed_service" in st.session_state:
                         del st.session_state.last_deployed_service
                 if err_list:
                     st.error(f"Errors occurred:\n" + "\n".join([f"- {e}" for e in err_list]))

    with c2:
        if st.button("Revoke Access"):
             if not target_svc or not target_role_input.strip():
                 st.error("Service and Role(s) are required.")
             else:
                 roles = [r.strip().upper() for r in target_role_input.split(',') if r.strip()]
                 success_list = []
                 err_list = []
                 
                 # Generate one Trace ID for the entire logical transaction (the batch revoke)
                 tid_batch = uuid.uuid4().hex
                 log_action("RBAC_REVOKE_BATCH_START", {"service": target_svc, "roles": roles}, user_id=user_id, trace_id=tid_batch)
                 
                 for role in roles:
                     # Escape double quotes to prevent SQL injection in identifiers
                     safe_role = '"' + role.replace('"', '""') + '"'
                     safe_svc = target_svc.replace('"', '""')
                     safe_db = db.replace('"', '""')
                     safe_sch = schema.replace('"', '""')
                     try:
                         session.sql(f'REVOKE USAGE ON CORTEX SEARCH SERVICE "{safe_db}"."{safe_sch}"."{safe_svc}" FROM ROLE {safe_role}').collect()
                         success_list.append(role)
                         log_action("RBAC_REVOKE_SUCCESS", {"service": target_svc, "role": role}, user_id=user_id, trace_id=tid_batch)
                     except Exception as e:
                         err_msg = str(e)
                         err_list.append(f"{role}: {err_msg}")
                         log_action("RBAC_REVOKE_ERROR", {"service": target_svc, "role": role, "error": err_msg}, user_id=user_id, level="ERROR", trace_id=tid_batch)
                 
                 if success_list:
                     st.success(f"Revoked access from: {', '.join(success_list)}")
                 if err_list:
                     st.error(f"Errors occurred:\n" + "\n".join([f"- {e}" for e in err_list]))
