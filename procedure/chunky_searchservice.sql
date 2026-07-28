-- ============================================================================
-- chunky_searchservice
-- Cortex Search Service Manager.
-- Commands: create, list, describe, alter, drop, revert
-- Handler source: procedure/utils/chunky_searchservice_handler.py
--
-- Single-bundle IMPORTS: utils_bundle.zip contains chunky_utils/ + poppler_bundle/
-- + pdf2image/. Note: chunky_searchservice doesn't need poppler at runtime
-- (no PDF rendering), but the single bundle is the only one we maintain.
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_searchservice(
    command VARCHAR,
    instruction VARIANT
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
from chunky_utils.chunky_searchservice_handler import run
$$;
