# Chunky

A high-fidelity Retrieval-Augmented Generation (RAG) pipeline deployed as a Snowflake Native App. Chunky specializes in converting complex PDF documents into structured, searchable data using a combination of structural layout parsing and multi-modal Vision AI.

**Two deployment modes:**
- **Snowflake Mode** — Full production deployment with Cortex AI, Cortex Search, and Snowpark
- **Local Mode** — Standalone development/testing version using SQLite (Windows/Linux/macOS)

---

## 1. Project Overview

### Operational Purpose
Chunky transforms unstructured PDF files stored in Snowflake stages into high-fidelity Markdown chunks. It solves the "PDF-to-RAG" gap by enforcing a strict 1-chunk-per-page minimum and providing "Surgical Mode" for precise document restructuring.

### Deployment Modes

| Feature | Snowflake Mode | Local Mode |
|---------|---------------|------------|
| Database | Snowflake SQL | SQLite (`chunky_local.db`) |
| AI Parsing | `AI_PARSE_DOCUMENT` + Vision LLM | Simulated text extraction |
| Search | Cortex Search Services | SQLite `LIKE` search |
| Auth | Gatekeeper + RBAC | Local admin (no auth) |
| PDF Rendering | `pdf2image` + poppler | Not available |
| Entry Point | `streamlit run streamlit_app.py` | `streamlit run streamlit_app_local.py` |
| Requirements | `requirements.txt` | `requirements_local.txt` |

### Problem Solved
- **OCR Fidelity Gaps**: Combines structural parsing via `AI_PARSE_DOCUMENT` with visual extraction via Vision LLMs to handle complex tables and layouts.
- **Silent Data Loss**: Prevents pages from being omitted from the index by generating synthetic `PLACEHOLDER` chunks when AI extraction fails [`views/refinery/ingestion_strategies/layout.py`](views/refinery/ingestion_strategies/layout.py).
- **Rigid Metadata**: Implements "Surgical Range Mapping" to replace specific page ranges $[s_s, s_e]$ with new content, automatically shifting all downstream pages by the calculated delta $\text{delta} = (r_e - r_s + 1) - (s_e - s_s + 1)$ [`utils/page_mapping.py`](utils/page_mapping.py).
- **Cortex Cost Opacity**: Provides real-time credit estimation and historical cost tracking based on actual token usage and model-specific pricing [`utils/core_utils.py:346`](utils/core_utils.py:346).
- **Blocking UI**: Overcomes Streamlit's single-threaded model by using a "one-job-per-rerun" driver, allowing users to cancel long-running batches in real-time [`views/refinery/batch_processor.py`](views/refinery/batch_processor.py).

### Explicit Non-Goals
- **Non-PDF Formats**: Does not support `.docx`, `.html`, or `.txt` files.
- **Local OCR**: Relies entirely on Snowflake Cortex; no local Tesseract or PyMuPDF text extraction.
- **Multi-Tenancy**: Does not manage tenants; relies on Snowflake's native RBAC.
- **Persistent Chat**: Chat history is session-only and capped at 30 messages [`streamlit_app.py:43`](streamlit_app.py:43).

---

## 2. High-Level Architecture

### Major Modules & Responsibilities
- **Orchestration**: `streamlit_app.py` routes requests; `views/refinery/batch_processor.py` manages the ingestion job queue and the non-blocking execution loop.
- **Ingestion Engine**: `views/refinery/ingestion_strategies/` contains modular logic for `layout`, `vision`, and `hybrid` (repair) parsing.
- **Surgical Engine**: `utils/page_mapping.py` (Range calculation) and `views/refinery/ingestion_core.py` (Surgical DELETE/UPDATE shift) manage document restructuring.
- **Snowflake Interface**: `utils/snowflake_utils.py` wraps Snowpark sessions and Cortex AI calls.
- **Core Utilities**: `utils/core_utils.py` handles PDF image rendering, token counting, and financial calculations.
- **Auth/Gatekeeper**: `utils/auth_utils.py` validates user identities and mapping roles.

### Data Flow
1. **Authentication**: User $\rightarrow$ `auth_utils` $\rightarrow$ Snowflake Session $\rightarrow$ `auth_context`.
2. **Surgical Tagging**: Session $\rightarrow$ `set_query_tag` $\rightarrow$ Snowflake `QUERY_TAG` [`utils/snowflake_utils.py:45`](utils/snowflake_utils.py:45).
3. **Ingestion**: PDF $\rightarrow$ Strategy (Layout/Vision) $\rightarrow$ Chunk Generation $\rightarrow$ Snowflake Table.
4. **Surgical Shift**: Range Mapping $\rightarrow$ Bottom-up DELETE of source range $\rightarrow$ UPDATE shift of downstream pages $\rightarrow$ Ingestion of replacement pages.
5. **Hybrid Repair**: Defective Chunk $\rightarrow$ Vision AI $\rightarrow$ `ENHANCED` Chunk.
6. **RAG Query**: User Query $\rightarrow$ Cortex Search $\rightarrow$ Context Chunks $\rightarrow$ LLM $\rightarrow$ Response.

### Execution Model
- **Runtime**: Synchronous Streamlit application.
- **Processing**: One-Job-Per-Rerun Driver. Instead of a blocking `for` loop, the system processes a single job and triggers `st.rerun()`. This yields control back to the Streamlit UI thread, enabling responsive "Stop Batch" functionality [`views/refinery/batch_processor.py`](views/refinery/batch_processor.py).

---

## 3. Repository Structure

| Path | Purpose | Why it exists |
| :--- | :--- | :--- |
| `streamlit_app.py` | Main Entry Point | Central router and session state manager. |
| `utils/` | Shared Logic | Low-level helpers for Snowflake, PDF, and Auth. |
| `utils/page_mapping.py` | Range Mapping Logic | Calculates deltas and maps source $\rightarrow$ target pages. |
| `utils/core_utils.py` | Core Math/PDF | Centralizes `PRICING_REGISTRY` and `PDFUtils`. |
| `utils/snowflake_utils.py` | Cortex Wrappers | Isolates Snowpark/Cortex API calls. |
| `views/` | UI Layers | Decouples Streamlit views from business logic. |
| `views/refinery/` | Ingestion Pipeline | Dedicated namespace for the "Refinery" (Ingestion $\rightarrow$ QA). |
| `views/refinery/batch_processor.py` | Batch Driver | Implements the non-blocking job execution cycle. |
| `views/refinery/batch_exceptions.py` | Error Definitions | Houses `BatchCancelledError` to prevent circular imports. |
| `views/refinery/ingestion_core.py` | SQL Core | Implements surgical shifts and table initialization. |
| `views/refinery/ingestion_strategies/` | Parsing Logic | Modularizes Layout, Vision, and Hybrid strategies. |
| `views/refinery/surgical_ui.py` | Range Configuration | UI for defining surgical page replacements. |
| `prompts.py` | Prompt Registry | Prevents hardcoding AI instructions in views. |
| `logger_config.py` | Audit Log | Centralizes `log_action` for system observability. |
| `streamlit_app_local.py` | Local Entry Point | Standalone local mode with SQLite backend. |
| `utils/local_db_utils.py` | SQLite Database Layer | Replaces Snowflake operations for local development. |
| `views/qastudio.py` | QA Studio (shared) | Chunk inspection, draft editing, PDF rendering — used by both CCS wizard and Doc Refinery |
| `views/ccs/` | Create Search Service Wizard | 5-page guided wizard. `wizard.py` contains all logic, copies patterns from Doc Refinery. |
| `requirements_local.txt` | Local Dependencies | Minimal deps for local mode (no Snowflake). |
| `procedure/` | Headless Stored Procedures | Self-contained Snowflake procedures (no Streamlit dependency). See [`procedure/README.md`](procedure/README.md). |
| `procedure/utils/` | Procedure Handler Modules | Pure-Python source of truth for every procedure's runtime logic, bundled into `utils_bundle.zip` for Snowflake IMPORTS. |
| `procedure/script/` | Local Helper Scripts | Standalone CLIs (not procedures) — browser-auth file uploader and the dummy-PDF generator. |
| `procedure/script/pdf/` | Test Fixtures | 5-page dummy investor-presentation PDF for end-to-end ingestion tests. |
| `procedure/templates/` | SQL Templates | `.sql.j2` templates that `build_procedures.py` renders into the deployable `.sql` files. |

**Note on Layout**: The monolith `views/refinery/ingestion_strategies.py` was eradicated to prevent module resolution conflicts. Logic is now strictly in the `ingestion_strategies/` package.

---

## 4. Core Concepts & Domain Model

### Key Abstractions
- **Job**: A unit of work defining a source file, page range, and extraction strategy.
- **Surgical Range Mapping**: A mapping where a source page range $[s_s, s_e]$ is replaced by a new range, shifting all subsequent pages by the difference in size.
- **Chunk**: The atomic unit of RAG.
    - `STANDARD`: Layout-parsed.
    - `ENHANCED`: Vision-repaired.
    - `PLACEHOLDER`: Synthetic (ensures page coverage).

### Domain Glossary
- **Surgical Shift**: The process of deleting a specific page range and shifting all subsequent `PAGE_NUMBER` values in Snowflake to maintain document continuity.
- **Bottom-Up Processing**: Sorting range mappings by `source_end` descending to ensure shifts do not invalidate subsequent deletions in the same batch.
- **Hybrid Repair**: The process of using a Vision LLM to fix a structural defect in a Layout-parsed chunk.
- **Cortex Search**: Snowflake's native vector search service used for context retrieval.
- _Avoid_: "OCR" (The system uses AI-parsing/Vision, not traditional OCR).

---

## 5. Detailed Behavior

### Normal Execution (Ingestion)
1. **Job Definition**: User configures a job in `tab_config.py`.
2. **Surgical Mapping**: If enabled, users map source ranges $\rightarrow$ replacement files/pages via `surgical_ui.py`.
3. **Initialization**: `batch_processor.py` ensures the target table exists and is `CHANGE_TRACKING` enabled.
4. **Surgical Delete (if applicable)**:
    - `RangeMappingEngine` computes deltas [`utils/page_mapping.py:15`](utils/page_mapping.py:15).
    - `_execute_surgical_delete_with_shift` wraps multi-range DELETEs in an explicit Snowflake transaction (`BEGIN`/`COMMIT`/`ROLLBACK`) for atomicity [`views/refinery/ingestion_core.py:121`](views/refinery/ingestion_core.py:121).
5. **Extraction**:
    - **Layout**: Calls `AI_PARSE_DOCUMENT`. Applies range bounds filter via `RangeMappingEngine.target_page_for()` — pages outside replacement ranges are skipped. If pages are missing, generates `PLACEHOLDER` chunks.
    - **Vision**: Renders PDF to images $\rightarrow$ calls `claude-haiku-4-5`.
6. **Commit**: Data is written to Snowflake in batches.

### Batch Cancellation
- The `batch_processor` processes one job, checks `st.session_state.cancel_batch`, and calls `st.rerun()`.
- Strategy files (e.g., `layout.py`) contain checkpoints that raise `BatchCancelledError` if the cancel flag is detected [`views/refinery/ingestion_strategies/layout.py:136`](views/refinery/ingestion_strategies/layout.py:136).

### Edge Cases & Failure Modes
- **Model Failure**: If a Vision call fails, the page is logged as `VISION_EXTRACTION_SKIPPED` and omitted.
- **Auth Expiry**: If the Snowflake session terminates, the UI prompts a refresh via `tab_config.py`.
- **Transaction Safety**: Multi-range surgical deletes use explicit `BEGIN`/`COMMIT`/`ROLLBACK` transactions. If any DELETE fails, all prior deletes in the job are rolled back to prevent partial corruption [`views/refinery/ingestion_core.py:121`](views/refinery/ingestion_core.py:121).
- **Session Bloat**: `chunk_cache` is capped at 5,000 entries to prevent Streamlit memory crashes [`utils/core_utils.py`](utils/core_utils.py).
- **PDF Page Detection**: `PDFUtils.get_page_count` uses a two-tier fallback: poppler (`pdfinfo_from_bytes`) first, then `pypdf` (pure Python). All failures are logged via `log_action` for diagnosability [`utils/core_utils.py`](utils/core_utils.py).
- **Surgical PDF Page Mapping**: After surgical replacement, `PAGE_NUMBER` values in the table are shifted from the original PDF page numbering. Chunks store `original_pdf_page` in `CHUNK_METADATA.surgical.page_mappings` so QA Studio renders the correct PDF page image [`views/refinery/tab_qa.py`](views/refinery/tab_qa.py).
- **Surgical Range Validation**: The Surgical UI validates that source and replacement ranges are within bounds: `source_start`/`source_end` are clamped to the actual min/max `PAGE_NUMBER` in the target table for the selected file; `replacement_start`/`replacement_end` are clamped to the replacement PDF's page count. Validation errors use `return` (not `st.stop()`) to halt only the fragment, not the entire page [`views/refinery/surgical_ui.py`](views/refinery/surgical_ui.py).
- **Range Bounds Filter**: Layout strategy uses `RangeMappingEngine.target_page_for()` to skip PDF pages that fall outside all replacement ranges. This prevents a 10-page replacement PDF from leaking pages outside the intended `[replacement_start, replacement_end]` bounds [`views/refinery/ingestion_strategies/layout.py`](views/refinery/ingestion_strategies/layout.py).
- **Page Coverage Tracking**: Job metrics track `layout_pages_list` and `vision_pages_list` (sets of page numbers) so the dashboard shows exactly which pages were processed by each strategy.

---

## 6. Public Interfaces

### User Interface (Streamlit)
| Tab | Input | Output | Side Effect |
| :--- | :--- | :--- | :--- |
| **Doc Refinery** | PDF Path, Strategy, Range | Job Queue, Progress Bar, Execution Dashboard | Data written to Snowflake |
| **QA Studio** | Table selector (from completed jobs), PDF Name filter, Page filter, Chunk selector | Chunk inspection (surgical-aware PDF rendering), Draft editor | `admin_queue` updated |
| **RAG Playground** | User Query, Model Selection | LLM Response, Retrieval Meta | `monitoring_logs` updated |
| **Cost Analytics** | Job Selection | Credit/USD/IDR breakdown | None |
| **Quality Analytics** | (None) | Defect distribution charts | None |

### Internal API
- `run_batch_execution(session, db, schema, stage_path)`: Non-blocking entry point for processing the job queue [`views/refinery/batch_processor.py`](views/refinery/batch_processor.py).
- `RangeMappingEngine.compute_delta(rm)`: Logic for calculating page shift offsets [`utils/page_mapping.py:15`](utils/page_mapping.py:15).
- `set_query_tag(session, auth_context)`: Sets the session `QUERY_TAG` for warehouse attribution [`utils/snowflake_utils.py:45`](utils/snowflake_utils.py:45).

---

## 7. State, Persistence, and Data

### Persistence Layer
- **Snowflake Tables**: All chunks, job metrics, and mapping histories are persisted as table rows.
- **Snowflake Stages**: Source PDFs are stored as files in internal/external stages.

### Session State (Transient)
- `auth_context`: Active DB, Schema, and User identity.
- `job_queue`: List of pending and completed jobs for the current session.
- `cancel_batch`: Boolean flag used to interrupt the `batch_processor` loop.
- `batch_audit`: Aggregate metrics dict from the most recent batch run; drives the Report Dashboard.
- `ingestion_history`: List of all completed/failed jobs across batch runs in the current session; used as fallback for the dashboard.
- `chunk_cache`: In-memory subset of chunks for fast QA rendering.
- `query_tag_set`: Boolean flag ensuring `set_query_tag` is called once per session.
- `jb_preset`, `jb_mode`, `jb_scope`, `jb_table_name`, `jb_chunk`, `jb_overlap`, `jb_layout`, `jb_vision`, `jb_link`, `jb_pstart`, `jb_pend`, `jb_file`: Persisted Job Builder fields — initialized via `setdefault()` so values survive tab navigation. Widget keys read from `_jbv_*` helper keys (source of truth) to avoid Streamlit's "widget value already set" conflict.

---

## 8. Dependencies & Integration

| Dependency | Purpose | Coupling Point |
| :--- | :--- | :--- |
| **Snowflake Cortex** | AI Processing | `utils/snowflake_utils.py` $\rightarrow$ `AI_COMPLETE` / `AI_PARSE_DOCUMENT` |
| **Snowpark** | DB Interaction | `utils/snowflake_utils.py` $\rightarrow$ `session.sql()` |
| **pdf2image** | PDF Rendering | `utils/core_utils.py` $\rightarrow$ `PDFUtils` |
| **Pillow** | Image Optimization | `utils/core_utils.py` $\rightarrow$ `save_optimized_image` |
| **Pandas** | Data Handling | `views/refinery/batch_processor.py` $\rightarrow$ Metric aggregation |

---

## 9. Setup, Build, and Execution

### Platform Assumptions
- Must be deployed as a **Snowflake Native App**.
- Requires a Snowflake environment with `poppler` installed (for `pdf2image`).

### Execution Steps
1. **Deployment**: Deploy via SnowCLI or Snowflake UI using `environment.yml`.
2. **Authentication**: Login via the Gatekeeper screen (email must be mapped in `auth_utils.py`).
3. **Configuration**: Select a target database and schema from the authenticated context.
4. **Ingestion**: Define a job $\rightarrow$ Run Batch $\rightarrow$ Validate in QA Tab.

---

## 10. Testing & Validation

### Validation Strategy
- **Human-in-the-loop (HITL)**: The **QA Tab** allows users to manually compare AI extracts against the original PDF.
- **Metric-Based**: The Ingestion Tab tracks "Success Rate" and "Processed Pages" to identify batch failures.
- **Surgical Health Check**: The **Tools Tab** implements a "Shift Engine Self-Test" that creates synthetic data in a temporary table to verify DELETE/UPDATE shift logic [`views/refinery/tab_tools.py`](views/refinery/tab_tools.py).
- **Invariant Check**: `UNCHUNKED_PAGES` logs are used to verify the 1-chunk-per-page rule.

### Coverage Gaps
- No automated unit tests for parsing logic; validation is purely manual/metric-based.
- No regression suite for "Surgical Mode" mapping beyond the health check tool.

### Automated Test Suite

The repository includes multiple automated test suites that run without Snowflake:

```bash
# Run all tests
python3 -m pytest tests/ -v

# Doc Refinery logic (range mapping math, surgical DELETE, constants)
python3 -m pytest tests/test_refinery.py -v

# CCS wizard structure (AST + flow simulation)
python3 -m pytest tests/test_wizard.py -v

# Real Streamlit E2E tests (AppTest framework — actually launches the app)
python3 -m pytest tests/test_streamlit_e2e.py -v

# Headless procedure utility layer (no Streamlit, no Snowflake)
python3 -m pytest tests/test_procedure_utils.py -v
```

| Suite | Purpose | What it catches |
|-------|---------|-----------------|
| `tests/test_refinery.py` | Doc Refinery logic | Range mapping math, surgical DELETE behavior, constants |
| `tests/test_wizard.py` | CCS wizard structure | Imports, syntax, AST-level anti-patterns (e.g. `value=` + `key=` combo), flow simulation for the Target Table Name auto-fill |
| `tests/test_streamlit_e2e.py` | Real Streamlit app launch | Actually runs `streamlit_app_local.py` via `AppTest`, verifies no exceptions, calls `page2_builder.render()` with a mock session |
| `tests/test_procedure_utils.py` | Headless procedure layer | Pure-function helpers, handler dispatch, revert logic, build-script output, upload-script arg parsing, dummy PDF validity |

---

## 11. Known Limitations & Non-Goals

- **Vision Latency**: Vision extraction is orders of magnitude slower than Layout parsing due to image rendering.
- **Cache Limits**: 5,000-chunk limit in `chunk_cache` prevents full review of massive documents in one session.
- **Cortex Limits**: Subject to Snowflake's account-level Cortex concurrency limits.
- **PDF Complexity**: Highly irregular tables may still require manual `Hybrid Repair`.
- **PDF Page Detection Fallback**: When `poppler` is unavailable or fails, `pypdf` (pure Python) is used as fallback. Both paths log errors explicitly — silent `return 1` defaults have been eliminated.

---

## 12. Change Sensitivity

| Component | Sensitivity | Risk of Modification |
| :--- | :--- | :--- |
| `ingestion_strategies/` | **High** | Changes to chunking, placeholders, or range bounds filter break the 1-chunk-per-page invariant. |
| `utils/page_mapping.py` | **High** | Incorrect delta calculations or `target_page_for` logic cause permanent data corruption. |
| `surgical_ui.py` | **High** | Using `st.stop()` instead of `return` halts the entire page, hiding the Ingestion tab. |
| `batch_processor.py` | **Medium** | Breaking the `st.rerun()` cycle will re-introduce UI blocking. |
| `auth_utils.py` | **Medium** | Errors in role mapping block all user access. |
| `core_utils.py` | **Medium** | Changes to `PRICING_REGISTRY` lead to incorrect financial reporting. |
| `views/` | **Low** | UI changes are generally isolated to specific tabs. |
| `utils/local_db_utils.py` | **Low** | SQLite utilities for local mode; isolated from Snowflake code. |
| `procedure/utils/` | **Medium** | Shared by all Snowflake procedures. Test changes via `tests/test_procedure_utils.py` before deploying. |
| `procedure/utils/revert.py` | **High** | Bugs in TIME TRAVEL logic could corrupt tables or fail to restore them. |
| `procedure/utils/query_log.py` | **Medium** | Incorrect query-ID capture breaks the revert command's ability to find prior operations. |

---

## 12.5. Headless Procedures (production deployment)

The Streamlit app is great for interactive use, but production workloads
call Chunky from MCP tools, scheduled jobs, and other Snowflake
procedures — none of which can drive a UI. The `procedure/` directory
contains the headless equivalents.

**Key differences vs. the Streamlit app:**

| Aspect | Streamlit app | Headless procedures |
|--------|---------------|---------------------|
| Configuration | `st.session_state` widgets | `instruction` JSON parameter |
| Warnings | Displayed BEFORE execution (modal) | Returned in JSON AFTER execution (`warning` field) |
| Revert | Manual re-run of ingest with reversed mappings | Native Snowflake TIME TRAVEL via `REVERT` command |
| Query tracking | None | Every SQL operation's query ID captured in response |
| Dependencies | Top-level `utils/`, `views/`, Streamlit | `procedure/utils/` only — fully self-contained |

**Procedure inventory:**

| Procedure | Commands |
|-----------|----------|
| `chunky_chunks` | `ingest`, `list_chunks`, `update_chunk`, `delete_chunks`, `revert` |
| `chunky_qa` | `search`, `inspect`, `generate_draft`, `commit`, `delete`, `revert` |
| `chunky_searchservice` | `create`, `list`, `describe`, `alter`, `drop`, `revert` |
| `chunky_internal_init_table` | (sub-procedure) CREATE TABLE IF NOT EXISTS |
| `chunky_internal_grant_table` | (sub-procedure) GRANT ALL PRIVILEGES with retry |
| `chunky_internal_surgical_delete` | (sub-procedure) Bottom-up DELETE in a transaction |
| `chunky_internal_parse_pdf` | (sub-procedure) AI_PARSE_DOCUMENT wrapper |
| `chunky_internal_build_chunk_ref` | (sub-procedure) Canonical CHUNK_REF builder |

**Revert flow:**

Every destructive operation returns a `revert` object in its JSON
response containing the pre-operation timestamp, the captured query
IDs, and a ready-to-run `CALL ...('REVERT', ...)` command string. The
caller can either re-run that command verbatim or pass the
`timestamp_before`/`query_ids` to a fresh `REVERT` call. Tables are
reverted via native Snowflake TIME TRAVEL (`CREATE OR REPLACE TABLE
... CLONE ... AT(TIMESTAMP => ...)`); Cortex Search Services are
reverted by re-executing the previously captured DDL.

See [`procedure/README.md`](procedure/README.md) for the quick-start
guide and [`procedure/ARCHITECTURE.md`](procedure/ARCHITECTURE.md) for
the full architecture.

---

## 13. Local Development & Testing

### Quick Start (Local Mode)

```bash
# 1. Install local dependencies (no Snowflake required)
pip install -r requirements_local.txt

# 2. Run the local Streamlit app
streamlit run streamlit_app_local.py

# 3. Open http://localhost:8501 in your browser
```

### Local Mode Features
- **SQLite Database**: All data stored in `chunky_local.db` (configurable via `CHUNKY_LOCAL_DB` env var)
- **Text Ingestion**: Paste text or upload `.txt`/`.md`/`.csv` files for chunking
- **QA Studio**: Inspect and edit chunks locally
- **RAG Playground**: Simulated search using text matching
- **Webapp Demo**: Native Streamlit form demo (works in both modes, Snowflake-compatible)
- **Cost Analytics**: Simulated cost tracking

### Local Mode Architecture

```
streamlit_app_local.py          # Entry point (no Snowflake)
├── utils/local_db_utils.py     # SQLite database layer
│   ├── get_connection()        # SQLite connection with WAL mode
│   ├── init_database()         # Creates all tables
│   ├── insert_chunks_batch()   # Batch chunk insertion
│   ├── search_chunks()         # Text-based search (replaces Cortex)
│   └── ...                     # Full CRUD operations
├── views/webapp_demo.py        # Shared HTML+CSS+JS demo page
└── logger_config.py            # Shared logging (works in both modes)
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CHUNKY_LOCAL_DB` | `chunky_local.db` | Path to SQLite database file |

### Running Tests

```bash
# Run unit tests (works without Snowflake)
python -m pytest tests/test_refinery.py -v
```

### Switching Between Modes

- **Snowflake Mode**: `streamlit run streamlit_app.py` (requires Snowflake environment)
- **Local Mode**: `streamlit run streamlit_app_local.py` (standalone, no dependencies)

Both modes share:
- `views/webapp_demo.py` (native form demo)
- `prompts.py` (Vision extraction prompts)
- `logger_config.py` (logging infrastructure)
- `utils/constants.py` (shared constants)

---

## 14. Create Search Service Wizard

### Purpose
A guided 5-page wizard for creating a Cortex Search Service. Walks users through role selection, data source configuration, ingestion execution, QA inspection, and search service creation — all in a single Streamlit page with pagination.

### Pages

| Step | Title | Description |
|------|-------|-------------|
| **1** | Service Setup | Select role, verify database/schema from Gate, set service name (CSS_ prefix), validate IT_AI privileges |
| **2** | Data Source & Config | Browse stage files (grouped by directory), select a PDF, configure scope, strategy, and chunk parameters. Supports SURGICAL mode with page mapping UI. Auto-fills table name from PDF. Warns about duplicate pages. Grants field defaults to empty with example tooltip. |
| **3** | Confirm & Execute | Review configuration summary, run ingestion via batch processor, view styled results dashboard with grant status, defect details, page coverage map, observability lineage, and CSV export. Mode column has color coding (green=APPEND, red=OVERWRITE, blue=SURGICAL). |
| **4** | QA Studio | Inspect, edit, and repair chunks from completed jobs. Uses the shared `views/qastudio.py` module. No Search Scope UI — always uses "From Completed Jobs" behavior. Optional — user can skip to Step 5. |
| **5** | Search Service Configuration | Configure search columns (with search type and embedding model), attribute columns, target lag, and create the Cortex Search Service with privilege grants |

### Access
Navigate to **"QA Studio"** or **"Create Cortex Search"** in the sidebar (available in both Snowflake and Local modes). QA Studio is also accessible as Step 4 of the CCS wizard.

### Design
- **Hybrid approach**: `st.html()` for styled step headers + native Streamlit widgets for all inputs
- **Pagination**: Session-state-based page tracking with Back/Next navigation
- **Privilege validation**: Checks IT_AI has CREATE CORTEX SEARCH SERVICE on the target schema
- **Stage browsing**: Lists PDFs grouped by directory with single-select radio
- **Batch execution**: Reuses the one-job-per-rerun batch processor from Doc Refinery (code moved to `views/ccs/batch_processor.py`)
- **Surgical mode**: Full page mapping UI with range-based surgical replacement, duplicate page detection
- **Auto-fill table name**: Normalizes PDF filename to valid Snowflake table name (ALL CAPS, underscores, no special chars). Uses the `setdefault` + direct widget-key assignment pattern from `HTML_lesson_learnt.md §12` — never combines `value=` and `key=` on the same widget.
- **Styled job workbench**: Status-based row coloring (green=Completed, red=Failed, yellow=Warning, blue=Running) + Mode column color coding (green=APPEND, red=OVERWRITE, blue=SURGICAL)
- **Report dashboard**: Aggregate overview with performance, cost, data yield + per-job details with grant status, defect details, page coverage map, observability lineage
- **CSV export**: Download job chunks as CSV from each completed job's expander
- **Shared QA Studio**: Chunk inspection, draft editing, batch generation, commit/delete operations — extracted to `views/qastudio.py` and shared between CCS wizard and Doc Refinery
- **Query tagging**: Automatic warehouse attribution via session-level QUERY_TAG
- **Search service creation**: Generates CREATE CORTEX SEARCH SERVICE SQL with single-index or multi-index syntax based on column selections
- **Privilege grants**: Grants USAGE on search service and SELECT on source table to roles from Step 1

### Code Organization

All wizard code lives in `views/ccs/`. The following files were moved from `views/refinery/` to make the wizard self-contained:

| File | Purpose |
|------|----------|
| `batch_processor.py` | One-job-per-rerun batch execution driver |
| `batch_exceptions.py` | `BatchCancelledError` exception class |
| `ingestion_core.py` | Table initialization and surgical delete operations |
| `ingestion_strategies/` | Layout, Vision, and Hybrid repair strategies |
| `refinery_common.py` | `execute_sql_safe()`, `_build_chunk_ref()` utilities |
| `surgical_ui.py` | Range-based surgical mapping UI (`@st.fragment`) |
| `qa.py` | QA Studio helpers — imports from `views/qastudio.py` |
| `tools.py` | Maintenance tools — shift engine self-test, temp cleanup |

See [`HTML_lesson_learnt.md`](HTML_lesson_learnt.md) §11 for Snowflake runtime specifics.

---

## 15. Vision Extraction Prompt

### Silver Bullet Prompt
The Vision extraction prompt follows a "Document Reconstruction Specialist" paradigm:

- **Lossless reproduction**: Every word, number, symbol from the image appears in output
- **Honest uncertainty marking**: Illegible text → `[unclear: best guess]` or `[?]`
- **Spatial relationship preservation**: Layout conveys meaning
- **Image as ground truth**: Translate into Markdown, don't interpret or improve

### Key Extraction Rules
- **Tables**: Merged cells (vertical/horizontal), multi-line cells with `<br>`, header completeness
- **Charts**: Extract data points into tables + narrated description of trends
- **Visual Elements**: Descriptive text with `[VISUAL: ...]` tags
- **Numbers**: Exact reproduction — no rounding, reformatting, or unit conversion
- **Language**: Maintain original languages, diacritics, and script mixing

### Prompt Location
All prompts centralized in [`prompts.py`](prompts.py):
- `get_silver_bullet_prompt()` — Main reconstruction prompt
- `get_vision_extraction_prompt()` — Vision-only mode
- `get_layout_repair_prompt()` — Visual/layout defect repair
- `get_chat_system_prompt()` — RAG Playground persona