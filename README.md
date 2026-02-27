# Chunky - Snowflake Native RAG Ecosystem

## 1. Project Overview

### What the System Does
Chunky is a Snowflake-native Streamlit application that provides a complete Retrieval-Augmented Generation (RAG) ecosystem. It operates as a multi-tab application running entirely within Snowflake's Snowpark environment, providing:

1. **Document Ingestion Pipeline (Doc Refinery)**: Converts PDF documents stored in Snowflake stages into chunked text tables using Snowflake Cortex AI_PARSE_DOCUMENT and vision-based AI processing
2. **Cortex Search Service Deployment**: Creates and manages Snowflake Cortex Search Services for semantic search over ingested documents
3. **RAG Playground**: Interactive chat interface that queries deployed search services and generates responses using Snowflake Cortex LLMs
4. **Cost & Quality Analytics**: Monitoring dashboards for tracking credit consumption and response quality
5. **QA Studio**: Quality assurance workbench for testing and refining document chunks with Markdown preview capabilities

### What Problem It Solves
- Eliminates the need for external document processing infrastructure by using Snowflake-native AI functions
- Provides deterministic RBAC (Role-Based Access Control) for document access through a gatekeeper authentication flow
- Offers transparent cost tracking for AI operations within Snowflake
- Enables multi-strategy document processing (Layout-only, Vision-only, or Hybrid)
- Maintains LLM isolation from citation metadata (CHUNK_REF) to prevent hyperlink syntax from contaminating context

### What It Explicitly Does NOT Do
- Does not support document upload from local filesystem (documents must already exist in Snowflake stages)
- Does not perform user authentication via external identity providers (relies on Snowflake's native authentication and hardcoded user-role mappings)
- Does not support real-time streaming ingestion (batch processing only)
- Does not provide horizontal scaling beyond Snowflake warehouse constraints
- Does not send CHUNK_REF hyperlinks to the LLM (isolation enforced in `retrieve_context`)

---

## 2. High-Level Architecture

### Major Components

```
┌─────────────────────────────────────────────────────────────────────┐
│                        streamlit_app.py (Entry Point)               │
│   - Session state initialization                                    │
│   - Gatekeeper authentication check                                 │
│   - Navigation routing                                              │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        │                       │                       │
        ▼                       ▼                       ▼
┌───────────────┐     ┌─────────────────┐     ┌────────────────┐
│  views/home   │     │ views/admin.py  │     │  views/chat.py │
│  (Landing)    │     │ (Doc Refinery)  │     │ (RAG Playground)│
└───────────────┘     └────────┬────────┘     └────────────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
        ▼                      ▼                      ▼
┌────────────────┐   ┌─────────────────┐   ┌─────────────────┐
│tab_config.py   │   │batch_processor.py│   │tab_deployment.py│
│(Job Builder)   │   │(Orchestrator)    │   │(Orchestrator)   │
└────────────────┘   └────────┬────────┘   └────────┬────────┘
                             │                     │
              ┌───────────────┼─────────────┐       │
              │               │             │       │
              ▼               ▼             ▼       ▼
       ┌────────────┐  ┌─────────────┐  ┌────────────────────┐
       │ingestion_  │  │ingestion_   │  │deployment_ui.py    │
       │core.py     │  │strategies.py│  │deployment_logic.py │
       └────────────┘  └─────────────┘  └────────────────────┘
```

### Modular Architecture (PLAN-02)

The codebase follows a strict separation of concerns:

| Module Layer | Purpose | Constraint |
|--------------|---------|------------|
| **Orchestrators** (`batch_processor.py`, `tab_deployment.py`) | Coordinate flow, manage session state, handle errors | No business logic; delegates to helpers |
| **UI Layer** (`deployment_ui.py`) | Render Streamlit components, capture user input | No database operations; calls logic layer |
| **Logic Layer** (`deployment_logic.py`, `ingestion_core.py`) | Execute SQL, perform calculations, manage transactions | No st.rerun() calls; returns values |
| **Strategy Layer** (`ingestion_strategies.py`) | Implement parsing algorithms, mutate job metrics | Receives context; never fetches own schema |
| **Common Layer** (`common.py`) | Stateless pure utilities shared across modules | Zero imports from orchestrators |

### Data Flow Step-by-Step

1. **Authentication Flow**
   - User accesses the Streamlit app within Snowflake
   - `streamlit_app.py` checks for `auth_context` in session state
   - If missing, `render_login_screen()` displays the Gatekeeper form
   - User enters Database, Schema, and Stage names
   - System verifies user email against `USER_ROLE_MAP` hardcoded dictionary
   - System checks stage access via `STAGE_ACCESS_MAP` or stored procedure `GET_ROLES_WITH_STAGE_ACCESS`
   - On success, `auth_context` is set with db, schema, stage, user, and role

2. **Document Ingestion Flow**
   - User navigates to Doc Refinery → Config tab
   - Selects PDF file from stage (via `LIST @stage_path PATTERN='.*\.pdf'`)
   - Configures write mode (APPEND/OVERWRITE/SURGICAL), chunk size, overlap
   - For new tables, selects roles for RBAC grants via multi-select dropdown
   - Job is appended to `st.session_state.job_queue`
   - On Ingestion tab, user triggers `run_batch_execution()`
   - Orchestrator delegates to `ingestion_core.py` for table initialization and surgical delete
   - Strategy helpers from `ingestion_strategies.py` execute parsing (Layout, Hybrid, Vision)
   - CHUNK_REF column populated with Markdown-formatted citation including clickable hyperlink
   - GRANT statements execute only for newly created tables with selected roles

3. **Search Service Deployment Flow**
   - User navigates to Deployment tab
   - `deployment_ui.py` functions render configuration sections
   - `deployment_logic.py` handles cost estimation and deployment execution
   - System generates SQL preview for `CREATE OR REPLACE CORTEX SEARCH SERVICE`
   - CHUNK_REF conditionally included in SELECT columns when present in table schema
   - On execution, service is created and GRANT USAGE is applied to selected roles

4. **RAG Query Flow**
   - User navigates to RAG Playground
   - Configures search services, model, temperature, retrieval limit
   - User enters query via chat input
   - System calls `SNOWFLAKE.CORTEX.SEARCH_PREVIEW` for each selected service
   - `retrieve_context()` extracts CHUNK_REF into `retrieval_meta` BEFORE building text payload
   - CHUNK_REF explicitly excluded from `full_context_chunks` (LLM isolation boundary)
   - Context chunks are concatenated and formatted as XML prompt
   - `AI_COMPLETE` generates response with configured model
   - Response displayed with retrieval inspector showing rendered context chunks
   - CHUNK_REF rendered as clickable hyperlink when `[Digital Copy]` marker present

5. **QA Studio Flow**
   - User navigates to Doc Refinery → QA tab
   - Searches for chunks by table and page range
   - Adds chunks to Workbench for editing
   - Display mode toggle: Rendered (Markdown preview) or Raw (editable text)
   - Draft generation via vision-based AI processing
   - Commit updates chunk text in target table

### Control Flow Model
- **Runtime**: Streamlit's reactive execution model (top-to-bottom script re-execution on interaction)
- **State Management**: `st.session_state` for all persistent data (auth context, job queue, chat history)
- **Batch Processing**: Synchronous, blocking execution with progress bars
- **Rerun Isolation**: Only UI layer functions call `st.rerun()`; logic layer returns values

---

## 3. Repository Structure

```
Chunky/
├── streamlit_app.py          # Application entry point, routing, session init
├── logger_config.py          # Logging configuration with SessionStateLogHandler
├── prompts.py                # Centralized AI prompt templates
├── requirements.txt          # Python dependencies
│
├── utils/
│   ├── __init__.py
│   ├── auth_utils.py         # Authentication logic, USER_ROLE_MAP, STAGE_ACCESS_MAP
│   ├── constants.py          # Financial rates, embedding models, label definitions
│   ├── core_utils.py         # PDFUtils, QualityInspector, RAGAnalytics classes
│   └── snowflake_utils.py    # Snowflake interaction functions (retrieve, generate, monitor)
│
└── views/
    ├── __init__.py
    ├── admin.py              # Doc Refinery orchestrator (imports all refinery tabs)
    ├── analytics_cost.py     # Cost analytics dashboard
    ├── analytics_quality.py  # Quality monitoring dashboard
    ├── chat.py               # RAG Playground chat interface with Retrieval Inspector
    ├── home.py               # Landing page
    └── logs.py               # System logs viewer
    │
    └── refinery/
        ├── __init__.py
        ├── batch_processor.py    # Execution orchestrator (delegates to ingestion modules)
        ├── common.py             # Shared utilities (_build_chunk_ref, execute_sql_safe)
        ├── ingestion_core.py     # Table initialization, surgical delete operations
        ├── ingestion_strategies.py # Layout, Hybrid Repair, Vision parsing strategies
        ├── deployment_ui.py      # Deployment UI rendering functions
        ├── deployment_logic.py   # Deployment execution logic, cost estimation
        ├── tab_config.py         # Job Builder UI
        ├── tab_deployment.py     # Deployment orchestrator (delegates to deployment modules)
        ├── tab_ingestion.py      # Batch execution UI and reporting
        ├── tab_qa.py             # QA Studio with Rendered/Raw display mode toggle
        └── tab_tools.py          # Utility tools
```

### Critical Files Explained

| File | Purpose | Key Dependencies |
|------|---------|------------------|
| `streamlit_app.py` | Entry point; must run within Snowflake Snowpark environment | All view modules, auth_utils |
| `utils/auth_utils.py` | Contains hardcoded `USER_ROLE_MAP` and `STAGE_ACCESS_MAP` - modifying these changes access control | None (standalone) |
| `views/refinery/batch_processor.py` | Orchestrator only; delegates to ingestion_core and ingestion_strategies | ingestion_core, ingestion_strategies |
| `views/refinery/ingestion_core.py` | Table initialization and surgical delete; stateless functions | common.py only |
| `views/refinery/ingestion_strategies.py` | Three parsing strategies with incremental metrics mutation | common.py, core_utils, snowflake_utils |
| `views/refinery/tab_deployment.py` | Orchestrator only; delegates to deployment_ui and deployment_logic | deployment_ui, deployment_logic |
| `views/refinery/deployment_ui.py` | All UI rendering for deployment tab; dynamic schema discovery | deployment_logic |
| `views/refinery/deployment_logic.py` | Cost estimation, deployment execution, security validation | snowflake_utils |
| `views/refinery/common.py` | CHUNK_REF builder with Markdown hyperlink formatting | urllib.parse |
| `utils/snowflake_utils.py` | LLM isolation boundary in retrieve_context; CHUNK_REF extraction | prompts, constants |
| `views/refinery/tab_qa.py` | QA Studio with Rendered/Raw display mode toggle | snowflake_utils, core_utils |
| `views/chat.py` | RAG Playground with Retrieval Inspector showing rendered chunks | snowflake_utils |
| `prompts.py` | All AI prompt templates; changes here affect document reconstruction quality | None |

### Unconventional Structures
- **Hardcoded Role Mappings**: `USER_ROLE_MAP` and `STAGE_ACCESS_MAP` in `auth_utils.py` are hardcoded dictionaries rather than database tables, requiring code deployment for user access changes
- **Session State as Database**: All job queues, ingestion history, and audit data stored in `st.session_state` rather than persistent tables
- **Nested Tab Architecture**: `views/admin.py` imports and renders 5 sub-tabs from `views/refinery/` package
- **Module Constraint Enforcement**: `common.py` has zero imports from orchestrators; `ingestion_*.py` modules have zero imports from `batch_processor.py` or `tab_deployment.py`
- **LLM Isolation Boundary**: CHUNK_REF extracted in `retrieve_context()` and never passed to LLM; hyperlink syntax cannot contaminate context

---

## 4. Core Concepts & Domain Model

### Key Abstractions

#### Job Object
```python
{
    "id": int,                    # Unique identifier
    "file": str,                  # PDF filename from stage
    "table": str,                 # Target table name (without db.schema prefix)
    "mode": str,                  # "APPEND" | "OVERWRITE" | "SURGICAL"
    "scope": str,                 # "Full Doc" | "Page Range"
    "range": tuple,               # (start_page, end_page) or None
    "estimated_pages": int,
    "layout": bool,               # Use Layout Parser (Strategy A)
    "vision": bool,               # Use Vision Parser (Strategy C)
    "params": tuple,              # (chunk_size, overlap)
    "surgical_file": str | None,  # File filter for SURGICAL mode
    "grant_roles": list,          # Roles for RBAC grants on new tables
    "status": str,                # "Pending" | "Running" | "Completed" | "Failed" | "Cancelled"
    "metrics": dict               # Execution metrics (mutated incrementally)
}
```

#### Auth Context Object
```python
{
    "db": str,        # Locked database context
    "schema": str,    # Locked schema context
    "stage": str,     # Stage name for document source
    "user": str,      # User email
    "role": str       # Primary role from intersection
}
```

#### Chunk Table Schema
All ingested tables follow this schema:
```sql
CREATE TABLE db.schema.table_name (
    RELATIVE_PATH VARCHAR,    -- Source PDF filename
    PAGE_NUMBER NUMBER,       -- Page number in source document
    CHUNK VARCHAR,            -- Text content
    CHUNK_ID VARCHAR,         -- UUID identifier
    CHUNK_TYPE VARCHAR,       -- 'STANDARD' | 'ENHANCED'
    CHUNK_REF VARCHAR         -- Markdown citation with clickable hyperlink
)
```

#### CHUNK_REF Format (PLAN-01)
The CHUNK_REF column contains Markdown-formatted citation strings:

**With Link (new format):**
```
[Digital Copy](<URL-encoded-link>) | Doc Source: {path} | Page Num: {num}
```

**Without Link (legacy format):**
```
Doc Source: {path} | Page Num: {num}
```

URL encoding uses `urllib.parse.quote(link, safe=":/?#&=@")` to preserve structurally significant URL characters while encoding spaces and parentheses that would break Markdown link syntax.

### Implicit Rules & Invariants

1. **Context Locking**: Once authenticated, db/schema/stage cannot be changed without logout
2. **Table Naming**: User-provided table names are stripped of prefixes; full path is always `"{db}"."{schema}"."{table_name}"`
3. **Identifier Escaping**: All Snowflake identifiers are escaped by doubling internal double-quotes (`.replace('"', '""')`)
4. **COPY GRANTS**: OVERWRITE mode uses `COPY GRANTS` to preserve existing table permissions
5. **Grant Exclusivity**: RBAC grants only execute when `grant_roles` is populated (new tables only)
6. **Vision Fallback**: If both `layout` and `vision` are True, Hybrid mode runs (Layout + Quality Repair)
7. **Message Limit**: Chat history capped at 30 messages in session state
8. **Image Size Limit**: Images compressed to <3.5MB for Cortex vision processing
9. **Incremental Metrics**: Job metrics mutated incrementally to capture partial success on failure
10. **Rerun Isolation**: Only `_render_deployment_action_bar()` calls `st.rerun()` in deployment flow
11. **LLM Isolation**: CHUNK_REF never enters `full_context_chunks`; extracted separately in `retrieve_context()`
12. **Dynamic Schema Discovery**: CHUNK_REF included in SELECT only when present in table schema
13. **Null Score Handling**: `scores.get("key") or 0` pattern prevents TypeError on None values

### Terminology

| Term | Definition |
|------|------------|
| **Layout Parser** | Snowflake Cortex `AI_PARSE_DOCUMENT` function for text extraction |
| **Vision Parser** | Claude-4-Sonnet via `AI_COMPLETE` for image-based text extraction |
| **Silver Bullet** | Prompt template for document reconstruction with table formatting rules |
| **Surgical Mode** | Delete-then-insert pattern for updating specific file/page ranges |
| **Cortex Search Service** | Snowflake native vector search index over chunk tables |
| **Gatekeeper** | Authentication flow that validates user and stage access |
| **Chunk Ref** | Citation string with Markdown hyperlink: `[Digital Copy](url) \| Doc Source: {path} \| Page Num: {num}` |
| **LLM Isolation Boundary** | Code in `retrieve_context()` that separates CHUNK_REF from context sent to LLM |
| **Display Mode** | QA Studio toggle: Rendered (Markdown preview) vs Raw (editable text) |

---

## 5. Detailed Behavior

### Normal Execution Flow

#### Document Ingestion (Batch Processing)
1. **Job Queue Building** (tab_config.py)
   - User selects PDF from stage file list
   - Page count extracted via `pdfinfo_from_bytes`
   - For new tables: role multiselect appears, defaults to all mapped roles
   - Job added to `st.session_state.job_queue`

2. **Batch Execution** (batch_processor.py orchestrator)
   ```
   For each job in queue:
     1. Skip if status in ['Completed', 'Cancelled']
     2. Resolve table path (escape identifiers with .replace('"', '""'))
     3. Determine page range (Full or Range)
     4. SURGICAL: Call _execute_surgical_delete from ingestion_core.py
     5. CENTRALIZED INIT: Call _initialize_target_table from ingestion_core.py
        - OVERWRITE: CREATE OR REPLACE TABLE ... COPY GRANTS
        - NEW TABLE: CREATE TABLE with standard schema
        - EXISTING: Ensure CHUNK_TYPE and CHUNK_REF columns exist
     6. STRATEGY A (if layout=True):
        - Call _execute_layout_strategy from ingestion_strategies.py
        - Build AI_PARSE_DOCUMENT SQL, collect rows, write_pandas
     7. STRATEGY B (if layout=True AND vision=True):
        - Call _execute_hybrid_repair_strategy from ingestion_strategies.py
        - Quality inspection, vision repair for defective chunks
     8. STRATEGY C (if vision=True AND layout=False):
        - Call _execute_vision_strategy from ingestion_strategies.py
        - Page-by-page vision processing
     9. RBAC GRANTS (if grant_roles populated):
        - For each role: GRANT ALL PRIVILEGES ON TABLE (with escaped role name)
     10. Update job status and metrics via _finalize_job_metrics
   ```

3. **Cost Calculation**
   - Layout: 3.33 credits per 1000 pages
   - Vision: Based on token usage × model pricing
   - Aggregated in `batch_metrics`

#### Search Service Deployment
1. **UI Rendering** (deployment_ui.py)
   - `_fetch_and_validate_source_metadata`: Context display, table selection
   - `_render_service_config_section`: Service name, warehouse, lag configuration
   - `_render_embedding_strategy_section`: Model selection, column configuration
     - Dynamic schema discovery via `get_table_schema`
     - CHUNK_REF conditionally included in default_sel when present
   - `_render_sql_preview_section`: DDL generation with escaped identifiers
   - `_render_cost_estimation_section`: Token sampling and cost calculation

2. **Execution** (deployment_logic.py)
   - `_execute_cortex_deployment`: Validates SQL prefix, executes DDL, runs GRANT loop
   - Security check: SQL must target `"{db}"."{schema}"."CSS_..."` format
   - All identifiers escaped before SQL construction

#### RAG Query Execution
1. User enters query in chat input
2. For each selected service:
   - Call `SNOWFLAKE.CORTEX.SEARCH_PREVIEW(service_path, query_json)`
   - Extract chunks and relevance scores
3. **LLM Isolation Boundary** (retrieve_context):
   - Extract CHUNK_REF (case-insensitive) into `c_ref`
   - Build `string_values` excluding CHUNK_REF key
   - Prefer CHUNK column, then text, then max-length fallback
   - Store `chunk_ref` in `retrieval_meta` separately
4. Build XML prompt: `<sys_prompt> + <chat_history> + <rag> + <latest_message>`
5. Check 200k character limit
6. Call `AI_COMPLETE(model, prompt, parameters, show_details=TRUE)`
7. Parse JSON response, extract content and usage
8. Display response, append to chat history
9. Retrieval Inspector shows rendered context chunks with clickable links

#### QA Studio Operation
1. User searches chunks by table and page range
2. Selected chunks added to Workbench (`admin_queue`)
3. Display mode initialized to "Rendered" per session
4. Radio toggle switches between:
   - **Rendered**: `st.markdown()` displays draft as read-only preview
   - **Raw**: `st.text_area()` allows editing, writes back to `item['draft_text']`
5. Commit button reads `item['draft_text']` regardless of mode
6. Ordering constraint: data_editor sync loop must execute before inspector

### Edge Cases & Failure Modes

| Scenario | Behavior |
|----------|----------|
| PDF not found in stage | Job fails with error message; status set to 'Failed' |
| Table doesn't exist for SURGICAL mode | Blocked at job configuration; error displayed |
| No roles selected for new table | Submission blocked; error message displayed |
| Vision INSERT fails | Exception bubbles up; job status = 'Failed' |
| Grant fails | Job status = 'Completed with Warnings'; manual review required |
| Context exceeds 200k chars | Error displayed; user advised to lower retrieval limit |
| User not in USER_ROLE_MAP | Assigned 'PUBLIC' role; access limited to PUBLIC-accessible stages |
| Stage not in STAGE_ACCESS_MAP | Calls `GET_ROLES_WITH_STAGE_ACCESS` stored procedure |
| Identifier contains double-quote | Escaped via `.replace('"', '""')` before SQL construction |
| CHUNK_REF contains None score | `or 0` pattern prevents TypeError in float conversion |
| Legacy table without CHUNK_REF | Column omitted from default_sel; service still deploys |
| CHUNK_REF URL has spaces/parentheses | URL-encoded to preserve Markdown link syntax |
| Toggle from Raw to Rendered | Draft text preserved in session state |

### Configuration Paths

| Configuration | Location | Effect |
|---------------|----------|--------|
| `USER_ROLE_MAP` | `utils/auth_utils.py` | Maps email addresses to allowed roles |
| `STAGE_ACCESS_MAP` | `utils/auth_utils.py` | Maps stage paths to authorized roles |
| `EMBEDDING_MODELS` | `utils/constants.py` | Available models and metadata |
| `EMBEDDING_PRICING` | `utils/constants.py` | Credit cost per 1M tokens |
| `LABEL_DEFINITIONS` | `utils/constants.py` | Monitoring categories and labels |
| `CREDIT_TO_USD/IDR` | `utils/constants.py` | Currency conversion rates |

---

## 6. Public Interfaces

### Entry Points

#### `streamlit_app.py:main()`
Primary entry point. Must be executed within Snowflake Streamlit environment.

**Preconditions:**
- Active Snowflake Snowpark session
- User authenticated through Snowflake's native auth

**Behavior:**
- Initializes session state defaults
- Checks for `auth_context` (triggers Gatekeeper if missing)
- Routes to appropriate view based on navigation selection

### Key Functions

#### `utils/auth_utils.py`

```python
def get_user_mapped_roles(email: str) -> list[str]
```
Returns list of uppercase role names for given email, or `['PUBLIC']` if not found.

```python
def render_login_screen(session) -> None
```
Renders authentication form. Sets `st.session_state.auth_context` on success.

```python
def get_authorized_roles_for_stage(session, db, schema, stage) -> tuple[list, str|None]
```
Returns (roles, error_message). Checks `STAGE_ACCESS_MAP` first, then calls stored procedure.

#### `views/refinery/batch_processor.py`

```python
def run_batch_execution(session, db, schema, stage_path) -> None
```
Orchestrates all jobs in `st.session_state.job_queue`. Delegates to ingestion modules.

**Side Effects:**
- Creates/modifies tables in specified schema
- Uploads temporary images to stage
- Executes GRANT statements
- Sets `st.session_state.batch_audit` with metrics

#### `views/refinery/ingestion_core.py`

```python
def _initialize_target_table(session, full_table, db, schema, table_name,
                              mode, tbl_exists, tbl_cols) -> None
```
Issues CREATE / CREATE OR REPLACE / ALTER TABLE as required. Raises Exception on failure.

```python
def _execute_surgical_delete(session, full_table, safe_file, pg_filter_sql,
                              job_queue, current_job_index) -> tuple[bool, str]
```
Returns (success, error_message). Cascade-cancels downstream jobs on failure.

#### `views/refinery/ingestion_strategies.py`

```python
def _execute_layout_strategy(session, job, full_table, stage_path,
                              db, schema, table_name,
                              chunk_sz, chunk_ov, json_opts, safe_file,
                              job_pages_count) -> None
```
Mutates `job['metrics']['standard_cnt']` incrementally. Raises Exception on failure.

```python
def _execute_hybrid_repair_strategy(session, job, full_table, stage_path,
                                     safe_file, pg_filter_sql,
                                     get_pdf_bytes, job_alert) -> None
```
Per-page vision repair. Mutates `job['metrics']` after each successful UPDATE.

```python
def _execute_vision_strategy(session, job, full_table, stage_path,
                              chunk_sz, chunk_ov, target_range, get_pdf_bytes) -> None
```
Page-by-page vision extraction. Mutates `job['metrics']` after each INSERT.

#### `views/refinery/deployment_logic.py`

```python
def _execute_cortex_deployment(session, db, schema, final_sql,
                                full_svc_identifier, deploy_grant_roles, user) -> bool
```
Returns True on success, False on validation/execution failure. Never calls st.rerun().

```python
def _render_cost_estimation_section(session, tgt_table_full, target_col,
                                     selected_model, lag_val, lag_unit) -> None
```
Renders cost estimation UI. Sets `st.session_state.last_est` on success.

#### `views/refinery/common.py`

```python
def _build_chunk_ref(rel_path: str, page_num, link: str = "") -> str
```
Builds CHUNK_REF citation string. When link present, returns Markdown hyperlink format:
`[Digital Copy](<url-encoded-link>) | Doc Source: {path} | Page Num: {num}`

URL encoding preserves `:/?#&=@` characters; encodes spaces and parentheses.

#### `utils/snowflake_utils.py`

```python
def retrieve_context(session, config: dict, prompt: str) -> tuple[list, list]
```
Returns (context_chunks, retrieval_metadata) from configured search services.

**LLM Isolation Boundary:**
- Extracts CHUNK_REF before building text payload
- Excludes CHUNK_REF from `string_values` fallback
- Stores `chunk_ref` in retrieval_meta for Retrieval Inspector

```python
def generate_llm_response(session, xml_prompt, model_name, temp, top_p) -> dict
```
Returns dict with keys: `text`, `usage`, `parsing_success`, `raw_response`, `resp_data`.

```python
def run_cortex(session, prompt, stage_path, rel_img_path, model) -> tuple[str, int, int]
```
Returns (response_text, input_tokens, output_tokens) for vision-capable model calls.

### Expected Inputs/Outputs

| Function | Input | Output | Constraints |
|----------|-------|--------|-------------|
| `run_batch_execution` | Snowpark session, db, schema, stage_path | None (side effects) | Session must have write access |
| `_initialize_target_table` | Session, full_table, mode, tbl_exists, tbl_cols | None | Raises on failure |
| `_execute_surgical_delete` | Session, full_table, safe_file, pg_filter_sql, job_queue, idx | (bool, str) | Mutates job_queue on failure |
| `_execute_cortex_deployment` | Session, db, schema, final_sql, svc_id, roles, user | bool | Never calls st.rerun() |
| `retrieve_context` | Session, config dict with services/limit | (chunks list, meta list) | Services must exist; CHUNK_REF isolated |
| `generate_llm_response` | Session, XML prompt <200k chars | Response dict | Prompt length limit enforced |
| `_build_chunk_ref` | rel_path, page_num, link (optional) | str | URL encodes link for Markdown safety |

---

## 7. State, Persistence, and Data

### Session State Keys

| Key | Type | Purpose | Lifecycle |
|-----|------|---------|-----------|
| `config` | dict | Legacy config (db, schema, user_id) | Initialized at startup |
| `auth_context` | dict | Authenticated user context | Set on login, cleared on logout |
| `job_queue` | list[dict] | Pending/completed jobs | Modified during job building and execution |
| `messages` | list[dict] | Chat history | Capped at 30 messages |
| `services_cache` | list[str] | Discovered search services | Refreshed on scan |
| `active_config` | dict | Current RAG configuration | Set on config apply |
| `batch_audit` | dict | Last batch execution metrics | Set after batch completion |
| `ingestion_history` | list[dict] | Historical job records | Appended after each job |
| `file_metadata_cache` | dict | PDF page counts | Populated on file selection |
| `system_logs` | list[dict] | In-memory log buffer | Circular buffer (1000 max) |
| `deployment_tables_cache` | list[str] | Tables for deployment dropdown | Invalidated on deployment |
| `deployment_warehouses_cache` | list[str] | Warehouses for dropdown | Session-scoped |
| `admin_service_cache` | list[str] | Services for RBAC panel | Invalidated on deployment |
| `last_deployed_service` | str | Most recently deployed service name | Cleared after RBAC grant |
| `last_est` | dict | Last cost estimation result | Cleared on DDL change |
| `cortex_sql_preview` | str | Generated DDL preview | Cleared on deployment/cancel |
| `qa_display_mode` | str | QA Studio display mode ("Rendered" or "Raw") | Default "Rendered" |
| `admin_queue` | list[dict] | QA Workbench items | Modified during QA operations |

### Persistent Storage

| Data | Storage | Format | Lifecycle |
|------|---------|--------|-----------|
| Chunk tables | Snowflake tables | SQL table with standard schema | Persisted indefinitely |
| Cortex Search Services | Snowflake services | Managed by Snowflake | Persisted until dropped |
| Application logs | `app_activity.log` file | Text log lines | File system (may not persist in Snowflake) |
| Temporary images | Stage `@stage/_temp_images/` | JPEG files | Not automatically cleaned |

### Data Migration/Reset

- **No migration scripts**: Schema changes require manual ALTER TABLE
- **Reset**: Clear `st.session_state` and reconnect; tables persist in Snowflake
- **Cleanup**: `PDFUtils.clear_temp_images()` clears local temp directory (not stage)

---

## 8. Dependencies & Integration

### External Libraries

| Library | Version | Purpose |
|---------|---------|---------|
| `streamlit` | >=1.24.0 | UI framework |
| `snowflake-snowpark-python` | >=1.8.0 | Snowflake connectivity |
| `snowflake-connector-python` | >=3.0.6 | Database driver |
| `pdf2image` | >=1.16.0 | PDF to image conversion |
| `Pillow` | >=9.2.0 | Image processing |
| `pandas` | >=1.4.0 | Data manipulation |
| `plotly` | >=5.6.0 | Visualization |
| `mistletoe` | >=0.12.0 | Markdown parsing for table validation |

### System Dependencies

| Dependency | Purpose | Installation |
|------------|---------|--------------|
| `poppler` | PDF rendering for pdf2image | `poppler-utils` (Linux), `brew install poppler` (macOS) |

### Snowflake Dependencies

| Object | Type | Purpose | Required |
|--------|------|---------|----------|
| `SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT` | Function | PDF text extraction | Yes |
| `SNOWFLAKE.CORTEX.AI_COMPLETE` | Function | LLM generation | Yes |
| `SNOWFLAKE.CORTEX.SEARCH_PREVIEW` | Function | Vector search | Yes |
| `SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER` | Function | Text chunking | Yes |
| `SNOWFLAKE.CORTEX.COUNT_TOKENS` | Function | Token counting for cost estimation | Yes |
| `GET_ROLES_WITH_STAGE_ACCESS` | Stored Procedure | Dynamic stage access check | Optional (fallback) |

### Environment Assumptions

1. **Execution Context**: Must run within Snowflake Streamlit (not standalone)
2. **Authentication**: Snowflake native authentication with email extraction via `st.user`
3. **Warehouse**: Compute warehouse required for Cortex functions
4. **Network**: No external network access required (all Snowflake-native)

---

## 9. Setup, Build, and Execution

### Prerequisites
1. Snowflake account with Cortex AI enabled
2. Snowflake Streamlit capability enabled
3. Database, schema, and stage created for document storage
4. `GET_ROLES_WITH_STAGE_ACCESS` stored procedure (optional, for dynamic stage access)

### Deployment Steps

1. **Create Snowflake Streamlit App**
   ```sql
   CREATE OR REPLACE STREAMLIT <db>.<schema>.<app_name>
   FROM '<stage_path>'
   MAIN_FILE = 'streamlit_app.py';
   ```

2. **Upload Code to Stage**
   ```sql
   PUT file://streamlit_app.py @<stage_path> OVERWRITE=TRUE;
   PUT file://logger_config.py @<stage_path> OVERWRITE=TRUE;
   PUT file://prompts.py @<stage_path> OVERWRITE=TRUE;
   -- Upload all .py files including new modular files
   PUT file://views/refinery/ingestion_core.py @<stage_path>/views/refinery/ OVERWRITE=TRUE;
   PUT file://views/refinery/ingestion_strategies.py @<stage_path>/views/refinery/ OVERWRITE=TRUE;
   PUT file://views/refinery/deployment_ui.py @<stage_path>/views/refinery/ OVERWRITE=TRUE;
   PUT file://views/refinery/deployment_logic.py @<stage_path>/views/refinery/ OVERWRITE=TRUE;
   PUT file://views/refinery/common.py @<stage_path>/views/refinery/ OVERWRITE=TRUE;
   ```

3. **Configure User Access**
   Edit `USER_ROLE_MAP` in `utils/auth_utils.py`:
   ```python
   USER_ROLE_MAP = {
       "user@example.com": ["ROLE1", "ROLE2"],
   }
   ```

4. **Configure Stage Access**
   Edit `STAGE_ACCESS_MAP` in `utils/auth_utils.py`:
   ```python
   STAGE_ACCESS_MAP = {
       "DB.SCHEMA.STAGE": ["ROLE1", "ROLE2"],
   }
   ```

### Local Development (Limited)

Local development is possible but requires:
1. Snowflake connection via `snowflake-connector-python`
2. `poppler` installed for PDF processing
3. Manual session creation (no `get_active_session()`)

Limitations:
- Authentication flow will not work (no `st.user`)
- Must use secrets for user email
- Stage operations require proper credentials

---

## 10. Testing & Validation

### What Testing Exists
- **No automated tests**: Repository contains no test files or test framework
- **Manual validation**: All testing is manual via Streamlit UI

### Validation Procedures

| Component | Validation Method |
|-----------|-------------------|
| Authentication | Login with valid/invalid users, verify context set |
| Job Building | Configure job, verify payload in session state |
| Batch Execution | Run with test PDF, verify table creation and chunk count |
| RBAC Grants | Query `INFORMATION_SCHEMA.TABLE_PRIVILEGES` after ingestion |
| Search Service | Query service via `SEARCH_PREVIEW`, verify results |
| Chat | Send query, verify response and context retrieval |
| Identifier Escaping | Test with table names containing special characters |
| CHUNK_REF Isolation | Verify no `[Digital Copy]` in LLM prompt logs |
| Display Mode Toggle | Toggle Rendered/Raw, verify draft preservation |
| Legacy Table | Deploy service on table without CHUNK_REF column |

### Test Gaps

1. **No unit tests**: All functions untested at unit level
2. **No integration tests**: End-to-end flows not automated
3. **No performance tests**: No benchmarks for large documents
4. **No security tests**: RBAC not verified programmatically

---

## 11. Known Limitations & Non-Goals

### Hard-Coded Assumptions

| Assumption | Location | Impact |
|------------|----------|--------|
| User emails mapped in code | `auth_utils.py:USER_ROLE_MAP` | New users require code deployment |
| Stage access in code | `auth_utils.py:STAGE_ACCESS_MAP` | New stages require code deployment |
| Admin contact email | `auth_utils.py:ADMIN_CONTACT` | Displayed in error messages |
| App owner role | `auth_utils.py:APP_OWNER_ROLE` | Used in error messages |
| Chunk table schema | `ingestion_core.py` | All tables have identical columns |
| Image size limit | `core_utils.py:MAX_IMAGE_MB = 3.5` | Images compressed to this limit |
| Display mode default | `tab_qa.py` | Default "Rendered" per session |

### Technical Debt

1. **Greedy exception handling removed**: Vision INSERT errors now bubble up correctly
2. **Table initialization centralized**: No longer coupled to Layout strategy
3. **QUERY_HISTORY scanning removed**: RBAC now uses explicit role mapping only
4. **Modular architecture implemented**: PLAN-02 refactoring complete
5. **LLM isolation enforced**: CHUNK_REF never sent to LLM (PLAN-01)
6. **Dynamic schema discovery**: CHUNK_REF conditionally included (PLAN-01)

### Non-Goals

| Not Implemented | Evidence |
|-----------------|----------|
| Document upload | No file upload handlers; documents must be in stage |
| Real-time ingestion | Batch processing only; no streaming |
| Multi-tenant isolation | Single auth context per session |
| External LLM support | Only Snowflake Cortex models available |
| Custom embedding models | Limited to `EMBEDDING_MODELS` constant |
| CHUNK_REF in LLM context | Explicitly isolated in `retrieve_context()` |

---

## 12. Change Sensitivity

### Most Fragile Components

| Component | Fragility Reason |
|-----------|------------------|
| `batch_processor.py` | Orchestrator coordinates all ingestion; changes affect flow |
| `ingestion_strategies.py` | Core parsing logic; changes affect document quality |
| `auth_utils.py` | Hardcoded mappings; changes affect all access control |
| `prompts.py` | Prompt changes affect document quality; no versioning |
| `tab_config.py` | Job payload structure; changes must sync with batch_processor |
| `snowflake_utils.py:retrieve_context` | LLM isolation boundary; changes affect CHUNK_REF handling |

### Tightly Coupled Areas

1. **Job Queue Flow**: `tab_config.py` → `batch_processor.py`
   - Job payload structure must match exactly
   - New fields require updates in both files

2. **Auth Context**: `auth_utils.py` → all views
   - All views assume `auth_context` exists with db/schema/stage/user/role
   - Structure changes require updates across all views

3. **Chunk Table Schema**: `ingestion_core.py` → `tab_deployment.py`
   - Deployment expects standard chunk table schema
   - Schema changes break search service creation

4. **Deployment Flow**: `deployment_ui.py` ↔ `deployment_logic.py`
   - UI functions call logic layer for execution
   - Logic layer must return values (no st.rerun)

5. **QA Studio Ordering**: `tab_qa.py` data_editor sync → inspector
   - Sync loop must execute before inspector reads draft_text
   - Reordering breaks draft state consistency

### Easiest Extension Points

| Component | Extension Type |
|-----------|----------------|
| `EMBEDDING_MODELS` constant | Add new model metadata |
| `LABEL_DEFINITIONS` constant | Add monitoring categories |
| `prompts.py` | Modify prompt templates |
| `USER_ROLE_MAP` | Add user mappings |
| `STAGE_ACCESS_MAP` | Add stage mappings |
| `ingestion_strategies.py` | Add new parsing strategy |

### Hardest Extension Points

| Component | Difficulty Reason |
|-----------|-------------------|
| New write mode | Requires changes in tab_config, ingestion_core, and grant logic |
| Alternative authentication | Requires replacing entire Gatekeeper flow |
| Custom chunk schema | Requires changes across ingestion_core, deployment, and search |
| Multi-stage support | Currently locked to single stage per session |
| CHUNK_REF format change | Requires updates to common.py, snowflake_utils.py, and chat.py |
