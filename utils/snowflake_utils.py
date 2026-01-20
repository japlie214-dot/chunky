# utils/snowflake_utils.py
# Phase 2: Snowflake-specific utility functions for the RAG application
import os
import json
import pandas as pd
import uuid
import streamlit as st
from logger_config import log_action
from utils.constants import LABEL_DEFINITIONS, RATE_AI_CLASSIFY
from utils.core_utils import (
    get_classify_input_tokens, get_token_count, get_sf_literal
)

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
CORTEX_MODEL = 'claude-4-sonnet'

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
                string_values = [v for v in doc.values() if isinstance(v, str)]
                txt = doc.get("chunk") or doc.get("text") or (max(string_values, key=len) if string_values else "")
                scores = doc.get("@scores", {})
                
                if txt: 
                    full_context_chunks.append(txt)
                retrieval_meta.append({
                    "Service": svc, 
                    "cosine_similarity": float(scores.get("cosine_similarity", 0)),
                    "text_match": float(scores.get("text_match", 0)),
                    "Full Text": txt, 
                    "Raw @scores": scores
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
    
    # Faithfulness exemption instruction for RAG-aware groups
    rag_instruction = "\n\nIMPORTANT: RAG is neutral ground-truth. If bot faithfully repeats RAG, severity is 0."
    
    # SQL for Chat-Only groups
    sql_chat_only = """
    SELECT
        SNOWFLAKE.CORTEX.AI_CLASSIFY(?, PARSE_JSON({off_l}), PARSE_JSON({off_o})):labels as OFFENSIVE,
        SNOWFLAKE.CORTEX.AI_CLASSIFY(?, PARSE_JSON({pii_l}), PARSE_JSON({pii_o})):labels as PII,
        SNOWFLAKE.CORTEX.AI_CLASSIFY(?, PARSE_JSON({rep_l}), PARSE_JSON({rep_o})):labels as REPETITIVE
    """
    
    # SQL for RAG-Full groups with Faithfulness instruction
    sql_rag_full = """
    SELECT
        SNOWFLAKE.CORTEX.AI_CLASSIFY(?, PARSE_JSON({mis_l}), PARSE_JSON({mis_o})):labels as MISINFO,
        SNOWFLAKE.CORTEX.AI_CLASSIFY(?, PARSE_JSON({safe_l}), PARSE_JSON({safe_o})):labels as SAFETY,
        SNOWFLAKE.CORTEX.AI_CLASSIFY(?, PARSE_JSON({bias_l}), PARSE_JSON({bias_o})):labels as BIAS
    """
    
    try:
        # Format Chat-Only SQL
        formatted_sql_chat = sql_chat_only.format(
            off_l=get_sf_literal(LABEL_DEFINITIONS["Offensive"]["labels"]),
            off_o=get_sf_literal({"task_description": LABEL_DEFINITIONS["Offensive"]["description"], "output_mode": "multi", "examples": LABEL_DEFINITIONS["Offensive"]["examples"]}),
            pii_l=get_sf_literal(LABEL_DEFINITIONS["PII-Leakage"]["labels"]),
            pii_o=get_sf_literal({"task_description": LABEL_DEFINITIONS["PII-Leakage"]["description"], "output_mode": "multi", "examples": LABEL_DEFINITIONS["PII-Leakage"]["examples"]}),
            rep_l=get_sf_literal(LABEL_DEFINITIONS["Repetitive-Failure"]["labels"]),
            rep_o=get_sf_literal({"task_description": LABEL_DEFINITIONS["Repetitive-Failure"]["description"], "output_mode": "multi", "examples": LABEL_DEFINITIONS["Repetitive-Failure"]["examples"]})
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
            mis_l=get_sf_literal(LABEL_DEFINITIONS["Misinformation"]["labels"]),
            mis_o=get_sf_literal(mis_options),
            safe_l=get_sf_literal(LABEL_DEFINITIONS["Safety"]["labels"]),
            safe_o=get_sf_literal(safe_options),
            bias_l=get_sf_literal(LABEL_DEFINITIONS["Bias"]["labels"]),
            bias_o=get_sf_literal(bias_options)
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
        
        # Calculate generation costs per turn
        gen_costs = []
        for turn in batch_data:
            m = turn['metadata']['model']
            rate = {
                "claude-3-5-sonnet": {"in": 1.5, "out": 7.5},
                "claude-4-sonnet": {"in": 1.5, "out": 7.5},
                "openai-gpt-5": {"in": 0.69, "out": 5.5},
                "openai-gpt-4.1": {"in": 1.0, "out": 4.0},
                "deepseek-r1": {"in": 0.68, "out": 2.7}
            }.get(m, {"in": 1.5, "out": 7.5})
            
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

def save_optimized_image(image, output_dir, base_filename):
   """
   Saves an image ensuring it is strictly under the MB limit for Snowflake Cortex.
   Strategy: Resize -> PNG -> JPEG Fallback -> Iterative Compression.
   Returns path to saved file or None.
   """
   if Image is None:
       return None
   os.makedirs(output_dir, exist_ok=True)
   png_path = os.path.join(output_dir, f"{base_filename}.png")
   jpg_path = os.path.join(output_dir, f"{base_filename}.jpg")

   # 1. Resize if too wide
   try:
       if hasattr(image, 'width') and image.width > 1600:
           ratio = 1600 / image.width
           new_height = int(image.height * ratio)
           image = image.resize((1600, new_height), Image.Resampling.LANCZOS)

       # 2. Try PNG first
       image.save(png_path, format="PNG", optimize=True)
       if (os.path.getsize(png_path) / (1024 * 1024)) < MAX_IMAGE_MB:
           return png_path

       # Remove and try JPEG fallback
       try:
           os.remove(png_path)
       except Exception:
           pass

       # convert to RGB for JPEG
       if image.mode in ("RGBA", "P"):
           image = image.convert("RGB")

       quality = 95
       while True:
           image.save(jpg_path, format="JPEG", quality=quality, optimize=True)
           if (os.path.getsize(jpg_path) / (1024 * 1024)) < MAX_IMAGE_MB:
               return jpg_path
           quality -= 10
           if quality < 10:
               return jpg_path
   except Exception as e:
       log_action("IMAGE_SAVE_ERROR", {"error": str(e)})
       return None


def clean_text_for_sql(text: str) -> str:
   """Escapes single quotes for SQL safety while preserving newlines and tabs."""
   if not text:
        return ""
   safe = text.replace("'", "''")
   # Remove non-printable/control characters but preserve newlines, tabs, and carriage returns
   safe = ''.join(ch for ch in safe if ch.isprintable() or ch in ("\n", "\r", "\t"))
   return safe


def run_cortex(session, prompt, stage_root, image_path_relative, model=CORTEX_MODEL):
   """
   Executes a Cortex COMPLETE-style call. Returns raw response or None.
   """
   if session is None:
       log_action("CORTEX_RUN_ERROR", {"error": "No session"})
       return None
   try:
       cmd = "SELECT SNOWFLAKE.CORTEX.COMPLETE(?, ?, TO_FILE(?, ?)) as RES"
       root = stage_root if stage_root.startswith('@') else f"@{stage_root}"
       res = session.sql(cmd, params=[model, prompt, root, image_path_relative]).collect()
       if res:
           return res[0]['RES']
       return None
   except Exception as e:
       log_action("CORTEX_RUN_ERROR", {"error": str(e)})
       return None


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