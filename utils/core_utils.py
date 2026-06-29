# utils/core_utils.py
import os
import shutil
import re
import statistics
import json
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from logger_config import log_action
from utils.constants import (
    CREDIT_TO_USD, USD_TO_IDR, CREDIT_TO_IDR, RATE_AI_CLASSIFY, LABEL_DEFINITIONS
)

import prompts

# Safe Import: pdf2image
try:
    from pdf2image import pdfinfo_from_bytes, convert_from_bytes
except Exception:
    pdfinfo_from_bytes = None
    convert_from_bytes = None

# Safe Import: pypdf
try:
    from pypdf import PdfReader
    import io
    PYPDF_AVAILABLE = True
except ImportError:
    PdfReader = None
    PYPDF_AVAILABLE = False

# Safe Import: PIL
try:
    from PIL import Image
except Exception:
    Image = None

# Standard Library Import: difflib
import difflib
import io

# Safe Import: mistletoe for improved table validation
try:
    import mistletoe
    from mistletoe.block_token import Table, TableRow
    from mistletoe import Document
    MISTLETOE_AVAILABLE = True
except ImportError:
    MISTLETOE_AVAILABLE = False
    mistletoe = None
    Document = None
    Table = None
    TableRow = None

# -----------------------------------------------------------------------------
# HELPER FUNCTIONS
# -----------------------------------------------------------------------------

def get_token_count(text: str) -> int:
    """Fallback token count approximation for turns without usage data."""
    return len(text) // 4


def get_classify_input_tokens(session, input_text: str, categories) -> int:
    """Get precise input token count for AI_CLASSIFY by passing categories."""
    try:
        categories_json = json.dumps(categories)
        sql = "SELECT SNOWFLAKE.CORTEX.AI_COUNT_TOKENS('ai_classify', ?, PARSE_JSON(?))"
        res = session.sql(sql, params=[input_text, categories_json]).collect()
        return int(res[0][0])
    except Exception as e:
        st.warning("⚠️ AI_COUNT_TOKENS for Classify failed. Using approximation.")
        st.write(f"Debug Error: {e}")
        return len(input_text) // 4


def render_gauge(group_name: str, score_value: float):
    """Render a gauge chart for severity score"""
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score_value * 100,
        domain={'x': [0, 1], 'y': [0, 1]},
        title={'text': group_name, 'font': {'size': 12}},
        gauge={
            'axis': {'range': [None, 100], 'tickwidth': 1, 'tickcolor': "darkgray"},
            'bar': {"color": "#00CC96" if score_value * 100 < 75 else "#EF553B"},
            'bgcolor': "white",
            'borderwidth': 2,
            'bordercolor': "gray",
            'steps': [
                {'range': [0, 25], 'color': '#00CC96'},
                {'range': [25, 50], 'color': '#FAB81A'},
                {'range': [50, 75], 'color': '#FF6692'},
                {'range': [75, 100], 'color': '#EF553B'}
            ],
            'threshold': {
                'line': {'color': "red", 'width': 4},
                'thickness': 0.75,
                'value': 90
            }
        }
    ))
    fig.update_layout(height=200, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, use_container_width=True)


def display_cost_card(label: str, credits_value: float, delta_str: str = None, help_text: str = None):
    """
    Canonical financial cost renderer. Computes USD and IDR from central constants.
    Replaces st.metric for all cost-bearing values. Never calls st.metric.
    CSS-contained for safe rendering inside narrow st.columns grids.
    """
    import math
    import streamlit as st

    # None/NaN guard — must check None before isnan (isnan raises on None)
    if credits_value is None or math.isnan(credits_value):
        credits_value = 0.0

    usd_val = credits_value * CREDIT_TO_USD
    idr_val = credits_value * CREDIT_TO_IDR

    delta_html = (
        f"<span style='float:right;font-size:0.85em;color:#888;'>{delta_str}</span>"
        if delta_str else ""
    )

    help_html = f"<div style='width:100%;font-size:0.75em;color:#999;margin-top:2px;word-break:break-word;'>{help_text}</div>" if help_text else ""
    st.markdown(f"""
<div style="box-sizing:border-box;width:100%;max-width:100%;overflow:hidden;padding:12px 15px;border:1px solid #e0e0e0;border-radius:8px;background-color:#ffffff;">
    <div style="width:100%;font-size:0.9em;color:#555;word-break:break-word;">
        {label}{delta_html}{help_html}
    </div>
    <div style="width:100%;margin-top:4px;font-size:1.5em;font-weight:700;color:#111;word-break:break-word;">
        {credits_value:.4f} Cr
    </div>
    <div style="width:100%;margin-top:2px;font-size:0.85em;font-weight:600;color:#1B5E20;word-break:break-word;">
        USD ${usd_val:.4f}&nbsp;&nbsp;|&nbsp;&nbsp;Rp {idr_val:,.0f}
    </div>
</div>
    """, unsafe_allow_html=True)


def get_sf_literal(data) -> str:
    """Prepare Snowflake-native SQL string literals for JSON data"""
    # Maintain valid JSON with double quotes and wrap in single quotes for SQL
    return "\'" + json.dumps(data).replace("'", "''") + "'"

def sanitize_nbsp(text: str) -> str:
    """
    Removes ALL &nbsp; entities (named and numeric) from text.
    Handles: &nbsp;, &#160;, &#xa0;, &#XA0;
    """
    if not text:
        return text
    return re.sub(r'&nbsp;|&#160;|&#x[aA]0;', ' ', text)


# -----------------------------------------------------------------------------
# PLAN-08: ADDITIONAL UTILITY CLASSES
# -----------------------------------------------------------------------------

class PDFUtils:
    """Utilities for PDF metadata and file hygiene."""

    @staticmethod
    def get_page_count(pdf_bytes):
        """Extracts page count efficiently without rendering images.

        Uses pypdf (pure Python, reliable page count) as primary.
        Falls back to poppler (pdfinfo_from_bytes) if pypdf unavailable.
        Cross-validates when both are available and logs mismatches.
        """
        if not pdf_bytes:
            log_action("PDF_PAGE_COUNT_EMPTY", "Received empty bytes for page count.")
            return 1

        pypdf_count = None
        poppler_count = None

        # --- Strategy 1: pypdf (pure Python, reliable for page count) ---
        if PYPDF_AVAILABLE and PdfReader is not None:
            try:
                reader = PdfReader(io.BytesIO(pdf_bytes))
                pypdf_count = len(reader.pages)
            except Exception as e:
                log_action("PDF_PAGE_COUNT_PYPDF_FAILED", {"error": str(e)}, level="WARNING")

        # --- Strategy 2: poppler (pdfinfo_from_bytes) ---
        if pdfinfo_from_bytes is not None:
            try:
                info = pdfinfo_from_bytes(pdf_bytes)
                poppler_count = info.get('Pages', 1)
            except Exception as e:
                log_action("PDF_PAGE_COUNT_POPPLER_FAILED", {"error": str(e)}, level="WARNING")

        # --- Cross-validation: log mismatch, prefer pypdf ---
        if pypdf_count is not None and poppler_count is not None:
            if pypdf_count != poppler_count:
                log_action("PDF_PAGE_COUNT_MISMATCH", {
                    "pypdf": pypdf_count, "poppler": poppler_count,
                    "using": "pypdf"
                }, level="WARNING")

        if pypdf_count is not None and pypdf_count >= 1:
            return pypdf_count
        if poppler_count is not None and poppler_count >= 1:
            return poppler_count

        # --- Both strategies failed ---
        log_action("PDF_PAGE_COUNT_ALL_FAILED", "Both pypdf and poppler failed. Defaulting to 1.", level="ERROR")
        return 1

    @staticmethod
    def get_safe_folder(name: str) -> str:
        """
        Centralized sanitization for folder names.
        Returns a sanitized version of the name safe for file paths.
        """
        if not name:
            return "default"
        return "".join(c for c in name if c.isalnum() or c in "._-")

    @staticmethod
    def clear_temp_images(stage_path_root):
        """Cleans up local temp directories to prevent bloat."""
        try:
            import tempfile
            local_temp_base = os.path.join(tempfile.gettempdir(), "rag_app_temp")
            temp_dirs = [
                os.path.join(local_temp_base, "_temp_images"),
                os.path.join(local_temp_base, "_temp_audit")
            ]
            for path_to_rm in temp_dirs:
                if os.path.exists(path_to_rm):
                    shutil.rmtree(path_to_rm, ignore_errors=True)
        except Exception as e:
            try:
                log_action("PDFUTILS_CLEANUP_ERROR", {"error": str(e)})
            except Exception:
                print(f"Cleanup warning: {e}")

    @staticmethod
    def extract_links_from_bytes(pdf_bytes, page_number: int) -> list:
        """Extracts URLs from a specific 1-based page using pypdf with a 90% area filter."""
        if not PYPDF_AVAILABLE:
            log_action("PYPDF_NOT_AVAILABLE", "Hyperlink extraction disabled.")
            return []
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            if page_number < 1 or page_number > len(reader.pages):
                return []
            page = reader.pages[page_number - 1]
            page_area = float(page.mediabox.width) * float(page.mediabox.height)
            if page_area == 0:
                return []
            
            urls = []
            if "/Annots" in page:
                for annot_ref in page["/Annots"]:
                    annot = annot_ref.get_object()
                    if annot.get("/Subtype") == "/Link" and "/A" in annot:
                        action = annot["/A"].get_object()
                        if action.get("/S") == "/URI" and "/URI" in action:
                            url = action["/URI"]
                            rect = annot.get("/Rect")
                            if rect and len(rect) == 4:
                                width = abs(float(rect[2]) - float(rect[0]))
                                height = abs(float(rect[3]) - float(rect[1]))
                                annot_area = width * height
                                if (annot_area / page_area) > 0.90:
                                    log_action("WHOLE_PAGE_LINK_FILTERED", {"url": url})
                                    continue
                            if url not in urls:
                                urls.append(url)
            return urls
        except Exception as e:
            log_action("LINK_EXTRACTION_ERROR", {"error": str(e)})
            return []

    @staticmethod
    def format_link_block(urls: list) -> str:
        if not urls:
            return ""
        lines = "\n".join(f"  - {u}" for u in urls)
        return f"\n\n[External links:\n{lines}\n]"

    @staticmethod
    def strip_link_block(text: str) -> tuple:
        import re
        m = re.search(r'\n\n\[External links:\n.*?\n\]$', text, re.DOTALL)
        if m:
            return text[:m.start()], m.group(0)
        return text, ""

    @staticmethod
    def safe_concat(chunk_text: str, link_block: str) -> str:
        MAX_SAFE_LENGTH = 15_000_000
        combined = chunk_text + link_block
        if len(combined.encode("utf-8")) > MAX_SAFE_LENGTH:
            return chunk_text + "\n\n[External links: ... truncated — see original PDF]"
        return combined


# All hardcoded prompts have been centralized in the prompts.py module
# Use prompts.get_silver_bullet_prompt(), prompts.get_vision_extraction_prompt(), etc.

# Re-export the entire prompts module for backward compatibility
PromptEngine = prompts


def save_optimized_image(image, output_dir, base_filename, sub_folder=None):
    """
    Saves an image ensuring it is strictly under the MB limit for Snowflake Cortex.
    Strategy: Resize -> PNG -> JPEG Fallback -> Iterative Compression.
    Returns path to saved file or None.
    Supports hierarchical storage via sub_folder.
    """
    MAX_IMAGE_MB = 3.5  # Cortex limit
    if Image is None:
        return None
    
    # Handle Sub-folder logic using centralized sanitization
    if sub_folder:
        # Use centralized sanitization to ensure consistency across the app
        safe_sub = PDFUtils.get_safe_folder(sub_folder)
        final_dir = os.path.join(output_dir, safe_sub)
    else:
        final_dir = output_dir
        
    os.makedirs(final_dir, exist_ok=True)
    
    png_path = os.path.join(final_dir, f"{base_filename}.png")
    jpg_path = os.path.join(final_dir, f"{base_filename}.jpg")

    try:
        # 1. Resize if too wide
        if hasattr(image, 'width') and image.width > 1600:
            ratio = 1600 / image.width
            new_height = int(image.height * ratio)
            image = image.resize((1600, new_height), Image.Resampling.LANCZOS)

        # 2. Try PNG first
        image.save(png_path, format="PNG", optimize=True)
        if (os.path.getsize(png_path) / (1024 * 1024)) < MAX_IMAGE_MB:
            return png_path

        # Remove and try JPEG fallback
        try:
            os.remove(png_path)
        except Exception:
            pass

        # Convert to RGB for JPEG
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")

        # 3. JPEG fallback with iterative compression
        quality = 95
        while True:
            image.save(jpg_path, format="JPEG", quality=quality, optimize=True)
            if (os.path.getsize(jpg_path) / (1024 * 1024)) < MAX_IMAGE_MB:
                return jpg_path
            quality -= 10
            if quality < 10:
                return jpg_path
    except Exception as e:
        log_action("IMAGE_SAVE_ERROR", {"error": str(e)})
        return None


class RAGAnalytics:
    """
    Advanced RAG testing utilities for performance analysis and cost tracking.
    Uses Snowflake Credit Table 6(a) logic (1 Credit = $3.71 USD).
    """
    CREDIT_PRICE_USD = 3.71
    
    # Pricing Registry (Credits per 1M tokens)
    PRICING_REGISTRY = {
        'claude-sonnet-4-6': {'input': 1.65, 'output': 8.25},
        'claude-haiku-4-5':  {'input': 0.60, 'output': 3.00},
        'deepseek-r1':       {'input': 0.68, 'output': 2.70},
        'openai-gpt-4.1':    {'input': 1.00, 'output': 4.00},
        'openai-gpt-5':      {'input': 0.69, 'output': 5.50}
    }

    @staticmethod
    def calculate_cost_from_tokens(model_name, input_tokens, output_tokens):
        # Default pricing updated to match the primary model (claude-sonnet-4-6)
        pricing = RAGAnalytics.PRICING_REGISTRY.get(model_name, {'input': 1.65, 'output': 8.25})
        input_credits = (input_tokens / 1_000_000) * pricing['input']
        output_credits = (output_tokens / 1_000_000) * pricing['output']
        total_credits = input_credits + output_credits
        return {
            'model': model_name,
            'total_credits': total_credits,
            'total_cost': total_credits * RAGAnalytics.CREDIT_PRICE_USD
        }

    @staticmethod
    def compare_texts_xray(original, generated):
        """Perform X-Ray text comparison using difflib."""
        if not original or not generated: return {'error': 'Empty text'}
        
        ratio = difflib.SequenceMatcher(None, original, generated).ratio()
        differ = difflib.unified_diff(
            original.splitlines(), generated.splitlines(),
            fromfile='Source', tofile='Generated', lineterm=''
        )
        return {
            'similarity_ratio': ratio,
            'diff_text': '\n'.join(differ)
        }


class QualityInspector:
    """Static forensic tools to detect low-quality chunks requiring AI reconstruction."""

    @staticmethod
    def check_nbsp_chain(text, min_chain=5):
        """Detects chains of 5+ consecutive &nbsp; entities in text."""
        if not text:
            return False
        nbsp_entity = r'(?:&nbsp;|&#160;|&#x[aA]0;)'
        pattern = nbsp_entity + r'(?:\s*' + nbsp_entity + r'){4,}'
        return bool(re.search(pattern, text))

    @staticmethod
    def check_repetition(text):
        """Detects parsing loops via Token Entropy."""
        if not text or len(text) < 100:
            return False
        tokens = text.split()
        if len(tokens) < 10:
            return False
        unique_ratio = len(set(tokens)) / len(tokens)
        return unique_ratio < 0.20

    @staticmethod
    def check_syntax_noise(text):
        """Detects LaTeX commands and unnecessary escapes."""
        if not text:
            return False
        if re.search(r'\\[a-zA-Z]+', text):
            return True
        if re.search(r'\\[%$&_#]', text):
            return True
        return False

    @staticmethod
    def check_phantom_spaces(text):
        """Detects broken numeric formatting (e.g. '1. 000') with high-context awareness."""
        if not text:
            return False
        lines = text.split('\n')
        rgx_table = re.compile(r'(?<!\d)\d{1,3}[.,]\s+\d{3}\b')
        rgx_narrative_dot = re.compile(r'(?<!\d)\d{1,3}\.\s+\d{3}\b')
        rgx_narrative_comma = re.compile(r'(?<![\d\-\/\(\#])\d{1,3},\s+\d{3}\b')
        for line in lines:
            if '|' in line:
                if rgx_table.search(line):
                    return True
            else:
                if rgx_narrative_dot.search(line):
                    return True
                if rgx_narrative_comma.search(line):
                    return True
        return False

    @staticmethod
    def check_table_health(text):
        """Validates structure using Mistletoe AST (if available) and fallback regex."""
        if not text:
            return False
        
        # Try Mistletoe AST validation first
        if MISTLETOE_AVAILABLE:
            try:
                doc = mistletoe.Document(text)
                for node in doc.children:
                    if isinstance(node, Table):
                        if not node.children or not isinstance(node.children[0], TableRow):
                            return "MISSING_HEADER"
                        rows = node.children[1:]
                        if not rows: return "GHOST_TABLE"
                        # Check column alignment
                        header_cols = len(node.children[0].children)
                        for row in rows:
                            if isinstance(row, TableRow) and len(row.children) != header_cols:
                                return "MISALIGNED_COLUMNS"
            except Exception:
                # Fall back to regex-based validation
                pass
        
        # Fallback regex-based validation
        lines = text.split('\n')
        pipe_lines = [line for line in lines if '|' in line]
        if len(pipe_lines) < 3:
            return False
        empty_rows = [line for line in pipe_lines if re.match(r'^\s*(\|\s*)+\|?\s*$', line)]
        if len(empty_rows) > (len(pipe_lines) * 0.5):
            return "GHOST_TABLE"
        has_separator = any(re.search(r'\|[\s-]*:?-+[\s-]*:?\|', line) for line in pipe_lines)
        if not has_separator:
            return "MISSING_HEADER"
        pipe_counts = [line.count('|') for line in pipe_lines]
        if not pipe_counts:
            return False
        median_pipes = statistics.median(pipe_counts)
        misaligned_count = sum(1 for c in pipe_counts if abs(c - median_pipes) > 1)
        if misaligned_count > (len(pipe_lines) * 0.3):
            return "MISALIGNED_COLUMNS"
        return False

    @staticmethod
    def inspect(text):
        """Master function to return the primary defect type."""
        if not text:
            return "EMPTY"
        if len(text) < 500:
            return "REPAIR_LOW_INFO"
        if "![" in text:
            return "REPAIR_VISUAL"
        if QualityInspector.check_nbsp_chain(text):
            return "REPAIR_NBSP_CHAIN"
        if QualityInspector.check_repetition(text):
            return "REPAIR_LOOP"
        table_status = QualityInspector.check_table_health(text)
        if table_status:
            return f"REPAIR_TABLE_{table_status}"
        if QualityInspector.check_phantom_spaces(text):
            return "REPAIR_NUMBERS"
        if QualityInspector.check_syntax_noise(text):
            return "REPAIR_SYNTAX"
        return "OK"


# -----------------------------------------------------------------------------
# PLAN-02: SQL TEXT UTILITIES (Moved from snowflake_utils.py to break circular import)
# -----------------------------------------------------------------------------

def clean_text_for_sql(text: str) -> str:
    """
    Escapes single quotes for SQL safety while preserving newlines and tabs.
    
    Args:
        text: Input text to sanitize
        
    Returns:
        Sanitized text safe for SQL string literals
    """
    if not text:
        return ""
    safe = text.replace("'", "''")
    # Remove non-printable/control characters but preserve newlines, tabs, and carriage returns
    safe = ''.join(ch for ch in safe if ch.isprintable() or ch in ("\n", "\r", "\t"))
    return safe


def to_sql_literal(value) -> str:
    """
    Convert a Python value (str/list/dict) to a Snowflake SQL constant literal expression.
    
    Args:
        value: Python value to convert (str, list, dict, int, float, bool, None)
        
    Returns:
        SQL literal string representation
        
    Raises:
        ValueError: If value type is not supported
    """
    if isinstance(value, str):
        escaped = clean_text_for_sql(value)
        return f"'{escaped}'"
    elif value is None:
        return "NULL"
    elif isinstance(value, (int, float)):
        return str(value)
    elif isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    elif isinstance(value, list):
        if not value:
            return "[]"
        items = ", ".join(to_sql_literal(item) for item in value)
        return f"[{items}]"
    elif isinstance(value, dict):
        if not value:
            return "{}"
        items = ", ".join(f"'{k}': {to_sql_literal(v)}" for k, v in value.items())
        return f"{{{items}}}"
    else:
        raise ValueError(f"Unsupported type for SQL literal: {type(value)}")


def get_cache_percentage() -> float:
    """
    Returns chunk_cache fill level as a percentage of the 5,000-unit cap.
    Safe to call before any batch run (uses .get() default).
    """
    import streamlit as st
    return len(st.session_state.get('chunk_cache', [])) / 5000.0 * 100.0
