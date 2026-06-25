import streamlit as st
import uuid
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
    replacement_file = st.selectbox(
        "Replacement PDF (from chunked PDFs)",
        available_replacements,
        index=available_replacements.index(st.session_state[sel_key]) if st.session_state[sel_key] in available_replacements else 0,
        key=sel_key,
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
    # Without the file guard, switching from a 50-page to a 5-page replacement PDF
    # leaves stale mappings with values >5, causing number_input max_value violations.
    current_rep = st.session_state.get(f"{key_prefix}_current_replacement")
    current_rng = st.session_state.get(f"{key_prefix}_current_range")

    if (mappings_key not in st.session_state
            or current_rep != replacement_file
            or current_rng != (source_start, source_end)):
        # Default: one mapping covering the full source range.
        # Each mapping gets a stable _uid for widget key binding.
        # This prevents Streamlit widget state aliasing when rows are deleted:
        # without _uid, deleting row 0 causes row 1's key to shift from
        # "{prefix}_src_s_1" to "{prefix}_src_s_0", and Streamlit re-applies
        # the deleted row's widget state to the new row.
        st.session_state[mappings_key] = [{
            '_uid': uuid.uuid4().hex[:12],
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
        # Ensure _uid exists for rows created before the uid fix was applied.
        # This handles the edge case where session_state contains legacy mappings
        # from a previous run that didn't include _uid.
        if '_uid' not in m:
            m['_uid'] = uuid.uuid4().hex[:12]

        cols = st.columns([1, 1, 1, 1, 0.5])
        # Widget keys use m['_uid'] (stable) instead of i (unstable).
        # When a row is deleted, i shifts for all subsequent rows, but _uid
        # stays constant. This prevents Streamlit from re-applying deleted
        # row's widget state to the wrong row.
        uid = m['_uid']
        m['source_start'] = cols[0].number_input(
            f"src_s_{uid}", value=int(m['source_start']), min_value=1, step=1,
            key=f"{key_prefix}_src_s_{uid}", label_visibility="collapsed"
        )
        m['source_end'] = cols[1].number_input(
            f"src_e_{uid}", value=int(m['source_end']), min_value=1, step=1,
            key=f"{key_prefix}_src_e_{uid}", label_visibility="collapsed"
        )
        m['replacement_start'] = cols[2].number_input(
            f"rep_s_{uid}", value=int(m['replacement_start']), min_value=1, max_value=replacement_page_count, step=1,
            key=f"{key_prefix}_rep_s_{uid}", label_visibility="collapsed"
        )
        m['replacement_end'] = cols[3].number_input(
            f"rep_e_{uid}", value=int(m['replacement_end']), min_value=1, max_value=replacement_page_count, step=1,
            key=f"{key_prefix}_rep_e_{uid}", label_visibility="collapsed"
        )
        if cols[4].button("🗑", key=f"{key_prefix}_del_{uid}", help="Remove this range"):
            rows_to_remove.append(i)

    # Remove deleted rows — guarded to prevent infinite rerun loop.
    # Without the guard, st.rerun executes on every render pass because
    # the for-loop above runs unconditionally (Streamlit is top-to-bottom).
    if rows_to_remove:
        for idx in sorted(rows_to_remove, reverse=True):
            mappings.pop(idx)
        st.session_state[mappings_key] = mappings
        # Ref: https://docs.streamlit.io/develop/api-reference/execution-flow/st.rerun
        # scope="fragment" reruns only this @st.fragment, preserving parent
        # job configuration (mode, scope, file selection, chunk params).
        # scope="fragment" was introduced in Streamlit 1.37.0; codebase
        # requires >=1.40.0, so this is safe.
        st.rerun(scope="fragment")

    # Add row button
    if st.button("➕ Add Range", key=f"{key_prefix}_add"):
        mappings.append({
            '_uid': uuid.uuid4().hex[:12],
            'source_start': source_start,
            'source_end': source_end,
            'replacement_start': 1,
            'replacement_end': min(source_end - source_start + 1, replacement_page_count)
        })
        st.session_state[mappings_key] = mappings
        st.rerun(scope="fragment")

    # Auto-fill next row — increments both source and replacement ranges
    # by the span of the last row. Clamps replacement end to PDF page count.
    if st.button("⚡ Auto-fill next", key=f"{key_prefix}_autofill"):
        if mappings:
            last = mappings[-1]
            span = last['source_end'] - last['source_start']
            new_src_s = last['source_end'] + 1
            new_src_e = new_src_s + span
            new_rep_s = last['replacement_end'] + 1
            new_rep_e = min(new_rep_s + span, replacement_page_count)
            # Guard: don't create a row where replacement exceeds PDF bounds.
            # Silent no-op if the replacement PDF doesn't have enough pages.
            if new_rep_s <= replacement_page_count:
                mappings.append({
                    '_uid': uuid.uuid4().hex[:12],
                    'source_start': new_src_s,
                    'source_end': new_src_e,
                    'replacement_start': new_rep_s,
                    'replacement_end': new_rep_e,
                })
                st.session_state[mappings_key] = mappings
                st.rerun(scope="fragment")

    st.session_state[mappings_key] = mappings

    # Validate — empty mappings list is invalid for SURGICAL jobs.
    # RangeMappingEngine.validate() returns is_valid=True for empty lists
    # (zero errors). This is correct for the engine (empty may be valid in
    # other contexts), but SURGICAL mode requires at least one range.
    if not mappings:
        is_valid = False
        errors = ["At least one range mapping is required for SURGICAL mode."]
    else:
        range_objs = [RangeMapping(
            source_start=m['source_start'], source_end=m['source_end'],
            replacement_start=m['replacement_start'], replacement_end=m['replacement_end']
        ) for m in mappings]
        is_valid, errors = RangeMappingEngine.validate(range_objs, replacement_page_count)

    # Show delta preview
    # Ref: https://docs.streamlit.io/develop/api-reference/text/st.markdown
    # Native :color[text] syntax works in Streamlit >=1.28.0.
    if is_valid:
        st.info("**Delta Preview:**")
        for i, m in enumerate(mappings):
            range_obj = RangeMapping(
                source_start=m['source_start'], source_end=m['source_end'],
                replacement_start=m['replacement_start'], replacement_end=m['replacement_end']
            )
            delta = RangeMappingEngine.compute_delta(range_obj)
            sign = "+" if delta >= 0 else ""
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
