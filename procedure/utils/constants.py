"""
procedure/utils/constants.py
Shared constants for Chunky Snowflake stored procedures.

Centralising these values here eliminates hardcoded literals scattered across
the procedure handlers. Every constant is overridable at call-time via the
instruction JSON (the procedures fall back to these defaults only when the
caller does not supply a value).
"""

# ---------------------------------------------------------------------------
# Default Snowflake context (overridable via instruction JSON)
# These are ONLY defaults — every procedure accepts db/schema/table_name as
# parameters so the same procedure works in DEV_DB, PROD_DB, or any other
# database without code changes.
# ---------------------------------------------------------------------------
DEFAULT_DB = "DEV_DB"
DEFAULT_SCHEMA = "DNA"
DEFAULT_LIB_STAGE = "@DEV_DB.DNA.STG_LIB"  # Stage that hosts utils_bundle.zip
DEFAULT_UTILS_BUNDLE = "utils_bundle.zip"  # Single bundle (Python + poppler + pdf2image)

# ---------------------------------------------------------------------------
# Procedure names — used by the revert command strings so they are
# parameterised instead of being hardcoded literals in every handler.
# ---------------------------------------------------------------------------
PROC_CHUNKY_CHUNKS = "chunky_chunks"
PROC_CHUNKY_QA = "chunky_qa"
PROC_CHUNKY_SEARCHSERVICE = "chunky_searchservice"

# ---------------------------------------------------------------------------
# Chunk schema used by the ingestion procedure
# ---------------------------------------------------------------------------
CHUNK_ID_PREFIX = "CHK_"
CHUNK_INSERT_MAX_CHARS = 15_000_000
SNOWFLAKE_MAX_STRING_BYTES = 16_777_216
CHUNK_CACHE_MAX_SIZE = 5000
LAYOUT_BATCH_SIZE = 100
TEMP_IMAGE_PREFIX = "_temp_images"

# ---------------------------------------------------------------------------
# Cortex AI defaults (overridable via instruction.cortex_model)
# ---------------------------------------------------------------------------
DEFAULT_CORTEX_MODEL = "claude-haiku-4-5"
FALLBACK_VISION_MODEL = "claude-haiku-4-5"

# ---------------------------------------------------------------------------
# Default extraction strategy.
# Vision-only by default — Layout is opt-in (callers who want layout must
# set `layout: true` in the instruction JSON). This matches the Streamlit
# app's "Vision" tab default and keeps cost predictable for first-time
# callers who don't specify a strategy.
# ---------------------------------------------------------------------------
DEFAULT_USE_LAYOUT = False
DEFAULT_USE_VISION = True

# ---------------------------------------------------------------------------
# Layout response shapes returned by AI_PARSE_DOCUMENT.
# When called WITHOUT page_filter, the function returns a flat
# {content, metadata} structure. The content uses the form-feed character
# (\f) as a page separator. When called WITH page_filter, it returns
# {pages: [{index, content}], metadata}.
# ---------------------------------------------------------------------------
LAYOUT_PAGE_SEPARATOR = "\f"

# ---------------------------------------------------------------------------
# Time-travel retention safety window (Snowflake standard edition = 1 day).
# Revert commands refuse to operate beyond this window to avoid silent
# failures when data has aged out of Time Travel.
# ---------------------------------------------------------------------------
TIME_TRAVEL_MAX_HOURS = 24

# ---------------------------------------------------------------------------
# Revert defaults
# ---------------------------------------------------------------------------
DEFAULT_REVERT_STRATEGY = "time_travel"  # "time_travel" | "result_scan"

# ---------------------------------------------------------------------------
# Warning templates (rendered AFTER execution in the JSON response)
# ---------------------------------------------------------------------------
WARNING_INGEST_OVERWRITE = (
    "OVERWRITE mode destroyed all prior rows in the target table. "
    "Use the REVERT command with the returned `revert.timestamp_before` "
    f"within {TIME_TRAVEL_MAX_HOURS}h to restore the previous state."
)
WARNING_INGEST_SURGICAL = (
    "SURGICAL mode deleted source page ranges and inserted replacement "
    "chunks. Revert is available via TIME TRAVEL using the returned "
    "`revert.timestamp_before`."
)
WARNING_INGEST_APPEND = (
    "APPEND mode added new rows to the target table. To remove the newly "
    "inserted chunks, use the REVERT command with the returned query_ids."
)
WARNING_INGEST_APPEND_DUPLICATE_PAGES = (
    "APPEND mode inserted rows for pages that already existed in the table "
    "for this file. The duplicate PAGE_NUMBERs were still inserted — use "
    "REVERT to roll back if this was unintended."
)
WARNING_TABLE_NEWLY_CREATED = (
    "A new chunk table was created for this ingest (no prior table existed)."
)
WARNING_QA_COMMIT = (
    "Chunk content was overwritten. The previous content can be retrieved "
    "via TIME TRAVEL using `revert.timestamp_before`."
)
WARNING_QA_DELETE = (
    "Chunks were permanently deleted. They can be restored via TIME TRAVEL "
    "using `revert.timestamp_before` within 24h."
)
WARNING_SEARCHSERVICE_CREATE = (
    "CREATE OR REPLACE dropped any prior Cortex Search Service with the "
    "same name. The dropped service cannot be restored via TIME TRAVEL — "
    "recreate it with the returned `data.ddl` if needed."
)
WARNING_SEARCHSERVICE_DROP = (
    "Cortex Search Service was dropped. It cannot be restored — recreate "
    "it using the original DDL if available."
)
WARNING_SEARCHSERVICE_ALTER = (
    "Search service was altered. The previous TARGET_LAG / grants are not "
    "captured in Time Travel — manual restoration is required."
)
WARNING_HYBRID_REPAIR = (
    "Hybrid repair ran Vision extraction on chunks flagged by the quality "
    "inspector. Repaired chunks are now tagged CHUNK_TYPE='ENHANCED'."
)
WARNING_LAYOUT_FLAT_RESPONSE = (
    "AI_PARSE_DOCUMENT returned a flat {content, metadata} response (no "
    "pages array). The handler split the content by form-feed (\\f) to "
    "reconstruct per-page chunks. Verify the page count matches the PDF."
)

# ---------------------------------------------------------------------------
# Pricing registry (USD per 1M tokens). Used by cost estimation commands.
# Keep in sync with utils/core_utils.py:RAGAnalytics.PRICING_REGISTRY.
# ---------------------------------------------------------------------------
PRICING_REGISTRY = {
    "claude-haiku-4-5": {"input": 0.80, "output": 4.00},
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.00},
    "llama3.1-405b": {"input": 2.70, "output": 2.70},
    "llama3.1-70b": {"input": 0.99, "output": 0.99},
    "llama3.1-8b": {"input": 0.18, "output": 0.18},
    "mistral-large2": {"input": 2.00, "output": 6.00},
    "mixtral-8x7b": {"input": 0.24, "output": 0.24},
    "snowflake-arctic": {"input": 0.50, "output": 0.75},
    "reka-flash": {"input": 0.10, "output": 0.10},
    "jamba-instruct": {"input": 0.50, "output": 0.75},
}

# Cost per 1000 pages for layout-only AI_PARSE_DOCUMENT (credits).
# Mirrors utils/constants.py:LAYOUT_COST_PER_1K_PAGES so both sides
# (Streamlit + procedure) report the same cost.
LAYOUT_COST_PER_1K_PAGES = 3.33
