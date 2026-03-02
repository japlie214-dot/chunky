# views/refinery/tab_ingestion.py
# Ingestion Tab - Batch Execution for the Doc Refinery package
import streamlit as st
import pandas as pd
import time
from utils.core_utils import RAGAnalytics, CREDIT_TO_IDR, CREDIT_TO_USD, display_cost_card, get_cache_percentage
from views.refinery.batch_processor import run_batch_execution

def render_ingestion_tab(session):
    """Context Locking"""
    # PLAN-16: Step-based memory banner (Golden Rules 7, 8, 9, 10)
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
    
    if not st.session_state.get('job_queue'):
        st.info("ℹ️ No jobs queued.")
        # render_quality_inspector(session)
        return

    # Build queue data
    q_data = []
    for j in st.session_state.job_queue:
        target_roles = ", ".join(j.get("grant_roles", [])) if j.get("grant_roles") else "N/A"
        q_data.append({
            "ID": j["id"],
            "File": j["file"],
            "Table": j["table"],
            "Target Roles": target_roles,
            "Status": j["status"],
            "Access Granted": j.get('metrics', {}).get("access_granted", "")
        })

    df_q = pd.DataFrame(q_data)

    def style_status(val):
        if val == "Completed with Warnings":
            return "color: orange"
        return ""

    if hasattr(df_q.style, "map"):
        styled_df = df_q.style.map(style_status, subset=["Status"])
    else:
        styled_df = df_q.style.applymap(style_status, subset=["Status"])

    if "batch_audit" in st.session_state and st.session_state.batch_audit:
        with st.expander("📋 Job Queue (All Statuses)", expanded=False):
            st.dataframe(styled_df, use_container_width=True)
    else:
        st.markdown("#### 📋 Pending Execution Queue")
        st.dataframe(styled_df, use_container_width=True)

    if st.button("🚀 Run Batch Execution", key="batch_run", type="primary"):
        try:
            # Enforce Context
            run_batch_execution(session, db, schema, stage_path)
        except Exception as e:
            st.error(f"Batch runner failed: {e}")

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
                        p1.caption(f"Strategy: {'L' if selected_job['layout'] else ''}{'+' if selected_job['layout'] and selected_job['vision'] else ''}{'V' if selected_job['vision'] else ''}")
                    else:
                        p1.metric("Status", job_status)
                        p1.caption(f"Strategy: {'L' if selected_job['layout'] else ''}{'+' if selected_job['layout'] and selected_job['vision'] else ''}{'V' if selected_job['vision'] else ''}")
                    
                    p2.metric("Pages Processed", jm.get('pages', 0))
                    
                    duration = jm.get('duration', 0)
                    p3.metric("Duration", f"{duration:.2f}s")
                    
                    pgs = jm.get('pages', 1) # Avoid div0
                    speed = duration / pgs if pgs > 0 else 0
                    p4.metric("Avg Speed", f"{speed:.2f}s/pg")
                    
                    # PLAN-01: Display Access Granted column
                    access_granted = jm.get('access_granted', '')
                    if access_granted == 'Failed':
                        p5.markdown(f"<div style='color: orange;' title='Permissions need manual review.'>🔐 Access: Failed</div>", unsafe_allow_html=True)
                    else:
                        p5.metric("🔐 Access Granted", access_granted if access_granted else "N/A")

                    target_roles_display = ", ".join(selected_job.get("grant_roles", [])) if selected_job.get("grant_roles") else "N/A"
                    p6.metric("🎯 Target Roles", target_roles_display)
                    
                    # Section 2: Costs
                    st.divider()
                    st.markdown("#### 💰 Cost Breakdown (Est.)")
                    
                    # Layout Cost Calculation
                    l_pages = jm.get('layout_pages', 0)
                    cost_layout = (l_pages / 1000) * 3.33
                    
                    # Vision Cost Calculation
                    v_in = jm.get('vision_input_tokens', 0)
                    v_out = jm.get('vision_output_tokens', 0)
                    # Use central pricing registry for consistency and maintainability
                    pricing = RAGAnalytics.PRICING_REGISTRY.get('claude-4-sonnet', {'input': 1.50, 'output': 7.50})
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
                                       'PAGE_NUMBER', 'RELATIVE_PATH', 'CHUNK_REF']
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
