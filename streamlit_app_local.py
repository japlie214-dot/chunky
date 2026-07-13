# streamlit_app_local.py
# Local development version of Chunky - uses SQLite instead of Snowflake
# Run: streamlit run streamlit_app_local.py

import streamlit as st
import traceback
import json
import os
import tempfile
import io

# Local imports
from logger_config import log_action
from utils.local_db_utils import (
    init_database, get_connection, get_database_stats,
    get_distinct_files, get_chunks, get_chunk_count, get_page_range_for_file,
    insert_chunks_batch, delete_chunks_by_range, update_chunk, search_chunks,
    create_job, update_job_status, get_jobs,
    log_monitoring_turn, get_monitoring_logs,
    log_cost, get_cost_summary,
    register_service, get_services,
    save_form_submission, get_form_submissions,
    save_surgical_mapping, get_surgical_mappings,
    get_local_db_path
)
from utils.display_safety import safe_markdown, safe_code, safe_json, safe_dataframe

# Safe Import: pdf2image (mirrors Snowflake environment)
try:
    from pdf2image import convert_from_bytes, pdfinfo_from_bytes
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    convert_from_bytes = None
    pdfinfo_from_bytes = None
    PDF2IMAGE_AVAILABLE = False

# Poppler path — auto-detect common locations if not on system PATH
# Windows default: C:\poppler\poppler-26.02.0\Library\bin
# Set CHUNKY_POPPLER_PATH env var to override
POPPLER_PATH = os.environ.get("CHUNKY_POPPLER_PATH", None)
if POPPLER_PATH is None and os.name == 'nt':
    # Auto-detect common Windows install paths
    _candidates = [
        r"C:\poppler\poppler-26.02.0\Library\bin",
        r"C:\poppler\Library\bin",
        r"C:\Program Files\poppler\Library\bin",
    ]
    for _p in _candidates:
        if os.path.isdir(_p):
            POPPLER_PATH = _p
            break

# Safe Import: pypdf for PDF text extraction
try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PdfReader = None
    PYPDF_AVAILABLE = False

# Safe Import: Pillow
try:
    from PIL import Image
except ImportError:
    Image = None

# Local PDF storage directory
LOCAL_PDF_DIR = os.environ.get("CHUNKY_LOCAL_PDFS", "local_pdfs")

# -----------------------------------------------------------------------------
# 1. APP CONFIGURATION
# -----------------------------------------------------------------------------
st.set_page_config(layout="wide", page_title="Chunky (Local)")

# -----------------------------------------------------------------------------
# 2. DATABASE INITIALIZATION
# -----------------------------------------------------------------------------
if "local_db_initialized" not in st.session_state:
    init_database()
    st.session_state.local_db_initialized = True

# -----------------------------------------------------------------------------
# 3. SESSION STATE INITIALIZATION
# -----------------------------------------------------------------------------
if "chunk_cache" not in st.session_state:
    st.session_state.chunk_cache = []

if "config" not in st.session_state:
    st.session_state.config = {
        "db": "LOCAL",
        "schema": "DEFAULT",
        "stage": "LOCAL_FILES",
        "user_id": "local_user",
        "target_table": "chunks"
    }

if "messages" not in st.session_state:
    st.session_state.messages = []

if len(st.session_state.messages) > 30:
    st.session_state.messages = st.session_state.messages[-30:]

if "services_cache" not in st.session_state:
    st.session_state.services_cache = []

if "active_config" not in st.session_state:
    st.session_state.active_config = {}

if "monitoring_logs" not in st.session_state:
    st.session_state.monitoring_logs = []

if "pending_batch" not in st.session_state:
    st.session_state.pending_batch = []

if "batch_in_progress" not in st.session_state:
    st.session_state.batch_in_progress = False

if "cancel_batch" not in st.session_state:
    st.session_state.cancel_batch = False

# Simulated auth context for local mode
if "auth_context" not in st.session_state:
    st.session_state.auth_context = {
        "db": "LOCAL",
        "schema": "DEFAULT",
        "stage": "LOCAL_FILES",
        "user": "local_user@dev.local",
        "role": "LOCAL_ADMIN"
    }

if "app_started" not in st.session_state:
    log_action("APP_STARTUP", "Local application initialized", "local_user")
    st.session_state.app_started = True


# -----------------------------------------------------------------------------
# 4. LOCAL HOME VIEW
# -----------------------------------------------------------------------------
def render_local_home():
    """Render the local mode home page."""
    st.title("🥥 Chunky (Local Mode)")
    st.markdown("### Local Development & Testing Environment")

    st.success("🔒 **Running in Local Mode** — Using SQLite instead of Snowflake")

    st.info(
        "Welcome to **Chunky Local**, a standalone version for development and testing. "
        "This version uses **SQLite** as the database backend and simulates Snowflake Cortex "
        "functionality for offline testing."
    )

    # Database Stats
    st.markdown("---")
    st.header("📊 Local Database Overview")

    conn = get_connection()
    stats = get_database_stats(conn)
    conn.close()

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Chunks", stats['total_chunks'])
    with col2:
        st.metric("Files", stats['total_files'])
    with col3:
        st.metric("Jobs Run", stats['total_jobs'])
    with col4:
        st.metric("Services", stats['total_services'])

    # Capability Status
    st.markdown("#### 🔧 Environment Capabilities")
    cap1, cap2, cap3 = st.columns(3)
    with cap1:
        if PYPDF_AVAILABLE:
            st.success("✅ pypdf — PDF text extraction")
        else:
            st.error("❌ pypdf — not installed")
    with cap2:
        if PDF2IMAGE_AVAILABLE:
            poppler_info = f" (path: `{POPPLER_PATH}`)" if POPPLER_PATH else " (on system PATH)"
            st.success(f"✅ pdf2image — PDF page rendering{poppler_info}")
        else:
            st.error("❌ pdf2image — not installed")
    with cap3:
        st.info(f"📁 PDF storage: `{LOCAL_PDF_DIR}/`")

    if stats['chunk_types']:
        st.markdown("**Chunk Type Distribution:**")
        for ctype, count in stats['chunk_types'].items():
            st.write(f"  - `{ctype}`: {count} chunks")

    # Documentation
    st.markdown("---")
    st.header("📖 Local Mode Documentation")

    with st.expander("🔧 How Local Mode Works", expanded=True):
        st.markdown("""
        **Architecture:**
        - Uses **SQLite** (`chunky_local.db`) as the database backend
        - Simulates Snowflake Cortex Search with text-based search
        - All data persists locally between runs
        
        **Key Differences from Snowflake Mode:**
        | Feature | Snowflake | Local |
        |---------|-----------|-------|
        | Database | Snowflake SQL | SQLite |
        | AI Parsing | Cortex AI_PARSE_DOCUMENT | Simulated (text extraction) |
        | Vision | Cortex AI_COMPLETE + Image | Simulated |
        | Search | Cortex Search Services | SQLite LIKE search |
        | Auth | Gatekeeper + RBAC | Local admin |
        
        **Database Location:**
        Default: `./chunky_local.db`  
        Override: Set `CHUNKY_LOCAL_DB` environment variable
        """)

    with st.expander("🚀 Getting Started"):
        st.markdown("""
        1. **Add PDFs**: Place PDF files in the `local_files/` directory
        2. **Configure Jobs**: Use the Doc Refinery tab to set up ingestion jobs
        3. **Run Ingestion**: Process documents into chunks
        4. **Search**: Use RAG Playground to test retrieval
        5. **Inspect**: Use QA Studio to review chunk quality
        """)

    with st.expander("⚠️ Limitations"):
        st.markdown("""
        - **No real AI**: Vision/Layout parsing is simulated
        - **No real embeddings**: Search uses text matching, not vector similarity
        - **No Cortex services**: Services are mocked for UI testing
        - **Single user**: No multi-tenancy or RBAC
        - **No PDF rendering**: PDF preview is not available in local mode
        """)

    # Quick actions
    st.markdown("---")
    st.header("⚡ Quick Actions")

    qa1, qa2, qa3 = st.columns(3)
    with qa1:
        if st.button("🧹 Reset Database", type="secondary"):
            from utils.local_db_utils import reset_database
            conn = get_connection()
            reset_database(conn)
            conn.close()
            st.session_state.chunk_cache = []
            st.success("Database reset!")
            st.rerun()

    with qa2:
        if st.button("📋 View Database Path"):
            st.code(get_local_db_path())

    with qa3:
        if st.button("🔄 Refresh Stats"):
            st.rerun()


# -----------------------------------------------------------------------------
# 5. LOCAL DOC REFINERY
# -----------------------------------------------------------------------------
def render_local_refinery():
    """Render the local Doc Refinery (simplified for testing)."""
    st.title("🏭 Doc Refinery (Local)")

    t1, t2, t3 = st.tabs(["Ingestion", "QA Studio", "Jobs"])

    with t1:
        _render_local_ingestion()
    with t2:
        _render_local_qa()
    with t3:
        _render_local_jobs()


def _render_local_ingestion():
    """Render the local ingestion tab."""
    st.subheader("📄 Document Ingestion (Local)")

    pdf_types = ['pdf'] if PYPDF_AVAILABLE else []
    supported_types = ['txt', 'md', 'csv'] + pdf_types
    st.info(
        "In local mode, paste text directly or upload a file for chunking."
        + (" PDF text extraction via pypdf is available." if PYPDF_AVAILABLE else "")
        + (" PDF image rendering via pdf2image is available." if PDF2IMAGE_AVAILABLE else "")
    )

    # File upload or text input
    input_method = st.radio("Input Method", ["Text Input", "File Upload"], horizontal=True)

    text_content = ""
    file_name = "manual_input.txt"
    pdf_bytes = None
    pdf_page_count = 0

    if input_method == "Text Input":
        text_content = st.text_area(
            "Paste document text here",
            height=300,
            placeholder="Paste your document content..."
        )
    else:
        uploaded = st.file_uploader("Upload file", type=supported_types)
        if uploaded:
            file_name = uploaded.name
            raw_bytes = uploaded.read()

            if file_name.lower().endswith('.pdf') and PYPDF_AVAILABLE:
                # Extract text from PDF
                pdf_bytes = raw_bytes
                try:
                    reader = PdfReader(io.BytesIO(pdf_bytes))
                    pdf_page_count = len(reader.pages)
                    text_parts = []
                    for i, page in enumerate(reader.pages):
                        page_text = page.extract_text() or ""
                        text_parts.append(page_text)
                    text_content = "\n\n".join(text_parts)
                    st.success(f"📄 Extracted text from {pdf_page_count} PDF pages ({len(text_content):,} chars)")

                    # Save PDF locally for QA rendering
                    os.makedirs(LOCAL_PDF_DIR, exist_ok=True)
                    pdf_path = os.path.join(LOCAL_PDF_DIR, file_name)
                    with open(pdf_path, 'wb') as f:
                        f.write(pdf_bytes)
                    st.caption(f"PDF saved to `{pdf_path}` for QA rendering")
                except Exception as e:
                    st.error(f"PDF extraction failed: {e}")
            else:
                text_content = raw_bytes.decode('utf-8', errors='replace')

    # Chunking parameters
    col1, col2, col3 = st.columns(3)
    with col1:
        chunk_size = st.number_input("Chunk Size (chars)", 500, 50000, 8000, step=500)
    with col2:
        overlap_pct = st.slider("Overlap %", 0, 50, 20)
    with col3:
        target_table = st.text_input("Target Table", value="chunks", key="local_target_table")

    chunk_overlap = int(chunk_size * (overlap_pct / 100))

    if st.button("➕ Ingest Document", type="primary", disabled=not text_content.strip()):
        # Simple chunking logic (mirrors Snowflake's SPLIT_TEXT_RECURSIVE_CHARACTER)
        chunks = _chunk_text(text_content, chunk_size, chunk_overlap)

        conn = get_connection()
        job_id = create_job(
            conn, file_name, target_table, "APPEND", "Full Doc",
            1, len(chunks), len(chunks)
        )

        chunks_data = []
        for i, chunk_text in enumerate(chunks):
            chunks_data.append({
                'relative_path': file_name,
                'page_number': i + 1,
                'chunk': chunk_text,
                'chunk_type': 'STANDARD',
                'chunk_ref': f"{file_name} | Page Num: {i + 1}",
                'link_block': '',
                'chunk_metadata': {
                    'write_mode': 'APPEND',
                    'chunk_type': 'standard',
                    'parser': 'local_text',
                    'chunk_index': i
                }
            })

        chunk_ids = insert_chunks_batch(conn, chunks_data)
        update_job_status(conn, job_id, 'Completed',
                         actual_pages=len(chunks),
                         layout_pages=len(chunks))

        st.success(f"✅ Ingested {len(chunks)} chunks from `{file_name}`")
        st.json({"job_id": job_id, "chunks_created": len(chunk_ids)})
        conn.close()


def _chunk_text(text, chunk_size, overlap):
    """
    Simple recursive character text splitter.
    Mirrors Snowflake's SPLIT_TEXT_RECURSIVE_CHARACTER behavior.
    """
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]

        # Try to break at paragraph boundary
        if end < len(text):
            last_para = chunk.rfind('\n\n')
            if last_para > chunk_size * 0.5:
                chunk = chunk[:last_para]
                end = start + last_para

        chunks.append(chunk.strip())
        start = end - chunk_overlap

    return [c for c in chunks if c]


def _render_local_qa():
    """Render the local QA Studio with PDF rendering support."""
    st.subheader("🔍 QA Studio (Local)")

    conn = get_connection()
    files = get_distinct_files(conn)

    if not files:
        st.info("No documents ingested yet. Go to the Ingestion tab to add some.")
        conn.close()
        return

    selected_file = st.selectbox("Select File", files, key="qa_file")

    if selected_file:
        page_min, page_max = get_page_range_for_file(conn, selected_file)
        page_filter = st.slider(
            "Page Range",
            min_value=page_min,
            max_value=page_max,
            value=(page_min, page_max),
            key="qa_page_range"
        )

        # Check if PDF exists for visual rendering
        pdf_path = os.path.join(LOCAL_PDF_DIR, selected_file)
        has_pdf = os.path.exists(pdf_path) and PDF2IMAGE_AVAILABLE

        if has_pdf:
            st.caption(f"📄 PDF visual rendering available (`{pdf_path}`)")
        elif selected_file.lower().endswith('.pdf'):
            st.caption("⚠️ PDF file not found locally. Upload it via the Ingestion tab for visual rendering.")

        chunks = get_chunks(conn, relative_path=selected_file, limit=500)
        st.write(f"**{len(chunks)} chunks** found for `{selected_file}`")

        for chunk in chunks:
            pg = chunk['page_number']
            if pg < page_filter[0] or pg > page_filter[1]:
                continue

            with st.expander(f"📄 Page {pg} — {chunk['chunk_type']} ({chunk['chunk_id'][:16]}...)"):
                # Split: visual on left, text on right (mirrors Snowflake QA)
                col_vis, col_edit = st.columns(2)

                with col_vis:
                    st.caption(f"📄 {selected_file} (Pg {pg})")
                    if has_pdf:
                        try:
                            with open(pdf_path, 'rb') as f:
                                pdf_bytes = f.read()
                            images = convert_from_bytes(pdf_bytes, first_page=pg, last_page=pg, dpi=150, poppler_path=POPPLER_PATH)
                            if images:
                                st.image(images[0], use_container_width=True)
                            else:
                                st.warning("Could not render PDF page.")
                        except Exception as e:
                            st.error(f"PDF render error: {e}")
                    else:
                        st.info("Upload the PDF to the Ingestion tab to enable visual rendering.")

                with col_edit:
                    st.markdown(f"**Chunk ID:** `{chunk['chunk_id']}`")
                    st.markdown(f"**Type:** `{chunk['chunk_type']}`")
                    st.markdown(f"**Ref:** {chunk.get('chunk_ref', '—')}`")

                    # Editable chunk text
                    new_text = st.text_area(
                        "Chunk Content",
                        value=chunk['chunk'],
                        height=200,
                        key=f"edit_{chunk['chunk_id']}"
                    )

                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("💾 Save Changes", key=f"save_{chunk['chunk_id']}"):
                            update_chunk(conn, chunk['chunk_id'], new_text)
                            st.success("Chunk updated!")
                            st.rerun()
                    with c2:
                        st.caption(f"Last updated: {chunk.get('updated_at', '—')}`")

    conn.close()


def _render_local_jobs():
    """Render the local jobs history."""
    st.subheader("📊 Job History")

    conn = get_connection()
    jobs = get_jobs(conn, limit=20)
    conn.close()

    if not jobs:
        st.info("No jobs run yet.")
        return

    for job in jobs:
        status_color = {
            'Completed': '🟢',
            'Running': '🟡',
            'Failed': '🔴',
            'Pending': '⚪',
            'Cancelled': '🟠'
        }.get(job['status'], '⚪')

        with st.expander(f"{status_color} Job {job['job_id']} — {job['file_name']} ({job['status']})"):
            col1, col2, col3 = st.columns(3)
            with col1:
                st.write(f"**Mode:** {job['mode']}")
                st.write(f"**Scope:** {job['scope']}")
            with col2:
                st.write(f"**Pages:** {job.get('actual_pages', 0)} / {job.get('estimated_pages', '?')}")
                st.write(f"**Layout:** {job.get('layout_pages', 0)}")
            with col3:
                st.write(f"**Enhanced:** {job.get('enhanced_count', 0)}")
                st.write(f"**Started:** {job.get('started_at', '—')}")

            if job.get('error_message'):
                st.error(job['error_message'])


# -----------------------------------------------------------------------------
# 6. LOCAL RAG PLAYGROUND
# -----------------------------------------------------------------------------
def render_local_rag():
    """Render the local RAG Playground."""
    st.title("🧠 RAG Playground (Local)")

    st.info("Local mode uses simple text search instead of Cortex Search Services.")

    # Chat interface
    for msg in st.session_state.messages[-30:]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("Ask a question about your documents..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Searching local database..."):
                conn = get_connection()
                results = search_chunks(conn, prompt, limit=5)

                if results:
                    context = "\n\n---\n\n".join([
                        f"**[{r['relative_path']} — Page {r['page_number']}]**\n{r['chunk']}"
                        for r in results
                    ])

                    # Simulated LLM response (in local mode, we just show the context)
                    response = (
                        f"Based on searching your local database for **\"{prompt}\"**, "
                        f"I found **{len(results)}** relevant chunks:\n\n"
                        f"{context}\n\n"
                        f"---\n"
                        f"*Note: In local mode, no actual LLM is called. "
                        f"In Snowflake mode, this context would be sent to Claude/GPT "
                        f"via Cortex AI_COMPLETE.*"
                    )

                    # Log for monitoring
                    log_monitoring_turn(
                        conn, prompt, response, context,
                        "local_simulated", 0, 0
                    )
                else:
                    response = (
                        f"No relevant chunks found for **\"{prompt}\"**. "
                        f"Try ingesting some documents first via the Doc Refinery tab."
                    )

                conn.close()

            safe_markdown(response, label="local_rag_response")
            st.session_state.messages.append({"role": "assistant", "content": response})

    # Retrieval Inspector
    if st.session_state.messages:
        st.markdown("---")
        st.markdown("#### 🔍 Retrieval Inspector")
        with st.expander("Last Query Context"):
            conn = get_connection()
            logs = get_monitoring_logs(conn, limit=1)
            conn.close()
            if logs:
                st.json(logs[0])


# -----------------------------------------------------------------------------
# 7. LOCAL COST ANALYTICS
# -----------------------------------------------------------------------------
def render_local_cost():
    """Render the local cost analytics."""
    st.title("📊 Cost Analytics (Local)")
    st.info("Cost tracking is simulated in local mode.")

    conn = get_connection()
    summary = get_cost_summary(conn)
    conn.close()

    if summary:
        st.markdown("#### Model Usage Summary")
        for row in summary:
            st.write(f"**{row['model']}**: {row['call_count']} calls, "
                     f"{row['total_input_tokens']} in / {row['total_output_tokens']} out tokens")
    else:
        st.info("No cost data yet. Ingest some documents and use the RAG Playground.")


# -----------------------------------------------------------------------------
# 8. LOCAL WEBAPP DEMO
# -----------------------------------------------------------------------------
def render_local_webapp_demo():
    """Render the webapp demo page for local mode."""
    from views.webapp_demo import render_webapp_demo
    render_webapp_demo()


# -----------------------------------------------------------------------------
# 9. LOCAL SYSTEM LOGS
# -----------------------------------------------------------------------------
def render_local_logs():
    """Render local system logs."""
    st.title("📋 System Logs (Local)")

    if 'system_logs' in st.session_state and st.session_state.system_logs:
        logs = list(reversed(st.session_state.system_logs[-100:]))
        for log in logs:
            level_icon = {
                'INFO': 'ℹ️',
                'WARNING': '⚠️',
                'ERROR': '❌'
            }.get(log.get('level', 'INFO'), 'ℹ️')
            safe_markdown(f"{level_icon} `[{log.get('timestamp', '')}]` {log.get('message', '')}", label="local_log_entry")
    else:
        st.info("No logs yet.")

    # Show raw log file if exists
    if os.path.exists("app_activity.log"):
        with st.expander("📄 Raw Log File"):
            with open("app_activity.log", "r") as f:
                st.code(f.read()[-5000:])  # Last 5000 chars


# -----------------------------------------------------------------------------
# 10. MAIN CONTROLLER
# -----------------------------------------------------------------------------
def main():
    # Sidebar
    with st.sidebar:
        st.title("🥥 Chunky")
        st.caption("Local Development Mode")

        page_selection = st.radio("Go to:", [
            "Home",
            "Doc Refinery",
            "RAG Playground",
            "Webapp Demo",
            "Cost Analytics",
            "System Logs"
        ])

        st.markdown("---")

        ctx = st.session_state.auth_context
        st.caption("🔒 Active Context:")
        st.code(f"Mode: LOCAL\nDB: {get_local_db_path()}\nUser: {ctx['user']}")

        st.markdown("---")

        if st.button("🔄 Refresh Session"):
            st.rerun()

        if st.button("🧹 Clear In-Memory Chunks"):
            st.session_state.chunk_cache = []
            st.rerun()

        if st.button("🗑️ Clear Chat History"):
            st.session_state.messages = []
            st.rerun()

    # Routing
    try:
        if page_selection == "Home":
            render_local_home()
        elif page_selection == "Doc Refinery":
            render_local_refinery()
        elif page_selection == "RAG Playground":
            render_local_rag()
        elif page_selection == "Webapp Demo":
            render_local_webapp_demo()
        elif page_selection == "Cost Analytics":
            render_local_cost()
        elif page_selection == "System Logs":
            render_local_logs()
    except Exception as e:
        log_action("APP_CRASH", traceback.format_exc())
        st.error(f"An unexpected error occurred: {e}")
        st.code(traceback.format_exc())


if __name__ == "__main__":
    main()
