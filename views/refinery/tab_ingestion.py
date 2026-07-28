# views/refinery/tab_ingestion.py
# Ingestion Tab - Batch Execution for the Doc Refinery package
import streamlit as st
import pandas as pd
import time
from utils.core_utils import RAGAnalytics, CREDIT_TO_IDR, CREDIT_TO_USD, display_cost_card, get_cache_percentage
from utils.constants import LAYOUT_COST_PER_1K_PAGES, FALLBACK_VISION_MODEL
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
                "layout_pages_list": set(), "vision_pages_list": set(),
                "standard_chunks": 0, "enhanced_chunks": 0,
                "total_time": 0.0, "time_layout": 0.0, "time_vision": 0.0,
                "credits_layout": 0.0, "credits_vision": 0.0,
                "enhancement_breakdown": {},
            }
            st.session_state.batch_start_time = time.time()
            st.rerun()
    else:
        # Check if there are actually pending jobs remaining.
        # run_batch_execution sets batch_in_progress=False when all jobs finish,
        # but the UI was already rendered before that call. This guard hides
        # the Stop button and warning once no pending jobs remain.
        has_pending = any(
            j['status'] not in ['Completed', 'Completed with Warnings', 'Failed', 'Cancelled']
            for j in st.session_state.get('job_queue', [])
        )
        if has_pending:
            st.warning("⚠️ Batch in progress. Click Stop to halt after the current job completes.")
            if st.button("🛑 Stop Batch", key="batch_stop", type="primary"):
                st.session_state.cancel_batch = True
                st.rerun()
        else:
            st.session_state.batch_in_progress = False

    # One-job-per-rerun batch driver
    # run_batch_execution processes ONE job, then calls st.rerun() internally.
    # Between reruns, the Stop button is clickable. This is the ONLY way to
    # get responsive cancellation in Streamlit's single-threaded model.
    if st.session_state.batch_in_progress:
        try:
            run_batch_execution(session, db, schema, stage_path)
        except Exception as e:
            st.error(f"Batch runner failed: {e}")
            # Ensure batch_audit is set so the dashboard renders even on failure.
            bm = st.session_state.get('batch_metrics', {})
            if bm:
                bm.setdefault('total_time', time.time() - st.session_state.get('batch_start_time', time.time()))
                bm.setdefault('total_chunks', bm.get('standard_chunks', 0) + bm.get('enhanced_chunks', 0))
                st.session_state.batch_audit = bm
            st.session_state.batch_in_progress = False
            st.session_state.cancel_batch = False

    # Report Dashboard — render from batch_audit (current batch) or
    # ingestion_history (all completed jobs this session) as fallback.
    _dashboard_audit = st.session_state.get('batch_audit')
    if not _dashboard_audit and st.session_state.get('ingestion_history'):
        # Reconstruct aggregate metrics from ingestion_history so the
        # dashboard is always available after at least one batch run.
        _hist = st.session_state.ingestion_history
        _reconstructed = {
            'jobs_completed': sum(1 for j in _hist if j.get('status') == 'Completed'),
            'jobs_failed':    sum(1 for j in _hist if j.get('status') == 'Failed'),
            'jobs_warning':   sum(1 for j in _hist if j.get('status') == 'Completed with Warnings'),
            'jobs_cancelled': sum(1 for j in _hist if j.get('status') == 'Cancelled'),
            'total_pages':    sum(j.get('metrics', {}).get('pages', 0) for j in _hist),
            'total_chunks':   sum(j.get('metrics', {}).get('standard_cnt', 0) + j.get('metrics', {}).get('enhanced_cnt', 0) for j in _hist),
            'layout_pages_processed': sum(j.get('metrics', {}).get('layout_pages', 0) for j in _hist),
            'vision_pages_processed': sum(len(j.get('metrics', {}).get('vision_pages_list', set())) for j in _hist),
            'layout_pages_list': set().union(*(j.get('metrics', {}).get('layout_pages_list', set()) for j in _hist)),
            'vision_pages_list': set().union(*(j.get('metrics', {}).get('vision_pages_list', set()) for j in _hist)),
            'standard_chunks': sum(j.get('metrics', {}).get('standard_cnt', 0) for j in _hist),
            'enhanced_chunks': sum(j.get('metrics', {}).get('enhanced_cnt', 0) for j in _hist),
            'total_time':     sum(j.get('metrics', {}).get('duration', 0) for j in _hist),
            'time_layout':    sum(j.get('metrics', {}).get('time_layout', 0) for j in _hist),
            'time_vision':    sum(j.get('metrics', {}).get('time_vision', 0) for j in _hist),
            'credits_layout': sum((j.get('metrics', {}).get('layout_pages', 0) / 1000) * LAYOUT_COST_PER_1K_PAGES for j in _hist),
            'credits_vision': 0.0,
            'enhancement_breakdown': {},
        }
        for j in _hist:
            vt = j.get('metrics', {}).get('vision_tokens', {})
            for model_name, usage in vt.items():
                pricing = RAGAnalytics.PRICING_REGISTRY.get(model_name, {'input': 0.60, 'output': 3.00})
                _reconstructed['credits_vision'] += (usage['in'] / 1_000_000 * pricing['input']) + (usage['out'] / 1_000_000 * pricing['output'])
            for etype, count in j.get('metrics', {}).get('types', {}).items():
                _reconstructed['enhancement_breakdown'][etype] = _reconstructed['enhancement_breakdown'].get(etype, 0) + count
        if any(v for k, v in _reconstructed.items() if k not in ('enhancement_breakdown',)):
            _dashboard_audit = _reconstructed

    if _dashboard_audit:
        st.divider()
        bm = _dashboard_audit
        
        # Prominent completion banner so users notice the dashboard below
        total_finished = bm['jobs_completed'] + bm['jobs_failed'] + bm.get('jobs_warning', 0) + bm.get('jobs_cancelled', 0)
        if bm['jobs_failed'] > 0:
            st.error(f"⚠️ Batch finished with {bm['jobs_failed']} failure(s). Review details below.")
        elif bm.get('jobs_warning', 0) > 0:
            st.warning(f"⚠️ Batch completed with {bm.get('jobs_warning', 0)} warning(s). Review details below.")
        else:
            st.success(f"🎉 Batch completed successfully — {bm['jobs_completed']} of {total_finished} jobs succeeded.")
        
        rpt_tab1, rpt_tab2 = st.tabs(["📊 Overview", "📋 Details"])
        
        with rpt_tab1:
            st.subheader("Batch Performance Overview")
            
            # Row 1: High Level
            m1, m2, m3 = st.columns(3)
            total_finished = bm['jobs_completed'] + bm['jobs_failed'] + bm.get('jobs_warning', 0)
            m1.metric("✅ Success Rate", f"{(bm['jobs_completed'] / total_finished * 100) if total_finished > 0 else 0:.0f}%", f"{bm['jobs_completed']} Jobs")
            m2.markdown(f"<div style='color: orange; font-size: 18px; font-weight: bold;' title='Data ingested but permissions need manual review.'>⚠️ Warnings: {bm.get('jobs_warning', 0)}</div>", unsafe_allow_html=True)
            m3.metric("📄 Processed Pages", bm.get('total_pages', 0))

            st.divider()

            # Section: ⏱️ Performance
            st.markdown("#### ⏱️ Performance")
            total_t = bm.get('total_time', 1)
            t_layout = bm.get('time_layout', 0)
            t_vision = bm.get('time_vision', 0)
            l_pages = bm.get('layout_pages_processed', 0)
            v_pages = bm.get('vision_pages_processed', 0)
            avg_pg_time = total_t / bm['total_pages'] if bm['total_pages'] > 0 else 0
            l_speed = t_layout / l_pages if l_pages > 0 else 0
            v_speed = t_vision / v_pages if v_pages > 0 else 0

            perf1, perf2, perf3, perf4 = st.columns(4)
            perf1.metric("⏱️ Total Time", f"{total_t:.1f}s")
            perf2.metric("⚡ Avg Speed", f"{avg_pg_time:.2f}s/pg" if bm['total_pages'] > 0 else "0s")
            perf3.metric("🔧 Layout Speed", f"{l_speed:.2f}s/pg")
            perf4.metric("👁️ Vision Speed", f"{v_speed:.2f}s/pg")

            if bm['total_pages'] > 0:
                l_cov = (l_pages / bm['total_pages']) * 100
                v_cov = (v_pages / bm['total_pages']) * 100
                st.caption(f"Page Coverage: Layout {l_cov:.1f}% ({l_pages}/{bm['total_pages']}) | Vision {v_cov:.1f}% ({v_pages}/{bm['total_pages']})")
                st.progress(v_pages / bm['total_pages'])
                st.caption(f"Time Reference: Layout {t_layout:.1f}s | Vision {t_vision:.1f}s")

            st.divider()

            # Section: 📊 Chunk Statistics
            st.markdown("#### 📊 Chunk Statistics")
            total_chunks = bm.get('total_chunks', 0)
            chunk_cache = st.session_state.get('chunk_cache', [])
            avg_chunk_size = 0
            if chunk_cache:
                sizes = [len(str(c.get('CHUNK', ''))) for c in chunk_cache if c.get('CHUNK')]
                avg_chunk_size = sum(sizes) / len(sizes) if sizes else 0

            # Token totals from vision metrics
            total_tokens = 0
            for j in st.session_state.get('ingestion_history', []):
                jm = j.get('metrics', {})
                total_tokens += jm.get('vision_input_tokens', 0) + jm.get('vision_output_tokens', 0)
            avg_tokens = total_tokens / total_chunks if total_chunks > 0 else 0

            cs1, cs2, cs3, cs4 = st.columns(4)
            cs1.metric("📦 Total Chunks", total_chunks)
            cs2.metric("📏 Avg Size/Chunk", f"{avg_chunk_size:,.0f} chars" if avg_chunk_size > 0 else "N/A")
            cs3.metric("🔤 Total Tokens", f"{total_tokens:,}" if total_tokens > 0 else "N/A")
            cs4.metric("📊 Avg Tokens/Chunk", f"{avg_tokens:,.0f}" if avg_tokens > 0 else "N/A")

            st.divider()

            # Section: 💰 Cost Estimation
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
            st.caption(f"*Conversion Rate: 1 Cr = ${CREDIT_TO_USD:.2f} = Rp {CREDIT_TO_IDR:,.0f}*")
            st.caption(f"*Based on: Layout ({LAYOUT_COST_PER_1K_PAGES} Cr/1k Pages) | Vision (Input 1.50/Output 7.50 per 1M Tokens)*")
            
            st.divider()
            
            # Section: 📄 Data Yield
            st.markdown("#### 📄 Data Yield")
            c1, c2, c3 = st.columns(3)
            c1.metric("Total Chunks", total_chunks)
            c2.metric("Standard Chunks", bm.get('standard_chunks', 0))
            c3.metric("✨ Enhanced Chunks", bm.get('enhanced_chunks', 0))
            
            if total_chunks > 0:
                st.progress(bm['enhanced_chunks'] / total_chunks)

        with rpt_tab2:
            # Use job_queue (current batch) or ingestion_history (all completed jobs) as fallback
            _detail_jobs = [j for j in st.session_state.job_queue if j.get('status') not in ['Pending']]
            if not _detail_jobs:
                _detail_jobs = [j for j in st.session_state.get('ingestion_history', []) if j.get('metrics')]
            if not _detail_jobs:
                st.info("No completed jobs to display.")
            else:
                # Job Selector
                job_opts = _detail_jobs
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
                    
                    # Page-Level Layout/Vision Coverage
                    layout_pages = jm.get('layout_pages_list', set())
                    vision_pages = jm.get('vision_pages_list', set())
                    all_pages = layout_pages | vision_pages
                    if all_pages:
                        with st.expander("📄 Page Coverage (Layout / Vision)", expanded=False):
                            for pg in sorted(all_pages):
                                has_layout = "✅ Layout" if pg in layout_pages else "❌ Layout"
                                has_vision = "✅ Vision" if pg in vision_pages else "❌ Vision"
                                st.markdown(f"**Page {pg}:** {has_layout} | {has_vision}")
                            st.caption(
                                f"Layout: {len(layout_pages)} pages | "
                                f"Vision: {len(vision_pages)} pages | "
                                f"Total unique: {len(all_pages)} pages"
                            )
                    
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
                    cost_layout = (l_pages / 1000) * LAYOUT_COST_PER_1K_PAGES
                    
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
                        pricing = RAGAnalytics.PRICING_REGISTRY.get(FALLBACK_VISION_MODEL, {'input': 0.60, 'output': 3.00})
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
