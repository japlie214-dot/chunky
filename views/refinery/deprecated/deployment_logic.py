# views/refinery/deployment_logic.py
# Core execution logic for Cortex Search deployment - separated from UI rendering
import uuid
import streamlit as st
from logger_config import log_action
from utils.snowflake_utils import execute_grant_with_retry
from utils.core_utils import display_cost_card
from utils.constants import EMBEDDING_PRICING, CREDIT_TO_USD, CREDIT_TO_IDR


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


def _render_cost_estimation_section(session, tgt_table_full, target_col,
                                     selected_model, lag_val, lag_unit):
    """
    Manages the 'Calculate Estimated Embedding Cost' button, sampling queries,
    COUNT_TOKENS call, last_est session state key, and cost card display.
    Invoked only when cortex_sql_preview is non-empty.
    Logs effective sampled_rows count for audit (see Phase 4).
    """
    st.markdown("##### 💰 Embedding Cost Estimation")
    if st.button("🔌 Calculate Estimated Embedding Cost", key="calc_est_btn"):
        with st.spinner("Sampling rows and counting tokens..."):
            tid_est = uuid.uuid4().hex
            user    = st.session_state.auth_context.get("user", "anonymous")
            try:
                # PLAN-02: Escape column identifier
                safe_tgt_col = target_col.replace('"', '""')
                
                sql_cnt = f"SELECT COUNT(*) FROM {tgt_table_full}"
                log_action("COST_EST_ROW_COUNT_START", {"sql": sql_cnt}, user_id=user, trace_id=tid_est)
                total_rows = session.sql(sql_cnt).collect()[0][0]
                log_action("COST_EST_ROW_COUNT_SUCCESS", {"total_rows": total_rows}, user_id=user, trace_id=tid_est)

                sql_sample  = f'SELECT "{safe_tgt_col}" FROM {tgt_table_full} SAMPLE (100 ROWS)'
                log_action("COST_EST_SAMPLE_START", {"sql": sql_sample}, user_id=user, trace_id=tid_est)
                sample_rows = session.sql(sql_sample).collect()
                log_action("COST_EST_SAMPLE_SUCCESS", {"retrieved_count": len(sample_rows)}, user_id=user, trace_id=tid_est)

                accumulated_text  = ""
                sampled_rows_count = 0
                BATCH_LIMIT       = 600000
                for row in sample_rows:
                    accumulated_text   += str(row[0]) + " "
                    sampled_rows_count += 1
                    if len(accumulated_text) >= BATCH_LIMIT:
                        break

                # Phase 4: log effective sample for audit; surface warning if BATCH_LIMIT hit
                log_action(
                    "COST_EST_SAMPLE_EFFECTIVE",
                    {
                        "sampled_rows":    sampled_rows_count,
                        "intended_rows":   len(sample_rows),
                        "chars_accumulated": len(accumulated_text),
                        "batch_limit_hit": len(accumulated_text) >= BATCH_LIMIT,
                    },
                    user_id=user, trace_id=tid_est,
                )

                total_sampled_tokens = 0
                if accumulated_text:
                    token_model_map = {"snowflake-arctic-embed-l-v2.0-8k": "snowflake-arctic-embed-l-v2.0"}
                    token_model     = token_model_map.get(selected_model, selected_model)
                    t_sql = "SELECT SNOWFLAKE.CORTEX.COUNT_TOKENS(?, ?)"
                    log_action("COST_EST_TOKENS_START", {"model": token_model, "chars": len(accumulated_text)}, user_id=user, trace_id=tid_est)
                    total_sampled_tokens = session.sql(t_sql, params=[token_model, accumulated_text]).collect()[0][0]
                    log_action("COST_EST_TOKENS_SUCCESS", {"tokens": total_sampled_tokens}, user_id=user, trace_id=tid_est)

                if sampled_rows_count > 0:
                    avg_tokens_per_row = total_sampled_tokens / sampled_rows_count
                    total_est_tokens   = avg_tokens_per_row * total_rows
                    price_per_1m       = EMBEDDING_PRICING.get(selected_model, 0.05)
                    est_credits        = (total_est_tokens / 1_000_000) * price_per_1m
                    st.session_state.last_est = {
                        "credits":         est_credits,
                        "usd":             est_credits * CREDIT_TO_USD,
                        "idr":             est_credits * CREDIT_TO_IDR,
                        "total_rows":      total_rows,
                        "sampled_rows":    sampled_rows_count,
                        "intended_sample": len(sample_rows),
                        "avg_tokens":      avg_tokens_per_row,
                        "price_rate":      price_per_1m,
                        "lag":             f"{lag_val} {lag_unit}",
                        "model":           selected_model,
                    }
            except Exception as e:
                log_action("COST_EST_ERROR", {"error": str(e)}, user_id=user, level="ERROR", trace_id=tid_est)
                st.error(f"Estimation failed: {e}")

    if "last_est" in st.session_state:
        est = st.session_state.last_est
        if est['total_rows'] > 100:
            st.warning(f"⚠️ **Sampling Active:** Estimate based on {est['sampled_rows']} rows out of {est['total_rows']:,} total.")
        if est['sampled_rows'] < est['intended_sample']:
            st.caption(f"ℹ️ *Sample capped at {est['sampled_rows']} rows to stay within processing character limits.*")
        with st.expander("📝 View Calculation Variables & Formula", expanded=True):
            st.markdown(f"**Formula:** `(Avg Tokens/Row * Total Rows / 1,000,000) * Credit Rate` (@ {est['model']})")
            v1, v2, v3, v4 = st.columns(4)
            v1.metric("Total Rows", f"{est['total_rows']:,}")
            v2.metric("Sampled Rows", est['sampled_rows'])
            v3.metric("Avg Tokens/Row", f"{est['avg_tokens']:.2f}")
            per_token = est['price_rate'] / 1_000_000
            v4.metric("Credit Rate (Cr / 1M Tokens)", f"{est['price_rate']:.2f}",
                      help=f"Granular Cost: {per_token:.10f} Cr / Token")
            st.divider()
            display_cost_card("Estimated Embedding Cost", est['credits'])
            st.caption(f"*Conversion Rate: 1 Cr = ${CREDIT_TO_USD:.2f} = Rp {CREDIT_TO_IDR:,.0f}*")
        st.info(f"💡 **Recurrence:** You will pay approx. **{est['credits']:.6f} Credits** every **{est['lag']}**.")


def _execute_cortex_deployment(session, db, schema, final_sql,
                                full_svc_identifier, deploy_grant_roles, user):
    """
    Validates, executes the CREATE OR REPLACE CORTEX SEARCH SERVICE DDL,
    runs the multi-role GRANT loop, and sets sticky session state on success.
    Returns True on success, False on any validation or execution failure.
    Never calls st.rerun() — that responsibility belongs to the calling UI layer.
    On success: invalidates admin_service_cache and deployment_tables_cache
    so the next render cycle fetches fresh lists.
    """
    # PLAN-02: Escape identifiers for the security prefix check
    safe_db_pfx = db.replace('"', '""')
    safe_sch_pfx = schema.replace('"', '""')
    required_target_prefix = f'CREATE OR REPLACE CORTEX SEARCH SERVICE "{safe_db_pfx}"."{safe_sch_pfx}"."CSS_'.upper()
    
    if required_target_prefix not in final_sql.upper():
        st.error("⛔ **Security Violation!**")
        st.markdown(
            f"The SQL target must be in your secured context and follow naming standards: "
            f"`{db}.{schema}.CSS_...`\n\n"
            "**Options:**\n"
            "1. Update the SQL to use the correct Database/Schema and CSS_ prefix.\n"
            "2. Click **'❌ Disconnect / Change'** in the Sidebar.\n"
            "3. Copy this SQL and execute it manually in a **Snowflake Worksheet**."
        )
        return False

    forbidden = ["DROP TABLE", "DELETE FROM", "TRUNCATE", "ALTER TABLE"]
    if any(k in final_sql.upper() for k in forbidden):
        st.error("⛔ Security Block: Destructive keywords detected.")
        return False

    tid_deploy = uuid.uuid4().hex
    try:
        with st.spinner("Deploying..."):
            log_action("DEPLOY_SERVICE_START", {"sql": final_sql}, user_id=user, trace_id=tid_deploy)
            session.sql(final_sql).collect()
            log_action("DEPLOY_SERVICE_SUCCESS", {"service": full_svc_identifier}, user_id=user, trace_id=tid_deploy)

            grant_successes = []
            for role in deploy_grant_roles:
                # PLAN-02: Escape all identifiers in manual SQL construction
                safe_db = db.replace('"', '""')
                safe_sch = schema.replace('"', '""')
                safe_svc = full_svc_identifier.replace('"', '""')
                safe_role = role.upper().replace('"', '""')
                
                grant_sql = (
                    f'GRANT USAGE ON CORTEX SEARCH SERVICE '
                    f'"{safe_db}"."{safe_sch}"."{safe_svc}" TO ROLE "{safe_role}"'
                )
                grant_res = execute_grant_with_retry(session, grant_sql, user, role.upper())
                if grant_res != "Failed":
                    grant_successes.append(grant_res)
            if grant_successes:
                st.toast(f"Access granted to: {', '.join(grant_successes)}")

            st.session_state.last_deployed_service  = full_svc_identifier
            st.session_state.cortex_sql_preview      = ""
            # Invalidate both caches so next render reflects post-deployment state
            for _key in ["admin_service_cache", "deployment_tables_cache"]:
                if _key in st.session_state:
                    del st.session_state[_key]
            return True
    except Exception as e:
        log_action("DEPLOY_SERVICE_ERROR", {"error": str(e)}, user_id=user, level="ERROR", trace_id=tid_deploy)
        st.error(f"Deployment Failed: {e}")
        return False
