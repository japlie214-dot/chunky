# Chunky Headless — Build Plan

> **This is a build plan, not a review.** It specifies a working product:
> three Snowflake stored procedures that take a PDF on a stage to a serving
> Cortex Search Service, driven entirely over an API. Where the existing
> prototype is wrong, the plan carries the corrected implementation inline —
> not a description of the problem. The defect register that motivated each
> fix is in [Appendix A](#appendix-a--defect-register), out of the way.
>
> **Definition of done.** From a clean `SBOX_DB.AI_SB`, one command runs:
> `pytest` → build bundle → deploy → smoke test, and the smoke test performs a
> real Ingest → QA → Deploy cycle against a real PDF, ending with a Cortex
> Search Service that returns hits, has scheduled indexing switched off, and
> re-indexes on demand when the chunk table changes. Every step is
> reproducible on the Windows machine this work is being done from.

---

## Table of contents

1. [Locked decisions](#1-locked-decisions)
2. [The user's journey](#2-the-users-journey) — what this is *for*
3. [Architecture](#3-architecture)
4. [Environment](#4-environment)
5. [Repository layout](#5-repository-layout)
6. [Phase 0 — Bring-up](#phase-0--bring-up)
7. [Phase 1 — Build & deploy toolchain](#phase-1--build--deploy-toolchain)
8. [Phase 2 — Package skeleton](#phase-2--package-skeleton)
9. [Phase 3 — Ingest](#phase-3--ingest)
10. [Phase 4 — QA](#phase-4--qa)
11. [Phase 5 — Deploy](#phase-5--deploy)
12. [Phase 6 — Leases, progress & API contract](#phase-6--leases-progress--api-contract)
13. [Phase 7 — Production auth, docs, handover](#phase-7--production-auth-docs-handover)
14. [Testing](#14-testing)
15. [The iteration loop](#15-the-iteration-loop)
16. [Runbook](#16-runbook)
17. [Working agreement](#17-working-agreement)
18. [Appendix A — Defect register](#appendix-a--defect-register)
19. [Appendix B — File disposition](#appendix-b--file-disposition)

---

## 1. Locked decisions

No open questions block the build. These are settled; implement them.

| # | Decision |
|---|---|
| 1 | **Procedure names:** `chunky_chunks` → **`CHUNKY_INGEST`**, `chunky_searchservice` → **`CHUNKY_DEPLOY`**, `chunky_qa` unchanged. |
| 2 | **No backward compatibility.** No aliases for the old procedure names, no migration views, no support for the old table shape. The Streamlit app is being retired; do not touch it and do not accommodate it. |
| 3 | **`db` and `schema` are required on every command.** There is no `DEFAULT_DB` / `DEFAULT_SCHEMA`. A headless caller has no session gate to fall back on, so an omitted target is an error, never a guess. |
| 4 | **Chunk table:** six columns — `CHUNK_ID` (`CHK_<ULID>`), `PDF_NAME`, `PAGE_NUMBER`, `CHUNK`, `CHUNK_METADATA` (VARIANT), `PAGE_SCREENSHOT` (BINARY). `CHUNK_TYPE`, `CHUNK_REF` and `LINK_BLOCK` become `CHUNK_METADATA` fields — confirmed no consumer needs them as columns. |
| 5 | **Page screenshots** render at **200 DPI**, capped at **3.5 MB** and **8000 px** on the long edge, stored once per page in `PAGE_SCREENSHOT`. `SCREENSHOT_MAX_BYTES == CORTEX_IMAGE_MAX_BYTES == 3_500_000` — **one budget, not two**. Claude models cap image input at 3.75 MB and reject resolutions above 8000×8000, so rendering straight to the Cortex-safe size means the stored image is always sendable as-is: no derivative, no second encode path, no way to accidentally hand Cortex an oversized image. QA and Vision re-parse read the column instead of re-rendering the PDF. |
| 6 | **Search-service warehouse:** the caller's `CURRENT_WAREHOUSE()`, unless `instruction.warehouse` is supplied. Never a constant. |
| 7 | **Embedding model:** `voyage-multilingual-2` — confirmed available in this region. It is the default. |
| 8 | **Scheduled reindexing is switched off** immediately after a service is created and verified: `ALTER CORTEX SEARCH SERVICE … SUSPEND INDEXING`. Serving continues; only the background refresh stops. |
| 8b | **`TARGET_LAG` is not part of the instruction surface.** It is syntactically required by `CREATE CORTEX SEARCH SERVICE`, so Chunky emits a fixed internal constant (`'365 days'`) and never accepts it from a caller — not on `create`, not on `alter`. With indexing suspended (decision 8) the value is inert; exposing it would let someone set a short lag that does nothing until a future `RESUME INDEXING` turns it into an expensive surprise. Refresh is explicit and on-demand (decision 10). |
| 9 | **The chunk table's `COMMENT` carries a JSON metadata block**, including the list of Cortex Search Services that index it. Chunky writes it and reads it back. |
| 10 | **Every successful mutation auto-reindexes.** After an ingest (or a QA commit/delete) succeeds, read the table comment, find the services, and `REFRESH` each one, then re-suspend. |
| 11 | **Production API auth: key-pair JWT.** A dedicated service user with `RSA_PUBLIC_KEY`. The dev OAuth path stays for local work. |
| 12 | **Ship both poppler architectures** (arm64 + x86_64) in one bundle. |
| 13 | **Render-stage cleanup is manual:** a `gc_renders` command plus a documented sweep. No scheduled task. |
| 14 | **The signature is fixed at `(COMMAND VARCHAR, INSTRUCTION VARIANT)`.** No extra parameters on any procedure, ever. One call shape is what lets a single generic client, agent tool definition and `sf call` wrapper drive all three procedures. |
| 15 | **Every procedure answers `help`**, generated from a declarative command registry so it cannot drift from the implementation (§3.9). |
| 16 | **No control table.** There is no `CHUNKY_RUNS`, no run history, no idempotency ledger, no hard QA gate. Coordination lives in the chunk table's own `COMMENT` as **advisory leases** (§3.6): one ingest at a time per table, one QA write at a time, reads never blocked. Audit and cost come from `QUERY_HISTORY` via `QUERY_TAG`. Live progress lives in the lease, so it survives the simplification. |
| 17 | **`sign_off` is advisory.** It writes a `qa` block into the comment; `CHUNKY_DEPLOY('create')` warns when it is missing or stale, and proceeds. Nothing is hard-blocked — a bypass flag everyone learns to pass is worse than no gate. |

---

## 2. The user's journey

Headless Chunky has no screen. That makes it *easier* to build something
technically correct and useless — a set of procedures that each return
`success: true` while nobody can actually get a document searchable without
reading the source. This section is the antidote: what someone is trying to
do, in their words, and what each step forces the design to provide. Several
requirements in Phases 3–6 exist only because of what is written here, and
they are marked **⇒ design consequence** where they appear.

### 2.1 Who is on the other end

Four consumers, with different needs and one shared property: none of them can
see a screen we control.

| | Who | What they want | What breaks them |
|---|---|---|---|
| **A** | **The integrator** — builds the API client or the replacement front end | A contract they can code against once | Inconsistent response shapes; hidden state; not knowing whether a retry is safe |
| **B** | **The knowledge owner** — "I have a document, make it findable" (reaching Chunky through whatever UI A builds) | Confidence the extraction is right, and that search now finds their thing | Silence during long runs; no way to see what the AI actually read |
| **C** | **The operator** — runs it at volume, pays for it, fixes it at 6pm | Cost visibility, an audit trail, and a way back | Errors with no next action; no record of what ran |
| **D** | **The agent** — an LLM calling these procedures as tools | Self-describing commands and errors that state the remedy | Errors that require reading Snowflake docs to interpret |

**D is not hypothetical, and it sharpens everything.** An agent cannot infer
from `Failed to read PDF: 253006` that the file name is wrong. It will retry
the identical call, or invent a fix. An error that says *what to do next* is
the difference between a tool an agent can use and one it thrashes against.
Designing for D happens to give A, B and C a better product too.

### 2.2 The journey, step by step

**"I have a PDF I want people to be able to search."**

---

**① "Where does the file go?"**

Chunky does not upload. The PDF must already be on a Snowflake stage, and the
stage must be server-side encrypted with a directory table. That is the very
first step and it is *outside the product* — the most common way a first
attempt fails.

⇒ **design consequence.** `ingest` must fail on a missing file with the stage
it looked at, the exact path it built, and the `LIST` command to check —
not a raw Snowflake error code. `API.md` opens with staging, not with
`ingest`.

---

**② "What will this cost me, and how long will it take?"**

Before committing to a 200-page document, both matter — and *time* matters
more than money to the client author, because it sets the HTTP timeout.

⇒ **design consequence.** `estimate_cost` returns
`estimated_duration_seconds` alongside credits, derived from page count × the
measured per-page Vision latency. The response says which `timeout` to put in
the SQL API request body. A cost estimate that doesn't answer "how long"
leaves the integrator guessing, and they will guess low.

---

**③ "Go." … and then six minutes of nothing.**

This is the single biggest UX hole in a headless design. A 40-page Vision
ingest is ~6 minutes. The SQL API returns HTTP 202 immediately, then the
client polls a statement handle that says only *running*. The knowledge owner
(B) sees a spinner with no information; the integrator (A) cannot build a
progress bar; the operator (C) cannot tell a slow run from a hung one.

⇒ **design consequence — this changes Phase 3 and Phase 6.** The ingest's
lease in the table comment (§3.6) carries a `progress` block that it
**updates as it goes**, not only at start and end:

```json
{"pages_total": 40, "pages_done": 17, "phase": "vision",
 "current_page": 18, "started_at": "…", "eta_seconds": 184}
```

`CHUNKY_INGEST('status', {db, schema, table})` reads it. Throttled to at most
one write per 30 s — a heartbeat is `ALTER TABLE … SET COMMENT`, i.e. DDL, so
one per page on a long document is real churn for no added insight. It must
sit **outside** any explicit transaction, and a failure to write progress must
never fail the ingest.

Without this, the product is technically complete and feels broken.

---

**④ "Did it actually read my document properly?"**

Nobody trusts an AI extraction they cannot check. The knowledge owner wants
the page image side by side with the text that came out of it. That is the
whole reason QA exists, and it is why `PAGE_SCREENSHOT` is worth its storage:
**it is the trust mechanism, not just a cache.**

⇒ **design consequence.** `CHUNKY_QA('inspect')` returns the chunk text *and*
a working image URL in **one call** — never "call inspect, then call something
else for the picture". The image must be legible enough to read a dense
financial table, which is why 200 DPI is a floor rather than a target, and why
the storage cap is generous (Phase 3.5). And `search` must stay cheap enough
to browse: screenshots off by default, so scanning 100 chunks costs nothing.

---

**⑤ "Page 7 is mangled. Fix just page 7."**

A user who spots one bad page wants to fix that page. Re-running the whole
document to repair one page is the kind of thing that makes people abandon a
tool.

⇒ **design consequence.** `generate_draft` → review → `commit` operates on
`chunk_ids`, and `ingest` accepts `range` with `mode: SURGICAL`. Both already
exist; the API contract must make the single-page repair loop the *documented
example*, not a footnote — because the obvious-but-wrong move is to re-ingest.

---

**⑥ "Is it good enough to publish?"**

The QA gate. Its job is to stop an unreviewed table becoming a live search
service — but a gate that only says "no" is bureaucracy, and people route
around bureaucracy (here, with `force: true`, permanently).

⇒ **design consequence.** `sign_off` is **advisory, not a gate** — it writes
a `qa` block into the table comment, and `CHUNKY_DEPLOY('create')` *warns*
when it is missing or older than the last ingest, then proceeds. A refused
sign-off (defects found) still returns the breakdown *and the offending
`chunk_id`s*, so the next action is obvious and small; a clean table signs off
in one call with no ceremony. Nothing is ever hard-blocked, because a headless
pipeline that legitimately trusts its input should not have to learn a bypass
flag on day one — and a bypass everyone uses is worse than no gate.

---

**⑦ "Make it searchable."**

The moment of truth is not "the service was created". It is **"I searched for
something I know is in there and found it."**

⇒ **design consequence.** `create` chains `wait_ready` → `verify` →
`suspend_indexing` by default, and `verify` takes the *user's own* query and
returns the matched text so a human can eyeball it. Zero hits against a
non-empty table is a failure, not a success. One call in, a serving service
out — the user never has to know that "created" and "queryable" are different
states in Cortex Search.

---

**⑧ "I've added the 2025 report to the same collection."**

Here is where most document-indexing systems quietly rot: the user adds
content, and the search index silently doesn't have it. They only find out
when someone reports a bad answer, weeks later.

⇒ **design consequence — the whole point of the table-comment protocol
(§3.4).** The user names a document and a table. They never name a search
service again after the first deploy. Chunky reads the comment, finds the
services, refreshes them, and re-suspends. If it can't, the response says
which service is stale and gives the one command to fix it — and the ingest
still reports success, because the rows *are* written and telling them
otherwise would be a lie.

This is the single highest-value behaviour in the product. Everything about
the comment protocol exists to serve it.

---

**⑧b "Someone else is already loading that table."**

Two people — or two agent runs, or a retried API call whose first attempt is
still going — start an ingest into the same table minutes apart. Left alone,
page numbers interleave and an `OVERWRITE` deletes rows the other job is still
writing. The damage is silent and only shows up later as a document with
missing pages.

⇒ **design consequence — this is why `CHUNKY_RUNS` was dropped rather than
replaced.** The only coordination anyone actually needs is *"is somebody
writing to this table right now?"*, and the table comment can answer it
(§3.6). The second caller is refused with the holder, how long they've been
going, and **how far along they are**, so "try later" becomes "try in three
minutes". Leases expire, `force: true` breaks them, and reads are never
blocked — a reviewer can keep reading while an ingest runs.

---

**⑨ "That was wrong. Undo it."**

⇒ **design consequence.** Every mutating response carries a
`revert.command` string that is a complete, runnable `CALL` — copy, paste,
done. No reconstruction from a timestamp, no reading the docs. The backup
table is left behind deliberately, so a bad revert is also reversible.

---

**⑩ "What happened last Tuesday? What did this cost?"**

⇒ **design consequence.** No history table (§3.6) — `QUERY_TAG` carries
`Chunky/<version>|user=…|cmd=…|run=<run_id>` on every statement, so
`QUERY_HISTORY` and `CORTEX_FUNCTIONS_USAGE_HISTORY` already answer "what ran
last Tuesday" and "what did ingestion cost last month" without Chunky owning
an object. `help`'s troubleshooting topic ships both queries ready to paste,
because an audit trail nobody can find is not an audit trail.

### 2.3 What the whole product is, in three calls

If the design is right, the happy path reads like this and nothing else is
needed:

```sql
CALL CHUNKY_INGEST('ingest',   {…document, table…});   -- ② ③
CALL CHUNKY_QA    ('sign_off', {…table, verdict…});    -- ④ ⑤ ⑥ (advisory)
CALL CHUNKY_DEPLOY('create',   {…table, service…});    -- ⑦
```

…and from then on, adding a document is **one** call, because ⑧ is automatic:

```sql
CALL CHUNKY_INGEST('ingest', {…next document…});       -- reindexes by itself
```

Everything else in the command surface — `estimate_cost`, `list_chunks`,
`inspect_quality`, `generate_draft`, `revert`, `status`, `reindex` — exists to
support a specific moment above. If a command cannot be traced to one, it does
not need to exist.

### 2.4 Audit: are the prototype's outputs clear enough?

Short answer: **no — 4 of 31 error sites meet the bar.** This is an inventory
of every user-facing string in `procedure/utils/*.py`, graded against §2.2's
demand that a response tell the caller what to do next. It is the evidence for
the `remedy` field and the `help` command, and the checklist Phase 3–5 works
through.

| Grade | Count | Pattern |
|---|---|---|
| ✅ Actionable | 4 | names the cause, the fix, and often a workaround |
| ◐ Named but inert | 8 | says which operation failed, not what to do |
| ✗ Raw passthrough | 14 | `"error": str(e)` — a bare Snowflake error |
| ✗ Terse validation | 5 | says what's missing, not what a valid call looks like |

#### ✅ What good looks like — already in the codebase

The Vision/poppler failure is the best string in the prototype, and it is the
template for everything else:

> *"Vision extraction requires poppler binaries bundled for the runtime
> architecture (detected: arm64). The utils_bundle.zip is missing
> poppler_bundle/arm64/poppler/bin/. Rebuild the bundle with
> `python3 procedure/build_bundle.py --clean` … and re-upload to your stage.
> As a workaround, set `vision: false, layout: true` in the instruction JSON
> to use Layout-only ingestion."*

Cause, evidence, fix, **and a workaround that unblocks the caller right now**.
The other three that pass: `revert.py`'s "Revert requires either
`timestamp_before` or `query_ids`" (names both valid options), its
retention-window error (names the timestamp, the age and the limit), and
`chunky_searchservice_handler`'s "Cortex Search Services cannot be reverted
via TIME TRAVEL — the original DDL must be supplied" (explains *why*).

Tellingly, all four sit in the most recently written modules. The pattern was
being discovered but never retrofitted.

#### ✗ The 14 raw passthroughs

`list_chunks`, `update_chunk`, `delete_chunks`, `inspect_quality`, QA
`search` / `inspect` / `delete`, and every `chunky_searchservice` command
return `{"success": false, "error": str(e), "data": None}`. The caller gets

```
"error": "002003 (42S02): SQL compilation error: Object 'SBOX_DB.AI_SB.CHUNKS_X' does not exist or not authorized."
```

…and cannot tell whether the table is missing, misspelled, or simply not
granted to their role — three problems with three different fixes. The agent
(consumer D) will retry verbatim. **⇒** every `except` gets the failing
object, the caller's role, and a `remedy` that distinguishes the cases:
*"Table not found or not visible to role ANALYST. Check the name with `SHOW
TABLES LIKE 'CHUNKS_X' IN SCHEMA SBOX_DB.AI_SB`; if it exists, ask for `SELECT`
on it."*

#### ✗ The worst one: unknown command

All three procedures answer an unrecognised command with

```json
{"success": false, "error": "Unknown command: ingets", "data": null}
```

This is the **most likely first mistake anyone makes** — a typo, a
half-remembered name, an agent guessing — and the response is a dead end. It
doesn't list the valid commands, doesn't suggest the near match, and there is
nothing else to call to find out. **⇒** this single case is why `help` exists
(§3.9); the dispatcher's unknown-command branch lists the valid commands,
offers the closest match, and points at `help`.

#### ◐ Named but inert

`"Table init failed: {e}"`, `"Failed to read PDF: {e}"`,
`"AI_PARSE_DOCUMENT failed: {e}"`, `"Surgical delete failed: {e}"`,
`"TIME TRAVEL CLONE failed: {e}"`, `"DROP TABLE failed (cannot revert without
backup): {e}"`, `"Row-level revert failed: {e}"`, and the batch summary
`"Batch ingest complete: N succeeded, M failed."`.

These name the operation, which is real progress over a raw error, but stop
there. `"Failed to read PDF"` in particular is journey step ① — the single
most common first-run failure — and it says nothing about staging, nothing
about how `stage_path` and `file` are joined, and nothing about `LIST`.
`"Batch ingest complete: 3 succeeded, 2 failed"` doesn't say *which* two.

#### ✗ Terse validation

`"No filter provided"`, `"No chunk_ids provided"`, `"No commits provided"`,
`"No jobs provided in instruction.jobs"`, `"Chunk not found: CHK_…"`. Correct,
and useless to someone who doesn't already know the schema. **⇒** a validation
failure should render the field spec for that command — which the command
registry (§3.9) makes free.

#### The success path is silent about what comes next

A successful `ingest` returns metrics, grants, warnings and a revert payload —
and no indication that QA and Deploy exist. For a headless caller with no UI
to guide them, and for an agent in particular, the response is the *only*
place that knowledge can live.

**⇒** every successful mutating response carries a `next` block:

```json
"next": [
  {"why": "Review the extraction before publishing it",
   "call": "CALL CHUNKY_QA('search', OBJECT_CONSTRUCT('db','SBOX_DB','schema','AI_SB','table','CHUNKS_X','limit',20));"},
  {"why": "Then sign off so the service can be deployed",
   "call": "CALL CHUNKY_QA('sign_off', OBJECT_CONSTRUCT('db','SBOX_DB','schema','AI_SB','table','CHUNKS_X','run_id','RUN_01JZ…','verdict','APPROVED'));"}
]
```

Fully-formed, paste-able calls with the caller's own values already
substituted — the same standard `revert.command` already meets. `next` is
context-dependent: after `sign_off` it points at `CHUNKY_DEPLOY('create')`;
after a `create` whose reindex failed it points at `reindex`; after a QA
`commit` it is empty, because nothing further is required.

#### Two shaping problems

`chunky_searchservice`'s `list` and `describe` return
`[r.as_dict() for r in rows]` — raw `SHOW`/`DESCRIBE` output, whatever columns
Snowflake happens to emit that release. **⇒** project a stable subset
(`name`, `serving_state`, `indexing_state`, `indexing_error`, `warehouse`,
`source_tables`), keep the raw rows under `data.raw`, and the contract stops
depending on Snowflake's column naming.

`grant_result` reports `success_roles` / `failed_roles` with no reason
attached. **⇒** the three-list shape in Phase 2.6.

### 2.5 Principles this yields

These are the tie-breakers. When an implementation choice is otherwise even,
these decide it.

1. **Every error names the next action.** The envelope carries an optional
   `remedy` string: a literal command or a one-line instruction. `error` says
   what went wrong; `remedy` says what to do. This is a top-level field
   (§3.7), not prose buried in a warning.
2. **Never report success when data may have been lost**, and never report
   failure when the data landed. A stale search index after a successful write
   is a warning with a remedy, not a failed ingest.
3. **Long operations must be observable while they run**, not only when they
   finish.
4. **The user thinks in documents, not tables.** `status`, `list_chunks` and
   `inspect_quality` all accept `pdf_name`; the table comment's `sources`
   answers "what's in here?" without a scan.
5. **Defaults are the safe common case.** Screenshots off in `search`, on in
   `inspect`. `wait_ready`, `verify` and `suspend_indexing` on in `create`.
   `auto_reindex` on everywhere.
6. **Nothing requires reading Snowflake documentation.** A missing
   `SNOWFLAKE.CORTEX_USER` grant, a client-side-encrypted stage, a suspended
   index — each produces a self-contained explanation.
7. **The same call twice is safe.** Client-supplied `run_id`; a handler that
   sees a `SUCCEEDED` run refuses to redo it and returns the original result.

---

## 3. Architecture

### 3.1 Three procedures, one per workflow stage

| Stage | Procedure | Owns |
|---|---|---|
| **Ingest** | `CHUNKY_INGEST` | PDF on a stage → chunk table. Layout / Vision / hybrid repair, surgical page mapping, screenshots, grants, table comment, auto-reindex, time-travel revert. |
| **QA** | `CHUNKY_QA` | Review and repair of chunks. Search, inspect, AI redraft, commit, delete, **sign-off**. |
| **Deploy** | `CHUNKY_DEPLOY` | Cortex Search Service lifecycle: create → wait ready → verify serving → suspend indexing. Plus on-demand `reindex`. |

All three: `(command VARCHAR, instruction VARIANT) RETURNS VARIANT`,
`EXECUTE AS CALLER`, `RUNTIME_VERSION = '3.11'`.

**`EXECUTE AS CALLER` is load-bearing.** `IT_AI` owns the procedure objects;
callers hold their own roles. Every statement — `CREATE TABLE`, `INSERT`,
`AI_COMPLETE`, `CREATE CORTEX SEARCH SERVICE` — runs with the caller's role,
warehouse and privileges. Therefore:

- No code may branch on a role name. (The prototype skips `IT_AI` in three
  places; delete all three — Appendix A, I7.)
- A privilege error is information about the caller. Return it, name the
  missing grant, do not route around it.
- Defaults that imply compute or scope come from the caller's session or the
  instruction, never from a constant.

What a **calling** role needs:

| Stage | Grants |
|---|---|
| all | `USAGE` on the procedure, its schema, its database; `USAGE` on a warehouse; `SNOWFLAKE.CORTEX_USER` |
| Ingest | `CREATE TABLE` on the target schema (or `INSERT`/`SELECT`/`UPDATE` on an existing one); `READ` on the docs stage; `READ, WRITE` on the render stage; `OWNERSHIP` or `MODIFY` on the chunk table (to write its `COMMENT` — that is the lease) |
| QA | `SELECT, UPDATE, DELETE` on the chunk table; `READ, WRITE` on the render stage |
| Deploy | `CREATE CORTEX SEARCH SERVICE` on the schema; `SELECT` on every source table; `USAGE` on the warehouse the service will use; `OWNERSHIP` or `MODIFY` on the chunk table (to write its comment) |

### 3.2 Command surface

**`CHUNKY_INGEST`** — `help`, `ingest`, `batch_ingest`, `estimate_cost`,
`list_chunks`, `list_chunks_csv`, `update_chunk`, `delete_chunks`,
`inspect_quality`, `revert`, `status`, `rebuild_comment`.

**`CHUNKY_QA`** — `help`, `search`, `inspect`, `generate_draft`, `commit`,
`delete`, `sign_off`, `revert`, `gc_renders`.

**`CHUNKY_DEPLOY`** — `help`, `create`, `wait_ready`, `verify`, `reindex`,
`suspend_indexing`, `resume_indexing`, `list`, `describe`, `alter`, `drop`,
`revert`.

`alter` covers grants and `COMMENT` only. It does **not** accept `target_lag`
(decision 8b) — the prototype's `cmd_alter` exists mainly to set it, so that
code path is deleted rather than ported.

**All three procedures also answer `help`** — see §3.9.

### 3.3 Chunk table

```sql
CREATE TABLE IF NOT EXISTS "<db>"."<schema>"."<table>" (
    CHUNK_ID        VARCHAR   NOT NULL,   -- 'CHK_' || <26-char ULID>
    PDF_NAME        VARCHAR   NOT NULL,
    PAGE_NUMBER     NUMBER    NOT NULL,
    CHUNK           VARCHAR,
    CHUNK_METADATA  VARIANT,
    PAGE_SCREENSHOT BINARY
) CHANGE_TRACKING = TRUE
  COMMENT = '<the JSON block from §3.4>';
```

`CHANGE_TRACKING = TRUE` is required by Cortex Search on every underlying
object, and it must have non-zero time-travel retention (the account default
of 1 day is fine, and is also what `revert` depends on).

#### `CHUNK_ID` — `CHK_<ULID>`

26 Crockford-base32 characters; the first 10 encode the millisecond
timestamp, so IDs sort chronologically as plain strings.

`procedure/utils/ulid.py` — complete, stdlib only:

```python
"""ULID generation. Lexicographically sortable, timestamp-prefixed ids.

Crockford base32: no I, L, O or U, so an id is unambiguous when read aloud
or transcribed. 10 chars of millisecond timestamp + 16 chars of randomness.
"""
from __future__ import annotations
import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford
_TIME_LEN = 10
_RAND_LEN = 16


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out))


def timestamp_prefix(ms: int | None = None) -> str:
    """The 10-character time component. Shared by every chunk in a batch."""
    if ms is None:
        ms = int(time.time() * 1000)
    return _encode(ms, _TIME_LEN)


def new_ulid(ms: int | None = None) -> str:
    return timestamp_prefix(ms) + _encode(
        int.from_bytes(os.urandom(10), "big"), _RAND_LEN
    )


def chunk_id(ms: int | None = None) -> str:
    return "CHK_" + new_ulid(ms)


def run_id(ms: int | None = None) -> str:
    return "RUN_" + new_ulid(ms)


def is_ulid(value: str) -> bool:
    return len(value) == _TIME_LEN + _RAND_LEN and all(c in _ALPHABET for c in value)
```

The chunk insert splits page text with `SPLIT_TEXT_RECURSIVE_CHARACTER` inside
a `LATERAL FLATTEN`, so there is no Python in that loop. Compose the ULID
across the boundary: Python supplies the timestamp prefix once per batch, SQL
supplies the randomness.

```sql
'CHK_' || '{ts_prefix}' ||
TRANSLATE(UPPER(RANDSTR(16, RANDOM())), 'ILOU', '1107') AS CHUNK_ID
```

`RANDSTR` returns `[a-zA-Z0-9]`; uppercasing plus that `TRANSLATE` maps the
four excluded letters onto canonical substitutes, so the result is always a
valid Crockford ULID. `ulid.is_ulid()` is the test assertion.

> **Verified.** The module above produces `01KZN62R7AA72EZS6767NG1R7H` (26
> chars); ids minted a second apart sort chronologically as plain strings; and
> 20,000 simulated `TRANSLATE(UPPER(RANDSTR(16,…)),'ILOU','1107')` outputs all
> pass `is_ulid()`. The 10-char timestamp component does not overflow until
> year ~37000.

#### `CHUNK_METADATA`

The single home for everything that is not one of the other five columns:

```json
{
  "chunk_type": "STANDARD",
  "chunk_ref":  "Doc Source: report.pdf | Page Num: 12",
  "link_block": "\n\n[External links:\n  - https://example.com\n]",
  "chunk_index": 3,
  "chunk_count": 7,
  "parser": {
    "strategy": "vision",
    "layout": false, "vision": true,
    "cortex_model": "claude-haiku-4-5",
    "chunk_size": 8000, "overlap": 20,
    "repaired": false,
    "screenshot_dpi": 200
  },
  "surgical": {
    "write_mode": "SURGICAL",
    "source_range": [4, 6],
    "page_mappings": [{"source": 4, "target": 4, "original_pdf_page": 1}]
  },
  "run_id": "RUN_01JZ...",
  "bundle_version": "2.0.0",
  "created_at": "2026-08-10T04:12:33Z"
}
```

`chunk_type` is one of `STANDARD` (layout), `ENHANCED` (vision or repaired),
`PLACEHOLDER` (extraction fell through — the page exists but has no content).

`link_block` is retained for provenance; the links are *also* appended to
`CHUNK` at insert time, as before. Do not append twice.

#### `PAGE_SCREENSHOT`

PNG (or JPEG) bytes of the rendered page. **Poppler runs once, at ingest.**
Everything downstream — QA inspect, `generate_draft`, hybrid repair — reads
this column instead of re-downloading the PDF and re-rendering, which is the
slowest and most fragile part of the stack.

- **200 DPI**, cap **3.5 MB** and **8000 px** on the long edge. See
  `render.py` in Phase 3.5 for the exact ladder.
- **One row per page carries it**; the other chunks of that page get `NULL`.
  A 7-chunk page would otherwise store the same image seven times.
- `instruction.store_screenshots` (default `true`) turns it off for very large
  documents.
- Write with `TO_BINARY(?, 'BASE64')`; read with `BASE64_ENCODE(...)`.
- Anything that still exceeds the cap after the ladder is stored as `NULL`
  with a warning naming the page — **never fail an insert over a screenshot.**

#### One budget, sized for the tightest consumer

```python
SCREENSHOT_DPI        = 200
SCREENSHOT_MAX_BYTES  = 3_500_000        # == CORTEX_IMAGE_MAX_BYTES
CORTEX_IMAGE_MAX_BYTES = SCREENSHOT_MAX_BYTES
CORTEX_IMAGE_MAX_EDGE  = 8000
```

The stored image has three consumers — the QA reviewer, the Vision re-parse,
and Snowflake's `BINARY` column — and the tightest of them sets the number.
Claude models cap image input at **3.75 MB** and reject anything above
**8000×8000**; Chunky's default Vision model is `claude-haiku-4-5`. Snowflake's
8 MB `BINARY` limit is not binding.

Rendering directly to the Cortex-safe size means **the stored image is always
sendable as-is**: no derivative, no second encode path, and no way to
accidentally hand `AI_COMPLETE` an oversized image — a failure that would
otherwise surface as an opaque Cortex error two phases from the cause. 200 DPI
at 3.5 MB still keeps a dense financial table legible, which is what the QA
reviewer (§2.2 ④) actually needs.

If a caller overrides `cortex_model` to a non-Claude model the ceiling rises
to 10 MB, but Chunky does not exploit that — one budget, one code path.

Cortex still needs the image on a **stage**: `AI_COMPLETE` takes a `FILE` via
`TO_FILE(stage, path)` and cannot read a BINARY column. So a Vision re-parse
is: decode the column → `/tmp` → `PUT` to `@CHUNKY_RENDER` → `TO_FILE` →
`AI_COMPLETE`. No resize step, because there is nothing to resize. That still
skips the PDF download and the poppler render, which is the expensive,
arch-fragile half.

### 3.4 Table comment — the metadata protocol

Chunky writes a JSON block into the chunk table's `COMMENT` and reads it back.
This is how a mutation knows which search services depend on it.

```json
{
  "chunky": {
    "schema_version": 2,
    "bundle_version": "2.0.0",
    "created_at": "2026-08-10T04:00:00Z",
    "created_by": "ANALYST_SVC",
    "last_modified_at": "2026-08-10T06:31:02Z",
    "last_run_id": "RUN_01JZ...",
    "sources": [
      {"pdf_name": "report.pdf", "pages": 5, "chunks": 12,
       "last_run_id": "RUN_01JZ...", "last_ingested_at": "2026-08-10T06:31:02Z"}
    ],
    "search_services": [
      {"fqn": "\"SBOX_DB\".\"AI_SB\".\"CSS_REPORTS\"",
       "created_at": "2026-08-10T05:10:00Z",
       "created_by_run_id": "RUN_01JZ...",
       "indexing": "SUSPENDED",
       "target_lag": "365 days",
       "embedding_model": "voyage-multilingual-2"}
    ],
    "qa": {"status": "SIGNED_OFF", "at": "2026-08-10T06:00:00Z",
           "by": "ANALYST_SVC", "run_id": "RUN_01JZ...", "defects": 0},
    "locks": {"ingest": null, "qa": null, "deploy": null}
  }
}
```

The comment carries two kinds of state, and the distinction matters when
reasoning about races:

| | Keys | Lifetime | If lost |
|---|---|---|---|
| **Durable** | `sources`, `search_services`, `qa`, `created_*` | until the table is dropped | auto-reindex silently stops; `rebuild_comment` regenerates it |
| **Live** | `locks` | only while a job runs | a lease leaks until its TTL expires |

`procedure/utils/table_comment.py`:

```python
"""Read/write the Chunky metadata block in a table's COMMENT.

This is the only coordination surface Chunky has -- there is no CHUNKY_RUNS
table (§3.6). It answers two questions cheaply and without a catalog scan:
"which Cortex Search Services index this table?" (so a write can refresh
them) and "is anyone else writing to it right now?" (so two jobs don't
interleave).

Every writer MERGES: read, mutate one key, write back. Never replace the
whole block -- a lease heartbeat and an ingest's `sources` update can be in
flight at the same time, and whichever writes last must not erase the other.

Losing the block degrades auto-reindex to a warning and leaks any live lease
until its TTL; it never loses chunk data. `rebuild_comment` regenerates the
durable half from a live SHOW CORTEX SEARCH SERVICES scan plus the table
itself.
"""
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional

from ._shared import qualify, clean_text_for_sql

KEY = "chunky"
SCHEMA_VERSION = 2
# Snowflake accepts long comments, but a multi-megabyte one is a smell and
# slows every SHOW TABLES. Trim `sources` oldest-first past this.
MAX_COMMENT_CHARS = 8000
MAX_SOURCES = 50


def read(session, log, db: str, schema: str, table: str) -> Dict[str, Any]:
    """Return the chunky block, or {} when absent/unparseable."""
    try:
        rows = log.execute(
            f'SELECT COMMENT AS C FROM "{db}".INFORMATION_SCHEMA.TABLES '
            f"WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            params=[schema, table],
        )
        if not rows or not rows[0]["C"]:
            return {}
        return (json.loads(rows[0]["C"]) or {}).get(KEY, {}) or {}
    except Exception:
        # A non-JSON comment (someone typed prose) is not an error.
        return {}


def write(session, log, db: str, schema: str, table: str,
          block: Dict[str, Any]) -> None:
    block["schema_version"] = SCHEMA_VERSION
    payload = _trim(json.dumps({KEY: block}, separators=(",", ":")), block)
    log.execute(
        f"COMMENT ON TABLE {qualify(db, schema, table)} IS "
        f"'{clean_text_for_sql(payload)}'"
    )


def _trim(payload: str, block: Dict[str, Any]) -> str:
    while len(payload) > MAX_COMMENT_CHARS and len(block.get("sources", [])) > 1:
        block["sources"] = sorted(
            block["sources"], key=lambda s: s.get("last_ingested_at", "")
        )[1:][:MAX_SOURCES]
        payload = json.dumps({KEY: block}, separators=(",", ":"))
    return payload


def record_ingest(session, log, db, schema, table, *, pdf_name, pages, chunks,
                  run_id, actor, now) -> Dict[str, Any]:
    block = read(session, log, db, schema, table)
    block.setdefault("created_at", now)
    block.setdefault("created_by", actor)
    block.setdefault("search_services", [])
    sources = [s for s in block.get("sources", []) if s.get("pdf_name") != pdf_name]
    sources.append({"pdf_name": pdf_name, "pages": pages, "chunks": chunks,
                    "last_run_id": run_id, "last_ingested_at": now})
    block["sources"] = sources[-MAX_SOURCES:]
    block["last_modified_at"] = now
    block["last_run_id"] = run_id
    write(session, log, db, schema, table, block)
    return block


def record_service(session, log, db, schema, table, *, service_fqn,
                   run_id, indexing, target_lag, embedding_model, now) -> None:
    block = read(session, log, db, schema, table)
    services = [s for s in block.get("search_services", [])
                if s.get("fqn") != service_fqn]
    services.append({"fqn": service_fqn, "created_at": now,
                     "created_by_run_id": run_id, "indexing": indexing,
                     "target_lag": target_lag,
                     "embedding_model": embedding_model})
    block["search_services"] = services
    block["last_modified_at"] = now
    write(session, log, db, schema, table, block)


def forget_service(session, log, db, schema, table, *, service_fqn, now) -> None:
    block = read(session, log, db, schema, table)
    block["search_services"] = [s for s in block.get("search_services", [])
                                if s.get("fqn") != service_fqn]
    block["last_modified_at"] = now
    write(session, log, db, schema, table, block)


def services_for(session, log, db, schema, table) -> List[str]:
    return [s["fqn"] for s in read(session, log, db, schema, table)
            .get("search_services", []) if s.get("fqn")]
```

`CHUNKY_INGEST('rebuild_comment', {db, schema, table})` regenerates the
durable half from a live `SHOW CORTEX SEARCH SERVICES` scan (matching any
service whose definition references this table) plus
`SELECT DISTINCT PDF_NAME, COUNT(*)` off the table itself — for when the
comment is lost, hand-edited, or a service was created outside Chunky. It also
clears any lease, which makes it the blunt recovery tool when a slot is stuck
and nobody wants to wait out the TTL.

### 3.5 Index lifecycle — suspended by default, refreshed on demand

The default Cortex Search behaviour is a background job that re-checks the
source on a `TARGET_LAG` schedule and burns credits doing so. Chunky's corpus
changes only when Chunky changes it, so that schedule is pure waste.

```
create service (INITIALIZE = ON_CREATE)
        │
        ▼
   wait_ready ──▶ verify (SEARCH_PREVIEW returns hits)
        │
        ▼
ALTER CORTEX SEARCH SERVICE <fqn> SUSPEND INDEXING     ← scheduled refresh off
        │                                                serving stays ON
        │
        │   … later: CHUNKY_INGEST('ingest') writes new rows …
        ▼
   read table COMMENT → search_services[]
        │
        ▼
ALTER … RESUME INDEXING  →  ALTER … REFRESH  →  poll DESCRIBE  →  SUSPEND INDEXING
```

`SUSPEND INDEXING` and `SUSPEND SERVING` are independent — suspending indexing
leaves the service fully queryable against the index it already built.

`TARGET_LAG` is syntactically required by the DDL, so Chunky emits a fixed
`constants.TARGET_LAG = "365 days"` and **never exposes it** (decision 8b).
It is not an instruction field on `create`, and `alter` rejects it. With
indexing suspended the value is inert; a short lag would do nothing until a
future `RESUME INDEXING` turned it into an expensive surprise. Refresh in this
system is explicit, triggered by a write, and immediately followed by
re-suspension — a schedule would only ever duplicate work Chunky already does
at the right moment.

`REFRESH` on a service whose indexing is suspended is not guaranteed to
proceed. The reindex helper therefore always resumes first, and restores the
previous state afterwards:

```python
# procedure/utils/reindex.py
def reindex_service(session, log, service_fqn: str, *, wait: bool = True,
                    timeout_seconds: int = 900, poll_seconds: int = 10,
                    restore_suspended: bool = True) -> dict:
    """RESUME -> REFRESH -> wait for indexing to settle -> SUSPEND.

    Never raises. A failed reindex must not fail the ingest that triggered it:
    the rows are already committed, and the caller needs to be told which
    service is stale and how to fix it -- not to have a successful write
    reported as a failure.
    """
    out = {"service": service_fqn, "status": "unknown", "error": None,
           "was_suspended": None, "duration_seconds": None}
    t0 = _now(session, log)
    try:
        out["was_suspended"] = _indexing_state(session, log, service_fqn) == "SUSPENDED"
        if out["was_suspended"]:
            log.execute(f"ALTER CORTEX SEARCH SERVICE {service_fqn} RESUME INDEXING")
        log.execute(f"ALTER CORTEX SEARCH SERVICE {service_fqn} REFRESH")
        if wait:
            ok, state, err = _wait_indexing(session, log, service_fqn,
                                            timeout_seconds, poll_seconds)
            out["indexing_state"] = state
            if not ok:
                out["status"] = "timeout" if not err else "failed"
                out["error"] = err or f"still {state} after {timeout_seconds}s"
                return out
        if restore_suspended and out["was_suspended"]:
            log.execute(f"ALTER CORTEX SEARCH SERVICE {service_fqn} SUSPEND INDEXING")
        out["status"] = "refreshed"
    except Exception as exc:
        out["status"] = "failed"
        out["error"] = str(exc)
    finally:
        out["duration_seconds"] = _elapsed(session, log, t0)
    return out
```

Every mutating command in `CHUNKY_INGEST` and `CHUNKY_QA` ends with:

```python
reindex_results = []
if inst.get("auto_reindex", True) and result_ok:
    for fqn in table_comment.services_for(session, log, db, schema, table):
        reindex_results.append(reindex.reindex_service(
            session, log, fqn,
            wait=inst.get("reindex_wait", True),
            timeout_seconds=int(inst.get("reindex_timeout_seconds", 900)),
        ))
for r in reindex_results:
    if r["status"] != "refreshed":
        warnings.append(
            f"Search service {r['service']} was not reindexed ({r['status']}: "
            f"{r['error']}). Its results are stale. Re-run: "
            f"CALL CHUNKY_DEPLOY('reindex', OBJECT_CONSTRUCT("
            f"'db','{db}','schema','{schema}','table','{table}'));"
        )
```

and reports `data.reindex = reindex_results`.

**Never let a reindex failure fail the write.** The rows are committed; the
caller needs to know a service is stale, not to be told their ingest failed.

### 3.6 Concurrency: advisory leases in the table comment

**There is no `CHUNKY_RUNS` table. There is no control schema, no run history,
no idempotency ledger, no hard QA gate.** Everything that needs coordinating
lives in the table's own `COMMENT`, which Chunky already reads and writes for
the search-service list (§3.4). One mechanism, no new objects.

What this actually has to prevent is narrow and worth naming precisely: **two
writers mangling the same chunk table at once.** Two concurrent ingests into
one table interleave page numbers, and an `OVERWRITE` under a running Vision
pass destroys rows the other job is still inserting. That is the real damage.
Everything else the registry was carrying — history, idempotency, a gate —
either has a cheaper answer or is not worth an object.

#### The `locks` block

```json
"locks": {
  "ingest": {
    "holder": "ANALYST_SVC", "role": "ANALYST",
    "token": "01KZND…",                       // ULID, this attempt's identity
    "run_id": "RUN_01KZND…",
    "since": "2026-08-10T06:20:00Z",
    "expires_at": "2026-08-10T06:50:00Z",
    "detail": "report.pdf pages 1-40, vision",
    "progress": {"phase": "vision", "pages_done": 17, "pages_total": 40,
                 "eta_seconds": 184, "updated_at": "2026-08-10T06:23:41Z"}
  },
  "qa": null,
  "deploy": null
}
```

`procedure/utils/locks.py`:

```python
def acquire(session, log, db, schema, table, slot, *, holder, run_id,
            detail="", ttl_seconds=1800, force=False) -> tuple[bool, dict]:
    """Take an advisory lease on (table, slot). Returns (acquired, info).

    Read-modify-write on a COMMENT is not a compare-and-swap -- Snowflake has
    no conditional DDL -- so this is deliberately an *advisory* lease, not a
    mutex. See "Why advisory is enough" below for why that is the right call
    here rather than a compromise.
    """
    block = table_comment.read(session, log, db, schema, table)
    current = (block.get("locks") or {}).get(slot)
    now = _now(session, log)

    if current and not force and not _expired(current, now):
        return False, current                      # someone else is working

    token = ulid.new_ulid()
    _write_slot(session, log, db, schema, table, slot, {
        "holder": holder, "role": _role(session, log), "token": token,
        "run_id": run_id, "since": now, "detail": detail,
        "expires_at": _plus(now, ttl_seconds), "progress": None,
    })

    # Verify-after-write. Two callers that both read "free" will both write;
    # the later write wins, and the earlier writer sees a token that is not
    # its own and stands down. The jitter widens the gap between the two
    # writes so the common case resolves cleanly.
    _sleep(random.uniform(0.8, 2.5))
    winner = (table_comment.read(session, log, db, schema, table)
              .get("locks") or {}).get(slot) or {}
    if winner.get("token") != token:
        return False, winner
    return True, winner


def heartbeat(session, log, db, schema, table, slot, token, progress,
              min_interval_seconds=30) -> None:
    """Refresh progress + extend expires_at. Throttled, and never raises."""


def release(session, log, db, schema, table, slot, token) -> None:
    """Clear the slot if we still hold it. Runs in a `finally`, never raises.

    Merges rather than replaces -- `sources` and `search_services` may have
    been written during the run and must survive.
    """
```

#### Conflict matrix

| Command | Takes | Blocked by |
|---|---|---|
| `ingest`, `batch_ingest` | `ingest` | `ingest`, `qa` |
| `update_chunk`, `delete_chunks` | `ingest` | `ingest`, `qa` |
| QA `commit`, `delete` | `qa` | `ingest`, `qa` |
| `CHUNKY_DEPLOY('create')`, `reindex` | `deploy` | `deploy`; **warns** on `ingest` |
| everything else — `search`, `inspect`, `generate_draft`, `list_chunks`, `inspect_quality`, `estimate_cost`, `status`, `help`, `revert` | nothing | never blocked |

Reads are never blocked and never lock. `generate_draft` calls Cortex but
writes nothing, so it stays free — a reviewer can keep working while an ingest
runs, they just can't `commit` until it finishes.

`revert` deliberately takes no lock: it is the thing you reach for when
something has gone wrong, and a recovery tool that can be blocked by the
wreckage it is meant to clean up is worse than useless.

#### Why advisory is enough

The honest limitation: between one caller's read and its verify, another
caller can slip through. Jitter narrows that window to a second or two against
operations that run for minutes.

What makes this acceptable is the **failure mode**: when the lease fails to
arbitrate, both callers proceed — which is exactly what happens today, with no
lease at all. The lease can only ever improve on the status quo; it cannot
introduce a failure the current design doesn't already have. That is a very
different risk profile from a lock that can deadlock, leak, or wrongly block.

Being wrong in the other direction is the dangerous one, so the design leans
against false blocking: leases expire (`ttl_seconds`, default 30 min, extended
by each heartbeat), `force: true` breaks any lease, and the blocked-caller
error names exactly how.

If strictness is ever needed, there is a one-line upgrade that *is* atomic:
`CREATE TABLE <table>__CHUNKY_LOCK (X INT)` **without** `IF NOT EXISTS`.
Snowflake serialises object creation, so exactly one concurrent caller
succeeds and the rest get "already exists" — a real test-and-set. Release is
`DROP TABLE`. It costs a transient object per active job, which is why it is
documented here rather than built now.

#### What a blocked caller sees

```json
{
  "success": false,
  "error": "Table SBOX_DB.AI_SB.CHUNKS_REPORTS is being ingested by ANALYST_SVC (started 2026-08-10T06:20:00Z, 4m ago, 17/40 pages done, ~3m remaining).",
  "remedy": "Wait for it to finish — poll CALL CHUNKY_INGEST('status', OBJECT_CONSTRUCT('db','SBOX_DB','schema','AI_SB','table','CHUNKS_REPORTS')). If that job is dead, its lease expires at 2026-08-10T06:50:00Z, or override now with 'force': true.",
  "data": {"lock": { …the whole lock block… }}
}
```

Live progress in a *rejection* is not decoration: it turns "try again later"
into "try again in three minutes", which is the difference between a caller
that polls sensibly and one that hammers.

#### Stale leases

A procedure that dies mid-run leaves its slot set. Three defences, in order of
preference: the TTL expires it; a heartbeat that stops updating makes staleness
visible in `status`; `force: true` breaks it immediately. Release runs in a
`finally` so the ordinary failure path — an exception during ingest — always
clears it.

#### What we gave up, and what replaces it

| Lost with `CHUNKY_RUNS` | Replacement |
|---|---|
| Run history / "what happened last Tuesday" | `QUERY_HISTORY` filtered on `QUERY_TAG`, which already carries `Chunky/<version>\|user=…\|cmd=…\|run=<run_id>`. Free, and it survives table drops. |
| Actual cost per run | `SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY` joined on the same query tag. |
| Live progress | Kept — it lives in the lease (`locks.ingest.progress`) for as long as the run is live, which is exactly as long as anyone needs it. |
| "What's in this table?" | Kept — the comment's `sources` array. |
| Idempotent retry on `run_id` | Dropped, accepted. `run_id` remains a correlation id for `QUERY_TAG` and progress. |
| Hard QA sign-off gate | Softened to a warning — see Phase 4.4. `sign_off` writes `qa` into the comment; `CHUNKY_DEPLOY('create')` warns when it is missing or older than the last ingest, and proceeds. |

Progress surviving only while the run is live is the right trade: it is
operational state, not history, and nobody polls a run that finished.

### 3.7 Response envelope

One shape, built by one function. `procedure/utils/_shared.py`:

```python
def ok(command, data=None, *, log=None, warnings=None, revert=None,
       run_id=None, remedy=None, next_steps=None, extra=None) -> dict:
    """`next_steps` is a list of {why, call} with the caller's own values
    already substituted -- see §2.4. A headless caller has no UI to tell them
    the workflow continues, so the response is the only place that can."""
    return _envelope(True, command, data, None, log, warnings, revert,
                     run_id, remedy, next_steps, extra)


def err(command, error, *, remedy=None, data=None, log=None, warnings=None,
        run_id=None, next_steps=None, extra=None) -> dict:
    """`error` says what went wrong; `remedy` says what to do about it.

    `remedy` is a literal command or a one-line instruction, and it is not
    optional in spirit -- an agent (§2.1, consumer D) cannot infer a fix from
    a Snowflake error code, and will either retry the identical call or invent
    one. Every raise site should be able to answer "and then what?".
    """
    return _envelope(False, command, data, str(error), log, warnings, None,
                     run_id, remedy, next_steps, extra)


def _envelope(success, command, data, error, log, warnings, revert,
              run_id, remedy, next_steps, extra) -> dict:
    from . import __version__
    warnings = list(warnings or [])
    out = {
        "success": success,
        "command": command,
        "run_id": run_id,
        "data": data,
        "error": error,
        "remedy": remedy,
        "next": next_steps or [],
        "warning": " | ".join(warnings) if warnings else None,
        "warnings": warnings,
        "revert": revert,
        "bundle_version": __version__,
        "query_ids": [], "timestamp_before": None,
        "timestamp_after": None, "query_count": 0,
    }
    if log is not None:
        out.update(log.to_dict())
    if extra:
        out.update(extra)
    return out
```

Worked example of the difference, for the most common first-run failure:

```json
{
  "success": false,
  "error": "PDF not found on stage: '@SBOX_DB.AI_SB.DOCS/chunky_fixtures/report.pdf'",
  "remedy": "Check the file is staged: LIST @SBOX_DB.AI_SB.DOCS PATTERN='.*report.*'; then PUT it with `sf put <local.pdf> \"@SBOX_DB.AI_SB.DOCS/chunky_fixtures/\"`. Chunky does not upload files — 'stage_path' and 'file' are joined as '<stage_path>/<file>'.",
  "data": {"stage_path": "@SBOX_DB.AI_SB.DOCS", "file": "chunky_fixtures/report.pdf"}
}
```

A T1 test asserts every command's response has an identical key set. That test
is what stops the envelope drifting, which it already has in the prototype
(`list_chunks_csv` drops `query_count`; `batch_ingest` omits the log fields
entirely).

### 3.8 API contract

Transport is the **Snowflake SQL API v2**. Two facts drive the client design:

1. **Statements over ~45 s return HTTP 202** with a `statementHandle`; the
   client polls `GET /api/v2/statements/{handle}` (200 done, 202 running,
   422 failed). Vision ingest is ~8–12 s per page, serially — so every ingest
   is an async call. Do not design around a synchronous response.
2. **The procedure's VARIANT arrives as a JSON *string*** in the first cell.
   The client must `JSON.parse` it.

```http
POST /api/v2/statements?async=true
Authorization: Bearer <jwt>
X-Snowflake-Authorization-Token-Type: KEYPAIR_JWT
Content-Type: application/json

{
  "statement": "CALL CHUNKY_INGEST(?, PARSE_JSON(?))",
  "timeout": 3600,
  "database": "SBOX_DB", "schema": "AI_SB",
  "warehouse": "SHARED_ID_XS", "role": "ANALYST_SVC",
  "bindings": {
    "1": {"type": "TEXT", "value": "ingest"},
    "2": {"type": "TEXT", "value": "{\"db\":\"SBOX_DB\",\"schema\":\"AI_SB\",...}"}
  }
}
```

The instruction is always a **TEXT bind into `PARSE_JSON(?)`** — never
concatenated into SQL.

Because a 202 hides the run_id, the client **generates the `run_id` and passes
it in** (`instruction.run_id`). Handlers use it when present. This also makes
calls traceable: it lands in `QUERY_TAG` and in the lease's `progress`, so a
client can correlate a 202 handle with the work actually running. It is **not**
an idempotency key — there is no ledger to check it against (decision 16). A
retried ingest that arrives while the first is still running is refused by the
lease, which is the protection that actually matters.

`procedure/deploy/sfapi.py` already implements this, including key-pair JWT.

### 3.9 `help` — the procedures document themselves

The signature stays exactly `(COMMAND VARCHAR, INSTRUCTION VARIANT)`. No
extra parameters, ever: one shape for every call is what makes a generic
client, a generic agent tool definition, and a generic `sf call` wrapper
possible. `help` is a command like any other.

```sql
CALL CHUNKY_INGEST('help');                                    -- overview + command list
CALL CHUNKY_INGEST('help', OBJECT_CONSTRUCT('command','ingest'));   -- one command, in full
CALL CHUNKY_INGEST('help', OBJECT_CONSTRUCT('topic','troubleshooting'));
CALL CHUNKY_INGEST('help', OBJECT_CONSTRUCT('topic','workflow'));
```

This is the answer to §2.4's worst finding: an unrecognised command currently
dead-ends, and there is nothing to call to find out what is valid. Now there
is, and it is discoverable from the failure itself.

#### Generated from a registry, never hand-written

Hand-written help goes stale within two sprints and then actively misleads.
So each handler declares its commands **once**, in a registry that the
dispatcher, the input validator, `help`, and the `API.md` generator all read:

```python
# procedure/utils/chunky_ingest_handler.py
COMMANDS = {
  "ingest": {
    "summary": "Extract a staged PDF into chunk rows, then reindex any "
               "search services that depend on the table.",
    "detail":  "Runs init -> optional surgical delete -> Layout and/or Vision "
               "extraction -> hybrid repair -> screenshots -> grants -> table "
               "comment -> auto-reindex. Long-running: ~8-12s per page for "
               "Vision. Call it asynchronously and poll `status`.",
    "fields": {
      "db":         {"type": "string", "required": True,
                     "desc": "Target database. Always required — Chunky never guesses."},
      "schema":     {"type": "string", "required": True},
      "table":      {"type": "string", "required": True,
                     "desc": "Created if absent, with the schema in §3.3."},
      "stage_path": {"type": "string", "required": True,
                     "example": "@SBOX_DB.AI_SB.DOCS",
                     "desc": "Stage holding the PDF. Must be server-side "
                             "encrypted with a directory table."},
      "file":       {"type": "string", "required": True,
                     "example": "chunky_fixtures/report.pdf",
                     "desc": "Path within stage_path. Joined as "
                             "'<stage_path>/<file>'. Chunky does not upload."},
      "mode":       {"type": "enum", "values": ["APPEND", "OVERWRITE", "SURGICAL"],
                     "default": "APPEND"},
      "range":      {"type": "array[int,int]", "default": None,
                     "desc": "1-based inclusive page range. Omit for the whole PDF."},
      "vision":     {"type": "bool", "default": True},
      "layout":     {"type": "bool", "default": False,
                     "desc": "Both true = Layout first, then Vision repairs "
                             "chunks the quality inspector flags."},
      "run_id":     {"type": "string", "default": "generated",
                     "desc": "Supply your own to correlate the SQL API "
                             "statement handle with QUERY_HISTORY and with "
                             "`status` progress while it runs."},
      # … store_screenshots, auto_reindex, grant_roles, cortex_model, …
    },
    "returns": ["data.metrics", "data.page_coverage", "data.grant_result",
                "data.reindex", "revert", "next"],
    "example": { … a complete, runnable instruction … },
    "errors": [
      {"when": "the PDF is not on the stage",
       "remedy": "LIST @<stage> PATTERN='.*<name>.*'; Chunky does not upload — "
                 "stage the file first."},
      {"when": "SNOWFLAKE.CORTEX_USER is not granted to your role",
       "remedy": "GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE <your_role>;"},
      {"when": "poppler is missing for the runtime architecture",
       "remedy": "Rebuild and re-upload the bundle, or set "
                 "vision:false, layout:true to use Layout-only."},
    ],
  },
  # … one entry per command …
}
```

What the registry buys, all from one declaration:

1. **Dispatch** — `COMMANDS.keys()` *is* the command list. A command cannot
   exist without documentation, and documentation cannot describe a command
   that does not exist.
2. **Input validation** — required fields checked, defaults applied, enums
   enforced, and **unknown fields rejected with a near-match suggestion**
   (`"Unknown field 'tabel'. Did you mean 'table'?"`). Today a typo'd optional
   field is silently ignored, and the caller wonders why `vision` had no
   effect.
3. **`help`** — rendered from the same dict, so it cannot drift.
4. **Unknown-command recovery** — the dispatcher's fallback lists valid
   commands, offers the closest match by edit distance, and names `help`.
5. **`API.md`** — generated by a build script from the registries, not
   maintained by hand (Phase 6.3).
6. **Agent tool definitions** — a client can call `help` once at startup and
   build its own tool schema. That is what makes Chunky usable by consumer D
   without anyone writing a bespoke integration.

#### What `help` returns

```json
{
  "success": true, "command": "help",
  "data": {
    "procedure": "CHUNKY_INGEST",
    "bundle_version": "2.0.0",
    "purpose": "Stage 1 of 3. Turns a staged PDF into chunk rows.",
    "workflow": "CHUNKY_INGEST('ingest') -> CHUNKY_QA('sign_off') -> CHUNKY_DEPLOY('create'). After the first deploy, re-ingesting reindexes automatically. One ingest at a time per table; poll status to see who holds it.",
    "signature": "CALL CHUNKY_INGEST(<command> VARCHAR, <instruction> VARIANT)",
    "always_required": ["db", "schema"],
    "commands": [
      {"command": "ingest", "summary": "…", "required": ["db","schema","table","stage_path","file"]},
      …
    ],
    "see_also": ["CALL CHUNKY_INGEST('help', {'command':'ingest'})",
                 "CALL CHUNKY_QA('help')", "CALL CHUNKY_DEPLOY('help')"]
  }
}
```

With `{command: "ingest"}` the payload is the full field table, the runnable
example, the `returns` list and the `errors` table. With
`{topic: "troubleshooting"}` it is the failure→remedy table for the whole
procedure. With `{topic: "workflow"}` it is the three-call story from §2.3,
including that `db`/`schema` are always required and that ingest is
long-running and should be called asynchronously.

`help` reads nothing and writes nothing — no warehouse work beyond the
statement itself, so it is free to call and safe to call first.

---

## 4. Environment

Verified live from this machine (✅ rows).

| Item | Value |
|---|---|
| Account | `ab14212.ap-southeast-1` (`AB14212`, `AWS_AP_SOUTHEAST_1`) ✅ |
| Deploy user / role | `MCDAAI_DNA` / `IT_AI` ✅ — owner of the procedures, *not* the caller identity |
| Target | **`SBOX_DB.AI_SB`** ✅ |
| Library stage | `@SBOX_DB.AI_SB.CHUNKY_UTILS` ✅ exists, `INTERNAL NO CSE`, `DIRECTORY = Y`, **empty** |
| Docs stage | `@SBOX_DB.AI_SB.DOCS` ✅ exists, `INTERNAL NO CSE`, `DIRECTORY = Y`, holds real PDFs |
| Render stage | `@SBOX_DB.AI_SB.CHUNKY_RENDER` — create in Phase 0 |
| Warehouse | `SHARED_ID_XS` ✅ |
| Embedding model | `voyage-multilingual-2` — available |

`INTERNAL NO CSE` means no client-side encryption, i.e. server-side encrypted
— which is what `GET_PRESIGNED_URL`, `TO_FILE()`, `AI_PARSE_DOCUMENT` and
`AI_COMPLETE`-with-image all require. `DIRECTORY = (ENABLE = TRUE)` is
required by `AI_PARSE_DOCUMENT`. Both existing stages already satisfy this;
create the render stage to match:

```sql
CREATE STAGE IF NOT EXISTS SBOX_DB.AI_SB.CHUNKY_RENDER
    DIRECTORY = (ENABLE = TRUE) ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');
```

> `@SBOX_DB.AI_SB.DOCS` already contains `docs/_temp_images/<pdf>/pN.png` and
> `docs/_temp_audit/audit_CHK_*.png` — the retiring Streamlit app writing page
> renders into the data stage. Phase 0 sweeps them; the headless version never
> writes there.

Grants the **deploy** role needs:

```sql
GRANT USAGE                        ON DATABASE SBOX_DB                  TO ROLE IT_AI;
GRANT USAGE, CREATE TABLE, CREATE PROCEDURE, CREATE STAGE,
      CREATE CORTEX SEARCH SERVICE ON SCHEMA SBOX_DB.AI_SB              TO ROLE IT_AI;
GRANT READ, WRITE                  ON STAGE SBOX_DB.AI_SB.CHUNKY_UTILS  TO ROLE IT_AI;
GRANT READ, WRITE                  ON STAGE SBOX_DB.AI_SB.DOCS          TO ROLE IT_AI;
GRANT READ, WRITE                  ON STAGE SBOX_DB.AI_SB.CHUNKY_RENDER TO ROLE IT_AI;
GRANT USAGE                        ON WAREHOUSE SHARED_ID_XS            TO ROLE IT_AI;
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER                               TO ROLE IT_AI;
```

### Development machine

| Fact | Consequence |
|---|---|
| Windows 11, PowerShell | shell examples are PowerShell |
| `.venv-proc` ✅ (Python 3.11.9, `snowflake-connector-python[secure-local-storage]` 4.7.2, `pytest`, `pypdf`) | ready |
| **No WSL, no Docker** | the prototype's build scripts (`ldd`, `dpkg-deb`, `readelf`, `file`) cannot run — Phase 1 replaces them with pure Python |
| Outbound HTTPS to `pypi.org` and `deb.debian.org` ✅ | the pure-Python builder is viable |

### The `sf` CLI — built and verified

`procedure/deploy/` exists and works: snowball's auth mechanism with the
read-only guard removed.

| File | Purpose |
|---|---|
| `winkeyring.py` | chunked Windows Credential Manager backend (verbatim from snowball). Windows caps a credential blob at 2560 bytes; OAuth tokens exceed it, so without this the connector cannot cache tokens at all. |
| `auth.py` | `OAUTH_AUTHORIZATION_CODE` connect, retry that absorbs `250002 (08003)`, identity-mismatch detection, token-cache introspection, `oauth_access_token()` |
| `config.py` | flag → env → `config.json` → `~/.snowball/config.json` → default |
| `sqlsplit.py` | `$$`-aware statement splitter |
| `sfapi.py` | SQL API v2: submit, 202→poll, VARIANT unwrap; OAuth / PAT / key-pair JWT |
| `sf.py` | `auth`, `config`, `sql`, `script`, `call`, `put`, `get`, `ls`, `api`, `api-call`, `api-status` |

```
$ python procedure/deploy/sf.py auth whoami
  user : MCDAAI_DNA   role : IT_AI   warehouse : SHARED_ID_XS
  database : SBOX_DB  schema : AI_SB  region : AWS_AP_SOUTHEAST_1

$ python procedure/deploy/sf.py api "SELECT CURRENT_ROLE() AS R"
  R
  IT_AI
```

Both silent — no browser — reusing the refresh token already cached. The
second proves the SQL API path works today, so the API tier is testable from
day one.

---

## 5. Repository layout

Target state. `procedure/` is self-contained; nothing imports from the
Streamlit app.

```
procedure/
├── plan.md                    (this document)
├── API.md                     (Phase 6 — the consumer-facing contract)
├── ARCHITECTURE.md            (Phase 7 — rewritten against shipped code)
├── README.md                  (Phase 7 — operator quick-start)
├── CHANGELOG.md               (Phase 7)
├── requirements.txt
├── dev.ps1                    (the iteration loop)
│
├── build/
│   ├── build_bundle.py        pure-Python bundle assembly
│   ├── debfetch.py            Debian index + .deb (ar) unpack, stdlib only
│   ├── elfdeps.py             ELF DT_NEEDED walker + arch assertion
│   ├── render_sql.py          {{VAR}} substitution, no jinja2
│   └── out/                   (git-ignored) bundle + rendered .sql
│
├── deploy/                    ✅ built
│   ├── winkeyring.py  auth.py  config.py  sqlsplit.py  sfapi.py  sf.py
│   ├── config.example.json    config.json (git-ignored)
│   ├── bootstrap.sql          render stage only    (Phase 1)
│   ├── preflight.py           environment checks    (Phase 0)
│   ├── deploy.py              PUT + CREATE + verify (Phase 1)
│   ├── smoke_test.py          T2/T3 live tests      (Phase 3+)
│   ├── api_smoke.py           T4 over the SQL API   (Phase 6)
│   └── jobs/                  instruction JSON fixtures
│
├── templates/
│   ├── chunky_ingest.sql.j2  chunky_qa.sql.j2  chunky_deploy.sql.j2
│
├── utils/                     → zipped as chunky_utils/
│   ├── __init__.py            __version__
│   ├── constants.py  _shared.py  ulid.py  query_log.py
│   ├── registry.py            (new — dispatch, validation, help)
│   ├── locks.py               (new — advisory leases in the comment)
│   ├── table_comment.py       (new)
│   ├── reindex.py             (new)
│   ├── render.py              (new — one renderer, screenshot column I/O)
│   ├── page_mapping.py  metadata_handler.py  layout_parse.py
│   ├── quality_inspector.py  hybrid_repair.py  prompts.py
│   ├── poppler_bootstrap.py  revert.py  grant_table.py
│   ├── chunky_ingest_handler.py
│   ├── chunky_qa_handler.py
│   └── chunky_deploy_handler.py
│
├── tests/                     conftest.py  test_units.py  test_handlers.py
│                              test_sql_shape.py  test_bundle.py
└── script/
    ├── make_dummy_pdf.py
    └── pdf/fy2024-tbk-investor-presentation.pdf
```

Deleted: `init_table.py`, `surgical_delete.py`, `parse_pdf.py`,
`build_chunk_ref.py` (imported by nothing), `build_poppler_bundle.sh`,
`build_arm_poppler.py`, the generated `chunky_*.sql` in `procedure/`,
`utils/README.md`, `utils_bundle.zip` (untrack it), `script/upload_to_stage.py`
(superseded by `sf put`), `snowflake-mcp/` (out of scope — leave it, don't
extend it).

---

## Phase 0 — Bring-up

**Deliverable:** a verified environment and a written record of what it is.

### 0.1 Environment ✅

`.venv-proc` and `procedure/deploy/` exist and are verified (§4).

Rewrite `procedure/requirements.txt` — it omits `keyring` and asks for
`snowflake-connector-python>=3.0.6`, which predates the OAuth
authorization-code flow:

```
snowflake-connector-python[secure-local-storage]>=3.16
pytest>=7.0
pypdf>=3.17
reportlab>=4.0     # only to regenerate the fixture PDF
```

### 0.2 `preflight.py`

Connects, runs every check, prints a pass/fail table, and **does not stop at
the first failure**:

| # | Check | Failure means |
|---|---|---|
| 1 | `CURRENT_USER/ROLE/WAREHOUSE/ACCOUNT/REGION` | config wrong |
| 2 | `SHOW DATABASES LIKE 'SBOX_DB'`, `SHOW SCHEMAS LIKE 'AI_SB'` | wrong target |
| 3 | **Write probe** — `CREATE OR REPLACE TABLE _CHUNKY_PREFLIGHT (X INT)`, insert, select, drop | not actually writable |
| 4 | `SHOW STAGES` — `CHUNKY_UTILS`, `DOCS` present and `NO CSE` | stage encryption wrong |
| 5 | Create `CHUNKY_RENDER` | missing `CREATE STAGE` |
| 6 | `--sweep`: `REMOVE @DOCS/docs/_temp_images/`, `@DOCS/docs/_temp_audit/` (dry-run by default) | — |
| 7 | `SELECT SNOWFLAKE.CORTEX.AI_COMPLETE('claude-haiku-4-5','reply OK')` | `SNOWFLAKE.CORTEX_USER` not granted |
| 8 | `SHOW GRANTS TO ROLE IT_AI` — look for `CREATE CORTEX SEARCH SERVICE` | can't deploy |
| 9 | **Cortex Search round trip** — one-row table → `CREATE CORTEX SEARCH SERVICE` with `EMBEDDING_MODEL='voyage-multilingual-2'` and `WAREHOUSE = CURRENT_WAREHOUSE()` → `SUSPEND INDEXING` → `REFRESH` → `DROP`. Proves the Phase 5.1 DDL, the caller-warehouse rule, and the whole index-lifecycle model before a line of handler code depends on them. | the Deploy stage design is wrong — stop and fix here |
| 10 | **Architecture probe** — throwaway Python UDF returning `platform.machine()`, called ~12×. Record which arches appear. | informs bundle size only |

### 0.3 Stage the fixture

The 5-page fixture is small and predictable; the PDFs already on `DOCS` are
7–38 MB and too slow for the inner loop.

```powershell
python procedure\deploy\sf.py put `
  procedure\script\pdf\fy2024-tbk-investor-presentation.pdf `
  "@SBOX_DB.AI_SB.DOCS/chunky_fixtures/"
```

**Exit:** preflight all-green (check 9 especially), `CHUNKY_RENDER` created,
fixture staged, arch probe recorded.

---

## Phase 1 — Build & deploy toolchain

**Deliverable:** one command takes an edit in `procedure/utils/*.py` to a live
updated procedure in `SBOX_DB.AI_SB`, on Windows, with no external tools.
Nothing after this phase is verifiable without it.

### 1.1 `build/debfetch.py` — .deb unpacking in pure Python

Replaces `dpkg-deb`. A `.deb` is an `ar` archive: 8-byte magic `!<arch>\n`,
then 60-byte headers — `name[16] mtime[12] uid[6] gid[6] mode[8] size[10]
magic[2]` — each followed by `size` bytes padded to an even offset.

```python
AR_MAGIC = b"!<arch>\n"

def ar_members(blob: bytes):
    """Yield (name, payload) for each member of an `ar` archive."""
    if blob[:8] != AR_MAGIC:
        raise ValueError("not an ar archive")
    off = 8
    while off + 60 <= len(blob):
        header = blob[off:off + 60]
        name = header[0:16].decode("ascii", "replace").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        start = off + 60
        yield name, blob[start:start + size]
        off = start + size + (size % 2)          # members are 2-byte aligned


def extract_data_tar(deb_bytes: bytes, dest: Path) -> None:
    for name, payload in ar_members(deb_bytes):
        if not name.startswith("data.tar"):
            continue
        mode = {"data.tar.xz": "r:xz", "data.tar.gz": "r:gz",
                "data.tar": "r:"}.get(name)
        if mode is None:
            raise RuntimeError(f"unsupported compression: {name} "
                               f"(pin an older package version)")
        with tarfile.open(fileobj=io.BytesIO(payload), mode=mode) as tf:
            tf.extractall(dest, filter="data")   # filter= blocks path traversal
        return
    raise RuntimeError("no data.tar.* member in .deb")
```

`gzip`, `lzma` and `tarfile` are stdlib. If a package ships `data.tar.zst`,
pin an older version rather than adding a dependency.

> **Verified on this Windows machine, no external tools.** Running the parser
> above against the live Debian mirror:
>
> ```
> deb: pool/main/p/poppler/poppler-utils_22.12.0-2+deb12u2_arm64.deb  (176,644 bytes)
> ar members: [('debian-binary', 4), ('control.tar.xz', 1680), ('data.tar.xz', 174772)]
> data member: data.tar.xz  ->  36 entries
> poppler bins: ./usr/bin/pdfinfo, ./usr/bin/pdftoppm, ./usr/bin/pdftotext
> pdftoppm: ELF magic ok, e_machine 0xB7 -> arm64
> ```
>
> So B1 is not a hope — the download, the `ar` walk, the xz/tar unpack and the
> cross-arch ELF detection all work here today. Phase 1 is assembly, not
> research.

The Debian index download, `Packages.gz` parsing and breadth-first `Depends`
resolution already exist in `build_arm_poppler.py` and are correct — lift them
verbatim. Change only the arch: fetch **both** `arm64` and `amd64` from
Debian, so the bundle no longer depends on what happens to be installed on the
build host.

### 1.2 `build/elfdeps.py` — DT_NEEDED without `readelf`

```python
"""Parse ELF headers to find shared-library dependencies and the target arch.

`readelf` worked cross-arch because it only *parses* the file. So does this,
in ~80 lines of struct, and it runs on Windows.
"""
EM = {0x3E: "x86_64", 0xB7: "arm64"}

def elf_arch(path) -> str | None:
    """'x86_64' | 'arm64' | None. Used to ASSERT every bundled binary is the
    arch its directory claims -- a mis-arch'd pdftoppm fails at runtime with
    empty output and no error message, which is the worst kind of bug."""
    with open(path, "rb") as f:
        head = f.read(20)
    if head[:4] != b"\x7fELF":
        return None
    little = head[5] == 1
    return EM.get(int.from_bytes(head[18:20], "little" if little else "big"))


def needed(path) -> list[str]:
    """DT_NEEDED entries. Walk program headers -> PT_DYNAMIC (type 2) ->
    the Elf64_Dyn array; collect DT_NEEDED (tag 1) string-table offsets and
    DT_STRTAB (tag 5); resolve the offsets against the PT_LOAD segment that
    contains DT_STRTAB's vaddr."""
    ...
```

`build_bundle.py` must **assert** `elf_arch()` matches the directory for every
binary and library it writes. The prototype has no such check.

### 1.3 `build/build_bundle.py`

Assembles one zip:

```
utils_bundle_v<version>+<hash>.zip
├── chunky_utils/                       every .py from procedure/utils/
├── poppler_bundle/arm64/poppler/{bin,lib}/
├── poppler_bundle/x86_64/poppler/{bin,lib}/
├── pdf2image/                          unzipped from the PyPI wheel
└── MANIFEST.json                       version, timestamp, per-arch sha256s
```

- `pdf2image` is `py3-none-any` — a wheel is a zip, so `zipfile` reads the
  package directory straight out of it. Do not shell out to `pip --target`.
  Pin the version; record it in `MANIFEST.json`.
- Ship **both** arches (decision 12).

### 1.4 Defeating the IMPORTS cache — the most important operational detail

Snowflake caches a procedure's `IMPORTS` artifacts per warehouse. Re-`PUT`ting
a same-named zip does **not** reliably make a live procedure pick it up, and
the failure mode is an afternoon spent debugging code that is not running.

- The zip filename embeds `procedure/utils/__init__.py:__version__` plus a
  short hash of the zipped `chunky_utils/` contents. Same code → same name
  (cheap re-deploys); changed code → new name (the cache cannot lie).
- `render_sql.py` substitutes that exact filename into `IMPORTS = (...)`.
- `deploy.py` always issues `CREATE OR REPLACE PROCEDURE`.
- `deploy.py` then calls `CHUNKY_INGEST('status', {ping: true})` and
  **fails the build** if the returned `bundle_version` is not the version it
  just built. Never proceed past that mismatch.
- `deploy.py --gc` prunes old bundle versions from the stage.

### 1.5 `deploy/deploy.py`

Thin orchestration over the existing `deploy/` modules:

1. Connect (`deploy.auth.connect()`).
2. `PUT` the bundle. **Windows path note:** build the URI as `"file://" +
   Path(p).resolve().as_posix()` — `file://C:/Users/...` works,
   `file://C:\Users\...` silently matches nothing.
3. `LIST` and assert the staged size equals the local size — a truncated `PUT`
   is otherwise silent.
4. Run `bootstrap.sql` (render stage only — there is no control table) — idempotent.
5. Execute the three rendered `.sql` files via `sqlsplit`.
6. Verify: `SHOW PROCEDURES LIKE 'CHUNKY\_%'`, then the version assertion.

Flags: `--dry-run`, `--only ingest|qa|deploy`, `--skip-put`, `--gc`.

**Exit:** bundle builds on Windows with no external tools; every bundled
binary passes the arch assertion; `deploy.py` succeeds and the version
assertion passes; editing a comment in `constants.py` produces a different zip
name and still passes.

---

## Phase 2 — Package skeleton

**Deliverable:** the module layout of §5, with the shared primitives in place,
so Phases 3–5 write handler logic and nothing else.

1. **Rename** the three handler modules and templates per decision 1. Update
   `constants.py` to `PROC_INGEST = "CHUNKY_INGEST"`, `PROC_QA = "CHUNKY_QA"`,
   `PROC_DEPLOY = "CHUNKY_DEPLOY"`. `_shared.make_revert_command` is already
   parameterised, so only the constants change — but verify the emitted
   `revert.command` string is a runnable `CALL`, because operators paste it.
   **No compatibility aliases** (decision 2).
2. **Delete** the dead modules and files listed in §5. Before deleting
   `surgical_delete.py`, diff it against the inlined version in the handler —
   it has transaction handling the inline copy may have dropped. Port anything
   missing; don't lose it.
3. **`constants.py`** — remove `DEFAULT_DB` and `DEFAULT_SCHEMA` outright
   (decision 3). Add `DEFAULT_EMBEDDING_MODEL = "voyage-multilingual-2"`,
   `SUPPORTED_EMBEDDING_MODELS`, `TARGET_LAG = "365 days"` (fixed, not
   caller-facing — decision 8b), `SCREENSHOT_DPI = 200`,
   `SCREENSHOT_MAX_BYTES = CORTEX_IMAGE_MAX_BYTES = 3_500_000`,
   `CORTEX_IMAGE_MAX_EDGE = 8000`.
4. **`__init__.py`** — `__version__ = "2.0.0"`, correct module docstring.
5. **`_shared.py`** — add `ok`/`err` (§3.7), `safe_identifier`,
   `safe_stage_path`, and `require(inst, "db", "schema", ...)`:

```python
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,254}$")
_STAGE = re.compile(r'^@[A-Za-z0-9_$."]+(/[A-Za-z0-9_.\-/ ]*)?$')


def safe_identifier(name, what="identifier") -> str:
    s = str(name or "").strip()
    if not _IDENT.match(s):
        raise ValueError(
            f"Invalid {what}: {name!r}. Must match [A-Za-z_][A-Za-z0-9_$]*."
        )
    return s


def require(inst: dict, *keys) -> tuple:
    """Fetch required instruction keys, or raise with all of them named.

    db and schema are always required (decision 3) -- a headless caller has no
    session gate to fall back on, so an omitted target is an error, not a
    default.
    """
    missing = [k for k in keys if not inst.get(k)]
    if missing:
        raise ValueError(
            f"Missing required instruction field(s): {', '.join(missing)}. "
            f"Every Chunky command requires an explicit 'db' and 'schema'."
        )
    return tuple(inst[k] for k in keys)
```

6. **Delete the `IT_AI` special-case** from `grant_table._normalise_role`,
   `_shared.safe_role` and `chunky_deploy_handler._safe_role`. One shared
   `safe_role` in `_shared.py`; grant exactly what the caller lists; report
   results as three lists:

```json
"grant_result": {
  "granted":  ["ANALYST", "DATA_ENG"],
  "rejected": [{"role": "bad-role!", "reason": "not a valid Snowflake identifier"}],
  "failed":   [{"role": "FINANCE",   "reason": "Insufficient privileges to operate on table"}]
}
```

7. **`ulid.py`**, **`table_comment.py`**, **`reindex.py`**, **`render.py`**,
   **`locks.py`** — created here, filled in by their owning phase.
8. **The command registry and `help` (§3.9)** — this lands in Phase 2, not
   later, because everything after it depends on the dispatcher shape.
   `procedure/utils/registry.py` provides:

```python
def dispatch(session, command, instruction, commands, procedure_name):
    """Validate against the registry, then route. The single entry point
    every handler's run() delegates to, so validation, help and the
    unknown-command recovery path cannot diverge between procedures."""
    cmd = (command or "").strip().lower()
    if cmd in ("help", "", "?"):
        return render_help(procedure_name, commands, instruction or {})
    if cmd not in commands:
        near = difflib.get_close_matches(cmd, commands, n=1, cutoff=0.6)
        return err(cmd or "(none)",
                   f"Unknown command {command!r} for {procedure_name}.",
                   remedy=(f"Did you mean '{near[0]}'? " if near else "") +
                          f"Valid commands: {', '.join(sorted(commands))}. "
                          f"Run CALL {procedure_name}('help') for details.",
                   data={"valid_commands": sorted(commands)})
    spec = commands[cmd]
    inst, problems = validate(instruction, spec)   # required, defaults,
                                                   # enums, unknown fields
    if problems:
        return err(cmd, "; ".join(problems),
                   remedy=f"CALL {procedure_name}('help', "
                          f"OBJECT_CONSTRUCT('command','{cmd}')) "
                          f"for the full field list.",
                   data={"fields": field_summary(spec)})
    return spec["handler"](session, inst)


def render_help(procedure_name, commands, inst): ...
def validate(instruction, spec): ...
```

   `validate` also rejects **unknown fields with a near-match suggestion** —
   today a typo'd optional field is silently ignored and the caller wonders
   why `vision` had no effect.

9. **Response-clarity retrofit (§2.4).** Sweep every `except` and every
   validation branch in the three handlers:
   - no bare `str(e)` — attach the failing object, the caller's role, and a
     `remedy`;
   - every mutating success path sets `next` with fully-substituted,
     paste-able calls;
   - `list`/`describe` project a stable subset and keep raw rows under
     `data.raw`.

   The `errors` table in each command's registry entry is the source for the
   `remedy` strings, so `help`'s troubleshooting topic and the live error
   messages stay identical by construction.

10. **Move the tests** into `procedure/tests/`, keeping the `chunky_utils`
    alias trick from the existing `conftest.py`.

**Exit:** T0+T1 green; `deploy.py` deploys three procedures under the new
names; `CALL CHUNKY_INGEST('help')` lists every command and
`CALL CHUNKY_INGEST('nonsense')` returns the valid list plus a near-match
suggestion; `CHUNKY_INGEST('status', {ping:true})` returns `bundle_version 2.0.0`;
zero grep hits for `chunky_chunks`, `RELATIVE_PATH`, `DEFAULT_DB`, or a
hardcoded `IT_AI` outside this plan.

---

## Phase 3 — Ingest

**Deliverable:** `CHUNKY_INGEST('ingest', …)` writes correct chunks, truthful
metrics, page screenshots, a table comment, and reindexes dependent services —
or fails loudly having written nothing it can't roll back.

### 3.1 Table shape (decision 4)

- `CREATE TABLE` per §3.3, both the `IF NOT EXISTS` and the
  `OR REPLACE … COPY GRANTS` (OVERWRITE) variants, with `COMMENT`.
- `RELATIVE_PATH` → `PDF_NAME` — ~40 call sites. Sweep first, list them, make
  the list the commit body (§17).
- `CHUNK_TYPE` / `CHUNK_REF` / `LINK_BLOCK` become `CHUNK_METADATA` fields.
  Note `LINK_BLOCK` is *also* concatenated into `CHUNK` at insert; keep that,
  and don't append twice.
- `CHUNK_ID` via the composed ULID (§3.3).

### 3.2 No silent data loss

The prototype's per-batch insert is wrapped in `except Exception: ROLLBACK`
that appends no warning and does not abort, so a failing insert silently drops
a batch and `ingest` still returns `success: true`. That is the exact failure
mode Chunky exists to prevent.

The corrected shape:

```python
for i, batch in enumerate(batches):
    try:
        ... write_pandas + INSERT ... SELECT ... LATERAL FLATTEN ...
    except Exception as exc:
        pages = [r["PAGE_NUMBER"] for r in batch]
        warnings.append(
            f"Layout insert failed for batch {i + 1}/{len(batches)} "
            f"(pages {min(pages)}-{max(pages)}): {exc}. "
            f"Remaining batches were not attempted."
        )
        failed_batch = {"index": i, "pages": pages, "error": str(exc)}
        break                                   # abort: partial > clean fail
...
if failed_batch:
    return err("ingest", "Layout extraction failed — the table may be "
                         "partially written. Use the returned revert payload.",
               data={..., "failed_batch": failed_batch},
               log=log, warnings=warnings, revert=revert_payload, run_id=run_id)
```

Same for the Vision path (which already checks `pages_processed == 0`, but
must also fail on *partial* success below a threshold). Add the mirror check
after layout: `metrics["layout_pages"] > 0` or fail.

### 3.3 Temporary table

`CREATE TEMPORARY TABLE {name} (...)` — unqualified, session-scoped,
auto-dropped. The prototype creates a **permanent** `TEMP_CHUNKS_<uuid>` in
the caller's schema and drops it in a `finally`; if the SP dies in between it
is orphaned and visible to everyone. Live-test `write_pandas` against a temp
table under `EXECUTE AS CALLER` — it works in theory.

### 3.4 Transactions

Live-test `BEGIN`/`COMMIT` inside a Python SP. Preferred outcome: remove it
from the layout insert path (a single `INSERT … SELECT … LATERAL FLATTEN` is
already atomic) and keep it only for the multi-statement surgical delete,
where it is genuinely needed. Record the live result in `ARCHITECTURE.md`.

### 3.5 `render.py` — one renderer, 200 DPI, two budgets (decisions 5, 5b)

```python
"""Page rendering and screenshot column I/O. One implementation.

The prototype defines save_optimized_image three times, in
chunky_chunks_handler, chunky_qa_handler and hybrid_repair, with divergent
RGBA->RGB conversion order. This is the only copy.
"""
from .constants import (SCREENSHOT_DPI, SCREENSHOT_MAX_BYTES,
                        CORTEX_IMAGE_MAX_BYTES, CORTEX_IMAGE_MAX_EDGE)

_JPEG_LADDER = (92, 85, 75, 65, 55, 45)


def render_page(pdf_bytes: bytes, page: int, dpi: int = SCREENSHOT_DPI) -> bytes:
    """PDF page -> image bytes, <= SCREENSHOT_MAX_BYTES and <= 8000 px.

    Rendered straight to the Cortex-safe budget so the stored PAGE_SCREENSHOT
    can be handed to AI_COMPLETE with no derivative step -- see "One budget"
    in §3.3. 200 DPI is the quality floor for Vision extraction of dense
    tables and for a human reading a financial statement in QA. PNG first
    because it is lossless for text; JPEG only when PNG cannot fit, because
    JPEG artefacts on small type cost extraction accuracy.
    """
    from PIL import Image
    from pdf2image import convert_from_bytes
    from .poppler_bootstrap import get_poppler_bin_or_raise

    images = convert_from_bytes(pdf_bytes, first_page=page, last_page=page,
                                dpi=dpi, poppler_path=get_poppler_bin_or_raise())
    if not images:
        raise RuntimeError(f"poppler returned no image for page {page}")

    img = images[0]
    # Claude rejects >8000x8000 outright. Only large-format source pages
    # (A0 posters, engineering drawings) reach this at 200 DPI, but they do.
    longest = max(img.width, img.height)
    if longest > CORTEX_IMAGE_MAX_EDGE:
        ratio = CORTEX_IMAGE_MAX_EDGE / longest
        img = img.resize((int(img.width * ratio), int(img.height * ratio)),
                         Image.Resampling.LANCZOS)
    return _encode_within(img, SCREENSHOT_MAX_BYTES)


def _encode_within(image, budget: int) -> bytes:
    """PNG -> JPEG ladder -> downscale, until it fits `budget`."""
    import io
    from PIL import Image

    scale = 1.0
    for _ in range(4):                         # at most 4 downscale attempts
        img = image if scale == 1.0 else image.resize(
            (int(image.width * scale), int(image.height * scale)),
            Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        if buf.tell() <= budget:
            return buf.getvalue()

        rgb = img.convert("RGB") if img.mode in ("RGBA", "P", "LA") else img
        for quality in _JPEG_LADDER:
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= budget:
                return buf.getvalue()
        scale *= 0.8                           # still too big: shrink, retry
    return buf.getvalue()                      # caller enforces the hard cap


def screenshot_for_page(session, log, db, schema, table, pdf_name, page):
    """The column first; poppler only as a fallback. Returns (bytes, source)."""
    import base64
    rows = log.execute(
        f"SELECT BASE64_ENCODE(PAGE_SCREENSHOT) AS B64 FROM {qualify(db, schema, table)} "
        f"WHERE PDF_NAME = ? AND PAGE_NUMBER = ? AND PAGE_SCREENSHOT IS NOT NULL "
        f"LIMIT 1", params=[pdf_name, page])
    if rows and rows[0]["B64"]:
        return base64.b64decode(rows[0]["B64"]), "column"
    return None, "missing"


def screenshot_to_stage(session, log, image: bytes, render_stage, key) -> str:
    """Write bytes to the render stage; returns the stage-relative path.
    Needed because AI_COMPLETE takes TO_FILE(stage, path), not a BINARY.
    The stored image is already within the Cortex budget, so it goes as-is."""
    ...
```

Hard cap at the insert: if the encoded bytes still exceed
`SCREENSHOT_MAX_BYTES`, store `NULL` and warn, naming the page. Never fail the
insert over a screenshot.

T0 covers the ladder with synthetic images: PNG path, JPEG fallback,
downscale path, and the >8000 px resolution guard. Add one assertion that
`SCREENSHOT_MAX_BYTES == CORTEX_IMAGE_MAX_BYTES` — if a future change splits
them, the no-derivative invariant silently breaks.

Screenshot storage is one row per page: build the batch so the first chunk of
each page carries the base64 and the rest carry `NULL`.

### 3.6 Truthful metrics

Count what was written, not what was submitted. After each batch:

```sql
SELECT CHUNK_METADATA:chunk_type::VARCHAR AS T, COUNT(*) AS N
FROM <table> WHERE PDF_NAME = ? AND PAGE_NUMBER BETWEEN ? AND ?
GROUP BY 1
```

Report `pages_expected`, `pages_written`, `chunks_written`,
`page_coverage` (which expected pages got ≥1 chunk), and the per-type counts.
The Streamlit app had a page-coverage map; headless callers need the same
signal.

### 3.7 Actionable Cortex errors

`run_cortex` returns `(text, prompt_tokens, completion_tokens, error)` instead
of swallowing everything into `("", 0, 0)`. Map the common failures to a
one-line remedy: model not available in region, `SNOWFLAKE.CORTEX_USER` not
granted, rate limited, image too large, empty response. The existing poppler
error string is the model to copy — it is the best error message in the
prototype.

### 3.8 Render stage, not the docs stage

Page images go to
`@SBOX_DB.AI_SB.CHUNKY_RENDER/{run_id}/{safe_pdf_name}/p{N}.png`, never into
the docs stage. `instruction.render_stage` is required (no default that points
at data). After a successful ingest, `REMOVE @CHUNKY_RENDER/{run_id}/` unless
`keep_renders: true` — the durable copy is the `PAGE_SCREENSHOT` column.

Ripple: `hybrid_repair.run_hybrid_repair` and the QA handler both derive image
paths from `stage_path` today. Five call sites; sweep before editing.

### 3.9 Identifier validation

Every caller-supplied `db`/`schema`/`table` through `safe_identifier()`, every
stage through `safe_stage_path()`, at the top of each command. `EXECUTE AS
CALLER` bounds the blast radius to the caller's own privileges, but building
SQL from an unvalidated string is still wrong.

### 3.10 Table comment + auto-reindex (decisions 9, 10)

At the end of a successful ingest, in order:

1. `table_comment.record_ingest(...)` — update `sources`, `last_run_id`,
   `last_modified_at`.
2. `reindex` every service in `search_services` (§3.5), unless
   `auto_reindex: false`.
3. `locks.release(...)` — in a `finally`, so a failure path clears it too.
4. Return, with `data.reindex` and any stale-service warnings.

### 3.11 Lease, progress heartbeat, and the response envelope

Three things that are all one edit to `cmd_ingest`'s outer structure:

```python
def cmd_ingest(session, inst):
    db, schema, table = require(inst, "db", "schema", "table")
    run_id = inst.get("run_id") or ulid.run_id()        # X5 — never ""
    log = QueryLog(session)

    acquired, holder = locks.acquire(
        session, log, db, schema, table, "ingest",
        holder=_current_user(session, log), run_id=run_id,
        detail=f"{inst.get('file','')} {inst.get('range') or 'full'}",
        force=bool(inst.get("force")))
    if not acquired:
        return err("ingest", _busy_message(db, schema, table, holder),
                   remedy=_busy_remedy(db, schema, table, holder),
                   data={"lock": holder}, run_id=run_id, log=log)
    try:
        ...                                            # the existing pipeline,
                                                       # heartbeating as it goes
        return ok("ingest", data={...}, log=log, warnings=warnings,
                  revert=revert_payload, run_id=run_id,
                  next_steps=_next_after_ingest(db, schema, table, run_id))
    finally:
        locks.release(session, log, db, schema, table, "ingest", token)
```

Three defects close here at once:

- **X4** — every `return` in the file becomes `ok(...)` / `err(...)`. They are
  already imported; nothing calls them. Grep for `return {` in the handler
  afterwards; there should be none.
- **X5** — `run_id` is generated when absent, never `""`.
- **X2** — the lease.

**Heartbeat:** `locks.heartbeat(...)` after each Vision page and each Layout
batch, throttled to one write per 30 s, outside any transaction, wrapped so a
failure warns and continues. `eta_seconds` from the running mean of per-page
latency in this run.

This is the difference between a client that can show a progress bar and one
that shows a spinner for six minutes — and, because the heartbeat also extends
`expires_at`, a long-but-healthy job never has its own lease expire underneath
it.

### 3.12 `estimate_cost` answers "how long" (§2.2 ②)

Add `estimated_duration_seconds` (pages × measured per-page latency, split by
strategy) and `suggested_timeout_seconds` (that, × 1.5, floored at 60) to the
response, plus a `note` saying to put the latter in the SQL API request body's
`"timeout"`. An estimate that answers only "how much" leaves the integrator to
guess the timeout, and they will guess low.

Seed the per-page constants from Phase 3's own smoke runs; correct them in
Phase 7 against `SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY`.

### 3.13 Errors carry a remedy (§2.4 principle 1)

Every `err(...)` supplies `remedy`. The failures worth handcrafting, because
they are the ones people actually hit:

| Failure | Remedy says |
|---|---|
| PDF not on the stage | the `LIST` to run, the `sf put` to fix it, and that `stage_path` + `file` are joined |
| `SNOWFLAKE.CORTEX_USER` not granted | the exact `GRANT DATABASE ROLE` statement |
| stage is client-side encrypted | that SSE is required, and to re-create the stage with `ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')` |
| poppler arch missing | rebuild the bundle, or `vision: false, layout: true` as a workaround |
| model unavailable in region | the supported list, and that availability is regional |
| insufficient privileges | the object and the privilege, named |

Also accept `pdf_name` (not just `file`) as a filter on `list_chunks`,
`inspect_quality` and `delete_chunks` — users think in documents, not tables
(§2.4 principle 4).

### 3.14 Query-log cost

`instruction.capture_query_ids` (default `true`). When `false`, `QueryLog`
skips the `SELECT LAST_QUERY_ID()` round-trip after every statement — which
roughly doubles the statement count today — and `revert` falls back to
`timestamp_before` alone. That is sufficient for every path except
query-id-scoped revert.

**Exit:** live ingest of the fixture in all three strategies (Vision-only,
Layout-only, Layout+Vision) into `SBOX_DB.AI_SB`; ≥1 chunk per page; screenshots
present, one per page, each ≤3.5 MB; metrics match `SELECT COUNT(*)`; table
comment parses and lists the source; `revert` restores; a deliberately broken
run returns `success: false` with the failing page range named.

---

## Phase 4 — QA

**Deliverable:** QA that reads screenshots from the table, costs little enough
to call freely, and gates Deploy.

### 4.1 Screenshots from the column

`render.screenshot_for_page()` (Phase 3.5) resolves column-first. On a `NULL`
column, fall back to poppler, **write the result back into the row**, and warn
— a missing screenshot means the ingest didn't do its job.

Surgical awareness is unchanged: `get_original_pdf_page()` maps a stored
`PAGE_NUMBER` back to the source PDF page via
`CHUNK_METADATA:surgical.page_mappings`. The stored screenshot is already the
right page's image, so only the poppler fallback needs the mapping.

`instruction.screenshot_format`:

| value | behaviour |
|---|---|
| `"url"` (default) | write to `@CHUNKY_RENDER`, return `GET_PRESIGNED_URL`. Response stays small. |
| `"base64"` | inline in `data`. Convenient for an agent; a VARIANT return is capped at 16 MB and base64 inflates by ~33%, so enforce `max_screenshots` — a 3.5 MB image becomes ~4.7 MB of JSON. |
| `"none"` | skip. |

### 4.2 `search` stays cheap

`include_screenshots` default **false**; `max_screenshots` default 10; keyed by
`(pdf_name, page_number)` and resolved **once** per call. The prototype
re-renders the same page for every chunk on it — a 20-chunk page costs 20
renders at `limit: 100`. `inspect` keeps screenshots on (single page).

### 4.3 Honest results

- `commit` returns `success = (no result errored)`, with
  `committed`/`failed`/`skipped` counts and each error in `warnings`.
- `render` failures reach the caller in `warnings`, not only `print()`.
- Delete the duplicate `get_silver_bullet_prompt` from the QA handler; import
  from `prompts.py`. Diff the two first — if they've drifted, `prompts.py`
  wins (it's the one `hybrid_repair` uses) and the diff goes in the commit
  message.

### 4.4 `sign_off` — advisory, not a gate (decision 17)

```sql
CALL CHUNKY_QA('sign_off', OBJECT_CONSTRUCT(
    'db','SBOX_DB','schema','AI_SB','table','CHUNKS_REPORTS',
    'run_id','RUN_01JZ...',
    'verdict','APPROVED',
    'notes','5/5 pages verified against source',
    'acknowledge_defects', FALSE));
```

1. Run `QualityInspector` over the table (or the run's file/range).
2. Defects > 0 and not `acknowledge_defects` → `success: false` with the
   breakdown and the offending `chunk_id`s. That is the *inspection* refusing,
   and it is the one thing here that does block — but it blocks the sign-off,
   not the deploy.
3. Otherwise write the verdict into the table comment's `qa` block:
   `{status, at, by, run_id, notes, defects}`.

`CHUNKY_DEPLOY('create')` reads that block and **warns** when it is absent, or
when `qa.at` predates `sources[*].last_ingested_at` (signed off, then more
content arrived). It never refuses, and there is no `force` flag to learn —
per decision 17, a bypass everyone passes by reflex is worse than no gate,
because it trains people to ignore the warning it was supposed to raise.

The warning is worth getting right, since it is the whole mechanism:

> *"Deploying CHUNKS_REPORTS with no QA sign-off. Nobody has reviewed this
> extraction. Run `CALL CHUNKY_QA('sign_off', …)` first if that matters —
> the service is live either way."*

### 4.5 `gc_renders` (decision 13)

`CHUNKY_QA('gc_renders', {render_stage, older_than_hours: 24, dry_run: true})`
→ `LIST` + `REMOVE`. Dry-run by default. Document the manual sweep in
`README.md`; no scheduled task.

**Exit:** live round trip — `search` without screenshots, `inspect` with a
presigned URL that returns HTTP 200 (and `data.screenshot_source == "column"`,
proving no re-render), `generate_draft` → `commit` → verify → `revert` →
verify, then `sign_off`. Sign-off on a deliberately corrupted chunk must be
refused. `commit` must trigger a reindex of the table's services.

---

## Phase 5 — Deploy

**Deliverable:** `CHUNKY_DEPLOY('create', …)` produces a Cortex Search Service
that is proven serving, with scheduled indexing switched off, recorded in the
table comment.

### 5.1 The DDL builder

This is the piece that cannot work in the prototype — it never emits
`WAREHOUSE`, which Snowflake requires. Reference implementation:

```python
def _build_create_ddl(session, log, inst, run_id) -> tuple[str, dict]:
    db, schema = require(inst, "db", "schema")
    service = safe_identifier(inst["service_name"], "service_name")
    tables = [safe_identifier(t, "table") for t in (inst.get("tables") or [])]
    if not tables:
        raise ValueError("instruction.tables must list at least one table")

    # --- warehouse: the caller's, unless they name one (decision 6) --------
    warehouse = inst.get("warehouse")
    source = "instruction"
    if not warehouse:
        rows = log.execute("SELECT CURRENT_WAREHOUSE() AS W")
        warehouse = rows[0]["W"] if rows else None
        source = "session"
    if not warehouse:
        raise ValueError(
            "No warehouse for the search service. Pass 'warehouse' in the "
            "instruction, or run with a session warehouse set.")
    warehouse = safe_identifier(warehouse, "warehouse")

    model = inst.get("embedding_model", DEFAULT_EMBEDDING_MODEL)
    if model not in SUPPORTED_EMBEDDING_MODELS:
        raise ValueError(
            f"Unsupported embedding_model {model!r}. Supported: "
            f"{', '.join(SUPPORTED_EMBEDDING_MODELS)} "
            f"(availability is region-dependent).")

    # TARGET_LAG is required by the DDL but is NOT a caller-facing knob
    # (decision 8b). Indexing is suspended right after verify, so the value is
    # inert; refresh is explicit. Reject it loudly rather than ignoring it --
    # silently dropping a field the caller set is worse than saying no.
    if "target_lag" in inst or "target_lag_unit" in inst:
        raise ValueError(
            "target_lag is not configurable. Chunky suspends scheduled "
            "indexing after deployment and refreshes on demand instead — see "
            "CHUNKY_DEPLOY('reindex', ...).")

    search_cols, vector_cols, text_cols = _categorise(inst.get("search_columns"))
    attributes, projections = _resolve_attributes(
        session, log, db, schema, tables, inst.get("attribute_columns"))

    single = len(search_cols) == 1 and len(vector_cols) <= 1

    # --- clause ORDER is fixed by Snowflake -------------------------------
    #   index clauses -> PRIMARY KEY -> ATTRIBUTES -> WAREHOUSE
    #   -> TARGET_LAG -> EMBEDDING_MODEL (single-index only) -> AS <query>
    parts = [f"CREATE OR REPLACE CORTEX SEARCH SERVICE {qualify(db, schema, service)}"]
    if single:
        parts.append(f'  ON "{search_cols[0]}"')
    else:
        if text_cols:
            parts.append("  TEXT INDEXES " + ", ".join(f'"{c}"' for c in text_cols))
        if vector_cols:
            parts.append("  VECTOR INDEXES " + ", ".join(
                f"\"{v['col']}\" (model='{v['model']}')" for v in vector_cols))
    if inst.get("primary_key", "CHUNK_ID"):
        pk = safe_identifier(inst.get("primary_key", "CHUNK_ID"), "primary_key")
        parts.append(f'  PRIMARY KEY ("{pk}")')
    if attributes:
        parts.append("  ATTRIBUTES " + ", ".join(f'"{c}"' for c in attributes))
    parts.append(f'  WAREHOUSE = "{warehouse}"')
    parts.append(f"  TARGET_LAG = '{TARGET_LAG}'")     # fixed constant
    if single and vector_cols:
        parts.append(f"  EMBEDDING_MODEL = '{model}'")
    parts.append(f"  COMMENT = '{clean_text_for_sql(inst.get('comment') or f'chunky {BUNDLE_VERSION} run {run_id}')}'")
    parts.append("AS (\n" + _union_query(db, schema, tables, projections) + "\n)")

    return "\n".join(parts), {
        "warehouse": warehouse, "warehouse_source": source,
        "embedding_model": model if (single and vector_cols) else None,
        "target_lag": TARGET_LAG, "attributes": attributes,
        "multi_index": not single,
    }
```

Points that are easy to get wrong and are tested in T1
(`tests/test_sql_shape.py`, no Snowflake needed):

- `WAREHOUSE` is present. Always.
- `ATTRIBUTES` precedes `WAREHOUSE`; `WAREHOUSE` precedes `TARGET_LAG`.
- `EMBEDDING_MODEL` appears **only** in the single-index form — in the
  multi-index form the model is per column inside `VECTOR INDEXES`.
- No trailing `;` inside the string passed to `session.sql()`.
- `PAGE_SCREENSHOT` is **never** projected into the `AS` query — it is BINARY,
  useless as an attribute, and would bloat the index enormously.

`_resolve_attributes` handles the §3.3 schema change: `ATTRIBUTES` can only
name columns of the `AS` query, and `chunk_type`/`chunk_ref` now live inside
`CHUNK_METADATA`. So accept either spelling and synthesise the projection:

```python
# {"column": "CHUNK_TYPE"}  or  {"metadata_field": "chunk_type"}
#   -> AS query gets:  CHUNK_METADATA:chunk_type::VARCHAR AS "CHUNK_TYPE"
#   -> ATTRIBUTES gets: "CHUNK_TYPE"
```

Resolve against the table's real column list (`INFORMATION_SCHEMA.COLUMNS`) so
a caller never has to know where a field is stored.

### 5.2 Change-tracking preflight

For every source table: `SHOW TABLES LIKE '<t>' IN SCHEMA "<db>"."<schema>"`,
read `change_tracking`. If `OFF`, `ALTER TABLE … SET CHANGE_TRACKING = TRUE`
when `auto_enable_change_tracking` (default `true`), else fail with the exact
`ALTER` to run. Also confirm `DATA_RETENTION_TIME_IN_DAYS > 0`.

### 5.3 `wait_ready`

Poll `DESCRIBE CORTEX SEARCH SERVICE <fqn>` for `INDEXING_STATE` /
`INDEXING_ERROR`, and `SHOW CORTEX SEARCH SERVICES LIKE '<n>' IN SCHEMA
"<db>"."<schema>"` for `SERVING_STATE`. Exit on healthy, on a non-empty
`INDEXING_ERROR` (return it), or on timeout (`success: false` with the last
observed states — a timeout is not necessarily a failure; the caller may poll
again). Pin the exact column names in `constants.py` after Phase 0 check 9
confirms them, and tolerate their absence rather than `KeyError`.

Note the prototype's `cmd_alter` runs `SHOW … IN SCHEMA` with no schema name,
which is only valid when a current schema happens to be set. Always qualify.

### 5.4 `verify` — prove it serves

`SNOWFLAKE.CORTEX.SEARCH_PREVIEW('<fqn>', '{"query":"…","limit":N}')`. Return
the hit count and a trimmed preview of each hit. **Zero hits against a
non-empty table is a failure** — that is the whole difference between
"created" and "ready to serve".

### 5.5 Suspend indexing (decision 8)

Immediately after `verify` passes:

```sql
ALTER CORTEX SEARCH SERVICE <fqn> SUSPEND INDEXING;
```

Then re-`DESCRIBE` and assert `INDEXING_STATE` reports suspended, and that
`SERVING_STATE` is still serving — the point is to stop the scheduled refresh
without taking the service offline. Record `indexing: "SUSPENDED"` in the
table comment.

`instruction.suspend_indexing` (default `true`) lets a caller keep the
schedule if they ever want it.

### 5.6 `create` orchestrates the whole thing

Default `create` is one call:

```
acquire 'deploy' lease → sign-off check (warn only) → change-tracking preflight
  → build DDL → execute → wait_ready → verify → suspend indexing
  → table_comment.record_service() → grants → release lease
```

`wait_ready`, `verify_query` and `suspend_indexing` are all overridable, but
the defaults give a caller a serving, non-scheduled service from one command.

### 5.7 `reindex`, and the rest

- `reindex` — `{db, schema, table}` (all services for that table, from the
  comment) or `{service_name}` (one). Uses `reindex.reindex_service` (§3.5).
- `drop` — capture `GET_DDL` first, drop, then
  `table_comment.forget_service()` on every table it indexed.
- `revert` — execute the captured DDL as **one statement**; never split on
  `;`, which corrupts any `AS (<query>)` containing one.
- `describe` / `list` — verify `DESCRIBE CORTEX SEARCH SERVICE
  IDENTIFIER('<fqn>')` is accepted; if not, interpolate the validated
  identifier. Verify `GET_DDL('CORTEX_SEARCH_SERVICE', …)` is supported; if
  not, reconstruct from `DESCRIBE` and say so — a `revert` depending on a
  silently-`None` DDL is worse than no `revert`.

**Exit:** from a signed-off table, one `create` call returns `success: true`
with `data.warehouse_source`, healthy `serving_state`, `indexing_state`
suspended, and ≥1 verify hit. A `create` with no sign-off **succeeds with a
warning** naming the missing sign-off. Then: ingest one more page into the
table and confirm the service auto-reindexes and returns the new content.

---

## Phase 6 — Leases, progress & API contract

### 6.1 `locks.py`

The full design is §3.6. What Phase 6 builds:

```python
def acquire(session, log, db, schema, table, slot, *, holder, run_id,
            detail="", ttl_seconds=1800, force=False) -> tuple[bool, dict]
def heartbeat(session, log, db, schema, table, slot, token, progress,
              min_interval_seconds=30) -> None
def release(session, log, db, schema, table, slot, token) -> None
def describe(session, log, db, schema, table) -> dict   # all slots, staleness
```

Wiring, with the conflict matrix from §3.6:

| Handler | Slot | Where |
|---|---|---|
| `ingest`, `batch_ingest`, `update_chunk`, `delete_chunks` | `ingest` | acquire at the top, `release` in a `finally` |
| QA `commit`, `delete` | `qa` | same |
| `CHUNKY_DEPLOY('create')`, `reindex` | `deploy` | same |

Three rules the implementation must not bend:

1. **`release` lives in a `finally`.** An exception during ingest must still
   clear the slot, or the next caller waits out a 30-minute TTL for nothing.
2. **`release` merges, never replaces.** `sources` and `search_services` are
   written *during* the run, by the same handler; a release that writes a
   stale whole-block would erase them.
3. **A lease failure never fails the caller's work.** If `acquire` cannot read
   or write the comment, warn and proceed — coordination is best-effort, and
   failing an ingest because a `COMMENT` statement errored would be absurd.

`batch_ingest` takes **one** lease for the whole batch, not one per job.
Per-job leases would let another writer interleave between jobs, which is
exactly what the lease exists to prevent.

### 6.2 Progress heartbeat

`locks.heartbeat` writes into `locks.<slot>.progress` and extends
`expires_at`. This is what makes a six-minute ingest observable (§2.2 ③),
and it is why dropping `CHUNKY_RUNS` costs nothing on the UX side.

```json
{"phase": "vision", "pages_done": 17, "pages_total": 40,
 "eta_seconds": 184, "updated_at": "2026-08-10T06:23:41Z"}
```

- Throttled to **at most one write per 30 s** — a heartbeat is
  `ALTER TABLE … SET COMMENT`, which is DDL, and one per page on a 200-page
  document would be 200 DDL statements for no added insight.
- `eta_seconds` from the running mean of per-page latency **in this run**, not
  a constant.
- Outside any explicit transaction, and wrapped so a failure warns rather than
  raising. A dropped heartbeat costs a stale ETA, nothing more.

### 6.3 `status`

Now free: it reads the comment, which already holds everything.

- `{db, schema, table}` → the whole parsed block — live `locks` with progress,
  `sources`, `search_services`, `qa`. One call answers *"is anyone working on
  this, how far along, what's in it, and what indexes it?"* This is what a
  client polls alongside the SQL API statement handle: the handle says
  *running*, this says *page 17 of 40, ~3 minutes left*.
- `{ping: true}` → `{bundle_version, manifest, poppler_arch, current_user,
  current_role, current_warehouse}` — the cheap health check `deploy.py` uses
  for the version assertion.

Deliberately **not** supported, because there is no registry to answer them —
say so in `help` rather than returning an empty result:

- history for a finished run → `QUERY_HISTORY` filtered on `QUERY_TAG`
  (`Chunky/<version>|user=…|cmd=…|run=<run_id>`);
- actual cost → `CORTEX_FUNCTIONS_USAGE_HISTORY` on the same tag, noting
  ACCOUNT_USAGE latency of up to a few hours;
- `{run_id}` lookup after completion → same answer.

`help`'s `troubleshooting` topic carries both ready-made queries, so a caller
who needs history is one copy-paste away rather than stuck.

### 6.3 `API.md`

Per command: instruction schema (required/optional/default for every field),
`data` schema, warnings it can emit, errors it can return. Plus:

- The async pattern with a worked example, including that
  `data[0][0]` is a **JSON string** to parse.
- Timeout guidance: `"timeout"` in the POST body and
  `STATEMENT_TIMEOUT_IN_SECONDS`. Vision ingest ≈ N × 8–12 s serially — size
  the timeout from the page count, and prefer `batch_ingest` with small
  `range` windows over one enormous call.
- Idempotency: client-supplied `run_id`, and what a repeated call does.
- The caller-privilege table from §3.1.
- **Generated from the command registries** (§3.9) by a build script, not
  maintained by hand — the same declaration that drives dispatch, validation
  and `help`. A doc that can go stale will.
- Error catalogue: `success: false` with `error` (procedure-level) versus HTTP
  422 (Snowflake-level). Clients must handle both.

**Exit:** two live proofs.

1. **The lease works.** Start a real Vision ingest asynchronously, and while
   it runs: `status` shows the lease with live `progress`; a second `ingest`
   on the same table is **refused** naming the holder and ETA; a `search` on
   the same table **succeeds** (reads are never blocked); the second ingest
   with `force: true` proceeds. When the first finishes, `status` shows the
   slot cleared.
2. **The API path works.** `procedure/deploy/api_smoke.py` drives Ingest → QA
   → sign-off → Deploy → verify → reindex entirely over
   `POST /api/v2/statements`, including the 202-poll path, polling `status`
   for progress alongside the statement handle.

Proof 1 is the one that cannot be faked offline — it needs two concurrent
sessions against the real account.

---

## Phase 7 — Production auth, docs, handover

### 7.1 Key-pair JWT (decision 11)

`sfapi.keypair_jwt()` is implemented. What remains is the Snowflake side —
document it, don't create the user yourself:

```powershell
# generate (unencrypted key; use an encrypted one + passphrase in production)
openssl genrsa -out chunky_api_rsa_key.pem 2048
openssl rsa -in chunky_api_rsa_key.pem -pubout -out chunky_api_rsa_key.pub
```

```sql
CREATE USER CHUNKY_API_SVC TYPE = SERVICE
    DEFAULT_ROLE = <calling_role> DEFAULT_WAREHOUSE = <wh>;
ALTER USER CHUNKY_API_SVC SET RSA_PUBLIC_KEY = '<contents, no header/footer>';
GRANT ROLE <calling_role> TO USER CHUNKY_API_SVC;
```

The JWT's `iss` is `<ACCOUNT>.<USER>.SHA256:<base64 fingerprint of the DER
public key>` and `sub` is `<ACCOUNT>.<USER>`, where `<ACCOUNT>` is the account
name **without** the region suffix, uppercased (`AB14212`). `sfapi.py` already
computes this. Document key rotation ownership; support `RSA_PUBLIC_KEY_2` for
zero-downtime rotation.

The key file lives outside the repo; `config.json` holds only its path and is
git-ignored.

### 7.2 Docs

- **`ARCHITECTURE.md`** — rewritten against shipped code: three stages, the
  comment protocol, the index lifecycle, the advisory leases, the real module
  list, and the *verified* answers to every question this plan defers to a
  live test (transactions in SPs, `GET_DDL` support, `IDENTIFIER()` in
  `DESCRIBE`, `write_pandas` to a temp table under `EXECUTE AS CALLER`, the
  architecture-probe result, the exact `DESCRIBE` column names).
- **`README.md`** — operator quick-start pointing at `dev.ps1` and §16.
- **`CHANGELOG.md`** — starting at `2.0.0`.

### 7.3 Documented constraints

- **Concurrency:** two ingests into the same table concurrently are not safe
  (`OVERWRITE` mode, `revert`'s rename pattern, and the comment's
  read-modify-write all assume one writer). Document it; do not build locking.
- **Cost:** `estimate_cost`'s Vision heuristic (1500 in / 1200 out tokens per
  page) is unvalidated. Compare against
  `SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY` after Phase 3 and
  correct the constants, or label them order-of-magnitude.
- **Screenshot budget:** ~1 MB/page at 200 DPI, stored once per page. A
  500-page document is ~500 MB. `ingest` warns above 200 pages and suggests
  `store_screenshots: false`.
- **Secrets:** nothing in the repo; `config.json` and the private key
  git-ignored; no tool prints a token.

---

## 14. Testing

Five tiers. Each phase names the tiers it must pass.

### T0 — offline units (`tests/test_units.py`)
Pure functions, no Snowflake, no network. `_shared` (qualify, escaping,
`safe_identifier`, `require`, chunk_ref, revert-command rendering, `ok`/`err`),
`ulid` (26 Crockford chars, sortable, `is_ulid` on the SQL-composed form),
`page_mapping` surgical arithmetic, `layout_parse` on both
`AI_PARSE_DOCUMENT` shapes, `quality_inspector`, `metadata_handler`,
`table_comment` (round-trip, trimming, malformed comment → `{}`),
`render._encode_within_budget` (synthetic images: PNG path, JPEG fallback,
downscale path, the >8000 px guard, output ≤3.5 MB; plus an assertion that
`SCREENSHOT_MAX_BYTES == CORTEX_IMAGE_MAX_BYTES`, since splitting them would
silently break the no-derivative invariant). Fast enough to run on save.

### T1 — offline handlers, SQL shape & bundle
Snowpark session mocked.

- Dispatch: every command routes; an unknown command returns the valid list
  and a near-match suggestion; an unknown *field* is rejected with a
  suggestion; a missing required field names every missing key at once.
- `help`: every command in each registry has `summary`, `fields`, `example`
  and `errors`; every `fields` entry has a type; every required field appears
  in the example; and `set(COMMANDS) == set(dispatch table)` — the test that
  makes drift between code and documentation impossible.
- `next`: every mutating command's success response has a non-empty `next`,
  and every `call` string in it parses as a complete `CALL … ;` statement.
- No `err(...)` call site anywhere passes `remedy=None`.
- **Every command's response has an identical key set** (§3.7).
- Failure injection: session raises on statement N → `success: false` and the
  reason in `warnings` (the regression test for Phase 3.2).
- **`test_sql_shape.py`** — build SQL from fixed instructions and assert on the
  string. The cheapest guard against the whole class of bug that makes the
  prototype's Deploy stage non-functional:
  - `CREATE TABLE` names exactly the six columns of §3.3.
  - `INSERT` never mentions `RELATIVE_PATH`, `CHUNK_TYPE`, `CHUNK_REF` or
    `LINK_BLOCK` as columns.
  - Search DDL contains `WAREHOUSE =`; `ATTRIBUTES` before `WAREHOUSE`;
    `WAREHOUSE` before `TARGET_LAG`; `EMBEDDING_MODEL` absent in the
    multi-index form; `PAGE_SCREENSHOT` absent from the `AS` query;
    metadata attributes projected as `CHUNK_METADATA:x::VARCHAR AS "X"`.
  - `COMMENT ON TABLE` payload parses as JSON and round-trips.
- Bundle: exact `chunky_utils/*.py` set; `poppler_bundle/{arm64,x86_64}/…`
  present with the **correct ELF machine type** via `elfdeps.elf_arch`;
  `pdf2image` importable after extraction; `MANIFEST.json` version ==
  `chunky_utils.__version__`; `poppler_bootstrap` extracts, writes the
  ld-linux wrappers and `chmod +x`es them.

### T2 — live smoke (`deploy/smoke_test.py --tier 2`)
Real Snowflake, `SBOX_DB.AI_SB`, tiny inputs, fixed `SMOKE_`-prefixed names,
teardown first and last, `--keep` to debug.

| # | Call | Assert |
|---|---|---|
| 0 | `CHUNKY_INGEST('help')`, `CHUNKY_QA('help')`, `CHUNKY_DEPLOY('help')` | each lists its commands; `help` costs no warehouse work |
| 1 | `CHUNKY_INGEST('status',{ping})` | `bundle_version` == locally built |
| 2 | `estimate_cost` | `pages_to_process == 5` |
| 3 | `ingest` Vision, `[1,2]`, OVERWRITE | chunks > 0; coverage `{1,2}`; 2 screenshots, each ≤3.5 MB and ≤8000 px; `CHUNK_ID` ULID-shaped |
| 4 | read table `COMMENT` | parses; `sources[0].pdf_name` correct |
| 5 | `list_chunks` | matches |
| 6 | `inspect_quality` | breakdown present |
| 7 | `revert` by timestamp | table restored; backup table exists |
| 8 | `ingest` Layout, full doc | 5 pages covered |
| 9 | `ingest` Layout+Vision, `[3,3]`, SURGICAL | ≥1 `chunk_type=ENHANCED` |
| 10 | `QA search` no screenshots | rows; fast |
| 11 | `QA inspect` | presigned URL → HTTP 200; `screenshot_source == "column"` |
| 12 | `generate_draft` → `commit` → `search` | content changed |
| 13 | `QA revert` | restored |
| 14 | `sign_off` on a corrupted chunk | **refused**, offending chunk_ids named |
| 15 | `sign_off` with `acknowledge_defects` | comment `qa` block written |
| 16 | `DEPLOY create` (wait_ready, verify) | serving; `indexing_state` **suspended**; ≥1 hit; comment lists the service |
| 17 | `ingest` one more page | `data.reindex[0].status == "refreshed"` |
| 18 | `DEPLOY verify` for the new content | the new page's text is findable |
| 19 | `DEPLOY drop` + `revert` | restored; comment updated both times |
| 20 | teardown | table, service dropped; `REMOVE` render prefix |

Steps 16–18 are the ones that prove the product: a service that serves, does
not burn credits on a schedule, and still reflects new data.

### T3 — live workflow (`--tier 3`)
`create` without sign-off refused; with `force` succeeds and warns;
the comment's `qa` block records the right `by`/`at`;
`status` reports the right derived stage at each point; a repeated call with
the same `run_id` does not re-run.

### T4 — API (`deploy/api_smoke.py`)
T3 over `POST /api/v2/statements`, exercising the 202→poll path on the long
ingest and the VARIANT-string unwrap. Run it once with the dev OAuth token and
once with a key-pair JWT.

### T5 — architecture matrix (occasional)
Repeat T2 step 3 ≥10× and record `poppler_arch` from `status`. If only one
arch ever appears, raise halving the bundle as an optimisation — don't act on
it silently.

---

## 15. The iteration loop

```powershell
.\.venv-proc\Scripts\Activate.ps1
cd C:\Users\10102721\Documents\ClaudeFiles\chunky

python -m pytest procedure\tests -q -m "not live"   # T0 + T1
python procedure\build\build_bundle.py              # versioned zip
python procedure\deploy\deploy.py                   # PUT + CREATE + version assert
python procedure\deploy\smoke_test.py --tier 2      # T2
```

Wrap in `procedure\dev.ps1` with `-SkipBuild` / `-SkipDeploy` / `-Tier N`.

Guard rails:

- `deploy.py` **fails** on a `bundle_version` mismatch. Never proceed past it
  — it means the IMPORTS cache is serving old code and every result after it
  is meaningless.
- `smoke_test.py` writes `procedure/.smoke/<timestamp>.json` with every
  request and response. On a failure, the previous run's JSON is the first
  thing to read.
- Every failing statement's `query_id` goes into the smoke output;
  `SELECT * FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY()) WHERE QUERY_ID='…'`
  is the follow-up.
- Procedure `print()` output lands in the query log — keep the diagnostics in
  `poppler_bootstrap` and `render`.

| Symptom | First check |
|---|---|
| `ModuleNotFoundError: chunky_utils` | zip's top-level dir; `SELECT GET_DDL('PROCEDURE', …)` to see the deployed `IMPORTS` |
| Vision returns nothing, no error | `status` → `poppler_arch`; then the ld-linux wrappers in `/tmp` |
| `AI_PARSE_DOCUMENT` returns flat content | stage `DIRECTORY`/`ENCRYPTION`; the flat-response warning is already emitted |
| `TO_FILE` / presigned URL errors | stage must be `SNOWFLAKE_SSE` |
| Cortex "model not available" | region availability + `SNOWFLAKE.CORTEX_USER` |
| Service created but 0 hits | `INDEXING_STATE`, `INDEXING_ERROR`, then source `change_tracking` |
| Service returns stale content | did the ingest reindex? `data.reindex`; then `CHUNKY_DEPLOY('reindex', …)` |
| Behaviour doesn't match the code | the version assertion — you're running an old bundle |

---

## 16. Runbook

### One-time (mostly done)

```powershell
cd C:\Users\10102721\Documents\ClaudeFiles\chunky
.\.venv-proc\Scripts\Activate.ps1

python procedure\deploy\sf.py config show      # which tier answered for each value
python procedure\deploy\sf.py auth whoami      # silent if the refresh token is valid
python procedure\deploy\preflight.py           # checks + creates CHUNKY_RENDER
```

Expected from `whoami` on this machine:

```
user : MCDAAI_DNA   role : IT_AI   warehouse : SHARED_ID_XS
database : SBOX_DB  schema : AI_SB  region : AWS_AP_SOUTHEAST_1
```

If a browser login succeeds but Snowflake still rejects the session with
*"differs from the user tied to the access token"*, the configured `user` is
not the Snowflake username behind that SSO identity. `deploy/auth.py` purges
the poisoned cache and says so — fix the user, don't guess twice.

### Every deploy

```powershell
python procedure\dev.ps1                       # or the four commands in §15
```

### By hand, if you need it

```sql
PUT 'file://C:/Users/10102721/Documents/ClaudeFiles/chunky/procedure/build/out/utils_bundle_v2.0.0+ab12cd34.zip'
    @SBOX_DB.AI_SB.CHUNKY_UTILS AUTO_COMPRESS = FALSE OVERWRITE = TRUE;
LIST @SBOX_DB.AI_SB.CHUNKY_UTILS;              -- size must match the local file

CREATE OR REPLACE PROCEDURE CHUNKY_INGEST(command VARCHAR, instruction VARIANT)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
IMPORTS = ('@SBOX_DB.AI_SB.CHUNKY_UTILS/utils_bundle_v2.0.0+ab12cd34.zip')
PACKAGES = ('snowflake-snowpark-python', 'pandas', 'pypdf', 'pillow')
HANDLER = 'run'
EXECUTE AS CALLER
AS $$
from chunky_utils.chunky_ingest_handler import run
$$;
-- same for CHUNKY_QA and CHUNKY_DEPLOY

CALL CHUNKY_INGEST('status', OBJECT_CONSTRUCT('ping', TRUE));   -- prove the version
```

`pdf2image` and `poppler` are **not** in `PACKAGES` — they ship inside the zip.
`pandas` for `write_pandas`, `pypdf` for page counts and links, `pillow` for
image encoding.

### End to end

Run as a **calling role**, not as `IT_AI` — the procedures are
`EXECUTE AS CALLER` and the interesting failures are privilege failures that
only appear under a real caller.

```sql
USE DATABASE SBOX_DB; USE SCHEMA AI_SB; USE WAREHOUSE SHARED_ID_XS;

-- INGEST  (db + schema are mandatory, always)
CALL CHUNKY_INGEST('ingest', OBJECT_CONSTRUCT(
    'run_id',       'RUN_01JZMANUAL0000000000001',
    'db','SBOX_DB', 'schema','AI_SB', 'table','SMOKE_CHUNKS',
    'stage_path',   '@SBOX_DB.AI_SB.DOCS',
    'file',         'chunky_fixtures/fy2024-tbk-investor-presentation.pdf',
    'render_stage', '@SBOX_DB.AI_SB.CHUNKY_RENDER',
    'mode',         'OVERWRITE',
    'range',        ARRAY_CONSTRUCT(1, 5),
    'layout', FALSE, 'vision', TRUE,
    'chunk_size', 8000, 'overlap', 20,
    'store_screenshots', TRUE,
    'auto_reindex', TRUE,
    'grant_roles', ARRAY_CONSTRUCT('ANALYST')
));

-- QA
CALL CHUNKY_QA('search', OBJECT_CONSTRUCT(
    'db','SBOX_DB','schema','AI_SB','table','SMOKE_CHUNKS',
    'search_text','revenue','limit',10,'include_screenshots',FALSE));

CALL CHUNKY_QA('inspect', OBJECT_CONSTRUCT(
    'db','SBOX_DB','schema','AI_SB','table','SMOKE_CHUNKS',
    'chunk_id','CHK_01JZ...','screenshot_format','url'));

CALL CHUNKY_QA('sign_off', OBJECT_CONSTRUCT(
    'db','SBOX_DB','schema','AI_SB','table','SMOKE_CHUNKS',
    'run_id','RUN_01JZMANUAL0000000000001',
    'verdict','APPROVED','notes','manual check'));

-- DEPLOY. No 'warehouse' key -> CURRENT_WAREHOUSE(), i.e. SHARED_ID_XS.
-- Indexing is suspended automatically once verify passes.
CALL CHUNKY_DEPLOY('create', OBJECT_CONSTRUCT(
    'db','SBOX_DB','schema','AI_SB',
    'service_name','SMOKE_CSS',
    'tables', ARRAY_CONSTRUCT('SMOKE_CHUNKS'),
    'search_columns', ARRAY_CONSTRUCT(OBJECT_CONSTRUCT(
        'table','SMOKE_CHUNKS','column','CHUNK',
        'search_type','Hybrid','embedding_model','voyage-multilingual-2')),
    'attribute_columns', ARRAY_CONSTRUCT(
        OBJECT_CONSTRUCT('table','SMOKE_CHUNKS','column','PDF_NAME'),
        OBJECT_CONSTRUCT('table','SMOKE_CHUNKS','column','PAGE_NUMBER'),
        OBJECT_CONSTRUCT('table','SMOKE_CHUNKS','metadata_field','chunk_type')),
    'wait_ready', TRUE, 'verify_query', 'revenue',
    'suspend_indexing', TRUE,
    'grant_roles', ARRAY_CONSTRUCT('ANALYST')
));

-- Add more content: the ingest reindexes SMOKE_CSS automatically,
-- because the table's COMMENT lists it.
CALL CHUNKY_INGEST('ingest', OBJECT_CONSTRUCT(
    'run_id','RUN_01JZMANUAL0000000000002',
    'db','SBOX_DB','schema','AI_SB','table','SMOKE_CHUNKS',
    'stage_path','@SBOX_DB.AI_SB.DOCS',
    'file','chunky_fixtures/fy2024-tbk-investor-presentation.pdf',
    'render_stage','@SBOX_DB.AI_SB.CHUNKY_RENDER',
    'mode','APPEND', 'range', ARRAY_CONSTRUCT(6, 6)));

-- Or force it by hand
CALL CHUNKY_DEPLOY('reindex', OBJECT_CONSTRUCT(
    'db','SBOX_DB','schema','AI_SB','table','SMOKE_CHUNKS'));
```

From PowerShell, which is what you will actually do:

```powershell
python procedure\deploy\sf.py call CHUNKY_INGEST ingest   @procedure\deploy\jobs\smoke_ingest.json
python procedure\deploy\sf.py call CHUNKY_QA     sign_off @procedure\deploy\jobs\smoke_signoff.json
python procedure\deploy\sf.py call CHUNKY_DEPLOY create   @procedure\deploy\jobs\smoke_deploy.json
```

### Teardown

```sql
DROP CORTEX SEARCH SERVICE IF EXISTS SBOX_DB.AI_SB.SMOKE_CSS;
DROP TABLE IF EXISTS SBOX_DB.AI_SB.SMOKE_CHUNKS;
REMOVE @SBOX_DB.AI_SB.CHUNKY_RENDER/RUN_01JZMANUAL0000000000001/;
-- revert leaves *_revert_backup_<epoch> tables behind by design:
SHOW TABLES LIKE '%_revert_backup_%' IN SCHEMA SBOX_DB.AI_SB;
```

---

## 17. Working agreement

Adapted from `snowball/AGENTS.md`, which earned these the hard way.

### Ripple-effect analysis, before and during any change

Before touching a shared function, constant or default: **find every call site
and read what each one assumes**, not just the one you're changing. A change
that's correct where you made it but leaves a sibling holding a now-false
assumption is a bug that hasn't been triggered yet.

The specific hazards in this codebase:

- `constants.py` feeds all three handlers, `revert.py`, `hybrid_repair.py`,
  `layout_parse.py`, `render.py`. Changing `DEFAULT_USE_VISION`,
  `SCREENSHOT_MAX_BYTES` or `TIME_TRAVEL_MAX_HOURS` changes behaviour in
  places you aren't looking at.
- `_shared.py` is imported everywhere; `qualify`, `clean_text_for_sql` and
  `make_revert_command` are each used in a dozen f-string SQL sites where the
  return value is concatenated, not checked. A `None` or an unescaped quote
  doesn't raise — it builds wrong SQL.
- `stage_path` threads through `ingest` → layout → vision → `hybrid_repair` →
  QA render. Phase 3.8 splits it into `stage_path` + `render_stage`; miss one site
  and images land in the docs stage again.
- `PDF_NAME` is ~40 sites. `QueryLog.to_dict()`'s keys are the public API of
  every command.
- `table_comment.write()` is a read-modify-write. Every new writer must merge,
  never replace, or it drops another stage's data.

For any change to shared code:

1. Search every call site first — the whole `procedure/` tree plus `tests/`.
2. Read what each site *does with the value*. A falsy check degrades fine;
   string interpolation into SQL does not.
3. Keep a running list of every site you touched and why. That list **is** the
   commit body.
4. Fix every affected site in the same change. Fixing one and leaving three
   silently broken is worse than not starting — it looks done.
5. Scale it to real risk. A rename with one call site doesn't need a
   paragraph. Anything in `constants.py`, `_shared.py`, `query_log.py`,
   `render.py` or `table_comment.py` always does.

### Verify before calling it done

- Run T0+T1 (`pytest procedure\tests -m "not live"`) — fast, offline, catches
  a bundled-import regression before a deploy does.
- For anything touching `constants.py`, `_shared.py`, `poppler_bootstrap.py`,
  `render.py` or any SQL-building helper, also run T2 by hand. The offline
  suite mocks the session, so "it imported fine" and "it works against
  Snowflake" are different claims — and in this codebase they diverge. Every
  🔴 in Appendix A is a case where the offline tests are green and the code
  cannot work.
- If a change touches anything `API.md`, `README.md` or `ARCHITECTURE.md`
  documents — a default, a command's behaviour, an error message — update the
  doc in the same sitting. A code fix that leaves the docs describing old
  behaviour is its own ripple effect, and it's the one an API consumer hits.

### Engineering defaults

- Prefer the smallest correct change. Don't add abstraction, configurability
  or defensive handling for states that cannot occur.
- Docstrings explain *why*, not what; comments are rare and reserved for
  non-obvious constraints. `poppler_bootstrap.py`'s module docstring — which
  explains the zipimport/ELF/ld-linux problem before showing the fix — is the
  tone to copy.
- **Never swallow an exception without recording it in `warnings`.** The
  prototype's worst defects (I1, I6, Q3, D5) are all bare `except: pass`.
- **Never return `success: true` when data may have been lost.**
- A failure in bookkeeping (lease, table comment, reindex) must never fail the
  caller's actual work. Warn, name the fix, carry on. The one exception is a
  lease that is *held by someone else* — that is a real answer, not a
  bookkeeping failure, and it must refuse.

### Versioning

`procedure/utils/__init__.py` holds `__version__`. Bump the **patch** digit
whenever behaviour changes; minor/major are the maintainer's call. The version
appears in the bundle filename, `MANIFEST.json`, every `QUERY_TAG`, the table
comment and every response — it's how a deployed behaviour is traced back to a
commit.

---

## Appendix A — Defect register

Everything found by reading the prototype. 🔴 = structural, the code cannot
work. 🟠 = strongly suspected, carries a verification step. Nothing here has
been *observed* failing against Snowflake, because the prototype has never
been run against Snowflake.

### Build & deploy

| # | | Finding | Phase |
|---|---|---|---|
| B1 | 🔴 | `build_bundle.py --arches x86_64` needs host `ldd` + `poppler-utils`; `build_arm_poppler.py` needs `dpkg-deb`, `readelf`, `file`. None exist on Windows; no WSL, no Docker. The bundle cannot be rebuilt on the dev machine. | 1 |
| B2 | 🔴 | Snowflake caches procedure `IMPORTS` per warehouse. Re-`PUT`ting a same-named zip doesn't reliably refresh a live procedure. Nothing addresses this, so every iteration risks testing stale code. | 1 |
| B3 | 🟠 | No deploy script. The README says "run the three SQL files in Snowsight" — not an iteration loop. | 1 |
| B4 | 🟠 | `_render_sql()` needs `jinja2` but the templates only use `{{VAR}}`. | 1 |
| B5 | — | x86_64 poppler comes from the build host, so the bundle isn't reproducible. | 1 |
| B6 | — | No version stamp anywhere. Impossible to tell which bundle a deployed procedure runs. | 2 |

### Structure

| # | | Finding | Phase |
|---|---|---|---|
| S1 | 🟠 | `init_table.py`, `surgical_delete.py`, `parse_pdf.py`, `build_chunk_ref.py` are imported by **nothing** — their logic was inlined. Still bundled, still documented as live sub-handlers, and `test_bundle_e2e.py` asserts their presence, so docs and tests actively protect dead code. | 2 |
| S2 | — | `save_optimized_image` defined **three times** with divergent RGBA→RGB ordering. | 2/3 |
| S3 | — | `utils/__init__.py` documents an import path (`from utils.init_table import run`) that is wrong — the package is `chunky_utils`. | 2 |
| S4 | — | `ARCHITECTURE.md`'s "legacy two-bundle layout" and `build_poppler_bundle.sh` describe a deprecated path. | 2 |
| S5 | — | Tests live in `chunky/tests/` while `procedure/README.md` claims the directory is "fully self-contained". | 2 |

### Ingest

| # | | Finding | Phase |
|---|---|---|---|
| I1 | 🔴 | `_run_layout_extraction`'s per-batch insert is wrapped in `except Exception: ROLLBACK` that appends **no warning** and does not abort. A failing insert silently drops a batch and `cmd_ingest` still returns `success: true`. Silent data loss — the exact failure Chunky exists to prevent. | 3.2 |
| I2 | 🟠 | Staging table is a **permanent** `TEMP_CHUNKS_<uuid>` in the caller's schema, orphaned if the SP dies. Should be `CREATE TEMPORARY TABLE`. | 3.3 |
| I3 | 🟠 | Explicit `BEGIN`/`COMMIT` inside a Python SP; Snowflake has no nested transactions. Needs a live test. | 3.4 |
| I4 | 🟠 | Vision writes page images into the **source docs stage** and never cleans up. **Already observed** on `@SBOX_DB.AI_SB.DOCS` (`docs/_temp_images/`, `docs/_temp_audit/`). | 3.8 |
| I5 | — | `standard_cnt` counts *page records*, not inserted chunks — wrong whenever a page exceeds `chunk_size`. | 3.6 |
| I6 | — | `run_cortex` swallows every exception into `("", 0, 0)`. A quota error, a bad model name and an empty page are indistinguishable. | 3.7 |
| I7 | 🟠 | **Three** hardcoded `if role == "IT_AI": skip` copies, justified by a Streamlit-era comment that is false here: `EXECUTE AS CALLER` means `IT_AI` only *owns* the procedures. Delete all three. | 2 |
| I8 | — | Vision is strictly serial, one `AI_COMPLETE` per page (~8–12 s). Must be stated in the API contract; it's why ingest is async. | 6 |
| I9 | — | Caller-supplied identifiers interpolated into f-string SQL with no validation. | 3.9 |
| I10 | 🟠 | Table shape superseded by decision 4 — touches `CREATE TABLE`, both insert paths, every `SELECT`, surgical metadata, QA, hybrid repair, and the search DDL. | 3.1 |
| I11 | — | `QueryLog.execute` fires `SELECT LAST_QUERY_ID()` after **every** statement, and `to_dict()` fires another timestamp query — roughly doubling statement count. | 3.11 |

### QA

| # | | Finding | Phase |
|---|---|---|---|
| Q1 | 🔴 | `cmd_search` renders a screenshot **per returned chunk**. At `limit: 100` that's 100 PDF downloads, 100 poppler renders, 100 stage PUTs and 100 presigns in one call. | 4.1/4.2 |
| Q2 | — | No `sign_off` command at all. | 4.4 |
| Q3 | — | `render_page_screenshot` reports failures via `print()` only; the reason never reaches the caller. | 4.3 |
| Q4 | — | `get_silver_bullet_prompt` defined in **both** `prompts.py` and the QA handler; the handler uses its own copy, so edits to `prompts.py` don't affect QA. | 4.3 |
| Q5 | — | `cmd_commit` returns `success: true` even when every commit errored. | 4.3 |

### Deploy

| # | | Finding | Phase |
|---|---|---|---|
| D1 | 🔴 | **`cmd_create` never emits `WAREHOUSE`**, which Snowflake requires in both DDL forms. The Deploy stage cannot work. Clause order is also fixed. | 5.1 |
| D2 | 🔴 | No readiness check. `CREATE` returns before indexing completes; "ready to serve" is unimplemented. | 5.3/5.4 |
| D3 | 🟠 | `SHOW CORTEX SEARCH SERVICES LIKE '<n>' IN SCHEMA` with no schema name — only valid when a current schema is set, which `EXECUTE AS CALLER` doesn't guarantee. | 5.3 |
| D4 | 🟠 | `DESCRIBE CORTEX SEARCH SERVICE IDENTIFIER('<fqn>')` — verify `IDENTIFIER()` is accepted here. | 5.7 |
| D5 | 🟠 | `GET_DDL('CORTEX_SEARCH_SERVICE', …)` — verify the object type is supported. `revert` depends on it and it currently fails silently to `None`. | 5.7 |
| D6 | — | `cmd_revert` splits saved DDL on `;`, corrupting any `AS (<query>)` containing one. | 5.7 |
| D7 | — | Generated DDL appends `;` inside the string passed to `session.sql()`. | 5.1 |
| D8 | — | No `CHANGE_TRACKING` verification on source tables. | 5.2 |
| D9 | — | `PRIMARY KEY`, `REFRESH_MODE`, `INITIALIZE`, `AUTO_SUSPEND`, `COMMENT` all supported and all unexposed. | 5.1 |
| D10 | — | No embedding-model validation; availability is region-dependent. | 5.1 |
| D11 | — | Grant failures on the service swallowed by `except Exception: pass`. | 2 |

### Cross-cutting

| # | | Finding | Phase |
|---|---|---|---|
| X1 | — | Response envelope hand-built in ~25 places and already drifting (`list_chunks_csv` drops `query_count`; `batch_ingest` omits the log fields). | 2 |
| X2 | — | No coordination of any kind: two concurrent ingests into one table interleave page numbers, and `OVERWRITE` destroys rows another job is mid-insert on. Silent corruption. | 6 |
| X4 | 🟠 | **`cmd_ingest` imports `ok`/`err` from `_shared` but never calls them** — every `return` is still a hand-built dict, so the shipped response has no `run_id`, `remedy`, `next` or `bundle_version`. Confirmed by grep against the deployed handler: zero call sites. The envelope work is written but not connected. | 3 |
| X5 | 🟠 | `run_id=inst.get("run_id", "")` silently becomes an empty string when the caller omits it — visible live in the table comment as `"last_run_id":""`. Must be `inst.get("run_id") or ulid.run_id()`. | 3 |
| X3 | — | Tests mock the Snowpark session entirely; nothing has ever touched Snowflake. | 13 |

---

## Appendix B — File disposition

| File | Disposition |
|---|---|
| `deploy/winkeyring.py` `auth.py` `config.py` `sqlsplit.py` `sfapi.py` `sf.py` `README.md` `config.example.json` | ✅ **built and verified** |
| `deploy/preflight.py` `deploy.py` `bootstrap.sql` `smoke_test.py` `api_smoke.py` `jobs/` | **new** — Phases 0/1/3/6 |
| `build/build_bundle.py` `debfetch.py` `elfdeps.py` `render_sql.py` | **new** — replaces `build_bundle.py`, `build_arm_poppler.py`, `build_poppler_bundle.sh` |
| `utils/ulid.py` `table_comment.py` `reindex.py` `render.py` `locks.py` `registry.py` | **new** |
| `utils/chunky_chunks_handler.py` | → `chunky_ingest_handler.py`, rewritten per Phase 3 |
| `utils/chunky_searchservice_handler.py` | → `chunky_deploy_handler.py`, rewritten per Phase 5 |
| `utils/chunky_qa_handler.py` | keep, rewritten per Phase 4 |
| `utils/constants.py` | keep; drop `DEFAULT_DB`/`DEFAULT_SCHEMA`, add the §Phase 2.3 constants |
| `utils/_shared.py` | keep; add `ok`/`err`, `safe_identifier`, `safe_stage_path`, `require`, unified `safe_role` |
| `utils/query_log.py` | keep; make capture opt-out |
| `utils/revert.py` | keep; live-test the rename+clone pattern |
| `utils/page_mapping.py` `layout_parse.py` `metadata_handler.py` `quality_inspector.py` `prompts.py` `hybrid_repair.py` | keep |
| `utils/poppler_bootstrap.py` | keep — the best-reasoned module in the prototype |
| `utils/grant_table.py` | keep; delete the `IT_AI` skip |
| `utils/init_table.py` `surgical_delete.py` `parse_pdf.py` `build_chunk_ref.py` | **delete** — dead. Diff `surgical_delete.py` against the inlined copy first |
| `build_arm_poppler.py` `build_bundle.py` `build_poppler_bundle.sh` | **delete** (logic lifted into `build/`) |
| `chunky_chunks.sql` `chunky_qa.sql` `chunky_searchservice.sql` | **delete** — generated artifacts belong in `build/out/` |
| `templates/*.sql.j2` | keep, renamed. **No compat-alias template** (decision 2) |
| `script/upload_to_stage.py` | **delete** — `sf put`/`ls`/`get` cover it; a second auth path is a liability |
| `script/make_dummy_pdf.py` `script/pdf/*.pdf` | keep — the fixture is the smoke-test input |
| `utils/README.md` | **delete** — fold into `ARCHITECTURE.md` |
| `README.md` `ARCHITECTURE.md` | **rewrite** in Phase 7 |
| `utils_bundle.zip` (22 MB, tracked in git) | **untrack** (`git rm --cached`) and git-ignore |
| `snowflake-mcp/` | out of scope — leave it, don't extend it |
| `streamlit_app.py`, top-level `utils/`, `views/` | **do not touch** — the Streamlit app is being retired separately |
