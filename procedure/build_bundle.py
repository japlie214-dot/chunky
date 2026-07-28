"""
procedure/build_bundle.py
Build a single utils_bundle.zip containing:
  - chunky_utils/   ← all Python handler modules (from procedure/utils/*.py)
  - poppler_bundle/ ← poppler binaries + shared libs (Linux x86_64)
  - pdf2image/      ← the pdf2image Python package

Snowflake extracts the zip to /home/udf/<id>/, so handlers can resolve
the poppler binaries via:

    import os
    _UDF_ROOT = os.path.dirname(os.path.dirname(__file__))  # /home/udf/<id>/
    _POPPLER_BIN = os.path.join(_UDF_ROOT, 'poppler_bundle', 'poppler', 'bin')
    _POPPLER_LIB = os.path.join(_UDF_ROOT, 'poppler_bundle', 'poppler', 'lib')
    sys.path.insert(0, _UDF_ROOT)  # so `from pdf2image import convert_from_bytes` works

Usage:
    python3 procedure/build_bundle.py            # build utils_bundle.zip
    python3 procedure/build_bundle.py --sql      # also render .sql from .j2 templates
    python3 procedure/build_bundle.py --clean    # remove the existing zip first
"""
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PROC_DIR = Path(__file__).resolve().parent
UTILS_SRC = PROC_DIR / "utils"
TEMPLATES_DIR = PROC_DIR / "templates"
OUT_ZIP = PROC_DIR / "utils_bundle.zip"

# Default values used when rendering .sql.j2 templates. Override via env vars
# (LIB_STAGE, UTILS_BUNDLE, PYTHON_RUNTIME) for non-default deployments.
DEFAULTS = {
    "LIB_STAGE": os.environ.get("LIB_STAGE", "@DEV_DB.DNA.STG_LIB"),
    "UTILS_BUNDLE": os.environ.get("UTILS_BUNDLE", "utils_bundle.zip"),
    "POPPLER_BUNDLE": os.environ.get("POPPLER_BUNDLE", "utils_bundle.zip"),  # same zip now
    "PYTHON_RUNTIME": os.environ.get("PYTHON_RUNTIME", "3.11"),
}


# -----------------------------------------------------------------------------
# Step 1: copy procedure/utils/*.py → chunky_utils/*.py
# -----------------------------------------------------------------------------
def _add_chunky_utils(zf: zipfile.ZipFile, tmpdir: Path) -> None:
    """Copy every .py from procedure/utils/ into the zip under chunky_utils/."""
    print("[1/3] Adding chunky_utils/ ...")
    if not UTILS_SRC.is_dir():
        raise SystemExit(f"Missing source dir: {UTILS_SRC}")
    for py in sorted(UTILS_SRC.glob("*.py")):
        target = Path("chunky_utils") / py.name
        zf.write(py, arcname=str(target))
        print(f"  + {target}")


# -----------------------------------------------------------------------------
# Step 2: locate poppler binaries + shared libs on the host
# -----------------------------------------------------------------------------
def _find_binaries(names: list[str]) -> dict[str, Path]:
    """Locate each binary by name on PATH."""
    found: dict[str, Path] = {}
    for name in names:
        path = shutil.which(name)
        if path:
            found[name] = Path(path)
        else:
            # Common Linux locations
            for cand in (f"/usr/bin/{name}", f"/usr/local/bin/{name}"):
                if os.path.isfile(cand):
                    found[name] = Path(cand)
                    break
    return found


def _ldd_deps(binary: Path) -> list[Path]:
    """Return list of shared-library dependencies of `binary` via `ldd`."""
    try:
        out = subprocess.run(
            ["ldd", str(binary)], capture_output=True, text=True, check=False,
        ).stdout
    except Exception:
        return []
    deps: list[Path] = []
    for line in out.splitlines():
        if "=>" not in line:
            continue
        right = line.split("=>", 1)[1].strip()
        # right looks like "/lib/x86_64-linux-gnu/libc.so.6 (0x...)"
        path = right.split(" ", 1)[0].strip()
        if path and os.path.isfile(path):
            deps.append(Path(path))
    return deps


def _add_poppler(zf: zipfile.ZipFile) -> None:
    """Add poppler binaries + shared libs to the zip under poppler_bundle/."""
    print("[2/3] Adding poppler_bundle/ ...")
    binaries = _find_binaries(["pdftoppm", "pdfinfo", "pdftotext"])
    if not binaries:
        print("  ! No poppler binaries found on this host.")
        print("    Install poppler-utils first:  apt-get install -y poppler-utils")
        print("    (or run procedure/build_poppler_bundle.sh inside Docker)")
        return

    libs_added: set[Path] = set()
    arc_added: set[str] = set()  # track archive names to avoid duplicates
    for name, path in binaries.items():
        arc = f"poppler_bundle/poppler/bin/{name}"
        if arc not in arc_added:
            zf.write(path, arcname=arc)
            arc_added.add(arc)
            print(f"  + {arc}")
        for dep in _ldd_deps(path):
            if dep in libs_added:
                continue
            libs_added.add(dep)
            arc_lib = f"poppler_bundle/poppler/lib/{dep.name}"
            if arc_lib in arc_added:
                continue
            arc_added.add(arc_lib)
            zf.write(dep, arcname=arc_lib)
            print(f"  + {arc_lib}")

    # Copy the dynamic linker if present (needed by poppler binaries
    # when run inside Snowflake's sandbox).
    for ld in ("/lib64/ld-linux-x86-64.so.2",
               "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"):
        if not os.path.isfile(ld):
            continue
        if Path(ld) in libs_added:
            continue
        arc_lib = f"poppler_bundle/poppler/lib/{Path(ld).name}"
        if arc_lib in arc_added:
            continue
        libs_added.add(Path(ld))
        arc_added.add(arc_lib)
        zf.write(ld, arcname=arc_lib)
        print(f"  + {arc_lib}")


# -----------------------------------------------------------------------------
# Step 3: add pdf2image package
# -----------------------------------------------------------------------------
def _add_pdf2image(zf: zipfile.ZipFile, tmpdir: Path) -> None:
    """pip-install pdf2image into a temp dir, then zip it under pdf2image/."""
    print("[3/3] Adding pdf2image/ ...")
    target = tmpdir / "pdf2image_root"
    target.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "--quiet", "--no-deps", "--target", str(target), "pdf2image"],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"  ! pip install pdf2image failed: {e}")
        print("    Skipping pdf2image — vision extraction will not work.")
        return

    pdf2image_dir = target / "pdf2image"
    if not pdf2image_dir.is_dir():
        print("  ! pdf2image package directory not found after install.")
        return

    for py in sorted(pdf2image_dir.rglob("*.py")):
        rel = py.relative_to(target)
        zf.write(py, arcname=str(rel))
        print(f"  + {rel}")


# -----------------------------------------------------------------------------
# Render .sql from .j2 templates (optional)
# -----------------------------------------------------------------------------
def _render_sql() -> None:
    """Render every .sql.j2 in templates/ into a sibling .sql in procedure/."""
    print("\nRendering .sql templates ...")
    try:
        import jinja2  # type: ignore
    except ImportError:
        print("  ! jinja2 not installed — skipping template rendering.")
        print("    Install with:  pip install jinja2")
        return

    for j2 in sorted(TEMPLATES_DIR.glob("*.sql.j2")):
        out_sql = PROC_DIR / j2.name.replace(".j2", "")
        text = j2.read_text()
        rendered = jinja2.Template(text).render(**DEFAULTS)
        out_sql.write_text(rendered)
        print(f"  + {out_sql.name}")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql", action="store_true",
                        help="Also render .sql files from .j2 templates")
    parser.add_argument("--clean", action="store_true",
                        help="Remove the existing utils_bundle.zip first")
    args = parser.parse_args()

    if args.clean and OUT_ZIP.exists():
        OUT_ZIP.unlink()
        print(f"Removed {OUT_ZIP}")

    print(f"Building {OUT_ZIP} ...")
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
            _add_chunky_utils(zf, tmpdir)
            _add_poppler(zf)
            _add_pdf2image(zf, tmpdir)

    size_kb = OUT_ZIP.stat().st_size // 1024
    print(f"\n✅ Built {OUT_ZIP.name} ({size_kb} KB)")

    if args.sql:
        _render_sql()

    print("\nUpload to Snowflake:")
    print(f"  PUT file://{OUT_ZIP} {DEFAULTS['LIB_STAGE']} AUTO_COMPRESS=FALSE;")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
