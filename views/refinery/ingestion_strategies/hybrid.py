# views/refinery/ingestion_strategies/hybrid.py
import time
import os
import tempfile
import pandas as pd
import streamlit as st
from logger_config import log_action
from views.refinery.common import execute_sql_safe, _build_chunk_ref
from utils.core_utils import PDFUtils, QualityInspector, convert_from_bytes, save_optimized_image
from utils.snowflake_utils import run_cortex, CORTEX_MODEL
import prompts

def _execute_hybrid_repair_strategy(session, job, full_table, stage_path,
                                     safe_file, pg_filter_sql,
                                     get_pdf_bytes, job_alert):
    """Analyzes and repairs visual/structural parser defects using Vision LLM context loops."""
    query_sql = (
        f"SELECT CHUNK_ID, PAGE_NUMBER, CHUNK, RELATIVE_PATH, LINK_BLOCK "
        f"FROM {full_table} "
        f"WHERE RELATIVE_PATH = '{safe_file}' {pg_filter_sql}"
    )
    ok, rows = execute_sql_safe(session, query_sql)
    if not (ok and rows):
        return

    df = pd.DataFrame(rows)
    df['STATUS'] = df['CHUNK'].apply(QualityInspector.inspect)
    defects = df[df['STATUS'] != 'OK']
    if defects.empty:
        return

    t_vis_start = time.time()
    total_fix = len(defects)
    processed_fix = 0
    job_alert.warning(f"🛠️ Found {total_fix} OCR defects in `{job['file']}`. Starting AI Repair...")
    repair_progress = st.progress(0, text="Initializing Repairs...")

    try:
        target_page_check = int(job.get('surgical_target_page', 0))
        for pg_num in defects['PAGE_NUMBER'].unique():
            pg_defects = defects[defects['PAGE_NUMBER'] == pg_num]
            source_page_to_render = job['range'][0] if target_page_check > 0 and pg_num == target_page_check else pg_num
            imgs = convert_from_bytes(get_pdf_bytes(), first_page=source_page_to_render, last_page=source_page_to_render)
            if not imgs:
                continue
                
            with tempfile.TemporaryDirectory() as td:
                img_name = f"repair_p{pg_num}"
                img_path = save_optimized_image(imgs[0], td, img_name, sub_folder=job['file'])
                if not img_path:
                    continue
                    
                safe_sub = PDFUtils.get_safe_folder(job['file'])
                full_stage_path = f"{stage_path}/_temp_images/{safe_sub}"
                session.file.put(img_path, full_stage_path, auto_compress=False, overwrite=True)
                rel_img_path = f"_temp_images/{safe_sub}/{os.path.basename(img_path)}"

                for _, row in pg_defects.iterrows():
                    processed_fix += 1
                    repair_progress.progress(processed_fix / total_fix, text=f"Repairing {processed_fix}/{total_fix}")
                    
                    link_block = row.get('LINK_BLOCK')
                    if pd.notna(link_block) and link_block:
                        clean_text = str(row['CHUNK']).replace(link_block, "", 1).rstrip()
                        quarantined_block = link_block
                    else:
                        clean_text, quarantined_block = PDFUtils.strip_link_block(str(row['CHUNK']))

                    defect_instruction = f"Fix defect: {row['STATUS']}\nIMPORTANT: Do NOT add, invent, or reference any URLs in your output."
                    prompt = prompts.get_layout_repair_prompt(clean_text, defect_instruction) if row['STATUS'] == 'REPAIR_VISUAL' else prompts.get_silver_bullet_prompt(clean_text, defect_instruction)
                    
                    res_txt, p_tok, c_tok = run_cortex(session, prompt, stage_path, rel_img_path, model=CORTEX_MODEL)
                    if res_txt:
                        if quarantined_block:
                            res_txt = PDFUtils.safe_concat(res_txt.rstrip(), quarantined_block)

                        c_ref = _build_chunk_ref(row['RELATIVE_PATH'], pg_num, job.get('link', ''))
                        
                        if len(st.session_state.chunk_cache) < 5000:
                            st.session_state.chunk_cache.append({
                                'job_id': job['id'], 'CHUNK_ID': row['CHUNK_ID'], 'CHUNK': res_txt,
                                'CHUNK_TYPE': 'ENHANCED', 'PAGE_NUMBER': pg_num, 'RELATIVE_PATH': row['RELATIVE_PATH'],
                                'CHUNK_REF': c_ref, 'LINK_BLOCK': row.get('LINK_BLOCK', '')
                            })
                        
                        upd_sql = f"UPDATE {full_table} SET CHUNK = ?, CHUNK_TYPE = 'ENHANCED', CHUNK_REF = ? WHERE CHUNK_ID = ?"
                        session.sql(upd_sql, params=[res_txt, c_ref, row['CHUNK_ID']]).collect()
                        
                        job['metrics']['vision_pages_list'].add(pg_num)
                        job['metrics']['vision_input_tokens'] += p_tok
                        job['metrics']['vision_output_tokens'] += c_tok
                        job['metrics']['enhanced_cnt'] += 1
                        
                        ttypes = job['metrics'].get('types', {})
                        etype = f"Repair: {row['STATUS']}"
                        ttypes[etype] = ttypes.get(etype, 0) + 1
                        job['metrics']['types'] = ttypes
                        
                        if job['metrics']['standard_cnt'] > 0:
                            job['metrics']['standard_cnt'] -= 1
    finally:
        repair_progress.empty()
        job_alert.empty()
        job['metrics']['time_vision'] += (time.time() - t_vis_start)
