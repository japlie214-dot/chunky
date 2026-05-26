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
