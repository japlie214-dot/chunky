"""
procedure/utils/chunky_chunks_handler.py
Ingestion Engine. Commands: ingest, list_chunks, update_chunk,
delete_chunks, revert.

Source (logical): CCS wizard Pages 2-3 + Doc Refinery batch_processor.
This file is the headless, Streamlit-free version of that logic.

Headless changes vs. the original Streamlit-side code:
  * No `streamlit` imports, no `st.session_state`, no UI fragments.
  * All configuration comes from `instruction` (the caller's JSON).
  * Every SQL operation runs through `QueryLog.execute` so we can
    return query_ids for revert.
  * Warnings are returned in the response AFTER execution (the original
    Streamlit app showed them before; headless callers can't do that).
  * New `revert` command rewinds the table via TIME TRAVEL using
    either `timestamp_before` or `query_ids` from a prior call.
"""
from __future__ import annotations
import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

# ---- Procedure-internal shared modules (bundled in utils_bundle.zip) ----
from .constants import (
    CHUNK_ID_PREFIX,
    CHUNK_INSERT_MAX_CHARS,
    SNOWFLAKE_MAX_STRING_BYTES,
    DEFAULT_CORTEX_MODEL,
    WARNING_INGEST_OVERWRITE,
    WARNING_INGEST_SURGICAL,
    WARNING_INGEST_APPEND,
)
from .query_log import QueryLog
from .page_mapping import RangeMapping, RangeMappingEngine
from .metadata_handler import ChunkMetadataHandler
from .revert import revert_table, revert_rows


# ---------------------------------------------------------------------------
# Poppler bootstrap (bundled via poppler_bundle.zip)
# ---------------------------------------------------------------------------
_POPPLER_BASE = os.path.join(os.path.dirname(__file__), 'poppler_bundle', 'poppler')
_POPPLER_BIN = os.path.join(_POPPLER_BASE, 'bin')
_POPPLER_LIB = os.path.join(_POPPLER_BASE, 'lib')
if os.path.isdir(_POPPLER_LIB):
    _ld = os.environ.get('LD_LIBRARY_PATH', '')
    os.environ['LD_LIBRARY_PATH'] = _POPPLER_LIB + (':' + _ld if _ld else '')
if os.path.isdir(_POPPLER_BIN):
    os.environ['PATH'] = _POPPLER_BIN + ':' + os.environ.get('PATH', '')


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def clean_text_for_sql(text: str) -> str:
    if not text:
        return ""
    safe = text.replace("'", "''")
    return ''.join(ch for ch in safe if ch.isprintable() or ch in ("\n", "\r", "\t"))


def sanitize_nbsp(text: str) -> str:
    if not text:
        return text
    return re.sub(r'&nbsp;|&#160;|&#x[aA]0;', ' ', text)


def build_chunk_ref(rel_path: str, page_num: int, link: str = "") -> str:
    base = f"Doc Source: {rel_path} | Page Num: {page_num}"
    if link:
        import urllib.parse
        safe_link = urllib.parse.quote(link, safe=":/?#&=@")
        return f"[Digital Copy]({safe_link}) | {base}"
    return base


def get_pdf_page_count(pdf_bytes: bytes) -> int:
    """Get PDF page count using pypdf (pure Python)."""
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return len(reader.pages)
    except Exception:
        return 1


def extract_links_from_bytes(pdf_bytes: bytes, page_number: int) -> List[str]:
    """Extract URLs from a PDF page using pypdf."""
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if page_number < 1 or page_number > len(reader.pages):
            return []
        page = reader.pages[page_number - 1]
        urls: List[str] = []
        if "/Annots" in page:
            for annot_ref in page["/Annots"]:
                annot = annot_ref.get_object()
                if annot.get("/Subtype") == "/Link" and "/A" in annot:
                    action = annot["/A"].get_object()
                    if action.get("/S") == "/URI" and "/URI" in action:
                        url = action["/URI"]
                        if url not in urls:
                            urls.append(url)
        return urls
    except Exception:
        return []


def format_link_block(urls: List[str]) -> str:
    if not urls:
        return ""
    lines = "\n".join(f"  - {u}" for u in urls)
    return f"\n\n[External links:\n{lines}\n]"


def save_optimized_image(image, output_dir: str, base_filename: str,
                         sub_folder: Optional[str] = None) -> Optional[str]:
    """Save image under 3.5MB for Snowflake Cortex."""
    MAX_IMAGE_MB = 3.5
    try:
        from PIL import Image as PILImage
    except ImportError:
        return None

    if sub_folder:
        safe_sub = "".join(c for c in sub_folder if c.isalnum() or c in "._-")
        final_dir = os.path.join(output_dir, safe_sub)
    else:
        final_dir = output_dir
    os.makedirs(final_dir, exist_ok=True)

    png_path = os.path.join(final_dir, f"{base_filename}.png")
    jpg_path = os.path.join(final_dir, f"{base_filename}.jpg")

    try:
        if hasattr(image, 'width') and image.width > 1600:
            ratio = 1600 / image.width
            new_height = int(image.height * ratio)
            image = image.resize((1600, new_height), PILImage.Resampling.LANCZOS)

        image.save(png_path, format="PNG", optimize=True)
        if (os.path.getsize(png_path) / (1024 * 1024)) < MAX_IMAGE_MB:
            return png_path

        try:
            os.remove(png_path)
        except Exception:
            pass

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
    except Exception:
        return None


def run_cortex(session, log: QueryLog, prompt: str, stage_root: str,
               image_path_relative: str, model: str = DEFAULT_CORTEX_MODEL):
    """Execute AI_COMPLETE with an image and capture the query ID."""
    root = stage_root if stage_root.startswith('@') else f"@{stage_root}"
    safe_prompt = prompt.replace("'", "''")
    safe_root = root.replace("'", "''")
    safe_path = image_path_relative.replace("'", "''")

    sql = f"""
        SELECT SNOWFLAKE.CORTEX.AI_COMPLETE(
            '{model}',
            '{safe_prompt}',
            TO_FILE('{safe_root}', '{safe_path}')
        ) AS RES
    """
    try:
        res = log.execute(sql)
        if not res or not res[0]["RES"]:
            return "", 0, 0
        text = res[0]["RES"].strip()
        p_tokens = (len(prompt) // 4) + 1000
        c_tokens = len(text) // 4
        return text, p_tokens, c_tokens
    except Exception:
        return "", 0, 0


def get_silver_bullet_prompt(input_text: str, context_instruction: Optional[str] = None) -> str:
    context_block = (
        f"<priority_instruction>\n{context_instruction}\n</priority_instruction>"
        if context_instruction and context_instruction.strip()
        else "<priority_instruction>\nStandard RAG Processing.\n</priority_instruction>"
    )
    return f"""You are a Document Reconstruction Specialist. Convert the page image into lossless, structured Markdown.

{context_block}

## CORE RULES
1. Reproduce, don't summarize. Every word, number, symbol appears in output.
2. Mark uncertainty: [unclear: best guess] or [?].
3. Preserve spatial relationships.
4. Image is ground truth.

## TABLES
- Merged cells: REPEAT value in every row it spans.
- Multi-line cells: use <br>.
- Empty cells: | |

## OUTPUT
Produce Markdown truest to the image. No commentary.

INPUT TEXT:
\"\"\"
{input_text}
\"\"\"
"""


def _qualify(db: str, schema: str, table: str) -> str:
    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table.replace('"', '""')
    return f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'


# ---------------------------------------------------------------------------
# Command: ingest
# ---------------------------------------------------------------------------
def cmd_ingest(session, inst: Dict[str, Any]) -> Dict:
    """Full ingestion pipeline: init -> surgical -> layout -> vision -> grant."""
    t_start = time.time()
    log = QueryLog(session)

    file = inst["file"]
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    stage_path = inst["stage_path"]
    mode = inst.get("mode", "APPEND").upper()
    scope = inst.get("scope", "Full Doc")
    chunk_sz = int(inst.get("chunk_size", 8000))
    overlap = int(inst.get("overlap", 20))
    use_layout = bool(inst.get("layout", True))
    use_vision = bool(inst.get("vision", False))
    link = inst.get("link", "")
    grant_roles = inst.get("grant_roles", []) or []
    surgical_mappings = inst.get("surgical_range_mappings", []) or []
    page_range = inst.get("range", [1, 1])
    cortex_model = inst.get("cortex_model", DEFAULT_CORTEX_MODEL)

    full_table = _qualify(db, schema, table)
    safe_file = clean_text_for_sql(file)

    metrics = {
        "start": t_start, "end": None, "duration": 0,
        "layout_pages": 0, "vision_pages": 0,
        "standard_cnt": 0, "enhanced_cnt": 0,
        "placeholder_cnt": 0, "total_pages": 0,
    }

    # 1. Init table
    init_sql = f"""
        CREATE TABLE IF NOT EXISTS {full_table} (
            RELATIVE_PATH VARCHAR, PAGE_NUMBER NUMBER, CHUNK VARCHAR,
            CHUNK_ID VARCHAR, CHUNK_TYPE VARCHAR DEFAULT 'STANDARD',
            CHUNK_REF VARCHAR, LINK_BLOCK VARCHAR, CHUNK_METADATA VARIANT
        ) CHANGE_TRACKING = TRUE
    """
    try:
        log.execute(init_sql)
    except Exception as e:
        return {
            "success": False, "command": "ingest",
            "error": f"Table init failed: {e}", "data": None,
            **log.to_dict(),
        }

    if mode == "OVERWRITE":
        log.execute(
            f"CREATE OR REPLACE TABLE {full_table} ("
            "RELATIVE_PATH VARCHAR, PAGE_NUMBER NUMBER, CHUNK VARCHAR, "
            "CHUNK_ID VARCHAR, CHUNK_TYPE VARCHAR DEFAULT 'STANDARD', "
            "CHUNK_REF VARCHAR, LINK_BLOCK VARCHAR, CHUNK_METADATA VARIANT"
            ") CHANGE_TRACKING = TRUE COPY GRANTS"
        )

    # 2. Surgical delete (only in SURGICAL mode with mappings)
    if mode == "SURGICAL" and surgical_mappings:
        sorted_rms = sorted(
            surgical_mappings,
            key=lambda m: int(m["source_end"]), reverse=True,
        )
        log.execute("BEGIN")
        try:
            for rm in sorted_rms:
                del_sql = (
                    f"DELETE FROM {full_table} "
                    f"WHERE RELATIVE_PATH = '{safe_file}' "
                    f"AND PAGE_NUMBER BETWEEN {int(rm['source_start'])} "
                    f"AND {int(rm['source_end'])}"
                )
                log.execute(del_sql)
            log.execute("COMMIT")
        except Exception as e:
            try:
                log.execute("ROLLBACK")
            except Exception:
                pass
            return {
                "success": False, "command": "ingest",
                "error": f"Surgical delete failed: {e}", "data": None,
                **log.to_dict(),
            }

    # 3. Get PDF bytes
    try:
        pdf_bytes = session.file.get_stream(f"{stage_path}/{file}").read()
    except Exception as e:
        return {
            "success": False, "command": "ingest",
            "error": f"Failed to read PDF: {e}", "data": None,
            **log.to_dict(),
        }

    total_pages = get_pdf_page_count(pdf_bytes)

    # 4. Layout extraction
    if use_layout:
        page_filters = []
        if surgical_mappings:
            for rm in surgical_mappings:
                page_filters.append({
                    "start": int(rm["replacement_start"]) - 1,
                    "end": int(rm["replacement_end"]),
                })
        elif scope == "Page Range":
            page_filters.append({"start": page_range[0] - 1, "end": page_range[1]})

        parse_opts: Dict[str, Any] = {"mode": "LAYOUT"}
        if page_filters:
            parse_opts["page_filter"] = page_filters

        parse_sql = f"""
            SELECT SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(
                TO_FILE('{stage_path.replace("'", "''")}', '{safe_file}'),
                PARSE_JSON('{json.dumps(parse_opts).replace("'", "''")}')
            ) AS J
        """
        try:
            raw_res = log.execute(parse_sql)
            if not raw_res or raw_res[0]["J"] is None:
                return {
                    "success": False, "command": "ingest",
                    "error": "AI_PARSE_DOCUMENT returned NULL", "data": None,
                    **log.to_dict(),
                }
            raw = raw_res[0]["J"]
            doc_json = json.loads(raw) if isinstance(raw, str) else raw
            pages_data = doc_json.get("pages") or []
        except Exception as e:
            return {
                "success": False, "command": "ingest",
                "error": f"AI_PARSE_DOCUMENT failed: {e}", "data": None,
                **log.to_dict(),
            }

        page_records: List[Dict] = []
        range_mappings: Optional[List[RangeMapping]] = None
        if surgical_mappings:
            range_mappings = [
                RangeMapping(
                    source_start=int(rm["source_start"]),
                    source_end=int(rm["source_end"]),
                    replacement_start=int(rm["replacement_start"]),
                    replacement_end=int(rm["replacement_end"]),
                )
                for rm in surgical_mappings
            ]

        for pg in pages_data:
            pg_num = int(pg.get("index", 0)) + 1
            content = sanitize_nbsp(pg.get("content", ""))

            encoded = content.encode("utf-8")
            if len(encoded) > SNOWFLAKE_MAX_STRING_BYTES:
                content = encoded[:SNOWFLAKE_MAX_STRING_BYTES].decode("utf-8", "ignore")

            if range_mappings:
                db_pg_num = RangeMappingEngine.target_page_for(range_mappings, pg_num)
                if db_pg_num is None:
                    continue
            else:
                db_pg_num = pg_num

            links = extract_links_from_bytes(pdf_bytes, pg_num)
            link_block = format_link_block(links)
            chunk_ref = build_chunk_ref(file, db_pg_num, link)

            page_records.append({
                "RELATIVE_PATH": file, "PAGE_NUMBER": db_pg_num,
                "PAGE_TEXT": content, "LINK_BLOCK": link_block,
                "CHUNK_REF": chunk_ref, "CHUNK_TYPE": "STANDARD",
            })

        # Placeholders for any missing pages
        if range_mappings:
            expected_pages = set()
            for rm in range_mappings:
                expected_pages.update(
                    range(rm.replacement_start, rm.replacement_end + 1)
                )
        elif scope == "Page Range":
            expected_pages = set(range(page_range[0], page_range[1] + 1))
        else:
            expected_pages = set(range(1, total_pages + 1))

        returned_pages = {int(pg.get("index", 0)) + 1 for pg in pages_data}
        missing = sorted(expected_pages - returned_pages)
        for mp in missing:
            page_records.append({
                "RELATIVE_PATH": file, "PAGE_NUMBER": mp,
                "PAGE_TEXT": f"[Page {mp} — extraction fallback]",
                "LINK_BLOCK": "",
                "CHUNK_REF": build_chunk_ref(file, mp, link),
                "CHUNK_TYPE": "PLACEHOLDER",
            })

        page_records.sort(key=lambda x: x["PAGE_NUMBER"])

        # Build chunk metadata
        if mode == "SURGICAL" and surgical_mappings and range_mappings:
            per_page_mappings = RangeMappingEngine.to_per_page_mappings(range_mappings)
            for pm in per_page_mappings:
                pm["original_pdf_page"] = pm["source"]
            chunk_metadata = ChunkMetadataHandler.build_surgical_select_metadata(
                original_file=file,
                source_range=(page_range[0], page_range[1]),
                replacement_file=file,
                page_mappings=per_page_mappings,
            )
        else:
            metadata_dict = ChunkMetadataHandler.create_initial_metadata(
                write_mode=mode, chunk_type="standard",
                parser_config={"layout": True, "vision": use_vision},
            )
            chunk_metadata = ChunkMetadataHandler.serialize_metadata(metadata_dict)

        # Temp table + batch insert
        temp_name = f"TEMP_CHUNKS_{uuid.uuid4().hex}"
        temp_full = _qualify(db, schema, temp_name)
        log.execute(f"""
            CREATE OR REPLACE TABLE {temp_full} (
                RELATIVE_PATH VARCHAR, PAGE_NUMBER NUMBER, PAGE_TEXT VARCHAR,
                LINK_BLOCK VARCHAR, CHUNK_REF VARCHAR, CHUNK_TYPE VARCHAR
            )
        """)

        batches = [page_records[i:i+100] for i in range(0, len(page_records), 100)]
        try:
            for batch in batches:
                import pandas as pd
                df_batch = pd.DataFrame(batch)
                log.execute(f"TRUNCATE TABLE {temp_full}")
                session.write_pandas(
                    df_batch, table_name=temp_name,
                    database=db, schema=schema,
                    overwrite=False, auto_create_table=False,
                )

                log.execute("BEGIN")
                try:
                    insert_sql = f"""
                    INSERT INTO {full_table}
                        (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE,
                         CHUNK_REF, LINK_BLOCK, CHUNK_METADATA)
                    SELECT
                        t.RELATIVE_PATH, t.PAGE_NUMBER,
                        CASE WHEN NVL(t.LINK_BLOCK, '') = '' THEN c.value::VARCHAR
                             ELSE SUBSTR(c.value::VARCHAR || t.LINK_BLOCK, 1, {CHUNK_INSERT_MAX_CHARS}) END,
                        CONCAT('{CHUNK_ID_PREFIX}', UUID_STRING()), t.CHUNK_TYPE,
                        t.CHUNK_REF, t.LINK_BLOCK,
                        PARSE_JSON('{chunk_metadata.replace("'", "''")}')
                    FROM {temp_full} t,
                    LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(
                        t.PAGE_TEXT, 'markdown', {chunk_sz}, {overlap})) c
                    """
                    log.execute(insert_sql)
                    log.execute("COMMIT")
                    metrics["layout_pages"] += len(batch)
                    metrics["standard_cnt"] += len(batch)
                except Exception:
                    try:
                        log.execute("ROLLBACK")
                    except Exception:
                        pass
        finally:
            try:
                log.execute(f"DROP TABLE IF EXISTS {temp_full}")
            except Exception:
                pass

    # 5. Vision extraction (standalone, no layout)
    if use_vision and not use_layout:
        target_range = range(page_range[0], page_range[1] + 1)
        try:
            from pdf2image import convert_from_bytes
        except ImportError:
            return {
                "success": False, "command": "ingest",
                "error": "pdf2image not available", "data": None,
                **log.to_dict(),
            }

        for pg in target_range:
            imgs = convert_from_bytes(
                pdf_bytes, first_page=pg, last_page=pg, poppler_path=_POPPLER_BIN,
            )
            if not imgs:
                continue

            with _tempdir() as td:
                img_name = f"vis_{uuid.uuid4().hex[:8]}_{pg}"
                img_path = save_optimized_image(imgs[0], td, img_name, sub_folder=file)
                if not img_path:
                    continue

                safe_sub = "".join(c for c in file if c.isalnum() or c in "._-")
                full_stage = f"{stage_path}/_temp_images/{safe_sub}"
                session.file.put(
                    img_path, full_stage, auto_compress=False, overwrite=True,
                )
                rel_img = f"_temp_images/{safe_sub}/{os.path.basename(img_path)}"

                prompt = get_silver_bullet_prompt("", "Vision Extraction Mode")
                res_txt, _, _ = run_cortex(
                    session, log, prompt, stage_path, rel_img, cortex_model,
                )
                if not res_txt:
                    continue

                res_txt = sanitize_nbsp(res_txt)
                links = extract_links_from_bytes(pdf_bytes, pg)
                link_block = format_link_block(links)
                c_ref = build_chunk_ref(file, pg, link)

                meta = ChunkMetadataHandler.create_initial_metadata(
                    write_mode=mode, chunk_type="enhanced",
                    parser_config={"layout": False, "vision": True},
                )
                chunk_meta = ChunkMetadataHandler.serialize_metadata(meta)

                ins_sql = f"""
                INSERT INTO {full_table}
                    (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE,
                     CHUNK_REF, LINK_BLOCK, CHUNK_METADATA)
                SELECT ?, ?, CASE WHEN NVL(?, '') = '' THEN C.VALUE::VARCHAR
                     ELSE SUBSTR(C.VALUE::VARCHAR || ?, 1, {CHUNK_INSERT_MAX_CHARS}) END,
                       CONCAT('{CHUNK_ID_PREFIX}', UUID_STRING()), 'ENHANCED', ?, ?,
                       PARSE_JSON(?)
                FROM LATERAL FLATTEN(
                    INPUT => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(
                        ?, 'markdown', {chunk_sz}, {overlap})
                ) C
                """
                log.execute(
                    ins_sql,
                    params=[file, pg, link_block, link_block, c_ref, link_block,
                            chunk_meta, res_txt],
                )
                metrics["vision_pages"] += 1
                metrics["enhanced_cnt"] += 1

    # 6. Apply grants
    grant_result = None
    if grant_roles:
        valid_roles = [r for r in grant_roles if r and str(r).upper() != "IT_AI"]
        for role in valid_roles:
            try:
                safe_role = str(role).upper().replace('"', '""')
                log.execute(
                    f'GRANT ALL PRIVILEGES ON TABLE {full_table} '
                    f'TO ROLE "{safe_role}"'
                )
            except Exception:
                pass

    metrics["end"] = time.time()
    metrics["duration"] = metrics["end"] - t_start
    metrics["total_pages"] = metrics["layout_pages"] + metrics["vision_pages"]

    # Post-execution warning (headless: we cannot prompt the caller before
    # the work runs, so the warning is delivered alongside the result).
    if mode == "OVERWRITE":
        warning = WARNING_INGEST_OVERWRITE
    elif mode == "SURGICAL":
        warning = WARNING_INGEST_SURGICAL
    else:
        warning = WARNING_INGEST_APPEND

    revert_payload = {
        "command": "CALL chunky_chunks('REVERT', "
                   "OBJECT_CONSTRUCT('db', '" + db + "', "
                   "'schema', '" + schema + "', "
                   "'table', '" + table + "', "
                   "'timestamp_before', '" + (log.timestamp_before or "") + "'));",
        "timestamp_before": log.timestamp_before,
        "query_ids": log.ids,
    }

    return {
        "success": True, "command": "ingest",
        "data": {
            "table": table, "file": file, "mode": mode, "metrics": metrics,
            "grant_result": grant_result,
        },
        "error": None,
        "warning": warning,
        "revert": revert_payload,
        **log.to_dict(),
    }


# ---------------------------------------------------------------------------
# Command: list_chunks
# ---------------------------------------------------------------------------
def cmd_list_chunks(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    full_table = _qualify(db, schema, table)

    where = []
    if inst.get("file"):
        where.append(f"RELATIVE_PATH = '{clean_text_for_sql(inst['file'])}'")
    if inst.get("page_range"):
        pr = inst["page_range"]
        where.append(f"PAGE_NUMBER BETWEEN {int(pr[0])} AND {int(pr[1])}")
    if inst.get("chunk_id"):
        where.append(f"CHUNK_ID = '{clean_text_for_sql(inst['chunk_id'])}'")

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    limit = int(inst.get("limit", 100))

    sql = f"""
        SELECT CHUNK_ID, PAGE_NUMBER, CHUNK, CHUNK_TYPE, RELATIVE_PATH,
               CHUNK_REF, LINK_BLOCK, CHUNK_METADATA
        FROM {full_table} {where_clause}
        ORDER BY PAGE_NUMBER
        LIMIT {limit}
    """
    try:
        rows = log.execute(sql)
        chunks = []
        for r in rows:
            rd = r.as_dict()
            chunks.append({
                "chunk_id": rd.get("CHUNK_ID", ""),
                "page_number": rd.get("PAGE_NUMBER", 0),
                "chunk": rd.get("CHUNK", ""),
                "chunk_type": rd.get("CHUNK_TYPE", "STANDARD"),
                "relative_path": rd.get("RELATIVE_PATH", ""),
                "chunk_ref": rd.get("CHUNK_REF", ""),
                "link_block": rd.get("LINK_BLOCK", ""),
                "chunk_metadata": rd.get("CHUNK_METADATA"),
            })
        return {
            "success": True, "command": "list_chunks",
            "data": {"chunks": chunks, "count": len(chunks)},
            "error": None,
            **log.to_dict(),
        }
    except Exception as e:
        return {
            "success": False, "command": "list_chunks",
            "error": str(e), "data": None,
            **log.to_dict(),
        }


# ---------------------------------------------------------------------------
# Command: update_chunk
# ---------------------------------------------------------------------------
def cmd_update_chunk(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    chunk_id = inst["chunk_id"]
    new_chunk = inst["chunk"]
    full_table = _qualify(db, schema, table)

    try:
        log.execute(
            f"UPDATE {full_table} SET CHUNK = ? WHERE CHUNK_ID = ?",
            params=[new_chunk, chunk_id],
        )
        return {
            "success": True, "command": "update_chunk",
            "data": {"chunk_id": chunk_id},
            "error": None,
            "warning": "Chunk content was overwritten. Use the REVERT command "
                       "with `timestamp_before` to restore the previous value.",
            "revert": {
                "command": "CALL chunky_chunks('REVERT', "
                           "OBJECT_CONSTRUCT('db', '" + db + "', "
                           "'schema', '" + schema + "', "
                           "'table', '" + table + "', "
                           "'timestamp_before', '" + (log.timestamp_before or "") + "'));",
                "timestamp_before": log.timestamp_before,
                "query_ids": log.ids,
            },
            **log.to_dict(),
        }
    except Exception as e:
        return {
            "success": False, "command": "update_chunk",
            "error": str(e), "data": None,
            **log.to_dict(),
        }


# ---------------------------------------------------------------------------
# Command: delete_chunks
# ---------------------------------------------------------------------------
def cmd_delete_chunks(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    full_table = _qualify(db, schema, table)

    where = []
    if inst.get("file"):
        where.append(f"RELATIVE_PATH = '{clean_text_for_sql(inst['file'])}'")
    if inst.get("page_range"):
        pr = inst["page_range"]
        where.append(f"PAGE_NUMBER BETWEEN {int(pr[0])} AND {int(pr[1])}")
    if inst.get("chunk_ids"):
        ids = inst["chunk_ids"]
        id_list = ", ".join(f"'{clean_text_for_sql(c)}'" for c in ids)
        where.append(f"CHUNK_ID IN ({id_list})")

    if not where:
        return {
            "success": False, "command": "delete_chunks",
            "error": "No filter provided", "data": None,
            **log.to_dict(),
        }

    where_clause = " AND ".join(where)
    try:
        log.execute(f"DELETE FROM {full_table} WHERE {where_clause}")
        return {
            "success": True, "command": "delete_chunks",
            "data": {"deleted": True},
            "error": None,
            "warning": WARNING_INGEST_APPEND.replace("APPEND mode added new rows",
                                                    "Chunks were deleted"),
            "revert": {
                "command": "CALL chunky_chunks('REVERT', "
                           "OBJECT_CONSTRUCT('db', '" + db + "', "
                           "'schema', '" + schema + "', "
                           "'table', '" + table + "', "
                           "'timestamp_before', '" + (log.timestamp_before or "") + "'));",
                "timestamp_before": log.timestamp_before,
                "query_ids": log.ids,
            },
            **log.to_dict(),
        }
    except Exception as e:
        return {
            "success": False, "command": "delete_chunks",
            "error": str(e), "data": None,
            **log.to_dict(),
        }


# ---------------------------------------------------------------------------
# Command: revert
# ---------------------------------------------------------------------------
def cmd_revert(session, inst: Dict[str, Any]) -> Dict:
    """Revert the target table to a previous state via TIME TRAVEL."""
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    timestamp_before = inst.get("timestamp_before")
    query_ids = inst.get("query_ids", [])
    file = inst.get("file")
    page_range = inst.get("page_range")

    # Row-scoped revert if both file and page_range supplied
    if file and page_range and timestamp_before:
        return revert_rows(
            session, db, schema, table,
            timestamp_before=timestamp_before,
            file=file,
            page_range=tuple(page_range),
        )

    return revert_table(
        session, db, schema, table,
        timestamp_before=timestamp_before,
        query_ids=query_ids,
    )


# ---------------------------------------------------------------------------
# Tempdir helper (avoids `import tempfile` at module top to keep cold
# import lean in Snowflake's Python runtime)
# ---------------------------------------------------------------------------
class _tempdir:
    def __enter__(self):
        import tempfile
        self._d = tempfile.TemporaryDirectory()
        return self._d.name

    def __exit__(self, *exc):
        try:
            self._d.cleanup()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------
def run(session, command, instruction):
    """Main entry point for the chunky_chunks procedure."""
    cmd = (command or "").upper()
    inst = instruction if isinstance(instruction, dict) else json.loads(str(instruction))

    if cmd == "INGEST":
        return cmd_ingest(session, inst)
    elif cmd == "LIST_CHUNKS":
        return cmd_list_chunks(session, inst)
    elif cmd == "UPDATE_CHUNK":
        return cmd_update_chunk(session, inst)
    elif cmd == "DELETE_CHUNKS":
        return cmd_delete_chunks(session, inst)
    elif cmd == "REVERT":
        return cmd_revert(session, inst)
    else:
        return {
            "success": False, "command": cmd,
            "error": f"Unknown command: {command}", "data": None,
        }
