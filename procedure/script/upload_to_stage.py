#!/usr/bin/env python3
"""
procedure/script/upload_to_stage.py
Local command-line uploader for Snowflake stages.

This is NOT a Snowflake stored procedure — it runs on your laptop and
authenticates against Snowflake via the system browser (SSO / external
browser auth, the same flow `snowsql --authenticator=externalbrowser`
uses).

Why a separate script?
----------------------
The headless procedures in this repo expect their input PDFs to already
live on a Snowflake stage. The Snowflake MCP server
(`procedure/snowflake-mcp/`) is one way to put them there, but it
requires Claude Desktop. This script is the zero-dependency
alternative: just `pip install snowflake-connector-python` and run.

Usage
-----
    # Upload a single file
    python3 procedure/script/upload_to_stage.py \\
        --account myorg-myaccount \\
        --user me@company.com \\
        --stage @DEV_DB.DNA.DOCS \\
        --file procedure/script/pdf/fy2024-tbk-investor-presentation.pdf

    # Upload every PDF in a directory
    python3 procedure/script/upload_to_stage.py \\
        --account myorg-myaccount \\
        --user me@company.com \\
        --stage @DEV_DB.DNA.DOCS \\
        --dir procedure/script/pdf/

    # List what's already on the stage
    python3 procedure/script/upload_to_stage.py \\
        --account myorg-myaccount \\
        --user me@company.com \\
        --stage @DEV_DB.DNA.DOCS \\
        --list

Configuration
-------------
Account and user may also be supplied via environment variables or a
config file so you don't have to type them every time:

  Environment variables:
    SNOWFLAKE_ACCOUNT=myorg-myaccount
    SNOWFLAKE_USER=me@company.com

  Config file (~/.chunky-upload.json or path passed to --config):
    {
      "account": "myorg-myaccount",
      "user": "me@company.com",
      "role": "IT_AI",
      "warehouse": "COMPUTE_WH"
    }

Auth always uses `authenticator=externalbrowser` — the script opens a
browser window for SSO the first time and caches the token in
`~/.snowflake/` (managed by the Snowflake connector).
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any

CONFIG_PATHS = [
    Path.home() / ".chunky-upload.json",
    Path.home() / ".snowflake-mcp.json",  # reuse MCP config if present
]


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def load_config(explicit_path: Optional[str] = None) -> Dict[str, Any]:
    """Read connection config from --config, env vars, or default files."""
    cfg: Dict[str, Any] = {}

    # 1. Default config files (first match wins)
    for p in CONFIG_PATHS:
        if p.is_file():
            try:
                cfg.update(json.loads(p.read_text()))
            except Exception:
                pass
            break

    # 2. Explicit --config (overrides)
    if explicit_path:
        p = Path(explicit_path).expanduser()
        if not p.is_file():
            raise SystemExit(f"Config file not found: {p}")
        cfg.update(json.loads(p.read_text()))

    # 3. Environment variables (highest priority)
    if os.environ.get("SNOWFLAKE_ACCOUNT"):
        cfg["account"] = os.environ["SNOWFLAKE_ACCOUNT"]
    if os.environ.get("SNOWFLAKE_USER"):
        cfg["user"] = os.environ["SNOWFLAKE_USER"]
    if os.environ.get("SNOWFLAKE_ROLE"):
        cfg["role"] = os.environ["SNOWFLAKE_ROLE"]
    if os.environ.get("SNOWFLAKE_WAREHOUSE"):
        cfg["warehouse"] = os.environ["SNOWFLAKE_WAREHOUSE"]

    return cfg


def connect(cfg: Dict[str, Any]):
    """Open a Snowflake connection using externalbrowser auth."""
    try:
        import snowflake.connector
    except ImportError:
        raise SystemExit(
            "snowflake-connector-python is not installed.\n"
            "Install it with:  pip install snowflake-connector-python"
        )

    account = cfg.get("account")
    user = cfg.get("user")
    if not account or not user:
        raise SystemExit(
            "Missing account/user. Provide via --account/--user, env vars "
            "(SNOWFLAKE_ACCOUNT/SNOWFLAKE_USER), or a config file."
        )

    print(f"Connecting to Snowflake as {user}@{account}...")
    print("(A browser window will open for SSO on first use.)")
    conn = snowflake.connector.connect(
        account=account,
        user=user,
        authenticator="externalbrowser",
        client_store_temporary_credential=True,
        role=cfg.get("role"),
        warehouse=cfg.get("warehouse"),
    )
    print("Connected.")
    return conn


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
def upload_file(conn, local_path: Path, stage: str,
                overwrite: bool = True, auto_compress: bool = False) -> Dict:
    """PUT a single local file to a Snowflake stage."""
    if not local_path.is_file():
        raise SystemExit(f"Not a file: {local_path}")

    file_url = f"file://{local_path.resolve()}"
    target = stage.rstrip("/")
    put_sql = (
        f"PUT '{file_url}' {target} "
        f"OVERWRITE={'TRUE' if overwrite else 'FALSE'} "
        f"AUTO_COMPRESS={'TRUE' if auto_compress else 'FALSE'}"
    )
    print(f"  PUT {local_path.name} -> {target}")
    cursor = conn.cursor()
    try:
        cursor.execute(put_sql)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        result = [dict(zip(columns, row)) for row in rows]
        return {"success": True, "sql": put_sql, "result": result}
    except Exception as e:
        return {"success": False, "error": str(e), "sql": put_sql}
    finally:
        cursor.close()


def upload_dir(conn, local_dir: Path, stage: str,
               pattern: str = "*.pdf",
               overwrite: bool = True,
               auto_compress: bool = False) -> list:
    """PUT every file matching `pattern` in `local_dir` to `stage`."""
    if not local_dir.is_dir():
        raise SystemExit(f"Not a directory: {local_dir}")

    files = sorted(local_dir.glob(pattern))
    if not files:
        print(f"No files matching {pattern!r} in {local_dir}")
        return []

    print(f"Uploading {len(files)} file(s) from {local_dir} to {stage}...")
    results = []
    for f in files:
        results.append(upload_file(conn, f, stage, overwrite, auto_compress))
    return results


def list_stage(conn, stage: str, pattern: str = "") -> list:
    """LIST files on a stage."""
    sql = f"LIST {stage}"
    if pattern:
        sql += f" PATTERN='{pattern}'"
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Upload local files to a Snowflake stage using "
                    "browser-based SSO auth.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Connection
    p.add_argument("--account", help="Snowflake account (e.g. myorg-myaccount)")
    p.add_argument("--user", help="Snowflake user (email or username)")
    p.add_argument("--role", help="Role to assume after login")
    p.add_argument("--warehouse", help="Warehouse to use")
    p.add_argument("--config", help="Path to a JSON config file")

    # Target
    p.add_argument("--stage", required=True,
                   help="Target stage (e.g. @DEV_DB.DNA.DOCS)")

    # Operation (mutually exclusive)
    op = p.add_mutually_exclusive_group(required=True)
    op.add_argument("--file", help="Single file to upload")
    op.add_argument("--dir", help="Directory to upload (use --pattern to filter)")
    op.add_argument("--list", action="store_true",
                    help="List files on the stage instead of uploading")

    # Upload options
    p.add_argument("--pattern", default="*.pdf",
                   help="Glob pattern for --dir (default: *.pdf)")
    p.add_argument("--no-overwrite", action="store_true",
                   help="Don't overwrite existing files on the stage")
    p.add_argument("--auto-compress", action="store_true",
                   help="Compress files during upload (default: off)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # Merge CLI args into config (CLI wins)
    cfg = load_config(args.config)
    if args.account:
        cfg["account"] = args.account
    if args.user:
        cfg["user"] = args.user
    if args.role:
        cfg["role"] = args.role
    if args.warehouse:
        cfg["warehouse"] = args.warehouse

    conn = connect(cfg)
    try:
        if args.list:
            print(f"\nFiles on {args.stage}:")
            files = list_stage(conn, args.stage)
            if not files:
                print("  (empty)")
            for f in files:
                # Different accounts return different column sets — pick
                # the most useful ones defensively.
                name = f.get("name") or f.get("file_name") or "?"
                size = f.get("size") or f.get("file_size") or "?"
                print(f"  {size:>12}  {name}")
            return 0

        overwrite = not args.no_overwrite
        if args.file:
            result = upload_file(
                conn, Path(args.file).expanduser(),
                args.stage, overwrite, args.auto_compress,
            )
            print(json.dumps(result, indent=2, default=str))
            return 0 if result.get("success") else 1

        if args.dir:
            results = upload_dir(
                conn, Path(args.dir).expanduser(),
                args.stage, args.pattern, overwrite, args.auto_compress,
            )
            print(json.dumps(results, indent=2, default=str))
            return 0 if all(r.get("success") for r in results) else 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
