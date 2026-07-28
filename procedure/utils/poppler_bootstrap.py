"""
procedure/utils/poppler_bootstrap.py
Single source of truth for resolving poppler binaries bundled into
utils_bundle.zip.

Snowflake runtime model (corrected)
-----------------------------------
Snowflake Python UDFs/SPs do NOT extract IMPORTS zips to disk. The zip
is placed in the working directory as a single file:

    /home/udf/<id>/utils_bundle.zip    ← file, not directory

Python modules inside the zip are importable via zipimport (Python's
built-in zip importer). When you do `from chunky_utils.X import Y`,
Python finds `chunky_utils/X.py` INSIDE the zip without extracting.

Native binaries (ELF executables like `pdftoppm`) CANNOT be executed
from inside a zip — the kernel can't mmap them. They MUST be extracted
to a real filesystem path first.

The only writable directory in a Snowflake Python UDF is `/tmp/`. The
working directory (`/home/udf/<id>/`) is read-only.

Wrapper-script pattern (the critical fix)
-----------------------------------------
Even after extracting the binaries to /tmp/ and setting LD_LIBRARY_PATH,
`pdftoppm` may still fail silently (returns empty output, no error
message) because:

  1. Snowflake's subprocess environment may not reliably inherit
     LD_LIBRARY_PATH.
  2. The bundled `libc.so.6` must be loaded by the bundled `ld-linux`
     (not the system's) to avoid version mismatches. If the system's
     ld-linux loads the bundled libc, you get a segfault or silent
     failure.

The standard fix (used by conda, NixOS, AppImage, and every Snowflake
UDF that bundles native binaries) is to wrap each binary in a shell
script that invokes the bundled dynamic linker explicitly:

    #!/bin/sh
    exec "/tmp/.../lib/ld-linux-aarch64.so.1" \
        --library-path "/tmp/.../lib" \
        "/tmp/.../bin/pdftoppm.real" "$@"

This bypasses LD_LIBRARY_PATH entirely and guarantees the bundled
ld-linux loads the bundled libc and all other bundled .so files.

This module:
  1. Finds the utils_bundle.zip via sys.path (or __file__).
  2. Detects the runtime CPU architecture (aarch64 → arm64, x86_64 → x86_64).
  3. Extracts the poppler binaries + shared libs for that arch from the
     zip to /tmp/chunky_poppler_<pid>_<arch>/. Binaries are saved as
     <name>.real; shared libs go to lib/.
  4. Creates shell-script wrappers <name> (one per binary) that invoke
     the bundled ld-linux with --library-path.
  5. chmod +x the wrappers and .real binaries.
  6. Sets LD_LIBRARY_PATH (belt-and-suspenders, in case anything
     bypasses the wrapper).
  7. Returns the bin directory (containing the wrappers) for
     `pdf2image.convert_from_bytes(poppler_path=...)`.

The extraction is idempotent — if /tmp/chunky_poppler_<pid>_<arch>/
already exists with the wrappers, it's reused (the pid in the path
means each UDF invocation gets its own copy; Snowflake reuses the UDF
process across calls so the extraction only happens once).
"""
from __future__ import annotations
import os
import platform
import shutil
import stat
import sys
import tempfile
import zipfile
from typing import Optional


# -----------------------------------------------------------------------------
# Architecture detection
# -----------------------------------------------------------------------------
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
    # Unknown arch — fall back to x86_64 (more common in Snowflake today).
    return "x86_64"


# -----------------------------------------------------------------------------
# Find the utils_bundle.zip
# -----------------------------------------------------------------------------
def _find_bundle_zip() -> Optional[str]:
    """
    Locate utils_bundle.zip in the Snowflake runtime.

    Strategy (in order):
      1. Look at sys.path entries ending in `.zip` — Snowflake adds the
         IMPORTS zip there so zipimport can find Python modules.
      2. Fall back to __file__ — for a module inside the zip, __file__
         looks like `/path/to/zip/inner/module.py`. Split on `.zip/`
         to recover the zip path.
      3. Scan common Snowflake working directories for *.zip files.
    """
    # Strategy 1: sys.path
    for entry in sys.path:
        if not entry:
            continue
        if entry.endswith(".zip") and os.path.isfile(entry):
            return entry

    # Strategy 2: __file__ (this module is inside the zip)
    this_file = os.path.abspath(__file__)
    # __file__ for a module inside a zip looks like:
    #   /home/udf/<id>/utils_bundle.zip/chunky_utils/poppler_bootstrap.py
    if ".zip" in this_file:
        # Split on the first .zip/ occurrence
        idx = this_file.find(".zip")
        if idx != -1:
            zip_path = this_file[:idx + len(".zip")]
            if os.path.isfile(zip_path):
                return zip_path

    # Strategy 3: scan working directories
    for scan_dir in (
        os.environ.get("PYTHON_WORKING_DIR", ""),
        "/home/udf",
        tempfile.gettempdir(),
        ".",
    ):
        if not scan_dir or not os.path.isdir(scan_dir):
            continue
        try:
            for name in os.listdir(scan_dir):
                if name.endswith(".zip") and os.path.isfile(
                    os.path.join(scan_dir, name)
                ):
                    return os.path.join(scan_dir, name)
        except OSError:
            continue

    return None


# -----------------------------------------------------------------------------
# Extraction
# -----------------------------------------------------------------------------
_EXTRACTED_BIN_DIR: Optional[str] = None
_EXTRACTED_LIB_DIR: Optional[str] = None
_EXTRACTED_ARCH: Optional[str] = None


def _find_dynamic_linker(lib_dir: str) -> Optional[str]:
    """
    Find the bundled dynamic linker in `lib_dir`.

    ARM64: ld-linux-aarch64.so.1
    x86_64: ld-linux-x86-64.so.2
    """
    for name in ("ld-linux-aarch64.so.1", "ld-linux-x86-64.so.2"):
        path = os.path.join(lib_dir, name)
        if os.path.isfile(path):
            return path
    return None


def _create_wrapper_script(wrapper_path: str, real_binary: str,
                           ld_linux: str, lib_dir: str) -> None:
    """
    Create a shell-script wrapper that invokes the bundled dynamic linker
    explicitly with --library-path.

    This is the standard pattern for bundled binaries (conda, NixOS,
    AppImage). It bypasses LD_LIBRARY_PATH inheritance issues entirely —
    the wrapper invokes the bundled ld-linux directly, which loads the
    bundled libc.so.6 and all other bundled .so files from lib_dir.

    pdf2image calls the wrapper (an executable shell script) via
    subprocess; the wrapper handles the dynamic linker invocation.
    """
    script = (
        "#!/bin/sh\n"
        f'exec "{ld_linux}" --library-path "{lib_dir}" "{real_binary}" "$@"\n'
    )
    with open(wrapper_path, "w") as f:
        f.write(script)
    os.chmod(wrapper_path, 0o755)


def _extract_poppler_from_zip(zip_path: str, arch: str) -> Optional[str]:
    """
    Extract poppler binaries + shared libs for `arch` from `zip_path`
    to /tmp/chunky_poppler_<pid>_<arch>/.

    For each binary (pdftoppm, pdfinfo, pdftotext):
      1. Extract the original ELF to <name>.real
      2. Create a shell-script wrapper <name> that invokes the bundled
         ld-linux with --library-path, pointing at <name>.real

    The wrapper pattern is necessary because Snowflake's subprocess
    environment may not reliably inherit LD_LIBRARY_PATH, and the
    bundled libc.so.6 must be loaded by the bundled ld-linux (not the
    system's) to avoid version mismatches.

    Returns the bin directory on success, or None on failure.
    Idempotent: if the target directory already has the wrappers, reuses it.
    """
    global _EXTRACTED_BIN_DIR, _EXTRACTED_LIB_DIR, _EXTRACTED_ARCH

    # Idempotency: if we already extracted for this arch, reuse.
    if _EXTRACTED_BIN_DIR and _EXTRACTED_ARCH == arch and \
       os.path.isdir(_EXTRACTED_BIN_DIR):
        return _EXTRACTED_BIN_DIR

    extract_root = os.path.join(
        tempfile.gettempdir(),
        f"chunky_poppler_{os.getpid()}_{arch}",
    )
    bin_dir = os.path.join(extract_root, "poppler", "bin")
    lib_dir = os.path.join(extract_root, "poppler", "lib")

    # Idempotency: if a previous extraction (same pid) already populated
    # bin_dir with wrappers, reuse it without re-extracting.
    pdftoppm_wrapper = os.path.join(bin_dir, "pdftoppm")
    pdftoppm_real = os.path.join(bin_dir, "pdftoppm.real")
    if os.path.isfile(pdftoppm_wrapper) and os.path.isfile(pdftoppm_real):
        _EXTRACTED_BIN_DIR = bin_dir
        _EXTRACTED_LIB_DIR = lib_dir
        _EXTRACTED_ARCH = arch
        _configure_env(bin_dir, lib_dir)
        return bin_dir

    # Open the zip and extract the poppler_bundle/<arch>/ subtree.
    prefix = f"poppler_bundle/{arch}/poppler/"
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = [
                m for m in zf.namelist()
                if m.startswith(prefix) and not m.endswith("/")
            ]
            if not members:
                return None

            os.makedirs(extract_root, exist_ok=True)
            for member in members:
                # member looks like: poppler_bundle/<arch>/poppler/bin/pdftoppm
                # We want to extract to: <extract_root>/poppler/bin/pdftoppm.real
                rel = member[len(f"poppler_bundle/{arch}/"):]  # poppler/bin/pdftoppm
                target = os.path.join(extract_root, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)

                # Extract binaries as <name>.real (the wrapper will be <name>)
                if "/bin/" in member:
                    target = target + ".real"

                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

                # Set permissions
                if "/bin/" in member:
                    os.chmod(target, 0o755)  # binaries need +x
                else:
                    basename = os.path.basename(target)
                    # The dynamic linker (ld-linux-*) MUST be executable —
                    # the wrapper script exec's it directly. Other .so
                    # files just need +r.
                    if basename.startswith("ld-linux-"):
                        os.chmod(target, 0o755)
                    else:
                        current = os.stat(target).st_mode
                        os.chmod(target, current | stat.S_IRUSR | stat.S_IRGRP)
    except Exception as e:
        # Best-effort cleanup on failure
        shutil.rmtree(extract_root, ignore_errors=True)
        print(f"[poppler_bootstrap] extraction failed: {e}")
        return None

    # Find the bundled dynamic linker
    ld_linux = _find_dynamic_linker(lib_dir)
    if not ld_linux:
        print(
            f"[poppler_bootstrap] bundled dynamic linker not found in {lib_dir}. "
            f"Expected ld-linux-aarch64.so.1 (arm64) or ld-linux-x86-64.so.2 (x86_64)."
        )
        return None

    # Create wrapper scripts for each binary
    for bin_name in ("pdftoppm", "pdfinfo", "pdftotext"):
        real_path = os.path.join(bin_dir, bin_name + ".real")
        if not os.path.isfile(real_path):
            print(f"[poppler_bootstrap] {bin_name}.real not found after extraction")
            return None
        wrapper_path = os.path.join(bin_dir, bin_name)
        _create_wrapper_script(wrapper_path, real_path, ld_linux, lib_dir)

    # Verify the wrapper was created
    if not os.path.isfile(pdftoppm_wrapper):
        print(
            f"[poppler_bootstrap] pdftoppm wrapper not created at {pdftoppm_wrapper}"
        )
        return None

    _EXTRACTED_BIN_DIR = bin_dir
    _EXTRACTED_LIB_DIR = lib_dir
    _EXTRACTED_ARCH = arch
    _configure_env(bin_dir, lib_dir)
    return bin_dir


def _configure_env(bin_dir: str, lib_dir: str) -> None:
    """Set LD_LIBRARY_PATH and PATH so poppler binaries find their libs."""
    if os.path.isdir(lib_dir):
        existing_ld = os.environ.get("LD_LIBRARY_PATH", "")
        if lib_dir not in existing_ld:
            os.environ["LD_LIBRARY_PATH"] = (
                lib_dir + (":" + existing_ld if existing_ld else "")
            )
    if os.path.isdir(bin_dir):
        existing_path = os.environ.get("PATH", "")
        if bin_dir not in existing_path:
            os.environ["PATH"] = bin_dir + ":" + existing_path


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------
def _udf_root() -> str:
    """
    Return the directory one level above the chunky_utils package.

    NOTE: In Snowflake, this is the directory CONTAINING the zip file
    (not an extracted tree). The actual poppler binaries live INSIDE
    the zip and must be extracted to /tmp/ at runtime — see
    `bootstrap()` below.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def poppler_bin_dir(arch: Optional[str] = None) -> str:
    """
    Return the poppler bin directory for the given arch (or runtime-detected).

    NOTE: In Snowflake, this returns the path INSIDE the zip — which is
    not directly executable. Use `bootstrap()` or `POPPLER_BIN` to get
    the extracted /tmp/ path that can actually be passed to poppler_path=.
    """
    a = arch or detect_arch()
    return f"poppler_bundle/{a}/poppler/bin"  # path inside the zip


def bootstrap() -> dict:
    """
    Idempotently configure the environment so pdf2image + poppler work.

    - Finds utils_bundle.zip (via sys.path or __file__).
    - Detects the runtime architecture (aarch64 → arm64, x86_64 → x86_64).
    - Extracts poppler binaries + shared libs for that arch from the zip
      to /tmp/chunky_poppler_<pid>_<arch>/.
    - chmod +x the extracted binaries.
    - Sets LD_LIBRARY_PATH and PATH.
    - Adds the udf root to sys.path so `from pdf2image import ...` works
      (this is needed when the bundle is NOT a Snowflake IMPORTS zip —
      e.g. when running tests locally with the extracted directory).

    Returns a dict with:
        arch: the detected architecture ('arm64' or 'x86_64')
        bin_dir: the EXTRACTED poppler bin directory in /tmp/ (or None)
        lib_dir: the EXTRACTED poppler lib directory in /tmp/ (or None)
        available: True if poppler binaries are usable for this arch
        zip_path: the path to utils_bundle.zip (or None)
        extract_root: the /tmp/ extraction directory (or None)
    """
    arch = detect_arch()
    zip_path = _find_bundle_zip()

    # For local testing (bundle extracted to a real directory), fall back
    # to the old path-based resolution. This branch is NOT used in Snowflake.
    if zip_path is None:
        # Try the old layout: <udf_root>/poppler_bundle/<arch>/poppler/bin
        # (works when the zip has been manually extracted, e.g. in tests)
        disk_bin = os.path.join(
            _udf_root(), "poppler_bundle", arch, "poppler", "bin"
        )
        disk_lib = os.path.join(
            _udf_root(), "poppler_bundle", arch, "poppler", "lib"
        )
        if os.path.isdir(disk_bin) and os.path.isfile(
            os.path.join(disk_bin, "pdftoppm")
        ):
            _configure_env(disk_bin, disk_lib)
            return {
                "arch": arch,
                "bin_dir": disk_bin,
                "lib_dir": disk_lib if os.path.isdir(disk_lib) else None,
                "available": True,
                "zip_path": None,
                "extract_root": None,
                "extraction_method": "disk_fallback",
            }
        return {
            "arch": arch,
            "bin_dir": None,
            "lib_dir": None,
            "available": False,
            "zip_path": None,
            "extract_root": None,
            "extraction_method": "none",
        }

    # Snowflake path: extract from the zip to /tmp/.
    bin_dir = _extract_poppler_from_zip(zip_path, arch)
    if bin_dir is None:
        return {
            "arch": arch,
            "bin_dir": None,
            "lib_dir": None,
            "available": False,
            "zip_path": zip_path,
            "extract_root": None,
            "extraction_method": "zip_extract_failed",
        }

    # Add the udf root to sys.path so `from pdf2image import ...` works.
    # In Snowflake, the zip is already in sys.path (that's how we found
    # it), so pdf2image/ inside the zip is importable via zipimport.
    # For local testing, the zip path's parent may need to be on sys.path.
    zip_parent = os.path.dirname(zip_path)
    if zip_parent not in sys.path:
        sys.path.insert(0, zip_parent)

    return {
        "arch": arch,
        "bin_dir": bin_dir,
        "lib_dir": _EXTRACTED_LIB_DIR,
        "available": True,
        "zip_path": zip_path,
        "extract_root": os.path.dirname(bin_dir),
        "extraction_method": "zip_extract",
    }


# Resolve once at import time so the env is configured before any
# `from pdf2image import ...` happens in a downstream module.
_BOOTSTRAP_RESULT = bootstrap()
POPPLER_BIN: Optional[str] = _BOOTSTRAP_RESULT["bin_dir"]
POPPLER_ARCH: str = _BOOTSTRAP_RESULT["arch"]
POPPLER_AVAILABLE: bool = _BOOTSTRAP_RESULT["available"]
POPPLER_ZIP_PATH: Optional[str] = _BOOTSTRAP_RESULT.get("zip_path")
POPPLER_EXTRACT_ROOT: Optional[str] = _BOOTSTRAP_RESULT.get("extract_root")


def get_poppler_bin_or_raise() -> str:
    """
    Return the extracted poppler bin directory, or raise a descriptive
    RuntimeError if poppler is not available for the runtime arch.

    Use this in handlers that absolutely need poppler (Vision extraction,
    page rendering) — the error message tells the caller exactly what to do.
    """
    if POPPLER_BIN:
        return POPPLER_BIN
    arch = POPPLER_ARCH
    if POPPLER_ZIP_PATH:
        raise RuntimeError(
            f"poppler binaries for the runtime architecture ({arch}) were "
            f"not found inside {POPPLER_ZIP_PATH}. The zip must contain "
            f"poppler_bundle/{arch}/poppler/bin/ with pdftoppm, pdfinfo, "
            f"and pdftotext. Rebuild the bundle with "
            f"`python3 procedure/build_bundle.py --clean` (which bundles "
            f"BOTH arm64 and x86_64 by default) and re-upload to your "
            f"stage. As a workaround, set `vision: false, layout: true` "
            f"in the instruction JSON to use Layout-only ingestion."
        )
    raise RuntimeError(
        f"utils_bundle.zip was not found in the Snowflake runtime "
        f"(checked sys.path, __file__, and common working directories). "
        f"poppler binaries for the runtime architecture ({arch}) cannot "
        f"be extracted. Verify the procedure's IMPORTS clause includes "
        f"'@DEV_DB.DNA.STG_LIB/utils_bundle.zip' and that the zip is "
        f"uploaded to the stage. As a workaround, set `vision: false, "
        f"layout: true` in the instruction JSON to use Layout-only "
        f"ingestion (no poppler needed)."
    )
