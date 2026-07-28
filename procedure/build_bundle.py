"""
procedure/build_bundle.py
Build a single utils_bundle.zip containing:
  - chunky_utils/   ← all Python handler modules (from procedure/utils/*.py)
  - poppler_bundle/ ← poppler binaries for BOTH ARM64 + x86_64
                      (Snowflake warehouses with resource_constraint=None
                      may schedule on either arch)
  - pdf2image/      ← the pdf2image Python package

Snowflake extracts the zip to /home/udf/<id>/, so handlers can resolve
the poppler binaries via:

    import os
    _UDF_ROOT = os.path.dirname(os.path.dirname(__file__))  # /home/udf/<id>/
    _ARCH = platform.machine()  # 'aarch64' or 'x86_64'
    _POPPLER_BIN = os.path.join(_UDF_ROOT, 'poppler_bundle', _ARCH, 'poppler', 'bin')

`procedure/utils/poppler_bootstrap.py` centralises this logic and picks
the right arch at runtime.

Usage:
    python3 procedure/build_bundle.py                          # default: BOTH arches
    python3 procedure/build_bundle.py --arches arm64           # ARM64 only
    python3 procedure/build_bundle.py --arches x86_64          # x86_64 only
    python3 procedure/build_bundle.py --arches arm64 x86_64    # explicit both
    python3 procedure/build_bundle.py --sql                    # also render .sql
    python3 procedure/build_bundle.py --clean                  # remove existing zip
    python3 procedure/build_bundle.py --deb-cache /tmp/debs    # cache .deb files
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
from typing import List

PROC_DIR = Path(__file__).resolve().parent
UTILS_SRC = PROC_DIR / "utils"
TEMPLATES_DIR = PROC_DIR / "templates"
OUT_ZIP = PROC_DIR / "utils_bundle.zip"

# Default values used when rendering .sql.j2 templates. Override via env vars
# (LIB_STAGE, UTILS_BUNDLE, PYTHON_RUNTIME) for non-default deployments.
DEFAULTS = {
    "LIB_STAGE": os.environ.get("LIB_STAGE", "@DEV_DB.DNA.STG_LIB"),
    "UTILS_BUNDLE": os.environ.get("UTILS_BUNDLE", "utils_bundle.zip"),
    "POPPLER_BUNDLE": os.environ.get("POPPLER_BUNDLE", "utils_bundle.zip"),  # same zip
    "PYTHON_RUNTIME": os.environ.get("PYTHON_RUNTIME", "3.11"),
}


# -----------------------------------------------------------------------------
# Step 1: chunky_utils/
# -----------------------------------------------------------------------------
def _add_chunky_utils(zf: zipfile.ZipFile) -> None:
    """Copy every .py from procedure/utils/ into the zip under chunky_utils/."""
    print("[1/4] Adding chunky_utils/ ...")
    if not UTILS_SRC.is_dir():
        raise SystemExit(f"Missing source dir: {UTILS_SRC}")
    for py in sorted(UTILS_SRC.glob("*.py")):
        target = Path("chunky_utils") / py.name
        zf.write(py, arcname=str(target))
        print(f"  + {target}")


# -----------------------------------------------------------------------------
# Step 2: poppler_bundle/<arch>/poppler/...  (one or both arches)
# -----------------------------------------------------------------------------
def _add_poppler_arm64(zf: zipfile.ZipFile, deb_cache: Path) -> None:
    """Build an ARM64 poppler bundle by downloading Debian arm64 .deb packages.

    Delegates to procedure/build_arm_poppler.py, then zips the result
    under poppler_bundle/arm64/.
    """
    print("[2/4] Adding poppler_bundle/arm64/ ...")
    out_dir = Path(tempfile.mkdtemp(prefix="arm_poppler_"))
    try:
        cmd = [
            sys.executable, str(PROC_DIR / "build_arm_poppler.py"),
            "--out", str(out_dir),
        ]
        if deb_cache:
            cmd += ["--deb-cache", str(deb_cache)]
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            raise SystemExit("ARM64 poppler build failed — see output above.")
        # Print the tail of the build output so the user sees what happened
        for line in result.stdout.splitlines()[-10:]:
            print(f"  {line}")

        # out_dir contains: poppler/bin/, poppler/lib/, MANIFEST.txt
        # We want: poppler_bundle/arm64/poppler/bin/, poppler_bundle/arm64/poppler/lib/
        arc_added: set[str] = set()
        for path in sorted(out_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(out_dir)
            # Prepend poppler_bundle/arm64/ to every path
            arc = f"poppler_bundle/arm64/{rel}"
            if arc in arc_added:
                continue
            arc_added.add(arc)
            zf.write(path, arcname=arc)
            print(f"  + {arc}")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _add_poppler_x86_64(zf: zipfile.ZipFile) -> None:
    """Build an x86_64 poppler bundle from the host's own poppler-utils install.

    Uses `ldd` to discover shared-lib dependencies. Requires poppler-utils
    to be installed on the build host (apt-get install poppler-utils).
    Zips the result under poppler_bundle/x86_64/.
    """
    print("[3/4] Adding poppler_bundle/x86_64/ ...")
    binaries = _find_host_binaries(["pdftoppm", "pdfinfo", "pdftotext"])
    if not binaries:
        print("  ! No poppler binaries found on this host.")
        print("    Install poppler-utils first:  apt-get install -y poppler-utils")
        print("    Skipping x86_64 — the bundle will NOT work on x86_64 warehouses.")
        return

    libs_added: set[Path] = set()
    arc_added: set[str] = set()
    prefix = "poppler_bundle/x86_64/poppler"

    for name, path in binaries.items():
        arc = f"{prefix}/bin/{name}"
        if arc not in arc_added:
            zf.write(path, arcname=arc)
            arc_added.add(arc)
            print(f"  + {arc}")
        for dep in _ldd_deps(path):
            if dep in libs_added:
                continue
            libs_added.add(dep)
            arc_lib = f"{prefix}/lib/{dep.name}"
            if arc_lib in arc_added:
                continue
            arc_added.add(arc_lib)
            zf.write(dep, arcname=arc_lib)
            print(f"  + {arc_lib}")

    # Dynamic linker
    for ld in ("/lib64/ld-linux-x86-64.so.2",
               "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"):
        if not os.path.isfile(ld):
            continue
        if Path(ld) in libs_added:
            continue
        arc_lib = f"{prefix}/lib/{Path(ld).name}"
        if arc_lib in arc_added:
            continue
        libs_added.add(Path(ld))
        arc_added.add(arc_lib)
        zf.write(ld, arcname=arc_lib)
        print(f"  + {arc_lib}")


def _find_host_binaries(names: List[str]) -> dict[str, Path]:
    """Locate each binary by name on PATH (x86_64 path only)."""
    found: dict[str, Path] = {}
    for name in names:
        path = shutil.which(name)
        if path:
            found[name] = Path(path)
        else:
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
        path = right.split(" ", 1)[0].strip()
        if path and os.path.isfile(path):
            deps.append(Path(path))
    return deps


# -----------------------------------------------------------------------------
# Step 3: pdf2image/
# -----------------------------------------------------------------------------
def _add_pdf2image(zf: zipfile.ZipFile) -> None:
    """pip-install pdf2image into a temp dir, then zip it under pdf2image/."""
    print("[4/4] Adding pdf2image/ ...")
    tmpdir = Path(tempfile.mkdtemp(prefix="pdf2image_"))
    try:
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
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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
    parser.add_argument(
        "--arches", nargs="+", default=["arm64", "x86_64"],
        choices=["arm64", "x86_64"],
        help="Target architectures to bundle (default: arm64 x86_64 — both). "
             "Snowflake warehouses with resource_constraint=None may schedule "
             "on either arch, so bundling both is the safe default.",
    )
    parser.add_argument("--deb-cache",
                        help="Cache directory for downloaded .deb files (ARM64 only)")
    args = parser.parse_args()

    if args.clean and OUT_ZIP.exists():
        OUT_ZIP.unlink()
        print(f"Removed {OUT_ZIP}")

    arches = args.arches
    print(f"Building {OUT_ZIP} (target arches: {arches}) ...")
    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        _add_chunky_utils(zf)
        if "arm64" in arches:
            deb_cache = Path(args.deb_cache) if args.deb_cache else None
            _add_poppler_arm64(zf, deb_cache or Path(tempfile.gettempdir()) / "chunky_arm_debs")
        if "x86_64" in arches:
            _add_poppler_x86_64(zf)
        _add_pdf2image(zf)

    size_kb = OUT_ZIP.stat().st_size // 1024
    print(f"\n✅ Built {OUT_ZIP.name} ({size_kb} KB) — arches: {arches}")

    if args.sql:
        _render_sql()

    print("\nUpload to Snowflake:")
    print(f"  PUT file://{OUT_ZIP} {DEFAULTS['LIB_STAGE']} AUTO_COMPRESS=FALSE;")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
