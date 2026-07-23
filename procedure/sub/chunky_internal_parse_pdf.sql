-- ============================================================================
-- chunky_internal_parse_pdf
-- Calls AI_PARSE_DOCUMENT with the given options and returns parsed JSON.
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
PACKAGES = ('snowflake-snowpark-python',)
HANDLER = 'run'
EXECUTE AS CALLER
AS
$$
import json

def run(session, stage_path, file, options):
    """Call AI_PARSE_DOCUMENT and return parsed JSON."""
    safe_file = file.replace("'", "''")
    safe_stage = stage_path.replace("'", "''")
    opts_json = json.dumps(options) if isinstance(options, dict) else str(options)

    sql = f"""
        SELECT SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(
            TO_FILE('{safe_stage}', '{safe_file}'),
            PARSE_JSON('{opts_json.replace("'", "''")}')
        ) AS J
    """
    try:
        res = session.sql(sql).collect()
        if not res or res[0]["J"] is None:
            return {"success": False, "error": "AI_PARSE_DOCUMENT returned NULL", "data": None}
        raw = res[0]["J"]
        doc = json.loads(raw) if isinstance(raw, str) else raw
        return {"success": True, "error": None, "data": doc}
    except Exception as e:
        return {"success": False, "error": str(e), "data": None}
$$;
