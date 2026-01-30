# views/analytics_cost.py
# Phase 3: Cost Analytics View Module
# PLAN-10: Monitoring overhead hidden, focusing on generation costs only
import streamlit as st
import pandas as pd
import json
import plotly.graph_objects as go
from logger_config import log_action
from utils.core_utils import CREDIT_TO_USD, CREDIT_TO_IDR, display_cost_card

def render_cost_analytics():
    """Render the Cost Analytics view"""
    st.title("💰 Cost Analytics")
    log_action("NAVIGATE", "Visited Cost Analytics")
    
    # Debug Hook
    with st.expander("🛠️ Debug: Raw Session Logs"):
        st.write(st.session_state.monitoring_logs)
    
    if not st.session_state.monitoring_logs:
        st.info("No monitoring data available yet. Chat in the Playground to generate batches.")
        return
    
    # Calculate statistics for Processed Logs only
    processed_turns_costs = [
        turn_cost['total_cost']
        for log in st.session_state.monitoring_logs
        for turn_cost in log['gen_costs']
    ]
    
    total_gen_cost_logged = sum(processed_turns_costs)
    # Overhead calculation preserved but inactive per PLAN-10
    # total_overhead_cost = sum([log['overhead_cost'] for log in st.session_state.monitoring_logs])
    
    # Total cost is strictly generation for this view
    total_cost = total_gen_cost_logged
    
    # Chatbot Generation Costs Section
    st.subheader("🤖 Chatbot Generation Costs")
    
    # Prepare data for metrics
    all_turns_gen = []
    for log in st.session_state.monitoring_logs:
        all_turns_gen.extend(log['gen_costs'])
    
    total_gen_cr = total_gen_cost_logged
    avg_gen_cr = total_gen_cr / len(all_turns_gen) if all_turns_gen else 0
    
    c1, c2 = st.columns(2)
    with c1: 
        display_cost_card("Total Generation Cost", total_gen_cr, total_gen_cr * CREDIT_TO_IDR, 
                         help_text="Total credits spent on LLM generation")
    with c2: 
        display_cost_card("Avg Cost / Turn", avg_gen_cr, avg_gen_cr * CREDIT_TO_IDR, 
                         help_text="Average generation cost per turn")
    
    # Model Performance Breakdown
    st.caption("Model Performance Breakdown")
    if all_turns_gen:
        df_gen = pd.DataFrame(all_turns_gen)
        model_stats = df_gen.groupby("model")["total_cost"].agg(['count', 'sum', 'min', 'max', 'mean']).reset_index()
        model_stats.columns = ["Model", "Turns", "Total Cr", "Min Cr", "Max Cr", "Avg Cr"]
        model_stats["Rp (Total)"] = model_stats["Total Cr"] * CREDIT_TO_IDR
        st.dataframe(
            model_stats.style.highlight_max(axis=0, subset=["Avg Cr"], color="#FFE5E5"),
            use_container_width=True,
            hide_index=True
        )
    
    # Calculate model_costs for later use
    model_costs = {}
    for log in st.session_state.monitoring_logs:
        for turn_cost in log['gen_costs']:
            model = turn_cost['model']
            if model not in model_costs:
                model_costs[model] = 0.0
            model_costs[model] += turn_cost['total_cost']
    
    # --- COMMENTED OUT MONITORING OVERHEAD ---
    # st.divider()
    # st.subheader("🛡️ Monitoring Overhead")
    # 
    # total_overhead_cr = total_overhead_cost
    # total_logged_turns = sum(len(log['turns']) for log in st.session_state.monitoring_logs)
    # avg_overhead_cr = total_overhead_cr / total_logged_turns if total_logged_turns > 0 else 0
    # 
    # c1, c2 = st.columns(2)
    # with c1: 
    #     display_cost_card("Total Analysis Cost", total_overhead_cr, total_overhead_cr * CREDIT_TO_IDR, 
    #                      help_text="Total credits spent on severity analysis monitoring")
    # with c2: 
    #     display_cost_card("Avg Overhead / Turn", avg_overhead_cr, avg_overhead_cr * CREDIT_TO_IDR, 
    #                      help_text="Average monitoring cost per turn")
    
    # Statistical Analysis & Outliers
    st.divider()
    st.subheader("📊 Statistical Cost Analysis")
    
    if all_turns_gen:
        df_turns = pd.DataFrame(all_turns_gen)
        mean_cost = df_turns['total_cost'].mean()
        std_cost = df_turns['total_cost'].std() if len(df_turns) > 1 else 0
        
        upper_band = mean_cost + std_cost
        lower_band = mean_cost - std_cost
        
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Mean Cost/Turn", f"{mean_cost:.4f}")
        col2.metric("Std Dev (σ)", f"{std_cost:.4f}")
        col3.metric("Upper Band (+1σ)", f"{upper_band:.4f}")
        col4.metric("Lower Band (-1σ)", f"{lower_band:.4f}")
        
        # Visualization
        fig_stat = go.Figure()
        fig_stat.add_trace(go.Scatter(y=df_turns['total_cost'], mode='lines+markers', name='Turn Cost'))
        fig_stat.add_hline(y=upper_band, line_dash="dash", line_color="red", annotation_text="+1σ (High)")
        fig_stat.add_hline(y=lower_band, line_dash="dash", line_color="green", annotation_text="-1σ (Low)")
        fig_stat.add_hline(y=mean_cost, line_color="gray", annotation_text="Mean")
        fig_stat.update_layout(title="Cost per Turn Variance", xaxis_title="Turn Index", yaxis_title="Credits")
        st.plotly_chart(fig_stat, use_container_width=True)
        
        # Outlier Identification
        outlier_batches = set()
        for log in st.session_state.monitoring_logs:
            for turn_cost in log['gen_costs']:
                if turn_cost['total_cost'] > upper_band or turn_cost['total_cost'] < lower_band:
                    outlier_batches.add(log['batch_id'])
    else:
        outlier_batches = set()
        st.info("Insufficient data for statistical analysis.")
    
    # Overall Summary (Generation Only)
    st.subheader("📊 Overall Cost Summary (Generation Only)")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Cost (Credits)", f"{total_cost:.4f}")
    with col2:
        st.metric("Total Cost (USD)", f"${total_cost * CREDIT_TO_USD:.2f}")
    with col3:
        st.metric("Total Cost (IDR)", f"Rp {total_cost * CREDIT_TO_IDR:,.0f}")
    
    # -------------------------------------------------------------------------
    # MULTI-ANGLE COST DISSECTION
    # -------------------------------------------------------------------------
    st.divider()
    st.subheader("📊 Multi-Angle Cost Breakdown")
    
    angle_tab1, angle_tab2, angle_tab3 = st.tabs([
        "💎 Model Investment",
        "🎫 Token Composition",
        "📈 Cumulative Spend"
    ])

    with angle_tab1:
        st.caption("Total credit consumption per LLM model.")
        if model_costs:
            fig_model = go.Figure(data=[go.Bar(
                x=list(model_costs.keys()),
                y=list(model_costs.values()),
                marker=dict(color='#00CC96'),
                text=[f"{v:.4f} Cr" for v in model_costs.values()],
                textposition='auto',
            )])
            fig_model.update_layout(yaxis_title="Credits", xaxis_title="Model")
            st.plotly_chart(fig_model, use_container_width=True)

    with angle_tab2:
        st.caption("Ratio of Input (Context/RAG) vs. Output (LLM Answer) tokens.")
        total_in = sum(t['in_tokens'] for t in all_turns_gen)
        total_out = sum(t['out_tokens'] for t in all_turns_gen)
        
        if total_in + total_out > 0:
            fig_tokens = go.Figure(data=[go.Pie(
                labels=["Input Tokens (Context)", "Output Tokens (Answer)"],
                values=[total_in, total_out],
                hole=0.4,
                marker=dict(colors=["#636EFA", "#EF553B"])
            )])
            st.plotly_chart(fig_tokens, use_container_width=True)
            st.info(f"Total Tokens Processed: {total_in + total_out:,}")
        else:
            st.info("No token data available.")

    with angle_tab3:
        st.caption("Accumulated credit spend over the course of this session.")
        if all_turns_gen:
            df_gen = pd.DataFrame(all_turns_gen)
            df_gen['cumulative_cost'] = df_gen['total_cost'].cumsum()
            
            fig_trend = go.Figure()
            fig_trend.add_trace(go.Scatter(
                x=list(range(1, len(df_gen) + 1)),
                y=df_gen['cumulative_cost'],
                mode='lines+markers',
                fill='tozeroy',
                line=dict(color='#00CC96')
            ))
            fig_trend.update_layout(xaxis_title="Turn Number", yaxis_title="Total Credits Spent")
            st.plotly_chart(fig_trend, use_container_width=True)
    
    log_action("COST_ANALYTICS_VIEWED", {
        "total_cost": total_cost,
        "total_gen_cost": total_gen_cost_logged,
        "batches_processed": len(st.session_state.monitoring_logs)
    })