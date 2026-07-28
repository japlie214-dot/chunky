"""
procedure/utils/quality_inspector.py
Forensic detector for low-quality chunks that should be repaired by Vision.

Ported (verbatim logic) from utils/core_utils.py:QualityInspector so the
procedure bundle is fully self-contained.
"""
from __future__ import annotations
import re
import statistics


class QualityInspector:
    """Static forensic tools to detect low-quality chunks requiring AI reconstruction."""

    @staticmethod
    def check_nbsp_chain(text, min_chain=5):
        if not text:
            return False
        nbsp_entity = r"(?:&nbsp;|&#160;|&#x[aA]0;)"
        pattern = nbsp_entity + r"(?:\s*" + nbsp_entity + r"){4,}"
        return bool(re.search(pattern, text))

    @staticmethod
    def check_repetition(text):
        if not text or len(text) < 100:
            return False
        tokens = text.split()
        if len(tokens) < 10:
            return False
        unique_ratio = len(set(tokens)) / len(tokens)
        return unique_ratio < 0.20

    @staticmethod
    def check_syntax_noise(text):
        if not text:
            return False
        if re.search(r"\\[a-zA-Z]+", text):
            return True
        if re.search(r"\\[%$&_#]", text):
            return True
        return False

    @staticmethod
    def check_phantom_spaces(text):
        if not text:
            return False
        lines = text.split("\n")
        rgx_table = re.compile(r"(?<!\d)\d{1,3}[.,]\s+\d{3}\b")
        rgx_narrative_dot = re.compile(r"(?<!\d)\d{1,3}\.\s+\d{3}\b")
        rgx_narrative_comma = re.compile(r"(?<![\d\-\/\(\#])\d{1,3},\s+\d{3}\b")
        for line in lines:
            if "|" in line:
                if rgx_table.search(line):
                    return True
            else:
                if rgx_narrative_dot.search(line):
                    return True
                if rgx_narrative_comma.search(line):
                    return True
        return False

    @staticmethod
    def check_table_health(text):
        if not text:
            return False
        # Regex-based validation (mistletoe is not bundled in the procedure)
        lines = text.split("\n")
        pipe_lines = [line for line in lines if "|" in line]
        if len(pipe_lines) < 3:
            return False
        empty_rows = [
            line for line in pipe_lines
            if re.match(r"^\s*(\|\s*)+\|?\s*$", line)
        ]
        if len(empty_rows) > (len(pipe_lines) * 0.5):
            return "GHOST_TABLE"
        has_separator = any(
            re.search(r"\|[\s-]*:?-+[\s-]*:?\|", line) for line in pipe_lines
        )
        if not has_separator:
            return "MISSING_HEADER"
        pipe_counts = [line.count("|") for line in pipe_lines]
        if not pipe_counts:
            return False
        median_pipes = statistics.median(pipe_counts)
        misaligned_count = sum(1 for c in pipe_counts if abs(c - median_pipes) > 1)
        if misaligned_count > (len(pipe_lines) * 0.3):
            return "MISALIGNED_COLUMNS"
        return False

    @staticmethod
    def inspect(text):
        """Return the primary defect type, or 'OK' if the chunk is healthy."""
        if not text:
            return "EMPTY"
        if len(text) < 500:
            return "REPAIR_LOW_INFO"
        if "![" in text:
            return "REPAIR_VISUAL"
        if QualityInspector.check_nbsp_chain(text):
            return "REPAIR_NBSP_CHAIN"
        if QualityInspector.check_repetition(text):
            return "REPAIR_LOOP"
        table_status = QualityInspector.check_table_health(text)
        if table_status:
            return f"REPAIR_TABLE_{table_status}"
        if QualityInspector.check_phantom_spaces(text):
            return "REPAIR_NUMBERS"
        if QualityInspector.check_syntax_noise(text):
            return "REPAIR_SYNTAX"
        return "OK"
