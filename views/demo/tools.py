# views/refinery/tab_tools.py
# Tools Tab - Maintenance Tools for the Doc Refinery package
import streamlit as st
import uuid
from logger_config import log_action
from views.demo.ingestion_core import _execute_surgical_delete_with_shift
from views.demo.refinery_common import execute_sql_safe
from utils.constants import TEMP_IMAGE_PREFIX

def render_tools_tab(session):
    st.subheader("5. Maintenance Tools")
    ctx = st.session_state.auth_context

    if st.button("🧹 Clear Temp Stages"):
        try:
            session.sql(f"REMOVE @{ctx['db']}.{ctx['schema']}.{ctx['stage']}/{TEMP_IMAGE_PREFIX}").collect()
            st.success("Cleaned")
        except Exception as e:
            st.warning(f"Error: {e}")

    st.divider()
    st.markdown("### 🧪 Shift Engine Self-Test")
    st.caption("Runs a real workflow against the staging DB to verify the surgical shift logic.")

    if st.button("▶️ Run Shift Engine Self-Test"):
        _run_shift_engine_health_check(session, ctx)


def _run_shift_engine_health_check(session, ctx):
    """
    Health checker for _execute_surgical_delete_with_shift.

    Creates a temp table with the SUS_CHUNKS schema, inserts 10 synthetic
    rows simulating an original document, then calls the shift function
    with a range mapping that replaces pages 2-3 with 5 replacement pages
    (delta=+3). Verifies:
    1. Pages 2-3 are deleted.
    2. Pages 4-10 are shifted to 7-13.
    3. CHUNK_REF strings are updated from "Page Num: N" to "Page Num: N+3".

    Then tests a second scenario: negative delta (replace 3 pages with 1).
    Then tests a third scenario: zero delta (replace 1 page with 1 page).

    Returns structured result dict and displays it in the UI.
    """
    db = ctx["db"]
    schema = ctx["schema"]
    safe_db = db.replace('"', '""')
    safe_sch = schema.replace('"', '""')
    temp_table = f"TEMP_SHIFT_TEST_{uuid.uuid4().hex[:8]}"
    temp_full = f'"{safe_db}"."{safe_sch}"."{temp_table}"'
    test_file = "shift_test_document.pdf"

    results = []

    try:
        # Step 1: Create temp table with SUS_CHUNKS schema
        # Ref: ingestion_core.py:24-29 for the canonical schema
        session.sql(f"""
            CREATE TABLE {temp_full} (
                RELATIVE_PATH VARCHAR, PAGE_NUMBER NUMBER, CHUNK VARCHAR,
                CHUNK_ID VARCHAR, CHUNK_TYPE VARCHAR DEFAULT 'STANDARD',
                CHUNK_REF VARCHAR, LINK_BLOCK VARCHAR, CHUNK_METADATA VARIANT
            )
        """).collect()

        # Step 2: Insert 10 synthetic rows (pages 1-10)
        rows = []
        for pg in range(1, 11):
            chunk_id = f"TEST_CHK_{pg}"
            chunk_ref = f"Doc Source: {test_file} | Page Num: {pg}"
            rows.append(
                f"('{test_file}', {pg}, 'content_page_{pg}', '{chunk_id}', 'STANDARD', '{chunk_ref}', '', NULL)"
            )
        insert_sql = f"""
            INSERT INTO {temp_full} (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE, CHUNK_REF, LINK_BLOCK, CHUNK_METADATA)
            VALUES {', '.join(rows)}
        """
        session.sql(insert_sql).collect()

        # Step 3: Scenario 1 — Positive delta (+3)
        # Replace source pages 2-3 (size=2) with replacement pages 1-5 (size=5)
        # Expected: delta=+3, pages 4-10 shift to 7-13
        st.markdown("**Scenario 1: Positive delta (+3)**")
        st.write("Replace table pages 2-3 with PDF pages 1-5 → delta=+3")

        range_mappings_1 = [{
            'source_start': 2, 'source_end': 3,
            'replacement_start': 1, 'replacement_end': 5
        }]

        ok, err = _execute_surgical_delete_with_shift(
            session, temp_full, test_file, range_mappings_1, [], 0
        )

        if not ok:
            results.append({'test': 'scenario_1_shift', 'status': 'FAIL', 'error': err})
        else:
            # Verify: pages 2-3 deleted, pages 4-10 shifted to 7-13
            res = session.sql(f"""
                SELECT PAGE_NUMBER, CHUNK_REF FROM {temp_full}
                WHERE RELATIVE_PATH = '{test_file}'
                ORDER BY PAGE_NUMBER
            """).collect()

            remaining_pages = {r[0] for r in res}
            expected_pages = {1, 7, 8, 9, 10, 11, 12, 13}
            pages_ok = remaining_pages == expected_pages

            # Verify CHUNK_REF re-stamping
            ref_ok = True
            for r in res:
                pg = r[0]
                ref = r[1]
                expected_ref = f"Doc Source: {test_file} | Page Num: {pg}"
                if ref != expected_ref:
                    ref_ok = False
                    results.append({
                        'test': f'scenario_1_chunk_ref_page_{pg}',
                        'status': 'FAIL',
                        'expected': expected_ref,
                        'actual': ref
                    })

            results.append({
                'test': 'scenario_1_pages',
                'status': 'PASS' if pages_ok else 'FAIL',
                'expected': sorted(expected_pages),
                'actual': sorted(remaining_pages)
            })
            results.append({
                'test': 'scenario_1_chunk_refs',
                'status': 'PASS' if ref_ok else 'FAIL'
            })

        # Step 4: Scenario 2 — Negative delta (-2)
        # Clean up and re-insert fresh rows
        st.markdown("**Scenario 2: Negative delta (-2)**")
        st.write("Replace table pages 5-7 (size=3) with PDF pages 1-1 (size=1) → delta=-2")

        session.sql(f"DELETE FROM {temp_full} WHERE RELATIVE_PATH = '{test_file}'").collect()
        rows2 = []
        for pg in range(1, 11):
            chunk_id = f"TEST_CHK_2_{pg}"
            chunk_ref = f"Doc Source: {test_file} | Page Num: {pg}"
            rows2.append(
                f"('{test_file}', {pg}, 'content_page_{pg}', '{chunk_id}', 'STANDARD', '{chunk_ref}', '', NULL)"
            )
        session.sql(f"INSERT INTO {temp_full} (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE, CHUNK_REF, LINK_BLOCK, CHUNK_METADATA) VALUES {', '.join(rows2)}").collect()

        range_mappings_2 = [{
            'source_start': 5, 'source_end': 7,
            'replacement_start': 1, 'replacement_end': 1
        }]

        ok, err = _execute_surgical_delete_with_shift(
            session, temp_full, test_file, range_mappings_2, [], 0
        )

        if not ok:
            results.append({'test': 'scenario_2_shift', 'status': 'FAIL', 'error': err})
        else:
            res = session.sql(f"""
                SELECT PAGE_NUMBER FROM {temp_full}
                WHERE RELATIVE_PATH = '{test_file}'
                ORDER BY PAGE_NUMBER
            """).collect()
            remaining_pages = {r[0] for r in res}
            # Pages 5-7 deleted. Pages 8-10 shifted by -2 → 6-8.
            # But wait: pages 1-4 stay. Pages 8→6, 9→7, 10→8.
            # So remaining: {1, 2, 3, 4, 6, 7, 8}
            expected_pages = {1, 2, 3, 4, 6, 7, 8}
            pages_ok = remaining_pages == expected_pages
            results.append({
                'test': 'scenario_2_pages',
                'status': 'PASS' if pages_ok else 'FAIL',
                'expected': sorted(expected_pages),
                'actual': sorted(remaining_pages)
            })

        # Step 5: Scenario 3 — Zero delta (no shift)
        st.markdown("**Scenario 3: Zero delta (no shift)**")
        st.write("Replace table pages 4-4 (size=1) with PDF pages 1-1 (size=1) → delta=0")

        session.sql(f"DELETE FROM {temp_full} WHERE RELATIVE_PATH = '{test_file}'").collect()
        rows3 = []
        for pg in range(1, 11):
            chunk_id = f"TEST_CHK_3_{pg}"
            chunk_ref = f"Doc Source: {test_file} | Page Num: {pg}"
            rows3.append(
                f"('{test_file}', {pg}, 'content_page_{pg}', '{chunk_id}', 'STANDARD', '{chunk_ref}', '', NULL)"
            )
        session.sql(f"INSERT INTO {temp_full} (RELATIVE_PATH, PAGE_NUMBER, CHUNK, CHUNK_ID, CHUNK_TYPE, CHUNK_REF, LINK_BLOCK, CHUNK_METADATA) VALUES {', '.join(rows3)}").collect()

        range_mappings_3 = [{
            'source_start': 4, 'source_end': 4,
            'replacement_start': 1, 'replacement_end': 1
        }]

        ok, err = _execute_surgical_delete_with_shift(
            session, temp_full, test_file, range_mappings_3, [], 0
        )

        if not ok:
            results.append({'test': 'scenario_3_shift', 'status': 'FAIL', 'error': err})
        else:
            res = session.sql(f"""
                SELECT PAGE_NUMBER FROM {temp_full}
                WHERE RELATIVE_PATH = '{test_file}'
                ORDER BY PAGE_NUMBER
            """).collect()
            remaining_pages = {r[0] for r in res}
            # Page 4 deleted, no shift. Pages 1-3, 5-10 remain.
            expected_pages = {1, 2, 3, 5, 6, 7, 8, 9, 10}
            pages_ok = remaining_pages == expected_pages
            results.append({
                'test': 'scenario_3_pages',
                'status': 'PASS' if pages_ok else 'FAIL',
                'expected': sorted(expected_pages),
                'actual': sorted(remaining_pages)
            })

    except Exception as e:
        results.append({'test': 'health_check_exception', 'status': 'FAIL', 'error': str(e)})
        log_action("SHIFT_HEALTH_CHECK_ERROR", {"error": str(e)})

    finally:
        # Always clean up the temp table
        try:
            session.sql(f"DROP TABLE IF EXISTS {temp_full}").collect()
        except Exception:
            pass

    # Display results
    all_pass = all(r.get('status') == 'PASS' for r in results)
    if all_pass:
        st.success("✅ All shift engine tests PASSED")
    else:
        st.error("❌ Some shift engine tests FAILED")

    st.json(results)

    return {
        'status': 'healthy' if all_pass else 'unhealthy',
        'checks': results,
        'rawOutput': str(results)
    }
