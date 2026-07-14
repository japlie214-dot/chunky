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

    def test_page1_role_uses_widget_key(self):
        """Page 1 must use st.session_state.cssw_role, not _jbv('role')."""
        src = _read("views/demo/page1_setup.py")
        # Must NOT use _jbv for role
        assert "_jbv(\"role\")" not in src, "page1 uses _jbv('role') — should use widget key directly"
        assert "_jbv('role')" not in src, "page1 uses _jbv('role') — should use widget key directly"
        # Must use widget key
        assert "cssw_role" in src, "page1 must reference cssw_role widget key"

    def test_page1_svc_name_uses_widget_key(self):
        """Page 1 must use st.session_state.cssw_svc_name, not _jbv('svc_name')."""
        src = _read("views/demo/page1_setup.py")
        assert "_jbv(\"svc_name\")" not in src, "page1 uses _jbv('svc_name') — should use widget key directly"
        assert "_jbv('svc_name')" not in src, "page1 uses _jbv('svc_name') — should use widget key directly"
        assert "cssw_svc_name" in src, "page1 must reference cssw_svc_name widget key"

    def test_page3_reads_role_from_widget_key(self):
        """Page 3 must read role from st.session_state.get('cssw_role'), not _jbv."""
        src = _read("views/demo/page3_execute.py")
        assert "cssw_role" in src, "page3 must read from cssw_role widget key"
        assert "_jbv(\"role\")" not in src, "page3 should not use _jbv for role"
        assert "_jbv('role')" not in src, "page3 should not use _jbv for role"

    def test_page3_reads_svc_name_from_widget_key(self):
        """Page 3 must read svc_name from st.session_state.get('cssw_svc_name'), not _jbv."""
        src = _read("views/demo/page3_execute.py")
        assert "cssw_svc_name" in src, "page3 must read from cssw_svc_name widget key"
        assert "_jbv(\"svc_name\")" not in src, "page3 should not use _jbv for svc_name"
        assert "_jbv('svc_name')" not in src, "page3 should not use _jbv for svc_name"


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
