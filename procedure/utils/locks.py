"""Advisory table-comment leases for ingest, QA, and deploy writers."""
from __future__ import annotations
import random
import time
from datetime import datetime, timedelta, timezone
from . import table_comment
from .ulid import new_ulid

_last_heartbeat = {}

def _now(session, log):
    try:
        rows = log.execute("SELECT TO_VARCHAR(CURRENT_TIMESTAMP()) AS TS")
        if rows:
            return str(rows[0]["TS"])
    except Exception:
        pass
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _dt(value):
    try:
        text = str(value).strip().replace("Z", "+00:00")
        # Snowflake commonly renders timestamps as
        # ``YYYY-MM-DD HH:MM:SS.mmm +0700``; normalize the separated offset
        # before handing it to datetime.fromisoformat.
        text = text.replace(" +", "+")
        return datetime.fromisoformat(text)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)

def _expired(lock, now):
    return _dt(lock.get("expires_at")) <= _dt(now)

def _role(session, log):
    try:
        rows = log.execute("SELECT CURRENT_ROLE() AS R")
        return str(rows[0]["R"]) if rows else "unknown"
    except Exception:
        return "unknown"

def _write_slot(session, log, db, schema, table, slot, value):
    block = table_comment.read(session, log, db, schema, table)
    locks = dict(block.get("locks") or {})
    locks[slot] = value
    block["locks"] = locks
    table_comment.write(session, log, db, schema, table, block)


def _write_slot_committed(session, log, db, schema, table, slot, value):
    """Publish one lease mutation in an explicit procedure-scoped transaction."""
    log.execute("BEGIN")
    try:
        _write_slot(session, log, db, schema, table, slot, value)
        log.execute("COMMIT")
    except Exception:
        try:
            log.execute("ROLLBACK")
        except Exception:
            pass
        raise

def acquire(session, log, db, schema, table, slot, *, holder, run_id,
            detail="", ttl_seconds=1800, force=False):
    try:
        block = table_comment.read(session, log, db, schema, table)
        current = (block.get("locks") or {}).get(slot)
        now = _now(session, log)
        if current and not force and not _expired(current, now):
            return False, current
        token = new_ulid()
        info = {"holder": holder, "role": _role(session, log), "token": token,
                "run_id": run_id, "since": now, "detail": detail,
                "expires_at": (_dt(now) + timedelta(seconds=ttl_seconds)).isoformat(),
                "progress": None, "ttl_seconds": ttl_seconds}
        _write_slot_committed(session, log, db, schema, table, slot, info)
        time.sleep(random.uniform(.8, 2.5))
        winner = (table_comment.read(session, log, db, schema, table).get("locks") or {}).get(slot) or {}
        if winner.get("token") == token:
            print(f"[chunky] lease verified slot={slot} token={token}", flush=True)
            return True, winner
        if not winner:
            # The comment write was not observable (for example a mocked
            # session or a transient metadata read). Treat that as a lost
            # advisory coordination attempt, not as a data-write failure.
            print(f"[chunky] lease verification inconclusive slot={slot} token={token}", flush=True)
            return True, {"coordination_warning": (
                              "lease verification was inconclusive: comment read "
                              "did not contain the proposed token"),
                          "token": None}
        print(f"[chunky] lease lost slot={slot} proposed={token} winner={winner.get('token')}", flush=True)
        return False, winner
    except Exception as exc:
        # Coordination is advisory. A comment failure must not turn a data
        # write into an application failure; the caller proceeds unlocked.
        print(f"[chunky] lease coordination exception slot={slot}: {exc}", flush=True)
        return True, {"coordination_warning": f"lease coordination failed: {exc}",
                      "token": None}

def heartbeat(session, log, db, schema, table, slot, token, progress,
              min_interval_seconds=30):
    key = (db, schema, table, slot, token)
    now_mono = time.monotonic()
    if now_mono - _last_heartbeat.get(key, 0) < min_interval_seconds:
        return
    try:
        block = table_comment.read(session, log, db, schema, table)
        current = (block.get("locks") or {}).get(slot) or {}
        if current.get("token") != token:
            return
        current["progress"] = {**progress, "updated_at": _now(session, log)}
        ttl = int(current.get("ttl_seconds") or 1800)
        current["expires_at"] = (_dt(_now(session, log)) + timedelta(seconds=ttl)).isoformat()
        _write_slot_committed(session, log, db, schema, table, slot, current)
        _last_heartbeat[key] = now_mono
    except Exception:
        pass

def release(session, log, db, schema, table, slot, token):
    try:
        block = table_comment.read(session, log, db, schema, table)
        locks = dict(block.get("locks") or {})
        current = locks.get(slot) or {}
        if current.get("token") == token:
            locks[slot] = None
            block["locks"] = locks
            _write_slot_committed(session, log, db, schema, table, slot, None)
    except Exception:
        pass


def describe(session, log, db, schema, table):
    """Return all lease slots with an explicit stale marker for each holder."""
    block = table_comment.read(session, log, db, schema, table)
    now = _now(session, log)
    locks = dict(block.get("locks") or {})
    for slot in ("ingest", "qa", "deploy"):
        value = locks.get(slot)
        if value:
            value = dict(value)
            value["stale"] = _expired(value, now)
            locks[slot] = value
        else:
            locks[slot] = None
    return locks
