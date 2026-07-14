# views/webapp_demo.py
# Demo page showcasing a form inside Streamlit using native widgets.
#
# Previous version used st.components.v2 (CCv2) which is NOT supported in
# Snowflake warehouse runtime. This rewrite uses Option 4: native Streamlit
# widgets that replicate the same form behavior — no HTML/JS required.
#
# Reference: HTML_lesson_learnt.md §11 (Snowflake warehouse runtime limitations)

import streamlit as st
from logger_config import log_action

# -----------------------------------------------------------------------------
# Department options (shared between form and display)
# -----------------------------------------------------------------------------
_DEPARTMENT_OPTIONS = {
    "": "-- Select --",
    "engineering": "Engineering",
    "data-science": "Data Science",
    "analytics": "Analytics",
    "product": "Product",
    "other": "Other",
}

_DEPARTMENT_KEYS = list(_DEPARTMENT_OPTIONS.keys())
_DEPARTMENT_LABELS = list(_DEPARTMENT_OPTIONS.values())

_PRIORITY_OPTIONS = ["low", "medium", "high"]
_PRIORITY_LABELS = {"low": "🟢 Low", "medium": "🟡 Medium", "high": "🔴 High"}


# -----------------------------------------------------------------------------
# Display helper (markdown table — avoids widget key locking issues)
# -----------------------------------------------------------------------------

def _render_saved_values(saved: dict):
    """Render the saved form data using a markdown table."""
    if not saved:
        st.info("📝 No form data saved yet. Fill out the form above and click **Save to Streamlit**.")
        return

    name = saved.get("name", "—") or "—"
    email = saved.get("email", "—") or "—"
    role = saved.get("role", "—") or "—"
    dept = _DEPARTMENT_OPTIONS.get(saved.get("department", ""), "—")
    prio = saved.get("priority", "medium").capitalize()
    notes = saved.get("notes", "—") or "—"
    notify = saved.get("notify", False)

    prio_colors = {"Low": "green", "Medium": "orange", "High": "red"}
    prio_color = prio_colors.get(prio, "gray")
    notify_text = "✅ Enabled" if notify else "❌ Disabled"

    lines = [
        "| Field | Value |",
        "|-------|-------|",
        f"| **Name** | {name} |",
        f"| **Email** | {email} |",
        f"| **Role** | {role} |",
        f"| **Department** | {dept} |",
        f"| **Priority** | :{prio_color}[{prio}] |",
        f"| **Notes** | {notes} |",
        f"| **Notifications** | {notify_text} |",
    ]
    st.markdown("\n".join(lines))

    with st.expander("📄 Raw JSON"):
        st.json(saved)


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------

def render_webapp_demo():
    """
    Main entry point for the Webapp Demo page.
    Uses native Streamlit widgets (Option 4) for full Snowflake compatibility.
    """
    st.title("🌐 Webapp Demo")
    log_action("NAVIGATE", "Visited Webapp Demo Page")

    st.markdown("""
    This page demonstrates a **form built with native Streamlit widgets** — fully
    compatible with Snowflake warehouse runtime. No HTML/JS iframes required.

    Fill out the form below and click **Save to Streamlit** to persist data.
    Saved values survive tab navigation and are displayed in the read-only panel.
    """)

    st.markdown("---")

    # Initialize persisted form data
    if "webapp_form_data" not in st.session_state:
        st.session_state.webapp_form_data = {}

    saved = st.session_state.webapp_form_data

    # --- Section 1: Form ---
    st.markdown("#### 📝 Form")

    with st.form("webapp_form", clear_on_submit=False):
        col1, col2 = st.columns(2)
        name = col1.text_input(
            "Full Name",
            value=saved.get("name", ""),
            placeholder="John Doe",
        )
        email = col2.text_input(
            "Email",
            value=saved.get("email", ""),
            placeholder="john@example.com",
        )

        col3, col4 = st.columns(2)
        role = col3.text_input(
            "Role",
            value=saved.get("role", ""),
            placeholder="Data Engineer",
        )
        dept_index = 0
        current_dept = saved.get("department", "")
        if current_dept in _DEPARTMENT_KEYS:
            dept_index = _DEPARTMENT_KEYS.index(current_dept)
        department = col4.selectbox(
            "Department",
            options=_DEPARTMENT_KEYS,
            format_func=lambda k: _DEPARTMENT_OPTIONS[k],
            index=dept_index,
        )

        prio_index = _PRIORITY_OPTIONS.index(saved.get("priority", "medium"))
        priority = st.radio(
            "Priority",
            options=_PRIORITY_OPTIONS,
            format_func=lambda k: _PRIORITY_LABELS[k],
            index=prio_index,
            horizontal=True,
        )

        notes = st.text_area(
            "Notes",
            value=saved.get("notes", ""),
            placeholder="Any additional notes...",
        )

        notify = st.checkbox(
            "Send email notifications",
            value=saved.get("notify", False),
        )

        submitted = st.form_submit_button("💾 Save to Streamlit", type="primary")

    if submitted:
        form_data = {
            "name": name,
            "email": email,
            "role": role,
            "department": department,
            "priority": priority,
            "notes": notes,
            "notify": notify,
        }
        st.session_state.webapp_form_data = form_data
        log_action("WEBAPP_FORM_SAVE", {"source": "native_form", "name": name})
        st.success("✅ Form saved! Check the display below.")
        st.rerun()

    st.markdown("---")

    # --- Section 2: Display saved values ---
    st.markdown("#### 📊 Saved Form Values")
    _render_saved_values(st.session_state.webapp_form_data)

    # --- Section 3: Clear ---
    st.markdown("---")
    if st.session_state.webapp_form_data and st.button("🗑️ Clear All Saved Data"):
        st.session_state.webapp_form_data = {}
        st.rerun()
