-- ============================================================================
-- chunky_internal_build_chunk_ref
-- Builds the canonical CHUNK_REF string from file + page + optional link.
-- Handler source: procedure/utils/build_chunk_ref.py
-- Shared by: chunky_chunks, chunky_qa
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_internal_build_chunk_ref(
    rel_path VARCHAR,
    page_num NUMBER,
    link VARCHAR
)
RETURNS VARCHAR
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
IMPORTS = ('@DEV_DB.DNA.STG_LIB/utils_bundle.zip')
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run'
EXECUTE AS CALLER
AS
$$
from chunky_utils.build_chunk_ref import run
$$;
