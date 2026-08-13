# Chunky headless Snowflake procedures

Chunky exposes three caller-executed Snowflake procedures:

| Procedure | Responsibility |
|---|---|
| `CHUNKY_INGEST` | Create and populate six-column chunk tables from staged PDFs. |
| `CHUNKY_QA` | Literal review, chunk inspection, AI drafts, commits, and revert. |
| `CHUNKY_DEPLOY` | Create, verify, suspend, and explicitly reindex Cortex Search Services. |

The procedures accept the fixed signature `(COMMAND VARCHAR, INSTRUCTION VARIANT)`.
Every instruction requires explicit `db` and `schema`. The implementation is
self-contained under [`procedure/utils`](utils/) and does not import the retired
Streamlit application. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for design
details and [`plan.md`](plan.md) for the authoritative requirements.

## Build and deploy

The Windows deployment loop is driven by [`procedure/deploy/sf.py`](deploy/sf.py)
and [`procedure/build/build_bundle.py`](build/build_bundle.py). The bundle name
contains its content hash so Snowflake's IMPORTS cache cannot silently run an
older version.

```powershell
# From the repository root
python procedure\build\build_bundle.py --clean

# Upload the exact generated bundle. The command prints its filename.
python procedure\deploy\sf.py put `
  procedure\build\out\utils_bundle_v2.0.0+<hash>.zip `
  @SBOX_DB.AI_SB.CHUNKY_UTILS
```

Render the three procedure definitions with the generated bundle name. The
following values target the validated development schema:

```powershell
python -c "from pathlib import Path; from procedure.build.render_sql import render; b='utils_bundle_v2.0.0+<hash>.zip'; out=Path('procedure/build/out'); vals={'UTILS_BUNDLE':b,'LIB_STAGE':'@SBOX_DB.AI_SB.CHUNKY_UTILS','PYTHON_RUNTIME':'3.11'}; [(Path(out/(n+'.sql')).write_text(render(Path('procedure/templates/'+n+'.sql.j2').read_text(), vals), encoding='utf-8')) for n in ['chunky_ingest','chunky_qa','chunky_deploy']]"

python procedure\deploy\sf.py script procedure\build\out\chunky_ingest.sql --keep-going
python procedure\deploy\sf.py script procedure\build\out\chunky_qa.sql --keep-going
python procedure\deploy\sf.py script procedure\build\out\chunky_deploy.sql --keep-going
```

Each deployment verifies `GET_DDL` and fails if the procedure does not import
the exact bundle just built. The three SQL templates are
[`chunky_ingest.sql.j2`](templates/chunky_ingest.sql.j2),
[`chunky_qa.sql.j2`](templates/chunky_qa.sql.j2), and
[`chunky_deploy.sql.j2`](templates/chunky_deploy.sql.j2).

## Common workflow: `QLIK_CHECKLIST`

The examples below use the existing `SBOX_DB.AI_SB.QLIK_CHECKLIST` table and a
PDF already staged at `@SBOX_DB.AI_SB.DOCS`. Change only `file` if the staged
filename is different.

### 1. Ingest

`pages` is a one-based inclusive range. Omit it to process the whole PDF.
Vision is the default extraction strategy; screenshots are stored once per
page in `PAGE_SCREENSHOT`. Successful mutations automatically refresh recorded
search services unless `auto_reindex: false` is supplied.

```sql
CALL CHUNKY_INGEST('ingest', OBJECT_CONSTRUCT(
    'db', 'SBOX_DB',
    'schema', 'AI_SB',
    'table', 'QLIK_CHECKLIST',
    'stage_path', '@SBOX_DB.AI_SB.DOCS',
    'file', 'QLIK_CHECKLIST.pdf',
    'mode', 'APPEND',
    'layout', FALSE,
    'vision', TRUE,
    'store_screenshots', TRUE,
    'auto_reindex', TRUE
));
```

For a targeted rerun:

```sql
CALL CHUNKY_INGEST('ingest', OBJECT_CONSTRUCT(
    'db', 'SBOX_DB', 'schema', 'AI_SB',
    'table', 'QLIK_CHECKLIST',
    'stage_path', '@SBOX_DB.AI_SB.DOCS',
    'file', 'QLIK_CHECKLIST.pdf',
    'mode', 'APPEND',
    'pages', ARRAY_CONSTRUCT(1, 16)
));

CALL CHUNKY_INGEST('status', OBJECT_CONSTRUCT(
    'db', 'SBOX_DB', 'schema', 'AI_SB', 'table', 'QLIK_CHECKLIST'
));
```

### 2. QA review

Use the ingest-side `extraction_report` for a file/range-wide quality report.
Use `CHUNKY_INGEST('list_chunks')` to obtain a real `CHK_<ULID>` before calling
QA inspection. `grep` is deliberately a literal local `CONTAINS()` filter; it
is not semantic ranked search.

```sql
CALL CHUNKY_INGEST('extraction_report', OBJECT_CONSTRUCT(
    'db', 'SBOX_DB', 'schema', 'AI_SB', 'table', 'QLIK_CHECKLIST',
    'file', 'QLIK_CHECKLIST.pdf',
    'pages', ARRAY_CONSTRUCT(1, 16)
));

CALL CHUNKY_INGEST('list_chunks', OBJECT_CONSTRUCT(
    'db', 'SBOX_DB', 'schema', 'AI_SB', 'table', 'QLIK_CHECKLIST',
    'file', 'QLIK_CHECKLIST.pdf', 'limit', 20
));

CALL CHUNKY_QA('grep', OBJECT_CONSTRUCT(
    'db', 'SBOX_DB', 'schema', 'AI_SB', 'table', 'QLIK_CHECKLIST',
    'contains', 'checklist', 'limit', 20
));

CALL CHUNKY_QA('inspect_chunk', OBJECT_CONSTRUCT(
    'db', 'SBOX_DB', 'schema', 'AI_SB', 'table', 'QLIK_CHECKLIST',
    'chunk_id', 'CHK_REPLACE_WITH_ID_FROM_LIST_CHUNKS',
    'stage_path', '@SBOX_DB.AI_SB.DOCS'
));
```

`stage_path` is currently needed by `inspect_chunk` to produce a presigned
screenshot URL. Without it, inspection still returns the chunk but the URL is
`NULL`; this contract is tracked as U30 in [`plan.md`](plan.md:3645).

### 3. Deploy a semantic search service

The service defaults are `CHUNK`, `PDF_NAME`, and `PAGE_NUMBER`. `TARGET_LAG`
is fixed internally to `365 days`; scheduled indexing is suspended after the
service is verified. This is intentional: later table changes are picked up
by explicit refresh, not by a background schedule.

```sql
CALL CHUNKY_DEPLOY('create', OBJECT_CONSTRUCT(
    'db', 'SBOX_DB',
    'schema', 'AI_SB',
    'service_name', 'QLIK_CHECKLIST_SEARCH',
    'tables', ARRAY_CONSTRUCT('QLIK_CHECKLIST'),
    'search_columns', ARRAY_CONSTRUCT(
        OBJECT_CONSTRUCT('column', 'CHUNK')
    ),
    'attribute_columns', ARRAY_CONSTRUCT(
        OBJECT_CONSTRUCT('column', 'PDF_NAME'),
        OBJECT_CONSTRUCT('column', 'PAGE_NUMBER')
    ),
    'verify_query', 'checklist',
    'suspend_indexing', TRUE
));
```

The create response should contain verification hits and report
`indexing_state: "SUSPENDED"` while `serving_state` remains active.

```sql
SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(
    'SBOX_DB.AI_SB.QLIK_CHECKLIST_SEARCH',
    '{"query":"checklist","limit":5}'
) AS RESULT;
```

### 4. Explicit reindex after later edits

Use canonical `tables[]`, even when refreshing one table. This finds all
services recorded in the table comment, refreshes them, and suspends indexing
again afterward.

```sql
CALL CHUNKY_DEPLOY('reindex', OBJECT_CONSTRUCT(
    'db', 'SBOX_DB',
    'schema', 'AI_SB',
    'tables', ARRAY_CONSTRUCT('QLIK_CHECKLIST'),
    'wait', TRUE
));
```

## Command summary

### `CHUNKY_INGEST`

`help`, `ingest`, `batch_ingest`, `estimate_cost`, `list_chunks`,
`list_chunks_csv`, `update_chunk`, `delete_chunks`, `extraction_report`,
`revert`, and `status`.

`delete_chunks` accepts documented selectors `chunk_ids`, `file`, and `pages`.
It counts matching rows before deletion and reports the actual count.

### `CHUNKY_QA`

`help`, `grep`, `inspect_chunk`, `generate_draft`, `commit`, and `revert`.
There is no QA delete command; deletion is owned by
`CHUNKY_INGEST('delete_chunks')`.

### `CHUNKY_DEPLOY`

`help`, `autobuild`, `create`, `list`, `describe`, `alter`, `drop`, `revert`,
and `reindex`.

Use `search_columns` and `attribute_columns` as arrays of strings or objects:

```text
"CHUNK"
{"column":"CHUNK"}
{"column":"LINKS", "metadata_field":"links"}
{"column":"CHUNK", "search_type":"text", "tables":["QLIK_CHECKLIST"]}
```

## Help and diagnostics

Every procedure generates help from its command registry:

```sql
CALL CHUNKY_INGEST('help');
CALL CHUNKY_QA('help');
CALL CHUNKY_DEPLOY('help', OBJECT_CONSTRUCT('command', 'create'));
```

Every response includes a common envelope with `success`, `command`, `data`,
`error`, `remedy`, `warnings`, `run_id`, query IDs, and timestamps where
applicable. Search/index bookkeeping failures are warnings after rows have
already been written; a stale service can be refreshed with the `reindex` call
above.

## Local validation

```powershell
python -m compileall -q procedure\utils
pytest -q procedure\tests
```

The tests are offline and mock Snowpark. Live acceptance also requires a real
ingest, QA inspection, service creation, `SEARCH_PREVIEW`, and a table-edit →
`tables[]` reindex round trip.

## Known open investigations

- **U29:** vision extraction can preserve literal `\\n` sequences instead of
  real newline bytes in `CHUNK`; this is documented in [`plan.md`](plan.md:3644)
  and intentionally not changed until the raw `AI_COMPLETE` response is traced.
- **U30:** `inspect_chunk` needs a clarified screenshot-stage contract; see the
  note in the QA section above.
