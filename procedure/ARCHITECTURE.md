 # Chunky headless architecture

This directory contains the deployable, Streamlit-free implementation of
Chunky. The runtime is three Snowflake Python stored procedures backed by one
versioned utility bundle:

| Procedure | Responsibility |
|---|---|
| `CHUNKY_INGEST` | Create or replace a Chunky table in the six-column shape, extract PDF pages, persist chunks and screenshots, record source metadata, and refresh dependent Search Services. |
| `CHUNKY_QA` | Literal local text filtering, chunk inspection, draft generation, reviewed edits, deletion, and Time Travel revert. |
| `CHUNKY_DEPLOY` | Build, create, describe, list, alter, drop, revert, and reindex Cortex Search Services. |

Every procedure accepts a command and a VARIANT instruction and returns a JSON
response envelope. Command schemas are declared in the handler registry, so
help output and validation are generated from the same metadata used for
dispatch.

## Runtime packaging and deployment

[`procedure/build/build_bundle.py`](build/build_bundle.py) creates a
deterministic, versioned `utils_bundle_*.zip`. The bundle contains the
procedure utilities, `pdf2image`, and Poppler binaries for both `arm64` and
`x86_64`; [`procedure/utils/poppler_bootstrap.py`](utils/poppler_bootstrap.py)
selects the matching runtime architecture. The generated SQL templates import
that exact bundle. The deploy CLI refuses stale SQL imports and verifies each
procedure with `GET_DDL` after deployment.

The bundle is uploaded to the library stage before executing the generated
SQL. PDF fixtures live on a separate document stage and are never packaged in
the utility bundle.

## Storage contract

Chunky-managed tables have exactly six columns:

```text
CHUNK_ID          VARCHAR NOT NULL
PDF_NAME          VARCHAR NOT NULL
PAGE_NUMBER       NUMBER NOT NULL
CHUNK             VARCHAR
CHUNK_METADATA    VARIANT
PAGE_SCREENSHOT   BINARY
```

`CHUNK_METADATA` is the compatibility boundary for all derived fields. Legacy
columns such as `LINK_BLOCK`, `CHUNK_TYPE`, `CHUNK_REF`, and `RELATIVE_PATH`
must not be added to or selected from the target table. Current metadata may
include:

```json
{
  "chunk_type": "standard",
  "chunk_ref": "Doc Source: report.pdf | Page Num: 4",
  "links": [
    {"target": "https://example.com", "type": "external"},
    {"target": "page 4", "type": "internal"}
  ],
  "parser": {"layout": true, "vision": false}
}
```

The human-readable link block is appended to `CHUNK` for searchability, while
the `links` array is the single structured metadata field and survives QA rewriting. Plain-text URLs
are not treated as annotations. External URI annotations and internal PDF
destination annotations are extracted separately.

## Ingestion pipeline

`CHUNKY_INGEST('ingest', ...)` follows this sequence:

1. Generate a non-empty run ID and acquire the table's `ingest` advisory lease.
2. Ensure the six-column table exists; `OVERWRITE` preserves the comment block
   across `CREATE OR REPLACE` and re-verifies the lease afterward.
3. Read the staged PDF and determine its page count.
4. Run Layout (`AI_PARSE_DOCUMENT`) and/or Vision (`AI_COMPLETE`) according to
   the instruction. Layout uses a temporary staging table whose extra columns
   are internal only; it projects all derived values into `CHUNK_METADATA`.
5. Split page text with Cortex recursive splitting and insert screenshots only
   on the first split row for each page.
6. On any staging or batch insert error, roll back, identify the failing page
   range, stop immediately, and return `success: false`. A failed batch must
   never be converted into a successful zero-row ingest.
7. For hybrid mode, inspect Layout output and repair defective pages with
   Vision while preserving link metadata.
8. Record source metadata in the table comment, refresh dependent services,
   apply grants, and release the lease in `finally`.

The layout path and Vision path share the same six-column target contract.
Screenshots are rendered once during ingest and stored as binary data; QA only
creates a presigned rendering when a caller requests a screenshot URL.

## Advisory coordination

There is no run-history or control table. The target table `COMMENT` contains a
versioned JSON block with source records, Search Service records, and three
best-effort advisory lease slots: `ingest`, `qa`, and `deploy`.

Lease mutations use explicit scoped `BEGIN`/`COMMIT` transactions so an
independent Snowflake session can observe them. Readers do not acquire leases.
Each lease has a token, holder, run ID, progress, heartbeat/expiry timestamps,
and configurable TTL. Expired leases can be overridden with `force`; release
is token-aware and preserves unrelated comment fields. External validation
must disable persisted result caching (`USE_CACHED_RESULT = FALSE`).

## QA semantics

QA `grep` is intentionally a literal `CONTAINS()` filter over stored local
chunk text. It is not semantic ranking. Semantic ranked search is performed by
the deployed Cortex Search Service through `SEARCH_PREVIEW`; help output makes
this distinction explicit.

`inspect_chunk` reads only the six physical columns and derives type, reference, and
links from `CHUNK_METADATA`. `generate_draft` keeps the original link block
outside the AI rewrite and reattaches it if the model omits it. Commit warnings
are emitted only when at least one supplied draft was actually committed.

## Cortex Search Service lifecycle

`CHUNKY_DEPLOY` centralizes Search Service DDL generation. Defaults are:

- search column: `CHUNK`;
- attributes: `PDF_NAME`, `PAGE_NUMBER`;
- primary key: `CHUNK_ID`;
- embedding model: `voyage-multilingual-2`.

Multiple source tables use explicit `UNION ALL` or an explicit equality `JOIN`.
Join predicates qualify both table sides, and join mode is validated against
Chunky-managed six-column sources. The common DDL tail always emits warehouse,
target lag, comment, and source query. RIGHT/FULL joins produce refresh-mode
caveats. Creation reads back refresh/indexing/serving state, and readiness
accepts an active serving service even while a warm replacement reports a
transient indexing state. Listing uses `SHOW CORTEX SEARCH SERVICES`.

`autobuild` discovers Chunky tables from their comment markers, `reindex`
performs the dependent service refresh lifecycle, and service records are
stored back into the source table comments.

## Error handling and verification

Handlers use structured success/error envelopes with remedy text, warnings,
query IDs, timestamps, and revert information where applicable. Registry
validation rejects unknown fields, reports missing required fields before the
handler runs, and provides did-you-mean suggestions. Status validates database,
schema, and table existence instead of treating a nonexistent object as an
empty Chunky table.

Offline tests cover SQL shape, six-column schema assumptions, link annotation
extraction, registry requirements, lease transactions, and warm Search Service
readiness. Live deployment verifies imported bundle identity through `GET_DDL`;
live ingest is required for Snowflake SQL behavior that mocks cannot prove.
