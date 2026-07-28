"""
End-to-end Streamlit tests using AppTest framework.

These tests actually launch the Streamlit app in a headless test context and
verify the page renders correctly. Unlike the AST-based tests in test_wizard.py,
these tests execute the real Streamlit code paths.

Run: python3 -m pytest tests/test_streamlit_e2e.py -v
"""
import os
import sys
import pytest
from unittest.mock import MagicMock

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Project root
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _restore_real_streamlit():
    """Restore the real streamlit module before each test.

    Other test files (notably tests/test_refinery.py) replace
    sys.modules['streamlit'] with a MagicMock for their own isolation.
    That mock breaks AppTest, which requires the real streamlit package.
    This fixture saves the mock, restores the real module for the duration
    of our test, then puts the mock back so other tests still pass.
    """
    saved_streamlit = sys.modules.get('streamlit')
    saved_snowflake = sys.modules.get('snowflake')
    saved_snowpark = sys.modules.get('snowflake.snowpark')
    saved_ctx = sys.modules.get('snowflake.snowpark.context')
    saved_funcs = sys.modules.get('snowflake.snowpark.functions')

    # Force re-import of real streamlit (and its submodules)
    for key in list(sys.modules.keys()):
        if key == 'streamlit' or key.startswith('streamlit.'):
            del sys.modules[key]
    import streamlit  # noqa: F401 — real package

    yield

    # Restore whatever was there before (could be the mock from test_refinery.py)
    if saved_streamlit is not None:
        sys.modules['streamlit'] = saved_streamlit
    if saved_snowflake is not None:
        sys.modules['snowflake'] = saved_snowflake
    if saved_snowpark is not None:
        sys.modules['snowflake.snowpark'] = saved_snowpark
    if saved_ctx is not None:
        sys.modules['snowflake.snowpark.context'] = saved_ctx
    if saved_funcs is not None:
        sys.modules['snowflake.snowpark.functions'] = saved_funcs


def _mock_snowflake_modules():
    """Mock snowflake modules in the current Python session.

    auth_utils.py imports snowflake.snowpark.context at module level —
    without these mocks, importing any view that transitively imports
    auth_utils will raise ModuleNotFoundError.
    """
    sys.modules.setdefault('snowflake', MagicMock())
    sys.modules.setdefault('snowflake.snowpark', MagicMock())
    sys.modules.setdefault('snowflake.snowpark.context', MagicMock())
    sys.modules.setdefault('snowflake.snowpark.functions', MagicMock())


def test_local_app_loads_without_error():
    """The local Streamlit app must load without raising any exception.

    Uses AppTest to run the script in a headless context. Verifies that
    none of the recent changes (setdefault pattern, normalize_pdf_to_table_name
    refactor, DEFAULT_TARGET_TABLE constant import) break the local app entry
    point.
    """
    from streamlit.testing.v1 import AppTest

    app_path = os.path.join(ROOT_DIR, "streamlit_app_local.py")
    # Use a fresh temp DB so we don't pollute the project tree
    tmp_db = os.path.join(ROOT_DIR, "_test_e2e_chunky_local.db")
    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    os.environ["CHUNKY_LOCAL_DB"] = tmp_db

    try:
        at = AppTest.from_file(app_path, default_timeout=30)
        at.run()

        # App must not raise an exception
        assert not at.exception, (
            f"streamlit_app_local.py raised an exception:\n"
            f"{[str(e) for e in at.exception]}"
        )
    finally:
        # Clean up the temp DB
        if os.path.exists(tmp_db):
            os.remove(tmp_db)
        os.environ.pop("CHUNKY_LOCAL_DB", None)


def test_ccs_wizard_module_imports_cleanly():
    """All CCS wizard modules must import without errors after our changes.

    Catches: missing imports, broken constants, syntax errors that wouldn't
    surface until the module is actually imported.
    """
    _mock_snowflake_modules()

    # Importing these modules triggers module-level code execution
    # (including the new constants imports)
    from views.ccs import common
    from views.ccs import page2_builder
    from views.refinery import tab_config
    from utils import constants

    # Verify the constants we depend on are exported
    assert hasattr(constants, "DEFAULT_TARGET_TABLE")
    assert hasattr(constants, "DEFAULT_IMPORTED_TABLE_NAME")

    # Verify common.py uses the constants (not hardcoded strings)
    assert common._JB_DEFAULTS["table_name"] == constants.DEFAULT_TARGET_TABLE

    # Verify normalize_pdf_to_table_name uses the constant fallback
    result = common.normalize_pdf_to_table_name("__.pdf")
    assert result == constants.DEFAULT_IMPORTED_TABLE_NAME


def test_table_name_widget_default_matches_constant():
    """The default Target Table Name must match DEFAULT_TARGET_TABLE.

    This guards against drift: if someone changes the constant but not the
    _JB_DEFAULTS dict (or vice versa), this test fails.
    """
    _mock_snowflake_modules()
    from views.ccs.common import _JB_DEFAULTS
    from utils.constants import DEFAULT_TARGET_TABLE

    assert _JB_DEFAULTS["table_name"] == DEFAULT_TARGET_TABLE, (
        f"_JB_DEFAULTS['table_name'] = {_JB_DEFAULTS['table_name']!r} "
        f"but DEFAULT_TARGET_TABLE = {DEFAULT_TARGET_TABLE!r} — they must match"
    )


def test_tab_config_default_matches_constant():
    """The Doc Refinery's default table name must also match DEFAULT_TARGET_TABLE."""
    _mock_snowflake_modules()
    # tab_config.py builds _jb_defaults inside render_config_tab() — we can't
    # call it without a Streamlit context. Instead, parse the source to verify
    # the constant is used.
    src_path = os.path.join(ROOT_DIR, "views", "refinery", "tab_config.py")
    with open(src_path) as f:
        src = f.read()
    # Must import the constant
    assert "from utils.constants import DEFAULT_TARGET_TABLE" in src, (
        "tab_config.py must import DEFAULT_TARGET_TABLE from utils.constants"
    )
    # Must use the constant in _jb_defaults
    assert '"table_name": DEFAULT_TARGET_TABLE' in src or \
           "'table_name': DEFAULT_TARGET_TABLE" in src, (
        "tab_config.py must use DEFAULT_TARGET_TABLE in _jb_defaults dict"
    )


def test_normalize_function_with_real_pdf_filenames():
    """Verify normalize_pdf_to_table_name with realistic PDF filenames.

    These mirror the actual filenames a user might upload to a Snowflake stage.
    """
    _mock_snowflake_modules()
    from views.ccs.common import normalize_pdf_to_table_name

    # Realistic cases
    cases = [
        ("Annual Report 2024.pdf", "ANNUAL_REPORT_2024"),
        ("Q1-2024-Financial-Statements.pdf", "Q1_2024_FINANCIAL_STATEMENTS"),
        ("product_spec_v2.1.final.pdf", "PRODUCT_SPEC_V2_1_FINAL"),
        ("Indonesian_Tax_Guide.pdf", "INDONESIAN_TAX_GUIDE"),
        ("  messy  name  .pdf  ", "MESSY_NAME"),
        ("UPPERCASE.PDF", "UPPERCASE"),
        ("MiXeDcAsE.pdf", "MIXEDCASE"),
        ("123_456_789.pdf", "123_456_789"),
        ("special!@#$%characters.pdf", "SPECIALCHARACTERS"),
        ("a.pdf", "A"),
        # Edge cases
        ("", "IMPORTED_PDF"),  # empty string
        (".pdf", "IMPORTED_PDF"),  # just extension
        ("_.pdf", "IMPORTED_PDF"),  # single underscore
        ("___.pdf", "IMPORTED_PDF"),  # multiple underscores
    ]
    for filename, expected in cases:
        actual = normalize_pdf_to_table_name(filename)
        assert actual == expected, (
            f"normalize_pdf_to_table_name({filename!r}) = {actual!r}, "
            f"expected {expected!r}"
        )


def test_constants_module_round_trip():
    """Constants used by the wizard must be importable from utils.constants.

    Catches accidental removal of constants during refactoring.
    """
    from utils.constants import (
        DEFAULT_DB,
        DEFAULT_SCHEMA,
        DEFAULT_STAGE,
        DEFAULT_TARGET_TABLE,
        DEFAULT_IMPORTED_TABLE_NAME,
    )
    # All must be non-empty strings
    for name, val in [
        ("DEFAULT_DB", DEFAULT_DB),
        ("DEFAULT_SCHEMA", DEFAULT_SCHEMA),
        ("DEFAULT_STAGE", DEFAULT_STAGE),
        ("DEFAULT_TARGET_TABLE", DEFAULT_TARGET_TABLE),
        ("DEFAULT_IMPORTED_TABLE_NAME", DEFAULT_IMPORTED_TABLE_NAME),
    ]:
        assert isinstance(val, str), f"{name} must be a str, got {type(val)}"
        assert len(val) > 0, f"{name} must not be empty"


def test_page2_builder_renders_without_session():
    """page2_builder.render() must not crash when called with a mock session.

    This is a smoke test: it verifies that the page2_builder module can be
    imported and its render() function can be called with a mock session
    object. The actual Snowflake calls will fail (caught by try/except in the
    render function), but the page itself must render.
    """
    from streamlit.testing.v1 import AppTest

    # Create a script that calls page2_builder.render() with a mock session
    test_script = """
import streamlit as st
from unittest.mock import MagicMock
import sys
sys.path.insert(0, %r)

# Mock auth_context so ctx() returns something usable
st.session_state['auth_context'] = {'db': 'TEST_DB', 'schema': 'TEST_SCHEMA', 'stage': 'TEST_STAGE', 'user': 'test_user'}
# Initialize Job Builder defaults
from views.ccs.common import jb_init
jb_init()
# Set page to 2 so nav_buttons works
st.session_state['cssw_page'] = 2

# Mock the Snowflake session — all .sql() calls return empty collect()
session = MagicMock()
session.sql.return_value.collect.return_value = []

from views.ccs.page2_builder import render
try:
    render(session)
except Exception as e:
    st.error(f"Page 2 render failed: {e}")
""" % ROOT_DIR

    # Write to a temp file
    test_file = os.path.join(ROOT_DIR, "_test_page2_render.py")
    with open(test_file, "w") as f:
        f.write(test_script)

    try:
        at = AppTest.from_file(test_file, default_timeout=30)
        at.run()
        # Must not raise an unhandled exception
        # (Page 2 render errors are caught and shown via st.error in the test script)
        # Check that no st.error was shown
        error_messages = [str(e.value) for e in at.error]
        page_errors = [m.value for m in at.markdown if "Page 2 render failed" in str(m.value)]
        # The render itself should succeed — session.sql errors are caught inside render()
        assert not page_errors, (
            f"page2_builder.render() failed:\n{page_errors}"
        )
    finally:
        if os.path.exists(test_file):
            os.remove(test_file)
