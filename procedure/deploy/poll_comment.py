"""Poll a table COMMENT from one independent Snowflake connector session.

This is a live-validation utility, not procedure code.  Every JSONL record
contains the connector session ID, query ID, unparsed COMMENT string, and the
decoded ingest lease so external visibility can be audited without relying on
the stored procedure's Snowpark session.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from deploy.auth import connect


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--samples", type=int, default=90)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    conn = connect(database=args.db, schema=args.schema, quiet=True)
    cur = conn.cursor()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        cur.execute("ALTER SESSION SET USE_CACHED_RESULT = FALSE")
        cur.execute(
            "SELECT CURRENT_SESSION(), CURRENT_USER(), CURRENT_ROLE(), "
            "CURRENT_WAREHOUSE()"
        )
        session_id, user, role, warehouse = cur.fetchone()
        header = {
            "event": "poller_session",
            "session_id": session_id,
            "user": user,
            "role": role,
            "warehouse": warehouse,
        }
        with args.output.open("w", encoding="utf-8") as stream:
            print(json.dumps(header), flush=True)
            stream.write(json.dumps(header) + "\n")
            for sample in range(1, args.samples + 1):
                cur.execute(
                    f'SELECT CURRENT_TIMESTAMP(), CURRENT_SESSION(), COMMENT '
                    f'FROM "{args.db}".INFORMATION_SCHEMA.TABLES '
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                    (args.schema, args.table),
                )
                observed_at, observed_session, raw_comment = cur.fetchone()
                try:
                    block = json.loads(raw_comment or "{}").get("chunky", {})
                except (TypeError, json.JSONDecodeError):
                    block = {}
                lock = (block.get("locks") or {}).get("ingest")
                record = {
                    "event": "comment_sample",
                    "sample": sample,
                    "observed_at": str(observed_at),
                    "session_id": observed_session,
                    "query_id": cur.sfqid,
                    "lock_present": bool(lock),
                    "run_id": (lock or {}).get("run_id"),
                    "progress": (lock or {}).get("progress"),
                    "raw_comment": raw_comment,
                }
                line = json.dumps(record, separators=(",", ":"), default=str)
                print(line, flush=True)
                stream.write(line + "\n")
                stream.flush()
                time.sleep(args.interval)
    finally:
        cur.close()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
