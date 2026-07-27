-- ============================================================================
-- chunky_internal_init_table
-- Creates the target table with standard Chunky schema if it doesn't exist.
-- For OVERWRITE mode: CREATE OR REPLACE (drops existing data).
-- For APPEND/SURGICAL: CREATE IF NOT EXISTS.
-- Handler source: procedure/utils/init_table.py
-- Shared by: chunky_chunks, chunky_qa
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_internal_init_table(
    db VARCHAR,
    schema VARCHAR,
    table_name VARCHAR,
    mode VARCHAR
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
from chunky_utils.init_table import run
$$;
