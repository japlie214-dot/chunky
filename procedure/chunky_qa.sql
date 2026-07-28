-- ============================================================================
-- chunky_qa
-- Headless QA Studio.
-- Commands: search, inspect, generate_draft, commit, delete, revert
-- Handler source: procedure/utils/chunky_qa_handler.py
--
-- Single-bundle IMPORTS: utils_bundle.zip contains chunky_utils/ +
-- poppler_bundle/ + pdf2image/ (all in one zip).
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_qa(
    command VARCHAR,
    instruction VARIANT
)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
IMPORTS = ('@DEV_DB.DNA.STG_LIB/utils_bundle.zip')
PACKAGES = ('snowflake-snowpark-python', 'pandas', 'pypdf', 'pillow')
HANDLER = 'run'
EXECUTE AS CALLER
AS
$$
from chunky_utils.chunky_qa_handler import run
$$;
