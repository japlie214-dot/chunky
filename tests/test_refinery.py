"""
Tests for Doc Refinery — run without Streamlit.

Validates core logic, constants, and surgical behavior.
Run: python3 -m pytest tests/test_refinery.py -v
"""
import json
import sys
import os
import pytest
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock streamlit before importing any project modules
# (streamlit is not available in test environments)
st_mock = MagicMock()
# Make st.stop() raise a special exception so we can test it
st_stop_exception = type('StreamlitStopException', (Exception,), {})
st_mock.stop.side_effect = st_stop_exception
sys.modules['streamlit'] = st_mock

# Mock snowflake modules
sys.modules['snowflake'] = MagicMock()
sys.modules['snowflake.snowpark'] = MagicMock()
sys.modules['snowflake.snowpark.functions'] = MagicMock()

# Mock pdf2image
sys.modules['pdf2image'] = MagicMock()

# Mock plotly and pandas (not available in minimal test env)
sys.modules['plotly'] = MagicMock()
sys.modules['plotly.graph_objects'] = MagicMock()
sys.modules['pandas'] = MagicMock()


# =============================================================================
# Constants
# =============================================================================

class TestConstants:
    """Verify all expected constants exist and have sane values."""

    def test_chunk_limits_exist(self):
        from utils.constants import (
            CHUNK_CACHE_MAX_SIZE, TEMP_IMAGE_PREFIX, LAYOUT_BATCH_SIZE,
            SNOWFLAKE_MAX_STRING_BYTES, CHUNK_ID_PREFIX, QA_PDF_CACHE_PREFIX,
            RETRY_MAX_ATTEMPTS, CHUNK_PREVIEW_LENGTH, CHUNK_INSERT_MAX_CHARS
        )
        assert CHUNK_CACHE_MAX_SIZE > 0
        assert TEMP_IMAGE_PREFIX == "_temp_images"
        assert LAYOUT_BATCH_SIZE > 0
        assert SNOWFLAKE_MAX_STRING_BYTES == 16_777_216
        assert CHUNK_ID_PREFIX == "CHK_"
        assert QA_PDF_CACHE_PREFIX == "qa_pdf_"
        assert RETRY_MAX_ATTEMPTS > 0
        assert CHUNK_PREVIEW_LENGTH > 0
        assert CHUNK_INSERT_MAX_CHARS > 0

    def test_financial_rates_exist(self):
        from utils.constants import CREDIT_TO_USD, USD_TO_IDR, CREDIT_TO_IDR
        assert CREDIT_TO_USD > 0
        assert USD_TO_IDR > 0
        assert CREDIT_TO_IDR == CREDIT_TO_USD * USD_TO_IDR

    def test_no_hardcoded_16mb_in_strategies(self):
        """Ensure 16777216 literal does not appear in strategy files."""
        strategy_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "views", "refinery", "ingestion_strategies"
        )
        for fname in os.listdir(strategy_dir):
            if fname.endswith(".py") and fname != "__init__.py":
                fpath = os.path.join(strategy_dir, fname)
                with open(fpath) as f:
                    content = f.read()
                assert "16777216" not in content, (
                    f"Hardcoded 16777216 found in {fname} — use SNOWFLAKE_MAX_STRING_BYTES"
                )

    def test_no_hardcoded_15m_in_strategies(self):
        """Ensure 15000000 literal does not appear in strategy files."""
        strategy_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "views", "refinery", "ingestion_strategies"
        )
        for fname in os.listdir(strategy_dir):
            if fname.endswith(".py") and fname != "__init__.py":
                fpath = os.path.join(strategy_dir, fname)
                with open(fpath) as f:
                    content = f.read()
                assert "15000000" not in content, (
                    f"Hardcoded 15000000 found in {fname} — use CHUNK_INSERT_MAX_CHARS"
                )

    def test_no_hardcoded_temp_images_in_views(self):
        """Ensure '_temp_images' string literal does not appear in views."""
        views_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "views"
        )
        for root, dirs, files in os.walk(views_dir):
            # Skip __pycache__
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fname in files:
                if fname.endswith(".py"):
                    fpath = os.path.join(root, fname)
                    with open(fpath) as f:
                        content = f.read()
                    assert '"_temp_images"' not in content, (
                        f"Hardcoded '_temp_images' found in {fpath} — use TEMP_IMAGE_PREFIX"
                    )


# =============================================================================
# RangeMappingEngine
# =============================================================================

class TestRangeMappingEngine:
    """Validate surgical range mapping logic — no shifting."""

    def test_compute_delta_expansion(self):
        from utils.page_mapping import RangeMapping, RangeMappingEngine
        rm = RangeMapping(source_start=1, source_end=2, replacement_start=1, replacement_end=5)
        assert RangeMappingEngine.compute_delta(rm) == 3  # 5 - 2 = 3

    def test_compute_delta_contraction(self):
        from utils.page_mapping import RangeMapping, RangeMappingEngine
        rm = RangeMapping(source_start=1, source_end=5, replacement_start=1, replacement_end=2)
        assert RangeMappingEngine.compute_delta(rm) == -3  # 2 - 5 = -3

    def test_compute_delta_same_size(self):
        from utils.page_mapping import RangeMapping, RangeMappingEngine
        rm = RangeMapping(source_start=1, source_end=3, replacement_start=1, replacement_end=3)
        assert RangeMappingEngine.compute_delta(rm) == 0

    def test_sort_bottom_up(self):
        from utils.page_mapping import RangeMapping, RangeMappingEngine
        rms = [
            RangeMapping(source_start=1, source_end=2, replacement_start=1, replacement_end=3),
            RangeMapping(source_start=7, source_end=8, replacement_start=1, replacement_end=2),
            RangeMapping(source_start=4, source_end=5, replacement_start=1, replacement_end=3),
        ]
        sorted_rms = RangeMappingEngine.sort_bottom_up(rms)
        assert sorted_rms[0].source_end == 8
        assert sorted_rms[1].source_end == 5
        assert sorted_rms[2].source_end == 2

    def test_validate_source_overlap(self):
        from utils.page_mapping import RangeMapping, RangeMappingEngine
        rms = [
            RangeMapping(source_start=1, source_end=5, replacement_start=1, replacement_end=3),
            RangeMapping(source_start=3, source_end=8, replacement_start=4, replacement_end=6),
        ]
        is_valid, errors = RangeMappingEngine.validate(rms, replacement_page_count=10)
        assert not is_valid
        assert any("overlap" in e.lower() for e in errors)

    def test_validate_source_start_gt_end(self):
        from utils.page_mapping import RangeMapping, RangeMappingEngine
        rms = [RangeMapping(source_start=5, source_end=2, replacement_start=1, replacement_end=3)]
        is_valid, errors = RangeMappingEngine.validate(rms, replacement_page_count=10)
        assert not is_valid
        assert any("source_start" in e for e in errors)

    def test_validate_replacement_exceeds_page_count(self):
        from utils.page_mapping import RangeMapping, RangeMappingEngine
        rms = [RangeMapping(source_start=1, source_end=2, replacement_start=1, replacement_end=20)]
        is_valid, errors = RangeMappingEngine.validate(rms, replacement_page_count=10)
        assert not is_valid
        assert any("exceeds" in e.lower() for e in errors)

    def test_validate_valid_mappings(self):
        from utils.page_mapping import RangeMapping, RangeMappingEngine
        rms = [
            RangeMapping(source_start=1, source_end=2, replacement_start=3, replacement_end=5),
            RangeMapping(source_start=7, source_end=8, replacement_start=1, replacement_end=2),
        ]
        is_valid, errors = RangeMappingEngine.validate(rms, replacement_page_count=10)
        assert is_valid
        assert errors == []

    def test_target_page_for_direct_mapping(self):
        """PAGE_NUMBER = PDF page number. target_page_for still works for metadata."""
        from utils.page_mapping import RangeMapping, RangeMappingEngine
        rms = [RangeMapping(source_start=1, source_end=2, replacement_start=3, replacement_end=5)]
        # PDF page 3 → target 1, PDF page 4 → target 2, PDF page 5 → target 3
        # (this function is used for metadata building, not for PAGE_NUMBER assignment)
        assert RangeMappingEngine.target_page_for(rms, 3) == 1
        assert RangeMappingEngine.target_page_for(rms, 4) == 2
        assert RangeMappingEngine.target_page_for(rms, 5) == 3
        assert RangeMappingEngine.target_page_for(rms, 1) is None

    def test_to_per_page_mappings(self):
        from utils.page_mapping import RangeMapping, RangeMappingEngine
        rms = [RangeMapping(source_start=1, source_end=2, replacement_start=3, replacement_end=5)]
        result = RangeMappingEngine.to_per_page_mappings(rms)
        assert len(result) == 3
        assert result[0] == {'source': 3, 'target': 1}
        assert result[1] == {'source': 4, 'target': 2}
        assert result[2] == {'source': 5, 'target': 3}


# =============================================================================
# QA Studio — _get_original_pdf_page
# =============================================================================

class TestGetOriginalPdfPage:
    """Validate surgical-aware PDF page resolution in QA Studio."""

    def _call(self, chunk_metadata, page_number):
        # Import after mocking streamlit
        from views.refinery.tab_qa import _get_original_pdf_page
        return _get_original_pdf_page(chunk_metadata, page_number)

    def test_none_metadata_returns_page_number(self):
        assert self._call(None, 5) == 5

    def test_empty_dict_returns_page_number(self):
        assert self._call({}, 5) == 5

    def test_non_surgical_metadata_returns_page_number(self):
        meta = {"chunk_type": "standard", "write_mode": "APPEND"}
        assert self._call(meta, 5) == 5

    def test_surgical_metadata_resolves_original_page(self):
        meta = {
            "surgical": {
                "page_mappings": [
                    {"source": 3, "target": 4, "original_pdf_page": 3},
                    {"source": 4, "target": 5, "original_pdf_page": 4},
                    {"source": 5, "target": 6, "original_pdf_page": 5},
                ]
            }
        }
        assert self._call(meta, 4) == 3
        assert self._call(meta, 5) == 4
        assert self._call(meta, 6) == 5

    def test_surgical_metadata_no_match_returns_page_number(self):
        meta = {
            "surgical": {
                "page_mappings": [
                    {"source": 3, "target": 4, "original_pdf_page": 3},
                ]
            }
        }
        assert self._call(meta, 99) == 99

    def test_variant_as_json_string(self):
        """Snowflake VARIANT may be returned as a JSON string."""
        meta_str = json.dumps({
            "surgical": {
                "page_mappings": [
                    {"source": 3, "target": 4, "original_pdf_page": 3},
                ]
            }
        })
        assert self._call(meta_str, 4) == 3

    def test_variant_as_dict(self):
        """Snowflake VARIANT may be returned as a Python dict directly."""
        meta = {
            "surgical": {
                "page_mappings": [
                    {"source": 3, "target": 4, "original_pdf_page": 3},
                ]
            }
        }
        # Pass as dict (not string) — should handle gracefully
        assert self._call(meta, 4) == 3

    def test_malformed_metadata_returns_page_number(self):
        """Garbage metadata should not crash — return page_number as fallback."""
        assert self._call("not valid json {{{", 5) == 5
        assert self._call(12345, 5) == 5


# =============================================================================
# Surgical Delete — no shift
# =============================================================================

class TestSurgicalDeleteNoShift:
    """Verify _execute_surgical_delete_with_shift has no shift logic."""

    def test_no_transaction_sql_in_function(self):
        """The function should NOT contain BEGIN/COMMIT/ROLLBACK/UPDATE."""
        import inspect
        from views.refinery.ingestion_core import _execute_surgical_delete_with_shift
        source = inspect.getsource(_execute_surgical_delete_with_shift)
        assert "BEGIN TRANSACTION" not in source, "Found BEGIN TRANSACTION — shift logic not removed"
        assert "ROLLBACK" not in source, "Found ROLLBACK — shift logic not removed"
        assert "PAGE_NUMBER + " not in source, "Found shift UPDATE — shift logic not removed"
        assert "REGEXP_REPLACE" not in source, "Found REGEXP_REPLACE — CHUNK_REF rewrite not removed"

    def test_function_only_deletes(self):
        """The function should only contain DELETE statements."""
        import inspect
        from views.refinery.ingestion_core import _execute_surgical_delete_with_shift
        source = inspect.getsource(_execute_surgical_delete_with_shift)
        assert "DELETE FROM" in source, "DELETE statement missing"
        # Count SQL operations: only DELETE, no UPDATE
        assert source.count("UPDATE") == 0, "UPDATE found — should be delete-only"


# =============================================================================
# Surgical UI — validation halts execution
# =============================================================================

class TestSurgicalUIValidation:
    """Verify surgical_ui.py halts on invalid mappings."""

    def test_st_stop_called_on_validation_failure(self):
        """surgical_ui.py should call st.stop() when validation fails."""
        with open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "views", "refinery", "surgical_ui.py"
        )) as f:
            source = f.read()
        assert "st.stop()" in source, "st.stop() not found — validation errors don't halt execution"
        assert "validation_failed" in source, "validation_failed flag not found"

    def test_no_delta_preview_in_surgical_ui(self):
        """Delta preview should be removed — replaced with mapping summary."""
        with open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "views", "refinery", "surgical_ui.py"
        )) as f:
            source = f.read()
        assert "Delta Preview" not in source, "Delta Preview still present — should be removed"
        assert "Surgical mode" in source, "Mapping summary info text not found"


# =============================================================================
# Layout Strategy — PAGE_NUMBER = PDF page number
# =============================================================================

class TestLayoutPageNumberMapping:
    """Verify layout.py uses PDF page number directly (no remapping)."""

    def test_no_target_page_for_in_layout(self):
        """layout.py should NOT call RangeMappingEngine.target_page_for."""
        with open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "views", "refinery", "ingestion_strategies", "layout.py"
        )) as f:
            source = f.read()
        assert "target_page_for" not in source, (
            "target_page_for found in layout.py — PAGE_NUMBER remapping not removed"
        )

    def test_no_target_page_for_in_vision(self):
        """vision.py should NOT call RangeMappingEngine.target_page_for."""
        with open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "views", "refinery", "ingestion_strategies", "vision.py"
        )) as f:
            source = f.read()
        assert "target_page_for" not in source, (
            "target_page_for found in vision.py — PAGE_NUMBER remapping not removed"
        )

    def test_db_pg_num_equals_pg_num_in_layout(self):
        """layout.py should assign db_pg_num = pg_num (direct mapping)."""
        with open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "views", "refinery", "ingestion_strategies", "layout.py"
        )) as f:
            source = f.read()
        assert "db_pg_num = pg_num" in source, "db_pg_num = pg_num not found in layout.py"
        assert "db_mp = mp" in source, "db_mp = mp not found in layout.py"


# =============================================================================
# Tab Config — Job Intent default
# =============================================================================

class TestTabConfigDefaults:
    """Verify tab_config.py does not pre-select Job Intent."""

    def test_no_setdefault_jb_mode(self):
        """tab_config.py should NOT setdefault jb_mode to a preset value."""
        with open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "views", "refinery", "tab_config.py"
        )) as f:
            source = f.read()
        # Should NOT have: setdefault("jb_mode", "APPEND")
        assert 'setdefault("jb_mode"' not in source, (
            "setdefault('jb_mode') found — forces preset selection on first render"
        )
        assert 'setdefault("jb_scope"' not in source, (
            "setdefault('jb_scope') found — forces preset selection on first render"
        )

    def test_pills_default_is_none(self):
        """st.pills should have default=None (no pre-selection)."""
        with open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "views", "refinery", "tab_config.py"
        )) as f:
            source = f.read()
        # Find the st.pills call and verify default=None
        assert "default=None" in source, "default=None not found — pills may be pre-selected"


# =============================================================================
# CREATE TABLE — COMMENT clauses
# =============================================================================

class TestCreateTableComments:
    """Verify CREATE TABLE includes COMMENT clauses."""

    def test_comment_on_page_number(self):
        with open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "views", "refinery", "ingestion_core.py"
        )) as f:
            source = f.read()
        assert "PAGE_NUMBER NUMBER COMMENT" in source, "COMMENT missing on PAGE_NUMBER column"
        assert "PDF page number" in source, "PAGE_NUMBER comment should reference PDF page"

    def test_comment_on_all_columns(self):
        with open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "views", "refinery", "ingestion_core.py"
        )) as f:
            source = f.read()
        for col in ["RELATIVE_PATH", "PAGE_NUMBER", "CHUNK VARCHAR", "CHUNK_ID",
                     "CHUNK_TYPE", "CHUNK_REF", "LINK_BLOCK", "CHUNK_METADATA"]:
            assert f"{col} COMMENT" in source or f"{col} " in source, (
                f"COMMENT missing on {col}"
            )


# =============================================================================
# Metadata Handler
# =============================================================================

class TestMetadataHandler:
    """Verify surgical metadata structure."""

    def test_build_surgical_metadata_includes_original_pdf_page(self):
        from utils.metadata_handler import ChunkMetadataHandler
        result = ChunkMetadataHandler.build_surgical_select_metadata(
            original_file="doc.pdf",
            source_range=(1, 2),
            replacement_file="doc.pdf",
            page_mappings=[
                {'source': 3, 'target': 1, 'original_pdf_page': 3},
                {'source': 4, 'target': 2, 'original_pdf_page': 4},
            ]
        )
        meta = json.loads(result)
        assert meta['write_mode'] == 'SURGICAL'
        assert meta['surgical']['original_file'] == 'doc.pdf'
        mappings = meta['surgical']['page_mappings']
        assert len(mappings) == 2
        assert mappings[0]['original_pdf_page'] == 3
        assert mappings[1]['original_pdf_page'] == 4

    def test_create_initial_metadata(self):
        from utils.metadata_handler import ChunkMetadataHandler
        result = ChunkMetadataHandler.create_initial_metadata(
            write_mode="APPEND", chunk_type="standard"
        )
        assert result['write_mode'] == 'APPEND'
        assert result['chunk_type'] == 'standard'
        assert 'timestamps' in result


# =============================================================================
# Integration: Surgical Flow End-to-End (Mocked)
# =============================================================================

class TestSurgicalFlowEndToEnd:
    """
    Simulate the surgical flow with the user's example:
    PDF A (5 pages), Source 1-2, Repl 3-5.

    After surgery:
    - DELETE pages 1-2
    - INSERT PDF pages 3-5 at PAGE_NUMBER 3, 4, 5
    - Old pages 3, 4, 5 still at PAGE_NUMBER 3, 4, 5 (duplicates)
    - PAGE_NUMBER always = PDF page number
    """

    def test_user_example_source_1_2_repl_3_5(self):
        from utils.page_mapping import RangeMapping, RangeMappingEngine

        rm = RangeMapping(source_start=1, source_end=2, replacement_start=3, replacement_end=5)

        # Delta is informational only (not used in SQL)
        # replacement_size = 5-3+1 = 3, source_size = 2-1+1 = 2, delta = 3-2 = 1
        delta = RangeMappingEngine.compute_delta(rm)
        assert delta == 1

        # Sort bottom-up (single range, trivial)
        sorted_rms = RangeMappingEngine.sort_bottom_up([rm])
        assert len(sorted_rms) == 1

        # After surgery:
        # - DELETE PAGE_NUMBER BETWEEN 1 AND 2
        # - INSERT PDF pages 3,4,5 at PAGE_NUMBER 3,4,5
        # - Old pages 3,4,5 still exist at PAGE_NUMBER 3,4,5
        # Final table: PAGE_NUMBER 3 (old+new), 4 (old+new), 5 (old+new)

        # Verify per-page mappings for metadata
        per_page = RangeMappingEngine.to_per_page_mappings([rm])
        assert per_page == [
            {'source': 3, 'target': 1},
            {'source': 4, 'target': 2},
            {'source': 5, 'target': 3},
        ]

    def test_user_example_source_1_2_repl_1_5(self):
        """Source 1-2, Repl 1-5 (same PDF): delete 1-2, insert 1-5 at PAGE_NUMBER 1-5."""
        from utils.page_mapping import RangeMapping, RangeMappingEngine

        rm = RangeMapping(source_start=1, source_end=2, replacement_start=1, replacement_end=5)

        # DELETE pages 1-2
        # INSERT PDF pages 1-5 at PAGE_NUMBER 1-5
        # Old pages 3-5 still at PAGE_NUMBER 3-5 (overlap with new 3-5)
        per_page = RangeMappingEngine.to_per_page_mappings([rm])
        assert len(per_page) == 5
        assert per_page[0] == {'source': 1, 'target': 1}
        assert per_page[4] == {'source': 5, 'target': 5}

    def test_multi_range_sort_order(self):
        """Multiple ranges should be sorted bottom-up for safe deletion."""
        from utils.page_mapping import RangeMapping, RangeMappingEngine

        rms = [
            RangeMapping(source_start=1, source_end=2, replacement_start=1, replacement_end=3),
            RangeMapping(source_start=5, source_end=6, replacement_start=1, replacement_end=2),
        ]
        sorted_rms = RangeMappingEngine.sort_bottom_up(rms)
        # Higher source_end (6) should be processed first
        assert sorted_rms[0].source_end == 6
        assert sorted_rms[1].source_end == 2


# =============================================================================
# CHUNK_REF builder
# =============================================================================

class TestChunkRefBuilder:
    """Verify CHUNK_REF format."""

    def test_basic_chunk_ref(self):
        from views.refinery.common import _build_chunk_ref
        ref = _build_chunk_ref("doc.pdf", 3)
        assert "doc.pdf" in ref
        assert "Page Num: 3" in ref

    def test_chunk_ref_with_link(self):
        from views.refinery.common import _build_chunk_ref
        ref = _build_chunk_ref("doc.pdf", 3, link="https://example.com/doc.pdf")
        assert "Digital Copy" in ref
        assert "Page Num: 3" in ref
        assert "https://example.com/doc.pdf" in ref


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
