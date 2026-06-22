# Chunky

A high-fidelity Retrieval-Augmented Generation (RAG) pipeline deployed as a Snowflake Native App. Chunky specializes in converting complex PDF documents into structured, searchable data using a combination of structural layout parsing and multi-modal Vision AI.

---

## 1. Project Overview

### Operational Purpose
Chunky transforms unstructured PDF files stored in Snowflake stages into high-fidelity Markdown chunks. It solves the "PDF-to-RAG" gap by enforcing a strict 1-chunk-per-page minimum and providing "Surgical Mode" for precise document restructuring.

### Problem Solved
- **OCR Fidelity Gaps**: Combines structural parsing via `AI_PARSE_DOCUMENT` with visual extraction via Vision LLMs to handle complex tables and layouts.
- **Silent Data Loss**: Prevents pages from being omitted from the index by generating synthetic `PLACEHOLDER` chunks when AI extraction fails [`views/refinery/ingestion_strategies/layout.py`](views/refinery/ingestion_strategies/layout.py).
- **Rigid Metadata**: Allows users to map source PDF pages to target indices in a replacement document, enabling precise updates to existing indices [`utils/page_mapping.py`](utils/page_mapping.py).
- **Cortex Cost Opacity**: Provides real-time credit estimation and historical cost tracking based on actual token usage and model-specific pricing [`utils/core_utils.py:346`](utils/core_utils.py:346).

### Explicit Non-Goals
- **Non-PDF Formats**: Does not support `.docx`, `.html`, or `.txt` files.
- **Local OCR**: Relies entirely on Snowflake Cortex; no local Tesseract or PyMuPDF text extraction.
- **Multi-Tenancy**: Does not manage tenants; relies on Snowflake's native RBAC.
- **Persistent Chat**: Chat history is session-only and capped at 30 messages [`streamlit_app.py:43`](streamlit_app.py:43).

---

## 2. High-Level Architecture

### Major Modules & Responsibilities
- **Orchestration**: `streamlit_app.py` routes requests; `views/refinery/batch_processor.py` manages the ingestion job queue and transaction lifecycle.
- **Ingestion Engine**: `views/refinery/ingestion_strategies/` contains modular logic for `layout`, `vision`, and `hybrid` (repair) parsing.
- **Snowflake Interface**: `utils/snowflake_utils.py` wraps Snowpark sessions and Cortex AI calls.
- **Core Utilities**: `utils/core_utils.py` handles PDF image rendering, token counting, and financial calculations.
- **Auth/Gatekeeper**: `utils/auth_utils.py` validates user identities and mapping roles.

### Data Flow
1. **Authentication**: User $\rightarrow$ `auth_utils` $\rightarrow$ Snowflake Session $\rightarrow$ `auth_context`.
2. **Surgical Tagging**: Session $\rightarrow$ `set_query_tag` $\rightarrow$ Snowflake `QUERY_TAG` [`utils/snowflake_utils.py:45`](utils/snowflake_utils.py:45).
3. **Ingestion**: PDF $\rightarrow$ Strategy (Layout/Vision) $\rightarrow$ Chunk Generation $\rightarrow$ Snowflake Table.
4. **Hybrid Repair**: Defective Chunk $\rightarrow$ Vision AI $\rightarrow$ `ENHANCED` Chunk.
5. **RAG Query**: User Query $\rightarrow$ Cortex Search $\rightarrow$ Context Chunks $\rightarrow$ LLM $\rightarrow$ Response.

### Execution Model
- **Runtime**: Synchronous Streamlit application.
- **Processing**: Event-driven batch queue. Jobs are processed sequentially in the `batch_processor` to prevent Snowflake warehouse overload.

---

## 3. Repository Structure

| Path | Purpose | Why it exists |
| :--- | :--- | :--- |
| `streamlit_app.py` | Main Entry Point | Central router and session state manager. |
| `utils/` | Shared Logic | Low-level helpers for Snowflake, PDF, and Auth. |
| `utils/core_utils.py` | Core Math/PDF | Centralizes `PRICING_REGISTRY` and `PDFUtils`. |
| `utils/snowflake_utils.py` | Cortex Wrappers | Isolates Snowpark/Cortex API calls. |
| `views/` | UI Layers | Decouples Streamlit views from business logic. |
| `views/refinery/` | Ingestion Pipeline | Dedicated namespace for the "Refinery" (Ingestion $\rightarrow$ QA). |
| `views/refinery/ingestion_strategies/` | Parsing Logic | Modularizes Layout, Vision, and Hybrid strategies. |
| `prompts.py` | Prompt Registry | Prevents hardcoding AI instructions in views. |
| `logger_config.py` | Audit Log | Centralizes `log_action` for system observability. |

**Note on Layout**: The monolith `views/refinery/ingestion_strategies.py` was eradicated to prevent module resolution conflicts and unpacking crashes. Logic is now strictly in the `ingestion_strategies/` package.

---

## 4. Core Concepts & Domain Model

### Key Abstractions
- **Job**: A unit of work defining a source file, page range, and extraction strategy.
- **Surgical Mode**: A process where source pages are mapped to a target document's indices to allow precise updates.
- **Chunk**: The atomic unit of RAG.
    - `STANDARD`: Layout-parsed.
    - `ENHANCED`: Vision-repaired.
    - `PLACEHOLDER`: Synthetic (ensures page coverage).

### Domain Glossary
- **Surgical Delete**: Targeted removal of chunks based on page mappings before re-ingestion.
- **Hybrid Repair**: The process of using a Vision LLM to fix a structural defect in a Layout-parsed chunk.
- **Cortex Search**: Snowflake's native vector search service used for context retrieval.
- _Avoid_: "OCR" (The system uses AI-parsing/Vision, not traditional OCR).

---

## 5. Detailed Behavior

### Normal Execution (Ingestion)
1. **Job Definition**: User configures a job in `tab_config.py`.
2. **Surgical Mapping**: If enabled, users map source pages $\rightarrow$ target pages via `surgical_ui.py`.
3. **Initialization**: `batch_processor.py` ensures the target table exists and is `CHANGE_TRACKING` enabled.
4. **Extraction**:
    - **Layout**: Calls `AI_PARSE_DOCUMENT`. If pages are missing, generates `PLACEHOLDER` chunks.
    - **Vision**: Renders PDF to images $\rightarrow$ calls `claude-haiku-4-5`.
5. **Commit**: Data is written to Snowflake in batches of 100 pages.

### Edge Cases & Failure Modes
- **Model Failure**: If a Vision call fails, the page is logged as `VISION_EXTRACTION_SKIPPED` and omitted.
- **Auth Expiry**: If the Snowflake session terminates, the UI prompts a refresh via `tab_config.py`.
- **Surgical Collision**: `page_mapping.py` detects duplicate target page assignments before a job is queued.
- **Session Bloat**: `chunk_cache` is capped at 5,000 entries to prevent Streamlit memory crashes [`utils/core_utils.py`](utils/core_utils.py).

---

## 6. Public Interfaces

### User Interface (Streamlit)
| Tab | Input | Output | Side Effect |
| :--- | :--- | :--- | :--- |
| **Doc Refinery** | PDF Path, Strategy, Range | Job Queue, Progress Bar | Data written to Snowflake |
| **RAG Playground** | User Query, Model Selection | LLM Response, Retrieval Meta | `monitoring_logs` updated |
| **Cost Analytics** | Job Selection | Credit/USD/IDR breakdown | None |
| **Quality Analytics** | (None) | Defect distribution charts | None |

### Internal API
- `run_batch_execution(session, db, schema, stage_path)`: Entry point for processing the job queue.
- `set_query_tag(session, auth_context)`: Sets the session `QUERY_TAG` for warehouse attribution [`utils/snowflake_utils.py:45`](utils/snowflake_utils.py:45).

---

## 7. State, Persistence, and Data

### Persistence Layer
- **Snowflake Tables**: All chunks, job metrics, and mapping histories are persisted as table rows.
- **Snowflake Stages**: Source PDFs are stored as files in internal/external stages.

### Session State (Transient)
- `auth_context`: Active DB, Schema, and User identity.
- `job_queue`: List of pending and completed jobs for the current session.
- `chunk_cache`: In-memory subset of chunks for fast QA rendering.
- `query_tag_set`: Boolean flag ensuring `set_query_tag` is called once per session.

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
- **Invariant Check**: `UNCHUNKED_PAGES` logs are used to verify the 1-chunk-per-page rule.

### Coverage Gaps
- No automated unit tests for parsing logic; validation is purely manual/metric-based.
- No regression suite for "Surgical Mode" mapping.

---

## 11. Known Limitations & Non-Goals

- **Vision Latency**: Vision extraction is orders of magnitude slower than Layout parsing due to image rendering.
- **Cache Limits**: 5,000-chunk limit in `chunk_cache` prevents full review of massive documents in one session.
- **Cortex Limits**: Subject to Snowflake's account-level Cortex concurrency limits.
- **PDF Complexity**: Highly irregular tables may still require manual `Hybrid Repair`.

---

## 12. Change Sensitivity

| Component | Sensitivity | Risk of Modification |
| :--- | :--- | :--- |
| `ingestion_strategies/` | **High** | Changes to chunking or placeholders break the 1-chunk-per-page invariant. |
| `auth_utils.py` | **Medium** | Errors in role mapping block all user access. |
| `core_utils.py` | **Medium** | Changes to `PRICING_REGISTRY` lead to incorrect financial reporting. |
| `views/` | **Low** | UI changes are generally isolated to specific tabs. |