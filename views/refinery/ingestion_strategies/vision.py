# views/refinery/ingestion_strategies/vision.py
import time
import os
import tempfile
import streamlit as st
from logger_config import log_action
from views.refinery.common import execute_sql_safe, _build_chunk_ref
from utils.core_utils import PDFUtils, convert_from_bytes, save_optimized_image
from utils.snowflake_utils import run_cortex, CORTEX_MODEL
import prompts

def _execute_vision_strategy(session, job, full_table, stage_path,
                              chunk_sz, chunk_ov, target_range, get_pdf_bytes):
    """Processes document extraction through purely visual layout decoding channels."""
    t_vis_start = time.time()
    raw_file = job['file']
    target_file = job.get('surgical_target_file') or job['file']
    target_page = int(job.get('surgical_target_page', 0))
    total_v_pgs = len(target_range)
    vision_progress = st.progress(0, text="Initializing Vision...")

    def get_val(row_dict, key, default):
        if key.upper() in row_dict: return row_dict[key.upper()]
        if key.lower() in row_dict: return row_dict[key.lower()]
        return default

    for i, pg in enumerate(target_range):
        vision_progress.progress((i + 1) / total_v_pgs, text=f"Processing Page {pg}")
        db_pg = target_page if target_page > 0 else pg
        imgs = convert_from_bytes(get_pdf_bytes(), first_page=pg, last_page=pg)
        img_path, res_txt, p_tok, c_tok = None, None, 0, 0
        
        if imgs:
            with tempfile.TemporaryDirectory() as td:
                img_name = f"vis_{job['id']}_{pg}"
                img_path = save_optimized_image(imgs[0], td, img_name, sub_folder=job['file'])
                if img_path:
                    safe_sub = PDFUtils.get_safe_folder(job['file'])
                    full_stage_path = f"{stage_path}/_temp_images/{safe_sub}"
                    session.file.put(img_path, full_stage_path, auto_compress=False, overwrite=True)
                    rel_img_path = f"_temp_images/{safe_sub}/{os.path.basename(img_path)}"
                    
                    prompt = prompts.get_vision_extraction_prompt()
                    res_txt, p_tok, c_tok = run_cortex(session, prompt, stage_path, rel_img_path, model=CORTEX_MODEL)

        if not imgs or not img_path or not res_txt:
            c_ref = _build_chunk_ref(target_file, db_pg, job.get('link', ''))
            placeholder_text = f"[Page {pg} — Vision extraction fallback]"
            ins_sql = f"INSERT INTO {full_table} (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE, CHUNK_REF, LINK_BLOCK) VALUES (?, ?, ?, CONCAT('CHK_', UUID_STRING()), 'PLACEHOLDER', ?, '')"
            session.sql(ins_sql, params=[target_file, db_pg, placeholder_text, c_ref]).collect()
            continue

        links = PDFUtils.extract_links_from_bytes(get_pdf_bytes(), pg)
        link_block = PDFUtils.format_link_block(links)
        c_ref = _build_chunk_ref(target_file, db_pg, job.get('link', ''))
        
        ins_sql = f"""
        INSERT INTO {full_table}
            (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE, CHUNK_REF, LINK_BLOCK)
        SELECT ?, ?, CASE WHEN NVL(?, '') = '' THEN C.VALUE::VARCHAR ELSE SUBSTR(C.VALUE::VARCHAR || ?, 1, 15000000) END,
               CONCAT('CHK_', UUID_STRING()), 'ENHANCED', ?, ?
        FROM LATERAL FLATTEN(
            INPUT => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(?, 'markdown', ?, ?)
        ) C
        """
        session.sql(ins_sql, params=[target_file, db_pg, link_block, link_block, c_ref, link_block, res_txt, chunk_sz, chunk_ov]).collect()

        sel_back = f"SELECT CHUNK_ID, CHUNK, CHUNK_TYPE, PAGE_NUMBER, RELATIVE_PATH, CHUNK_REF, LINK_BLOCK FROM {full_table} WHERE RELATIVE_PATH = ? AND PAGE_NUMBER = ? ORDER BY CHUNK_ID"
        inserted_rows = session.sql(sel_back, params=[target_file, db_pg]).collect()
        
        for r in inserted_rows:
            rd = r.as_dict()
            if len(st.session_state.chunk_cache) < 5000:
                st.session_state.chunk_cache.append({
                    'job_id': job['id'], 'CHUNK_ID': get_val(rd, 'CHUNK_ID', ''), 'CHUNK': get_val(rd, 'CHUNK', ''),
                    'CHUNK_TYPE': get_val(rd, 'CHUNK_TYPE', 'ENHANCED'), 'PAGE_NUMBER': get_val(rd, 'PAGE_NUMBER', 0),
                    'RELATIVE_PATH': get_val(rd, 'RELATIVE_PATH', ''), 'CHUNK_REF': get_val(rd, 'CHUNK_REF', ''), 'LINK_BLOCK': get_val(rd, 'LINK_BLOCK', '')
                })

        job['metrics']['vision_pages_list'].add(pg)
        job['metrics']['vision_input_tokens'] += p_tok
        job['metrics']['vision_output_tokens'] += c_tok
        job['metrics']['enhanced_cnt'] += len(inserted_rows)
        
        ttypes = job['metrics'].get('types', {})
        ttypes["Vision Extraction"] = ttypes.get("Vision Extraction", 0) + len(inserted_rows)
        job['metrics']['types'] = ttypes

    vision_progress.empty()
    job['metrics']['time_vision'] += (time.time() - t_vis_start)
