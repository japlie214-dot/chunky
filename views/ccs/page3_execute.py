# views/ccs/page3_execute.py
# Page 3: Job Queue & Execution — review, run batch, results.
# Enhanced with: styled DataFrame, grant status, defect details,
# page coverage map, observability, query tagging, CSV export.
# Execution COPIED from views/refinery/tab_ingestion.py.

import time
import datetime
import streamlit as st
import pandas as pd
from views.ccs.common import render_header, nav_buttons, ctx
from utils.constants import (
    DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE,
    PAGE_WARNING_THRESHOLD, LAYOUT_COST_PER_1K_PAGES,
    CREDIT_TO_USD, CREDIT_TO_IDR, CHUNK_CACHE_MAX_SIZE,
)


def _fetch_all_table_columns(session, db, schema, jobs):
    """Fetch column names and types from ALL unique target tables (cached silently)."""
    seen = {}
    for j in jobs:
        tbl = j["table"].split(".")[-1]
        if tbl in seen:
            continue
        full_table = f'"{db}"."{schema}"."{tbl}"'
        try:
            res = session.sql(f"DESCRIBE TABLE {full_table}").collect()
            seen[tbl] = [{"name": row["name"], "type": row["type"], "table": tbl} for row in res]
        except Exception:
            seen[tbl] = []
    return seen


def _style_status(row):
    """Apply whole-row background color based on job status."""
    colors = {
        'Completed': 'background-color: #d4edda',
        'Failed': 'background-color: #f8d7da',
        'Completed with Warnings': 'background-color: #fff3cd',
        'Cancelled': 'background-color: #e2e3e5',
        'Running': 'background-color: #cce5ff',
    }
    bg = colors.get(row.get('Status', ''), '')
    return [bg] * len(row)


def _style_mode(val):
    """Apply background color to Mode column values."""
    mode_colors = {
        'APPEND': 'background-color: #d4edda; color: #155724',
        'OVERWRITE': 'background-color: #f8d7da; color: #721c24',
        'SURGICAL': 'background-color: #cce5ff; color: #004085',
    }
    return mode_colors.get(val, '')


def render(session):
    render_header(3)

    c = ctx()
    db = c.get("db", DEFAULT_DB)
    schema = c.get("schema", DEFAULT_SCHEMA)
    stage = c.get("stage", DEFAULT_STAGE)
    stage_path = f"@{db}.{schema}.{stage}"

    svc_name = st.session_state.get("_wiz_svc_name", "")
    role = st.session_state.get("_wiz_role", "")
    jobs = st.session_state.get("cssw_jobs", [])

    # --- Session Memory Warning Banner ---
    chunk_cache = st.session_state.get("chunk_cache", [])
    cache_pct = (len(chunk_cache) / CHUNK_CACHE_MAX_SIZE * 100) if CHUNK_CACHE_MAX_SIZE > 0 else 0
    if cache_pct >= 80:
        if cache_pct < 90:
            bg, fg = "#FFC107", "#333333"
        elif cache_pct < 100:
            bg, fg = "#FF5722", "#FFFFFF"
        else:
            bg, fg = "#D32F2F", "#FFFFFF"
        st.markdown(f"""
        <div style='background:{bg};color:{fg};padding:10px 14px;border-radius:6px;font-weight:bold;'>
            ⚠️ Session memory at {int(cache_pct)}% capacity ({len(chunk_cache)}/{CHUNK_CACHE_MAX_SIZE}). Export or clear chunks soon.
        </div>
        """, unsafe_allow_html=True)
        if st.button("🧹 Clear In-Memory Chunks", key="page3_clear_cache"):
            st.session_state.chunk_cache = []
            st.rerun()

    if not jobs:
        st.warning("No jobs queued. Go back to Step 2 and add jobs.")
        nav_buttons(can_next=False)
        return

    terminal = {"Completed", "Completed with Warnings", "Failed", "Cancelled"}
    has_pending = any(j.get("status", "Pending") not in terminal for j in jobs)

    # --- Summary ---
    st.markdown("#### 📋 Configuration Summary")
    sc1, sc2 = st.columns(2)
    with sc1:
        st.markdown(f"- **Service Name:** `{svc_name}`")
        st.markdown(f"- **Owner Role:** `{role}`")
        st.markdown(f"- **Location:** `{db}.{schema}`")
    with sc2:
        total_pages = sum(j["estimated_pages"] for j in jobs)
        st.markdown(f"- **Total Jobs:** {len(jobs)}")
        st.markdown(f"- **Total Pages:** {total_pages}")
        files_listing = ', '.join('`' + j['file'] + '`' for j in jobs)
        st.markdown(f"- **Files:** {files_listing}")

    st.divider()

    # --- Styled Job Workbench ---
    st.markdown(f"#### 📊 Job Workbench ({len(jobs)} jobs)")

    wb_data = [{
        "Select": j.get("selected", False),
        "ID": j["id"],
        "File": j["file"],
        "Table": j["table"].split(".")[-1],
        "Mode": j["mode"],
        "Status": j.get("status", "Pending"),
        "Pages": j.get("estimated_pages", 0),
    } for j in jobs]
    wb_df = pd.DataFrame(wb_data)

    styled_df = wb_df.style.apply(_style_status, axis=1).applymap(_style_mode, subset=['Mode'])

    edited_wb = st.data_editor(
        styled_df,
        column_config={
            "Select": st.column_config.CheckboxColumn("Select", width="small"),
            "ID": st.column_config.NumberColumn("ID", disabled=True, width="small"),
            "File": st.column_config.TextColumn("File", disabled=True, width="medium"),
            "Table": st.column_config.TextColumn("Table", disabled=True, width="medium"),
            "Mode": st.column_config.SelectboxColumn("Mode", options=["APPEND", "OVERWRITE", "SURGICAL"], width="small"),
            "Status": st.column_config.TextColumn("Status", disabled=True, width="small"),
            "Pages": st.column_config.NumberColumn("Pages", disabled=True, width="small"),
        },
        use_container_width=True,
        hide_index=True,
        key="cssw_page3_workbench",
    )

    # Sync selected state back
    for _, row in edited_wb.iterrows():
        tgt = next((j for j in jobs if j["id"] == row["ID"]), None)
        if tgt:
            tgt["selected"] = bool(row["Select"])
            tgt["mode"] = row["Mode"]

    # Delete controls
    wb1, wb2 = st.columns(2)
    with wb1:
        if st.button("🗑️ Delete Selected Jobs"):
            before = len(st.session_state.cssw_jobs)
            st.session_state.cssw_jobs = [j for j in jobs if not j.get("selected")]
            deleted = before - len(st.session_state.cssw_jobs)
            if deleted:
                st.toast(f"Deleted {deleted} job(s)")
            st.rerun()

    with wb2:
        deletable_statuses = ["Failed", "Cancelled", "Completed with Warnings"]
        existing_statuses = set(j.get("status", "Pending") for j in jobs)
        for ds in deletable_statuses:
            if ds in existing_statuses:
                count = sum(1 for j in jobs if j.get("status") == ds)
                if st.button(f"🗑️ Delete All {ds} ({count})"):
                    st.session_state.cssw_jobs = [j for j in jobs if j.get("status") != ds]
                    st.toast(f"Deleted {count} {ds} job(s)")
                    st.rerun()

    st.divider()
    st.markdown("#### 🚀 Execute")

    if "batch_in_progress" not in st.session_state:
        st.session_state.batch_in_progress = False
    if "cancel_batch" not in st.session_state:
        st.session_state.cancel_batch = False

    # --- Show execute button whenever there are pending jobs ---
    if has_pending and not st.session_state.batch_in_progress:
        pending_count = sum(1 for j in jobs if j.get("status", "Pending") not in terminal)
        pending_pages = sum(j.get("estimated_pages", 0) for j in jobs
                           if j.get("status", "Pending") not in terminal)
        if pending_pages > PAGE_WARNING_THRESHOLD:
            st.warning(f"⚠️ You have {pending_pages} pages queued. Large batches can overwhelm manual QA.")
        st.info(f"📋 {pending_count} job(s) pending execution.")
        if st.button("🚀 Run Batch Execution", type="primary"):
            if "job_queue" not in st.session_state:
                st.session_state.job_queue = []
            existing_ids = {j["id"] for j in st.session_state.job_queue}
            for j in jobs:
                if j["id"] not in existing_ids:
                    st.session_state.job_queue.append(j)
            st.session_state.cssw_batch_started = True
            st.session_state.batch_in_progress = True
            st.session_state.cancel_batch = False
            st.session_state.batch_metrics = {
                "jobs_completed": 0, "jobs_failed": 0, "jobs_warning": 0, "jobs_cancelled": 0,
                "total_pages": 0, "total_chunks": 0,
                "layout_pages_processed": 0, "vision_pages_processed": 0,
                "layout_pages_list": set(), "vision_pages_list": set(),
                "standard_chunks": 0, "enhanced_chunks": 0,
                "total_time": 0.0, "time_layout": 0.0, "time_vision": 0.0,
                "credits_layout": 0.0, "credits_vision": 0.0, "enhancement_breakdown": {},
            }
            st.session_state.batch_start_time = time.time()

            # Set query tag for warehouse attribution
            try:
                from utils.snowflake_utils import set_query_tag
                auth_ctx = st.session_state.get("auth_context", {})
                if auth_ctx and "query_tag_set" not in st.session_state:
                    set_query_tag(session, auth_ctx)
                    st.session_state.query_tag_set = True
            except Exception:
                pass  # Non-blocking

            st.rerun()

    # --- Batch in progress ---
    if st.session_state.batch_in_progress:
        has_batch_pending = any(j["status"] not in terminal
                               for j in st.session_state.get("job_queue", []))
        if has_batch_pending:
            st.warning("⚠️ Batch in progress. Click Stop to halt after the current job.")
            if st.button("🛑 Stop Batch"):
                st.session_state.cancel_batch = True
                st.rerun()
        from views.ccs.batch_processor import run_batch_execution
        try:
            run_batch_execution(session, db, schema, stage_path)
        except Exception as e:
            st.error(f"Batch runner failed: {e}")
            st.session_state.batch_in_progress = False

    # --- Results (show whenever at least one job has been processed) ---
    any_processed = any(j.get("status", "Pending") in terminal for j in jobs)
    if any_processed:
        # Sync job statuses from job_queue
        for wj in jobs:
            for gj in st.session_state.get("job_queue", []):
                if gj["id"] == wj["id"]:
                    wj["status"] = gj.get("status", wj["status"])
                    wj["metrics"] = gj.get("metrics", {})
                    wj["grant_status"] = gj.get("grant_status", {})
                    wj["skipped_page_ranges"] = gj.get("skipped_page_ranges", [])

        completed = sum(1 for j in jobs if j["status"] == "Completed")
        failed = sum(1 for j in jobs if j["status"] == "Failed")
        warns = sum(1 for j in jobs if j["status"] == "Completed with Warnings")
        pending = sum(1 for j in jobs if j.get("status", "Pending") not in terminal)

        if failed > 0:
            st.error(f"⚠️ {failed} job(s) failed.")
        elif warns > 0:
            st.warning(f"⚠️ {completed} completed, {warns} with warnings.")
        elif pending > 0:
            st.info(f"📋 {completed} completed, {pending} pending.")
        else:
            st.success(f"🎉 All {completed} job(s) completed!")

        st.divider()

        # --- Aggregate Report Dashboard ---
        st.markdown("#### 📊 Batch Report Dashboard")
        rpt_tab1, rpt_tab2 = st.tabs(["📊 Overview", "📋 Details"])

        with rpt_tab1:
            _render_overview_dashboard(jobs, terminal)

        with rpt_tab2:
            _render_details_tab(session, db, schema, jobs, terminal, stage_path)

        # --- Next button ---
        can_next = not has_pending
        if not can_next:
            st.warning("⚠️ Complete all pending jobs before proceeding.")
        nav_buttons(can_next=can_next, next_label="Next ➡️")

    else:
        st.info("Click **Run Batch Execution** to start.")
        nav_buttons(can_next=False)


# =============================================================================
# Report Dashboard Renderers
# =============================================================================

def _render_overview_dashboard(jobs, terminal):
    """Render the aggregate overview dashboard tab."""
    from utils.core_utils import RAGAnalytics

    finished_jobs = [j for j in jobs if j.get("status", "Pending") in terminal]
    if not finished_jobs:
        st.info("No completed jobs to display.")
        return

    total_finished = len(finished_jobs)
    completed = sum(1 for j in finished_jobs if j["status"] == "Completed")
    failed = sum(1 for j in finished_jobs if j["status"] == "Failed")
    warns = sum(1 for j in finished_jobs if j["status"] == "Completed with Warnings")

    # High Level
    m1, m2, m3 = st.columns(3)
    m1.metric("✅ Success Rate", f"{(completed / total_finished * 100) if total_finished > 0 else 0:.0f}%", f"{completed} Jobs")
    m2.markdown(f"<div style='color: orange; font-size: 18px; font-weight: bold;'>⚠️ Warnings: {warns}</div>", unsafe_allow_html=True)
    total_pages = sum(j.get("metrics", {}).get("pages", j.get("estimated_pages", 0)) for j in finished_jobs)
    m3.metric("📄 Processed Pages", total_pages)

    st.divider()

    # Performance
    st.markdown("#### ⏱️ Performance")
    total_time = sum(j.get("metrics", {}).get("duration", 0) for j in finished_jobs)
    t_layout = sum(j.get("metrics", {}).get("time_layout", 0) for j in finished_jobs)
    t_vision = sum(j.get("metrics", {}).get("time_vision", 0) for j in finished_jobs)
    l_pages = sum(j.get("metrics", {}).get("layout_pages", 0) for j in finished_jobs)
    v_pages = sum(len(j.get("metrics", {}).get("vision_pages_list", set())) for j in finished_jobs)
    avg_pg_time = total_time / total_pages if total_pages > 0 else 0
    l_speed = t_layout / l_pages if l_pages > 0 else 0
    v_speed = t_vision / v_pages if v_pages > 0 else 0

    perf1, perf2, perf3, perf4 = st.columns(4)
    perf1.metric("⏱️ Total Time", f"{total_time:.1f}s")
    perf2.metric("⚡ Avg Speed", f"{avg_pg_time:.2f}s/pg" if total_pages > 0 else "0s")
    perf3.metric("🔧 Layout Speed", f"{l_speed:.2f}s/pg")
    perf4.metric("👁️ Vision Speed", f"{v_speed:.2f}s/pg")

    if total_pages > 0:
        l_cov = (l_pages / total_pages) * 100
        v_cov = (v_pages / total_pages) * 100
        st.caption(f"Page Coverage: Layout {l_cov:.1f}% ({l_pages}/{total_pages}) | Vision {v_cov:.1f}% ({v_pages}/{total_pages})")

    st.divider()

    # Cost Estimation
    st.markdown("#### 💰 Cost Estimation")
    c_lay = sum((j.get("metrics", {}).get("layout_pages", 0) / 1000) * LAYOUT_COST_PER_1K_PAGES for j in finished_jobs)
    c_vis = 0.0
    for j in finished_jobs:
        vt = j.get("metrics", {}).get("vision_tokens", {})
        if vt:
            for model_name, usage in vt.items():
                pricing = RAGAnalytics.PRICING_REGISTRY.get(model_name, {'input': 0.60, 'output': 3.00})
                c_vis += (usage['in'] / 1_000_000 * pricing['input']) + (usage['out'] / 1_000_000 * pricing['output'])
    c_total = c_lay + c_vis

    cc1, cc2, cc3 = st.columns(3)
    cc1.metric("💰 Layout Cost", f"{c_lay:.4f} Cr")
    cc2.metric("💰 Vision Cost", f"{c_vis:.4f} Cr")
    cc3.metric("💰 Total Cost", f"{c_total:.4f} Cr")
    usd = c_total * CREDIT_TO_USD
    idr = usd * CREDIT_TO_IDR
    st.caption(f"≈ ${usd:.2f} · Rp {idr:,.0f}")

    st.divider()

    # Data Yield
    st.markdown("#### 📄 Data Yield")
    total_chunks = sum(
        j.get("metrics", {}).get("standard_cnt", 0) + j.get("metrics", {}).get("enhanced_cnt", 0)
        for j in finished_jobs
    )
    standard = sum(j.get("metrics", {}).get("standard_cnt", 0) for j in finished_jobs)
    enhanced = sum(j.get("metrics", {}).get("enhanced_cnt", 0) for j in finished_jobs)

    d1, d2, d3 = st.columns(3)
    d1.metric("📦 Total Chunks", total_chunks)
    d2.metric("📐 Standard Chunks", standard)
    d3.metric("✨ Enhanced Chunks", enhanced)

    if total_chunks > 0 and enhanced > 0:
        st.progress(enhanced / total_chunks)
        st.caption(f"Enhancement Rate: {(enhanced / total_chunks) * 100:.1f}%")


def _render_details_tab(session, db, schema, jobs, terminal, stage_path):
    """Render the per-job details tab with grant status, defects, page coverage, CSV export."""
    from utils.core_utils import RAGAnalytics

    finished_jobs = [j for j in jobs if j.get("status", "Pending") in terminal]
    if not finished_jobs:
        st.info("No completed jobs to display.")
        return

    for j in finished_jobs:
        jm = j.get("metrics", {})
        tbl = j["table"].split(".")[-1]
        icon = {"Completed": "✅", "Failed": "❌", "Completed with Warnings": "⚠️"}.get(j["status"], "ℹ️")

        with st.expander(f"{icon} Job #{j['id']}: `{j['file']}` → `{tbl}` — {j['status']}", expanded=False):
            # --- Job Configuration ---
            s, e = j.get('range', (1, 1))
            scope_str = j['scope'] if j.get('scope') == 'Full Doc' else f"Pages {s}–{e}"
            strat = []
            if j.get('layout'): strat.append("Layout")
            if j.get('vision'): strat.append("Vision")

            cfg1, cfg2, cfg3, cfg4 = st.columns(4)
            cfg1.markdown(f"**Target Table:** `{tbl}`")
            cfg2.markdown(f"**Mode:** {j.get('mode', 'N/A')}")
            cfg3.markdown(f"**Scope:** {scope_str}")
            cfg4.markdown(f"**Strategy:** {' + '.join(strat) if strat else 'N/A'}")

            cfg5, cfg6, cfg7 = st.columns(3)
            cfg5.markdown(f"**Chunk Size:** {j.get('params', (0, 0))[0]:,}")
            cfg6.markdown(f"**Overlap:** {j.get('params', (0, 0))[1]}")
            if j.get('link'):
                cfg7.markdown(f"**PDF Link:** {j['link']}")
            if j.get('grant_roles'):
                st.markdown(f"**Grant Roles:** {', '.join(j['grant_roles'])}")

            st.divider()

            # --- Performance ---
            rc1, rc2, rc3, rc4 = st.columns(4)
            rc1.metric("📄 Pages", jm.get("pages", 0))
            rc2.metric("📦 Chunks", jm.get("standard_cnt", 0) + jm.get("enhanced_cnt", 0))
            rc3.metric("⏱️ Duration", f"{jm.get('duration', 0):.1f}s")
            rc4.metric("📊 Status", j["status"])

            # Error display
            error_msg = jm.get("error", "")
            if error_msg:
                st.error(f"**Failure Reason:** {error_msg}")

            # Skipped page ranges
            if j.get("skipped_page_ranges"):
                skipped_ranges = ', '.join([f"pp. {s['start']}-{s['end']}" for s in j['skipped_page_ranges']])
                st.warning(f"⚠️ **Partial Processing:** Skipped: {skipped_ranges}")

            # --- Grant Status Indicator ---
            gs = j.get("grant_status", {})
            if gs.get("attempted"):
                if gs.get("success"):
                    st.markdown("<div style='color: green; font-weight: bold;'>✅ Grants: Success</div>", unsafe_allow_html=True)
                else:
                    failed_roles = ", ".join(gs.get("failed_roles", []))
                    st.markdown(f"<div style='color: red; font-weight: bold;'>❌ Grants: Failed ({failed_roles})</div>", unsafe_allow_html=True)
            else:
                st.caption("ℹ️ Grants: N/A")

            st.divider()

            # --- Layout/Vision Speed ---
            lay_pages = jm.get("layout_pages", 0)
            vis_pages = len(jm.get("vision_pages_list", set()))
            total_pg = jm.get("pages", 0)
            t_layout = jm.get("time_layout", 0)
            t_vision = jm.get("time_vision", 0)

            st1, st2, st3, st4 = st.columns(4)
            st1.metric("🔧 Layout Pages", lay_pages)
            st2.metric("👁️ Vision Pages", vis_pages)
            st3.metric("⚡ Layout Speed", f"{t_layout / lay_pages:.2f}s/pg" if lay_pages > 0 else "N/A")
            st4.metric("⚡ Vision Speed", f"{t_vision / vis_pages:.2f}s/pg" if vis_pages > 0 else "N/A")

            if total_pg > 0:
                l_cov = (lay_pages / total_pg) * 100
                v_cov = (vis_pages / total_pg) * 100
                st.caption(f"Coverage: Layout {l_cov:.0f}% ({lay_pages}/{total_pg}) · Vision {v_cov:.0f}% ({vis_pages}/{total_pg})")

            # --- Page Coverage Map ---
            layout_pages_set = jm.get("layout_pages_list", set())
            vision_pages_set = jm.get("vision_pages_list", set())
            all_pages = layout_pages_set | vision_pages_set
            if all_pages:
                with st.expander("📄 Page Coverage (Layout / Vision)", expanded=False):
                    for pg in sorted(all_pages):
                        has_layout = "✅ Layout" if pg in layout_pages_set else "❌ Layout"
                        has_vision = "✅ Vision" if pg in vision_pages_set else "❌ Vision"
                        st.markdown(f"**Page {pg}:** {has_layout} | {has_vision}")
                    st.caption(
                        f"Layout: {len(layout_pages_set)} pages | "
                        f"Vision: {len(vision_pages_set)} pages | "
                        f"Total unique: {len(all_pages)} pages"
                    )

            # --- Defect Details ---
            defects_detail = jm.get("defects_detail", [])
            if defects_detail:
                with st.expander("🔍 Auto-Fixed Defects by Page", expanded=False):
                    from collections import defaultdict
                    by_page = defaultdict(list)
                    for d in defects_detail:
                        by_page[d['page']].append(d)
                    for page_num in sorted(by_page.keys()):
                        defects = by_page[page_num]
                        defect_types = ", ".join(f"`{d['defect_type']}` ({d['status']})" for d in defects)
                        st.markdown(f"**Page {page_num}:** {defect_types}")
                    st.caption(f"Total: {len(defects_detail)} defect records across {len(by_page)} pages.")

            st.divider()

            # --- Chunk Stats ---
            standard = jm.get("standard_cnt", 0)
            enhanced = jm.get("enhanced_cnt", 0)
            total_chunks = standard + enhanced

            ch1, ch2, ch3 = st.columns(3)
            ch1.metric("📐 Standard Chunks", standard)
            ch2.metric("✨ Enhanced Chunks", enhanced)
            if total_chunks > 0 and enhanced > 0:
                ch3.metric("🔧 Enhancement Rate", f"{(enhanced / total_chunks) * 100:.1f}%")

            # Enhancement types
            if jm.get("types"):
                with st.expander("✨ Enhancement Details"):
                    st.json(jm['types'])

            st.divider()

            # --- Cost Breakdown ---
            c_layout = (lay_pages / 1000) * LAYOUT_COST_PER_1K_PAGES if lay_pages > 0 else 0
            c_vision = 0
            vision_tokens = jm.get("vision_tokens", {})
            if vision_tokens:
                for model_name, usage in vision_tokens.items():
                    pricing = RAGAnalytics.PRICING_REGISTRY.get(model_name, {'input': 0.60, 'output': 3.00})
                    c_vision += (usage['in'] / 1_000_000 * pricing['input']) + (usage['out'] / 1_000_000 * pricing['output'])
            c_total = c_layout + c_vision

            if c_total > 0:
                cost1, cost2, cost3 = st.columns(3)
                cost1.metric("💰 Layout Cost", f"{c_layout:.4f} Cr")
                cost2.metric("💰 Vision Cost", f"{c_vision:.4f} Cr")
                cost3.metric("💰 Total Cost", f"{c_total:.4f} Cr")
                usd = c_total * CREDIT_TO_USD
                idr = usd * CREDIT_TO_IDR
                st.caption(f"≈ ${usd:.2f} · Rp {idr:,.0f}")

            # --- Observability / Lineage ---
            lineage = jm.get("lineage")
            if lineage:
                with st.expander("🔍 Observability Lineage", expanded=False):
                    summary = lineage.get("summary", {})
                    st.markdown(f"**Status:** {summary.get('status', 'N/A')}")
                    st.markdown(f"**Total Activities:** {summary.get('total_activities', 0)}")
                    for entry in lineage.get("lineage", []):
                        status_icon = {"PASSED": "✅", "FAILED": "❌", "RUNNING": "🔄"}.get(entry.get("status"), "❓")
                        st.markdown(f"- {status_icon} **{entry.get('activity_name', 'N/A')}** — {entry.get('status', 'N/A')}")
                        if entry.get("error"):
                            st.caption(f"  Error: {entry['error']}")

            # --- CSV Export ---
            st.divider()
            st.markdown("#### 💾 Download Results as CSV")

            job_chunks = [
                cc for cc in st.session_state.get("chunk_cache", [])
                if cc.get("job_id") == j["id"]
            ]

            if not job_chunks:
                st.caption(
                    "ℹ️ Session backup data is unavailable for this job "
                    "(cache may have been cleared or the chunk cap was reached). "
                    "Query the Snowflake table directly to retrieve all ingested chunks."
                )
            else:
                export_cols = ['CHUNK_ID', 'CHUNK', 'CHUNK_TYPE',
                               'PAGE_NUMBER', 'RELATIVE_PATH', 'CHUNK_REF', 'LINK_BLOCK']
                df_raw = pd.DataFrame(job_chunks)
                df_export = df_raw.reindex(columns=export_cols)
                csv_bytes = df_export.to_csv(index=False).encode('utf-8')
                ts = jm.get('completion_ts', 'unknown')
                st.download_button(
                    label="📥 Download CSV",
                    data=csv_bytes,
                    file_name=f"backup_job{j['id']}_{ts}.csv",
                    mime="text/csv",
                    key=f"dl_{j['id']}"
                )
