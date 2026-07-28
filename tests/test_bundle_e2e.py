"""
Verify that the single utils_bundle.zip:
  1. Contains all expected modules
  2. Can be unpacked to a temp dir
  3. The handlers can be imported from the unpacked chunky_utils/ package
  4. poppler_bootstrap resolves the correct path
  5. layout_parse handles both AI_PARSE_DOCUMENT response shapes
  6. The hybrid_repair / quality_inspector modules import cleanly

This is an end-to-end smoke test of the bundle (mimics what Snowflake
does when it extracts the IMPORTS zip to /home/udf/<id>/).
"""
from __future__ import annotations
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parent.parent
PROC_DIR = REPO / "procedure"
ZIP_PATH = PROC_DIR / "utils_bundle.zip"


def test_bundle_contains_all_required_files():
    """Every required file must be present in utils_bundle.zip."""
    required_chunky_utils = [
        "__init__.py", "_shared.py", "build_chunk_ref.py",
        "chunky_chunks_handler.py", "chunky_qa_handler.py",
        "chunky_searchservice_handler.py", "constants.py",
        "grant_table.py", "hybrid_repair.py", "init_table.py",
        "layout_parse.py", "metadata_handler.py", "page_mapping.py",
        "parse_pdf.py", "poppler_bootstrap.py", "prompts.py",
        "quality_inspector.py", "query_log.py", "revert.py",
        "surgical_delete.py",
    ]
    required_pdf2image = [
        "pdf2image/__init__.py", "pdf2image/pdf2image.py",
        "pdf2image/exceptions.py", "pdf2image/parsers.py",
        "pdf2image/generators.py",
    ]
    with zipfile.ZipFile(ZIP_PATH) as zf:
        names = set(zf.namelist())
    for f in required_chunky_utils:
        assert f"chunky_utils/{f}" in names, f"Missing: chunky_utils/{f}"
    for f in required_pdf2image:
        assert f in names, f"Missing: {f}"
    # poppler binaries must always be present (the bundle is built for
    # ARM64 by default — works on any host because build_arm_poppler.py
    # downloads pre-built ARM64 .deb packages from the Debian mirror).
    bin_files = [n for n in names if n.startswith("poppler_bundle/poppler/bin/")]
    assert bin_files, "Missing poppler binaries"
    lib_files = [n for n in names if n.startswith("poppler_bundle/poppler/lib/")]
    assert lib_files, "Missing poppler shared libs"


def test_bundle_poppler_binaries_are_arm64():
    """The bundled poppler binaries must be ARM64 ELF (Snowflake ARM warehouses).

    Reads the ELF header of each binary directly from the zip and verifies
    e_machine == EM_AARCH64 (0xB7). This is the strongest possible test
    that the bundle will work on Snowflake's ARM warehouses.
    """
    with zipfile.ZipFile(ZIP_PATH) as zf:
        for bin_name in ("pdftoppm", "pdfinfo", "pdftotext"):
            arc = f"poppler_bundle/poppler/bin/{bin_name}"
            assert arc in zf.namelist(), f"Missing: {arc}"
            with zf.open(arc) as f:
                header = f.read(20)
            # ELF magic
            assert header[:4] == b"\x7fELF", f"{bin_name}: not an ELF"
            # 64-bit
            assert header[4] == 2, f"{bin_name}: not 64-bit"
            # little-endian
            assert header[5] == 1, f"{bin_name}: not little-endian"
            # e_machine at offset 18-19 (little-endian)
            e_machine = (header[19] << 8) | header[18]
            assert e_machine == 0xB7, \
                f"{bin_name}: expected ARM64 (0xB7), got 0x{e_machine:x}"


def test_bundle_includes_arm_dynamic_linker():
    """The ARM64 dynamic linker (ld-linux-aarch64.so.1) must be bundled."""
    with zipfile.ZipFile(ZIP_PATH) as zf:
        names = zf.namelist()
    assert "poppler_bundle/poppler/lib/ld-linux-aarch64.so.1" in names, \
        "Missing ARM64 dynamic linker (ld-linux-aarch64.so.1)"


def test_bundle_handlers_importable_after_extraction():
    """Extract the bundle to a temp dir and import every handler."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        with zipfile.ZipFile(ZIP_PATH) as zf:
            zf.extractall(td_path)

        # The temp dir mimics /home/udf/<id>/
        # Add it to sys.path so `from chunky_utils.X import Y` works.
        sys.path.insert(0, str(td_path))
        try:
            # Remove any cached chunky_utils modules so we get the fresh
            # ones from the extracted bundle.
            for mod_name in list(sys.modules.keys()):
                if mod_name.startswith("chunky_utils"):
                    del sys.modules[mod_name]

            # Import every handler — any ImportError will fail this test.
            from chunky_utils import chunky_chunks_handler
            from chunky_utils import chunky_qa_handler
            from chunky_utils import chunky_searchservice_handler
            from chunky_utils import init_table
            from chunky_utils import grant_table
            from chunky_utils import surgical_delete
            from chunky_utils import parse_pdf
            from chunky_utils import build_chunk_ref
            from chunky_utils import revert
            from chunky_utils import query_log
            from chunky_utils import constants
            from chunky_utils import page_mapping
            from chunky_utils import metadata_handler
            from chunky_utils import _shared
            from chunky_utils import poppler_bootstrap
            from chunky_utils import layout_parse
            from chunky_utils import quality_inspector
            from chunky_utils import hybrid_repair
            from chunky_utils import prompts

            # All handlers must expose `run` (sub-procedures) or `run` (main)
            assert hasattr(chunky_chunks_handler, "run")
            assert hasattr(chunky_qa_handler, "run")
            assert hasattr(chunky_searchservice_handler, "run")
            assert hasattr(init_table, "run")
            assert hasattr(grant_table, "run")
            assert hasattr(surgical_delete, "run")
            assert hasattr(parse_pdf, "run")
            assert hasattr(build_chunk_ref, "run")
        finally:
            sys.path.pop(0)
            for mod_name in list(sys.modules.keys()):
                if mod_name.startswith("chunky_utils"):
                    del sys.modules[mod_name]


def test_bundle_poppler_path_resolution():
    """poppler_bootstrap must resolve to <extract_root>/poppler_bundle/poppler/bin."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        with zipfile.ZipFile(ZIP_PATH) as zf:
            zf.extractall(td_path)

        sys.path.insert(0, str(td_path))
        try:
            for mod_name in list(sys.modules.keys()):
                if mod_name.startswith("chunky_utils"):
                    del sys.modules[mod_name]

            from chunky_utils import poppler_bootstrap
            bin_dir = poppler_bootstrap.poppler_bin_dir()
            # The bin dir must be under td_path/poppler_bundle/poppler/bin
            expected_prefix = str(td_path / "poppler_bundle" / "poppler" / "bin")
            assert bin_dir == expected_prefix, \
                f"Expected {expected_prefix}, got {bin_dir}"

            # And the udf root must be td_path
            assert poppler_bootstrap._udf_root() == str(td_path), \
                f"Expected {td_path}, got {poppler_bootstrap._udf_root()}"
        finally:
            sys.path.pop(0)
            for mod_name in list(sys.modules.keys()):
                if mod_name.startswith("chunky_utils"):
                    del sys.modules[mod_name]


def test_bundle_pdf2image_importable():
    """`from pdf2image import convert_from_bytes` must work after extraction."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        with zipfile.ZipFile(ZIP_PATH) as zf:
            zf.extractall(td_path)

        sys.path.insert(0, str(td_path))
        try:
            for mod_name in list(sys.modules.keys()):
                if mod_name == "pdf2image" or mod_name.startswith("pdf2image."):
                    del sys.modules[mod_name]

            from pdf2image import convert_from_bytes
            assert callable(convert_from_bytes), \
                "pdf2image.convert_from_bytes must be callable"
        finally:
            sys.path.pop(0)
            for mod_name in list(sys.modules.keys()):
                if mod_name == "pdf2image" or mod_name.startswith("pdf2image."):
                    del sys.modules[mod_name]


def test_layout_parse_handles_both_shapes_via_bundle():
    """The layout_parse module in the bundle must handle both response shapes."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        with zipfile.ZipFile(ZIP_PATH) as zf:
            zf.extractall(td_path)

        sys.path.insert(0, str(td_path))
        try:
            for mod_name in list(sys.modules.keys()):
                if mod_name.startswith("chunky_utils"):
                    del sys.modules[mod_name]

            from chunky_utils.layout_parse import parse_ai_parse_document_response

            # Shape A — explicit pages array
            raw_a = {
                "pages": [
                    {"index": 0, "content": "page 1"},
                    {"index": 1, "content": "page 2"},
                ],
                "metadata": {"pageCount": 2},
            }
            pages_a, _, used_ff_a = parse_ai_parse_document_response(raw_a)
            assert len(pages_a) == 2
            assert used_ff_a is False

            # Shape B — flat content with form-feed
            raw_b = {
                "content": "page 1\fpage 2\fpage 3",
                "metadata": {"pageCount": 3},
            }
            pages_b, _, used_ff_b = parse_ai_parse_document_response(raw_b)
            assert len(pages_b) == 3
            assert pages_b[0]["content"] == "page 1"
            assert pages_b[2]["content"] == "page 3"
            assert used_ff_b is True
        finally:
            sys.path.pop(0)
            for mod_name in list(sys.modules.keys()):
                if mod_name.startswith("chunky_utils"):
                    del sys.modules[mod_name]


def test_revert_uses_safe_rename_pattern_via_bundle():
    """revert_table must use ALTER TABLE RENAME, not CREATE OR REPLACE TABLE X CLONE X."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        with zipfile.ZipFile(ZIP_PATH) as zf:
            zf.extractall(td_path)

        sys.path.insert(0, str(td_path))
        try:
            for mod_name in list(sys.modules.keys()):
                if mod_name.startswith("chunky_utils"):
                    del sys.modules[mod_name]

            from chunky_utils.revert import revert_table

            s = MagicMock()
            executed_sql = []

            def fake_sql(sql, params=None):
                executed_sql.append(sql)
                sql_upper = sql.upper()
                if "DATEDIFF" in sql_upper:
                    return MagicMock(collect=MagicMock(
                        return_value=[{"HOURS_AGO": 1, "QID": "q2"}]
                    ))
                if "CURRENT_TIMESTAMP" in sql_upper:
                    return MagicMock(collect=MagicMock(
                        return_value=[{"TS": "2024-01-01 12:00:00.000", "QID": "q1"}]
                    ))
                return MagicMock(collect=MagicMock(return_value=[{"QID": "q3"}]))

            s.sql.side_effect = fake_sql
            result = revert_table(s, "DEV_DB", "DNA", "T",
                                  timestamp_before="2024-01-01 12:00:00.000")
            assert result["success"] is True

            # The unsafe pattern must NOT appear
            joined = " ".join(executed_sql).upper()
            assert "CREATE OR REPLACE TABLE" not in joined or \
                   "CLONE" not in joined.split("CREATE OR REPLACE TABLE")[1].split("AT(")[0], \
                f"Unsafe CREATE OR REPLACE TABLE X CLONE X detected. SQLs: {executed_sql}"

            # The safe RENAME pattern MUST appear
            assert any("ALTER TABLE" in sql.upper() and "RENAME TO" in sql.upper()
                       for sql in executed_sql), \
                f"ALTER TABLE RENAME TO not issued. SQLs: {executed_sql}"

            # And a CLONE ... AT(TIMESTAMP => ...) must appear (the actual revert)
            assert any("CLONE" in sql.upper() and "AT(TIMESTAMP" in sql.upper()
                       for sql in executed_sql), \
                f"TIME TRAVEL CLONE not issued. SQLs: {executed_sql}"
        finally:
            sys.path.pop(0)
            for mod_name in list(sys.modules.keys()):
                if mod_name.startswith("chunky_utils"):
                    del sys.modules[mod_name]


def test_chunks_handler_dispatches_all_commands_via_bundle():
    """The chunky_chunks_handler in the bundle must dispatch every documented command."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        with zipfile.ZipFile(ZIP_PATH) as zf:
            zf.extractall(td_path)

        sys.path.insert(0, str(td_path))
        try:
            for mod_name in list(sys.modules.keys()):
                if mod_name.startswith("chunky_utils"):
                    del sys.modules[mod_name]

            from chunky_utils.chunky_chunks_handler import run

            s = MagicMock()
            s.sql.return_value.collect.return_value = [
                {"QID": "q1", "TS": "2024-01-01 12:00:00.000", "CNT": 0}
            ]

            # Each command must be recognised (not "Unknown command")
            commands_and_insts = [
                ("INGEST", {
                    "db": "X", "schema": "Y", "table": "Z",
                    "stage_path": "@X.Y.DOCS", "file": "x.pdf",
                }),
                ("LIST_CHUNKS", {"db": "X", "schema": "Y", "table": "Z"}),
                ("LIST_CHUNKS_CSV", {"db": "X", "schema": "Y", "table": "Z"}),
                ("UPDATE_CHUNK", {
                    "db": "X", "schema": "Y", "table": "Z",
                    "chunk_id": "CHK_x", "chunk": "new content",
                }),
                ("DELETE_CHUNKS", {
                    "db": "X", "schema": "Y", "table": "Z",
                    "chunk_ids": ["CHK_x"],
                }),
                ("INSPECT_QUALITY", {"db": "X", "schema": "Y", "table": "Z"}),
                ("ESTIMATE_COST", {
                    "db": "X", "schema": "Y", "table": "Z",
                    "stage_path": "@X.Y.DOCS", "file": "x.pdf",
                }),
                ("BATCH_INGEST", {"jobs": []}),  # will return success=False but not "Unknown"
                ("REVERT", {"db": "X", "schema": "Y", "table": "Z"}),
            ]
            for cmd, inst in commands_and_insts:
                result = run(s, cmd, inst)
                err = result.get("error") or ""
                assert "Unknown command" not in err, \
                    f"Command {cmd} not recognised: {err}"

            # Unknown command must still error
            result = run(s, "BOGUS", {})
            assert result["success"] is False
            assert "Unknown command" in result["error"]
        finally:
            sys.path.pop(0)
            for mod_name in list(sys.modules.keys()):
                if mod_name.startswith("chunky_utils"):
                    del sys.modules[mod_name]


if __name__ == "__main__":
    # Allow running as a script for quick smoke testing.
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
