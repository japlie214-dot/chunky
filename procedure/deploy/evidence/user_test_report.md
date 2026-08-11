# Chunky — First-Time User Test Report

Tester persona: a user with no prior knowledge of the codebase, working only from
`CALL CHUNKY_INGEST/CHUNKY_QA/CHUNKY_DEPLOY('help', ...)` output. All work done in
`SBOX_DB.AI_SB`, objects prefixed `USERTEST_`. Fixture: 24-page PDF
`fy2024-tbk-investor-presentation.pdf` staged at `@SBOX_DB.AI_SB.DOCS`.

## Overall verdict

**Not usable end-to-end by a newcomer relying only on `help`.** The three
procedures' top-level command lists and per-command field lists are genuinely
good — better than most internal tools — and the happy-path core loop (ingest
→ list/search chunks → deploy → search) does work and produces surprisingly
good extraction quality. But I hit **five separate unhandled Python
tracebacks** leaking through `help`-documented commands, a **DEPLOY create
path that is broken for the single-search-column case** (the most obvious
first thing anyone would try), a **help schema that repeatedly claims fields
are optional when they are actually required** (crashing instead of
validating), and a **systemic content-encoding bug** where every stored chunk
is double-JSON-encoded (confirmed by the product's own `inspect_quality`
command flagging 24/25 chunks as defective). A user without the ability to
read the Python source (which I fell back to twice) would be stuck at the
first DEPLOY attempt and would not know that `commit`'s `commits[].chunk`
field is actually named `draft_text`, or that `search_columns[]` needs a
`table` key or it silently nulls out the column.

## Chronological log

### 1. Connectivity
`auth whoami` returned a live session immediately, no browser prompt. Good.

### 2. Discovery via `help`
`CALL CHUNKY_INGEST('help', {})`, `CHUNKY_QA('help', {})`, `CHUNKY_DEPLOY('help', {})`
each returned a clean list of commands with required-field summaries, plus a
per-command `help({"command": "..."})` giving the full field table (type +
default + required). This is a strong design — much better than "read the
source." Reaction: **delighted**.

CLI friction (not the product's fault, but worth flagging): passing JSON with
embedded spaces via PowerShell to `sf.py call ... <json>` reliably breaks
(`sf: error: unrecognized arguments: mills"}`) because PowerShell re-tokenizes
the string. Had to switch to writing instructions to a file and using
`@file.json`, which the CLI does support and which then worked reliably.

### 3. Ingest — Vision mode
```
CHUNKY_INGEST ingest {db, schema, table:"USERTEST_CHUNKS",
  stage_path:"@SBOX_DB.AI_SB.DOCS", file:"fy2024-tbk-investor-presentation.pdf"}
```
Note: I guessed the plain filename (no `chunky_fixtures/` subfolder) as a
first-time user would, and it resolved — the file the fixture pointed to via
`stage_path` apparently exists directly under `DOCS`, not under the
subfolder mentioned in my task brief. `estimate_cost` (see below) independently
confirmed `total_pages_in_pdf: 24` for this file, so it's the right file.

Took **404 seconds (~6.7 min)** for 24 pages of vision extraction — slow but
expected for vision. `status` while in flight showed live `progress` (current
page, pages_done/total, phase) — genuinely nice, this is the kind of thing a
long-running job should expose and often doesn't. Reaction: **delighted**.

Result: `success: true`, 24 pages, `vision_pages: 24`, and a ready-to-paste
`revert.command` (full SQL with all query_ids baked in) to undo the ingest.
Reaction: **delighted** — this is exactly what a nervous first-time user wants
after a destructive-feeling operation.

### 4. Cost estimation (before running)
```
CHUNKY_INGEST estimate_cost {..., stage_path, file}
```
Returned pages, vision token estimates, USD estimate (`vision_usd: 0.144`),
and a caveat that estimates are heuristic. This answers requirement #8
directly and is exactly the kind of "know before you run it" feature that's
often missing. Reaction: **delighted**. (Minor: it's not obvious from the
command list alone that `estimate_cost` should be run *before* `ingest` —
nothing in `ingest`'s help points forward to it.)

### 5. Concurrency / locking
While the first ingest was still running, I fired a second `ingest` at the
same table:
```
"error": "Table SBOX_DB.AI_SB.USERTEST_CHUNKS is being ingested by MCDAAI_DNA",
"remedy": "Wait and poll CHUNKY_INGEST('status', ...) or pass 'force': true to override."
```
Excellent — precise, actionable, tells you exactly what to do next. This is
the best error message in the whole test. Reaction: **delighted**.

### 6. list_chunks / QA search
`CHUNKY_INGEST list_chunks` and `CHUNKY_QA search {search_text:"feed mills"}`
both worked well and returned genuinely good markdown chunk text with
`[VISUAL: ...]` descriptions for images/logos — extraction quality is
impressive on its face.

### 7. QA `inspect` — BUG (unhandled SQL error)
```
CHUNKY_QA inspect {db, schema, table, chunk_id}
"error": "(1304) ... SQL compilation error: ... invalid identifier 'CHUNK_TYPE'"
```
Reproduced twice, consistently. I checked the actual table via
`DESCRIBE TABLE` (falling back past the docs here) and confirmed the table
`CHUNKY_INGEST` creates has columns `CHUNK_ID, PDF_NAME, PAGE_NUMBER, CHUNK,
CHUNK_METADATA, PAGE_SCREENSHOT` — there is no `CHUNK_TYPE`, `LINK_BLOCK`, or
`CHUNK_REF` column, even though `list_chunks` happily returns those fields
(empty strings). `CHUNKY_QA inspect`'s underlying query references
`CHUNK_TYPE`, which doesn't exist on tables produced by the currently-deployed
`CHUNKY_INGEST`. **This makes `inspect` — a documented, help-listed command —
completely unusable out of the box.** Reaction: **blocked, then annoyed.**

### 8. QA `generate_draft` — BUG (raw traceback, wrong required-field info)
Called with only `db/schema/table/chunk_ids` (as help's field table implied
`stage_path` was optional — no `"required": true` on it):
```
KeyError: 'stage_path'
  File ".../chunky_qa_handler.py", line 445, in cmd_generate_draft
    stage_path = inst["stage_path"]
```
Raw Python traceback surfaced to the caller, not a validation error. Retried
with `stage_path` added — worked, returned a draft plus a real signed S3
screenshot URL. Reaction: **annoyed** (avoidable crash) then **relieved** it
worked once I guessed the missing field.

### 9. QA `commit` — confusing silent no-op + misleading warning
First attempt used a field named `chunk` inside the `commits[]` array
(guessed, since `help` only says `"commits": {"type": "array"}` with no
schema for the array items):
```
"data": {"results": [{"chunk_id": "...", "status": "skipped"}]},
"success": true,
"warning": "Chunk content was overwritten. The previous content can be retrieved via TIME TRAVEL..."
```
Nothing was actually written (confirmed: re-searching for the edited text
found 0 rows). **The top-level warning claims content was overwritten even
when the per-item status says `"skipped"` and 0 query_ids were touched** —
this is actively misleading, worse than no message at all. The correct field
turned out to be `draft_text` (learned by pattern-matching against
`generate_draft`'s output shape, not from any doc). With `draft_text`, it
worked, `status: "committed"`, and a precise revert command was returned.
Reaction: **confused, then annoyed at the misleading warning.**

### 10. QA `revert` — worked well
Reverting the commit via the returned `revert.command`
(`timestamp_before` + `query_ids`) worked, restored the original text, and
additionally created a safety backup table (`..._revert_backup_<epoch>`) with
a note that it's recoverable via `UNDROP TABLE`. Good belt-and-suspenders
design. Reaction: **delighted** — though it does leave an extra table behind
that a user must remember to clean up themselves (not mentioned anywhere in
the response that this table exists or needs cleanup).

### 11. Error-message quality checks (typo / missing field)
```
CHUNKY_QA search {db, schema, tabel:"..."}   # typo
"error": "Unknown field 'tabel'. Did you mean 'table'?; Missing required instruction field: table.",
"remedy": "CALL CHUNKY_QA('help', OBJECT_CONSTRUCT('command','search')) for the full field list."
```
This is genuinely excellent — "did you mean" typo detection plus a pointer
back to help. Same clean message for a fully missing required field.
Reaction: **delighted**. This is the standard the two tracebacks above should
have met and didn't.

### 12. QA `delete` — BUG (false-positive success)
```
CHUNKY_QA delete {chunk_ids:["CHK_DOES_NOT_EXIST_12345"]}
"data": {"deleted": 1}, "success": true,
"warning": "Chunks were permanently deleted..."
```
The chunk_id never existed. I verified via `SELECT COUNT(*)` that the table's
row count was unchanged (still 24) before and after. **`deleted` counted the
number of chunk_ids *requested*, not rows actually affected**, so a delete of
nonexistent IDs is falsely reported as a successful permanent deletion.
Reaction: **concerned** — this is the kind of bug that could make someone
believe cleanup happened, or mask a typo'd chunk_id silently.

### 13. DEPLOY `create` — BUG, twice, then a genuine architecture bug
First attempt, following `help` exactly (`tables`, `search_columns:["CHUNK"]`
as plain strings, no `warehouse`):
```
AttributeError: Row object has no attribute get
  chunky_deploy_handler.py:249, in _cmd_create_unlocked
    warehouse = inst.get("warehouse") or (warehouse_rows[0].get("W") if warehouse_rows else None)
```
Raw traceback — `help` never mentions that omitting `warehouse` when no
session warehouse fallback works crashes instead of erroring cleanly.

Added `"warehouse":"SHARED_ID_XS"` and kept `search_columns:["CHUNK"]` (plain
strings, as `help`'s `{"type":"array"}` gives no item schema):
```
AttributeError: 'str' object has no attribute 'get'
  chunky_deploy_handler.py:267, in _cmd_create_unlocked
    col = sc.get("column")
```
So `search_columns` items must be objects, not strings — undocumented.
Switched to `[{"column":"CHUNK"}]`:
```
"ddl": "... ON \"CHUNK\" TARGET_LAG = '365 days' EMBEDDING_MODEL = 'voyage-multilingual-2' AS (\n  SELECT NULL AS \"CHUNK\" FROM \"SBOX_DB\".\"AI_SB\".\"USERTEST_CHUNKS\"\n);",
"error": "... Missing option(s): [WAREHOUSE]"
```
Two things wrong here simultaneously: (a) the generated `AS` query selects
`NULL AS "CHUNK"` instead of the real column — because the code's
"does this table own this search column" check
(`chunky_deploy_handler.py:329-335`) matches on `sc.get("table") == tbl_name`,
so a `search_columns` item also needs an undocumented `"table"` key or the
column is silently nulled; and (b) I read the source at this point
(genuinely stuck — passed a real `warehouse` value and it still errored
"Missing option(s): [WAREHOUSE]") and found the actual bug:
`chunky_deploy_handler.py` lines 291-298 (the `use_single` branch, taken
whenever there's exactly one search column — the single most obvious thing a
first-time user would try) never appends the `WAREHOUSE = "..."` DDL clause
at all. It's only appended in the multi-column branch (line 311). **This
means DEPLOY create is unconditionally broken for any single-search-column
service — the natural first thing to try — with no workaround possible from
the calling interface.** I worked around it by adding a second search column
(`PDF_NAME`) to force the multi-column code path, which then hit one more gap:
that branch doesn't fall back to a default embedding model when omitted
(only the single-column branch does), producing `Invalid embedding model  in
the VECTOR INDEXES clause` until I passed `embedding_model` explicitly.

After all three workarounds, `create` succeeded, `describe` showed
`indexing_state: ACTIVE`, `source_data_num_rows: 24`, and a raw
`SNOWFLAKE.CORTEX.SEARCH_PREVIEW` SQL call against the service returned
correct, relevant results for "feed mills breeding farms" — so once built
correctly, the underlying search service genuinely works well. Reaction:
**blocked for a long time, then relieved**, but this is squarely the low
point of the whole test — three stacked undocumented/broken behaviors to get
through the single most standard workflow.

### 14. Keeping the search service in sync — no real answer
Re-ingesting a page in `APPEND` mode returned `"reindex": []` (nothing
happened automatically), and `CHUNKY_INGEST status` never listed the search
service under `search_services` even though it demonstrably existed and was
active (confirmed independently via `DEPLOY describe`). The `create` DDL uses
`TARGET_LAG = '365 days'`, i.e. effectively never auto-refreshes on its own.
An earlier `create` error (the `target_lag` rejection when I tried to pass
it) had suggested: `"remedy": "Use CHUNKY_INGEST('reindex', ...) for explicit
refresh."`(**BUG**: `reindex` is not a real command — confirmed by calling
it directly:)
```
CHUNKY_DEPLOY reindex {...}
"error": "Unknown command 'reindex' for CHUNKY_DEPLOY.",
"remedy": "Valid commands: alter, create, describe, drop, list, revert."
```
So the product's own error message pointed me at a command that does not
exist. **There is no product-level answer to "keep the search index in sync
after new ingests" — it's left entirely to the user, and the one hint the
product gives about how to do it is itself broken.** Reaction: **annoyed and
somewhat alarmed** — this is a core promise of the product (ingest → search)
that quietly doesn't deliver.

### 15. DEPLOY `list` — BUG (crash, then wrong SQL)
`help` marks `db`/`schema` as plain `{"type":"string"}` (not required) for
`list`. Calling with `{}` crashed:
```
KeyError: 'db'
  chunky_deploy_handler.py:76, in cmd_list
```
Retried with `db`/`schema` supplied:
```
"error": "(1304) ... SQL compilation error:\nUnknown table function INFORMATION_SCHEMA.CORTEX_SEARCH_SERVICES"
```
`DEPLOY list` appears to be entirely non-functional — the underlying SQL
table function name is wrong. Reaction: **blocked** — had to fall back to raw
`SHOW CORTEX SEARCH SERVICES` SQL myself to check what existed.

### 16. Content-quality bug found via the product's own `inspect_quality`
```
CHUNKY_INGEST inspect_quality {db, schema, table}
"defects": 25, "total": 25,
"defect_breakdown": {"REPAIR_SYNTAX": 23, "REPAIR_LOW_INFO": 2}
```
Nearly every chunk (24 of 25 rows, including the accidental duplicate from
step 14) is flagged `REPAIR_SYNTAX`. I checked the raw column value directly
with `SELECT LEFT(CHUNK, 60) ...` and confirmed the stored text is literally
double-JSON-encoded: it begins with a literal `"` character and contains
literal two-character `\n` sequences instead of real newlines (e.g.
`"# Index\n\n[VISUAL: Orange geometric shape...`, with the leading `"` and
`\n` present as raw bytes in the column, not JSON-escaping introduced by my
tooling). This is a systemic extraction/storage bug — the vision-extraction
step's JSON-string output is being written to the `CHUNK` column without
being decoded first. It doesn't block search (Cortex Search still returned
relevant hits), but it means every chunk of every vision-ingested document in
this bundle version carries this defect. Good news: the product's own
`inspect_quality` command caught it correctly and precisely — this is the
tool working as designed, catching a real upstream bug.

### 17. Layout-based ingest — not attempted
Given the amount of time consumed by the DEPLOY bugs above and the
double-encoding bug found via `inspect_quality`, I did not additionally run a
second `layout:true` ingest into a separate table. `ingest`'s help does
expose `layout` (default `false`) as a documented toggle, so the interface
for it is discoverable, but I did not verify its actual output quality.

### 18. Cleanup
Used the product's own commands where they existed:
- `CHUNKY_DEPLOY drop {service_name:"USERTEST_CSS"}` — worked cleanly,
  returned the full previous DDL for potential revert.
- No product command drops a whole table, so `DROP TABLE` via raw SQL was
  used for `USERTEST_CHUNKS` and the `USERTEST_CHUNKS_revert_backup_<epoch>`
  table that QA's `revert` (step 10) had silently created. Verified via
  `SHOW TABLES LIKE 'USERTEST%'` and `SHOW CORTEX SEARCH SERVICES LIKE
  'USERTEST%'` that both are empty — schema left clean.

## Prioritized friction points / bugs

1. **[Critical] DEPLOY `create` is broken for single-search-column services**
   (`chunky_deploy_handler.py` ~line 291-298 never emits `WAREHOUSE = ...` in
   the `use_single` branch). This is the most natural first thing a new user
   tries and it fails with a confusing `Missing option(s): [WAREHOUSE]` error
   even after correctly supplying `warehouse`. No workaround exists from the
   calling interface alone.
2. **[Critical] The one documented remedy for keeping a search service in
   sync after new ingests points to a nonexistent command**
   (`CHUNKY_DEPLOY('reindex', ...)` — actual error: `"Unknown command
   'reindex' for CHUNKY_DEPLOY."`). Combined with `TARGET_LAG = '365 days'`
   and `CHUNKY_INGEST status` never listing associated search services, this
   means there is no discoverable, working way to keep search results
   current after adding content.
3. **[High] Systemic double-JSON-encoding of chunk text.** Every
   vision-extracted chunk stores a literal leading `"` and literal `\n`
   sequences instead of real newlines (confirmed via raw SQL and via the
   product's own `inspect_quality`, which flagged 24/25 chunks
   `REPAIR_SYNTAX`). This is a content-correctness bug, not a docs problem.
4. **[High] Five raw, unhandled Python tracebacks leak through
   `help`-documented commands** when required-but-undocumented-as-required
   fields are omitted: `CHUNKY_QA generate_draft` (missing `stage_path`),
   `CHUNKY_DEPLOY create` (missing `warehouse`, and separately
   `search_columns` items being strings instead of objects), and
   `CHUNKY_DEPLOY list` (missing `db`). In every case, `help`'s field table
   for that command did **not** mark the field `"required": true`, so a user
   following the docs exactly will crash. Contrast with the well-behaved
   validation path (`CHUNKY_QA search` missing `table` → clean "Missing
   required instruction field" + remedy) — the good pattern clearly exists in
   the codebase, it's just not applied consistently everywhere.
5. **[High] `search_columns[]` and `attribute_columns[]` need an undocumented
   `"table"` key**, or the corresponding column is silently replaced with
   `NULL` in the generated search-service DDL (`SELECT NULL AS "CHUNK"`)
   with no warning that this happened. A user would deploy a "successful"
   search service that indexes nothing and have no way to know without
   inspecting the returned `ddl` closely.
6. **[Medium] `QA commit`'s `commits[]` item schema is undocumented** — the
   field is `draft_text`, not `chunk` (my first guess, matching the
   column name). Passing the wrong key gives `status: "skipped"` with
   `success: true` and a **misleading top-level warning claiming content was
   overwritten when nothing happened**. The per-item `status` is correct;
   the top-level `warning` text is not conditioned on it.
7. **[Medium] `QA delete` reports `"deleted": N` based on the number of
   chunk_ids requested, not rows actually deleted.** Deleting a
   nonexistent chunk_id returns `success: true, "deleted": 1` with a
   "permanently deleted" warning, even though zero rows changed.
8. **[Medium] `QA inspect` is unconditionally broken**
   (`invalid identifier 'CHUNK_TYPE'`) against tables created by the
   currently-deployed `CHUNKY_INGEST`, because the table schema it creates
   (`CHUNK_ID, PDF_NAME, PAGE_NUMBER, CHUNK, CHUNK_METADATA,
   PAGE_SCREENSHOT`) doesn't include columns (`CHUNK_TYPE`, `LINK_BLOCK`,
   `CHUNK_REF`) that `inspect`'s query expects. `list_chunks` masks this by
   defaulting those fields to empty strings instead of reading real columns,
   which is presumably why the mismatch wasn't caught before.
9. **[Medium] `DEPLOY list` is non-functional**
   (`Unknown table function INFORMATION_SCHEMA.CORTEX_SEARCH_SERVICES`) —
   there's no working way to enumerate existing search services through the
   product itself.
10. **[Low] QA `revert` leaves an untracked backup table behind**
    (`<table>_revert_backup_<epoch>`) with no mention in the response that
    it exists or should eventually be cleaned up.
11. **[Low, environment] JSON instructions containing spaces reliably break
    when passed inline on a PowerShell command line** (not the product's
    fault, but worth documenting in the CLI's own usage text) — the `@file.json`
    form works and should be the recommended default in any usage example.

## What worked well and shouldn't be changed

- **`help` and `help({"command": ...})`** as the self-documentation mechanism
  is a genuinely good pattern — clear, structured, discoverable, and mostly
  accurate. The failures above are about the content of the field metadata
  (missing `required` flags, missing nested-object shapes), not the mechanism
  itself.
- **Advisory locking with live progress and a precise remedy.** The
  in-flight-ingest collision message (`"... is being ingested by
  MCDAAI_DNA"` + exact `CHUNKY_INGEST('status', ...)` / `force:true` remedy)
  and `status`'s live `progress` block (current_page/pages_total/phase) are
  best-in-class for this kind of tool.
- **`estimate_cost`** — pages, USD, and duration-relevant metrics before
  committing to a long vision run, with an honest "heuristic" caveat.
- **Typo detection in field validation** (`"Unknown field 'tabel'. Did you
  mean 'table'?"`) plus a pointer back to the specific `help` call — this is
  the standard every error path in the product should meet.
- **Revert plumbing.** Both `CHUNKY_INGEST` and `CHUNKY_QA` return a
  ready-to-run `revert.command` (with `timestamp_before` and `query_ids`
  baked in) on every mutating call, and QA's `revert` additionally makes a
  safety backup table before restoring. This is a strong, consistent safety
  net once you're past the DEPLOY-specific hole in it (item 2 above).
- **Extraction quality itself** (once the double-encoding issue is
  set aside) is impressive — the vision pipeline produces well-structured
  markdown with meaningful `[VISUAL: ...]` descriptions of logos, charts, and
  photos, not just raw OCR text.
- **`inspect_quality`** did exactly what a quality-inspection command should:
  it caught a real, systemic content bug (item 3) that I would not have found
  from `list_chunks` output alone, since that view happens to look correct
  when pretty-printed.

## Where I had to read source code

I fell back to reading `procedure/utils/chunky_deploy_handler.py` in two
places, both after the documented interface gave contradictory or impossible
results:
1. After supplying a valid `warehouse` and still getting `Missing option(s):
   [WAREHOUSE]` from Snowflake — there was no way to tell from `help` or from
   the error message that the DDL builder skips the `WAREHOUSE` clause
   entirely on the single-search-column code path (lines 291-298 vs. 311).
2. To understand why my `search_columns` entries produced `SELECT NULL AS
   "CHUNK"` in the generated DDL instead of the real column — the `sc.get("table")
   == tbl_name` matching logic (lines 329-335) requires a `table` key that
   `help`'s `{"search_columns": {"type": "array"}}` gives no indication of.

In both cases the self-documentation was not sufficient to get unstuck; the
error text described a symptom ("Missing option(s): [WAREHOUSE]", a `NULL`
column with no accompanying warning) but not a cause or remedy the user could
act on without the source.
