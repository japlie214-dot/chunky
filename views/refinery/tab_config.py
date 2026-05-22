# views/refinery/tab_config.py
# Config Tab - Job Management for the Doc Refinery package
import streamlit as st
import pandas as pd
import os
from utils.core_utils import PDFUtils
from utils.snowflake_utils import get_table_schema
from utils.auth_utils import get_user_mapped_roles

def render_config_tab(session):
    st.subheader("1. Job Management")
    
    # Context Retrieval
    ctx = st.session_state.auth_context
    db, schema, stage = ctx["db"], ctx["schema"], ctx["stage"]
    stage_path = f"@{db}.{schema}.{stage}"

    # Infrastructure Display (Read-Only)
    with st.expander(f"🔒 Active Context: {db}.{schema}", expanded=True):
        st.info(f"**Stage:** `{stage}` | **Path:** `{stage_path}`")
        
        # Wrap file listing in try/except to catch XP process errors
        pdf_files = []
        try:
            files = session.sql(f"LIST {stage_path} PATTERN='.*\\.pdf'").collect()
            pdf_files = [os.path.basename(f['name']) for f in files]
        except Exception as e:
            # Handle Snowflake XP/Session termination errors gracefully
            if "XP" in str(e) or "terminated" in str(e):
                st.error("⚠️ Connection unstable. Please refresh the page to reconnect.")
            else:
                st.warning(f"Could not list files: {e}")

    # Job Builder
    st.markdown("#### 📋 Job Builder")
    with st.container():
        jc1, jc2, jc3 = st.columns(3)
        
        with jc1:
            st.markdown("**📄 File & Scope**")
            sel_file = st.selectbox("Select PDF", pdf_files if pdf_files else ["No files"], key="jb_file")
            
            # PLAN-17: PDF Download Link moved above Scope selector with help text
            pdf_link = st.text_input(
                "PDF Download Link (Optional)",
                value="",
                key="jb_link",
                help="This will be used as reference as to where we could get the digital copy of the PDF."
            )
            
            scope = st.radio("Scope", ["Full Doc", "Page Range"], horizontal=True, key="jb_scope")
            
            # Metadata Caching
            page_count_est = 1
            if sel_file != "No files":
                if 'file_metadata_cache' not in st.session_state: st.session_state.file_metadata_cache = {}
                if sel_file in st.session_state.file_metadata_cache:
                    page_count_est = st.session_state.file_metadata_cache[sel_file]['page_count']
                else:
                    try:
                        stream = session.file.get_stream(f"{stage_path}/{sel_file}")
                        pdf_bytes = stream.read()
                        page_count_est = PDFUtils.get_page_count(pdf_bytes)
                        st.session_state.file_metadata_cache[sel_file] = {'page_count': page_count_est}
                    except: pass
                st.caption(f"Detected {page_count_est} pages")

            p_start, p_end = 1, page_count_est
            if scope == "Page Range":
                c_rng1, c_rng2 = st.columns(2)
                p_start = c_rng1.number_input("Start", 1, max(1, page_count_est), value=1, key="jb_pstart")
                p_end = c_rng2.number_input("End", 1, max(1, page_count_est), value=min(10, page_count_est), key="jb_pend")

        with jc2:
            st.markdown("**🎯 Target & Strategy**")
            # Locked to current schema context, but user can define Table Name
            target_table_name = st.text_input("Target Table Name", "SUS_CHUNKS", key="jb_table_name")
            target_table = target_table_name # Will be prefixed with ctx later
            
            # Active Table Check
            target_table_base = target_table_name.split('.')[-1]
            tbl_exists, _, tbl_err = get_table_schema(session, db, schema, target_table_base)
            
            mode_help = (
                "**APPEND**: Adds new chunks to the end of the table.\n"
                "**OVERWRITE**: Drops and recreates the table.\n"
                "**SURGICAL**: Removes specific file/page entries before inserting new ones (Requires existing table)."
            )
            mode = st.radio("Write Mode", ["APPEND", "OVERWRITE", "SURGICAL"], index=0, key="jb_mode", help=mode_help)
            
            # Display dynamic status messages & Block SURGICAL mode
            blocking_error = False
            grant_roles = []
            surgical_target_file = None
            surgical_target_page = 0
            
            if mode == "SURGICAL":
                if not tbl_exists:
                    st.error("❌ Table must exist for SURGICAL mode.")
                    blocking_error = True
                else:
                    st.success("✅ Target table confirmed.")
                    existing_files = [sel_file]
                    try:
                        safe_target_table_base = target_table_base.replace('"', '""')
                        res = session.sql(f'SELECT DISTINCT RELATIVE_PATH FROM "{db}"."{schema}"."{safe_target_table_base}"').collect()
                        existing_files = sorted(list(set([r['RELATIVE_PATH'] for r in res] + [sel_file])))
                    except Exception:
                        pass
                    surgical_target_file = st.selectbox("Target File to Replace", existing_files, index=existing_files.index(sel_file) if sel_file in existing_files else 0, key="jb_surg_file")
                    surgical_target_page = st.number_input("Target Page Number (0 = All matching range)", min_value=0, step=1, value=0, key="jb_surg_pg")
                    
                    if surgical_target_page > 0 and (scope != "Page Range" or p_start != p_end):
                        st.error("❌ Exact-page replacement requires a 1-to-1 mapping. Set Scope to 'Page Range' and ensure Start Page equals End Page.")
                        blocking_error = True
            elif mode in ["APPEND", "OVERWRITE"]:
                if tbl_exists:
                    st.info(f"ℹ️ Table exists. Data will be {mode.lower()}ed.")
                else:
                    st.warning("🆕 Table does not exist. It will be created.")
                    import re
                    avail_roles = get_user_mapped_roles(ctx.get("user", ""))
                    
                    # ARCHITECTURAL CONSTRAINT: IT_AI is excluded from standard grants intentionally.
                    # The Streamlit app runs with owner's rights (IT_AI), meaning any new tables created are
                    # automatically owned by IT_AI. Granting access to the owner is redundant and causes execution errors.
                    auto_roles = [r for r in avail_roles if r.upper() != "IT_AI"]
                    default_str = auto_roles[0] if auto_roles else ""
                    
                    grant_input = st.text_input(
                        "Grants for New Table",
                        value=default_str,
                        placeholder="e.g., IT_DS, IT_BI, \"CUSTOM-ROLE\"",
                        help="Comma or space-separated Snowflake role names. IT_AI is automatically the owner. Invalid roles are skipped.",
                        key="jb_grant_roles"
                    )
                    
                    # Robust parsing: extracts unquoted words OR double-quoted strings, preserving internal spaces
                    raw_splits = re.findall(r'[^,\s"]+|"[^"]*"', grant_input)
                    grant_roles = list(dict.fromkeys(
                        r.strip().upper() for r in raw_splits
                        if r.strip() and r.strip().upper() != "IT_AI"
                    ))
            
            use_layout = st.checkbox("Use Layout Parser (Structural)", True, key="jb_layout")
            use_vision = st.checkbox("Use Vision Parser (Charts/Images)", True, key="jb_vision")
            if not use_layout and not use_vision:
                st.error("Select at least one strategy.")
                blocking_error = True

        with jc3:
            st.markdown("**⚙️ Parameters**")
            chk_help = "Maximum characters per chunk. Chunks are strictly bounded by page; they do not cross page boundaries."
            chk_sz = st.number_input("Chunk Size", 1000, 30000, 8000, step=500, key="jb_chunk", help=chk_help)
            
            ov_help = "Characters repeated between adjacent chunks *on the same page only*."
            overlap_pct = st.slider("Overlap %", 0, 50, 20, key="jb_overlap", help=ov_help)
            overlap = int(chk_sz * (overlap_pct / 100))
            
            # Validate Page Range inputs
            if scope == "Page Range" and p_start > p_end:
                st.error("❌ Start Page cannot be greater than End Page.")
                blocking_error = True
            
            if st.button("➕ Add Job", key="jb_add", type="primary", disabled=bool(blocking_error or not pdf_files)):
                # Correct inclusive page range calculation
                est_pages = (p_end - p_start) + 1 if scope == "Page Range" else page_count_est
                if 'job_queue' not in st.session_state: st.session_state.job_queue = []
                
                new_id = max([j['id'] for j in st.session_state.job_queue] + [0]) + 1
                st.session_state.job_queue.append({
                    "id": new_id,
                    "file": sel_file,
                    "table": target_table,
                    "mode": mode,
                    "scope": scope,
                    "range": (p_start, p_end),
                    "estimated_pages": est_pages,
                    "layout": use_layout,
                    "vision": use_vision,
                    "params": (chk_sz, overlap),
                    "surgical_file": sel_file if mode == "SURGICAL" else None,
                    "surgical_target_file": surgical_target_file,
                    "surgical_target_page": surgical_target_page,
                    "grant_roles": grant_roles,
                    "link": pdf_link,
                    "status": "Pending"
                })
                st.success("Job Added")
                st.rerun()

    # Job Queue Display
    if 'job_queue' in st.session_state and st.session_state.job_queue:
        st.divider()
        st.markdown("#### 📊 Job Queue Workbench")
        
        # Helper to format scope for display/editing
        def fmt_scope(j):
            if j['scope'] == 'Full Doc': return "Full"
            s, e = j['range']
            return f"{s}-{e}"

        q_data = []
        for j in st.session_state.job_queue:
            q_data.append({
                "selected": j.get("selected", False),
                "id": j["id"],
                "file": j["file"],
                "table": j["table"],
                "Mode": j["mode"],
                "Scope Constraint": fmt_scope(j),
                "Target File": j.get("surgical_target_file", j["file"]),
                "Target Page": j.get("surgical_target_page", 0),
                "PDF Link": j.get("link", ""),
                "Assigned Roles": ", ".join(j.get("grant_roles", [])),
                "L": j.get("layout", True),
                "V": j.get("vision", True),
                "pages": j.get("estimated_pages", 1),
                "status": j["status"]
            })
            
        edited_df = st.data_editor(
            pd.DataFrame(q_data),
            column_config={
                "selected": st.column_config.CheckboxColumn("Select", width="small"),
                "id": st.column_config.NumberColumn("ID", disabled=True, width="small"),
                "file": st.column_config.TextColumn("File", disabled=True, width="medium"),
                "Mode": st.column_config.SelectboxColumn("Mode", options=["APPEND", "OVERWRITE", "SURGICAL"], width="small"),
                "Scope Constraint": st.column_config.TextColumn("Scope (e.g., '1-10' or 'Full')", width="medium"),
                "Target File": st.column_config.TextColumn("Target File", width="medium"),
                "Target Page": st.column_config.NumberColumn("Target Page", min_value=0, step=1, width="small"),
                "PDF Link": st.column_config.TextColumn("PDF Link", width="medium"),
                "Assigned Roles": st.column_config.TextColumn("Assigned Roles (comma-separated)", width="medium"),
                "status": st.column_config.TextColumn("Status", disabled=True)
            },
            use_container_width=True,
            hide_index=True,
            key="config_job_editor_v3"
        )
        
        # Sync Logic with Validation
        if not edited_df.equals(pd.DataFrame(q_data)):
            for index, row in edited_df.iterrows():
                target_job = next((j for j in st.session_state.job_queue if j["id"] == row["id"]), None)
                if not target_job: continue
                
                # 1. Update Mode and Boolean Flags
                target_job["mode"] = row["Mode"]
                target_job["selected"] = row["selected"]
                target_job["layout"] = row["L"]
                target_job["vision"] = row["V"]
                target_job["surgical_target_file"] = str(row.get("Target File")) if pd.notna(row.get("Target File")) else target_job["file"]
                target_job["surgical_target_page"] = int(row.get("Target Page")) if pd.notna(row.get("Target Page")) else 0
                
                # PLAN-16: pd.notna() guards prevent the string "nan" from entering the
                # job dict when a user leaves a cell blank in the data editor.
                target_job["link"] = (
                    str(row["PDF Link"]) if pd.notna(row.get("PDF Link")) else ""
                )
                raw_roles = (
                    str(row["Assigned Roles"]) if pd.notna(row.get("Assigned Roles")) else ""
                )
                import re
                # ARCHITECTURAL CONSTRAINT: IT_AI is excluded to prevent redundant owner grants.
                # Findall pattern extracts unquoted words OR double-quoted strings, preserving internal spaces.
                target_job["grant_roles"] = list(dict.fromkeys(
                    r.strip().upper()
                    for r in re.findall(r'[^,\s"]+|"[^"]*"', raw_roles)
                    if r.strip() and r.strip().upper() != "IT_AI"
                ))
                
                # 2. Validate & Update Scope
                new_scope_str = str(row["Scope Constraint"]).strip().lower()
                
                # Get max pages from cache
                max_pg = 1
                if target_job['file'] in st.session_state.file_metadata_cache:
                    max_pg = st.session_state.file_metadata_cache[target_job['file']]['page_count']
                
                valid_update = False
                
                if new_scope_str in ["full", "full doc", "all"]:
                    target_job["scope"] = "Full Doc"
                    target_job["range"] = (1, max_pg)
                    target_job["estimated_pages"] = max_pg
                    valid_update = True
                elif "-" in new_scope_str:
                    try:
                        parts = new_scope_str.split("-")
                        if len(parts) == 2:
                            s, e = int(parts[0]), int(parts[1])
                            if 1 <= s <= e <= max_pg:
                                target_job["scope"] = "Page Range"
                                target_job["range"] = (s, e)
                                target_job["estimated_pages"] = e - s
                                valid_update = True
                            else:
                                st.toast(f"⚠️ Range {s}-{e} invalid for {target_job['file']} (Max {max_pg})", icon="❌")
                    except:
                        pass
                
                if not valid_update and new_scope_str != fmt_scope(target_job).lower():
                    st.toast(f"⚠️ Invalid format '{row['Scope Constraint']}'. Use 'Full' or 'Start-End'.", icon="❌")
            
            st.rerun()
        
        bc1, bc2 = st.columns(2)
        with bc1:
            if st.button("🗑️ Delete Selected Jobs"):
                st.session_state.job_queue = [j for j in st.session_state.job_queue if not j.get("selected")]
                st.rerun()
        with bc2:
             if st.button("💥 Clear Queue"):
                st.session_state.job_queue = []
                st.session_state.batch_audit = {}
                st.rerun()
