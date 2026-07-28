# Chunky Procedures

Headless Snowflake stored procedures that expose Chunky's ingestion,
QA, and Cortex Search Service management as MCP-callable tools.

This directory is **fully self-contained** — none of the procedures
import from the top-level `utils/` package or reference the Streamlit
app. The Python handlers live in `procedure/utils/` and are bundled
into a single `procedure/utils_bundle.zip` for Snowflake IMPORTS.

For the full architecture, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Quick start

### 1. Build the single bundle

```bash
# Build utils_bundle.zip (Python handlers + ARM64 poppler binaries + pdf2image)
# ARM64 is the default — works on Snowflake ARM warehouses (AWS Graviton,
# Ampere Altra). Cross-builds from x86_64 hosts via Debian .deb downloads.
python3 procedure/build_bundle.py --clean

# Optionally render the .sql files from .j2 templates
python3 procedure/build_bundle.py --sql

# To build for x86_64 warehouses instead (uses the host's poppler-utils):
python3 procedure/build_bundle.py --arch x86_64
```

The build script:
- Zips `procedure/utils/*.py` under `chunky_utils/` (the import name Snowflake sees)
- Zips `pdftoppm`, `pdfinfo`, `pdftotext` and their shared-library deps under `poppler_bundle/poppler/bin/` and `poppler_bundle/poppler/lib/`
  - **ARM64 (default)**: downloads pre-built ARM64 .deb packages from the Debian mirror and extracts them. Works on any host (x86_64 or ARM64) — no root, no Docker, no qemu. See `build_arm_poppler.py` for details.
  - **x86_64**: uses the host's own `poppler-utils` install (requires `apt-get install poppler-utils`)
- pip-installs `pdf2image` into a temp dir and zips it under `pdf2image/`

All three live in **one zip** (`utils_bundle.zip`) so Snowflake extracts them side-by-side at `/home/udf/<id>/`.

> **Architecture note**: Snowflake warehouses are increasingly ARM64
> (AWS Graviton, Ampere Altra). The bundle defaults to ARM64 so it works
> out-of-the-box on those warehouses. If your warehouse is x86_64, pass
> `--arch x86_64`. The bundled poppler binaries MUST match the warehouse
> architecture or `pdf2image.convert_from_bytes` will fail with
> `OSError: [Errno 8] Exec format error`.

### 2. Upload the bundle to your Snowflake stage

```sql
-- In a Snowsight worksheet or via snowsql:
PUT file://procedure/utils_bundle.zip @DEV_DB.DNA.STG_LIB AUTO_COMPRESS=FALSE;
```

### 3. Deploy the procedures

Run `chunky_chunks.sql`, `chunky_qa.sql`, and `chunky_searchservice.sql`
individually in Snowsight or with `snowsql`. These three files are the
complete deployable procedure bundle.

---

## Deploying to a different database / schema

The defaults (`DEV_DB.DNA`) match the original Chunky deployment. To
target a different database/schema:

1. Set the `LIB_STAGE` env var before running `build_bundle.py --sql`:
   ```bash
   LIB_STAGE=@PROD_DB.PROD_SCHEMA.STG_LIB python3 procedure/build_bundle.py --sql
   ```
2. Or hand-edit the `IMPORTS` stage in the three deployable SQL files.

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
    'range', [1, 5],                      -- optional; omit for full doc
    'layout', false,                      -- default false (Vision-only)
    'vision', true,                       -- default true
    'chunk_size', 8000,
    'overlap', 20,
    'grant_roles', ['ANALYST']
));
```

**Default strategy: Vision-only.** Set `layout: true` for Layout-only.
Set both `layout: true, vision: true` for Layout+Vision (hybrid repair —
Layout runs first, then Vision repairs any chunks flagged by the
quality inspector).

The response includes:

```json
{
  "success": true,
  "command": "ingest",
  "data": {
    "table": "MY_CHUNKS",
    "file": "...",
    "mode": "OVERWRITE",
    "metrics": { ... },
    "grant_result": { ... },
    "table_newly_created": true,
    "duplicate_pages": []
  },
  "warning": "A new chunk table was created for this ingest ... | OVERWRITE mode destroyed ...",
  "warnings": [
    "A new chunk table was created for this ingest.",
    "OVERWRITE mode destroyed all prior rows ..."
  ],
  "revert": {
    "command": "CALL chunky_chunks('REVERT', OBJECT_CONSTRUCT('db', 'DEV_DB', ...));",
    "timestamp_before": "2024-01-01 12:00:00.000",
    "query_ids": ["abc-123", "def-456", ...]
  },
  "query_ids": ["abc-123", "def-456", ...],
  "timestamp_before": "2024-01-01 12:00:00.000"
}
```

**Warnings are post-execution** (headless callers can't display modals
before running). The `warnings` array lets the caller surface each one
individually; `warning` is the same content joined with `" | "`.

### All chunky_chunks commands

| Command | Purpose |
|---------|---------|
| `ingest` | Full PDF ingestion (init → surgical delete → layout/vision → hybrid repair → grant) |
| `list_chunks` | List/read chunks with filters |
| `list_chunks_csv` | Same as `list_chunks` but returns a single CSV string in `data.csv` |
| `update_chunk` | Edit chunk content by chunk_id |
| `delete_chunks` | Delete by file/range/chunk_ids |
| `inspect_quality` | Run QualityInspector on chunks — returns defect status without modifying |
| `batch_ingest` | Run multiple ingest jobs in one CALL (`instruction.jobs = [...]`) |
| `estimate_cost` | Pre-flight cost estimate (credits + USD) without ingesting |
| `revert` | Rewind the table via TIME TRAVEL |

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

-- Option C: row-scoped revert (only undo changes to a specific file/range)
CALL chunky_chunks('REVERT', OBJECT_CONSTRUCT(
    'db', 'DEV_DB', 'schema', 'DNA', 'table', 'MY_CHUNKS',
    'timestamp_before', '2024-01-01 12:00:00.000',
    'file', 'doc.pdf', 'range', [2, 5]
));
```

Revert uses the **safe rename pattern** (not `CREATE OR REPLACE TABLE X
CLONE X AT(...)`, which fails because source/target are the same object):

1. `ALTER TABLE <t> RENAME TO <t>_revert_backup_<epoch>` (preserves Time Travel)
2. `CREATE TABLE <t> CLONE <t>_revert_backup_<epoch> AT(TIMESTAMP => '<ts>')`
3. The backup table is left in place — drop it once you've verified the revert.

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
2. Edit the `.sql.j2` templates in `procedure/templates/` if a procedure
   signature or `IMPORTS` stage changes.
3. Rebuild the bundle and render SQL:
   ```bash
   python3 procedure/build_bundle.py --clean --sql
   ```
4. Re-upload `utils_bundle.zip` to your Snowflake stage.
5. Deploy the three SQL files individually.

For local testing without Snowflake:

```bash
python3 -m pytest tests/test_procedure_utils.py -v
```

These tests mock the Snowpark session so they run in CI / local dev
environments without Snowflake credentials. They cover:

- Pure-function helpers (build_chunk_ref, _shared, page_mapping, metadata_handler, layout_parse)
- Constants sanity checks (incl. new vision-default + single-bundle constants)
- QueryLog behavior
- Each sub-procedure handler (init_table, grant_table, surgical_delete, parse_pdf)
- Revert helpers (success path, retention-window violation, **safe rename pattern verification**)
- Main handler dispatch (unknown command, revert routing, new commands list_chunks_csv / inspect_quality / estimate_cost)
- Build script (single-bundle contents, poppler binaries + pdf2image included, no separate poppler_bundle.zip)
- Local upload script (arg parsing, config loading)
- Dummy PDF (exists, valid, has expected content)
