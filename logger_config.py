# logger_config.py
# Phase 1: Persistent, untruncated logging system for RAG application
# PLAN-10: Added SessionStateLogHandler for in-memory session-based logging
import logging
import os
import json
from datetime import datetime

# Configure the logger
LOG_FILE = "app_activity.log"

# -----------------------------------------------------------------------------
# PLAN-10: SessionStateLogHandler for in-memory session logging
# -----------------------------------------------------------------------------

class SessionStateLogHandler(logging.Handler):
    """
    Custom logging handler that appends records to st.session_state['system_logs'].
    Maintains a circular buffer to prevent memory issues.
    """
    def __init__(self, capacity=1000):
        super().__init__()
        self.capacity = capacity

    def emit(self, record):
        try:
            import streamlit as st
            # PLAN-10 FIX: Ensure we are in a valid Streamlit script context
            if not st.runtime.exists():
                return
            if 'system_logs' not in st.session_state:
                st.session_state['system_logs'] = []
            
            log_entry = {
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "level": record.levelname,
                "message": record.getMessage(),
                "logger": record.name
            }
            
            st.session_state['system_logs'].append(log_entry)
            
            # Maintain capacity
            if len(st.session_state['system_logs']) > self.capacity:
                st.session_state['system_logs'] = st.session_state['system_logs'][-self.capacity:]
                
        except Exception:
            self.handleError(record)

# Create a custom logger
logger = logging.getLogger("rag_app_logger")
logger.setLevel(logging.INFO)

# Avoid adding multiple handlers if they already exist (Streamlit reload safety)
if not logger.handlers:
    # File Handler (kept for backward compatibility)
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setLevel(logging.INFO)
    
    # Formatter - Minimal decoration to allow raw untruncated JSON payloads
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    
    # PLAN-10: Add SessionStateLogHandler for UI rendering
    try:
        ui_handler = SessionStateLogHandler()
        ui_handler.setFormatter(formatter)
        logger.addHandler(ui_handler)
    except Exception:
        # Streamlit may not be available during import
        pass

def log_action(action_type: str, details: any, user_id: str = "anonymous"):
    """
    Logs an action with full untruncated details.
    
    Args:
        action_type (str): Short code for the action (e.g., 'CHAT_INPUT', 'CONFIG_UPDATE').
        details (any): Dictionary, string, or object containing payload.
        user_id (str): Identifier for the user context.
    """
    try:
        # Convert non-string details to JSON string to ensure full capture
        if not isinstance(details, str):
            payload = json.dumps(details, default=str, indent=None)
        else:
            payload = details
            
        log_entry = f"[USER:{user_id}] [ACTION:{action_type}] PAYLOAD: {payload}"
        logger.info(log_entry)
    except Exception as e:
        logger.error(f"Failed to log action: {e}")