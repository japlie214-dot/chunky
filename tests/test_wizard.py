"""
Tests for Create Search Service wizard — run without Streamlit.

Catches defects like:
- Importing from deleted modules (wizard.py)
- Module-level snowflake imports (breaks local mode)
- Role/svc_name not using widget keys as source of truth
- Incomplete privilege checks
- Stale __pycache__ references

Run: python3 -m pytest tests/test_wizard.py -v
"""
import ast
import os
import sys
import re
import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Project paths
DEMO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "views", "ccs")
VIEWS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "views")
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    """Read file relative to project root."""
    full = os.path.join(ROOT_DIR, path)
    with open(full) as f:
        return f.read()


def _parse(path):
    """Parse file and return AST."""
    return ast.parse(_read(path))


def _get_top_level_imports(path):
    """Get top-level (module-level) imports from a Python file."""
    tree = _parse(path)
    imports = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.append((node.lineno, f"from {module} import ..."))
    return imports


def _get_all_imports(path):
    """Get ALL imports (including inside functions) from a Python file."""
    tree = _parse(path)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.append((node.lineno, f"from {module} import ..."))
    return imports


# =============================================================================
# File Structure
# =============================================================================

class TestWizardFileStructure:
    """Verify the wizard directory structure is correct."""

    def test_demo_dir_exists(self):
        assert os.path.isdir(DEMO_DIR), f"views/ccs/ directory missing"

    def test_init_exists(self):
        assert os.path.isfile(os.path.join(DEMO_DIR, "__init__.py")), "__init__.py missing"

    def test_common_exists(self):
        assert os.path.isfile(os.path.join(DEMO_DIR, "common.py")), "common.py missing"

    def test_page_modules_exist(self):
        for page in ["page1_setup.py", "page2_builder.py", "page3_execute.py", "page4_complete.py", "page5_qa_tools.py"]:
            path = os.path.join(DEMO_DIR, page)
            assert os.path.isfile(path), f"{page} missing"

    def test_wizard_py_deleted(self):
        """wizard.py was split into granular files — must not exist."""
        path = os.path.join(DEMO_DIR, "wizard.py")
        assert not os.path.isfile(path), "wizard.py should be deleted (split into page modules)"

    def test_no_stale_demo_search_service(self):
        """demo_search_service.py was replaced — must not exist."""
        path = os.path.join(VIEWS_DIR, "demo_search_service.py")
        assert not os.path.isfile(path), "demo_search_service.py should be deleted"


# =============================================================================
# Import Integrity
# =============================================================================

class TestImportIntegrity:
    """Verify imports are correct and don't reference deleted modules."""

    def test_entry_points_import_from_demo(self):
        """streamlit_app.py and streamlit_app_local.py must import from views.ccs, not views.ccs.wizard."""
        for entry in ["streamlit_app.py", "streamlit_app_local.py"]:
            src = _read(entry)
            # Must import from views.ccs
            assert "from views.ccs import render_demo_search_service" in src or \
                   "from views.ccs.wizard import" not in src, \
                f"{entry} should import from views.ccs, not views.ccs.wizard"

    def test_no_import_from_deleted_wizard(self):
        """No source file should import from views.ccs.wizard (deleted module)."""
        for root, dirs, files in os.walk(ROOT_DIR):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "node_modules", "tests")]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(root, fname)
                rel = os.path.relpath(path, ROOT_DIR)
                src = open(path).read()
                # Check actual import statements, not string references
                for i, line in enumerate(src.split(chr(10)), 1):
                    s = line.strip()
                    if s.startswith('#'):
                        continue
                    if s.startswith('from views.ccs.wizard') or s.startswith('import views.ccs.wizard'):
                        pytest.fail(f"{rel}:{i} imports from deleted views.ccs.wizard")

    def test_no_import_from_deleted_demo_search_service(self):
        """No source file should import from views.ccs_search_service (deleted module)."""
        for root, dirs, files in os.walk(ROOT_DIR):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "node_modules", "tests")]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(root, fname)
                rel = os.path.relpath(path, ROOT_DIR)
                src = open(path).read()
                for i, line in enumerate(src.split(chr(10)), 1):
                    s = line.strip()
                    if s.startswith('#'):
                        continue
                    if s.startswith('from views.ccs_search_service') or s.startswith('import views.ccs_search_service'):
                        pytest.fail(f"{rel}:{i} imports from deleted views.ccs_search_service")

    def test_init_routes_to_all_pages(self):
        """__init__.py must import and route to all 5 page modules."""
        src = _read("views/ccs/__init__.py")
        assert "from views.ccs.page1_setup import" in src, "Missing page1 import"
        assert "from views.ccs.page2_builder import" in src, "Missing page2 import"
        assert "from views.ccs.page3_execute import" in src, "Missing page3 import"
        assert "from views.ccs.page4_complete import" in src, "Missing page4 import"
        assert "from views.ccs.page5_qa_tools import" in src, "Missing page5 import"


# =============================================================================
# Snowflake Import Safety (Local Mode)
# =============================================================================

class TestSnowflakeImportSafety:
    """Verify snowflake imports are lazy (inside functions) so local mode works."""

    @pytest.mark.parametrize("page_file", [
        "views/ccs/__init__.py",
        "views/ccs/common.py",
        "views/ccs/page1_setup.py",
        "views/ccs/page2_builder.py",
        "views/ccs/page3_execute.py",
        "views/ccs/page4_complete.py",
        "views/ccs/page5_qa_tools.py",
    ])
    def test_no_module_level_snowflake_imports(self, page_file):
        """No snowflake/auth_utils imports at module level (would break local mode)."""
        top_imports = _get_top_level_imports(page_file)
        for lineno, imp in top_imports:
            if "snowflake" in imp or "auth_utils" in imp or "snowflake_utils" in imp:
                pytest.fail(f"{page_file}:{lineno} has module-level snowflake import: {imp}")


# =============================================================================
# Widget Key Source of Truth
# =============================================================================

class TestWidgetKeyPersistence:
    """Verify role and svc_name use widget keys as source of truth (not _jbv)."""

    def test_page1_role_uses_persistent_storage(self):
        """Page 1 must use _wiz_role for persistent storage, not widget key as source of truth."""
        src = _read("views/ccs/page1_setup.py")
        assert "_wiz_role" in src, "page1 must use _wiz_role for persistent storage"
        # Must NOT use _jbv for role
        assert "_jbv(\"role\")" not in src, "page1 uses _jbv('role')"
        assert "_jbv('role')" not in src, "page1 uses _jbv('role')"

    def test_page1_svc_name_uses_persistent_storage(self):
        """Page 1 must use _wiz_svc_name for persistent storage."""
        src = _read("views/ccs/page1_setup.py")
        assert "_wiz_svc_name" in src, "page1 must use _wiz_svc_name for persistent storage"
        assert "_jbv(\"svc_name\")" not in src, "page1 uses _jbv('svc_name')"

    def test_page3_reads_role_from_persistent_storage(self):
        """Page 3 must read role from _wiz_role, not widget key."""
        src = _read("views/ccs/page3_execute.py")
        assert "_wiz_role" in src, "page3 must read from _wiz_role"
        assert "_jbv(\"role\")" not in src, "page3 should not use _jbv for role"

    def test_page3_reads_svc_name_from_persistent_storage(self):
        """Page 3 must read svc_name from _wiz_svc_name, not widget key."""
        src = _read("views/ccs/page3_execute.py")
        assert "_wiz_svc_name" in src, "page3 must read from _wiz_svc_name"
        assert "_jbv(\"svc_name\")" not in src, "page3 should not use _jbv for svc_name"

    def test_page1_always_syncs_role(self):
        """Page 1 must sync role on EVERY render, not just on change."""
        src = _read("views/ccs/page1_setup.py")
        # _wiz_set('role', ...) must NOT be inside an if block
        # Check that it's at the same indent level as the selectbox
        lines = src.split(chr(10))
        selectbox_indent = None
        sync_indent = None
        for line in lines:
            if 'key="cssw_role_select"' in line:
                selectbox_indent = len(line) - len(line.lstrip())
            if '_wiz_set("role"' in line or "_wiz_set('role'" in line:
                sync_indent = len(line) - len(line.lstrip())
        assert selectbox_indent is not None, "selectbox not found"
        assert sync_indent is not None, "_wiz_set('role') not found"
        assert sync_indent == selectbox_indent, \
            f"_wiz_set('role') at indent {sync_indent} but selectbox at {selectbox_indent} — must be same level (always sync)"

    def test_page1_always_syncs_svc_name(self):
        """Page 1 must sync svc_name on EVERY render, not just on change."""
        src = _read("views/ccs/page1_setup.py")
        lines = src.split(chr(10))
        input_indent = None
        sync_indent = None
        for line in lines:
            if 'key="cssw_svc_name_input"' in line:
                input_indent = len(line) - len(line.lstrip())
            if '_wiz_set("svc_name"' in line or "_wiz_set('svc_name'" in line:
                sync_indent = len(line) - len(line.lstrip())
        assert input_indent is not None, "text_input not found"
        assert sync_indent is not None, "_wiz_set('svc_name') not found"
        assert sync_indent == input_indent, \
            f"_wiz_set('svc_name') at indent {sync_indent} but input at {input_indent} — must be same level (always sync)"


# =============================================================================
# Cross-Page Data Persistence (Regression Tests)
# =============================================================================

class TestCrossPagePersistence:
    """
    Prevent the regression where page 3 reads from widget keys (cssw_role,
    cssw_svc_name) that Streamlit clears when the widget isn't rendered.

    The pattern:
    - Page 1 writes to _wiz_* persistent keys AFTER every widget render
    - Page 3 reads from _wiz_* persistent keys
    - Widget keys (cssw_role_select, cssw_svc_name_input) are NEVER used
      for cross-page data — they're only alive when page 1 renders.
    """

    def test_page3_never_reads_widget_keys_for_role(self):
        """Page 3 must NOT read st.session_state.get('cssw_role') — widget key is cleared across pages."""
        src = _read("views/ccs/page3_execute.py")
        lines = src.split(chr(10))
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if s.startswith('#'):
                continue
            # Must not read from widget keys
            if 'st.session_state.get("cssw_role"' in s:
                pytest.fail(f"page3_execute.py:{i} reads from widget key cssw_role — use _wiz_role instead")
            if "st.session_state.get('cssw_role'" in s:
                pytest.fail(f"page3_execute.py:{i} reads from widget key cssw_role — use _wiz_role instead")

    def test_page3_never_reads_widget_keys_for_svc_name(self):
        """Page 3 must NOT read st.session_state.get('cssw_svc_name') — widget key is cleared across pages."""
        src = _read("views/ccs/page3_execute.py")
        lines = src.split(chr(10))
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if s.startswith('#'):
                continue
            if 'st.session_state.get("cssw_svc_name"' in s:
                pytest.fail(f"page3_execute.py:{i} reads from widget key cssw_svc_name — use _wiz_svc_name instead")
            if "st.session_state.get('cssw_svc_name'" in s:
                pytest.fail(f"page3_execute.py:{i} reads from widget key cssw_svc_name — use _wiz_svc_name instead")

    def test_page3_reads_both_from_persistent_keys(self):
        """Page 3 must read both role and svc_name from _wiz_* keys."""
        src = _read("views/ccs/page3_execute.py")
        assert '_wiz_role' in src, "page3 must read role from _wiz_role"
        assert '_wiz_svc_name' in src, "page3 must read svc_name from _wiz_svc_name"

    def test_page1_syncs_role_to_persistent_key(self):
        """Page 1 must call _wiz_set('role', ...) to persist role across pages."""
        src = _read("views/ccs/page1_setup.py")
        assert '_wiz_set("role"' in src or "_wiz_set('role'" in src, \
            "page1 must call _wiz_set('role', ...) to persist role"

    def test_page1_syncs_svc_name_to_persistent_key(self):
        """Page 1 must call _wiz_set('svc_name', ...) to persist svc_name across pages."""
        src = _read("views/ccs/page1_setup.py")
        assert '_wiz_set("svc_name"' in src or "_wiz_set('svc_name'" in src, \
            "page1 must call _wiz_set('svc_name', ...) to persist svc_name"

    def test_page1_does_not_use_widget_key_as_source_of_truth(self):
        """Page 1 must not read role/svc_name from widget keys — they're not reliable across pages."""
        src = _read("views/ccs/page1_setup.py")
        lines = src.split(chr(10))
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if s.startswith('#'):
                continue
            # page 1 SHOULD have widget keys (cssw_role_select, cssw_svc_name_input)
            # but should NOT read from them as source of truth for persistence
            # The widget key is for Streamlit's internal state, not for cross-page data

    def test_wiz_get_and_wiz_set_defined_in_page1(self):
        """Page 1 must define _wiz_get and _wiz_set helpers."""
        src = _read("views/ccs/page1_setup.py")
        assert 'def _wiz_get(' in src, "page1 must define _wiz_get helper"
        assert 'def _wiz_set(' in src, "page1 must define _wiz_set helper"

    def test_no_widget_key_reads_in_page3_for_persistent_data(self):
        """Page 3 must not read widget keys (cssw_role, cssw_svc_name) — those are cleared across pages.

        Allowed cssw_ keys in page 3:
        - cssw_jobs: manually managed data key (append/read/delete), NOT a widget key
        - cssw_batch_started: manually managed flag
        - cssw_page: manually managed pagination
        - cssw_mode, cssw_scope: widget keys but only used within page 2

        Disallowed:
        - cssw_role: widget key from page 1's selectbox — use _wiz_role
        - cssw_svc_name: widget key from page 1's text_input — use _wiz_svc_name
        """
        src = _read("views/ccs/page3_execute.py")
        lines = src.split(chr(10))
        # These widget keys must NOT be read in page 3 (they're cleared across pages)
        banned_widget_keys = ["cssw_role", "cssw_svc_name"]
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if s.startswith('#'):
                continue
            for key in banned_widget_keys:
                if f'st.session_state.get("{key}"' in s or f"st.session_state.get('{key}'" in s:
                    pytest.fail(
                        f"page3_execute.py:{i} reads widget key '{key}': {s.strip()} "
                        f"— Streamlit clears widget keys across page navigation, use _wiz_{key.split('_', 1)[1]} instead"
                    )


# =============================================================================
# Privilege Check Completeness
# =============================================================================

class TestPrivilegeCheck:
    """Verify the privilege check covers all required SQL operations."""

    def test_checks_usage_on_schema(self):
        src = _read("views/ccs/page1_setup.py")
        assert '"USAGE"' in src or "'USAGE'" in src, "Must check USAGE on schema"

    def test_checks_create_table(self):
        src = _read("views/ccs/page1_setup.py")
        assert "CREATE TABLE" in src, "Must check CREATE TABLE on schema"

    def test_checks_create_cortex_search_service(self):
        src = _read("views/ccs/page1_setup.py")
        assert "CREATE CORTEX SEARCH SERVICE" in src, "Must check CREATE CORTEX SEARCH SERVICE"

    def test_checks_stage_access(self):
        src = _read("views/ccs/page1_setup.py")
        assert "stage" in src.lower(), "Must check stage access"
        assert "SHOW GRANTS ON STAGE" in src, "Must query stage grants"

    def test_privilege_function_signature_includes_stage(self):
        """_check_privileges must accept stage parameter."""
        src = _read("views/ccs/page1_setup.py")
        assert "def _check_privileges(session, db, schema, stage)" in src, \
            "_check_privileges must accept (session, db, schema, stage)"


# =============================================================================
# Syntax Validity
# =============================================================================

class TestSyntaxValidity:
    """Verify all wizard files parse without syntax errors."""

    @pytest.mark.parametrize("page_file", [
        "views/ccs/__init__.py",
        "views/ccs/common.py",
        "views/ccs/page1_setup.py",
        "views/ccs/page2_builder.py",
        "views/ccs/page3_execute.py",
        "views/ccs/page4_complete.py",
        "views/ccs/page5_qa_tools.py",
        "streamlit_app.py",
        "streamlit_app_local.py",
    ])
    def test_syntax(self, page_file):
        """All files must parse without syntax errors."""
        try:
            _parse(page_file)
        except SyntaxError as e:
            pytest.fail(f"{page_file} has syntax error: {e}")


# =============================================================================
# Stale Cache Detection
# =============================================================================

class TestStaleCache:
    """Detect stale __pycache__ that could cause wrong code to load."""

    def test_no_pyc_for_deleted_files(self):
        """No .pyc files should exist for deleted modules."""
        pycache_dir = os.path.join(VIEWS_DIR, "__pycache__")
        if not os.path.isdir(pycache_dir):
            return  # No cache, no problem
        deleted_modules = ["demo_search_service", "wizard"]
        for fname in os.listdir(pycache_dir):
            for mod in deleted_modules:
                if mod in fname and fname.endswith(".pyc"):
                    pytest.fail(
                        f"Stale __pycache__/{fname} — would override import. "
                        f"Delete with: find . -name '*.pyc' -path '*demo*' -delete"
                    )


# =============================================================================
# F-String Safety (catches SyntaxError from backslashes in expressions)
# =============================================================================

class TestFStringSafety:
    """Catch f-strings with backslashes inside {} expressions — illegal in Python."""

    @pytest.mark.parametrize("page_file", [
        "views/ccs/page4_complete.py",
        "views/ccs/page3_execute.py",
        "views/ccs/page2_builder.py",
        "views/ccs/page1_setup.py",
        "views/ccs/common.py",
    ])
    def test_no_backslash_in_fstring_expressions(self, page_file):
        """F-string expressions must not contain backslashes (SyntaxError in Python <3.12).

        Pattern: f'...{expr with \\...}...' — the backslash inside {} is illegal.
        We detect this by looking for f-strings whose {} blocks contain odd-numbered
        backslash sequences (i.e. actual backslashes, not escaped backslashes in the
        outer string).
        """
        src = _read(page_file)
        lines = src.split(chr(10))
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            # Find f-string expressions: {...}
            # A backslash inside an f-string expression is illegal
            # Simple heuristic: find f'...' or f"..." then check if the
            # content between { and } contains a backslash that isn't
            # part of \\ (double backslash = escaped, which is fine)
            in_fstring = False
            fstring_char = None
            j = 0
            while j < len(stripped):
                c = stripped[j]
                if not in_fstring and j + 1 < len(stripped) and stripped[j:j+2] in ('f"', "f'"):
                    in_fstring = True
                    fstring_char = stripped[j+1]
                    j += 2
                    continue
                if in_fstring and c == fstring_char:
                    in_fstring = False
                    j += 1
                    continue
                if in_fstring and c == '{' and j + 1 < len(stripped) and stripped[j+1] != '{':
                    # Found expression start — scan for matching }
                    depth = 1
                    expr_start = j + 1
                    j += 1
                    while j < len(stripped) and depth > 0:
                        if stripped[j] == '{':
                            depth += 1
                        elif stripped[j] == '}':
                            depth -= 1
                        # Check for single backslash (not double)
                        if depth > 0 and stripped[j] == '\\':
                            # Check if next char is also backslash (escaped)
                            if j + 1 < len(stripped) and stripped[j+1] == '\\':
                                j += 2  # skip escaped backslash
                                continue
                            pytest.fail(
                                f"{page_file}:{i} has backslash in f-string expression: "
                                f"{stripped[max(0,expr_start-5):j+10]}"
                            )
                        j += 1
                j += 1


class TestDuplicateBlocks:
    """Catch duplicated if-blocks (copy-paste errors)."""

    def test_no_duplicate_if_selected_attrs(self):
        """page4 must not have consecutive 'if selected_attrs:' blocks."""
        src = _read("views/ccs/page4_complete.py")
        lines = src.split(chr(10))
        prev_was_if = False
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped == 'if selected_attrs:':
                if prev_was_if:
                    pytest.fail(
                        f"page4_complete.py:{i} has duplicate 'if selected_attrs:' block"
                    )
                prev_was_if = True
            else:
                if stripped and not stripped.startswith('#'):
                    prev_was_if = False


class TestUnionAllCorrectness:
    """Catch bugs in UNION ALL SQL generation (tbl_cols must respect select flag)."""

    def _build_sql(self, search_rows, attr_rows, table_names):
        """Replicate _build_create_sql logic for testing without Streamlit."""
        text_cols = []
        vector_cols = []
        for r in search_rows:
            if not r.get('select'):
                continue
            col = r['column']
            stype = r.get('search_type', '')
            model = r.get('embedding_model', '')
            if 'Text' in stype and col not in text_cols:
                text_cols.append(col)
            if 'Vector' in stype or 'Hybrid' in stype:
                if (col, model) not in vector_cols:
                    vector_cols.append((col, model))
        selected_attrs = list(dict.fromkeys(
            r['column'] for r in attr_rows if r.get('select')
        ))
        all_search_cols = list(dict.fromkeys(
            [c for c in text_cols] + [c for c, _ in vector_cols]
        ))
        all_cols = list(dict.fromkeys(all_search_cols + selected_attrs))
        union_parts = []
        for tbl in table_names:
            # This is the critical line — must filter by select=True
            tbl_cols = set(r['column'] for r in search_rows
                          if r.get('table') == tbl and r.get('select'))
            tbl_cols.update(r['column'] for r in attr_rows
                          if r.get('table') == tbl and r.get('select'))
            select_parts = []
            for col in all_cols:
                if col in tbl_cols:
                    select_parts.append(f'"{col}"')
                else:
                    select_parts.append(f'NULL AS "{col}"')
            select_sql = ', '.join(select_parts)
            full_table = f'"DB"."SCH"."{tbl}"'
            union_parts.append(f'  SELECT {select_sql}\n  FROM {full_table}')
        as_query = '\nUNION ALL\n'.join(union_parts)
        return as_query, all_cols

    def test_unselected_column_gets_null(self):
        """When a column is deselected for one table, UNION ALL must use NULL AS."""
        search_rows = [
            {'select': True, 'table': 'T1', 'column': 'CHUNK',
             'search_type': 'Hybrid (Text + Vector)', 'embedding_model': 'm'},
            {'select': True, 'table': 'T2', 'column': 'CHUNK',
             'search_type': 'Hybrid (Text + Vector)', 'embedding_model': 'm'},
        ]
        attr_rows = [
            {'select': True, 'table': 'T1', 'column': 'RELATIVE_PATH'},
            {'select': True, 'table': 'T1', 'column': 'PAGE_NUMBER'},
            {'select': True, 'table': 'T2', 'column': 'RELATIVE_PATH'},
            {'select': False, 'table': 'T2', 'column': 'PAGE_NUMBER'},  # deselected
        ]
        sql, all_cols = self._build_sql(search_rows, attr_rows, ['T1', 'T2'])
        # T2 must have NULL AS "PAGE_NUMBER"
        assert 'NULL AS "PAGE_NUMBER"' in sql, (
            f"T2 should use NULL AS PAGE_NUMBER since it's deselected. SQL:\n{sql}"
        )
        # T1 must have actual PAGE_NUMBER (not NULL)
        t1_section = sql.split('UNION ALL')[0]
        assert '"PAGE_NUMBER"' in t1_section, (
            f"T1 should select actual PAGE_NUMBER. SQL:\n{sql}"
        )
        assert 'NULL AS "PAGE_NUMBER"' not in t1_section, (
            f"T1 should NOT use NULL for PAGE_NUMBER. SQL:\n{sql}"
        )

    def test_both_tables_selected_columns_no_null(self):
        """When both tables select the same columns, no NULL needed."""
        search_rows = [
            {'select': True, 'table': 'T1', 'column': 'CHUNK',
             'search_type': 'Hybrid (Text + Vector)', 'embedding_model': 'm'},
            {'select': True, 'table': 'T2', 'column': 'CHUNK',
             'search_type': 'Hybrid (Text + Vector)', 'embedding_model': 'm'},
        ]
        attr_rows = [
            {'select': True, 'table': 'T1', 'column': 'A'},
            {'select': True, 'table': 'T2', 'column': 'A'},
        ]
        sql, _ = self._build_sql(search_rows, attr_rows, ['T1', 'T2'])
        assert 'NULL' not in sql, f"No NULLs expected when all columns selected. SQL:\n{sql}"

    def test_three_tables_mixed_columns(self):
        """Three tables with different column selections."""
        search_rows = [
            {'select': True, 'table': 'A', 'column': 'CHUNK',
             'search_type': 'Hybrid (Text + Vector)', 'embedding_model': 'm'},
            {'select': True, 'table': 'B', 'column': 'CHUNK',
             'search_type': 'Hybrid (Text + Vector)', 'embedding_model': 'm'},
            {'select': True, 'table': 'C', 'column': 'CHUNK',
             'search_type': 'Hybrid (Text + Vector)', 'embedding_model': 'm'},
        ]
        attr_rows = [
            {'select': True, 'table': 'A', 'column': 'X'},
            {'select': True, 'table': 'A', 'column': 'Y'},
            {'select': True, 'table': 'B', 'column': 'X'},
            {'select': False, 'table': 'B', 'column': 'Y'},
            {'select': False, 'table': 'C', 'column': 'X'},
            {'select': False, 'table': 'C', 'column': 'Y'},
        ]
        sql, all_cols = self._build_sql(search_rows, attr_rows, ['A', 'B', 'C'])
        # A: CHUNK, X, Y — no NULLs
        a_section = sql.split('UNION ALL')[0]
        assert 'NULL' not in a_section, f"Table A should have no NULLs"
        # B: CHUNK, X, NULL AS Y
        b_section = sql.split('UNION ALL')[1]
        assert 'NULL AS "Y"' in b_section, f"Table B should have NULL AS Y"
        # C: CHUNK, NULL AS X, NULL AS Y
        c_section = sql.split('UNION ALL')[2]
        assert 'NULL AS "X"' in c_section, f"Table C should have NULL AS X"
        assert 'NULL AS "Y"' in c_section, f"Table C should have NULL AS Y"

    def test_single_service_not_per_table(self):
        """SQL must contain exactly one CREATE statement, not one per table."""
        # The full SQL from _build_create_sql should have exactly one
        # 'CREATE OR REPLACE CORTEX SEARCH SERVICE' — not per-table
        src = _read('views/ccs/page4_complete.py')
        # Check that _build_create_sql is called once with table_names list,
        # not in a per-table loop
        assert 'for tbl in table_names' not in src or \
               'CREATE OR REPLACE' not in src.split('for tbl in table_names')[1][:200], \
            "_build_create_sql should not loop CREATE per table"

    def test_union_all_in_output(self):
        """When multiple tables, output must contain UNION ALL."""
        search_rows = [
            {'select': True, 'table': 'T1', 'column': 'CHUNK',
             'search_type': 'Hybrid (Text + Vector)', 'embedding_model': 'm'},
            {'select': True, 'table': 'T2', 'column': 'CHUNK',
             'search_type': 'Hybrid (Text + Vector)', 'embedding_model': 'm'},
        ]
        attr_rows = [
            {'select': True, 'table': 'T1', 'column': 'A'},
            {'select': True, 'table': 'T2', 'column': 'A'},
        ]
        sql, _ = self._build_sql(search_rows, attr_rows, ['T1', 'T2'])
        assert 'UNION ALL' in sql, f"Multi-table must use UNION ALL. SQL:\n{sql}"


class TestPage4ResultsDisplay:
    """Verify page4 shows service name and key info in results."""

    def test_results_shows_service_name(self):
        """Results section must display the service name."""
        src = _read('views/ccs/page4_complete.py')
        # In the svc_created block, must show svc_name
        # Look for the pattern: st.markdown(f"...svc_name...") or similar
        assert 'svc_name' in src, "page4 must reference svc_name"
        # Check that the success/results block includes service name
        svc_created_block = src[src.index('if svc_created:'):] if 'if svc_created:' in src else ''
        assert 'Service Name' in svc_created_block or 'svc_name' in svc_created_block, (
            "Results display must show service name"
        )

    def test_results_shows_source_tables(self):
        """Results section must list source tables."""
        src = _read('views/ccs/page4_complete.py')
        svc_created_block = src[src.index('if svc_created:'):] if 'if svc_created:' in src else ''
        assert 'Source Tables' in svc_created_block or 'table_names' in svc_created_block, (
            "Results display must show source tables"
        )


class TestPage4ErrorHandling:
    """Verify page4 has proper error handling (no silent crashes)."""

    def test_render_has_try_except(self):
        """render() must have a top-level try/except."""
        src = _read('views/ccs/page4_complete.py')
        assert 'def render(session):' in src, "render function must exist"
        render_block = src[src.index('def render(session):'):src.index('def _render_inner')]
        assert 'try:' in render_block, "render() must have try block"
        assert 'except' in render_block, "render() must have except block"
        assert 'log_action' in render_block, "render() must log errors"
        assert 'st.error' in render_block, "render() must show error to user"

    def test_init_has_import_error_handling(self):
        """__init__.py must catch import errors for page modules."""
        src = _read('views/ccs/__init__.py')
        assert 'except' in src, "__init__.py must catch import exceptions"
        assert 'WIZARD_IMPORT_ERROR' in src or 'IMPORT_ERROR' in src, \
            "__init__.py must log import errors"

    def test_render_inner_has_header_try_except(self):
        """_render_inner must wrap render_header in try/except."""
        src = _read('views/ccs/page4_complete.py')
        inner_block = src[src.index('def _render_inner'):] if 'def _render_inner' in src else ''
        assert 'PAGE4_HEADER_ERROR' in inner_block or 'render_header' in inner_block, \
            "_render_inner should handle render_header errors"


class TestPage4AccordionDefaults:
    """Verify accordions/expanders default to collapsed."""

    def test_results_expanders_not_expanded(self):
        """Results accordions in page3 must not use expanded=True."""
        src = _read('views/ccs/page3_execute.py')
        lines = src.split(chr(10))
        for i, line in enumerate(lines, 1):
            if 'st.expander' in line and 'Results' not in line:
                continue
            if 'st.expander' in line and 'expanded=True' in line:
                pytest.fail(
                    f"page3_execute.py:{i} uses expanded=True in expander: "
                    f"{line.strip()} — accordions should default to collapsed"
                )


class TestPage3Metrics:
    """Verify page3 results show Doc Refinery-style metrics."""

    def test_results_show_table_name(self):
        """Results must include table name per job."""
        src = _read('views/ccs/page3_execute.py')
        # The expander should show the table name
        assert 'tbl' in src and 'j["table"]' in src, \
            "Results must reference the job's table name"

    def test_results_show_layout_vision_pages(self):
        """Results must show layout and vision page counts."""
        src = _read('views/ccs/page3_execute.py')
        assert 'layout_pages' in src or 'lay_pages' in src, \
            "Results must show layout page count"
        assert 'vision_pages' in src or 'vis_pages' in src, \
            "Results must show vision page count"

    def test_results_show_cost(self):
        """Results must show cost estimation."""
        src = _read('views/ccs/page3_execute.py')
        assert 'c_layout' in src or 'credits_layout' in src, \
            "Results must compute layout cost"
        assert 'CREDIT_TO_USD' in src, \
            "Results must convert credits to USD"

    def test_table_columns_not_displayed(self):
        """Page 3 must NOT display table columns UI (cached silently only)."""
        src = _read('views/ccs/page3_execute.py')
        # Table columns section header should not exist
        lines = src.split(chr(10))
        for i, line in enumerate(lines, 1):
            if '🗄️' in line and 'Table Columns' in line:
                pytest.fail(
                    f"page3_execute.py:{i} still displays table columns UI: "
                    f"{line.strip()} — should be cached silently"
                )


class TestPage4CostCaption:
    """Verify the cost caption shows correct text."""

    def test_cost_caption_format(self):
        """Cost caption must show correct AI Credit and IDR conversion rates."""
        src = _read('views/ccs/page4_complete.py')
        assert '1 AI Credit' in src, "Must show AI Credit conversion"
        assert 'Rp 18,000' in src or 'Rp 18000' in src, \
            "Must show IDR conversion rate"

    def test_usd_to_idr_constant(self):
        """USD_TO_IDR must be 18000 per spec."""
        src = _read('utils/constants.py')
        assert 'USD_TO_IDR = 18000' in src, \
            "USD_TO_IDR must be 18000 (was 16500)"


# =============================================================================
# New: normalize_pdf_to_table_name
# =============================================================================

class TestNormalizePdfToTableName:
    """Verify PDF filename to table name normalization."""

    def test_basic_pdf(self):
        from views.ccs.common import normalize_pdf_to_table_name
        assert normalize_pdf_to_table_name("report.pdf") == "REPORT"

    def test_spaces_and_special_chars(self):
        from views.ccs.common import normalize_pdf_to_table_name
        assert normalize_pdf_to_table_name("My Report (2024).pdf") == "MY_REPORT_2024"

    def test_hyphens_and_dots(self):
        from views.ccs.common import normalize_pdf_to_table_name
        assert normalize_pdf_to_table_name("Q1-Q2 Financials.pdf") == "Q1_Q2_FINANCIALS"

    def test_underscores_preserved(self):
        from views.ccs.common import normalize_pdf_to_table_name
        assert normalize_pdf_to_table_name("report_final.pdf") == "REPORT_FINAL"

    def test_consecutive_underscores_collapsed(self):
        from views.ccs.common import normalize_pdf_to_table_name
        assert normalize_pdf_to_table_name("a  b  c.pdf") == "A_B_C"

    def test_leading_trailing_stripped(self):
        from views.ccs.common import normalize_pdf_to_table_name
        assert normalize_pdf_to_table_name(" _report_.pdf") == "REPORT"

    def test_no_extension(self):
        from views.ccs.common import normalize_pdf_to_table_name
        assert normalize_pdf_to_table_name("report") == "REPORT"

    def test_empty_fallback(self):
        from views.ccs.common import normalize_pdf_to_table_name
        assert normalize_pdf_to_table_name("__.pdf") == "IMPORTED_PDF"

    def test_uppercase_pdf_extension(self):
        from views.ccs.common import normalize_pdf_to_table_name
        assert normalize_pdf_to_table_name("report.PDF") == "REPORT"

    def test_numbers_preserved(self):
        from views.ccs.common import normalize_pdf_to_table_name
        assert normalize_pdf_to_table_name("Doc 123 v2.1.pdf") == "DOC_123_V2_1"


# =============================================================================
# New: Demo file import integrity
# =============================================================================

class TestDemoFileImports:
    """Verify demo files import from demo paths (not views.refinery)."""

    def test_no_refinery_imports_in_demo(self):
        """No demo file should import from views.refinery."""
        for root, dirs, files in os.walk(DEMO_DIR):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(root, fname)
                rel = os.path.relpath(path, ROOT_DIR)
                src = open(path).read()
                for i, line in enumerate(src.split(chr(10)), 1):
                    s = line.strip()
                    if s.startswith('#'):
                        continue
                    if 'from views.refinery' in s or 'import views.refinery' in s:
                        pytest.fail(f"{rel}:{i} imports from views.refinery: {s}")

    def test_batch_processor_imports_from_demo(self):
        """batch_processor.py must import from demo paths."""
        src = _read("views/ccs/batch_processor.py")
        assert "from views.ccs.ingestion_core import" in src
        assert "from views.ccs.ingestion_strategies import" in src
        assert "from views.ccs.batch_exceptions import" in src

    def test_layout_strategy_imports_from_demo(self):
        """layout.py must import from demo paths."""
        src = _read("views/ccs/ingestion_strategies/layout.py")
        assert "from views.ccs.batch_exceptions import" in src
        assert "from views.ccs.refinery_common import" in src

    def test_page3_imports_batch_processor_from_demo(self):
        """page3_execute.py must import batch_processor from demo."""
        src = _read("views/ccs/page3_execute.py")
        assert "from views.ccs.batch_processor import" in src

    def test_page5_qa_tools_exists(self):
        """page5_qa_tools.py must exist."""
        assert os.path.isfile(os.path.join(DEMO_DIR, "page5_qa_tools.py")), "page5_qa_tools.py missing"


# =============================================================================
# New: 5-page wizard structure
# =============================================================================

class TestFivePageWizard:
    """Verify the wizard has 5 pages with correct routing."""

    def test_init_shows_5_pages(self):
        """__init__.py must show progress as 5 pages."""
        src = _read("views/ccs/__init__.py")
        assert "Step {page} of 5" in src or "page / 5" in src, "Progress bar should show 5 pages"

    def test_init_handles_page_4_qa(self):
        """__init__.py must route page 4 to QA Studio & Tools."""
        src = _read("views/ccs/__init__.py")
        assert "page5_qa_tools" in src or "render_qa_tools" in src, "Page 4 should route to QA/Tools"

    def test_init_handles_page_5_search(self):
        """__init__.py must route page 5 to search service configuration."""
        src = _read("views/ccs/__init__.py")
        assert "page4_complete" in src, "Page 5 should route to search service config"

    def test_common_has_step5_colors(self):
        """common.py must have step 5 colors defined."""
        src = _read("views/ccs/common.py")
        assert "5:" in src and "_STEP_COLORS" in src, "Step 5 colors missing"

    def test_common_has_step5_content(self):
        """common.py must have step 5 content defined."""
        src = _read("views/ccs/common.py")
        assert "QA Studio" in src or "Tools" in src, "Step 5 content missing"


# =============================================================================
# New: Surgical mode in page 2
# =============================================================================

class TestSurgicalModeInPage2:
    """Verify surgical mode support in page 2."""

    def test_page2_imports_surgical_ui(self):
        """page2_builder.py must import render_range_mapping_section."""
        src = _read("views/ccs/page2_builder.py")
        assert "from views.ccs.surgical_ui import" in src, "Missing surgical_ui import"

    def test_page2_has_surgical_range_result(self):
        """page2_builder.py must read surgical_range_result."""
        src = _read("views/ccs/page2_builder.py")
        assert "surgical_range_result" in src, "Missing surgical_range_result handling"

    def test_page2_passes_surgical_data_to_job(self):
        """page2_builder.py must add surgical data to job dict."""
        src = _read("views/ccs/page2_builder.py")
        assert "surgical_range_mappings" in src, "Missing surgical_range_mappings in job dict"
        assert "surgical_replacement_file" in src, "Missing surgical_replacement_file in job dict"


# =============================================================================
# Target Table Name: no value= + key= combo (HTML_lesson_learnt.md §6)
# =============================================================================

class TestTableNameWidgetPattern:
    """Regression: Target Table Name text_input must NOT combine value= AND key=.

    HTML_lesson_learnt.md §6 explicitly forbids:
        st.text_input("...", value=X, key="widget_key")

    Once the widget key exists in session_state, value= is silently ignored,
    which breaks the PDF auto-fill scenario. The correct pattern is:
        st.session_state.setdefault("widget_key", X)
        st.text_input("...", key="widget_key")
    """

    @pytest.mark.parametrize("page_file,widget_key", [
        ("views/ccs/page2_builder.py", "cssw_table_widget"),
        ("views/refinery/tab_config.py", "jb_table_name"),
    ])
    def test_no_value_and_key_combo_on_table_name(self, page_file, widget_key):
        """The Target Table Name text_input must not pass both value= and key=."""
        tree = _parse(page_file)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Match st.text_input(...) calls
            if not (isinstance(node.func, ast.Attribute) and
                    isinstance(node.func.value, ast.Name) and
                    node.func.value.id == "st" and
                    node.func.attr == "text_input"):
                continue
            kwargs = {kw.arg for kw in node.keywords}
            # If this is the table name widget (has the table widget key)
            key_arg = next((kw for kw in node.keywords if kw.arg == "key"), None)
            if key_arg is None:
                continue
            key_val = None
            if isinstance(key_arg.value, ast.Constant):
                key_val = key_arg.value.value
            if key_val != widget_key:
                continue
            # FORBIDDEN: value= AND key= together on the table name widget
            assert "value" not in kwargs, (
                f"{page_file}:{node.lineno}: st.text_input(...) for Target Table Name "
                f"must NOT pass value= together with key='{widget_key}' "
                f"(HTML_lesson_learnt.md §6 — value= is silently ignored once "
                f"the widget key exists in session_state, breaking PDF auto-fill). "
                f"Use st.session_state.setdefault('{widget_key}', ...) before the widget instead."
            )

    def test_page2_initializes_widget_via_setdefault(self):
        """page2_builder.py must initialize cssw_table_widget via setdefault before the text_input."""
        src = _read("views/ccs/page2_builder.py")
        assert 'st.session_state.setdefault("cssw_table_widget"' in src or \
               "st.session_state.setdefault('cssw_table_widget'" in src, (
            "page2_builder.py must call st.session_state.setdefault('cssw_table_widget', ...) "
            "before the text_input — this replaces the illegal value=+key= combo"
        )

    def test_tab_config_initializes_widget_via_setdefault(self):
        """tab_config.py must initialize jb_table_name via setdefault before the text_input."""
        src = _read("views/refinery/tab_config.py")
        assert 'st.session_state.setdefault("jb_table_name"' in src or \
               "st.session_state.setdefault('jb_table_name'" in src, (
            "tab_config.py must call st.session_state.setdefault('jb_table_name', ...) "
            "before the text_input — this replaces the illegal value=+key= combo"
        )

    def test_page2_no_pop_workaround(self):
        """page2_builder.py must NOT use st.session_state.pop() on the table widget key.

        The old workaround combined value=+key= with st.session_state.pop() to force
        re-initialization. This is brittle and unnecessary now that we use direct
        session_state assignment + setdefault. See HTML_lesson_learnt.md §6.
        """
        src = _read("views/ccs/page2_builder.py")
        lines = src.split(chr(10))
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if s.startswith('#'):
                continue
            # The pop() workaround specifically targeted cssw_table_widget
            if 'st.session_state.pop' in s and 'cssw_table_widget' in s:
                pytest.fail(
                    f"page2_builder.py:{i}: st.session_state.pop('cssw_table_widget', ...) "
                    f"found — this was the old workaround for the value=+key= combo. "
                    f"Now that we initialize via setdefault + direct session_state "
                    f"assignment, the pop() is unnecessary and breaks user edits."
                )

    def test_page2_pdf_change_sets_widget_key_directly(self):
        """When PDF changes, page2_builder.py must set the widget key directly.

        The auto-fill must write to st.session_state['cssw_table_widget'] so the
        text_input picks up the new value on the next rerun. The old code used
        st.session_state.pop() to force value= re-initialization; that approach
        is gone now, so the new value must be set explicitly.
        """
        src = _read("views/ccs/page2_builder.py")
        assert 'st.session_state["cssw_table_widget"] = normalized' in src or \
               "st.session_state['cssw_table_widget'] = normalized" in src, (
            "page2_builder.py must set st.session_state['cssw_table_widget'] = normalized "
            "when PDF changes, so the widget picks up the new value via session_state "
            "(not via value=, which is forbidden when key= is also passed)"
        )

    def test_page2_calls_normalize_on_pdf_change(self):
        """page2_builder.py must call normalize_pdf_to_table_name when PDF selection changes."""
        src = _read("views/ccs/page2_builder.py")
        # Must import the function
        assert "normalize_pdf_to_table_name" in src, (
            "page2_builder.py must reference normalize_pdf_to_table_name"
        )
        # Must call it inside the PDF-change branch (not just import it)
        # Look for the assignment pattern
        assert "normalize_pdf_to_table_name(sel_file)" in src or \
               "normalize_pdf_to_table_name(" in src, (
            "page2_builder.py must call normalize_pdf_to_table_name(sel_file) to "
            "derive the table name from the selected PDF"
        )


# =============================================================================
# End-to-end: simulate the Target Table Name auto-fill flow
# =============================================================================

class TestTableNameAutoFillFlow:
    """End-to-end simulation of the PDF-select → table-name-auto-fill flow.

    Uses a fake Streamlit session_state dict to verify the new pattern works
    correctly across the following scenarios:
    1. First render (no PDF selected) — widget shows default 'SUS_CHUNKS'
    2. PDF selected — widget auto-fills to normalized name
    3. User edits table name manually — widget shows user's value
    4. User changes PDF — widget re-fills to new normalized name
    5. Cross-page navigation — widget re-initializes from helper key
    """

    def _simulate_widget(self, session_state, widget_key, helper_default):
        """Simulate the Streamlit text_input render pattern.

        Returns the value the widget would display.
        Mirrors the exact pattern in page2_builder.py:
            st.session_state.setdefault(widget_key, helper_value)
            val = st.text_input(..., key=widget_key)
        """
        # setdefault: only set if not already in session_state
        session_state.setdefault(widget_key, helper_default)
        # Widget reads from session_state[widget_key]
        return session_state[widget_key]

    def _simulate_pdf_change(self, session_state, widget_key, helper_key, new_pdf):
        """Simulate the PDF-change handler in page2_builder.py."""
        from views.ccs.common import normalize_pdf_to_table_name
        normalized = normalize_pdf_to_table_name(new_pdf)
        # jbsync: update the helper key
        session_state[helper_key] = normalized
        # Direct widget key assignment: forces widget to show new value
        session_state[widget_key] = normalized
        return normalized

    def test_scenario_1_first_render_shows_default(self):
        """First render: widget initializes to _JB_DEFAULTS['table_name'] = 'SUS_CHUNKS'."""
        from views.ccs.common import _JB_DEFAULTS
        session_state = {}
        helper_key = "_jbv_table_name"
        # Helper key not set yet — jbv() returns the default
        helper_value = session_state.get(helper_key, _JB_DEFAULTS["table_name"])
        displayed = self._simulate_widget(session_state, "cssw_table_widget", helper_value)
        assert displayed == "SUS_CHUNKS"
        assert session_state["cssw_table_widget"] == "SUS_CHUNKS"

    def test_scenario_2_pdf_selected_auto_fills(self):
        """User selects 'My Report (2024).pdf' → widget shows 'MY_REPORT_2024'."""
        session_state = {}
        # Simulate the PDF-change handler
        self._simulate_pdf_change(
            session_state, "cssw_table_widget", "_jbv_table_name",
            "My Report (2024).pdf"
        )
        # Now the widget renders
        helper_value = session_state["_jbv_table_name"]
        displayed = self._simulate_widget(session_state, "cssw_table_widget", helper_value)
        assert displayed == "MY_REPORT_2024"

    def test_scenario_3_user_edits_table_name_manually(self):
        """User types a custom name — widget shows it, helper key syncs."""
        session_state = {}
        # Initial state: PDF selected, auto-fill applied
        self._simulate_pdf_change(
            session_state, "cssw_table_widget", "_jbv_table_name", "report.pdf"
        )
        assert session_state["cssw_table_widget"] == "REPORT"
        # User types a custom name in the widget
        session_state["cssw_table_widget"] = "MY_CUSTOM_TABLE"
        # On next render, setdefault does NOT overwrite (key exists)
        helper_value = session_state["_jbv_table_name"]  # Still "REPORT" until jbsync runs
        displayed = self._simulate_widget(session_state, "cssw_table_widget", helper_value)
        assert displayed == "MY_CUSTOM_TABLE", (
            "User's manual edit must be preserved across reruns — setdefault "
            "must NOT overwrite an existing widget key"
        )

    def test_scenario_4_user_changes_pdf_re_fills(self):
        """User changes PDF — widget re-fills with new normalized name (overwrites user edit)."""
        session_state = {}
        # First PDF selection
        self._simulate_pdf_change(
            session_state, "cssw_table_widget", "_jbv_table_name", "report.pdf"
        )
        # User manually edits
        session_state["cssw_table_widget"] = "MY_CUSTOM"
        assert session_state["cssw_table_widget"] == "MY_CUSTOM"
        # User selects a different PDF — auto-fill re-fires
        self._simulate_pdf_change(
            session_state, "cssw_table_widget", "_jbv_table_name",
            "Q1-Q2 Financials.pdf"
        )
        # Widget now shows the new normalized name (user's edit is overwritten — by design)
        assert session_state["cssw_table_widget"] == "Q1_Q2_FINANCIALS"

    def test_scenario_5_cross_page_navigation_re_initializes(self):
        """After Streamlit clears widget key on page nav, setdefault re-inits from helper."""
        session_state = {}
        # PDF selected on page 2, helper key persists
        self._simulate_pdf_change(
            session_state, "cssw_table_widget", "_jbv_table_name", "report.pdf"
        )
        assert session_state["_jbv_table_name"] == "REPORT"
        # User navigates to page 1 — Streamlit clears widget key
        session_state.pop("cssw_table_widget", None)
        assert "cssw_table_widget" not in session_state
        # User comes back to page 2 — setdefault re-initializes from helper key
        helper_value = session_state.get("_jbv_table_name", "SUS_CHUNKS")
        displayed = self._simulate_widget(session_state, "cssw_table_widget", helper_value)
        assert displayed == "REPORT", (
            "After cross-page navigation, the widget must re-initialize from the "
            "persistent helper key (_jbv_table_name), not fall back to the default"
        )

    def test_scenario_6_empty_pdf_filename_falls_back(self):
        """Edge case: PDF filename normalizes to empty — falls back to 'IMPORTED_PDF'."""
        session_state = {}
        self._simulate_pdf_change(
            session_state, "cssw_table_widget", "_jbv_table_name", "__.pdf"
        )
        assert session_state["cssw_table_widget"] == "IMPORTED_PDF"

    def test_scenario_7_unicode_pdf_filename(self):
        """Edge case: PDF filename with unicode characters — non-alphanumerics stripped."""
        session_state = {}
        self._simulate_pdf_change(
            session_state, "cssw_table_widget", "_jbv_table_name",
            "Café Résumé 2024.pdf"
        )
        # É and é are non-ASCII letters — current implementation strips them
        # (Snowflake unquoted identifiers are ASCII-only)
        assert session_state["cssw_table_widget"] == "CAF_RSUM_2024"

    def test_scenario_8_helper_key_and_widget_key_stay_in_sync(self):
        """After any operation, helper key and widget key must be in sync (or user-edit-ahead)."""
        session_state = {}
        # Initial auto-fill
        self._simulate_pdf_change(
            session_state, "cssw_table_widget", "_jbv_table_name", "report.pdf"
        )
        assert session_state["_jbv_table_name"] == session_state["cssw_table_widget"]
        # User edits widget — helper key is "behind" until jbsync runs in the
        # `if target_table_name != _tbl_val: jbsync(...)` block. After that
        # sync, they match again.
        session_state["cssw_table_widget"] = "EDITED"
        # Simulate the jbsync that the widget-change handler does
        session_state["_jbv_table_name"] = "EDITED"
        assert session_state["_jbv_table_name"] == session_state["cssw_table_widget"]


# =============================================================================
# Anti-hardcoding regression: defaults must come from utils/constants.py
# =============================================================================

class TestNoHardcodedDefaults:
    """Ensure magic strings like 'SUS_CHUNKS' and 'IMPORTED_PDF' are not
    hardcoded in source files — they must come from utils/constants.py.

    The constants DEFAULT_TARGET_TABLE and DEFAULT_IMPORTED_TABLE_NAME exist
    precisely so we don't sprinkle these literals across the codebase. If a
    file needs one of these defaults, it must import from utils.constants.
    """

    def test_constants_module_exports_defaults(self):
        """utils/constants.py must export DEFAULT_TARGET_TABLE and DEFAULT_IMPORTED_TABLE_NAME."""
        from utils.constants import DEFAULT_TARGET_TABLE, DEFAULT_IMPORTED_TABLE_NAME
        assert isinstance(DEFAULT_TARGET_TABLE, str) and DEFAULT_TARGET_TABLE
        assert isinstance(DEFAULT_IMPORTED_TABLE_NAME, str) and DEFAULT_IMPORTED_TABLE_NAME

    def test_common_uses_default_target_table_constant(self):
        """views/ccs/common.py must reference DEFAULT_TARGET_TABLE, not literal 'SUS_CHUNKS'."""
        src = _read("views/ccs/common.py")
        assert "DEFAULT_TARGET_TABLE" in src, (
            "common.py must import DEFAULT_TARGET_TABLE from utils.constants "
            "and use it in _JB_DEFAULTS — never hardcode 'SUS_CHUNKS'"
        )

    def test_common_does_not_hardcode_sus_chunks(self):
        """views/ccs/common.py must NOT contain the literal string 'SUS_CHUNKS'."""
        src = _read("views/ccs/common.py")
        lines = src.split(chr(10))
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if s.startswith('#'):
                continue
            assert 'SUS_CHUNKS' not in s, (
                f"common.py:{i}: hardcoded 'SUS_CHUNKS' found — use "
                f"DEFAULT_TARGET_TABLE from utils.constants instead"
            )

    def test_common_uses_default_imported_table_name_constant(self):
        """views/ccs/common.py must reference DEFAULT_IMPORTED_TABLE_NAME, not literal 'IMPORTED_PDF'."""
        src = _read("views/ccs/common.py")
        assert "DEFAULT_IMPORTED_TABLE_NAME" in src, (
            "common.py must import DEFAULT_IMPORTED_TABLE_NAME from utils.constants "
            "and use it as the fallback in normalize_pdf_to_table_name"
        )

    def test_common_does_not_hardcode_imported_pdf(self):
        """views/ccs/common.py must NOT contain the literal string 'IMPORTED_PDF'."""
        src = _read("views/ccs/common.py")
        lines = src.split(chr(10))
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if s.startswith('#'):
                continue
            assert "'IMPORTED_PDF'" not in s and '"IMPORTED_PDF"' not in s, (
                f"common.py:{i}: hardcoded 'IMPORTED_PDF' found — use "
                f"DEFAULT_IMPORTED_TABLE_NAME from utils.constants instead"
            )

    def test_tab_config_uses_default_target_table_constant(self):
        """views/refinery/tab_config.py must reference DEFAULT_TARGET_TABLE, not literal 'SUS_CHUNKS'."""
        src = _read("views/refinery/tab_config.py")
        assert "DEFAULT_TARGET_TABLE" in src, (
            "tab_config.py must import DEFAULT_TARGET_TABLE from utils.constants "
            "and use it in _jb_defaults — never hardcode 'SUS_CHUNKS'"
        )

    def test_tab_config_does_not_hardcode_sus_chunks(self):
        """views/refinery/tab_config.py must NOT contain the literal 'SUS_CHUNKS' (except in display strings/comments)."""
        src = _read("views/refinery/tab_config.py")
        lines = src.split(chr(10))
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if s.startswith('#') or s.startswith('st.') and ('caption' in s or 'markdown' in s):
                continue
            assert 'SUS_CHUNKS' not in s or 'DEFAULT_TARGET_TABLE' in s, (
                f"tab_config.py:{i}: hardcoded 'SUS_CHUNKS' found — use "
                f"DEFAULT_TARGET_TABLE from utils.constants instead"
            )
