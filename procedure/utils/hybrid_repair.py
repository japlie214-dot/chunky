"""
procedure/utils/hybrid_repair.py
Headless port of views/refinery/ingestion_strategies/hybrid.py.

When both Layout and Vision are enabled (Layout+Vision hybrid mode), the
ingestion handler first runs Layout (AI_PARSE_DOCUMENT), then this
module inspects each chunk for quality defects and re-runs Vision on
the defective pages to produce ENHANCED chunks.

Quality rules are in `quality_inspector.py` (verbatim port of the
Streamlit-side QualityInspector).
"""
from __future__ import annotations
import os
import tempfile
import uuid
from typing import Any, Dict, List, Optional

from .quality_inspector import QualityInspector
from .prompts import (
    get_silver_bullet_prompt,
    get_layout_repair_prompt,
)
from ._shared import (
    clean_text_for_sql,
    sanitize_nbsp,
    build_chunk_ref,
)


def _safe_subfolder(file: str) -> str:
    return "".join(c for c in file if c.isalnum() or c in "._-")


def _save_optimized_image(image, output_dir: str, base_filename: str,
                          sub_folder: Optional[str] = None) -> Optional[str]:
    """Save image under 3.5MB for Snowflake Cortex."""
    MAX_IMAGE_MB = 3.5
    try:
        from PIL import Image as PILImage
    except ImportError:
        return None

    if sub_folder:
        safe_sub = _safe_subfolder(sub_folder)
        final_dir = os.path.join(output_dir, safe_sub)
    else:
        final_dir = output_dir
    os.makedirs(final_dir, exist_ok=True)

    png_path = os.path.join(final_dir, f"{base_filename}.png")

    try:
        if hasattr(image, "width") and image.width > 1600:
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


def _run_cortex(session, log, prompt: str, stage_root: str,
                image_path_relative: str, model: str):
    """Execute AI_COMPLETE with an image and capture the query id."""
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


def run_hybrid_repair(session, log, full_table: str, stage_path: str,
                      file: str, page_filter_sql: str,
                      pdf_bytes: bytes, poppler_bin: Optional[str],
                      cortex_model: str,
                      link: str = "") -> Dict[str, Any]:
    """
    Inspect layout-extracted chunks for defects; repair defective ones
    via Vision.

    Returns a metrics dict with repair counts and per-defect breakdown.
    """
    from .constants import TEMP_IMAGE_PREFIX

    # 1. Fetch all chunks for this file
    where_clauses = [f"PDF_NAME = '{clean_text_for_sql(file)}'"]
    if page_filter_sql:
        where_clauses.append(page_filter_sql)
    where_clause = " AND ".join(where_clauses)

    query_sql = (
        f"SELECT CHUNK_ID, PAGE_NUMBER, CHUNK, PDF_NAME, LINK_BLOCK "
        f"FROM {full_table} WHERE {where_clause}"
    )
    try:
        rows = log.execute(query_sql)
    except Exception as e:
        return {"defects_found": 0, "repaired": 0, "error": str(e)}

    if not rows:
        return {"defects_found": 0, "repaired": 0}

    # 2. Identify defects
    defects = []
    for r in rows:
        rd = r.as_dict() if hasattr(r, "as_dict") else dict(r)
        chunk_text = rd.get("CHUNK", "") or ""
        status = QualityInspector.inspect(chunk_text)
        if status != "OK":
            defects.append({
                "chunk_id": rd.get("CHUNK_ID", ""),
                "page_number": int(rd.get("PAGE_NUMBER", 0)),
                "chunk": chunk_text,
                "pdf_name": rd.get("PDF_NAME", file),
                "link_block": rd.get("LINK_BLOCK", ""),
                "status": status,
            })

    metrics: Dict[str, Any] = {
        "defects_found": len(defects),
        "repaired": 0,
        "by_defect_type": {},
    }
    for d in defects:
        metrics["by_defect_type"][d["status"]] = (
            metrics["by_defect_type"].get(d["status"], 0) + 1
        )

    if not defects:
        return metrics

    # 3. Repair each defective page (group by page to avoid re-rendering)
    # Verify poppler is available BEFORE attempting any work.
    if not poppler_bin:
        metrics["error"] = (
            "Hybrid repair requires poppler binaries bundled for the runtime "
            "architecture. The utils_bundle.zip is missing the poppler_bundle/"
            "<arch>/poppler/bin/ directory for this warehouse's architecture. "
            "Rebuild with `python3 procedure/build_bundle.py --clean` (bundles "
            "BOTH arm64 and x86_64 by default) and re-upload to your stage."
        )
        return metrics

    try:
        from pdf2image import convert_from_bytes
    except ImportError as e:
        metrics["error"] = (
            f"pdf2image is not available: {e}. The utils_bundle.zip is missing "
            f"the pdf2image/ package. Rebuild with "
            f"`python3 procedure/build_bundle.py --clean` and re-upload."
        )
        return metrics

    pages_to_repair: Dict[int, List[Dict]] = {}
    for d in defects:
        pages_to_repair.setdefault(d["page_number"], []).append(d)

    for pg_num, page_defects in pages_to_repair.items():
        try:
            imgs = convert_from_bytes(
                pdf_bytes, first_page=pg_num, last_page=pg_num,
                poppler_path=poppler_bin,
            )
        except Exception:
            continue
        if not imgs:
            continue

        with tempfile.TemporaryDirectory() as td:
            img_name = f"repair_p{pg_num}_{uuid.uuid4().hex[:8]}"
            img_path = _save_optimized_image(
                imgs[0], td, img_name, sub_folder=file,
            )
            if not img_path:
                continue

            safe_sub = _safe_subfolder(file)
            full_stage = f"{stage_path}/{TEMP_IMAGE_PREFIX}/{safe_sub}"
            try:
                session.file.put(
                    img_path, full_stage,
                    auto_compress=False, overwrite=True,
                )
            except Exception:
                continue
            rel_img = (
                f"{TEMP_IMAGE_PREFIX}/{safe_sub}/{os.path.basename(img_path)}"
            )

            for d in page_defects:
                link_block = d.get("link_block") or ""
                if link_block:
                    clean_text = d["chunk"].replace(link_block, "", 1).rstrip()
                    quarantined_block = link_block
                else:
                    clean_text = d["chunk"]
                    quarantined_block = ""

                defect_instruction = (
                    f"Fix defect: {d['status']}\n"
                    "IMPORTANT: Do NOT add, invent, or reference any URLs "
                    "in your output."
                )
                if d["status"] == "REPAIR_VISUAL":
                    prompt = get_layout_repair_prompt(clean_text, defect_instruction)
                else:
                    prompt = get_silver_bullet_prompt(clean_text, defect_instruction)

                res_txt, _, _ = _run_cortex(
                    session, log, prompt, stage_path, rel_img, cortex_model,
                )
                if not res_txt:
                    continue

                res_txt = sanitize_nbsp(res_txt)
                if quarantined_block:
                    # Re-append the link block (safe concat)
                    res_txt = res_txt.rstrip() + "\n" + quarantined_block

                c_ref = build_chunk_ref(d["pdf_name"], pg_num, link)
                upd_sql = (
                    f"UPDATE {full_table} "
                    "SET CHUNK = ?, CHUNK_TYPE = 'ENHANCED', CHUNK_REF = ? "
                    "WHERE CHUNK_ID = ?"
                )
                try:
                    log.execute(
                        upd_sql,
                        params=[res_txt, c_ref, d["chunk_id"]],
                    )
                    metrics["repaired"] += 1
                except Exception:
                    continue

    return metrics
