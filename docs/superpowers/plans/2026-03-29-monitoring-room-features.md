# Monitoring Room Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add RBAC, shift logbook, inline tag trending, kiosk auto-cycle, and updated documentation to make PLC4X Manager production-ready for 24h monitoring rooms.

**Architecture:** All features are independent and modify the existing Flask backend (`admin/app.py`) and vanilla JS frontend (`admin/static/js/app.js`, `admin/templates/index.html`). No new dependencies. Each task produces working, testable software.

**Tech Stack:** Flask, vanilla JavaScript, SVG charts, CSS, Docker

---

## File Structure

| File | Responsibility |
|------|---------------|
| `admin/app.py` | Add RBAC middleware, logbook CRUD endpoints, trending query endpoint, user role field |
| `admin/static/js/app.js` | Logbook UI, trending modal, kiosk mode, RBAC-aware tab visibility |
| `admin/templates/index.html` | Logbook tab, kiosk button in header, role indicator |
| `admin/templates/login.html` | Show role after login |
| `admin/static/css/style.css` | Kiosk mode styles, logbook styles, trending modal |
| `README.md` | Updated feature list, architecture diagram, API reference |
| `docs/INSTALLATION.md` | Updated setup guide with RBAC, logbook, kiosk instructions |

---

### Task 1: RBAC — Role-Based Access Control

Two roles: **admin** (full access) and **operator** (view + acknowledge alarms + logbook only).

**Files:**
- Modify: `admin/app.py` (auth, JWT, role check middleware)
- Modify: `admin/static/js/app.js` (tab visibility, button hiding)
- Modify: `admin/templates/index.html` (role indicator in header)

- [ ] **Step 1: Add ADMIN_USERS config with roles**

In `admin/app.py`, replace single `ADMIN_USERNAME`/`ADMIN_PASSWORD` with a multi-user structure. Add after line 42:

```python
# Users: {username: {password, role}}  role = "admin" or "operator"
# Default: admin/admin (admin), operator/operator (operator)
USERS = {}
_users_env = os.environ.get("USERS_JSON", "")
if _users_env:
    try:
        USERS = _json.loads(_users_env)
    except Exception:
        pass
if not USERS:
    USERS = {
        ADMIN_USERNAME: {"password": ADMIN_PASSWORD, "role": "admin"},
    }
    op_user = os.environ.get("OPERATOR_USERNAME", "operator")
    op_pass = os.environ.get("OPERATOR_PASSWORD", "operator")
    if op_user:
        USERS[op_user] = {"password": op_pass, "role": "operator"}
```

- [ ] **Step 2: Update login to use USERS dict and include role in JWT**

In `api_login()`, replace the `hmac.compare_digest` check with:

```python
user_entry = USERS.get(username)
if user_entry and hmac.compare_digest(password, user_entry["password"]):
    role = user_entry.get("role", "operator")
    token = create_jwt_token(username, role)
    audit_log("login", {"status": "success", "role": role}, user=username)
    return jsonify({"token": token, "username": username, "role": role, "expiresIn": JWT_EXPIRY_HOURS * 3600})
```

Update `create_jwt_token` to accept and include role:

```python
def create_jwt_token(username, role="admin"):
    payload = {
        "sub": username,
        "role": role,
        "iat": datetime.datetime.now(datetime.timezone.utc),
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=JWT_EXPIRY_HOURS)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")
```

- [ ] **Step 3: Set g.role in before_request**

Where `g.user` is set from JWT payload, also set:

```python
g.user = payload.get("sub")
g.role = payload.get("role", "operator")
```

- [ ] **Step 4: Add require_admin decorator**

```python
def require_admin(f):
    """Decorator: returns 403 if user is not admin."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if getattr(g, "role", "operator") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated
```

Apply `@require_admin` to these endpoint groups:
- All `PUT/POST/DELETE /api/devices/*` (device/tag CRUD)
- All `PUT/POST/DELETE /api/hmi/*` (HMI config changes)
- `PUT /api/config`, `PUT /api/config/server`
- `POST /api/server/restart`
- All `/api/users/*` (OPC-UA user management)
- All `/api/security/*` write operations
- `PUT /api/devices/*/oee-config`
- `PUT /api/devices/*/tags/*/alarms`
- `POST /api/plc4x/update`, `POST /api/plc4x/rollback`
- `POST /api/auth/change-password`

Do NOT restrict (operators need these):
- All GET endpoints (read data)
- `POST /api/alarms/acknowledge`, `POST /api/alarms/acknowledge-all`
- `POST /api/live/write` (controlled by allowWrite per device)
- `GET /api/export/csv`, `GET /api/export/pdf`
- Logbook endpoints (Task 2)
- `POST /api/auth/login`

- [ ] **Step 5: Frontend — store role, hide admin tabs for operators**

In `app.js`, after login success, store role:

```javascript
sessionStorage.setItem("jwt_role", data.role || "admin");
```

Add helper:

```javascript
function getRole() { return sessionStorage.getItem("jwt_role") || "admin"; }
function isAdmin() { return getRole() === "admin"; }
```

In `showTab` init and DOMContentLoaded, hide admin-only tabs:

```javascript
const adminTabs = ["devices", "server", "security", "backups", "apidocs"];
if (!isAdmin()) {
    adminTabs.forEach(t => {
        const btn = document.querySelector(`.tab[onclick*="'${t}'"]`);
        if (btn) btn.style.display = "none";
    });
}
```

- [ ] **Step 6: Show role indicator in header**

In `index.html`, after the logout button area, add:

```html
<span id="userRole" style="font-size:0.75rem;opacity:0.7"></span>
```

In `app.js` DOMContentLoaded:

```javascript
document.getElementById("userRole").textContent = `${sessionStorage.getItem("jwt_user")} (${getRole()})`;
```

- [ ] **Step 7: Commit**

```
feat: add RBAC with admin and operator roles
```

---

### Task 2: Shift Logbook

Operators record observations, incidents, and handover notes per shift. Stored as JSONL file (same pattern as audit trail).

**Files:**
- Modify: `admin/app.py` (logbook CRUD endpoints)
- Modify: `admin/static/js/app.js` (logbook tab UI)
- Modify: `admin/templates/index.html` (logbook tab)

- [ ] **Step 1: Add logbook endpoints in app.py**

```python
LOGBOOK_PATH = os.path.join(os.environ.get("CONFIG_PATH", "/app/config/config.yml").rsplit("/", 1)[0], "logbook.jsonl")
LOGBOOK_LOCK = FileLock("/tmp/plc4x_logbook.lock", timeout=5)


@app.route("/api/logbook", methods=["GET"])
def api_logbook_list():
    """Returns logbook entries. Query params: lines (default 100), shift (filter)."""
    lines = request.args.get("lines", 100, type=int)
    lines = max(1, min(lines, 1000))
    shift_filter = request.args.get("shift")
    try:
        with LOGBOOK_LOCK:
            with open(LOGBOOK_PATH, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
    except FileNotFoundError:
        return jsonify({"entries": [], "total": 0})
    entries = []
    for line in reversed(all_lines):
        try:
            entry = _json.loads(line.strip())
            if shift_filter and entry.get("shift") != shift_filter:
                continue
            entries.append(entry)
            if len(entries) >= lines:
                break
        except Exception:
            continue
    return jsonify({"entries": entries, "total": len(all_lines)})


@app.route("/api/logbook", methods=["POST"])
def api_logbook_add():
    """Adds a logbook entry. Body: {shift, category, message, priority}"""
    data = request.get_json()
    if not data or not data.get("message", "").strip():
        return jsonify({"error": "'message' is required"}), 400
    entry = {
        "id": f"log-{int(datetime.datetime.now().timestamp()*1000)}",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "user": getattr(g, "user", "unknown"),
        "shift": data.get("shift", ""),
        "category": data.get("category", "observation"),
        "priority": data.get("priority", "normal"),
        "message": data["message"].strip()
    }
    with LOGBOOK_LOCK:
        with open(LOGBOOK_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry) + "\n")
    return jsonify(entry), 201
```

- [ ] **Step 2: Add Logbook tab in index.html**

Add tab button after Audit:

```html
<button class="tab" onclick="showTab('logbook',event)">Logbook</button>
```

Add tab content:

```html
<div id="tab-logbook" class="tab-content" style="display:none">
    <div class="card">
        <div class="card-header">
            <h2>Shift Logbook</h2>
            <div style="display:flex;gap:8px;align-items:center">
                <select id="logbookShift" onchange="loadLogbook()">
                    <option value="">All shifts</option>
                    <option value="morning">Morning (06-14)</option>
                    <option value="afternoon">Afternoon (14-22)</option>
                    <option value="night">Night (22-06)</option>
                </select>
                <button class="btn btn-primary btn-sm" onclick="showLogbookForm()">+ New Entry</button>
            </div>
        </div>
        <div class="card-body">
            <div id="logbookForm" style="display:none;margin-bottom:16px;padding:12px;border:1px solid var(--border);border-radius:6px">
                <div style="display:flex;gap:8px;margin-bottom:8px">
                    <select id="logCategory" style="flex:1">
                        <option value="observation">Observation</option>
                        <option value="incident">Incident</option>
                        <option value="maintenance">Maintenance</option>
                        <option value="handover">Shift Handover</option>
                        <option value="alarm">Alarm Note</option>
                    </select>
                    <select id="logPriority" style="flex:1">
                        <option value="normal">Normal</option>
                        <option value="important">Important</option>
                        <option value="critical">Critical</option>
                    </select>
                    <select id="logShift" style="flex:1">
                        <option value="morning">Morning</option>
                        <option value="afternoon">Afternoon</option>
                        <option value="night">Night</option>
                    </select>
                </div>
                <textarea id="logMessage" rows="3" placeholder="Enter observation, incident, or handover note..." style="width:100%;padding:8px;border:1px solid var(--border);border-radius:4px;resize:vertical"></textarea>
                <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">
                    <button class="btn btn-outline btn-sm" onclick="document.getElementById('logbookForm').style.display='none'">Cancel</button>
                    <button class="btn btn-primary btn-sm" onclick="saveLogbookEntry()">Save</button>
                </div>
            </div>
            <div id="logbookContent"><p class="text-muted">Loading...</p></div>
        </div>
    </div>
</div>
```

- [ ] **Step 3: Add logbook JS functions in app.js**

Add to showTab:
```javascript
if (name === "logbook") loadLogbook();
```

Functions:

```javascript
function showLogbookForm() {
    const form = document.getElementById("logbookForm");
    form.style.display = "block";
    // Auto-detect shift from current hour
    const hour = new Date().getHours();
    const shift = hour >= 6 && hour < 14 ? "morning" : hour >= 14 && hour < 22 ? "afternoon" : "night";
    document.getElementById("logShift").value = shift;
    document.getElementById("logMessage").focus();
}

async function saveLogbookEntry() {
    const message = document.getElementById("logMessage").value.trim();
    if (!message) { toast("Message is required", "error"); return; }
    const body = {
        message,
        category: document.getElementById("logCategory").value,
        priority: document.getElementById("logPriority").value,
        shift: document.getElementById("logShift").value
    };
    try {
        await api("/api/logbook", "POST", body);
        toast("Logbook entry saved");
        document.getElementById("logbookForm").style.display = "none";
        document.getElementById("logMessage").value = "";
        loadLogbook();
    } catch (e) { toast(e.message, "error"); }
}

async function loadLogbook() {
    const container = document.getElementById("logbookContent");
    if (!container) return;
    const shift = document.getElementById("logbookShift")?.value || "";
    try {
        let url = "/api/logbook?lines=100";
        if (shift) url += `&shift=${shift}`;
        const data = await api(url);
        const entries = data.entries || [];
        if (entries.length === 0) {
            container.innerHTML = '<p class="text-muted">No logbook entries.</p>';
            return;
        }
        let html = "";
        for (const e of entries) {
            const ts = e.timestamp ? new Date(e.timestamp).toLocaleString() : "";
            const prCls = e.priority === "critical" ? "badge-error" : e.priority === "important" ? "badge-warn" : "badge-ok";
            const catCls = e.category === "incident" ? "badge-error" : e.category === "handover" ? "badge-warn" : "badge-ok";
            html += `<div style="padding:10px 14px;border:1px solid var(--border);border-radius:6px;margin-bottom:8px${e.priority === "critical" ? ";border-left:3px solid var(--danger)" : e.priority === "important" ? ";border-left:3px solid var(--warning)" : ""}">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                    <div style="display:flex;gap:6px;align-items:center">
                        <strong>${escHtml(e.user)}</strong>
                        <span class="status-badge ${catCls}" style="font-size:0.65rem">${escHtml(e.category)}</span>
                        <span class="status-badge ${prCls}" style="font-size:0.65rem">${escHtml(e.priority)}</span>
                        ${e.shift ? `<span class="text-muted" style="font-size:0.75rem">${escHtml(e.shift)}</span>` : ""}
                    </div>
                    <span class="text-muted" style="font-size:0.8rem">${ts}</span>
                </div>
                <div style="font-size:0.9rem;white-space:pre-wrap">${escHtml(e.message)}</div>
            </div>`;
        }
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p class="text-muted">Error loading logbook.</p>';
    }
}
```

- [ ] **Step 4: Commit**

```
feat: add shift logbook for operator observations and handover notes
```

---

### Task 3: Inline Tag Trending

Click a tag anywhere (Live Data, Dashboard) to see a quick trend chart in a modal. No Grafana needed.

**Files:**
- Modify: `admin/app.py` (tag history query endpoint)
- Modify: `admin/static/js/app.js` (trending modal with SVG chart)

- [ ] **Step 1: Add tag history endpoint**

```python
@app.route("/api/tags/history", methods=["GET"])
def api_tag_history():
    """Returns tag value history for trending. Query: device, tag, hours (default 1)."""
    device = request.args.get("device")
    tag_alias = request.args.get("tag")
    if not device or not tag_alias:
        return jsonify({"error": "'device' and 'tag' are required"}), 400
    hours = request.args.get("hours", 1, type=int)
    hours = max(1, min(hours, 720))
    try:
        _safe_flux_str(device)
        _safe_flux_str(tag_alias)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    bucket = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")
    # Use appropriate aggregation window
    window = "5s" if hours <= 1 else "30s" if hours <= 6 else "1m" if hours <= 24 else "5m" if hours <= 168 else "30m"
    flux = f'''
    from(bucket: "{bucket}")
      |> range(start: -{hours}h)
      |> filter(fn: (r) => r._measurement == "plc4x_tags")
      |> filter(fn: (r) => r.device == "{device}")
      |> filter(fn: (r) => r.alias == "{tag_alias}")
      |> filter(fn: (r) => r._field == "value")
      |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
      |> sort(columns: ["_time"])
      |> limit(n: 2000)
    '''
    try:
        records = _influx_query(flux)
        points = [{"t": r.get_time().isoformat(), "v": r.get_value()} for r in records if r.get_value() is not None]
        return jsonify({"device": device, "tag": tag_alias, "hours": hours, "points": points})
    except Exception as e:
        return jsonify({"error": f"Query failed: {e}"}), 500
```

- [ ] **Step 2: Add trending modal in app.js**

```javascript
async function showTagTrend(device, tag, hours) {
    hours = hours || 1;
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.style.display = "flex";
    overlay.innerHTML = `<div class="modal" style="max-width:700px;width:90%">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <h3 style="margin:0">${escHtml(device)} / ${escHtml(tag)}</h3>
            <div style="display:flex;gap:6px">
                <button class="btn btn-outline btn-sm trend-period" data-h="1">1h</button>
                <button class="btn btn-outline btn-sm trend-period" data-h="6">6h</button>
                <button class="btn btn-outline btn-sm trend-period" data-h="24">24h</button>
                <button class="btn btn-outline btn-sm trend-period" data-h="168">7d</button>
                <button class="btn btn-outline btn-sm" onclick="this.closest('.modal-overlay').remove()">Close</button>
            </div>
        </div>
        <div id="trendChart" style="width:100%;height:250px"><p class="text-muted" style="text-align:center;padding:40px">Loading...</p></div>
        <div id="trendStats" style="font-size:0.8rem;margin-top:8px;color:var(--text-secondary)"></div>
    </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener("click", e => { if (e.target === overlay) overlay.remove(); });
    // Period buttons
    overlay.querySelectorAll(".trend-period").forEach(btn => {
        btn.addEventListener("click", () => loadTrendData(device, tag, parseInt(btn.dataset.h), overlay));
    });
    loadTrendData(device, tag, hours, overlay);
}

async function loadTrendData(device, tag, hours, overlay) {
    const chart = overlay.querySelector("#trendChart");
    const stats = overlay.querySelector("#trendStats");
    // Highlight active period button
    overlay.querySelectorAll(".trend-period").forEach(b => b.classList.toggle("active", parseInt(b.dataset.h) === hours));
    chart.innerHTML = '<p class="text-muted" style="text-align:center;padding:40px">Loading...</p>';
    try {
        const data = await api(`/api/tags/history?device=${encodeURIComponent(device)}&tag=${encodeURIComponent(tag)}&hours=${hours}`);
        const points = data.points || [];
        if (points.length === 0) {
            chart.innerHTML = '<p class="text-muted" style="text-align:center;padding:40px">No data for this period.</p>';
            stats.innerHTML = "";
            return;
        }
        // Calculate stats
        const values = points.map(p => p.v);
        const min = Math.min(...values), max = Math.max(...values);
        const avg = values.reduce((a, b) => a + b, 0) / values.length;
        const last = values[values.length - 1];
        stats.innerHTML = `<strong>Last:</strong> ${last.toFixed(2)} | <strong>Min:</strong> ${min.toFixed(2)} | <strong>Max:</strong> ${max.toFixed(2)} | <strong>Avg:</strong> ${avg.toFixed(2)} | ${points.length} samples`;
        // Render SVG chart
        renderTrendChart(chart, points, min, max);
    } catch (e) {
        chart.innerHTML = `<p class="text-muted" style="text-align:center;padding:40px">${escHtml(e.message)}</p>`;
    }
}

function renderTrendChart(container, points, dataMin, dataMax) {
    const w = container.clientWidth || 650, h = 230;
    const pad = { l: 55, r: 10, t: 10, b: 30 };
    const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
    const range = dataMax - dataMin || 1;
    const margin = range * 0.1;
    const yMin = dataMin - margin, yMax = dataMax + margin;
    const yRange = yMax - yMin;

    const polyPoints = points.map((p, i) => {
        const x = pad.l + (i / (points.length - 1 || 1)) * pw;
        const y = pad.t + ph - ((p.v - yMin) / yRange) * ph;
        return `${x},${y}`;
    }).join(" ");

    // Y-axis labels
    const ySteps = 5;
    let yLabels = "";
    for (let i = 0; i <= ySteps; i++) {
        const val = yMin + (i / ySteps) * yRange;
        const y = pad.t + ph - (i / ySteps) * ph;
        yLabels += `<text x="${pad.l - 5}" y="${y + 3}" text-anchor="end" font-size="9" fill="var(--text-muted)">${val.toFixed(1)}</text>`;
        yLabels += `<line x1="${pad.l}" y1="${y}" x2="${w - pad.r}" y2="${y}" stroke="var(--border)" stroke-width="0.5"/>`;
    }

    // X-axis time labels
    const step = Math.max(1, Math.floor(points.length / 6));
    let xLabels = "";
    for (let i = 0; i < points.length; i += step) {
        const x = pad.l + (i / (points.length - 1 || 1)) * pw;
        const t = new Date(points[i].t);
        const lbl = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        xLabels += `<text x="${x}" y="${h - 5}" text-anchor="middle" font-size="9" fill="var(--text-muted)">${lbl}</text>`;
    }

    container.innerHTML = `<svg width="${w}" height="${h}" style="display:block">
        ${yLabels}${xLabels}
        <polyline points="${polyPoints}" fill="none" stroke="#c8102e" stroke-width="2"/>
    </svg>`;
}
```

- [ ] **Step 3: Make tags clickable in Live Data view**

In the `loadLiveData` function, where tags are rendered in the table, wrap tag values with an onclick:

```javascript
// In the tag value cell, change from:
`<td>${value}</td>`
// To:
`<td style="cursor:pointer" onclick="showTagTrend('${escAttr(deviceName)}','${escAttr(tag.alias)}')" title="Click for trend">${value}</td>`
```

- [ ] **Step 4: Commit**

```
feat: add inline tag trending modal with SVG chart
```

---

### Task 4: Auto-Cycle Kiosk Mode

For TV monitors in the monitoring room. Auto-cycles through Dashboard, HMI screens, and OEE at configurable intervals.

**Files:**
- Modify: `admin/static/js/app.js` (kiosk logic)
- Modify: `admin/templates/index.html` (kiosk button)
- Modify: `admin/static/css/style.css` (kiosk fullscreen styles)

- [ ] **Step 1: Add kiosk button in header**

In `index.html`, after the logout button area:

```html
<button class="btn btn-outline btn-sm" id="kioskBtn" onclick="toggleKiosk()" title="Kiosk Mode">Kiosk</button>
```

- [ ] **Step 2: Add kiosk CSS styles**

```css
.kiosk-mode .header-tabs,
.kiosk-mode #kioskBtn,
.kiosk-mode .card-header .btn,
.kiosk-mode .footer {
    display: none !important;
}
.kiosk-mode .header {
    padding: 4px 16px;
}
.kiosk-mode .header h1 {
    font-size: 1rem;
}
.kiosk-mode .main {
    padding: 8px;
}
.kiosk-mode .card {
    border: none;
    box-shadow: none;
}
.kiosk-mode #kioskExitBtn {
    display: block !important;
}
```

- [ ] **Step 3: Add kiosk JS logic**

```javascript
let _kioskInterval = null;
let _kioskScreens = [];
let _kioskIndex = 0;
let _kioskDelay = 30000; // 30 seconds

function toggleKiosk() {
    if (_kioskInterval) {
        exitKiosk();
        return;
    }
    // Determine screens to cycle: dashboard, each HMI equipment in fullscreen, OEE
    _kioskScreens = ["dashboard"];
    // Add HMI screens if available
    if (typeof _hmiConfig !== "undefined" && _hmiConfig.plants) {
        for (const plant of _hmiConfig.plants) {
            for (const area of (plant.areas || [])) {
                for (const eq of (area.equipment || [])) {
                    _kioskScreens.push({ type: "hmi", plantId: plant.id, areaId: area.id, equipId: eq.id, label: eq.name });
                }
            }
        }
    }
    _kioskScreens.push("oee");
    _kioskIndex = 0;

    document.body.classList.add("kiosk-mode");
    showKioskScreen();
    _kioskInterval = setInterval(() => {
        _kioskIndex = (_kioskIndex + 1) % _kioskScreens.length;
        showKioskScreen();
    }, _kioskDelay);

    // ESC to exit
    document.addEventListener("keydown", kioskEscHandler);
}

function kioskEscHandler(e) {
    if (e.key === "Escape") exitKiosk();
}

function exitKiosk() {
    if (_kioskInterval) { clearInterval(_kioskInterval); _kioskInterval = null; }
    document.body.classList.remove("kiosk-mode");
    document.removeEventListener("keydown", kioskEscHandler);
    showTab("dashboard");
}

function showKioskScreen() {
    const screen = _kioskScreens[_kioskIndex];
    if (screen === "dashboard") {
        showTab("dashboard");
    } else if (screen === "oee") {
        showTab("oee");
    } else if (screen.type === "hmi") {
        showTab("hmi");
        // Navigate to the specific equipment and show fullscreen
        _hmiCurrentPlant = screen.plantId;
        _hmiCurrentArea = screen.areaId;
        _hmiCurrentEquipment = screen.equipId;
        hmiShowScreen();
    }
}
```

- [ ] **Step 4: Add URL parameter for auto-start kiosk**

In DOMContentLoaded:

```javascript
if (new URLSearchParams(window.location.search).has("kiosk")) {
    setTimeout(toggleKiosk, 2000); // Wait for data to load
}
```

This allows bookmarking `http://localhost:8080?kiosk` for TV monitors.

- [ ] **Step 5: Commit**

```
feat: add kiosk mode with auto-cycling screens for TV monitors
```

---

### Task 5: Documentation Update

Update README and installation guide with all new features.

**Files:**
- Modify: `README.md`
- Modify: `docs/INSTALLATION.md`

- [ ] **Step 1: Update README.md feature list**

Add to the features section:
```markdown
- **Alarm system**: per-tag thresholds with conditional profiles (per product), virtual tags, sound notification, acknowledge, history
- **OEE dashboard**: Availability x Performance x Quality with SVG gauges and trend
- **Multi-plant dashboard**: consolidated view with device status, alarms, OEE per plant
- **PDF reports & CSV export**: device status, tag statistics, alarm history
- **Shift logbook**: operator observations, incidents, handover notes per shift
- **Inline tag trending**: click any tag for instant SVG trend chart
- **Kiosk mode**: auto-cycling screens for monitoring room TV displays
- **RBAC**: admin and operator roles with granular access control
- **Audit trail**: automatic logging of all actions with user, timestamp, IP
```

- [ ] **Step 2: Update README API Reference**

Add new sections:
```markdown
### Alarms
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/alarms` | Active alarms and history |
| POST | `/api/alarms/acknowledge` | Acknowledge an alarm |
| POST | `/api/alarms/acknowledge-all` | Acknowledge all alarms |
| PUT | `/api/devices/<name>/tags/<alias>/alarms` | Set alarm thresholds |

### OEE
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/oee/calculate` | Calculate OEE for a device |
| GET | `/api/oee/trend` | OEE trend data |
| PUT | `/api/devices/<name>/oee-config` | Configure OEE |

### Reports
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/export/csv` | Export tag history as CSV |
| GET | `/api/export/pdf` | Generate PDF report |

### Logbook
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/logbook` | List logbook entries |
| POST | `/api/logbook` | Add logbook entry |

### Audit
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/audit` | Audit trail entries |

### Tag Trending
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/tags/history` | Tag value history for trending |
```

- [ ] **Step 3: Update README architecture diagram**

Update the containers table to reflect current feature set.

- [ ] **Step 4: Update docs/INSTALLATION.md**

Add sections for:
- RBAC setup (OPERATOR_USERNAME, OPERATOR_PASSWORD env vars)
- Kiosk mode (URL parameter `?kiosk`)
- Alarm configuration guide
- OEE configuration guide

- [ ] **Step 5: Commit**

```
docs: update README and installation guide with all new features
```

---

## Self-Review Checklist

1. **Spec coverage:** All 5 features covered — RBAC (Task 1), Logbook (Task 2), Trending (Task 3), Kiosk (Task 4), Docs (Task 5). No gaps.
2. **Placeholder scan:** All steps contain actual code. No "TBD" or "add appropriate handling".
3. **Type consistency:** `g.role` set in Task 1 Step 3, used in Step 4 decorator. `getRole()`/`isAdmin()` defined in Step 5, used in Step 6. `showTagTrend` defined in Task 3 Step 2, called in Step 3. Consistent.
