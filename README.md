# Chunky

A Streamlit-based Retrieval-Augmented Generation (RAG) application that runs within Snowflake's Native App environment. The system provides document ingestion, quality assurance, semantic search deployment, and AI-powered chat capabilities using Snowflake Cortex services.

---

## 1. Project Overview

### What the System Does
The RAG Ecosystem is a document processing and conversational AI platform that:
1. **Ingests PDF documents** from Snowflake stages, converting them into searchable text chunks using OCR and AI-based extraction (Layout and Vision strategies).
2. **Ensures Page-Level Coverage**: Guarantees at least one chunk per page. In Layout mode, synthetic `PLACEHOLDER` chunks are used when extraction fails. In Vision mode, pages that fail all model attempts are skipped to avoid low-fidelity noise.
3. **Supports Surgical Ingestion**: Allows targeted replacement of specific files or page ranges within an existing target table. This includes a dynamic page-mapping interface to align source pages with target indices in a replacement PDF.
4. **Provides quality assurance tools** for reviewing, editing, and enhancing extracted document content via a "Hybrid Repair" strategy.
5. **Facilitates semantic search deployment** by providing structured instructions for creating Cortex Search services in Snowsight.
6. **Offers a chat interface** for conversational querying against deployed search services.
7. **Tracks costs and quality metrics** for AI operations (token usage, credit consumption, and page-level defect/repair logs).
8. **Monitors AI responses** for safety, bias, misinformation, and other quality dimensions using a predefined label registry.

### Problem Solved
The system addresses the challenge of converting unstructured PDF documents into high-fidelity, searchable data for RAG. It specifically solves:
- **OCR Gaps**: Uses a combination of structural layout parsing and vision-based extraction to handle complex PDFs.
- **Data Loss**: Enforces a strict 1-chunk-per-page minimum to ensure no page is silently omitted from the index.
- **Metadata Rigidity**: Through "Surgical Mode," users can map source pages to target indices in a replacement PDF, allowing for document restructuring and precise corrections.
- **Quality Decay**: Provides a "Hybrid Repair" mechanism to fix OCR defects using vision AI.
- **Cost Opacity**: Dynamically calculates estimated credit costs for layout and vision operations, mapping token usage to the specific AI model utilized per transaction.

### What It Explicitly Does NOT Do
- Does not support document formats other than PDF.
- Does not perform local OCR; relies entirely on Snowflake Cortex `AI_PARSE_DOCUMENT` and vision models.
- Does not support multi-tenant isolation beyond Snowflake's native role-based access.
- Does not persist chat history beyond the current session (messages capped at 30).
- Does not provide real-time streaming responses (uses synchronous Cortex calls).
- Does not include automated test coverage; validation is performed via UI metrics and manual audit.

---

## 2. High-Level Architecture

### Major Components
- **`streamlit_app.py`**: The central entry point and router. Manages session state and coordinates view transitions.
- **`utils/auth_utils.py`**: The "Gatekeeper." Validates user identity and role-based access to Snowflake stages.
- **`views/refinery/`**: The core ingestion engine.
    - `batch_processor.py`: Orchestrates the job queue, manages transactions, and aggregates metrics.
    - `ingestion_core.py`: Handles table initialization (including `CHANGE_TRACKING` enablement) and surgical deletion.
    - `ingestion_strategies/`: A specialized package containing the parsing logic.
        - `layout.py`: Implements structural parsing via Cortex `AI_PARSE_DOCUMENT`.
        - `vision.py`: Implements image-based parsing via Vision LLMs.
        - `hybrid.py`: Implements targeted OCR correction.
    - `tab_config.py`, `tab_ingestion.py`, `tab_qa.py`: UI layers for job definition, execution, and manual review.
- **`views/chat.py`**: The RAG interface for querying deployed Cortex Search services.
- **`utils/constants.py`**: Centralized configuration for production database contexts, pricing, monitoring labels, and advisory thresholds (e.g., `PAGE_WARNING_THRESHOLD`).
- **`utils/snowflake_utils.py`**: Low-level wrappers for Snowpark session management and Cortex AI calls.

### Data Flow
1. **Authentication**: User $\rightarrow$ `auth_utils` $\rightarrow$ Snowflake Session $\rightarrow$ `auth_context` (Session State).
2. **Ingestion Pipeline**:
    - PDF $\rightarrow$ `AI_PARSE_DOCUMENT` (Layout) OR PDF $\rightarrow$ `pdf2image` $\rightarrow$ Vision Model.
    - Result $\rightarrow$ Missing Page Detection $\rightarrow$ Placeholder Generation (if needed).
    - Metadata Aliasing: If in SURGICAL mode, `RELATIVE_PATH` and `PAGE_NUMBER` are overridden by target values.
    - Chunks $\rightarrow$ Snowflake Table (with `CHUNK_TYPE` as 'STANDARD', 'ENHANCED', or 'PLACEHOLDER').
3. **Hybrid Repair**:
    - Defective Chunk $\rightarrow$ Vision AI $\rightarrow$ Updated Chunk $\rightarrow$ `CHUNK_TYPE` = 'ENHANCED'.
4. **RAG Query**:
    - User Query $\rightarrow$ Cortex Search Service $\rightarrow$ Context Chunks $\rightarrow$ Cortex AI $\rightarrow$ Final Response.

### Execution Model
- **Runtime**: Synchronous Streamlit application.
- **Batch Processing**: Ingestion is handled as a queue of "Jobs." The `batch_processor` iterates through this queue, executing strategies sequentially and committing results to Snowflake tables.

---

## 3. Repository Structure

- `streamlit_app.py`: Main application loop and routing.
- `utils/`:
    - `auth_utils.py`: Identity mapping and stage access verification.
    - `constants.py`: Global defaults (DB/Schema), pricing, monitoring labels, and advisory thresholds.
    - `core_utils.py`: PDF utilities (`PDFUtils`), analytics (`RAGAnalytics`), and quality inspection.
    - `snowflake_utils.py`: Snowpark session and Cortex API wrappers.
    - `page_mapping.py`: Logic for calculating source-to-target page mappings and duplicate detection for surgical mode.
    - `metadata_handler.py`: Standardizes JSON structures for `CHUNK_METADATA` records.
    - `table_migrator.py`: Logic for pre-flight schema checks and conditional `ALTER TABLE` commands to normalize legacy tables.
- `views/`:
    - `home.py`, `chat.py`, `admin.py`, `logs.py`: Top-level application views.
    - `refinery/`:
        - `batch_processor.py`: The orchestrator for the ingestion pipeline.
        - `ingestion_strategies/`: Package containing specialized extraction modules (`layout.py`, `vision.py`, `hybrid.py`).
        - `ingestion_core.py`: Shared table initialization and cleanup logic.
        - `common.py`: Shared SQL utilities for the refinery.
        - `tab_config.py`: UI for defining ingestion jobs, including the surgical mapping interface.
        - `tab_ingestion.py`: UI for running batches and viewing metrics.
        - `tab_qa.py`: UI for human-in-the-loop chunk editing.
        - `tab_deployment.py`: Instructions for deploying Cortex Search.
        - `surgical_ui.py`: Fragment-based UI for paginated page-mapping configuration.
        - `deprecated/`: Contains retired programmatic deployment logic (`deployment_ui.py`, `deployment_logic.py`).
- `logger_config.py`: Centralized action logging for audit trails.
- `prompts.py`: Registry of all AI prompts used across the system.
- `environment.yml`: Conda environment specification for Snowflake Native App.
- `requirements.txt`: Python dependency list.

---

## 4. Core Concepts & Domain Model

### Data Model
The primary data artifact is the **Chunk Table**, typically containing:
- `RELATIVE_PATH`: The PDF path in the Snowflake stage (overridden in Surgical mode).
- `PAGE_NUMBER`: 1-based index of the page (overridden in Surgical mode).
- `CHUNK`: The extracted text content.
- `CHUNK_ID`: Unique identifier (e.g., `CHK_UUID`).
- `CHUNK_TYPE`: 
    - `STANDARD`: Extracted via Layout parser.
    - `ENHANCED`: Extracted or repaired via Vision AI.
    - `PLACEHOLDER`: Synthetic chunk created to ensure page coverage.
- `CHUNK_REF`: A unique reference string for tracing.
- `LINK_BLOCK`: Extracted hyperlinks associated with the chunk.
- `CHUNK_METADATA`: A `VARIANT` column storing JSON metadata (e.g., surgical mapping history, parser configs, timestamps).

### Invariants
- **Page Coverage**: Every page in the requested range must result in at least one row in the target table (Layout strategy) or be explicitly logged as skipped due to total model failure (Vision strategy).
- **Identifier Escaping**: All Snowflake identifiers (DB, Schema, Table) are double-quoted to handle special characters and case sensitivity.
- **Surgical Mapping Validity**: Surgical jobs must have a valid, non-duplicate page mapping before they can be added to the queue.
- **Session Memory**: The `chunk_cache` is capped at 5,000 entries to prevent Streamlit session crashes.
- **Owner Rights**: The app runs as `IT_AI`. To avoid redundant grant errors, `IT_AI` is explicitly excluded from target grant lists.

---

## 5. Detailed Behavior

### Normal Execution (Ingestion)
1. User defines a job (File, Range, Strategy) in the Config Tab.
2. If `SURGICAL` mode is selected, the user maps source pages to a replacement PDF's pages via the `surgical_ui` fragment.
3. `batch_processor` initializes a target table (performing pre-flight migration via `LegacyTableMigrator` if necessary) and performs a "Surgical Delete" if requested.
4. The chosen strategy (`_execute_layout_strategy` or `_execute_vision_strategy`) processes the PDF.
5. **Placeholder Logic**: If the AI parser returns fewer pages than the document range, the system generates `PLACEHOLDER` chunks for the missing indices.
6. **Metadata Aliasing**: In `SURGICAL` mode, `RELATIVE_PATH` and `PAGE_NUMBER` are overridden by target values, and the mapping history is stored in `CHUNK_METADATA`.
7. Data is written to Snowflake in batches of 100 pages using temporary tables and parameterized SQL bindings.
8. Metrics (tokens, credits, page counts) are updated in the job state.

### Failure Modes & Error Handling
- **Cortex API Failures**: Handled via try-except blocks; failed batches are added to `skipped_page_ranges` and logged.
- **Render Failures**: If `pdf2image` fails to render a page or all Vision LLM attempts fail, the page is skipped and logged as a `VISION_EXTRACTION_SKIPPED` event.
- **Session Instability**: `tab_config.py` detects "XP" or "terminated" errors from Snowflake and prompts the user to refresh.
- **Invalid Role Syntax**: Grant failures due to invalid role names are captured in `grant_status` and logged as warnings.
- **Queue Overload**: When pending pages exceed `PAGE_WARNING_THRESHOLD`, the system displays an advisory warning to the user.

---

## 6. Public Interfaces

### Streamlit UI Entry Points
- **Config Tab**: Job definition and queue management.
- **Ingestion Tab**: Batch execution and performance dashboard.
- **QA Tab**: Manual review and editing of chunks.
- **Chat Tab**: RAG interface.

### Internal API
- `run_batch_execution(session, db, schema, stage_path)`: Main entry point for the ingestion orchestrator.
- `_execute_layout_strategy(...)`: Performs structural parsing.
- `_execute_vision_strategy(...)`: Performs image-based parsing.
- `_execute_hybrid_repair_strategy(...)`: Performs targeted OCR correction.
- `_execute_surgical_delete_with_mappings(...)`: Handles targeted cleanup based on page mapping arrays.

---

## 7. State, Persistence, and Data

- **Persistence**: All processed chunks and job histories are stored in Snowflake tables.
- **Session State**:
    - `auth_context`: User identity and active DB/Schema.
    - `job_queue`: List of pending and completed ingestion jobs.
    - `chunk_cache`: A subset of ingested chunks held in memory for fast UI rendering.
    - `surgical_mapping_result`: Temporary storage for the active page-mapping configuration.
- **Lifecycle**: `chunk_cache` is transient and cleared on session restart or via the "Clear In-Memory Chunks" button.

---

## 8. Dependencies & Integration

- **Snowflake Cortex**: Used for `AI_PARSE_DOCUMENT`, `AI_COMPLETE` (Vision/Text), and `Cortex Search`.
- **Snowpark**: Used for all database interactions and stage file management.
- **pdf2image / Poppler**: Required for converting PDF pages to images for the Vision strategy.
- **Pillow**: Image optimization and handling.
- **Pandas**: Data manipulation for batch uploads and metric aggregation.

---

## 9. Setup, Build, and Execution

### Environment
The application is designed to run as a **Snowflake Native App**.
1. Create a Snowflake environment using `environment.yml`.
2. Ensure the `poppler` system library is installed in the environment for `pdf2image` to function.
3. Deploy the application via the Snowflake UI or SnowCLI.

### Execution
- Launch the Streamlit app within Snowflake.
- Authenticate via the Gatekeeper (requires a valid email mapped in `auth_utils.py`).
- Connect to a Snowflake stage containing PDF files.

---

## 10. Testing & Validation

- **Manual Validation**: Performed via the **QA Tab**, where users compare AI extracts against original document context.
- **Metric Audit**: The Ingestion Tab provides "Success Rate" and "Processed Pages" metrics to validate batch completion.
- **Coverage Check**: The `UNCHUNKED_PAGES` log action serves as a programmatic check for the 1-chunk-per-page invariant.

---

## 11. Known Limitations & Non-Goals

- **Scaling**: The 5,000-chunk session cache limit means very large documents cannot be fully reviewed in the QA tab without frequent cache clears.
- **Performance**: Vision extraction is significantly slower than layout parsing due to image rendering and multi-modal AI calls.
- **PDF Complexity**: Extremely complex layouts may still require manual repair via the Hybrid strategy.

---


- **High Sensitivity**: `views/refinery/ingestion_strategies/`. Any change to the chunking logic or placeholder generation affects the core data invariant.
- **Medium Sensitivity**: `auth_utils.py`. Changes to the role map or app ID query will block user access.
- **Low Sensitivity**: `views/` (UI components). Most UI changes are isolated to specific tabs.