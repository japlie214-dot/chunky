-- ============================================================================
-- chunky_internal_build_chunk_ref
-- Builds the canonical CHUNK_REF string from file + page + optional link.
-- Shared by: chunky_chunks, chunky_qa
-- ============================================================================
CREATE OR REPLACE PROCEDURE chunky_internal_build_chunk_ref(
    rel_path VARCHAR,
    page_num NUMBER,
    link VARCHAR
)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
    SELECT
        CASE
            WHEN link IS NULL OR link = '' THEN
                'Doc Source: ' || rel_path || ' | Page Num: ' || TO_VARCHAR(page_num)
            ELSE
                '[Digital Copy](' || REPLACE(REPLACE(REPLACE(link, ' ', '%20'), '(', '%28'), ')', '%29') || ') | Doc Source: ' || rel_path || ' | Page Num: ' || TO_VARCHAR(page_num)
        END
$$;
