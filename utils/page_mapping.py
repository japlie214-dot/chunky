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
