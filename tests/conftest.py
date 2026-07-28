"""
conftest.py — pytest configuration shared by all test modules.

Makes the procedure's `utils/` package importable under the alias
`chunky_utils` so test modules can do `from chunky_utils.init_table
import run` and have it work both locally (where the source lives in
`procedure/utils/`) and inside the Snowflake IMPORTS zip (where the
files are under a top-level `chunky_utils/` directory).

The on-disk directory is `procedure/utils/` (matching the user's
instruction), but we deliberately use `chunky_utils` as the import
name to avoid colliding with the top-level `utils/` package that
belongs to the Streamlit app.
"""
from __future__ import annotations
import sys
import importlib
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROC_DIR = REPO_ROOT / "procedure"
PROC_UTILS_DIR = PROC_DIR / "utils"


def _register_chunky_utils_alias() -> None:
    """Register `chunky_utils` as an alias for `procedure/utils/`."""
    if "chunky_utils" in sys.modules:
        return  # already registered (e.g. by a previous test session)

    # Load procedure/utils/__init__.py as the `chunky_utils` package.
    init_file = PROC_UTILS_DIR / "__init__.py"
    if not init_file.is_file():
        return

    spec = importlib.util.spec_from_file_location(
        "chunky_utils",
        init_file,
        submodule_search_locations=[str(PROC_UTILS_DIR)],
    )
    if spec and spec.loader:
        module = importlib.util.module_from_spec(spec)
        sys.modules["chunky_utils"] = module
        spec.loader.exec_module(module)


_register_chunky_utils_alias()
