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
DEFAULT_LIB_STAGE = "@DEV_DB.DNA.STG_LIB"  # Stage that hosts poppler_bundle.zip
DEFAULT_POPPLER_BUNDLE = "poppler_bundle.zip"

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
