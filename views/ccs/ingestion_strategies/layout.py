# views/refinery/ingestion_strategies/layout.py
import time
import uuid
import json
import pandas as pd
import streamlit as st
from views.ccs.batch_exceptions import BatchCancelledError
from logger_config import log_action
from views.ccs.refinery_common import execute_sql_safe, _build_chunk_ref
from utils.core_utils import PDFUtils, clean_text_for_sql
from utils.constants import (
    SNOWFLAKE_MAX_STRING_BYTES, CHUNK_INSERT_MAX_CHARS,
    CHUNK_ID_PREFIX, CHUNK_CACHE_MAX_SIZE
)
from utils.metadata_handler import ChunkMetadataHandler

def _execute_layout_strategy(session, job, full_table, stage_path,
                              db, schema, table_name,
                              chunk_sz, chunk_ov, json_opts, safe_file,
                              job_pages_count, get_pdf_bytes):
    """Executes layout extraction with batching, placeholder generation, and null-safety checks."""
    t_layout_start = time.time()
    job.setdefault('skipped_page_ranges', [])

    # Range mapping support: pre-compute RangeMapping list if present
    range_mappings = None
    if job.get('surgical_range_mappings'):
        from utils.page_mapping import RangeMapping, RangeMappingEngine
        range_mappings = [
            RangeMapping(
                source_start=int(rm['source_start']),
                source_end=int(rm['source_end']),
                replacement_start=int(rm['replacement_start']),
                replacement_end=int(rm['replacement_end'])
            )
            for rm in job['surgical_range_mappings']
        ]

    target_file = clean_text_for_sql(job['file'])
    target_page = int(job.get('surgical_target_page') or 0)
    src_sql = f"SELECT SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(TO_FILE('{stage_path}', '{safe_file}'), PARSE_JSON('{json_opts}')) AS J"
    try:
        raw_res = session.sql(src_sql).collect()[0]["J"]
        # Defensive Check: Handle Snowflake returning NULL on parsing failures
        if raw_res is None:
            raise ValueError(f"AI_PARSE_DOCUMENT returned NULL. Options: {json_opts}")
            
        doc_json = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
        if doc_json is None:
            raise ValueError("Parsed JSON payload is NULL")
            
        pages_data = doc_json.get("pages") or []
    except Exception as e:
        raise Exception(f"AI_PARSE_DOCUMENT Failed: {e}")

    page_records = []
    link_val = job.get('link', '')
    pdf_bytes = get_pdf_bytes()
    
    for pg in pages_data:
        pg_num = int(pg.get("index", 0)) + 1
        content = pg.get("content", "")
        from utils.core_utils import sanitize_nbsp
        content = sanitize_nbsp(content)
        
        # Enforce Snowflake 16MB string limit
        encoded = content.encode('utf-8')
        if len(encoded) > SNOWFLAKE_MAX_STRING_BYTES:
            content = encoded[:SNOWFLAKE_MAX_STRING_BYTES].decode('utf-8', 'ignore')
            log_action("PAGE_TEXT_TRUNCATED", {"page": pg_num})
        
        # Range-mapped bounds filter: skip PDF pages outside all replacement ranges
        if range_mappings:
            from utils.page_mapping import RangeMappingEngine
            db_pg_num = RangeMappingEngine.target_page_for(range_mappings, pg_num)
            if db_pg_num is None:
                # PDF page is outside all replacement ranges — skip it
                continue
        else:
            db_pg_num = pg_num

        links = PDFUtils.extract_links_from_bytes(pdf_bytes, pg_num)
        link_block = PDFUtils.format_link_block(links)
        chunk_ref = _build_chunk_ref(target_file, db_pg_num, link_val)
        
        page_records.append({
            'RELATIVE_PATH': target_file, 'PAGE_NUMBER': db_pg_num, 'PAGE_TEXT': content,
            'LINK_BLOCK': link_block, 'CHUNK_REF': chunk_ref, 'CHUNK_TYPE': 'STANDARD'
        })

    # Page range check and placeholder insertions
    # FIX: For range-mapped jobs, expected_pages = union of replacement ranges
    if range_mappings:
        expected_pages = set()
        for rm in range_mappings:
            expected_pages.update(range(rm.replacement_start, rm.replacement_end + 1))
    else:
        s_pg, e_pg = job.get('range', (1, job_pages_count)) if job.get('scope') == "Page Range" else (1, job_pages_count)
        expected_pages = set(range(s_pg, e_pg + 1))
    returned_source_pages = {int(pg.get("index", 0)) + 1 for pg in pages_data}
    missing = sorted(expected_pages - returned_source_pages)
    
    for mp in missing:
        # PAGE_NUMBER directly reflects the PDF page number. No remapping.
        db_mp = mp
        page_records.append({
            'RELATIVE_PATH': target_file, 'PAGE_NUMBER': db_mp, 'PAGE_TEXT': f"[Page {mp} — extraction fallback]",
            'LINK_BLOCK': '', 'CHUNK_REF': _build_chunk_ref(target_file, db_mp, link_val), 'CHUNK_TYPE': 'PLACEHOLDER'
        })
    page_records.sort(key=lambda x: x['PAGE_NUMBER'])

    temp_table_name = f'TEMP_CHUNKS_{uuid.uuid4().hex}'
    temp_table_full = f'"{db}"."{schema}"."{temp_table_name}"'
    
    session.sql(f"""
        CREATE OR REPLACE TABLE {temp_table_full} (
            RELATIVE_PATH VARCHAR, PAGE_NUMBER NUMBER, PAGE_TEXT VARCHAR,
            LINK_BLOCK VARCHAR, CHUNK_REF VARCHAR, CHUNK_TYPE VARCHAR
        )
    """).collect()

    batches = [page_records[i:i+100] for i in range(0, len(page_records), 100)]
    job['metrics']['total_batches'] = len(batches)
    job['metrics']['layout_pages'] = 0
    job['metrics']['standard_cnt'] = job['metrics'].get('standard_cnt', 0)

    try:
        for batch in batches:
            # Cancel checkpoint: checked between page-batch commits
            if st.session_state.get('cancel_batch', False):
                raise BatchCancelledError(f"Cancelled before batch {batch[0]['PAGE_NUMBER']}-{batch[-1]['PAGE_NUMBER']}")
            df_batch = pd.DataFrame(batch)
            batch_start, batch_end = batch[0]['PAGE_NUMBER'], batch[-1]['PAGE_NUMBER']
            
            success = False
            for attempt in range(3):
                try:
                    session.sql(f"TRUNCATE TABLE {temp_table_full}").collect()
                    session.write_pandas(df_batch, table_name=temp_table_name, database=db, schema=schema, overwrite=False, auto_create_table=False)
                    success = True
                    break
                except Exception as e:
                    time.sleep(1)
            
            if not success:
                job['skipped_page_ranges'].append({'start': batch_start, 'end': batch_end})
                continue
            
            session.sql("BEGIN").collect()
            try:
                pre_res = session.sql(f"SELECT CHUNK_ID FROM {full_table} WHERE RELATIVE_PATH = '{target_file}'").collect()
                pre_existing_ids = {r[0] for r in pre_res}

                # FIX: Added surgical_range_mappings branch BEFORE the legacy
                # surgical_page_mappings check.
                if job['mode'] == 'SURGICAL' and job.get('surgical_range_mappings'):
                    from utils.page_mapping import RangeMappingEngine
                    per_page_mappings = RangeMappingEngine.to_per_page_mappings(range_mappings)
                    source_range = job.get('range', (1, job_pages_count))
                    replacement_file = job.get('surgical_replacement_file', job['file'])
                    # Enrich per-page mappings with original_pdf_page so QA Studio
                    # can render the correct PDF page even after PAGE_NUMBER shifts.
                    for pm in per_page_mappings:
                        pm['original_pdf_page'] = pm['source']
                    chunk_metadata = ChunkMetadataHandler.build_surgical_select_metadata(
                        original_file=job['file'], source_range=source_range,
                        replacement_file=replacement_file, page_mappings=per_page_mappings
                    )
                elif job['mode'] == 'SURGICAL' and 'surgical_page_mappings' in job:
                    source_range = job.get('range', (1, job_pages_count))
                    replacement_file = job.get('surgical_replacement_file', job['file'])
                    chunk_metadata = ChunkMetadataHandler.build_surgical_select_metadata(
                        original_file=job['file'], source_range=source_range, replacement_file=replacement_file, page_mappings=job['surgical_page_mappings']
                    )
                else:
                    metadata_dict = ChunkMetadataHandler.create_initial_metadata(write_mode=job['mode'], chunk_type="standard", parser_config={"layout": True, "vision": job.get('vision', False)})
                    chunk_metadata = ChunkMetadataHandler.serialize_metadata(metadata_dict)

                insert_sql = f"""
                INSERT INTO {full_table} (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE, CHUNK_REF, LINK_BLOCK, CHUNK_METADATA)
                SELECT
                    t.RELATIVE_PATH, t.PAGE_NUMBER,
                    CASE WHEN NVL(t.LINK_BLOCK, '') = '' THEN c.value::VARCHAR ELSE SUBSTR(c.value::VARCHAR || t.LINK_BLOCK, 1, {CHUNK_INSERT_MAX_CHARS}) END,
                    CONCAT('{CHUNK_ID_PREFIX}', UUID_STRING()), t.CHUNK_TYPE, t.CHUNK_REF, t.LINK_BLOCK,
                    PARSE_JSON(?)
                FROM {temp_table_full} t,
                LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(t.PAGE_TEXT, 'markdown', {chunk_sz}, {chunk_ov})) c
                """
                session.sql(insert_sql, params=[chunk_metadata]).collect()

                post_res = session.sql(f"SELECT CHUNK_ID, PAGE_NUMBER, CHUNK, CHUNK_TYPE, RELATIVE_PATH, CHUNK_REF, LINK_BLOCK FROM {full_table} WHERE RELATIVE_PATH = '{target_file}'").collect()
                new_rows = [r.as_dict() for r in post_res if r["CHUNK_ID"] not in pre_existing_ids]
                session.sql("COMMIT").collect()

                for rd in new_rows:
                    if len(st.session_state.chunk_cache) < CHUNK_CACHE_MAX_SIZE:
                        st.session_state.chunk_cache.append({
                            'job_id': job['id'], 'CHUNK_ID': rd.get('CHUNK_ID', ''), 'CHUNK': rd.get('CHUNK', ''),
                            'CHUNK_TYPE': rd.get('CHUNK_TYPE', 'STANDARD'), 'PAGE_NUMBER': rd.get('PAGE_NUMBER', 0),
                            'RELATIVE_PATH': rd.get('RELATIVE_PATH', ''), 'CHUNK_REF': rd.get('CHUNK_REF', ''), 'LINK_BLOCK': rd.get('LINK_BLOCK', '')
                        })
                
                job['metrics']['layout_pages'] += len(batch)
                job['metrics']['standard_cnt'] += len(new_rows)
                # Track which pages were processed by layout
                for rec in batch:
                    job['metrics']['layout_pages_list'].add(rec['PAGE_NUMBER'])
            except Exception as e:
                session.sql("ROLLBACK").collect()
                job['skipped_page_ranges'].append({'start': batch_start, 'end': batch_end, 'error': str(e)})
    finally:
        session.sql(f"DROP TABLE IF EXISTS {temp_table_full}").collect()

    job['metrics']['time_layout'] += (time.time() - t_layout_start)
