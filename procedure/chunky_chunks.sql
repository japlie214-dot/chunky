-- ============================================================================
-- chunky_chunks
-- Ingestion Engine.
-- Commands: ingest, list_chunks, list_chunks_csv, update_chunk, delete_chunks,
--           inspect_quality, batch_ingest, estimate_cost, revert
-- Handler source: procedure/utils/chunky_chunks_handler.py
--
-- Single-bundle IMPORTS: utils_bundle.zip contains chunky_utils/ +
-- poppler_bundle/ + pdf2image/ (all in one zip).
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_chunks(
    command VARCHAR,
    instruction VARIANT
)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
RESOURCE_CONSTRAINT = (architecture = 'x86')
IMPORTS = ('@DEV_DB.DNA.STG_LIB/utils_bundle.zip')
PACKAGES = ('snowflake-snowpark-python', 'pandas', 'pypdf', 'pillow')
HANDLER = 'run'
EXECUTE AS CALLER
AS
$$
from chunky_utils.chunky_chunks_handler import run
$$;
