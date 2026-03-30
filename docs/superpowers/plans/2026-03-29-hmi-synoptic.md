# HMI / Synoptic View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a hierarchical HMI system (Plant → Area → Equipment → Synoptic Screen) with Konva.js canvas, 16 element types (8 read + 5 animated + 3 write), zoom/pan, drag-and-drop editor, live data updates, PLC write controls, fullscreen for TV monitors, and pre-configured demo screens.

**Architecture:** New "HMI" tab in the admin panel with breadcrumb navigation. HMI config stored as hierarchical JSON in the config volume. Konva.js provides 2D canvas with layers, drag/drop, resize, zoom/pan, and animation engine. Admin edits screens (drag elements, set properties). Operator navigates hierarchy and views live synoptics. Writes use existing `/api/live/write` with allowWrite + audit.

**Tech Stack:** Konva.js 9.x (150KB CDN), vanilla JS, existing Flask API, existing CSS variables.

---

## Element Types (16 total)

### Read-Only — Value Display

| # | Type | Visual | Animation |
|---|------|--------|-----------|
| 1 | **display** | Text box: label + value + unit, color by threshold | Blinks red border on critical |
| 2 | **gauge** | Semicircular arc with needle, color by threshold | Arc tweens to new value |
| 3 | **tank** | Rectangle with fill level based on value + min/max | Level animates up/down smoothly |
| 4 | **bargraph** | Vertical or horizontal bar, fills by percentage | Bar tweens to new size |
| 5 | **progressBar** | Horizontal bar with percentage text | Width tweens, color by threshold |

### Read-Only — Status Indicators

| # | Type | Visual | Animation |
|---|------|--------|-----------|
| 6 | **indicator** | Circle LED, green/red based on value (bool/number) | Glow pulse when on |
| 7 | **valve** | Bowtie shape (two triangles), rotates when open | 90° rotation tween on state change |
| 8 | **motor** | Circle with blades, spins when running | Continuous rotation when value != 0 |
| 9 | **image** | Custom image/icon, can swap image based on on/off state | Switches between imageOn/imageOff |
| 10 | **alarmBanner** | Red banner that appears when tag exceeds threshold | Slides in + blinks when active |

### Read-Only — Decorative/Layout

| # | Type | Visual | Animation |
|---|------|--------|-----------|
| 11 | **label** | Static text (title, section name, note) | None (no tag binding) |
| 12 | **pipe** | Dashed line connecting two points, shows flow direction | Dash offset animation when active |

### Write — Controls

| # | Type | Visual | Interaction |
|---|------|--------|------------|
| 13 | **button** | Clickable rectangle with label | Writes a fixed value to tag (confirm + allowWrite) |
| 14 | **switch** | Toggle ON/OFF slider | Writes 0 or 1 to tag (confirm + allowWrite) |
| 15 | **slider** | Horizontal bar with draggable handle | Writes continuous value (min-max) to tag (allowWrite) |
| 16 | **numericInput** | Text field where operator types a number | Writes typed value to tag (confirm + allowWrite) |

---

## Navigation Flow

```
Level 0: Plant List        Level 1: Areas           Level 2: Equipment       Level 3: Synoptic Screen
┌──────────┐               ┌──────────┐             ┌──────────┐             ┌──────────────────────┐
│ SP-HQ    │──click──►     │Utilities │──click──►   │Boiler 01 │──click──►  │ [Konva Canvas]       │
│ 3 areas  │               │ 3 equip  │             │ ● Online │             │ Background image +   │
│ ● Online │               │ ● Online │             └──────────┘             │ 16 element types     │
└──────────┘               └──────────┘                                      │ Live data + controls │
┌──────────┐               ┌──────────┐             ┌──────────┐             └──────────────────────┘
│ RJ-Plant │               │Production│             │ Pump 01  │
│ 2 areas  │               │ 5 equip  │             │ ⚠ Warn   │
└──────────┘               └──────────┘             └──────────┘

Breadcrumb: HMI > SP-HQ > Utilities > Boiler 01
```

---

## Config Structure

**File:** `/app/config/hmi-screens.json`

```json
{
  "plants": [
    {
      "id": "plant-sp",
      "name": "SP-HQ",
      "areas": [
        {
          "id": "area-util",
          "name": "Utilities",
          "equipment": [
            {
              "id": "equip-boiler1",
              "name": "Boiler 01",
              "device": "Demo-Simulated",
              "screen": {
                "backgroundImage": "",
                "width": 1200,
                "height": 700,
                "elements": [
                  {"id": "el-1", "type": "display", "x": 200, "y": 100, "width": 130, "height": 55, "tag": "RandomInteger", "label": "Temperature", "unit": "°C", "fontSize": 22, "thresholds": {"warning": 1000000000, "critical": 1500000000}},
                  {"id": "el-2", "type": "gauge", "x": 400, "y": 80, "width": 120, "height": 100, "tag": "RandomFloat", "label": "Pressure", "unit": "bar", "min": -1e20, "max": 1e20},
                  {"id": "el-3", "type": "tank", "x": 50, "y": 100, "width": 80, "height": 200, "tag": "RandomInteger", "min": 0, "max": 2147483647, "unit": "%", "fillColor": "#38bdf8"},
                  {"id": "el-4", "type": "indicator", "x": 600, "y": 120, "width": 30, "height": 30, "tag": "RandomBool", "colorOn": "#22c55e", "colorOff": "#ef4444", "labelOn": "Flame ON", "labelOff": "Flame OFF"},
                  {"id": "el-5", "type": "motor", "x": 700, "y": 200, "width": 60, "height": 60, "tag": "RandomBool", "colorOn": "#22c55e", "colorOff": "#6b7280", "label": "Fan"},
                  {"id": "el-6", "type": "valve", "x": 350, "y": 300, "width": 40, "height": 40, "tag": "RandomBool", "colorOpen": "#22c55e", "colorClosed": "#ef4444"},
                  {"id": "el-7", "type": "bargraph", "x": 850, "y": 100, "width": 40, "height": 180, "tag": "RandomInteger", "min": 0, "max": 2147483647, "fillColor": "#38bdf8", "orientation": "vertical"},
                  {"id": "el-8", "type": "progressBar", "x": 200, "y": 400, "width": 250, "height": 25, "tag": "RandomInteger", "min": 0, "max": 2147483647, "fillColor": "#22c55e"},
                  {"id": "el-9", "type": "alarmBanner", "x": 200, "y": 450, "width": 300, "height": 35, "tag": "RandomBool", "text": "BOILER OVERHEAT ALARM", "triggerValue": 1},
                  {"id": "el-10", "type": "label", "x": 50, "y": 30, "width": 300, "height": 40, "text": "Boiler 01 — Control Panel", "fontSize": 20, "color": "#1b1f24"},
                  {"id": "el-11", "type": "pipe", "x": 150, "y": 350, "width": 200, "height": 0, "points": [0,0, 200,0], "flowActive": "RandomBool", "color": "#38bdf8"},
                  {"id": "el-12", "type": "switch", "x": 600, "y": 400, "width": 70, "height": 35, "tag": "RandomBool", "label": "Burner", "writeValueOn": 1, "writeValueOff": 0}
                ]
              }
            }
          ]
        },
        {
          "id": "area-elec",
          "name": "Electrical",
          "equipment": [
            {
              "id": "equip-meter1",
              "name": "Energy Meter",
              "device": "Demo-Simulated",
              "screen": {
                "backgroundImage": "",
                "width": 1200,
                "height": 700,
                "elements": [
                  {"id": "el-20", "type": "display", "x": 100, "y": 100, "width": 130, "height": 55, "tag": "RandomInteger", "label": "Voltage L1", "unit": "V", "fontSize": 22},
                  {"id": "el-21", "type": "display", "x": 250, "y": 100, "width": 130, "height": 55, "tag": "RandomFloat", "label": "Current L1", "unit": "A", "fontSize": 22},
                  {"id": "el-22", "type": "gauge", "x": 500, "y": 80, "width": 140, "height": 120, "tag": "RandomFloat", "label": "Power Factor", "unit": "", "min": -1e20, "max": 1e20},
                  {"id": "el-23", "type": "indicator", "x": 700, "y": 120, "width": 30, "height": 30, "tag": "RandomBool", "colorOn": "#22c55e", "colorOff": "#ef4444", "labelOn": "Breaker ON", "labelOff": "Breaker OFF"},
                  {"id": "el-24", "type": "label", "x": 50, "y": 30, "width": 400, "height": 40, "text": "Energy Meter — Main Panel", "fontSize": 20, "color": "#1b1f24"},
                  {"id": "el-25", "type": "progressBar", "x": 100, "y": 250, "width": 300, "height": 25, "tag": "RandomInteger", "min": 0, "max": 2147483647, "fillColor": "#f59e0b", "label": "Load"},
                  {"id": "el-26", "type": "numericInput", "x": 500, "y": 300, "width": 150, "height": 40, "tag": "RandomInteger", "label": "Set Voltage", "unit": "V", "min": 0, "max": 500},
                  {"id": "el-27", "type": "slider", "x": 100, "y": 350, "width": 300, "height": 40, "tag": "RandomInteger", "label": "Dimmer", "min": 0, "max": 100, "unit": "%"}
                ]
              }
            }
          ]
        }
      ]
    }
  ]
}
```

---

## File Structure

| File | Responsibility |
|------|---------------|
| `admin/app.py` | API: HMI CRUD (plants, areas, equipment, screens), image upload, demo loader |
| `admin/static/js/hmi.js` | Navigation, Konva canvas, 16 element renderers, edit mode, live updates (~1200 lines) |
| `admin/static/css/hmi.css` | Navigation cards, breadcrumb, toolbar, properties panel, fullscreen |
| `admin/templates/index.html` | HMI tab, Konva.js CDN |
| `admin/hmi-demo.json` | Pre-configured demo screens |

---

## Task 1: Backend — HMI API Endpoints

**Files:** Modify `admin/app.py`

- [ ] **Step 1:** Add HMI config load/save helpers
- [ ] **Step 2:** Add full hierarchical CRUD: `GET /api/hmi/config`, `PUT /api/hmi/config` (save entire config), plus convenience endpoints for plants, areas, equipment
- [ ] **Step 3:** Add image upload: `POST /api/hmi/upload-image`
- [ ] **Step 4:** Add demo loader: `POST /api/hmi/load-demo` (loads hmi-demo.json)
- [ ] **Step 5:** Commit

---

## Task 2: Demo Screens

**Files:** Create `admin/hmi-demo.json`

- [ ] **Step 1:** Create demo JSON with:
  - Plant: "Demo-Plant"
  - Area "Utilities" → Equipment "Boiler 01" (12 elements: display, gauge, tank, indicator, motor, valve, bargraph, progressBar, alarmBanner, label, pipe, switch)
  - Area "Electrical" → Equipment "Energy Meter" (8 elements: 2x display, gauge, indicator, label, progressBar, numericInput, slider)
  - All linked to Demo-Simulated device tags
- [ ] **Step 2:** Commit

---

## Task 3: CSS Styles

**Files:** Create `admin/static/css/hmi.css`, Modify `admin/templates/index.html`

- [ ] **Step 1:** Navigation: `.hmi-breadcrumb`, `.hmi-grid`, `.hmi-nav-card` (with status indicator dot)
- [ ] **Step 2:** Canvas wrapper, toolbar, separator, add-element buttons
- [ ] **Step 3:** Properties panel sidebar (300px, scrollable, form groups)
- [ ] **Step 4:** Fullscreen overlay (fixed, z-500, black bg, ESC exit button)
- [ ] **Step 5:** Link CSS + Konva.js CDN in index.html
- [ ] **Step 6:** Commit

---

## Task 4: Tab + JS Skeleton + Navigation State

**Files:** Modify `admin/templates/index.html`, `admin/static/js/app.js`, Create `admin/static/js/hmi.js`

- [ ] **Step 1:** Add HMI tab button and content div in index.html
- [ ] **Step 2:** Add showTab hook in app.js: `if (name === "hmi") loadHMI();`
- [ ] **Step 3:** Create hmi.js with state, loadHMI(), navigation functions skeleton
- [ ] **Step 4:** Commit

---

## Task 5: Navigation — Plant/Area/Equipment Cards

**Files:** Modify `admin/static/js/hmi.js`

- [ ] **Step 1:** Plant cards: grid with name, area count, status dot (aggregated from live data)
- [ ] **Step 2:** Area cards: equipment count, status
- [ ] **Step 3:** Equipment cards: device name, status, tag count
- [ ] **Step 4:** Click handlers: plant → areas, area → equipment, equipment → screen
- [ ] **Step 5:** Breadcrumb: "HMI > Plant > Area > Equipment" with clickable back-links
- [ ] **Step 6:** Commit

---

## Task 6: Konva Canvas — Layers, Background, Zoom/Pan

**Files:** Modify `admin/static/js/hmi.js`

- [ ] **Step 1:** Init Konva.Stage with container dimensions
- [ ] **Step 2:** Create layers: backgroundLayer + elementsLayer + uiLayer (Transformer)
- [ ] **Step 3:** Load background image as Konva.Image
- [ ] **Step 4:** Zoom with mousewheel (scale toward pointer, clamp 0.3-3x)
- [ ] **Step 5:** Pan: stage.draggable in view mode
- [ ] **Step 6:** Scale to fit container on load + window resize
- [ ] **Step 7:** Commit

---

## Task 7: Elements — Display, Gauge, Tank, BarGraph, ProgressBar

**Files:** Modify `admin/static/js/hmi.js`

- [ ] **Step 1:** `display` — Konva.Group: dark Rect + label Text + value Text + unit Text. Border color by threshold. Blink animation on critical.
- [ ] **Step 2:** `gauge` — Konva.Group: gray Arc (background) + colored Arc (value %). Text center. Tween arc on value change.
- [ ] **Step 3:** `tank` — Konva.Group: outer Rect (border) + inner Rect (fill clipped to level). Tween height on value change. Color by threshold.
- [ ] **Step 4:** `bargraph` — Konva.Group: background Rect + fill Rect. Supports vertical/horizontal orientation. Tween size on value change.
- [ ] **Step 5:** `progressBar` — Konva.Group: background Rect + fill Rect + percentage Text. Width tweens. Color by threshold.
- [ ] **Step 6:** Commit

---

## Task 8: Elements — Indicator, Valve, Motor, Image, AlarmBanner

**Files:** Modify `admin/static/js/hmi.js`

- [ ] **Step 1:** `indicator` — Konva.Circle: fill colorOn/colorOff. ShadowBlur pulse tween when on. Optional label text.
- [ ] **Step 2:** `valve` — Konva.Group: two Line triangles (bowtie). Rotation tween 0↔90° on state change. Color by open/closed.
- [ ] **Step 3:** `motor` — Konva.Group: Circle + 4 Line blades. Continuous rotation Animation when value != 0. Color by running/stopped.
- [ ] **Step 4:** `image` — Konva.Image: loads imageUrl. Optionally swaps between imageOn/imageOff based on tag value.
- [ ] **Step 5:** `alarmBanner` — Konva.Group: red Rect + white Text. Visible only when tag matches triggerValue. Blink animation + slide-in.
- [ ] **Step 6:** Commit

---

## Task 9: Elements — Pipe, Label (decorative)

**Files:** Modify `admin/static/js/hmi.js`

- [ ] **Step 1:** `pipe` — Konva.Line with dash pattern. DashOffset animation for flow effect. Color by active tag.
- [ ] **Step 2:** `label` — Konva.Text: static text, font size, color. No tag binding. Used for titles, notes, section headers.
- [ ] **Step 3:** Commit

---

## Task 10: Elements — Button, Switch, Slider, NumericInput (write controls)

**Files:** Modify `admin/static/js/hmi.js`

- [ ] **Step 1:** `button` — Konva.Group: rounded Rect + Text. Click: check allowWrite, confirm(), POST /api/live/write. Scale tween on press. Disabled when !allowWrite.
- [ ] **Step 2:** `switch` — Konva.Group: pill Rect + circle handle. Click toggles between writeValueOn/writeValueOff. Tween handle position. Confirm + allowWrite check.
- [ ] **Step 3:** `slider` — Konva.Group: track Rect + handle Circle. Handle draggable horizontally between min-max. On drag end: confirm + write value. Shows current value text.
- [ ] **Step 4:** `numericInput` — Konva.Group: Rect border + Text (current value). Click opens browser prompt() to enter value. Confirm + write. Validates min/max.
- [ ] **Step 5:** All write elements: check device.allowWrite before enabling, show disabled appearance when !allowWrite, all writes logged to audit.
- [ ] **Step 6:** Commit

---

## Task 11: Edit Mode — Drag, Resize, Properties, Element CRUD

**Files:** Modify `admin/static/js/hmi.js`

- [ ] **Step 1:** Edit mode toggle: elements draggable, Transformer visible, toolbar shows, properties panel shows
- [ ] **Step 2:** Element selection: click → attach Transformer, populate properties panel
- [ ] **Step 3:** Drag/resize end: sync positions back to config, auto-save
- [ ] **Step 4:** Properties panel HTML form: type selector, tag dropdown (from device), label, unit, fontSize, thresholds, type-specific fields (min/max, colors, writeValue, orientation, triggerValue, imageUrl, points)
- [ ] **Step 5:** Add element toolbar: 16 buttons grouped by category (Display: display/gauge/tank/bargraph/progressBar, Status: indicator/valve/motor/image/alarmBanner, Layout: label/pipe, Control: button/switch/slider/numericInput)
- [ ] **Step 6:** Delete element button in properties panel
- [ ] **Step 7:** Upload background image in toolbar
- [ ] **Step 8:** Auto-save with 500ms debounce
- [ ] **Step 9:** Commit

---

## Task 12: Live Data Updates

**Files:** Modify `admin/static/js/hmi.js`

- [ ] **Step 1:** Polling loop: `/api/live/read` every 2s, build deviceMap
- [ ] **Step 2:** Update function: loop elements, find tag value, call type-specific updater with tweens/animations
- [ ] **Step 3:** Start/stop updates on screen enter/leave
- [ ] **Step 4:** Commit

---

## Task 13: Fullscreen + URL Parameters

**Files:** Modify `admin/static/js/hmi.js`

- [ ] **Step 1:** Fullscreen: move canvas to overlay, rescale, hide toolbar/properties. ESC to exit.
- [ ] **Step 2:** URL param `?hmi=equip-id`: auto-navigate to screen, enter fullscreen
- [ ] **Step 3:** Commit

---

## Task 14: CRUD Modals for Plants, Areas, Equipment

**Files:** Modify `admin/static/js/hmi.js`, `admin/templates/index.html`

- [ ] **Step 1:** Add Plant modal: name input
- [ ] **Step 2:** Add Area modal: name input (under selected plant)
- [ ] **Step 3:** Add Equipment modal: name + device dropdown (from config devices)
- [ ] **Step 4:** Edit/delete buttons on each navigation card
- [ ] **Step 5:** Commit

---

## Task 15: Documentation

**Files:** Modify `admin/templates/index.html`, `docs/INSTALLATION.md`, `admin/static/swagger.json`, `README.md`

- [ ] **Step 1:** Add HMI to Getting Started
- [ ] **Step 2:** INSTALLATION.md: HMI section (hierarchy, 16 element types, fullscreen for TV, URL param)
- [ ] **Step 3:** Add HMI endpoints to swagger.json
- [ ] **Step 4:** Update README features
- [ ] **Step 5:** Commit

---

## Summary

| Task | What | Est. lines |
|------|------|-----------|
| 1 | Backend API (CRUD + upload + demo loader) | 150 |
| 2 | Demo screens JSON (2 areas, 2 equipment, 20 elements) | 150 |
| 3 | CSS styles | 200 |
| 4 | Tab + JS skeleton + navigation state | 120 |
| 5 | Navigation cards plant → area → equipment | 150 |
| 6 | Konva canvas: layers, background, zoom/pan | 100 |
| 7 | Elements: display, gauge, tank, bargraph, progressBar | 200 |
| 8 | Elements: indicator, valve, motor, image, alarmBanner | 200 |
| 9 | Elements: pipe, label | 60 |
| 10 | Elements: button, switch, slider, numericInput | 200 |
| 11 | Edit mode: drag, resize, properties, CRUD, auto-save | 300 |
| 12 | Live data polling + updates | 80 |
| 13 | Fullscreen + URL params | 60 |
| 14 | CRUD modals for plants/areas/equipment | 120 |
| 15 | Documentation | 60 |
| **Total** | **15 tasks, ~20 commits** | **~2150 lines** |

### Pre-configured demos

| Plant | Area | Equipment | Elements (count) |
|-------|------|-----------|-----------------|
| Demo-Plant | Utilities | Boiler 01 | display, gauge, tank, indicator, motor, valve, bargraph, progressBar, alarmBanner, label, pipe, switch (**12**) |
| Demo-Plant | Electrical | Energy Meter | 2x display, gauge, indicator, label, progressBar, numericInput, slider (**8**) |
| **Total** | | | **20 elements covering all 16 types** |
