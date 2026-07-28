"""
procedure/utils/poppler_bootstrap.py
Single source of truth for resolving poppler binaries bundled into
utils_bundle.zip.

Snowflake warehouses with `resource_constraint = None` may be scheduled
on either x86_64 OR ARM64 compute nodes — we cannot predict which at
deploy time. The bundle therefore ships poppler binaries for BOTH
architectures, and this module picks the right one at runtime by
inspecting `platform.machine()`.

When Snowflake extracts utils_bundle.zip to /home/udf/<id>/, the layout is:

    /home/udf/<id>/
    ├── chunky_utils/                ← this package (Python handlers)
    ├── poppler_bundle/
    │   ├── arm64/
    │   │   └── poppler/
    │   │       ├── bin/             ← pdftoppm, pdfinfo, pdftotext (ARM64 ELF)
    │   │       └── lib/             ← libc.so.6, libgcc_s.so.1, ... (ARM64)
    │   └── x86_64/
    │       └── poppler/
    │           ├── bin/             ← pdftoppm, pdfinfo, pdftotext (x86_64 ELF)
    │           └── lib/             ← libc.so.6, libgcc_s.so.1, ... (x86_64)
    └── pdf2image/                   ← Python package (pure Python — arch-agnostic)

So the poppler path is resolved ONE level up from `chunky_utils/`,
then under the runtime-detected arch subdirectory.
"""
from __future__ import annotations
import os
import platform
import sys
from typing import Optional


def _udf_root() -> str:
    """
    Return the directory one level above the chunky_utils package.

    When deployed to Snowflake:   /home/udf/<id>/
    When running locally:         /home/z/my-project/repo/chunky/procedure/
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def detect_arch() -> str:
    """
    Detect the runtime CPU architecture and return the bundle subdirectory
    name (`arm64` or `x86_64`).

    Snowflake ARM warehouses report `aarch64` (Linux kernel convention).
    Snowflake x86 warehouses report `x86_64`. macOS reports `arm64`
    (Apple Silicon) or `x86_64` (Intel) — normalised here.
    """
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    # Unknown arch — fall back to x86_64 (more common in Snowflake today)
    # but the caller should treat this as a soft signal, not a guarantee.
    return "x86_64"


def poppler_bin_dir(arch: Optional[str] = None) -> str:
    """Return the poppler bin directory for the given arch (or runtime-detected)."""
    a = arch or detect_arch()
    return os.path.join(_udf_root(), "poppler_bundle", a, "poppler", "bin")


def poppler_lib_dir(arch: Optional[str] = None) -> str:
    """Return the poppler lib directory for the given arch (or runtime-detected)."""
    a = arch or detect_arch()
    return os.path.join(_udf_root(), "poppler_bundle", a, "poppler", "lib")


def pdf2image_parent_dir() -> str:
    """
    Directory whose child `pdf2image/` is importable.
    Add this to sys.path to enable `from pdf2image import convert_from_bytes`.
    """
    return _udf_root()


def _verify_poppler_binaries(bin_dir: str) -> bool:
    """Return True if at least one poppler binary exists in `bin_dir`."""
    if not os.path.isdir(bin_dir):
        return False
    for name in ("pdftoppm", "pdfinfo", "pdftotext"):
        if os.path.isfile(os.path.join(bin_dir, name)):
            return True
    return False


def bootstrap() -> dict:
    """
    Idempotently configure the environment so pdf2image + poppler work.

    - Detects the runtime architecture (aarch64 → arm64, x86_64 → x86_64).
    - Adds poppler_lib_dir to LD_LIBRARY_PATH (so the bundled libc etc. are found).
    - Adds poppler_bin_dir to PATH (so pdf2image can find pdftoppm).
    - Adds the udf root to sys.path (so `from pdf2image import ...` works).

    Returns a dict with:
        arch: the detected architecture ('arm64' or 'x86_64')
        bin_dir: the poppler bin directory (or None if not bundled for this arch)
        lib_dir: the poppler lib directory (or None if not bundled for this arch)
        available: True if poppler binaries are bundled for this arch
    """
    arch = detect_arch()
    bin_dir = poppler_bin_dir(arch)
    lib_dir = poppler_lib_dir(arch)
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

    available = _verify_poppler_binaries(bin_dir)
    return {
        "arch": arch,
        "bin_dir": bin_dir if available else None,
        "lib_dir": lib_dir if os.path.isdir(lib_dir) else None,
        "available": available,
    }


# Resolve once at import time so the env is configured before any
# `from pdf2image import ...` happens in a downstream module.
_BOOTSTRAP_RESULT = bootstrap()
POPPLER_BIN: Optional[str] = _BOOTSTRAP_RESULT["bin_dir"]
POPPLER_ARCH: str = _BOOTSTRAP_RESULT["arch"]
POPPLER_AVAILABLE: bool = _BOOTSTRAP_RESULT["available"]


def get_poppler_bin_or_raise() -> str:
    """
    Return the poppler bin directory, or raise a descriptive RuntimeError
    if poppler is not bundled for the runtime architecture.

    Use this in handlers that absolutely need poppler (Vision extraction,
    page rendering) — the error message tells the caller exactly what to do.
    """
    if POPPLER_BIN:
        return POPPLER_BIN
    arch = POPPLER_ARCH
    raise RuntimeError(
        f"poppler binaries are not bundled for the runtime architecture "
        f"({arch}). The utils_bundle.zip must contain both "
        f"poppler_bundle/arm64/poppler/bin/ and "
        f"poppler_bundle/x86_64/poppler/bin/ so the procedure works on "
        f"Snowflake warehouses with resource_constraint=None (which may "
        f"schedule on either arch). Rebuild the bundle with "
        f"`python3 procedure/build_bundle.py --clean` and re-upload to "
        f"your stage. As a workaround, set `vision: false` in the "
        f"instruction JSON to use Layout-only ingestion (no poppler needed)."
    )
