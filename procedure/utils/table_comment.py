"""Read-modify-write protocol stored in chunk table COMMENT."""
from __future__ import annotations
import json
from ._shared import qualify, clean_text_for_sql

KEY = "chunky"
SCHEMA_VERSION = 2
MAX_COMMENT_CHARS = 8000
MAX_SOURCES = 50

def read(session, log, db, schema, table):
    try:
        # CURRENT_TIMESTAMP makes this metadata read ineligible for persisted
        # result reuse. Lease coordination must never observe a cached COMMENT.
        rows = log.execute(f'SELECT COMMENT AS C, CURRENT_TIMESTAMP() AS READ_AT '
                           f'FROM "{db}".INFORMATION_SCHEMA.TABLES '
                           "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?", params=[schema, table])
        return (json.loads(rows[0]["C"] or {}).get(KEY, {}) if rows else {}) or {}
    except Exception:
        return {}

def write(session, log, db, schema, table, block):
    block["schema_version"] = SCHEMA_VERSION
    payload = json.dumps({KEY: block}, separators=(",", ":"))
    while len(payload) > MAX_COMMENT_CHARS and len(block.get("sources", [])) > 1:
        block["sources"] = sorted(block["sources"], key=lambda x: x.get("last_ingested_at", ""))[1:]
        payload = json.dumps({KEY: block}, separators=(",", ":"))
    log.execute(f"COMMENT ON TABLE {qualify(db, schema, table)} IS '{clean_text_for_sql(payload)}'")

def record_ingest(session, log, db, schema, table, *, pdf_name, pages, chunks, run_id, actor, now):
    block = read(session, log, db, schema, table)
    block.setdefault("created_at", now); block.setdefault("created_by", actor)
    block.setdefault("search_services", [])
    previous = next((x for x in block.get("sources", []) if x.get("pdf_name") == pdf_name), None)
    block["sources"] = [x for x in block.get("sources", []) if x.get("pdf_name") != pdf_name]
    if previous:
        pages += int(previous.get("pages") or 0)
        chunks += int(previous.get("chunks") or 0)
    block["sources"].append({"pdf_name": pdf_name, "pages": pages, "chunks": chunks,
                              "last_run_id": run_id, "last_ingested_at": now})
    block["sources"] = block["sources"][-MAX_SOURCES:]
    block["last_modified_at"] = now; block["last_run_id"] = run_id
    write(session, log, db, schema, table, block)
    return block

def record_service(session, log, db, schema, table, *, service_fqn, run_id, indexing,
                   target_lag, embedding_model, now):
    block = read(session, log, db, schema, table)
    block["search_services"] = [x for x in block.get("search_services", []) if x.get("fqn") != service_fqn]
    block["search_services"].append({"fqn": service_fqn, "created_at": now,
                                      "created_by_run_id": run_id, "indexing": indexing,
                                      "target_lag": target_lag, "embedding_model": embedding_model})
    block["last_modified_at"] = now; write(session, log, db, schema, table, block)

def forget_service(session, log, db, schema, table, *, service_fqn, now):
    block = read(session, log, db, schema, table)
    block["search_services"] = [x for x in block.get("search_services", []) if x.get("fqn") != service_fqn]
    block["last_modified_at"] = now; write(session, log, db, schema, table, block)

def services_for(session, log, db, schema, table):
    return [x["fqn"] for x in read(session, log, db, schema, table).get("search_services", []) if x.get("fqn")]
