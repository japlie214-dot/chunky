-- ============================================================================
-- chunky_qa
-- Headless QA Studio. Commands: search, inspect, generate_draft, commit, delete
-- Source: CCS wizard Page 4 + views/qastudio.py
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_qa(
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
import os
import tempfile

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
# Constants
# ---------------------------------------------------------------------------
CHUNK_ID_PREFIX = "CHK_"
CHUNK_INSERT_MAX_CHARS = 15_000_000
CORTEX_MODEL = "claude-haiku-4-5"
TEMP_IMAGE_PREFIX = "_temp_images"


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


def get_original_pdf_page(chunk_metadata, page_number):
    """Resolve original PDF page from surgical metadata."""
    if not chunk_metadata:
        return page_number
    try:
        if isinstance(chunk_metadata, str):
            meta = json.loads(chunk_metadata)
        elif isinstance(chunk_metadata, dict):
            meta = chunk_metadata
        else:
            meta = json.loads(str(chunk_metadata))
        mappings = meta.get("surgical", {}).get("page_mappings", [])
        for pm in mappings:
            if pm.get("target") == page_number:
                return pm.get("original_pdf_page", pm.get("source", page_number))
    except Exception:
        pass
    return page_number


def save_optimized_image(image, output_dir, base_filename, sub_folder=None):
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

    try:
        if hasattr(image, 'width') and image.width > 1600:
            ratio = 1600 / image.width
            new_height = int(image.height * ratio)
            image = image.resize((1600, new_height), PILImage.Resampling.LANCZOS)

        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")

        image.save(png_path, format="PNG", optimize=True)
        if (os.path.getsize(png_path) / (1024 * 1024)) < MAX_IMAGE_MB:
            return png_path

        jpg_path = os.path.join(final_dir, f"{base_filename}.jpg")
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


def render_page_screenshot(session, stage_path, file, page_number):
    """Render PDF page as image, upload to stage, return presigned URL."""
    try:
        from pdf2image import convert_from_bytes
    except ImportError:
        return None

    try:
        pdf_bytes = session.file.get_stream(f"{stage_path}/{file}").read()
    except Exception:
        return None

    imgs = convert_from_bytes(pdf_bytes, first_page=page_number, last_page=page_number, poppler_path=_POPPLER_BIN)
    if not imgs:
        return None

    with tempfile.TemporaryDirectory() as td:
        img_name = f"qa_p{page_number}_{uuid.uuid4().hex[:8]}"
        img_path = save_optimized_image(imgs[0], td, img_name, sub_folder=file)
        if not img_path:
            return None

        safe_sub = "".join(c for c in file if c.isalnum() or c in "._-")
        full_stage = f"{stage_path}/{TEMP_IMAGE_PREFIX}/{safe_sub}"
        try:
            session.file.put(img_path, full_stage, auto_compress=False, overwrite=True)
        except Exception:
            return None

        rel_path = f"{TEMP_IMAGE_PREFIX}/{safe_sub}/{os.path.basename(img_path)}"

        # Get presigned URL
        safe_stage = stage_path.replace("'", "''")
        safe_rel = rel_path.replace("'", "''")
        try:
            url_sql = f"SELECT GET_PRESIGNED_URL('{safe_stage}', '{safe_rel}', 3600) AS URL"
            res = session.sql(url_sql).collect()
            if res and res[0]["URL"]:
                return res[0]["URL"]
        except Exception:
            pass
    return None


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


def run_cortex(session, prompt, stage_root, image_path_relative):
    root = stage_root if stage_root.startswith("@") else f"@{stage_root}"
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


# ---------------------------------------------------------------------------
# Command: search
# ---------------------------------------------------------------------------

def cmd_search(session, inst):
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    stage_path = inst.get("stage_path", "")
    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table.replace('"', '""')
    full_table = f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'

    where = []
    if inst.get("file"):
        if isinstance(inst["file"], list):
            files = inst["file"]
            in_list = ", ".join(f"'{clean_text_for_sql(f)}'" for f in files if f)
            if in_list:
                where.append(f"RELATIVE_PATH IN ({in_list})")
        else:
            safe_f = clean_text_for_sql(inst["file"])
            where.append(f"RELATIVE_PATH = '{safe_f}'")
    if inst.get("page_range"):
        pr = inst["page_range"]
        where.append(f"PAGE_NUMBER BETWEEN {int(pr[0])} AND {int(pr[1])}")
    if inst.get("search_text"):
        safe_txt = clean_text_for_sql(inst["search_text"])
        where.append(f"CONTAINS(CHUNK, '{safe_txt}')")

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
            page_num = rd.get("PAGE_NUMBER", 0)
            rel_path = rd.get("RELATIVE_PATH", "")

            # Generate page screenshot URL if stage_path provided
            screenshot_url = None
            if stage_path:
                original_pg = get_original_pdf_page(rd.get("CHUNK_METADATA"), page_num)
                screenshot_url = render_page_screenshot(session, stage_path, rel_path, original_pg)

            chunks.append({
                "chunk_id": rd.get("CHUNK_ID", ""),
                "page_number": page_num,
                "chunk": rd.get("CHUNK", ""),
                "chunk_type": rd.get("CHUNK_TYPE", "STANDARD"),
                "relative_path": rel_path,
                "chunk_ref": rd.get("CHUNK_REF", ""),
                "link_block": rd.get("LINK_BLOCK", ""),
                "chunk_metadata": rd.get("CHUNK_METADATA"),
                "page_screenshot_url": screenshot_url,
            })
        return {"success": True, "command": "search", "data": {"chunks": chunks, "count": len(chunks)}, "error": None}
    except Exception as e:
        return {"success": False, "command": "search", "error": str(e), "data": None}


# ---------------------------------------------------------------------------
# Command: inspect
# ---------------------------------------------------------------------------

def cmd_inspect(session, inst):
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    stage_path = inst.get("stage_path", "")
    chunk_id = inst["chunk_id"]

    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table.replace('"', '""')
    full_table = f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'

    try:
        sql = f"SELECT CHUNK, CHUNK_METADATA, PAGE_NUMBER, RELATIVE_PATH, CHUNK_TYPE, CHUNK_REF, LINK_BLOCK FROM {full_table} WHERE CHUNK_ID = ?"
        rows = session.sql(sql, params=[chunk_id]).collect()
        if not rows:
            return {"success": False, "command": "inspect", "error": f"Chunk not found: {chunk_id}", "data": None}

        rd = rows[0].as_dict()
        page_num = rd.get("PAGE_NUMBER", 0)
        rel_path = rd.get("RELATIVE_PATH", "")
        chunk_metadata = rd.get("CHUNK_METADATA")

        # Resolve original PDF page (surgical-aware)
        original_pg = get_original_pdf_page(chunk_metadata, page_num)

        # Generate page screenshot
        screenshot_url = None
        if stage_path:
            screenshot_url = render_page_screenshot(session, stage_path, rel_path, original_pg)

        return {
            "success": True, "command": "inspect",
            "data": {
                "chunk_id": chunk_id,
                "page_number": page_num,
                "original_pdf_page": original_pg,
                "chunk": rd.get("CHUNK", ""),
                "chunk_type": rd.get("CHUNK_TYPE", "STANDARD"),
                "relative_path": rel_path,
                "chunk_ref": rd.get("CHUNK_REF", ""),
                "link_block": rd.get("LINK_BLOCK", ""),
                "chunk_metadata": chunk_metadata,
                "page_screenshot_url": screenshot_url,
            },
            "error": None
        }
    except Exception as e:
        return {"success": False, "command": "inspect", "error": str(e), "data": None}


# ---------------------------------------------------------------------------
# Command: generate_draft
# ---------------------------------------------------------------------------

def cmd_generate_draft(session, inst):
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    stage_path = inst["stage_path"]
    chunk_ids = inst.get("chunk_ids", [])
    instruction_text = inst.get("instruction_text", "")

    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table.replace('"', '""')
    full_table = f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'

    if not chunk_ids:
        return {"success": False, "command": "generate_draft", "error": "No chunk_ids provided", "data": None}

    drafts = []
    for cid in chunk_ids:
        safe_id = clean_text_for_sql(cid)
        try:
            sql = f"SELECT CHUNK, PAGE_NUMBER, RELATIVE_PATH, CHUNK_METADATA FROM {full_table} WHERE CHUNK_ID = ?"
            rows = session.sql(sql, params=[safe_id]).collect()
            if not rows:
                drafts.append({"chunk_id": cid, "draft_text": None, "page_screenshot_url": None, "status": "not_found"})
                continue

            rd = rows[0].as_dict()
            original_chunk = rd.get("CHUNK", "")
            page_num = rd.get("PAGE_NUMBER", 0)
            rel_path = rd.get("RELATIVE_PATH", "")
            chunk_metadata = rd.get("CHUNK_METADATA")

            # Resolve original PDF page
            original_pg = get_original_pdf_page(chunk_metadata, page_num)

            # Render page screenshot
            screenshot_url = render_page_screenshot(session, stage_path, rel_path, original_pg)

            # Generate AI draft
            prompt = get_silver_bullet_prompt(original_chunk, instruction_text)

            # Upload page image for Cortex
            try:
                from pdf2image import convert_from_bytes
                pdf_bytes = session.file.get_stream(f"{stage_path}/{rel_path}").read()
                imgs = convert_from_bytes(pdf_bytes, first_page=original_pg, last_page=original_pg, poppler_path=_POPPLER_BIN)
                if imgs:
                    with tempfile.TemporaryDirectory() as td:
                        img_name = f"draft_{uuid.uuid4().hex[:8]}_{original_pg}"
                        img_path = save_optimized_image(imgs[0], td, img_name, sub_folder=rel_path)
                        if img_path:
                            safe_sub = "".join(c for c in rel_path if c.isalnum() or c in "._-")
                            full_stage = f"{stage_path}/{TEMP_IMAGE_PREFIX}/{safe_sub}"
                            session.file.put(img_path, full_stage, auto_compress=False, overwrite=True)
                            rel_img = f"{TEMP_IMAGE_PREFIX}/{safe_sub}/{os.path.basename(img_path)}"
                            draft_text, _, _ = run_cortex(session, prompt, stage_path, rel_img)
                            if draft_text:
                                drafts.append({
                                    "chunk_id": cid,
                                    "draft_text": draft_text,
                                    "page_screenshot_url": screenshot_url,
                                    "status": "ready"
                                })
                            else:
                                drafts.append({"chunk_id": cid, "draft_text": None, "page_screenshot_url": screenshot_url, "status": "generation_failed"})
                        else:
                            drafts.append({"chunk_id": cid, "draft_text": None, "page_screenshot_url": screenshot_url, "status": "image_save_failed"})
                else:
                    drafts.append({"chunk_id": cid, "draft_text": None, "page_screenshot_url": None, "status": "render_failed"})
            except Exception as e:
                drafts.append({"chunk_id": cid, "draft_text": None, "page_screenshot_url": screenshot_url, "status": f"error: {e}"})

        except Exception as e:
            drafts.append({"chunk_id": cid, "draft_text": None, "page_screenshot_url": None, "status": f"error: {e}"})

    return {"success": True, "command": "generate_draft", "data": {"drafts": drafts}, "error": None}


# ---------------------------------------------------------------------------
# Command: commit
# ---------------------------------------------------------------------------

def cmd_commit(session, inst):
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    commits = inst.get("commits", [])

    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table.replace('"', '""')
    full_table = f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'

    if not commits:
        return {"success": False, "command": "commit", "error": "No commits provided", "data": None}

    results = []
    for c in commits:
        cid = c.get("chunk_id")
        draft = c.get("draft_text")
        if not cid or not draft:
            results.append({"chunk_id": cid, "status": "skipped"})
            continue
        try:
            session.sql(f"UPDATE {full_table} SET CHUNK = ? WHERE CHUNK_ID = ?",
                         params=[draft, cid]).collect()
            results.append({"chunk_id": cid, "status": "committed"})
        except Exception as e:
            results.append({"chunk_id": cid, "status": f"error: {e}"})

    return {"success": True, "command": "commit", "data": {"results": results}, "error": None}


# ---------------------------------------------------------------------------
# Command: delete
# ---------------------------------------------------------------------------

def cmd_delete(session, inst):
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    chunk_ids = inst.get("chunk_ids", [])

    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table.replace('"', '""')
    full_table = f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'

    if not chunk_ids:
        return {"success": False, "command": "delete", "error": "No chunk_ids provided", "data": None}

    id_list = ", ".join(f"'{clean_text_for_sql(c)}'" for c in chunk_ids)
    try:
        session.sql(f"DELETE FROM {full_table} WHERE CHUNK_ID IN ({id_list})").collect()
        return {"success": True, "command": "delete", "data": {"deleted": len(chunk_ids)}, "error": None}
    except Exception as e:
        return {"success": False, "command": "delete", "error": str(e), "data": None}


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def run(session, command, instruction):
    cmd = command.upper() if command else ""
    inst = instruction if isinstance(instruction, dict) else json.loads(str(instruction))

    if cmd == "SEARCH":
        return cmd_search(session, inst)
    elif cmd == "INSPECT":
        return cmd_inspect(session, inst)
    elif cmd == "GENERATE_DRAFT":
        return cmd_generate_draft(session, inst)
    elif cmd == "COMMIT":
        return cmd_commit(session, inst)
    elif cmd == "DELETE":
        return cmd_delete(session, inst)
    else:
        return {"success": False, "command": cmd, "error": f"Unknown command: {command}", "data": None}
$$;
