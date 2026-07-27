# procedure/utils — Python handler modules

This package is the **single source of truth** for every Chunky
procedure's runtime logic. The `.sql` files in `procedure/` are thin
wrappers that IMPORT `utils_bundle.zip` (built from this directory)
and call `run` from the appropriate handler module.

## Why a separate package?

Before this refactor, the procedure SQL files inlined the entire
Python handler code between `$$` markers. That worked but had three
problems:

1. **No IDE support inside SQL strings** — no autocomplete, no
   jump-to-definition, no type hints.
2. **Duplicated logic** — `clean_text_for_sql`, `build_chunk_ref`,
   `save_optimized_image` etc. were copy-pasted across multiple
   procedures.
3. **Tight coupling to the Streamlit-side `utils/`** — the procedures
   imported from the top-level `utils/` package which also contains
   Streamlit-specific code, making it impossible to deploy the
   procedures without the Streamlit app.

This package solves all three by:
- Keeping each handler in a normal `.py` file (full IDE support).
- Sharing helpers via intra-package imports
  (`from .query_log import QueryLog`).
- Being **self-contained** — no imports from outside `procedure/utils/`.

## Module map

### Sub-procedure handlers
| Module | Snowflake procedure | Purpose |
|--------|---------------------|---------|
| `init_table.py`       | Ingestion helper | CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE |
| `grant_table.py`      | Ingestion helper | GRANT ALL PRIVILEGES with retry + role-name validation |
| `surgical_delete.py`  | Ingestion helper | Bottom-up DELETE with BEGIN/COMMIT/ROLLBACK |
| `parse_pdf.py`        | Ingestion helper | AI_PARSE_DOCUMENT wrapper |
| `build_chunk_ref.py`  | Ingestion helper | Canonical CHUNK_REF string builder |

### Main-procedure handlers
| Module | Snowflake procedure | Commands |
|--------|---------------------|----------|
| `chunky_chunks_handler.py`         | `chunky_chunks`         | ingest, list_chunks, update_chunk, delete_chunks, revert |
| `chunky_qa_handler.py`             | `chunky_qa`             | search, inspect, generate_draft, commit, delete, revert |
| `chunky_searchservice_handler.py`  | `chunky_searchservice`  | create, list, describe, alter, drop, revert |

### Shared utilities
| Module | Purpose |
|--------|---------|
| `constants.py`        | Single source of truth for DB/schema/model/warnings — eliminates hardcoded literals |
| `query_log.py`        | `QueryLog` — collects query IDs + pre/post timestamps for revert support |
| `page_mapping.py`     | `RangeMapping` / `RangeMappingEngine` — surgical shift math |
| `metadata_handler.py` | Per-chunk metadata stamping |
| `revert.py`           | TIME TRAVEL-based revert helpers (used by main handlers' `revert` command) |

## Conventions

### Every handler exposes `run(...)`

Snowflake Python procedures need a single entry point. We standardise
on `run` so the SQL templates can all use `HANDLER = 'run'`.

For sub-procedures, `run`'s signature matches the procedure's SQL
parameters (with `session` as the first arg, supplied implicitly by
Snowflake):

```python
# init_table.py
def run(session, db: str, schema: str, table_name: str, mode: str) -> Dict:
    ...
```

For main procedures, `run` takes `(session, command, instruction)`:

```python
# chunky_chunks_handler.py
def run(session, command, instruction):
    ...
```

### Every handler returns a JSON-serialisable dict

Snowflake `VARIANT` return type maps to JSON. Every handler returns a
dict with at least:

- `success: bool`
- `command: str` (echoed back)
- `data: object | None`
- `error: str | None`

Plus, for any handler that touches the database:

- `query_ids: list[str]` — Snowflake query IDs captured by `QueryLog`
- `timestamp_before: str | None` — pre-operation timestamp for revert
- `timestamp_after: str | None` — post-operation timestamp
- `query_count: int`

And, for destructive operations:

- `warning: str` — post-execution warning
- `revert: object | None` — revert instructions (command string, timestamp, query_ids)

### Use `QueryLog` for every SQL statement

```python
from .query_log import QueryLog

def run(session, ...):
    log = QueryLog(session)
    log.execute("CREATE TABLE ...")
    log.execute("INSERT INTO ...", params=[...])
    return {"success": True, ..., **log.to_dict()}
```

`QueryLog.execute` runs the SQL via `session.sql(...).collect()`,
captures the resulting query ID via `LAST_QUERY_ID()`, and appends it
to `log.ids`. The pre-operation timestamp is captured in `__init__`
so callers can REVERT to it later.

### Never import from outside `procedure/utils/`

The package must be fully self-contained so the `utils_bundle.zip`
built from it is sufficient for Snowflake IMPORTS. If you need a
helper that lives in the top-level `utils/`, copy it here instead of
importing from there.

## Testing

```bash
python3 -m pytest tests/test_procedure_utils.py -v
```

The tests mock the Snowpark session (`MagicMock`) so they run without
Snowflake credentials. They cover:
- Pure-function helpers (build_chunk_ref, page_mapping, metadata_handler)
- Constants sanity checks
- QueryLog behavior
- Each sub-procedure handler (init_table, grant_table, surgical_delete, parse_pdf)
- Revert helpers (success path, retention-window violation, missing inputs)
- Main handler dispatch (unknown command, revert routing)
- Build script (utils_bundle.zip contents, generated SQL correctness)
- Local upload script (arg parsing, config loading)
- Dummy PDF (exists, valid, has expected content)
