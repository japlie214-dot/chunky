-- ============================================================================
-- chunky_internal_grant_table
-- Grants ALL PRIVILEGES on a table to specified roles with retry logic.
-- Handler source: procedure/utils/grant_table.py
-- Shared by: chunky_chunks, chunky_searchservice
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_internal_grant_table(
    db VARCHAR,
    schema VARCHAR,
    table_name VARCHAR,
    roles VARIANT
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
from chunky_utils.grant_table import run
$$;
