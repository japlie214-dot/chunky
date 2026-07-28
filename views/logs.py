# views/logs.py
# System Logs View - Session-based log viewer
# Added Trace ID filtering for surgical log inspection
import streamlit as st
import pandas as pd
import re
from logger_config import log_action
from utils.display_safety import safe_dataframe, safe_download_button

def render_logs_view():
    """
    Render the System Logs view for inspecting session logs.
    Displays st.session_state['system_logs'] with filtering capabilities.
    """
    st.title("📜 System Logs")
    log_action("NAVIGATE", "Visited System Logs")
    
    # Check if logs exist
    if 'system_logs' not in st.session_state or not st.session_state.system_logs:
        st.info("No logs generated in this session yet.")
        return

    # Controls
    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        if st.button("🗑️ Clear Logs"):
            # PLAN-10 FIX: Capture count before clearing to report accurate number
            count = len(st.session_state.system_logs)
            st.session_state.system_logs = []
            log_action("LOGS_CLEARED", {"count": count})
            st.rerun()
    with c2:
        if st.button("🔄 Refresh"):
            st.rerun()
            
    # Data Processing
    df = pd.DataFrame(st.session_state.system_logs)
    
    if df.empty:
        st.info("No logs to display.")
        return
    
    # Statistics
    with st.expander("📊 Log Statistics", expanded=False):
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Logs", len(df))
        with col2:
            st.metric("INFO", len(df[df['level'] == 'INFO']))
        with col3:
            st.metric("WARNING", len(df[df['level'] == 'WARNING']))
        with col4:
            st.metric("ERROR", len(df[df['level'] == 'ERROR']))
    
    # Extract Trace IDs from log messages for filtering
    df['trace_id'] = df['message'].apply(
        lambda x: re.search(r'\[TRACE:([a-f0-9-]+)\]', str(x)).group(1) if re.search(r'\[TRACE:([a-f0-9-]+)\]', str(x)) else ''
    )
    
    # Robustness: Filter out empty strings and sort for the dropdown
    unique_trace_ids = sorted([t for t in df['trace_id'].unique() if t])
    
    # Filtering
    st.markdown("### 🔍 Filter Logs")
    
    c_f1, c_f2, c_f3 = st.columns([1, 1, 2])
    with c_f1:
        level_filter = st.multiselect("Level", sorted(df['level'].unique()), default=sorted(df['level'].unique()))
    with c_f2:
        # Trace ID selection for isolating specific transactions
        trace_id_filter = st.multiselect("Trace ID", unique_trace_ids, default=[])
    
    logger_filter = st.multiselect(
        "Filter by Logger",
        sorted(df['logger'].unique()),
        default=[]
    )
    
    # Search
    search_term = st.text_input("Search in messages", placeholder="Type to search...")
    
    # Apply filters
    df_filtered = df.copy()
    
    if level_filter:
        df_filtered = df_filtered[df_filtered['level'].isin(level_filter)]
    
    if trace_id_filter:
        df_filtered = df_filtered[df_filtered['trace_id'].isin(trace_id_filter)]
    
    if logger_filter:
        df_filtered = df_filtered[df_filtered['logger'].isin(logger_filter)]
    
    if search_term:
        df_filtered = df_filtered[
            df_filtered['message'].str.contains(search_term, case=False, na=False)
        ]
    
    # Display
    if not df_filtered.empty:
        # Reverse order to show newest first
        df_display = df_filtered.iloc[::-1]
        
        st.markdown(f"### 📋 Displaying {len(df_display)} log entries")
        
        safe_dataframe(
            df_display,
            use_container_width=True,
            column_config={
                "timestamp": st.column_config.TextColumn("Time", width="small"),
                "level": st.column_config.TextColumn("Level", width="small"),
                "trace_id": st.column_config.TextColumn("Trace ID", width="medium"),
                "logger": st.column_config.TextColumn("Logger", width="medium"),
                "message": st.column_config.TextColumn("Message", width="large"),
            },
            hide_index=True,
            height=500,
            label="system_logs"
        )
        
        # Download option
        csv = df_display.to_csv(index=False)
        safe_download_button(
            "📥 Download Logs (CSV)",
            csv,
            "system_logs.csv",
            "text/csv"
        )
    else:
        st.info("No logs match the current filters.")