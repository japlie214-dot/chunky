# Chunky Procedures — Architecture

> Headless Snowflake stored procedures that expose Chunky's ingestion,
> QA, and Cortex Search Service management as MCP-callable tools.
> The Streamlit app remains at the repository root for local UI use; the
> procedures in this directory do **not** depend on Streamlit.

## Overview

Three main procedures + five sub-procedures, all backed by Python
handlers in `procedure/utils/`. Every procedure is `EXECUTE AS CALLER`
and accepts a `(command, instruction)` signature so the same procedure
can host many commands.

**Principles:**
- Commands are natural/descriptive (not CRUD). Each procedure can host as many commands as needed.
- `EXECUTE AS CALLER` — caller's session, warehouse, role.
- No logging, no cancel, no Streamlit UI state.
- Warnings are returned in the JSON response **AFTER** execution (the
  Streamlit app showed them before — headless callers cannot do that).
- Every SQL operation runs through `QueryLog.execute`, which captures
  the Snowflake query ID and the pre-operation timestamp. Both are
  returned in the response so the caller can REVERT.
- Vision model is configurable via `instruction.cortex_model` (default
  `claude-haiku-4-5`, defined in `procedure/utils/constants.py`).
- Stage paths come from the caller — no hardcoded stage in the handlers.
- The only hardcoded value in the procedure DDL is the IMPORTS stage
  (`@DEV_DB.DNA.STG_LIB` by default); update the three deployable SQL
  files when targeting another environment.

## Procedures

### `chunky_chunks` — Ingestion Engine (Python/Snowpark)
- `ingest` — Full PDF ingestion (init table → surgical delete → AI_PARSE_DOCUMENT → vision → hybrid → chunk → insert → grant)
- `list_chunks` — List/read chunks with filters
- `update_chunk` — Edit chunk content by chunk_id
- `delete_chunks` — Delete by file/page/chunk_ids
- `revert` — Rewind the table via TIME TRAVEL using the `timestamp_before` or `query_ids` from a prior call

### `chunky_qa` — Headless QA Studio (Python/Snowpark)
- `search` — Search/list chunks with filters. Returns page screenshot URLs via `GET_PRESIGNED_URL`.
- `inspect` — Full chunk details (surgical-aware). Returns page screenshot URL.
- `generate_draft` — AI draft via Vision (render page → AI_COMPLETE). Returns draft + screenshot URL.
- `commit` — Commit draft to table
- `delete` — Delete specific chunks
- `revert` — Rewind the table via TIME TRAVEL

### `chunky_searchservice` — Cortex Search Service Manager (Python/Snowpark)
- `create` — Create service (single/multi-index, UNION ALL across tables)
- `list` — List services in schema
- `describe` — Describe service details
- `alter` — Alter target lag / grants
- `drop` — Drop service
- `revert` — Recreate a previously-dropped service from saved DDL
  (Cortex Search Services are NOT time-travelable; revert works by
  re-executing the DDL captured in the original operation's response)

## Shared utility modules (in `procedure/utils/`)

The main procedures import the bundled `utils_bundle.zip` and call their
Python handlers. Internal helper SQL wrappers are not part of the deployable
bundle.

| Module | Description |
|--------|-------------|
| `init_table.py`        | CREATE TABLE IF NOT EXISTS (or CREATE OR REPLACE for OVERWRITE) |
| `surgical_delete.py`   | DELETE with transaction safety, sorted bottom-up |
| `build_chunk_ref.py`   | Build the canonical CHUNK_REF string |
| `grant_table.py`       | GRANT with retry + role-name validation |
| `parse_pdf.py`         | AI_PARSE_DOCUMENT wrapper |

Shared utility modules in the same package:

| Module | Description |
|--------|-------------|
| `constants.py`         | Single source of truth for DB/schema/model/warnings |
| `query_log.py`         | `QueryLog` — collects query IDs + pre/post timestamps |
| `page_mapping.py`      | `RangeMapping` / `RangeMappingEngine` (surgical math) |
| `metadata_handler.py`  | Per-chunk metadata stamping |
| `revert.py`            | TIME TRAVEL-based revert helpers |

Main-procedure handlers:

| Module | Description |
|--------|-------------|
| `chunky_chunks_handler.py`         | Ingestion engine dispatch |
| `chunky_qa_handler.py`             | QA Studio dispatch |
| `chunky_searchservice_handler.py`  | Search Service Manager dispatch |

## Revert Strategy

### Tables (chunky_chunks, chunky_qa)
Native Snowflake TIME TRAVEL via `CREATE OR REPLACE TABLE <t> CLONE <t>
AT(TIMESTAMP => '<ts>')`. A backup of the pre-revert state is also
CLONE'd to `<t>_revert_backup_<epoch>` so the caller can recover if the
revert goes wrong.

The original operation returns:
```json
{
  "success": true,
  "command": "ingest",
  "data": { ... },
  "warning": "OVERWRITE mode destroyed all prior rows ...",
  "revert": {
    "command": "CALL chunky_chunks('REVERT', OBJECT_CONSTRUCT(...));",
    "timestamp_before": "2024-01-01 12:00:00.000",
    "query_ids": ["abc", "def", ...]
  },
  "query_ids": ["abc", "def", ...],
  "timestamp_before": "2024-01-01 12:00:00.000"
}
```

The caller can REVERT by either:
- Re-running the `revert.command` string verbatim, OR
- Calling `chunky_chunks('REVERT', { db, schema, table, timestamp_before })`, OR
- Calling `chunky_chunks('REVERT', { db, schema, table, query_ids })`
  (the procedure looks up `START_TIME` for each query ID via
  `INFORMATION_SCHEMA.QUERY_HISTORY()` and uses the earliest one).

Row-scoped revert is also supported (e.g. only undo the changes for a
specific file + page range):
```sql
CALL chunky_chunks('REVERT', OBJECT_CONSTRUCT(
    'db', 'DEV_DB', 'schema', 'DNA', 'table', 'T',
    'timestamp_before', '2024-01-01 12:00:00.000',
    'file', 'doc.pdf', 'page_range', [2, 5]
));
```

### Cortex Search Services (chunky_searchservice)
Cortex Search Services are NOT time-travelable. Revert works by
re-executing the DDL captured in the original operation's response
(under `data.previous_ddl` / `revert.ddl`).

## PDF Rendering
`pdf2image` + `poppler` bundled via stage import
(`@DEV_DB.DNA.STG_LIB/poppler_bundle.zip`). `RESOURCE_CONSTRAINT =
(architecture = 'x86')` required on `chunky_chunks` and `chunky_qa`
(only those need poppler for vision extraction).

## Build & Deploy

### One-time setup

1. **Build the poppler bundle** (only if `poppler_bundle.zip` is stale):
   ```bash
   cd procedure/
   bash build_poppler_bundle.sh
   ```
   This produces `procedure/poppler_bundle.zip`.

2. **Upload the bundles to your Snowflake stage**:
   ```sql
   PUT file://procedure/utils_bundle.zip @DEV_DB.DNA.STG_LIB AUTO_COMPRESS=FALSE;
   PUT file://procedure/poppler_bundle.zip @DEV_DB.DNA.STG_LIB AUTO_COMPRESS=FALSE;
   ```

3. **Deploy the procedures** by running `chunky_chunks.sql`, `chunky_qa.sql`,
   and `chunky_searchservice.sql` individually.

### Customising the target database / schema

The defaults (`DEV_DB.DNA`) match the original Chunky deployment. To
deploy elsewhere, update the `IMPORTS` stage in each of the three SQL files.

## File Structure
```
procedure/
├── ARCHITECTURE.md                  (this file)
├── README.md                        (operator quick-start)
├── build_poppler_bundle.sh          (rebuilds poppler_bundle.zip)
├── chunky_chunks.sql                (deployable procedure)
├── chunky_qa.sql                    (deployable procedure)
├── chunky_searchservice.sql         (deployable procedure)
├── poppler_bundle.zip               (binary — rebuild with build_poppler_bundle.sh)
├── utils_bundle.zip                 (binary — upload with the procedures)
├── utils/                           (Python handler modules — source of truth)
├── script/                          (Local scripts, NOT Snowflake procedures)
│   ├── README.md
│   ├── upload_to_stage.py           (browser-auth file uploader)
│   ├── make_dummy_pdf.py            (regenerates the test PDF)
│   └── pdf/
│       └── fy2024-tbk-investor-presentation.pdf  (5-page dummy PDF)
└── snowflake-mcp/                   (existing MCP server for Claude Desktop)
```
