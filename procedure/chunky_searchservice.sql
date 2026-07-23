-- ============================================================================
-- chunky_searchservice
-- Cortex Search Service Manager. Commands: create, list, describe, alter, drop
-- Source: CCS wizard Page 5 (views/ccs/page4_complete.py)
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_searchservice(
    command VARCHAR,
    instruction VARIANT
)
RETURNS VARIANT
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    cmd VARCHAR := UPPER(command);
    svc_name VARCHAR;
    db VARCHAR;
    schema VARCHAR;
    result VARIANT;
BEGIN
    -- ================================================================
    -- LIST: List all Cortex Search Services in a schema
    -- ================================================================
    IF (:cmd = 'LIST') THEN
        db := instruction:db::VARCHAR;
        schema := instruction:schema::VARCHAR;

        LET res RESULTSET := (
            SELECT *
            FROM TABLE(INFORMATION_SCHEMA.CORTEX_SEARCH_SERVICES(
                DATABASE_NAME => :db,
                SCHEMA_NAME => :schema
            ))
        );

        LET rows ARRAY := ARRAY_CONSTRUCT();
        LET c CURSOR FOR res;
        OPEN c;
        LET row_data VARIANT;
        LOOP
            FETCH c INTO row_data;
            IF (SQLCODE != 0) THEN LEAVE; END IF;
            rows := ARRAY_APPEND(:rows, :row_data);
        END LOOP;
        CLOSE c;

        RETURN OBJECT_CONSTRUCT('success', TRUE, 'command', 'list', 'data', :rows, 'error', NULL);
    END IF;

    -- ================================================================
    -- DESCRIBE: Describe a specific service
    -- ================================================================
    IF (:cmd = 'DESCRIBE') THEN
        db := instruction:db::VARCHAR;
        schema := instruction:schema::VARCHAR;
        svc_name := instruction:service_name::VARCHAR;
        full_svc := '"' || REPLACE(:db, '"', '""') || '"."' || REPLACE(:schema, '"', '""') || '"."' || REPLACE(:svc_name, '"', '""') || '"';

        LET res RESULTSET := (DESCRIBE CORTEX SEARCH SERVICE IDENTIFIER(:full_svc));
        LET rows ARRAY := ARRAY_CONSTRUCT();
        LET c CURSOR FOR res;
        OPEN c;
        LET row_data VARIANT;
        LOOP
            FETCH c INTO row_data;
            IF (SQLCODE != 0) THEN LEAVE; END IF;
            rows := ARRAY_APPEND(:rows, :row_data);
        END LOOP;
        CLOSE c;

        RETURN OBJECT_CONSTRUCT('success', TRUE, 'command', 'describe', 'data', :rows, 'error', NULL);
    END IF;

    -- ================================================================
    -- DROP: Drop a Cortex Search Service
    -- ================================================================
    IF (:cmd = 'DROP') THEN
        db := instruction:db::VARCHAR;
        schema := instruction:schema::VARCHAR;
        svc_name := instruction:service_name::VARCHAR;
        full_svc := '"' || REPLACE(:db, '"', '""') || '"."' || REPLACE(:schema, '"', '""') || '"."' || REPLACE(:svc_name, '"', '""') || '"';

        BEGIN
            EXECUTE IMMEDIATE 'DROP CORTEX SEARCH SERVICE ' || :full_svc;
            RETURN OBJECT_CONSTRUCT('success', TRUE, 'command', 'drop', 'data', OBJECT_CONSTRUCT('dropped', :svc_name), 'error', NULL);
        EXCEPTION
            WHEN OTHER THEN
                RETURN OBJECT_CONSTRUCT('success', FALSE, 'command', 'drop', 'data', NULL, 'error', SQLERRM);
        END;
    END IF;

    -- ================================================================
    -- ALTER: Alter target lag and/or grant roles
    -- ================================================================
    IF (:cmd = 'ALTER') THEN
        svc_name := instruction:service_name::VARCHAR;
        db := instruction:db::VARCHAR;
        schema := instruction:schema::VARCHAR;
        full_svc := '"' || REPLACE(:db, '"', '""') || '"."' || REPLACE(:schema, '"', '""') || '"."' || REPLACE(:svc_name, '"', '""') || '"';

        -- Alter target lag if provided
        IF (instruction:target_lag IS NOT NULL AND instruction:target_lag_unit IS NOT NULL) THEN
            LET lag_str VARCHAR := instruction:target_lag::VARCHAR || ' ' || instruction:target_lag_unit::VARCHAR;
            BEGIN
                EXECUTE IMMEDIATE 'ALTER CORTEX SEARCH SERVICE ' || :full_svc || ' SET TARGET_LAG = ''' || :lag_str || '''';
            EXCEPTION
                WHEN OTHER THEN
                    RETURN OBJECT_CONSTRUCT('success', FALSE, 'command', 'alter', 'data', NULL, 'error', SQLERRM);
            END;
        END IF;

        -- Grant USAGE if roles provided
        IF (instruction:grant_roles IS NOT NULL) THEN
            CALL chunky_internal_grant_table(:db, :schema, :svc_name, instruction:grant_roles);
        END IF;

        RETURN OBJECT_CONSTRUCT('success', TRUE, 'command', 'alter', 'data', OBJECT_CONSTRUCT('service', :svc_name), 'error', NULL);
    END IF;

    -- ================================================================
    -- CREATE: Create a Cortex Search Service
    -- ================================================================
    IF (:cmd = 'CREATE') THEN
        svc_name := instruction:service_name::VARCHAR;
        db := instruction:db::VARCHAR;
        schema := instruction:schema::VARCHAR;

        -- Build the CREATE SQL from instruction JSON
        -- This mirrors _build_create_sql from page4_complete.py
        LET tables VARIANT := instruction:tables;
        LET search_cols VARIANT := instruction:search_columns;
        LET attr_cols VARIANT := instruction:attribute_columns;
        LET lag_num INTEGER := COALESCE(instruction:target_lag::INTEGER, 365);
        LET lag_unit VARCHAR := COALESCE(instruction:target_lag_unit::VARCHAR, 'days');
        LET target_lag_str VARCHAR := :lag_num || ' ' || :lag_unit;

        -- Collect text cols, vector cols, attribute cols
        LET text_cols ARRAY := ARRAY_CONSTRUCT();
        LET vector_cols ARRAY := ARRAY_CONSTRUCT();
        LET attr_list ARRAY := ARRAY_CONSTRUCT();
        LET all_search_cols ARRAY := ARRAY_CONSTRUCT();

        -- Parse search columns
        IF (:search_cols IS NOT NULL) THEN
            LET sc RESULTSET := (
                SELECT
                    value:table::VARCHAR AS tbl,
                    value:column::VARCHAR AS col,
                    COALESCE(value:search_type::VARCHAR, 'Hybrid') AS stype,
                    COALESCE(value:embedding_model::VARCHAR, '') AS model
                FROM LATERAL FLATTEN(input => :search_cols)
            );
            LET sc_c CURSOR FOR sc;
            OPEN sc_c;
            LET sc_row VARIANT;
            LOOP
                FETCH sc_c INTO sc_row;
                IF (SQLCODE != 0) THEN LEAVE; END IF;
                IF (sc_row:stype LIKE '%Text%' AND NOT ARRAY_CONTAINS(sc_row:col::VARIANT, :text_cols)) THEN
                    text_cols := ARRAY_APPEND(:text_cols, sc_row:col);
                END IF;
                IF (sc_row:stype LIKE '%Vector%' OR sc_row:stype LIKE '%Hybrid%') THEN
                    vector_cols := ARRAY_APPEND(:vector_cols, OBJECT_CONSTRUCT('col', sc_row:col, 'model', sc_row:model));
                END IF;
                IF (NOT ARRAY_CONTAINS(sc_row:col::VARIANT, :all_search_cols)) THEN
                    all_search_cols := ARRAY_APPEND(:all_search_cols, sc_row:col);
                END IF;
            END LOOP;
            CLOSE sc_c;
        END IF;

        -- Parse attribute columns
        IF (:attr_cols IS NOT NULL) THEN
            LET ac RESULTSET := (
                SELECT DISTINCT value:column::VARCHAR AS col
                FROM LATERAL FLATTEN(input => :attr_cols)
            );
            LET ac_c CURSOR FOR ac;
            OPEN ac_c;
            LET ac_row VARCHAR;
            LOOP
                FETCH ac_c INTO ac_row;
                IF (SQLCODE != 0) THEN LEAVE; END IF;
                IF (NOT ARRAY_CONTAINS(:ac_row::VARIANT, :attr_list)) THEN
                    attr_list := ARRAY_APPEND(:attr_list, :ac_row);
                END IF;
            END LOOP;
            CLOSE ac_c;
        END IF;

        -- Determine single-index vs multi-index
        LET use_single BOOLEAN := (ARRAY_SIZE(:all_search_cols) = 1 AND ARRAY_SIZE(:vector_cols) <= 1);

        -- Build the DDL
        LET ddl VARCHAR := 'CREATE OR REPLACE CORTEX SEARCH SERVICE "'
            || REPLACE(:db, '"', '""') || '"."'
            || REPLACE(:schema, '"', '""') || '"."'
            || REPLACE(:svc_name, '"', '""') || '"\n';

        IF (:use_single) THEN
            ddl := :ddl || '  ON "' || :all_search_cols[0] || '"\n';
            IF (ARRAY_SIZE(:attr_list) > 0) THEN
                LET attr_clause VARCHAR := '';
                LET ai INTEGER;
                FOR ai IN 0 TO ARRAY_SIZE(:attr_list) - 1 DO
                    IF (ai > 0) THEN attr_clause := :attr_clause || ', '; END IF;
                    attr_clause := :attr_clause || '"' || :attr_list[ai] || '"';
                END FOR;
                ddl := :ddl || '  ATTRIBUTES ' || :attr_clause || '\n';
            END IF;
            ddl := :ddl || '  TARGET_LAG = ''' || :target_lag_str || '''\n';
            IF (ARRAY_SIZE(:vector_cols) > 0) THEN
                ddl := :ddl || '  EMBEDDING_MODEL = ''' || :vector_cols[0]:model || '''\n';
            END IF;
        ELSE
            IF (ARRAY_SIZE(:text_cols) > 0) THEN
                LET tc VARCHAR := '';
                LET ti INTEGER;
                FOR ti IN 0 TO ARRAY_SIZE(:text_cols) - 1 DO
                    IF (ti > 0) THEN tc := :tc || ', '; END IF;
                    tc := :tc || '"' || :text_cols[ti] || '"';
                END FOR;
                ddl := :ddl || '  TEXT INDEXES ' || :tc || '\n';
            END IF;
            IF (ARRAY_SIZE(:vector_cols) > 0) THEN
                LET vc VARCHAR := '';
                LET vi INTEGER;
                FOR vi IN 0 TO ARRAY_SIZE(:vector_cols) - 1 DO
                    IF (vi > 0) THEN vc := :vc || ', '; END IF;
                    vc := :vc || '"' || :vector_cols[vi]:col || '" (model=''' || :vector_cols[vi]:model || ''')';
                END FOR;
                ddl := :ddl || '  VECTOR INDEXES ' || :vc || '\n';
            END IF;
            IF (ARRAY_SIZE(:attr_list) > 0) THEN
                LET ac2 VARCHAR := '';
                LET ai2 INTEGER;
                FOR ai2 IN 0 TO ARRAY_SIZE(:attr_list) - 1 DO
                    IF (ai2 > 0) THEN ac2 := :ac2 || ', '; END IF;
                    ac2 := :ac2 || '"' || :attr_list[ai2] || '"';
                END FOR;
                ddl := :ddl || '  ATTRIBUTES ' || :ac2 || '\n';
            END IF;
            ddl := :ddl || '  TARGET_LAG = ''' || :target_lag_str || '''\n';
        END IF;

        -- Build UNION ALL query
        LET union_parts ARRAY := ARRAY_CONSTRUCT();
        LET all_cols ARRAY := ARRAY_CONSTRUCT();
        -- Merge search + attr columns
        LET ci INTEGER;
        FOR ci IN 0 TO ARRAY_SIZE(:all_search_cols) - 1 DO
            IF (NOT ARRAY_CONTAINS(:all_search_cols[ci], :all_cols)) THEN
                all_cols := ARRAY_APPEND(:all_cols, :all_search_cols[ci]);
            END IF;
        END FOR;
        FOR ci IN 0 TO ARRAY_SIZE(:attr_list) - 1 DO
            IF (NOT ARRAY_CONTAINS(:attr_list[ci], :all_cols)) THEN
                all_cols := ARRAY_APPEND(:all_cols, :attr_list[ci]);
            END IF;
        END FOR;

        IF (:tables IS NOT NULL) THEN
            LET tbl_res RESULTSET := (
                SELECT value::VARCHAR AS tbl FROM LATERAL FLATTEN(input => :tables)
            );
            LET tbl_c CURSOR FOR tbl_res;
            OPEN tbl_c;
            LET tbl_name VARCHAR;
            LOOP
                FETCH tbl_c INTO tbl_name;
                IF (SQLCODE != 0) THEN LEAVE; END IF;

                LET full_tbl VARCHAR := '"' || REPLACE(:db, '"', '""') || '"."'
                    || REPLACE(:schema, '"', '""') || '"."'
                    || REPLACE(:tbl_name, '"', '""') || '"';

                -- Build SELECT list: existing cols as-is, missing as NULL
                LET select_parts VARCHAR := '';
                LET col_name VARCHAR;
                LET tbl_has_col BOOLEAN;
                LET cj INTEGER;
                FOR cj IN 0 TO ARRAY_SIZE(:all_cols) - 1 DO
                    col_name := :all_cols[cj];
                    -- Check if this table has this column in search_cols or attr_cols
                    tbl_has_col := FALSE;
                    IF (:search_cols IS NOT NULL) THEN
                        LET chk RESULTSET := (
                            SELECT 1 FROM LATERAL FLATTEN(input => :search_cols)
                            WHERE value:table::VARCHAR = :tbl_name AND value:column::VARCHAR = :col_name
                            LIMIT 1
                        );
                        LET chk_c CURSOR FOR chk;
                        OPEN chk_c;
                        LET chk_row INTEGER;
                        FETCH chk_c INTO chk_row;
                        IF (FOUND) THEN tbl_has_col := TRUE; END IF;
                        CLOSE chk_c;
                    END IF;
                    IF (NOT :tbl_has_col AND :attr_cols IS NOT NULL) THEN
                        LET chk2 RESULTSET := (
                            SELECT 1 FROM LATERAL FLATTEN(input => :attr_cols)
                            WHERE value:table::VARCHAR = :tbl_name AND value:column::VARCHAR = :col_name
                            LIMIT 1
                        );
                        LET chk2_c CURSOR FOR chk2;
                        OPEN chk2_c;
                        LET chk2_row INTEGER;
                        FETCH chk2_c INTO chk2_row;
                        IF (FOUND) THEN tbl_has_col := TRUE; END IF;
                        CLOSE chk2_c;
                    END IF;

                    IF (cj > 0) THEN select_parts := :select_parts || ', '; END IF;
                    IF (:tbl_has_col) THEN
                        select_parts := :select_parts || '"' || :col_name || '"';
                    ELSE
                        select_parts := :select_parts || 'NULL AS "' || :col_name || '"';
                    END IF;
                END FOR;

                union_parts := ARRAY_APPEND(:union_parts,
                    '  SELECT ' || :select_parts || ' FROM ' || :full_tbl);
            END LOOP;
            CLOSE tbl_c;
        END IF;

        -- Join UNION ALL
        LET as_query VARCHAR := '';
        LET ui INTEGER;
        FOR ui IN 0 TO ARRAY_SIZE(:union_parts) - 1 DO
            IF (ui > 0) THEN as_query := :as_query || '\nUNION ALL\n'; END IF;
            as_query := :as_query || :union_parts[ui];
        END FOR;

        ddl := :ddl || 'AS (\n' || :as_query || '\n);';

        -- Execute the CREATE
        BEGIN
            EXECUTE IMMEDIATE :ddl;

            -- Grant USAGE on service
            IF (instruction:grant_roles IS NOT NULL) THEN
                LET grant_svc VARCHAR := '';
                LET role_res RESULTSET := (
                    SELECT value::VARCHAR AS r FROM LATERAL FLATTEN(input => instruction:grant_roles)
                );
                LET role_c CURSOR FOR role_res;
                OPEN role_c;
                LET role_name VARCHAR;
                LOOP
                    FETCH role_c INTO role_name;
                    IF (SQLCODE != 0) THEN LEAVE; END IF;
                    IF (UPPER(:role_name) != 'IT_AI') THEN
                        BEGIN
                            EXECUTE IMMEDIATE 'GRANT USAGE ON CORTEX SEARCH SERVICE "'
                                || REPLACE(:db, '"', '""') || '"."'
                                || REPLACE(:schema, '"', '""') || '"."'
                                || REPLACE(:svc_name, '"', '""') || '" TO ROLE "'
                                || UPPER(REPLACE(:role_name, '"', '""')) || '"';
                        EXCEPTION
                            WHEN OTHER THEN NULL; -- Best effort
                        END;
                    END IF;
                END LOOP;
                CLOSE role_c;
            END IF;

            RETURN OBJECT_CONSTRUCT(
                'success', TRUE,
                'command', 'create',
                'data', OBJECT_CONSTRUCT('service_name', :svc_name, 'ddl', :ddl),
                'error', NULL
            );
        EXCEPTION
            WHEN OTHER THEN
                RETURN OBJECT_CONSTRUCT(
                    'success', FALSE,
                    'command', 'create',
                    'data', OBJECT_CONSTRUCT('ddl', :ddl),
                    'error', SQLERRM
                );
        END;
    END IF;

    -- Unknown command
    RETURN OBJECT_CONSTRUCT('success', FALSE, 'command', :cmd, 'data', NULL, 'error', 'Unknown command: ' || :cmd);
END
$$;
