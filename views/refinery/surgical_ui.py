# views/refinery/surgical_ui.py
import streamlit as st
from typing import List, Dict, Optional
from utils.page_mapping import PageMappingEngine, PageMapping

ITEMS_PER_PAGE = 10

@st.fragment
def render_page_mapping_section(source_file: str, source_start: int, source_end: int, replacement_files: List[str], replacement_pages_map: Dict[str, int], key_prefix: str = "surg"):
    st.markdown("#### 📑 Page Mapping Configuration")
    
    available_replacements = [f for f in replacement_files if f in replacement_pages_map]
    if not available_replacements:
        st.warning("No replacement PDFs available in the target table.")
        st.session_state['surgical_mapping_result'] = {'is_valid': False}
        return

    sel_key = f"{key_prefix}_replacement_file"
    if sel_key not in st.session_state:
        st.session_state[sel_key] = available_replacements[0] if available_replacements else None
    
    replacement_file = st.selectbox(
        "Replacement PDF (from chunked PDFs)",
        available_replacements,
        index=available_replacements.index(st.session_state[sel_key]) if st.session_state[sel_key] in available_replacements else 0,
        key=sel_key
    )
    
    if not replacement_file:
        st.session_state['surgical_mapping_result'] = {'is_valid': False}
        return

    replacement_page_count = replacement_pages_map.get(replacement_file, 1)
    source_pages = list(range(source_start, source_end + 1))
    total_pages = len(source_pages)
    st.caption(f"📄 {replacement_file} has {replacement_page_count} pages")

    mappings_key = f"{key_prefix}_mappings"
    current_rep = st.session_state.get(f"{key_prefix}_current_replacement")
    current_rng = st.session_state.get(f"{key_prefix}_current_range")

    if mappings_key not in st.session_state or current_rep != replacement_file or current_rng != (source_start, source_end):
        default_mappings = PageMappingEngine.calculate_default_mappings(source_start, source_end, replacement_page_count)
        st.session_state[mappings_key] = [{'source': m.source_page, 'target': m.target_page, 'is_auto': m.is_auto} for m in default_mappings]
        st.session_state[f"{key_prefix}_current_replacement"] = replacement_file
        st.session_state[f"{key_prefix}_current_range"] = (source_start, source_end)

    mappings = st.session_state[mappings_key]
    total_page_count = (total_pages + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    page_state_key = f"{key_prefix}_mapping_page"
    
    if page_state_key not in st.session_state:
        st.session_state[page_state_key] = 1
    current_page = st.session_state[page_state_key]

    col_prev, col_info, col_next = st.columns([1, 2, 1])
    
    def go_prev():
        st.session_state[page_state_key] = max(1, current_page - 1)
        
    def go_next():
        st.session_state[page_state_key] = min(total_page_count, current_page + 1)

    with col_prev:
        st.button("◀ Prev", key=f"{key_prefix}_prev", disabled=(current_page <= 1), on_click=go_prev)
    with col_info:
        st.markdown(f"**Page {current_page} of {total_page_count}**")
    with col_next:
        st.button("Next ▶", key=f"{key_prefix}_next", disabled=(current_page >= total_page_count), on_click=go_next)

    start_idx = (current_page - 1) * ITEMS_PER_PAGE
    end_idx = min(start_idx + ITEMS_PER_PAGE, total_pages)
    
    st.markdown("---")
    target_options = list(range(1, replacement_page_count + 1))
    
    for i in range(start_idx, end_idx):
        mapping = mappings[i]
        col_s, col_a, col_t = st.columns([1, 1, 2])
        col_s.markdown(f"**Source Page {mapping['source']}**")
        col_a.markdown("➜")
        new_target = col_t.selectbox(
            "Target Page",
            target_options,
            index=min(mapping['target'] - 1, len(target_options) - 1),
            key=f"{key_prefix}_target_{mapping['source']}",
            label_visibility="collapsed"
        )
        if new_target != mapping['target']:
            mappings[i]['target'] = new_target
            mappings[i]['is_auto'] = False

    st.session_state[mappings_key] = mappings
    page_mapping_objs = [PageMapping(source_page=m['source'], target_page=m['target'], is_auto=m['is_auto']) for m in mappings]
    
    duplicates = PageMappingEngine.detect_duplicates(page_mapping_objs)
    is_valid, errors = PageMappingEngine.validate_mappings(page_mapping_objs, replacement_page_count)
    
    if duplicates:
        st.warning(f"⚠️ Duplicate Mappings: {[d['target_page'] for d in duplicates]} have multiple source pages mapped.")
    if errors:
        for err in errors:
            st.error(err)

    st.session_state['surgical_mapping_result'] = {
        'source_file': source_file,
        'source_range': (source_start, source_end),
        'replacement_file': replacement_file,
        'replacement_pages': replacement_page_count,
        'page_mappings': mappings,
        'is_valid': is_valid and len(duplicates) == 0,
        'has_warnings': len(duplicates) > 0
    }

# =============================================================================
# Range-Based Surgical Mapping UI (new feature)
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
    from utils.page_mapping import RangeMapping, RangeMappingEngine

    st.markdown("#### 📑 Range Mapping Configuration")

    available_replacements = [f for f in replacement_files if f in replacement_pages_map]
    if not available_replacements:
        st.warning("No replacement PDFs available in the target table.")
        st.session_state['surgical_range_result'] = {'is_valid': False}
        return

    sel_key = f"{key_prefix}_replacement_file"
    if sel_key not in st.session_state:
        st.session_state[sel_key] = available_replacements[0]

    replacement_file = st.selectbox(
        "Replacement PDF (from chunked PDFs)",
        available_replacements,
        index=available_replacements.index(st.session_state[sel_key]) if st.session_state[sel_key] in available_replacements else 0,
        key=sel_key
    )

    if not replacement_file:
        st.session_state['surgical_range_result'] = {'is_valid': False}
        return

    replacement_page_count = replacement_pages_map.get(replacement_file, 1)
    st.caption(f"📄 {replacement_file} has {replacement_page_count} pages")

    # Initialize range mappings in session state
    mappings_key = f"{key_prefix}_mappings"
    if mappings_key not in st.session_state:
        # Default: one mapping covering the full source range
        st.session_state[mappings_key] = [{
            'source_start': source_start,
            'source_end': source_end,
            'replacement_start': 1,
            'replacement_end': min(source_end - source_start + 1, replacement_page_count)
        }]

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

    # Show delta preview
    if is_valid:
        st.info("📋 **Delta Preview:**")
        for i, m in enumerate(mappings):
            delta = RangeMappingEngine.compute_delta(range_objs[i])
            sign = "+" if delta >= 0 else ""
            st.write(
                f"Range {i+1}: Replace table pages {m['source_start']}-{m['source_end']} "
                f"with PDF pages {m['replacement_start']}-{m['replacement_end']} "
                f"→ delta: {sign}{delta}"
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
