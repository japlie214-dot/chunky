# views/refinery/ingestion_strategies.py
# Parsing strategies for the Doc Refinery ingestion pipeline
"""
Ingestion strategies: Layout (SQL), Hybrid Repair, and Vision Only.
CONSTRAINT: This module must have zero imports from batch_processor.py or
tab_deployment.py. All functions receive job dict and mutate job['metrics']
incrementally to capture partial success.
"""
import time
import os
import tempfile
import pandas as pd
import streamlit as st
from logger_config import log_action
from views.refinery.common import execute_sql_safe, _build_chunk_ref
from utils.core_utils import (
    PDFUtils, QualityInspector, convert_from_bytes, save_optimized_image
)
from utils.snowflake_utils import run_cortex, CORTEX_MODEL
import prompts


# -----------------------------------------------------------------------------
# Layout Strategy (SQL-based)
# -----------------------------------------------------------------------------

def _execute_layout_strategy(session, job, full_table, stage_path,
                              db, schema, table_name,
                              chunk_sz, chunk_ov, json_opts, safe_file,
                              job_pages_count):
    """
    Executes the Layout (SQL) strategy:  SELECT → collect → augment → write_pandas.
    Mutates job['metrics']['standard_cnt'] incrementally (once per augmented row)
    so partial progress is captured if write_pandas raises.
    Requires job_pages_count explicitly; never reads a non-existent 'estimated_pages' key.
    
    Args:
        session: Snowpark session
        job: Job dictionary with metrics dict
        full_table: Fully qualified table name with escaped identifiers
        stage_path: Stage path for document storage
        db: Database name
        schema: Schema name
        table_name: Table name (bare, no db/schema prefix)
        chunk_sz: Chunk size for text splitting
        chunk_ov: Chunk overlap for text splitting
        json_opts: JSON options for AI_PARSE_DOCUMENT
        safe_file: SQL-escaped filename
        job_pages_count: Total page count for the job
    """
    t_layout_start = time.time()

    src_sql = f"""
    WITH PARSED AS (
        SELECT '{safe_file}' AS RELATIVE_PATH,
        SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(
            TO_FILE('{stage_path}', '{safe_file}'), PARSE_JSON('{json_opts}')
        ) AS J
    )
    SELECT
        P.RELATIVE_PATH::VARCHAR              AS RELATIVE_PATH,
        (pg.value:index::INT + 1)::NUMBER     AS PAGE_NUMBER,
        ch.value::VARCHAR                     AS CHUNK,
        CONCAT('CHK_', UUID_STRING())::VARCHAR AS CHUNK_ID,
        'STANDARD'::VARCHAR                   AS CHUNK_TYPE
    FROM PARSED P,
         LATERAL FLATTEN(input => J:pages) pg,
         LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(
             pg.value:content::VARCHAR, 'markdown', {chunk_sz}, {chunk_ov}
         )) ch
    """
    try:
        collected_rows = session.sql(src_sql).collect()
    except Exception as e:
        raise Exception(f"Layout SQL SELECT Failed: {e}")

    def get_val(row_dict, key, default):
        if key.upper() in row_dict: return row_dict[key.upper()]
        if key.lower() in row_dict: return row_dict[key.lower()]
        return default

    augmented_rows = []
    link_val = job.get('link', '')

    for row in collected_rows:
        r    = row.as_dict()
        rel  = get_val(r, 'RELATIVE_PATH', '')
        pg_n = get_val(r, 'PAGE_NUMBER', 0)
        chk  = get_val(r, 'CHUNK', '')
        cid  = get_val(r, 'CHUNK_ID', '')
        ctyp = get_val(r, 'CHUNK_TYPE', 'STANDARD')
        c_ref = _build_chunk_ref(rel, pg_n, link_val)
        augmented_rows.append({
            'RELATIVE_PATH': rel, 'PAGE_NUMBER': pg_n, 'CHUNK': chk,
            'CHUNK_ID': cid,      'CHUNK_TYPE': ctyp, 'CHUNK_REF': c_ref,
        })
        # Incremental metric mutation — captured by finally even if write_pandas raises
        job['metrics']['standard_cnt'] += 1
        if len(st.session_state.chunk_cache) < 5000:
            st.session_state.chunk_cache.append({
                'job_id': job['id'], 'CHUNK_ID': cid, 'CHUNK': chk,
                'CHUNK_TYPE': ctyp, 'PAGE_NUMBER': pg_n,
                'RELATIVE_PATH': rel, 'CHUNK_REF': c_ref,
            })
        elif not job.get('cache_limit_logged'):
            log_action("CACHE_LIMIT_REACHED", {"file": job['file']}, level="WARNING")
            job['cache_limit_logged'] = True

    if augmented_rows:
        df_write = pd.DataFrame(augmented_rows)
        # Dtype enforcement — pd.StringDtype() for CHUNK_REF prevents "None" string corruption
        df_write['RELATIVE_PATH'] = df_write['RELATIVE_PATH'].astype(str)
        df_write['PAGE_NUMBER']   = df_write['PAGE_NUMBER'].astype('int64')
        df_write['CHUNK']         = df_write['CHUNK'].astype(str)
        df_write['CHUNK_ID']      = df_write['CHUNK_ID'].astype(str)
        df_write['CHUNK_TYPE']    = df_write['CHUNK_TYPE'].astype(str)
        df_write['CHUNK_REF']     = df_write['CHUNK_REF'].astype(pd.StringDtype())
        try:
            session.write_pandas(
                df_write,
                table_name=table_name,
                database=db,
                schema=schema,
                overwrite=False,
                auto_create_table=False,
            )
        except Exception as e:
            raise Exception(f"Layout write_pandas Failed: {e}")

    job['metrics']['layout_pages']  = job_pages_count   # set after successful write
    job['metrics']['time_layout']  += (time.time() - t_layout_start)


# -----------------------------------------------------------------------------
# Hybrid Repair Strategy
# -----------------------------------------------------------------------------

def _execute_hybrid_repair_strategy(session, job, full_table, stage_path,
                                     safe_file, pg_filter_sql,
                                     get_pdf_bytes, job_alert):
    """
    Executes the Hybrid Repair strategy: fetches defective chunks, runs vision AI
    per defect, and writes each repair immediately (per-chunk UPDATE is mandatory;
    see Plan rationale — batching is prohibited here).
    job_alert: the st.empty() placeholder created by the orchestrator, passed in
    so the helper can display and clear its warning feedback.
    Mutates job['metrics'] incrementally on every successful repair.
    
    Args:
        session: Snowpark session
        job: Job dictionary with metrics dict
        full_table: Fully qualified table name with escaped identifiers
        stage_path: Stage path for document storage
        safe_file: SQL-escaped filename
        pg_filter_sql: Additional page range filter SQL
        get_pdf_bytes: Callable that returns PDF bytes
        job_alert: st.empty() placeholder for UI feedback
    """
    q_sql = (
        f"SELECT CHUNK_ID, PAGE_NUMBER, CHUNK, RELATIVE_PATH "
        f"FROM {full_table} "
        f"WHERE RELATIVE_PATH = '{safe_file}' {pg_filter_sql}"
    )
    ok, rows = execute_sql_safe(session, q_sql)
    if not (ok and rows):
        return

    df = pd.DataFrame(rows)
    df['STATUS'] = df['CHUNK'].apply(QualityInspector.inspect)
    defects = df[df['STATUS'] != 'OK']
    if defects.empty:
        return

    t_vis_start    = time.time()
    total_fix      = len(defects)
    processed_fix  = 0
    job_alert.warning(
        f"🛠️ Found {total_fix} OCR defects in `{job['file']}`. Starting AI Repair..."
    )
    repair_progress = st.progress(0, text="Initializing Repairs...")

    try:
        for pg_num in defects['PAGE_NUMBER'].unique():
            imgs = convert_from_bytes(get_pdf_bytes(), first_page=pg_num, last_page=pg_num)
            if not imgs:
                continue
            with tempfile.TemporaryDirectory() as td:
                img_name  = f"repair_p{pg_num}"
                img_path  = save_optimized_image(imgs[0], td, img_name, sub_folder=job['file'])
                if not img_path:
                    continue
                safe_sub        = PDFUtils.get_safe_folder(job['file'])
                full_stage_path = f"{stage_path}/_temp_images/{safe_sub}"
                session.file.put(img_path, full_stage_path, auto_compress=False, overwrite=True)
                rel_img_path = f"_temp_images/{safe_sub}/{os.path.basename(img_path)}"

                for _, row in defects[defects['PAGE_NUMBER'] == pg_num].iterrows():
                    processed_fix += 1
                    repair_progress.progress(
                        processed_fix / total_fix,
                        text=f"Repairing {processed_fix}/{total_fix}",
                    )
                    prompt = prompts.get_silver_bullet_prompt(
                        row['CHUNK'], f"Fix defect: {row['STATUS']}"
                    )
                    res_txt, p_tok, c_tok = run_cortex(
                        session, prompt, stage_path, rel_img_path, model=CORTEX_MODEL
                    )
                    if res_txt:
                        c_ref = _build_chunk_ref(row['RELATIVE_PATH'], pg_num, job.get('link', ''))
                        # Cache before SQL write
                        cache_entry = {
                            'job_id': job['id'], 'CHUNK_ID': row['CHUNK_ID'],
                            'CHUNK': res_txt, 'CHUNK_TYPE': 'ENHANCED',
                            'PAGE_NUMBER': pg_num, 'RELATIVE_PATH': row['RELATIVE_PATH'],
                            'CHUNK_REF': c_ref,
                        }
                        if len(st.session_state.chunk_cache) < 5000:
                            st.session_state.chunk_cache.append(cache_entry)
                        elif not job.get('cache_limit_logged'):
                            log_action("CACHE_LIMIT_REACHED", {"file": job['file']}, level="WARNING")
                            job['cache_limit_logged'] = True
                        # Per-chunk immediate write (DO NOT BATCH — fault-tolerance requirement)
                        upd_sql = (
                            f"UPDATE {full_table} "
                            f"SET CHUNK = ?, CHUNK_TYPE = 'ENHANCED', CHUNK_REF = ? "
                            f"WHERE CHUNK_ID = ?"
                        )
                        try:
                            session.sql(upd_sql, params=[res_txt, c_ref, row['CHUNK_ID']]).collect()
                        except Exception as e:
                            log_action("SQL_UPDATE_ERROR", {"error": str(e)})
                        # Incremental metric mutations immediately after confirmed write
                        job['metrics']['vision_pages_list'].add(pg_num)
                        job['metrics']['vision_input_tokens']  += p_tok
                        job['metrics']['vision_output_tokens'] += c_tok
                        etype = f"Repair: {row['STATUS']}"
                        job['metrics']['enhanced_cnt'] += 1
                        job['metrics']['types'][etype]  = job['metrics']['types'].get(etype, 0) + 1
                        if job['metrics']['standard_cnt'] > 0:
                            job['metrics']['standard_cnt'] -= 1
    except Exception as e:
        log_action("REPAIR_ERROR", {"job": job['id'], "error": str(e)})
    finally:
        repair_progress.empty()
        job_alert.empty()
        job['metrics']['time_vision'] += (time.time() - t_vis_start)


# -----------------------------------------------------------------------------
# Vision Only Strategy
# -----------------------------------------------------------------------------

def _execute_vision_strategy(session, job, full_table, stage_path,
                              chunk_sz, chunk_ov, target_range, get_pdf_bytes):
    """
    Executes the Vision Only strategy: renders each page as an image, sends to
    Cortex AI_COMPLETE, splits the result, inserts rows, then SELECT-backs to
    retrieve Snowflake-generated CHUNK_IDs for the session cache.
    Mutates job['metrics'] incrementally after each successful page insertion.
    Creates and clears its own vision_progress bar.
    
    Args:
        session: Snowpark session
        job: Job dictionary with metrics dict
        full_table: Fully qualified table name with escaped identifiers
        stage_path: Stage path for document storage
        chunk_sz: Chunk size for text splitting
        chunk_ov: Chunk overlap for text splitting
        target_range: Range of page numbers to process
        get_pdf_bytes: Callable that returns PDF bytes
    """
    t_vis_start   = time.time()
    raw_file      = job['file']
    total_v_pgs   = len(target_range)
    vision_progress = st.progress(0, text="Initializing Vision...")

    def get_val(row_dict, key, default):
        if key.upper() in row_dict: return row_dict[key.upper()]
        if key.lower() in row_dict: return row_dict[key.lower()]
        return default

    for i, pg in enumerate(target_range):
        vision_progress.progress((i + 1) / total_v_pgs, text=f"Processing Page {pg}")
        imgs = convert_from_bytes(get_pdf_bytes(), first_page=pg, last_page=pg)
        if not imgs:
            continue
        with tempfile.TemporaryDirectory() as td:
            img_name  = f"vis_{job['id']}_{pg}"
            img_path  = save_optimized_image(imgs[0], td, img_name, sub_folder=job['file'])
            if not img_path:
                continue
            safe_sub        = PDFUtils.get_safe_folder(job['file'])
            full_stage_path = f"{stage_path}/_temp_images/{safe_sub}"
            session.file.put(img_path, full_stage_path, auto_compress=False, overwrite=True)
            rel_img_path = f"_temp_images/{safe_sub}/{os.path.basename(img_path)}"

            prompt  = prompts.get_vision_extraction_prompt()
            res_txt, p_tok, c_tok = run_cortex(
                session, prompt, stage_path, rel_img_path, model=CORTEX_MODEL
            )
            if res_txt:
                c_ref   = _build_chunk_ref(raw_file, pg, job.get('link', ''))
                ins_sql = f"""
                INSERT INTO {full_table}
                    (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE, CHUNK_REF)
                SELECT ?, ?, C.VALUE::VARCHAR,
                       CONCAT('CHK_', UUID_STRING()), 'ENHANCED', ?
                FROM LATERAL FLATTEN(
                    INPUT => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(
                        ?, 'markdown', ?, ?
                    )
                ) C
                """
                session.sql(ins_sql, params=[raw_file, pg, c_ref, res_txt, chunk_sz, chunk_ov]).collect()

                # SELECT-back to obtain Snowflake-generated CHUNK_IDs (UUID_STRING() is server-side)
                sel_back = (
                    f"SELECT CHUNK_ID, CHUNK, CHUNK_TYPE, PAGE_NUMBER, RELATIVE_PATH, CHUNK_REF "
                    f"FROM {full_table} "
                    f"WHERE RELATIVE_PATH = ? AND PAGE_NUMBER = ? ORDER BY CHUNK_ID"
                )
                inserted_rows = session.sql(sel_back, params=[raw_file, pg]).collect()
                inserted_cnt  = len(inserted_rows)

                for r in inserted_rows:
                    rd = r.as_dict()
                    cache_entry = {
                        'job_id':        job['id'],
                        'CHUNK_ID':      get_val(rd, 'CHUNK_ID', ''),
                        'CHUNK':         get_val(rd, 'CHUNK', ''),
                        'CHUNK_TYPE':    get_val(rd, 'CHUNK_TYPE', 'ENHANCED'),
                        'PAGE_NUMBER':   get_val(rd, 'PAGE_NUMBER', 0),
                        'RELATIVE_PATH': get_val(rd, 'RELATIVE_PATH', ''),
                        'CHUNK_REF':     get_val(rd, 'CHUNK_REF', ''),
                    }
                    if len(st.session_state.chunk_cache) < 5000:
                        st.session_state.chunk_cache.append(cache_entry)
                    elif not job.get('cache_limit_logged'):
                        log_action("CACHE_LIMIT_REACHED", {"file": job['file']}, level="WARNING")
                        job['cache_limit_logged'] = True

                # Incremental metric mutations immediately after confirmed insert
                job['metrics']['vision_pages_list'].add(pg)
                job['metrics']['vision_input_tokens']  += p_tok
                job['metrics']['vision_output_tokens'] += c_tok
                job['metrics']['enhanced_cnt'] += inserted_cnt
                etype = "Vision Extraction"
                job['metrics']['types'][etype] = job['metrics']['types'].get(etype, 0) + inserted_cnt

    vision_progress.empty()
    job['metrics']['time_vision'] += (time.time() - t_vis_start)
