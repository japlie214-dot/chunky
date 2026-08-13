"""Best-effort RESUME -> REFRESH -> SUSPEND lifecycle for dependent services."""
from __future__ import annotations
import time


def _service_parts(service_fqn: str):
    """Return db, schema, name for quoted or canonical unquoted FQNs."""
    parts = [p.strip().strip('"') for p in str(service_fqn).split('.')]
    if len(parts) >= 3:
        return parts[-3], parts[-2], parts[-1]
    return None, None, parts[-1]

def reindex_service(session, log, service_fqn: str, *, wait=True,
                    timeout_seconds=900, poll_seconds=10,
                    restore_suspended=True) -> dict:
    result = {"service": service_fqn, "status": "unknown", "error": None,
              "was_suspended": None, "duration_seconds": None}
    started = time.monotonic()
    try:
        rows = log.execute(f"DESCRIBE CORTEX SEARCH SERVICE {service_fqn}")
        first = rows[0].as_dict() if rows and hasattr(rows[0], "as_dict") else (dict(rows[0]) if rows else {})
        state = str(first.get("INDEXING_STATE", first.get("indexing_state", ""))).upper()
        result["was_suspended"] = state == "SUSPENDED"
        log.execute(f"ALTER CORTEX SEARCH SERVICE {service_fqn} RESUME INDEXING")
        log.execute(f"ALTER CORTEX SEARCH SERVICE {service_fqn} REFRESH")
        if wait:
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                rows = log.execute(f"DESCRIBE CORTEX SEARCH SERVICE {service_fqn}")
                row = rows[0].as_dict() if rows and hasattr(rows[0], "as_dict") else (dict(rows[0]) if rows else {})
                state = str(row.get("INDEXING_STATE", row.get("indexing_state", ""))).upper()
                try:
                    db, schema, name = _service_parts(service_fqn)
                    show_sql = f"SHOW CORTEX SEARCH SERVICES LIKE '{name}'"
                    if db and schema:
                        show_sql += f' IN SCHEMA "{db}"."{schema}"'
                    shown = log.execute(show_sql)
                    if shown:
                        shown_row = shown[0].as_dict() if hasattr(shown[0], "as_dict") else dict(shown[0])
                        serving = str(shown_row.get("SERVING_STATE", shown_row.get("serving_state", ""))).upper()
                    else:
                        serving = ""
                except Exception:
                    serving = ""
                result["indexing_state"] = state
                if state in {"SUCCESS", "SUCCEEDED", "IDLE", "READY"} or (state == "ACTIVE" and serving in {"ACTIVE", "SERVING", "READY"}):
                    break
                time.sleep(poll_seconds)
            else:
                result["status"] = "timeout"
                result["error"] = f"still {state} after {timeout_seconds}s"
                return result
        if restore_suspended:
            log.execute(f"ALTER CORTEX SEARCH SERVICE {service_fqn} SUSPEND INDEXING")
        result["status"] = "refreshed"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
    finally:
        # A timeout, cancellation or polling exception must not leave a
        # normally suspended service indexing indefinitely.
        if result.get("was_suspended") and result.get("status") != "refreshed":
            try:
                log.execute(f"ALTER CORTEX SEARCH SERVICE {service_fqn} SUSPEND INDEXING")
            except Exception:
                pass
        result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result
