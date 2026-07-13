# views/webapp_demo.py
# Demo page showcasing HTML+CSS+JS webapp running within Streamlit
# Uses st.components.v2 (CCv2) — NOT the deprecated v1.html()
#
# CCv2 pattern:
#   JS → Python: setStateValue(key, value) for persistent state
#                setTriggerValue(key, value) for one-shot events
#   Python → JS: data={} dict passed on every mount

import streamlit as st
from logger_config import log_action

# -----------------------------------------------------------------------------
# CCv2 Component Definition (declare once at module level)
# -----------------------------------------------------------------------------

_FORM_HTML = """\
<div id="chunky-form-root"></div>
"""

_FORM_CSS = """\
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
#chunky-form-root {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  min-height: 400px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}
.container {
  background: white;
  border-radius: 16px;
  box-shadow: 0 20px 60px rgba(0,0,0,0.3);
  padding: 32px;
  width: 100%;
  max-width: 600px;
}
h1 { color: #333; font-size: 1.6em; margin-bottom: 4px; }
.subtitle { color: #666; font-size: 0.9em; margin-bottom: 24px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.7em; font-weight: 600; margin-left: 6px; }
.badge-html { background: #e44d26; color: white; }
.badge-css { background: #264de4; color: white; }
.badge-js { background: #f7df1e; color: #333; }
.form-group { margin-bottom: 16px; }
label { display: block; color: #444; font-weight: 600; margin-bottom: 4px; font-size: 0.85em; }
input[type="text"], input[type="email"], select, textarea {
  width: 100%; padding: 10px 14px; border: 2px solid #e0e0e0; border-radius: 8px;
  font-size: 0.95em; transition: border-color 0.3s; outline: none;
}
input:focus, select:focus, textarea:focus { border-color: #667eea; }
textarea { resize: vertical; min-height: 60px; }
.row { display: flex; gap: 12px; }
.row .form-group { flex: 1; }
.prio-opts { display: flex; gap: 8px; margin-top: 4px; }
.prio-opt { flex: 1; }
.prio-opt input[type="radio"] { display: none; }
.prio-opt label {
  display: block; text-align: center; padding: 8px; border: 2px solid #e0e0e0;
  border-radius: 8px; cursor: pointer; transition: all 0.3s; font-weight: 500; font-size: 0.9em;
}
.prio-opt input:checked + label { color: white; }
.prio-low label { color: #28a745; }
.prio-low input:checked + label { background: #28a745; border-color: #28a745; }
.prio-med label { color: #ffc107; }
.prio-med input:checked + label { background: #ffc107; border-color: #ffc107; color: #333; }
.prio-high label { color: #dc3545; }
.prio-high input:checked + label { background: #dc3545; border-color: #dc3545; }
.chk-group { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.chk-group input { width: 18px; height: 18px; accent-color: #667eea; }
.btn-row { display: flex; gap: 12px; margin-top: 24px; }
.btn {
  flex: 1; padding: 12px; border: none; border-radius: 8px; font-size: 0.95em;
  font-weight: 600; cursor: pointer; transition: transform 0.2s;
}
.btn:active { transform: scale(0.98); }
.btn-primary {
  background: linear-gradient(135deg, #667eea, #764ba2); color: white;
  box-shadow: 0 4px 15px rgba(102,126,234,0.4);
}
.btn-secondary { background: #f0f0f0; color: #555; }
.status { margin-top: 12px; padding: 10px; border-radius: 8px; display: none; font-weight: 500; font-size: 0.9em; }
.status.ok { background: #d4edda; color: #155724; display: block; }
"""

_FORM_JS = """\
export default function (component) {
  const { data, parentElement, setStateValue, setTriggerValue } = component
  const root = parentElement.querySelector("#chunky-form-root")
  if (!root) return

  // Hydrate from Python data
  const d = data || {}

  root.innerHTML = `
    <div class="container">
      <h1>🥥 Chunky Form</h1>
      <p class="subtitle">HTML+CSS+JS inside Streamlit
        <span class="badge badge-html">HTML</span>
        <span class="badge badge-css">CSS</span>
        <span class="badge badge-js">JS</span>
      </p>
      <div class="row">
        <div class="form-group">
          <label>Full Name</label>
          <input type="text" id="f-name" placeholder="John Doe" value="${_esc(d.name || '')}" />
        </div>
        <div class="form-group">
          <label>Email</label>
          <input type="email" id="f-email" placeholder="john@example.com" value="${_esc(d.email || '')}" />
        </div>
      </div>
      <div class="row">
        <div class="form-group">
          <label>Role</label>
          <input type="text" id="f-role" placeholder="Data Engineer" value="${_esc(d.role || '')}" />
        </div>
        <div class="form-group">
          <label>Department</label>
          <select id="f-dept">
            <option value="">-- Select --</option>
            ${_opt("engineering", "Engineering", d.department)}
            ${_opt("data-science", "Data Science", d.department)}
            ${_opt("analytics", "Analytics", d.department)}
            ${_opt("product", "Product", d.department)}
            ${_opt("other", "Other", d.department)}
          </select>
        </div>
      </div>
      <div class="form-group">
        <label>Priority</label>
        <div class="prio-opts">
          <div class="prio-opt prio-low">
            <input type="radio" name="prio" id="p-lo" value="low" ${d.priority === "low" ? "checked" : ""} />
            <label for="p-lo">🟢 Low</label>
          </div>
          <div class="prio-opt prio-med">
            <input type="radio" name="prio" id="p-md" value="medium" ${d.priority === "medium" || !d.priority ? "checked" : ""} />
            <label for="p-md">🟡 Medium</label>
          </div>
          <div class="prio-opt prio-high">
            <input type="radio" name="prio" id="p-hi" value="high" ${d.priority === "high" ? "checked" : ""} />
            <label for="p-hi">🔴 High</label>
          </div>
        </div>
      </div>
      <div class="form-group">
        <label>Notes</label>
        <textarea id="f-notes" placeholder="Any additional notes...">${_esc(d.notes || '')}</textarea>
      </div>
      <div class="form-group">
        <div class="chk-group">
          <input type="checkbox" id="f-notify" ${d.notify ? "checked" : ""} />
          <label for="f-notify">Send email notifications</label>
        </div>
      </div>
      <div class="btn-row">
        <button class="btn btn-primary" id="f-submit" type="button">💾 Save to Streamlit</button>
        <button class="btn btn-secondary" id="f-clear" type="button">🗑️ Clear</button>
      </div>
      <div id="f-status" class="status"></div>
    </div>
  `

  function _esc(s) { return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;") }
  function _opt(val, label, sel) { return `<option value="${val}" ${sel === val ? "selected" : ""}>${label}</option>` }

  // Success notification via localStorage — survives Streamlit reruns.
  // On submit, we write a timestamp to localStorage. On every render,
  // we check if the timestamp is within 3 seconds and show the notification.
  // No reruns triggered, no state sync needed. Pure client-side.
  const statusEl = root.querySelector("#f-status")
  const notifyUntil = parseInt(localStorage.getItem("chunky_notify_until") || "0", 10)
  if (statusEl && Date.now() < notifyUntil) {
    statusEl.className = "status ok"
    statusEl.textContent = "✅ Form saved! Check the Streamlit display below."
    setTimeout(() => {
      if (statusEl) statusEl.className = "status"
      localStorage.removeItem("chunky_notify_until")
    }, notifyUntil - Date.now())
  }

  // Sync state on blur/change so Submit always has current DOM values.
  // Without this, typing then immediately clicking Submit loses the edit
  // because innerHTML rebuilds from stale data on rerender.
  const textFields = root.querySelectorAll("input[type=text], input[type=email], textarea")
  textFields.forEach(el => {
    el.addEventListener("blur", () => _sync())
    el.addEventListener("keydown", e => { if (e.key === "Enter") _sync() })
  })
  root.querySelectorAll("input[name=prio], input[type=checkbox]").forEach(el => {
    el.addEventListener("change", () => _sync())
  })
  const deptEl = root.querySelector("#f-dept")
  if (deptEl) deptEl.addEventListener("change", () => _sync())

  // Also catch focusout on the entire form container (catches click-outside)
  root.querySelector(".container").addEventListener("focusout", () => _sync())

  function _sync() {
    setStateValue("saved", _collect())
  }

  // Submit button → sync + trigger + localStorage notification
  const btn = root.querySelector("#f-submit")
  if (btn) btn.onclick = () => {
    _sync()
    localStorage.setItem("chunky_notify_until", String(Date.now() + 3000))
    setTriggerValue("submitted", true)
  }

  // Clear button (client-side only, no rerun)
  const clr = root.querySelector("#f-clear")
  if (clr) clr.onclick = () => {
    root.querySelectorAll("input[type=text], input[type=email], textarea").forEach(e => e.value = "")
    root.querySelector("#f-dept").value = ""
    root.querySelector("#p-md").checked = true
    root.querySelector("#f-notify").checked = false
    const st = root.querySelector("#f-status")
    if (st) st.className = "status"
    _sync()
  }

  function _collect() {
    const prio = root.querySelector("input[name=prio]:checked")
    return {
      name: (root.querySelector("#f-name") || {}).value || "",
      email: (root.querySelector("#f-email") || {}).value || "",
      role: (root.querySelector("#f-role") || {}).value || "",
      department: (root.querySelector("#f-dept") || {}).value || "",
      priority: prio ? prio.value : "medium",
      notes: (root.querySelector("#f-notes") || {}).value || "",
      notify: (root.querySelector("#f-notify") || {}).checked || false
    }
  }
}
"""

# Register component ONCE at module level
_CHUNKY_FORM = st.components.v2.component(
    "chunky_form_demo",
    html=_FORM_HTML,
    css=_FORM_CSS,
    js=_FORM_JS,
)


# -----------------------------------------------------------------------------
# Display helper (native Streamlit — shows saved values)
# -----------------------------------------------------------------------------

def _render_saved_values(saved: dict):
    """Render the saved form data using native Streamlit widgets."""
    if not saved:
        st.info("📝 No form data saved yet. Fill out the form above and click **Save to Streamlit**.")
        return

    dept_labels = {
        "engineering": "Engineering", "data-science": "Data Science",
        "analytics": "Analytics", "product": "Product", "other": "Other"
    }

    name = saved.get("name", "—")
    email = saved.get("email", "—")
    role = saved.get("role", "—")
    dept = dept_labels.get(saved.get("department", ""), "—")
    prio = saved.get("priority", "—").capitalize()
    notes = saved.get("notes", "—")
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
    Main entry point for the HTML+CSS+JS Webapp Demo page.
    Uses CCv2 (st.components.v2) for proper bidirectional JS ↔ Python communication.
    """
    st.title("🌐 HTML+CSS+JS Webapp Demo")
    log_action("NAVIGATE", "Visited Webapp Demo Page")

    st.markdown("""
    This page demonstrates **HTML+CSS+JS webapps running inside Streamlit** using
    [Custom Components v2](https://docs.streamlit.io/develop/api-reference/custom-components/st.components.v2.component).
    The form below is pure HTML/CSS/JavaScript. When you interact with it, data flows
    back to Python via `setStateValue()` and `setTriggerValue()`.
    """)

    st.markdown("---")

    # Initialize persisted form data
    if "webapp_form_data" not in st.session_state:
        st.session_state.webapp_form_data = {}

    # --- Section 2: Read persisted state from CCv2 FIRST ---
    # setStateValue('saved', ...) persists across reruns (unlike trigger which resets)
    # Synced on blur/change events so form data is always current before any rerun
    component_state = st.session_state.get("webapp_form", {})
    saved = component_state.get("saved", {})
    if saved and isinstance(saved, dict) and saved.get("name"):
        if saved != st.session_state.webapp_form_data:
            st.session_state.webapp_form_data = saved
            log_action("WEBAPP_FORM_SAVE", {"source": "ccv2", "name": saved.get("name")})

    # --- Section 1: CCv2 Form ---
    st.markdown("#### 📝 HTML Form (CCv2 inline component)")

    result = _CHUNKY_FORM(
        data=st.session_state.webapp_form_data,
        key="webapp_form",
    )

    st.markdown("---")

    # --- Section 3: Display saved values ---
    st.markdown("#### 📊 Saved Form Values (Streamlit Python)")
    _render_saved_values(st.session_state.webapp_form_data)

    # --- Section 4: Manual override ---
    st.markdown("---")
    st.markdown("#### 🔧 Manual Override")
    st.caption("Fallback: save data directly via a native Streamlit form.")

    with st.form("manual_form"):
        saved = st.session_state.webapp_form_data
        mf1, mf2 = st.columns(2)
        m_name = mf1.text_input("Name", value=saved.get("name", ""), key="manual_name")
        m_email = mf2.text_input("Email", value=saved.get("email", ""), key="manual_email")
        mf3, mf4 = st.columns(2)
        m_role = mf3.text_input("Role", value=saved.get("role", ""), key="manual_role")
        dept_options = ["", "engineering", "data-science", "analytics", "product", "other"]
        m_dept = mf4.selectbox("Department", dept_options,
                               index=dept_options.index(saved.get("department", "")) if saved.get("department", "") in dept_options else 0,
                               key="manual_dept")
        m_prio = st.radio("Priority", ["low", "medium", "high"],
                          index=["low", "medium", "high"].index(saved.get("priority", "medium")),
                          horizontal=True, key="manual_prio")
        m_notes = st.text_area("Notes", value=saved.get("notes", ""), key="manual_notes")
        m_notify = st.checkbox("Email Notifications", value=saved.get("notify", False), key="manual_notify")

        if st.form_submit_button("💾 Save Manual Entry", type="primary"):
            st.session_state.webapp_form_data = {
                "name": m_name, "email": m_email, "role": m_role,
                "department": m_dept, "priority": m_prio,
                "notes": m_notes, "notify": m_notify
            }
            log_action("WEBAPP_FORM_SAVE", {"source": "manual", "name": m_name})
            st.success("✅ Saved!")
            st.rerun()

    if st.session_state.webapp_form_data and st.button("🗑️ Clear All Saved Data"):
        st.session_state.webapp_form_data = {}
        st.rerun()
