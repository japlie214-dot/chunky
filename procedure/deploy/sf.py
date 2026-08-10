#!/usr/bin/env python
"""sf — the Chunky deploy CLI. snowball's auth, with writes enabled.

    sf auth login|status|logout|whoami   one-time browser login, token state
    sf config show|set                   where identity and scope come from
    sf sql "<statement>"                 run one statement (DDL and DML allowed)
    sf script <file.sql>                 run a .sql file, statement by statement
    sf call <PROC> <cmd> <instruction>   CALL a Chunky procedure, unwrap the JSON
    sf put <local> <@stage>              upload a file to a stage
    sf get <@stage/file> <dir>           download from a stage
    sf ls <@stage>                       list a stage
    sf api "<statement>"                 same, over the SQL API v2 (202/poll)
    sf api-call <PROC> <cmd> <inst>      CALL over the SQL API v2
    sf api-status <handle>               poll a deferred SQL API statement

Unlike snowball there is NO read-only guard. This tool exists to CREATE
PROCEDURE, PUT bundles, CALL procedures that write, and DROP test objects.
Every statement is query-tagged `ChunkyDeploy/<version>|user=..|cmd=..` so
toolchain traffic stays separable in QUERY_HISTORY.

Run `sf <command> --help` for details.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Windows consoles default to a legacy code page (cp1252 here), which raises
# UnicodeEncodeError on any non-Latin-1 character in real data. Force UTF-8
# and degrade gracefully rather than crashing mid-render.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Run as a plain script (`python procedure/deploy/sf.py ...`) or as a module
# (`python -m deploy.sf` from procedure/). Putting `procedure/` on sys.path
# makes `deploy` an importable package so the intra-package `from . import`
# statements in auth/sfapi/config resolve either way.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "deploy"

from deploy import config  # noqa: E402
from deploy.config import ConfigError  # noqa: E402


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _render(cols, rows, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps([dict(zip(cols, r)) for r in rows], indent=2, default=str))
        return
    if fmt == "csv":
        import csv

        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
        return

    if not rows:
        print("(no rows)")
        return
    text = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [len(c) for c in cols]
    for r in text:
        for i, v in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], min(len(v), 60))
    line = "  ".join(c[:60].ljust(widths[i]) for i, c in enumerate(cols))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in text:
        print("  ".join(v[:60].ljust(widths[i]) for i, v in enumerate(r)))
    print(f"\n{len(rows)} row(s)")


def _connect(args):
    from deploy import auth

    os.environ["CHUNKY_SF_COMMAND"] = getattr(args, "_command", "sf")
    return auth.connect(
        user=getattr(args, "user", None),
        account=getattr(args, "account", None),
        role=getattr(args, "role", None),
        warehouse=getattr(args, "warehouse", None),
        database=getattr(args, "database", None),
        schema=getattr(args, "schema", None),
    )


def _run(cur, statement, params=None):
    cur.execute(statement, params)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall() if cur.description else []
    return cols, rows


# ---------------------------------------------------------------------------
# auth / config
# ---------------------------------------------------------------------------
def h_auth_login(args):
    from deploy import auth

    info = auth.login(
        user=args.user, account=args.account, role=args.role,
        warehouse=args.warehouse, database=args.database, schema=args.schema,
    )
    for k, v in info.items():
        print(f"  {k:<16}: {v}")
    return 0


def h_auth_status(args):
    from deploy import auth

    for k, v in auth.token_status(args.user, args.account).items():
        print(f"  {k:<32}: {v}")
    return 0


def h_auth_logout(args):
    from deploy import auth

    removed = auth.logout(args.user, args.account)
    print(f"Cleared: {', '.join(removed) if removed else '(nothing cached)'}")
    return 0


def h_auth_whoami(args):
    from deploy import auth

    conn = _connect(args)
    try:
        for k, v in auth.session_info(conn).items():
            print(f"  {k:<12}: {v}")
    finally:
        conn.close()
    return 0


def h_config_show(args):
    for k, v in config.describe_config().items():
        print(f"{k:<18}: {v}")
    return 0


def h_config_set(args):
    values = {
        "user": args.user, "account": args.account, "role": args.role,
        "warehouse": args.warehouse, "database": args.database,
        "schema": args.schema, "lib_stage": args.lib_stage,
        "docs_stage": args.docs_stage, "render_stage": args.render_stage,
        "private_key_path": args.private_key_path,
    }
    if not any(v is not None for v in values.values()):
        print("Nothing to set. Pass at least one of: --user --account --role "
              "--warehouse --database --schema --lib-stage --docs-stage "
              "--render-stage --private-key-path")
        return 2
    print(f"Saved configuration to {config.save_config(**values)}")
    for k, v in config.describe_config().items():
        print(f"  {k:<18}: {v}")
    return 0


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------
def h_sql(args):
    statement = args.statement
    if statement == "-":
        statement = sys.stdin.read()
    conn = _connect(args)
    try:
        cur = conn.cursor()
        try:
            cols, rows = _run(cur, statement)
            if args.query_id:
                print(f"-- query_id: {cur.sfqid}", file=sys.stderr)
            _render(cols, rows, args.format)
        finally:
            cur.close()
    finally:
        conn.close()
    return 0


def h_script(args):
    from deploy.sqlsplit import split_statements

    path = Path(args.file).expanduser()
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        return 2
    statements = split_statements(path.read_text(encoding="utf-8"))
    print(f"{path.name}: {len(statements)} statement(s)")

    if args.dry_run:
        for i, s in enumerate(statements, 1):
            head = " ".join(s.split())[:100]
            print(f"  [{i:>3}] {head}{'...' if len(head) == 100 else ''}")
        return 0

    conn = _connect(args)
    failures = 0
    try:
        for i, stmt in enumerate(statements, 1):
            head = " ".join(stmt.split())[:80]
            cur = conn.cursor()
            try:
                cur.execute(stmt)
                if cur.description:
                    cur.fetchall()
                print(f"  [{i:>3}/{len(statements)}] ok    {head}")
            except Exception as exc:
                failures += 1
                print(f"  [{i:>3}/{len(statements)}] FAIL  {head}")
                print(f"        {exc}", file=sys.stderr)
                print(f"        query_id: {cur.sfqid}", file=sys.stderr)
                if not args.keep_going:
                    return 1
            finally:
                cur.close()
    finally:
        conn.close()
    if failures:
        print(f"\n{failures} statement(s) failed.", file=sys.stderr)
    return 1 if failures else 0


def _load_instruction(raw: str) -> dict:
    """Accept inline JSON, `@path/to.json`, or `-` for stdin."""
    if raw == "-":
        return json.loads(sys.stdin.read())
    if raw.startswith("@"):
        return json.loads(Path(raw[1:]).expanduser().read_text(encoding="utf-8"))
    return json.loads(raw)


def h_call(args):
    instruction = _load_instruction(args.instruction)
    conn = _connect(args)
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                f"CALL {args.procedure}(%s, PARSE_JSON(%s))",
                (args.command_name, json.dumps(instruction)),
            )
            row = cur.fetchone()
            print(f"-- query_id: {cur.sfqid}", file=sys.stderr)
        finally:
            cur.close()
    finally:
        conn.close()

    if not row:
        print("(procedure returned no rows)", file=sys.stderr)
        return 1
    value = row[0]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            print(value)
            return 0
    print(json.dumps(value, indent=2, default=str))
    return 0 if (isinstance(value, dict) and value.get("success")) else 1


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------
def _file_uri(path: Path) -> str:
    """A PUT-safe file:// URI.

    The connector wants forward slashes; a Windows backslash path silently
    fails to match anything. `as_posix()` on a resolved path gives
    'C:/Users/...' which is what PUT accepts.
    """
    return "file://" + path.resolve().as_posix()


def h_put(args):
    path = Path(args.local).expanduser()
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        return 2
    stmt = (
        f"PUT '{_file_uri(path)}' {args.stage.rstrip('/')} "
        f"AUTO_COMPRESS={'TRUE' if args.auto_compress else 'FALSE'} "
        f"OVERWRITE={'FALSE' if args.no_overwrite else 'TRUE'}"
    )
    conn = _connect(args)
    try:
        cur = conn.cursor()
        try:
            cols, rows = _run(cur, stmt)
            _render(cols, rows, args.format)
        finally:
            cur.close()
    finally:
        conn.close()
    return 0


def h_get(args):
    target = Path(args.dest).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    stmt = f"GET {args.stage_path} '{_file_uri(target)}'"
    conn = _connect(args)
    try:
        cur = conn.cursor()
        try:
            cols, rows = _run(cur, stmt)
            _render(cols, rows, args.format)
        finally:
            cur.close()
    finally:
        conn.close()
    return 0


def h_ls(args):
    stmt = f"LIST {args.stage}"
    if args.pattern:
        stmt += f" PATTERN='{args.pattern}'"
    conn = _connect(args)
    try:
        cur = conn.cursor()
        try:
            cols, rows = _run(cur, stmt)
            _render(cols, rows, args.format)
        finally:
            cur.close()
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# SQL API v2
# ---------------------------------------------------------------------------
def _api_kwargs(args) -> dict:
    return dict(
        user=args.user, account=args.account, role=args.role,
        warehouse=args.warehouse, database=args.database, schema=args.schema,
        timeout_seconds=args.timeout,
    )


def _on_poll(attempt, status, resp):
    print(f"  [poll {attempt}] HTTP {status}", file=sys.stderr)


def h_api(args):
    from deploy import sfapi

    statement = args.statement
    if statement == "-":
        statement = sys.stdin.read()
    try:
        resp = sfapi.execute(
            statement, asynchronous=args.asynchronous,
            poll_seconds=args.poll_seconds, max_wait_seconds=args.max_wait,
            on_poll=_on_poll, **_api_kwargs(args),
        )
    except sfapi.SqlApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _render(sfapi.columns(resp), sfapi.rows(resp), args.format)
    return 0


def h_api_call(args):
    from deploy import sfapi

    instruction = _load_instruction(args.instruction)
    try:
        result = sfapi.call_procedure(
            args.procedure, args.command_name, instruction,
            asynchronous=True, poll_seconds=args.poll_seconds,
            max_wait_seconds=args.max_wait, on_poll=_on_poll,
            **_api_kwargs(args),
        )
    except sfapi.SqlApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("success") else 1


def h_api_status(args):
    from deploy import sfapi

    token, token_type = sfapi.resolve_token(user=args.user, account=args.account)
    status, resp = sfapi.poll(args.handle, token, token_type, account=args.account)
    print(f"HTTP {status}")
    print(json.dumps(resp, indent=2, default=str)[:4000])
    return 0 if status == 200 else 1


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def _add_common(p):
    p.add_argument("--user")
    p.add_argument("--account")
    p.add_argument("--role")
    p.add_argument("--warehouse")
    p.add_argument("--database")
    p.add_argument("--schema")


def _add_format(p):
    p.add_argument("--format", choices=("table", "json", "csv"), default="table")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sf", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # auth
    auth_p = sub.add_parser("auth", help="login, token status, sign out")
    auth_sub = auth_p.add_subparsers(dest="sub", required=True)
    for name, fn, helptext in (
        ("login", h_auth_login, "connect now (opens a browser the first time)"),
        ("status", h_auth_status, "cached-token state, no connection"),
        ("logout", h_auth_logout, "clear cached tokens"),
        ("whoami", h_auth_whoami, "connect and print the session context"),
    ):
        sp = auth_sub.add_parser(name, help=helptext)
        _add_common(sp)
        sp.set_defaults(func=fn, _command=f"auth.{name}")

    # config
    cfg_p = sub.add_parser("config", help="show or set local configuration")
    cfg_sub = cfg_p.add_subparsers(dest="sub", required=True)
    sp = cfg_sub.add_parser("show", help="effective values and where each came from")
    sp.set_defaults(func=h_config_show, _command="config.show")
    sp = cfg_sub.add_parser("set", help="write values to procedure/deploy/config.json")
    _add_common(sp)
    sp.add_argument("--lib-stage", dest="lib_stage")
    sp.add_argument("--docs-stage", dest="docs_stage")
    sp.add_argument("--render-stage", dest="render_stage")
    sp.add_argument("--private-key-path", dest="private_key_path")
    sp.set_defaults(func=h_config_set, _command="config.set")

    # sql
    sp = sub.add_parser("sql", help="run one statement ('-' reads stdin)")
    sp.add_argument("statement")
    _add_common(sp)
    _add_format(sp)
    sp.add_argument("--query-id", action="store_true",
                    help="print the Snowflake query id to stderr")
    sp.set_defaults(func=h_sql, _command="sql")

    # script
    sp = sub.add_parser("script", help="run a .sql file ($$-aware splitting)")
    sp.add_argument("file")
    _add_common(sp)
    sp.add_argument("--dry-run", action="store_true",
                    help="print the statements without running them")
    sp.add_argument("--keep-going", action="store_true",
                    help="continue after a failed statement")
    sp.set_defaults(func=h_script, _command="script")

    # call
    sp = sub.add_parser("call", help="CALL a Chunky procedure and unwrap its JSON")
    sp.add_argument("procedure", help="e.g. CHUNKY_INGEST")
    sp.add_argument("command_name", metavar="command", help="e.g. ingest")
    sp.add_argument("instruction",
                    help="inline JSON, @path/to.json, or - for stdin")
    _add_common(sp)
    sp.set_defaults(func=h_call, _command="call")

    # stages
    sp = sub.add_parser("put", help="upload a file to a stage")
    sp.add_argument("local")
    sp.add_argument("stage")
    sp.add_argument("--auto-compress", action="store_true")
    sp.add_argument("--no-overwrite", action="store_true")
    _add_common(sp)
    _add_format(sp)
    sp.set_defaults(func=h_put, _command="put")

    sp = sub.add_parser("get", help="download from a stage")
    sp.add_argument("stage_path")
    sp.add_argument("dest")
    _add_common(sp)
    _add_format(sp)
    sp.set_defaults(func=h_get, _command="get")

    sp = sub.add_parser("ls", help="list a stage")
    sp.add_argument("stage")
    sp.add_argument("--pattern")
    _add_common(sp)
    _add_format(sp)
    sp.set_defaults(func=h_ls, _command="ls")

    # SQL API
    sp = sub.add_parser("api", help="run a statement over the SQL API v2")
    sp.add_argument("statement")
    _add_common(sp)
    _add_format(sp)
    sp.add_argument("--async", dest="asynchronous", action="store_true")
    sp.add_argument("--timeout", type=int, default=3600)
    sp.add_argument("--poll-seconds", type=float, default=3.0)
    sp.add_argument("--max-wait", type=int, default=3600)
    sp.set_defaults(func=h_api, _command="api")

    sp = sub.add_parser("api-call", help="CALL a procedure over the SQL API v2")
    sp.add_argument("procedure")
    sp.add_argument("command_name", metavar="command")
    sp.add_argument("instruction")
    _add_common(sp)
    sp.add_argument("--timeout", type=int, default=3600)
    sp.add_argument("--poll-seconds", type=float, default=3.0)
    sp.add_argument("--max-wait", type=int, default=3600)
    sp.set_defaults(func=h_api_call, _command="api-call")

    sp = sub.add_parser("api-status", help="poll a deferred SQL API statement")
    sp.add_argument("handle")
    _add_common(sp)
    sp.set_defaults(func=h_api_status, _command="api-status")

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        if os.environ.get("CHUNKY_SF_TRACEBACK"):
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
