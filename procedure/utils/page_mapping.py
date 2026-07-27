"""
procedure/utils/page_mapping.py
Range-mapping math for the surgical ingestion pipeline.

Copied verbatim (no behavioural change) from the top-level
`utils/page_mapping.py` so the Snowflake procedures are fully
self-contained — they no longer import from the Streamlit-side `utils/`
package.

Two engines live here:
  * `PageMappingEngine`     — legacy per-page mapping helpers (still used
                              by views/refinery/surgical_ui.py).
  * `RangeMappingEngine`    — range-based mapping helpers used by both
                              the Streamlit app and the `chunky_chunks`
                              procedure.
"""
from __future__ import annotations
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from collections import defaultdict


@dataclass
class PageMapping:
    source_page: int
    target_page: int
    is_auto: bool = True


class PageMappingEngine:
    @staticmethod
    def calculate_default_mappings(source_start: int, source_end: int,
                                   replacement_pages: int) -> List[PageMapping]:
        mappings = []
        for source_page in range(source_start, source_end + 1):
            source_index = source_page - source_start
            if source_page <= replacement_pages:
                target_page = source_page
            else:
                target_page = (source_index % replacement_pages) + 1
            mappings.append(
                PageMapping(source_page=source_page,
                            target_page=target_page, is_auto=True)
            )
        return mappings

    @staticmethod
    def validate_mappings(mappings: List[PageMapping],
                          replacement_pages: int) -> Tuple[bool, List[str]]:
        errors = []
        for mapping in mappings:
            if mapping.target_page < 1:
                errors.append(f"Page {mapping.source_page}: Target page must be >= 1")
            if mapping.target_page > replacement_pages:
                errors.append(
                    f"Page {mapping.source_page}: Target page {mapping.target_page} "
                    f"exceeds replacement PDF ({replacement_pages} pages)"
                )
        return len(errors) == 0, errors

    @staticmethod
    def detect_duplicates(mappings: List[PageMapping]) -> List[Dict]:
        target_to_sources = defaultdict(list)
        for mapping in mappings:
            target_to_sources[mapping.target_page].append(mapping.source_page)

        duplicates = []
        for target_page, source_pages in target_to_sources.items():
            if len(source_pages) > 1:
                duplicates.append(
                    {'target_page': target_page,
                     'source_pages': sorted(source_pages)}
                )
        return duplicates


# =============================================================================
# Range-based surgical mapping
# =============================================================================

@dataclass
class RangeMapping:
    """
    A single range replacement definition.

    source_start / source_end:
        Page numbers in the Snowflake TABLE (original document) to DELETE.

    replacement_start / replacement_end:
        Page numbers in the PDF on the stage (job['file']) to EXTRACT via
        AI_PARSE_DOCUMENT and INSERT as new chunks.
    """
    source_start: int
    source_end: int
    replacement_start: int
    replacement_end: int


class RangeMappingEngine:
    """Static utilities for validating and transforming RangeMapping lists."""

    @staticmethod
    def compute_delta(m: RangeMapping) -> int:
        """Positive = expansion, negative = contraction, zero = no shift."""
        source_size = m.source_end - m.source_start + 1
        replacement_size = m.replacement_end - m.replacement_start + 1
        return replacement_size - source_size

    @staticmethod
    def validate(mappings: List[RangeMapping],
                 replacement_page_count: int) -> Tuple[bool, List[str]]:
        errors = []
        for i, m in enumerate(mappings):
            if m.source_start > m.source_end:
                errors.append(
                    f"Range {i+1}: source_start ({m.source_start}) > source_end ({m.source_end})"
                )
            if m.replacement_start > m.replacement_end:
                errors.append(
                    f"Range {i+1}: replacement_start ({m.replacement_start}) > "
                    f"replacement_end ({m.replacement_end})"
                )
            if m.source_start < 1:
                errors.append(f"Range {i+1}: source_start ({m.source_start}) must be >= 1")
            if m.replacement_start < 1:
                errors.append(f"Range {i+1}: replacement_start ({m.replacement_start}) must be >= 1")
            if m.replacement_end > replacement_page_count:
                errors.append(
                    f"Range {i+1}: replacement_end ({m.replacement_end}) exceeds "
                    f"replacement PDF page count ({replacement_page_count})"
                )

        source_ranges = sorted((m.source_start, m.source_end) for m in mappings)
        for i in range(1, len(source_ranges)):
            if source_ranges[i][0] <= source_ranges[i-1][1]:
                errors.append(
                    f"Source ranges overlap: {source_ranges[i-1]} and {source_ranges[i]}"
                )

        repl_ranges = sorted((m.replacement_start, m.replacement_end) for m in mappings)
        for i in range(1, len(repl_ranges)):
            if repl_ranges[i][0] <= repl_ranges[i-1][1]:
                errors.append(
                    f"Replacement ranges overlap: {repl_ranges[i-1]} and {repl_ranges[i]}"
                )

        return len(errors) == 0, errors

    @staticmethod
    def target_page_for(mappings: List[RangeMapping],
                        replacement_page: int) -> Optional[int]:
        """Map a replacement PDF page to the target table PAGE_NUMBER."""
        for m in mappings:
            if m.replacement_start <= replacement_page <= m.replacement_end:
                offset = replacement_page - m.replacement_start
                return m.source_start + offset
        return None

    @staticmethod
    def sort_bottom_up(mappings: List[RangeMapping]) -> List[RangeMapping]:
        """Sort by source_end DESC — see module docstring for why this matters."""
        return sorted(mappings, key=lambda m: m.source_end, reverse=True)

    @staticmethod
    def to_per_page_mappings(mappings: List[RangeMapping]) -> List[Dict]:
        """
        Convert range mappings to the per-page dict format expected by
        ChunkMetadataHandler.build_surgical_select_metadata.
        """
        result = []
        for m in mappings:
            for pdf_page in range(m.replacement_start, m.replacement_end + 1):
                target_pg = m.source_start + (pdf_page - m.replacement_start)
                result.append({'source': pdf_page, 'target': target_pg})
        return result
