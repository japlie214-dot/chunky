# utils/display_safety.py
# 32 MB Message Size Safety for Streamlit in Snowflake (Warehouse Runtime)
#
# Snowflake enforces a 32 MB limit on messages between the Streamlit backend
# and frontend. Exceeding it raises MessageSizeError. This module provides
# guards that truncate data before it reaches any st.* display command.
#
# Reference: https://docs.snowflake.com/en/developer-guide/streamlit/limitations

import streamlit as st
import json
import pandas as pd
from logger_config import log_action

# 32 MB in bytes, with 2 MB headroom for serialization overhead
MAX_MESSAGE_BYTES = 30 * 1024 * 1024  # 30 MB (safe threshold)

# Character-level approximation: 1 char ≈ 1-4 bytes in UTF-8
# Use 1 byte per char as conservative estimate for ASCII-heavy content
MAX_CHARS_APPROX = 30_000_000

# Row-level limits for DataFrames (each row serializes to JSON for frontend)
MAX_DATAFRAME_ROWS = 50_000


def _estimate_bytes(obj) -> int:
    """Estimate the serialized size of an object in bytes."""
    if obj is None:
        return 0
    if isinstance(obj, str):
        return len(obj.encode('utf-8'))
    if isinstance(obj, (int, float, bool)):
        return 8
    if isinstance(obj, dict):
        try:
            return len(json.dumps(obj, default=str).encode('utf-8'))
        except Exception:
            return sum(_estimate_bytes(k) + _estimate_bytes(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return sum(_estimate_bytes(item) for item in obj)
    if isinstance(obj, pd.DataFrame):
        # DataFrame → JSON is roughly 2-3x the CSV size; use memory_usage as proxy
        try:
            return obj.memory_usage(deep=True).sum()
        except Exception:
            return len(obj) * 100  # rough fallback
    if hasattr(obj, '__len__'):
        try:
            return len(str(obj).encode('utf-8'))
        except Exception:
            return 0
    return 0


def _truncate_text(text: str, max_chars: int = MAX_CHARS_APPROX,
                   label: str = "content") -> str:
    """Truncate text to fit within max_chars, appending a warning."""
    if text is None:
        return ""
    if not text or len(text.encode('utf-8')) <= max_chars:
        return text
    # Truncate at character level (conservative: 1 byte/char)
    truncated = text[:max_chars]
    warning = (
        f"\n\n---\n⚠️ **Content truncated** ({label}): "
        f"Original {len(text):,} chars → showing {max_chars:,} chars. "
        f"Exceeds Streamlit's 32 MB message limit."
    )
    log_action("DISPLAY_TRUNCATED", {
        "label": label,
        "original_chars": len(text),
        "truncated_to": max_chars,
        "original_bytes_est": len(text.encode('utf-8'))
    }, level="WARNING")
    return truncated + warning


def _truncate_dataframe(df: pd.DataFrame, max_rows: int = MAX_DATAFRAME_ROWS,
                        label: str = "dataframe") -> pd.DataFrame:
    """Truncate a DataFrame if it has too many rows."""
    if df is None or len(df) <= max_rows:
        return df
    truncated = df.head(max_rows)
    log_action("DATAFRAME_TRUNCATED", {
        "label": label,
        "original_rows": len(df),
        "truncated_to": max_rows
    }, level="WARNING")
    return truncated


# =============================================================================
# SAFE DISPLAY WRAPPERS
# =============================================================================

def safe_markdown(text: str, label: str = "markdown", **kwargs):
    """Display markdown with 32 MB safety truncation."""
    safe_text = _truncate_text(str(text) if text else "", label=label)
    st.markdown(safe_text, **kwargs)


def safe_code(code: str, label: str = "code", **kwargs):
    """Display code with 32 MB safety truncation."""
    safe_text = _truncate_text(str(code) if code else "", label=label)
    st.code(safe_text, **kwargs)


def safe_write(*args, label: str = "write", **kwargs):
    """Display write output with 32 MB safety truncation."""
    # st.write accepts multiple args; check total size
    total_bytes = sum(_estimate_bytes(a) for a in args)
    if total_bytes > MAX_MESSAGE_BYTES:
        # Truncate string args
        safe_args = []
        for a in args:
            if isinstance(a, str):
                safe_args.append(_truncate_text(a, label=label))
            elif isinstance(a, pd.DataFrame):
                safe_args.append(_truncate_dataframe(a, label=label))
            else:
                safe_args.append(a)
        st.write(*safe_args, **kwargs)
    else:
        st.write(*args, **kwargs)


def safe_json(data, label: str = "json", **kwargs):
    """Display JSON with 32 MB safety truncation."""
    size = _estimate_bytes(data)
    if size > MAX_MESSAGE_BYTES:
        # Truncate to a safe size
        text = json.dumps(data, default=str, indent=2)
        truncated = _truncate_text(text, label=label)
        st.code(truncated, language="json")
        log_action("JSON_TRUNCATED", {"label": label, "bytes": size}, level="WARNING")
    else:
        st.json(data, **kwargs)


def safe_dataframe(df: pd.DataFrame, label: str = "dataframe", **kwargs):
    """Display a DataFrame with 32 MB safety truncation."""
    if df is None:
        st.info("No data to display.")
        return
    safe_df = _truncate_dataframe(df, label=label)
    # Also check serialized size
    try:
        est_bytes = safe_df.memory_usage(deep=True).sum()
        if est_bytes > MAX_MESSAGE_BYTES:
            # Further reduce by sampling
            sample_size = max(100, int(len(safe_df) * MAX_MESSAGE_BYTES / est_bytes))
            safe_df = safe_df.head(sample_size)
            log_action("DATAFRAME_SIZE_REDUCED", {
                "label": label, "rows": len(safe_df), "bytes_est": est_bytes
            }, level="WARNING")
    except Exception:
        pass
    st.dataframe(safe_df, **kwargs)


def safe_data_editor(df: pd.DataFrame, label: str = "data_editor", **kwargs):
    """Display a data editor with 32 MB safety truncation."""
    if df is None:
        st.info("No data to display.")
        return None
    safe_df = _truncate_dataframe(df, label=label)
    return st.data_editor(safe_df, **kwargs)


def safe_text_area(text: str, label: str = "text_area", max_chars: int = 100_000,
                   **kwargs):
    """Display a text area with safety truncation for very large text."""
    safe_text = _truncate_text(str(text) if text else "", max_chars=max_chars,
                               label=label)
    return st.text_area(value=safe_text, **kwargs)


def safe_table(data, label: str = "table", **kwargs):
    """Display a table with 32 MB safety truncation."""
    if isinstance(data, pd.DataFrame):
        data = _truncate_dataframe(data, label=label)
    elif isinstance(data, list) and len(data) > MAX_DATAFRAME_ROWS:
        data = data[:MAX_DATAFRAME_ROWS]
        log_action("TABLE_TRUNCATED", {"label": label, "rows": len(data)}, level="WARNING")
    st.table(data, **kwargs)


def safe_download_button(label: str, data: str, file_name: str,
                         mime: str = "text/plain", **kwargs):
    """Display a download button with 32 MB safety truncation."""
    if isinstance(data, str) and len(data.encode('utf-8')) > MAX_MESSAGE_BYTES:
        data = _truncate_text(data, label=file_name)
        log_action("DOWNLOAD_TRUNCATED", {"file": file_name}, level="WARNING")
    st.download_button(label=label, data=data, file_name=file_name, mime=mime,
                       **kwargs)


def safe_html(html: str, label: str = "html", **kwargs):
    """Display HTML in components.html with 32 MB safety truncation.
    Returns the value from components.html (used for iframe→parent communication).
    """
    import streamlit.components.v1 as components
    safe_text = _truncate_text(str(html) if html else "", label=label,
                               max_chars=MAX_CHARS_APPROX)
    return components.html(safe_text, **kwargs)


def check_message_size(obj, label: str = "data") -> bool:
    """
    Pre-check if an object would exceed the 32 MB limit.
    Returns True if safe, False if it would exceed.
    Logs a warning if exceeded.
    """
    size = _estimate_bytes(obj)
    if size > MAX_MESSAGE_BYTES:
        log_action("MESSAGE_SIZE_EXCEEDED", {
            "label": label,
            "estimated_bytes": size,
            "limit_bytes": MAX_MESSAGE_BYTES
        }, level="WARNING")
        return False
    return True


def get_size_warning(obj, label: str = "data") -> str | None:
    """
    Returns a warning string if the object exceeds the 32 MB limit,
    or None if it's safe. Useful for conditional display logic.
    """
    size = _estimate_bytes(obj)
    if size > MAX_MESSAGE_BYTES:
        size_mb = size / (1024 * 1024)
        return (
            f"⚠️ **{label}** is {size_mb:.1f} MB — exceeds the "
            f"32 MB Streamlit message limit. Data will be truncated."
        )
    return None
