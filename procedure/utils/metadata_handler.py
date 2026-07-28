"""
procedure/utils/metadata_handler.py
Stamping of per-chunk metadata.

Copied from the top-level `utils/metadata_handler.py` so the Snowflake
procedures are self-contained — they no longer import from the
Streamlit-side `utils/` package.
"""
from __future__ import annotations
import json
from typing import Dict, List, Tuple
from datetime import datetime, timezone


def _utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp string (Z-suffixed)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ChunkMetadataHandler:
    @staticmethod
    def create_initial_metadata(write_mode: str,
                                chunk_type: str = "standard",
                                parser_config: Dict = None) -> Dict:
        metadata = {
            "chunk_type": chunk_type,
            "write_mode": write_mode,
            "timestamps": {"created": _utc_now_iso()},
        }
        if parser_config:
            metadata["parser"] = parser_config
        return metadata

    @staticmethod
    def build_surgical_select_metadata(original_file: str,
                                       source_range: Tuple[int, int],
                                       replacement_file: str,
                                       page_mappings: List[Dict]) -> str:
        metadata = {
            "chunk_type": "standard",
            "write_mode": "SURGICAL",
            "surgical": {
                "original_file": original_file,
                "original_range": {"start": source_range[0], "end": source_range[1]},
                "replacement_file": replacement_file,
                "page_mappings": page_mappings,
            },
            "timestamps": {"surgical_timestamp": _utc_now_iso()},
        }
        return json.dumps(metadata)

    @staticmethod
    def serialize_metadata(metadata: Dict) -> str:
        return json.dumps(metadata)
