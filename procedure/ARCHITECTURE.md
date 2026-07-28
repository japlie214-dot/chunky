# Chunky Procedures — Architecture

> Headless Snowflake stored procedures that expose Chunky's ingestion,
> QA, and Cortex Search Service management as MCP-callable tools.
> The Streamlit app remains at the repository root for local UI use; the
> procedures in this directory do **not** depend on Streamlit.

## Overview

Three main procedures, all backed by Python handlers in
`procedure/utils/`. Every procedure is `EXECUTE AS CALLER` and accepts a
`(command, instruction)` signature so the same procedure can host many
commands.

**Principles:**
- Commands are natural/descriptive (not CRUD). Each procedure can host as many commands as needed.
- `EXECUTE AS CALLER` — caller's session, warehouse, role.
- No logging, no cancel, no Streamlit UI state.
- Warnings are returned in the JSON response **AFTER** execution (the
  Streamlit app showed them before — headless callers cannot do that).
  The response includes both `warning` (joined with `|`) and `warnings`
  (an array) so callers can surface each one individually.
- Every SQL operation runs through `QueryLog.execute`, which captures
  the Snowflake query ID and the pre-operation timestamp. Both are
  returned in the response so the caller can REVERT.
- Vision model is configurable via `instruction.cortex_model` (default
  `claude-haiku-4-5`, defined in `procedure/utils/constants.py`).
- Stage paths come from the caller — no hardcoded stage in the handlers.
- The only hardcoded value in the procedure DDL is the IMPORTS stage
  (`@DEV_DB.DNA.STG_LIB` by default); override via the `LIB_STAGE` env
  var when running `build_bundle.py --sql`.

## Procedures

### `chunky_chunks` — Ingestion Engine (Python/Snowpark)
- `ingest` — Full PDF ingestion (init table → surgical delete → AI_PARSE_DOCUMENT → vision → hybrid repair → chunk → insert → grant)
- `list_chunks` — List/read chunks with filters
- `list_chunks_csv` — Same as list_chunks but returns a single CSV string in `data.csv`
- `update_chunk` — Edit chunk content by chunk_id
- `delete_chunks` — Delete by file/range/chunk_ids
- `inspect_quality` — Run QualityInspector on chunks (no modification)
- `batch_ingest` — Run multiple ingest jobs in one CALL (`instruction.jobs = [...]`)
- `estimate_cost` — Pre-flight cost estimate (credits + USD) without ingesting
- `revert` — Rewind the table via TIME TRAVEL using `timestamp_before` or `query_ids`

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
| `constants.py`         | Single source of truth for DB/schema/model/warnings + procedure-name constants |
| `query_log.py`         | `QueryLog` — collects query IDs + pre/post timestamps |
| `page_mapping.py`      | `RangeMapping` / `RangeMappingEngine` (surgical math) |
| `metadata_handler.py`  | Per-chunk metadata stamping |
| `revert.py`            | TIME TRAVEL-based revert helpers (safe rename pattern) |
| `_shared.py`           | Pure helpers shared across handlers (qualify, clean_text_for_sql, sanitize_nbsp, build_chunk_ref, safe_role, make_revert_command) |
| `poppler_bootstrap.py` | Single source of truth for resolving poppler binaries from the bundle |
| `layout_parse.py`      | Normalises AI_PARSE_DOCUMENT responses (handles both `{pages: [...]}` and flat `{content, metadata}` shapes) |
| `quality_inspector.py` | Verbatim port of Streamlit-side QualityInspector (defect detection) |
| `hybrid_repair.py`     | Headless port of Streamlit hybrid repair (Vision re-extract of defective layout chunks) |
| `prompts.py`           | Self-contained copy of the Vision/Layout prompts |

Main-procedure handlers:

| Module | Description |
|--------|-------------|
| `chunky_chunks_handler.py`         | Ingestion engine dispatch |
| `chunky_qa_handler.py`             | QA Studio dispatch |
| `chunky_searchservice_handler.py`  | Search Service Manager dispatch |

## Default Extraction Strategy

By default, `chunky_chunks('ingest', ...)` runs **Vision-only** (no
Layout). Callers can opt in to other strategies via the `layout` /
`vision` flags:

| `layout` | `vision` | Strategy | Behaviour |
|----------|----------|----------|-----------|
| `false` (default) | `true` (default) | Vision-only | Render each PDF page to an image, call Vision AI for markdown extraction. Slower but higher fidelity for complex layouts. |
| `true` | `false` | Layout-only | Call AI_PARSE_DOCUMENT with `mode: LAYOUT`. Fastest. Falls back to PLACEHOLDER chunks for any pages the parser misses. |
| `true` | `true` | Layout + Vision (hybrid repair) | Layout runs first, then QualityInspector flags defective chunks, then Vision re-extracts those pages. Defective chunks are tagged `CHUNK_TYPE='ENHANCED'`. |
| `false` | `false` | (invalid) | Returns an error — at least one strategy must be enabled. |

## AI_PARSE_DOCUMENT Response Shapes

`AI_PARSE_DOCUMENT` returns one of two JSON shapes depending on whether
`page_filter` was supplied in the options:

### Shape A — `page_filter` supplied (Page Range or Surgical mode)
```json
{
  "pages": [{"index": 0, "content": "..."}, {"index": 1, "content": "..."}],
  "metadata": {"pageCount": 2, ...}
}
```

### Shape B — no `page_filter` (Full Doc mode)
```json
{
  "content": "page 1 markdown\fpage 2 markdown\fpage 3 markdown",
  "metadata": {"pageCount": 3, ...}
}
```

The flat content uses the form-feed character (`\f`) as a page
separator. `procedure/utils/layout_parse.py` normalises both shapes
into a list of `{"index": int, "content": str}` dicts so the ingestion
handler can treat them uniformly. When the flat shape is detected, a
`WARNING_LAYOUT_FLAT_RESPONSE` is appended to the response.

## Revert Strategy

### Tables (chunky_chunks, chunky_qa)

Native Snowflake TIME TRAVEL via a **safe rename pattern**:

```sql
-- 1. Rename the current (potentially corrupt) table to a backup name.
--    RENAME preserves the table's Time Travel history because the
--    physical table is unchanged — only its identifier moves.
ALTER TABLE <t> RENAME TO <t>_revert_backup_<epoch>;

-- 2. Recreate the original table from TIME TRAVEL of the renamed
--    backup. Source (renamed) != target (original name), so this
--    CLONE works.
CREATE TABLE <t> CLONE <t>_revert_backup_<epoch>
    AT(TIMESTAMP => '<ts>'::TIMESTAMP_LTZ);

-- 3. The backup table is left in place — drop it once you've verified
--    the revert.
```

**Why not `CREATE OR REPLACE TABLE X CLONE X AT(...)`?** That statement
fails because Snowflake resolves the source reference before dropping
the target, and the clone operation conflicts when source == target.

The original operation returns:
```json
{
  "success": true,
  "command": "ingest",
  "data": { ... },
  "warning": "OVERWRITE mode destroyed all prior rows ...",
  "warnings": ["OVERWRITE mode destroyed all prior rows ..."],
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
    'file', 'doc.pdf', 'range', [2, 5]
));
```

### Cortex Search Services (chunky_searchservice)
Cortex Search Services are NOT time-travelable. Revert works by
re-executing the DDL captured in the original operation's response
(under `data.previous_ddl` / `revert.ddl`).

## PDF Rendering

`pdf2image` + `poppler` are bundled into the **single** `utils_bundle.zip`.
Snowflake extracts the zip to `/home/udf/<id>/`, producing:

```
/home/udf/<id>/
├── chunky_utils/                ← Python handlers
├── poppler_bundle/
│   └── poppler/
│       ├── bin/                 ← pdftoppm, pdfinfo, pdftotext
│       └── lib/                 ← libc.so.6, libgcc_s.so.1, ...
└── pdf2image/                   ← Python package
```

`procedure/utils/poppler_bootstrap.py` resolves the poppler path
**one level up from `chunky_utils/`** (i.e. `/home/udf/<id>/poppler_bundle/...`)
and adds the udf root to `sys.path` so `from pdf2image import ...` works.

The bundled poppler binaries are Linux x86_64 ELF executables. They will
only run on Snowflake warehouses whose compute nodes are x86_64 Linux
(the default on most Snowflake accounts). If your account uses ARM-based
warehouses, Vision extraction will fail at `pdf2image.convert_from_bytes`
— in that case, disable Vision (`vision: false` in the instruction JSON)
and use Layout-only ingestion, or rebuild the bundle with ARM-compatible
poppler binaries.

We deliberately do **not** set `RESOURCE_CONSTRAINT = (architecture =
'x86')` on the procedures because that clause is not available on all
Snowflake editions. Callers are responsible for ensuring the warehouse
running `chunky_chunks` / `chunky_qa` is x86-compatible when Vision is
enabled.

## Build & Deploy

### One-time setup

1. **Build the single bundle**:
   ```bash
   python3 procedure/build_bundle.py --clean --sql
   ```
   This produces `procedure/utils_bundle.zip` (containing chunky_utils/
   + poppler_bundle/ + pdf2image/) and renders the .sql files from
   .j2 templates.

2. **Upload the bundle to your Snowflake stage**:
   ```sql
   PUT file://procedure/utils_bundle.zip @DEV_DB.DNA.STG_LIB AUTO_COMPRESS=FALSE;
   ```

3. **Deploy the procedures** by running `chunky_chunks.sql`, `chunky_qa.sql`,
   and `chunky_searchservice.sql` individually.

### Customising the target database / schema

The defaults (`DEV_DB.DNA`) match the original Chunky deployment. To
deploy elsewhere, override the `LIB_STAGE` env var when running
`build_bundle.py --sql`:

```bash
LIB_STAGE=@PROD_DB.PROD_SCHEMA.STG_LIB python3 procedure/build_bundle.py --sql
```

### Legacy two-bundle layout (deprecated)

The old layout used two zips: `utils_bundle.zip` (Python only) and
`poppler_bundle.zip` (poppler + pdf2image). The `build_poppler_bundle.sh`
script can still produce the legacy `poppler_bundle.zip` if you have a
specific reason to keep poppler in a separate zip (e.g. a deployment
that already imports both). New deployments should use the single-bundle
layout produced by `build_bundle.py`.

## File Structure
```
procedure/
├── ARCHITECTURE.md                  (this file)
├── README.md                        (operator quick-start)
├── build_bundle.py                  (builds single utils_bundle.zip)
├── build_poppler_bundle.sh          (DEPRECATED — legacy two-bundle layout)
├── chunky_chunks.sql                (deployable procedure)
├── chunky_qa.sql                    (deployable procedure)
├── chunky_searchservice.sql         (deployable procedure)
├── utils_bundle.zip                 (binary — single bundle: Python + poppler + pdf2image)
├── utils/                           (Python handler modules — source of truth)
├── templates/                       (.sql.j2 templates rendered by build_bundle.py)
├── script/                          (Local scripts, NOT Snowflake procedures)
│   ├── README.md
│   ├── upload_to_stage.py           (browser-auth file uploader)
│   ├── make_dummy_pdf.py            (regenerates the test PDF)
│   └── pdf/
│       └── fy2024-tbk-investor-presentation.pdf  (5-page dummy PDF)
└── snowflake-mcp/                   (existing MCP server for Claude Desktop)
```
