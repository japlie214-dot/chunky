-- ============================================================================
-- chunky_internal_surgical_delete
-- Deletes pages by range mappings with transaction safety (BEGIN/COMMIT/ROLLBACK).
-- Sorts mappings bottom-up (highest source_end first) to avoid invalidation.
-- Handler source: procedure/utils/surgical_delete.py
-- Shared by: chunky_chunks
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_internal_surgical_delete(
    db VARCHAR,
    schema VARCHAR,
    table_name VARCHAR,
    file VARCHAR,
    range_mappings VARIANT
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
from chunky_utils.surgical_delete import run
$$;
