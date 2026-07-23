-- ============================================================================
-- chunky_chunks
-- Ingestion Engine. Commands: ingest, list_chunks, update_chunk, delete_chunks
-- Source: CCS wizard Pages 2-3 + Doc Refinery batch_processor
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_chunks(
    command VARCHAR,
    instruction VARIANT
)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
RESOURCE_CONSTRAINT = (architecture = 'x86')
IMPORTS = ('@DEV_DB.DNA.STG_LIB/poppler_bundle.zip')
PACKAGES = ('snowflake-snowpark-python', 'pandas', 'pypdf', 'pillow')
HANDLER = 'run'
EXECUTE AS CALLER
AS
$$
import json
import uuid
import time
import re
import os
from datetime import datetime

# Configure poppler path from the imported bundle
_POPPLER_BASE = os.path.join(os.path.dirname(__file__), 'poppler_bundle', 'poppler')
_POPPLER_BIN = os.path.join(_POPPLER_BASE, 'bin')
_POPPLER_LIB = os.path.join(_POPPLER_BASE, 'lib')
if os.path.isdir(_POPPLER_LIB):
    _ld = os.environ.get('LD_LIBRARY_PATH', '')
    os.environ['LD_LIBRARY_PATH'] = _POPPLER_LIB + (':' + _ld if _ld else '')
if os.path.isdir(_POPPLER_BIN):
    os.environ['PATH'] = _POPPLER_BIN + ':' + os.environ.get('PATH', '')

# ---------------------------------------------------------------------------
# Constants (from utils/constants.py)
# ---------------------------------------------------------------------------
CHUNK_ID_PREFIX = "CHK_"
CHUNK_INSERT_MAX_CHARS = 15_000_000
SNOWFLAKE_MAX_STRING_BYTES = 16_777_216
CHUNK_CACHE_MAX_SIZE = 5000
LAYOUT_COST_PER_1K_PAGES = 3.33

CORTEX_MODEL = "claude-haiku-4-5"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_text_for_sql(text):
    if not text:
        return ""
    safe = text.replace("'", "''")
    return ''.join(ch for ch in safe if ch.isprintable() or ch in ("\n", "\r", "\t"))


def sanitize_nbsp(text):
    if not text:
        return text
    import re as _re
    return _re.sub(r'&nbsp;|&#160;|&#x[aA]0;', ' ', text)


def build_chunk_ref(rel_path, page_num, link=""):
    base = f"Doc Source: {rel_path} | Page Num: {page_num}"
    if link:
        import urllib.parse
        safe_link = urllib.parse.quote(link, safe=":/?#&=@")
        return f"[Digital Copy]({safe_link}) | {base}"
    return base


def get_pdf_page_count(pdf_bytes):
    """Get PDF page count using pypdf (pure Python)."""
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return len(reader.pages)
    except Exception:
        return 1


def extract_links_from_bytes(pdf_bytes, page_number):
    """Extract URLs from a PDF page using pypdf."""
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if page_number < 1 or page_number > len(reader.pages):
            return []
        page = reader.pages[page_number - 1]
        urls = []
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


def format_link_block(urls):
    if not urls:
        return ""
    lines = "\n".join(f"  - {u}" for u in urls)
    return f"\n\n[External links:\n{lines}\n]"


def save_optimized_image(image, output_dir, base_filename, sub_folder=None):
    """Save image under 3.5MB for Snowflake Cortex."""
    MAX_IMAGE_MB = 3.5
    import os
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


def run_cortex(session, prompt, stage_root, image_path_relative):
    """Execute AI_COMPLETE with an image."""
    root = stage_root if stage_root.startswith('@') else f"@{stage_root}"
    safe_prompt = prompt.replace("'", "''")
    safe_root = root.replace("'", "''")
    safe_path = image_path_relative.replace("'", "''")

    sql = f"""
        SELECT SNOWFLAKE.CORTEX.AI_COMPLETE(
            '{CORTEX_MODEL}',
            '{safe_prompt}',
            TO_FILE('{safe_root}', '{safe_path}')
        ) AS RES
    """
    try:
        res = session.sql(sql).collect()
        if not res or not res[0]["RES"]:
            return "", 0, 0
        text = res[0]["RES"].strip()
        p_tokens = (len(prompt) // 4) + 1000
        c_tokens = len(text) // 4
        return text, p_tokens, c_tokens
    except Exception:
        return "", 0, 0


def get_silver_bullet_prompt(input_text, context_instruction=None):
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


# ---------------------------------------------------------------------------
# Command: ingest
# ---------------------------------------------------------------------------

def cmd_ingest(session, inst):
    """Full ingestion pipeline: init table → surgical → layout → vision → hybrid → chunk → insert → grant."""
    t_start = time.time()
    file = inst["file"]
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    stage_path = inst["stage_path"]
    mode = inst.get("mode", "APPEND")
    scope = inst.get("scope", "Full Doc")
    chunk_sz = inst.get("chunk_size", 8000)
    overlap = inst.get("overlap", 20)
    use_layout = inst.get("layout", True)
    use_vision = inst.get("vision", False)
    link = inst.get("link", "")
    grant_roles = inst.get("grant_roles", [])
    surgical_mappings = inst.get("surgical_range_mappings", [])
    page_range = inst.get("range", [1, 1])

    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table.replace('"', '""')
    full_table = f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'
    safe_file = clean_text_for_sql(file)

    metrics = {
        "start": t_start, "end": None, "duration": 0,
        "layout_pages": 0, "vision_pages": 0,
        "standard_cnt": 0, "enhanced_cnt": 0,
        "placeholder_cnt": 0, "total_pages": 0,
    }

    # 1. Init table
    session.sql(f"""
        CREATE TABLE IF NOT EXISTS {full_table} (
            RELATIVE_PATH VARCHAR, PAGE_NUMBER NUMBER, CHUNK VARCHAR,
            CHUNK_ID VARCHAR, CHUNK_TYPE VARCHAR DEFAULT 'STANDARD',
            CHUNK_REF VARCHAR, LINK_BLOCK VARCHAR, CHUNK_METADATA VARIANT
        ) CHANGE_TRACKING = TRUE
    """).collect()

    if mode == "OVERWRITE":
        session.sql(f"CREATE OR REPLACE TABLE {full_table} ("
            "RELATIVE_PATH VARCHAR, PAGE_NUMBER NUMBER, CHUNK VARCHAR, "
            "CHUNK_ID VARCHAR, CHUNK_TYPE VARCHAR DEFAULT 'STANDARD', "
            "CHUNK_REF VARCHAR, LINK_BLOCK VARCHAR, CHUNK_METADATA VARIANT"
            ") CHANGE_TRACKING = TRUE COPY GRANTS").collect()

    # 2. Surgical delete
    if mode == "SURGICAL" and surgical_mappings:
        sorted_rms = sorted(surgical_mappings, key=lambda m: int(m["source_end"]), reverse=True)
        session.sql("BEGIN").collect()
        try:
            for rm in sorted_rms:
                del_sql = (f"DELETE FROM {full_table} "
                    f"WHERE RELATIVE_PATH = '{safe_file}' "
                    f"AND PAGE_NUMBER BETWEEN {int(rm['source_start'])} AND {int(rm['source_end'])}")
                session.sql(del_sql).collect()
            session.sql("COMMIT").collect()
        except Exception as e:
            session.sql("ROLLBACK").collect()
            return {"success": False, "command": "ingest", "error": f"Surgical delete failed: {e}", "data": None}

    # 3. Get PDF bytes
    try:
        pdf_bytes = session.file.get_stream(f"{stage_path}/{file}").read()
    except Exception as e:
        return {"success": False, "command": "ingest", "error": f"Failed to read PDF: {e}", "data": None}

    total_pages = get_pdf_page_count(pdf_bytes)

    # 4. Layout extraction
    if use_layout:
        page_filters = []
        if surgical_mappings:
            for rm in surgical_mappings:
                page_filters.append({"start": int(rm["replacement_start"]) - 1, "end": int(rm["replacement_end"])})
        elif scope == "Page Range":
            page_filters.append({"start": page_range[0] - 1, "end": page_range[1]})

        parse_opts = {"mode": "LAYOUT"}
        if page_filters:
            parse_opts["page_filter"] = page_filters

        parse_sql = f"""
            SELECT SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(
                TO_FILE('{stage_path.replace("'", "''")}', '{safe_file}'),
                PARSE_JSON('{json.dumps(parse_opts).replace("'", "''")}')
            ) AS J
        """
        try:
            raw_res = session.sql(parse_sql).collect()[0]["J"]
            if raw_res is None:
                return {"success": False, "command": "ingest", "error": "AI_PARSE_DOCUMENT returned NULL", "data": None}
            doc_json = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
            pages_data = doc_json.get("pages") or []
        except Exception as e:
            return {"success": False, "command": "ingest", "error": f"AI_PARSE_DOCUMENT failed: {e}", "data": None}

        # Process pages into records
        page_records = []
        range_mappings = None
        if surgical_mappings:
            from utils.page_mapping import RangeMapping, RangeMappingEngine
            range_mappings = [
                RangeMapping(
                    source_start=int(rm["source_start"]), source_end=int(rm["source_end"]),
                    replacement_start=int(rm["replacement_start"]), replacement_end=int(rm["replacement_end"])
                ) for rm in surgical_mappings
            ]

        for pg in pages_data:
            pg_num = int(pg.get("index", 0)) + 1
            content = sanitize_nbsp(pg.get("content", ""))

            encoded = content.encode("utf-8")
            if len(encoded) > SNOWFLAKE_MAX_STRING_BYTES:
                content = encoded[:SNOWFLAKE_MAX_STRING_BYTES].decode("utf-8", "ignore")

            if range_mappings:
                from utils.page_mapping import RangeMappingEngine
                db_pg_num = RangeMappingEngine.target_page_for(range_mappings, pg_num)
                if db_pg_num is None:
                    continue
            else:
                db_pg_num = pg_num

            links = extract_links_from_bytes(pdf_bytes, pg_num)
            link_block = format_link_block(links)
            chunk_ref = build_chunk_ref(file, db_pg_num, link)

            page_records.append({
                "RELATIVE_PATH": file, "PAGE_NUMBER": db_pg_num, "PAGE_TEXT": content,
                "LINK_BLOCK": link_block, "CHUNK_REF": chunk_ref, "CHUNK_TYPE": "STANDARD"
            })

        # Placeholder for missing pages
        if range_mappings:
            expected_pages = set()
            for rm in range_mappings:
                expected_pages.update(range(rm.replacement_start, rm.replacement_end + 1))
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
                "LINK_BLOCK": "", "CHUNK_REF": build_chunk_ref(file, mp, link),
                "CHUNK_TYPE": "PLACEHOLDER"
            })

        page_records.sort(key=lambda x: x["PAGE_NUMBER"])

        # Build metadata
        if mode == "SURGICAL" and surgical_mappings:
            from utils.metadata_handler import ChunkMetadataHandler
            from utils.page_mapping import RangeMappingEngine
            per_page_mappings = RangeMappingEngine.to_per_page_mappings(range_mappings)
            for pm in per_page_mappings:
                pm["original_pdf_page"] = pm["source"]
            chunk_metadata = ChunkMetadataHandler.build_surgical_select_metadata(
                original_file=file, source_range=(page_range[0], page_range[1]),
                replacement_file=file, page_mappings=per_page_mappings
            )
        else:
            from utils.metadata_handler import ChunkMetadataHandler
            metadata_dict = ChunkMetadataHandler.create_initial_metadata(
                write_mode=mode, chunk_type="standard",
                parser_config={"layout": True, "vision": use_vision}
            )
            chunk_metadata = ChunkMetadataHandler.serialize_metadata(metadata_dict)

        # Temp table + batch insert
        temp_name = f"TEMP_CHUNKS_{uuid.uuid4().hex}"
        temp_full = f'"{safe_db}"."{safe_sch}"."{temp_name}"'
        session.sql(f"""
            CREATE OR REPLACE TABLE {temp_full} (
                RELATIVE_PATH VARCHAR, PAGE_NUMBER NUMBER, PAGE_TEXT VARCHAR,
                LINK_BLOCK VARCHAR, CHUNK_REF VARCHAR, CHUNK_TYPE VARCHAR
            )
        """).collect()

        batches = [page_records[i:i+100] for i in range(0, len(page_records), 100)]

        try:
            for batch in batches:
                import pandas as pd
                df_batch = pd.DataFrame(batch)
                session.sql(f"TRUNCATE TABLE {temp_full}").collect()
                session.write_pandas(df_batch, table_name=temp_name, database=db, schema=schema,
                                     overwrite=False, auto_create_table=False)

                session.sql("BEGIN").collect()
                try:
                    insert_sql = f"""
                    INSERT INTO {full_table} (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE, CHUNK_REF, LINK_BLOCK, CHUNK_METADATA)
                    SELECT
                        t.RELATIVE_PATH, t.PAGE_NUMBER,
                        CASE WHEN NVL(t.LINK_BLOCK, '') = '' THEN c.value::VARCHAR
                             ELSE SUBSTR(c.value::VARCHAR || t.LINK_BLOCK, 1, {CHUNK_INSERT_MAX_CHARS}) END,
                        CONCAT('{CHUNK_ID_PREFIX}', UUID_STRING()), t.CHUNK_TYPE, t.CHUNK_REF, t.LINK_BLOCK,
                        PARSE_JSON('{chunk_metadata.replace("'", "''")}')
                    FROM {temp_full} t,
                    LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(t.PAGE_TEXT, 'markdown', {chunk_sz}, {overlap})) c
                    """
                    session.sql(insert_sql).collect()
                    session.sql("COMMIT").collect()
                    metrics["layout_pages"] += len(batch)
                    metrics["standard_cnt"] += len(batch)
                except Exception:
                    session.sql("ROLLBACK").collect()
        finally:
            session.sql(f"DROP TABLE IF EXISTS {temp_full}").collect()

    # 5. Vision extraction (standalone, no layout)
    if use_vision and not use_layout:
        target_range = range(page_range[0], page_range[1] + 1)
        import tempfile, os
        try:
            from pdf2image import convert_from_bytes
        except ImportError:
            return {"success": False, "command": "ingest", "error": "pdf2image not available", "data": None}

        for pg in target_range:
            imgs = convert_from_bytes(pdf_bytes, first_page=pg, last_page=pg, poppler_path=_POPPLER_BIN)
            if not imgs:
                continue

            with tempfile.TemporaryDirectory() as td:
                img_name = f"vis_{uuid.uuid4().hex[:8]}_{pg}"
                img_path = save_optimized_image(imgs[0], td, img_name, sub_folder=file)
                if not img_path:
                    continue

                safe_sub = "".join(c for c in file if c.isalnum() or c in "._-")
                full_stage = f"{stage_path}/_temp_images/{safe_sub}"
                session.file.put(img_path, full_stage, auto_compress=False, overwrite=True)
                rel_img = f"_temp_images/{safe_sub}/{os.path.basename(img_path)}"

                prompt = get_silver_bullet_prompt("", "Vision Extraction Mode")
                res_txt, p_tok, c_tok = run_cortex(session, prompt, stage_path, rel_img)
                if not res_txt:
                    continue

                res_txt = sanitize_nbsp(res_txt)
                links = extract_links_from_bytes(pdf_bytes, pg)
                link_block = format_link_block(links)
                c_ref = build_chunk_ref(file, pg, link)

                from utils.metadata_handler import ChunkMetadataHandler
                meta = ChunkMetadataHandler.create_initial_metadata(
                    write_mode=mode, chunk_type="enhanced",
                    parser_config={"layout": False, "vision": True}
                )
                chunk_meta = ChunkMetadataHandler.serialize_metadata(meta)

                ins_sql = f"""
                INSERT INTO {full_table}
                    (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE, CHUNK_REF, LINK_BLOCK, CHUNK_METADATA)
                SELECT ?, ?, CASE WHEN NVL(?, '') = '' THEN C.VALUE::VARCHAR
                     ELSE SUBSTR(C.VALUE::VARCHAR || ?, 1, {CHUNK_INSERT_MAX_CHARS}) END,
                       CONCAT('{CHUNK_ID_PREFIX}', UUID_STRING()), 'ENHANCED', ?, ?, PARSE_JSON(?)
                FROM LATERAL FLATTEN(
                    INPUT => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(?, 'markdown', {chunk_sz}, {overlap})
                ) C
                """
                session.sql(ins_sql, params=[file, pg, link_block, link_block, c_ref, link_block, chunk_meta, res_txt]).collect()
                metrics["vision_pages"] += 1
                metrics["enhanced_cnt"] += 1

    # 6. Apply grants
    grant_result = None
    if grant_roles:
        valid_roles = [r for r in grant_roles if r and r.upper() != "IT_AI"]
        if valid_roles:
            for role in valid_roles:
                try:
                    safe_role = role.upper().replace('"', '""')
                    grant_sql = f'GRANT ALL PRIVILEGES ON TABLE {full_table} TO ROLE "{safe_role}"'
                    session.sql(grant_sql).collect()
                except Exception:
                    pass

    metrics["end"] = time.time()
    metrics["duration"] = metrics["end"] - t_start
    metrics["total_pages"] = metrics["layout_pages"] + metrics["vision_pages"]

    return {
        "success": True, "command": "ingest",
        "data": {"table": table, "file": file, "mode": mode, "metrics": metrics},
        "error": None
    }


# ---------------------------------------------------------------------------
# Command: list_chunks
# ---------------------------------------------------------------------------

def cmd_list_chunks(session, inst):
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table.replace('"', '""')
    full_table = f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'

    where = []
    if inst.get("file"):
        safe_f = clean_text_for_sql(inst["file"])
        where.append(f"RELATIVE_PATH = '{safe_f}'")
    if inst.get("page_range"):
        pr = inst["page_range"]
        where.append(f"PAGE_NUMBER BETWEEN {int(pr[0])} AND {int(pr[1])}")
    if inst.get("chunk_id"):
        safe_id = clean_text_for_sql(inst["chunk_id"])
        where.append(f"CHUNK_ID = '{safe_id}'")

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    limit = inst.get("limit", 100)

    sql = f"""
        SELECT CHUNK_ID, PAGE_NUMBER, CHUNK, CHUNK_TYPE, RELATIVE_PATH,
               CHUNK_REF, LINK_BLOCK, CHUNK_METADATA
        FROM {full_table} {where_clause}
        ORDER BY PAGE_NUMBER
        LIMIT {int(limit)}
    """
    try:
        rows = session.sql(sql).collect()
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
        return {"success": True, "command": "list_chunks", "data": {"chunks": chunks, "count": len(chunks)}, "error": None}
    except Exception as e:
        return {"success": False, "command": "list_chunks", "error": str(e), "data": None}


# ---------------------------------------------------------------------------
# Command: update_chunk
# ---------------------------------------------------------------------------

def cmd_update_chunk(session, inst):
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    chunk_id = inst["chunk_id"]
    new_chunk = inst["chunk"]

    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table.replace('"', '""')
    full_table = f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'

    try:
        session.sql(f"UPDATE {full_table} SET CHUNK = ? WHERE CHUNK_ID = ?",
                     params=[new_chunk, chunk_id]).collect()
        return {"success": True, "command": "update_chunk", "data": {"chunk_id": chunk_id}, "error": None}
    except Exception as e:
        return {"success": False, "command": "update_chunk", "error": str(e), "data": None}


# ---------------------------------------------------------------------------
# Command: delete_chunks
# ---------------------------------------------------------------------------

def cmd_delete_chunks(session, inst):
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table.replace('"', '""')
    full_table = f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'

    where = []
    if inst.get("file"):
        safe_f = clean_text_for_sql(inst["file"])
        where.append(f"RELATIVE_PATH = '{safe_f}'")
    if inst.get("page_range"):
        pr = inst["page_range"]
        where.append(f"PAGE_NUMBER BETWEEN {int(pr[0])} AND {int(pr[1])}")
    if inst.get("chunk_ids"):
        ids = inst["chunk_ids"]
        id_list = ", ".join(f"'{clean_text_for_sql(c)}'" for c in ids)
        where.append(f"CHUNK_ID IN ({id_list})")

    if not where:
        return {"success": False, "command": "delete_chunks", "error": "No filter provided", "data": None}

    where_clause = " AND ".join(where)
    try:
        res = session.sql(f"DELETE FROM {full_table} WHERE {where_clause}").collect()
        return {"success": True, "command": "delete_chunks", "data": {"deleted": True}, "error": None}
    except Exception as e:
        return {"success": False, "command": "delete_chunks", "error": str(e), "data": None}


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def run(session, command, instruction):
    """Main entry point for chunky_chunks procedure."""
    cmd = command.upper() if command else ""
    inst = instruction if isinstance(instruction, dict) else json.loads(str(instruction))

    if cmd == "INGEST":
        return cmd_ingest(session, inst)
    elif cmd == "LIST_CHUNKS":
        return cmd_list_chunks(session, inst)
    elif cmd == "UPDATE_CHUNK":
        return cmd_update_chunk(session, inst)
    elif cmd == "DELETE_CHUNKS":
        return cmd_delete_chunks(session, inst)
    else:
        return {"success": False, "command": cmd, "error": f"Unknown command: {command}", "data": None}
$$;
