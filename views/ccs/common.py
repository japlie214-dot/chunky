# views/ccs/common.py
# Shared helpers for the Create Search Service wizard.
# Header, navigation, _jbv/_jbsync, context, presets.

import re
import streamlit as st
from logger_config import log_action
from utils.constants import (
    DEFAULT_DB, DEFAULT_SCHEMA, DEFAULT_STAGE,
    DEFAULT_TARGET_TABLE, DEFAULT_IMPORTED_TABLE_NAME,
)

# -----------------------------------------------------------------------------
# Styled header (st.html — hybrid approach, works in Snowflake)
# -----------------------------------------------------------------------------

_HEADER_HTML = """
<style>
  .wizard-hero {{
    background: linear-gradient(135deg, {grad_start} 0%, {grad_end} 100%);
    border-radius: 14px; padding: 24px 28px; color: white;
    box-shadow: 0 6px 24px rgba(0,0,0,0.18); margin-bottom: 20px;
  }}
  .wizard-hero h2 {{ margin: 0 0 4px 0; font-size: 1.35em; }}
  .wizard-hero p  {{ margin: 0; opacity: 0.88; font-size: 0.88em; }}
  .step-badge {{
    display: inline-block; background: rgba(255,255,255,0.25);
    border-radius: 10px; padding: 2px 10px; font-size: 0.72em;
    font-weight: 700; margin-left: 8px;
  }}
</style>
<div class="wizard-hero">
  <h2>{icon} {title} <span class="step-badge">Step {step} of 4</span></h2>
  <p>{subtitle}</p>
</div>
"""

_STEP_COLORS = {1: ("#667eea", "#764ba2"), 2: ("#f093fb", "#f5576c"),
                3: ("#4facfe", "#00f2fe"), 4: ("#fa709a", "#fee140"),
                5: ("#43e97b", "#38f9d7")}
_STEP_CONTENT = {
    1: ("⚙️", "Service Setup", "Configure the role, database, schema, and name for your Cortex Search Service."),
    2: ("📂", "Job Builder", "Select files, configure intent, scope, strategy, and parameters. Add one or more jobs."),
    3: ("🚀", "Job Queue & Execution", "Review queued jobs, run the batch, view results, and inspect table columns."),
    4: ("🕵️", "QA Studio & Tools", "Inspect, edit, and repair chunks. Run maintenance tools."),
    5: ("🔍", "Search Service Configuration", "Configure search columns, attributes, target lag, and create the Cortex Search Service."),
}


def render_header(step: int):
    icon, title, subtitle = _STEP_CONTENT[step]
    g1, g2 = _STEP_COLORS[step]
    st.html(_HEADER_HTML.format(icon=icon, title=title, subtitle=subtitle,
                                 step=step, grad_start=g1, grad_end=g2))


# -----------------------------------------------------------------------------
# Pagination
# -----------------------------------------------------------------------------

def get_page():
    return st.session_state.get("cssw_page", 1)


def set_page(p):
    st.session_state.cssw_page = p


def nav_buttons(can_next, next_label="Next ➡️", show_back=True):
    c1, _, c3 = st.columns([1, 2, 1])
    with c1:
        if show_back and st.button("⬅️ Back"):
            set_page(get_page() - 1)
            st.rerun()
    with c3:
        if st.button(next_label, disabled=not can_next, type="primary"):
            set_page(get_page() + 1)
            st.rerun()


# -----------------------------------------------------------------------------
# Context helper
# -----------------------------------------------------------------------------

def ctx():
    """Safe access to auth_context — never throws KeyError."""
    return st.session_state.get("auth_context", {})


# -----------------------------------------------------------------------------
# Intent presets — COPIED from views/refinery/tab_config.py
# -----------------------------------------------------------------------------

PRESET_OPTIONS = ["Add New Pages", "Replace Specific Pages", "Replace All Data"]
_MODE_SCOPE_TO_PRESET = {
    ("APPEND", "Full Doc"): "Add New Pages",
    ("SURGICAL", "Page Range"): "Replace Specific Pages",
    ("OVERWRITE", "Full Doc"): "Replace All Data",
}


def sync_preset_to_state(preset_label):
    mapping = {"Add New Pages": ("APPEND", "Full Doc"),
               "Replace Specific Pages": ("SURGICAL", "Page Range"),
               "Replace All Data": ("OVERWRITE", "Full Doc")}
    mode, scope = mapping[preset_label]
    st.session_state["cssw_mode"] = mode
    st.session_state["cssw_scope"] = scope


def derive_preset_label(mode, scope):
    return _MODE_SCOPE_TO_PRESET.get((mode, scope))


# -----------------------------------------------------------------------------
# Job Builder helpers — COPIED from views/refinery/tab_config.py
# -----------------------------------------------------------------------------

_JB_DEFAULTS = {
    "file": "", "table_name": DEFAULT_TARGET_TABLE, "link": "",
    "pstart": 1, "pend": 10, "grant_roles": "",
    "layout": True, "vision": True, "chunk": 8000, "overlap": 20,
    "group": "", "role": "", "svc_name": "CSS_",
}


def jb_init():
    for field, default in _JB_DEFAULTS.items():
        key = f"_jbv_{field}"
        if key not in st.session_state:
            st.session_state[key] = default


def jbv(field):
    return st.session_state.get(f"_jbv_{field}", _JB_DEFAULTS.get(field))


def jbsync(field, value):
    st.session_state[f"_jbv_{field}"] = value


# -----------------------------------------------------------------------------
# Stage file listing — COPIED from tab_config.py
# -----------------------------------------------------------------------------

def list_stage_files(session, stage_path):
    try:
        files = session.sql(f"LIST {stage_path} PATTERN='.*\\.pdf'").collect()
        prefix = stage_path.lstrip("@").split(".")[-1] + "/"
        result = []
        for f in files:
            fname = f["name"]
            if fname.lower().startswith(prefix.lower()):
                relative = fname[len(prefix):]
                result.append(relative if relative else fname)
            else:
                result.append(fname)
        return sorted(result)
    except Exception as e:
        log_action("STAGE_LIST_ERROR", {"stage": stage_path, "error": str(e)}, level="WARNING")
        return []


def group_by_directory(files):
    groups = {}
    for f in files:
        d = f.rsplit("/", 1)[0] if "/" in f else "(root)"
        groups.setdefault(d, []).append(f)
    return {k: sorted(v) for k, v in sorted(groups.items())}


def normalize_pdf_to_table_name(filename: str) -> str:
    """Normalize a PDF filename to a valid Snowflake table name.

    Rules:
    - Strip leading/trailing whitespace (defensive — filenames from LIST
      shouldn't have any, but user-pasted strings might)
    - Strip .pdf extension (case-insensitive)
    - Convert to ALL CAPS
    - Replace all non-alphanumeric characters (except _) with nothing
    - Replace all spaces with _
    - Collapse consecutive underscores
    - Strip leading/trailing underscores
    - Ensure result is not empty

    Examples:
        'My Report (2024).pdf' → 'MY_REPORT_2024'
        'Q1-Q2 Financials.pdf' → 'Q1_Q2_FINANCIALS'
        'report_final.pdf' → 'REPORT_FINAL'
    """
    import re as _re
    # Defensive: strip leading/trailing whitespace so the .pdf suffix check
    # works even on user-pasted input. Snowflake LIST output never has
    # surrounding whitespace, but this is cheap insurance.
    name = filename.strip() if isinstance(filename, str) else (filename or "")
    # Strip .pdf extension (case-insensitive)
    if name.lower().endswith('.pdf'):
        name = name[:-4]
    # Convert to uppercase
    name = name.upper()
    # Replace common separators (spaces, hyphens, dots) with underscores
    name = name.replace(' ', '_').replace('-', '_').replace('.', '_')
    # Remove all non-alphanumeric characters except underscore
    name = _re.sub(r'[^A-Z0-9_]', '', name)
    # Collapse consecutive underscores
    name = _re.sub(r'_+', '_', name)
    # Strip leading/trailing underscores
    name = name.strip('_')
    # Fallback if empty
    if not name:
        name = DEFAULT_IMPORTED_TABLE_NAME
    return name
