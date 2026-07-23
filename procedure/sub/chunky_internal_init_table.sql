-- ============================================================================
-- chunky_internal_init_table
-- Creates the target table with standard Chunky schema if it doesn't exist.
-- For OVERWRITE mode: CREATE OR REPLACE (drops existing data).
-- For APPEND/SURGICAL: CREATE IF NOT EXISTS, then migrate if schema mismatch.
-- Shared by: chunky_chunks, chunky_qa
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_internal_init_table(
    db VARCHAR,
    schema VARCHAR,
    table_name VARCHAR,
    mode VARCHAR
)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    full_table VARCHAR := '"' || REPLACE(db, '"', '""') || '"."' || REPLACE(schema, '"', '""') || '"."' || REPLACE(table_name, '"', '""') || '"';
    cmd VARCHAR;
    grants VARCHAR;
    init_sql VARCHAR;
    tbl_exists BOOLEAN;
BEGIN
    -- Check if table exists
    BEGIN
        LET res RESULTSET := (SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_CATALOG = :db AND TABLE_SCHEMA = :schema AND TABLE_NAME = :table_name);
        LET c CURSOR FOR res;
        OPEN c;
        FETCH c INTO tbl_exists;
        CLOSE c;
        tbl_exists := (tbl_exists > 0);
    EXCEPTION
        WHEN OTHER THEN
            tbl_exists := FALSE;
    END;

    IF (:mode = 'OVERWRITE' OR NOT :tbl_exists) THEN
        cmd := CASE WHEN :mode = 'OVERWRITE' THEN 'CREATE OR REPLACE' ELSE 'CREATE' END;
        grants := CASE WHEN :mode = 'OVERWRITE' THEN ' COPY GRANTS' ELSE '' END;

        init_sql := :cmd || ' TABLE ' || :full_table || ' ('
            || 'RELATIVE_PATH VARCHAR, '
            || 'PAGE_NUMBER NUMBER, '
            || 'CHUNK VARCHAR, '
            || 'CHUNK_ID VARCHAR, '
            || 'CHUNK_TYPE VARCHAR DEFAULT ''STANDARD'', '
            || 'CHUNK_REF VARCHAR, '
            || 'LINK_BLOCK VARCHAR, '
            || 'CHUNK_METADATA VARIANT'
            || ') CHANGE_TRACKING = TRUE' || :grants;

        EXECUTE IMMEDIATE :init_sql;
        RETURN 'CREATED';
    ELSE
        -- Table exists, mode is APPEND or SURGICAL — no-op
        RETURN 'EXISTS';
    END IF;
END
$$;
