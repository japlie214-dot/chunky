# observability.py
# Activity-Driven Observability Convention implementation.
# Mechanism: Higher-Order Function (HOF) — the Activity is passed as a
# first-class callable to `observe(name, fn, accumulator)`, which calls it
# and records inputs/outputs. No metaprogramming required.
#
# When the Accumulator's `active` flag is False, all record methods are
# no-ops with zero overhead (no allocation, no serialization).

import re
import json
from typing import Any, Callable, Dict, List


# ---------------------------------------------------------------------------
# Auto-masking patterns (Rule 7)
# ---------------------------------------------------------------------------

# Base64 strings: character set is [A-Za-z0-9+/=] or URL-safe [A-Za-z0-9_-],
# length >= 32 characters.
_BASE64_RE = re.compile(r'^[A-Za-z0-9+/=_-]{32,}$')

# JWT tokens: three period-separated base64 segments.
_JWT_RE = re.compile(r'^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$')

# Hex blobs: long strings of hex characters (length >= 64).
_HEX_RE = re.compile(r'^[0-9a-fA-F]{64,}$')

# Float-vector arrays: array of 64+ numeric elements suggesting float precision.
_FLOAT_VECTOR_MIN_LENGTH = 64

# Per-key truncation threshold (Rule 6)
_MAX_CHARS = 50_000


def _should_mask(value: Any) -> bool:
    """Check if a value matches any sensitive pattern that requires masking."""
    if isinstance(value, str):
        if _BASE64_RE.match(value):
            return True
        if _JWT_RE.match(value):
            return True
        if _HEX_RE.match(value):
            return True
    if isinstance(value, list) and len(value) >= _FLOAT_VECTOR_MIN_LENGTH:
        try:
            sample = value[:min(10, len(value))]
            if all(isinstance(x, (int, float)) for x in sample):
                # Float-vectors have high-precision decimal components
                if any(isinstance(x, float) and len(str(x).split('.')[-1]) > 4
                       for x in sample if isinstance(x, float)):
                    return True
        except (TypeError, ValueError):
            pass
    return False


def _mask_value(key: str, value: Any) -> str:
    """Replace value with a masked placeholder preserving key name and size hint."""
    if isinstance(value, str):
        return f"[MASKED:{key}:len={len(value)}]"
    if isinstance(value, list):
        return f"[MASKED:{key}:len={len(value)}]"
    if isinstance(value, (bytes, bytearray)):
        return f"[MASKED:{key}:bytes={len(value)}]"
    return f"[MASKED:{key}]"


def _truncate_value(key: str, value: str, max_chars: int = _MAX_CHARS) -> str:
    """Truncate a single key's value if it exceeds max_chars (Rule 6)."""
    if len(value) > max_chars:
        dropped = len(value) - max_chars
        return value[:max_chars] + f"...[TRUNCATED:{dropped}_chars_dropped]"
    return value


def _snapshot_args(args: tuple, kwargs: dict) -> Dict[str, Any]:
    """Create a truncated + auto-masked snapshot of Activity inputs."""
    snapshot = {}
    for i, arg in enumerate(args):
        key = f"arg_{i}"
        if _should_mask(arg):
            snapshot[key] = _mask_value(key, arg)
        elif isinstance(arg, str):
            snapshot[key] = _truncate_value(key, arg)
        else:
            try:
                serialized = json.dumps(arg, default=str)
                if len(serialized) > _MAX_CHARS:
                    snapshot[key] = _truncate_value(key, serialized)
                else:
                    snapshot[key] = arg
            except (TypeError, ValueError):
                snapshot[key] = str(arg)[:_MAX_CHARS]

    for k, v in kwargs.items():
        if _should_mask(v):
            snapshot[k] = _mask_value(k, v)
        elif isinstance(v, str):
            snapshot[k] = _truncate_value(k, v)
        else:
            try:
                serialized = json.dumps(v, default=str)
                if len(serialized) > _MAX_CHARS:
                    snapshot[k] = _truncate_value(k, serialized)
                else:
                    snapshot[k] = v
            except (TypeError, ValueError):
                snapshot[k] = str(v)[:_MAX_CHARS]
    return snapshot


def _snapshot_output(output: Any) -> Any:
    """Create a truncated + auto-masked snapshot of an Activity's output."""
    if _should_mask(output):
        return _mask_value("output", output)
    if isinstance(output, str):
        return _truncate_value("output", output)
    try:
        serialized = json.dumps(output, default=str)
        if len(serialized) > _MAX_CHARS:
            return _truncate_value("output", serialized)
        return output
    except (TypeError, ValueError):
        return str(output)[:_MAX_CHARS]


# ---------------------------------------------------------------------------
# Accumulator (Rules 2, 4, 5)
# ---------------------------------------------------------------------------

class Accumulator:
    """
    Observability Accumulator — created once at the invocation boundary,
    threaded through every Activity.

    When `active` is False (default), all record methods are no-ops.
    On Activity error, records the failure and re-raises unchanged (Rule 5).
    """

    def __init__(self, active: bool = False):
        self._active = active
        self._lineage: List[Dict[str, Any]] = []
        self._status: str = "PASSED"

    @property
    def active(self) -> bool:
        return self._active

    def record_start(self, activity_name: str, inputs: Dict[str, Any]) -> None:
        if not self._active:
            return
        self._lineage.append({
            "activity_name": activity_name,
            "status": "RUNNING",
            "inputs": inputs,
            "outputs": None,
            "error": None,
        })

    def record_success(self, activity_name: str, outputs: Any) -> None:
        if not self._active:
            return
        for entry in reversed(self._lineage):
            if entry["activity_name"] == activity_name and entry["status"] == "RUNNING":
                entry["status"] = "PASSED"
                entry["outputs"] = _snapshot_output(outputs)
                break

    def record_failure(self, activity_name: str, error: str) -> None:
        if not self._active:
            return
        self._status = "FAILED"
        for entry in reversed(self._lineage):
            if entry["activity_name"] == activity_name and entry["status"] == "RUNNING":
                entry["status"] = "FAILED"
                entry["error"] = error
                break

    def to_lineage(self) -> Dict[str, Any]:
        """Produce the Lineage report."""
        return {
            "summary": {
                "status": self._status,
                "total_activities": len(self._lineage),
            },
            "lineage": list(self._lineage),
        }


# ---------------------------------------------------------------------------
# observe() HOF
# ---------------------------------------------------------------------------

def observe(name: str, fn: Callable, accumulator: Accumulator, *args, **kwargs) -> Any:
    """
    Execute `fn` as a named Activity, recording inputs/outputs.

    - Snapshots inputs before calling fn (truncated + masked).
    - On success: records outputs, returns result.
    - On failure: records error, re-raises exception unchanged (Rule 5).
    """
    inputs_snapshot = _snapshot_args(args, kwargs)
    accumulator.record_start(name, inputs_snapshot)
    try:
        result = fn(*args, **kwargs)
        accumulator.record_success(name, result)
        return result
    except Exception as e:
        accumulator.record_failure(name, str(e))
        raise
