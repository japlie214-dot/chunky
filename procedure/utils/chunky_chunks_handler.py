"""
procedure/utils/chunky_chunks_handler.py
Ingestion Engine. Commands: ingest, list_chunks, list_chunks_csv,
update_chunk, delete_chunks, inspect_quality, batch_ingest,
estimate_cost, revert.

Source (logical): CCS wizard Pages 2-3 + Doc Refinery batch_processor +
ingestion_strategies/{layout,vision,hybrid}.py. This file is the
headless, Streamlit-free version of that logic.

Headless changes vs. the original Streamlit-side code:
  * No `streamlit` imports, no `st.session_state`, no UI fragments.
  * All configuration comes from `instruction` (the caller's JSON).
  * Every SQL operation runs through `QueryLog.execute` so we can
    return query_ids for revert.
  * Warnings are returned in the response AFTER execution (the original
    Streamlit app showed them before; headless callers can't do that).
  * Default extraction strategy is Vision-only (DEFAULT_USE_VISION=True,
    DEFAULT_USE_LAYOUT=False). Layout-only and Layout+Vision (hybrid
    repair) both work as in the Streamlit app.
  * Single `range` parameter replaces the old `scope`+`page_range`
    pair. If `range` is omitted, the entire PDF is ingested.
  * AI_PARSE_DOCUMENT flat-response handling: when the function returns
    {content, metadata} (no `pages` array), the handler splits the
    content by form-feed to reconstruct per-page chunks.
  * Hybrid repair (Layout+Vision) is now implemented headlessly —
    QualityInspector flags defective chunks and Vision re-extracts them.
"""
from __future__ import annotations
import csv
import io
import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ---- Procedure-internal shared modules (bundled in utils_bundle.zip) ----
from .constants import (
    CHUNK_ID_PREFIX,
    CHUNK_INSERT_MAX_CHARS,
    SNOWFLAKE_MAX_STRING_BYTES,
    DEFAULT_CORTEX_MODEL,
    DEFAULT_USE_LAYOUT,
    DEFAULT_USE_VISION,
    LAYOUT_BATCH_SIZE,
    LAYOUT_COST_PER_1K_PAGES,
    PRICING_REGISTRY,
    FALLBACK_VISION_MODEL,
    PROC_CHUNKY_CHUNKS,
    WARNING_INGEST_OVERWRITE,
    WARNING_INGEST_SURGICAL,
    WARNING_INGEST_APPEND,
    WARNING_INGEST_APPEND_DUPLICATE_PAGES,
    WARNING_TABLE_NEWLY_CREATED,
    WARNING_HYBRID_REPAIR,
    WARNING_LAYOUT_FLAT_RESPONSE,
)
from .query_log import QueryLog
from .page_mapping import RangeMapping, RangeMappingEngine
from .metadata_handler import ChunkMetadataHandler
from .revert import revert_table, revert_rows
from .poppler_bootstrap import POPPLER_BIN
from .layout_parse import (
    parse_ai_parse_document_response,
    expected_pages_for_range,
)
from .quality_inspector import QualityInspector
from .hybrid_repair import run_hybrid_repair
from .prompts import get_silver_bullet_prompt, get_vision_extraction_prompt
from ._shared import (
    qualify as _qualify,
    clean_text_for_sql,
    sanitize_nbsp,
    build_chunk_ref,
    format_link_block,
    make_revert_command,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def get_pdf_page_count(pdf_bytes: bytes) -> int:
    """Get PDF page count using pypdf (pure Python)."""
    try:
        from pypdf import PdfReader
        import io as _io
        reader = PdfReader(_io.BytesIO(pdf_bytes))
        return len(reader.pages)
    except Exception:
        return 1


def extract_links_from_bytes(pdf_bytes: bytes, page_number: int) -> List[str]:
    """Extract URLs from a PDF page using pypdf."""
    try:
        from pypdf import PdfReader
        import io as _io
        reader = PdfReader(_io.BytesIO(pdf_bytes))
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
        if hasattr(image, "width") and image.width > 1600:
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
    root = stage_root if stage_root.startswith("@") else f"@{stage_root}"
    safe_prompt = prompt.replace("'", "''")
    safe_root = root.replace("'", "''")
    safe_path = image_path_relative.replace("'", "''")

    sql = (
        "SELECT SNOWFLAKE.CORTEX.AI_COMPLETE("
        f"'{model}', '{safe_prompt}', "
        f"TO_FILE('{safe_root}', '{safe_path}')"
        ") AS RES"
    )
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


# ---------------------------------------------------------------------------
# Command: ingest
# ---------------------------------------------------------------------------
def cmd_ingest(session, inst: Dict[str, Any]) -> Dict:
    """
    Full ingestion pipeline:
      init -> surgical delete -> layout -> vision -> hybrid repair -> grant

    Configuration (instruction JSON):
      Required: db, schema, table, stage_path, file
      Optional:
        mode              — OVERWRITE | APPEND | SURGICAL  (default APPEND)
        range             — [start, end] page range (default: full doc)
        layout            — bool (default False)
        vision            — bool (default True)
        chunk_size        — int (default 8000)
        overlap           — int (default 20)
        link              — str (default "")
        grant_roles       — list[str]
        cortex_model      — str (default DEFAULT_CORTEX_MODEL)
        surgical_range_mappings — list[{source_start, source_end,
                                        replacement_start, replacement_end}]
    """
    t_start = time.time()
    log = QueryLog(session)

    file = inst["file"]
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    stage_path = inst["stage_path"]
    mode = inst.get("mode", "APPEND").upper()
    chunk_sz = int(inst.get("chunk_size", 8000))
    overlap = int(inst.get("overlap", 20))
    # Defaults: Vision-only (matches the Streamlit tab default and keeps
    # cost predictable for first-time callers).
    use_layout = bool(inst.get("layout", DEFAULT_USE_LAYOUT))
    use_vision = bool(inst.get("vision", DEFAULT_USE_VISION))
    link = inst.get("link", "")
    grant_roles = inst.get("grant_roles", []) or []
    surgical_mappings = inst.get("surgical_range_mappings", []) or []
    page_range_raw = inst.get("range")
    cortex_model = inst.get("cortex_model", DEFAULT_CORTEX_MODEL)

    # Normalise page_range to a tuple or None
    if page_range_raw and isinstance(page_range_raw, (list, tuple)) and len(page_range_raw) == 2:
        page_range: Optional[Tuple[int, int]] = (int(page_range_raw[0]), int(page_range_raw[1]))
    else:
        page_range = None

    full_table = _qualify(db, schema, table)
    safe_file = clean_text_for_sql(file)

    metrics: Dict[str, Any] = {
        "start": t_start, "end": None, "duration": 0,
        "layout_pages": 0, "vision_pages": 0,
        "standard_cnt": 0, "enhanced_cnt": 0,
        "placeholder_cnt": 0, "total_pages": 0,
        "hybrid_repair": None,
    }

    warnings: List[str] = []

    # 1. Detect whether the table already exists (so we can warn the
    #    caller that a new table was created).
    table_existed_before = False
    try:
        rows = log.execute(
            "SELECT COUNT(*) AS CNT FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_CATALOG = ? AND TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            params=[db, schema, table],
        )
        table_existed_before = bool(rows and int(rows[0]["CNT"]) > 0)
    except Exception:
        table_existed_before = False

    # 2. Init table (CREATE TABLE IF NOT EXISTS, or CREATE OR REPLACE for OVERWRITE)
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

    table_newly_created = (not table_existed_before)
    if table_newly_created:
        warnings.append(WARNING_TABLE_NEWLY_CREATED)

    # 3. Detect duplicate pages in APPEND mode BEFORE inserting — caller
    #    is warned in the response (we still proceed with the insert so
    #    the caller can revert if it was unintended).
    duplicate_pages: List[int] = []
    if mode == "APPEND":
        try:
            where_parts = [f"RELATIVE_PATH = '{safe_file}'"]
            if page_range:
                where_parts.append(
                    f"PAGE_NUMBER BETWEEN {page_range[0]} AND {page_range[1]}"
                )
            dup_sql = (
                f"SELECT DISTINCT PAGE_NUMBER FROM {full_table} "
                f"WHERE {' AND '.join(where_parts)} ORDER BY PAGE_NUMBER"
            )
            dup_rows = log.execute(dup_sql)
            for r in dup_rows:
                rd = r.as_dict() if hasattr(r, "as_dict") else dict(r)
                pn = rd.get("PAGE_NUMBER")
                if pn is not None:
                    duplicate_pages.append(int(pn))
        except Exception:
            pass
        if duplicate_pages:
            warnings.append(WARNING_INGEST_APPEND_DUPLICATE_PAGES)
            warnings.append(
                "Duplicate PAGE_NUMBERs for this file: "
                + ", ".join(str(p) for p in duplicate_pages[:20])
                + (" ..." if len(duplicate_pages) > 20 else "")
            )

    # 4. Surgical delete (only in SURGICAL mode with mappings)
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

    # 5. Get PDF bytes + page count
    try:
        pdf_bytes = session.file.get_stream(f"{stage_path}/{file}").read()
    except Exception as e:
        return {
            "success": False, "command": "ingest",
            "error": f"Failed to read PDF: {e}", "data": None,
            **log.to_dict(),
        }

    total_pages = get_pdf_page_count(pdf_bytes)

    # 6. Layout extraction (always runs when use_layout=True)
    if use_layout:
        layout_warnings = _run_layout_extraction(
            session, log, inst, full_table, db, schema, table, file,
            stage_path, safe_file, pdf_bytes, total_pages, mode,
            use_vision, link, page_range, surgical_mappings, chunk_sz,
            overlap, metrics,
        )
        warnings.extend(layout_warnings)

    # 7. Vision extraction (standalone — only when vision=True and layout=False)
    if use_vision and not use_layout:
        vision_warnings = _run_vision_extraction(
            session, log, inst, full_table, file, stage_path, pdf_bytes,
            total_pages, mode, link, page_range, surgical_mappings,
            chunk_sz, overlap, cortex_model, metrics,
        )
        warnings.extend(vision_warnings)
        # If vision extraction completely failed (no pages processed),
        # surface that as an error in the response so the caller knows
        # the ingest didn't actually ingest anything.
        if vision_warnings and metrics["vision_pages"] == 0:
            return {
                "success": False, "command": "ingest",
                "error": (
                    "Vision extraction failed for all pages. See warnings for "
                    "details. Common causes: (1) poppler binaries not bundled "
                    "for the runtime architecture — rebuild with "
                    "`python3 procedure/build_bundle.py --clean` (bundles BOTH "
                    "arm64 and x86_64 by default); (2) pdf2image package "
                    "missing from utils_bundle.zip; (3) PDF is corrupted or "
                    "image-only with no renderable content. As a workaround, "
                    "set `vision: false, layout: true` in the instruction JSON."
                ),
                "data": {
                    "table": table, "file": file, "mode": mode,
                    "metrics": metrics,
                    "table_newly_created": table_newly_created,
                    "duplicate_pages": duplicate_pages,
                },
                "warning": " | ".join(warnings) if warnings else None,
                "warnings": warnings,
                **log.to_dict(),
            }

    # 8. Hybrid repair (Layout+Vision: repair defective layout chunks via Vision)
    if use_layout and use_vision:
        page_filter_sql = ""
        if surgical_mappings:
            # The surgical delete already constrained the working set;
            # pass a broader filter so hybrid repair covers all newly
            # inserted rows.
            min_src = min(int(rm["source_start"]) for rm in surgical_mappings)
            max_tgt = max(
                int(rm["source_start"])
                + (int(rm["replacement_end"]) - int(rm["replacement_start"]))
                for rm in surgical_mappings
            )
            page_filter_sql = (
                f"AND PAGE_NUMBER BETWEEN {min_src} AND {max_tgt}"
            )
        elif page_range:
            page_filter_sql = (
                f"AND PAGE_NUMBER BETWEEN {page_range[0]} AND {page_range[1]}"
            )

        repair_metrics = run_hybrid_repair(
            session, log, full_table, stage_path, file, page_filter_sql,
            pdf_bytes, POPPLER_BIN, cortex_model, link,
        )
        metrics["hybrid_repair"] = repair_metrics
        if repair_metrics.get("error"):
            warnings.append(
                f"Hybrid repair skipped: {repair_metrics['error']}"
            )
        elif repair_metrics.get("repaired", 0) > 0:
            warnings.append(WARNING_HYBRID_REPAIR)
            metrics["enhanced_cnt"] += repair_metrics["repaired"]
            # Subtract repaired from standard_cnt (they were converted)
            metrics["standard_cnt"] = max(
                0, metrics["standard_cnt"] - repair_metrics["repaired"]
            )

    # 9. Apply grants
    grant_result = None
    if grant_roles:
        from .grant_table import run as grant_run
        grant_result = grant_run(session, db, schema, table, grant_roles)

    metrics["end"] = time.time()
    metrics["duration"] = metrics["end"] - t_start
    metrics["total_pages"] = metrics["layout_pages"] + metrics["vision_pages"]

    # 10. Post-execution warning (mode-specific)
    if mode == "OVERWRITE":
        warnings.append(WARNING_INGEST_OVERWRITE)
    elif mode == "SURGICAL":
        warnings.append(WARNING_INGEST_SURGICAL)
    else:
        # APPEND: only emit the generic APPEND warning if we didn't
        # already emit the duplicate-pages warning (avoids repetition).
        if not duplicate_pages:
            warnings.append(WARNING_INGEST_APPEND)

    revert_payload = {
        "command": make_revert_command(
            PROC_CHUNKY_CHUNKS, db, schema, table,
            log.timestamp_before, log.ids,
        ),
        "timestamp_before": log.timestamp_before,
        "query_ids": log.ids,
    }

    return {
        "success": True, "command": "ingest",
        "data": {
            "table": table, "file": file, "mode": mode, "metrics": metrics,
            "grant_result": grant_result,
            "table_newly_created": table_newly_created,
            "duplicate_pages": duplicate_pages,
        },
        "error": None,
        "warning": " | ".join(warnings) if warnings else None,
        "warnings": warnings,
        "revert": revert_payload,
        **log.to_dict(),
    }


# ---------------------------------------------------------------------------
# Layout extraction helper (extracted from cmd_ingest)
# ---------------------------------------------------------------------------
def _run_layout_extraction(
    session, log: QueryLog, inst: Dict, full_table: str,
    db: str, schema: str, table: str, file: str,
    stage_path: str, safe_file: str, pdf_bytes: bytes,
    total_pages: int, mode: str, use_vision: bool, link: str,
    page_range: Optional[Tuple[int, int]],
    surgical_mappings: List[Dict],
    chunk_sz: int, overlap: int,
    metrics: Dict[str, Any],
) -> List[str]:
    """Run AI_PARSE_DOCUMENT and insert STANDARD/PLACEHOLDER chunks.

    ALWAYS passes page_filter to AI_PARSE_DOCUMENT. Without page_filter,
    the function returns a flat {content, metadata} response where the
    content has form-feed (\\f) page separators — and if those
    separators are absent (which happens for some PDFs), every page
    collapses into a single chunk saved as page 1.

    By always supplying page_filter, we force the structured {pages: [...]}
    response shape, which guarantees per-page extraction. The page count
    is obtained from the PDF via pypdf (passed in as `total_pages`).
    """
    warnings: List[str] = []

    # Determine page_filter and expected pages.
    # ALWAYS supply page_filter — even for Full Doc scope — so we get
    # the structured {pages: [...]} response instead of flat content.
    page_filters: List[Dict] = []
    if surgical_mappings:
        for rm in surgical_mappings:
            page_filters.append({
                "start": int(rm["replacement_start"]) - 1,
                "end": int(rm["replacement_end"]),
            })
    elif page_range:
        page_filters.append({
            "start": page_range[0] - 1,
            "end": page_range[1],
        })
    elif total_pages and total_pages > 0:
        # Full Doc scope — construct a page_filter covering all pages
        # so AI_PARSE_DOCUMENT returns the per-page array.
        page_filters.append({
            "start": 0,
            "end": total_pages,
        })

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
            return [f"AI_PARSE_DOCUMENT returned NULL for file {file}"]
        raw = raw_res[0]["J"]
        pages_data, meta, used_ff_split = parse_ai_parse_document_response(raw)
        if used_ff_split:
            warnings.append(WARNING_LAYOUT_FLAT_RESPONSE)
            # If we got flat content but the metadata says there are N
            # pages and we only recovered 1, that means the form-feed
            # split didn't find separators. Warn loudly so the caller
            # knows the per-page extraction failed.
            if meta and isinstance(meta, dict):
                meta_page_count = meta.get("pageCount") or meta.get("page_count")
                if meta_page_count and len(pages_data) == 1 and int(meta_page_count) > 1:
                    warnings.append(
                        f"WARNING: AI_PARSE_DOCUMENT returned flat content for "
                        f"a {meta_page_count}-page PDF but no form-feed (\\f) "
                        f"separators were found. All content was saved as page 1. "
                        f"This is a known limitation of AI_PARSE_DOCUMENT for "
                        f"some PDFs — consider using vision=true (Vision-only) "
                        f"or vision=true+layout=true (hybrid) for per-page "
                        f"extraction on this document."
                    )
    except Exception as e:
        return [f"AI_PARSE_DOCUMENT failed: {e}"]

    if not pages_data:
        return ["AI_PARSE_DOCUMENT returned no pages and no content"]

    # Build range_mappings if surgical
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

    page_records: List[Dict] = []
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

    # Compute expected pages
    if range_mappings:
        expected = set()
        for rm in range_mappings:
            expected.update(range(rm.replacement_start, rm.replacement_end + 1))
    elif page_range:
        expected = set(range(page_range[0], page_range[1] + 1))
    else:
        expected = set(range(1, total_pages + 1))

    returned_pages = {int(pg.get("index", 0)) + 1 for pg in pages_data}
    missing = sorted(expected - returned_pages)
    for mp in missing:
        page_records.append({
            "RELATIVE_PATH": file, "PAGE_NUMBER": mp,
            "PAGE_TEXT": f"[Page {mp} — extraction fallback]",
            "LINK_BLOCK": "",
            "CHUNK_REF": build_chunk_ref(file, mp, link),
            "CHUNK_TYPE": "PLACEHOLDER",
        })
        metrics["placeholder_cnt"] += 1

    page_records.sort(key=lambda x: x["PAGE_NUMBER"])

    # Build chunk metadata
    if mode == "SURGICAL" and surgical_mappings and range_mappings:
        per_page_mappings = RangeMappingEngine.to_per_page_mappings(range_mappings)
        for pm in per_page_mappings:
            pm["original_pdf_page"] = pm["source"]
        chunk_metadata = ChunkMetadataHandler.build_surgical_select_metadata(
            original_file=file,
            source_range=(page_range or (1, total_pages)),
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

    batches = [
        page_records[i:i + LAYOUT_BATCH_SIZE]
        for i in range(0, len(page_records), LAYOUT_BATCH_SIZE)
    ]
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
                # Count only STANDARD rows (placeholders are counted separately)
                metrics["standard_cnt"] += sum(
                    1 for r in batch if r["CHUNK_TYPE"] == "STANDARD"
                )
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

    return warnings


# ---------------------------------------------------------------------------
# Vision extraction helper (standalone, when use_vision and not use_layout)
# ---------------------------------------------------------------------------
def _run_vision_extraction(
    session, log: QueryLog, inst: Dict, full_table: str,
    file: str, stage_path: str, pdf_bytes: bytes,
    total_pages: int, mode: str, link: str,
    page_range: Optional[Tuple[int, int]],
    surgical_mappings: List[Dict],
    chunk_sz: int, overlap: int,
    cortex_model: str, metrics: Dict[str, Any],
) -> List[str]:
    """Render each PDF page to an image and call Vision AI to extract content.

    Returns a list of warning/error strings. An empty list means everything
    succeeded with no warnings.
    """
    from .constants import TEMP_IMAGE_PREFIX
    warnings: List[str] = []

    if surgical_mappings:
        target_range: List[int] = []
        for rm in surgical_mappings:
            target_range.extend(
                range(int(rm["replacement_start"]), int(rm["replacement_end"]) + 1)
            )
    elif page_range:
        target_range = list(range(page_range[0], page_range[1] + 1))
    else:
        target_range = list(range(1, total_pages + 1))

    # Verify poppler is available BEFORE attempting any work — fail fast
    # with a descriptive error if it's missing.
    try:
        from .poppler_bootstrap import get_poppler_bin_or_raise, POPPLER_AVAILABLE, POPPLER_ARCH
        if not POPPLER_AVAILABLE:
            return [
                f"Vision extraction requires poppler binaries bundled for the "
                f"runtime architecture (detected: {POPPLER_ARCH}). The "
                f"utils_bundle.zip is missing poppler_bundle/{POPPLER_ARCH}/poppler/bin/. "
                f"Rebuild the bundle with `python3 procedure/build_bundle.py --clean` "
                f"(which bundles BOTH arm64 and x86_64 by default) and re-upload to "
                f"your stage. As a workaround, set `vision: false, layout: true` in "
                f"the instruction JSON to use Layout-only ingestion."
            ]
        poppler_bin = get_poppler_bin_or_raise()
    except RuntimeError as e:
        return [str(e)]

    # Import pdf2image — this MUST succeed because poppler_bootstrap added
    # the udf root to sys.path at import time. If it fails, the bundle is
    # broken (pdf2image package missing from utils_bundle.zip).
    try:
        from pdf2image import convert_from_bytes
    except ImportError as e:
        return [
            f"pdf2image is not available: {e}. The utils_bundle.zip is missing "
            f"the pdf2image/ package. Rebuild the bundle with "
            f"`python3 procedure/build_bundle.py --clean` and re-upload to "
            f"your stage. As a workaround, set `vision: false, layout: true` "
            f"in the instruction JSON to use Layout-only ingestion."
        ]

    pages_skipped = 0
    pages_processed = 0
    for pg in target_range:
        try:
            imgs = convert_from_bytes(
                pdf_bytes, first_page=pg, last_page=pg,
                poppler_path=poppler_bin,
            )
        except Exception as e:
            # pdf2image failed — most likely poppler binary architecture
            # mismatch (e.g. x86 binary on ARM warehouse) or corrupt PDF.
            warnings.append(
                f"Page {pg}: pdf2image render failed: {e}. "
                f"Check that the bundled poppler binaries match the "
                f"warehouse architecture (detected: {POPPLER_ARCH})."
            )
            pages_skipped += 1
            continue
        if not imgs:
            warnings.append(f"Page {pg}: pdf2image returned no image (skipped).")
            pages_skipped += 1
            continue

        with _tempdir() as td:
            img_name = f"vis_{uuid.uuid4().hex[:8]}_{pg}"
            img_path = save_optimized_image(imgs[0], td, img_name, sub_folder=file)
            if not img_path:
                warnings.append(f"Page {pg}: image optimisation failed (skipped).")
                pages_skipped += 1
                continue

            safe_sub = "".join(c for c in file if c.isalnum() or c in "._-")
            full_stage = f"{stage_path}/{TEMP_IMAGE_PREFIX}/{safe_sub}"
            try:
                session.file.put(
                    img_path, full_stage, auto_compress=False, overwrite=True,
                )
            except Exception as e:
                warnings.append(f"Page {pg}: stage upload failed: {e} (skipped).")
                pages_skipped += 1
                continue
            rel_img = f"{TEMP_IMAGE_PREFIX}/{safe_sub}/{os.path.basename(img_path)}"

            prompt = get_vision_extraction_prompt()
            res_txt, _, _ = run_cortex(
                session, log, prompt, stage_path, rel_img, cortex_model,
            )
            if not res_txt:
                warnings.append(
                    f"Page {pg}: Vision AI returned empty response (skipped)."
                )
                pages_skipped += 1
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
            try:
                log.execute(
                    ins_sql,
                    params=[file, pg, link_block, link_block, c_ref, link_block,
                            chunk_meta, res_txt],
                )
                metrics["vision_pages"] += 1
                metrics["enhanced_cnt"] += 1
                pages_processed += 1
            except Exception as e:
                warnings.append(f"Page {pg}: insert failed: {e} (skipped).")
                pages_skipped += 1
                continue

    if pages_skipped > 0:
        warnings.append(
            f"Vision extraction: {pages_processed} pages processed, "
            f"{pages_skipped} pages skipped (out of {len(target_range)} total). "
            f"See the per-page warnings above for details."
        )

    return warnings


# ---------------------------------------------------------------------------
# Command: list_chunks (also supports CSV output via list_chunks_csv)
# ---------------------------------------------------------------------------
def cmd_list_chunks(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    full_table = _qualify(db, schema, table)

    where = []
    if inst.get("file"):
        if isinstance(inst["file"], list):
            in_list = ", ".join(
                f"'{clean_text_for_sql(f)}'" for f in inst["file"] if f
            )
            if in_list:
                where.append(f"RELATIVE_PATH IN ({in_list})")
        else:
            where.append(f"RELATIVE_PATH = '{clean_text_for_sql(inst['file'])}'")
    if inst.get("range"):
        pr = inst["range"]
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
            rd = r.as_dict() if hasattr(r, "as_dict") else dict(r)
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


def cmd_list_chunks_csv(session, inst: Dict[str, Any]) -> Dict:
    """Same as list_chunks but returns a single CSV string in data.csv."""
    result = cmd_list_chunks(session, inst)
    if not result.get("success"):
        return result
    chunks = result["data"]["chunks"]
    out = io.StringIO()
    writer = csv.writer(out, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "chunk_id", "page_number", "chunk_type", "relative_path",
        "chunk_ref", "link_block", "chunk",
    ])
    for c in chunks:
        writer.writerow([
            c["chunk_id"], c["page_number"], c["chunk_type"],
            c["relative_path"], c["chunk_ref"], c["link_block"],
            c["chunk"],
        ])
    return {
        "success": True, "command": "list_chunks_csv",
        "data": {
            "csv": out.getvalue(),
            "row_count": len(chunks),
        },
        "error": None,
        **{k: v for k, v in result.items() if k in ("query_ids", "timestamp_before")},
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
                "command": make_revert_command(
                    PROC_CHUNKY_CHUNKS, db, schema, table,
                    log.timestamp_before, log.ids,
                ),
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
    if inst.get("range"):
        pr = inst["range"]
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
                "command": make_revert_command(
                    PROC_CHUNKY_CHUNKS, db, schema, table,
                    log.timestamp_before, log.ids,
                ),
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
# Command: inspect_quality
# ---------------------------------------------------------------------------
def cmd_inspect_quality(session, inst: Dict[str, Any]) -> Dict:
    """
    Inspect chunks for quality defects (using QualityInspector) without
    modifying them. Returns per-chunk defect status so the caller can
    decide whether to run a hybrid repair or a targeted update.
    """
    log = QueryLog(session)
    table = inst["table"]
    db = inst["db"]
    schema = inst["schema"]
    full_table = _qualify(db, schema, table)

    where = []
    if inst.get("file"):
        where.append(f"RELATIVE_PATH = '{clean_text_for_sql(inst['file'])}'")
    if inst.get("range"):
        pr = inst["range"]
        where.append(f"PAGE_NUMBER BETWEEN {int(pr[0])} AND {int(pr[1])}")
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    sql = (
        f"SELECT CHUNK_ID, PAGE_NUMBER, CHUNK, CHUNK_TYPE, RELATIVE_PATH "
        f"FROM {full_table} {where_clause} "
        f"ORDER BY PAGE_NUMBER"
    )
    try:
        rows = log.execute(sql)
    except Exception as e:
        return {
            "success": False, "command": "inspect_quality",
            "error": str(e), "data": None,
            **log.to_dict(),
        }

    findings: List[Dict] = []
    defect_breakdown: Dict[str, int] = {}
    for r in rows:
        rd = r.as_dict() if hasattr(r, "as_dict") else dict(r)
        chunk_text = rd.get("CHUNK", "") or ""
        status = QualityInspector.inspect(chunk_text)
        findings.append({
            "chunk_id": rd.get("CHUNK_ID", ""),
            "page_number": rd.get("PAGE_NUMBER", 0),
            "relative_path": rd.get("RELATIVE_PATH", ""),
            "chunk_type": rd.get("CHUNK_TYPE", "STANDARD"),
            "status": status,
            "chunk_length": len(chunk_text),
        })
        if status != "OK":
            defect_breakdown[status] = defect_breakdown.get(status, 0) + 1

    return {
        "success": True, "command": "inspect_quality",
        "data": {
            "findings": findings,
            "total": len(findings),
            "defects": sum(v for k, v in defect_breakdown.items() if k != "OK"),
            "defect_breakdown": defect_breakdown,
        },
        "error": None,
        **log.to_dict(),
    }


# ---------------------------------------------------------------------------
# Command: batch_ingest — run multiple ingest jobs in one CALL
# ---------------------------------------------------------------------------
def cmd_batch_ingest(session, inst: Dict[str, Any]) -> Dict:
    """
    Run multiple ingest jobs in a single CALL. Each job uses the same
    instruction schema as cmd_ingest. Useful for bulk-loading multiple
    PDFs without round-tripping per file.
    """
    jobs = inst.get("jobs") or []
    if not jobs:
        return {
            "success": False, "command": "batch_ingest",
            "error": "No jobs provided in instruction.jobs",
            "data": None,
        }
    results = []
    successes = 0
    failures = 0
    for j in jobs:
        r = cmd_ingest(session, j)
        results.append({
            "file": j.get("file"),
            "table": j.get("table"),
            "success": r.get("success", False),
            "error": r.get("error"),
            "warning": r.get("warning"),
            "metrics": r.get("data", {}).get("metrics") if r.get("data") else None,
        })
        if r.get("success"):
            successes += 1
        else:
            failures += 1
    return {
        "success": failures == 0, "command": "batch_ingest",
        "data": {
            "results": results,
            "total": len(results),
            "successes": successes,
            "failures": failures,
        },
        "error": None,
        "warning": (
            f"Batch ingest complete: {successes} succeeded, {failures} failed."
            if failures else None
        ),
    }


# ---------------------------------------------------------------------------
# Command: estimate_cost — pre-flight cost estimate (no ingestion)
# ---------------------------------------------------------------------------
def cmd_estimate_cost(session, inst: Dict[str, Any]) -> Dict:
    """
    Estimate the credit/USD cost of an ingest job BEFORE running it.
    Uses the PRICING_REGISTRY to compute Vision costs; Layout cost is a
    flat per-1k-pages rate.
    """
    log = QueryLog(session)
    file = inst["file"]
    stage_path = inst["stage_path"]
    use_layout = bool(inst.get("layout", DEFAULT_USE_LAYOUT))
    use_vision = bool(inst.get("vision", DEFAULT_USE_VISION))
    cortex_model = inst.get("cortex_model", DEFAULT_CORTEX_MODEL)
    page_range_raw = inst.get("range")

    # Get PDF bytes + page count (read-only)
    try:
        pdf_bytes = session.file.get_stream(f"{stage_path}/{file}").read()
    except Exception as e:
        return {
            "success": False, "command": "estimate_cost",
            "error": f"Failed to read PDF: {e}", "data": None,
            **log.to_dict(),
        }
    total_pages = get_pdf_page_count(pdf_bytes)

    if page_range_raw and len(page_range_raw) == 2:
        page_count = max(0, int(page_range_raw[1]) - int(page_range_raw[0]) + 1)
    else:
        page_count = total_pages

    # Layout cost (flat per 1k pages)
    layout_credits = 0.0
    if use_layout:
        layout_credits = (page_count / 1000) * LAYOUT_COST_PER_1K_PAGES

    # Vision cost (rough token estimate)
    vision_usd = 0.0
    vision_input_tokens = 0
    vision_output_tokens = 0
    if use_vision:
        # Heuristic: each page → ~1500 input tokens (image) + ~1200 output tokens
        vision_input_tokens = page_count * 1500
        vision_output_tokens = page_count * 1200
        pricing = PRICING_REGISTRY.get(cortex_model) or PRICING_REGISTRY.get(FALLBACK_VISION_MODEL, {"input": 0.80, "output": 4.00})
        vision_usd = (
            (vision_input_tokens / 1_000_000) * pricing["input"]
            + (vision_output_tokens / 1_000_000) * pricing["output"]
        )

    return {
        "success": True, "command": "estimate_cost",
        "data": {
            "file": file,
            "total_pages_in_pdf": total_pages,
            "pages_to_process": page_count,
            "strategies": {
                "layout": use_layout,
                "vision": use_vision,
                "hybrid_repair": use_layout and use_vision,
            },
            "estimated_cost": {
                "layout_credits": round(layout_credits, 4),
                "vision_input_tokens": vision_input_tokens,
                "vision_output_tokens": vision_output_tokens,
                "vision_usd": round(vision_usd, 4),
                "cortex_model": cortex_model if use_vision else None,
            },
            "note": (
                "Vision token estimates are heuristic (1500 in / 1200 out per page). "
                "Actual usage may vary based on page complexity."
            ),
        },
        "error": None,
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
    page_range_raw = inst.get("range") or inst.get("page_range")

    # Row-scoped revert if both file and range supplied
    if file and page_range_raw and timestamp_before:
        return revert_rows(
            session, db, schema, table,
            timestamp_before=timestamp_before,
            file=file,
            page_range=tuple(page_range_raw),
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
    elif cmd == "LIST_CHUNKS_CSV":
        return cmd_list_chunks_csv(session, inst)
    elif cmd == "UPDATE_CHUNK":
        return cmd_update_chunk(session, inst)
    elif cmd == "DELETE_CHUNKS":
        return cmd_delete_chunks(session, inst)
    elif cmd == "INSPECT_QUALITY":
        return cmd_inspect_quality(session, inst)
    elif cmd == "BATCH_INGEST":
        return cmd_batch_ingest(session, inst)
    elif cmd == "ESTIMATE_COST":
        return cmd_estimate_cost(session, inst)
    elif cmd == "REVERT":
        return cmd_revert(session, inst)
    else:
        return {
            "success": False, "command": cmd,
            "error": f"Unknown command: {command}", "data": None,
        }
