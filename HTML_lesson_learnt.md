# HTML + Streamlit: Lessons Learnt

Hard-won knowledge from building HTML+CSS+JS components inside Streamlit. Every item here caused at least one real bug.

---

## 1. CCv2 Only — Never Use v1

`st.components.v1.html()` is **deprecated**. The entire v1 communication pattern is broken.

| Banned (v1) | Use Instead (v2) |
|-------------|-------------------|
| `st.components.v1.html()` | `st.components.v2.component()` |
| `window.parent.postMessage()` | `setStateValue()` / `setTriggerValue()` |
| `Streamlit.setComponentValue()` | Does not exist in v2 |
| `Streamlit.setFrameHeight()` | CCv2 handles sizing automatically |
| `streamlit-component-lib` (npm) | `@streamlit/component-v2-lib` if needed |

**Reference:** Streamlit skill `references/custom-components-v2.md`

---

## 2. CCv2 Communication Model

```
JS → Python (persistent):  setStateValue("key", value)   — survives reruns
JS → Python (one-shot):    setTriggerValue("key", value) — resets after rerun
Python → JS:               data={} dict passed on every mount
```

- `setStateValue` stores in `st.session_state[component_key][state_key]`
- `setTriggerValue` fires once, then resets
- Both **trigger a Streamlit rerun** — calling both = two reruns

### The Double-Rerun Trap

```js
// ❌ BAD: two reruns — notification flashes and disappears
btn.onclick = () => {
    setStateValue("saved", data)     // rerun #1
    setTriggerValue("submitted", true) // rerun #2
}

// ✅ GOOD: one rerun
btn.onclick = () => {
    setTriggerValue("submitted", true) // single rerun
}
```

If you need both state persistence AND an event, use `setStateValue` for data sync on `blur`/`change` events (separate from submit), and `setTriggerValue` only on submit.

---

## 3. Shadow DOM Scope

CCv2 components run inside a **shadow DOM**. This means:

```js
// ❌ BROKEN: can't find elements inside shadow DOM
document.querySelector("#my-element")

// ✅ CORRECT: scope to the component's parent element
parentElement.querySelector("#my-element")
```

Always use `parentElement.querySelector` inside CCv2 JS. `document.querySelector` silently returns `null`.

---

## 4. DOM Rebuilds on Every Rerun

Streamlit **destroys and rebuilds the DOM** on every rerun. This means:

- `setTimeout` references to DOM elements become **stale** after rerun
- CSS animations **restart** from 0% on rerun
- `setInterval` callbacks reference **detached** DOM elements
- Any closure capturing a DOM element is **dead** after rerun

### Notification Survival Pattern

Use **localStorage** for data that must survive reruns:

```js
// On submit: write deadline to localStorage
localStorage.setItem("notify_until", String(Date.now() + 3000))

// On every render: check localStorage and show if within deadline
const until = parseInt(localStorage.getItem("notify_until") || "0", 10)
if (Date.now() < until) {
    const el = parentElement.querySelector("#status")
    if (el) {
        el.className = "status ok"
        el.textContent = "✅ Saved!"
        setTimeout(() => {
            const fresh = parentElement.querySelector("#status")
            if (fresh) { fresh.className = "status" }
            localStorage.removeItem("notify_until")
        }, until - Date.now())
    }
}
```

Key points:
- `localStorage` survives Streamlit reruns (browser-level storage)
- `parentElement.querySelector` finds the **current** DOM element (not stale)
- `setTimeout` fires on the **current** element reference
- If another rerun happens before timeout, the new render re-checks localStorage

---

## 5. Blur Sync Pattern for Forms

CCv2 forms must sync data before any rerun. The pattern:

```js
// Sync on blur/change — not on every keystroke (avoids rerun-per-keypress)
input.addEventListener("blur", () => setStateValue("formData", collect()))
select.addEventListener("change", () => setStateValue("formData", collect()))

// Also catch focusout on the container (click-outside-then-submit)
container.addEventListener("focusout", () => setStateValue("formData", collect()))
```

### Why Not Sync on Keystroke?

```js
// ❌ BAD: reruns Streamlit on every keystroke
input.oninput = () => setStateValue("value", input.value)

// ✅ GOOD: sync only when user leaves the field
input.addEventListener("blur", () => setStateValue("value", input.value))
```

### The "Click Submit Without Blurring" Edge Case

If the user types in a field and clicks Submit without blurring first:
1. The `blur` event fires when focus moves to the button → `_sync()` runs
2. The `click` event fires on the button → `setTriggerValue()` runs
3. Both happen in the same event loop → one rerun with current data

If `blur` doesn't fire (rare edge case), add a `focusout` listener on the form container as fallback.

---

## 6. Displaying Saved Data

### ❌ Don't Use Disabled Widgets

```python
# BROKEN: widget key locks to session_state, ignores external value= changes
st.text_input("Name", value=saved["name"], disabled=True, key="disp_name")
```

Once a widget key exists in `session_state`, the displayed value is locked to `session_state[key]`, not to the `value=` parameter. External data changes don't propagate.

### ✅ Use Markdown Tables

```python
st.markdown(f"| Field | Value |\n|-------|-------|\n| **Name** | {name} |")
```

Markdown re-renders from the current data on every rerun. No locking, no stale state.

---

## 7. CCv2 Component Registration

Register components **once at module level**, not inside functions:

```python
# ✅ CORRECT: register once at import time
_MY_COMPONENT = st.components.v2.component("my_comp", html=HTML, js=JS)

def my_wrapper(key, data):
    return _MY_COMPONENT(data=data, key=key)
```

```python
# ❌ WRONG: re-registers on every function call
def my_component(data):
    comp = st.components.v2.component("my_comp", html=HTML, js=JS)
    return comp(data=data)
```

---

## 8. JS Entry Point

CCv2 JS must export a default function:

```js
export default function (component) {
    const { data, parentElement, setStateValue, setTriggerValue } = component
    // your code here
}
```

- `data` — dict from Python's `data=` parameter
- `parentElement` — the DOM element to render under (use for all queries)
- `setStateValue(key, value)` — persistent state (survives reruns)
- `setTriggerValue(key, value)` — one-shot event (resets after rerun)

---

## 9. Python-Side State Reading

```python
# Read component state (set by setStateValue)
component_state = st.session_state.get("my_component_key", {})
saved_data = component_state.get("stateKey", {})

# Read trigger (one-shot, resets after rerun)
trigger_value = component_state.get("triggerKey")
```

**Timing:** Component state is available in `st.session_state` during the script execution (before the component mounts). You can read it, process it, and pass updated data back via `data=`.

---

## 10. Common Pitfalls Summary

| Pitfall | Symptom | Fix |
|---------|---------|-----|
| Using v1 `components.html()` | Form renders but data never reaches Python | Use `st.components.v2.component()` |
| `document.querySelector` in shadow DOM | Elements not found, silent null | Use `parentElement.querySelector` |
| Calling `setStateValue` + `setTriggerValue` on same event | Two reruns, notification flashes | Use only one per event handler |
| `st.text_input(disabled=True)` with fixed key | Display doesn't update | Use `st.markdown` table |
| Syncing on every keystroke | Rerun on each keypress, awful UX | Sync on `blur`/`change` only |
| `setTimeout` with DOM reference | Stale reference after rerun | Use `parentElement.querySelector` inside timeout callback |
| `document.querySelector` for notification | Can't find shadow DOM elements | Use `parentElement.querySelector` |
| Registering component inside function | Duplicate registrations, confusing behavior | Register once at module level |
| Reading trigger value across reruns | Trigger resets, data disappears | Use `setStateValue` for persistent data |
| `use_container_width` (deprecated) | Deprecation warnings | Use `width="stretch"` or `width="content"` |
| `st.components.v2.component()` in Snowflake | `Unsupported component error` removed by security policy | Use native Streamlit widgets (Option 4) |
| `st.components.v1.html()` (deprecated) | Form renders but data never reaches Python | Use native Streamlit widgets (Option 4) |
| `row.get("col", default)` on Snowflake Row | `Row object has no attribute get` | Use `row["col"]` with `or` for defaults: `row["col"] or ""` |
| `st.text_input(value=..., key=...)` for auto-fill | Widget locks to session_state[key], auto-fill silently ignored after first user edit | Use `setdefault` + direct widget-key assignment (see §12) |
| `st.session_state.pop()` to force `value=` re-init | Brittle, flickers, only works for one rerun | Set widget key directly: `st.session_state[key] = new_value` (see §12) |
| Importing `snowflake.snowpark` at module level | `ModuleNotFoundError` in local mode | Lazy import inside functions that need it |

---

## 11. Streamlit in Snowflake (Warehouse Runtime) Specifics

- **CSP blocks external scripts** — all HTML/CSS/JS must be inline
- **Package-based v2 components NOT supported** in warehouse runtime
- **`st.components.v2.component()` is REMOVED by Snowflake's security policy** — attempting to use it raises `Unsupported component error`
- **`st.components.v1.html()` is deprecated** — v1 communication pattern (`postMessage`) is broken
- **Use native Streamlit widgets** for forms and interactive UI in Snowflake (see Option 4 in error message)
- **32 MB message limit** — use `utils/display_safety.py` guards
- **`st.set_page_config`** — `page_title`, `page_icon`, `menu_items` not supported
- **Single-session caching** — cached values not shared between viewers
- **`QUERY_WAREHOUSE`** sets code warehouse, not query warehouse

---

## 12. The `value=` + `key=` Anti-Pattern (Programmatic Auto-Fill)

§6 covers the *display* side of the same bug (disabled widgets with `value=`). This section covers the *input* side: when a widget needs to be auto-filled programmatically (e.g. derive a table name from a selected PDF filename), the naive approach silently breaks.

### ❌ Bad: combine `value=` and `key=`

```python
# BROKEN: once 'cssw_table_widget' exists in session_state, value= is ignored
_tbl_val = jbv("table_name")  # helper key (source of truth)
target_table_name = st.text_input("Target Table Name", value=_tbl_val, key="cssw_table_widget")
```

On the first render, this *appears* to work. But once the user types something, `session_state["cssw_table_widget"]` holds their input, and `value=_tbl_val` is silently ignored on every subsequent rerun. The auto-fill handler that updates `_tbl_val` has no effect on the widget — the user sees their old input forever.

### ❌ Worse: the `pop()` workaround

```python
# BRITTLE: delete the widget key to force value= re-initialization
if sel_file != _file_val:
    jbsync("file", sel_file)
    jbsync("table_name", normalize_pdf_to_table_name(sel_file))
    st.session_state.pop("cssw_table_widget", None)  # force re-init from value=
```

This *seems* to work, but:
1. The `pop()` triggers a Streamlit `KeyError` warning in some versions
2. If the widget re-renders before the pop completes, the old value flashes
3. It only works for *one* rerun — the next time the user types, the lock returns
4. It conflicts with the `setdefault` pattern used elsewhere (causes flicker)

### ✅ Correct: `setdefault` + direct widget-key assignment

```python
# 1. When the user changes the PDF, set the widget key DIRECTLY (not just the helper key)
if sel_file != _file_val:
    jbsync("file", sel_file)
    normalized = normalize_pdf_to_table_name(sel_file)
    jbsync("table_name", normalized)
    st.session_state["cssw_table_widget"] = normalized  # ← widget reads from here

# 2. When the widget renders, initialize ONLY if the key doesn't exist yet
_tbl_val = jbv("table_name")  # helper key (source of truth, survives page navigation)
st.session_state.setdefault("cssw_table_widget", _tbl_val)  # no-op if key exists
target_table_name = st.text_input("Target Table Name", key="cssw_table_widget")  # ← no value=
if target_table_name != _tbl_val:
    jbsync("table_name", target_table_name)  # sync user edits back to helper key
```

### Why this works

| Scenario | What happens |
|----------|--------------|
| First render (no PDF) | `setdefault` initializes widget to `_tbl_val` (= `"SUS_CHUNKS"` default) |
| User selects PDF | Handler writes new value to `session_state["cssw_table_widget"]` directly → widget picks it up on next rerun |
| User edits table name | Widget updates `session_state["cssw_table_widget"]` automatically → `if target_table_name != _tbl_val` triggers `jbsync` |
| User changes PDF again | Handler overwrites widget key with new normalized name (by design — user's manual edit is replaced) |
| Cross-page navigation | Streamlit clears widget key; on return, `setdefault` re-initializes from the persistent helper key `_jbv_table_name` |

### The two-key discipline

- **Helper key** (`_jbv_<field>`): persistent source of truth, survives page navigation, never cleared by Streamlit
- **Widget key** (`cssw_<field>_widget`): transient, cleared by Streamlit on page navigation, set via `setdefault` from the helper key on each render

Never read from the widget key in another page — it might not exist. Never write to the helper key from another page expecting the widget to update — the widget key needs to be set directly.

### Regression test

`tests/test_wizard.py::TestTableNameWidgetPattern` AST-inspects both `page2_builder.py` and `tab_config.py` and fails if the `value=` + `key=` combo reappears on the Target Table Name widget. `TestTableNameAutoFillFlow` simulates 8 scenarios with a fake `session_state` dict.

---

## References

- Streamlit CCv2 docs: `references/custom-components-v2.md`
- State sync patterns: `references/ccv2-state-sync.md`
- Snowflake limitations: https://docs.snowflake.com/en/developer-guide/streamlit/limitations
- Snowflake runtime: https://docs.snowflake.com/en/developer-guide/streamlit/app-development/runtime-environments
- Streamlit session_state docs: https://docs.streamlit.io/develop/concepts/architecture/session-state
