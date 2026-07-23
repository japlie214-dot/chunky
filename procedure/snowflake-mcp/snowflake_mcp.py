"""
Snowflake MCP Server — File Upload to Stage
Connect to Snowflake and PUT local files to stages.
"""
import os
import sys
import json
from pathlib import Path
from mcp.server.fastmcp import FastMCP

# Initialize
mcp = FastMCP("snowflake")

# Global connection (lazy init)
_conn = None

def get_connection():
    """Get or create Snowflake connection."""
    global _conn
    if _conn is not None:
        try:
            _conn.cursor().execute("SELECT 1")
            return _conn
        except Exception:
            _conn = None

    import snowflake.connector

    # Read config from environment or config file
    config_path = os.path.expanduser("~/.snowflake-mcp.json")
    config = {}
    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)

    account = config.get("account") or os.environ.get("SNOWFLAKE_ACCOUNT")
    user = config.get("user") or os.environ.get("SNOWFLAKE_USER")

    if not account or not user:
        raise ValueError(
            "Missing Snowflake credentials. Set ~/.snowflake-mcp.json or "
            "SNOWFLAKE_ACCOUNT + SNOWFLAKE_USER env vars."
        )

    _conn = snowflake.connector.connect(
        account=account,
        user=user,
        authenticator=config.get("authenticator", "externalbrowser"),
        client_store_temporary_credential=True,
    )
    return _conn


@mcp.tool()
def put_file(local_path: str, stage: str, dest_path: str = "", overwrite: bool = True, auto_compress: bool = False) -> str:
    """Upload a local file to a Snowflake stage.

    Args:
        local_path: Absolute path to the local file (e.g. /Users/me/report.pdf)
        stage: Target stage name (e.g. @DEV_DB.DNA.DOCS or @DEV_DB.DNA.STG_LIB)
        dest_path: Optional sub-path on the stage (e.g. reports/). Defaults to stage root.
        overwrite: Overwrite existing file on stage (default True)
        auto_compress: Compress the file during upload (default False)
    """
    path = Path(local_path).expanduser().resolve()
    if not path.exists():
        return json.dumps({"success": False, "error": f"File not found: {local_path}"})
    if not path.is_file():
        return json.dumps({"success": False, "error": f"Not a file: {local_path}"})

    # Build PUT command
    file_url = f"file://{path}"
    target = stage.rstrip("/")
    if dest_path:
        target = f"{target}/{dest_path.lstrip('/')}"

    put_sql = f"PUT '{file_url}' {target} OVERWRITE={'TRUE' if overwrite else 'FALSE'} AUTO_COMPRESS={'TRUE' if auto_compress else 'FALSE'}"

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(put_sql)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        result = [dict(zip(columns, row)) for row in rows]
        return json.dumps({"success": True, "sql": put_sql, "result": result}, default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e), "sql": put_sql})


@mcp.tool()
def list_stage(stage: str, pattern: str = "") -> str:
    """List files on a Snowflake stage.

    Args:
        stage: Stage path (e.g. @DEV_DB.DNA.DOCS)
        pattern: Optional regex pattern to filter files (e.g. .*\\.pdf)
    """
    list_sql = f"LIST {stage}"
    if pattern:
        list_sql += f" PATTERN='{pattern}'"

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(list_sql)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        result = [dict(zip(columns, row)) for row in rows]
        return json.dumps({"success": True, "files": result}, default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def remove_file(stage: str, path: str) -> str:
    """Remove a file from a Snowflake stage.

    Args:
        stage: Stage path (e.g. @DEV_DB.DNA.DOCS)
        path: Relative file path on the stage (e.g. reports/Q1_2024.pdf)
    """
    full_path = f"{stage.rstrip('/')}/{path.lstrip('/')}"
    remove_sql = f"REMOVE '{full_path}'"

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(remove_sql)
        return json.dumps({"success": True, "removed": full_path})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def execute_sql(sql: str) -> str:
    """Execute a SQL statement on Snowflake. For quick queries and diagnostics.

    Args:
        sql: SQL statement to execute
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(sql)
        if cursor.description:
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            result = [dict(zip(columns, row)) for row in rows]
            return json.dumps({"success": True, "columns": columns, "rows": result, "row_count": len(result)}, default=str)
        else:
            return json.dumps({"success": True, "message": "Statement executed successfully"})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def get_presigned_url(stage: str, path: str, expiration: int = 3600) -> str:
    """Get a pre-signed URL for a file on a Snowflake stage.

    Args:
        stage: Stage path (e.g. @DEV_DB.DNA.DOCS)
        path: Relative file path on the stage
        expiration: URL expiration in seconds (default 3600 = 1 hour)
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        safe_stage = stage.replace("'", "''")
        safe_path = path.replace("'", "''")
        cursor.execute(f"SELECT GET_PRESIGNED_URL('{safe_stage}', '{safe_path}', {expiration}) AS URL")
        row = cursor.fetchone()
        if row and row[0]:
            return json.dumps({"success": True, "url": row[0], "expires_in": expiration})
        return json.dumps({"success": False, "error": "No URL returned"})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
