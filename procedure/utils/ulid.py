"""Stdlib-only Crockford ULID identifiers used for chunks and runs."""
from __future__ import annotations
import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        value, remainder = divmod(value, 32)
        out.append(_ALPHABET[remainder])
    return "".join(reversed(out))

def timestamp_prefix(ms: int | None = None) -> str:
    return _encode(int(time.time() * 1000) if ms is None else ms, 10)

def new_ulid(ms: int | None = None) -> str:
    return timestamp_prefix(ms) + _encode(int.from_bytes(os.urandom(10), "big"), 16)

def chunk_id(ms: int | None = None) -> str:
    return "CHK_" + new_ulid(ms)

def run_id(ms: int | None = None) -> str:
    return "RUN_" + new_ulid(ms)

def is_ulid(value: str) -> bool:
    return isinstance(value, str) and len(value) == 26 and all(c in _ALPHABET for c in value)
