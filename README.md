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
   - Extracted text chunked and stored in target table

3. **QA Flow**:
   - Chunks queried from database
   - Original text displayed alongside AI-generated draft
   - Human reviews/edits draft
   - Committed changes update database

4. **Chat Flow**:
   - User query sent to Cortex Search service
   - Retrieved context chunks combined with query
   - Cortex AI generates response
   - Response logged for quality monitoring

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
│   ├── __init__.py           # Package marker with SP documentation
│   ├── auth_utils.py         # Authentication, role validation, stage access
│   ├── constants.py          # Label definitions for monitoring, rate constants
│   ├── core_utils.py         # PDF processing, quality inspection, analytics
│   └── snowflake_utils.py    # Snowflake/Cortex interaction functions
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
        ├── __init__.py           # Package marker
        ├── common.py             # Shared utilities (SQL execution, chunk ref building)
        ├── batch_processor.py    # Batch ingestion orchestration
        ├── deployment_logic.py   # Cortex Search deployment logic
        ├── deployment_ui.py      # Deployment tab UI components
        ├── ingestion_core.py     # Core ingestion orchestration
        ├── ingestion_strategies.py # PDF parsing strategies (Layout, Hybrid, Vision)
        ├── tab_config.py         # Job Builder configuration tab
        ├── tab_deployment.py     # Search service deployment tab
        ├── tab_ingestion.py      # Ingestion execution tab
        ├── tab_qa.py             # QA Studio tab
        └── tab_tools.py          # Utility tools tab
```

### Key File Responsibilities

| File | Purpose |
|------|---------|
| `streamlit_app.py` | Application bootstrap, navigation routing, session state initialization |
| `utils/snowflake_utils.py` | All Snowflake Cortex API calls (AI_COMPLETE, AI_CLASSIFY, SEARCH_PREVIEW) |
| `utils/core_utils.py` | PDF processing, token counting, quality inspection, cost calculation |
| `utils/auth_utils.py` | Login flow, role verification, stage access control |
| `prompts.py` | Document reconstruction prompts with positive guidance framework |
| `views/refinery/ingestion_strategies.py` | Three ingestion strategies: Layout (SQL), Hybrid Repair, Vision Only |

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
    "id": str,                    # CHUNK_ID
    "status": str,                # 'Pending', 'Ready', 'Modified', 'Committed', 'Error: ...'
    "file": str,                  # Source PDF filename
    "table": str,                 # Target table name
    "page_number": int,           # Page number in source PDF
    "selected": bool,             # Selection state for batch operations
    "draft_text": str,            # AI-generated draft for review
    "context_instruction": str,   # Custom instructions for AI processing
    "preview": str                # First 80 chars of original chunk
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
   - **Hybrid Repair**: Standard parsing + AI enhancement for detected defects
   - **Vision Only**: Direct image-to-text via Cortex vision model

3. **Quality Labels**: Six monitoring dimensions (Offensive, PII-Leakage, Repetitive-Failure, Misinformation, Safety, Bias) with detailed sub-labels defined in `utils/constants.py`

4. **Pricing Registry**: Credit costs per 1M tokens for different models, used for cost tracking

### Implicit Rules

- **Schema Enforcement**: All table operations prefix with authenticated `db.schema` context
- **Chunk Cache Limit**: In-memory cache capped at 5000 entries to prevent memory exhaustion
- **Chat History Limit**: Messages capped at 30 per session
- **Log Capacity**: Session logs capped at 1000 entries (circular buffer)
- **Image Size Limit**: PDF page images limited to 3.5MB before optimization
- **Hyperlink Extraction**: Extracts links from PDF pages using pypdf with 90% area filter

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
6. Job added to `job_queue` in session state

#### Document Ingestion (Execution)
1. Jobs displayed in queue with status
2. "Run All Jobs" triggers `batch_processor.py`
3. Each job processed according to strategy:
   - **Layout**: SQL-based `AI_PARSE_DOCUMENT` call with hyperlink extraction
   - **Hybrid**: Layout + defect detection + AI repair
   - **Vision**: Page-by-page image extraction and AI transcription
4. Chunks written to target table with LINK_BLOCK column
5. Metrics (tokens, counts) updated in job dict

#### QA Studio
1. Search chunks by page range and/or file filter
2. Add chunks to workbench for review
3. Select chunk for detailed inspection
4. View original vs. AI-generated draft
5. Edit draft in Raw mode or view rendered Markdown
6. Generate new draft with custom instructions
7. Commit changes to database

### Edge Cases & Failure Modes

| Scenario | Behavior |
|----------|----------|
| No Snowflake session | Error message: "No active Snowflake session detected" |
| Missing PDF file | Job status set to "Error: PDF Load {exception}" |
| OCR failure on page | Page skipped, error logged |
| Chunk ID not found | Status set to "Error: ID not found" |
| SQL injection attempt | Parameterized queries prevent injection; single quotes in IDs handled via escaping |
| Image too large | Compressed via `save_optimized_image()` with quality reduction |
| Cortex API error | Exception caught, logged, status updated with error message |
| Unauthorized stage access | Login screen shows "Access Denied" message |
| Missing LINK_BLOCK | Empty string stored in cache/exports |

### Configuration Paths

- **Default database**: `SBOX_DB` (hardcoded in initial config)
- **Default schema**: `AI_SB`
- **Default table**: `SUS_CHUNKS`
- **Primary model**: `claude-sonnet-4-6` (CORTEX_MODEL constant)
- **Credit price**: $3.71 USD per credit (CREDIT_PRICE_USD)
- **Chunk size**: 15000000 (15MB) for SQL operations

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
def run_cortex(session, prompt, stage_root, rel_img_path, model=CORTEX_MODEL) -> tuple[str, int, int]
    """Execute Cortex AI_COMPLETE with vision capability.
    Returns: (response_text, input_tokens, output_tokens)"""

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
    def strip_link_block(text: str) -> tuple
    def safe_concat(chunk_text: str, link_block: str) -> str

class QualityInspector:
    @staticmethod
    def inspect(chunk_text) -> str  # Returns 'OK' or defect description

class RAGAnalytics:
    PRICING_REGISTRY: dict  # Model -> {input, output} credits per 1M tokens
    @staticmethod
    def calculate_cost_from_tokens(model, input_tokens, output_tokens) -> dict
```

#### Prompts (`prompts.py`)

```python
def get_silver_bullet_prompt(input_text, context_instruction=None) -> str
    """Document reconstruction prompt with positive guidance framework."""

def get_vision_extraction_prompt() -> str
    """Vision-only transcription prompt."""

def get_chat_system_prompt() -> str
    """RAG Playground system persona."""

def get_faithfulness_instruction() -> str
    """Monitoring exemption for RAG-grounded responses."""
```

### Constraints

- All Cortex calls are synchronous (no streaming)
- Maximum 5000 chunks in memory cache
- Maximum 1000 log entries in session
- Maximum 30 chat messages in history
- Hyperlinks extracted with 90% area filter from PDF pages

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
- **Cortex responses**: JSON with `text` and `usage` fields
- **Search results**: JSON with `results` array containing chunk data
- **Logs**: Structured JSON with `timestamp`, `level`, `message`, `logger`

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
| `AI_COMPLETE` | `run_cortex()` | Vision + text generation |
| `AI_CLASSIFY` | `process_monitoring_batch()` | Quality label classification |
| `SEARCH_PREVIEW` | `retrieve_context()` | Semantic search |
| `AI_PARSE_DOCUMENT` | Layout strategy | Server-side PDF parsing |
| `SPLIT_TEXT_RECURSIVE_CHARACTER` | Vision strategy | Text chunking |

### Environment Assumptions

- Application runs within Snowflake Streamlit context (not standalone)
- User authenticated via Snowflake SSO
- Stage exists in same schema as target tables
- Cortex services enabled on Snowflake account

---

## 9. Setup, Build, and Execution

### Prerequisites

1. Snowflake account with:
   - Cortex AI services enabled
   - Streamlit in Snowflake feature enabled
   - Appropriate role permissions (IT_AI, IT_DS, or similar)

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

---

## 10. Testing & Validation

### Existing Tests

**None.** The repository contains no automated test files, test directories, or test configurations.

### Manual Validation

The system relies on manual testing through the Streamlit UI:
- Ingestion results verified through QA Studio
- Chat responses evaluated via RAG Playground
- Quality monitoring reviewed in Quality Analytics

### Coverage Gaps

- No unit tests for utility functions
- No integration tests for Cortex calls
- No end-to-end tests for ingestion pipeline
- No regression tests for UI components

---

## 11. Known Limitations & Non-Goals

### Hard-Coded Assumptions

- Primary model identifier: `claude-sonnet-4-6` (must exist in Snowflake Cortex)
- Admin contact: `ALVIN.LIE@JAPFA.COM`
- App owner role: `IT_AI`
- Credit price: $3.71 USD (Snowflake pricing table 6(a))
- Input credits per 1M tokens: 1.65 for `claude-sonnet-4-6`
- Output credits per 1M tokens: 8.25 for `claude-sonnet-4-6`

### Technical Debt

- **No test coverage**: All testing is manual
- **Hardcoded user mappings**: `USER_ROLE_MAP` and `STAGE_ACCESS_MAP` require code changes to update
- **No migration scripts**: Schema changes require manual DDL
- **No API versioning**: Changes to Cortex APIs may break functionality
- **Suspension of hyperlinks**: Links are extracted and quarantined, then re-appended after AI repair

### Features NOT Implemented

- Multi-document batch download
- Real-time collaborative editing
- Version history for chunks
- Automated quality scoring
- Export to external formats
- Webhook notifications
- Scheduled ingestion
- Support for non-PDF documents

### Trade-offs

1. **Synchronous processing**: Simpler code but blocks UI during long operations
2. **Session-based state**: No persistence across sessions but simpler architecture
3. **Single model**: Uses one model for all operations; no model selection per task
4. **Manual QA**: Human-in-the-loop required; no automated quality gates
5. **Hyperlink handling**: Links extracted and stored separately from main text; format is JSON-like string

---

## 12. Change Sensitivity

### High-Risk Areas (Fragile)

| Area | Risk | Impact of Changes |
|------|------|-------------------|
| `CORTEX_MODEL` constant | Model availability | All AI operations fail if model unavailable |
| `PRICING_REGISTRY` | Pricing accuracy | Cost calculations incorrect |
| `auth_utils.py` | Access control | Security bypass or lockout |
| `ingestion_strategies.py` | Document processing | Data corruption or loss |
| `prompts.py` | AI behavior | Output quality degradation |

### Tightly Coupled Components

1. **Ingestion pipeline**: `tab_ingestion.py` → `batch_processor.py` → `ingestion_strategies.py` → `snowflake_utils.py`
   - Changes to any layer require updates to callers

2. **QA workflow**: `tab_qa.py` ↔ `admin_queue` session state
   - Widget keys must match session state access patterns

3. **Cost calculation**: `PRICING_REGISTRY` ↔ `calculate_cost_from_tokens()` ↔ monitoring batch
   - Model names must match across all rate dictionaries

### Extension Points (Easiest to Modify)

1. **New monitoring labels**: Add to `LABEL_DEFINITIONS` in `constants.py`
2. **New prompt templates**: Add function to `prompts.py`
3. **New analytics views**: Add function to respective `views/` module
4. **New ingestion strategies**: Add function to `ingestion_strategies.py` with same signature

### Modification Difficulty

| Change Type | Difficulty | Reason |
|-------------|------------|--------|
| Add new label | Easy | Add to dict in constants |
| Add new prompt | Easy | Add function to prompts.py |
| Change AI model | Medium | Update constant + pricing + test |
| Add new ingestion strategy | Medium | Follow existing pattern |
| Modify chunk schema | Hard | Multiple tables, queries affected |
| Add authentication method | Hard | Deep integration with Snowflake context |
| Change UI framework | Very Hard | Full rewrite required |