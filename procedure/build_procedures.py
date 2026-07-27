#!/usr/bin/env python3
"""
procedure/build_procedures.py
Generate the deployable .sql files for every Chunky procedure and bundle
the shared Python utilities into a single zip that procedures IMPORTS.

Why this script exists
-----------------------
The Python handler code lives in `procedure/utils/*.py` so it can be
imported by tests, linted, refactored with normal Python tooling, and
read without unescaping SQL string literals. But Snowflake CREATE
PROCEDURE statements need:

  1. The handler module available on a stage (via IMPORTS).
  2. A short inline Python block that imports the handler and exposes
     it as the HANDLER symbol.

Doing the bundling and SQL generation by hand is error-prone — a single
rename in `procedure/utils/` would break every procedure. This script
automates both pieces.

What it does
------------
1. Zips `procedure/utils/` -> `procedure/utils_bundle.zip`
   (top-level `utils/` directory so `from utils.foo import run` works
   inside Snowflake).
2. Reads each `.sql.j2` template from `procedure/templates/`.
3. Substitutes `{{...}}` placeholders (e.g. `{{LIB_STAGE}}`,
   `{{POPPLER_BUNDLE}}`).
4. Writes the final `.sql` files to `procedure/`.

Run it after editing any handler or constants file:

    python3 procedure/build_procedures.py

Configuration
-------------
The defaults below are overridable via environment variables so the same
script works for DEV_DB, PROD_DB, or any other Snowflake account:

  CHUNKY_LIB_STAGE      default: @DEV_DB.DNA.STG_LIB
  CHUNKY_POPPLER_BUNDLE default: poppler_bundle.zip
  CHUNKY_UTILS_BUNDLE   default: utils_bundle.zip
  CHUNKY_PYTHON_RUNTIME default: 3.11
"""
from __future__ import annotations
import os
import sys
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROC_DIR = Path(__file__).resolve().parent          # procedure/
UTILS_DIR = PROC_DIR / "utils"
TEMPLATES_DIR = PROC_DIR / "templates"


# ---------------------------------------------------------------------------
# Configurable defaults (overridable via env vars — reduces hardcoding)
# ---------------------------------------------------------------------------
def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


LIB_STAGE = _env("CHUNKY_LIB_STAGE", "@DEV_DB.DNA.STG_LIB")
POPPLER_BUNDLE = _env("CHUNKY_POPPLER_BUNDLE", "poppler_bundle.zip")
UTILS_BUNDLE = _env("CHUNKY_UTILS_BUNDLE", "utils_bundle.zip")
PYTHON_RUNTIME = _env("CHUNKY_PYTHON_RUNTIME", "3.11")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def render_template(template: str, **subs: str) -> str:
    """Replace `{{name}}` placeholders in `template` with values from `subs`."""
    out = template
    for key, val in subs.items():
        out = out.replace("{{" + key + "}}", val)
    return out


def build_utils_bundle() -> Path:
    """
    Zip `procedure/utils/` into `procedure/utils_bundle.zip`.

    The zip preserves a top-level `chunky_utils/` directory so that
    inside Snowflake the handler can do `from chunky_utils.init_table
    import run`. We deliberately do NOT use `utils/` as the top-level
    dir — that would shadow the Streamlit-side `utils/` package when
    tests import both.
    """
    if not UTILS_DIR.is_dir():
        raise SystemExit(f"ERROR: utils directory not found: {UTILS_DIR}")

    zip_path = PROC_DIR / UTILS_BUNDLE
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for py_file in sorted(UTILS_DIR.rglob("*.py")):
            # arcname = chunky_utils/<relative path>
            arcname = "chunky_utils/" + py_file.relative_to(UTILS_DIR).as_posix()
            zf.write(py_file, arcname)
    return zip_path


# ---------------------------------------------------------------------------
# Procedure specs — each maps an output .sql file to a template
# ---------------------------------------------------------------------------
PROCEDURES = [
    # Sub-procedures (now backed by procedure/utils/*.py)
    {
        "output": "chunky_internal_init_table.sql",
        "template": "chunky_internal_init_table.sql.j2",
    },
    {
        "output": "chunky_internal_grant_table.sql",
        "template": "chunky_internal_grant_table.sql.j2",
    },
    {
        "output": "chunky_internal_surgical_delete.sql",
        "template": "chunky_internal_surgical_delete.sql.j2",
    },
    {
        "output": "chunky_internal_parse_pdf.sql",
        "template": "chunky_internal_parse_pdf.sql.j2",
    },
    {
        "output": "chunky_internal_build_chunk_ref.sql",
        "template": "chunky_internal_build_chunk_ref.sql.j2",
    },
    # Main procedures
    {
        "output": "chunky_chunks.sql",
        "template": "chunky_chunks.sql.j2",
    },
    {
        "output": "chunky_qa.sql",
        "template": "chunky_qa.sql.j2",
    },
    {
        "output": "chunky_searchservice.sql",
        "template": "chunky_searchservice.sql.j2",
    },
]

# Substitutions shared by every template
COMMON_SUBS = {
    "LIB_STAGE": LIB_STAGE,
    "POPPLER_BUNDLE": POPPLER_BUNDLE,
    "UTILS_BUNDLE": UTILS_BUNDLE,
    "PYTHON_RUNTIME": PYTHON_RUNTIME,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    if not TEMPLATES_DIR.is_dir():
        print(f"ERROR: templates directory not found: {TEMPLATES_DIR}",
              file=sys.stderr)
        return 1

    # 1. Build the utils bundle zip
    print("Building utils bundle...")
    zip_path = build_utils_bundle()
    print(f"  wrote {zip_path.relative_to(PROC_DIR)} "
          f"({zip_path.stat().st_size:,} bytes)")

    # 2. Validate that every template exists before writing anything
    for spec in PROCEDURES:
        tpl_path = TEMPLATES_DIR / spec["template"]
        if not tpl_path.is_file():
            print(f"ERROR: template not found: {tpl_path}", file=sys.stderr)
            return 1

    # 3. Render each procedure
    print("\nGenerating SQL files...")
    for spec in PROCEDURES:
        tpl = read_text(TEMPLATES_DIR / spec["template"])
        rendered = render_template(tpl, **COMMON_SUBS)
        out_path = PROC_DIR / spec["output"]
        out_path.write_text(rendered, encoding="utf-8")
        print(f"  wrote {out_path.relative_to(PROC_DIR)}")

    # 4. Generate the master installer
    install_tpl_path = TEMPLATES_DIR / "00_install_all.sql.j2"
    if install_tpl_path.is_file():
        install = render_template(read_text(install_tpl_path), **COMMON_SUBS)
        (PROC_DIR / "00_install_all.sql").write_text(install, encoding="utf-8")
        print("  wrote 00_install_all.sql")

    print("\nBuild complete. Next steps:")
    print(f"  1. Upload the utils bundle to Snowflake:")
    print(f"     PUT file://{PROC_DIR / UTILS_BUNDLE} {LIB_STAGE} AUTO_COMPRESS=FALSE;")
    print(f"  2. (If not already present) Upload the poppler bundle:")
    print(f"     PUT file://{PROC_DIR / POPPLER_BUNDLE} {LIB_STAGE} AUTO_COMPRESS=FALSE;")
    print(f"  3. Deploy the procedures:")
    print(f"     snowsql -f procedure/00_install_all.sql")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
