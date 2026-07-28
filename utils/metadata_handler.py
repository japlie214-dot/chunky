# utils/metadata_handler.py
import json
from typing import Dict, List, Tuple
from datetime import datetime

class ChunkMetadataHandler:
    @staticmethod
    def create_initial_metadata(write_mode: str, chunk_type: str = "standard", parser_config: Dict = None) -> Dict:
        metadata = {
            "chunk_type": chunk_type,
            "write_mode": write_mode,
            "timestamps": {"created": datetime.utcnow().isoformat() + "Z"}
        }
        if parser_config:
            metadata["parser"] = parser_config
        return metadata

    @staticmethod
    def build_surgical_select_metadata(original_file: str, source_range: Tuple[int, int], replacement_file: str, page_mappings: List[Dict]) -> str:
        metadata = {
            "chunk_type": "standard",
            "write_mode": "SURGICAL",
            "surgical": {
                "original_file": original_file,
                "original_range": {"start": source_range[0], "end": source_range[1]},
                "replacement_file": replacement_file,
                "page_mappings": page_mappings
            },
            "timestamps": {"surgical_timestamp": datetime.utcnow().isoformat() + "Z"}
        }
        return json.dumps(metadata)

    @staticmethod
    def serialize_metadata(metadata: Dict) -> str:
        return json.dumps(metadata)
