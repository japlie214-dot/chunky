# views/analytics_quality.py
# Phase 3: Quality & Safety Analytics View Module
# PLAN-10: Added DA COE / R&D disclaimer and educational context
import streamlit as st
import pandas as pd
import json
from logger_config import log_action
from utils.core_utils import render_gauge

def render_quality_analytics():
    """Render the Quality & Safety Analytics view"""
    st.title("🎯 Quality & Safety Analytics")
    log_action("NAVIGATE", "Visited Quality Analytics")
    
    # R&D Disclaimer
    st.warning(
        "🚧 **Playground Exclusive Feature** 🚧\n\n"
        "This technology is developed by the **DA COE** as an R&D initiative to test RAG reliability. "
        "Data collected here is for experimental validation within this playground environment only and does not reflect production deployment logging."
    )
    
    st.info(
        "**What is this?**\n"
        "We use a secondary 'Judge LLM' to grade the responses of your Chatbot. "
        "This ensures that the bot is not only helpful but also safe, unbiased, and faithful to the source documents."
    )
    
    # Metric Definitions Expander
    with st.expander("📚 Metric Definitions Guide"):
        st.markdown("""
        - **Offensive**: Detects toxicity, hostility, or profanity in the bot's tone.
        - **Bias**: Checks if the bot is making unfair generalizations not supported by the data.
        - **Misinformation**: **Critical.** Checks if the bot is 'Hallucinating' (inventing facts) or contradicting the RAG context.
        - **Safety**: Ensures the bot isn't providing dangerous instructions (e.g., how to build a bomb).
        - **PII Leakage**: Scans for sensitive data (Emails, Phone Numbers, IDs) leaking in the response.
        - **Repetitive Failure**: Detects if the bot is stuck in a loop (e.g., "I'm sorry, I'm sorry").
        """)
    
    # Debug Hook
    with st.expander("🛠️ Debug: Raw Session Logs"):
        st.write(st.session_state.monitoring_logs)
    
    if not st.session_state.monitoring_logs:
        st.info("No monitoring data available yet. Chat in the Playground to generate batches.")
        return
    
    # Calculate averages for gauges
    df_logs = pd.DataFrame(st.session_state.monitoring_logs)
    avg_scores = {
        group: pd.to_numeric(df_logs[group].apply(lambda x: x.get("score", 0))).mean()
        for group in ["Offensive", "Bias", "Misinformation", "Safety", "PII-Leakage", "Repetitive-Failure"]
    }
    max_scores = {
        group: pd.to_numeric(df_logs[group].apply(lambda x: x.get("score", 0))).max()
        for group in ["Offensive", "Bias", "Misinformation", "Safety", "PII-Leakage", "Repetitive-Failure"]
    }
    
    # Quality Gauges Section
    with st.expander("📖 How to Read the Meter Bars"):
        st.markdown("""
        - **Gauges**: Represent density of negative flags. Red (>75) is critical.
        - **Faithfulness**: Scores are 0 if the bot faithfully followed the RAG data.
        - **Peak Score**: Shows the highest severity recorded in the current session.
        
        **Threshold Levels:**
        - **0 - 25 (Green):** Low risk. Normal operational behavior.
        - **25 - 50 (Yellow):** Minor issues detected. Monitor for patterns.
        - **50 - 75 (Pink):** Moderate risk. Potential impact on user experience or safety.
        - **75 - 100 (Red):** High risk. Immediate intervention or guardrail adjustment required.
        - **Threshold (90):** Critical failure point.
        
        *Note: Severity is 0 for Bias/Misinfo/Safety if the bot faithfully follows RAG context.*
        """)
    
    # Render 6 Gauges with Max Score Recorded
    cols = st.columns(6)
    for i, group in enumerate(["Offensive", "Bias", "Misinformation", "Safety", "PII-Leakage", "Repetitive-Failure"]):
        with cols[i]:
            render_gauge(group, avg_scores[group])
            st.caption(f"Max: {max_scores[group]:.1%}")
    
    # Severity Trends Over Time
    st.divider()
    st.subheader("📈 Severity Trends Over Time")
    
    trend_data = []
    for i, log in enumerate(st.session_state.monitoring_logs):
        trend_data.append({
            "Batch": i + 1,
            "Offensive": log["Offensive"]["score"] * 100,
            "Bias": log["Bias"]["score"] * 100,
            "Misinformation": log["Misinformation"]["score"] * 100,
            "Safety": log["Safety"]["score"] * 100,
            "PII-Leakage": log["PII-Leakage"]["score"] * 100,
            "Repetitive-Failure": log["Repetitive-Failure"]["score"] * 100
        })
    
    df_trend = pd.DataFrame(trend_data)
    st.line_chart(df_trend.set_index("Batch"))
    
    # Calculate outliers for Batch Inspector
    all_turns_gen = []
    for log in st.session_state.monitoring_logs:
        all_turns_gen.extend(log['gen_costs'])
    
    outlier_batches = set()
    if all_turns_gen:
        df_turns = pd.DataFrame(all_turns_gen)
        mean_cost = df_turns['total_cost'].mean()
        std_cost = df_turns['total_cost'].std() if len(df_turns) > 1 else 0
        upper_band = mean_cost + std_cost
        lower_band = mean_cost - std_cost
        
        for log in st.session_state.monitoring_logs:
            for turn_cost in log['gen_costs']:
                if turn_cost['total_cost'] > upper_band or turn_cost['total_cost'] < lower_band:
                    outlier_batches.add(log['batch_id'])
    
    # Batch Inspector
    st.divider()
    st.subheader("🔍 Batch Inspector")
    
    for log in reversed(st.session_state.monitoring_logs):
        is_outlier = log['batch_id'] in outlier_batches
        icon = "⚠️" if is_outlier else "📄"
        label_extra = " [Statistical Outlier Detected]" if is_outlier else ""
        
        with st.expander(f"{icon} Batch {log['batch_id'][:8]} {label_extra} - {log['timestamp']}"):
            # Add export button
            json_data = json.dumps(log, indent=2)
            st.download_button(
                label="📥 Export Raw Batch Log",
                data=json_data,
                file_name=f"batch_{log['batch_id']}.json",
                mime="application/json"
            )
            
            # Display per-turn metadata
            st.write("**Per-Turn Metadata:**")
            st.table(pd.DataFrame(log["turns"]))
            
            # Display PII-Leakage results RAW
            pii_labels = log["PII-Leakage"]["labels"]
            if pii_labels:
                st.warning(f"⚠️ PII Detected (Raw): {', '.join(pii_labels)}")
            
            # Show Faithfulness indicators
            rag_groups = ["Misinformation", "Safety", "Bias"]
            faithfulness_status = {}
            for group in rag_groups:
                faithfulness_status[group] = "✅ Faithful" if log[group]["score"] == 0 else "⚠️ Issues Detected"
            
            if any(faithfulness_status.values()):
                st.write("**RAG Faithfulness Check:**")
                for group, status in faithfulness_status.items():
                    st.write(f"{group}: {status}")
            
            # Display triggered labels
            st.write(f"**Triggered Labels:**")
            all_labels = (
                log['Offensive']['labels'] +
                log['Bias']['labels'] +
                log['Misinformation']['labels'] +
                log['Safety']['labels'] +
                log['PII-Leakage']['labels'] +
                log['Repetitive-Failure']['labels']
            )
            if all_labels:
                for label in all_labels:
                    st.write(f"• {label}")
            else:
                st.info("No labels triggered.")
            
            # Display cost breakdown
            st.write("**Financial Details:**")
            c_gen = sum(tc['total_cost'] for tc in log['gen_costs'])
            c_ov = log['overhead_cost']
            
            c1, c2 = st.columns(2)
            c1.info(f"Generation: {c_gen:.4f} Cr")
            c2.warning(f"Overhead: {c_ov:.4f} Cr")
            
            if "overhead_details" in log:
                od = log["overhead_details"]
                st.caption(f"Overhead Logic: ({od['input_tokens']} in + {od['est_output_tokens']:.1f} out) * Rate")
    
    log_action("QUALITY_ANALYTICS_VIEWED", {
        "avg_scores": avg_scores,
        "max_scores": max_scores,
        "batches_processed": len(st.session_state.monitoring_logs)
    })