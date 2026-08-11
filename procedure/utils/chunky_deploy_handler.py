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
from ._shared import make_revert_command, err
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


# ---------------------------------------------------------------------------
# Command: list
# ---------------------------------------------------------------------------
def cmd_list(session, inst: Dict[str, Any]) -> Dict:
    log = QueryLog(session)
    db = inst["db"]
    schema = inst["schema"]

    sql = """
        SELECT *
        FROM TABLE(INFORMATION_SCHEMA.CORTEX_SEARCH_SERVICES(
            DATABASE_NAME => ?,
            SCHEMA_NAME => ?
        ))
    """
    try:
        rows = log.execute(sql, params=[db, schema])
        result = [r.as_dict() for r in rows]
        return {
            "success": True, "command": "list",
            "data": result, "error": None,
            **log.to_dict(),
        }
    except Exception as e:
        return {
            "success": False, "command": "list",
            "error": str(e), "data": None,
            **log.to_dict(),
        }


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
    svc_name = inst["service_name"]
    db = inst["db"]
    schema = inst["schema"]
    full_svc = _qualify(db, schema, svc_name)

    tables = inst.get("tables") or []
    search_cols = inst.get("search_columns") or []
    attr_cols = inst.get("attribute_columns") or []
    if "target_lag" in inst or "target_lag_unit" in inst:
        return {"success": False, "command": "create", "data": None,
                "error": "target_lag is not configurable.",
                "remedy": "Use CHUNKY_DEPLOY('reindex', ...) for explicit refresh."}
    warehouse_rows = log.execute("SELECT CURRENT_WAREHOUSE() AS W")
    warehouse = inst.get("warehouse") or (warehouse_rows[0].get("W") if warehouse_rows else None)
    if not warehouse:
        return {"success": False, "command": "create", "data": None,
                "error": "No warehouse for the search service.",
                "remedy": "Pass 'warehouse' or set a session warehouse."}
    target_lag_str = TARGET_LAG
    grant_roles = inst.get("grant_roles") or []

    # Capture existing service DDL (if any) so the caller can revert.
    previous_ddl = _get_ddl(session, log, full_svc)

    # Categorise search columns
    text_cols: List[str] = []
    vector_cols: List[Dict[str, str]] = []
    attr_list: List[str] = []
    all_search_cols: List[str] = []

    for sc in search_cols:
        col = sc.get("column")
        if not col:
            continue
        stype = (sc.get("search_type") or "Hybrid")
        model = sc.get("embedding_model") or ""
        if "Text" in stype and col not in text_cols:
            text_cols.append(col)
        if "Vector" in stype or "Hybrid" in stype:
            vector_cols.append({"col": col, "model": model})
        if col not in all_search_cols:
            all_search_cols.append(col)

    for ac in attr_cols:
        col = ac.get("column")
        if col and col not in attr_list:
            attr_list.append(col)

    use_single = (len(all_search_cols) == 1 and len(vector_cols) <= 1)

    # Build the DDL
    ddl_parts: List[str] = [
        f"CREATE OR REPLACE CORTEX SEARCH SERVICE {full_svc}",
    ]

    if use_single:
        ddl_parts.append(f'  ON "{all_search_cols[0]}"')
        if attr_list:
            attr_clause = ", ".join(f'"{c}"' for c in attr_list)
            ddl_parts.append(f"  ATTRIBUTES {attr_clause}")
        ddl_parts.append(f"  TARGET_LAG = '{target_lag_str}'")
        if vector_cols:
            ddl_parts.append(f"  EMBEDDING_MODEL = '{vector_cols[0]['model'] or DEFAULT_EMBEDDING_MODEL}'")
    else:
        if text_cols:
            tc = ", ".join(f'"{c}"' for c in text_cols)
            ddl_parts.append(f"  TEXT INDEXES {tc}")
        if vector_cols:
            vc = ", ".join(
                f'"{v["col"]}" (model=\'{v["model"]}\')' for v in vector_cols
            )
            ddl_parts.append(f"  VECTOR INDEXES {vc}")
        if attr_list:
            ac2 = ", ".join(f'"{c}"' for c in attr_list)
            ddl_parts.append(f"  ATTRIBUTES {ac2}")
        ddl_parts.append(f'  WAREHOUSE = "{warehouse}"')
        ddl_parts.append(f"  TARGET_LAG = '{target_lag_str}'")

    # Build UNION ALL query
    all_cols: List[str] = []
    for c in all_search_cols:
        if c not in all_cols:
            all_cols.append(c)
    for c in attr_list:
        if c not in all_cols:
            all_cols.append(c)

    union_parts: List[str] = []
    for tbl_name in tables:
        full_tbl = _qualify(db, schema, tbl_name)
        select_parts: List[str] = []
        for cj, col in enumerate(all_cols):
            # Check if this table has this column in search_cols or attr_cols
            tbl_has_col = any(
                (sc.get("table") == tbl_name and sc.get("column") == col)
                for sc in search_cols
            ) or any(
                (ac.get("table") == tbl_name and ac.get("column") == col)
                for ac in attr_cols
            )
            if cj > 0:
                select_parts.append(", ")
            if tbl_has_col:
                select_parts.append(f'"{col}"')
            else:
                select_parts.append(f'NULL AS "{col}"')
        union_parts.append(
            f"  SELECT {''.join(select_parts)} FROM {full_tbl}"
        )

    as_query = "\nUNION ALL\n".join(union_parts)
    ddl_parts.append(f"AS (\n{as_query}\n);")
    ddl = "\n".join(ddl_parts)

    # Execute the CREATE
    try:
        log.execute(ddl)

        # Grant USAGE on service
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
            "success": True, "command": "create",
            "data": {
                "service_name": svc_name,
                "ddl": ddl,
                "previous_ddl": previous_ddl,
            },
            "error": None,
            "warning": WARNING_SEARCHSERVICE_CREATE,
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
            "success": False, "command": "create",
            "data": {"ddl": ddl, "previous_ddl": previous_ddl},
            "error": str(e),
            **log.to_dict(),
        }


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
        # Execute each statement in the DDL (GET_DDL returns multi-statement)
        for stmt in ddl.split(";"):
            stmt = stmt.strip()
            if stmt:
                log.execute(stmt)
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
        ("create", cmd_create, "Create and configure a Cortex Search Service."),
        ("list", cmd_list, "List search services."),
        ("describe", cmd_describe, "Describe a search service."),
        ("alter", cmd_alter, "Alter grants or service settings."),
        ("drop", cmd_drop, "Drop a search service."),
        ("revert", cmd_revert, "Restore a saved service definition."),
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
    "tables": {"type": "array"}, "search_columns": {"type": "array"},
    "attribute_columns": {"type": "array"}, "warehouse": {"type": "string"},
    "grant_roles": {"type": "array"},
}
COMMANDS["list"]["fields"] = {"db": {"type": "string"}, "schema": {"type": "string"}}
COMMANDS["describe"]["fields"] = {**_DEPLOY_BASE_FIELDS}
COMMANDS["alter"]["fields"] = {**_DEPLOY_BASE_FIELDS, "grant_roles": {"type": "array"}}
COMMANDS["drop"]["fields"] = {**_DEPLOY_BASE_FIELDS}
COMMANDS["revert"]["fields"] = {**_DEPLOY_BASE_FIELDS, "ddl": {"type": "string"}}

# Main handler
# ---------------------------------------------------------------------------
def run(session, command, instruction):
    """Main entry point for the chunky_searchservice procedure."""
    cmd = (command or "").strip().lower()
    inst = instruction if isinstance(instruction, dict) else json.loads(str(instruction))

    from .registry import dispatch
    return dispatch(session, cmd, inst, COMMANDS, "CHUNKY_DEPLOY")
