# views/demo/page4_complete.py
# Page 4: Search Service Configuration — search columns, attributes, target lag,
# warehouse, CREATE CORTEX SEARCH SERVICE execution, and privilege grants.
# Supports multi-table: each job can target a different table.

import re
import traceback
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
    except Exception as e:
        log_action("WAREHOUSE_DETECT_ERROR", {"error": str(e)}, level="WARNING")
    return ""


def _fetch_all_table_columns(session, db, schema, jobs):
    """Fetch column names and types from ALL unique target tables."""
    seen = {}
    for j in jobs:
        tbl = j["table"].split(".")[-1]
        if tbl in seen:
            continue
        full_table = f'"{db}"."{schema}"."{tbl}"'
        try:
            res = session.sql(f"DESCRIBE TABLE {full_table}").collect()
            seen[tbl] = [{"name": row["name"], "type": row["type"], "table": tbl} for row in res]
        except Exception as e:
            log_action("DESCRIBE_TABLE_ERROR", {"table": full_table, "error": str(e)}, level="WARNING")
            seen[tbl] = []
    return seen


def _init_search_config(all_table_columns):
    """Initialize default search/attribute config in session state if not set.
    all_table_columns: dict {table_name: [cols]}
    """
    if "cssw_search_cols" not in st.session_state:
        defaults = []
        for tbl, cols in all_table_columns.items():
            for c in cols:
                col_name = c["name"]
                is_chunk = col_name.upper() == "CHUNK"
                defaults.append({
                    "select": is_chunk,
                    "table": tbl,
                    "column": col_name,
                    "search_type": "Hybrid (Text + Vector)" if is_chunk else "Text",
                    "embedding_model": "snowflake-arctic-embed-l-v2.0",
                })
        st.session_state.cssw_search_cols = defaults

    if "cssw_attribute_cols" not in st.session_state:
        auto_attrs = {"RELATIVE_PATH", "PAGE_NUMBER"}
        defaults = []
        for tbl, cols in all_table_columns.items():
            for c in cols:
                col_name = c["name"]
                defaults.append({
                    "select": col_name.upper() in auto_attrs,
                    "table": tbl,
                    "column": col_name,
                })
        st.session_state.cssw_attribute_cols = defaults

    if "cssw_target_lag_num" not in st.session_state:
        st.session_state.cssw_target_lag_num = 365
    if "cssw_target_lag_unit" not in st.session_state:
        st.session_state.cssw_target_lag_unit = "days"


def _build_create_sql(svc_name, db, schema, table_name, search_rows, attr_rows,
                      warehouse, target_lag_num, target_lag_unit):
    """Build the CREATE CORTEX SEARCH SERVICE SQL for one table."""
    full_table = f'"{db}"."{schema}"."{table_name}"'
    target_lag = f"{target_lag_num} {target_lag_unit}"

    text_cols = []
    vector_cols = []
    for r in search_rows:
        if not r.get("select") or r.get("table") != table_name:
            continue
        col = r["column"]
        stype = r.get("search_type", "")
        model = r.get("embedding_model", "")
        if "Text" in stype:
            text_cols.append(col)
        if "Vector" in stype or "Hybrid" in stype:
            vector_cols.append((col, model))

    selected_attrs = [r["column"] for r in attr_rows
                      if r.get("select") and r.get("table") == table_name]

    all_search_cols = list(dict.fromkeys(
        [c for c in text_cols] + [c for c, _ in vector_cols]
    ))
    all_cols = list(dict.fromkeys(all_search_cols + selected_attrs))
    cols_sql = ", ".join(f'"{c}"' for c in all_cols)

    use_single_index = len(all_search_cols) == 1 and len(vector_cols) <= 1

    if use_single_index:
        search_col = all_search_cols[0]
        sql = f'CREATE OR REPLACE CORTEX SEARCH SERVICE "{db}"."{schema}"."{svc_name}"\n'
        sql += f'  ON "{search_col}"\n'
        if selected_attrs:
            sql += f'  ATTRIBUTES {", ".join(f"\"{a}\"" for a in selected_attrs)}\n'
        sql += f"  WAREHOUSE = {warehouse}\n"
        sql += f"  TARGET_LAG = '{target_lag}'\n"
        if vector_cols:
            _, model = vector_cols[0]
            sql += f"  EMBEDDING_MODEL = '{model}'\n"
        sql += f"AS (\n  SELECT {cols_sql}\n  FROM {full_table}\n);"
    else:
        sql = f'CREATE OR REPLACE CORTEX SEARCH SERVICE "{db}"."{schema}"."{svc_name}"\n'
        if text_cols:
            sql += f'  TEXT INDEXES {", ".join(f"\"{c}\"" for c in text_cols)}\n'
        if vector_cols:
            vec_parts = [f'"{col}" (model=\'{model}\')' for col, model in vector_cols]
            sql += f"  VECTOR INDEXES {', '.join(vec_parts)}\n"
        if selected_attrs:
            sql += f'  ATTRIBUTES {", ".join(f"\"{a}\"" for a in selected_attrs)}\n'
        sql += f"  WAREHOUSE = {warehouse}\n"
        sql += f"  TARGET_LAG = '{target_lag}'\n"
        sql += f"AS (\n  SELECT {cols_sql}\n  FROM {full_table}\n);"

    return sql


def _grant_search_service_privileges(session, db, schema, svc_name, roles):
    """Grant USAGE on the Cortex Search Service to specified roles."""
    from utils.snowflake_utils import execute_grant_with_retry

    full_svc = f'"{db}"."{schema}"."{svc_name}"'
    results = {"success": [], "failed": []}
    role_pattern = re.compile(r'^([A-Z_][A-Z0-9_$]*|"[^"]+")$', re.IGNORECASE)

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


def _grant_table_select(session, db, schema, table_names, roles):
    """Grant SELECT on source tables to specified roles (excluding IT_AI)."""
    from utils.snowflake_utils import execute_grant_with_retry
    results = {"success": [], "failed": []}
    role_pattern = re.compile(r'^([A-Z_][A-Z0-9_$]*|"[^"]+")$', re.IGNORECASE)

    for tbl in table_names:
        full_table = f'"{db}"."{schema}"."{tbl}"'
        for role in roles:
            if role.upper() == "IT_AI":
                continue
            if not role_pattern.match(role):
                continue
            if role.startswith('"') and role.endswith('"'):
                grant_sql = f"GRANT SELECT ON TABLE {full_table} TO ROLE {role}"
            else:
                safe_role = role.upper().replace('"', '""')
                grant_sql = f'GRANT SELECT ON TABLE {full_table} TO ROLE "{safe_role}"'
            res = execute_grant_with_retry(session, grant_sql, "", role.upper())
            if res == "Failed":
                results["failed"].append(f"{role}@{tbl}")
            else:
                results["success"].append(f"{role}@{tbl}")

    return results


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _section_search_columns(table_names):
    """Section 1: Search Column Selection with Table Name column."""
    st.markdown("#### 🔍 Search Columns")
    st.caption("Select columns to index for search. CHUNK is auto-selected per table.")

    sf1, sf2 = st.columns(2)
    with sf1:
        search_filter = st.text_input(
            "Filter by Column Name", "", key="cssw_search_filter",
            placeholder="e.g. CHUNK, TITLE..."
        )
    with sf2:
        table_filter = st.selectbox(
            "Filter by Table", ["All Tables"] + table_names,
            key="cssw_search_table_filter"
        )

    filtered_indices = []
    for i, row in enumerate(st.session_state.cssw_search_cols):
        col_match = search_filter.lower() in row["column"].lower()
        tbl_match = table_filter == "All Tables" or row.get("table", "") == table_filter
        if col_match and tbl_match:
            filtered_indices.append(i)

    if filtered_indices:
        search_data = [st.session_state.cssw_search_cols[i] for i in filtered_indices]
        search_df = pd.DataFrame(search_data)

        edited_search = st.data_editor(
            search_df,
            column_config={
                "select": st.column_config.CheckboxColumn("Select", width="small"),
                "table": st.column_config.TextColumn("Table Name", disabled=True, width="medium"),
                "column": st.column_config.TextColumn("Column Name", disabled=True, width="medium"),
                "search_type": st.column_config.SelectboxColumn(
                    "Search Type", options=_SEARCH_TYPE_OPTIONS, width="medium"
                ),
                "embedding_model": st.column_config.SelectboxColumn(
                    "Embedding Model", options=_EMBEDDING_MODEL_OPTIONS, width="medium"
                ),
            },
            disabled=["table", "column"],
            use_container_width=True,
            hide_index=True,
            key="cssw_search_editor",
        )

        for idx_pos, orig_idx in enumerate(filtered_indices):
            if idx_pos < len(edited_search):
                edited_row = edited_search.iloc[idx_pos]
                st.session_state.cssw_search_cols[orig_idx]["select"] = bool(edited_row["select"])
                st.session_state.cssw_search_cols[orig_idx]["search_type"] = edited_row["search_type"]
                st.session_state.cssw_search_cols[orig_idx]["embedding_model"] = edited_row["embedding_model"]
    else:
        st.info("No columns match the filter.")

    any_search_selected = any(r["select"] for r in st.session_state.cssw_search_cols)
    if not any_search_selected:
        st.error("❌ Select at least one search column.")

    return any_search_selected


def _section_cost_explanation():
    """Expandable section explaining search types and embedding costs."""
    with st.expander("ℹ️ Search Types & Embedding Model Costs", expanded=False):
        st.markdown(
            "**Search Types:**\n"
            "- **Hybrid (Text + Vector):** Combines keyword (lexical) search with "
            "semantic (vector) search for the best relevance. Recommended for most use cases.\n"
            "- **Text:** Keyword-based search only. Faster and cheaper, but misses semantic meaning.\n"
            "- **Vector:** Semantic search only. Understands meaning but may miss exact keyword matches.\n\n"
            "**Embedding Models (cost per 1 million tokens):**"
        )
        for model in _EMBEDDING_MODEL_OPTIONS:
            credits = EMBEDDING_PRICING.get(model, 0)
            st.markdown(f"- **{model}:** {credits:.2f} AI Credits")
        st.caption("1 AI Credit ≈ $3.71\n$1 = Rp 18,000")


def _section_attribute_columns(table_names):
    """Section 2: Attribute Column Selection with Table Name column."""
    st.markdown("#### 🏷️ Attribute Columns")
    st.caption("Columns available for filtering queries. RELATIVE_PATH and PAGE_NUMBER are auto-selected per table.")

    af1, af2 = st.columns(2)
    with af1:
        attr_filter = st.text_input(
            "Filter by Column Name", "", key="cssw_attr_filter",
            placeholder="e.g. RELATIVE_PATH, PAGE_NUMBER..."
        )
    with af2:
        attr_table_filter = st.selectbox(
            "Filter by Table", ["All Tables"] + table_names,
            key="cssw_attr_table_filter"
        )

    filtered_attr_indices = []
    for i, row in enumerate(st.session_state.cssw_attribute_cols):
        col_match = attr_filter.lower() in row["column"].lower()
        tbl_match = attr_table_filter == "All Tables" or row.get("table", "") == attr_table_filter
        if col_match and tbl_match:
            filtered_attr_indices.append(i)

    if filtered_attr_indices:
        attr_data = [st.session_state.cssw_attribute_cols[i] for i in filtered_attr_indices]
        attr_df = pd.DataFrame(attr_data)

        edited_attr = st.data_editor(
            attr_df,
            column_config={
                "select": st.column_config.CheckboxColumn("Select", width="small"),
                "table": st.column_config.TextColumn("Table Name", disabled=True, width="medium"),
                "column": st.column_config.TextColumn("Column Name", disabled=True, width="large"),
            },
            disabled=["table", "column"],
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


def _section_target_lag():
    """Section 3: Target Lag configuration."""
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

    return lag_num, lag_unit


def _section_preview_and_execute(session, svc_name, db, schema, table_names,
                                  warehouse, lag_num, lag_unit,
                                  any_search_selected, user_roles):
    """Section 4-5: SQL preview per table and execute button."""
    st.markdown("#### 📝 Service Configuration Preview")

    # Build SQL for each table
    per_table_sql = {}
    for tbl in table_names:
        sql = _build_create_sql(
            svc_name, db, schema, tbl,
            st.session_state.cssw_search_cols,
            st.session_state.cssw_attribute_cols,
            warehouse, lag_num, lag_unit,
        )
        per_table_sql[tbl] = sql

    for tbl, sql in per_table_sql.items():
        with st.expander(f"CREATE SERVICE for `{tbl}`", expanded=True):
            st.code(sql, language="sql")

    if user_roles:
        roles_display = ", ".join(f"`{r}`" for r in user_roles)
        st.markdown(f"**Privileges will be granted to:** {roles_display}")

    st.divider()

    # --- Execute ---
    st.markdown("#### 🚀 Create Search Services")

    svc_created = st.session_state.get("cssw_svc_created", False)

    if not svc_created:
        if not any_search_selected:
            st.button("🚀 Create Cortex Search Services", disabled=True,
                       key="cssw_create_disabled")
        elif st.button("🚀 Create Cortex Search Services", type="primary",
                       key="cssw_create_btn"):
            created = []
            errors = []

            # Step 1: Create one service per table
            for tbl, sql in per_table_sql.items():
                with st.spinner(f"Creating service `{svc_name}` for `{tbl}`..."):
                    try:
                        session.sql(sql).collect()
                        created.append(tbl)
                        log_action("CSSW_SVC_CREATED", {"svc": svc_name, "table": tbl})
                    except Exception as e:
                        errors.append((tbl, str(e)))
                        log_action("CSSW_SVC_CREATE_ERROR", {"svc": svc_name, "table": tbl, "error": str(e)}, level="ERROR")

            if created:
                st.success(f"✅ Service created for: {', '.join('`' + t + '`' for t in created)}")
            for tbl, err in errors:
                st.error(f"❌ Failed for `{tbl}`: {err}")

            if not errors:
                st.session_state.cssw_svc_created = True

            # Step 2: Grant USAGE on search service
            if user_roles and created:
                with st.spinner(f"Granting service privileges to {', '.join(user_roles)}..."):
                    grant_results = _grant_search_service_privileges(
                        session, db, schema, svc_name, user_roles
                    )
                    if grant_results["success"]:
                        st.success(f"✅ USAGE granted to: {', '.join(set(r.split('@')[0] for r in grant_results['success']))}")
                    if grant_results["failed"]:
                        st.warning(f"⚠️ Service grants failed for: {', '.join(grant_results['failed'])}")

            # Step 3: Grant SELECT on all source tables
            if user_roles and created:
                with st.spinner(f"Granting table access to {', '.join(user_roles)}..."):
                    tbl_results = _grant_table_select(session, db, schema, created, user_roles)
                    if tbl_results["success"]:
                        st.success(f"✅ SELECT granted on tables")
                    if tbl_results["failed"]:
                        st.warning(f"⚠️ Table grants failed for: {', '.join(tbl_results['failed'])}")

            st.rerun()

    if svc_created:
        st.success(f"🎉 Cortex Search Service(s) ready!")
        st.markdown(f"- **Database:** `{db}`")
        st.markdown(f"- **Schema:** `{schema}`")
        st.markdown(f"- **Warehouse:** `{warehouse}`")
        st.markdown(f"- **Target Lag:** `{lag_num} {lag_unit}`")

        search_selected = [r for r in st.session_state.cssw_search_cols if r.get("select")]
        if search_selected:
            st.markdown("**Search Columns by Table:**")
            by_table = {}
            for r in search_selected:
                by_table.setdefault(r.get("table", ""), []).append(r)
            for tbl, rows in by_table.items():
                st.markdown(f"*`{tbl}`*:")
                for r in rows:
                    st.markdown(f"  - `{r['column']}` — {r['search_type']} ({r['embedding_model']})")

        attr_selected = [r for r in st.session_state.cssw_attribute_cols if r.get("select")]
        if attr_selected:
            st.markdown("**Attributes by Table:**")
            by_table = {}
            for r in attr_selected:
                by_table.setdefault(r.get("table", ""), []).append(r)
            for tbl, rows in by_table.items():
                names = ", ".join("`" + r["column"] + "`" for r in rows)
                st.markdown(f"  *`{tbl}`*: {names}")

        st.divider()
        if st.button("🔄 Create Another Service"):
            for key in list(st.session_state.keys()):
                if key.startswith("cssw_") or key.startswith("_jbv_") or key.startswith("_wiz_"):
                    del st.session_state[key]
            st.rerun()


# ---------------------------------------------------------------------------
# Main render — top-level try/except so errors are NEVER silent
# ---------------------------------------------------------------------------

def render(session):
    try:
        _render_inner(session)
    except Exception as e:
        tb = traceback.format_exc()
        log_action("PAGE4_RENDER_ERROR", {"error": str(e), "traceback": tb}, level="ERROR")
        st.error(f"❌ Page 4 encountered an error: `{e}`")
        with st.expander("🔧 Technical Details", expanded=False):
            st.code(tb)
        nav_buttons(can_next=False, show_back=True)


def _render_inner(session):
    render_header(4)

    c = ctx()
    db = c.get("db", DEFAULT_DB)
    schema = c.get("schema", DEFAULT_SCHEMA)
    svc_name = st.session_state.get("_wiz_svc_name", "CSS_")
    role = st.session_state.get("_wiz_role", "")
    jobs = st.session_state.get("cssw_jobs", [])

    log_action("PAGE4_RENDER_START", {
        "db": db, "schema": schema, "svc": svc_name, "jobs": len(jobs),
    })

    # --- Fetch table columns (from Step 3 cache or fresh query) ---
    all_table_columns = st.session_state.get("cssw_table_columns", {})
    if not all_table_columns and jobs:
        all_table_columns = _fetch_all_table_columns(session, db, schema, jobs)
        if all_table_columns:
            st.session_state.cssw_table_columns = all_table_columns

    if not all_table_columns:
        st.warning("⚠️ No table columns available. Go back to Step 3 and run the batch first.")
        nav_buttons(can_next=False, show_back=True)
        return

    table_names = [t for t, cols in all_table_columns.items() if cols]
    if not table_names:
        st.warning("⚠️ No valid tables found. Go back to Step 3 and run the batch first.")
        nav_buttons(can_next=False, show_back=True)
        return

    _init_search_config(all_table_columns)

    # --- Current warehouse (auto-detect, hidden from user) ---
    warehouse = _get_current_warehouse(session)
    if not warehouse:
        warehouse = st.session_state.get("_wiz_warehouse", "")
    if not warehouse:
        st.error("❌ Could not detect current warehouse. Please ensure a warehouse is active.")
        nav_buttons(can_next=False)
        return
    st.session_state["_wiz_warehouse"] = warehouse

    # --- Collect roles for grants ---
    user_roles = [role] if role else []
    for j in jobs:
        for gr in j.get("grant_roles", []):
            if gr and gr not in user_roles:
                user_roles.append(gr)

    # --- Sections ---
    any_search_selected = _section_search_columns(table_names)
    _section_cost_explanation()
    st.divider()
    _section_attribute_columns(table_names)
    st.divider()
    lag_num, lag_unit = _section_target_lag()
    st.divider()
    _section_preview_and_execute(
        session, svc_name, db, schema, table_names,
        warehouse, lag_num, lag_unit, any_search_selected, user_roles,
    )
    nav_buttons(can_next=False, show_back=True)
