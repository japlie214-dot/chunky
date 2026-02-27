# views/refinery/deployment_ui.py
# UI rendering functions for Cortex Search deployment - separated from execution logic
import streamlit as st
import uuid
from logger_config import log_action
from utils.snowflake_utils import scan_for_services
from utils.core_utils import clean_text_for_sql
from utils.auth_utils import get_user_mapped_roles
from utils.constants import EMBEDDING_MODELS, TARGET_LAG_UNITS
from views.refinery.deployment_logic import check_lag_warning, _execute_cortex_deployment


def _fetch_and_validate_source_metadata(session, db, schema, tid_setup):
    """
    Fetches and caches SHOW TABLES, renders the locked Context & Source UI block,
    and returns (tgt_table_base, tgt_table_full).
    Cache key: st.session_state['deployment_tables_cache'].
    Populated on first call; invalidated by _execute_cortex_deployment on success
    and by the sidebar Refresh Session button (Phase 4).
    """
    user = st.session_state.auth_context.get("user", "anonymous")
    if "deployment_tables_cache" not in st.session_state:
        try:
            sql_list = f"SHOW TABLES IN SCHEMA \"{db}\".\"{schema}\""
            log_action("METADATA_FETCH_START", {"sql": sql_list}, user_id=user, trace_id=tid_setup)
            tables_res = session.sql(sql_list).collect()
            log_action("METADATA_FETCH_SUCCESS", {"table_count": len(tables_res)}, user_id=user, trace_id=tid_setup)
            table_list = []
            for r in tables_res:
                row_dict = r.as_dict()
                name = row_dict.get('name') or row_dict.get('NAME')
                if name:
                    table_list.append(name)
            st.session_state.deployment_tables_cache = table_list
        except Exception as e:
            log_action("METADATA_FETCH_ERROR", {"error": str(e)}, user_id=user, level="ERROR", trace_id=tid_setup)
            st.session_state.deployment_tables_cache = ["SUS_CHUNKS"]

    table_list = st.session_state.deployment_tables_cache
    st.markdown("#### 📁 Context & Source")
    c_ctx1, c_ctx2 = st.columns(2)
    help_msg = "Context is locked by Gatekeeper. To change project, click 'Disconnect / Change' in the sidebar."
    with c_ctx1:
        st.text_input("Active Database", value=db, disabled=True, help=help_msg)
    with c_ctx2:
        st.text_input("Active Schema", value=schema, disabled=True, help=help_msg)

    table_idx = 0
    if table_list and "SUS_CHUNKS" in table_list:
        table_idx = table_list.index("SUS_CHUNKS")
    tgt_table_base = st.selectbox(
        "Source Table Name",
        options=table_list if table_list else ["SUS_CHUNKS"],
        index=table_idx if table_list else 0,
    )
    # PLAN-01: Ensure identifiers are escaped to handle special characters or mixed case
    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = tgt_table_base.replace('"', '""')
    tgt_table_full = f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'
    return tgt_table_base, tgt_table_full


def _render_service_config_section(session, db, schema):
    """
    Renders the Service Configuration UI block and returns a config dict.
    Fetches and caches SHOW WAREHOUSES once per session under
    st.session_state['deployment_warehouses_cache'] (no invalidation trigger;
    warehouse lists are stable within a session).
    """
    st.markdown("#### ⚙️ Service Configuration")
    c_pfx1, c_pfx2 = st.columns([1, 4])
    with c_pfx1:
        st.text_input("Prefix", value="CSS_", disabled=True, help="Standardized CSS Prefix")
    with c_pfx2:
        svc_user_name = st.text_input("Service Name", "RAG_V1", key="dep_svc_name").strip()

    full_svc_identifier = f"CSS_{svc_user_name}"

    c_infra1, c_infra2, c_infra3 = st.columns(3)
    with c_infra1:
        if "deployment_warehouses_cache" not in st.session_state:
            try:
                wh_data = session.sql("SHOW WAREHOUSES").collect()
                wh_list = []
                for r in wh_data:
                    row_dict = r.as_dict()
                    name = row_dict.get('name') or row_dict.get('NAME')
                    if name:
                        wh_list.append(name)
                st.session_state.deployment_warehouses_cache = wh_list
            except Exception:
                st.session_state.deployment_warehouses_cache = ["COMPUTE_WH"]
        warehouse_sel = st.selectbox("Warehouse", st.session_state.deployment_warehouses_cache, index=0)
    with c_infra2:
        lag_val = st.number_input("Target Lag", min_value=1, value=365)
    with c_infra3:
        lag_unit = st.selectbox("Lag Unit", TARGET_LAG_UNITS, index=2)

    svc_comment = st.text_area("Comment (Optional)", placeholder="Describe this search service...")

    st.markdown("#### 🛡️ Deployment RBAC")
    user_email      = st.session_state.auth_context.get("user", "")
    avail_roles     = get_user_mapped_roles(user_email)
    deploy_grant_roles = st.multiselect(
        "Grant Access To Roles", options=avail_roles, default=avail_roles,
        help="Roles that will automatically receive USAGE on the new Cortex Search Service.",
    )
    lag_warn = check_lag_warning(lag_val, lag_unit)
    if lag_warn:
        st.warning(lag_warn)

    return {
        "svc_user_name":       svc_user_name,
        "full_svc_identifier": full_svc_identifier,
        "warehouse_sel":       warehouse_sel,
        "lag_val":             lag_val,
        "lag_unit":            lag_unit,
        "svc_comment":         svc_comment,
        "deploy_grant_roles":  deploy_grant_roles,
    }


def _render_embedding_strategy_section(cols):
    """
    Renders the Embedding & Search Strategy and Column Configuration UI blocks.
    Receives cols from the pre-fetched schema; never calls get_table_schema internally.
    Returns an embedding strategy dict.
    """
    st.markdown("#### 🧠 Embedding & Search Strategy")
    model_options  = list(EMBEDDING_MODELS.keys())
    selected_model = st.selectbox("Embedding Model", options=model_options, index=0)
    model_meta     = EMBEDDING_MODELS[selected_model]
    if model_meta["warning"]:
        st.warning(model_meta["warning"])
    else:
        st.info(f"✅ Recommended: {model_meta['lang']} | {model_meta['context']} Context | {model_meta['dim']} Dim")

    target_col = st.selectbox(
        "Search Target Column (ON)", options=cols,
        index=cols.index("CHUNK") if "CHUNK" in cols else 0,
    )
    st.markdown("#### ⚙️ Column Configuration")
    # CHUNK_REF included by default when present; omitted for legacy tables without the column.
    # CHUNK_REF in SELECT is safe — LLM isolation is enforced inside retrieve_context, not here.
    default_sel = [c for c in ["CHUNK", "RELATIVE_PATH", "PAGE_NUMBER", "CHUNK_ID", "CHUNK_REF"] if c in cols]
    select_cols_list = st.multiselect(
        "Result Columns (SELECT)", cols, default=default_sel,
        key="dep_select_cols", help="Columns returned by the search service.",
    )
    safe_defaults = [c for c in ["PAGE_NUMBER", "RELATIVE_PATH"] if c in cols]
    atts = st.multiselect(
        "Filter Attributes (ATTRIBUTES)", cols, default=safe_defaults,
        key="dep_atts", help="Columns used for filtering (WHERE clause).",
    )
    return {
        "selected_model":   selected_model,
        "target_col":       target_col,
        "select_cols_list": select_cols_list,
        "atts":             atts,
    }


def _render_sql_preview_section(db, schema, tgt_table_full, svc_config, embedding_strategy):
    """
    Manages the 'Generate SQL Preview' button, DDL construction, and the
    cortex_sql_preview session state key.
    Deletes last_est on new generation to prevent stale cost estimates
    persisting alongside a modified DDL.
    Returns the current value of cortex_sql_preview (empty string if not yet generated).
    """
    if "cortex_sql_preview" not in st.session_state:
        st.session_state.cortex_sql_preview = ""

    if st.button("📝 Generate SQL Preview"):
        if "last_est" in st.session_state:
            del st.session_state.last_est

        if not svc_config["svc_user_name"]:
            st.error("❌ Service Name cannot be empty.")
        elif not embedding_strategy["select_cols_list"]:
            st.error("❌ You must select at least one Result Column (SELECT).")
        elif embedding_strategy["target_col"] not in embedding_strategy["select_cols_list"]:
            st.error(f"❌ Search Target '{embedding_strategy['target_col']}' must be included in Result Columns (SELECT).")
        else:
            try:
                # PLAN-02: Escape identifiers for manual SQL construction
                safe_db = db.replace('"', '""')
                safe_sch = schema.replace('"', '""')
                safe_svc = svc_config["full_svc_identifier"].replace('"', '""')
                safe_tgt = embedding_strategy["target_col"].replace('"', '""')
                
                quoted_atts    = [f'"{a.replace(chr(34), chr(34)+chr(34))}"' for a in embedding_strategy["atts"]]
                attr_clause    = f"ATTRIBUTES ({', '.join(quoted_atts)})" if embedding_strategy["atts"] else ""
                comment_clause = (
                    f"\nCOMMENT = '{clean_text_for_sql(svc_config['svc_comment'])}'"
                    if svc_config["svc_comment"] else ""
                )
                quoted_selects = [f'"{c.replace(chr(34), chr(34)+chr(34))}"' for c in embedding_strategy["select_cols_list"]]
                st.session_state.cortex_sql_preview = (
                    f'CREATE OR REPLACE CORTEX SEARCH SERVICE '
                    f'"{safe_db}"."{safe_sch}"."{safe_svc}"\n'
                    f'ON "{safe_tgt}" {attr_clause}\n'
                    f'WAREHOUSE = "{svc_config["warehouse_sel"]}"\n'
                    f"TARGET_LAG = '{svc_config['lag_val']} {svc_config['lag_unit']}'\n"
                    f"EMBEDDING_MODEL = '{embedding_strategy['selected_model']}'"
                    f"{comment_clause}\n"
                    f"AS (\n    SELECT {', '.join(quoted_selects)}\n    FROM {tgt_table_full}\n)"
                )
            except Exception as e:
                st.error(f"Generation failed: {e}")

    if st.session_state.cortex_sql_preview:
        st.markdown("#### 📜 SQL Preview & Edit")
        st.session_state.cortex_sql_preview = st.text_area(
            "Review DDL", value=st.session_state.cortex_sql_preview,
            height=300, key="cortex_ddl_editor",
        )
    return st.session_state.cortex_sql_preview


def _render_deployment_action_bar(cortex_sql_preview, svc_config, db, schema, user, session):
    """
    Renders the Execute & Deploy and Cancel buttons.
    Issues st.rerun() on success — the only location in the deployment flow
    where a rerun is triggered, keeping Streamlit lifecycle side-effects
    visible and confined to the UI layer.
    """
    c_exec, c_cancel = st.columns([1, 4])
    with c_exec:
        if st.button("🚀 Execute & Deploy", type="primary"):
            if not svc_config["deploy_grant_roles"]:
                st.error("❌ You must select at least one role for deployment grants.")
            else:
                success = _execute_cortex_deployment(
                    session, db, schema, cortex_sql_preview,
                    svc_config["full_svc_identifier"],
                    svc_config["deploy_grant_roles"], user,
                )
                if success:
                    st.rerun()
    with c_cancel:
        if st.button("❌ Cancel"):
            st.session_state.cortex_sql_preview = ""
            if "last_est" in st.session_state:
                del st.session_state.last_est
            st.rerun()


def _render_service_management_section(session, db, schema, user):
    """
    Renders the Service Management panel: DESCRIBE, live status metrics,
    and the Indexing/Serving/Configuration lifecycle tabs.
    Dual-format DESCRIBE normalization (Vertical PROPERTY/VALUE vs. Horizontal
    column-per-attribute) is carried over verbatim from the original monolith.
    """
    st.divider()
    st.header("🔧 Service Management")
    m_svc_list     = scan_for_services(session, db, schema)
    selected_m_svc = st.selectbox("Select Service to Manage", m_svc_list, key="m_svc_sel")

    if not selected_m_svc:
        return

    # PLAN-02: Escape identifiers for manual SQL construction
    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_svc = selected_m_svc.replace('"', '""')
    m_full_name = f'"{safe_db}"."{safe_sch}"."{safe_svc}"'
    
    svc_meta    = {}
    tid         = uuid.uuid4().hex
    d_sql       = f"DESCRIBE CORTEX SEARCH SERVICE {m_full_name}"
    log_action("DESCRIBE_SERVICE_START", {"sql": d_sql}, user_id=user, trace_id=tid)
    try:
        desc_rows = session.sql(d_sql).collect()
        log_action("DESCRIBE_SERVICE_SUCCESS", {"rows": [r.as_dict() for r in desc_rows]}, user_id=user, trace_id=tid)
        for r in desc_rows:
            row_dict = {k.upper().strip('"'): v for k, v in r.as_dict().items()}
            if "PROPERTY" in row_dict and "VALUE" in row_dict:
                svc_meta[str(row_dict["PROPERTY"]).upper().lower()] = row_dict["VALUE"]
            else:
                for k, v in row_dict.items():
                    svc_meta[k.lower()] = v
        svc_meta["indexing_status"] = svc_meta.get("indexing_state")
        svc_meta["serving_status"]  = svc_meta.get("serving_state")
    except Exception as e:
        log_action("DESCRIBE_SERVICE_ERROR", {"error": str(e)}, level="ERROR", user_id=user, trace_id=tid)
        st.warning(f"Could not fetch metadata: {e}")

    with st.container():
        st.markdown("##### 📡 Live Service Status")
        s_idx, s_srv, s_lag, s_wh = st.columns(4)
        s_idx.metric("Indexing",      svc_meta.get("indexing_status", "Unknown"))
        s_srv.metric("Serving",       svc_meta.get("serving_status",  "Unknown"))
        s_lag.metric("Current Lag",   svc_meta.get("target_lag",      "N/A"))
        s_wh.metric( "Warehouse",     svc_meta.get("warehouse",       "N/A"))

    m_tab1, m_tab2 = st.tabs(["⚡ Status & Refresh", "⚙️ Configuration"])
    with m_tab1:
        st.markdown("##### ⚙️ Indexing Control")
        c1, c2, c3 = st.columns(3)
        for btn_label, sql_verb, action_key, msg_fn in [
            ("▶️ Resume Indexing",  "RESUME INDEXING",  "RESUME_INDEX",  lambda: st.success("Indexing Resumed")),
            ("⏸️ Suspend Indexing", "SUSPEND INDEXING", "SUSPEND_INDEX", lambda: st.warning("Indexing Suspended")),
            ("🔄 Trigger Refresh",  "REFRESH",          "REFRESH",       lambda: st.info("Manual Refresh Triggered")),
        ]:
            col = c1 if "RESUME_INDEX" in action_key else (c2 if "SUSPEND" in action_key else c3)
            if col.button(btn_label):
                tid = uuid.uuid4().hex
                sql = f"ALTER CORTEX SEARCH SERVICE {m_full_name} {sql_verb}"
                log_action(f"SERVICE_{action_key}_START", {"sql": sql}, user_id=user, trace_id=tid)
                try:
                    session.sql(sql).collect()
                    log_action(f"SERVICE_{action_key}_SUCCESS", {"service": m_full_name}, user_id=user, trace_id=tid)
                    msg_fn()
                except Exception as e:
                    log_action(f"SERVICE_{action_key}_ERROR", {"error": str(e)}, user_id=user, level="ERROR", trace_id=tid)
                    st.error(f"Action failed: {e}")

        st.markdown("##### 🌐 Serving Control")
        s1, s2 = st.columns(2)
        for col, btn_label, sql_verb, action_key, msg_fn in [
            (s1, "▶️ Resume Serving",  "RESUME SERVING",  "RESUME_SERVING",  lambda: st.success("Serving Resumed")),
            (s2, "⏸️ Suspend Serving", "SUSPEND SERVING", "SUSPEND_SERVING", lambda: st.warning("Serving Suspended")),
        ]:
            if col.button(btn_label):
                tid = uuid.uuid4().hex
                sql = f"ALTER CORTEX SEARCH SERVICE {m_full_name} {sql_verb}"
                log_action(f"SERVICE_{action_key}_START", {"sql": sql}, user_id=user, trace_id=tid)
                try:
                    session.sql(sql).collect()
                    log_action(f"SERVICE_{action_key}_SUCCESS", {"service": m_full_name}, user_id=user, trace_id=tid)
                    msg_fn()
                except Exception as e:
                    log_action(f"SERVICE_{action_key}_ERROR", {"error": str(e)}, user_id=user, level="ERROR", trace_id=tid)
                    st.error(f"Action failed: {e}")

    with m_tab2:
        st.markdown("#### Update Parameters")
        st.caption(f"Current: `{svc_meta.get('target_lag')}` on `{svc_meta.get('warehouse')}`")
        new_lag_val  = st.number_input("New Target Lag", 1, 365, value=30, key="m_lag_val")
        new_lag_unit = st.selectbox("New Unit", TARGET_LAG_UNITS, index=2, key="m_lag_unit")
        m_lag_warn   = check_lag_warning(new_lag_val, new_lag_unit)
        if m_lag_warn:
            st.warning(m_lag_warn)
        new_wh = st.text_input("New Warehouse", key="m_wh")
        if st.button("💾 Apply SET Changes"):
            tid    = uuid.uuid4().hex
            params = []
            if new_lag_val:
                params.append(f"TARGET_LAG = '{new_lag_val} {new_lag_unit}'")
            if new_wh.strip():
                safe_wh = new_wh.strip().upper().replace('"', '""')
                params.append(f'WAREHOUSE = "{safe_wh}"')
            if params:
                final_sql = f"ALTER CORTEX SEARCH SERVICE {m_full_name} SET " + ", ".join(params)
                log_action("ALTER_SERVICE_START", {"sql": final_sql}, user_id=user, trace_id=tid)
                try:
                    res = session.sql(final_sql).collect()
                    log_action("ALTER_SERVICE_SUCCESS", {"result": [r.as_dict() for r in res]}, user_id=user, trace_id=tid)
                    st.success("Parameters Updated")
                except Exception as e:
                    log_action("ALTER_SERVICE_ERROR", {"error": str(e)}, user_id=user, level="ERROR", trace_id=tid)
                    st.error(f"Update failed: {e}")


def _render_rbac_management_section(session, db, schema, user_id):
    """
    Renders the RBAC panel with lazy-loaded service cache and multi-role
    Grant/Revoke buttons.
    The admin_service_cache guard (if key not in st.session_state) is intentionally
    inside this function to preserve lazy-evaluation semantics: the SHOW scan runs
    only when this section first renders after cache invalidation, not on every rerun.
    Clears last_deployed_service after a successful grant action.
    """
    st.divider()
    st.markdown("#### 🔐 RBAC (Active Schema)")

    if "admin_service_cache" not in st.session_state:
        # PLAN-02: Escape identifiers for manual SQL construction
        safe_db = db.replace('"', '""')
        safe_sch = schema.replace('"', '""')
        tid_scan = uuid.uuid4().hex
        sql_scan = f'SHOW CORTEX SEARCH SERVICES IN SCHEMA "{safe_db}"."{safe_sch}"'
        log_action("RBAC_AUTOSCAN_START", {"sql": sql_scan}, user_id=user_id, trace_id=tid_scan)
        try:
            raw_svcs    = session.sql(sql_scan).collect()
            active_svcs = []
            for s in raw_svcs:
                row_dict   = s.as_dict()
                name       = row_dict.get('name')   or row_dict.get('NAME')
                status_raw = row_dict.get('status') or row_dict.get('STATUS')
                if name and status_raw and "active" in str(status_raw).lower():
                    active_svcs.append(name)
            st.session_state.admin_service_cache = sorted(list(set(active_svcs)))
            log_action("RBAC_AUTOSCAN_SUCCESS", {"count": len(active_svcs)}, user_id=user_id, trace_id=tid_scan)
        except Exception as e:
            st.session_state.admin_service_cache = []
            log_action("RBAC_AUTOSCAN_ERROR", {"error": str(e)}, user_id=user_id, level="ERROR", trace_id=tid_scan)

    svc_list   = st.session_state.admin_service_cache
    default_ix = 0
    if "last_deployed_service" in st.session_state:
        last_svc = st.session_state.last_deployed_service
        for i, s in enumerate(svc_list):
            if s == last_svc or f"CSS_{s}" == last_svc:
                default_ix = i
                break

    target_svc        = st.selectbox("Service", svc_list, index=default_ix if svc_list else None, key="rbac_svc")
    target_role_input = st.text_input(
        "Roles (Comma Separated)",
        placeholder="e.g. ACCOUNTADMIN, IT_AI, ANALYST",
        key="rbac_role",
        help="Enter multiple roles separated by commas to bulk-grant permissions.",
    )
    c1, c2 = st.columns(2)

    def _run_rbac_action(action, sql_template):
        if not target_svc or not target_role_input.strip():
            st.error("Service and Role(s) are required.")
            return
        roles      = [r.strip().upper() for r in target_role_input.split(',') if r.strip()]
        success_list, err_list = [], []
        tid_batch  = uuid.uuid4().hex
        log_action(f"RBAC_{action}_BATCH_START", {"service": target_svc, "roles": roles}, user_id=user_id, trace_id=tid_batch)
        for role in roles:
            safe_role = '"' + role.replace('"', '""') + '"'
            safe_svc  = target_svc.replace('"', '""')
            safe_db   = db.replace('"', '""')
            safe_sch  = schema.replace('"', '""')
            try:
                for sql in sql_template(safe_db, safe_sch, safe_svc, safe_role):
                    session.sql(sql).collect()
                success_list.append(role)
                log_action(f"RBAC_{action}_SUCCESS", {"service": target_svc, "role": role}, user_id=user_id, trace_id=tid_batch)
            except Exception as e:
                err_msg = str(e)
                err_list.append(f"{role}: {err_msg}")
                log_action(f"RBAC_{action}_ERROR", {"service": target_svc, "role": role, "error": err_msg}, user_id=user_id, level="ERROR", trace_id=tid_batch)
        if success_list:
            st.success(f"{action.title()}ed access {'to' if action=='GRANT' else 'from'}: {', '.join(success_list)}")
            if action == "GRANT" and "last_deployed_service" in st.session_state:
                del st.session_state.last_deployed_service
        if err_list:
            st.error("Errors occurred:\n" + "\n".join([f"- {e}" for e in err_list]))

    with c1:
        if st.button("Grant Access"):
            _run_rbac_action(
                "GRANT",
                lambda db_, sch_, svc_, role_: [
                    f'GRANT USAGE ON CORTEX SEARCH SERVICE "{db_}"."{sch_}"."{svc_}" TO ROLE {role_}',
                    f'GRANT USAGE ON SCHEMA "{db_}"."{sch_}" TO ROLE {role_}',
                ],
            )
    with c2:
        if st.button("Revoke Access"):
            _run_rbac_action(
                "REVOKE",
                lambda db_, sch_, svc_, role_: [
                    f'REVOKE USAGE ON CORTEX SEARCH SERVICE "{db_}"."{sch_}"."{svc_}" FROM ROLE {role_}',
                ],
            )
