# Analysis: Ingestion Features Missing from Create Cortex Search (CCS)

**Date:** 2026-07-21
**Status:** Awaiting approval — NOT implemented yet

---

## Executive Summary

The Doc Refinery (Ingestion) pipeline has **13 features** that the Create Cortex Search (CCS) wizard lacks. This document catalogs each gap, explains the impact, and proposes how to add each to CCS.

---

## Feature Gap Analysis

### 1. Auto-Fill Table Name from PDF (Page 2)

**Ingestion has:** User manually types the table name (default `SUS_CHUNKS`).
**CCS lacks:** Same — user manually types the table name.

**Proposed addition:** When a PDF is selected in Page 2, auto-fill the "Target Table Name" field with the normalized PDF name:
- Strip `.pdf` extension
- Convert to ALL CAPS
- Replace all non-alphanumeric characters (except `_`) with nothing
- Replace all spaces with `_`
- Collapse consecutive underscores
- Strip leading/trailing underscores

**Example:** `My Report (2024).pdf` → `MY_REPORT_2024`

**Implementation:**
- Add a `_normalize_pdf_to_table_name(filename: str) -> str` function in `views/demo/common.py`
- In `page2_builder.py`, after PDF selection, call `jbsync("table_name", normalized_name)` if the current table name is the default (`SUS_CHUNKS`) or empty
- User can still override manually

**Files to modify:** `views/demo/common.py`, `views/demo/page2_builder.py`

---

### 2. Session Memory Warning Banner (Page 3)

**Ingestion has:** A prominent warning banner when `chunk_cache` reaches 80%/90%/100% capacity, with a "Clear In-Memory Chunks" button. Located at the top of `tab_ingestion.py`.

**CCS lacks:** No memory monitoring at all.

**Proposed addition:** Add the same banner to the top of Page 3 (Execute), before the job summary. Copy the exact pattern from `tab_ingestion.py` lines 11-35.

**Files to modify:** `views/demo/page3_execute.py`

---

### 3. Surgical Mode (Page 2 + Page 3)

**Ingestion has:** Full SURGICAL write mode with:
- Page range mapping UI (`surgical_ui.py`)
- RangeMappingEngine integration
- Surgical delete with transaction safety (BEGIN/COMMIT/ROLLBACK)
- Bottom-up multi-range processing
- Range bounds filtering in layout strategy
- Fallback for RELATIVE_PATH data migration

**CCS lacks:** Only APPEND and OVERWRITE modes. No surgical support at all.

**Proposed addition:**
- Add "SURGICAL" to the Write Mode radio options in Page 2
- Import and call `render_range_mapping_section()` from `views/refinery/surgical_ui.py`
- In Page 3 execution, the existing `batch_processor.py` already handles surgical mode — CCS just needs to pass the right job dict keys
- Validate SURGICAL mode requires existing table (same as Ingestion)

**Files to modify:** `views/demo/page2_builder.py`, `views/demo/page3_execute.py`

---

### 4. Duplicate Page Detection (Page 2)

**Ingestion has:** When mode is APPEND and the table exists, queries the target table for existing pages matching the selected file/range. Shows a warning like "⚠️ Possible Duplicate Pages Detected (5 total): Pages 1, 2, 3, 4, 5 already exist..."

**CCS lacks:** No duplicate detection. Users can silently create duplicate content.

**Proposed addition:** After the "Add Job" button area in Page 2, when mode is APPEND and table exists, run the same duplicate-check SQL from `tab_config.py` lines 220-245.

**Files to modify:** `views/demo/page2_builder.py`

---

### 5. Report Dashboard (Page 3)

**Ingestion has:** A comprehensive Report Dashboard after batch execution with two tabs:
- **Overview tab:** Success rate, warnings, processed pages, performance metrics (total time, avg speed, layout/vision speed, page coverage), chunk statistics (total chunks, avg size, total tokens, avg tokens/chunk), cost estimation (layout/vision/total with credit cards), data yield (standard/enhanced chunks with progress bar)
- **Details tab:** Per-job inspection with status, pages, duration, speed, target roles, grant status, skipped page ranges, defect details by page, page coverage map, cost breakdown, data yield, CSV download

**CCS lacks:** Only basic per-job expanders with simple metrics. No aggregate dashboard.

**Proposed addition:** Add a `_render_report_dashboard()` function to Page 3 that mirrors `tab_ingestion.py` lines 130-320. Reuse the existing `batch_audit` session state.

**Files to modify:** `views/demo/page3_execute.py`

---

### 6. Job Management Controls (Page 2 & 3)

**Ingestion has (Page 2 - Job Queue Workbench):**
- Select/Deselect individual jobs via checkbox
- Delete Selected Jobs button
- Clear Queue button
- Inline editing of Mode, Scope, PDF Link, Roles, L/V flags
- Sync logic with validation for scope changes

**CCS has (Page 2):** ✅ Already has all of these (copied from Ingestion).

**Ingestion has (Page 3 - Ingestion Tab):**
- Styled DataFrame with status-based row coloring (green=Completed, red=Failed, yellow=Warning, blue=Running)
- Collapsible job queue with all statuses

**CCS lacks (Page 3):** No styled DataFrame, no status-based coloring, no collapsible queue.

**Proposed addition:** Add styled DataFrame rendering to Page 3's job workbench, matching `tab_ingestion.py` lines 60-85.

**Files to modify:** `views/demo/page3_execute.py`

---

### 7. Grant Status Indicator (Page 3)

**Ingestion has:** Per-job grant status tracking with visual indicators:
- ✅ Grants: Success (green)
- ❌ Grants: Failed (red)
- ℹ️ Grants: N/A (gray)
- Shows target roles and failed roles

**CCS lacks:** No grant status tracking after ingestion.

**Proposed addition:** The batch_processor already populates `job['grant_status']`. Page 3 just needs to render it in the job details expander.

**Files to modify:** `views/demo/page3_execute.py`

---

### 8. Defect Details by Page (Page 3)

**Ingestion has:** Per-job "Auto-Fixed Defects by Page" expander showing:
- Page number → defect types and statuses
- Total defect count across pages

**CCS lacks:** No defect visibility.

**Proposed addition:** The batch_processor already populates `job['metrics']['defects_detail']`. Page 3 just needs to render it.

**Files to modify:** `views/demo/page3_execute.py`

---

### 9. Page Coverage Map (Page 3)

**Ingestion has:** Per-job "Page Coverage (Layout / Vision)" expander showing:
- Each page number with ✅/❌ for Layout and Vision
- Summary: Layout N pages, Vision N pages, Total unique N pages

**CCS lacks:** Only shows aggregate counts (Layout Pages, Vision Pages), not per-page breakdown.

**Proposed addition:** The batch_processor already populates `job['metrics']['layout_pages_list']` and `job['metrics']['vision_pages_list']`. Page 3 just needs to render the per-page breakdown.

**Files to modify:** `views/demo/page3_execute.py`

---

### 10. Observability / Lineage Tracking

**Ingestion has:** `Accumulator` and `observe()` from `observability.py` that tracks:
- Activity-level execution timing
- Lineage metadata attached to job metrics
- Enabled via `st.session_state.observability_enabled`

**CCS lacks:** No observability integration.

**Proposed addition:** The batch_processor already uses the Accumulator. CCS just needs to expose the observability toggle (optional — low priority).

**Files to modify:** None (already works via batch_processor). Optional: add toggle to Page 3 UI.

---

### 11. Query Tagging (Session-Level)

**Ingestion has:** `set_query_tag()` called once per session to tag all Snowflake queries with user/app/db/schema/role metadata for warehouse attribution.

**CCS lacks:** No query tagging.

**Proposed addition:** The query tag is set in `streamlit_app.py` at the main session level. Since CCS runs within the same Streamlit app, it already benefits from the query tag set by the main app. **No change needed** — it's already inherited.

**Files to modify:** None (already works).

---

### 12. CSV Export (Page 3)

**Ingestion has:** Per-job "Download Results as CSV" button that exports chunks from `chunk_cache` with columns: CHUNK_ID, CHUNK, CHUNK_TYPE, PAGE_NUMBER, RELATIVE_PATH, CHUNK_REF, LINK_BLOCK.

**CCS lacks:** No CSV export.

**Proposed addition:** Add a download button to each completed job's results expander in Page 3. Read from `st.session_state.chunk_cache` filtered by `job_id`.

**Files to modify:** `views/demo/page3_execute.py`

---

### 13. QA Studio / Tools Tab

**Ingestion has:**
- **QA Tab** (`tab_qa.py`): Chunk inspection with PDF page rendering, draft editor, batch generation, commit/delete operations, workbench with multi-select
- **Tools Tab** (`tab_tools.py`): Temp stage cleanup, Shift Engine Self-Test with synthetic data

**CCS lacks:** No QA or Tools functionality.

**Proposed addition (low priority):** These are complex standalone tabs. For CCS, consider:
- Adding a simple "Inspect Table" button on Page 3 that shows a sample of chunks from the created table
- Adding a link/note to use the main Doc Refinery's QA tab for full inspection

**Files to modify:** `views/demo/page3_execute.py` (minimal), or defer to main Doc Refinery

---

## Priority Matrix

| # | Feature | Impact | Effort | Priority |
|---|---------|--------|--------|----------|
| 1 | Auto-Fill Table Name | Medium | Low | **P1** |
| 2 | Session Memory Warning | High | Low | **P1** |
| 3 | Surgical Mode | High | High | **P2** |
| 4 | Duplicate Page Detection | Medium | Low | **P1** |
| 5 | Report Dashboard | High | Medium | **P1** |
| 6 | Job Mgmt (Page 3 styling) | Low | Low | **P2** |
| 7 | Grant Status Indicator | Medium | Low | **P1** |
| 8 | Defect Details | Medium | Low | **P1** |
| 9 | Page Coverage Map | Medium | Low | **P2** |
| 10 | Observability | Low | None | **P3** (already works) |
| 11 | Query Tagging | Low | None | **P3** (already works) |
| 12 | CSV Export | Medium | Low | **P1** |
| 13 | QA Studio / Tools | Low | High | **P3** (defer) |

---

## Recommended Implementation Order

**Phase 1 (Quick Wins — P1):**
1. Auto-Fill Table Name from PDF (#1)
2. Session Memory Warning Banner (#2)
3. Duplicate Page Detection (#4)
4. Report Dashboard (#5)
5. Grant Status Indicator (#7)
6. Defect Details (#8)
7. CSV Export (#12)

**Phase 2 (Medium — P2):**
8. Surgical Mode (#3) — largest effort, requires surgical_ui integration
9. Job Management styling on Page 3 (#6)
10. Page Coverage Map (#9)

**Phase 3 (Defer — P3):**
11. Observability toggle (already works via batch_processor)
12. Query Tagging (already inherited from main app)
13. QA Studio / Tools (defer to main Doc Refinery)
