# views/webapp_demo.py
# Demo page showcasing HTML+CSS+JS webapp running within Streamlit
# The HTML form saves values that are displayed back in Streamlit

import streamlit as st
import streamlit.components.v1 as components
import json
from logger_config import log_action


def _build_html_form(saved_values: dict = None) -> str:
    """
    Build the HTML+CSS+JS form that runs inside Streamlit's iframe.
    Uses postMessage to send form data back to the parent Streamlit frame.
    
    Args:
        saved_values: Previously saved form values to pre-populate fields
    
    Returns:
        Complete HTML string for embedding
    """
    name_val = saved_values.get("name", "") if saved_values else ""
    email_val = saved_values.get("email", "") if saved_values else ""
    role_val = saved_values.get("role", "") if saved_values else ""
    dept_val = saved_values.get("department", "") if saved_values else ""
    priority_val = saved_values.get("priority", "medium") if saved_values else "medium"
    notes_val = saved_values.get("notes", "") if saved_values else ""
    notify_val = saved_values.get("notify", False) if saved_values else False

    notify_checked = "checked" if notify_val else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
    * {{
        box-sizing: border-box;
        margin: 0;
        padding: 0;
    }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 20px;
    }}
    .container {{
        background: white;
        border-radius: 16px;
        box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        padding: 40px;
        width: 100%;
        max-width: 600px;
    }}
    h1 {{
        color: #333;
        font-size: 1.8em;
        margin-bottom: 8px;
    }}
    .subtitle {{
        color: #666;
        font-size: 0.95em;
        margin-bottom: 30px;
    }}
    .form-group {{
        margin-bottom: 20px;
    }}
    label {{
        display: block;
        color: #444;
        font-weight: 600;
        margin-bottom: 6px;
        font-size: 0.9em;
    }}
    input[type="text"],
    input[type="email"],
    select,
    textarea {{
        width: 100%;
        padding: 12px 16px;
        border: 2px solid #e0e0e0;
        border-radius: 8px;
        font-size: 1em;
        transition: border-color 0.3s, box-shadow 0.3s;
        outline: none;
    }}
    input:focus,
    select:focus,
    textarea:focus {{
        border-color: #667eea;
        box-shadow: 0 0 0 3px rgba(102,126,234,0.2);
    }}
    textarea {{
        resize: vertical;
        min-height: 80px;
    }}
    .row {{
        display: flex;
        gap: 16px;
    }}
    .row .form-group {{
        flex: 1;
    }}
    .priority-options {{
        display: flex;
        gap: 12px;
        margin-top: 6px;
    }}
    .priority-option {{
        flex: 1;
    }}
    .priority-option input[type="radio"] {{
        display: none;
    }}
    .priority-option label {{
        display: block;
        text-align: center;
        padding: 10px;
        border: 2px solid #e0e0e0;
        border-radius: 8px;
        cursor: pointer;
        transition: all 0.3s;
        font-weight: 500;
    }}
    .priority-option input[type="radio"]:checked + label {{
        border-color: #667eea;
        background: #667eea;
        color: white;
    }}
    .priority-low label {{ color: #28a745; }}
    .priority-low input:checked + label {{ background: #28a745; border-color: #28a745; }}
    .priority-medium label {{ color: #ffc107; }}
    .priority-medium input:checked + label {{ background: #ffc107; border-color: #ffc107; color: #333; }}
    .priority-high label {{ color: #dc3545; }}
    .priority-high input:checked + label {{ background: #dc3545; border-color: #dc3545; }}
    .checkbox-group {{
        display: flex;
        align-items: center;
        gap: 10px;
        margin-top: 6px;
    }}
    .checkbox-group input[type="checkbox"] {{
        width: 20px;
        height: 20px;
        accent-color: #667eea;
    }}
    .checkbox-group label {{
        margin: 0;
        font-weight: 500;
    }}
    .btn-row {{
        display: flex;
        gap: 12px;
        margin-top: 30px;
    }}
    .btn {{
        flex: 1;
        padding: 14px;
        border: none;
        border-radius: 8px;
        font-size: 1em;
        font-weight: 600;
        cursor: pointer;
        transition: transform 0.2s, box-shadow 0.2s;
    }}
    .btn:active {{
        transform: scale(0.98);
    }}
    .btn-primary {{
        background: linear-gradient(135deg, #667eea, #764ba2);
        color: white;
        box-shadow: 0 4px 15px rgba(102,126,234,0.4);
    }}
    .btn-primary:hover {{
        box-shadow: 0 6px 20px rgba(102,126,234,0.6);
    }}
    .btn-secondary {{
        background: #f0f0f0;
        color: #555;
    }}
    .btn-secondary:hover {{
        background: #e0e0e0;
    }}
    .status {{
        margin-top: 16px;
        padding: 12px;
        border-radius: 8px;
        display: none;
        font-weight: 500;
    }}
    .status.success {{
        background: #d4edda;
        color: #155724;
        display: block;
    }}
    .status.error {{
        background: #f8d7da;
        color: #721c24;
        display: block;
    }}
    .badge {{
        display: inline-block;
        padding: 2px 8px;
        border-radius: 12px;
        font-size: 0.75em;
        font-weight: 600;
        margin-left: 8px;
    }}
    .badge-html {{ background: #e44d26; color: white; }}
    .badge-css {{ background: #264de4; color: white; }}
    .badge-js {{ background: #f7df1e; color: #333; }}
</style>
</head>
<body>
<div class="container">
    <h1>🥥 Chunky Form Demo</h1>
    <p class="subtitle">
        HTML+CSS+JS Webapp running inside Streamlit
        <span class="badge badge-html">HTML</span>
        <span class="badge badge-css">CSS</span>
        <span class="badge badge-js">JS</span>
    </p>

    <form id="chunkyForm">
        <div class="row">
            <div class="form-group">
                <label for="name">Full Name</label>
                <input type="text" id="name" name="name" placeholder="John Doe"
                       value="{name_val}">
            </div>
            <div class="form-group">
                <label for="email">Email Address</label>
                <input type="email" id="email" name="email" placeholder="john@example.com"
                       value="{email_val}">
            </div>
        </div>

        <div class="row">
            <div class="form-group">
                <label for="role">Role</label>
                <input type="text" id="role" name="role" placeholder="Data Engineer"
                       value="{role_val}">
            </div>
            <div class="form-group">
                <label for="department">Department</label>
                <select id="department" name="department">
                    <option value="">-- Select --</option>
                    <option value="engineering" {"selected" if dept_val == "engineering" else ""}>Engineering</option>
                    <option value="data-science" {"selected" if dept_val == "data-science" else ""}>Data Science</option>
                    <option value="analytics" {"selected" if dept_val == "analytics" else ""}>Analytics</option>
                    <option value="product" {"selected" if dept_val == "product" else ""}>Product</option>
                    <option value="other" {"selected" if dept_val == "other" else ""}>Other</option>
                </select>
            </div>
        </div>

        <div class="form-group">
            <label>Priority Level</label>
            <div class="priority-options">
                <div class="priority-option priority-low">
                    <input type="radio" id="prioLow" name="priority" value="low"
                           {"checked" if priority_val == "low" else ""}>
                    <label for="prioLow">🟢 Low</label>
                </div>
                <div class="priority-option priority-medium">
                    <input type="radio" id="prioMed" name="priority" value="medium"
                           {"checked" if priority_val == "medium" else ""}>
                    <label for="prioMed">🟡 Medium</label>
                </div>
                <div class="priority-option priority-high">
                    <input type="radio" id="prioHigh" name="priority" value="high"
                           {"checked" if priority_val == "high" else ""}>
                    <label for="prioHigh">🔴 High</label>
                </div>
            </div>
        </div>

        <div class="form-group">
            <label for="notes">Notes</label>
            <textarea id="notes" name="notes" placeholder="Any additional notes...">{notes_val}</textarea>
        </div>

        <div class="form-group">
            <div class="checkbox-group">
                <input type="checkbox" id="notify" name="notify" {notify_checked}>
                <label for="notify">Send email notifications</label>
            </div>
        </div>

        <div class="btn-row">
            <button type="submit" class="btn btn-primary">💾 Save to Streamlit</button>
            <button type="button" class="btn btn-secondary" id="clearBtn">🗑️ Clear</button>
        </div>
    </form>

    <div id="statusMsg" class="status"></div>
</div>

<script>
    const form = document.getElementById('chunkyForm');
    const statusMsg = document.getElementById('statusMsg');
    const clearBtn = document.getElementById('clearBtn');

    function sendDataToStreamlit(formData) {{
        // Streamlit.components.v1.html uses postMessage for iframe -> parent communication
        // The parent Streamlit frame listens on window.addEventListener('message', ...)
        const data = Object.fromEntries(formData.entries());
        // Handle checkbox (not included in formData.entries() if unchecked)
        data.notify = formData.has('notify');
        
        // Post message to parent (Streamlit)
        window.parent.postMessage({{
            type: 'streamlit:setComponentValue',
            value: data
        }}, '*');
        
        // Also try Streamlit's built-in mechanism
        // The Streamlit components API watches for messages with this format
        const streamlitData = JSON.stringify(data);
        window.parent.postMessage(streamlitData, '*');
    }}

    form.addEventListener('submit', function(e) {{
        e.preventDefault();
        const formData = new FormData(form);
        
        // Basic validation
        const name = formData.get('name');
        const email = formData.get('email');
        
        if (!name || !email) {{
            statusMsg.className = 'status error';
            statusMsg.textContent = '❌ Please fill in at least Name and Email.';
            return;
        }}
        
        if (!email.includes('@')) {{
            statusMsg.className = 'status error';
            statusMsg.textContent = '❌ Please enter a valid email address.';
            return;
        }}
        
        sendDataToStreamlit(formData);
        
        statusMsg.className = 'status success';
        statusMsg.textContent = '✅ Form data saved! Check the Streamlit panel below.';
        
        setTimeout(() => {{
            statusMsg.className = 'status';
        }}, 3000);
    }});

    clearBtn.addEventListener('click', function() {{
        form.reset();
        statusMsg.className = 'status';
    }});
</script>
</body>
</html>"""


def _build_display_panel(saved_values: dict) -> str:
    """
    Build a read-only HTML panel that displays the saved form values.
    This runs as a second iframe that updates when values change.
    """
    if not saved_values:
        return """<!DOCTYPE html>
<html><body style="font-family:sans-serif;text-align:center;padding:40px;color:#888;">
<p>📝 No form data saved yet. Fill out the form above and click "Save to Streamlit".</p>
</body></html>"""

    name = saved_values.get("name", "—")
    email = saved_values.get("email", "—")
    role = saved_values.get("role", "—")
    dept = saved_values.get("department", "—")
    priority = saved_values.get("priority", "—")
    notes = saved_values.get("notes", "—")
    notify = saved_values.get("notify", False)

    dept_labels = {
        "engineering": "Engineering",
        "data-science": "Data Science",
        "analytics": "Analytics",
        "product": "Product",
        "other": "Other"
    }
    dept_display = dept_labels.get(dept, dept or "—")

    prio_colors = {"low": "#28a745", "medium": "#ffc107", "high": "#dc3545"}
    prio_color = prio_colors.get(priority, "#666")
    prio_display = priority.capitalize() if priority else "—"

    return f"""<!DOCTYPE html>
<html><head>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f8f9fa; padding: 20px; }}
    .card {{ background: white; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.08); padding: 24px; max-width: 600px; margin: 0 auto; }}
    h2 {{ color: #333; font-size: 1.3em; margin-bottom: 20px; }}
    .field {{ margin-bottom: 14px; display: flex; align-items: baseline; }}
    .label {{ color: #888; font-size: 0.85em; width: 130px; flex-shrink: 0; }}
    .value {{ color: #333; font-weight: 500; }}
    .priority-badge {{
        display: inline-block; padding: 3px 12px; border-radius: 12px;
        font-size: 0.85em; font-weight: 600; color: white;
        background: {prio_color};
    }}
    .notify-badge {{
        display: inline-block; padding: 3px 12px; border-radius: 12px;
        font-size: 0.85em; font-weight: 600;
        background: {"#d4edda" if notify else "#f0f0f0"};
        color: {"#155724" if notify else "#888"};
    }}
</style>
</head><body>
<div class="card">
    <h2>📊 Saved Form Data (Streamlit-side)</h2>
    <div class="field"><span class="label">Name:</span><span class="value">{name}</span></div>
    <div class="field"><span class="label">Email:</span><span class="value">{email}</span></div>
    <div class="field"><span class="label">Role:</span><span class="value">{role}</span></div>
    <div class="field"><span class="label">Department:</span><span class="value">{dept_display}</span></div>
    <div class="field"><span class="label">Priority:</span><span class="priority-badge">{prio_display}</span></div>
    <div class="field"><span class="label">Notes:</span><span class="value">{notes}</span></div>
    <div class="field"><span class="label">Notifications:</span><span class="notify-badge">{"✅ Enabled" if notify else "❌ Disabled"}</span></div>
</div>
</body></html>"""


def _render_saved_json(saved_values: dict):
    """Render the raw saved data as a native Streamlit display."""
    if saved_values:
        st.markdown("#### 🔑 Saved Form Values (Streamlit Python)")
        col1, col2 = st.columns(2)
        with col1:
            st.text_input("Name", value=saved_values.get("name", ""), disabled=True, key="disp_name")
            st.text_input("Email", value=saved_values.get("email", ""), disabled=True, key="disp_email")
            st.text_input("Role", value=saved_values.get("role", ""), disabled=True, key="disp_role")
        with col2:
            dept = saved_values.get("department", "")
            st.text_input("Department", value=dept, disabled=True, key="disp_dept")
            st.text_input("Priority", value=saved_values.get("priority", ""), disabled=True, key="disp_prio")
            st.text_input("Notes", value=saved_values.get("notes", ""), disabled=True, key="disp_notes")
        st.checkbox("Email Notifications", value=saved_values.get("notify", False), disabled=True, key="disp_notify")

        with st.expander("📄 Raw JSON", expanded=False):
            st.json(saved_values)


def render_webapp_demo():
    """
    Main entry point for the HTML+CSS+JS Webapp Demo page.
    Renders an HTML form inside Streamlit and displays saved values.
    """
    st.title("🌐 HTML+CSS+JS Webapp Demo")
    log_action("NAVIGATE", "Visited Webapp Demo Page")

    st.markdown("""
    This page demonstrates that **HTML+CSS+JS webapps can run inside Streamlit**.
    The form below is pure HTML/CSS/JavaScript rendered in an iframe.
    When you submit the form, the data is captured and displayed in the Streamlit panel below.
    """)

    st.markdown("---")

    # Initialize saved values in session state
    if "webapp_form_data" not in st.session_state:
        st.session_state.webapp_form_data = {}

    saved = st.session_state.webapp_form_data

    # --- Section 1: HTML Form ---
    st.markdown("#### 📝 HTML Form (runs in iframe)")
    html_form = _build_html_form(saved)
    # Use height=700 to accommodate the full form without scrolling
    form_value = components.html(html_form, height=700, scrolling=True)

    # --- Section 2: Capture form data ---
    # components.html returns the value from window.parent.postMessage
    # We need to handle the Streamlit component protocol
    if form_value is not None and form_value != saved:
        # Streamlit components return the value set via postMessage
        if isinstance(form_value, str):
            try:
                parsed = json.loads(form_value)
                if isinstance(parsed, dict) and "name" in parsed:
                    st.session_state.webapp_form_data = parsed
                    st.rerun()
            except (json.JSONDecodeError, TypeError):
                pass
        elif isinstance(form_value, dict) and "name" in form_value:
            st.session_state.webapp_form_data = form_value
            st.rerun()

    st.markdown("---")

    # --- Section 3: Display Panel (HTML) ---
    st.markdown("#### 📊 Saved Values Display (HTML Panel)")
    display_html = _build_display_panel(saved)
    components.html(display_html, height=280, scrolling=False)

    # --- Section 4: Native Streamlit display ---
    st.markdown("---")
    _render_saved_json(saved)

    # --- Section 5: Manual input (fallback for component communication) ---
    st.markdown("---")
    st.markdown("#### 🔧 Manual Override")
    st.caption("If the HTML form data doesn't flow through automatically, use this fallback:")

    with st.form("manual_form"):
        mf1, mf2 = st.columns(2)
        m_name = mf1.text_input("Name", value=saved.get("name", ""), key="manual_name")
        m_email = mf2.text_input("Email", value=saved.get("email", ""), key="manual_email")
        mf3, mf4 = st.columns(2)
        m_role = mf3.text_input("Role", value=saved.get("role", ""), key="manual_role")
        m_dept = mf4.selectbox(
            "Department",
            ["", "engineering", "data-science", "analytics", "product", "other"],
            index=["", "engineering", "data-science", "analytics", "product", "other"].index(
                saved.get("department", "")
            ) if saved.get("department", "") in ["", "engineering", "data-science", "analytics", "product", "other"] else 0,
            key="manual_dept"
        )
        m_prio = st.radio(
            "Priority",
            ["low", "medium", "high"],
            index=["low", "medium", "high"].index(saved.get("priority", "medium")),
            horizontal=True,
            key="manual_prio"
        )
        m_notes = st.text_area("Notes", value=saved.get("notes", ""), key="manual_notes")
        m_notify = st.checkbox("Email Notifications", value=saved.get("notify", False), key="manual_notify")

        if st.form_submit_button("💾 Save Manual Entry", type="primary"):
            st.session_state.webapp_form_data = {
                "name": m_name,
                "email": m_email,
                "role": m_role,
                "department": m_dept,
                "priority": m_prio,
                "notes": m_notes,
                "notify": m_notify
            }
            log_action("WEBAPP_FORM_SAVE", {
                "source": "manual",
                "name": m_name,
                "email": m_email
            })
            st.success("✅ Form data saved!")
            st.rerun()

    # --- Clear button ---
    if saved and st.button("🗑️ Clear All Saved Data"):
        st.session_state.webapp_form_data = {}
        st.rerun()
