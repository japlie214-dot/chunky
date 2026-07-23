-- ============================================================================
-- 00_install_all.sql
-- Master installer for all Chunky procedures.
-- Run this file to create all procedures in order.
-- ============================================================================
-- Usage: snowsql -f procedure/00_install_all.sql
-- Or copy-paste sections into Snowsight.

-- Set context
USE DATABASE DEV_DB;
USE SCHEMA DNA;

-- ============================================================================
-- Sub-procedures (no dependencies)
-- ============================================================================

-- 1. Build CHUNK_REF string
!source sub/chunky_internal_build_chunk_ref.sql

-- 2. Init table (CREATE TABLE if not exists)
!source sub/chunky_internal_init_table.sql

-- 3. Surgical delete (transactional)
!source sub/chunky_internal_surgical_delete.sql

-- 4. Grant table (with retry)
!source sub/chunky_internal_grant_table.sql

-- 5. Parse PDF (AI_PARSE_DOCUMENT wrapper)
!source sub/chunky_internal_parse_pdf.sql

-- ============================================================================
-- Main procedures
-- ============================================================================

-- 6. chunky_chunks — Ingestion Engine
!source chunky_chunks.sql

-- 7. chunky_qa — Headless QA Studio
!source chunky_qa.sql

-- 8. chunky_searchservice — Cortex Search Service Manager
!source chunky_searchservice.sql

-- ============================================================================
-- Verify installation
-- ============================================================================
SHOW PROCEDURES IN SCHEMA DEV_DB.DNA;

SELECT procedure_name, argument_signature, procedure_language
FROM INFORMATION_SCHEMA.PROCEDURES
WHERE procedure_schema = 'DNA'
  AND procedure_name LIKE 'CHUNKY%'
ORDER BY procedure_name;
