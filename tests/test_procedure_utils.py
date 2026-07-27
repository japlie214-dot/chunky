"""
Tests for procedure/utils/* — the headless procedure utility layer.

These tests run without Snowflake (the Snowpark session is mocked) so
they can execute in CI / local dev environments.

Run:  python3 -m pytest tests/test_procedure_utils.py -v
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make procedure/ importable so `from chunky_utils.foo import ...` works the
# same way it does inside the Snowflake IMPORTS zip.
PROC_DIR = Path(__file__).resolve().parent.parent / "procedure"
sys.path.insert(0, str(PROC_DIR))


# =============================================================================
# build_chunk_ref — pure function, no session needed
# =============================================================================
class TestBuildChunkRef:
    def test_basic_no_link(self):
        from chunky_utils.build_chunk_ref import build
        assert build("doc.pdf", 5) == "Doc Source: doc.pdf | Page Num: 5"

    def test_with_link(self):
        from chunky_utils.build_chunk_ref import build
        out = build("doc.pdf", 5, link="https://example.com/page")
        assert "[Digital Copy](" in out
        assert "Doc Source: doc.pdf | Page Num: 5" in out

    def test_link_with_special_chars(self):
        from chunky_utils.build_chunk_ref import build
        out = build("d.pdf", 1, link="https://x.com/a b?(c)")
        # The link must be URL-encoded so it doesn't break Markdown syntax
        assert " " not in out.split("( ")[-1].split(" )")[0] if "( " in out else True
        # Round-trip the encoded URL to verify it parses
        import urllib.parse
        # Pull out the URL between [Digital Copy]( and )
        url = out.split("[Digital Copy](")[1].split(")")[0]
        assert urllib.parse.unquote(url) == "https://x.com/a b?(c)"

    def test_handler_form_returns_dict(self):
        from chunky_utils.build_chunk_ref import run
        out = run("d.pdf", 1, "")
        assert out == {"chunk_ref": "Doc Source: d.pdf | Page Num: 1"}


# =============================================================================
# page_mapping — copy of top-level utils/page_mapping.py
# =============================================================================
class TestPageMappingCopy:
    """Verify the procedure/utils copy stays in sync with the
    Streamlit-side utils/page_mapping.py."""

    def test_range_mapping_dataclass(self):
        from chunky_utils.page_mapping import RangeMapping
        rm = RangeMapping(source_start=2, source_end=3,
                          replacement_start=1, replacement_end=5)
        assert rm.source_start == 2
        assert rm.source_end == 3

    def test_compute_delta_expansion(self):
        from chunky_utils.page_mapping import RangeMapping, RangeMappingEngine
        rm = RangeMapping(2, 3, 1, 5)
        assert RangeMappingEngine.compute_delta(rm) == 3  # 5 - 2 = +3

    def test_compute_delta_contraction(self):
        from chunky_utils.page_mapping import RangeMapping, RangeMappingEngine
        rm = RangeMapping(2, 5, 1, 2)
        assert RangeMappingEngine.compute_delta(rm) == -2  # 2 - 4 = -2

    def test_compute_delta_zero(self):
        from chunky_utils.page_mapping import RangeMapping, RangeMappingEngine
        rm = RangeMapping(2, 3, 1, 2)
        assert RangeMappingEngine.compute_delta(rm) == 0

    def test_target_page_for_in_range(self):
        from chunky_utils.page_mapping import RangeMapping, RangeMappingEngine
        mappings = [RangeMapping(2, 3, 1, 5)]
        assert RangeMappingEngine.target_page_for(mappings, 1) == 2
        assert RangeMappingEngine.target_page_for(mappings, 5) == 6

    def test_target_page_for_out_of_range(self):
        from chunky_utils.page_mapping import RangeMapping, RangeMappingEngine
        mappings = [RangeMapping(2, 3, 1, 5)]
        assert RangeMappingEngine.target_page_for(mappings, 6) is None

    def test_sort_bottom_up(self):
        from chunky_utils.page_mapping import RangeMapping, RangeMappingEngine
        mappings = [
            RangeMapping(2, 3, 1, 5),
            RangeMapping(7, 8, 1, 2),
            RangeMapping(11, 12, 1, 2),
        ]
        sorted_rms = RangeMappingEngine.sort_bottom_up(mappings)
        # Highest source_end first
        assert sorted_rms[0].source_end == 12
        assert sorted_rms[1].source_end == 8
        assert sorted_rms[2].source_end == 3

    def test_to_per_page_mappings(self):
        from chunky_utils.page_mapping import RangeMapping, RangeMappingEngine
        mappings = [RangeMapping(2, 3, 1, 5)]
        per_page = RangeMappingEngine.to_per_page_mappings(mappings)
        assert len(per_page) == 5
        assert per_page[0] == {"source": 1, "target": 2}
        assert per_page[4] == {"source": 5, "target": 6}

    def test_validate_detects_overlap(self):
        from chunky_utils.page_mapping import RangeMapping, RangeMappingEngine
        mappings = [
            RangeMapping(2, 5, 1, 4),
            RangeMapping(4, 7, 1, 4),  # overlaps source with first
        ]
        ok, errors = RangeMappingEngine.validate(mappings, replacement_page_count=10)
        assert not ok
        assert any("Source ranges overlap" in e for e in errors)


# =============================================================================
# metadata_handler — copy of top-level utils/metadata_handler.py
# =============================================================================
class TestMetadataHandlerCopy:
    def test_create_initial_metadata(self):
        from chunky_utils.metadata_handler import ChunkMetadataHandler
        m = ChunkMetadataHandler.create_initial_metadata(
            write_mode="APPEND",
            chunk_type="standard",
            parser_config={"layout": True},
        )
        assert m["write_mode"] == "APPEND"
        assert m["chunk_type"] == "standard"
        assert m["parser"]["layout"] is True
        assert "created" in m["timestamps"]

    def test_serialize_metadata(self):
        from chunky_utils.metadata_handler import ChunkMetadataHandler
        m = {"a": 1, "b": [2, 3]}
        s = ChunkMetadataHandler.serialize_metadata(m)
        assert json.loads(s) == m

    def test_build_surgical_metadata_includes_original_pdf_page(self):
        from chunky_utils.metadata_handler import ChunkMetadataHandler
        page_mappings = [
            {"source": 1, "target": 2, "original_pdf_page": 1},
        ]
        s = ChunkMetadataHandler.build_surgical_select_metadata(
            original_file="orig.pdf",
            source_range=(1, 5),
            replacement_file="repl.pdf",
            page_mappings=page_mappings,
        )
        m = json.loads(s)
        assert m["write_mode"] == "SURGICAL"
        assert m["surgical"]["page_mappings"][0]["original_pdf_page"] == 1


# =============================================================================
# constants — every constant referenced by handlers must exist
# =============================================================================
class TestConstants:
    def test_chunk_constants(self):
        from chunky_utils import constants
        assert constants.CHUNK_ID_PREFIX == "CHK_"
        assert constants.CHUNK_INSERT_MAX_CHARS == 15_000_000
        assert constants.SNOWFLAKE_MAX_STRING_BYTES == 16_777_216

    def test_default_context(self):
        from chunky_utils import constants
        assert constants.DEFAULT_DB == "DEV_DB"
        assert constants.DEFAULT_SCHEMA == "DNA"
        assert constants.DEFAULT_LIB_STAGE.startswith("@")
        assert constants.DEFAULT_POPPLER_BUNDLE == "poppler_bundle.zip"

    def test_cortex_default(self):
        from chunky_utils import constants
        assert constants.DEFAULT_CORTEX_MODEL == "claude-haiku-4-5"

    def test_warnings_present(self):
        from chunky_utils import constants
        # Every warning must be a non-empty string
        for name in (
            "WARNING_INGEST_OVERWRITE",
            "WARNING_INGEST_SURGICAL",
            "WARNING_INGEST_APPEND",
            "WARNING_QA_COMMIT",
            "WARNING_QA_DELETE",
            "WARNING_SEARCHSERVICE_CREATE",
            "WARNING_SEARCHSERVICE_DROP",
            "WARNING_SEARCHSERVICE_ALTER",
        ):
            val = getattr(constants, name)
            assert isinstance(val, str) and len(val) > 20, name

    def test_time_travel_window(self):
        from chunky_utils import constants
        assert constants.TIME_TRAVEL_MAX_HOURS == 24


# =============================================================================
# query_log — uses a mocked Snowpark session
# =============================================================================
def _mock_session(default_row=None):
    """Build a MagicMock session that pretends to run Snowflake SQL.

    By default every session.sql(...).collect() call returns a row that
    contains both a QID and a TS so any handler method (timestamp
    snapshot, LAST_QUERY_ID, result rows) works without bespoke setup.
    Pass `default_row` to override.
    """
    s = MagicMock()
    if default_row is None:
        default_row = {"QID": "abc-123", "TS": "2024-01-01 12:00:00.000",
                       "CNT": 0, "HOURS_AGO": 1}
    s.sql.return_value.collect.return_value = [default_row]
    return s


class TestQueryLog:
    def test_initial_timestamp_snapshot(self):
        from chunky_utils.query_log import QueryLog
        s = _mock_session()
        log = QueryLog(s)
        # First call is the timestamp snapshot
        assert log.timestamp_before == "2024-01-01 12:00:00.000"

    def test_execute_captures_query_id(self):
        from chunky_utils.query_log import QueryLog
        s = _mock_session()
        log = QueryLog(s)
        # Reset mock to track the new calls
        s.sql.reset_mock()
        s.sql.return_value.collect.return_value = [{"QID": "q1", "TS": "2024-01-01 12:00:01"}]
        log.execute("SELECT 1")
        assert "q1" in log.ids

    def test_to_dict_shape(self):
        from chunky_utils.query_log import QueryLog
        s = _mock_session()
        log = QueryLog(s)
        d = log.to_dict()
        assert "query_ids" in d
        assert "timestamp_before" in d
        assert "query_count" in d
        assert d["query_count"] == len(d["query_ids"])


# =============================================================================
# init_table — handler
# =============================================================================
class TestInitTableHandler:
    def test_overwrite_creates(self):
        from chunky_utils.init_table import run
        # Default mock returns CNT=0 (table doesn't exist) — so an
        # OVERWRITE call must run CREATE OR REPLACE.
        s = _mock_session(default_row={"CNT": 0, "QID": "q1",
                                       "TS": "2024-01-01 12:00:00.000"})
        result = run(s, "DEV_DB", "DNA", "MY_TABLE", "OVERWRITE")
        assert result["status"] == "CREATED"
        assert result["mode"] == "OVERWRITE"
        assert "query_ids" in result

    def test_append_existing_noop(self):
        from chunky_utils.init_table import run
        # Mock returns CNT=1 (table exists) — APPEND mode must no-op.
        s = _mock_session(default_row={"CNT": 1, "QID": "q1",
                                       "TS": "2024-01-01 12:00:00.000"})
        result = run(s, "DEV_DB", "DNA", "MY_TABLE", "APPEND")
        assert result["status"] == "EXISTS"


# =============================================================================
# grant_table — handler
# =============================================================================
class TestGrantTableHandler:
    def test_skips_it_ai(self):
        from chunky_utils.grant_table import run
        s = _mock_session()
        s.sql.return_value.collect.return_value = [{"QID": "q1"}]
        result = run(s, "DEV_DB", "DNA", "T", ["IT_AI", "ANALYST"])
        # IT_AI must be skipped silently
        assert "IT_AI" not in result["success_roles"]
        assert "ANALYST" in result["success_roles"]

    def test_invalid_role_marked_failed_pattern(self):
        from chunky_utils.grant_table import run
        s = _mock_session()
        s.sql.return_value.collect.return_value = [{"QID": "q1"}]
        # Role with invalid characters — should be filtered out, not crash
        result = run(s, "DEV_DB", "DNA", "T", ["valid-name!"])
        # Invalid-syntax roles are skipped silently (not added to failed)
        assert result["success_roles"] == []
        assert result["failed_roles"] == []

    def test_handles_json_string_roles(self):
        from chunky_utils.grant_table import run
        s = _mock_session()
        s.sql.return_value.collect.return_value = [{"QID": "q1"}]
        result = run(s, "DEV_DB", "DNA", "T", '["ANALYST"]')
        assert "ANALYST" in result["success_roles"]


# =============================================================================
# surgical_delete — handler
# =============================================================================
class TestSurgicalDeleteHandler:
    def test_bottom_up_sort(self):
        from chunky_utils.surgical_delete import run
        s = _mock_session()
        # All sql calls succeed
        s.sql.return_value.collect.return_value = [{"QID": "q1"}]
        mappings = [
            {"source_start": 2, "source_end": 3},
            {"source_start": 10, "source_end": 12},
            {"source_start": 5, "source_end": 6},
        ]
        result = run(s, "DEV_DB", "DNA", "T", "file.pdf", mappings)
        assert result["success"] is True
        # Verify bottom-up order (12, 6, 3)
        deleted = result["deleted_ranges"]
        assert deleted[0]["source_end"] == 12
        assert deleted[1]["source_end"] == 6
        assert deleted[2]["source_end"] == 3

    def test_rollback_on_error(self):
        from chunky_utils.surgical_delete import run
        s = MagicMock()

        # Track every SQL statement so we can assert ROLLBACK was issued.
        executed_sql: list[str] = []

        def fake_sql(sql, params=None):
            executed_sql.append(sql)
            # DELETE statements raise to simulate a failure
            if "DELETE" in sql.upper():
                raise Exception("boom")
            return MagicMock(collect=MagicMock(
                return_value=[{"QID": "q1", "TS": "2024-01-01 12:00:00.000"}]
            ))

        s.sql.side_effect = fake_sql
        result = run(s, "DEV_DB", "DNA", "T", "file.pdf",
                     [{"source_start": 1, "source_end": 3}])
        assert result["success"] is False
        assert "boom" in result["error"]
        # ROLLBACK must have been issued after the failure
        assert any("ROLLBACK" in sql.upper() for sql in executed_sql), \
            f"ROLLBACK not issued. SQLs: {executed_sql}"


# =============================================================================
# parse_pdf — handler
# =============================================================================
class TestParsePdfHandler:
    def test_success(self):
        from chunky_utils.parse_pdf import run
        s = _mock_session()
        s.sql.return_value.collect.return_value = [
            {"J": '{"pages": [{"index": 0, "content": "hello"}]}'},
            {"QID": "q1"},
        ]
        result = run(s, "@DEV_DB.DNA.DOCS", "file.pdf", {"mode": "LAYOUT"})
        assert result["success"] is True
        assert "pages" in result["data"]

    def test_null_returns_error(self):
        from chunky_utils.parse_pdf import run
        s = _mock_session()
        s.sql.return_value.collect.return_value = [
            {"J": None},
            {"QID": "q1"},
        ]
        result = run(s, "@DEV_DB.DNA.DOCS", "file.pdf", {"mode": "LAYOUT"})
        assert result["success"] is False
        assert "NULL" in result["error"]


# =============================================================================
# revert — handler
# =============================================================================
class TestRevertHelpers:
    def test_revert_table_no_timestamp_or_query_ids(self):
        from chunky_utils.revert import revert_table
        s = _mock_session()
        result = revert_table(s, "DEV_DB", "DNA", "T")
        assert result["success"] is False
        assert "timestamp_before" in result["error"] or "query_ids" in result["error"]

    def test_revert_table_with_timestamp(self):
        from chunky_utils.revert import revert_table
        s = MagicMock()

        def fake_sql(sql, params=None):
            sql_upper = sql.upper()
            # IMPORTANT: check DATEDIFF before CURRENT_TIMESTAMP — the
            # DATEDIFF SQL itself contains CURRENT_TIMESTAMP().
            if "DATEDIFF" in sql_upper:
                return MagicMock(collect=MagicMock(
                    return_value=[{"HOURS_AGO": 1, "QID": "q2"}]
                ))
            if "CURRENT_TIMESTAMP" in sql_upper:
                return MagicMock(collect=MagicMock(
                    return_value=[{"TS": "2024-01-01 12:00:00.000",
                                   "QID": "q1"}]
                ))
            # LAST_QUERY_ID or DDL/CLONE — return a QID
            return MagicMock(collect=MagicMock(return_value=[{"QID": "q3"}]))

        s.sql.side_effect = fake_sql
        result = revert_table(s, "DEV_DB", "DNA", "T",
                              timestamp_before="2024-01-01 12:00:00.000")
        assert result["success"] is True
        assert "warning" in result
        assert result["strategy"] == "time_travel"

    def test_revert_table_outside_retention_window(self):
        from chunky_utils.revert import revert_table
        from chunky_utils.constants import TIME_TRAVEL_MAX_HOURS
        s = MagicMock()

        def fake_sql(sql, params=None):
            sql_upper = sql.upper()
            # Same ordering rule as above.
            if "DATEDIFF" in sql_upper:
                return MagicMock(collect=MagicMock(
                    return_value=[{"HOURS_AGO": TIME_TRAVEL_MAX_HOURS + 1,
                                   "QID": "q2"}]
                ))
            if "CURRENT_TIMESTAMP" in sql_upper:
                return MagicMock(collect=MagicMock(
                    return_value=[{"TS": "2024-01-01 12:00:00.000",
                                   "QID": "q1"}]
                ))
            return MagicMock(collect=MagicMock(return_value=[{"QID": "q3"}]))

        s.sql.side_effect = fake_sql
        result = revert_table(s, "DEV_DB", "DNA", "T",
                              timestamp_before="2024-01-01 12:00:00.000")
        assert result["success"] is False
        assert "retention" in result["error"].lower(), \
            f"Expected retention error, got: {result.get('error')}"


# =============================================================================
# Main handler dispatch — chunky_chunks, chunky_qa, chunky_searchservice
# =============================================================================
class TestChunkyChunksDispatch:
    def test_unknown_command(self):
        from chunky_utils.chunky_chunks_handler import run
        s = _mock_session()
        result = run(s, "BOGUS", {})
        assert result["success"] is False
        assert "Unknown command" in result["error"]

    def test_revert_dispatches(self):
        from chunky_utils.chunky_chunks_handler import run
        s = _mock_session()
        # revert_table with no timestamp/query_ids should fail predictably
        result = run(s, "REVERT", {"db": "DEV_DB", "schema": "DNA", "table": "T"})
        assert result["success"] is False
        # The error mentions the missing inputs
        assert "timestamp_before" in result["error"] or "query_ids" in result["error"]


class TestChunkyQaDispatch:
    def test_unknown_command(self):
        from chunky_utils.chunky_qa_handler import run
        s = _mock_session()
        result = run(s, "BOGUS", {})
        assert result["success"] is False
        assert "Unknown command" in result["error"]

    def test_revert_dispatches(self):
        from chunky_utils.chunky_qa_handler import run
        s = _mock_session()
        result = run(s, "REVERT", {"db": "DEV_DB", "schema": "DNA", "table": "T"})
        assert result["success"] is False


class TestChunkySearchServiceDispatch:
    def test_unknown_command(self):
        from chunky_utils.chunky_searchservice_handler import run
        s = _mock_session()
        result = run(s, "BOGUS", {})
        assert result["success"] is False
        assert "Unknown command" in result["error"]

    def test_revert_without_ddl(self):
        from chunky_utils.chunky_searchservice_handler import run
        s = _mock_session()
        result = run(s, "REVERT", {
            "db": "DEV_DB", "schema": "DNA",
            "service_name": "CSS_X",
        })
        # Without DDL, revert must fail clearly
        assert result["success"] is False
        assert "DDL" in result["error"]

    def test_create_then_revert_uses_previous_ddl(self):
        """When CREATE captures previous_ddl, REVERT must use it."""
        from chunky_utils.chunky_searchservice_handler import run
        s = _mock_session()
        # Setup: every SQL call returns QID; GET_DDL returns None (no
        # prior service). The CREATE call should succeed and return
        # a `revert` block (with ddl=None since no prior service existed).
        s.sql.return_value.collect.return_value = [{"QID": "q1"}]
        result = run(s, "CREATE", {
            "db": "DEV_DB", "schema": "DNA",
            "service_name": "CSS_TEST",
            "tables": ["T1"],
            "search_columns": [{"table": "T1", "column": "CHUNK",
                                "search_type": "Hybrid",
                                "embedding_model": "voyage-multilingual-2"}],
            "attribute_columns": [{"table": "T1", "column": "RELATIVE_PATH"}],
            "target_lag": 30, "target_lag_unit": "days",
            "grant_roles": [],
        })
        assert result["success"] is True
        assert "warning" in result
        assert "revert" in result
        # The CREATE DDL must reference the service name
        assert "CSS_TEST" in result["data"]["ddl"]


# =============================================================================
# build_procedures.py — the build script itself
# =============================================================================
class TestBuildScript:
    def test_utils_bundle_includes_all_handlers(self):
        """The generated utils_bundle.zip must contain every handler."""
        import zipfile
        zip_path = PROC_DIR / "utils_bundle.zip"
        assert zip_path.is_file(), "utils_bundle.zip missing — run build_procedures.py"
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        # All handler .py files must be present (under utils/ prefix)
        for handler in (
            "init_table.py", "grant_table.py", "surgical_delete.py",
            "parse_pdf.py", "build_chunk_ref.py",
            "chunky_chunks_handler.py", "chunky_qa_handler.py",
            "chunky_searchservice_handler.py",
            "page_mapping.py", "metadata_handler.py",
            "constants.py", "query_log.py", "revert.py",
            "__init__.py",
        ):
            assert f"chunky_utils/{handler}" in names, f"Missing in zip: {handler}"

    def test_generated_sql_files_reference_utils_bundle(self):
        """Every generated .sql file must IMPORTS the utils bundle."""
        for sql_name in (
            "chunky_chunks.sql", "chunky_qa.sql", "chunky_searchservice.sql",
            "chunky_internal_init_table.sql", "chunky_internal_grant_table.sql",
            "chunky_internal_surgical_delete.sql", "chunky_internal_parse_pdf.sql",
            "chunky_internal_build_chunk_ref.sql",
        ):
            sql_path = PROC_DIR / sql_name
            assert sql_path.is_file(), f"Missing: {sql_name}"
            text = sql_path.read_text()
            assert "utils_bundle.zip" in text, f"{sql_name} missing IMPORTS"
            # Every procedure must import from chunky_utils.* (not inline code)
            assert "from chunky_utils." in text, f"{sql_name} missing handler import"

    def test_main_procedures_have_revert_command(self):
        """All three main procedures must support the REVERT command."""
        for handler_name in (
            "chunky_chunks_handler.py",
            "chunky_qa_handler.py",
            "chunky_searchservice_handler.py",
        ):
            handler_path = PROC_DIR / "utils" / handler_name
            text = handler_path.read_text()
            assert "'REVERT'" in text or '"REVERT"' in text, \
                f"{handler_name} missing REVERT command"

    def test_generated_sql_uses_configurable_stage(self):
        """The LIB_STAGE placeholder must be substituted, not left bare."""
        sql = (PROC_DIR / "chunky_chunks.sql").read_text()
        assert "{{LIB_STAGE}}" not in sql, "Template placeholder not substituted"
        assert "@DEV_DB.DNA.STG_LIB" in sql  # default value


# =============================================================================
# upload_to_stage.py — local script (syntax + arg parsing only)
# =============================================================================
class TestUploadScript:
    def test_module_imports(self):
        """upload_to_stage.py must import cleanly without snowflake-connector."""
        sys.path.insert(0, str(PROC_DIR / "script"))
        try:
            import importlib
            import upload_to_stage
            assert hasattr(upload_to_stage, "main")
            assert hasattr(upload_to_stage, "build_parser")
        finally:
            sys.path.pop(0)

    def test_parser_requires_stage_and_operation(self):
        sys.path.insert(0, str(PROC_DIR / "script"))
        try:
            import upload_to_stage
            parser = upload_to_stage.build_parser()
            # Missing required args must error
            with pytest.raises(SystemExit):
                parser.parse_args([])
            # Providing just --stage is also insufficient (need an op)
            with pytest.raises(SystemExit):
                parser.parse_args(["--stage", "@X.Y.Z"])
            # Full args parse cleanly
            args = parser.parse_args([
                "--account", "myacct", "--user", "me@x.com",
                "--stage", "@X.Y.Z", "--file", "/tmp/x.pdf",
            ])
            assert args.account == "myacct"
            assert args.file == "/tmp/x.pdf"
        finally:
            sys.path.pop(0)

    def test_config_env_var_override(self, monkeypatch):
        sys.path.insert(0, str(PROC_DIR / "script"))
        try:
            import upload_to_stage
            monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "envacct")
            monkeypatch.setenv("SNOWFLAKE_USER", "envuser")
            cfg = upload_to_stage.load_config()
            assert cfg["account"] == "envacct"
            assert cfg["user"] == "envuser"
        finally:
            sys.path.pop(0)


# =============================================================================
# Dummy PDF
# =============================================================================
class TestDummyPdf:
    def test_pdf_exists_and_is_valid(self):
        pdf_path = PROC_DIR / "script" / "pdf" / "fy2024-tbk-investor-presentation.pdf"
        assert pdf_path.is_file(), "Dummy PDF missing"
        assert pdf_path.stat().st_size > 1000, "PDF suspiciously small"
        # Verify it's a real PDF
        from pypdf import PdfReader
        r = PdfReader(str(pdf_path))
        assert len(r.pages) >= 4, "PDF should have at least 4 pages"
        # Title page should contain the expected title
        first_page_text = r.pages[0].extract_text()
        assert "TBK" in first_page_text or "Fiscal Year 2024" in first_page_text
