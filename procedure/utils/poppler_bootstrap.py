"""
procedure/utils/poppler_bootstrap.py
Single source of truth for resolving poppler binaries bundled into
utils_bundle.zip.

When Snowflake extracts utils_bundle.zip to /home/udf/<id>/, the layout is:

    /home/udf/<id>/
    ├── chunky_utils/                ← this package (Python handlers)
    ├── poppler_bundle/
    │   └── poppler/
    │       ├── bin/                 ← pdftoppm, pdfinfo, pdftotext
    │       └── lib/                 ← libc.so.6, libgcc_s.so.1, ...
    └── pdf2image/                   ← Python package

So the poppler path must be resolved ONE level up from `chunky_utils/`,
not relative to `__file__` inside `chunky_utils/`. This module centralises
that resolution so every handler uses the same logic.
"""
from __future__ import annotations
import os
import sys
from typing import Optional


def _udf_root() -> str:
    """
    Return the directory one level above the chunky_utils package.

    When deployed to Snowflake:   /home/udf/<id>/
    When running locally:         /home/z/my-project/repo/chunky/procedure/
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def poppler_bin_dir() -> str:
    return os.path.join(_udf_root(), "poppler_bundle", "poppler", "bin")


def poppler_lib_dir() -> str:
    return os.path.join(_udf_root(), "poppler_bundle", "poppler", "lib")


def pdf2image_parent_dir() -> str:
    """
    Directory whose child `pdf2image/` is importable.
    Add this to sys.path to enable `from pdf2image import convert_from_bytes`.
    """
    return _udf_root()


def bootstrap() -> Optional[str]:
    """
    Idempotently configure the environment so pdf2image + poppler work.

    - Adds poppler_lib_dir to LD_LIBRARY_PATH (so the bundled libc etc. are found).
    - Adds poppler_bin_dir to PATH (so pdf2image can find pdftoppm).
    - Adds the udf root to sys.path (so `from pdf2image import ...` works).

    Returns the poppler bin directory (for explicit poppler_path= kwargs),
    or None if poppler isn't bundled in this deployment.
    """
    bin_dir = poppler_bin_dir()
    lib_dir = poppler_lib_dir()
    root = pdf2image_parent_dir()

    # LD_LIBRARY_PATH
    if os.path.isdir(lib_dir):
        existing = os.environ.get("LD_LIBRARY_PATH", "")
        if lib_dir not in existing:
            os.environ["LD_LIBRARY_PATH"] = (
                lib_dir + (":" + existing if existing else "")
            )

    # PATH
    if os.path.isdir(bin_dir):
        existing_path = os.environ.get("PATH", "")
        if bin_dir not in existing_path:
            os.environ["PATH"] = bin_dir + ":" + existing_path

    # sys.path for pdf2image
    if root not in sys.path:
        sys.path.insert(0, root)

    if os.path.isdir(bin_dir):
        return bin_dir
    return None


# Resolve once at import time so the env is configured before any
# `from pdf2image import ...` happens in a downstream module.
POPPLER_BIN: Optional[str] = bootstrap()
