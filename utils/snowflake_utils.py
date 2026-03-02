# utils/snowflake_utils.py
import os
import json
import pandas as pd
import uuid
import streamlit as st
from logger_config import log_action
from utils.constants import LABEL_DEFINITIONS, RATE_AI_CLASSIFY
from utils.core_utils import (
    get_classify_input_tokens, get_token_count, to_sql_literal
)
import prompts

# Safe Import: Snowpark
try:
    from snowflake.snowpark.context import get_active_session
    from snowflake.snowpark.exceptions import SnowparkSQLException
except Exception:
    get_active_session = None
    SnowparkSQLException = Exception

# Safe Import: PIL
try:
    from PIL import Image
except Exception:
    Image = None

# Constants for image handling
MAX_IMAGE_MB = 3.5
CORTEX_MODEL = 'claude-sonnet-4-6'

# -----------------------------------------------------------------------------
# SNOWFLAKE INTERACTION FUNCTIONS
# -----------------------------------------------------------------------------

def get_snowpark_session():
    """Safe wrapper to obtain an active Snowpark session if available."""
    if get_active_session is None:
        return None
    try:
        return get_active_session()
    except Exception:
        return None

def scan_for_services(session, db: str, schema: str) -> list:
    """
    Scan for available Cortex Search Services in the specified database and schema.
    
    Args:
        session: Active Snowflake session
        db: Database name
        schema: Schema name
        
    Returns:
        List of service names found
    """
    if session is None:
        log_action("SCAN_SERVICES_ERROR", {"db": db, "schema": schema, "error": "No session"})
        return []
    try:
        res = session.sql(f"SHOW CORTEX SEARCH SERVICES IN SCHEMA {db}.{schema}").collect()
        services = [row["name"] for row in res]
        log_action("SCAN_SERVICES", {"db": db, "schema": schema, "services_found": len(services)})
        return services
    except Exception as e:
        st.error(f"Scan failed: {e}")
        log_action("SCAN_SERVICES_ERROR", {"db": db, "schema": schema, "error": str(e)})
        return []

def retrieve_context(session, config: dict, prompt: str) -> tuple:
    """
    Retrieve context chunks from configured Cortex Search Services.
    
    Args:
        session: Active Snowflake session
        config: Configuration dictionary containing db, schema, services, and limit
        prompt: User query prompt
        
    Returns:
        Tuple of (full_context_chunks list, retrieval_meta list)
    """
    full_context_chunks = []
    retrieval_meta = []
    
    for svc in config["services"]:
        svc_path = f"{config['db']}.{config['schema']}.{svc}"
        try:
            query_json = json.dumps({"query": prompt, "limit": config["limit"], "columns": []})
            rows = session.sql("SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(?, ?) as R", params=[svc_path, query_json]).collect()
            results = json.loads(rows[0]["R"])["results"]
            
            for doc in results:
                # Extract CHUNK_REF first and exclude it from all text-payload paths.
                # This is the sole LLM isolation boundary — CHUNK_REF must never
                # enter full_context_chunks regardless of column casing or fallback logic.
                c_ref = doc.get("CHUNK_REF") or doc.get("chunk_ref") or ""

                # Build string_values without CHUNK_REF so the max-length fallback
                # cannot inadvertently select the hyperlink Markdown as the text payload.
                string_values = [
                    v for k, v in doc.items()
                    if isinstance(v, str) and k.upper() != "CHUNK_REF"
                ]

                # Prefer the canonical CHUNK column (uppercase first for Snowflake results),
                # then common aliases, then the longest remaining string value.
                txt = (
                    doc.get("CHUNK") or doc.get("chunk") or
                    doc.get("text") or
                    (max(string_values, key=len) if string_values else "")
                )
                scores = doc.get("@scores", {})

                if txt:
                    full_context_chunks.append(txt)
                retrieval_meta.append({
                    "Service": svc,
                    "cosine_similarity": float(scores.get("cosine_similarity") or 0),
                    "text_match": float(scores.get("text_match") or 0),
                    "Full Text": txt,
                    "chunk_ref": c_ref,   # Available to Retrieval Inspector; never sent to LLM.
                    "Raw @scores": scores,
                })
        except Exception as e:
            st.warning(f"Svc {svc} error: {e}")
            log_action("RETRIEVAL_ERROR", {"service": svc, "error": str(e)})
    
    log_action("RETRIEVE_CONTEXT", {
        "prompt": prompt,
        "services_count": len(config["services"]),
        "chunks_retrieved": len(full_context_chunks)
    })
    return full_context_chunks, retrieval_meta

def generate_llm_response(session, xml_prompt: str, model_name: str, temp: float, top_p_val: float) -> dict:
    """
    Generate LLM response using AI_COMPLETE.
    
    Args:
        session: Active Snowflake session
        xml_prompt: Formatted XML prompt
        model_name: Model name to use
        temp: Temperature parameter
        top_p_val: Top P parameter
        
    Returns:
        Dictionary with response text and usage data
    """
    try:
        sql = f"""
        SELECT AI_COMPLETE(
            model => '{model_name}',
            prompt => ?,
            model_parameters => {{'temperature': {temp}, 'top_p': {top_p_val}}},
            show_details => TRUE
        ) AS R
        """
        
        raw_res = session.sql(sql, params=[xml_prompt]).collect()[0]["R"]
        
        # Parse JSON response
        try:
            resp_data = json.loads(raw_res)
        except json.JSONDecodeError:
            st.error("Failed to parse LLM response as JSON.")
            st.code(raw_res)
            resp_data = {}
        
        # Robust response text extraction
        res_text = ""
        parsing_success = False
        if isinstance(resp_data, dict):
            # Standard OpenAI format
            if "choices" in resp_data and resp_data["choices"]:
                choice = resp_data["choices"][0]
                if isinstance(choice, dict):
                    msg = choice.get("message") or choice
                    res_text = msg.get("content") or msg.get("messages") or msg.get("text") or ""
                    if res_text: parsing_success = True
            # Fallback keys
            elif "completion" in resp_data:
                res_text = resp_data["completion"]
                parsing_success = True
            elif "text" in resp_data:
                res_text = resp_data["text"]
                parsing_success = True
            elif "response" in resp_data:
                res_text = resp_data["response"]
                parsing_success = True
            else:
                res_text = str(resp_data)
        else:
            res_text = str(raw_res)
        
        # Strip and handle empty responses
        res_text = res_text.strip()
        if not res_text:
            res_text = "[Warning: Model returned empty content]"
        
        # Extract usage data
        usage_data = resp_data.get("usage", {}) if isinstance(resp_data, dict) else {}
        
        if not usage_data:
            st.warning("⚠️ No exact token usage in response. Using approximation.")
            prompt_tokens_approx = len(xml_prompt) // 4
            completion_tokens_approx = len(res_text) // 4
            usage_data = {
                "prompt_tokens": prompt_tokens_approx,
                "completion_tokens": completion_tokens_approx,
                "total_tokens": prompt_tokens_approx + completion_tokens_approx
            }
        
        log_action("LLM_GENERATION", {
            "model": model_name,
            "temperature": temp,
            "top_p": top_p_val,
            "prompt_length": len(xml_prompt),
            "response_length": len(res_text),
            "usage": usage_data
        })
        
        return {
            "text": res_text,
            "usage": usage_data,
            "parsing_success": parsing_success,
            "raw_response": raw_res,
            "resp_data": resp_data
        }
        
    except Exception as e:
        st.error(f"LLM Error: {e}")
        log_action("LLM_GENERATION_ERROR", {"model": model_name, "error": str(e)})
        return {
            "text": f"Error: {e}",
            "usage": {},
            "parsing_success": False,
            "raw_response": "",
            "resp_data": {}
        }

def process_monitoring_batch(session, batch_data: list) -> dict:
    """
    Process a batch of 5 turns through AI classification with 6 groups.
    
    Args:
        session: Active Snowflake session
        batch_data: List of turn dictionaries
        
    Returns:
        Batch record dictionary with classification results
    """
    # Construct 'input_chat_only' for Offensive, PII-Leakage, Repetitive-Failure
    input_chat_only = "\n".join([
        f"[TURN {i+1}] User: {t['user_query']}\nBot: {t['bot_response']}" 
        for i, t in enumerate(batch_data)
    ])
    
    # Construct 'input_rag_full' for Misinformation, Safety, Bias
    input_rag_full = "\n".join([
        f"[TURN {i+1}] User: {t['user_query']}\nBot: {t['bot_response']}\nContext: {t['rag_context']}" 
        for i, t in enumerate(batch_data)
    ])
    
    # PLAN-11: Use centralized faithfulness instruction
    rag_instruction = prompts.get_faithfulness_instruction()
    
    # New SQL templates using native Snowflake literals (no PARSE_JSON)
    sql_chat_only = """
    SELECT
        SNOWFLAKE.CORTEX.AI_CLASSIFY(?, {off_l}, {off_o}):labels as OFFENSIVE,
        SNOWFLAKE.CORTEX.AI_CLASSIFY(?, {pii_l}, {pii_o}):labels as PII,
        SNOWFLAKE.CORTEX.AI_CLASSIFY(?, {rep_l}, {rep_o}):labels as REPETITIVE
    """
    
    sql_rag_full = """
    SELECT
        SNOWFLAKE.CORTEX.AI_CLASSIFY(?, {mis_l}, {mis_o}):labels as MISINFO,
        SNOWFLAKE.CORTEX.AI_CLASSIFY(?, {safe_l}, {safe_o}):labels as SAFETY,
        SNOWFLAKE.CORTEX.AI_CLASSIFY(?, {bias_l}, {bias_o}):labels as BIAS
    """
    
    try:
        # Build Chat-only options as Python dicts
        off_options = {
            "task_description": LABEL_DEFINITIONS["Offensive"]["description"],
            "output_mode": "multi",
            "examples": LABEL_DEFINITIONS["Offensive"]["examples"]
        }
        pii_options = {
            "task_description": LABEL_DEFINITIONS["PII-Leakage"]["description"],
            "output_mode": "multi",
            "examples": LABEL_DEFINITIONS["PII-Leakage"]["examples"]
        }
        rep_options = {
            "task_description": LABEL_DEFINITIONS["Repetitive-Failure"]["description"],
            "output_mode": "multi",
            "examples": LABEL_DEFINITIONS["Repetitive-Failure"]["examples"]
        }

        # Format Chat-Only SQL with native literals
        formatted_sql_chat = sql_chat_only.format(
            off_l=to_sql_literal(LABEL_DEFINITIONS["Offensive"]["labels"]),
            off_o=to_sql_literal(off_options),
            pii_l=to_sql_literal(LABEL_DEFINITIONS["PII-Leakage"]["labels"]),
            pii_o=to_sql_literal(pii_options),
            rep_l=to_sql_literal(LABEL_DEFINITIONS["Repetitive-Failure"]["labels"]),
            rep_o=to_sql_literal(rep_options)
        )
        
        # Format RAG-Full SQL with Faithfulness instruction
        mis_options = {
            "task_description": LABEL_DEFINITIONS["Misinformation"]["description"] + rag_instruction,
            "output_mode": "multi",
            "examples": LABEL_DEFINITIONS["Misinformation"]["examples"]
        }
        safe_options = {
            "task_description": LABEL_DEFINITIONS["Safety"]["description"] + rag_instruction,
            "output_mode": "multi",
            "examples": LABEL_DEFINITIONS["Safety"]["examples"]
        }
        bias_options = {
            "task_description": LABEL_DEFINITIONS["Bias"]["description"] + rag_instruction,
            "output_mode": "multi",
            "examples": LABEL_DEFINITIONS["Bias"]["examples"]
        }
        
        formatted_sql_rag = sql_rag_full.format(
            mis_l=to_sql_literal(LABEL_DEFINITIONS["Misinformation"]["labels"]),
            mis_o=to_sql_literal(mis_options),
            safe_l=to_sql_literal(LABEL_DEFINITIONS["Safety"]["labels"]),
            safe_o=to_sql_literal(safe_options),
            bias_l=to_sql_literal(LABEL_DEFINITIONS["Bias"]["labels"]),
            bias_o=to_sql_literal(bias_options)
        )
        
        # Execute both SQL calls
        res_chat = session.sql(formatted_sql_chat, params=[input_chat_only] * 3).collect()[0]
        res_rag = session.sql(formatted_sql_rag, params=[input_rag_full] * 3).collect()[0]
        
        # Parse results with null handling
        def parse_cls(raw):
            if raw is None:
                return {"labels": [], "score": 0.0}
            labels = json.loads(raw) if isinstance(raw, str) else raw
            return {"labels": labels, "score": len(labels) / 10.0}
        
        # Define model rates once outside the loop for efficiency
        model_rates = {
            "claude-sonnet-4-6": {"in": 1.65, "out": 8.25},
            "openai-gpt-5": {"in": 0.69, "out": 5.5},
            "openai-gpt-4.1": {"in": 1.0, "out": 4.0},
            "deepseek-r1": {"in": 0.68, "out": 2.7}
        }
        
        # Calculate generation costs per turn
        gen_costs = []
        for turn in batch_data:
            m = turn['metadata']['model']
            rate = model_rates.get(m, {"in": 1.65, "out": 8.25})
            
            if 'usage' in turn:
                in_tokens = turn['usage']['prompt_tokens']
                out_tokens = turn['usage']['completion_tokens']
            else:
                in_tokens = get_token_count(turn['user_query'] + turn['rag_context'])
                out_tokens = get_token_count(turn['bot_response'])
            
            gen_costs.append({
                "model": m,
                "in_cost": in_tokens / 1e6 * rate["in"],
                "out_cost": out_tokens / 1e6 * rate["out"],
                "total_cost": (in_tokens / 1e6 * rate["in"]) + (out_tokens / 1e6 * rate["out"]),
                "in_tokens": in_tokens,
                "out_tokens": out_tokens
            })
        
        # Calculate Monitoring tokens with dynamic overhead formula
        tokens_off = get_classify_input_tokens(session, input_chat_only, LABEL_DEFINITIONS["Offensive"]["labels"])
        tokens_pii = get_classify_input_tokens(session, input_chat_only, LABEL_DEFINITIONS["PII-Leakage"]["labels"])
        tokens_rep = get_classify_input_tokens(session, input_chat_only, LABEL_DEFINITIONS["Repetitive-Failure"]["labels"])
        
        tokens_mis = get_classify_input_tokens(session, input_rag_full, LABEL_DEFINITIONS["Misinformation"]["labels"])
        tokens_saf = get_classify_input_tokens(session, input_rag_full, LABEL_DEFINITIONS["Safety"]["labels"])
        tokens_bia = get_classify_input_tokens(session, input_rag_full, LABEL_DEFINITIONS["Bias"]["labels"])
        
        # Calculate output char length for overhead
        def get_json_len(raw_val):
            if raw_val is None: return 0
            return len(raw_val) if isinstance(raw_val, str) else len(json.dumps(raw_val))
        
        len_off = get_json_len(res_chat["OFFENSIVE"])
        len_pii = get_json_len(res_chat["PII"])
        len_rep = get_json_len(res_chat["REPETITIVE"])
        len_mis = get_json_len(res_rag["MISINFO"])
        len_saf = get_json_len(res_rag["SAFETY"])
        len_bia = get_json_len(res_rag["BIAS"])
        
        total_output_char_len = sum([len_off, len_pii, len_rep, len_mis, len_saf, len_bia])
        
        # Dynamic Overhead Formula
        total_mon_input_tokens = sum([tokens_off, tokens_pii, tokens_rep, tokens_mis, tokens_saf, tokens_bia])
        output_tokens_est = total_output_char_len / 4
        overhead_cost = (total_mon_input_tokens + output_tokens_est) * RATE_AI_CLASSIFY
        
        batch_record = {
            "batch_id": uuid.uuid4().hex,
            "timestamp": pd.Timestamp.now().isoformat(),
            "turns": [t["metadata"] for t in batch_data],
            "turns_raw": batch_data,
            "structured_input_len": len(input_chat_only) + len(input_rag_full),
            "gen_costs": gen_costs,
            "overhead_cost": overhead_cost,
            "overhead_details": {
                "input_tokens": total_mon_input_tokens,
                "output_char_len": total_output_char_len,
                "est_output_tokens": output_tokens_est
            },
            "Offensive": parse_cls(res_chat["OFFENSIVE"]),
            "PII-Leakage": parse_cls(res_chat["PII"]),
            "Repetitive-Failure": parse_cls(res_chat["REPETITIVE"]),
            "Misinformation": parse_cls(res_rag["MISINFO"]),
            "Safety": parse_cls(res_rag["SAFETY"]),
            "Bias": parse_cls(res_rag["BIAS"])
        }
        
        log_action("BATCH_PROCESSED", {
            "batch_id": batch_record["batch_id"],
            "turn_count": len(batch_data),
            "overhead_cost": overhead_cost,
            "gen_cost_total": sum(c["total_cost"] for c in gen_costs)
        })
        
        return batch_record
        
    except Exception as e:
        st.error(f"Batch Processing Error: {e}")
        log_action("BATCH_PROCESSING_ERROR", {"error": str(e)})
        return None

# -----------------------------------------------------------------------------
# PLAN-08: ADDITIONAL SNOWFLAKE HELPERS
# -----------------------------------------------------------------------------
# PLAN-12: save_optimized_image moved to utils/core_utils.py to resolve ImportError
# PLAN-02: clean_text_for_sql and to_sql_literal moved to utils/core_utils.py to resolve circular import

def run_cortex(session, prompt, stage_root, image_path_relative, model=CORTEX_MODEL):
   """
   Executes a Cortex AI_COMPLETE call using the Single-File positional signature.
   Returns (text_response, approx_prompt_tokens, approx_completion_tokens).
   """
   if session is None:
       log_action("CORTEX_RUN_ERROR", {"error": "No session"})
       return None, 0, 0
   try:
       # Documentation Signature: AI_COMPLETE(<model>, <prompt>, <file>)
       # TO_FILE order: (stage, path)
       cmd = "SELECT SNOWFLAKE.CORTEX.AI_COMPLETE(?, ?, TO_FILE(?, ?)) AS RES"
       
       # Define root before using it in the params list
       root = stage_root if stage_root.startswith('@') else f"@{stage_root}"
       res = session.sql(cmd, params=[model, prompt, root, image_path_relative]).collect()
       
       if not res:
           return None, 0, 0
           
       res_text = res[0]['RES']
       
       if not res_text:
           return "", 0, 0

       # Since this signature returns a STRING (not JSON usage), use approximation
       # Standard multimodal cost: approx 1000 tokens per image + text
       p_tokens = (len(prompt) // 4) + 1000
       c_tokens = len(res_text) // 4
       
       return res_text.strip(), p_tokens, c_tokens
           
   except Exception as e:
       log_action("CORTEX_RUN_ERROR", {"error": str(e)})
       return None, 0, 0


def get_table_schema(session, db: str, schema: str, table: str):
   """
   Checks if a table exists and returns (exists, columns, error_message)
   """
   if session is None:
       return False, [], "No Session"
   try:
       full_path = f"{db}.{schema}.{table}"
       res = session.sql(f"DESCRIBE TABLE {full_path}").collect()
       columns = [row['name'] for row in res]
       return True, columns, None
   except Exception as e:
       return False, [], str(e)


def execute_grant_with_retry(session, sql_command: str, user_email: str, role_name: str) -> str:
    """
    Execute a GRANT SQL command with retry logic.
    
    Args:
        session: Active Snowflake session
        sql_command: The GRANT SQL command to execute
        user_email: User email for logging prefix
        role_name: The role name to return on success
        
    Returns:
        role_name on success, "Failed" on failure after retry
    """
    import time
    
    # Attempt 1
    try:
        session.sql(sql_command).collect()
        log_action("GRANT_SUCCESS", f"[USER: {user_email}] [ROLE: {role_name}] {sql_command}")
        return role_name
    except Exception as e:
        log_action("GRANT_ERROR_ATTEMPT_1", f"[USER: {user_email}] [ROLE: {role_name}] Error: {str(e)} | SQL: {sql_command}")
    
    # Delay before retry
    time.sleep(3)
    
    # Attempt 2
    try:
        session.sql(sql_command).collect()
        log_action("GRANT_SUCCESS_RETRY", f"[USER: {user_email}] [ROLE: {role_name}] {sql_command}")
        return role_name
    except Exception as e:
        log_action("GRANT_ERROR_ATTEMPT_2", f"[USER: {user_email}] Final Error: {str(e)}")
        return "Failed"
