# views/demo/page4_complete.py
# Page 4: Search Service Configuration — search columns, attributes, target lag,
# warehouse, CREATE CORTEX SEARCH SERVICE execution, and privilege grants.

import re
import streamlit as st
import pandas as pd
from logger_config import log_action
from views.demo.common import render_header, nav_buttons, ctx
from utils.constants import (
    DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE,
    EMBEDDING_PRICING, CREDIT_TO_USD, USD_TO_IDR,
    TARGET_LAG_UNITS,
)

# --- Embedding model options (UI subset) ---
_EMBEDDING_MODEL_OPTIONS = [
    "snowflake-arctic-embed-l-v2.0",
    "snowflake-arctic-embed-m-v1.5",
]

# --- Search type options ---
_SEARCH_TYPE_OPTIONS = ["Hybrid (Text + Vector)", "Text", "Vector"]


def _get_current_warehouse(session):
    """Fetch the current warehouse from the Snowflake session."""
    try:
        res = session.sql("SELECT CURRENT_WAREHOUSE() AS WH").collect()
        if res and res[0]["WH"]:
            return res[0]["WH"]
    except Exception:
        pass
    return ""


def _fetch_table_columns(session, db, schema, jobs):
    """Fetch column names and types from the target table."""
    if not jobs:
        return []
    table_name = jobs[0]["table"].split(".")[-1]
    full_table = f'"{db}"."{schema}"."{table_name}"'
    try:
        res = session.sql(f"DESCRIBE TABLE {full_table}").collect()
        return [{"name": row["name"], "type": row["type"]} for row in res]
    except Exception:
        return []


def _init_search_config(table_columns):
    """Initialize default search/attribute config in session state if not set."""
    col_names = [c["name"] for c in table_columns]

    if "cssw_search_cols" not in st.session_state:
        defaults = []
        for c in col_names:
            is_chunk = c.upper() == "CHUNK"
            defaults.append({
                "select": is_chunk,
                "column": c,
                "search_type": "Hybrid (Text + Vector)" if is_chunk else "Text",
                "embedding_model": "snowflake-arctic-embed-l-v2.0" if is_chunk else "snowflake-arctic-embed-l-v2.0",
            })
        st.session_state.cssw_search_cols = defaults

    if "cssw_attribute_cols" not in st.session_state:
        auto_attrs = {"RELATIVE_PATH", "PAGE_NUMBER"}
        defaults = []
        for c in col_names:
            defaults.append({
                "select": c.upper() in auto_attrs,
                "column": c,
            })
        st.session_state.cssw_attribute_cols = defaults

    if "cssw_target_lag_num" not in st.session_state:
        st.session_state.cssw_target_lag_num = 365
    if "cssw_target_lag_unit" not in st.session_state:
        st.session_state.cssw_target_lag_unit = "days"


def _build_create_sql(svc_name, db, schema, table_name, search_rows, attr_rows,
                      warehouse, target_lag_num, target_lag_unit):
    """Build the CREATE CORTEX SEARCH SERVICE SQL."""
    full_table = f'"{db}"."{schema}"."{table_name}"'
    target_lag = f"{target_lag_num} {target_lag_unit}"

    # Collect selected columns by search type
    text_cols = []
    vector_cols = []
    for r in search_rows:
        if not r.get("select"):
            continue
        col = r["column"]
        stype = r.get("search_type", "")
        model = r.get("embedding_model", "")
        if "Text" in stype:
            text_cols.append(col)
        if "Vector" in stype or "Hybrid" in stype:
            vector_cols.append((col, model))

    selected_attrs = [r["column"] for r in attr_rows if r.get("select")]

    # All columns needed in the AS query
    all_search_cols = list(dict.fromkeys(
        [c for c in text_cols] + [c for c, _ in vector_cols]
    ))
    all_cols = list(dict.fromkeys(all_search_cols + selected_attrs))
    cols_sql = ", ".join(f'"{c}"' for c in all_cols)

    # Determine if single-index or multi-index
    use_single_index = (
        len(all_search_cols) == 1
        and len(vector_cols) <= 1
    )

    if use_single_index:
        # Single-index syntax: ON <col> [EMBEDDING_MODEL = ...]
        search_col = all_search_cols[0]
        sql = f'CREATE OR REPLACE CORTEX SEARCH SERVICE "{db}"."{schema}"."{svc_name}"\n'
        sql += f'  ON "{search_col}"\n'
        if selected_attrs:
            sql += f'  ATTRIBUTES {", ".join(f"""\"{a}\"""" for a in selected_attrs)}\n'
        sql += f'  WAREHOUSE = {warehouse}\n'
        sql += f"  TARGET_LAG = '{target_lag}'\n"
        if vector_cols:
            _, model = vector_cols[0]
            sql += f"  EMBEDDING_MODEL = '{model}'\n"
        sql += f"AS (\n  SELECT {cols_sql}\n  FROM {full_table}\n);"
    else:
        # Multi-index syntax: TEXT INDEXES ... VECTOR INDEXES ...
        sql = f'CREATE OR REPLACE CORTEX SEARCH SERVICE "{db}"."{schema}"."{svc_name}"\n'
        if text_cols:
            sql += f'  TEXT INDEXES {", ".join(f"""\"{c}\"""" for c in text_cols)}\n'
        if vector_cols:
            vec_parts = []
            for col, model in vector_cols:
                vec_parts.append(f'"{col}" (model=\'{model}\')')
            sql += f'  VECTOR INDEXES {", ".join(vec_parts)}\n'
        if selected_attrs:
            sql += f'  ATTRIBUTES {", ".join(f"""\"{a}\"""" for a in selected_attrs)}\n'
        sql += f'  WAREHOUSE = {warehouse}\n'
        sql += f"  TARGET_LAG = '{target_lag}'\n"
        sql += f"AS (\n  SELECT {cols_sql}\n  FROM {full_table}\n);"

    return sql


def _grant_search_service_privileges(session, db, schema, svc_name, roles):
    """Grant USAGE on the Cortex Search Service to specified roles."""
    from utils.snowflake_utils import execute_grant_with_retry
    import re as _re

    full_svc = f'"{db}"."{schema}"."{svc_name}"'
    results = {"success": [], "failed": []}
    role_pattern = _re.compile(r'^([A-Z_][A-Z0-9_$]*|"[^"]+")$', _re.IGNORECASE)

    for role in roles:
        if not role_pattern.match(role):
            results["failed"].append(f"{role} (Invalid Syntax)")
            continue
        if role.startswith('"') and role.endswith('"'):
            grant_sql = f"GRANT USAGE ON CORTEX SEARCH SERVICE {full_svc} TO ROLE {role}"
        else:
            safe_role = role.upper().replace('"', '""')
            grant_sql = f'GRANT USAGE ON CORTEX SEARCH SERVICE {full_svc} TO ROLE "{safe_role}"'
        res = execute_grant_with_retry(session, grant_sql, "", role.upper())
        if res == "Failed":
            results["failed"].append(role.upper())
        else:
            results["success"].append(role.upper())

    return results


def render(session):
    render_header(4)

    c = ctx()
    db = c.get("db", DEFAULT_DB)
    schema = c.get("schema", DEFAULT_SCHEMA)
    svc_name = st.session_state.get("_wiz_svc_name", "CSS_")
    role = st.session_state.get("_wiz_role", "")
    jobs = st.session_state.get("cssw_jobs", [])
    table_name = jobs[0]["table"].split(".")[-1] if jobs else ""

    # --- Fetch table columns (from Step 3 cache or fresh query) ---
    table_columns = st.session_state.get("cssw_table_columns", [])
    if not table_columns and jobs:
        table_columns = _fetch_table_columns(session, db, schema, jobs)
        if table_columns:
            st.session_state.cssw_table_columns = table_columns

    if not table_columns:
        st.warning("⚠️ No table columns available. Go back to Step 3 and run the batch first.")
        nav_buttons(can_next=False, show_back=True)
        return

    _init_search_config(table_columns)
    col_names = [c["name"] for c in table_columns]

    # --- Current warehouse (auto-detect, hidden from user) ---
    warehouse = _get_current_warehouse(session)
    if not warehouse:
        warehouse = st.session_state.get("_wiz_warehouse", "")
    if not warehouse:
        st.error("❌ Could not detect current warehouse. Please ensure a warehouse is active.")
        nav_buttons(can_next=False)
        return
    st.session_state["_wiz_warehouse"] = warehouse

    # =========================================================================
    # Section 1: Search Column Selection
    # =========================================================================
    st.markdown("#### 🔍 Search Columns")
    st.caption("Select columns to index for search. CHUNK is auto-selected.")

    search_filter = st.text_input(
        "Filter by Column Name", "", key="cssw_search_filter",
        placeholder="e.g. CHUNK, TITLE..."
    )

    # Apply filter
    filtered_indices = []
    for i, row in enumerate(st.session_state.cssw_search_cols):
        if search_filter.lower() in row["column"].lower():
            filtered_indices.append(i)

    if filtered_indices:
        search_data = [st.session_state.cssw_search_cols[i] for i in filtered_indices]
        search_df = pd.DataFrame(search_data)

        edited_search = st.data_editor(
            search_df,
            column_config={
                "select": st.column_config.CheckboxColumn("Select", width="small"),
                "column": st.column_config.TextColumn("Column Name", disabled=True, width="medium"),
                "search_type": st.column_config.SelectboxColumn(
                    "Search Type", options=_SEARCH_TYPE_OPTIONS, width="medium"
                ),
                "embedding_model": st.column_config.SelectboxColumn(
                    "Embedding Model", options=_EMBEDDING_MODEL_OPTIONS, width="medium"
                ),
            },
            disabled=["column"],
            use_container_width=True,
            hide_index=True,
            key="cssw_search_editor",
        )

        # Sync edits back to session state
        for idx_pos, orig_idx in enumerate(filtered_indices):
            if idx_pos < len(edited_search):
                edited_row = edited_search.iloc[idx_pos]
                st.session_state.cssw_search_cols[orig_idx]["select"] = bool(edited_row["select"])
                st.session_state.cssw_search_cols[orig_idx]["search_type"] = edited_row["search_type"]
                st.session_state.cssw_search_cols[orig_idx]["embedding_model"] = edited_row["embedding_model"]
    else:
        st.info("No columns match the filter.")

    # Validation: at least one search column selected
    any_search_selected = any(r["select"] for r in st.session_state.cssw_search_cols)
    if not any_search_selected:
        st.error("❌ Select at least one search column.")

    # --- Search type & embedding cost explanation ---
    with st.expander("ℹ️ Search Types & Embedding Model Costs", expanded=False):
        st.markdown("""
**Search Types:**
- **Hybrid (Text + Vector):** Combines keyword (lexical) search with semantic (vector) search for the best relevance. Recommended for most use cases.
- **Text:** Keyword-based search only. Faster and cheaper, but misses semantic meaning.
- **Vector:** Semantic search only. Understands meaning but may miss exact keyword matches.

**Embedding Models (cost per 1 million tokens):**
""")
        for model in _EMBEDDING_MODEL_OPTIONS:
            credits = EMBEDDING_PRICING.get(model, 0)
            usd = credits * CREDIT_TO_USD
            idr = usd * USD_TO_IDR
            st.markdown(f"- **{model}:** {credits:.2f} AI Credits ≈ ${usd:.2f} ≈ Rp {idr:,.0f}")
        st.caption("1 AI Credit ≈ $3.71 ≈ Rp 66,780")

    st.divider()

    # =========================================================================
    # Section 2: Attribute Column Selection
    # =========================================================================
    st.markdown("#### 🏷️ Attribute Columns")
    st.caption("Columns available for filtering queries. RELATIVE_PATH and PAGE_NUMBER are auto-selected.")

    attr_filter = st.text_input(
        "Filter by Column Name", "", key="cssw_attr_filter",
        placeholder="e.g. RELATIVE_PATH, PAGE_NUMBER..."
    )

    filtered_attr_indices = []
    for i, row in enumerate(st.session_state.cssw_attribute_cols):
        if attr_filter.lower() in row["column"].lower():
            filtered_attr_indices.append(i)

    if filtered_attr_indices:
        attr_data = [st.session_state.cssw_attribute_cols[i] for i in filtered_attr_indices]
        attr_df = pd.DataFrame(attr_data)

        edited_attr = st.data_editor(
            attr_df,
            column_config={
                "select": st.column_config.CheckboxColumn("Select", width="small"),
                "column": st.column_config.TextColumn("Column Name", disabled=True, width="large"),
            },
            disabled=["column"],
            use_container_width=True,
            hide_index=True,
            key="cssw_attr_editor",
        )

        for idx_pos, orig_idx in enumerate(filtered_attr_indices):
            if idx_pos < len(edited_attr):
                edited_row = edited_attr.iloc[idx_pos]
                st.session_state.cssw_attribute_cols[orig_idx]["select"] = bool(edited_row["select"])
    else:
        st.info("No columns match the filter.")

    st.divider()

    # =========================================================================
    # Section 3: Target Lag
    # =========================================================================
    st.markdown("#### ⏱️ Target Lag")
    st.caption("Maximum time the search service content should lag behind source table updates.")

    tl1, tl2 = st.columns([1, 2])
    with tl1:
        lag_num = st.number_input(
            "Number", min_value=1, value=st.session_state.cssw_target_lag_num,
            key="cssw_lag_num_widget"
        )
        st.session_state.cssw_target_lag_num = lag_num
    with tl2:
        lag_unit_idx = TARGET_LAG_UNITS.index(st.session_state.cssw_target_lag_unit) \
            if st.session_state.cssw_target_lag_unit in TARGET_LAG_UNITS else 2
        lag_unit = st.selectbox(
            "Unit", TARGET_LAG_UNITS, index=lag_unit_idx,
            key="cssw_lag_unit_widget"
        )
        st.session_state.cssw_target_lag_unit = lag_unit

    st.divider()

    # =========================================================================
    # Section 4: Preview SQL
    # =========================================================================
    st.markdown("#### 📝 Service Configuration Preview")

    search_rows = st.session_state.cssw_search_cols
    attr_rows = st.session_state.cssw_attribute_cols
    create_sql = _build_create_sql(
        svc_name, db, schema, table_name,
        search_rows, attr_rows, warehouse, lag_num, lag_unit
    )

    with st.expander("View CREATE CORTEX SEARCH SERVICE SQL", expanded=True):
        st.code(create_sql, language="sql")

    # Show selected roles for grants
    user_roles = [role] if role else []
    # Also include roles from jobs (grant_roles)
    for j in jobs:
        for gr in j.get("grant_roles", []):
            if gr and gr not in user_roles:
                user_roles.append(gr)

    if user_roles:
        st.markdown(f"**Privileges will be granted to:** {', '.join(f'`{r}`' for r in user_roles)}")

    st.divider()

    # =========================================================================
    # Section 5: Execute
    # =========================================================================
    st.markdown("#### 🚀 Create Search Service")

    svc_created = st.session_state.get("cssw_svc_created", False)

    if not svc_created:
        if not any_search_selected:
            st.button("🚀 Create Cortex Search Service", disabled=True,
                       key="cssw_create_disabled")
        elif st.button("🚀 Create Cortex Search Service", type="primary",
                       key="cssw_create_btn"):
            # --- Step 1: Create the service ---
            with st.spinner(f"Creating Cortex Search Service `{svc_name}`..."):
                try:
                    session.sql(create_sql).collect()
                    st.session_state.cssw_svc_created = True
                    st.success(f"✅ Cortex Search Service `{svc_name}` created successfully!")
                    log_action("CSSW_SVC_CREATED", {"svc": svc_name, "db": db, "schema": schema})
                except Exception as e:
                    st.error(f"❌ Failed to create service: {e}")
                    log_action("CSSW_SVC_CREATE_ERROR", {"svc": svc_name, "error": str(e)}, level="ERROR")
                    return

            # --- Step 2: Grant privileges on the search service ---
            if user_roles:
                with st.spinner(f"Granting privileges to {', '.join(user_roles)}..."):
                    grant_results = _grant_search_service_privileges(
                        session, db, schema, svc_name, user_roles
                    )
                    if grant_results["success"]:
                        st.success(f"✅ USAGE granted to: {', '.join(grant_results['success'])}")
                    if grant_results["failed"]:
                        st.warning(f"⚠️ Grants failed for: {', '.join(grant_results['failed'])}")

            # --- Step 3: Grant SELECT on source table to roles ---
            if user_roles and table_name:
                full_table = f'"{db}"."{schema}"."{table_name}"'
                from utils.snowflake_utils import execute_grant_with_retry
                for r in user_roles:
                    if r.upper() == "IT_AI":
                        continue  # IT_AI is already the owner
                    safe_role = r.upper().replace('"', '""')
                    grant_sql = f'GRANT SELECT ON TABLE {full_table} TO ROLE "{safe_role}"'
                    execute_grant_with_retry(session, grant_sql, "", safe_role)

            st.rerun()

    if svc_created:
        st.success(f"🎉 Cortex Search Service **`{svc_name}`** is ready!")
        st.markdown(f"- **Database:** `{db}`")
        st.markdown(f"- **Schema:** `{schema}`")
        st.markdown(f"- **Warehouse:** `{warehouse}`")
        st.markdown(f"- **Target Lag:** `{lag_num} {lag_unit}`")

        search_selected = [r for r in st.session_state.cssw_search_cols if r.get("select")]
        if search_selected:
            st.markdown("**Search Columns:**")
            for r in search_selected:
                st.markdown(f"- `{r['column']}` — {r['search_type']} ({r['embedding_model']})")

        attr_selected = [r for r in st.session_state.cssw_attribute_cols if r.get("select")]
        if attr_selected:
            attr_names = ', '.join('`' + r['column'] + '`' for r in attr_selected)
            st.markdown(f"**Attributes:** {attr_names}")

        st.divider()
        if st.button("🔄 Create Another Service"):
            for key in list(st.session_state.keys()):
                if key.startswith("cssw_") or key.startswith("_jbv_") or key.startswith("_wiz_"):
                    del st.session_state[key]
            st.rerun()

    nav_buttons(can_next=False, show_back=True)
