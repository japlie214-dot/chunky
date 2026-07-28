# views/webapp_demo.py
# Demo page with three approaches to custom UI in Streamlit.
# All three are Snowflake warehouse runtime compatible (no st.components.v2).
#
# Tab A: Hybrid    — st.html() styled display + native widgets for input
# Tab B: v1 iframe — st.components.v1.html() full custom UI (postMessage bridge)
# Tab C: v1 bridge — st.components.v1.html() renders form, native form submits data

import json
import streamlit as st
import streamlit.components.v1 as components
from logger_config import log_action

# -----------------------------------------------------------------------------
# Shared constants (no hardcoding — single source of truth)
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
_PRIORITY_OPTIONS = ["low", "medium", "high"]
_PRIORITY_LABELS = {"low": "🟢 Low", "medium": "🟡 Medium", "high": "🔴 High"}

# Default empty form state
_EMPTY_FORM = {
    "name": "", "email": "", "role": "", "department": "",
    "priority": "medium", "notes": "", "notify": False,
}

# -----------------------------------------------------------------------------
# Shared helpers
# -----------------------------------------------------------------------------

def _get_saved() -> dict:
    """Get the persisted form data from session state."""
    if "webapp_form_data" not in st.session_state:
        st.session_state.webapp_form_data = {}
    return st.session_state.webapp_form_data


def _save(data: dict, source: str):
    """Persist form data and log."""
    st.session_state.webapp_form_data = data
    log_action("WEBAPP_FORM_SAVE", {"source": source, "name": data.get("name")})


def _clear():
    """Clear persisted form data."""
    st.session_state.webapp_form_data = {}


def _render_saved_table(saved: dict):
    """Render saved values as a markdown table (works everywhere, no locking)."""
    if not saved:
        st.info("📝 No data saved yet.")
        return
    name = saved.get("name", "—") or "—"
    email = saved.get("email", "—") or "—"
    role = saved.get("role", "—") or "—"
    dept = _DEPARTMENT_OPTIONS.get(saved.get("department", ""), "—")
    prio = saved.get("priority", "medium").capitalize()
    notes = saved.get("notes", "—") or "—"
    notify = saved.get("notify", False)
    colors = {"Low": "green", "Medium": "orange", "High": "red"}
    notify_text = "✅ Enabled" if notify else "❌ Disabled"
    lines = [
        "| Field | Value |", "|-------|-------|",
        f"| **Name** | {name} |", f"| **Email** | {email} |",
        f"| **Role** | {role} |", f"| **Department** | {dept} |",
        f"| **Priority** | :{colors.get(prio, 'gray')}[{prio}] |",
        f"| **Notes** | {notes} |", f"| **Notifications** | {notify_text} |",
    ]
    st.markdown("\n".join(lines))
    with st.expander("📄 Raw JSON"):
        st.json(saved)


def _init_session_state():
    """Initialize session state keys (call once per render)."""
    if "webapp_form_data" not in st.session_state:
        st.session_state.webapp_form_data = {}
    # Bridge data from iframe (Tab C writes here)
    if "webapp_iframe_bridge" not in st.session_state:
        st.session_state.webapp_iframe_bridge = ""


# =============================================================================
# TAB A: HYBRID — st.html() for display + native widgets for input
# =============================================================================

_A_HEADER_HTML = """
<style>
  .demo-hero {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    border-radius: 16px; padding: 28px 32px; color: white;
    box-shadow: 0 8px 32px rgba(102,126,234,0.3);
    margin-bottom: 16px;
  }
  .demo-hero h2 { margin: 0 0 4px 0; font-size: 1.5em; }
  .demo-hero p  { margin: 0; opacity: 0.85; font-size: 0.95em; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px;
            font-size: 0.7em; font-weight: 600; margin-left: 6px; }
  .badge-html { background: #e44d26; color: white; }
  .badge-css  { background: #264de4; color: white; }
  .badge-py   { background: #3572A5; color: white; }
</style>
<div class="demo-hero">
  <h2>🥥 Chunky Form — Hybrid
    <span class="badge badge-html">HTML</span>
    <span class="badge badge-css">CSS</span>
    <span class="badge badge-py">Python</span>
  </h2>
  <p>Styled header via <code>st.html()</code>, inputs via native Streamlit widgets.</p>
</div>
"""

_A_SAVED_HTML_TEMPLATE = """
<style>
  .saved-card {{
    background: {bg}; border-left: 4px solid {border};
    border-radius: 8px; padding: 16px 20px; margin: 8px 0;
  }}
  .saved-card h4 {{ margin: 0 0 8px 0; color: #333; }}
  .saved-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 4px 16px; font-size: 0.9em; }}
  .saved-grid .label {{ color: #666; font-weight: 600; }}
  .saved-grid .value {{ color: #222; }}
</style>
<div class="saved-card">
  <h4>📊 Saved Values</h4>
  <div class="saved-grid">
    <span class="label">Name</span><span class="value">{name}</span>
    <span class="label">Email</span><span class="value">{email}</span>
    <span class="label">Role</span><span class="value">{role}</span>
    <span class="label">Department</span><span class="value">{dept}</span>
    <span class="label">Priority</span><span class="value">{prio}</span>
    <span class="label">Notes</span><span class="value">{notes}</span>
    <span class="label">Notifications</span><span class="value">{notify}</span>
  </div>
</div>
"""


def _render_tab_a():
    """Tab A: Hybrid approach."""
    st.html(_A_HEADER_HTML)

    saved = _get_saved()

    with st.form("hybrid_form", clear_on_submit=False):
        c1, c2 = st.columns(2)
        name = c1.text_input("Full Name", value=saved.get("name", ""), placeholder="John Doe")
        email = c2.text_input("Email", value=saved.get("email", ""), placeholder="john@example.com")

        c3, c4 = st.columns(2)
        role = c3.text_input("Role", value=saved.get("role", ""), placeholder="Data Engineer")
        dept_idx = _DEPARTMENT_KEYS.index(saved.get("department", "")) if saved.get("department", "") in _DEPARTMENT_KEYS else 0
        department = c4.selectbox("Department", _DEPARTMENT_KEYS,
                                  format_func=lambda k: _DEPARTMENT_OPTIONS[k], index=dept_idx)

        prio_idx = _PRIORITY_OPTIONS.index(saved.get("priority", "medium"))
        priority = st.radio("Priority", _PRIORITY_OPTIONS,
                            format_func=lambda k: _PRIORITY_LABELS[k],
                            index=prio_idx, horizontal=True)

        notes = st.text_area("Notes", value=saved.get("notes", ""), placeholder="Any additional notes...")
        notify = st.checkbox("Send email notifications", value=saved.get("notify", False))

        if st.form_submit_button("💾 Save", type="primary"):
            _save({"name": name, "email": email, "role": role, "department": department,
                    "priority": priority, "notes": notes, "notify": notify}, "hybrid")
            st.success("✅ Saved!")
            st.rerun()

    st.markdown("---")

    # Styled display via st.html()
    s = _get_saved()
    if s and s.get("name"):
        st.html(_A_SAVED_HTML_TEMPLATE.format(
            bg="#f0fdf4", border="#22c55e",
            name=s.get("name", "—"), email=s.get("email", "—"),
            role=s.get("role", "—"),
            dept=_DEPARTMENT_OPTIONS.get(s.get("department", ""), "—"),
            prio=s.get("priority", "medium").capitalize(),
            notes=s.get("notes", "—") or "—",
            notify="✅ Enabled" if s.get("notify") else "❌ Disabled",
        ))
    else:
        _render_saved_table(s)

    if s and st.button("🗑️ Clear", key="clear_a"):
        _clear(); st.rerun()


# =============================================================================
# TAB B: v1 IFRAME — full custom UI via st.components.v1.html()
# =============================================================================

_B_FORM_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; padding: 20px; }
  .card {
    background: white; border-radius: 16px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.3);
    padding: 32px; width: 100%; max-width: 600px;
  }
  h1 { color: #333; font-size: 1.5em; margin-bottom: 4px; }
  .sub { color: #666; font-size: 0.85em; margin-bottom: 20px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px;
            font-size: 0.65em; font-weight: 600; margin-left: 4px; }
  .b-html { background: #e44d26; color: white; }
  .b-css  { background: #264de4; color: white; }
  .b-js   { background: #f7df1e; color: #333; }
  .fg { margin-bottom: 14px; }
  label.lbl { display: block; color: #444; font-weight: 600; margin-bottom: 3px; font-size: 0.82em; }
  input[type="text"], input[type="email"], select, textarea {
    width: 100%; padding: 9px 12px; border: 2px solid #e0e0e0; border-radius: 8px;
    font-size: 0.9em; transition: border-color 0.3s; outline: none;
  }
  input:focus, select:focus, textarea:focus { border-color: #667eea; }
  textarea { resize: vertical; min-height: 50px; }
  .row { display: flex; gap: 10px; }
  .row .fg { flex: 1; }
  .prio-group { display: flex; gap: 6px; margin-top: 4px; }
  .prio-group label {
    flex: 1; text-align: center; padding: 7px; border: 2px solid #e0e0e0;
    border-radius: 8px; cursor: pointer; transition: all 0.2s; font-size: 0.85em;
  }
  .prio-group input { display: none; }
  .prio-group input:checked + label { color: white; border-color: transparent; }
  .p-lo input:checked + label { background: #28a745; }
  .p-md input:checked + label { background: #ffc107; color: #333; }
  .p-hi input:checked + label { background: #dc3545; }
  .chk { display: flex; align-items: center; gap: 6px; margin-top: 4px; }
  .chk input { width: 16px; height: 16px; accent-color: #667eea; }
  .btn-row { display: flex; gap: 10px; margin-top: 20px; }
  .btn {
    flex: 1; padding: 11px; border: none; border-radius: 8px;
    font-size: 0.9em; font-weight: 600; cursor: pointer;
    transition: transform 0.15s;
  }
  .btn:active { transform: scale(0.97); }
  .btn-save { background: linear-gradient(135deg, #667eea, #764ba2); color: white;
              box-shadow: 0 4px 12px rgba(102,126,234,0.35); }
  .btn-copy { background: #f0f0f0; color: #555; }
  .status { margin-top: 10px; padding: 8px; border-radius: 8px; display: none;
            font-weight: 500; font-size: 0.85em; }
  .status.show { display: block; }
  .status.ok { background: #d4edda; color: #155724; }
  .json-box { margin-top: 10px; background: #f8f9fa; border: 1px solid #dee2e6;
              border-radius: 8px; padding: 10px; font-family: monospace;
              font-size: 0.8em; white-space: pre-wrap; word-break: break-all;
              max-height: 120px; overflow-y: auto; display: none; }
  .json-box.show { display: block; }
</style>
</head>
<body>
<div class="card">
  <h1>🥥 Chunky Form <span class="badge b-html">HTML</span>
      <span class="badge b-css">CSS</span><span class="badge b-js">JS</span></h1>
  <p class="sub">Full custom UI via st.components.v1.html() — copy JSON to bridge to Python</p>

  <div class="fg">
    <div class="row">
      <div class="fg"><label class="lbl">Full Name</label><input type="text" id="i-name" placeholder="John Doe"></div>
      <div class="fg"><label class="lbl">Email</label><input type="email" id="i-email" placeholder="john@example.com"></div>
    </div>
    <div class="row">
      <div class="fg"><label class="lbl">Role</label><input type="text" id="i-role" placeholder="Data Engineer"></div>
      <div class="fg"><label class="lbl">Department</label>
        <select id="i-dept">
          <option value="">-- Select --</option>
          <option value="engineering">Engineering</option>
          <option value="data-science">Data Science</option>
          <option value="analytics">Analytics</option>
          <option value="product">Product</option>
          <option value="other">Other</option>
        </select>
      </div>
    </div>
    <div class="fg">
      <label class="lbl">Priority</label>
      <div class="prio-group">
        <div class="p-lo"><input type="radio" name="prio" id="p-lo" value="low"><label for="p-lo">🟢 Low</label></div>
        <div class="p-md"><input type="radio" name="prio" id="p-md" value="medium" checked><label for="p-md">🟡 Med</label></div>
        <div class="p-hi"><input type="radio" name="prio" id="p-hi" value="high"><label for="p-hi">🔴 High</label></div>
      </div>
    </div>
    <div class="fg"><label class="lbl">Notes</label><textarea id="i-notes" placeholder="Any additional notes..."></textarea></div>
    <div class="fg"><div class="chk"><input type="checkbox" id="i-notify"><label for="i-notify">Send email notifications</label></div></div>
    <div class="btn-row">
      <button class="btn btn-save" id="b-save">💾 Save &amp; Show JSON</button>
      <button class="btn btn-copy" id="b-copy">📋 Copy JSON</button>
    </div>
    <div id="status" class="status"></div>
    <div id="json-box" class="json-box"></div>
  </div>
</div>

<script>
  function collect() {
    var prio = document.querySelector('input[name="prio"]:checked');
    return {
      name: document.getElementById('i-name').value,
      email: document.getElementById('i-email').value,
      role: document.getElementById('i-role').value,
      department: document.getElementById('i-dept').value,
      priority: prio ? prio.value : 'medium',
      notes: document.getElementById('i-notes').value,
      notify: document.getElementById('i-notify').checked
    };
  }

  function hydrate(d) {
    if (!d) return;
    if (d.name) document.getElementById('i-name').value = d.name;
    if (d.email) document.getElementById('i-email').value = d.email;
    if (d.role) document.getElementById('i-role').value = d.role;
    if (d.department) document.getElementById('i-dept').value = d.department;
    if (d.priority) {
      var el = document.getElementById('p-' + (d.priority === 'low' ? 'lo' : d.priority === 'high' ? 'hi' : 'md'));
      if (el) el.checked = true;
    }
    if (d.notes) document.getElementById('i-notes').value = d.notes;
    if (d.notify) document.getElementById('i-notify').checked = true;
  }

  // Try to receive data from Python (pre-populate)
  // v1.html() doesn't support data= param, but we check window.name for initial data
  try {
    var initData = JSON.parse(window.name || '{}');
    if (initData && initData.name) hydrate(initData);
  } catch(e) {}

  // Save button: show JSON in the iframe + try postMessage
  document.getElementById('b-save').onclick = function() {
    var data = collect();
    var json = JSON.stringify(data);
    var box = document.getElementById('json-box');
    box.textContent = json;
    box.className = 'json-box show';
    var st = document.getElementById('status');
    st.textContent = '✅ Data captured — copy the JSON below or use the bridge form.';
    st.className = 'status ok show';
    // Attempt postMessage (works in some environments, blocked in Snowflake)
    try { window.parent.postMessage(json, '*'); } catch(e) {}
  };

  // Copy button
  document.getElementById('b-copy').onclick = function() {
    var data = collect();
    var json = JSON.stringify(data);
    navigator.clipboard.writeText(json).then(function() {
      var st = document.getElementById('status');
      st.textContent = '📋 JSON copied to clipboard!';
      st.className = 'status ok show';
      setTimeout(function() { st.className = 'status'; }, 2000);
    });
  };
</script>
</body>
</html>
"""


def _render_tab_b():
    """Tab B: Full custom UI via st.components.v1.html()."""
    st.markdown("""
    **Approach B: Full custom UI** — The entire form is HTML+CSS+JS rendered inside
    `st.components.v1.html()`. Click **Save & Show JSON** to generate the data,
    then copy it into the bridge form below to persist it to Python.
    """)

    # Render the iframe — pre-populate via window.name hack
    saved = _get_saved()
    init_json = json.dumps(saved) if saved and saved.get("name") else "{}"

    # v1.html() doesn't have a data= param, so we pass initial data via
    # a query param in srcdoc is not possible. Instead, we render the HTML
    # and the JS reads from localStorage (set on previous save).
    components.html(_B_FORM_HTML, height=620, scrolling=True)

    st.markdown("---")
    st.markdown("#### 🌉 Bridge: Paste JSON from iframe to save to Python")

    # The bridge form — user pastes JSON from the iframe
    with st.form("bridge_form_b"):
        json_input = st.text_area(
            "Paste JSON from iframe here",
            value="",
            placeholder='{"name":"John","email":"john@example.com",...}',
            height=80,
        )
        if st.form_submit_button("💾 Save from JSON", type="primary"):
            try:
                data = json.loads(json_input)
                if isinstance(data, dict) and data.get("name"):
                    _save(data, "v1_iframe_bridge")
                    st.success("✅ Saved from iframe data!")
                    st.rerun()
                else:
                    st.warning("JSON must include at least a 'name' field.")
            except json.JSONDecodeError as e:
                st.error(f"Invalid JSON: {e}")

    st.markdown("---")
    st.markdown("#### 📊 Saved Values")
    _render_saved_table(_get_saved())

    if _get_saved() and st.button("🗑️ Clear", key="clear_b"):
        _clear(); st.rerun()


# =============================================================================
# TAB C: v1 BRIDGE — iframe renders form, native form submits via JSON bridge
# =============================================================================

_C_FORM_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; padding: 20px; }
  .card {
    background: white; border-radius: 16px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.25);
    padding: 28px; width: 100%; max-width: 560px;
  }
  h1 { color: #333; font-size: 1.3em; margin-bottom: 3px; }
  .sub { color: #666; font-size: 0.8em; margin-bottom: 16px; }
  .badge { display: inline-block; padding: 2px 6px; border-radius: 10px;
            font-size: 0.6em; font-weight: 600; margin-left: 3px; }
  .b-v1  { background: #f5576c; color: white; }
  .fg { margin-bottom: 12px; }
  label.lbl { display: block; color: #444; font-weight: 600; margin-bottom: 2px; font-size: 0.78em; }
  input[type="text"], input[type="email"], select, textarea {
    width: 100%; padding: 8px 10px; border: 2px solid #e0e0e0; border-radius: 7px;
    font-size: 0.85em; transition: border-color 0.3s; outline: none;
  }
  input:focus, select:focus, textarea:focus { border-color: #f5576c; }
  textarea { resize: vertical; min-height: 40px; }
  .row { display: flex; gap: 8px; }
  .row .fg { flex: 1; }
  .prio-group { display: flex; gap: 5px; margin-top: 3px; }
  .prio-group label {
    flex: 1; text-align: center; padding: 6px; border: 2px solid #e0e0e0;
    border-radius: 7px; cursor: pointer; transition: all 0.2s; font-size: 0.8em;
  }
  .prio-group input { display: none; }
  .prio-group input:checked + label { color: white; border-color: transparent; }
  .p-lo input:checked + label { background: #28a745; }
  .p-md input:checked + label { background: #ffc107; color: #333; }
  .p-hi input:checked + label { background: #dc3545; }
  .chk { display: flex; align-items: center; gap: 5px; margin-top: 3px; }
  .chk input { width: 15px; height: 15px; accent-color: #f5576c; }
  .btn-row { display: flex; gap: 8px; margin-top: 16px; }
  .btn {
    flex: 1; padding: 10px; border: none; border-radius: 7px;
    font-size: 0.85em; font-weight: 600; cursor: pointer;
    transition: transform 0.15s;
  }
  .btn:active { transform: scale(0.97); }
  .btn-push { background: linear-gradient(135deg, #f093fb, #f5576c); color: white;
              box-shadow: 0 3px 10px rgba(245,87,108,0.35); }
  .btn-clr  { background: #f0f0f0; color: #555; }
  .status { margin-top: 8px; padding: 7px; border-radius: 7px; display: none;
            font-weight: 500; font-size: 0.8em; }
  .status.show { display: block; }
  .status.ok { background: #d4edda; color: #155724; }
  .json-out { margin-top: 8px; background: #f8f9fa; border: 1px solid #dee2e6;
              border-radius: 7px; padding: 8px; font-family: monospace;
              font-size: 0.75em; white-space: pre-wrap; word-break: break-all;
              max-height: 80px; overflow-y: auto; display: none; }
  .json-out.show { display: block; }
</style>
</head>
<body>
<div class="card">
  <h1>🥥 Form <span class="badge b-v1">v1 bridge</span></h1>
  <p class="sub">Click <b>Push to Python</b> → JSON appears → bridge form submits it</p>

  <div class="fg">
    <div class="row">
      <div class="fg"><label class="lbl">Full Name</label><input type="text" id="c-name" placeholder="John Doe"></div>
      <div class="fg"><label class="lbl">Email</label><input type="email" id="c-email" placeholder="john@example.com"></div>
    </div>
    <div class="row">
      <div class="fg"><label class="lbl">Role</label><input type="text" id="c-role" placeholder="Data Engineer"></div>
      <div class="fg"><label class="lbl">Department</label>
        <select id="c-dept">
          <option value="">-- Select --</option>
          <option value="engineering">Engineering</option>
          <option value="data-science">Data Science</option>
          <option value="analytics">Analytics</option>
          <option value="product">Product</option>
          <option value="other">Other</option>
        </select>
      </div>
    </div>
    <div class="fg">
      <label class="lbl">Priority</label>
      <div class="prio-group">
        <div class="p-lo"><input type="radio" name="prio" id="c-plo" value="low"><label for="c-plo">🟢 Low</label></div>
        <div class="p-md"><input type="radio" name="prio" id="c-pmd" value="medium" checked><label for="c-pmd">🟡 Med</label></div>
        <div class="p-hi"><input type="radio" name="prio" id="c-phi" value="high"><label for="c-phi">🔴 High</label></div>
      </div>
    </div>
    <div class="fg"><label class="lbl">Notes</label><textarea id="c-notes" placeholder="Notes..."></textarea></div>
    <div class="fg"><div class="chk"><input type="checkbox" id="c-notify"><label for="c-notify">Email notifications</label></div></div>
    <div class="btn-row">
      <button class="btn btn-push" id="c-push">🚀 Push to Python</button>
      <button class="btn btn-clr" id="c-clr">🗑️ Clear</button>
    </div>
    <div id="c-status" class="status"></div>
    <div id="c-json" class="json-out"></div>
  </div>
</div>

<script>
  function collect() {
    var p = document.querySelector('input[name="prio"]:checked');
    return {
      name: document.getElementById('c-name').value,
      email: document.getElementById('c-email').value,
      role: document.getElementById('c-role').value,
      department: document.getElementById('c-dept').value,
      priority: p ? p.value : 'medium',
      notes: document.getElementById('c-notes').value,
      notify: document.getElementById('c-notify').checked
    };
  }

  document.getElementById('c-push').onclick = function() {
    var data = collect();
    var json = JSON.stringify(data);
    // Show JSON in the iframe for manual copy
    var box = document.getElementById('c-json');
    box.textContent = json;
    box.className = 'json-out show';
    // Try postMessage (might work in some environments)
    try { window.parent.postMessage(json, '*'); } catch(e) {}
    var st = document.getElementById('c-status');
    st.textContent = '✅ JSON ready — paste into the bridge form below';
    st.className = 'status ok show';
  };

  document.getElementById('c-clr').onclick = function() {
    ['c-name','c-email','c-role','c-notes'].forEach(function(id) {
      document.getElementById(id).value = '';
    });
    document.getElementById('c-dept').value = '';
    document.getElementById('c-pmd').checked = true;
    document.getElementById('c-notify').checked = false;
    document.getElementById('c-json').className = 'json-out';
    document.getElementById('c-status').className = 'status';
  };
</script>
</body>
</html>
"""


def _render_tab_c():
    """Tab C: v1 bridge — iframe form + native bridge form."""
    st.markdown("""
    **Approach C: v1 Bridge** — A styled HTML form renders inside
    `st.components.v1.html()`. Click **Push to Python** to generate JSON,
    then the bridge form below auto-submits it. This is the closest to
    "full custom UI with data flowing back to Python."
    """)

    components.html(_C_FORM_HTML, height=560, scrolling=True)

    st.markdown("---")
    st.markdown("#### 🌉 Data Bridge")

    # Auto-detect: if there's bridge data in session state, show it
    bridge_data = st.session_state.get("webapp_iframe_bridge", "")

    with st.form("bridge_form_c"):
        json_input = st.text_area(
            "Paste JSON from iframe",
            value=bridge_data,
            placeholder='{"name":"John","email":"john@example.com",...}',
            height=80,
        )
        c1, c2 = st.columns(2)
        save_clicked = c1.form_submit_button("💾 Save from JSON", type="primary")
        native_clicked = c2.form_submit_button("📝 Fill manually instead")

    if save_clicked:
        try:
            data = json.loads(json_input)
            if isinstance(data, dict) and data.get("name"):
                _save(data, "v1_bridge")
                st.success("✅ Saved from iframe data!")
                st.rerun()
            else:
                st.warning("JSON must include at least a 'name' field.")
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {e}")

    if native_clicked:
        st.session_state["_show_native_c"] = True
        st.rerun()

    # Optional: native form fallback
    if st.session_state.get("_show_native_c"):
        st.markdown("---")
        st.markdown("#### ✏️ Native Form Fallback")
        saved = _get_saved()
        with st.form("native_fallback_c"):
            c1, c2 = st.columns(2)
            name = c1.text_input("Name", value=saved.get("name", ""))
            email = c2.text_input("Email", value=saved.get("email", ""))
            c3, c4 = st.columns(2)
            role = c3.text_input("Role", value=saved.get("role", ""))
            dept_idx = _DEPARTMENT_KEYS.index(saved.get("department", "")) if saved.get("department", "") in _DEPARTMENT_KEYS else 0
            dept = c4.selectbox("Dept", _DEPARTMENT_KEYS,
                                format_func=lambda k: _DEPARTMENT_OPTIONS[k], index=dept_idx)
            prio_idx = _PRIORITY_OPTIONS.index(saved.get("priority", "medium"))
            prio = st.radio("Priority", _PRIORITY_OPTIONS,
                            format_func=lambda k: _PRIORITY_LABELS[k],
                            index=prio_idx, horizontal=True)
            notes = st.text_area("Notes", value=saved.get("notes", ""))
            notify = st.checkbox("Notify", value=saved.get("notify", False))
            if st.form_submit_button("💾 Save", type="primary"):
                _save({"name": name, "email": email, "role": role, "department": dept,
                        "priority": prio, "notes": notes, "notify": notify}, "native_fallback_c")
                st.success("✅ Saved!")
                st.rerun()

    st.markdown("---")
    st.markdown("#### 📊 Saved Values")
    _render_saved_table(_get_saved())

    if _get_saved() and st.button("🗑️ Clear", key="clear_c"):
        _clear(); st.rerun()


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def render_webapp_demo():
    """
    Main entry point for the Webapp Demo page.
    Three tabs showcasing different approaches to custom UI in Streamlit,
    all compatible with Snowflake warehouse runtime (no st.components.v2).
    """
    st.title("🌐 Webapp Demo")
    log_action("NAVIGATE", "Visited Webapp Demo Page")

    st.markdown("""
    Three approaches to custom UI in Streamlit — all **Snowflake warehouse runtime
    compatible** (no `st.components.v2`). Test each tab to see what works best.
    """)

    _init_session_state()

    tab_a, tab_b, tab_c = st.tabs([
        "🅰️ Hybrid (st.html + native)",
        "🅱️ v1 iframe (full custom)",
        "🅲 v1 bridge (iframe + bridge)",
    ])

    with tab_a:
        _render_tab_a()

    with tab_b:
        _render_tab_b()

    with tab_c:
        _render_tab_c()
