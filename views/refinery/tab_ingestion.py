# views/refinery/tab_ingestion.py
# Ingestion Tab - Batch Execution for the Doc Refinery package
import streamlit as st
import pandas as pd
import time
from utils.core_utils import RAGAnalytics, CREDIT_TO_IDR, CREDIT_TO_USD, display_cost_card, get_cache_percentage
from views.refinery.batch_processor import run_batch_execution

def render_ingestion_tab(session):
    """Context Locking"""
    # Step-based memory banner
    pct = get_cache_percentage()
    if pct >= 80:
        if pct < 90:
            bg, fg = "#FFC107", "#333333"   # Yellow — dark text for contrast
        elif pct < 100:
            bg, fg = "#FF5722", "#FFFFFF"   # Orange
        else:
            bg, fg = "#D32F2F", "#FFFFFF"   # Red

        # Style ONLY this specific button by its aria-label (stable: Streamlit sets
        # aria-label = button text). Avoids re-colouring every primary button on page.
        st.markdown("""
        <style>
        button[aria-label="🧹 Clear In-Memory Chunks"] {
            background-color: #D32F2F !important;
            color: white !important;
            border: none !important;
        }
        </style>
        """, unsafe_allow_html=True)

        ban_col, btn_col = st.columns([5, 1])
        with ban_col:
            st.markdown(
                f"<div style='background:{bg};color:{fg};padding:10px 14px;"
                f"border-radius:6px;font-weight:bold;'>"
                f"⚠️ Session memory at {int(pct)}% capacity. Export or clear chunks soon."
                f"</div>",
                unsafe_allow_html=True
            )
        with btn_col:
            if st.button("🧹 Clear In-Memory Chunks", key="banner_clear"):
                st.session_state.chunk_cache = []
                st.rerun()

    st.subheader("2. Ingestion Execution")
    
    # Context Retrieval
    ctx = st.session_state.auth_context
    db, schema, stage = ctx["db"], ctx["schema"], ctx["stage"]
    stage_path = f"@{db}.{schema}.{stage}"
    
    # Initialize batch state session variables
    if 'batch_in_progress' not in st.session_state:
        st.session_state.batch_in_progress = False
    if 'cancel_batch' not in st.session_state:
        st.session_state.cancel_batch = False

    if not st.session_state.get('job_queue'):
        st.info("ℹ️ No jobs queued.")
        # render_quality_inspector(session)
        return

    from utils.constants import PAGE_WARNING_THRESHOLD
    pending_pages = sum(j.get('estimated_pages', 0) for j in st.session_state.job_queue if j.get('status') not in ['Completed', 'Completed with Warnings', 'Failed', 'Cancelled'])
    if pending_pages > PAGE_WARNING_THRESHOLD:
        st.warning(f"⚠️ **Advisory:** You have {pending_pages} pages queued. Processing large batches (> {PAGE_WARNING_THRESHOLD} pages) can overwhelm manual QA. Consider breaking this into smaller jobs.")
        st.toast(f"Queue size: {pending_pages} pages", icon="⚠️")

    # Build queue data
    q_data = []
    for j in st.session_state.job_queue:
        target_roles = ", ".join(j.get("grant_roles", [])) if j.get("grant_roles") else "N/A"
        q_data.append({
            "ID": j["id"],
            "File": j["file"],
            "Table": j["table"],
            "Target Roles": target_roles,
            "Status": j["status"]
        })

    df_q = pd.DataFrame(q_data)

    def style_status(row):
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

    styled_df = df_q.style.apply(style_status, axis=1)

    if "batch_audit" in st.session_state and st.session_state.batch_audit:
        with st.expander("📋 Job Queue (All Statuses)", expanded=False):
            st.dataframe(styled_df, use_container_width=True)
    else:
        st.markdown("#### 📋 Pending Execution Queue")
        st.dataframe(styled_df, use_container_width=True)

    # Run / Stop buttons — mutually exclusive based on batch_in_progress
    if not st.session_state.batch_in_progress:
        if st.button("🚀 Run Batch Execution", key="batch_run", type="primary"):
            # Initialize batch state for the one-job-per-rerun driver
            st.session_state.batch_in_progress = True
            st.session_state.cancel_batch = False
            st.session_state.batch_metrics = {
                "jobs_completed": 0, "jobs_failed": 0, "jobs_warning": 0,
                "jobs_cancelled": 0,
                "total_pages": 0, "total_chunks": 0,
                "layout_pages_processed": 0, "vision_pages_processed": 0,
                "standard_chunks": 0, "enhanced_chunks": 0,
                "total_time": 0.0, "time_layout": 0.0, "time_vision": 0.0,
                "credits_layout": 0.0, "credits_vision": 0.0,
                "enhancement_breakdown": {},
            }
            st.session_state.batch_start_time = time.time()
            st.rerun()
    else:
        st.warning("⚠️ Batch in progress. Click Stop to halt after the current job completes.")
        if st.button("🛑 Stop Batch", key="batch_stop", type="primary"):
            st.session_state.cancel_batch = True
            st.rerun()

    # One-job-per-rerun batch driver
    # run_batch_execution processes ONE job, then calls st.rerun() internally.
    # Between reruns, the Stop button is clickable. This is the ONLY way to
    # get responsive cancellation in Streamlit's single-threaded model.
    if st.session_state.batch_in_progress:
        try:
            run_batch_execution(session, db, schema, stage_path)
        except Exception as e:
            st.error(f"Batch runner failed: {e}")
            st.session_state.batch_in_progress = False
            st.session_state.cancel_batch = False

    # Report Dashboard
    if 'batch_audit' in st.session_state and st.session_state.batch_audit:
        st.divider()
        bm = st.session_state.batch_audit
        rpt_tab1, rpt_tab2 = st.tabs(["📊 Overview", "📋 Details"])
        
        with rpt_tab1:
            st.subheader("Batch Performance Overview")
            
            # Row 1: High Level - Expanded to 5 columns to accommodate Warning metric without regressions
            m1, m2, m3, m4, m5 = st.columns(5)
            # PLAN-01: Success rate calculation includes warnings in denominator
            total_finished = bm['jobs_completed'] + bm['jobs_failed'] + bm.get('jobs_warning', 0)
            m1.metric("✅ Success Rate", f"{(bm['jobs_completed'] / total_finished * 100) if total_finished > 0 else 0:.0f}%", f"{bm['jobs_completed']} Jobs")
            # PLAN-01: Orange styling for warnings with tooltip
            m2.markdown(f"<div style='color: orange; font-size: 18px; font-weight: bold;' title='Data ingested but permissions need manual review.'>⚠️ Warnings: {bm.get('jobs_warning', 0)}</div>", unsafe_allow_html=True)
            m3.metric("📄 Processed Pages", bm.get('total_pages', 0))
            
            # Time Breakdown
            total_t = bm.get('total_time', 1)
            t_layout = bm.get('time_layout', 0)
            t_vision = bm.get('time_vision', 0)
            
            m4.metric("⏱️ Total Time", f"{total_t:.1f}s")
            
            # PLAN-01: Restored Average Speed metric (was removed in previous change)
            avg_pg_time = total_t / bm['total_pages'] if bm['total_pages'] > 0 else 0
            m5.metric("⚡ Avg Speed", f"{avg_pg_time:.2f}s/pg" if bm['total_pages'] > 0 else "0s")

            # Parser Speed Row (NEW)
            s1, s2 = st.columns(2)
            l_pages = bm.get('layout_pages_processed', 0)
            v_pages = bm.get('vision_pages_processed', 0)
            
            l_speed = t_layout / l_pages if l_pages > 0 else 0
            v_speed = t_vision / v_pages if v_pages > 0 else 0
            s1.metric("🔧 Layout Speed", f"{l_speed:.2f}s/pg")
            s2.metric("👁️ Vision Speed", f"{v_speed:.2f}s/pg")

            # Page-Based Distribution (Coverage)
            if bm['total_pages'] > 0:
                l_cov = (l_pages / bm['total_pages']) * 100
                v_cov = (v_pages / bm['total_pages']) * 100
                
                # User requested % based on number of pages
                st.caption(f"Page Coverage: Layout {l_cov:.1f}% ({l_pages}/{bm['total_pages']}) | Vision {v_cov:.1f}% ({v_pages}/{bm['total_pages']})")
                
                # Progress bar shows ratio of pages touched by vision (the "enhanced" effort)
                st.progress(v_pages / bm['total_pages'])
                st.caption(f"Time Reference: Layout {t_layout:.1f}s | Vision {t_vision:.1f}s")

            st.divider()
            
            # Row 2: Cost Estimation (PLAN-16: using display_cost_card)
            st.markdown("#### 💰 Cost Estimation (Est.)")
            c_lay = bm.get('credits_layout', 0)
            c_vis = bm.get('credits_vision', 0)
            c_total = c_lay + c_vis
            
            cc1, cc2, cc3 = st.columns(3)
            with cc1:
                display_cost_card("Layout Cost", c_lay)
            with cc2:
                display_cost_card("Vision Cost", c_vis)
            with cc3:
                display_cost_card("Total Estimate", c_total)
            # PLAN-16: Conversion rate legend sourced from constants (maintainable)
            st.caption(f"*Conversion Rate: 1 Cr = ${CREDIT_TO_USD:.2f} = Rp {CREDIT_TO_IDR:,.0f}*")
            st.caption("*Based on: Layout (3.33 Cr/1k Pages) | Vision (Input 1.50/Output 7.50 per 1M Tokens)*")
            
            st.divider()
            
            # Row 3: Chunks & Enhancements
            c1, c2, c3 = st.columns(3)
            c1.metric("Total Chunks", bm.get('total_chunks', 0))
            c2.metric("Standard Chunks", bm.get('standard_chunks', 0))
            c3.metric("✨ Enhanced Chunks", bm.get('enhanced_chunks', 0))
            
            if bm.get('total_chunks', 0) > 0:
                st.progress(bm['enhanced_chunks'] / bm['total_chunks'])

        with rpt_tab2:
            if not st.session_state.job_queue:
                st.info("No jobs to display.")
            else:
                # Job Selector
                job_opts = [j for j in st.session_state.job_queue]
                selected_job = st.selectbox(
                    "Select Job to Inspect",
                    job_opts,
                    format_func=lambda x: f"Job {x['id']}: {x['file']} ({x['status']})"
                )
                
                if selected_job:
                    jm = selected_job.get('metrics', {})
                    st.divider()
                    
                    # Section 1: Performance
                    st.markdown("#### ⏱️ Performance & Speed")
                    p1, p2, p3, p4, p5, p6 = st.columns(6)
                    
                    job_status = selected_job['status']
                    if job_status == 'Completed with Warnings':
                        p1.markdown(f"<div style='color: orange; font-weight: bold;'>Status: {job_status}</div>", unsafe_allow_html=True)

                    # Display failure reason from existing metrics.error field
                    # (NOT a new field — batch_processor already populates this
                    # in both BatchCancelledError and Exception handlers)
                    error_msg = jm.get('error', '')
                    if error_msg:
                        st.error(f"**Failure Reason:** {error_msg}")

                    # Defect Detail - Page-Level Breakdown rendered cleanly below columns
                    defects_detail = jm.get('defects_detail', [])
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
                    
                    p1.caption(f"Strategy: {'L' if selected_job['layout'] else ''}{'+' if selected_job['layout'] and selected_job['vision'] else ''}{'V' if selected_job['vision'] else ''}")
                    
                    if not defects_detail:
                        p1.metric("Status", job_status)
                    
                    p2.metric("Pages Processed", jm.get('pages', 0))
                    
                    duration = jm.get('duration', 0)
                    p3.metric("Duration", f"{duration:.2f}s")
                    
                    pgs = jm.get('pages', 1) # Avoid div0
                    speed = duration / pgs if pgs > 0 else 0
                    p4.metric("Avg Speed", f"{speed:.2f}s/pg")
                    
                    target_roles_display = ", ".join(selected_job.get("grant_roles", [])) if selected_job.get("grant_roles") else "N/A"
                    p5.metric("🎯 Target Roles", target_roles_display)
                    
                    # Grant Status Pill
                    gs = selected_job.get('grant_status', {})
                    if gs.get('attempted'):
                        if gs.get('success'):
                            p6.markdown("<div style='color: green; font-weight: bold;'>✅ Grants: Success</div>", unsafe_allow_html=True)
                        else:
                            p6.markdown("<div style='color: red; font-weight: bold;'>❌ Grants: Failed</div>", unsafe_allow_html=True)
                    else:
                        p6.markdown("<div style='color: gray;'>ℹ️ Grants: N/A</div>", unsafe_allow_html=True)
                    
                    if selected_job.get('skipped_page_ranges'):
                        skipped_ranges = ', '.join([f"pp. {s['start']}-{s['end']}" for s in selected_job['skipped_page_ranges']])
                        st.warning(f"⚠️ **Partial Processing:** The following page ranges were skipped: {skipped_ranges}")
                    
                    # Section 2: Costs
                    st.divider()
                    st.markdown("#### 💰 Cost Breakdown (Est.)")
                    
                    # Layout Cost Calculation
                    l_pages = jm.get('layout_pages', 0)
                    cost_layout = (l_pages / 1000) * 3.33
                    
                    # Vision Cost Calculation (Dynamic)
                    cost_vision = 0.0
                    vt = jm.get('vision_tokens', {})
                    if vt:
                        for model_name, usage in vt.items():
                            pricing = RAGAnalytics.PRICING_REGISTRY.get(model_name, {'input': 0.60, 'output': 3.00})
                            cost_vision += (usage['in'] / 1_000_000 * pricing['input']) + (usage['out'] / 1_000_000 * pricing['output'])
                    else:
                        v_in = jm.get('vision_input_tokens', 0)
                        v_out = jm.get('vision_output_tokens', 0)
                        pricing = RAGAnalytics.PRICING_REGISTRY.get('claude-haiku-4-5', {'input': 0.60, 'output': 3.00})
                        cost_vision = (v_in / 1_000_000 * pricing['input']) + (v_out / 1_000_000 * pricing['output'])
                    
                    total_job_cost = cost_layout + cost_vision
                    
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        display_cost_card("Layout Cost", cost_layout)
                    with c2:
                        display_cost_card("Vision Cost", cost_vision)
                    with c3:
                        display_cost_card("Total Cost", total_job_cost)
                    st.caption(f"*Conversion Rate: 1 Cr = ${CREDIT_TO_USD:.2f} = Rp {CREDIT_TO_IDR:,.0f}*")
                    
                    # Section 3: Data Yield
                    st.divider()
                    st.markdown("#### 📄 Data Yield")
                    
                    std_cnt = jm.get('standard_cnt', 0)
                    enh_cnt = jm.get('enhanced_cnt', 0)
                    total_cnt = std_cnt + enh_cnt
                    
                    d1, d2, d3 = st.columns(3)
                    d1.metric("Total Chunks", total_cnt)
                    d2.metric("Standard", std_cnt)
                    d3.metric("Enhanced", enh_cnt)
                    
                    if total_cnt > 0:
                        st.caption("Enhanced Ratio")
                        st.progress(enh_cnt / total_cnt)
                        
                    # Enhancement Types Breakdown
                    if jm.get('types'):
                        with st.expander("✨ Enhancement Details"):
                            st.json(jm['types'])

                    st.divider()
                    st.markdown("#### 💾 Download Results as CSV (Session Backup)")

                    job_chunks = [
                        c for c in st.session_state.get('chunk_cache', [])
                        if c.get('job_id') == selected_job['id']
                    ]

                    if not job_chunks:
                        st.caption(
                            "ℹ️ Session backup data is unavailable for this job "
                            "(cache may have been cleared or the 5,000-chunk cap was reached). "
                            "Query the Snowflake table directly to retrieve all ingested chunks."
                        )
                    else:
                        export_cols = ['CHUNK_ID', 'CHUNK', 'CHUNK_TYPE',
                                       'PAGE_NUMBER', 'RELATIVE_PATH', 'CHUNK_REF', 'LINK_BLOCK']
                        df_raw = pd.DataFrame(job_chunks)
                        # Ensure all columns exist even if some chunks are missing data
                        df_export = df_raw.reindex(columns=export_cols)
                        csv_bytes = df_export.to_csv(index=False).encode('utf-8')
                        ts = selected_job.get('metrics', {}).get('completion_ts', 'unknown')
                        st.download_button(
                            label="📥 Download CSV",
                            data=csv_bytes,
                            file_name=f"backup_job{selected_job['id']}_{ts}.csv",
                            mime="text/csv",
                            key=f"dl_{selected_job['id']}"
                        )

    st.divider()
    # render_quality_inspector(session)
