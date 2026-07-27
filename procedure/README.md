# Chunky Procedures

Headless Snowflake stored procedures that expose Chunky's ingestion,
QA, and Cortex Search Service management as MCP-callable tools.

This directory is **fully self-contained** — none of the procedures
import from the top-level `utils/` package or reference the Streamlit
app. The Python handlers live in `procedure/utils/` and are bundled
into `procedure/utils_bundle.zip` for Snowflake IMPORTS.

For the full architecture, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Quick start

### 1. Upload the bundles to your Snowflake stage

```sql
-- In a Snowsight worksheet or via snowsql:
PUT file://procedure/utils_bundle.zip @DEV_DB.DNA.STG_LIB AUTO_COMPRESS=FALSE;
PUT file://procedure/poppler_bundle.zip @DEV_DB.DNA.STG_LIB AUTO_COMPRESS=FALSE;
```

> If `poppler_bundle.zip` is missing or stale, rebuild it on a Linux
> x86_64 host with `bash procedure/build_poppler_bundle.sh`.

### 2. Deploy the procedures

Run `chunky_chunks.sql`, `chunky_qa.sql`, and `chunky_searchservice.sql`
individually in Snowsight or with `snowsql`. These three files are the
complete deployable procedure bundle.

---

## Deploying to a different database / schema

The defaults (`DEV_DB.DNA`) match the original Chunky deployment. To
target a different database/schema:

```bash
```

Update the `IMPORTS` stage in the three deployable SQL files when using a
different environment.

---

## Calling the procedures

All three main procedures take `(command VARCHAR, instruction VARIANT)`
and return a VARIANT (JSON object).

### Ingest a PDF

```sql
CALL chunky_chunks('ingest', OBJECT_CONSTRUCT(
    'db', 'DEV_DB',
    'schema', 'DNA',
    'table', 'MY_CHUNKS',
    'stage_path', '@DEV_DB.DNA.DOCS',
    'file', 'fy2024-tbk-investor-presentation.pdf',
    'mode', 'OVERWRITE',                  -- OVERWRITE | APPEND | SURGICAL
    'scope', 'Full Doc',                  -- Full Doc | Page Range
    'layout', true,
    'vision', false,
    'chunk_size', 8000,
    'overlap', 20,
    'grant_roles', ['ANALYST']
));
```

The response includes:

```json
{
  "success": true,
  "command": "ingest",
  "data": { "table": "MY_CHUNKS", "metrics": { ... } },
  "warning": "OVERWRITE mode destroyed all prior rows ...",
  "revert": {
    "command": "CALL chunky_chunks('REVERT', OBJECT_CONSTRUCT(...));",
    "timestamp_before": "2024-01-01 12:00:00.000",
    "query_ids": ["abc-123", "def-456", ...]
  },
  "query_ids": ["abc-123", "def-456", ...],
  "timestamp_before": "2024-01-01 12:00:00.000"
}
```

### Revert a botched ingest

```sql
-- Option A: paste the revert.command string from the original response
CALL chunky_chunks('REVERT', OBJECT_CONSTRUCT(
    'db', 'DEV_DB', 'schema', 'DNA', 'table', 'MY_CHUNKS',
    'timestamp_before', '2024-01-01 12:00:00.000'
));

-- Option B: revert by query IDs
CALL chunky_chunks('REVERT', OBJECT_CONSTRUCT(
    'db', 'DEV_DB', 'schema', 'DNA', 'table', 'MY_CHUNKS',
    'query_ids', ['abc-123', 'def-456']
));

-- Option C: row-scoped revert (only undo changes to a specific file/page range)
CALL chunky_chunks('REVERT', OBJECT_CONSTRUCT(
    'db', 'DEV_DB', 'schema', 'DNA', 'table', 'MY_CHUNKS',
    'timestamp_before', '2024-01-01 12:00:00.000',
    'file', 'doc.pdf', 'page_range', [2, 5]
));
```

### QA Studio (search, inspect, generate_draft, commit, delete)

```sql
CALL chunky_qa('search', OBJECT_CONSTRUCT(
    'db', 'DEV_DB', 'schema', 'DNA', 'table', 'MY_CHUNKS',
    'stage_path', '@DEV_DB.DNA.DOCS',
    'search_text', 'revenue',
    'limit', 10
));
```

### Cortex Search Service

```sql
CALL chunky_searchservice('create', OBJECT_CONSTRUCT(
    'db', 'DEV_DB', 'schema', 'DNA',
    'service_name', 'CSS_MY_CHUNKS',
    'tables', ['MY_CHUNKS'],
    'search_columns', [
        OBJECT_CONSTRUCT('table', 'MY_CHUNKS', 'column', 'CHUNK',
                         'search_type', 'Hybrid',
                         'embedding_model', 'voyage-multilingual-2')
    ],
    'attribute_columns', [
        OBJECT_CONSTRUCT('table', 'MY_CHUNKS', 'column', 'RELATIVE_PATH')
    ],
    'target_lag', 30,
    'target_lag_unit', 'days',
    'grant_roles', ['ANALYST']
));
```

---

## Local helper scripts

The `procedure/script/` directory contains local Python scripts that
are NOT Snowflake procedures — they run on your laptop.

| Script | Purpose |
|--------|---------|
| `upload_to_stage.py` | Upload a file (or directory) to a Snowflake stage using browser-based SSO auth |
| `make_dummy_pdf.py`  | Regenerate the 5-page test PDF at `pdf/fy2024-tbk-investor-presentation.pdf` |

See [`script/README.md`](script/README.md) for details.

---

## Development workflow

1. Edit Python handlers in `procedure/utils/`.
2. Edit the three deployable SQL files if a procedure signature or
   `IMPORTS` stage changes.
3. Re-upload `utils_bundle.zip` to your Snowflake stage.
4. Deploy the three SQL files individually.

For local testing without Snowflake:

```bash
python3 -m pytest tests/test_procedure_utils.py -v
```

These tests mock the Snowpark session so they run in CI / local dev
environments without Snowflake credentials.
