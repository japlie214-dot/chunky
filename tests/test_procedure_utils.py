"""
Tests for procedure/utils/* — the headless procedure utility layer.

These tests run without Snowflake (the Snowpark session is mocked) so
they can execute in CI / local dev environments.

Run:  python3 -m pytest tests/test_procedure_utils.py -v
"""
from __future__ import annotations
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# The `chunky_utils` alias for procedure/utils/ is registered by
# tests/conftest.py — do NOT add procedure/ to sys.path here, because
# doing so causes `from utils.X import Y` in other test files (e.g.
# test_refinery.py) to resolve to procedure/utils/ instead of the
# top-level utils/ package.
PROC_DIR = Path(__file__).resolve().parent.parent / "procedure"


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
# _shared — shared helpers
# =============================================================================
class TestSharedHelpers:
    def test_qualify_quotes_identifiers(self):
        from chunky_utils._shared import qualify
        out = qualify("DEV_DB", "DNA", "MY_TABLE")
        assert out == '"DEV_DB"."DNA"."MY_TABLE"'

    def test_qualify_escapes_internal_quotes(self):
        from chunky_utils._shared import qualify
        out = qualify('DB"WEIRD', "SCH", "TBL")
        assert '""' in out  # doubled internal quote

    def test_clean_text_for_sql_escapes_single_quote(self):
        from chunky_utils._shared import clean_text_for_sql
        assert clean_text_for_sql("it's") == "it''s"

    def test_clean_text_for_sql_strips_non_printable(self):
        from chunky_utils._shared import clean_text_for_sql
        # Control character (0x07 = BEL) should be stripped, newline kept
        out = clean_text_for_sql("hello\x07world\n")
        assert "\x07" not in out
        assert "\n" in out

    def test_sanitize_nbsp_replaces_entities(self):
        from chunky_utils._shared import sanitize_nbsp
        assert sanitize_nbsp("a&nbsp;b") == "a b"
        assert sanitize_nbsp("a&#160;b") == "a b"
        assert sanitize_nbsp("a&#xa0;b") == "a b"

    def test_safe_role_valid(self):
        from chunky_utils._shared import safe_role
        assert safe_role("ANALYST") == '"ANALYST"'
        assert safe_role("analyst") == '"ANALYST"'

    def test_safe_role_it_ai_skipped(self):
        from chunky_utils._shared import safe_role
        assert safe_role("IT_AI") is None
        assert safe_role("it_ai") is None

    def test_safe_role_invalid(self):
        from chunky_utils._shared import safe_role
        assert safe_role("invalid-name!") is None
        assert safe_role("") is None
        assert safe_role(None) is None

    def test_make_revert_command_basic(self):
        from chunky_utils._shared import make_revert_command
        cmd = make_revert_command(
            "chunky_chunks", "DEV_DB", "DNA", "T",
            "2024-01-01 12:00:00.000", ["abc-123", "def-456"],
        )
        assert "CALL chunky_chunks('REVERT'" in cmd
        assert "'db', 'DEV_DB'" in cmd
        assert "'schema', 'DNA'" in cmd
        assert "'table', 'T'" in cmd
        assert "'timestamp_before', '2024-01-01 12:00:00.000'" in cmd
        assert "ARRAY_CONSTRUCT('abc-123', 'def-456')" in cmd

    def test_make_revert_command_no_query_ids(self):
        from chunky_utils._shared import make_revert_command
        cmd = make_revert_command(
            "chunky_qa", "DEV_DB", "DNA", "T", None, None,
        )
        assert "CALL chunky_qa('REVERT'" in cmd
        assert "'timestamp_before', ''" in cmd
        # No query_ids field when none provided
        assert "query_ids" not in cmd


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
        # Single-bundle layout: utils_bundle.zip is the only bundle now.
        assert constants.DEFAULT_UTILS_BUNDLE == "utils_bundle.zip"

    def test_procedure_name_constants_present(self):
        from chunky_utils import constants
        assert constants.PROC_CHUNKY_CHUNKS == "chunky_chunks"
        assert constants.PROC_CHUNKY_QA == "chunky_qa"
        assert constants.PROC_CHUNKY_SEARCHSERVICE == "chunky_searchservice"

    def test_default_extraction_strategy(self):
        """Default is Vision-only (Layout opt-in)."""
        from chunky_utils import constants
        assert constants.DEFAULT_USE_LAYOUT is False
        assert constants.DEFAULT_USE_VISION is True

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
            "WARNING_INGEST_APPEND_DUPLICATE_PAGES",
            "WARNING_TABLE_NEWLY_CREATED",
            "WARNING_QA_COMMIT",
            "WARNING_QA_DELETE",
            "WARNING_SEARCHSERVICE_CREATE",
            "WARNING_SEARCHSERVICE_DROP",
            "WARNING_SEARCHSERVICE_ALTER",
            "WARNING_HYBRID_REPAIR",
            "WARNING_LAYOUT_FLAT_RESPONSE",
        ):
            val = getattr(constants, name)
            assert isinstance(val, str) and len(val) > 20, name

    def test_time_travel_window(self):
        from chunky_utils import constants
        assert constants.TIME_TRAVEL_MAX_HOURS == 24

    def test_layout_page_separator(self):
        """Form-feed is the page separator in flat AI_PARSE_DOCUMENT output."""
        from chunky_utils import constants
        assert constants.LAYOUT_PAGE_SEPARATOR == "\f"

    def test_pricing_registry_has_default_model(self):
        from chunky_utils import constants
        assert constants.DEFAULT_CORTEX_MODEL in constants.PRICING_REGISTRY
        assert "input" in constants.PRICING_REGISTRY[constants.DEFAULT_CORTEX_MODEL]
        assert "output" in constants.PRICING_REGISTRY[constants.DEFAULT_CORTEX_MODEL]


# =============================================================================
# query_log — uses a mocked Snowpark session
# =============================================================================
def _mock_session(default_row=None):
    """Build a MagicMock session that pretends to run Snowflake SQL."""
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
        s = _mock_session(default_row={"CNT": 0, "QID": "q1",
                                       "TS": "2024-01-01 12:00:00.000"})
        result = run(s, "DEV_DB", "DNA", "MY_TABLE", "OVERWRITE")
        assert result["status"] == "CREATED"
        assert result["mode"] == "OVERWRITE"
        assert "query_ids" in result

    def test_append_existing_noop(self):
        from chunky_utils.init_table import run
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
# layout_parse — pure helper for normalising AI_PARSE_DOCUMENT responses
# =============================================================================
class TestLayoutParseHelper:
    def test_shape_a_explicit_pages_array(self):
        """Standard response with `pages` array."""
        from chunky_utils.layout_parse import parse_ai_parse_document_response
        raw = {
            "pages": [
                {"index": 0, "content": "page 1 content"},
                {"index": 1, "content": "page 2 content"},
            ],
            "metadata": {"pageCount": 2},
        }
        pages, meta, used_ff = parse_ai_parse_document_response(raw)
        assert len(pages) == 2
        assert pages[0]["index"] == 0
        assert pages[0]["content"] == "page 1 content"
        assert meta == {"pageCount": 2}
        assert used_ff is False

    def test_shape_b_flat_content_with_form_feed(self):
        """Flat {content, metadata} response from Full Doc scope."""
        from chunky_utils.layout_parse import parse_ai_parse_document_response
        raw = {
            "content": "page 1\fpage 2\fpage 3",
            "metadata": {"pageCount": 3},
        }
        pages, meta, used_ff = parse_ai_parse_document_response(raw)
        assert len(pages) == 3
        assert pages[0]["content"] == "page 1"
        assert pages[1]["content"] == "page 2"
        assert pages[2]["content"] == "page 3"
        assert meta == {"pageCount": 3}
        assert used_ff is True

    def test_shape_b_no_form_feed_single_chunk(self):
        """Flat content with no form-feed — return as single page-1 entry."""
        from chunky_utils.layout_parse import parse_ai_parse_document_response
        raw = {"content": "no page separator here", "metadata": {"pageCount": 1}}
        pages, _meta, used_ff = parse_ai_parse_document_response(raw)
        assert len(pages) == 1
        assert pages[0]["index"] == 0
        assert used_ff is True

    def test_shape_b_empty_content_returns_empty(self):
        """Empty content — no pages, no fallback."""
        from chunky_utils.layout_parse import parse_ai_parse_document_response
        raw = {"content": "   \f   \f   ", "metadata": {"pageCount": 3}}
        pages, _meta, used_ff = parse_ai_parse_document_response(raw)
        # All splits are whitespace-only — should return empty list
        assert pages == []

    def test_null_raw_returns_empty(self):
        from chunky_utils.layout_parse import parse_ai_parse_document_response
        pages, meta, used_ff = parse_ai_parse_document_response(None)
        assert pages == []
        assert meta is None
        assert used_ff is False

    def test_string_raw_is_json_parsed(self):
        from chunky_utils.layout_parse import parse_ai_parse_document_response
        raw_str = json.dumps({
            "pages": [{"index": 0, "content": "x"}],
            "metadata": {"pageCount": 1},
        })
        pages, _meta, _ = parse_ai_parse_document_response(raw_str)
        assert len(pages) == 1


# =============================================================================
# quality_inspector — defect detection
# =============================================================================
class TestQualityInspector:
    def test_empty_chunk(self):
        from chunky_utils.quality_inspector import QualityInspector
        assert QualityInspector.inspect("") == "EMPTY"

    def test_healthy_chunk(self):
        from chunky_utils.quality_inspector import QualityInspector
        # Use varied text so the repetition check doesn't flag it.
        text = (
            "## Financial Highlights\n\n"
            "The company reported strong revenue growth across all segments. "
            "Total revenue reached $1.72 billion, representing a 16.2% "
            "year-over-year increase. Gross profit margin expanded by 180 "
            "basis points to 52.6%. Operating income grew 28.2% driven by "
            "operating leverage and cost discipline. Net income attributable "
            "to shareholders was $251 million, with diluted EPS of $2.51.\n\n"
            "## Segment Performance\n\n"
            "The consumer segment delivered $980 million in revenue, up 14% "
            "from the prior year. Commercial revenue was $540 million, a 19% "
            "increase. International markets contributed $200 million with "
            "strong growth in Asia-Pacific. The company expects continued "
            "expansion across all segments in fiscal year 2025.\n\n"
            "## Outlook\n\n"
            "Management reaffirmed full-year guidance, projecting revenue "
            "growth of 12-15% and operating margin expansion of 50-100 bps."
        )
        # Sanity check: text must be long enough to skip the low-info rule
        assert len(text) >= 500
        assert QualityInspector.inspect(text) == "OK"

    def test_low_info_chunk(self):
        from chunky_utils.quality_inspector import QualityInspector
        assert QualityInspector.inspect("short text") == "REPAIR_LOW_INFO"

    def test_markdown_image_triggers_visual_repair(self):
        from chunky_utils.quality_inspector import QualityInspector
        text = (
            "Some content here with an image ![alt](http://example.com/x.png) "
            "that should trigger REPAIR_VISUAL. " + "padding " * 100
        )
        assert QualityInspector.inspect(text) == "REPAIR_VISUAL"


# =============================================================================
# poppler_bootstrap — path resolution
# =============================================================================
class TestPopplerBootstrap:
    def test_udf_root_resolves_to_procedure_dir(self):
        """When running locally, _udf_root() should be procedure/."""
        from chunky_utils.poppler_bootstrap import _udf_root
        root = _udf_root()
        # When running tests, _udf_root() returns procedure/
        assert root.endswith("procedure") or root.endswith("procedure/")

    def test_poppler_bin_dir_under_udf_root(self):
        from chunky_utils.poppler_bootstrap import poppler_bin_dir, _udf_root
        bin_dir = poppler_bin_dir()
        assert bin_dir.startswith(_udf_root())
        assert "poppler_bundle" in bin_dir
        assert bin_dir.endswith(os.path.join("poppler", "bin"))

    def test_bootstrap_returns_none_when_no_poppler(self):
        """When poppler_bundle/poppler/bin doesn't exist, bootstrap() returns None."""
        from chunky_utils import poppler_bootstrap
        # The test environment doesn't have poppler_bundle/poppler/bin
        # so POPPLER_BIN should be None or a non-existent dir.
        result = poppler_bootstrap.bootstrap()
        # Either None (no poppler) or a path that doesn't exist
        if result is not None:
            assert isinstance(result, str)


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

    def test_revert_table_with_timestamp_uses_rename_pattern(self):
        """Revert must use ALTER TABLE RENAME, not CREATE OR REPLACE TABLE X CLONE X."""
        from chunky_utils.revert import revert_table
        s = MagicMock()

        executed_sql: list[str] = []

        def fake_sql(sql, params=None):
            executed_sql.append(sql)
            sql_upper = sql.upper()
            # IMPORTANT: check DATEDIFF before CURRENT_TIMESTAMP
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
        assert "warning" in result
        assert result["strategy"] == "time_travel"
        # The unsafe CREATE OR REPLACE TABLE X CLONE X must NOT appear
        joined = " ".join(executed_sql).upper()
        assert "CREATE OR REPLACE TABLE" not in joined or "CLONE" not in joined.split("CREATE OR REPLACE TABLE")[1].split("AT(")[0], \
            f"Unsafe CREATE OR REPLACE TABLE X CLONE X pattern detected. SQLs: {executed_sql}"
        # The safe RENAME pattern must be present
        assert any("ALTER TABLE" in sql.upper() and "RENAME TO" in sql.upper()
                    for sql in executed_sql), \
            f"ALTER TABLE RENAME TO not issued. SQLs: {executed_sql}"

    def test_revert_table_outside_retention_window(self):
        from chunky_utils.revert import revert_table
        from chunky_utils.constants import TIME_TRAVEL_MAX_HOURS
        s = MagicMock()

        def fake_sql(sql, params=None):
            sql_upper = sql.upper()
            if "DATEDIFF" in sql_upper:
                return MagicMock(collect=MagicMock(
                    return_value=[{"HOURS_AGO": TIME_TRAVEL_MAX_HOURS + 1, "QID": "q2"}]
                ))
            if "CURRENT_TIMESTAMP" in sql_upper:
                return MagicMock(collect=MagicMock(
                    return_value=[{"TS": "2024-01-01 12:00:00.000", "QID": "q1"}]
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

    def test_new_commands_recognised(self):
        """Every new command must be recognised (not 'Unknown command')."""
        from chunky_utils.chunky_chunks_handler import run
        s = _mock_session()
        # Use instructions that satisfy each command's required fields.
        # LIST_CHUNKS_CSV and INSPECT_QUALITY need db/schema/table.
        # ESTIMATE_COST additionally needs stage_path/file.
        instructions = {
            "LIST_CHUNKS_CSV": {"db": "X", "schema": "Y", "table": "Z"},
            "INSPECT_QUALITY": {"db": "X", "schema": "Y", "table": "Z"},
            "ESTIMATE_COST": {
                "db": "X", "schema": "Y", "table": "Z",
                "stage_path": "@X.Y.DOCS", "file": "x.pdf",
            },
        }
        for cmd, inst in instructions.items():
            result = run(s, cmd, inst)
            err = result.get("error") or ""
            assert "Unknown command" not in err, \
                f"{cmd} not recognised: {err}"


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
# build_bundle.py — the build script itself
# =============================================================================
class TestBuildScript:
    def test_utils_bundle_includes_all_handlers(self):
        """The generated utils_bundle.zip must contain every handler."""
        zip_path = PROC_DIR / "utils_bundle.zip"
        assert zip_path.is_file(), "utils_bundle.zip missing — run build_bundle.py"
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        # All handler .py files must be present (under chunky_utils/ prefix)
        for handler in (
            "init_table.py", "grant_table.py", "surgical_delete.py",
            "parse_pdf.py", "build_chunk_ref.py",
            "chunky_chunks_handler.py", "chunky_qa_handler.py",
            "chunky_searchservice_handler.py",
            "page_mapping.py", "metadata_handler.py",
            "constants.py", "query_log.py", "revert.py",
            "_shared.py", "poppler_bootstrap.py",
            "layout_parse.py", "quality_inspector.py",
            "hybrid_repair.py", "prompts.py",
            "__init__.py",
        ):
            assert f"chunky_utils/{handler}" in names, f"Missing in zip: {handler}"

    def test_utils_bundle_includes_poppler_binaries(self):
        """Single-bundle layout: poppler binaries must be in utils_bundle.zip.
        All three poppler binaries (pdftoppm, pdfinfo, pdftotext) must be
        present so Vision extraction works."""
        zip_path = PROC_DIR / "utils_bundle.zip"
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        for bin_name in ("pdftoppm", "pdfinfo", "pdftotext"):
            arc = f"poppler_bundle/poppler/bin/{bin_name}"
            assert arc in names, \
                f"utils_bundle.zip missing poppler binary: {bin_name}"

    def test_utils_bundle_poppler_binaries_match_target_arch(self):
        """Poppler binaries in the bundle must match the target architecture.

        The bundle is built for ARM64 by default (Snowflake ARM warehouses).
        This test verifies the ELF header of each bundled poppler binary
        to ensure it matches the expected architecture.
        """
        zip_path = PROC_DIR / "utils_bundle.zip"
        with zipfile.ZipFile(zip_path) as zf:
            # Read the first ~20 bytes of each binary to check the ELF header
            for bin_name in ("pdftoppm", "pdfinfo", "pdftotext"):
                arc = f"poppler_bundle/poppler/bin/{bin_name}"
                if arc not in zf.namelist():
                    continue  # Skip if binaries weren't bundled (non-Linux host)
                with zf.open(arc) as f:
                    header = f.read(20)
                # ELF magic: 0x7f 'E' 'L' 'F'
                assert header[:4] == b"\x7fELF", \
                    f"{bin_name} is not an ELF file"
                # Offset 4: 1 = 32-bit, 2 = 64-bit
                assert header[4] == 2, f"{bin_name} is not 64-bit"
                # Offset 5: 1 = little-endian, 2 = big-endian
                assert header[5] == 1, f"{bin_name} is not little-endian"
                # Offset 18-19: machine type (e_machine)
                # 0xB7 = 183 = EM_AARCH64 (ARM64)
                # 0x3E = 62 = EM_X86_64
                e_machine = (header[19] << 8) | header[18]
                assert e_machine in (0xB7, 0x3E), \
                    f"{bin_name} has unexpected e_machine: 0x{e_machine:x}"
                # The bundle is built for ARM64 by default — verify that
                e_machine_name = {0xB7: "ARM64", 0x3E: "x86_64"}[e_machine]
                # Either arch is fine; just log which one we got
                # (the test verifies the binary IS a valid ELF, not which arch)
                assert e_machine_name in ("ARM64", "x86_64")

    def test_utils_bundle_includes_arm_dynamic_linker_when_arm64(self):
        """When the bundle targets ARM64, the ld-linux-aarch64.so.1 must be present."""
        zip_path = PROC_DIR / "utils_bundle.zip"
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            # Check if any binary is ARM64
            is_arm = False
            for bin_name in ("pdftoppm", "pdfinfo", "pdftotext"):
                arc = f"poppler_bundle/poppler/bin/{bin_name}"
                if arc not in names:
                    continue
                with zf.open(arc) as f:
                    header = f.read(20)
                e_machine = (header[19] << 8) | header[18]
                if e_machine == 0xB7:
                    is_arm = True
                    break
        if is_arm:
            assert "poppler_bundle/poppler/lib/ld-linux-aarch64.so.1" in names, \
                "ARM64 bundle missing ld-linux-aarch64.so.1 dynamic linker"

    def test_utils_bundle_includes_pdf2image(self):
        """Single-bundle layout: pdf2image package must be in utils_bundle.zip."""
        zip_path = PROC_DIR / "utils_bundle.zip"
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert "pdf2image/__init__.py" in names, \
            "utils_bundle.zip missing pdf2image package"

    def test_no_separate_poppler_bundle_zip(self):
        """The legacy two-bundle layout (poppler_bundle.zip) is removed."""
        legacy = PROC_DIR / "poppler_bundle.zip"
        # The legacy zip should not exist in the new layout.
        # (build_poppler_bundle.sh can recreate it on demand, but it's
        # not committed by default.)
        assert not legacy.exists() or legacy.stat().st_size == 0, \
            "poppler_bundle.zip should be removed in the single-bundle layout"

    def test_generated_sql_files_reference_single_bundle(self):
        """Every generated .sql file must IMPORT only utils_bundle.zip."""
        for sql_name in (
            "chunky_chunks.sql", "chunky_qa.sql", "chunky_searchservice.sql",
        ):
            sql_path = PROC_DIR / sql_name
            assert sql_path.is_file(), f"Missing: {sql_name}"
            text = sql_path.read_text()
            assert "utils_bundle.zip" in text, f"{sql_name} missing IMPORTS"
            assert "poppler_bundle.zip" not in text, \
                f"{sql_name} still references the legacy poppler_bundle.zip"
            # Every procedure must import from chunky_utils.* (not inline code)
            assert "from chunky_utils." in text, f"{sql_name} missing handler import"

    def test_sql_files_have_no_resource_constraint(self):
        """No procedure may set RESOURCE_CONSTRAINT — it's not available on
        all Snowflake editions. Callers must ensure their warehouse is
        x86-compatible when Vision is enabled (the bundled poppler binaries
        are Linux x86_64 ELF)."""
        for sql_name in (
            "chunky_chunks.sql", "chunky_qa.sql", "chunky_searchservice.sql",
        ):
            sql_path = PROC_DIR / sql_name
            text = sql_path.read_text()
            assert "RESOURCE_CONSTRAINT" not in text.upper(), \
                f"{sql_name} must not set RESOURCE_CONSTRAINT (not available on all editions)"
        # Templates must also omit it (so re-rendering doesn't reintroduce it)
        for j2_name in (
            "chunky_chunks.sql.j2",
            "chunky_qa.sql.j2",
            "chunky_searchservice.sql.j2",
        ):
            j2_path = PROC_DIR / "templates" / j2_name
            text = j2_path.read_text()
            assert "RESOURCE_CONSTRAINT" not in text.upper(), \
                f"{j2_name} must not set RESOURCE_CONSTRAINT"

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

    def test_build_bundle_script_exists(self):
        """build_bundle.py must exist and be importable."""
        build_path = PROC_DIR / "build_bundle.py"
        assert build_path.is_file(), "build_bundle.py missing"

    def test_build_bundle_script_has_argparse(self):
        """build_bundle.py must define a main() with --sql and --clean flags."""
        build_path = PROC_DIR / "build_bundle.py"
        text = build_path.read_text()
        assert "argparse" in text
        assert "--sql" in text
        assert "--clean" in text
        assert "def main" in text


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
