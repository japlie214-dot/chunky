# utils/page_mapping.py
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
    def calculate_default_mappings(source_start: int, source_end: int, replacement_pages: int) -> List[PageMapping]:
        mappings = []
        for source_page in range(source_start, source_end + 1):
            source_index = source_page - source_start
            if source_page <= replacement_pages:
                target_page = source_page
            else:
                target_page = (source_index % replacement_pages) + 1
            mappings.append(PageMapping(source_page=source_page, target_page=target_page, is_auto=True))
        return mappings

    @staticmethod
    def validate_mappings(mappings: List[PageMapping], replacement_pages: int) -> Tuple[bool, List[str]]:
        errors = []
        for mapping in mappings:
            if mapping.target_page < 1:
                errors.append(f"Page {mapping.source_page}: Target page must be >= 1")
            if mapping.target_page > replacement_pages:
                errors.append(f"Page {mapping.source_page}: Target page {mapping.target_page} exceeds replacement PDF ({replacement_pages} pages)")
        return len(errors) == 0, errors

    @staticmethod
    def detect_duplicates(mappings: List[PageMapping]) -> List[Dict]:
        target_to_sources = defaultdict(list)
        for mapping in mappings:
            target_to_sources[mapping.target_page].append(mapping.source_page)

        duplicates = []
        for target_page, source_pages in target_to_sources.items():
            if len(source_pages) > 1:
                duplicates.append({'target_page': target_page, 'source_pages': sorted(source_pages)})
        return duplicates


# =============================================================================
# Range-based surgical mapping (new feature)
# =============================================================================

@dataclass
class RangeMapping:
    """
    A single range replacement definition.

    source_start / source_end:
        Page numbers in the Snowflake TABLE (original document) to DELETE.
        These are the existing chunks that will be removed.

    replacement_start / replacement_end:
        Page numbers in the PDF on the stage (job['file']) to EXTRACT via
        AI_PARSE_DOCUMENT and INSERT as new chunks. The new chunks are written
        at table page numbers source_start .. source_start + replacement_size - 1.

    Example: source_start=2, source_end=3, replacement_start=1, replacement_end=5
        → DELETE table pages 2-3
        → Shift table pages >3 by delta=+3 (5-2=+3)
        → INSERT PDF pages 1-5 as table pages 2-6
    """
    source_start: int
    source_end: int
    replacement_start: int
    replacement_end: int


class RangeMappingEngine:
    """Static utilities for validating and transforming RangeMapping lists."""

    @staticmethod
    def compute_delta(m: RangeMapping) -> int:
        """
        Positive when replacement is larger (expansion).
        Negative when replacement is smaller (contraction).
        Zero when same size (no shift needed).
        """
        source_size = m.source_end - m.source_start + 1
        replacement_size = m.replacement_end - m.replacement_start + 1
        return replacement_size - source_size

    @staticmethod
    def validate(mappings: List[RangeMapping], replacement_page_count: int) -> Tuple[bool, List[str]]:
        """
        Validates range mappings. Returns (is_valid, errors).

        Checks:
        - source_start <= source_end for each mapping
        - replacement_start <= replacement_end for each mapping
        - All replacement pages are within [1, replacement_page_count]
        - Source ranges do not overlap each other
        - Replacement ranges do not overlap each other
        """
        errors = []
        for i, m in enumerate(mappings):
            if m.source_start > m.source_end:
                errors.append(f"Range {i+1}: source_start ({m.source_start}) > source_end ({m.source_end})")
            if m.replacement_start > m.replacement_end:
                errors.append(f"Range {i+1}: replacement_start ({m.replacement_start}) > replacement_end ({m.replacement_end})")
            if m.source_start < 1:
                errors.append(f"Range {i+1}: source_start ({m.source_start}) must be >= 1")
            if m.replacement_start < 1:
                errors.append(f"Range {i+1}: replacement_start ({m.replacement_start}) must be >= 1")
            if m.replacement_end > replacement_page_count:
                errors.append(f"Range {i+1}: replacement_end ({m.replacement_end}) exceeds replacement PDF page count ({replacement_page_count})")

        # Check source range overlaps
        source_ranges = [(m.source_start, m.source_end) for m in mappings]
        source_ranges.sort()
        for i in range(1, len(source_ranges)):
            if source_ranges[i][0] <= source_ranges[i-1][1]:
                errors.append(f"Source ranges overlap: {source_ranges[i-1]} and {source_ranges[i]}")

        # Check replacement range overlaps
        repl_ranges = [(m.replacement_start, m.replacement_end) for m in mappings]
        repl_ranges.sort()
        for i in range(1, len(repl_ranges)):
            if repl_ranges[i][0] <= repl_ranges[i-1][1]:
                errors.append(f"Replacement ranges overlap: {repl_ranges[i-1]} and {repl_ranges[i]}")

        return len(errors) == 0, errors

    @staticmethod
    def target_page_for(mappings: List[RangeMapping], replacement_page: int) -> Optional[int]:
        """
        Maps a replacement PDF page number to the target table PAGE_NUMBER.

        Returns None if the replacement_page is not covered by any mapping.
        This is used by the layout and vision strategies to determine which
        table PAGE_NUMBER to assign to each extracted PDF page.
        """
        for m in mappings:
            if m.replacement_start <= replacement_page <= m.replacement_end:
                offset = replacement_page - m.replacement_start
                return m.source_start + offset
        return None

    @staticmethod
    def sort_bottom_up(mappings: List[RangeMapping]) -> List[RangeMapping]:
        """
        Sorts mappings by source_end DESCENDING.

        When multiple ranges exist in one job, applying shifts bottom-up
        (highest source_end first) prevents a shift from moving pages into
        a region that a subsequent (lower) range will delete.

        Example: ranges [(2,3), (7,8)]
        - If we process (2,3) first and shift pages >3 by +3,
          page 7 becomes 10, page 8 becomes 11.
        - Then processing (7,8) would delete pages 10-11, not 7-8.
        - By processing (7,8) first, we shift pages >8, then process (2,3)
          and shift pages >3 (which doesn't affect the already-shifted >8 pages
          because they're now >11).
        """
        return sorted(mappings, key=lambda m: m.source_end, reverse=True)

    @staticmethod
    def to_per_page_mappings(mappings: List[RangeMapping]) -> List[Dict]:
        """
        Converts range mappings to the per-page dict format expected by
        ChunkMetadataHandler.build_surgical_select_metadata.

        This bridges the new range-mapping model to the existing metadata
        stamping code in layout.py.
        """
        result = []
        for m in mappings:
            for pdf_page in range(m.replacement_start, m.replacement_end + 1):
                target_pg = m.source_start + (pdf_page - m.replacement_start)
                result.append({'source': pdf_page, 'target': target_pg})
        return result
