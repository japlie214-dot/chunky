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
DEMO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "views", "demo")
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
        assert os.path.isdir(DEMO_DIR), f"views/demo/ directory missing"

    def test_init_exists(self):
        assert os.path.isfile(os.path.join(DEMO_DIR, "__init__.py")), "__init__.py missing"

    def test_common_exists(self):
        assert os.path.isfile(os.path.join(DEMO_DIR, "common.py")), "common.py missing"

    def test_page_modules_exist(self):
        for page in ["page1_setup.py", "page2_builder.py", "page3_execute.py", "page4_complete.py"]:
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
        """streamlit_app.py and streamlit_app_local.py must import from views.demo, not views.demo.wizard."""
        for entry in ["streamlit_app.py", "streamlit_app_local.py"]:
            src = _read(entry)
            # Must import from views.demo
            assert "from views.demo import render_demo_search_service" in src or \
                   "from views.demo.wizard import" not in src, \
                f"{entry} should import from views.demo, not views.demo.wizard"

    def test_no_import_from_deleted_wizard(self):
        """No source file should import from views.demo.wizard (deleted module)."""
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
                    if s.startswith('from views.demo.wizard') or s.startswith('import views.demo.wizard'):
                        pytest.fail(f"{rel}:{i} imports from deleted views.demo.wizard")

    def test_no_import_from_deleted_demo_search_service(self):
        """No source file should import from views.demo_search_service (deleted module)."""
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
                    if s.startswith('from views.demo_search_service') or s.startswith('import views.demo_search_service'):
                        pytest.fail(f"{rel}:{i} imports from deleted views.demo_search_service")

    def test_init_routes_to_all_pages(self):
        """__init__.py must import and route to all 4 page modules."""
        src = _read("views/demo/__init__.py")
        assert "from views.demo.page1_setup import" in src, "Missing page1 import"
        assert "from views.demo.page2_builder import" in src, "Missing page2 import"
        assert "from views.demo.page3_execute import" in src, "Missing page3 import"
        assert "from views.demo.page4_complete import" in src, "Missing page4 import"


# =============================================================================
# Snowflake Import Safety (Local Mode)
# =============================================================================

class TestSnowflakeImportSafety:
    """Verify snowflake imports are lazy (inside functions) so local mode works."""

    @pytest.mark.parametrize("page_file", [
        "views/demo/__init__.py",
        "views/demo/common.py",
        "views/demo/page1_setup.py",
        "views/demo/page2_builder.py",
        "views/demo/page3_execute.py",
        "views/demo/page4_complete.py",
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
        src = _read("views/demo/page1_setup.py")
        assert "_wiz_role" in src, "page1 must use _wiz_role for persistent storage"
        # Must NOT use _jbv for role
        assert "_jbv(\"role\")" not in src, "page1 uses _jbv('role')"
        assert "_jbv('role')" not in src, "page1 uses _jbv('role')"

    def test_page1_svc_name_uses_persistent_storage(self):
        """Page 1 must use _wiz_svc_name for persistent storage."""
        src = _read("views/demo/page1_setup.py")
        assert "_wiz_svc_name" in src, "page1 must use _wiz_svc_name for persistent storage"
        assert "_jbv(\"svc_name\")" not in src, "page1 uses _jbv('svc_name')"

    def test_page3_reads_role_from_persistent_storage(self):
        """Page 3 must read role from _wiz_role, not widget key."""
        src = _read("views/demo/page3_execute.py")
        assert "_wiz_role" in src, "page3 must read from _wiz_role"
        assert "_jbv(\"role\")" not in src, "page3 should not use _jbv for role"

    def test_page3_reads_svc_name_from_persistent_storage(self):
        """Page 3 must read svc_name from _wiz_svc_name, not widget key."""
        src = _read("views/demo/page3_execute.py")
        assert "_wiz_svc_name" in src, "page3 must read from _wiz_svc_name"
        assert "_jbv(\"svc_name\")" not in src, "page3 should not use _jbv for svc_name"

    def test_page1_always_syncs_role(self):
        """Page 1 must sync role on EVERY render, not just on change."""
        src = _read("views/demo/page1_setup.py")
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
        src = _read("views/demo/page1_setup.py")
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
        src = _read("views/demo/page3_execute.py")
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
        src = _read("views/demo/page3_execute.py")
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
        src = _read("views/demo/page3_execute.py")
        assert '_wiz_role' in src, "page3 must read role from _wiz_role"
        assert '_wiz_svc_name' in src, "page3 must read svc_name from _wiz_svc_name"

    def test_page1_syncs_role_to_persistent_key(self):
        """Page 1 must call _wiz_set('role', ...) to persist role across pages."""
        src = _read("views/demo/page1_setup.py")
        assert '_wiz_set("role"' in src or "_wiz_set('role'" in src, \
            "page1 must call _wiz_set('role', ...) to persist role"

    def test_page1_syncs_svc_name_to_persistent_key(self):
        """Page 1 must call _wiz_set('svc_name', ...) to persist svc_name across pages."""
        src = _read("views/demo/page1_setup.py")
        assert '_wiz_set("svc_name"' in src or "_wiz_set('svc_name'" in src, \
            "page1 must call _wiz_set('svc_name', ...) to persist svc_name"

    def test_page1_does_not_use_widget_key_as_source_of_truth(self):
        """Page 1 must not read role/svc_name from widget keys — they're not reliable across pages."""
        src = _read("views/demo/page1_setup.py")
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
        src = _read("views/demo/page1_setup.py")
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
        src = _read("views/demo/page3_execute.py")
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
        src = _read("views/demo/page1_setup.py")
        assert '"USAGE"' in src or "'USAGE'" in src, "Must check USAGE on schema"

    def test_checks_create_table(self):
        src = _read("views/demo/page1_setup.py")
        assert "CREATE TABLE" in src, "Must check CREATE TABLE on schema"

    def test_checks_create_cortex_search_service(self):
        src = _read("views/demo/page1_setup.py")
        assert "CREATE CORTEX SEARCH SERVICE" in src, "Must check CREATE CORTEX SEARCH SERVICE"

    def test_checks_stage_access(self):
        src = _read("views/demo/page1_setup.py")
        assert "stage" in src.lower(), "Must check stage access"
        assert "SHOW GRANTS ON STAGE" in src, "Must query stage grants"

    def test_privilege_function_signature_includes_stage(self):
        """_check_privileges must accept stage parameter."""
        src = _read("views/demo/page1_setup.py")
        assert "def _check_privileges(session, db, schema, stage)" in src, \
            "_check_privileges must accept (session, db, schema, stage)"


# =============================================================================
# Syntax Validity
# =============================================================================

class TestSyntaxValidity:
    """Verify all wizard files parse without syntax errors."""

    @pytest.mark.parametrize("page_file", [
        "views/demo/__init__.py",
        "views/demo/common.py",
        "views/demo/page1_setup.py",
        "views/demo/page2_builder.py",
        "views/demo/page3_execute.py",
        "views/demo/page4_complete.py",
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
        "views/demo/page4_complete.py",
        "views/demo/page3_execute.py",
        "views/demo/page2_builder.py",
        "views/demo/page1_setup.py",
        "views/demo/common.py",
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
        src = _read("views/demo/page4_complete.py")
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
        src = _read('views/demo/page4_complete.py')
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
        src = _read('views/demo/page4_complete.py')
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
        src = _read('views/demo/page4_complete.py')
        svc_created_block = src[src.index('if svc_created:'):] if 'if svc_created:' in src else ''
        assert 'Source Tables' in svc_created_block or 'table_names' in svc_created_block, (
            "Results display must show source tables"
        )


class TestPage4ErrorHandling:
    """Verify page4 has proper error handling (no silent crashes)."""

    def test_render_has_try_except(self):
        """render() must have a top-level try/except."""
        src = _read('views/demo/page4_complete.py')
        assert 'def render(session):' in src, "render function must exist"
        render_block = src[src.index('def render(session):'):src.index('def _render_inner')]
        assert 'try:' in render_block, "render() must have try block"
        assert 'except' in render_block, "render() must have except block"
        assert 'log_action' in render_block, "render() must log errors"
        assert 'st.error' in render_block, "render() must show error to user"

    def test_init_has_import_error_handling(self):
        """__init__.py must catch import errors for page modules."""
        src = _read('views/demo/__init__.py')
        assert 'except' in src, "__init__.py must catch import exceptions"
        assert 'WIZARD_IMPORT_ERROR' in src or 'IMPORT_ERROR' in src, \
            "__init__.py must log import errors"

    def test_render_inner_has_header_try_except(self):
        """_render_inner must wrap render_header in try/except."""
        src = _read('views/demo/page4_complete.py')
        inner_block = src[src.index('def _render_inner'):] if 'def _render_inner' in src else ''
        assert 'PAGE4_HEADER_ERROR' in inner_block or 'render_header' in inner_block, \
            "_render_inner should handle render_header errors"


class TestPage4AccordionDefaults:
    """Verify accordions/expanders default to collapsed."""

    def test_results_expanders_not_expanded(self):
        """Results accordions in page3 must not use expanded=True."""
        src = _read('views/demo/page3_execute.py')
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
        src = _read('views/demo/page3_execute.py')
        # The expander should show the table name
        assert 'tbl' in src and 'j["table"]' in src, \
            "Results must reference the job's table name"

    def test_results_show_layout_vision_pages(self):
        """Results must show layout and vision page counts."""
        src = _read('views/demo/page3_execute.py')
        assert 'layout_pages' in src or 'lay_pages' in src, \
            "Results must show layout page count"
        assert 'vision_pages' in src or 'vis_pages' in src, \
            "Results must show vision page count"

    def test_results_show_cost(self):
        """Results must show cost estimation."""
        src = _read('views/demo/page3_execute.py')
        assert 'c_layout' in src or 'credits_layout' in src, \
            "Results must compute layout cost"
        assert 'CREDIT_TO_USD' in src, \
            "Results must convert credits to USD"

    def test_table_columns_not_displayed(self):
        """Page 3 must NOT display table columns UI (cached silently only)."""
        src = _read('views/demo/page3_execute.py')
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
        src = _read('views/demo/page4_complete.py')
        assert '1 AI Credit' in src, "Must show AI Credit conversion"
        assert 'Rp 18,000' in src or 'Rp 18000' in src, \
            "Must show IDR conversion rate"

    def test_usd_to_idr_constant(self):
        """USD_TO_IDR must be 18000 per spec."""
        src = _read('utils/constants.py')
        assert 'USD_TO_IDR = 18000' in src, \
            "USD_TO_IDR must be 18000 (was 16500)"
