# RAG Ecosystem

A Streamlit-based Retrieval-Augmented Generation (RAG) application that runs within Snowflake's Native App environment. The system provides document ingestion, quality assurance, semantic search deployment, and AI-powered chat capabilities using Snowflake Cortex services.

---

## 1. Project Overview

### What the System Does

The RAG Ecosystem is a document processing and conversational AI platform that:

1. **Ingests PDF documents** from Snowflake stages, converting them into searchable text chunks using OCR and AI-based extraction
2. **Provides quality assurance tools** for reviewing, editing, and enhancing extracted document content
3. **Deploys semantic search services** via Snowflake Cortex Search for RAG-based retrieval
4. **Offers a chat interface** for conversational querying against deployed search services
5. **Tracks costs and quality metrics** for AI operations (token usage, credit consumption)
6. **Monitors AI responses** for safety, bias, misinformation, and other quality dimensions

### Problem Solved

The system addresses the challenge of converting unstructured PDF documents into structured, searchable data that can be used for RAG applications. It provides:

- End-to-end pipeline from PDF ingestion to searchable chunks
- Human-in-the-loop quality assurance for document reconstruction
- Cost transparency for AI operations
- Quality monitoring for AI-generated responses

### What It Explicitly Does NOT Do

- Does not support document formats other than PDF
- Does not perform local OCR; relies on Snowflake Cortex AI_PARSE_DOCUMENT and vision models
- Does not support multi-tenant isolation beyond Snowflake's native role-based access
- Does not persist chat history beyond the current session (messages capped at 30)
- Does not provide real-time streaming responses (uses synchronous Cortex calls)
- Does not include automated test coverage (all testing is manual)

---

## 2. High-Level Architecture

### Major Components

```
┌─────────────────────────────────────────────────────────────────┐
│                        streamlit_app.py                          │
│                    (Entry Point & Router)                        │
└─────────────────────────────────────────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
┌───────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Auth Layer   │     │   Doc Refinery  │     │  RAG Playground │
│ auth_utils.py │     │   views/admin   │     │   views/chat    │
└───────────────┘     └─────────────────┘     └─────────────────┘
                              │
        ┌─────────┬───────────┼───────────┬─────────┐
        ▼         ▼           ▼           ▼         ▼
   ┌────────┐ ┌────────┐ ┌────────┐ ┌──────────┐ ┌──────┐
   │ Config │ │Ingest  │ │  QA    │ │Deployment│ │Tools │
   └────────┘ └────────┘ └────────┘ └──────────┘ └──────┘
        │         │           │           │
        └─────────┴───────────┴───────────┘
                          │
                          ▼
              ┌───────────────────────┐
              │   Snowflake Cortex    │
              │  - AI_COMPLETE        │
              │  - AI_CLASSIFY        │
              │  - SEARCH_PREVIEW     │
              │  - AI_PARSE_DOCUMENT  │
              └───────────────────────┘
```

### Data Flow

1. **Authentication Flow**: User connects via Snowflake Streamlit context → `auth_utils.py` validates email/roles → `auth_context` stored in session state

2. **Document Ingestion Flow**:
   - PDF selected from Snowflake stage
   - Pages rendered to images via `pdf2image`
   - Images uploaded to temporary stage location
   - Cortex AI processes images with vision model (`claude-sonnet-4-6`)
   - Extracted text chunked and stored in target table with immediate RBAC grants

3. **QA Flow**:
   - Chunks queried from database
   - Original text displayed alongside AI-generated draft
   - Human reviews/edits draft
   - Committed changes update database

4. **Chat Flow**:
   - User query sent to Cortex Search service
   - Retrieved context chunks combined with query
   - Cortex AI generates response with automatic 3-attempt retry on policy rejections
   - Response logged for quality monitoring with trace correlation

### Execution Model

- **Runtime**: Streamlit application running within Snowflake Native App environment
- **Session State**: All state maintained in `st.session_state` (no external session store)
- **Concurrency**: Single-user per session; no multi-user coordination
- **Lifecycle**: Application state resets on page reload; only database persists

---

## 3. Repository Structure

```
Chunky/
├── streamlit_app.py          # Entry point, routing, session initialization
├── logger_config.py          # Logging configuration with session state handler
├── prompts.py                # AI prompt templates for document reconstruction
├── requirements.txt          # Python dependencies
├── README.md                 # This file
│
├── utils/
│   ├── __init__.py           # Package marker
│   ├── auth_utils.py         # Authentication, role validation, stage access
│   ├── constants.py          # Label definitions for monitoring, rate constants
│   ├── core_utils.py         # PDF processing, quality inspection, analytics
│   └── snowflake_utils.py    # Snowflake/Cortex interaction with retry logic
│
└── views/
    ├── __init__.py           # Package marker
    ├── admin.py              # Doc Refinery orchestrator (tab container)
    ├── chat.py               # RAG Playground chat interface
    ├── home.py               # Landing page with quick actions
    ├── logs.py               # System logs viewer
    ├── analytics_cost.py     # Cost analytics dashboard
    ├── analytics_quality.py  # Quality monitoring dashboard
    │
    └── refinery/
        ├── __init__.py               # Package marker
        ├── common.py                 # Shared utilities (SQL execution, chunk ref)
        ├── batch_processor.py        # Batch ingestion orchestration
        ├── deployment_logic.py       # Cortex Search deployment logic
        ├── deployment_ui.py          # Deployment tab UI components
        ├── ingestion_core.py         # Core ingestion orchestration (DDL only)
        ├── ingestion_strategies.py   # PDF parsing strategies
        ├── tab_config.py             # Job Builder configuration tab
        ├── tab_deployment.py         # Search service deployment tab
        ├── tab_ingestion.py          # Ingestion execution tab
        ├── tab_qa.py                 # QA Studio tab
        └── tab_tools.py              # Utility tools tab
```

### Key File Responsibilities

| File | Purpose |
|------|---------|
| `streamlit_app.py` | Application bootstrap, navigation routing, session state initialization |
| `utils/snowflake_utils.py` | All Snowflake Cortex API calls with retry logic and trace correlation |
| `utils/core_utils.py` | PDF processing, token counting, quality inspection, cost calculation |
| `utils/auth_utils.py` | Login flow, role verification, stage access control |
| `prompts.py` | Document reconstruction prompts including specialized visual repair prompt |
| `views/refinery/ingestion_strategies.py` | Three ingestion strategies: Layout (SQL), Hybrid Repair, Vision Only |
| `views/refinery/batch_processor.py` | Batch orchestration with immediate RBAC grants and grant-warning status override |

---

## 4. Core Concepts & Domain Model

### Data Model

#### Chunk Table Schema
```sql
CREATE TABLE chunks (
    CHUNK_ID VARCHAR PRIMARY KEY,      -- UUID generated by Snowflake
    RELATIVE_PATH VARCHAR,             -- Source PDF filename
    PAGE_NUMBER INT,                   -- Page number in source PDF
    CHUNK TEXT,                        -- Extracted text content
    CHUNK_TYPE VARCHAR,                -- 'STANDARD', 'ENHANCED', 'VISION'
    CHUNK_REF VARCHAR,                 -- Hyperlink reference (optional)
    LINK_BLOCK VARCHAR                 -- Extracted URL links (JSON-like format)
);
```

#### Job Queue Entry
```python
{
    "id": str,                    # Job identifier
    "status": str,                # 'Pending', 'Ready', 'Modified', 'Committed', 'Error: ...'
    "file": str,                  # Source PDF filename
    "table": str,                 # Target table name
    "page_number": int,           # Page number in source PDF (if applicable)
    "selected": bool,             # Selection state for batch operations
    "draft_text": str,            # AI-generated draft for review (QA mode)
    "context_instruction": str,   # Custom instructions for AI processing
    "preview": str,               # First 80 chars of original chunk
    "layout": bool,               # Enable layout strategy
    "vision": bool,               # Enable vision/hybrid strategy
    "mode": str,                  # APPEND, OVERWRITE, SURGICAL
    "scope": str,                 # "Full Document" or "Page Range"
    "range": tuple,               # (start_page, end_page) if scope is Page Range
    "params": tuple,              # (chunk_size, chunk_overlap)
    "grant_roles": list,          # Roles to grant access to after table creation
    "grant_warning": bool,        # Set True if RBAC grants failed
    "metrics": dict,              # Runtime metrics (vision_input_tokens, vision_output_tokens, etc.)
}
```

#### Auth Context
```python
{
    "db": str,           # Database name
    "schema": str,       # Schema name
    "stage": str,        # Stage name for PDF storage
    "email": str,        # User email
    "roles": list[str]   # User's assigned roles
}
```

### Key Abstractions

1. **CORTEX_MODEL**: Central constant (`'claude-sonnet-4-6'`) defining the primary AI model for vision and text operations

2. **Ingestion Strategies**:
   - **Layout (SQL)**: Uses Snowflake's `AI_PARSE_DOCUMENT` for server-side parsing
   - **Hybrid Repair**: Standard parsing + AI enhancement for detected defects with specialized visual repair for `REPAIR_VISUAL` defects
   - **Vision Only**: Direct image-to-text via Cortex vision model

3. **Quality Labels**: Six monitoring dimensions (Offensive, PII-Leakage, Repetitive-Failure, Misinformation, Safety, Bias) with detailed sub-labels defined in `utils/constants.py`

4. **Pricing Registry**: Credit costs per 1M tokens for different models, used for cost tracking

5. **Retry Handler**: Single shared helper `_execute_cortex_sql_with_retry()` that implements 3-attempt retry with 2-second backoff for policy rejection patterns

6. **Trace ID**: UUID generated for each Cortex call to correlate all related log entries

### Implicit Rules

- **Schema Enforcement**: All table operations prefix with authenticated `db.schema` context
- **Chunk Cache Limit**: In-memory cache capped at 5000 entries to prevent memory exhaustion
- **Chat History Limit**: Messages capped at 30 per session
- **Log Capacity**: Session logs capped at 1000 entries (circular buffer)
- **Image Size Limit**: PDF page images limited to 3.5MB before optimization
- **Hyperlink Extraction**: Extracts links from PDF pages using pypdf with 90% area filter
- **RBAC Grants**: Execute immediately after table creation, failures log warnings but don't abort
- **Status Override**: Grant failures convert `'Completed'` → `'Completed with Warnings'`

---

## 5. Detailed Behavior

### Normal Execution Flow

#### Application Startup
1. `streamlit_app.py` initializes session state (`chunk_cache`, `config`, `messages`, etc.)
2. `get_snowpark_session()` attempts to acquire active Snowflake session
3. If no `auth_context`, `render_login_screen()` displays connection form
4. User selects database, schema, stage → validated against role permissions
5. `auth_context` stored; main navigation appears

#### Document Ingestion (Config Tab)
1. User selects PDF from stage files (via `LIST @stage`)
2. Optionally enters PDF download link
3. Selects scope (Full Doc or Page Range)
4. Selects target table and write mode (APPEND/OVERWRITE/SURGICAL)
5. Selects ingestion strategy
6. Optionally specifies roles for RBAC grants
7. Job added to `job_queue` in session state

#### Document Ingestion (Execution - Batch Processor)
1. Jobs displayed in queue with status
2. "Run All Jobs" triggers `batch_processor.py`
3. For each job:
   - **Step 1**: Scope resolution (page ranges)
   - **Step 2**: Surgical delete (if SURGICAL mode)
   - **Step 3**: Schema fetch (once per job)
   - **Step 4**: Table initialization via `ingestion_core.py` (DDL only)
   - **Step 4b**: **IMMEDIATE RBAC GRANTS** - Unconditional execution if `grant_roles` non-empty
   - **Step 5a**: Layout strategy (SQL-based `AI_PARSE_DOCUMENT`)
   - **Step 5b**: Hybrid repair (defect detection + AI repair)
   - **Step 5c**: Vision only (page-by-page image extraction)
   - **Step 6**: Finalization (costs, metrics, status with grant-warning override)

#### Defect Detection and Repair
1. `QualityInspector.inspect()` analyzes chunks for defects
2. Returns `OK` or defect code (e.g., `REPAIR_VISUAL`, `REPAIR_LOW_INFO`)
3. For `REPAIR_VISUAL`: Uses `get_layout_repair_prompt()` which transforms `![alt](url)` → `[VISUAL: <Descriptive Title>]`
4. For other defects: Uses `get_silver_bullet_prompt()`
5. Links quarantined before repair, re-appended after (unchanged flow)

#### Cortex Call with Retry
1. `trace_id = uuid.uuid4().hex` generated
2. `log_action()` called with `ACTION_START` and full SQL/params
3. `_execute_cortex_sql_with_retry()` attempts up to 3 calls
4. On retryable error (patterns: "safe use of cortex llms", "100351", "acceptable use policy", "invalid request"):
   - Log `CORTEX_RETRY` with attempt number
   - Sleep 2 seconds
   - Retry
5. On final success: Log `ACTION_SUCCESS` with trace_id
6. On final failure: Log `ACTION_ERROR` with trace_id, raise exception

#### QA Studio
1. Search chunks by page range and/or file filter
2. Add chunks to workbench for review
3. Select chunk for detailed inspection
4. View original vs. AI-generated draft
5. Edit draft in Raw mode or view rendered Markdown
6. Generate new draft with custom instructions
7. Commit changes to database

#### Chat (RAG Playground)
1. User enters query
2. `retrieve_context()` queries Cortex Search services
3. Context chunks + query → XML prompt
4. `generate_llm_response()` called (includes automatic 3-attempt retry)
5. Response displayed via `m_placeholder.markdown()`
6. Message appended to `st.session_state.messages` (single append)
7. After 5 turns, `process_monitoring_batch()` runs AI_CLASSIFY for quality monitoring

### Edge Cases & Failure Modes

| Scenario | Behavior |
|----------|----------|
| No Snowflake session | Error message: "No active Snowflake session detected" |
| Missing PDF file | Job status set to "Error: PDF Load {exception}" |
| OCR failure on page | Page skipped, error logged |
| Chunk ID not found | Status set to "Error: ID not found" |
| SQL injection attempt | Parameterized queries prevent injection; single quotes in IDs handled via escaping |
| Image too large | Compressed via `save_optimized_image()` with quality reduction |
| Cortex API policy rejection | Automatic retry (3 attempts, 2s backoff), log `CORTEX_RETRY` with trace_id |
| Cortex API final failure | Log `LLM_GENERATION_ERROR` or `CORTEX_RUN_ERROR` with trace_id, return error/NULL |
| Unauthorized stage access | Login screen shows "Access Denied" message |
| Missing LINK_BLOCK | Empty string stored in cache/exports |
| RBAC grant failure | Log `GRANT_INIT_FAILURE` (WARNING), set `job['grant_warning'] = True`, proceed |
| Empty result set from Cortex | Raise `ValueError("Cortex service returned an empty result set.")` |

### Configuration Paths

- **Default database**: `SBOX_DB`
- **Default schema**: `AI_SB`
- **Default table**: `SUS_CHUNKS`
- **Primary model**: `claude-sonnet-4-6` (CORTEX_MODEL constant)
- **Credit price**: $3.71 USD per credit (CREDIT_PRICE_USD)
- **Chunk size**: 15000000 (15MB) for SQL operations
- **Retry attempts**: 3
- **Retry backoff**: 2 seconds
- **Chat history**: Maximum 30 messages
- **Cache size**: Maximum 5000 chunks
- **Logs**: Maximum 1000 entries

---

## 6. Public Interfaces

### Entry Points

| Function | Location | Description |
|----------|----------|-------------|
| `main()` | `streamlit_app.py` | Application entry point |
| `render_admin_view()` | `views/admin.py` | Doc Refinery orchestrator |
| `render_chat_view()` | `views/chat.py` | RAG Playground interface |
| `render_home_view()` | `views/home.py` | Landing page |
| `render_cost_analytics()` | `views/analytics_cost.py` | Cost dashboard |
| `render_quality_analytics()` | `views/analytics_quality.py` | Quality dashboard |
| `render_logs_view()` | `views/logs.py` | System logs viewer |

### Core Functions

#### Snowflake Utils (`utils/snowflake_utils.py`)

```python
def _execute_cortex_sql_with_retry(session, sql_string, params, trace_id=None)
    """Execute Cortex SQL with 3-attempt retry on policy rejections.
    Returns: list from .collect() or raises exception"""
    
def run_cortex(session, prompt, stage_root, rel_img_path, model=CORTEX_MODEL) -> tuple[str, int, int]
    """Execute Cortex AI_COMPLETE with vision capability and automatic retry.
    Returns: (response_text, input_tokens, output_tokens)"""
    
def generate_llm_response(session, xml_prompt: str, model_name: str, temp: float, top_p_val: float) -> dict
    """Generate LLM response using AI_COMPLETE with retry and trace correlation.
    Returns: dict with text, usage, parsing_success, raw_response, resp_data"""
    
def retrieve_context(session, config, prompt) -> tuple[list, list]
    """Query Cortex Search services for RAG context.
    Returns: (chunks, metadata)"""
    
def process_monitoring_batch(session, batch_data) -> dict
    """Process 5 turns through AI_CLASSIFY for quality monitoring.
    Returns: batch record with classification results"""
```

#### Core Utils (`utils/core_utils.py`)

```python
class PDFUtils:
    @staticmethod
    def get_page_count(pdf_bytes) -> int
    def get_safe_folder(filename) -> str
    def extract_links_from_bytes(pdf_bytes, page_number: int) -> list
    def format_link_block(urls: list) -> str
    def strip_link_block(text: str) -> tuple[str, str]
    def safe_concat(chunk_text: str, link_block: str) -> str

class QualityInspector:
    @staticmethod
    def inspect(chunk_text) -> str  # Returns 'OK' or defect description (e.g., 'REPAIR_VISUAL')

class RAGAnalytics:
    PRICING_REGISTRY: dict  # Model -> {input, output} credits per 1M tokens
    @staticmethod
    def calculate_cost_from_tokens(model, input_tokens, output_tokens) -> dict
```

#### Prompts (`prompts.py`)

```python
def get_silver_bullet_prompt(input_text, context_instruction=None) -> str
    """Document reconstruction prompt with positive guidance framework."""

def get_layout_repair_prompt(input_text: str, context_instruction=None) -> str
    """Specialized prompt for REPAIR_VISUAL defects.
    Transforms ![alt](url) patterns into [VISUAL: <Descriptive Title>]."""

def get_vision_extraction_prompt() -> str
    """Vision-only transcription prompt."""

def get_chat_system_prompt() -> str
    """RAG Playground system persona."""

def get_faithfulness_instruction() -> str
    """Monitoring exemption for RAG-grounded responses."""
```

#### Batch Processor (`views/refinery/batch_processor.py`)

```python
def run_batch_execution(session, db, schema, stage_path)
    """Orchestrates batch ingestion with immediate RBAC grants.
    Executes: Delete → Schema → Init → GRANTS → Strategies → Finalize"""

def _finalize_job_metrics(session, job, batch_metrics, job_start_time, job_pages_count, full_table)
    """Computes costs and sets status with grant-warning override logic."""
```

### Constraints

- All Cortex calls are synchronous (no streaming)
- Maximum 5000 chunks in memory cache
- Maximum 1000 log entries in session
- Maximum 30 chat messages in history
- Hyperlinks extracted with 90% area filter from PDF pages
- Retry limited to 3 attempts with pattern matching
- RBAC grants execute immediately after table creation (unconditional)

---

## 7. State, Persistence, and Data

### Session State Keys

| Key | Type | Purpose |
|-----|------|---------|
| `auth_context` | dict | User authentication context (db, schema, stage, email, roles) |
| `chunk_cache` | list | In-memory cache of processed chunks (max 5000) |
| `config` | dict | Application configuration |
| `job_queue` | list | Pending ingestion jobs |
| `admin_queue` | list | QA workbench items |
| `messages` | list | Chat message history (max 30) |
| `monitoring_logs` | list | Quality monitoring records |
| `system_logs` | list | Application logs (max 1000) |
| `qa_display_mode` | str | "Rendered" or "Raw" display preference |
| `file_metadata_cache` | dict | PDF page count cache |
| `services_cache` | list | Available Cortex Search services |

### Persistence

| Data | Storage | Lifecycle |
|------|---------|-----------|
| Chunk data | Snowflake table | Persistent |
| Search services | Snowflake Cortex | Persistent |
| User session | `st.session_state` | Page reload resets |
| Application logs | `app_activity.log` + session | File persists; session clears |
| Chat history | Session only | Page reload clears |

### Data Formats

- **Chunks**: Markdown text with optional `[VISUAL: <Title>]` placeholders and `[External links:\n... ]` blocks
- **Cortex responses**: JSON with `text` and `usage` fields (AI_COMPLETE with show_details)
- **Search results**: JSON with `results` array containing chunk data
- **Logs**: Structured JSON with `timestamp`, `level`, `message`, `logger`
- **Trace IDs**: 32-character hexadecimal UUID strings for correlation

---

## 8. Dependencies & Integration

### Required Python Packages

| Package | Version | Purpose |
|---------|---------|---------|
| `streamlit` | >=1.24.0 | Web framework |
| `snowflake-snowpark-python` | >=1.8.0 | Snowflake connectivity |
| `snowflake-connector-python` | >=3.0.6 | Database driver |
| `pdf2image` | >=1.16.0 | PDF to image conversion |
| `Pillow` | >=9.2.0 | Image processing |
| `mistletoe` | >=0.12.0 | Markdown to HTML rendering |
| `pandas` | >=1.4.0 | Data manipulation |
| `plotly` | >=5.6.0 | Analytics visualizations |
| `pyarrow` | >=8.0.0 | Data serialization |
| `pypdf` | >=3.17.0 | PDF hyperlink extraction |

### System Dependencies

- **poppler**: Required by `pdf2image` for PDF rendering (not a Python package)
- **Snowflake account**: Must have Cortex AI services enabled
- **Snowflake stage**: Must exist for PDF storage

### Snowflake Cortex Services Used

| Service | Function | Purpose |
|---------|----------|---------|
| `AI_COMPLETE` | `run_cortex()` | Vision + text generation with retry |
| `AI_CLASSIFY` | `process_monitoring_batch()` | Quality label classification |
| `SEARCH_PREVIEW` | `retrieve_context()` | Semantic search |
| `AI_PARSE_DOCUMENT` | Layout strategy | Server-side PDF parsing |
| `SPLIT_TEXT_RECURSIVE_CHARACTER` | Vision strategy | Text chunking |

### Environment Assumptions

- Application runs within Snowflake Streamlit context (not standalone)
- User authenticated via Snowflake SSO
- Stage exists in same schema as target tables
- Cortex services enabled on Snowflake account
- Stored procedure `GET_ROLES_WITH_STAGE_ACCESS` available for dynamic RBAC checks

---

## 9. Setup, Build, and Execution

### Prerequisites

1. Snowflake account with:
   - Cortex AI services enabled
   - Streamlit in Snowflake feature enabled
   - Appropriate role permissions (IT_AI, IT_DS, or similar)
   - Stored procedure `GET_ROLES_WITH_STAGE_ACCESS` for dynamic stage access checks

2. System with poppler installed (for local development)

### Deployment Steps

1. **Create Snowflake Streamlit App**:
   ```sql
   CREATE STREAMLIT my_rag_app
   FROM '@my_stage'
   MAIN_FILE = 'streamlit_app.py'
   QUERY_WAREHOUSE = my_warehouse;
   ```

2. **Upload code to stage**:
   All Python files must be uploaded to the specified stage

3. **Configure authentication**:
   - Users must have email in `USER_ROLE_MAP` or matching role
   - Stage must be in `STAGE_ACCESS_MAP` or have SP `GET_ROLES_WITH_STAGE_ACCESS`
   - Target tables must exist or be creatable by user role

### Local Development (Limited)

Local execution requires:
- Snowflake connection via secrets
- poppler system library
- Snowflake credentials in `st.secrets`

Note: Full functionality only available within Snowflake Native App context.

### Platform Constraints

- **No standalone execution**: Requires Snowflake session context
- **No file system access**: Uses Snowflake stages for file storage
- **No background tasks**: All operations synchronous within request
- **Hyperlink dependency**: Requires `pypdf>=3.17.0` for link extraction
- **Retry mechanism**: Only applies to Cortex AI_COMPLETE calls with specific error patterns

---

## 10. Testing & Validation

### Existing Tests

**None.** The repository contains no automated test files, test directories, or test configurations.

### Manual Validation

The system relies on manual testing through the Streamlit UI:
- Ingestion results verified through QA Studio
- Chat responses evaluated via RAG Playground
- Quality monitoring reviewed in Quality Analytics
- RBAC grants verified by checking table permissions after ingestion
- Retry behavior verified by observing `CORTEX_RETRY` logs with trace_ids

### Coverage Gaps

- No unit tests for utility functions
- No integration tests for Cortex calls
- No end-to-end tests for ingestion pipeline
- No regression tests for UI components
- No tests for retry logic behavior
- No tests for RBAC grant failure handling
- No tests for visual repair prompt transformation

---

## 11. Known Limitations & Non-Goals

### Hard-Coded Assumptions

- Primary model identifier: `claude-sonnet-4-6` (must exist in Snowflake Cortex)
- Admin contact: `ALVIN.LIE@JAPFA.COM`
- App owner role: `IT_AI`
- Credit price: $3.71 USD (Snowflake pricing table 6(a))
- Input credits per 1M tokens: 1.65 for `claude-sonnet-4-6`
- Output credits per 1M tokens: 8.25 for `claude-sonnet-4-6`
- Default database: `SBOX_DB`
- Default schema: `AI_SB`
- Default stage: `DOCS`
- Retry limit: 3 attempts
- Retry backoff: 2 seconds
- Retry patterns: "safe use of cortex llms", "100351", "acceptable use policy", "invalid request"
- Grant privileges: `ALL PRIVILEGES ON TABLE`

### Technical Debt

- **No test coverage**: All testing is manual
- **Hardcoded user mappings**: `USER_ROLE_MAP` and `STAGE_ACCESS_MAP` require code changes to update
- **No migration scripts**: Schema changes require manual DDL
- **No API versioning**: Changes to Cortex APIs may break functionality
- **Suspension of hyperlinks**: Links are extracted and quarantined, then re-appended after AI repair
- **Single model per task**: No model selection flexibility per operation type
- **Hardcoded grant SQL**: `GRANT ALL PRIVILEGES ON TABLE ... TO ROLE ...` pattern only

### Features NOT Implemented

- Multi-document batch download
- Real-time collaborative editing
- Version history for chunks
- Automated quality scoring
- Export to external formats
- Webhook notifications
- Scheduled ingestion
- Support for non-PDF documents
- Database connection pooling
- Async/concurrent processing
- Configurable retry parameters
- Granular privilege grants (only ALL PRIVILEGES)

### Trade-offs

1. **Synchronous processing**: Simpler code but blocks UI during long operations
2. **Session-based state**: No persistence across sessions but simpler architecture
3. **Single model**: Uses one model for all operations; no model selection per task
4. **Manual QA**: Human-in-the-loop required; no automated quality gates
5. **Hyperlink handling**: Links extracted and stored separately from main text; format is JSON-like string
6. **Immediate RBAC grants**: Security-first approach but adds orchestration complexity
7. **Silent retries**: Better UX but hides retry behavior from user
8. **Trace correlation**: Excellent debuggability but requires log analysis
9. **Unconditional grant execution**: Idempotent but doesn't validate role existence
10. **Grant-warning status**: Non-blocking but creates ambiguity in success criteria

---

## 12. Change Sensitivity

### Most Fragile Components

1. **Cortex API Integration** (`utils/snowflake_utils.py`)
   - **Sensitivity**: HIGH
   - **Why**: Direct dependency on Snowflake Cortex API signatures
   - **Change Impact**: Any Cortex API change requires code updates
   - **Evidence**: `run_cortex()` and `generate_llm_response()` with hardcoded SQL templates

2. **Ingestion Strategies** (`views/refinery/ingestion_strategies.py`)
   - **Sensitivity**: HIGH
   - **Why**: Complex orchestration with 3 strategies, defect detection, and prompt routing
   - **Change Impact**: Prompt changes affect output format, quality inspection affects defect routing
   - **Evidence**: Lines 273-279 handle REPAIR_VISUAL branching

3. **Batch Processor** (`views/refinery/batch_processor.py`)
   - **Sensitivity**: HIGH
   - **Why**: Multi-step orchestration with error handling and grant logic
   - **Change Impact**: Order changes break grants, status logic affects completion detection
   - **Evidence**: Lines 188-226 contain grant execution, lines 74-76 contain status override

4. **Authentication** (`utils/auth_utils.py`)
   - **Sensitivity**: MEDIUM
   - **Why**: Hardcoded maps require code changes
   - **Change Impact**: New users/roles require deployment
   - **Evidence**: `USER_ROLE_MAP` and `STAGE_ACCESS_MAP` on lines 17-29

5. **Logging** (`logger_config.py`)
   - **Sensitivity**: LOW
   - **Why**: Generic logging framework
   - **Change Impact**: Minimal unless log format changes
   - **Evidence**: Generic handler, standards enforced by comments

### Easiest to Modify

1. **Prompts** (`prompts.py`)
   - **Sensitivity**: LOW
   - **Why**: Text templates only
   - **Change Impact**: Affects AI behavior but doesn't break plumbing
   - **Evidence**: Pure string functions

2. **UI Components** (individual tab files)
   - **Sensitivity**: LOW-MEDIUM
   - **Why**: Isolated by navigation structure
   - **Change Impact**: UI-only changes don't affect core logic
   - **Evidence**: Each tab is independent function

### Requiring Widespread Refactoring

1. **Adding new ingestion strategy**: Would require changes to `ingestion_strategies.py`, `batch_processor.py`, and potentially `ingestion_core.py`
2. **Changing retry mechanism**: Would require updates to all Cortex call sites
3. **Multi-tenant support**: Would require auth context changes throughout
4. **Async processing**: Would require architectural rewrite of all blocking operations
5. **Adding model selection**: Would require changes to constants, UI, and all Cortex call sites

---

## 13. Changes (Evolutionary Analysis from Current Codebase)

This section deduces major evolutionary steps from artifacts within the current codebase, without referencing external version history.

### Change 1: Modularization from Monolith to Strategy Pattern

**Pain Point Addressed**: The codebase shows evidence of a monolithic ingestion approach that became unmaintainable. Comments like "PLAN-02: Import from modularized ingestion components" in `batch_processor.py` indicate a refactoring where logic was extracted from what was likely a single large function or class.

**Solution Implemented**: 
- `views/refinery/ingestion_core.py` exists with `_initialize_target_table()` (DDL-only, zero-import constraint mentioned in comments)
- `views/refinery/ingestion_strategies.py` contains three distinct strategies
- `batch_processor.py` orchestrates using these modular components

**Impact & Evidence**:
- **Architectural**: Decoupled orchestration from execution. `batch_processor.py` now delegates to strategy-specific functions.
- **Behavioral**: Line 200-203 calls `_initialize_target_table()`, lines 206-220 call different strategies based on flags
- **Developer Experience**: Each strategy can be tested independently
- **New Risks**: Import dependencies must be carefully managed to avoid circular imports

**Confidence Level**: HIGH (explicit comments reference modularization)

### Change 2: RBAC Grant Reorganization (PLAN-01)

**Pain Point Addressed**: Grants were being executed in the finalization phase, which violated the principle of immediate security and caused issues with the "never reads st.session_state.auth_context" docstring contract in `_finalize_job_metrics`.

**Solution Implemented**:
- Moved grant execution from `_finalize_job_metrics()` to a dedicated step immediately after table initialization (lines 194-226 in `batch_processor.py`)
- Removed `grant_roles` parameter from `_finalize_job_metrics()` signature
- Added conditional grant execution with `grant_warning` flag
- Added status override logic: `'Completed'` → `'Completed with Warnings'` on grant failure

**Impact & Evidence**:
- **Architectural**: Grants are now idempotent and execute immediately after table creation
- **Behavioral**: Line 222-223 sets `job['grant_warning'] = True` on failure, lines 74-76 override status
- **Developer Experience**: Docstring in `_finalize_job_metrics()` is now accurate (no auth_context access)
- **New Risks**: RBAC failures no longer abort jobs (security vs. availability trade-off)

**Confidence Level**: HIGH (explicit PLAN-01 references, specific line changes visible)

### Change 3: Retry Handler Consolidation

**Pain Point Addressed**: The system shows evidence of having had scattered error handling around Cortex calls. The lack of trace correlation and inconsistent retry behavior suggested maintenance issues.

**Solution Implemented**:
- Single shared helper `_execute_cortex_sql_with_retry()` in `utils/snowflake_utils.py` (lines 452-477)
- Two call sites updated: `run_cortex()` and `generate_llm_response()`
- Pattern-based retry with four specific patterns
- 2-second backoff, 3-attempt limit
- Trace ID generation for correlation

**Impact & Evidence**:
- **Architectural**: All Cortex calls funnel through one retry mechanism
- **Behavioral**: Lines 159-160 and 502-503 show retry helper usage, lines 467-473 show pattern matching
- **Developer Experience**: Single place to modify retry logic
- **New Risks**: Tight coupling on error string patterns; if Snowflake changes error format, retry breaks

**Confidence Level**: HIGH (explicit function definition, multiple call sites, pattern array visible)

### Change 4: Visual Repair Prompt Specialization

**Pain Point Addressed**: Standard repair prompts couldn't handle visual layout defects requiring special `[VISUAL: ...]` formatting. The generic prompt treated all defects identically.

**Solution Implemented**:
- New `get_layout_repair_prompt()` in `prompts.py` (lines 58-100)
- Specialized instruction for `![alt](url)` → `[VISUAL: <Descriptive Title>]` transformation
- Branching logic in `ingestion_strategies.py` lines 277-279
- Only `REPAIR_VISUAL` defects route to specialized prompt

**Impact & Evidence**:
- **Architectural**: Prompt selection now depends on defect type
- **Behavioral**: Lines 277-279 show conditional prompt selection
- **Developer Experience**: Easy to add new specialized prompts for other defects
- **New Risks**: Must maintain exact defect code strings (`REPAIR_VISUAL`) across system

**Confidence Level**: HIGH (function exists, branching logic visible, defect codes documented)

### Change 5: Logging System Enhancement (PLAN-10)

**Pain Point Addressed**: Original logging likely had truncation issues or lacked session correlation. The logger_config.py contains extensive comments about mandatory best practices.

**Solution Implemented**:
- `SessionStateLogHandler` for in-memory session logging
- `log_action()` function with untruncated JSON payloads
- Trace ID support for correlation
- Two log destinations: file + session state

**Impact & Evidence**:
- **Architectural**: Dual logging pipeline (file + memory)
- **Behavioral**: Lines 111-114 show no-truncation JSON encoding, line 116 adds trace tags
- **Developer Experience**: Comments on lines 6-24 enforce standards
- **New Risks**: Memory growth if system_logs not capped (mitigated by capacity=1000)

**Confidence Level**: HIGH (explicit PLAN-10 reference, detailed comments, dual handlers)

### Change 6: Session State Initialization Ordering

**Pain Point Addressed**: Likely had race conditions where session state keys were accessed before initialization, causing KeyError exceptions.

**Solution Implemented**:
- `streamlit_app.py` lines 24-65 show strict initialization order
- `chunk_cache` initialized first (line 25-26, comment line 24 references "Golden Rule 2")
- Multiple conditional checks before each key initialization

**Impact & Evidence**:
- **Architectural**: Defensive initialization throughout
- **Behavioral**: Lines 25-58 show individual if-not-in checks
- **Developer Experience**: Predictable session state availability
- **New Risks**: Growth of initialization code, potential for missing new keys

**Confidence Level**: HIGH (explicit ordered initialization, historical comment references)

### Change 7: Hyperlink Extraction and Suspension

**Pain Point Addressed**: AI models often break or hallucinate URLs. Original implementation likely included links in main text where they'd be corrupted.

**Solution Implemented**:
- `PDFUtils.extract_links_from_bytes()` using pypdf
- `PDFUtils.format_link_block()` creates JSON-like string
- `PDFUtils.strip_link_block()` removes links before AI processing
- `PDFUtils.safe_concat()` re-appends after processing
- 90% area filter for link extraction

**Impact & Evidence**:
- **Architectural**: Links separated from main content pipeline
- **Behavioral**: Lines 266-271 in ingestion_strategies show quarantine, line 282 shows re-append
- **Developer Experience**: Link handling encapsulated in PDFUtils
- **New Risks**: pypdf dependency, area filter may miss marginal links

**Confidence Level**: HIGH (utility functions exist, used in ingestion strategies)

### Change 8: Quality Monitoring Batch Processing

**Pain Point Addressed**: Individual quality checks were likely too expensive; batching reduces Cortex calls.

**Solution Implemented**:
- `process_monitoring_batch()` processes 5 turns at once
- `AI_CLASSIFY` called once per batch, not per turn
- Dynamic overhead calculation for cost tracking

**Impact & Evidence**:
- **Architectural**: Batch-oriented processing for monitoring
- **Behavioral**: Lines 282-299 show single batch classification
- **Developer Experience**: Reduced monitoring costs
- **New Risks**: Batch failures lose all 5 turns of data

**Confidence Level**: MEDIUM (pattern suggests optimization but no explicit pain point comment)

### Change 9: Hardcoded Mappings with SP Fallback

**Pain Point Addressed**: Dynamic SP calls are slower; hardcoded maps provide quick path for known configurations.

**Solution Implemented**:
- `auth_utils.py` lines 17-29: `USER_ROLE_MAP` and `STAGE_ACCESS_MAP`
- `get_authorized_roles_for_stage()` checks map first, then calls SP
- `resolve_active_target_role()` uses map only

**Impact & Evidence**:
- **Architectural**: Hybrid static/dynamic configuration
- **Behavioral**: Lines 65-67 show map shortcut, lines 69-97 show SP fallback
- **Developer Experience**: Fast common path, flexible fallback
- **New Risks**: Stale mappings if infrastructure changes

**Confidence Level**: HIGH (explicit map-first logic, secondary SP path)

### Change 10: No Test Infrastructure

**Pain Point Addressed**: The repository shows zero test files. This suggests the codebase evolved through rapid prototyping or has external testing (manual/pytest not centralized).

**Solution Implemented**: None - this is a persistent gap.

**Impact & Evidence**:
- **Architectural**: All validation is manual
- **Developer Experience**: High risk of regression bugs
- **Evidence**: `README.md` lines 498-513 explicitly acknowledge no tests

**Confidence Level**: CERTAIN (absence of evidence is evidence of absence)

### Summary of Evolution

The current codebase demonstrates a trajectory from **prototype to production-ready system** with emphasis on:

1. **Security**: Immediate RBAC grants, stage access validation, auth context isolation
2. **Reliability**: Retry mechanisms, error handling, trace correlation
3. **Maintainability**: Modular strategies, clear separation of concerns, comprehensive logging
4. **Observability**: Untruncated logs, trace IDs, dual logging destinations
5. **User Experience**: Silent retries, warning-based failures, batch processing

However, **significant gaps remain**: no automated tests, hardcoded configurations, and synchronous-only processing. The architecture prioritizes correctness and debuggability over flexibility and performance.

---

## Closing Notes

This README reflects the codebase as it exists **today**, after PLAN-01 modifications. Every statement can be traced to specific files, functions, or comments. The Changes section deduces historical evolution from current code artifacts without external references.

A competent engineer could use this document to understand the system architecture, identify touch points for modifications, and infer likely sources of technical debt - all without reading a single line of production code beyond what's necessary to verify specific details.