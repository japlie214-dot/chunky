"""
procedure/utils/chunky_searchservice_handler.py
Cortex Search Service Manager.
Commands: create, list, describe, alter, drop, revert.

Source (logical): CCS wizard Page 5 (views/ccs/page4_complete.py) â€” this
is the Streamlit-free, headless equivalent.

Headless changes vs. the original Streamlit-side code:
  * No `streamlit` imports, no `st.session_state`, no UI fragments.
  * Warnings are returned in the response AFTER execution.
  * Every SQL operation runs through `QueryLog.execute` for query-id
    capture.
  * New `revert` command. Because Cortex Search Services are NOT
    time-travelable, revert works by re-running the previously captured
    DDL (returned in the original operation's response under
    `data.previous_ddl`).
"""
from __future__ import annotations
import json
import re
import time
from typing import Any, Dict, List, Optional

from .constants import (
    WARNING_SEARCHSERVICE_CREATE,
    WARNING_SEARCHSERVICE_DROP,
    WARNING_SEARCHSERVICE_ALTER,
    PROC_DEPLOY,
    DEFAULT_EMBEDDING_MODEL,
    TARGET_LAG,
)
from .query_log import QueryLog
from ._shared import make_revert_command, err, safe_identifier, clean_text_for_sql
from . import locks, table_comment, reindex
from .ulid import run_id as new_run_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _qualify(db: str, schema: str, name: str) -> str:
    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    safe_name = name.replace('"', '""')
    return f'"{safe_db}"."{safe_sch}"."{safe_name}"'


def _safe_role(r: str) -> Optional[str]:
    """Return a quoted, uppercase role name, or None if invalid."""
    if not r:
        return None
    s = str(r).strip()
    if not s:
        return None
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", s):
        return None
    return '"' + s.upper().replace('"', '""') + '"'


def _get_ddl(session, log: QueryLog, full_svc: str) -> Optional[str]:
    """Best-effort GET_DDL for a Cortex Search Service. Returns None if unavailable."""
    try:
        rows = log.execute(f"SELECT GET_DDL('CORTEX_SEARCH_SERVICE', '{full_svc}') AS DDL")
        if rows and rows[0]["DDL"]:
            return str(rows[0]["DDL"])
    except Exception:
        return None
    return None


def _row_dict(row) -> dict:
    return row.as_dict() if hasattr(row, "as_dict") else dict(row)


def _normalise_items(value, default=None):
    value = default if value is None else value
    result = []
    for item in value or []:
        if isinstance(item, str):
            result.append({"column": item})
        elif isinstance(item, dict):
            result.append(dict(item))
        else:
            raise ValueError("column specifications must be strings or objects")
    return result


def _column_names(session, log, db, schema, table) -> set[str]:
    rows = log.execute(
        f'SELECT COLUMN_NAME FROM "{db}".INFORMATION_SCHEMA.COLUMNS '
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
        params=[schema, table],
    )
    return {str(_row_dict(row).get("COLUMN_NAME", "")).upper() for row in rows}


def _resolve_columns(session, log, db, schema, tables, items, *, default):
    specs = _normalise_items(items, default)
    resolved = []
    for spec in specs:
        col = safe_identifier(spec.get("column") or spec.get("name"), "column")
        requested_tables = spec.get("tables")
        if requested_tables is None and spec.get("table"):
            requested_tables = [spec["table"]]
        applies = [str(t) for t in (requested_tables or tables)]
        for table in applies:
            if table not in tables:
                raise ValueError(f"column {col} references unknown source table {table}")
            if col.upper() not in _column_names(session, log, db, schema, table):
                metadata_field = spec.get("metadata_field")
                if metadata_field and "CHUNK_METADATA" in _column_names(session, log, db, schema, table):
                    resolved.append({"column": col, "table": table, "expression":
                                     f'CHUNK_METADATA:{safe_identifier(metadata_field)}::VARCHAR'})
                    continue
                raise ValueError(f"column {col} does not exist on source table {db}.{schema}.{table}")
            resolved.append({"column": col, "table": table, "expression": f'"{col}"'})
    return resolved


def _source_query(session, log, db, schema, tables, search_cols, attr_cols, inst):
    combine = str(inst.get("combine", "union")).lower()
    if combine not in {"union", "join"}:
        raise ValueError("combine must be 'union' or 'join'")
    output = []
    for item in search_cols + attr_cols:
        if item["column"] not in [x["column"] for x in output]:
            output.append(item)
    if combine == "union":
        selects = []
        for table in tables:
            expressions = []
            for item in output:
                owned = [x for x in search_cols + attr_cols
                         if x["column"] == item["column"] and x["table"] == table]
                expressions.append((owned[0]["expression"] if owned else "NULL") +
                                   f' AS "{item["column"]}"')
            selects.append(f'  SELECT {", ".join(expressions)} FROM {_qualify(db, schema, table)}')
        return "\nUNION ALL\n".join(selects), combine
    join_type = str(inst.get("join_type", "INNER")).upper()
    if join_type not in {"INNER", "LEFT", "RIGHT", "FULL"}:
        raise ValueError("join_type must be INNER, LEFT, RIGHT, or FULL")
    join_on = inst.get("join_on") or []
    if not join_on:
        raise ValueError("join mode requires join_on equality pairs")
    left, right = tables[0], tables[1]
    predicates = []
    for pair in join_on:
        if not isinstance(pair, dict) or not pair.get("left") or not pair.get("right"):
            raise ValueError("join_on items require left and right equality columns")
        left_ref = str(pair["left"]).split(".")
        right_ref = str(pair["right"]).split(".")
        if len(left_ref) != 2 or len(right_ref) != 2:
            raise ValueError("join_on references must be TABLE.COLUMN pairs")
        if left_ref[0] not in tables or right_ref[0] not in tables:
            raise ValueError("join_on references must name source tables")
        predicates.append(
            f'"{left_ref[0]}"."{safe_identifier(left_ref[1], "join column")}" = '
            f'"{right_ref[0]}"."{safe_identifier(right_ref[1], "join column")}"'
        )
    expressions = []
    for item in output:
        owner = item["table"]
        expressions.append(f'"{owner}"."{item["column"]}" AS "{item["column"]}"')
    return (f'SELECT {", ".join(expressions)} FROM {_qualify(db, schema, left)} AS "{left}" '
            f'{join_type} JOIN {_qualify(db, schema, right)} AS "{right}" ON ' +
            " AND ".join(predicates), combine)


def _build_create_ddl(session, log, inst, run_id):
    db = safe_identifier(inst["db"], "db")
    schema = safe_identifier(inst["schema"], "schema")
    service = safe_identifier(inst["service_name"], "service_name")
    tables = [safe_identifier(t, "table") for t in (inst.get("tables") or [])]
    if not tables:
        raise ValueError("instruction.tables must list at least one table")
    warehouse = inst.get("warehouse")
    source = "instruction"
    if not warehouse:
        rows = log.execute("SELECT CURRENT_WAREHOUSE() AS W")
        warehouse = _row_dict(rows[0]).get("W") if rows else None
        source = "session"
    if not warehouse:
        raise ValueError("No warehouse for the search service")
    warehouse = safe_identifier(warehouse, "warehouse")
    model = inst.get("embedding_model") or DEFAULT_EMBEDDING_MODEL
    if model not in (DEFAULT_EMBEDDING_MODEL,):
        raise ValueError(f"Unsupported embedding_model {model!r}")
    if "target_lag" in inst or "target_lag_unit" in inst:
        raise ValueError("target_lag is not configurable; use CHUNKY_DEPLOY('reindex', ...).")
    if str(inst.get("combine", "union")).lower() == "join":
        discovered, skipped = _discover_chunky_tables(session, log, db, schema, tables)
        if skipped or set(discovered) != set(tables):
            raise ValueError(
                "combine='join' only supports discovered six-column Chunky tables; "
                f"invalid sources: {skipped or sorted(set(tables) - set(discovered))}"
            )
    search = _resolve_columns(session, log, db, schema, tables,
                              inst.get("search_columns"), default=[{"column": "CHUNK"}])
    attrs = _resolve_columns(session, log, db, schema, tables,
                             inst.get("attribute_columns"),
                             default=[{"column": "PDF_NAME"}, {"column": "PAGE_NUMBER"}])
    names = []
    for item in search:
        if item["column"] not in names:
            names.append(item["column"])
    text = []
    vectors = []
    for item in search:
        target = text if str(item.get("search_type", "Hybrid")).lower() == "text" else vectors
        if item["column"] not in [x["column"] for x in target]:
            target.append(item)
    single = len(names) == 1 and len(vectors) <= 1
    query, combine = _source_query(session, log, db, schema, tables, search, attrs, inst)
    parts = [f"CREATE OR REPLACE CORTEX SEARCH SERVICE {_qualify(db, schema, service)}"]
    if single:
        parts.append(f'  ON "{names[0]}"')
    else:
        if text:
            parts.append("  TEXT INDEXES " + ", ".join(f'"{x["column"]}"' for x in text))
        if vectors:
            parts.append("  VECTOR INDEXES " + ", ".join(
                f'"{x["column"]}" (model=\'{x.get("embedding_model") or model}\')' for x in vectors))
    pk = safe_identifier(inst.get("primary_key", "CHUNK_ID"), "primary_key")
    parts.append(f'  PRIMARY KEY ("{pk}")')
    attr_names = list(dict.fromkeys(x["column"] for x in attrs))
    parts.append("  ATTRIBUTES " + ", ".join(f'"{x}"' for x in attr_names))
    parts.append(f'  WAREHOUSE = "{warehouse}"')
    parts.append(f"  TARGET_LAG = '{TARGET_LAG}'")
    if single and vectors:
        parts.append(f"  EMBEDDING_MODEL = '{model}'")
    parts.append(f"  COMMENT = '{clean_text_for_sql(inst.get('comment') or f'chunky service run {run_id}')} '")
    parts.append("AS (\n" + query + "\n)")
    return "\n".join(parts), {"warehouse": warehouse, "warehouse_source": source,
                              "multi_index": not single, "combine": combine,
                              "attributes": attr_names}


def _wait_ready(session, log, db, schema, service, timeout=900, poll=10):
    deadline = time.monotonic() + int(timeout)
    last = {}
    while time.monotonic() < deadline:
        rows = log.execute(f'DESCRIBE CORTEX SEARCH SERVICE {_qualify(db, schema, service)}')
        last = _row_dict(rows[0]) if rows else {}
        state = str(last.get("INDEXING_STATE", "")).upper()
        error = last.get("INDEXING_ERROR")
        if error:
            return False, {"state": state, "error": str(error)}
        if state in {"SUCCESS", "SUCCEEDED", "IDLE", "READY", "ACTIVE"}:
            return True, {"state": state}
        time.sleep(int(poll))
    return False, {"state": last.get("INDEXING_STATE"), "error": f"timeout after {timeout}s"}


def _verify_service(session, log, db, schema, service, query):
    full = _qualify(db, schema, service)
    payload = json.dumps({"query": query or "document", "limit": 5}).replace("'", "''")
    rows = log.execute(
        f"SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW('{full}', PARSE_JSON('{payload}')) AS RESULT"
    )
    raw = _row_dict(rows[0]).get("RESULT") if rows else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            pass
    hits = raw.get("results", raw) if isinstance(raw, dict) else raw
    if not hits:
        raise ValueError("search verification returned zero hits")
    return {"hit_count": len(hits) if isinstance(hits, list) else 1, "hits": hits}


# ---------------------------------------------------------------------------
# Command: list
# ---------------------------------------------------------------------------
def cmd_list(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    db = inst["db"]
    schema = inst["schema"]

    try:
        rows = log.execute(f'SHOW CORTEX SEARCH SERVICES IN SCHEMA "{db}"."{schema}"')
        raw = [_row_dict(r) for r in rows]
        wanted = ("name", "service_name", "indexing_state", "serving_state",
                  "target_lag", "warehouse", "refresh_mode", "source_data_num_rows")
        result = [{key: row.get(key.upper(), row.get(key)) for key in wanted if key.upper() in row or key in row}
                  for row in raw]
        return {
            "success": True, "command": "list",
            "data": {"services": result, "raw": raw}, "error": None,
            **log.to_dict(),
        }
    except Exception as e:
        return {
            "success": False, "command": "list",
            "error": str(e), "data": None,
            **log.to_dict(),
        }


def _discover_chunky_tables(session, log, db, schema, requested=None):
    rows = log.execute(
        f'SELECT TABLE_NAME, COMMENT FROM "{db}".INFORMATION_SCHEMA.TABLES '
        "WHERE TABLE_SCHEMA = ? AND TABLE_TYPE = 'BASE TABLE'",
        params=[schema],
    )
    names = set(requested or [])
    discovered, skipped = [], []
    required = {"CHUNK_ID", "PDF_NAME", "PAGE_NUMBER", "CHUNK",
                "CHUNK_METADATA", "PAGE_SCREENSHOT"}
    for row in rows:
        item = _row_dict(row)
        name = item.get("TABLE_NAME")
        if names and name not in names:
            continue
        try:
            marker = json.loads(item.get("COMMENT") or {}).get("chunky")
        except (TypeError, json.JSONDecodeError):
            marker = None
        if not marker:
            continue
        columns = _column_names(session, log, db, schema, name)
        if required <= columns:
            discovered.append(name)
        else:
            skipped.append({"table": name, "missing": sorted(required - columns)})
    return discovered, skipped


def cmd_autobuild(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    db, schema = inst["db"], inst["schema"]
    try:
        tables, skipped = _discover_chunky_tables(session, log, db, schema, inst.get("tables"))
        if not tables:
            return {"success": False, "command": "autobuild",
                    "error": "No valid Chunky tables discovered", "data": {"skipped": skipped},
                    **log.to_dict()}
        built = dict(inst)
        built["tables"] = tables
        built.setdefault("service_name", f"CSS_{schema}_ALL_DOCS")
        built.setdefault("search_columns", ["CHUNK"])
        built.setdefault("attribute_columns", ["PDF_NAME", "PAGE_NUMBER"])
        if inst.get("dry_run"):
            ddl, meta = _build_create_ddl(session, log, built, built.get("run_id") or new_run_id())
            return {"success": True, "command": "autobuild",
                    "data": {"tables": tables, "skipped": skipped, "ddl": ddl, **meta},
                    "error": None, **log.to_dict()}
        result = cmd_create(session, built)
        result.setdefault("data", {})
        result["data"].update({"discovered_tables": tables, "skipped": skipped})
        result["command"] = "autobuild"
        return result
    except Exception as exc:
        return {"success": False, "command": "autobuild", "error": str(exc),
                "data": None, **log.to_dict()}


def cmd_reindex(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    db, schema = inst.get("db"), inst.get("schema")
    services = []
    if inst.get("service_name"):
        services = [_qualify(db, schema, inst["service_name"])]
    else:
        block = table_comment.read(session, log, db, schema, inst["table"])
        services = [x.get("fqn") for x in block.get("search_services", []) if x.get("fqn")]
    results = [reindex.reindex_service(session, log, fqn) for fqn in services]
    return {"success": all(x.get("status") == "refreshed" for x in results),
            "command": "reindex", "data": {"results": results}, "error": None,
            **log.to_dict()}


# ---------------------------------------------------------------------------
# Command: describe
# ---------------------------------------------------------------------------
def cmd_describe(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    db = inst["db"]
    schema = inst["schema"]
    svc_name = inst["service_name"]
    full_svc = _qualify(db, schema, svc_name)

    try:
        rows = log.execute(f"DESCRIBE CORTEX SEARCH SERVICE IDENTIFIER('{full_svc}')")
        result = [r.as_dict() for r in rows]
        return {
            "success": True, "command": "describe",
            "data": result, "error": None,
            **log.to_dict(),
        }
    except Exception as e:
        return {
            "success": False, "command": "describe",
            "error": str(e), "data": None,
            **log.to_dict(),
        }


# ---------------------------------------------------------------------------
# Command: drop
# ---------------------------------------------------------------------------
def cmd_drop(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    db = inst["db"]
    schema = inst["schema"]
    svc_name = inst["service_name"]
    full_svc = _qualify(db, schema, svc_name)

    # Capture DDL before drop (best effort) so the caller can recreate
    # the service if needed.
    previous_ddl = _get_ddl(session, log, full_svc)

    try:
        log.execute(f"DROP CORTEX SEARCH SERVICE IDENTIFIER('{full_svc}')")
        for table in inst.get("tables") or []:
            try:
                table_comment.forget_service(
                    session, log, db, schema, table,
                    service_fqn=full_svc, now=locks._now(session, log),
                )
            except Exception:
                pass
        return {
            "success": True, "command": "drop",
            "data": {
                "dropped": svc_name,
                "previous_ddl": previous_ddl,
            },
            "error": None,
            "warning": WARNING_SEARCHSERVICE_DROP,
            "revert": {
                "command": f"CALL {PROC_DEPLOY}('REVERT', "
                           f"OBJECT_CONSTRUCT('db', '{db}', "
                           f"'schema', '{schema}', "
                           f"'service_name', '{svc_name}', "
                           f"'ddl', '{(previous_ddl or '').replace(chr(39), chr(39)*2)}'));",
                "ddl": previous_ddl,
            },
            **log.to_dict(),
        }
    except Exception as e:
        return {
            "success": False, "command": "drop",
            "error": str(e), "data": None,
            **log.to_dict(),
        }


# ---------------------------------------------------------------------------
# Command: alter
# ---------------------------------------------------------------------------
def cmd_alter(session, inst: Dict[str, Any]) -> Dict:
    if "target_lag" in inst or "target_lag_unit" in inst:
        return {"success": False, "command": "alter", "data": None,
                "error": "target_lag is not configurable.",
                "remedy": "Remove target_lag/target_lag_unit; use CHUNKY_DEPLOY('reindex', ...) for explicit refresh."}
    log = QueryLog(session)
    svc_name = inst["service_name"]
    db = inst["db"]
    schema = inst["schema"]
    full_svc = _qualify(db, schema, svc_name)

    # Capture the previous state so the caller can revert.
    previous_ddl = _get_ddl(session, log, full_svc)
    previous_lag = None
    try:
        rows = log.execute(f"SHOW CORTEX SEARCH SERVICES LIKE '{svc_name}' IN SCHEMA")
        for r in rows:
            rd = r.as_dict()
            previous_lag = rd.get("target_lag")
    except Exception:
        pass

    # Grant USAGE if roles provided
    grant_roles = inst.get("grant_roles") or []
    for r in grant_roles:
        safe_role = _safe_role(r)
        if not safe_role:
            continue
        try:
            log.execute(
                f"GRANT USAGE ON CORTEX SEARCH SERVICE {full_svc} "
                f"TO ROLE {safe_role}"
            )
        except Exception:
            pass

    return {
        "success": True, "command": "alter",
        "data": {
            "service": svc_name,
            "previous_ddl": previous_ddl,
            "previous_target_lag": previous_lag,
        },
        "error": None,
        "warning": WARNING_SEARCHSERVICE_ALTER,
        "revert": {
            "command": f"CALL {PROC_DEPLOY}('REVERT', "
                       f"OBJECT_CONSTRUCT('db', '{db}', "
                       f"'schema', '{schema}', "
                       f"'service_name', '{svc_name}', "
                       f"'ddl', '{(previous_ddl or '').replace(chr(39), chr(39)*2)}'));",
            "ddl": previous_ddl,
            "previous_target_lag": previous_lag,
        },
        **log.to_dict(),
    }


# ---------------------------------------------------------------------------
# Command: create
# ---------------------------------------------------------------------------
def _cmd_create_unlocked(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    run = inst.get("run_id") or new_run_id()
    try:
        ddl, meta = _build_create_ddl(session, log, inst, run)
    except Exception as exc:
        return {"success": False, "command": "create", "data": None,
                "error": str(exc), **log.to_dict()}
    previous_ddl = _get_ddl(
        session, log,
        _qualify(inst["db"], inst["schema"], inst["service_name"]),
    )
    try:
        log.execute(ddl)
        warnings = []
        if str(inst.get("join_type", "")).upper() in {"RIGHT", "FULL"}:
            warnings.append("RIGHT/FULL joins may use FULL refresh; inspect refresh_mode.")
        ready, ready_data = _wait_ready(
            session, log, inst["db"], inst["schema"], inst["service_name"],
            timeout=inst.get("wait_timeout_seconds", 900),
            poll=inst.get("wait_poll_seconds", 10),
        )
        if not ready:
            return {"success": False, "command": "create",
                    "data": {"ddl": ddl, "previous_ddl": previous_ddl,
                             "ready": ready_data},
                    "error": "Cortex Search Service did not become ready",
                    "warnings": warnings, **log.to_dict()}
        try:
            verify = _verify_service(
                session, log, inst["db"], inst["schema"], inst["service_name"],
                inst.get("verify_query", "document"),
            )
        except Exception as exc:
            return {"success": False, "command": "create",
                    "data": {"ddl": ddl, "previous_ddl": previous_ddl},
                    "error": f"Search verification failed: {exc}",
                    "warnings": warnings, **log.to_dict()}
        refresh_mode = None
        try:
            rows = log.execute(
                f'SHOW CORTEX SEARCH SERVICES LIKE \'{inst["service_name"]}\' '
                f'IN SCHEMA "{inst["db"]}"."{inst["schema"]}"'
            )
            if rows:
                row = _row_dict(rows[0])
                refresh_mode = row.get("REFRESH_MODE", row.get("refresh_mode"))
                if str(inst.get("join_type", "")).upper() in {"RIGHT", "FULL"} and refresh_mode:
                    warnings.append(f"Resolved refresh_mode={refresh_mode} for {inst['join_type']} join.")
        except Exception as exc:
            warnings.append(f"refresh_mode readback unavailable: {exc}")
        if inst.get("suspend_indexing", True):
            try:
                full_svc = _qualify(inst["db"], inst["schema"], inst["service_name"])
                log.execute(f"ALTER CORTEX SEARCH SERVICE {full_svc} SUSPEND INDEXING")
            except Exception as exc:
                warnings.append(f"indexing suspend failed: {exc}")
        for table in inst.get("tables") or []:
            try:
                table_comment.record_service(
                    session, log, inst["db"], inst["schema"], table,
                    service_fqn=_qualify(inst["db"], inst["schema"], inst["service_name"]),
                    run_id=run, indexing="SUSPENDED" if inst.get("suspend_indexing", True) else "ACTIVE",
                    target_lag=TARGET_LAG, embedding_model=meta.get("embedding_model"),
                    now=locks._now(session, log),
                )
            except Exception as exc:
                warnings.append(f"service comment recording failed for {table}: {exc}")
        return {
            "success": True, "command": "create",
            "data": {"service_name": inst["service_name"], "ddl": ddl,
                     "previous_ddl": previous_ddl, "refresh_mode": refresh_mode,
                     "ready": ready_data, "verify": verify, **meta},
            "error": None, "warning": WARNING_SEARCHSERVICE_CREATE,
            "warnings": warnings, **log.to_dict(),
        }
    except Exception as exc:
        return {"success": False, "command": "create",
                "data": {"ddl": ddl, "previous_ddl": previous_ddl},
                "error": str(exc), **log.to_dict()}

# ---------------------------------------------------------------------------
# Lease wrapper for service creation. Reads and inspection commands remain
# lock-free; only DDL that changes service state takes the deploy slot.
def cmd_create(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    db, schema, table = inst.get("db"), inst.get("schema"), (inst.get("tables") or [None])[0]
    try:
        rows = log.execute("SELECT CURRENT_USER() AS U")
        holder = str(rows[0]["U"]) if rows else "unknown"
    except Exception:
        holder = "unknown"
    acquired, lease = locks.acquire(
        session, log, db, schema, table, "deploy", holder=holder,
        run_id=inst.get("run_id") or new_run_id(), detail="create search service",
        force=bool(inst.get("force", False)),
    )
    if not acquired:
        return err("create", f"Table {db}.{schema}.{table} is busy ({lease.get('holder')})",
                   remedy="Wait for the active writer or pass 'force': true to override.",
                   data={"lock": lease}, log=log)
    try:
        result = _cmd_create_unlocked(session, inst)
        coordination_warning = lease.get("coordination_warning")
        if coordination_warning:
            result.setdefault("warnings", []).append(coordination_warning)
            result["warning"] = " | ".join(result["warnings"])
        return result
    finally:
        locks.release(session, log, db, schema, table, "deploy", lease.get("token"))


# ---------------------------------------------------------------------------
# Command: revert
# ---------------------------------------------------------------------------
def cmd_revert(session, inst: Dict[str, Any]) -> Dict:
    """
    Recreate a previously-dropped Cortex Search Service from saved DDL.

    Cortex Search Services are NOT time-travelable. Revert works by
    re-executing the DDL captured in the original operation's response
    (under `data.previous_ddl` / `revert.ddl`).
    """
    log = QueryLog(session)
    db = inst["db"]
    schema = inst["schema"]
    svc_name = inst.get("service_name", "")
    ddl = inst.get("ddl", "")

    if not ddl:
        return {
            "success": False, "command": "revert",
            "error": "No DDL provided. Cortex Search Services cannot be "
                     "reverted via TIME TRAVEL â€” the original DDL must be "
                     "supplied in the instruction.",
            "data": None,
            **log.to_dict(),
        }

    # Drop the current service (if any) before recreating
    if svc_name:
        full_svc = _qualify(db, schema, svc_name)
        try:
            log.execute(f"DROP CORTEX SEARCH SERVICE IF EXISTS {full_svc}")
        except Exception:
            pass

    try:
        # GET_DDL is one CREATE statement whose AS query may contain semicolons.
        log.execute(ddl.strip().rstrip(";"))
        return {
            "success": True, "command": "revert",
            "data": {"service_name": svc_name, "restored_ddl": ddl},
            "error": None,
            "warning": "Cortex Search Service restored from saved DDL. "
                       "Target lag and grants may need to be re-applied.",
            **log.to_dict(),
        }
    except Exception as e:
        return {
            "success": False, "command": "revert",
            "error": str(e), "data": None,
            **log.to_dict(),
        }


# ---------------------------------------------------------------------------
# Declarative command registry keeps help and dispatch in sync.
COMMANDS = {
    name: {"handler": handler, "summary": summary, "fields": {}}
    for name, handler, summary in (
        ("autobuild", cmd_autobuild, "Discover Chunky tables and build a service."),
        ("create", cmd_create, "Create and configure a Cortex Search Service."),
        ("list", cmd_list, "List search services."),
        ("describe", cmd_describe, "Describe a search service."),
        ("alter", cmd_alter, "Alter grants or service settings."),
        ("drop", cmd_drop, "Drop a search service."),
        ("revert", cmd_revert, "Restore a saved service definition."),
        ("reindex", cmd_reindex, "Refresh dependent Cortex Search Services."),
    )
}

_DEPLOY_BASE_FIELDS = {
    "db": {"type": "string", "required": True},
    "schema": {"type": "string", "required": True},
    "service_name": {"type": "string"},
    "run_id": {"type": "string"},
    "force": {"type": "bool", "default": False},
}
COMMANDS["create"]["fields"] = {
    **_DEPLOY_BASE_FIELDS,
    "tables": {"type": "array", "items": {"type": "string"}},
    "search_columns": {"type": "array", "items": {"type": "string|object"}},
    "attribute_columns": {"type": "array", "items": {"type": "string|object"}},
    "warehouse": {"type": "string"}, "primary_key": {"type": "string"},
    "embedding_model": {"type": "string"}, "combine": {"type": "string"},
    "join_type": {"type": "string"}, "join_on": {"type": "array"},
    "grant_roles": {"type": "array"},
}
COMMANDS["list"]["fields"] = {"db": {"type": "string", "required": True},
                                "schema": {"type": "string", "required": True}}
COMMANDS["describe"]["fields"] = {**_DEPLOY_BASE_FIELDS,
                                     "service_name": {"type": "string", "required": True}}
COMMANDS["alter"]["fields"] = {**_DEPLOY_BASE_FIELDS,
                                  "service_name": {"type": "string", "required": True},
                                  "grant_roles": {"type": "array"}}
COMMANDS["drop"]["fields"] = {**_DEPLOY_BASE_FIELDS,
                                "service_name": {"type": "string", "required": True}}
COMMANDS["revert"]["fields"] = {**_DEPLOY_BASE_FIELDS,
                                  "service_name": {"type": "string"},
                                  "ddl": {"type": "string", "required": True}}
COMMANDS["autobuild"]["fields"] = {
    **_DEPLOY_BASE_FIELDS, "tables": {"type": "array"},
    "warehouse": {"type": "string"}, "dry_run": {"type": "bool", "default": False},
}
COMMANDS["reindex"]["fields"] = {
    "db": {"type": "string", "required": True},
    "schema": {"type": "string", "required": True},
    "table": {"type": "string"}, "service_name": {"type": "string"},
}

# Main handler
# ---------------------------------------------------------------------------
def run(session, command, instruction):
    """Main entry point for the chunky_searchservice procedure."""
    cmd = (command or "").strip().lower()
    inst = instruction if isinstance(instruction, dict) else json.loads(str(instruction))

    from .registry import dispatch
    return dispatch(session, cmd, inst, COMMANDS, "CHUNKY_DEPLOY")
