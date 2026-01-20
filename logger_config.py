# logger_config.py
# Phase 1: Persistent, untruncated logging system for RAG application
import logging
import os
import json
from datetime import datetime

# Configure the logger
LOG_FILE = "app_activity.log"

# Create a custom logger
logger = logging.getLogger("rag_app_logger")
logger.setLevel(logging.INFO)

# Avoid adding multiple handlers if they already exist (Streamlit reload safety)
if not logger.handlers:
    # File Handler
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setLevel(logging.INFO)
    
    # Formatter - Minimal decoration to allow raw untruncated JSON payloads
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)

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