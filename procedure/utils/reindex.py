"""Best-effort RESUME -> REFRESH -> SUSPEND lifecycle for dependent services."""
from __future__ import annotations
import time

def reindex_service(session, log, service_fqn: str, *, wait=True,
                    timeout_seconds=900, poll_seconds=10,
                    restore_suspended=True) -> dict:
    result = {"service": service_fqn, "status": "unknown", "error": None,
              "was_suspended": None, "duration_seconds": None}
    started = time.monotonic()
    try:
        rows = log.execute(f"DESCRIBE CORTEX SEARCH SERVICE {service_fqn}")
        state = str(rows[0].get("INDEXING_STATE", "") if rows else "").upper()
        result["was_suspended"] = state == "SUSPENDED"
        if result["was_suspended"]:
            log.execute(f"ALTER CORTEX SEARCH SERVICE {service_fqn} RESUME INDEXING")
        log.execute(f"ALTER CORTEX SEARCH SERVICE {service_fqn} REFRESH")
        if wait:
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                rows = log.execute(f"DESCRIBE CORTEX SEARCH SERVICE {service_fqn}")
                state = str(rows[0].get("INDEXING_STATE", "") if rows else "").upper()
                result["indexing_state"] = state
                if state in {"SUCCESS", "SUCCEEDED", "IDLE", "READY"}:
                    break
                time.sleep(poll_seconds)
            else:
                result["status"] = "timeout"
                result["error"] = f"still {state} after {timeout_seconds}s"
                return result
        if restore_suspended and result["was_suspended"]:
            log.execute(f"ALTER CORTEX SEARCH SERVICE {service_fqn} SUSPEND INDEXING")
        result["status"] = "refreshed"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
    finally:
        result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result
