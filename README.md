# Chunky - Snowflake Native RAG Ecosystem

## 1. Project Overview

### What the System Does
Chunky is a Snowflake-native Streamlit application that provides a complete Retrieval-Augmented Generation (RAG) ecosystem. It operates as a multi-tab application running entirely within Snowflake's Snowpark environment, providing:

1. **Document Ingestion Pipeline (Doc Refinery)**: Converts PDF documents stored in Snowflake stages into chunked text tables using Snowflake Cortex AI_PARSE_DOCUMENT and vision-based AI processing
2. **Cortex Search Service Deployment**: Creates and manages Snowflake Cortex Search Services for semantic search over ingested documents
3. **RAG Playground**: Interactive chat interface that queries deployed search services and generates responses using Snowflake Cortex LLMs
4. **Cost & Quality Analytics**: Monitoring dashboards for tracking credit consumption and response quality

### What Problem It Solves
- Eliminates the need for external document processing infrastructure by using Snowflake-native AI functions
- Provides deterministic RBAC (Role-Based Access Control) for document access through a gatekeeper authentication flow
- Offers transparent cost tracking for AI operations within Snowflake
- Enables multi-strategy document processing (Layout-only, Vision-only, or Hybrid)

### What It Explicitly Does NOT Do
- Does not support document upload from local filesystem (documents must already exist in Snowflake stages)
- Does not perform user authentication via external identity providers (relies on Snowflake's native authentication and hardcoded user-role mappings)
- Does not support real-time streaming ingestion (batch processing only)
- Does not provide horizontal scaling beyond Snowflake warehouse constraints

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
│(Job Builder)   │   │(Execution Engine)│   │(Search Services)│
└────────────────┘   └─────────────────┘   └─────────────────┘
```

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
   - Batch processor initializes table (CREATE TABLE if not exists)
   - Strategy A (Layout): Uses `SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT` for text extraction
   - Strategy B (Hybrid): Quality inspection + vision-based repair for defective chunks
   - Strategy C (Vision Only): Direct vision processing for image-heavy documents
   - GRANT statements execute only for newly created tables with selected roles

3. **Search Service Deployment Flow**
   - User navigates to Deployment tab
   - Selects source table, configures service name, warehouse, target lag
   - Selects embedding model and roles for access grants
   - System generates SQL preview for `CREATE OR REPLACE CORTEX SEARCH SERVICE`
   - On execution, service is created and GRANT USAGE is applied to selected roles

4. **RAG Query Flow**
   - User navigates to RAG Playground
   - Configures search services, model, temperature, retrieval limit
   - User enters query via chat input
   - System calls `SNOWFLAKE.CORTEX.SEARCH_PREVIEW` for each selected service
   - Context chunks are concatenated and formatted as XML prompt
   - `AI_COMPLETE` generates response with configured model
   - Response displayed with optional monitoring analysis

### Control Flow Model
- **Runtime**: Streamlit's reactive execution model (top-to-bottom script re-execution on interaction)
- **State Management**: `st.session_state` for all persistent data (auth context, job queue, chat history)
- **Batch Processing**: Synchronous, blocking execution with progress bars

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
    ├── chat.py               # RAG Playground chat interface
    ├── home.py               # Landing page
    └── logs.py               # System logs viewer
    │
    └── refinery/
        ├── __init__.py
        ├── batch_processor.py    # Core execution engine for document processing
        ├── common.py             # Shared utilities (execute_sql_safe)
        ├── tab_config.py         # Job Builder UI
        ├── tab_deployment.py     # Cortex Search Service deployment UI
        ├── tab_ingestion.py      # Batch execution UI and reporting
        ├── tab_qa.py             # QA Studio for testing
        └── tab_tools.py          # Utility tools
```

### Critical Files Explained

| File | Purpose | Key Dependencies |
|------|---------|------------------|
| `streamlit_app.py` | Entry point; must run within Snowflake Snowpark environment | All view modules, auth_utils |
| `utils/auth_utils.py` | Contains hardcoded `USER_ROLE_MAP` and `STAGE_ACCESS_MAP` - modifying these changes access control | None (standalone) |
| `views/refinery/batch_processor.py` | Core document processing logic; all three strategies implemented here | core_utils, snowflake_utils, prompts |
| `prompts.py` | All AI prompt templates; changes here affect document reconstruction quality | None |

### Unconventional Structures
- **Hardcoded Role Mappings**: `USER_ROLE_MAP` and `STAGE_ACCESS_MAP` in `auth_utils.py` are hardcoded dictionaries rather than database tables, requiring code deployment for user access changes
- **Session State as Database**: All job queues, ingestion history, and audit data stored in `st.session_state` rather than persistent tables
- **Nested Tab Architecture**: `views/admin.py` imports and renders 5 sub-tabs from `views/refinery/` package

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
    "metrics": dict               # Execution metrics
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
    CHUNK_TYPE VARCHAR        -- 'STANDARD' | 'ENHANCED'
)
```

### Implicit Rules & Invariants

1. **Context Locking**: Once authenticated, db/schema/stage cannot be changed without logout
2. **Table Naming**: User-provided table names are stripped of prefixes; full path is always `"{db}"."{schema}"."{table_name}"`
3. **COPY GRANTS**: OVERWRITE mode uses `COPY GRANTS` to preserve existing table permissions
4. **Grant Exclusivity**: RBAC grants only execute when `grant_roles` is populated (new tables only)
5. **Vision Fallback**: If both `layout` and `vision` are True, Hybrid mode runs (Layout + Quality Repair)
6. **Message Limit**: Chat history capped at 30 messages in session state
7. **Image Size Limit**: Images compressed to <3.5MB for Cortex vision processing

### Terminology

| Term | Definition |
|------|------------|
| **Layout Parser** | Snowflake Cortex `AI_PARSE_DOCUMENT` function for text extraction |
| **Vision Parser** | Claude-4-Sonnet via `AI_COMPLETE` for image-based text extraction |
| **Silver Bullet** | Prompt template for document reconstruction with table formatting rules |
| **Surgical Mode** | Delete-then-insert pattern for updating specific file/page ranges |
| **Cortex Search Service** | Snowflake native vector search index over chunk tables |
| **Gatekeeper** | Authentication flow that validates user and stage access |

---

## 5. Detailed Behavior

### Normal Execution Flow

#### Document Ingestion (Batch Processing)
1. **Job Queue Building** (tab_config.py)
   - User selects PDF from stage file list
   - Page count extracted via `pdfinfo_from_bytes`
   - For new tables: role multiselect appears, defaults to all mapped roles
   - Job added to `st.session_state.job_queue`

2. **Batch Execution** (batch_processor.py)
   ```
   For each job in queue:
     1. Skip if status in ['Completed', 'Cancelled']
     2. Resolve table path (strip prefixes, add db.schema)
     3. Determine page range (Full or Range)
     4. SURGICAL: DELETE existing rows for file/page range
     5. CENTRALIZED INIT:
        - OVERWRITE: CREATE OR REPLACE TABLE ... COPY GRANTS
        - NEW TABLE: CREATE TABLE with standard schema
        - EXISTING: Ensure CHUNK_TYPE column exists
     6. STRATEGY A (if layout=True):
        - Build AI_PARSE_DOCUMENT SQL
        - INSERT results into table
        - Capture chunk count
     7. STRATEGY B (if layout=True AND vision=True):
        - Query chunks for quality inspection
        - Identify defects (empty, garbled, table fragments)
        - For each defective page: vision repair
        - UPDATE chunks with repaired text
     8. STRATEGY C (if vision=True AND layout=False):
        - For each page: convert to image, upload to stage
        - Call AI_COMPLETE with vision model
        - INSERT extracted chunks
     9. RBAC GRANTS (if grant_roles populated):
        - For each role: GRANT ALL PRIVILEGES ON TABLE
        - Track success/failure
    10. Update job status and metrics
   ```

3. **Cost Calculation**
   - Layout: 3.33 credits per 1000 pages
   - Vision: Based on token usage × model pricing
   - Aggregated in `batch_metrics`

#### RAG Query Execution
1. User enters query in chat input
2. For each selected service:
   - Call `SNOWFLAKE.CORTEX.SEARCH_PREVIEW(service_path, query_json)`
   - Extract chunks and relevance scores
3. Build XML prompt: `<sys_prompt> + <chat_history> + <rag> + <latest_message>`
4. Check 200k character limit
5. Call `AI_COMPLETE(model, prompt, parameters, show_details=TRUE)`
6. Parse JSON response, extract content and usage
7. Display response, append to chat history
8. Optional: Run monitoring analysis via `AI_CLASSIFY`

### Edge Cases & Failure Modes

| Scenario | Behavior |
|----------|----------|
| PDF not found in stage | Job fails with error message; status set to 'Failed' |
| Table doesn't exist for SURGICAL mode | Blocked at job configuration; error displayed |
| No roles selected for new table | Submission blocked; error message displayed |
| Vision INSERT fails | Exception bubbles up; job status = 'Failed' (no longer silently swallowed) |
| Grant fails | Job status = 'Completed with Warnings'; manual review required |
| Context exceeds 200k chars | Error displayed; user advised to lower retrieval limit |
| User not in USER_ROLE_MAP | Assigned 'PUBLIC' role; access limited to PUBLIC-accessible stages |
| Stage not in STAGE_ACCESS_MAP | Calls `GET_ROLES_WITH_STAGE_ACCESS` stored procedure |

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
Processes all jobs in `st.session_state.job_queue`. Updates job status and metrics in place.

**Side Effects:**
- Creates/modifies tables in specified schema
- Uploads temporary images to stage
- Executes GRANT statements
- Sets `st.session_state.batch_audit` with metrics

#### `utils/snowflake_utils.py`

```python
def retrieve_context(session, config: dict, prompt: str) -> tuple[list, list]
```
Returns (context_chunks, retrieval_metadata) from configured search services.

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
| `retrieve_context` | Session, config dict with services/limit | (chunks list, meta list) | Services must exist |
| `generate_llm_response` | Session, XML prompt <200k chars | Response dict | Prompt length limit enforced |

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
   -- Upload all .py files
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
| Chunk table schema | `batch_processor.py` | All tables have identical columns |
| Image size limit | `core_utils.py:MAX_IMAGE_MB = 3.5` | Images compressed to this limit |

### Technical Debt

1. **Greedy exception handling removed**: Vision INSERT errors now bubble up correctly (fixed in PLAN-13)
2. **Table initialization centralized**: No longer coupled to Layout strategy (fixed in PLAN-13)
3. **QUERY_HISTORY scanning removed**: RBAC now uses explicit role mapping only (fixed in PLAN-13)

### Non-Goals

| Not Implemented | Evidence |
|-----------------|----------|
| Document upload | No file upload handlers; documents must be in stage |
| Real-time ingestion | Batch processing only; no streaming |
| Multi-tenant isolation | Single auth context per session |
| External LLM support | Only Snowflake Cortex models available |
| Custom embedding models | Limited to `EMBEDDING_MODELS` constant |

---

## 12. Change Sensitivity

### Most Fragile Components

| Component | Fragility Reason |
|-----------|------------------|
| `batch_processor.py` | Core execution logic; changes affect all document processing |
| `auth_utils.py` | Hardcoded mappings; changes affect all access control |
| `prompts.py` | Prompt changes affect document quality; no versioning |
| `tab_config.py` | Job payload structure; changes must sync with batch_processor |

### Tightly Coupled Areas

1. **Job Queue Flow**: `tab_config.py` → `batch_processor.py`
   - Job payload structure must match exactly
   - New fields require updates in both files

2. **Auth Context**: `auth_utils.py` → all views
   - All views assume `auth_context` exists with db/schema/stage/user/role
   - Structure changes require updates across all views

3. **Chunk Table Schema**: `batch_processor.py` → `tab_deployment.py`
   - Deployment expects standard chunk table schema
   - Schema changes break search service creation

### Easiest Extension Points

| Component | Extension Type |
|-----------|----------------|
| `EMBEDDING_MODELS` constant | Add new model metadata |
| `LABEL_DEFINITIONS` constant | Add monitoring categories |
| `prompts.py` | Modify prompt templates |
| `USER_ROLE_MAP` | Add user mappings |
| `STAGE_ACCESS_MAP` | Add stage mappings |

### Hardest Extension Points

| Component | Difficulty Reason |
|-----------|-------------------|
| New write mode | Requires changes in tab_config, batch_processor, and grant logic |
| Alternative authentication | Requires replacing entire Gatekeeper flow |
| Custom chunk schema | Requires changes across batch_processor, deployment, and search |
| Multi-stage support | Currently locked to single stage per session |
