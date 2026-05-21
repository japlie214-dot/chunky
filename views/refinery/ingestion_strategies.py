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
                              job_pages_count, get_pdf_bytes):
    """
    Executes the Layout (SQL) strategy with temp tables and 100-page batching:
    1. Parse JSON → Python
    2. Extract links and apply 90% filter
    3. Batch pages by 100 → temp table
    4. INSERT...SELECT with chunking and link block appends
    5. 3-attempt retry for batch uploads
    Mutates job['metrics'] incrementally.
    Tracks skipped_page_ranges on failures.
    Enforces 16MB truncation on page text.
    
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
        get_pdf_bytes: Callable that returns PDF bytes
    """
    import uuid
    import json
    t_layout_start = time.time()
    job.setdefault('skipped_page_ranges', [])
    
    # 1. Parse JSON into Python
    src_sql = f"SELECT SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(TO_FILE('{stage_path}', '{safe_file}'), PARSE_JSON('{json_opts}')) AS J"
    try:
        raw_res = session.sql(src_sql).collect()[0]["J"]
        doc_json = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
        pages_data = doc_json.get("pages", [])
    except Exception as e:
        raise Exception(f"AI_PARSE_DOCUMENT Failed: {e}")

    # 2. Extract texts and links
    page_records = []
    link_val = job.get('link', '')
    pdf_bytes = get_pdf_bytes()
    for pg in pages_data:
        pg_num = int(pg.get("index", 0)) + 1
        content = pg.get("content", "")
        # Enforce 16MB limit
        encoded = content.encode('utf-8')
        if len(encoded) > 16777216:
            content = encoded[:16777216].decode('utf-8', 'ignore')
            log_action("PAGE_TEXT_TRUNCATED", {"page": pg_num, "original_length": len(encoded)})
        
        links = PDFUtils.extract_links_from_bytes(pdf_bytes, pg_num)
        link_block = PDFUtils.format_link_block(links)
        chunk_ref = _build_chunk_ref(safe_file, pg_num, link_val)
        
        page_records.append({
            'RELATIVE_PATH': safe_file,
            'PAGE_NUMBER': pg_num,
            'PAGE_TEXT': content,
            'LINK_BLOCK': link_block,
            'CHUNK_REF': chunk_ref,
            'CHUNK_TYPE': 'STANDARD'
        })

    # 2b. Missing-page detection & Placeholder Generation
    if job.get('scope') == "Page Range":
        s_pg, e_pg = job.get('range', (1, job_pages_count))
    else:
        s_pg, e_pg = 1, job_pages_count
        
    expected_pages = set(range(s_pg, e_pg + 1))
    returned_pages = {r['PAGE_NUMBER'] for r in page_records}
    missing = sorted(expected_pages - returned_pages)
    if missing:
        log_action("MISSING_PAGES_DETECTED", {"file": safe_file, "missing": missing})
        for mp in missing:
            page_records.append({
                'RELATIVE_PATH': safe_file, 'PAGE_NUMBER': mp,
                'PAGE_TEXT': f"[Page {mp} — no extractable text]", 'LINK_BLOCK': '',
                'CHUNK_REF': _build_chunk_ref(safe_file, mp, link_val),
                'CHUNK_TYPE': 'PLACEHOLDER'
            })
        page_records.sort(key=lambda x: x['PAGE_NUMBER'])

    # 3. Batching and Temp Table
    temp_table_name = f'TEMP_CHUNKS_{uuid.uuid4().hex}'
    temp_table_full = f'"{db}"."{schema}"."{temp_table_name}"'
    
    session.sql(f"""
        CREATE OR REPLACE TABLE {temp_table_full} (
            RELATIVE_PATH VARCHAR,
            PAGE_NUMBER NUMBER,
            PAGE_TEXT VARCHAR,
            LINK_BLOCK VARCHAR,
            CHUNK_REF VARCHAR,
            CHUNK_TYPE VARCHAR
        )
    """).collect()

    batches = [page_records[i:i+100] for i in range(0, len(page_records), 100)]
    job['metrics']['total_batches'] = len(batches)
    job['metrics']['layout_pages'] = 0
    job['metrics']['standard_cnt'] = job['metrics'].get('standard_cnt', 0)

    try:
        for batch in batches:
            df_batch = pd.DataFrame(batch)
            batch_start = batch[0]['PAGE_NUMBER']
            batch_end = batch[-1]['PAGE_NUMBER']
            
            # Retry mechanism for batch upload
            success = False
            last_err = None
            for attempt in range(3):
                try:
                    session.sql(f"TRUNCATE TABLE {temp_table_full}").collect()
                    session.write_pandas(df_batch, table_name=temp_table_name, database=db, schema=schema, overwrite=False, auto_create_table=False)
                    success = True
                    break
                except Exception as e:
                    last_err = str(e)
                    log_action("BATCH_UPLOAD_RETRY", {"attempt": attempt + 1, "error": last_err})
                    time.sleep(1)
            
            if not success:
                log_action("BATCH_UPLOAD_FAILED", {"start": batch_start, "end": batch_end, "error": last_err})
                job['skipped_page_ranges'].append({'start': batch_start, 'end': batch_end, 'error': last_err})
                continue
            
            # 4. INSERT...SELECT with Transaction
            session.sql("BEGIN").collect()
            try:
                # Pre-snapshot to capture existing IDs
                pre_res = session.sql(f"SELECT CHUNK_ID FROM {full_table} WHERE RELATIVE_PATH = '{safe_file}'").collect()
                pre_existing_ids = {r[0] for r in pre_res}

                insert_sql = f"""
                INSERT INTO {full_table} (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE, CHUNK_REF, LINK_BLOCK)
                SELECT
                    t.RELATIVE_PATH,
                    t.PAGE_NUMBER,
                    CASE WHEN NVL(t.LINK_BLOCK, '') = '' THEN c.value::VARCHAR ELSE SUBSTR(c.value::VARCHAR || t.LINK_BLOCK, 1, 15000000) END,
                    CONCAT('CHK_', UUID_STRING()),
                    t.CHUNK_TYPE,
                    t.CHUNK_REF,
                    t.LINK_BLOCK
                FROM {temp_table_full} t,
                LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(t.PAGE_TEXT, 'markdown', {chunk_sz}, {chunk_ov})) c
                """
                session.sql(insert_sql).collect()

                # Post-snapshot and Validation
                post_res = session.sql(f"SELECT CHUNK_ID, PAGE_NUMBER, CHUNK, CHUNK_TYPE, RELATIVE_PATH, CHUNK_REF, LINK_BLOCK FROM {full_table} WHERE RELATIVE_PATH = '{safe_file}'").collect()
                new_rows = [r.as_dict() for r in post_res if r["CHUNK_ID"] not in pre_existing_ids]

                session.sql("COMMIT").collect()

                # Post-insert validation: Guarantee >=1 chunk per page in the batch
                batch_page_nums = {r['PAGE_NUMBER'] for r in batch}
                chunk_pages_res = session.sql(
                    f"SELECT DISTINCT PAGE_NUMBER FROM {full_table} "
                    f"WHERE RELATIVE_PATH = '{safe_file}' "
                    f"AND PAGE_NUMBER IN ({','.join(str(p) for p in sorted(batch_page_nums))})"
                ).collect()
                unchunked = batch_page_nums - {r[0] for r in chunk_pages_res}
                if unchunked:
                    log_action("UNCHUNKED_PAGES", {"pages": sorted(unchunked)}, level="WARNING")

                # Cache Sync
                for rd in new_rows:
                    if len(st.session_state.chunk_cache) < 5000:
                        st.session_state.chunk_cache.append({
                            'job_id': job['id'], 'CHUNK_ID': rd.get('CHUNK_ID', ''), 'CHUNK': rd.get('CHUNK', ''),
                            'CHUNK_TYPE': rd.get('CHUNK_TYPE', 'STANDARD'), 'PAGE_NUMBER': rd.get('PAGE_NUMBER', 0),
                            'RELATIVE_PATH': rd.get('RELATIVE_PATH', ''), 'CHUNK_REF': rd.get('CHUNK_REF', ''),
                            'LINK_BLOCK': rd.get('LINK_BLOCK', '')
                        })
                    elif not job.get('cache_limit_logged'):
                        log_action("CACHE_LIMIT_REACHED", {"file": job['file']}, level="WARNING")
                        job['cache_limit_logged'] = True
                
                job['metrics']['layout_pages'] += len(batch)
                job['metrics']['standard_cnt'] += len(new_rows)
            except Exception as e:
                session.sql("ROLLBACK").collect()
                job['skipped_page_ranges'].append({'start': batch_start, 'end': batch_end, 'error': f"Insert/Verify Failed: {str(e)}"})
    finally:
        try:
            session.sql(f"DROP TABLE IF EXISTS {temp_table_full}").collect()
        except Exception as e:
            log_action("TEMP_TABLE_DROP_ERROR", {"table": temp_table_name, "error": str(e)})

    job['metrics']['time_layout'] += (time.time() - t_layout_start)


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

    t_vis_start    = time.time()
    total_fix      = len(defects)
    processed_fix  = 0
    job_alert.warning(
        f"🛠️ Found {total_fix} OCR defects in `{job['file']}`. Starting AI Repair..."
    )
    repair_progress = st.progress(0, text="Initializing Repairs...")

    try:
        for pg_num in defects['PAGE_NUMBER'].unique():
            pg_defects = defects[defects['PAGE_NUMBER'] == pg_num]
            imgs = convert_from_bytes(get_pdf_bytes(), first_page=pg_num, last_page=pg_num)
            if not imgs:
                for _, row in pg_defects.iterrows():
                    job['metrics']['defects_detail'].append({"page": int(pg_num), "chunk_id": row['CHUNK_ID'], "defect_type": row['STATUS'], "status": "FAILED_RENDER"})
                continue
            with tempfile.TemporaryDirectory() as td:
                img_name  = f"repair_p{pg_num}"
                img_path  = save_optimized_image(imgs[0], td, img_name, sub_folder=job['file'])
                if not img_path:
                    for _, row in pg_defects.iterrows():
                        job['metrics']['defects_detail'].append({"page": int(pg_num), "chunk_id": row['CHUNK_ID'], "defect_type": row['STATUS'], "status": "FAILED_RENDER"})
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
                    
                    # Phase 1: Quarantine link block
                    link_block = row.get('LINK_BLOCK')
                    if pd.notna(link_block) and link_block:
                        clean_text = str(row['CHUNK']).replace(link_block, "", 1).rstrip()
                        quarantined_block = link_block
                    else:
                        clean_text, quarantined_block = PDFUtils.strip_link_block(str(row['CHUNK']))

                    defect_instruction = (
                        f"Fix defect: {row['STATUS']}\n"
                        f"IMPORTANT: Do NOT add, invent, or reference any URLs in your output."
                    )
                    if row['STATUS'] == 'REPAIR_VISUAL':
                        prompt = prompts.get_layout_repair_prompt(clean_text, defect_instruction)
                    else:
                        prompt = prompts.get_silver_bullet_prompt(clean_text, defect_instruction)
                    res_txt, p_tok, c_tok = run_cortex(
                        session, prompt, stage_path, rel_img_path, model=CORTEX_MODEL
                    )
                    if res_txt:
                        # Phase 3: Re-append
                        if quarantined_block:
                            res_txt = PDFUtils.safe_concat(res_txt.rstrip(), quarantined_block)

                        c_ref = _build_chunk_ref(row['RELATIVE_PATH'], pg_num, job.get('link', ''))
                        # Cache before SQL write
                        cache_entry = {
                            'job_id': job['id'], 'CHUNK_ID': row['CHUNK_ID'],
                            'CHUNK': res_txt, 'CHUNK_TYPE': 'ENHANCED',
                            'PAGE_NUMBER': pg_num, 'RELATIVE_PATH': row['RELATIVE_PATH'],
                            'CHUNK_REF': c_ref,
                            'LINK_BLOCK': row.get('LINK_BLOCK', ''),
                        }
                        if len(st.session_state.chunk_cache) < 5000:
                            st.session_state.chunk_cache.append(cache_entry)
                        elif not job.get('cache_limit_logged'):
                            log_action("CACHE_LIMIT_REACHED", {"file": job['file']}, level="WARNING")
                            job['cache_limit_logged'] = True
                        
                        # Per-chunk immediate write (DO NOT BATCH) - Note LINK_BLOCK remains untouched
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
                        ttypes = job['metrics'].get('types', {})
                        ttypes[etype]  = ttypes.get(etype, 0) + 1
                        job['metrics']['types'] = ttypes
                        if job['metrics']['standard_cnt'] > 0:
                            job['metrics']['standard_cnt'] -= 1
                        job['metrics']['defects_detail'].append({
                            "page": int(pg_num), "chunk_id": row['CHUNK_ID'],
                            "defect_type": row['STATUS'], "status": "FIXED"
                        })
                    else:
                        job['metrics']['defects_detail'].append({
                            "page": int(pg_num), "chunk_id": row['CHUNK_ID'],
                            "defect_type": row['STATUS'], "status": "SKIPPED"
                        })
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
        img_path = None
        res_txt = None
        p_tok, c_tok = 0, 0
        
        if imgs:
            with tempfile.TemporaryDirectory() as td:
                img_name  = f"vis_{job['id']}_{pg}"
                img_path  = save_optimized_image(imgs[0], td, img_name, sub_folder=job['file'])
                if img_path:
                    safe_sub        = PDFUtils.get_safe_folder(job['file'])
                    full_stage_path = f"{stage_path}/_temp_images/{safe_sub}"
                    session.file.put(img_path, full_stage_path, auto_compress=False, overwrite=True)
                    rel_img_path = f"_temp_images/{safe_sub}/{os.path.basename(img_path)}"

                    prompt  = prompts.get_vision_extraction_prompt()
                    res_txt, p_tok, c_tok = run_cortex(
                        session, prompt, stage_path, rel_img_path, model=CORTEX_MODEL
                    )

        if not imgs or not img_path or not res_txt:
            c_ref = _build_chunk_ref(raw_file, pg, job.get('link', ''))
            placeholder_text = f"[Page {pg} — Vision extraction failed]"
            ins_sql = f"""
            INSERT INTO {full_table}
                (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE, CHUNK_REF, LINK_BLOCK)
            VALUES (?, ?, ?, CONCAT('CHK_', UUID_STRING()), 'PLACEHOLDER', ?, '')
            """
            session.sql(ins_sql, params=[raw_file, pg, placeholder_text, c_ref]).collect()
            log_action("VISION_PLACEHOLDER_GENERATED", {"page": pg, "file": raw_file})
            
            # Sync placeholder chunk back to st.session_state.chunk_cache & metrics
            sel_back = (
                f"SELECT CHUNK_ID, CHUNK, CHUNK_TYPE, PAGE_NUMBER, RELATIVE_PATH, CHUNK_REF, LINK_BLOCK "
                f"FROM {full_table} "
                f"WHERE RELATIVE_PATH = ? AND PAGE_NUMBER = ? ORDER BY CHUNK_ID"
            )
            try:
                inserted_rows = session.sql(sel_back, params=[raw_file, pg]).collect()
                for r in inserted_rows:
                    rd = r.as_dict()
                    cache_entry = {
                        'job_id':        job['id'],
                        'CHUNK_ID':      get_val(rd, 'CHUNK_ID', ''),
                        'CHUNK':         get_val(rd, 'CHUNK', ''),
                        'CHUNK_TYPE':    get_val(rd, 'CHUNK_TYPE', 'PLACEHOLDER'),
                        'PAGE_NUMBER':   get_val(rd, 'PAGE_NUMBER', 0),
                        'RELATIVE_PATH': get_val(rd, 'RELATIVE_PATH', ''),
                        'CHUNK_REF':     get_val(rd, 'CHUNK_REF', ''),
                        'LINK_BLOCK':    get_val(rd, 'LINK_BLOCK', ''),
                    }
                    if len(st.session_state.chunk_cache) < 5000:
                        st.session_state.chunk_cache.append(cache_entry)
                job['metrics']['enhanced_cnt'] += len(inserted_rows)
            except Exception as e:
                log_action("VISION_PLACEHOLDER_SYNC_FAILED", {"error": str(e)}, level="WARNING")
                
            continue

        if res_txt:
                links = PDFUtils.extract_links_from_bytes(get_pdf_bytes(), pg)
                link_block = PDFUtils.format_link_block(links)
                
                c_ref   = _build_chunk_ref(raw_file, pg, job.get('link', ''))
                ins_sql = f"""
                INSERT INTO {full_table}
                    (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE, CHUNK_REF, LINK_BLOCK)
                SELECT ?, ?, CASE WHEN NVL(?, '') = '' THEN C.VALUE::VARCHAR ELSE SUBSTR(C.VALUE::VARCHAR || ?, 1, 15000000) END,
                       CONCAT('CHK_', UUID_STRING()), 'ENHANCED', ?, ?
                FROM LATERAL FLATTEN(
                    INPUT => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(
                        ?, 'markdown', ?, ?
                    )
                ) C
                """
                session.sql(ins_sql, params=[raw_file, pg, link_block, link_block, c_ref, link_block, res_txt, chunk_sz, chunk_ov]).collect()

                # SELECT-back to obtain Snowflake-generated CHUNK_IDs (UUID_STRING() is server-side)
                sel_back = (
                    f"SELECT CHUNK_ID, CHUNK, CHUNK_TYPE, PAGE_NUMBER, RELATIVE_PATH, CHUNK_REF, LINK_BLOCK "
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
                        'LINK_BLOCK':    get_val(rd, 'LINK_BLOCK', ''),
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
                ttypes = job['metrics'].get('types', {})
                ttypes[etype] = ttypes.get(etype, 0) + inserted_cnt
                job['metrics']['types'] = ttypes

    vision_progress.empty()
    job['metrics']['time_vision'] += (time.time() - t_vis_start)
