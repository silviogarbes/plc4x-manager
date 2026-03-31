# Phase 1: HMI Replay / Time Travel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add DVR-style replay to HMI screens — operators select a date/time and see historical tag values driving the same Konva.js elements (gauges, tanks, valves, motors).

**Architecture:** Reuses 100% of existing HMI rendering. New backend endpoints query InfluxDB for historical snapshots. Frontend adds a timeline bar with play/pause/speed controls. On entering replay, live updates stop; on exit, they resume.

**Tech Stack:** FastAPI (backend), InfluxDB Flux queries, Konva.js (frontend), vanilla JS

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `admin/routes/replay_routes.py` | `/api/replay/snapshot` and `/api/replay/range` endpoints |
| Modify | `admin/main.py` | Register replay_routes router |
| Modify | `admin/static/js/hmi.js` | Replay UI: toolbar button, panel, DVR controls, frame injection |
| Modify | `admin/static/css/hmi.css` | Replay bar, banner, and control styles |

---

## Task 1: Create Replay Backend — Snapshot Endpoint

**Files:**
- Create: `admin/routes/replay_routes.py`

- [ ] **Step 1: Create `admin/routes/replay_routes.py` with the snapshot endpoint**

```python
"""
HMI Replay / Time Travel routes for PLC4X Manager FastAPI.

Endpoints:
  GET /api/replay/snapshot  — all tag values for a device at a specific timestamp
  GET /api/replay/range     — series of snapshots for DVR playback
"""

from __future__ import annotations

import asyncio
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import CurrentUser, get_current_user
from config_manager import find_device, load_config
from influx import _influx_query, _safe_flux_str

router = APIRouter(tags=["replay"])

# ISO 8601 timestamp validation (e.g. 2026-03-30T14:30:00Z)
_ISO8601_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$'
)

# Allowed step values for range queries
_ALLOWED_STEPS = {"5s", "10s", "30s", "1m", "5m", "15m", "30m", "1h", "6h", "1d"}

# 90 days in hours — threshold for switching to hourly bucket
_RAW_RETENTION_HOURS = 2160


def _validate_timestamp(ts: str) -> str:
    """Validate ISO 8601 timestamp format."""
    if not ts or not _ISO8601_RE.match(ts):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ISO 8601 timestamp: {ts!r}. Expected format: 2026-03-30T14:30:00Z"
        )
    return ts


def _enforce_plant_access(device: str, user: CurrentUser) -> None:
    """Raise 403 if user cannot access the device's plant."""
    config = load_config()
    _, dev = find_device(config, device)
    if dev is not None:
        if user.plants and dev.get("plant") not in user.plants:
            raise HTTPException(status_code=403, detail="Access denied")


def _select_bucket_for_age(hours_ago: float) -> str:
    """Select InfluxDB bucket based on how old the data is."""
    if hours_ago <= _RAW_RETENTION_HOURS:
        return os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")
    return "plc4x_hourly"


@router.get("/api/replay/snapshot")
async def api_replay_snapshot(
    device: str = Query(..., description="Device name"),
    timestamp: str = Query(..., description="ISO 8601 timestamp, e.g. 2026-03-30T14:30:00Z"),
    user: CurrentUser = Depends(get_current_user),
):
    """Return all tag values for a device at a specific historical timestamp.

    Queries InfluxDB for a +/-5 second window around the requested timestamp,
    grouped by tag alias, returning the last value in each window.
    """
    # Validate inputs
    try:
        _safe_flux_str(device)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _validate_timestamp(timestamp)

    # Plant access check
    _enforce_plant_access(device, user)

    # Determine age to select bucket
    from datetime import datetime, timezone
    requested = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    hours_ago = (now - requested).total_seconds() / 3600
    bucket = _select_bucket_for_age(hours_ago)

    flux = f'''
import "experimental"

from(bucket: "{bucket}")
  |> range(start: experimental.subDuration(d: 5s, from: {timestamp}),
           stop:  experimental.addDuration(d: 5s, to:  {timestamp}))
  |> filter(fn: (r) => r._measurement == "plc4x_tags")
  |> filter(fn: (r) => r.device == "{device}")
  |> filter(fn: (r) => r._field == "value")
  |> group(columns: ["alias"])
  |> last()
'''
    try:
        records = await asyncio.to_thread(_influx_query, flux)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"InfluxDB query failed: {e}")

    tags = []
    actual_timestamp = None
    for r in records:
        alias = r.values.get("alias", "")
        value = r.get_value()
        ts = r.get_time()
        if ts and actual_timestamp is None:
            actual_timestamp = ts.isoformat().replace("+00:00", "Z")
        tags.append({"alias": alias, "value": value})

    return {
        "device": device,
        "timestamp": timestamp,
        "actual_timestamp": actual_timestamp,
        "tags": tags,
        "source_bucket": bucket,
    }
```

- [ ] **Step 2: Verify the file was created correctly**

```bash
# From project root, check syntax
cd /c/Silvio/plc4x-manager && python -c "import py_compile; py_compile.compile('admin/routes/replay_routes.py', doraise=True)"
```

- [ ] **Step 3: Test the snapshot endpoint with curl (expect 401 without auth)**

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:3080/api/replay/snapshot?device=Demo-Simulated&timestamp=2026-03-30T12:00:00Z
# Expected: 401 (not yet registered, so actually 404 until Task 4)
```

- [ ] **Step 4: Commit**

```bash
git add admin/routes/replay_routes.py
git commit -m "feat(replay): add snapshot endpoint for HMI time travel"
```

---

## Task 2: Add Range Endpoint for DVR Playback

**Files:**
- Modify: `admin/routes/replay_routes.py`

- [ ] **Step 1: Add the `/api/replay/range` endpoint to `replay_routes.py`**

Append the following after the snapshot endpoint (after line ~103 in the file created in Task 1):

```python
@router.get("/api/replay/range")
async def api_replay_range(
    device: str = Query(..., description="Device name"),
    start: str = Query(..., description="ISO 8601 start timestamp"),
    end: str = Query(..., description="ISO 8601 end timestamp"),
    step: str = Query(default="30s", description="Aggregation step (5s, 10s, 30s, 1m, 5m, 15m, 30m, 1h, 6h, 1d)"),
    user: CurrentUser = Depends(get_current_user),
):
    """Return a series of tag-value snapshots for DVR-style playback.

    Queries InfluxDB for the specified range, aggregated at the given step.
    Returns frames (one per timestamp) each containing all tag values.
    Maximum 720 frames to prevent excessive memory usage.
    """
    # Validate inputs
    try:
        _safe_flux_str(device)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _validate_timestamp(start)
    _validate_timestamp(end)

    if step not in _ALLOWED_STEPS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid step: {step!r}. Allowed: {sorted(_ALLOWED_STEPS)}"
        )

    # Plant access check
    _enforce_plant_access(device, user)

    # Determine bucket based on age of start time
    from datetime import datetime, timezone
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)

    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="end must be after start")

    hours_ago = (now - start_dt).total_seconds() / 3600
    duration_hours = (end_dt - start_dt).total_seconds() / 3600

    # Smart bucket selection (mirrors data_routes.py logic)
    if hours_ago > _RAW_RETENTION_HOURS:
        bucket = "plc4x_hourly"
    elif duration_hours <= 6:
        bucket = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")
    elif duration_hours <= 168:
        bucket = "plc4x_hourly"
    else:
        bucket = "plc4x_daily" if duration_hours > 720 else "plc4x_hourly"

    flux = f'''
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {end})
  |> filter(fn: (r) => r._measurement == "plc4x_tags")
  |> filter(fn: (r) => r.device == "{device}")
  |> filter(fn: (r) => r._field == "value")
  |> aggregateWindow(every: {step}, fn: last, createEmpty: false)
  |> group(columns: ["_time"])
  |> sort(columns: ["alias"])
  |> limit(n: 720)
'''
    try:
        records = await asyncio.to_thread(_influx_query, flux)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"InfluxDB query failed: {e}")

    # Group records into frames by timestamp
    frame_map = {}  # timestamp_str -> [{alias, value}]
    for r in records:
        alias = r.values.get("alias", "")
        value = r.get_value()
        ts = r.get_time()
        if ts is None:
            continue
        ts_str = ts.isoformat().replace("+00:00", "Z")
        if ts_str not in frame_map:
            frame_map[ts_str] = []
        frame_map[ts_str].append({"alias": alias, "value": value})

    # Sort frames chronologically, enforce 720-frame limit
    sorted_ts = sorted(frame_map.keys())[:720]
    frames = [{"timestamp": ts, "tags": frame_map[ts]} for ts in sorted_ts]

    return {
        "device": device,
        "start": start,
        "end": end,
        "step": step,
        "frames": frames,
        "frame_count": len(frames),
        "source_bucket": bucket,
    }
```

- [ ] **Step 2: Verify syntax**

```bash
cd /c/Silvio/plc4x-manager && python -c "import py_compile; py_compile.compile('admin/routes/replay_routes.py', doraise=True)"
```

- [ ] **Step 3: Commit**

```bash
git add admin/routes/replay_routes.py
git commit -m "feat(replay): add range endpoint for DVR playback frames"
```

---

## Task 3: Register Replay Router in main.py

**Files:**
- Modify: `admin/main.py`

- [ ] **Step 1: Add replay_routes import at line 347 (after plctag_routes import)**

At `admin/main.py` line 347, after `from routes.plctag_routes import router as plctag_router`, add:

```python
from routes.replay_routes import router as replay_router
```

- [ ] **Step 2: Add `app.include_router(replay_router)` at line 368 (after plctag_router)**

At `admin/main.py` line 368, after `app.include_router(plctag_router)`, add:

```python
app.include_router(replay_router)
```

- [ ] **Step 3: Test endpoint is reachable (after container restart)**

```bash
# Rebuild and restart the admin container
cd /c/Silvio/plc4x-manager && docker compose up -d --build admin

# Wait for startup, then test (expect 401 = route is registered and auth is enforced)
curl -s -o /dev/null -w "%{http_code}" http://localhost:3080/api/replay/snapshot?device=Demo-Simulated&timestamp=2026-03-30T12:00:00Z
# Expected: 401

# Test with valid auth (adjust token as needed)
TOKEN=$(curl -s -X POST http://localhost:3080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | python -c "import sys,json; print(json.load(sys.stdin)['token'])")

# Snapshot test
curl -s http://localhost:3080/api/replay/snapshot?device=Demo-Simulated\&timestamp=2026-03-30T12:00:00Z \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
# Expected: {"device": "Demo-Simulated", "timestamp": "...", "tags": [...], ...}

# Range test
curl -s "http://localhost:3080/api/replay/range?device=Demo-Simulated&start=2026-03-30T11:00:00Z&end=2026-03-30T12:00:00Z&step=5m" \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
# Expected: {"device": "Demo-Simulated", "frames": [...], "frame_count": N, ...}
```

- [ ] **Step 4: Commit**

```bash
git add admin/main.py
git commit -m "feat(replay): register replay_routes router in main.py"
```

---

## Task 4: Add Replay CSS Styles

**Files:**
- Modify: `admin/static/css/hmi.css`

- [ ] **Step 1: Append replay styles to the end of `admin/static/css/hmi.css` (after line 238)**

```css
/* ─── Replay / Time Travel ─────────────────────────────────────────────── */

.hmi-replay-banner {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  z-index: 1000;
  background: #c8102e;
  color: #fff;
  text-align: center;
  padding: 6px 16px;
  font-weight: 600;
  font-size: 13px;
  letter-spacing: 0.5px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  font-family: "IBM Plex Sans", sans-serif;
}

.hmi-replay-banner .replay-back-btn {
  background: rgba(255, 255, 255, 0.2);
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 0.4);
  border-radius: var(--radius, 6px);
  padding: 3px 12px;
  cursor: pointer;
  font-size: 12px;
  font-weight: 500;
  transition: background var(--transition, 0.2s);
}

.hmi-replay-banner .replay-back-btn:hover {
  background: rgba(255, 255, 255, 0.35);
}

.hmi-replay-bar {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  z-index: 1000;
  background: var(--bg-card, #fff);
  border-top: 2px solid var(--border, #e0e0e0);
  padding: 10px 16px;
  display: none;
  flex-direction: column;
  gap: 8px;
  font-family: "IBM Plex Sans", sans-serif;
}

.hmi-replay-bar.active {
  display: flex;
}

.hmi-replay-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.hmi-replay-row label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-muted, #6b7280);
  min-width: 40px;
}

.hmi-replay-row input[type="datetime-local"] {
  font-size: 12px;
  padding: 4px 8px;
  border: 1px solid var(--border, #e0e0e0);
  border-radius: var(--radius, 6px);
  background: var(--bg-card, #fff);
  color: inherit;
  font-family: "IBM Plex Mono", monospace;
}

.hmi-replay-row select {
  font-size: 12px;
  padding: 4px 8px;
  border: 1px solid var(--border, #e0e0e0);
  border-radius: var(--radius, 6px);
  background: var(--bg-card, #fff);
  color: inherit;
}

.hmi-replay-controls {
  display: flex;
  align-items: center;
  gap: 6px;
}

.hmi-replay-controls button {
  background: var(--bg-card, #fff);
  border: 1px solid var(--border, #e0e0e0);
  border-radius: var(--radius, 6px);
  padding: 4px 10px;
  cursor: pointer;
  font-size: 13px;
  transition: background var(--transition, 0.2s);
  color: inherit;
}

.hmi-replay-controls button:hover {
  background: var(--border, #e0e0e0);
}

.hmi-replay-controls button.active {
  background: #c8102e;
  color: #fff;
  border-color: #c8102e;
}

.hmi-replay-mode-toggle {
  display: flex;
  border: 1px solid var(--border, #e0e0e0);
  border-radius: var(--radius, 6px);
  overflow: hidden;
}

.hmi-replay-mode-toggle button {
  border: none;
  border-radius: 0;
  padding: 4px 12px;
  font-size: 12px;
  cursor: pointer;
  background: var(--bg-card, #fff);
  color: inherit;
  transition: background var(--transition, 0.2s);
}

.hmi-replay-mode-toggle button.active {
  background: #c8102e;
  color: #fff;
}

.hmi-replay-scrubber {
  flex: 1;
  min-width: 120px;
  accent-color: #c8102e;
}

.hmi-replay-timestamp {
  font-size: 12px;
  font-family: "IBM Plex Mono", monospace;
  color: var(--text-muted, #6b7280);
  min-width: 170px;
  text-align: right;
}

.hmi-replay-speed {
  font-size: 11px;
  font-weight: 600;
  color: #c8102e;
  min-width: 30px;
  text-align: center;
}

.hmi-replay-toolbar-btn {
  background: none;
  border: 1px solid var(--border, #e0e0e0);
  border-radius: var(--radius, 6px);
  padding: 4px 10px;
  cursor: pointer;
  font-size: 13px;
  color: inherit;
  transition: background var(--transition, 0.2s);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.hmi-replay-toolbar-btn:hover {
  background: var(--border, #e0e0e0);
}

.hmi-replay-toolbar-btn.replay-active {
  background: #c8102e;
  color: #fff;
  border-color: #c8102e;
}
```

- [ ] **Step 2: Verify no CSS syntax errors (manual — open HMI page in browser, check console)**

- [ ] **Step 3: Commit**

```bash
git add admin/static/css/hmi.css
git commit -m "style(replay): add CSS for replay bar, banner, and DVR controls"
```

---

## Task 5: Add Replay Button to HMI Toolbar

**Files:**
- Modify: `admin/static/js/hmi.js`

- [ ] **Step 1: Add replay state variables at the top of hmi.js (after line 26, after `_hmiActiveAlarms`)**

Insert after `let _hmiActiveAlarms = {};`:

```javascript
// ── Replay / Time Travel state ──
let _hmiReplayMode = false;       // true when replay is active
let _hmiReplayFrames = [];        // array of {timestamp, tags:[{alias,value}]}
let _hmiReplayIndex = 0;          // current frame index
let _hmiReplayInterval = null;    // setInterval handle for DVR playback
let _hmiReplaySpeed = 1;          // playback speed multiplier (0.5, 1, 2, 4)
let _hmiReplayPlaying = false;    // true when DVR is auto-advancing
```

- [ ] **Step 2: Add replay toolbar button inside `hmiRenderToolbar()` at line 565**

In the `hmiRenderToolbar()` function, just before the closing `toolbar.innerHTML = html;` (line 587), insert:

```javascript
    // Replay button (always visible, not just in edit mode)
    html += '<div class="hmi-toolbar-separator"></div>';
    html += '<button class="hmi-replay-toolbar-btn" onclick="hmiToggleReplay()" title="Replay / Time Travel">&#x1F553; Replay</button>';
```

- [ ] **Step 3: Also render the replay button when NOT in edit mode**

Find the section that calls `hmiRenderToolbar()` only in edit mode. We need the replay button visible always. Add a new function that injects just the replay button when the toolbar is not in edit mode.

After the `hmiRenderToolbar()` function (after line 588), add:

```javascript
function hmiEnsureReplayButton() {
    // In edit mode, hmiRenderToolbar already adds the button.
    // In view mode, we inject a minimal replay button into the toolbar area.
    if (_hmiEditMode) return;
    const toolbar = document.getElementById("hmiToolbar");
    if (!toolbar) return;
    if (toolbar.querySelector(".hmi-replay-toolbar-btn")) return;
    const btn = document.createElement("button");
    btn.className = "hmi-replay-toolbar-btn" + (_hmiReplayMode ? " replay-active" : "");
    btn.onclick = hmiToggleReplay;
    btn.title = "Replay / Time Travel";
    btn.innerHTML = "&#x1F553; Replay";
    toolbar.appendChild(btn);
}
```

Call `hmiEnsureReplayButton()` at the end of the screen rendering flow. Inside `hmiUpdateAllElements()` at line 1740, add at the very end of the function (before the closing `}`):

```javascript
    hmiEnsureReplayButton();
```

- [ ] **Step 4: Commit**

```bash
git add admin/static/js/hmi.js
git commit -m "feat(replay): add replay button to HMI toolbar"
```

---

## Task 6: Implement Replay Panel and DVR Controls

**Files:**
- Modify: `admin/static/js/hmi.js`

- [ ] **Step 1: Add the replay panel HTML injection function**

Add after `hmiEnsureReplayButton()` (inserted in Task 5):

```javascript
// =============================================
// Replay / Time Travel (Phase 1)
// =============================================

function hmiToggleReplay() {
    if (_hmiReplayMode) {
        hmiExitReplay();
    } else {
        hmiEnterReplayPanel();
    }
}

function hmiEnterReplayPanel() {
    // Show the replay bar at the bottom
    let bar = document.getElementById("hmiReplayBar");
    if (!bar) {
        bar = document.createElement("div");
        bar.id = "hmiReplayBar";
        bar.className = "hmi-replay-bar";
        const container = document.getElementById("hmiCanvasContainer") || document.getElementById("hmiView");
        if (container) {
            container.style.position = "relative";
            container.appendChild(bar);
        } else {
            document.body.appendChild(bar);
        }
    }

    // Default times: last 1 hour
    const now = new Date();
    const oneHourAgo = new Date(now.getTime() - 3600000);
    const fmt = (d) => d.toISOString().slice(0, 16); // for datetime-local input

    bar.innerHTML = `
        <div class="hmi-replay-row">
            <label>Mode</label>
            <div class="hmi-replay-mode-toggle">
                <button id="replayModeSnapshot" class="active" onclick="hmiSetReplayMode('snapshot')">Snapshot</button>
                <button id="replayModeDvr" onclick="hmiSetReplayMode('dvr')">DVR</button>
            </div>
            <label>Start</label>
            <input type="datetime-local" id="replayStart" value="${fmt(oneHourAgo)}" step="1">
            <label id="replayEndLabel">End</label>
            <input type="datetime-local" id="replayEnd" value="${fmt(now)}" step="1">
            <label>Step</label>
            <select id="replayStep">
                <option value="5s">5s</option>
                <option value="10s">10s</option>
                <option value="30s" selected>30s</option>
                <option value="1m">1m</option>
                <option value="5m">5m</option>
                <option value="15m">15m</option>
                <option value="30m">30m</option>
                <option value="1h">1h</option>
            </select>
            <button class="hmi-replay-controls" onclick="hmiReplayGo()" style="background:#c8102e;color:#fff;border-color:#c8102e;padding:4px 14px;border-radius:6px;cursor:pointer;font-weight:600">Load</button>
        </div>
        <div class="hmi-replay-row" id="replayTransport" style="display:none">
            <div class="hmi-replay-controls">
                <button onclick="hmiReplayStepBack()" title="Step back">&#x23EE;</button>
                <button id="replayPlayBtn" onclick="hmiReplayTogglePlay()" title="Play / Pause">&#x25B6;</button>
                <button onclick="hmiReplayStepForward()" title="Step forward">&#x23ED;</button>
                <button onclick="hmiReplayChangeSpeed()" title="Change speed"><span class="hmi-replay-speed" id="replaySpeedLabel">1x</span></button>
            </div>
            <input type="range" class="hmi-replay-scrubber" id="replayScrubber" min="0" max="0" value="0"
                   oninput="hmiReplayScrub(this.value)">
            <span class="hmi-replay-timestamp" id="replayTimestamp">--</span>
        </div>
    `;
    bar.classList.add("active");
    hmiSetReplayMode("snapshot");
}

function hmiSetReplayMode(mode) {
    const snBtn = document.getElementById("replayModeSnapshot");
    const dvrBtn = document.getElementById("replayModeDvr");
    const endLabel = document.getElementById("replayEndLabel");
    const endInput = document.getElementById("replayEnd");
    const stepSelect = document.getElementById("replayStep");

    if (mode === "snapshot") {
        snBtn.classList.add("active");
        dvrBtn.classList.remove("active");
        if (endLabel) endLabel.style.display = "none";
        if (endInput) endInput.style.display = "none";
        if (stepSelect) stepSelect.parentElement.querySelector('label:last-of-type') || null;
        // Hide step and end for snapshot mode
        if (endInput) endInput.style.display = "none";
        if (endLabel) endLabel.style.display = "none";
        if (stepSelect) stepSelect.style.display = "none";
    } else {
        dvrBtn.classList.add("active");
        snBtn.classList.remove("active");
        if (endLabel) endLabel.style.display = "";
        if (endInput) endInput.style.display = "";
        if (stepSelect) stepSelect.style.display = "";
    }
}

async function hmiReplayGo() {
    const snBtn = document.getElementById("replayModeSnapshot");
    const isSnapshot = snBtn && snBtn.classList.contains("active");

    const equip = hmiGetCurrentEquipment();
    if (!equip) { alert("No HMI equipment selected"); return; }
    const device = equip.device || equip.name;
    if (!device) { alert("Equipment has no device assigned"); return; }

    if (isSnapshot) {
        await hmiReplayLoadSnapshot(device);
    } else {
        await hmiReplayLoadRange(device);
    }
}

async function hmiReplayLoadSnapshot(device) {
    const startInput = document.getElementById("replayStart");
    const ts = new Date(startInput.value).toISOString().replace(/\.\d+Z$/, "Z");

    try {
        const data = await api(`/api/replay/snapshot?device=${encodeURIComponent(device)}&timestamp=${encodeURIComponent(ts)}`);
        // Enter replay mode
        _hmiReplayMode = true;
        hmiStopLiveUpdates();
        hmiShowReplayBanner(data.actual_timestamp || ts);

        // Inject snapshot data into _hmiDeviceMap
        hmiReplayInjectTags(device, data.tags);
        hmiUpdateAllElements();

        // Highlight toolbar button
        const btn = document.querySelector(".hmi-replay-toolbar-btn");
        if (btn) btn.classList.add("replay-active");
    } catch (e) {
        alert("Replay snapshot failed: " + (e.message || e));
    }
}

async function hmiReplayLoadRange(device) {
    const startInput = document.getElementById("replayStart");
    const endInput = document.getElementById("replayEnd");
    const stepSelect = document.getElementById("replayStep");

    const startTs = new Date(startInput.value).toISOString().replace(/\.\d+Z$/, "Z");
    const endTs = new Date(endInput.value).toISOString().replace(/\.\d+Z$/, "Z");
    const step = stepSelect.value;

    try {
        const data = await api(`/api/replay/range?device=${encodeURIComponent(device)}&start=${encodeURIComponent(startTs)}&end=${encodeURIComponent(endTs)}&step=${encodeURIComponent(step)}`);

        if (!data.frames || data.frames.length === 0) {
            alert("No data found for the selected range");
            return;
        }

        // Enter replay mode
        _hmiReplayMode = true;
        _hmiReplayFrames = data.frames;
        _hmiReplayIndex = 0;
        _hmiReplaySpeed = 1;
        _hmiReplayPlaying = false;
        hmiStopLiveUpdates();

        // Show transport controls
        const transport = document.getElementById("replayTransport");
        if (transport) transport.style.display = "flex";
        const scrubber = document.getElementById("replayScrubber");
        if (scrubber) {
            scrubber.max = _hmiReplayFrames.length - 1;
            scrubber.value = 0;
        }

        hmiShowReplayBanner(_hmiReplayFrames[0].timestamp);
        hmiReplayShowFrame(0, device);

        // Highlight toolbar button
        const btn = document.querySelector(".hmi-replay-toolbar-btn");
        if (btn) btn.classList.add("replay-active");
    } catch (e) {
        alert("Replay range failed: " + (e.message || e));
    }
}

function hmiReplayInjectTags(device, tags) {
    if (!_hmiDeviceMap[device]) {
        _hmiDeviceMap[device] = { tags: {}, allowWrite: false, status: "replay" };
    }
    for (const t of tags) {
        _hmiDeviceMap[device].tags[t.alias] = { alias: t.alias, value: t.value };
    }
}

function hmiReplayShowFrame(index, device) {
    if (index < 0 || index >= _hmiReplayFrames.length) return;
    _hmiReplayIndex = index;

    const frame = _hmiReplayFrames[index];
    if (!device) {
        const equip = hmiGetCurrentEquipment();
        device = equip ? (equip.device || equip.name) : null;
    }
    if (device) {
        hmiReplayInjectTags(device, frame.tags);
        hmiUpdateAllElements();
    }

    // Update UI
    const scrubber = document.getElementById("replayScrubber");
    if (scrubber) scrubber.value = index;

    const tsLabel = document.getElementById("replayTimestamp");
    if (tsLabel) tsLabel.textContent = frame.timestamp.replace("T", " ").replace("Z", " UTC");

    hmiUpdateReplayBanner(frame.timestamp);
}

function hmiShowReplayBanner(timestamp) {
    let banner = document.getElementById("hmiReplayBanner");
    if (!banner) {
        banner = document.createElement("div");
        banner.id = "hmiReplayBanner";
        banner.className = "hmi-replay-banner";
        const container = document.getElementById("hmiCanvasContainer") || document.getElementById("hmiView");
        if (container) {
            container.style.position = "relative";
            container.insertBefore(banner, container.firstChild);
        } else {
            document.body.prepend(banner);
        }
    }
    const display = timestamp.replace("T", " ").replace("Z", " UTC");
    banner.innerHTML = `REPLAY MODE &mdash; ${display} <button class="replay-back-btn" onclick="hmiExitReplay()">Back to Live</button>`;
    banner.style.display = "flex";
}

function hmiUpdateReplayBanner(timestamp) {
    const banner = document.getElementById("hmiReplayBanner");
    if (!banner) return;
    const display = timestamp.replace("T", " ").replace("Z", " UTC");
    banner.innerHTML = `REPLAY MODE &mdash; ${display} <button class="replay-back-btn" onclick="hmiExitReplay()">Back to Live</button>`;
}

function hmiExitReplay() {
    _hmiReplayMode = false;
    _hmiReplayFrames = [];
    _hmiReplayIndex = 0;
    _hmiReplayPlaying = false;
    if (_hmiReplayInterval) { clearInterval(_hmiReplayInterval); _hmiReplayInterval = null; }

    // Remove banner
    const banner = document.getElementById("hmiReplayBanner");
    if (banner) banner.remove();

    // Hide replay bar
    const bar = document.getElementById("hmiReplayBar");
    if (bar) bar.classList.remove("active");

    // Remove toolbar highlight
    const btn = document.querySelector(".hmi-replay-toolbar-btn");
    if (btn) btn.classList.remove("replay-active");

    // Resume live updates
    hmiStartLiveUpdates();
}

// ── DVR Transport Controls ──

function hmiReplayTogglePlay() {
    if (_hmiReplayPlaying) {
        hmiReplayPause();
    } else {
        hmiReplayPlay();
    }
}

function hmiReplayPlay() {
    if (_hmiReplayFrames.length === 0) return;
    _hmiReplayPlaying = true;
    const btn = document.getElementById("replayPlayBtn");
    if (btn) btn.innerHTML = "&#x23F8;"; // pause icon

    // Calculate interval: base 2000ms (matching live 2s), divided by speed
    const intervalMs = Math.max(100, 2000 / _hmiReplaySpeed);

    if (_hmiReplayInterval) clearInterval(_hmiReplayInterval);
    _hmiReplayInterval = setInterval(() => {
        if (_hmiReplayIndex < _hmiReplayFrames.length - 1) {
            hmiReplayShowFrame(_hmiReplayIndex + 1);
        } else {
            hmiReplayPause(); // stop at end
        }
    }, intervalMs);
}

function hmiReplayPause() {
    _hmiReplayPlaying = false;
    if (_hmiReplayInterval) { clearInterval(_hmiReplayInterval); _hmiReplayInterval = null; }
    const btn = document.getElementById("replayPlayBtn");
    if (btn) btn.innerHTML = "&#x25B6;"; // play icon
}

function hmiReplayStepForward() {
    hmiReplayPause();
    if (_hmiReplayIndex < _hmiReplayFrames.length - 1) {
        hmiReplayShowFrame(_hmiReplayIndex + 1);
    }
}

function hmiReplayStepBack() {
    hmiReplayPause();
    if (_hmiReplayIndex > 0) {
        hmiReplayShowFrame(_hmiReplayIndex - 1);
    }
}

function hmiReplayChangeSpeed() {
    const speeds = [0.5, 1, 2, 4];
    const idx = speeds.indexOf(_hmiReplaySpeed);
    _hmiReplaySpeed = speeds[(idx + 1) % speeds.length];
    const label = document.getElementById("replaySpeedLabel");
    if (label) label.textContent = _hmiReplaySpeed + "x";

    // If currently playing, restart with new speed
    if (_hmiReplayPlaying) {
        hmiReplayPause();
        hmiReplayPlay();
    }
}

function hmiReplayScrub(value) {
    hmiReplayPause();
    hmiReplayShowFrame(parseInt(value, 10));
}
```

- [ ] **Step 2: Verify no JS syntax errors (manual — open HMI page, check browser console for parse errors)**

- [ ] **Step 3: Commit**

```bash
git add admin/static/js/hmi.js
git commit -m "feat(replay): implement DVR panel, transport controls, and frame injection"
```

---

## Task 7: Guard Live Updates Against Replay Mode

**Files:**
- Modify: `admin/static/js/hmi.js`

- [ ] **Step 1: Add replay guard to `hmiStartLiveUpdates()` at line 1712**

Modify `hmiStartLiveUpdates()` to skip if replay is active. Change line 1712 from:

```javascript
function hmiStartLiveUpdates() {
    hmiStopLiveUpdates();
```

to:

```javascript
function hmiStartLiveUpdates() {
    if (_hmiReplayMode) return; // Do not start live updates during replay
    hmiStopLiveUpdates();
```

- [ ] **Step 2: Add replay guard to `hmiRefreshLiveData()` at line 1718**

At the top of `hmiRefreshLiveData()`, add an early return:

```javascript
async function hmiRefreshLiveData() {
    if (_hmiReplayMode) return; // Skip live data during replay
    try {
```

- [ ] **Step 3: Verify — manual test**

1. Open HMI page in browser
2. Click "Replay" button in toolbar
3. Select a timestamp, click "Load"
4. Verify red banner appears with "REPLAY MODE"
5. Verify live data stops updating (tag values freeze to historical snapshot)
6. Click "Back to Live"
7. Verify live updates resume (tag values update every 2 seconds)

- [ ] **Step 4: Commit**

```bash
git add admin/static/js/hmi.js
git commit -m "fix(replay): guard live updates from running during replay mode"
```

---

## Task 8: Integration Testing and Edge Cases

**Files:** None (testing only)

- [ ] **Step 1: Test input validation on snapshot endpoint**

```bash
TOKEN=$(curl -s -X POST http://localhost:3080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | python -c "import sys,json; print(json.load(sys.stdin)['token'])")

# Bad device name (injection attempt)
curl -s -w "\n%{http_code}" "http://localhost:3080/api/replay/snapshot?device=foo%22%20|&timestamp=2026-03-30T12:00:00Z" \
  -H "Authorization: Bearer $TOKEN"
# Expected: 400

# Bad timestamp format
curl -s -w "\n%{http_code}" "http://localhost:3080/api/replay/snapshot?device=Demo-Simulated&timestamp=not-a-date" \
  -H "Authorization: Bearer $TOKEN"
# Expected: 400

# Missing parameters
curl -s -w "\n%{http_code}" "http://localhost:3080/api/replay/snapshot" \
  -H "Authorization: Bearer $TOKEN"
# Expected: 422 (FastAPI validation)
```

- [ ] **Step 2: Test input validation on range endpoint**

```bash
# Invalid step value
curl -s -w "\n%{http_code}" "http://localhost:3080/api/replay/range?device=Demo-Simulated&start=2026-03-30T11:00:00Z&end=2026-03-30T12:00:00Z&step=3s" \
  -H "Authorization: Bearer $TOKEN"
# Expected: 400

# end before start
curl -s -w "\n%{http_code}" "http://localhost:3080/api/replay/range?device=Demo-Simulated&start=2026-03-30T12:00:00Z&end=2026-03-30T11:00:00Z&step=30s" \
  -H "Authorization: Bearer $TOKEN"
# Expected: 400
```

- [ ] **Step 3: Test plant-based RBAC (if a monitor/operator user with limited plants exists)**

```bash
# Create a test with a user that has limited plant access
# This depends on your USERS_JSON configuration
# If the user does not have access to the device's plant, expect 403
```

- [ ] **Step 4: Test DVR playback end-to-end (manual browser test)**

1. Open HMI page, select an equipment with an active device
2. Click "Replay" toolbar button
3. Switch to "DVR" mode
4. Set start = 1 hour ago, end = now, step = 30s
5. Click "Load" — verify frames load (frame_count shown in transport bar)
6. Click Play — verify frames advance automatically, gauge/tank values change
7. Click Pause — verify playback stops
8. Drag scrubber — verify jumping to arbitrary frame
9. Click speed button — verify cycling through 0.5x, 1x, 2x, 4x
10. Click "Step forward" / "Step back" — verify single-frame advance
11. Click "Back to Live" — verify live updates resume
12. Verify banner disappears and toolbar button returns to normal state

- [ ] **Step 5: Test snapshot mode end-to-end (manual browser test)**

1. Open HMI page, select an equipment with an active device
2. Click "Replay" toolbar button (Snapshot mode is default)
3. Set timestamp to 30 minutes ago
4. Click "Load"
5. Verify red banner shows "REPLAY MODE — [timestamp]"
6. Verify HMI elements show historical values (may differ from live)
7. Click "Back to Live" — verify return to live updates

- [ ] **Step 6: Final commit (if any fixes were needed)**

```bash
git add -A
git commit -m "test(replay): verify integration and edge cases for HMI replay"
```

---

## Summary

| Task | Description | Files |
|------|-------------|-------|
| 1 | Snapshot endpoint | `admin/routes/replay_routes.py` (create) |
| 2 | Range endpoint | `admin/routes/replay_routes.py` (modify) |
| 3 | Register router | `admin/main.py` (modify) |
| 4 | Replay CSS | `admin/static/css/hmi.css` (modify) |
| 5 | Toolbar button | `admin/static/js/hmi.js` (modify) |
| 6 | DVR panel and controls | `admin/static/js/hmi.js` (modify) |
| 7 | Live update guards | `admin/static/js/hmi.js` (modify) |
| 8 | Integration testing | No file changes |

**Total new code:** ~150 lines Python (backend), ~300 lines JavaScript (frontend), ~170 lines CSS.
**New files:** 1 (`admin/routes/replay_routes.py`).
**Modified files:** 3 (`admin/main.py`, `admin/static/js/hmi.js`, `admin/static/css/hmi.css`).
