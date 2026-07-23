-- ============================================================================
-- chunky_internal_grant_table
-- Grants ALL PRIVILEGES on a table to specified roles with retry logic.
-- Shared by: chunky_chunks, chunky_searchservice
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_internal_grant_table(
    db VARCHAR,
    schema VARCHAR,
    table_name VARCHAR,
    roles VARIANT
)
RETURNS VARIANT
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
    full_table VARCHAR := '"' || REPLACE(db, '"', '""') || '"."' || REPLACE(schema, '"', '""') || '"."' || REPLACE(table_name, '"', '""') || '"';
    role_pattern VARCHAR := '^[A-Z_][A-Z0-9_$]*$';
    r VARCHAR;
    safe_role VARCHAR;
    grant_sql VARCHAR;
    failed_roles ARRAY := ARRAY_CONSTRUCT();
    success_roles ARRAY := ARRAY_CONSTRUCT();
    attempt INTEGER;
    grant_ok BOOLEAN;
BEGIN
    LET role_list RESULTSET := (
        SELECT value::VARCHAR AS role_name
        FROM LATERAL FLATTEN(input => :roles)
    );

    LET c CURSOR FOR role_list;
    OPEN c;
    LOOP
        FETCH c INTO r;
        IF (SQLCODE != 0) THEN LEAVE; END IF;

        -- Skip empty or IT_AI
        IF (r IS NULL OR r = '' OR UPPER(r) = 'IT_AI') THEN
            CONTINUE;
        END IF;

        -- Skip invalid role names
        IF (NOT RLIKE(UPPER(r), :role_pattern, 'i')) THEN
            failed_roles := ARRAY_APPEND(:failed_roles, r || ' (Invalid Syntax)');
            CONTINUE;
        END IF;

        -- Build grant SQL
        IF (STARTSWITH(r, '"') AND ENDSWITH(r, '"')) THEN
            grant_sql := 'GRANT ALL PRIVILEGES ON TABLE ' || :full_table || ' TO ROLE ' || r;
        ELSE
            safe_role := UPPER(REPLACE(r, '"', '""'));
            grant_sql := 'GRANT ALL PRIVILEGES ON TABLE ' || :full_table || ' TO ROLE "' || :safe_role || '"';
        END IF;

        -- Retry logic (2 attempts)
        grant_ok := FALSE;
        FOR attempt IN 1 TO 2 DO
            BEGIN
                EXECUTE IMMEDIATE :grant_sql;
                grant_ok := TRUE;
                BREAK;
            EXCEPTION
                WHEN OTHER THEN
                    IF (attempt = 1) THEN
                        -- Wait 3 seconds before retry (not possible in SQL SP, just retry immediately)
                        NULL;
                    END IF;
            END;
        END FOR;

        IF (grant_ok) THEN
            success_roles := ARRAY_APPEND(:success_roles, UPPER(r));
        ELSE
            failed_roles := ARRAY_APPEND(:failed_roles, UPPER(r));
        END IF;
    END LOOP;
    CLOSE c;

    RETURN OBJECT_CONSTRUCT(
        'success', ARRAY_SIZE(:failed_roles) = 0,
        'success_roles', :success_roles,
        'failed_roles', :failed_roles
    );
END
$$;
