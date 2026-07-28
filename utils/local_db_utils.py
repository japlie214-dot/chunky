# utils/local_db_utils.py
# SQLite-based database utilities for local development/testing
# Replaces Snowflake-specific functionality for Windows/Linux local runs

import os
import json
import sqlite3
import uuid
import time
import re
from datetime import datetime

# Default local database path
LOCAL_DB_PATH = os.environ.get("CHUNKY_LOCAL_DB", "chunky_local.db")


def get_local_db_path():
    """Get the path to the local SQLite database."""
    return LOCAL_DB_PATH


def get_connection(db_path=None):
    """
    Get a SQLite connection with WAL mode for better concurrent access.
    
    Args:
        db_path: Path to SQLite database file. Uses default if None.
    
    Returns:
        sqlite3.Connection with row_factory set to sqlite3.Row
    """
    path = db_path or get_local_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_database(db_path=None):
    """
    Initialize the local SQLite database with all required tables.
    Mirrors the Snowflake schema structure.
    
    Args:
        db_path: Path to SQLite database file. Uses default if None.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    # Main chunks table (mirrors SUS_CHUNKS)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            relative_path TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            chunk TEXT,
            chunk_id TEXT UNIQUE NOT NULL,
            chunk_type TEXT DEFAULT 'STANDARD',
            chunk_ref TEXT,
            link_block TEXT,
            chunk_metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Job metrics table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT UNIQUE NOT NULL,
            file_name TEXT NOT NULL,
            table_name TEXT,
            mode TEXT,
            scope TEXT,
            page_range_start INTEGER,
            page_range_end INTEGER,
            estimated_pages INTEGER,
            actual_pages INTEGER DEFAULT 0,
            layout_pages INTEGER DEFAULT 0,
            vision_pages INTEGER DEFAULT 0,
            enhanced_count INTEGER DEFAULT 0,
            placeholder_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Pending',
            error_message TEXT,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Monitoring logs table (for RAG Playground)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS monitoring_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_query TEXT,
            bot_response TEXT,
            rag_context TEXT,
            model TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            metadata TEXT
        )
    """)

    # Cost tracking table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cost_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT,
            job_id TEXT,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            total_credits REAL DEFAULT 0,
            total_usd REAL DEFAULT 0,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Cortex search services table (local mock)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cortex_services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_name TEXT UNIQUE NOT NULL,
            database_name TEXT,
            schema_name TEXT,
            target_table TEXT,
            embedding_model TEXT DEFAULT 'snowflake-arctic-embed-l-v2.0-8k',
            search_column TEXT DEFAULT 'CHUNK',
            status TEXT DEFAULT 'ACTIVE',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Form submissions table (for webapp demo)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS form_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_data TEXT NOT NULL,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Surgical page mappings table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS surgical_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_start INTEGER NOT NULL,
            source_end INTEGER NOT NULL,
            replacement_file TEXT,
            replacement_start INTEGER NOT NULL,
            replacement_end INTEGER NOT NULL,
            delta INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create indexes for performance
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(relative_path)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_number)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chunks_type ON chunks(chunk_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_metrics_status ON job_metrics(status)")

    conn.commit()
    conn.close()


# =============================================================================
# CHUNK OPERATIONS
# =============================================================================

def insert_chunk(conn, relative_path, page_number, chunk_text, chunk_type='STANDARD',
                 chunk_ref='', link_block='', chunk_metadata=None):
    """
    Insert a chunk into the local database.
    
    Returns:
        The generated chunk_id
    """
    chunk_id = f"CHK_{uuid.uuid4().hex[:16]}"
    metadata_json = json.dumps(chunk_metadata) if chunk_metadata else None

    conn.execute("""
        INSERT INTO chunks (relative_path, page_number, chunk, chunk_id, chunk_type,
                           chunk_ref, link_block, chunk_metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (relative_path, page_number, chunk_text, chunk_id, chunk_type,
          chunk_ref, link_block, metadata_json))
    conn.commit()
    return chunk_id


def insert_chunks_batch(conn, chunks_data):
    """
    Insert multiple chunks in a batch.
    
    Args:
        chunks_data: List of dicts with keys: relative_path, page_number, chunk,
                     chunk_type, chunk_ref, link_block, chunk_metadata
    
    Returns:
        List of generated chunk_ids
    """
    chunk_ids = []
    for c in chunks_data:
        chunk_id = f"CHK_{uuid.uuid4().hex[:16]}"
        metadata_json = json.dumps(c.get('chunk_metadata')) if c.get('chunk_metadata') else None
        conn.execute("""
            INSERT INTO chunks (relative_path, page_number, chunk, chunk_id, chunk_type,
                               chunk_ref, link_block, chunk_metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (c['relative_path'], c['page_number'], c['chunk'], chunk_id,
              c.get('chunk_type', 'STANDARD'), c.get('chunk_ref', ''),
              c.get('link_block', ''), metadata_json))
        chunk_ids.append(chunk_id)
    conn.commit()
    return chunk_ids


def get_chunks(conn, relative_path=None, page_number=None, limit=100, offset=0):
    """
    Retrieve chunks with optional filtering.
    
    Args:
        relative_path: Filter by file path
        page_number: Filter by page number
        limit: Max results
        offset: Pagination offset
    
    Returns:
        List of chunk dicts
    """
    query = "SELECT * FROM chunks WHERE 1=1"
    params = []

    if relative_path:
        query += " AND relative_path = ?"
        params.append(relative_path)
    if page_number is not None:
        query += " AND page_number = ?"
        params.append(page_number)

    query += " ORDER BY page_number, chunk_id LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_chunk_count(conn, relative_path=None):
    """Get total chunk count, optionally filtered by file."""
    query = "SELECT COUNT(*) FROM chunks"
    params = []
    if relative_path:
        query += " WHERE relative_path = ?"
        params.append(relative_path)
    row = conn.execute(query, params).fetchone()
    return row[0]


def get_distinct_files(conn):
    """Get list of distinct file paths in the chunks table."""
    rows = conn.execute(
        "SELECT DISTINCT relative_path FROM chunks ORDER BY relative_path"
    ).fetchall()
    return [r[0] for r in rows]


def get_page_range_for_file(conn, relative_path):
    """Get min/max page numbers for a file."""
    row = conn.execute("""
        SELECT MIN(page_number), MAX(page_number)
        FROM chunks WHERE relative_path = ?
    """, (relative_path,)).fetchone()
    if row and row[0] is not None:
        return int(row[0]), int(row[1])
    return 1, 1


def delete_chunks_by_range(conn, relative_path, page_start, page_end):
    """Delete chunks for a file within a page range (for surgical mode)."""
    conn.execute("""
        DELETE FROM chunks
        WHERE relative_path = ? AND page_number BETWEEN ? AND ?
    """, (relative_path, page_start, page_end))
    conn.commit()


def update_chunk(conn, chunk_id, new_text):
    """Update a chunk's text (for QA editing)."""
    conn.execute("""
        UPDATE chunks SET chunk = ?, updated_at = CURRENT_TIMESTAMP
        WHERE chunk_id = ?
    """, (new_text, chunk_id))
    conn.commit()


def search_chunks(conn, query_text, limit=10):
    """
    Simple text search across chunks (local replacement for Cortex Search).
    Uses SQLite FTS or simple LIKE search.
    
    Args:
        query_text: Search query
        limit: Max results
    
    Returns:
        List of matching chunk dicts with relevance score
    """
    # Simple LIKE-based search (FTS would require fts5 extension)
    rows = conn.execute("""
        SELECT *, 
            CASE 
                WHEN chunk LIKE ? THEN 1.0
                WHEN chunk LIKE ? THEN 0.8
                ELSE 0.5
            END as relevance_score
        FROM chunks
        WHERE chunk LIKE ? OR chunk LIKE ?
        ORDER BY relevance_score DESC, page_number
        LIMIT ?
    """, (
        f"%{query_text}%",
        f"%{query_text.split()[0]}%" if query_text.split() else f"%{query_text}%",
        f"%{query_text}%",
        f"%{query_text.split()[0]}%" if query_text.split() else f"%{query_text}%",
        limit
    )).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# JOB METRICS OPERATIONS
# =============================================================================

def create_job(conn, file_name, table_name, mode, scope, page_range_start,
               page_range_end, estimated_pages):
    """Create a new job record and return job_id."""
    job_id = f"JOB_{uuid.uuid4().hex[:12]}"
    conn.execute("""
        INSERT INTO job_metrics (job_id, file_name, table_name, mode, scope,
                                page_range_start, page_range_end, estimated_pages,
                                status, started_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Running', CURRENT_TIMESTAMP)
    """, (job_id, file_name, table_name, mode, scope,
          page_range_start, page_range_end, estimated_pages))
    conn.commit()
    return job_id


def update_job_status(conn, job_id, status, error_message=None, **kwargs):
    """Update job status and optional metrics."""
    set_clauses = ["status = ?"]
    params = [status]

    if error_message:
        set_clauses.append("error_message = ?")
        params.append(error_message)

    for key in ['actual_pages', 'layout_pages', 'vision_pages',
                'enhanced_count', 'placeholder_count']:
        if key in kwargs:
            set_clauses.append(f"{key} = ?")
            params.append(kwargs[key])

    if status in ('Completed', 'Failed', 'Cancelled'):
        set_clauses.append("completed_at = CURRENT_TIMESTAMP")

    params.append(job_id)
    conn.execute(f"""
        UPDATE job_metrics SET {', '.join(set_clauses)} WHERE job_id = ?
    """, params)
    conn.commit()


def get_jobs(conn, limit=50):
    """Get recent jobs."""
    rows = conn.execute("""
        SELECT * FROM job_metrics ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_job_by_id(conn, job_id):
    """Get a specific job by ID."""
    row = conn.execute(
        "SELECT * FROM job_metrics WHERE job_id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


# =============================================================================
# MONITORING / ANALYTICS OPERATIONS
# =============================================================================

def log_monitoring_turn(conn, user_query, bot_response, rag_context,
                        model, prompt_tokens=0, completion_tokens=0,
                        metadata=None, batch_id=None):
    """Log a RAG playground turn for monitoring."""
    if not batch_id:
        batch_id = uuid.uuid4().hex[:12]
    metadata_json = json.dumps(metadata) if metadata else None
    conn.execute("""
        INSERT INTO monitoring_logs (batch_id, user_query, bot_response, rag_context,
                                    model, prompt_tokens, completion_tokens, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (batch_id, user_query, bot_response, rag_context,
          model, prompt_tokens, completion_tokens, metadata_json))
    conn.commit()
    return batch_id


def get_monitoring_logs(conn, limit=100):
    """Get recent monitoring logs."""
    rows = conn.execute("""
        SELECT * FROM monitoring_logs ORDER BY timestamp DESC LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def log_cost(conn, job_id, model, input_tokens, output_tokens,
             total_credits, total_usd, batch_id=None):
    """Log cost tracking data."""
    conn.execute("""
        INSERT INTO cost_tracking (batch_id, job_id, model, input_tokens, output_tokens,
                                  total_credits, total_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (batch_id, job_id, model, input_tokens, output_tokens,
          total_credits, total_usd))
    conn.commit()


def get_cost_summary(conn, job_id=None):
    """Get cost summary, optionally filtered by job."""
    query = """
        SELECT model,
               SUM(input_tokens) as total_input_tokens,
               SUM(output_tokens) as total_output_tokens,
               SUM(total_credits) as total_credits,
               SUM(total_usd) as total_usd,
               COUNT(*) as call_count
        FROM cost_tracking
    """
    params = []
    if job_id:
        query += " WHERE job_id = ?"
        params.append(job_id)
    query += " GROUP BY model ORDER BY total_credits DESC"

    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# CORTEX SERVICES (LOCAL MOCK)
# =============================================================================

def register_service(conn, service_name, database_name, schema_name,
                     target_table, embedding_model='snowflake-arctic-embed-l-v2.0-8k'):
    """Register a mock Cortex Search service."""
    conn.execute("""
        INSERT OR REPLACE INTO cortex_services
        (service_name, database_name, schema_name, target_table, embedding_model)
        VALUES (?, ?, ?, ?, ?)
    """, (service_name, database_name, schema_name, target_table, embedding_model))
    conn.commit()


def get_services(conn, database_name=None, schema_name=None):
    """Get registered services."""
    query = "SELECT * FROM cortex_services WHERE status = 'ACTIVE'"
    params = []
    if database_name:
        query += " AND database_name = ?"
        params.append(database_name)
    if schema_name:
        query += " AND schema_name = ?"
        params.append(schema_name)

    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# FORM SUBMISSIONS (WEBAPP DEMO)
# =============================================================================

def save_form_submission(conn, form_data: dict):
    """Save form submission data."""
    conn.execute("""
        INSERT INTO form_submissions (form_data) VALUES (?)
    """, (json.dumps(form_data),))
    conn.commit()


def get_form_submissions(conn, limit=50):
    """Get recent form submissions."""
    rows = conn.execute("""
        SELECT * FROM form_submissions ORDER BY submitted_at DESC LIMIT ?
    """, (limit,)).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d['form_data'] = json.loads(d['form_data'])
        results.append(d)
    return results


# =============================================================================
# SURGICAL MAPPINGS
# =============================================================================

def save_surgical_mapping(conn, job_id, source_file, source_start, source_end,
                          replacement_file, replacement_start, replacement_end, delta=0):
    """Save a surgical page mapping."""
    conn.execute("""
        INSERT INTO surgical_mappings
        (job_id, source_file, source_start, source_end, replacement_file,
         replacement_start, replacement_end, delta)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (job_id, source_file, source_start, source_end,
          replacement_file, replacement_start, replacement_end, delta))
    conn.commit()


def get_surgical_mappings(conn, job_id):
    """Get surgical mappings for a job."""
    rows = conn.execute("""
        SELECT * FROM surgical_mappings WHERE job_id = ? ORDER BY source_start
    """, (job_id,)).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# DATABASE STATS
# =============================================================================

def get_database_stats(conn):
    """Get overview statistics of the local database."""
    stats = {}
    stats['total_chunks'] = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    stats['total_files'] = conn.execute("SELECT COUNT(DISTINCT relative_path) FROM chunks").fetchone()[0]
    stats['total_jobs'] = conn.execute("SELECT COUNT(*) FROM job_metrics").fetchone()[0]
    stats['total_monitoring'] = conn.execute("SELECT COUNT(*) FROM monitoring_logs").fetchone()[0]
    stats['total_services'] = conn.execute("SELECT COUNT(*) FROM cortex_services WHERE status='ACTIVE'").fetchone()[0]
    stats['total_forms'] = conn.execute("SELECT COUNT(*) FROM form_submissions").fetchone()[0]

    # Chunk type breakdown
    rows = conn.execute("""
        SELECT chunk_type, COUNT(*) as cnt FROM chunks GROUP BY chunk_type
    """).fetchall()
    stats['chunk_types'] = {r[0]: r[1] for r in rows}

    return stats


def reset_database(conn):
    """Drop all tables and reinitialize. USE WITH CAUTION."""
    tables = ['chunks', 'job_metrics', 'monitoring_logs', 'cost_tracking',
              'cortex_services', 'form_submissions', 'surgical_mappings']
    for table in tables:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    init_database()
