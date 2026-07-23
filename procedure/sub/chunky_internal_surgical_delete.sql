-- ============================================================================
-- chunky_internal_surgical_delete
-- Deletes pages by range mappings with transaction safety (BEGIN/COMMIT/ROLLBACK).
-- Sorts mappings bottom-up (highest source_end first) to avoid invalidation.
-- Shared by: chunky_chunks
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_internal_surgical_delete(
    db VARCHAR,
    schema VARCHAR,
    table_name VARCHAR,
    file VARCHAR,
    range_mappings VARIANT
)
RETURNS VARIANT
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    full_table VARCHAR := '"' || REPLACE(db, '"', '""') || '"."' || REPLACE(schema, '"', '""') || '"."' || REPLACE(table_name, '"', '""') || '"';
    safe_file VARCHAR := REPLACE(file, '''', '''''');
    result VARIANT;
    i INTEGER;
    rm VARIANT;
    src_start INTEGER;
    src_end INTEGER;
    delete_sql VARCHAR;
    error_msg VARCHAR := '';
    success BOOLEAN := TRUE;
BEGIN
    -- Sort range mappings by source_end DESC (bottom-up)
    LET sorted_mappings RESULTSET := (
        SELECT value
        FROM LATERAL FLATTEN(input => :range_mappings)
        ORDER BY value:source_end::INTEGER DESC
    );

    BEGIN TRANSACTION;

    LET c CURSOR FOR sorted_mappings;
    OPEN c;
    LOOP
        FETCH c INTO rm;
        IF (SQLCODE != 0) THEN LEAVE; END IF;

        src_start := rm:source_start::INTEGER;
        src_end := rm:source_end::INTEGER;

        delete_sql := 'DELETE FROM ' || :full_table
            || ' WHERE RELATIVE_PATH = ''' || :safe_file || ''''
            || ' AND PAGE_NUMBER BETWEEN ' || :src_start || ' AND ' || :src_end;

        BEGIN
            EXECUTE IMMEDIATE :delete_sql;
        EXCEPTION
            WHEN OTHER THEN
                error_msg := SQLERRM;
                success := FALSE;
                ROLLBACK;
                CLOSE c;
                RETURN OBJECT_CONSTRUCT('success', FALSE, 'error', :error_msg);
        END;
    END LOOP;
    CLOSE c;

    COMMIT;

    RETURN OBJECT_CONSTRUCT('success', TRUE, 'error', NULL);
END
$$;
