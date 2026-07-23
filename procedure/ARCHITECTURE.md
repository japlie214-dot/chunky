# Chunky Procedures — Architecture

> See `chunky-drawing-board.html` (workspace root) for the full visual version.

## Overview

Three Snowflake Stored Procedures exposing Chunky's existing Streamlit functionality as headless MCP tools. Streamlit app untouched.

**Principles:**
- Commands are natural/descriptive (not CRUD). Each procedure can have as many commands as needed.
- `EXECUTE AS CALLER` — caller's session, warehouse, role
- No logging, no cancel, no state
- Vision model: hardcoded `claude-haiku-4-5`
- Stage paths: full path from caller
- No schema changes

## Procedures

### `chunky_chunks` — Ingestion Engine (Python/Snowpark)
- `ingest` — Full PDF ingestion (init table → surgical delete → AI_PARSE_DOCUMENT → vision → hybrid → chunk → insert → grant)
- `list_chunks` — List/read chunks with filters
- `update_chunk` — Edit chunk content by chunk_id
- `delete_chunks` — Delete by file/page/chunk_ids

### `chunky_qa` — Headless QA Studio (Python/Snowpark)
- `search` — Search/list chunks with filters. Returns page screenshot URLs via `GET_PRESIGNED_URL`.
- `inspect` — Full chunk details (surgical-aware). Returns page screenshot URL.
- `generate_draft` — AI draft via Vision (render page → AI_COMPLETE). Returns draft + screenshot URL.
- `commit` — Commit draft to table
- `delete` — Delete specific chunks

### `chunky_searchservice` — Cortex Search Service Manager (SQL)
- `create` — Create service (single/multi-index, UNION ALL across tables)
- `list` — List services in schema
- `describe` — Describe service details
- `alter` — Alter target lag / grants
- `drop` — Drop service

## Sub-Procedures (in `sub/`)
Only when shared by 2+ mains:
- `chunky_internal_init_table` (SQL) — CREATE TABLE if not exists
- `chunky_internal_surgical_delete` (SQL) — DELETE with transaction safety
- `chunky_internal_build_chunk_ref` (SQL) — Build CHUNK_REF string
- `chunky_internal_grant_table` (SQL) — GRANT with retry
- `chunky_internal_parse_pdf` (Python) — AI_PARSE_DOCUMENT wrapper

## PDF Rendering
`pdf2image` + `poppler` bundled via stage import (`@DEV_DB.DNA.STG_LIB/poppler_bundle.zip`).
`RESOURCE_CONSTRAINT = (architecture = 'x86')` required.

## File Structure
```
procedure/
├── ARCHITECTURE.md
├── build_poppler_bundle.sh
├── 00_install_all.sql
├── sub/ (5 internal helpers)
├── chunky_chunks.sql
├── chunky_qa.sql
└── chunky_searchservice.sql
```
