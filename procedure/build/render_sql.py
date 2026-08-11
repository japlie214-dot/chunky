"""Render simple {{VARIABLE}} SQL templates without Jinja2."""
from __future__ import annotations
import re

def render(template: str, values: dict) -> str:
    return re.sub(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}",
                  lambda match: str(values[match.group(1)]), template)
