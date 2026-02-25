# Chunky - RAG Ecosystem

A Streamlit-based application for document processing, semantic search deployment, and RAG (Retrieval-Augmented Generation) testing, designed to run within Snowflake's Snowpark environment.

---

## 1. Project Overview

### What the System Does

Chunky is an enterprise-grade document refinement and RAG testing suite that operates entirely within Snowflake. The application provides:

1. **Document Ingestion Pipeline**: Converts PDF documents stored in Snowflake stages into searchable text chunks using two parsing strategies:
   - **Layout Parser**: SQL-based extraction using `SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT` with `TO_FILE()` for direct file access (no DIRECTORY() dependency)
   - **Vision Parser**: Multimodal AI extraction using `SNOWFLAKE.CORTEX.AI_COMPLETE` with image analysis for complex documents (charts, tables, slides)

2. **Quality Assurance Studio**: Interactive editor for inspecting and manually correcting OCR-extracted text with AI-assisted reconstruction

3. **Cortex Search Deployment**: Automated creation and management of Snowflake Cortex Search Services with embedding model selection and automated RBAC grant execution

4. **RAG Playground**: Chat interface for testing deployed search services with configurable LLM models, temperature, and retrieval parameters

5. **Analytics Dashboard**: Cost tracking (credits, USD, IDR) and quality monitoring using a secondary "Judge LLM" to classify responses across six severity categories

6. **Automated Privilege Management**: Job-level grant execution with retry logic for table access (ALL PRIVILEGES) and search service access (USAGE)

### What Problem It Solves

The system bridges the gap between raw document storage and semantic search by:
- Automating PDF-to-chunk conversion with quality detection and AI repair
- Providing a unified interface for Cortex Search Service lifecycle management
- Enabling iterative testing of RAG configurations with cost visibility
- Implementing enterprise-grade access control through stage-based authentication
- Automating RBAC grants with fault-tolerant retry mechanisms

### What It Explicitly Does NOT Do

- **Does not support local development**: The application requires an active Snowflake Snowpark session and will display an error if run outside Snowflake
- **Does not process documents outside Snowflake stages**: All PDFs must be uploaded to a configured Snowflake stage before processing
- **Does not persist chat history across sessions**: Messages are stored in `st.session_state` and limited to 30 messages
- **Does not provide production monitoring**: Quality analytics are explicitly marked as "R&D/Playground exclusive" and not for production deployment logging
- **Does not support chunking across page boundaries**: Each chunk is strictly bounded by its source page
- **Does not use DIRECTORY() SQL function**: Document processing uses `TO_FILE()` direct file access pattern

---

## 2. High-Level Architecture

### Major Components

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           streamlit_app.py                                   │
│  (Entry Point, Session State, Navigation, Authentication Gatekeeper)        │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
        ┌───────────────────┐ ┌─────────────────┐ ┌──────────────────┐
        │    Views Layer    │ │   Utils Layer   │ │   Prompts Layer  │
        ├───────────────────┤ ├─────────────────┤ ├──────────────────┤
        │ home.py           │ │ auth_utils.py   │ │ prompts.py       │
        │ chat.py           │ │ snowflake_utils │ │ (AI templates)   │
        │ admin.py          │ │ core_utils.py   │ │                  │
        │ analytics_cost.py │ │ constants.py    │ │                  │
        │ analytics_quality │ │                 │ │                  │
        │ logs.py           │ │                 │ │                  │
        └───────────────────┘ └─────────────────┘ └──────────────────┘
                    │
                    ▼
        ┌───────────────────┐
        │ refinery/ Package │
        ├───────────────────┤
        │ tab_config.py     │ (Job Management)
        │ tab_ingestion.py  │ (Batch Execution)
        │ tab_qa.py         │ (QA Studio)
        │ tab_deployment.py │ (Cortex Search)
        │ tab_tools.py      │ (Maintenance)
        │ batch_processor.py│ (Core Logic)
        │ common.py         │ (Shared Utils)
        └───────────────────┘
```

### Component Responsibilities

| Component | Responsibility |
|-----------|---------------|
| [`streamlit_app.py`](streamlit_app.py:1) | Application entry point, page config, session state initialization, navigation routing, authentication gatekeeper integration |
| [`utils/auth_utils.py`](utils/auth_utils.py:1) | Stage-based authentication, role verification, user identity mapping, role resolution, login UI |
| [`utils/snowflake_utils.py`](utils/snowflake_utils.py:1) | Snowflake session management, Cortex AI calls (AI_COMPLETE, AI_CLASSIFY, SEARCH_PREVIEW), SQL execution helpers, grant retry logic |
| [`utils/core_utils.py`](utils/core_utils.py:1) | PDF processing, image optimization, quality inspection, cost calculation, text comparison |
| [`utils/constants.py`](utils/constants.py:1) | Financial conversion rates, monitoring label definitions, embedding model metadata |
| [`prompts.py`](prompts.py:1) | Centralized AI prompt templates for document reconstruction and chat |
| [`views/chat.py`](views/chat.py:1) | RAG Playground interface with configuration, chat, and retrieval inspection |
| [`views/admin.py`](views/admin.py:1) | Doc Refinery orchestrator importing all tab renderers |
| [`views/refinery/batch_processor.py`](views/refinery/batch_processor.py:1) | Core document processing logic with Layout/Vision/Hybrid strategies, surgical stop-logic, post-job grant execution |
| [`views/refinery/tab_deployment.py`](views/refinery/tab_deployment.py:1) | Cortex Search Service creation, alteration, automated RBAC grant execution |

### Data Flow

#### Document Ingestion Flow
```
1. User selects PDF from Stage → tab_config.py (Job Builder)
2. Job added to queue → st.session_state.job_queue
3. User triggers batch → tab_ingestion.py → batch_processor.py
4. Surgical Delete (if SURGICAL mode) → DELETE with try-except, cancellation on failure
5. Layout Parser (SQL) → AI_PARSE_DOCUMENT(TO_FILE(...)) → SPLIT_TEXT_RECURSIVE_CHARACTER
6. Quality Inspector → detects defects (loops, table issues, syntax noise)
7. Vision Parser (if enabled) → AI_COMPLETE with page images → repairs defects
8. Post-Job Grant Execution → resolve_active_target_role() → execute_grant_with_retry()
9. Chunks written to target table → RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE
```

#### RAG Query Flow
```
1. User configures services → chat.py scans for Cortex Search Services
2. User submits query → retrieve_context() calls SEARCH_PREVIEW
3. Context chunks assembled → XML prompt constructed
4. AI_COMPLETE called with model, temperature, top_p
5. Response displayed → retrieval metadata stored
6. Every 5 turns → process_monitoring_batch() runs AI_CLASSIFY across 6 categories
```

### Execution Model

- **Runtime**: Streamlit application running within Snowflake Snowpark container
- **Session Management**: All state stored in `st.session_state` (no external session store)
- **Authentication**: Gatekeeper pattern - user must authenticate to a Stage before accessing any functionality
- **Batch Processing**: Synchronous execution with progress bars; no job queue or async processing
- **Grant Execution**: Immediate execution after each job with 3-second delay retry on failure

---

## 3. Repository Structure

```
Chunky/
├── streamlit_app.py          # Main entry point (135 lines)
├── logger_config.py          # Logging configuration (126 lines)
├── prompts.py                # AI prompt templates (121 lines)
├── requirements.txt          # Python dependencies (20 lines)
├── README.md                 # This file
│
├── utils/                    # Utility modules
│   ├── __init__.py           # Package marker
│   ├── auth_utils.py         # Authentication, authorization, role resolution (270 lines)
│   ├── constants.py          # Constants & label definitions (189 lines)
│   ├── core_utils.py         # Core utilities (383 lines)
│   └── snowflake_utils.py    # Snowflake interaction, grant retry (545 lines)
│
└── views/                    # View modules
    ├── __init__.py           # Package marker
    ├── home.py               # Home page (81 lines)
    ├── chat.py               # RAG Playground (215 lines)
    ├── admin.py              # Doc Refinery orchestrator (24 lines)
    ├── analytics_cost.py     # Cost analytics (311 lines)
    ├── analytics_quality.py  # Quality analytics (191 lines)
    ├── logs.py               # System logs viewer (128 lines)
    │
    └── refinery/             # Doc Refinery package
        ├── __init__.py       # Package marker
        ├── common.py         # Shared utilities (23 lines)
        ├── batch_processor.py# Core processing logic (385 lines)
        ├── tab_config.py     # Job management (235 lines)
        ├── tab_ingestion.py  # Batch execution, UI with Access Granted column (230 lines)
        ├── tab_qa.py         # QA Studio (380 lines)
        ├── tab_deployment.py # Cortex Search deployment with auto-grant (670 lines)
        └── tab_tools.py      # Maintenance tools (12 lines)
```

### Critical File Purposes

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| [`streamlit_app.py`](streamlit_app.py:24) | Session state initialization | `st.session_state.config`, `st.session_state.messages`, `st.session_state.auth_context` |
| [`utils/auth_utils.py`](utils/auth_utils.py:110) | Role resolution and login UI | `resolve_active_target_role()`, `render_login_screen()`, `get_authorized_roles_for_stage()`, `logout()` |
| [`utils/snowflake_utils.py`](utils/snowflake_utils.py:514) | Grant execution with retry | `execute_grant_with_retry()`, `retrieve_context()`, `generate_llm_response()`, `process_monitoring_batch()` |
| [`utils/core_utils.py`](utils/core_utils.py:275) | Quality inspection | `QualityInspector.inspect()`, `PDFUtils.get_page_count()`, `RAGAnalytics.calculate_cost_from_tokens()` |
| [`views/refinery/batch_processor.py`](views/refinery/batch_processor.py:30) | Document processing | `run_batch_execution()` - handles Layout, Vision, Hybrid strategies, surgical stop-logic, post-job grants |

---

## 4. Core Concepts & Domain Model

### Authentication Model: Stage-Based Gatekeeper

The application implements a strict authentication pattern where users must connect to a specific Snowflake Stage before accessing any functionality.

**Authentication Flow**:
1. User enters Database, Schema, and Stage names in login form
2. System retrieves user email from `st.user` or `st.secrets`
3. Identity verified against hardcoded `USER_ROLE_MAP` or via `INFORMATION_SCHEMA.QUERY_HISTORY`
4. Stage access verified against `STAGE_ACCESS_MAP` or via `GET_ROLES_WITH_STAGE_ACCESS` stored procedure
5. Intersection of user roles and stage-authorized roles determines access
6. On success, `st.session_state.auth_context` is set with `{db, schema, stage, user, role}`

**Hardcoded Mappings** (in [`utils/auth_utils.py`](utils/auth_utils.py:16)):
```python
USER_ROLE_MAP = {
    "alvin.lie@japfa.com": ["IT_AI", "IT_DS", "IT_CSSWEB_AI"],
    "jordan.gani@japfa.com": ["IT_DS"],
    "admin@japfa.com": ["ACCOUNTADMIN", "IT_AI"]
}

STAGE_ACCESS_MAP = {
    "SBOX_DB.AI_SB.DOCS": ["IT_AI", "IT_BI", "IT_DS", "IT_CSSWEB_AI"]
}
```

### Role Resolution Priority

The [`resolve_active_target_role()`](utils/auth_utils.py:110) function determines the target role for grant execution:

1. **Single Map Role**: If user has only one mapped role in `USER_ROLE_MAP`, use it immediately
2. **History Scan**: Query `INFORMATION_SCHEMA.QUERY_HISTORY` for most recent role, matching both full email and email prefix (SSO compatibility)
3. **Fallback to Map[0]**: Use first mapped role, or session role, or `PUBLIC`

**Output**: UPPERCASE role name, ready for SQL execution with double-quotes.

### Grant Execution with Retry

The [`execute_grant_with_retry()`](utils/snowflake_utils.py:514) function handles privilege grants:

1. **Attempt 1**: Execute SQL, log success with `[USER: email]` prefix
2. **On Failure**: Log error with `[USER: email]` prefix, SQL command, and details
3. **Delay**: 3-second `time.sleep(3)`
4. **Attempt 2**: Execute SQL again
5. **On Final Failure**: Log error, return `"Failed"`
6. **Success**: Return the role name

### Chunk Data Model

Chunks are stored in Snowflake tables with the following schema:

| Column | Type | Description |
|--------|------|-------------|
| `CHUNK_ID` | VARCHAR | Unique identifier (format: `CHK_<UUID>`) |
| `RELATIVE_PATH` | VARCHAR | Source PDF filename |
| `PAGE_NUMBER` | NUMBER | Source page number (1-indexed, explicit cast) |
| `CHUNK` | VARCHAR | Text content |
| `CHUNK_TYPE` | VARCHAR | Either `STANDARD` (Layout) or `ENHANCED` (Vision/Repaired) |

**Invariants**:
- Chunks never span multiple pages
- `CHUNK_ID` is generated using `UUID_STRING()` 
- Default table name is `SUS_CHUNKS`
- All projected columns use explicit casting (`::VARCHAR`, `::NUMBER`)

### Quality Inspection Taxonomy

The [`QualityInspector`](utils/core_utils.py:275) class detects the following defect types:

| Status | Detection Method | Description |
|--------|-----------------|-------------|
| `OK` | Passes all checks | No defects detected |
| `EMPTY` | Null/empty check | Chunk is empty |
| `REPAIR_LOW_INFO` | Length < 500 chars | Insufficient content |
| `REPAIR_VISUAL` | Contains `![` | Image placeholder detected |
| `REPAIR_LOOP` | Token entropy < 0.20 | Repetitive content detected |
| `REPAIR_TABLE_*` | Mistletoe AST or regex | Table structure issues |
| `REPAIR_NUMBERS` | Phantom space regex | Broken numeric formatting |
| `REPAIR_SYNTAX` | LaTeX escape regex | Syntax noise detected |

### Monitoring Label Definitions

Six classification categories for response quality monitoring (defined in [`utils/constants.py`](utils/constants.py:15)):

| Category | Requires RAG | Purpose |
|----------|-------------|---------|
| Offensive | No | Toxicity, hostility, profanity detection |
| Bias | Yes | Prejudice and unfair generalization |
| Misinformation | Yes | Hallucination and fact contradiction |
| Safety | Yes | Dangerous advice and guardrail bypass |
| PII-Leakage | No | Sensitive data exposure |
| Repetitive-Failure | No | Loop detection and low-quality patterns |

Each category has 10 specific labels with descriptions and examples.

### Financial Constants

```python
CREDIT_TO_USD = 3.71      # 1 Credit = $3.71 USD
USD_TO_IDR = 16500        # 1 USD = Rp 16,500
CREDIT_TO_IDR = 61215     # Derived: 3.71 * 16500
RATE_AI_CLASSIFY = 1.39 / 1e6  # Credits per token for classification
```

---

## 5. Detailed Behavior

### Normal Execution: Application Startup

1. [`streamlit_app.py`](streamlit_app.py:19) calls `st.set_page_config()` with layout="wide"
2. Session state initialized with defaults:
   - `config`: `{db: "SBOX_DB", schema: "AI_SB", services_cache: [], user_id: "user_session_01", target_table: "SUS_CHUNKS"}`
   - `messages`: `[]` (limited to 30 messages)
   - `services_cache`: `[]`
   - `active_config`: `{}`
   - `monitoring_logs`: `[]`
   - `pending_batch`: `[]`
3. `log_action("APP_STARTUP", ...)` logged
4. `main()` called:
   - Attempts to get Snowpark session via `get_snowpark_session()`
   - If no session, displays error and returns
   - If no `auth_context`, renders login screen and returns
   - Otherwise, renders sidebar navigation and routes to selected page

### Normal Execution: Document Ingestion

1. **Job Configuration** ([`tab_config.py`](views/refinery/tab_config.py:9)):
   - User selects PDF from stage (files listed via `LIST @stage PATTERN='.*\.pdf'`)
   - PDF metadata cached in `st.session_state.file_metadata_cache`
   - User configures: target table, write mode (APPEND/OVERWRITE/SURGICAL), parsing strategies, chunk size, overlap
   - Job added to `st.session_state.job_queue`

2. **Batch Execution** ([`batch_processor.py`](views/refinery/batch_processor.py:30)):
   - For each job in queue (skipping `Completed` and `Cancelled`):
     - **Surgical Delete** (if SURGICAL mode): DELETE with try-except; on failure, mark current job as Failed and cancel subsequent jobs targeting same table
     - **Layout Parser** (if enabled): SQL-based extraction using `AI_PARSE_DOCUMENT(TO_FILE(...))` - no DIRECTORY() dependency
     - **Quality Analysis**: Chunks analyzed by `QualityInspector.inspect()`
     - **Vision Repair** (if enabled and defects found): Each defective chunk re-processed with `AI_COMPLETE` using page image
     - **Vision Only** (if Vision only): All pages processed with `AI_COMPLETE` for extraction
     - **Post-Job Grant**: `resolve_active_target_role()` → `execute_grant_with_retry()` for `GRANT ALL PRIVILEGES ON TABLE`
   - Metrics captured: pages processed, tokens used, chunks created, time breakdown, access_granted status

3. **Persistence**: Jobs upserted to `st.session_state.ingestion_history`

### Normal Execution: RAG Chat

1. **Configuration** ([`chat.py`](views/chat.py:24)):
   - User scans for Cortex Search Services in authenticated schema
   - User selects services, model, retrieval limit, temperature, top_p
   - Configuration stored in `st.session_state.active_config`

2. **Query Processing** ([`chat.py`](views/chat.py:86)):
   - User prompt appended to `st.session_state.messages`
   - `retrieve_context()` called for each selected service via `SEARCH_PREVIEW`
   - XML prompt constructed with `<sys_prompt>`, `<chat_history>`, `<rag>`, `<latest_message>`
   - Prompt length checked against 200,000 character limit
   - `generate_llm_response()` calls `AI_COMPLETE` with configured parameters
   - Response appended to messages with retrieval metadata

3. **Monitoring** ([`snowflake_utils.py`](utils/snowflake_utils.py:226)):
   - Every 5 turns, `process_monitoring_batch()` called
   - Batch processed through 6 `AI_CLASSIFY` calls (Offensive, PII, Repetitive, Misinformation, Safety, Bias)
   - Results stored in `st.session_state.monitoring_logs`

### Edge Cases and Error Handling

| Scenario | Behavior |
|----------|----------|
| No Snowflake session | Application displays error: "No active Snowflake session detected. Please run within Snowflake." |
| Authentication failure | User shown error with admin contact email; must retry authentication |
| Stage not found | Error message: "Object Not Found or Access Denied" with troubleshooting steps |
| Context too long (>200k chars) | Error: "Too much context. Lower retrieval limit." |
| LLM response parsing failure | Warning displayed with debug expander showing raw response |
| PDF processing failure | Job status set to 'Failed', error logged, processing continues to next job |
| SQL execution error | Error logged via `log_action()`, user shown error message |
| Image save failure | Item status set to 'Error: Image Save Failed', processing continues |
| Surgical Delete failure | Current job marked 'Failed', subsequent jobs targeting same table marked 'Cancelled', summary notification shown |
| Grant execution failure (Attempt 1) | Logged with `[USER: email]` prefix, 3-second delay, Attempt 2 executed |
| Grant execution failure (Attempt 2) | Job status set to 'Completed with Warnings' (Orange), Access Granted column shows 'Failed' |

### Configuration Paths

| Configuration | Location | Effect |
|---------------|----------|--------|
| `active_config` | `st.session_state` | Controls RAG chat: services, model, limit, temperature, top_p, system prompt |
| `auth_context` | `st.session_state` | Locks user to specific db/schema/stage |
| `job_queue` | `st.session_state` | List of pending document processing jobs |
| `file_metadata_cache` | `st.session_state` | Cached PDF page counts |
| `table_schema_cache` | `st.session_state` | Cached table schema information |
| `admin_service_cache` | `st.session_state` | Cached list of active Cortex Search Services |

---

## 6. Public Interfaces

### CLI Entry Point

```bash
streamlit run streamlit_app.py
```

**Note**: This will fail outside Snowflake environment due to missing Snowpark session.

### View Functions (Internal Routing)

| Function | Module | Description |
|----------|--------|-------------|
| `render_home_view()` | [`views/home.py`](views/home.py:7) | Home page with documentation |
| `render_chat_view()` | [`views/chat.py`](views/chat.py:12) | RAG Playground interface |
| `render_admin_view()` | [`views/admin.py`](views/admin.py:13) | Doc Refinery with 5 tabs |
| `render_cost_analytics()` | [`views/analytics_cost.py`](views/analytics_cost.py:11) | Cost tracking dashboard |
| `render_quality_analytics()` | [`views/analytics_quality.py`](views/analytics_quality.py:10) | Quality monitoring dashboard |
| `render_logs_view()` | [`views/logs.py`](views/logs.py:9) | System logs viewer |

### Refinery Tab Functions

| Function | Module | Description |
|----------|--------|-------------|
| `render_config_tab(session)` | [`views/refinery/tab_config.py`](views/refinery/tab_config.py:9) | Job builder and queue management |
| `render_ingestion_tab(session)` | [`views/refinery/tab_ingestion.py`](views/refinery/tab_ingestion.py:9) | Batch execution interface with Access Granted column |
| `render_qa_tab(session)` | [`views/refinery/tab_qa.py`](views/refinery/tab_qa.py:182) | QA Studio for chunk editing |
| `render_deployment_tab(session)` | [`views/refinery/tab_deployment.py`](views/refinery/tab_deployment.py:37) | Cortex Search deployment with auto-grant |
| `render_tools_tab(session)` | [`views/refinery/tab_tools.py`](views/refinery/tab_tools.py:5) | Maintenance tools |

### Core Utility Functions

| Function | Module | Signature | Description |
|----------|--------|-----------|-------------|
| `get_snowpark_session()` | [`snowflake_utils.py`](utils/snowflake_utils.py:39) | `() -> Session \| None` | Safe wrapper for Snowpark session |
| `retrieve_context()` | [`snowflake_utils.py`](utils/snowflake_utils.py:73) | `(session, config, prompt) -> (list, list)` | RAG context retrieval from Cortex Search |
| `generate_llm_response()` | [`snowflake_utils.py`](utils/snowflake_utils.py:120) | `(session, xml_prompt, model, temp, top_p) -> dict` | LLM completion via AI_COMPLETE |
| `run_cortex()` | [`snowflake_utils.py`](utils/snowflake_utils.py:463) | `(session, prompt, stage_root, image_path, model) -> (str, int, int)` | Multimodal AI_COMPLETE with image |
| `scan_for_services()` | [`snowflake_utils.py`](utils/snowflake_utils.py:48) | `(session, db, schema) -> list` | List Cortex Search Services |
| `execute_grant_with_retry()` | [`snowflake_utils.py`](utils/snowflake_utils.py:514) | `(session, sql_command, user_email, role_name) -> str` | Grant execution with 3-second delay retry |
| `resolve_active_target_role()` | [`auth_utils.py`](utils/auth_utils.py:110) | `(session, email) -> str` | Role resolution with priority fallback |
| `log_action()` | [`logger_config.py`](logger_config.py:97) | `(action_type, details, user_id, level, trace_id) -> None` | Structured logging |

### Prompt Functions

| Function | Module | Returns |
|----------|--------|---------|
| `get_silver_bullet_prompt(input_text, context_instruction)` | [`prompts.py`](prompts.py:5) | Document reconstruction prompt |
| `get_vision_extraction_prompt()` | [`prompts.py`](prompts.py:75) | Vision-only transcription prompt |
| `get_chat_system_prompt()` | [`prompts.py`](prompts.py:86) | RAG chat system prompt |
| `get_faithfulness_instruction()` | [`prompts.py`](prompts.py:100) | Monitoring exemption instruction |

### Quality Inspector

```python
from utils.core_utils import QualityInspector

status = QualityInspector.inspect(text: str) -> str
# Returns: "OK", "EMPTY", "REPAIR_LOW_INFO", "REPAIR_VISUAL", 
#           "REPAIR_LOOP", "REPAIR_TABLE_*", "REPAIR_NUMBERS", "REPAIR_SYNTAX"
```

### Backward Compatibility Notes

- `PromptEngine` is re-exported from [`core_utils.py`](utils/core_utils.py:168) as an alias to the `prompts` module for backward compatibility
- Legacy `config` session state keys are synced from `auth_context` during login
- `services_cache` maintained both in `config.services_cache` and as top-level `st.session_state.services_cache`

---

## 7. State, Persistence, and Data

### Session State Keys

| Key | Type | Purpose | Lifecycle |
|-----|------|---------|-----------|
| `config` | dict | Legacy configuration (db, schema, user_id, target_table) | Cleared on logout |
| `messages` | list | Chat history (max 30 messages) | Cleared on logout |
| `auth_context` | dict | Authentication context (db, schema, stage, user, role) | Cleared on logout |
| `services_cache` | list | Available Cortex Search Services | Session-scoped |
| `active_config` | dict | Active RAG configuration | Session-scoped |
| `monitoring_logs` | list | Batch monitoring results | Session-scoped |
| `pending_batch` | list | Turns awaiting batch processing | Cleared after 5 turns |
| `job_queue` | list | Document processing jobs | Session-scoped |
| `batch_audit` | dict | Last batch execution metrics (includes `jobs_warning`) | Session-scoped |
| `ingestion_history` | list | Historical job records | Session-scoped |
| `system_logs` | list | In-memory log entries (max 1000) | Session-scoped |
| `admin_queue` | list | QA workbench items | Session-scoped |
| `file_metadata_cache` | dict | PDF page counts | Session-scoped |
| `table_schema_cache` | dict | Table schema info | Session-scoped |
| `admin_service_cache` | list | Active Cortex Search Services | Invalidated on deployment |
| `last_deployed_service` | str | Last deployed service name | Cleared after RBAC grant |
| `cortex_sql_preview` | str | Generated DDL preview | Cleared on execute/cancel |
| `qa_results` | DataFrame | QA search results | Session-scoped |

### File System State

| Location | Purpose | Cleanup |
|----------|---------|---------|
| `app_activity.log` | Persistent log file | Manual cleanup |
| `<tempdir>/rag_app_temp/_temp_images/` | Temporary image storage | Cleared by `PDFUtils.clear_temp_images()` |
| `<tempdir>/rag_app_temp/_temp_audit/` | Temporary audit storage | Cleared by `PDFUtils.clear_temp_images()` |

### Snowflake Stage Storage

| Path | Purpose |
|------|---------|
| `@<stage>/` | Source PDF files |
| `@<stage>/_temp_images/<filename>/` | Temporary page images for Vision processing |

### Data Formats

**Chunk Record** (stored in Snowflake table):
```sql
CREATE TABLE chunks (
    CHUNK_ID VARCHAR PRIMARY KEY,
    RELATIVE_PATH VARCHAR,
    PAGE_NUMBER NUMBER,
    CHUNK VARCHAR,
    CHUNK_TYPE VARCHAR DEFAULT 'STANDARD'
);
```

**Job Metrics** (in `job['metrics']`):
```json
{
  "start": 1234567890.0,
  "end": 1234567895.0,
  "duration": 5.0,
  "pages": 10,
  "access_granted": "IT_AI",
  "layout_pages": 10,
  "vision_pages_list": [1, 2, 3],
  "vision_input_tokens": 5000,
  "vision_output_tokens": 2000,
  "standard_cnt": 50,
  "enhanced_cnt": 10,
  "types": {"Repair: REPAIR_LOOP": 5}
}
```

**Monitoring Batch Record** (in `monitoring_logs`):
```json
{
  "batch_id": "uuid",
  "timestamp": "ISO-8601",
  "turns": [...],
  "gen_costs": [...],
  "overhead_cost": 0.0,
  "Offensive": {"labels": [], "score": 0.0},
  "Bias": {"labels": [], "score": 0.0},
  "Misinformation": {"labels": [], "score": 0.0},
  "Safety": {"labels": [], "score": 0.0},
  "PII-Leakage": {"labels": [], "score": 0.0},
  "Repetitive-Failure": {"labels": [], "score": 0.0}
}
```

### Migration Behavior

- Tables are auto-created if they don't exist
- `CHUNK_TYPE` column is added via `ALTER TABLE` if missing
- `SURGICAL` mode deletes existing chunks for specific file/page before re-inserting
- No explicit migration scripts; schema changes handled inline

---

## 8. Dependencies & Integration

### Python Dependencies (from [`requirements.txt`](requirements.txt:1))

| Package | Version | Purpose |
|---------|---------|---------|
| `streamlit` | >=1.24.0 | Web framework |
| `pandas` | >=1.4.0 | Data manipulation |
| `numpy` | >=1.21.0 | Numerical operations |
| `plotly` | >=5.6.0 | Visualization (gauges, charts) |
| `pdf2image` | >=1.16.0 | PDF to image conversion |
| `Pillow` | >=9.2.0 | Image processing |
| `mistletoe` | >=0.12.0 | Markdown AST parsing for table validation |
| `pyarrow` | >=8.0.0 | Data serialization |
| `snowflake-snowpark-python` | >=1.8.0 | Snowflake Snowpark SDK |
| `snowflake-connector-python` | >=3.0.6 | Snowflake connectivity |

### System Dependencies

- **poppler**: Required by `pdf2image` for PDF rendering (not included; must be installed separately)

### External Services

| Service | Integration Point | Purpose |
|---------|-------------------|---------|
| Snowflake Cortex AI | `SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT` | Layout-based PDF parsing with `TO_FILE()` |
| Snowflake Cortex AI | `SNOWFLAKE.CORTEX.AI_COMPLETE` | Text generation and vision analysis |
| Snowflake Cortex AI | `SNOWFLAKE.CORTEX.AI_CLASSIFY` | Response quality classification |
| Snowflake Cortex AI | `SNOWFLAKE.CORTEX.SEARCH_PREVIEW` | Semantic search retrieval |
| Snowflake Cortex AI | `SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER` | Text chunking |
| Snowflake Cortex AI | `SNOWFLAKE.CORTEX.COUNT_TOKENS` | Token counting |
| Snowflake Cortex Search | DDL commands | Search service lifecycle |
| Snowflake RBAC | `GRANT ALL PRIVILEGES ON TABLE` | Table access grants |
| Snowflake RBAC | `GRANT USAGE ON CORTEX SEARCH SERVICE` | Service access grants |

### Stored Procedure Dependencies

| Procedure | Used In | Purpose |
|-----------|---------|---------|
| `GET_ROLES_WITH_STAGE_ACCESS(db, schema, stage)` | [`auth_utils.py`](utils/auth_utils.py:84) | Dynamic role verification for stage access |

### Environment Assumptions

- Application runs within Snowflake Streamlit container
- User authentication handled by Snowflake (email from `st.user`)
- Stage files accessible via `session.file.get_stream()`
- SQL execution via Snowpark `session.sql()`
- SSO usernames may be email prefix (e.g., `ALVIN.LIE`) rather than full email

---

## 9. Setup, Build, and Execution

### Prerequisites

1. Snowflake account with Cortex AI enabled
2. Snowflake Streamlit capability enabled
3. Poppler installed on the execution environment (for PDF rendering)
4. Appropriate roles and permissions (see Authentication Model)

### Deployment Steps

1. **Create Snowflake Streamlit App**:
   ```sql
   CREATE STREAMLIT <db>.<schema>.<app_name>
   FROM '<stage>'
   MAIN_FILE = 'streamlit_app.py';
   ```

2. **Upload Code to Stage**:
   ```sql
   PUT file://streamlit_app.py @<stage> OVERWRITE = TRUE;
   PUT file://logger_config.py @<stage> OVERWRITE = TRUE;
   PUT file://prompts.py @<stage> OVERWRITE = TRUE;
   -- Upload all .py files
   ```

3. **Configure Authentication**:
   - Update `USER_ROLE_MAP` in [`utils/auth_utils.py`](utils/auth_utils.py:16) with authorized users
   - Update `STAGE_ACCESS_MAP` with stage-to-role mappings
   - Or ensure `GET_ROLES_WITH_STAGE_ACCESS` stored procedure exists

4. **Grant Permissions**:
   ```sql
   GRANT USAGE ON STREAMLIT <db>.<schema>.<app_name> TO ROLE <role>;
   ```

### Local Development (Limited)

Local development is **not supported** due to Snowpark session dependency. However, syntax checking can be performed:

```bash
# Install dependencies
pip install -r requirements.txt

# Syntax check (will fail at runtime without Snowflake session)
python -m py_compile streamlit_app.py
```

### Platform Constraints

| Constraint | Impact |
|------------|--------|
| Snowflake-only | Cannot run outside Snowflake environment |
| No async processing | Batch jobs block the UI |
| Session state limits | 30 message cap in chat history |
| Image size limit | 3.5 MB max for Cortex Vision |
| Prompt length limit | 200,000 characters max |

---

## 10. Testing & Validation

### Test Coverage

**No automated tests exist in this repository.** All testing is manual through the Streamlit UI.

### Manual Testing Procedures

1. **Authentication Testing**:
   - Verify login with valid/invalid credentials
   - Test role-based access to different stages
   - Verify disconnect functionality

2. **Document Processing Testing**:
   - Upload test PDFs to stage
   - Configure jobs with various strategies (Layout only, Vision only, Hybrid)
   - Verify chunk quality in target table
   - Test SURGICAL mode for re-processing
   - Verify grant execution success/failure handling

3. **RAG Testing**:
   - Deploy Cortex Search Service
   - Configure and test chat interface
   - Verify retrieval metadata accuracy
   - Test monitoring batch processing (every 5 turns)

4. **Analytics Testing**:
   - Verify cost calculations match expected rates
   - Test quality classification with known problematic responses

5. **RBAC Testing**:
   - Verify automated grants after ingestion
   - Test grant retry on failure
   - Verify warning status and UI display

### Validation Gaps

- No unit tests for utility functions
- No integration tests for Snowflake interactions
- No regression tests for prompt templates
- No performance benchmarks for batch processing
- No validation of cost calculation accuracy against actual Snowflake billing

---

## 11. Known Limitations & Non-Goals

### Explicit Constraints

| Constraint | Location | Impact |
|------------|----------|--------|
| Snowflake-only execution | [`streamlit_app.py`](streamlit_app.py:67) | Application exits if no Snowpark session |
| Stage-based authentication | [`auth_utils.py`](utils/auth_utils.py:113) | Users cannot access app without valid stage permissions |
| Page-bounded chunks | [`tab_config.py`](views/refinery/tab_config.py:103) | Chunks never span multiple pages |
| 30 message limit | [`streamlit_app.py`](streamlit_app.py:37) | Chat history truncated |
| 200k character prompt limit | [`chat.py`](views/chat.py:115) | Context limit error |
| 3.5 MB image limit | [`core_utils.py`](utils/core_utils.py:178) | Images compressed/converted to fit |
| 1000 log entry limit | [`logger_config.py`](logger_config.py:43) | Circular buffer for in-memory logs |
| Progress bar counts only Green | [`batch_processor.py`](views/refinery/batch_processor.py:73) | "Completed with Warnings", "Failed", "Cancelled" excluded |

### Hard-coded Values

| Value | Location | Purpose |
|-------|----------|---------|
| `ADMIN_CONTACT = "ALVIN.LIE@JAPFA.COM"` | [`auth_utils.py`](utils/auth_utils.py:9) | Error message contact |
| `APP_OWNER_ROLE = "IT_AI"` | [`auth_utils.py`](utils/auth_utils.py:10) | Permission checks |
| `CORTEX_MODEL = 'claude-4-sonnet'` | [`snowflake_utils.py`](utils/snowflake_utils.py:33) | Default vision model |
| Default table: `SUS_CHUNKS` | [`streamlit_app.py`](streamlit_app.py:30) | Initial config |
| Default db/schema: `SBOX_DB.AI_SB` | [`streamlit_app.py`](streamlit_app.py:26) | Initial config |
| Grant retry delay: 3 seconds | [`snowflake_utils.py`](utils/snowflake_utils.py:527) | `time.sleep(3)` |

### Non-Goals (Explicitly Not Implemented)

- **Local development support**: Application requires Snowflake session
- **Production monitoring**: Quality analytics are R&D-only
- **Cross-page chunking**: Chunks are strictly page-bounded
- **Persistent chat history**: Messages stored in session state only
- **Async processing**: All operations are synchronous
- **Multi-tenant isolation**: Single authentication context per session

### Technical Debt

- [`tab_qa.py`](views/refinery/tab_qa.py:380): `render_quality_inspector()` is disabled/commented out
- [`analytics_cost.py`](views/analytics_cost.py:89): Monitoring overhead calculation commented out
- [`batch_processor.py`](views/refinery/batch_processor.py:20): `col` imported but not used
- Hardcoded user/role mappings should be configurable

---

## 12. Change Sensitivity

### Most Fragile Components

| Component | Risk | Reason |
|-----------|------|--------|
| [`utils/auth_utils.py`](utils/auth_utils.py:16) | High | Hardcoded user/role mappings; changes require code deployment |
| [`utils/constants.py`](utils/constants.py:7) | High | Financial rates hardcoded; incorrect values affect all cost calculations |
| [`prompts.py`](prompts.py:5) | High | Prompt changes affect all document processing quality |
| [`views/refinery/batch_processor.py`](views/refinery/batch_processor.py:30) | Medium | Core processing logic; changes affect all document ingestion |
| [`utils/snowflake_utils.py`](utils/snowflake_utils.py:226) | Medium | `process_monitoring_batch()` depends on specific SQL syntax for AI_CLASSIFY |

### Tightly Coupled Areas

1. **Authentication ↔ Session State**: `auth_context` must be set before any view can function; all views read from it
2. **Batch Processing ↔ Quality Inspector**: Defect detection logic must match repair prompt expectations
3. **Cost Analytics ↔ Constants**: All cost displays depend on `CREDIT_TO_USD`, `USD_TO_IDR`, `RATE_AI_CLASSIFY`
4. **Deployment ↔ RBAC**: Service deployment must complete before RBAC grants can be applied
5. **Grant Execution ↔ Role Resolution**: Grant SQL depends on correct role resolution output

### Extension Points

| Extension | Approach |
|-----------|----------|
| Add new LLM model | Add to model selectbox in [`chat.py`](views/chat.py:51), add pricing to [`core_utils.py`](utils/core_utils.py:239) `PRICING_REGISTRY` |
| Add new embedding model | Add to [`constants.py`](utils/constants.py:164) `EMBEDDING_MODELS` and `EMBEDDING_PRICING` |
| Add new monitoring category | Add to [`constants.py`](utils/constants.py:15) `LABEL_DEFINITIONS`, update [`snowflake_utils.py`](utils/snowflake_utils.py:253) SQL templates |
| Add new defect type | Add detection method to [`QualityInspector`](utils/core_utils.py:275), add status handling to batch processor |
| Add new view | Create view function, add to navigation in [`streamlit_app.py`](streamlit_app.py:85) |

### Modification Impact Analysis

| Change | Affected Files |
|--------|---------------|
| Modify authentication flow | `streamlit_app.py`, `auth_utils.py`, all views (auth_context usage) |
| Modify chunk schema | `tab_config.py`, `batch_processor.py`, `tab_qa.py`, `tab_deployment.py` |
| Modify prompt templates | `prompts.py`, all files using prompt functions |
| Modify cost calculation | `constants.py`, `core_utils.py`, `analytics_cost.py`, `tab_ingestion.py` |
| Add new Cortex feature | `snowflake_utils.py`, relevant view files |
| Modify grant logic | `snowflake_utils.py`, `batch_processor.py`, `tab_deployment.py` |

---

## Appendix: SQL Reference

### Cortex Search Service DDL Template

```sql
CREATE OR REPLACE CORTEX SEARCH SERVICE "<db>"."<schema>"."CSS_<service_name>"
ON "<target_column>" ATTRIBUTES ("<attr1>", "<attr2>")
WAREHOUSE = "<warehouse>"
TARGET_LAG = '<value> <unit>'
EMBEDDING_MODEL = '<model_name>'
COMMENT = '<comment>'
AS (
    SELECT "<col1>", "<col2>", "<col3>"
    FROM "<db>"."<schema>"."<table>"
)
```

### AI_PARSE_DOCUMENT Usage (PLAN-01: No DIRECTORY())

```sql
WITH PARSED AS (
    SELECT '<filename>' AS RELATIVE_PATH, 
    SNOWFLAKE.CORTEX.AI_PARSE_DOCUMENT(TO_FILE('@<stage>', '<filename>'), PARSE_JSON('{"mode": "LAYOUT"}')) AS J
)
SELECT
    P.RELATIVE_PATH::VARCHAR AS RELATIVE_PATH,
    (pg.value:index::INT+1)::NUMBER AS PAGE_NUMBER,
    ch.value::VARCHAR AS CHUNK,
    CONCAT('CHK_', UUID_STRING())::VARCHAR AS CHUNK_ID,
    'STANDARD'::VARCHAR AS CHUNK_TYPE
FROM PARSED P, LATERAL FLATTEN(input => J:pages) pg,
LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(pg.value:content::VARCHAR, 'markdown', <chunk_size>, <overlap>)) ch
```

### SEARCH_PREVIEW Usage

```sql
SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(
    '<db>.<schema>.<service>',
    '{"query": "<user_query>", "limit": 5, "columns": []}'
)
```

### Grant Execution Templates

```sql
-- Table Grant (Post-Ingestion)
GRANT ALL PRIVILEGES ON TABLE "<db>"."<schema>"."<table>" TO ROLE "<UPPERCASED_ROLE>"

-- Service Grant (Post-Deployment)
GRANT USAGE ON CORTEX SEARCH SERVICE "<db>"."<schema>"."<service>" TO ROLE "<UPPERCASED_ROLE>"
```

---

## Appendix: UI Status Indicators

### Job Status Colors

| Status | Color | Description |
|--------|-------|-------------|
| `Completed` | Green | Successful ingestion with grant |
| `Completed with Warnings` | Orange | Data ingested but grant failed; permissions need manual review |
| `Failed` | Red | Processing error |
| `Cancelled` | Default | Surgical delete failure; subsequent job cancelled |
| `Pending` | Default | Awaiting execution |
| `Running` | Default | Currently processing |

### Batch Audit Summary Metrics

| Metric | Column | Styling |
|--------|--------|---------|
| Success Rate | m1 | Standard |
| Warnings | m2 | Orange with tooltip |
| Processed Pages | m3 | Standard |
| Total Time | m4 | Standard |
| Avg Speed | m5 | Standard |
