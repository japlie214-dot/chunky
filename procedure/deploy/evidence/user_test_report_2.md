# Chunky user test report #2

Tester: first-time user, working only from `help` output (source read only where noted).
Scope: `SBOX_DB.AI_SB`, warehouse `SHARED_ID_XS`, all objects prefixed `USERTEST2_`.
Fixture: `@SBOX_DB.AI_SB.DOCS/chunky_fixtures/chunky_link_test.pdf` (7 pages, built to exercise hyperlink handling).

---

## 1. Hyperlink investigation — verdict up front

**The reported complaint ("Chunky failed at collecting the hyperlinks") is real and reproducible, but it has two distinct causes depending on which extraction strategy is used, plus a third loss point in the QA review flow:**

| Strategy (`vision`/`layout`) | Result |
|---|---|
| `vision:true, layout:false` (the **default**) | Ingest **succeeds**. Annotated hyperlinks *are* extracted and appended to the chunk text as a `[External links: ...]` block. But the dedicated structured field for links (`link_block`, returned by `list_chunks`/`search`) is **always empty string**, on every page, including pages where links were clearly captured in the text. There is no structured/queryable link data anywhere — only prose. |
| `layout:true` (alone, or combined with `vision:true`, i.e. "hybrid") | Ingest **crashes outright** with a raw Python traceback surfaced to the caller: `snowflake.connector.errors.ProgrammingError: 000904 (42000): SQL compilation error: ... invalid identifier 'LINK_BLOCK'`. Zero pages get ingested. This is the strategy documented by `help` as an alternative ("cheaper option"/layout parsing), and it is **completely non-functional** — reproduced identically 2/2 times, once alone and once combined with vision. |
| Any strategy → then QA `generate_draft` | `generate_draft` (the "AI draft" rewrite step meant for QA review) **silently drops the `[External links: ...]` block** from `draft_text` even for a chunk that has it in storage. If a user's QA workflow is generate_draft → commit, the previously-captured link is lost at commit time. (In my test, `commit` happened to no-op — see §3 — so the loss did not actually land, but the draft text itself already showed the link stripped.) |

### Per-link-type breakdown (vision strategy, the only one that produced usable output)

Fixture pages, expected vs. actual, vision mode:

| Page | Content | Expected | Actual (`list_chunks` output) |
|---|---|---|---|
| 1 | Annotated hyperlink to `https://example.com/investor-relations` | Link captured | Captured correctly in chunk text as `[External links: - https://example.com/investor-relations]`. `link_block` field = `""` (empty) despite this. |
| 2 | Plain-text URL `https://example.com/plain-text-only` (not a real annotation) | Preserved as literal text, not flagged as a "link" | Correctly preserved as literal text; no `[External links]` block generated (correct — it isn't a real annotation). |
| 3 | Three annotated hyperlinks | All three captured | All three captured in the `[External links: ...]` block. `link_block` still empty. |
| 4 | **Internal link** (page-to-page, "See page 1 for details") | Some indication of an internal navigation link | **Nothing.** No external-link block, no internal-link marker, no mention anywhere. The chunk text and metadata give zero evidence an internal link annotation exists on the page. This is the most likely real-world match for the original complaint: investor decks and SOPs often use internal ToC/cross-reference links, and Chunky drops them completely, silently. |
| 5 | Long paragraph, link mid-text, chunk split expected | Link attributed to the right chunk | Captured correctly; link appears in the chunk that contains "Methodology". |
| 6 | Table, no links | No link block | Correct — nothing captured. |
| 7 | Plain prose, no links | No link block | Correct — nothing captured. |

**Bottom line:** vision mode gets external/annotated links right as embedded text (5/5 pages with real annotations came through), completely misses internal links (1/1), and the layout-based alternative promised by `help` cannot ingest a single page without crashing. The structured `link_block` metadata field that a user would reasonably expect to query (`SELECT ... WHERE link_block LIKE '%...%'`) never has content, in either strategy — it's a stub.

### Root cause (had to fall back to source-reading here — see §5)

`DESCRIBE TABLE` on a freshly created Chunky table shows only `CHUNK_ID, PDF_NAME, PAGE_NUMBER, CHUNK, CHUNK_METADATA, PAGE_SCREENSHOT` — **no `LINK_BLOCK` column exists in the actual table schema** (`schema_version: 2`). The `link_block`, `chunk_ref`, `chunk_type` fields returned by `list_chunks`/`search` are synthesized/defaulted at read time, not real columns. When `layout:true` extraction tries to write a pandas dataframe that includes a `LINK_BLOCK` column via `session.write_pandas(...)`, Snowflake rejects it because the target table has no such column — hence the crash. QA's `inspect` command independently references a `CHUNK_TYPE` column directly in SQL (not through the tolerant JSON path) and crashes the same way: `invalid identifier 'CHUNK_TYPE'`. The product code appears to have been written against a newer table schema (with `LINK_BLOCK`/`CHUNK_TYPE`/`CHUNK_REF` as real columns) that was never migrated into the tables `ingest` actually creates.

---

## 2. Chronological log

1. `CALL CHUNKY_INGEST('help', {})` / `('help', {"command":"ingest"})` — clean, well-structured docs. Discovered `vision` (default true) and `layout` (default false) toggles.
2. Ingested fixture with `vision:true, layout:false` into `USERTEST2_LINKTEST_VISION` → **success**, 7/7 pages, `vision_pages: 7`. See §1 for content findings.
3. Ingested same fixture with `vision:false, layout:true` into `USERTEST2_LINKTEST_LAYOUT` → **crashed** with raw traceback, `invalid identifier 'LINK_BLOCK'`. Table was created (0 rows) before the crash — orphaned empty table left behind (cleaned up at end).
4. Ingested same fixture with `vision:true, layout:true` (hybrid) into `USERTEST2_LINKTEST_HYBRID` → **identical crash**, same stack trace, same missing identifier. Confirms `layout` extraction is broken outright, standalone or combined.
5. `CHUNKY_INGEST('status', ...)` on the two crashed tables confirmed no dangling locks were left (`locks.ingest: null`) — crash-safety on the lock/lease side is good.
6. `list_chunks` on the vision table — full text dump reviewed page by page (§1 table above).
7. `inspect_quality` on the vision table — flagged **all 7/7 chunks as defects** (`REPAIR_LOW_INFO` x6, `REPAIR_SYNTAX` x1), i.e. a 100% defect rate on a document that ingested exactly as designed. The short, deliberately-terse fixture pages (by design, to isolate link behavior) apparently trip a length-based "low info" heuristic for nearly every page. On a real, well-formed short document this makes `inspect_quality`'s signal close to noise — a user cannot tell "genuinely bad extraction" from "short page" from this command alone.
8. `CHUNKY_QA('search', {"search_text":"methodology"})` — worked, returned the page 5 chunk with its link intact.
9. `CHUNKY_QA('search', {"search_text":"revenue growth numbers table"})` — **zero results**, even though page 6's table is exactly about revenue. `CHUNKY_QA('search', {"search_text":"Revenue"})` (literal substring) → 1 result. Conclusion: `search` is a literal substring match on stored text, not the semantic search a "search" command next to a Cortex-Search-Service-backed product implies. Confirmed by directly calling `SNOWFLAKE.CORTEX.SEARCH_PREVIEW` on the deployed service with the same non-literal query — the deployed service found the right page via cosine similarity when the QA "search" command could not. This is a naming/expectation gap: two different things are both called "search."
10. `CHUNKY_QA('inspect', {chunk_id: <valid id>})` → **crashed**, `invalid identifier 'CHUNK_TYPE'` (raw JSON error, not a traceback — better than the ingest crash, but still a hard failure on a documented, basic command). Could not successfully inspect a single chunk through the documented interface at all.
11. `CHUNKY_QA('inspect', {})` (no `chunk_id`) → **crashed harder**, raw Python traceback, `KeyError: 'chunk_id'`. `help` does **not** mark `chunk_id` as `required` for `inspect` — it should.
12. `CHUNKY_QA('generate_draft', {chunk_ids:[...]})` without `stage_path` → **crashed**, raw traceback, `KeyError: 'stage_path'`. Again, `help` lists `stage_path` for `generate_draft` without `required: true` — undocumented requirement.
13. Retried `generate_draft` with `stage_path` supplied → succeeded, but silently dropped the `[External links: ...]` block from `draft_text` (§1).
14. `CHUNKY_QA('commit', {commits:[{chunk_id, chunk: <original text without the link footer>}]})` → returned `status: "skipped"` **and simultaneously** a warning saying `"Chunk content was overwritten."` These two signals directly contradict each other. Checked the actual stored `CHUNK` value afterward — it was **unchanged** (link footer still present), so "skipped" was the true outcome, but the misleading warning would make a real user believe they'd just destroyed data.
15. `CHUNKY_DEPLOY('create', {tables:["USERTEST2_LINKTEST_VISION"], search_columns:["CHUNK"], attribute_columns:["PAGE_NUMBER","PDF_NAME","CHUNK_ID"], ...})` → first attempt ran long (my client-side tool timed out and moved it to background/eventually reported "killed" locally), but the service was in fact created successfully server-side (`indexing_state: ACTIVE`, `source_data_num_rows: 7`) — killing the local client did not cancel the remote Snowflake work.
16. Ran `CHUNKY_DEPLOY('create', ...)` again with the **same** service name/table (deliberately, to try "call something twice in a row") → this second call polled for service readiness for the full 900s timeout and then reported `"error": "Cortex Search Service did not become ready"`, **despite the service being active and correctly answering searches the entire time** (verified independently via `SNOWFLAKE.CORTEX.SEARCH_PREVIEW`, which returned correct, ranked results). The advisory `deploy` lock from the first call remained held (visible via `CHUNKY_INGEST('status', ...)`) with a 30-minute TTL, blocking/confusing the second call.
17. `SNOWFLAKE.CORTEX.SEARCH_PREVIEW('...USERTEST2_LINKTEST_SEARCH', {"query":"financial results revenue growth", ...})` → correctly ranked the page-6 revenue table first by `cosine_similarity`, confirming the underlying Cortex Search Service and `CHUNKY_DEPLOY create` DDL are genuinely good — the problem in step 16 is Chunky's own readiness-polling/lock handling around repeat `create` calls, not the search service itself.
18. `CHUNKY_DEPLOY('describe', {service_name:"USERTEST2_LINKTEST_SEARCH"})` → clean, useful output including `service_query_url`.
19. Deliberately re-ingested the same fixture into the already-populated `USERTEST2_LINKTEST_VISION` table with identical instruction JSON (a duplicate `ingest` call) → **succeeded but duplicated every page** (14 rows total), with a clear warning: `"APPEND mode inserted rows for pages that already existed... Duplicate PAGE_NUMBERs for this file: 1, 2, 3, 4, 5, 6, 7"` plus a ready-to-run `revert` command. Used the returned `revert` command via `CHUNKY_INGEST('revert', ...)` — it worked, restored 7 rows, and additionally created a safety backup table (`..._revert_backup_<epoch>`), which itself then needed cleanup.
20. Missing-required-field test: `CHUNKY_INGEST('ingest', {db, schema, table})` (no `stage_path`/`file`) → clean, helpful structured error: `"Missing required instruction field: stage_path.; Missing required instruction field: file."` plus a `remedy` pointing back to `help`. Good UX.
21. Typo'd field test: `CHUNKY_INGEST('ingest', {..., "fiel": "chunky_link_test.pdf"})` → excellent error: `"Unknown field 'fiel'. Did you mean 'file'?"` — genuinely delightful, did-you-mean suggestion.
22. Garbage-value test: `CHUNKY_INGEST('status', {db:"NOT_A_REAL_DB", schema:"AI_SB", table:"USERTEST2_LINKTEST_VISION"})` → returned `success: true` with an empty/default status body (`sources: [], last_run_id: null, locks: all null`) instead of any error about the nonexistent database. A user could easily mistake "table has no history" for "I typo'd my database name."
23. Cleanup: dropped the search service via `CHUNKY_DEPLOY('drop', ...)` — worked cleanly, returned `previous_ddl` and a `revert` command (though the drop's own warning says the service "cannot be restored" via that revert, which is itself a slightly confusing pairing — a `revert` payload offered for an action described as irreversible). Dropped the four leftover tables (`_LAYOUT`, `_HYBRID`, `_VISION`, and the auto-created `_revert_backup_<epoch>`) via raw SQL, since `CHUNKY_INGEST` has no documented `drop` command for tables (only `revert`, which doesn't apply to a table headed for full deletion). Verified via `SHOW TABLES`/`SHOW CORTEX SEARCH SERVICES` that no `USERTEST2_*` objects remain in `AI_SB`.

---

## 3. Friction points / bugs, prioritized

**P0 — data-loss / crash risk**
- `layout:true` ingest (alone or combined with `vision:true`) crashes 100% of the time with a raw traceback (`invalid identifier 'LINK_BLOCK'`), and never produces a single chunk. This is one of two documented extraction strategies and it is entirely unusable.
- `CHUNKY_QA('inspect', {chunk_id:...})` crashes with `invalid identifier 'CHUNK_TYPE'` for *every* chunk in *every* table I tried it on. There is no working way to inspect a chunk through the documented interface.
- `CHUNKY_QA('inspect', {})` and `generate_draft` without `stage_path` throw raw `KeyError` tracebacks rather than the clean, structured "missing required field" errors that `ingest` produces for the same class of mistake — inconsistent error handling across procedures/commands.
- `generate_draft` silently strips the `[External links: ...]` block from its AI-generated draft text. Since generate_draft → commit is presumably the intended QA correction workflow, this is a direct path to silently losing previously-captured link data.

**P1 — misleading output**
- `commit` returned `status: "skipped"` for a chunk while its top-level `warning` field claimed `"Chunk content was overwritten."` The two are contradictory; I had to check the raw table via SQL to find out which one was true (it was not overwritten).
- `status` against a nonexistent database returns `success: true` with an empty-but-valid-looking body instead of surfacing that the database doesn't exist.
- Repeat `CHUNKY_DEPLOY('create', ...)` on an already-active service polled for the full 900-second timeout and reported failure ("did not become ready") even though the service was active and serving correct results the entire time — a false negative that would send a real user chasing a non-existent problem.
- `CHUNKY_DEPLOY('drop', ...)` bundles a `revert` command in its response even though the same response's `warning` says the drop "cannot be restored." Offering a revert payload for an irreversible action is confusing.

**P2 — documentation / help gaps**
- `help` for `inspect` does not mark `chunk_id` as `required`, but the command hard-crashes without it.
- `help` for `generate_draft` does not mark `stage_path` as `required`, but the command hard-crashes without it.
- Nothing in `help` clarifies that `link_block` (returned by `list_chunks`/`search`) is currently always empty, or that link text is instead appended inline to `chunk` as a `[External links: ...]` block — a user has to discover this by reading actual row content.
- Nothing in `help` clarifies that `CHUNKY_QA search` is a literal substring match rather than the semantic search implied by the product's Cortex Search Service integration; the two "search" surfaces (QA `search` vs. a deployed search service) behave completely differently and a user would naturally conflate them.
- `inspect_quality`'s heuristics flagged 100% of a small, well-formed test document as defective, with no guidance in `help` on what thresholds drive `REPAIR_LOW_INFO`/`REPAIR_SYNTAX` or how to tune them for short documents.

**P3 — operational rough edges**
- Killing/timing-out a client-side call to `CHUNKY_DEPLOY create` does not cancel the underlying Snowflake work; the service kept building server-side and finished successfully, but nothing in the tool surfaces that state to a client that gave up waiting.
- An abandoned `deploy` advisory lock persisted with a 30-minute TTL and directly caused the confusing "did not become ready" failure on the very next `create` call against the same objects.
- `revert` (a good safety feature) creates an additional backup table as a side effect, which is itself a new object a user must remember to clean up — not mentioned up front in `help` for `revert`/`ingest`.

---

## 4. What worked well

- `help` (both the command list and per-command field help) is genuinely good: clear required/optional/default markers, and every command's `help` was self-sufficient for constructing a first call.
- Field-validation errors on `ingest` are excellent: missing-required-field errors list every missing field at once, and typo'd field names get a "did you mean" suggestion pointing at the real field — the best error UX I hit in the whole test.
- `APPEND`-mode duplicate detection is solid: re-ingesting the same file didn't fail or silently overwrite, it added rows but *clearly* flagged the exact duplicate page numbers and handed back a ready-to-run `revert` command with the query IDs already filled in. Using that revert command worked exactly as advertised.
- Vision-mode extraction itself, text-wise, is good: it correctly distinguishes an annotated hyperlink (turned into an `[External links]` block) from a plain-text URL (left as literal text, not falsely flagged as a link) — precisely the distinction the fixture was built to test, and it got 5 of 5 annotated-link pages right.
- The actual Cortex Search Service that `CHUNKY_DEPLOY create` builds works correctly — semantic ranking via `SEARCH_PREVIEW` surfaced the right page for a non-literal query when literal `QA search` could not.
- `CHUNKY_DEPLOY drop`, `describe`, and `list` are clean, fast, and return genuinely useful structured data (DDL, previous DDL, per-service stats).
- No dangling locks were left behind by any of the crashed `ingest` calls — crash-safety on the locking layer held up even when the extraction logic itself blew up.

## 5. Where I had to fall back to source-reading, and why docs weren't enough

I read `procedure/utils/*.py` (specifically the ingest and QA handlers) exactly once, after reproducing the `LINK_BLOCK`/`CHUNK_TYPE` crashes and needing to understand *why* a documented field/behavior was failing outright rather than just misbehaving. `help` describes the instruction schema (what fields a command accepts) but says nothing about the actual table schema Chunky creates and expects (`DESCRIBE TABLE` was necessary to discover that `LINK_BLOCK`/`CHUNK_TYPE`/`CHUNK_REF` don't exist as real columns). There is no documented or self-service way to learn "what columns does a Chunky-managed table have, and does that match what the code expects" — `status` reports lock/lease/source metadata but not column-level schema info. Surfacing table schema version / expected-vs-actual column list in `help` or `status` would have let me diagnose this without opening the source.
