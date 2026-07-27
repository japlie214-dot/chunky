"""
procedure/utils/chunky_qa_handler.py
Headless QA Studio. Commands: search, inspect, generate_draft,
commit, delete, revert.

Source (logical): CCS wizard Page 4 + views/qastudio.py — this is the
Streamlit-free, headless equivalent.

Headless changes vs. the original Streamlit-side code:
  * No `streamlit` imports, no `st.session_state`, no UI fragments.
  * Warnings are returned in the response AFTER execution.
  * Every SQL operation runs through `QueryLog.execute` for query-id
    capture.
  * New `revert` command rewinds the table via TIME TRAVEL.
"""
from __future__ import annotations
import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional

from .constants import (
    CHUNK_ID_PREFIX,
    CHUNK_INSERT_MAX_CHARS,
    TEMP_IMAGE_PREFIX,
    DEFAULT_CORTEX_MODEL,
    WARNING_QA_COMMIT,
    WARNING_QA_DELETE,
)
from .query_log import QueryLog
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


def get_original_pdf_page(chunk_metadata, page_number: int) -> int:
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


def save_optimized_image(image, output_dir: str, base_filename: str,
                         sub_folder: Optional[str] = None) -> Optional[str]:
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


def render_page_screenshot(session, log: QueryLog, stage_path: str,
                           file: str, page_number: int) -> Optional[str]:
    """Render PDF page as image, upload to stage, return presigned URL."""
    try:
        from pdf2image import convert_from_bytes
    except ImportError:
        return None

    try:
        pdf_bytes = session.file.get_stream(f"{stage_path}/{file}").read()
    except Exception:
        return None

    imgs = convert_from_bytes(
        pdf_bytes, first_page=page_number, last_page=page_number,
        poppler_path=_POPPLER_BIN,
    )
    if not imgs:
        return None

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        img_name = f"qa_p{page_number}_{uuid.uuid4().hex[:8]}"
        img_path = save_optimized_image(imgs[0], td, img_name, sub_folder=file)
        if not img_path:
            return None

        safe_sub = "".join(c for c in file if c.isalnum() or c in "._-")
        full_stage = f"{stage_path}/{TEMP_IMAGE_PREFIX}/{safe_sub}"
        try:
            session.file.put(
                img_path, full_stage, auto_compress=False, overwrite=True,
            )
        except Exception:
            return None

        rel_path = f"{TEMP_IMAGE_PREFIX}/{safe_sub}/{os.path.basename(img_path)}"
        safe_stage = stage_path.replace("'", "''")
        safe_rel = rel_path.replace("'", "''")
        try:
            url_sql = (
                f"SELECT GET_PRESIGNED_URL('{safe_stage}', '{safe_rel}', 3600) AS URL"
            )
            res = log.execute(url_sql)
            if res and res[0]["URL"]:
                return res[0]["URL"]
        except Exception:
            pass
    return None


def get_silver_bullet_prompt(input_text: str,
                             context_instruction: Optional[str] = None) -> str:
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


def run_cortex(session, log: QueryLog, prompt: str, stage_root: str,
               image_path_relative: str, model: str = DEFAULT_CORTEX_MODEL):
    root = stage_root if stage_root.startswith("@") else f"@{stage_root}"
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


def _qualify(db: str, schema: str, table: str) -> str:
    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_tbl = table.replace('"', '""')
    return f'"{safe_db}"."{safe_sch}"."{safe_tbl}"'


# ---------------------------------------------------------------------------
# Command: search
# ---------------------------------------------------------------------------
def cmd_search(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    stage_path = inst.get("stage_path", "")
    full_table = _qualify(db, schema, table)

    where = []
    if inst.get("file"):
        if isinstance(inst["file"], list):
            files = inst["file"]
            in_list = ", ".join(
                f"'{clean_text_for_sql(f)}'" for f in files if f
            )
            if in_list:
                where.append(f"RELATIVE_PATH IN ({in_list})")
        else:
            where.append(f"RELATIVE_PATH = '{clean_text_for_sql(inst['file'])}'")
    if inst.get("page_range"):
        pr = inst["page_range"]
        where.append(f"PAGE_NUMBER BETWEEN {int(pr[0])} AND {int(pr[1])}")
    if inst.get("search_text"):
        where.append(f"CONTAINS(CHUNK, '{clean_text_for_sql(inst['search_text'])}')")

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
            page_num = rd.get("PAGE_NUMBER", 0)
            rel_path = rd.get("RELATIVE_PATH", "")

            screenshot_url = None
            if stage_path:
                original_pg = get_original_pdf_page(rd.get("CHUNK_METADATA"), page_num)
                screenshot_url = render_page_screenshot(
                    session, log, stage_path, rel_path, original_pg,
                )

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
        return {
            "success": True, "command": "search",
            "data": {"chunks": chunks, "count": len(chunks)},
            "error": None,
            **log.to_dict(),
        }
    except Exception as e:
        return {
            "success": False, "command": "search",
            "error": str(e), "data": None,
            **log.to_dict(),
        }


# ---------------------------------------------------------------------------
# Command: inspect
# ---------------------------------------------------------------------------
def cmd_inspect(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    stage_path = inst.get("stage_path", "")
    chunk_id = inst["chunk_id"]
    full_table = _qualify(db, schema, table)

    try:
        sql = (
            f"SELECT CHUNK, CHUNK_METADATA, PAGE_NUMBER, RELATIVE_PATH, "
            f"CHUNK_TYPE, CHUNK_REF, LINK_BLOCK FROM {full_table} "
            f"WHERE CHUNK_ID = ?"
        )
        rows = log.execute(sql, params=[chunk_id])
        if not rows:
            return {
                "success": False, "command": "inspect",
                "error": f"Chunk not found: {chunk_id}", "data": None,
                **log.to_dict(),
            }

        rd = rows[0].as_dict()
        page_num = rd.get("PAGE_NUMBER", 0)
        rel_path = rd.get("RELATIVE_PATH", "")
        chunk_metadata = rd.get("CHUNK_METADATA")

        original_pg = get_original_pdf_page(chunk_metadata, page_num)

        screenshot_url = None
        if stage_path:
            screenshot_url = render_page_screenshot(
                session, log, stage_path, rel_path, original_pg,
            )

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
            "error": None,
            **log.to_dict(),
        }
    except Exception as e:
        return {
            "success": False, "command": "inspect",
            "error": str(e), "data": None,
            **log.to_dict(),
        }


# ---------------------------------------------------------------------------
# Command: generate_draft
# ---------------------------------------------------------------------------
def cmd_generate_draft(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    stage_path = inst["stage_path"]
    chunk_ids = inst.get("chunk_ids", [])
    instruction_text = inst.get("instruction_text", "")
    cortex_model = inst.get("cortex_model", DEFAULT_CORTEX_MODEL)
    full_table = _qualify(db, schema, table)

    if not chunk_ids:
        return {
            "success": False, "command": "generate_draft",
            "error": "No chunk_ids provided", "data": None,
            **log.to_dict(),
        }

    drafts = []
    for cid in chunk_ids:
        safe_id = clean_text_for_sql(cid)
        try:
            sql = (
                f"SELECT CHUNK, PAGE_NUMBER, RELATIVE_PATH, CHUNK_METADATA "
                f"FROM {full_table} WHERE CHUNK_ID = ?"
            )
            rows = log.execute(sql, params=[safe_id])
            if not rows:
                drafts.append({
                    "chunk_id": cid, "draft_text": None,
                    "page_screenshot_url": None, "status": "not_found",
                })
                continue

            rd = rows[0].as_dict()
            original_chunk = rd.get("CHUNK", "")
            page_num = rd.get("PAGE_NUMBER", 0)
            rel_path = rd.get("RELATIVE_PATH", "")
            chunk_metadata = rd.get("CHUNK_METADATA")

            original_pg = get_original_pdf_page(chunk_metadata, page_num)
            screenshot_url = render_page_screenshot(
                session, log, stage_path, rel_path, original_pg,
            )

            prompt = get_silver_bullet_prompt(original_chunk, instruction_text)

            try:
                from pdf2image import convert_from_bytes
                pdf_bytes = session.file.get_stream(
                    f"{stage_path}/{rel_path}"
                ).read()
                imgs = convert_from_bytes(
                    pdf_bytes, first_page=original_pg, last_page=original_pg,
                    poppler_path=_POPPLER_BIN,
                )
                if not imgs:
                    drafts.append({
                        "chunk_id": cid, "draft_text": None,
                        "page_screenshot_url": None, "status": "render_failed",
                    })
                    continue

                import tempfile
                with tempfile.TemporaryDirectory() as td:
                    img_name = f"draft_{uuid.uuid4().hex[:8]}_{original_pg}"
                    img_path = save_optimized_image(
                        imgs[0], td, img_name, sub_folder=rel_path,
                    )
                    if not img_path:
                        drafts.append({
                            "chunk_id": cid, "draft_text": None,
                            "page_screenshot_url": screenshot_url,
                            "status": "image_save_failed",
                        })
                        continue

                    safe_sub = "".join(c for c in rel_path if c.isalnum() or c in "._-")
                    full_stage = f"{stage_path}/{TEMP_IMAGE_PREFIX}/{safe_sub}"
                    session.file.put(
                        img_path, full_stage, auto_compress=False, overwrite=True,
                    )
                    rel_img = f"{TEMP_IMAGE_PREFIX}/{safe_sub}/{os.path.basename(img_path)}"
                    draft_text, _, _ = run_cortex(
                        session, log, prompt, stage_path, rel_img, cortex_model,
                    )
                    if draft_text:
                        drafts.append({
                            "chunk_id": cid, "draft_text": draft_text,
                            "page_screenshot_url": screenshot_url,
                            "status": "ready",
                        })
                    else:
                        drafts.append({
                            "chunk_id": cid, "draft_text": None,
                            "page_screenshot_url": screenshot_url,
                            "status": "generation_failed",
                        })
            except Exception as e:
                drafts.append({
                    "chunk_id": cid, "draft_text": None,
                    "page_screenshot_url": screenshot_url,
                    "status": f"error: {e}",
                })
        except Exception as e:
            drafts.append({
                "chunk_id": cid, "draft_text": None,
                "page_screenshot_url": None, "status": f"error: {e}",
            })

    return {
        "success": True, "command": "generate_draft",
        "data": {"drafts": drafts},
        "error": None,
        **log.to_dict(),
    }


# ---------------------------------------------------------------------------
# Command: commit
# ---------------------------------------------------------------------------
def cmd_commit(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    commits = inst.get("commits", [])
    full_table = _qualify(db, schema, table)

    if not commits:
        return {
            "success": False, "command": "commit",
            "error": "No commits provided", "data": None,
            **log.to_dict(),
        }

    results = []
    for c in commits:
        cid = c.get("chunk_id")
        draft = c.get("draft_text")
        if not cid or not draft:
            results.append({"chunk_id": cid, "status": "skipped"})
            continue
        try:
            log.execute(
                f"UPDATE {full_table} SET CHUNK = ? WHERE CHUNK_ID = ?",
                params=[draft, cid],
            )
            results.append({"chunk_id": cid, "status": "committed"})
        except Exception as e:
            results.append({"chunk_id": cid, "status": f"error: {e}"})

    return {
        "success": True, "command": "commit",
        "data": {"results": results},
        "error": None,
        "warning": WARNING_QA_COMMIT,
        "revert": {
            "command": "CALL chunky_qa('REVERT', "
                       "OBJECT_CONSTRUCT('db', '" + db + "', "
                       "'schema', '" + schema + "', "
                       "'table', '" + table + "', "
                       "'timestamp_before', '" + (log.timestamp_before or "") + "'));",
            "timestamp_before": log.timestamp_before,
            "query_ids": log.ids,
        },
        **log.to_dict(),
    }


# ---------------------------------------------------------------------------
# Command: delete
# ---------------------------------------------------------------------------
def cmd_delete(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    chunk_ids = inst.get("chunk_ids", [])
    full_table = _qualify(db, schema, table)

    if not chunk_ids:
        return {
            "success": False, "command": "delete",
            "error": "No chunk_ids provided", "data": None,
            **log.to_dict(),
        }

    id_list = ", ".join(f"'{clean_text_for_sql(c)}'" for c in chunk_ids)
    try:
        log.execute(f"DELETE FROM {full_table} WHERE CHUNK_ID IN ({id_list})")
        return {
            "success": True, "command": "delete",
            "data": {"deleted": len(chunk_ids)},
            "error": None,
            "warning": WARNING_QA_DELETE,
            "revert": {
                "command": "CALL chunky_qa('REVERT', "
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
            "success": False, "command": "delete",
            "error": str(e), "data": None,
            **log.to_dict(),
        }


# ---------------------------------------------------------------------------
# Command: revert
# ---------------------------------------------------------------------------
def cmd_revert(session, inst: Dict[str, Any]) -> Dict:
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    timestamp_before = inst.get("timestamp_before")
    query_ids = inst.get("query_ids", [])
    file = inst.get("file")
    page_range = inst.get("page_range")

    if file and page_range and timestamp_before:
        return revert_rows(
            session, db, schema, table,
            timestamp_before=timestamp_before,
            file=file, page_range=tuple(page_range),
        )
    return revert_table(
        session, db, schema, table,
        timestamp_before=timestamp_before,
        query_ids=query_ids,
    )


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------
def run(session, command, instruction):
    """Main entry point for the chunky_qa procedure."""
    cmd = (command or "").upper()
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
    elif cmd == "REVERT":
        return cmd_revert(session, inst)
    else:
        return {
            "success": False, "command": cmd,
            "error": f"Unknown command: {command}", "data": None,
        }
