# views/demo/page3_execute.py
# Page 3: Job Queue & Execution — review, run batch, results.
# Execution COPIED from views/refinery/tab_ingestion.py.

import time
import streamlit as st
from views.demo.common import render_header, nav_buttons, ctx
from utils.constants import (
    DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE,
    PAGE_WARNING_THRESHOLD, LAYOUT_COST_PER_1K_PAGES,
    CREDIT_TO_USD, CREDIT_TO_IDR,
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

    if not jobs:
        st.warning("No jobs queued. Go back to Step 2 and add jobs.")
        nav_buttons(can_next=False); return

    # --- Summary (reads from jbv helper keys — always current) ---
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

    st.markdown("#### 📦 Job Details")
    for j in jobs:
        s, e = j["range"]
        scope_str = j["scope"] if j["scope"] == "Full Doc" else f"Pages {s}–{e}"
        strat = []
        if j["layout"]: strat.append("Layout")
        if j["vision"]: strat.append("Vision")
        with st.expander(f"Job #{j['id']}: `{j['file']}` → `{j['table']}` ({j['status']})"):
            dc1, dc2, dc3 = st.columns(3)
            with dc1:
                st.markdown(f"**Mode:** {j['mode']}")
                st.markdown(f"**Scope:** {scope_str}")
                st.markdown(f"**Pages:** {j['estimated_pages']}")
            with dc2:
                st.markdown(f"**Strategy:** {' + '.join(strat)}")
                st.markdown(f"**Chunk Size:** {j['params'][0]:,}")
                st.markdown(f"**Overlap:** {j['params'][1]}")
            with dc3:
                st.markdown(f"**Status:** {j['status']}")
                if j.get("link"):
                    st.markdown(f"**Link:** {j['link']}")
                if j.get("grant_roles"):
                    st.markdown(f"**Roles:** {', '.join(j['grant_roles'])}")

    st.divider()
    st.markdown("#### 🚀 Execute")

    if "batch_in_progress" not in st.session_state:
        st.session_state.batch_in_progress = False
    if "cancel_batch" not in st.session_state:
        st.session_state.cancel_batch = False

    batch_started = st.session_state.get("cssw_batch_started", False)

    if not batch_started and not st.session_state.batch_in_progress:
        pending_pages = sum(j.get("estimated_pages", 0) for j in jobs
                           if j.get("status") not in ["Completed", "Completed with Warnings", "Failed", "Cancelled"])
        if pending_pages > PAGE_WARNING_THRESHOLD:
            st.warning(f"⚠️ You have {pending_pages} pages queued. Large batches can overwhelm manual QA.")
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
            st.rerun()

    if st.session_state.batch_in_progress:
        has_pending = any(j["status"] not in ["Completed", "Completed with Warnings", "Failed", "Cancelled"]
                         for j in st.session_state.get("job_queue", []))
        if has_pending:
            st.warning("⚠️ Batch in progress. Click Stop to halt after the current job.")
            if st.button("🛑 Stop Batch"):
                st.session_state.cancel_batch = True; st.rerun()
        from views.refinery.batch_processor import run_batch_execution
        try:
            run_batch_execution(session, db, schema, stage_path)
        except Exception as e:
            st.error(f"Batch runner failed: {e}")
            st.session_state.batch_in_progress = False

    if batch_started and not st.session_state.batch_in_progress:
        # Sync job statuses from job_queue
        for wj in jobs:
            for gj in st.session_state.get("job_queue", []):
                if gj["id"] == wj["id"]:
                    wj["status"] = gj.get("status", wj["status"])
                    wj["metrics"] = gj.get("metrics", {})

        completed = sum(1 for j in jobs if j["status"] == "Completed")
        failed = sum(1 for j in jobs if j["status"] == "Failed")
        warns = sum(1 for j in jobs if j["status"] == "Completed with Warnings")
        if failed > 0:
            st.error(f"⚠️ {failed} job(s) failed.")
        elif warns > 0:
            st.warning(f"⚠️ {completed} completed, {warns} with warnings.")
        else:
            st.success(f"🎉 All {completed} job(s) completed!")

        st.divider()
        st.markdown("#### 📊 Results")

        for j in jobs:
            jm = j.get("metrics", {})
            tbl = j["table"].split(".")[-1]
            icon = {"Completed": "✅", "Failed": "❌", "Completed with Warnings": "⚠️"}.get(j["status"], "ℹ️")

            with st.expander(f"{icon} Job #{j['id']}: `{j['file']}` → `{tbl}` — {j['status']}", expanded=True):
                # Row 1: Overview
                rc1, rc2, rc3, rc4 = st.columns(4)
                rc1.metric("📄 Pages", jm.get("pages", 0))
                rc2.metric("📦 Chunks", jm.get("standard_cnt", 0) + jm.get("enhanced_cnt", 0))
                rc3.metric("⏱️ Duration", f"{jm.get('duration', 0):.1f}s")
                rc4.metric("📊 Status", j["status"])

                # Row 2: Strategy breakdown
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

                # Page coverage bar
                if total_pg > 0:
                    l_cov = (lay_pages / total_pg) * 100
                    v_cov = (vis_pages / total_pg) * 100
                    st.caption(f"Coverage: Layout {l_cov:.0f}% ({lay_pages}/{total_pg}) · Vision {v_cov:.0f}% ({vis_pages}/{total_pg})")

                # Row 3: Chunk details
                standard = jm.get("standard_cnt", 0)
                enhanced = jm.get("enhanced_cnt", 0)
                total_chunks = standard + enhanced

                ch1, ch2, ch3 = st.columns(3)
                ch1.metric("📐 Standard Chunks", standard)
                ch2.metric("✨ Enhanced Chunks", enhanced)
                if total_chunks > 0 and enhanced > 0:
                    ch3.metric("🔧 Enhancement Rate", f"{(enhanced / total_chunks) * 100:.1f}%")

                # Row 4: Cost estimation
                c_layout = (lay_pages / 1000) * LAYOUT_COST_PER_1K_PAGES if lay_pages > 0 else 0
                c_vision = 0
                vision_tokens = jm.get("vision_tokens", {})
                if vision_tokens:
                    from utils.core_utils import RAGAnalytics
                    from utils.constants import FALLBACK_VISION_MODEL
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

                if jm.get("error"):
                    st.error(f"Error: {jm['error']}")

        # --- Cache table columns for Step 4 (silently, no UI) ---
        if "cssw_table_columns" not in st.session_state:
            all_cols = _fetch_all_table_columns(session, db, schema, jobs)
            if all_cols:
                st.session_state.cssw_table_columns = all_cols

        nav_buttons(can_next=True, next_label="Next ➡️")
    elif not batch_started:
        st.info("Click **Run Batch Execution** to start.")
        nav_buttons(can_next=False)
    else:
        nav_buttons(can_next=False)
