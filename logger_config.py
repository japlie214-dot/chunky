# logger_config.py
# Phase 1: Persistent, untruncated logging system for RAG application
# PLAN-10: Added SessionStateLogHandler for in-memory session-based logging

###########################################################################################
# 🚨 MANDATORY LOGGING BEST PRACTICES - READ BEFORE CODING 🚨
#
# 1. TRACE CORRELATION (CRITICAL):
#    Every logical transaction (e.g., a SQL call) MUST generate a unique Trace ID (UUID).
#    Log an 'ACTION_START' entry with the input (full SQL/Params).
#    Log an 'ACTION_SUCCESS' or 'ACTION_ERROR' entry with the result/exception.
#    BOTH entries MUST share the same Trace ID to allow surgical debugging.
#
# 2. NO TRUNCATION (STRICT):
#    Never use string slicing (e.g., [:500]) on log payloads.
#    The `log_action` function uses `json.dumps` to ensure full capture of every character.
#
# 3. CONTEXTUAL TAGGING:
#    Always pass the `user_id` from the authentication context.
#    Use descriptive ACTION codes (e.g., 'DESCRIBE_SERVICE_START').
#
# 4. STRUCTURED PAYLOADS:
#    Prefer passing Python dicts/lists as the `details` argument.
###########################################################################################

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

def log_action(action_type: str, details: any, user_id: str = "anonymous", level: str = "INFO", trace_id: str = None):
    """
    Logs an action with full untruncated details and optional trace correlation.
    
    Args:
        action_type (str): Short code for the action (e.g., 'CHAT_INPUT', 'CONFIG_UPDATE').
        details (any): Dictionary, string, or object containing payload.
        user_id (str): Identifier for the user context.
        level (str): Log level - 'INFO', 'WARNING', or 'ERROR'.
        trace_id (str): Optional trace ID for correlating related log entries.
    """
    try:
        # Convert non-string details to JSON string to ensure full capture
        # No truncation - json.dumps does not have character limits by default
        if not isinstance(details, str):
            payload = json.dumps(details, default=str, indent=None)
        else:
            payload = details
            
        trace_tag = f" [TRACE:{trace_id}]" if trace_id else ""
        log_entry = f"[USER:{user_id}] [ACTION:{action_type}]{trace_tag} PAYLOAD: {payload}"
        
        if level.upper() == "ERROR":
            logger.error(log_entry)
        elif level.upper() == "WARNING":
            logger.warning(log_entry)
        else:
            logger.info(log_entry)
    except Exception as e:
        logger.error(f"Failed to log action: {e}")
