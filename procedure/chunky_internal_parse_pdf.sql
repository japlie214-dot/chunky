-- ============================================================================
-- chunky_internal_parse_pdf
-- Calls AI_PARSE_DOCUMENT with the given options and returns parsed JSON.
-- Handler source: procedure/utils/parse_pdf.py
-- Shared by: chunky_chunks, chunky_qa
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_internal_parse_pdf(
    stage_path VARCHAR,
    file VARCHAR,
    options VARIANT
)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
IMPORTS = ('@DEV_DB.DNA.STG_LIB/utils_bundle.zip')
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run'
EXECUTE AS CALLER
AS
$$
from chunky_utils.parse_pdf import run
$$;
