# views/refinery/surgical_ui.py
import streamlit as st
from typing import List, Dict, Optional
from utils.page_mapping import PageMappingEngine, PageMapping, RangeMapping, RangeMappingEngine

ITEMS_PER_PAGE = 10

# =============================================================================
# Range-Based Surgical Mapping UI
# =============================================================================

@st.fragment
def render_range_mapping_section(source_file: str, source_start: int, source_end: int,
                                  replacement_files: List[str], replacement_pages_map: Dict[str, int],
                                  key_prefix: str = "surg_range"):
    """
    Renders a dynamic list of range mapping rows for bulk surgical replacement.

    Each row defines:
    - Source Start/End: pages in the existing table to DELETE
    - Replacement Start/End: pages in the PDF to EXTRACT and INSERT

    The result is stored in st.session_state['surgical_range_result'] with keys:
    - is_valid: bool
    - replacement_file: str
    - replacement_pages: int
    - range_mappings: List[Dict] with keys source_start, source_end,
                      replacement_start, replacement_end
    """
    st.markdown("#### Page Mapping Configuration")

    available_replacements = [f for f in replacement_files if f in replacement_pages_map]
    if not available_replacements:
        st.warning("No replacement PDFs available in the target table.")
        st.session_state['surgical_range_result'] = {'is_valid': False}
        return

    sel_key = f"{key_prefix}_replacement_file"
    if sel_key not in st.session_state:
        st.session_state[sel_key] = available_replacements[0]

    # Ref: https://docs.streamlit.io/develop/api-reference/widgets/st.selectbox
    # The `help` parameter renders a tooltip (ℹ icon) next to the widget label.
    # This provides inline guidance without cluttering the UI.
    replacement_file = st.selectbox(
        "Replacement PDF (from chunked PDFs)",
        available_replacements,
        index=available_replacements.index(st.session_state[sel_key]) if st.session_state[sel_key] in available_replacements else 0,
        key=sel_key,
        # Info icon tooltip — MVP requirement #3
        help="Select the chunked PDF whose pages will replace the target pages in the table."
    )

    if not replacement_file:
        st.session_state['surgical_range_result'] = {'is_valid': False}
        return

    replacement_page_count = replacement_pages_map.get(replacement_file, 1)
    st.caption(f"{replacement_file} has {replacement_page_count} pages")

    # Initialize range mappings in session state
    mappings_key = f"{key_prefix}_mappings"

    # Track the file and range that produced the current mappings.
    # This mirrors the pattern used in the sibling render_page_mapping_section
    # at surgical_ui.py:39-46 (verified against cloned repo line 42).
    # Without the file guard, switching from a 50-page to a 5-page replacement PDF
    # leaves stale mappings with values >5, causing number_input max_value violations.
    current_rep = st.session_state.get(f"{key_prefix}_current_replacement")
    current_rng = st.session_state.get(f"{key_prefix}_current_range")

    if (mappings_key not in st.session_state
            or current_rep != replacement_file
            or current_rng != (source_start, source_end)):
        # Default: one mapping covering the full source range.
        # source_start/source_end come from the parent scope (MVP requirement #2: inherited defaults).
        st.session_state[mappings_key] = [{
            'source_start': source_start,
            'source_end': source_end,
            'replacement_start': 1,
            'replacement_end': min(source_end - source_start + 1, replacement_page_count)
        }]
        st.session_state[f"{key_prefix}_current_replacement"] = replacement_file
        st.session_state[f"{key_prefix}_current_range"] = (source_start, source_end)

    mappings = st.session_state[mappings_key]

    st.markdown("**Define range mappings:** Each row replaces Source pages (in the table) with Replacement pages (from the PDF).")

    # Render range mapping rows
    cols = st.columns([1, 1, 1, 1, 0.5])
    cols[0].markdown("**Source Start**")
    cols[1].markdown("**Source End**")
    cols[2].markdown("**Repl. Start**")
    cols[3].markdown("**Repl. End**")
    cols[4].markdown("**Del**")

    rows_to_remove = []
    for i, m in enumerate(mappings):
        cols = st.columns([1, 1, 1, 1, 0.5])
        m['source_start'] = cols[0].number_input(
            f"src_s_{i}", value=int(m['source_start']), min_value=1, step=1,
            key=f"{key_prefix}_src_s_{i}", label_visibility="collapsed"
        )
        m['source_end'] = cols[1].number_input(
            f"src_e_{i}", value=int(m['source_end']), min_value=1, step=1,
            key=f"{key_prefix}_src_e_{i}", label_visibility="collapsed"
        )
        m['replacement_start'] = cols[2].number_input(
            f"rep_s_{i}", value=int(m['replacement_start']), min_value=1, max_value=replacement_page_count, step=1,
            key=f"{key_prefix}_rep_s_{i}", label_visibility="collapsed"
        )
        m['replacement_end'] = cols[3].number_input(
            f"rep_e_{i}", value=int(m['replacement_end']), min_value=1, max_value=replacement_page_count, step=1,
            key=f"{key_prefix}_rep_e_{i}", label_visibility="collapsed"
        )
        if cols[4].button("🗑", key=f"{key_prefix}_del_{i}", help="Remove this range"):
            rows_to_remove.append(i)

    # Remove deleted rows
    for idx in sorted(rows_to_remove, reverse=True):
        mappings.pop(idx)

    # Add row button
    if st.button("➕ Add Range", key=f"{key_prefix}_add"):
        mappings.append({
            'source_start': source_start,
            'source_end': source_end,
            'replacement_start': 1,
            'replacement_end': min(source_end - source_start + 1, replacement_page_count)
        })

    st.session_state[mappings_key] = mappings

    # Validate
    range_objs = [RangeMapping(
        source_start=m['source_start'], source_end=m['source_end'],
        replacement_start=m['replacement_start'], replacement_end=m['replacement_end']
    ) for m in mappings]

    is_valid, errors = RangeMappingEngine.validate(range_objs, replacement_page_count)

    # Show delta preview — MVP requirement #4: color-coded output.
    # Ref: https://docs.streamlit.io/develop/api-reference/text/st.markdown
    # Native color syntax :color[text] works in Streamlit >=1.28.0 (no unsafe_allow_html needed).
    # Using palette colors (green/red) rather than custom HEX to ensure theme compatibility
    # in both light and dark Streamlit themes.
    if is_valid:
        st.info("**Delta Preview:**")
        for i, m in enumerate(mappings):
            delta = RangeMappingEngine.compute_delta(range_objs[i])
            sign = "+" if delta >= 0 else ""
            # Color the delta value: green for expansion, red for contraction.
            # The :color[text] syntax is native to st.markdown — no HTML, no unsafe_allow_html.
            if delta > 0:
                delta_text = f":green[{sign}{delta}]"
            elif delta < 0:
                delta_text = f":red[{sign}{delta}]"
            else:
                delta_text = f"{sign}{delta}"
            st.markdown(
                f"Range {i+1}: Replace table pages {m['source_start']}-{m['source_end']} "
                f"with PDF pages {m['replacement_start']}-{m['replacement_end']} "
                f"→ delta: {delta_text}"
            )

    if errors:
        for err in errors:
            st.error(err)

    st.session_state['surgical_range_result'] = {
        'source_file': source_file,
        'source_range': (source_start, source_end),
        'replacement_file': replacement_file,
        'replacement_pages': replacement_page_count,
        'range_mappings': mappings,
        'is_valid': is_valid,
    }
