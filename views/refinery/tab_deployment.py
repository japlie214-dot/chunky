# views/refinery/tab_deployment.py
# Deployment Tab - Cortex Search Deployment Orchestrator for the Doc Refinery package
import streamlit as st
import uuid
from logger_config import log_action
from utils.snowflake_utils import get_table_schema

from views.refinery.deployment_ui import (
    _fetch_and_validate_source_metadata,
    _render_service_config_section,
    _render_embedding_strategy_section,
    _render_sql_preview_section,
    _render_deployment_action_bar,
    _render_service_management_section,
    _render_rbac_management_section,
)
from views.refinery.deployment_logic import (
    _render_cost_estimation_section,
)


# -----------------------------------------------------------------------------
# render_deployment_tab - Top-Level Orchestrator
# -----------------------------------------------------------------------------

def render_deployment_tab(session):
    st.subheader("4. Cortex Search Deployment")
    ctx    = st.session_state.auth_context
    db, schema = ctx["db"], ctx["schema"]
    user   = ctx.get("user", "")

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

    tid_setup                       = uuid.uuid4().hex
    tgt_table_base, tgt_table_full  = _fetch_and_validate_source_metadata(session, db, schema, tid_setup)
    svc_config                      = _render_service_config_section(session, db, schema)

    exists, cols, _err = get_table_schema(session, db, schema, tgt_table_base)
    if not exists:
        st.error(f"❌ Table '{tgt_table_base}' is empty or does not exist. Cannot configure service.")
        return
    if not cols:
        st.error(f"❌ Table '{tgt_table_base}' has no columns. Cannot configure service.")
        return

    embedding_strategy  = _render_embedding_strategy_section(cols)
    cortex_sql_preview  = _render_sql_preview_section(db, schema, tgt_table_full, svc_config, embedding_strategy)

    if cortex_sql_preview:
        _render_cost_estimation_section(
            session, tgt_table_full,
            embedding_strategy['target_col'], embedding_strategy['selected_model'],
            svc_config['lag_val'], svc_config['lag_unit'],
        )
        _render_deployment_action_bar(cortex_sql_preview, svc_config, db, schema, user, session)

    _render_service_management_section(session, db, schema, user)
    _render_rbac_management_section(session, db, schema, user)
