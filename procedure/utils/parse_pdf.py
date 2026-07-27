"""
procedure/utils/parse_pdf.py
Python helper for calling AI_PARSE_DOCUMENT.

This helper calls
SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT with the supplied options and return
the parsed JSON.

Returns:
  {'success': True,  'data': <parsed document>, 'query_ids': [...]}
  {'success': False, 'error': '...',           'query_ids': [...]}
"""
from __future__ import annotations
import json
from typing import Dict, Any

from .query_log import QueryLog


def run(session, stage_path: str, file: str, options: Any) -> Dict:
    """
    Call SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT and return the parsed JSON.

    `options` may be a Python dict or a JSON string.
    """
    log = QueryLog(session)
    safe_file = (file or "").replace("'", "''")
    safe_stage = (stage_path or "").replace("'", "''")

    if isinstance(options, dict):
        opts_json = json.dumps(options)
    elif options is None:
        opts_json = "{}"
    else:
        opts_json = str(options)
    safe_opts = opts_json.replace("'", "''")

    sql = (
        "SELECT SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT("
        f"  TO_FILE('{safe_stage}', '{safe_file}'),"
        f"  PARSE_JSON('{safe_opts}')"
        ") AS J"
    )

    try:
        rows = log.execute(sql)
        if not rows or rows[0]["J"] is None:
            return {
                "success": False,
                "error": "AI_PARSE_DOCUMENT returned NULL",
                **log.to_dict(),
            }
        raw = rows[0]["J"]
        doc = json.loads(raw) if isinstance(raw, str) else raw
        return {"success": True, "data": doc, **log.to_dict()}
    except Exception as e:
        return {"success": False, "error": str(e), **log.to_dict()}
