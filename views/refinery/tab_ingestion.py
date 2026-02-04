# views/refinery/tab_ingestion.py
# Ingestion Tab - Batch Execution for the Doc Refinery package
import streamlit as st
import pandas as pd
import time
from utils.core_utils import RAGAnalytics, CREDIT_TO_IDR
from views.refinery.batch_processor import run_batch_execution

def render_ingestion_tab(session):
    """Context Locking"""
    st.subheader("2. Ingestion Execution")
    
    # Context Retrieval
    ctx = st.session_state.auth_context
    db, schema, stage = ctx["db"], ctx["schema"], ctx["stage"]
    stage_path = f"@{db}.{schema}.{stage}"
    
    if not st.session_state.get('job_queue'):
        st.info("ℹ️ No jobs queued.")
        # render_quality_inspector(session)
        return

    if 'batch_audit' not in st.session_state or not st.session_state.batch_audit:
        st.markdown("#### 📋 Pending Execution Queue")
        q_data = [{"ID": j["id"], "File": j["file"], "Table": j["table"], "Status": j["status"]} for j in st.session_state.job_queue]
        st.dataframe(pd.DataFrame(q_data), use_container_width=True)

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
            
            # Row 1: High Level
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("✅ Success Rate", f"{(bm['jobs_completed'] / (bm['jobs_completed']+bm['jobs_failed']) * 100) if (bm['jobs_completed']+bm['jobs_failed']) > 0 else 0:.0f}%", f"{bm['jobs_completed']} Jobs")
            m2.metric("📄 Processed Pages", bm.get('total_pages', 0))
            
            # Time Breakdown
            total_t = bm.get('total_time', 1)
            t_layout = bm.get('time_layout', 0)
            t_vision = bm.get('time_vision', 0)
            
            m3.metric("⏱️ Total Time", f"{total_t:.1f}s")
            
            # Avg Time per Page
            avg_pg_time = total_t / bm['total_pages'] if bm['total_pages'] > 0 else 0
            m4.metric("⚡ Total Avg Speed", f"{avg_pg_time:.2f}s/pg" if bm['total_pages'] > 0 else "0s")

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
            
            # Row 2: Cost Estimation
            st.markdown("#### 💰 Cost Estimation (Est.)")
            c_lay = bm.get('credits_layout', 0)
            c_vis = bm.get('credits_vision', 0)
            c_total = c_lay + c_vis
            
            cc1, cc2, cc3 = st.columns(3)
            cc1.metric("Layout Cost", f"{c_lay:.4f} Cr")
            cc2.metric("Vision Cost", f"{c_vis:.4f} Cr")
            
            # Total with IDR conversion
            idr_val = c_total * CREDIT_TO_IDR
            cc3.metric("Total Estimate", f"{c_total:.4f} Cr", f"Rp {idr_val:,.0f}")
            
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
                    p1, p2, p3, p4 = st.columns(4)
                    
                    p1.metric("Status", selected_job['status'])
                    p1.caption(f"Strategy: {'L' if selected_job['layout'] else ''}{'+' if selected_job['layout'] and selected_job['vision'] else ''}{'V' if selected_job['vision'] else ''}")
                    
                    p2.metric("Pages Processed", jm.get('pages', 0))
                    
                    duration = jm.get('duration', 0)
                    p3.metric("Duration", f"{duration:.2f}s")
                    
                    pgs = jm.get('pages', 1) # Avoid div0
                    speed = duration / pgs if pgs > 0 else 0
                    p4.metric("Avg Speed", f"{speed:.2f}s/pg")
                    
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
                    idr_job_cost = total_job_cost * CREDIT_TO_IDR
                    
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Layout Cost", f"{cost_layout:.4f} Cr", help="3.33 Cr / 1k pages")
                    c2.metric("Vision Cost", f"{cost_vision:.4f} Cr", help=f"In: {v_in} | Out: {v_out} (Tokens)")
                    c3.metric("Total Cost", f"{total_job_cost:.4f} Cr", f"Rp {idr_job_cost:,.0f}")
                    
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
    # render_quality_inspector(session)