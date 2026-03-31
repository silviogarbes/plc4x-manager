/**
 * PLC4X Manager - HMI / Synoptic View
 * Hierarchical navigation (Plant → Area → Equipment → Screen)
 * Konva.js canvas with 16 element types, drag/drop, zoom/pan, live data
 */

// =============================================
// State
// =============================================

let _hmiConfig = { plants: [] };
let _hmiCurrentLevel = "plants";  // "plants" | "areas" | "equipment" | "screen"
let _hmiCurrentPlant = null;
let _hmiCurrentArea = null;
let _hmiCurrentEquipment = null;
let _hmiEditMode = false;
let _hmiStage = null;              // Konva.Stage
let _hmiBackgroundLayer = null;    // Konva.Layer
let _hmiElementsLayer = null;      // Konva.Layer
let _hmiUiLayer = null;            // Konva.Layer (transformer)
let _hmiTransformer = null;        // Konva.Transformer
let _hmiSelectedElement = null;    // currently selected element config
let _hmiSelectedNode = null;       // currently selected Konva node
let _hmiRefreshInterval = null;
let _hmiDeviceMap = {};            // live data: {deviceName: {tags: {alias: tagData}, allowWrite: bool}}
let _hmiActiveAlarms = {};         // alarm state: {device/tag: alarmObj}
let _hmiSaveTimeout = null;

// ── Replay / Time Travel state ──
let _hmiReplayMode = false;
let _hmiReplayFrames = [];
let _hmiReplayIndex = 0;
let _hmiReplayInterval = null;
let _hmiReplaySpeed = 1;
let _hmiReplayPlaying = false;

// =============================================
// Init & Data Loading
// =============================================

async function loadHMI() {
    try {
        _hmiConfig = await api("/api/hmi/config");
        if (!_hmiConfig.plants) _hmiConfig.plants = [];
        hmiRender();
    } catch (e) {
        document.getElementById("hmiContent").innerHTML =
            '<p class="text-muted" style="text-align:center;padding:32px">Failed to load HMI config</p>';
    }
}

async function hmiLoadDemo() {
    if (!confirm("Load demo HMI screens? This will add a Demo-Plant with example equipment.")) return;
    try {
        await api("/api/hmi/load-demo", "POST");
        toast("Demo screens loaded");
        loadHMI();
    } catch (e) {
        toast(e.message, "error");
    }
}

// =============================================
// Navigation Rendering
// =============================================

function hmiRender() {
    switch (_hmiCurrentLevel) {
        case "plants": hmiShowPlants(); break;
        case "areas": hmiShowAreas(); break;
        case "equipment": hmiShowEquipment(); break;
        case "screen": hmiShowScreen(); break;
    }
}

async function hmiShowPlants() {
    _hmiEditMode = false;
    _hmiCurrentLevel = "plants";
    _hmiCurrentPlant = null;
    _hmiCurrentArea = null;
    _hmiCurrentEquipment = null;
    hmiStopLiveUpdates();
    hmiUpdateButtons();

    // Fetch live data for status indicators
    await hmiFetchLiveStatus();

    const container = document.getElementById("hmiContent");
    if (_hmiConfig.plants.length === 0) {
        container.innerHTML = '<p class="text-muted" style="text-align:center;padding:32px">No HMI screens configured. Click <strong>+ Plant</strong> or <strong>Load Demo</strong> to get started.</p>';
        return;
    }

    let html = hmiRenderBreadcrumb();
    html += '<div class="hmi-grid">';
    for (const plant of _hmiConfig.plants) {
        const areaCount = (plant.areas || []).length;
        const status = hmiGetPlantStatus(plant);
        html += `<div class="hmi-nav-card" onclick="hmiNavigateTo('areas','${escAttr(plant.id)}')">
            <div style="display:flex;justify-content:space-between;align-items:start">
                <h4>${escHtml(plant.name)}</h4>
                <span style="display:flex;gap:4px">
                    <button class="btn btn-outline btn-sm" style="font-size:0.65rem;padding:2px 6px" onclick="event.stopPropagation();hmiRenamePlant('${escAttr(plant.id)}')" title="Rename">&#9998;</button>
                    <button class="btn btn-outline btn-sm" style="font-size:0.65rem;padding:2px 6px" onclick="event.stopPropagation();hmiDeletePlant('${escAttr(plant.id)}')" title="Delete">&times;</button>
                </span>
            </div>
            <div class="meta">${areaCount} area${areaCount !== 1 ? 's' : ''}</div>
            <div class="meta"><span class="status-dot ${status.cls}"></span>${status.label}</div>
        </div>`;
    }
    html += '</div>';
    container.innerHTML = html;
}

async function hmiShowAreas() {
    _hmiEditMode = false;
    _hmiCurrentLevel = "areas";
    _hmiCurrentArea = null;
    _hmiCurrentEquipment = null;
    hmiStopLiveUpdates();
    hmiUpdateButtons();

    await hmiFetchLiveStatus();

    const plant = _hmiConfig.plants.find(p => p.id === _hmiCurrentPlant);
    if (!plant) { hmiShowPlants(); return; }

    const container = document.getElementById("hmiContent");
    let html = hmiRenderBreadcrumb();
    const areas = plant.areas || [];
    if (areas.length === 0) {
        html += '<p class="text-muted">No areas. Add one with the + button.</p>';
    } else {
        html += '<div class="hmi-grid">';
        for (const area of areas) {
            const equipCount = (area.equipment || []).length;
            const status = hmiGetAreaStatus(area);
            html += `<div class="hmi-nav-card" onclick="hmiNavigateTo('equipment','${escAttr(area.id)}')">
                <div style="display:flex;justify-content:space-between;align-items:start">
                    <h4>${escHtml(area.name)}</h4>
                    <span style="display:flex;gap:4px">
                        <button class="btn btn-outline btn-sm" style="font-size:0.65rem;padding:2px 6px" onclick="event.stopPropagation();hmiRenameArea('${escAttr(area.id)}')" title="Rename">&#9998;</button>
                        <button class="btn btn-outline btn-sm" style="font-size:0.65rem;padding:2px 6px" onclick="event.stopPropagation();hmiDeleteArea('${escAttr(area.id)}')" title="Delete">&times;</button>
                    </span>
                </div>
                <div class="meta">${equipCount} equipment</div>
                <div class="meta"><span class="status-dot ${status.cls}"></span>${status.label}</div>
            </div>`;
        }
        html += '</div>';
    }
    container.innerHTML = html;
}

async function hmiShowEquipment() {
    _hmiEditMode = false;
    _hmiCurrentLevel = "equipment";
    _hmiCurrentEquipment = null;
    hmiStopLiveUpdates();
    hmiUpdateButtons();

    await hmiFetchLiveStatus();

    const plant = _hmiConfig.plants.find(p => p.id === _hmiCurrentPlant);
    if (!plant) { hmiShowPlants(); return; }
    const area = (plant.areas || []).find(a => a.id === _hmiCurrentArea);
    if (!area) { hmiShowAreas(); return; }

    const container = document.getElementById("hmiContent");
    let html = hmiRenderBreadcrumb();
    const equips = area.equipment || [];
    if (equips.length === 0) {
        html += '<p class="text-muted">No equipment. Add one with the + button.</p>';
    } else {
        html += '<div class="hmi-grid">';
        for (const eq of equips) {
            const status = hmiGetEquipmentStatus(eq);
            html += `<div class="hmi-nav-card" onclick="hmiNavigateTo('screen','${escAttr(eq.id)}')">
                <div style="display:flex;justify-content:space-between;align-items:start">
                    <h4>${escHtml(eq.name)}</h4>
                    <span style="display:flex;gap:4px">
                        <button class="btn btn-outline btn-sm" style="font-size:0.65rem;padding:2px 6px" onclick="event.stopPropagation();hmiRenameEquipment('${escAttr(eq.id)}')" title="Rename">&#9998;</button>
                        <button class="btn btn-outline btn-sm" style="font-size:0.65rem;padding:2px 6px" onclick="event.stopPropagation();hmiDeleteEquipment('${escAttr(eq.id)}')" title="Delete">&times;</button>
                    </span>
                </div>
                <div class="meta">Device: ${escHtml(eq.device || 'none')}</div>
                <div class="meta"><span class="status-dot ${status.cls}"></span>${status.label}</div>
            </div>`;
        }
        html += '</div>';
    }
    container.innerHTML = html;
}

function hmiShowScreen() {
    _hmiCurrentLevel = "screen";
    hmiUpdateButtons();
    const container = document.getElementById("hmiContent");
    container.innerHTML = hmiRenderBreadcrumb() +
        '<div id="hmiToolbar" class="hmi-toolbar" style="display:none"></div>' +
        '<div class="hmi-canvas-wrapper">' +
        '  <div class="hmi-canvas-container" id="hmiCanvasContainer"></div>' +
        '  <div class="hmi-properties" id="hmiProperties" style="display:none"></div>' +
        '</div>';
    const equip = hmiGetCurrentEquipment();
    if (equip && equip.screen) {
        hmiInitCanvas(equip.screen);
    }
}

// =============================================
// Navigation Helpers
// =============================================

function hmiNavigateTo(level, id) {
    switch (level) {
        case "areas":
            _hmiCurrentPlant = id;
            _hmiCurrentLevel = "areas";
            break;
        case "equipment":
            _hmiCurrentArea = id;
            _hmiCurrentLevel = "equipment";
            break;
        case "screen":
            _hmiCurrentEquipment = id;
            _hmiCurrentLevel = "screen";
            break;
    }
    hmiRender();
}

function hmiRenderBreadcrumb() {
    let html = '<div class="hmi-breadcrumb">';
    html += '<a href="#" onclick="hmiShowPlants();return false">HMI</a>';

    if (_hmiCurrentPlant) {
        const plant = _hmiConfig.plants.find(p => p.id === _hmiCurrentPlant);
        if (plant) {
            html += '<span class="separator"> &gt; </span>';
            html += `<a href="#" onclick="hmiNavigateTo('areas','${escAttr(plant.id)}');return false">${escHtml(plant.name)}</a>`;
        }
    }
    if (_hmiCurrentArea) {
        const plant = _hmiConfig.plants.find(p => p.id === _hmiCurrentPlant);
        const area = plant ? (plant.areas || []).find(a => a.id === _hmiCurrentArea) : null;
        if (area) {
            html += '<span class="separator"> &gt; </span>';
            html += `<a href="#" onclick="hmiNavigateTo('equipment','${escAttr(area.id)}');return false">${escHtml(area.name)}</a>`;
        }
    }
    if (_hmiCurrentEquipment) {
        const plant = _hmiConfig.plants.find(p => p.id === _hmiCurrentPlant);
        const area = plant ? (plant.areas || []).find(a => a.id === _hmiCurrentArea) : null;
        const equip = area ? (area.equipment || []).find(e => e.id === _hmiCurrentEquipment) : null;
        if (equip) {
            html += '<span class="separator"> &gt; </span>';
            html += `<span>${escHtml(equip.name)}</span>`;
        }
    }

    html += '</div>';
    return html;
}

// =============================================
// Status Indicators for Navigation
// =============================================

async function hmiFetchLiveStatus() {
    try {
        const data = await api("/api/live/read");
        _hmiDeviceMap = {};
        for (const dev of data.devices || []) {
            _hmiDeviceMap[dev.name] = {
                tags: {},
                allowWrite: dev.allowWrite,
                status: dev.status
            };
            for (const tag of dev.tags || []) {
                _hmiDeviceMap[dev.name].tags[tag.alias] = tag;
            }
        }
    } catch {}
}

function hmiGetEquipmentStatus(equip) {
    const dev = _hmiDeviceMap[equip.device];
    if (!dev) return { cls: "offline", label: "Offline" };
    if (dev.status === "online") return { cls: "online", label: "Online" };
    if (dev.status === "error") return { cls: "error", label: "Error" };
    if (dev.status === "disabled") return { cls: "offline", label: "Disabled" };
    return { cls: "offline", label: dev.status || "Unknown" };
}

function hmiGetAreaStatus(area) {
    const equips = area.equipment || [];
    if (equips.length === 0) return { cls: "offline", label: "No equipment" };
    let online = 0, errors = 0;
    for (const eq of equips) {
        const s = hmiGetEquipmentStatus(eq);
        if (s.cls === "online") online++;
        else if (s.cls === "error") errors++;
    }
    if (errors > 0) return { cls: "error", label: `${errors} error${errors > 1 ? "s" : ""}` };
    if (online === equips.length) return { cls: "online", label: "All online" };
    if (online > 0) return { cls: "online", label: `${online}/${equips.length} online` };
    return { cls: "offline", label: "Offline" };
}

function hmiGetPlantStatus(plant) {
    const areas = plant.areas || [];
    if (areas.length === 0) return { cls: "offline", label: "No areas" };
    let allOnline = true, hasError = false;
    for (const area of areas) {
        const s = hmiGetAreaStatus(area);
        if (s.cls === "error") hasError = true;
        if (s.cls !== "online") allOnline = false;
    }
    if (hasError) return { cls: "error", label: "Has errors" };
    if (allOnline) return { cls: "online", label: "All online" };
    return { cls: "online", label: "Partial" };
}

function hmiUpdateButtons() {
    const editBtn = document.getElementById("hmiEditBtn");
    const fsBtn = document.getElementById("hmiFullscreenBtn");
    const addBtn = document.getElementById("hmiAddBtn");
    const demoBtn = document.getElementById("hmiLoadDemoBtn");

    if (editBtn) editBtn.style.display = _hmiCurrentLevel === "screen" ? "" : "none";
    if (fsBtn) fsBtn.style.display = _hmiCurrentLevel === "screen" ? "" : "none";
    if (demoBtn) demoBtn.style.display = _hmiCurrentLevel === "plants" ? "" : "none";

    if (addBtn) {
        switch (_hmiCurrentLevel) {
            case "plants":
                addBtn.textContent = "+ Plant";
                addBtn.onclick = hmiCreatePlant;
                addBtn.style.display = "";
                break;
            case "areas":
                addBtn.textContent = "+ Area";
                addBtn.onclick = hmiCreateArea;
                addBtn.style.display = "";
                break;
            case "equipment":
                addBtn.textContent = "+ Equipment";
                addBtn.onclick = hmiCreateEquipment;
                addBtn.style.display = "";
                break;
            case "screen":
                addBtn.style.display = "none";
                break;
        }
    }
}

// =============================================
// CRUD — Create Plant/Area/Equipment
// =============================================

function hmiShowModal(title, fields, onSave) {
    // Prevent duplicate modals
    document.querySelectorAll(".modal-overlay.hmi-modal").forEach(el => el.remove());
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay hmi-modal";
    overlay.style.display = "flex";
    let fieldsHtml = "";
    for (const f of fields) {
        if (f.type === "select") {
            const opts = f.options.map(o => `<option value="${escAttr(o.value)}"${o.selected ? " selected" : ""}>${escHtml(o.label)}</option>`).join("");
            fieldsHtml += `<div class="form-group"><label>${escHtml(f.label)}</label><select id="${f.id}">${opts}</select></div>`;
        } else {
            fieldsHtml += `<div class="form-group"><label>${escHtml(f.label)}</label><input type="text" id="${f.id}" value="${escAttr(f.value || "")}" placeholder="${escAttr(f.placeholder || "")}"></div>`;
        }
    }
    overlay.innerHTML = `<div class="modal" style="max-width:420px">
        <h3>${escHtml(title)}</h3>
        ${fieldsHtml}
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
            <button class="btn btn-outline hmi-modal-cancel">Cancel</button>
            <button class="btn btn-primary hmi-modal-save">OK</button>
        </div>
    </div>`;
    document.body.appendChild(overlay);
    const firstInput = overlay.querySelector("input");
    if (firstInput) { firstInput.focus(); firstInput.select(); }
    const close = () => overlay.remove();
    overlay.querySelector(".hmi-modal-cancel").onclick = close;
    overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
    overlay.addEventListener("keydown", e => { if (e.key === "Escape") close(); });
    overlay.querySelector(".hmi-modal-save").onclick = () => onSave(overlay, close);
    if (firstInput) firstInput.addEventListener("keydown", e => { if (e.key === "Enter") onSave(overlay, close); });
}

async function hmiCreatePlant() {
    hmiShowModal("Add Plant", [
        { id: "hmiFieldName", label: "Plant Name", placeholder: "Main Plant" }
    ], async (ov, close) => {
        const name = ov.querySelector("#hmiFieldName").value.trim();
        if (!name) { toast("Name is required", "error"); return; }
        try {
            await api("/api/hmi/plants", "POST", { name });
            toast("Plant created"); close();
            loadHMI();
        } catch (e) { toast(e.message, "error"); }
    });
}

async function hmiCreateArea() {
    if (!_hmiCurrentPlant) return;
    hmiShowModal("Add Area", [
        { id: "hmiFieldName", label: "Area Name", placeholder: "Utilities" }
    ], async (ov, close) => {
        const name = ov.querySelector("#hmiFieldName").value.trim();
        if (!name) { toast("Name is required", "error"); return; }
        try {
            await api(`/api/hmi/plants/${_hmiCurrentPlant}/areas`, "POST", { name });
            toast("Area created"); close();
            loadHMI().then(() => { _hmiCurrentLevel = "areas"; hmiRender(); });
        } catch (e) { toast(e.message, "error"); }
    });
}

async function hmiCreateEquipment() {
    if (!_hmiCurrentArea) return;
    let devices = [];
    try { devices = await api("/api/devices"); } catch {}
    if (devices.length === 0) {
        toast("No devices configured. Add a device in the Devices tab first.", "error");
        return;
    }
    const options = devices.map(d => ({ value: d.name, label: `${d.name} — ${d.connectionString || ''}` }));
    hmiShowModal("Add Equipment", [
        { id: "hmiFieldName", label: "Equipment Name", placeholder: "Boiler 01" },
        { id: "hmiFieldDevice", label: "Device", type: "select", options }
    ], async (ov, close) => {
        const name = ov.querySelector("#hmiFieldName").value.trim();
        const device = ov.querySelector("#hmiFieldDevice").value;
        if (!name) { toast("Name is required", "error"); return; }
        try {
            await api(`/api/hmi/areas/${_hmiCurrentArea}/equipment`, "POST", { name, device });
            toast("Equipment created"); close();
            loadHMI().then(() => { _hmiCurrentLevel = "equipment"; hmiRender(); });
        } catch (e) { toast(e.message, "error"); }
    });
}

async function hmiDeletePlant(id) {
    if (!confirm("Delete this plant and all its areas/equipment?")) return;
    try {
        await api(`/api/hmi/plants/${id}`, "DELETE");
        toast("Plant deleted");
        loadHMI();
    } catch (e) { toast(e.message, "error"); }
}

async function hmiDeleteArea(id) {
    if (!confirm("Delete this area and all its equipment?")) return;
    try {
        await api(`/api/hmi/areas/${id}`, "DELETE");
        toast("Area deleted");
        loadHMI().then(() => { _hmiCurrentLevel = "areas"; hmiRender(); });
    } catch (e) { toast(e.message, "error"); }
}

async function hmiDeleteEquipment(id) {
    if (!confirm("Delete this equipment and its screen?")) return;
    try {
        await api(`/api/hmi/equipment/${id}`, "DELETE");
        toast("Equipment deleted");
        loadHMI().then(() => { _hmiCurrentLevel = "equipment"; hmiRender(); });
    } catch (e) { toast(e.message, "error"); }
}

async function hmiRenamePlant(id) {
    const plant = _hmiConfig.plants.find(p => p.id === id);
    if (!plant) return;
    hmiShowModal("Edit Plant", [
        { id: "hmiFieldName", label: "Plant Name", value: plant.name }
    ], async (ov, close) => {
        const name = ov.querySelector("#hmiFieldName").value.trim();
        if (!name || name === plant.name) { close(); return; }
        try {
            await api(`/api/hmi/plants/${id}`, "PUT", { name });
            toast("Plant renamed"); close();
            loadHMI();
        } catch (e) { toast(e.message, "error"); }
    });
}

async function hmiRenameArea(id) {
    const plant = _hmiConfig.plants.find(p => p.id === _hmiCurrentPlant);
    if (!plant) return;
    const area = (plant.areas || []).find(a => a.id === id);
    if (!area) return;
    hmiShowModal("Edit Area", [
        { id: "hmiFieldName", label: "Area Name", value: area.name }
    ], async (ov, close) => {
        const name = ov.querySelector("#hmiFieldName").value.trim();
        if (!name || name === area.name) { close(); return; }
        try {
            await api(`/api/hmi/areas/${id}`, "PUT", { name });
            toast("Area renamed"); close();
            loadHMI().then(() => { _hmiCurrentLevel = "areas"; hmiRender(); });
        } catch (e) { toast(e.message, "error"); }
    });
}

async function hmiRenameEquipment(id) {
    const plant = _hmiConfig.plants.find(p => p.id === _hmiCurrentPlant);
    if (!plant) return;
    const area = (plant.areas || []).find(a => a.id === _hmiCurrentArea);
    if (!area) return;
    const eq = (area.equipment || []).find(e => e.id === id);
    if (!eq) return;
    let devices = [];
    try { devices = await api("/api/devices"); } catch {}
    const options = devices.map(d => ({ value: d.name, label: `${d.name} — ${d.connectionString || ''}`, selected: d.name === eq.device }));
    hmiShowModal("Edit Equipment", [
        { id: "hmiFieldName", label: "Equipment Name", value: eq.name },
        { id: "hmiFieldDevice", label: "Device", type: "select", options }
    ], async (ov, close) => {
        const name = ov.querySelector("#hmiFieldName").value.trim();
        const device = ov.querySelector("#hmiFieldDevice").value;
        if (!name) { toast("Name is required", "error"); return; }
        const body = {};
        if (name !== eq.name) body.name = name;
        if (device !== eq.device) body.device = device;
        if (Object.keys(body).length === 0) { close(); return; }
        try {
            await api(`/api/hmi/equipment/${id}`, "PUT", body);
            toast("Equipment updated"); close();
            loadHMI().then(() => { _hmiCurrentLevel = "equipment"; hmiRender(); });
        } catch (e) { toast(e.message, "error"); }
    });
}

// =============================================
// Edit Mode (Task 11)
// =============================================

function hmiToggleEdit() {
    _hmiEditMode = !_hmiEditMode;
    const btn = document.getElementById("hmiEditBtn");
    if (btn) btn.textContent = _hmiEditMode ? "View Mode" : "Edit Mode";

    const toolbar = document.getElementById("hmiToolbar");
    const props = document.getElementById("hmiProperties");
    const canvasContainer = document.getElementById("hmiCanvasContainer");

    if (toolbar) toolbar.style.display = _hmiEditMode ? "flex" : "none";
    if (props && !_hmiSelectedElement) props.style.display = "none";
    if (canvasContainer) canvasContainer.classList.toggle("edit-mode", _hmiEditMode);

    // Toggle stage dragging (pan only in view mode)
    if (_hmiStage) _hmiStage.draggable(!_hmiEditMode);

    // Toggle element dragging
    if (_hmiElementsLayer) {
        _hmiElementsLayer.children.forEach(node => {
            node.draggable(_hmiEditMode);
        });
    }

    // Render toolbar
    if (_hmiEditMode) hmiRenderToolbar();

    hmiDeselectElement();
}

function hmiRenderToolbar() {
    const toolbar = document.getElementById("hmiToolbar");
    if (!toolbar) return;

    const groups = [
        { label: "Display", types: ["display", "gauge", "tank", "bargraph", "progressBar"] },
        { label: "Status", types: ["indicator", "valve", "motor", "image", "alarmBanner"] },
        { label: "Layout", types: ["label", "pipe"] },
        { label: "Control", types: ["button", "switch", "slider", "numericInput"] },
    ];

    let html = '<label class="btn btn-outline btn-sm" style="cursor:pointer;margin-right:8px">Upload BG <input type="file" accept=".png,.jpg,.jpeg,.svg,.gif,.webp" style="display:none" onchange="hmiUploadBackground(this)"></label>';

    for (const g of groups) {
        html += '<div class="hmi-toolbar-separator"></div>';
        html += '<div class="hmi-toolbar-group">';
        html += `<span class="hmi-toolbar-group-label">${g.label}</span>`;
        for (const t of g.types) {
            html += `<button class="hmi-add-btn" onclick="hmiAddElement('${t}')">+ ${t}</button>`;
        }
        html += '</div>';
    }
    toolbar.innerHTML = html;
}

async function hmiUploadBackground(input) {
    const file = input.files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append("file", file);
    try {
        const token = sessionStorage.getItem("jwt_token");
        const res = await fetch("/api/hmi/upload-image", {
            method: "POST",
            headers: { "Authorization": "Bearer " + token },
            body: formData
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error);
        const equip = hmiGetCurrentEquipment();
        if (equip && equip.screen) {
            equip.screen.backgroundImage = data.url;
            hmiSetBackground(data.url, equip.screen.width, equip.screen.height);
            hmiAutoSave();
            toast("Background uploaded");
        }
    } catch (e) { toast(e.message, "error"); }
    input.value = "";
}

function hmiAddElement(type) {
    const equip = hmiGetCurrentEquipment();
    if (!equip || !equip.screen) return;

    const id = "el-" + Date.now();
    const defaults = {
        display: { width: 130, height: 55, label: "Value", unit: "", fontSize: 22, tag: "" },
        gauge: { width: 120, height: 100, label: "Gauge", unit: "", min: 0, max: 100, tag: "" },
        tank: { width: 80, height: 200, min: 0, max: 100, unit: "%", fillColor: "#38bdf8", tag: "" },
        bargraph: { width: 40, height: 180, min: 0, max: 100, fillColor: "#38bdf8", orientation: "vertical", tag: "" },
        progressBar: { width: 250, height: 25, min: 0, max: 100, fillColor: "#22c55e", tag: "" },
        indicator: { width: 30, height: 30, colorOn: "#22c55e", colorOff: "#ef4444", tag: "" },
        valve: { width: 40, height: 40, colorOpen: "#22c55e", colorClosed: "#ef4444", label: "Valve", writeValueOpen: 1, writeValueClose: 0, tag: "" },
        motor: { width: 60, height: 60, colorOn: "#22c55e", colorOff: "#6b7280", label: "Motor", writeValueOn: 1, writeValueOff: 0, tag: "" },
        image: { width: 60, height: 60, imageUrl: "", tag: "" },
        alarmBanner: { width: 300, height: 35, text: "ALARM", triggerValue: 1, tag: "" },
        label: { width: 200, height: 30, text: "Label", fontSize: 18, color: "#ffffff" },
        pipe: { width: 200, height: 0, points: [0,0,200,0], color: "#38bdf8", flowActive: "" },
        button: { width: 110, height: 40, label: "Action", writeValue: 1, confirmMessage: "Execute?", tag: "" },
        switch: { width: 70, height: 35, label: "Switch", writeValueOn: 1, writeValueOff: 0, tag: "" },
        slider: { width: 300, height: 40, label: "Slider", min: 0, max: 100, unit: "%", tag: "" },
        numericInput: { width: 150, height: 40, label: "Input", unit: "", min: 0, max: 1000, tag: "" },
    };

    // Place at center of visible canvas area
    const stageScale = _hmiStage ? _hmiStage.scaleX() : 1;
    const stagePos = _hmiStage ? _hmiStage.position() : {x:0, y:0};
    const centerX = (-stagePos.x + (_hmiStage ? _hmiStage.width() / 2 : 300)) / stageScale;
    const centerY = (-stagePos.y + (_hmiStage ? _hmiStage.height() / 2 : 200)) / stageScale;

    const el = { id, type, x: Math.round(centerX), y: Math.round(centerY), ...(defaults[type] || { width: 100, height: 50 }) };
    equip.screen.elements.push(el);

    // Create Konva node
    const node = hmiCreateElement(el);
    if (node && _hmiElementsLayer) {
        _hmiElementsLayer.add(node);
        _hmiElementsLayer.draw();
        hmiSelectElement(el, node);
    }
    hmiAutoSave();
}

function hmiDeleteSelectedElement() {
    if (!_hmiSelectedElement || !_hmiSelectedNode) return;
    if (!confirm("Delete this element?")) return;
    const equip = hmiGetCurrentEquipment();
    if (equip && equip.screen) {
        equip.screen.elements = equip.screen.elements.filter(e => e.id !== _hmiSelectedElement.id);
    }
    _hmiSelectedNode.destroy();
    hmiDeselectElement();
    _hmiElementsLayer.draw();
    hmiAutoSave();
}

// =============================================
// Konva Canvas
// =============================================

function hmiGetCurrentEquipment() {
    if (!_hmiCurrentPlant || !_hmiCurrentArea || !_hmiCurrentEquipment) return null;
    const plant = _hmiConfig.plants.find(p => p.id === _hmiCurrentPlant);
    if (!plant) return null;
    const area = (plant.areas || []).find(a => a.id === _hmiCurrentArea);
    if (!area) return null;
    return (area.equipment || []).find(e => e.id === _hmiCurrentEquipment);
}

function hmiGetElementAlarm(el) {
    const deviceName = hmiGetElementDeviceName(el);
    if (!deviceName || !el.tag) return null;
    return _hmiActiveAlarms[`${deviceName}/${el.tag}`] || null;
}

function hmiGetElementDevice(el) {
    const equip = hmiGetCurrentEquipment();
    const deviceName = el.device || (equip ? equip.device : null);
    return deviceName ? _hmiDeviceMap[deviceName] : null;
}

function hmiGetElementDeviceName(el) {
    const equip = hmiGetCurrentEquipment();
    return el.device || (equip ? equip.device : null);
}

function hmiInitCanvas(screen) {
    const container = document.getElementById("hmiCanvasContainer");
    if (!container || !screen) return;

    // Destroy previous stage
    if (_hmiStage) { _hmiStage.destroy(); _hmiStage = null; }

    // Calculate scale to fit container width
    const containerWidth = container.offsetWidth || 1000;
    const scale = containerWidth / screen.width;

    _hmiStage = new Konva.Stage({
        container: "hmiCanvasContainer",
        width: containerWidth,
        height: screen.height * scale,
    });

    // Scale the stage to map logical coords to screen
    _hmiStage.scale({ x: scale, y: scale });

    // Create layers
    _hmiBackgroundLayer = new Konva.Layer();
    _hmiElementsLayer = new Konva.Layer();
    _hmiUiLayer = new Konva.Layer();
    _hmiStage.add(_hmiBackgroundLayer);
    _hmiStage.add(_hmiElementsLayer);
    _hmiStage.add(_hmiUiLayer);

    // Transformer for edit mode
    _hmiTransformer = new Konva.Transformer({
        rotateEnabled: false,
        borderStroke: "#c8102e",
        anchorStroke: "#c8102e",
        anchorFill: "#fff",
        anchorSize: 8,
        visible: false,
    });
    _hmiUiLayer.add(_hmiTransformer);

    // Load background image if set
    if (screen.backgroundImage) {
        hmiSetBackground(screen.backgroundImage, screen.width, screen.height);
    }

    // Zoom with mouse wheel
    _hmiStage.on("wheel", (e) => {
        e.evt.preventDefault();
        const scaleBy = 1.08;
        const oldScale = _hmiStage.scaleX();
        const pointer = _hmiStage.getPointerPosition();
        const direction = e.evt.deltaY < 0 ? 1 : -1;
        const newScale = direction > 0 ? oldScale * scaleBy : oldScale / scaleBy;
        const clampedScale = Math.max(0.2, Math.min(4, newScale));
        const mousePointTo = {
            x: (pointer.x - _hmiStage.x()) / oldScale,
            y: (pointer.y - _hmiStage.y()) / oldScale,
        };
        _hmiStage.scale({ x: clampedScale, y: clampedScale });
        _hmiStage.position({
            x: pointer.x - mousePointTo.x * clampedScale,
            y: pointer.y - mousePointTo.y * clampedScale,
        });
    });

    // Pan by dragging stage (only in view mode)
    _hmiStage.draggable(!_hmiEditMode);

    // Click on empty area deselects in edit mode
    _hmiStage.on("click tap", (e) => {
        if (e.target === _hmiStage || e.target.getLayer() === _hmiBackgroundLayer) {
            hmiDeselectElement();
        }
    });

    // Render all elements
    hmiRenderAllElements(screen);

    // Start live data updates
    hmiStartLiveUpdates();
}

function hmiSetBackground(imageUrl, width, height) {
    const img = new Image();
    img.onload = () => {
        _hmiBackgroundLayer.destroyChildren();
        const bgImage = new Konva.Image({
            image: img,
            width: width,
            height: height,
            listening: false,
        });
        _hmiBackgroundLayer.add(bgImage);
        _hmiBackgroundLayer.draw();
    };
    img.src = imageUrl;
}

function hmiScaleToFit() {
    if (!_hmiStage || !_hmiCurrentEquipment) return;
    const equip = hmiGetCurrentEquipment();
    if (!equip || !equip.screen) return;
    const container = document.getElementById("hmiCanvasContainer");
    if (!container) return;
    const scale = container.offsetWidth / equip.screen.width;
    _hmiStage.width(container.offsetWidth);
    _hmiStage.height(equip.screen.height * scale);
    _hmiStage.scale({ x: scale, y: scale });
    _hmiStage.position({ x: 0, y: 0 });
}

window.addEventListener("resize", () => { if (_hmiStage) hmiScaleToFit(); });

// =============================================
// Element Renderers (stubs — Tasks 7-10)
// =============================================

function hmiRenderAllElements(screen) {
    _hmiElementsLayer.destroyChildren();
    _hmiUiLayer.destroyChildren();
    // Re-add transformer
    _hmiTransformer = new Konva.Transformer({
        rotateEnabled: false,
        borderStroke: "#c8102e",
        anchorStroke: "#c8102e",
        anchorFill: "#fff",
        anchorSize: 8,
        visible: false,
    });
    _hmiUiLayer.add(_hmiTransformer);

    for (const el of screen.elements || []) {
        const node = hmiCreateElement(el);
        if (node) {
            _hmiElementsLayer.add(node);
        }
    }
    _hmiElementsLayer.draw();
}

function hmiCreateElement(el) {
    let node = null;
    switch (el.type) {
        case "display":     node = hmiCreateDisplay(el); break;
        case "gauge":       node = hmiCreateGauge(el); break;
        case "tank":        node = hmiCreateTank(el); break;
        case "bargraph":    node = hmiCreateBarGraph(el); break;
        case "progressBar": node = hmiCreateProgressBar(el); break;
        case "indicator":   node = hmiCreateIndicator(el); break;
        case "valve":       node = hmiCreateValve(el); break;
        case "motor":       node = hmiCreateMotor(el); break;
        case "pipe":        node = hmiCreatePipe(el); break;
        case "label":       node = hmiCreateLabel(el); break;
        case "image":       node = hmiCreateImageEl(el); break;
        case "alarmBanner": node = hmiCreateAlarmBanner(el); break;
        case "button":      node = hmiCreateButton(el); break;
        case "switch":      node = hmiCreateSwitch(el); break;
        case "slider":      node = hmiCreateSlider(el); break;
        case "numericInput":node = hmiCreateNumericInput(el); break;
        default:
            node = hmiCreatePlaceholder(el);
    }
    if (node) {
        node.setAttr("elementId", el.id);
        // Click handler for edit mode selection
        node.on("click tap", () => {
            if (_hmiEditMode) hmiSelectElement(el, node);
        });
    }
    return node;
}

function hmiCreatePlaceholder(el) {
    const group = new Konva.Group({ x: el.x, y: el.y, width: el.width, height: el.height, id: el.id, draggable: _hmiEditMode });
    group.add(new Konva.Rect({ width: el.width, height: el.height, fill: "rgba(200,16,46,0.1)", stroke: "#c8102e", strokeWidth: 1, cornerRadius: 4 }));
    group.add(new Konva.Text({ text: el.type, width: el.width, height: el.height, fontSize: 11, fill: "#c8102e", align: "center", verticalAlign: "middle" }));
    return group;
}

function hmiSelectElement(el, node) {
    _hmiSelectedElement = el;
    _hmiSelectedNode = node;
    if (_hmiTransformer) {
        _hmiTransformer.nodes([node]);
        _hmiTransformer.visible(true);
        _hmiUiLayer.draw();
    }
    hmiRenderProperties(el);

    // Sync position back on drag end
    node.off("dragend.edit");
    node.on("dragend.edit", () => {
        el.x = Math.round(node.x());
        el.y = Math.round(node.y());
        hmiAutoSave();
    });

    // Sync size on transform end
    node.off("transformend.edit");
    node.on("transformend.edit", () => {
        const scaleX = node.scaleX();
        const scaleY = node.scaleY();
        el.width = Math.round((el.width || 100) * scaleX);
        el.height = Math.round((el.height != null ? el.height : 50) * scaleY);
        el.x = Math.round(node.x());
        el.y = Math.round(node.y());
        node.scaleX(1);
        node.scaleY(1);
        // Rebuild element with new size
        const newNode = hmiCreateElement(el);
        if (newNode) {
            node.destroy();
            _hmiElementsLayer.add(newNode);
            _hmiElementsLayer.draw();
            _hmiSelectedNode = newNode;
            _hmiTransformer.nodes([newNode]);
            _hmiUiLayer.draw();
        }
        hmiAutoSave();
    });
}

function hmiRenderProperties(el) {
    const panel = document.getElementById("hmiProperties");
    if (!panel) return;
    panel.style.display = "block";

    const equip = hmiGetCurrentEquipment();
    const selectedDevice = el.device || (equip ? equip.device : "");
    const deviceTags = [];
    if (selectedDevice && _hmiDeviceMap[selectedDevice]) {
        Object.keys(_hmiDeviceMap[selectedDevice].tags).forEach(alias => deviceTags.push(alias));
    }

    const deviceOptions = Object.keys(_hmiDeviceMap).map(d => `<option value="${escAttr(d)}" ${d === selectedDevice ? 'selected' : ''}>${escHtml(d)}</option>`).join("");
    const tagOptions = deviceTags.map(t => `<option value="${escAttr(t)}" ${t === el.tag ? 'selected' : ''}>${escHtml(t)}</option>`).join("");

    let html = `<h4>${el.type} Properties</h4>`;
    html += `<div class="form-group"><label>Type</label><input type="text" value="${escAttr(el.type)}" disabled></div>`;

    // Device selector (for types that read/write tags)
    if (el.type !== "label") {
        html += `<div class="form-group"><label>Device</label><select onchange="hmiPropChangeDevice(this.value)"><option value="">(inherit from equipment)</option>${deviceOptions}</select></div>`;
    }

    // Tag (for most types)
    if (el.type !== "label" && el.type !== "pipe") {
        html += `<div class="form-group"><label>Tag</label><select onchange="hmiPropChange('tag',this.value)"><option value="">Select tag...</option>${tagOptions}</select></div>`;
    }
    if (el.type === "pipe") {
        html += `<div class="form-group"><label>Flow Active Tag</label><select onchange="hmiPropChange('flowActive',this.value)"><option value="">Select tag...</option>${tagOptions.replace(new RegExp(`value="${escAttr(el.flowActive || "")}" `, "g"), `value="${escAttr(el.flowActive || "")}" selected `)}</select></div>`;
    }

    // Common fields
    if ("label" in el || el.type === "display" || el.type === "motor" || el.type === "button" || el.type === "switch" || el.type === "slider" || el.type === "numericInput" || el.type === "progressBar") {
        html += `<div class="form-group"><label>Label</label><input type="text" value="${escAttr(el.label || "")}" onchange="hmiPropChange('label',this.value)"></div>`;
    }
    if (el.type === "label" || el.type === "alarmBanner") {
        html += `<div class="form-group"><label>Text</label><input type="text" value="${escAttr(el.text || "")}" onchange="hmiPropChange('text',this.value)"></div>`;
    }
    if ("unit" in el) {
        html += `<div class="form-group"><label>Unit</label><input type="text" value="${escAttr(el.unit || "")}" onchange="hmiPropChange('unit',this.value)"></div>`;
    }
    if ("min" in el) {
        html += `<div class="form-group"><label>Min</label><input type="number" value="${el.min || 0}" onchange="hmiPropChange('min',parseFloat(this.value))"></div>`;
    }
    if ("max" in el) {
        html += `<div class="form-group"><label>Max</label><input type="number" value="${el.max || 100}" onchange="hmiPropChange('max',parseFloat(this.value))"></div>`;
    }
    if ("fontSize" in el) {
        html += `<div class="form-group"><label>Font Size</label><input type="number" value="${el.fontSize || 18}" onchange="hmiPropChange('fontSize',parseInt(this.value))"></div>`;
    }
    if (el.thresholds) {
        html += `<div class="form-group"><label>Warning Threshold</label><input type="number" value="${el.thresholds.warning || ""}" onchange="hmiPropThreshold('warning',parseFloat(this.value))"></div>`;
        html += `<div class="form-group"><label>Critical Threshold</label><input type="number" value="${el.thresholds.critical || ""}" onchange="hmiPropThreshold('critical',parseFloat(this.value))"></div>`;
    }
    if ("writeValue" in el) {
        html += `<div class="form-group"><label>Write Value</label><input type="text" value="${el.writeValue ?? ""}" onchange="hmiPropChange('writeValue',isNaN(this.value)?this.value:parseFloat(this.value))"></div>`;
    }
    if ("writeValueOn" in el) {
        html += `<div class="form-group"><label>Write Value (ON)</label><input type="text" value="${el.writeValueOn ?? 1}" onchange="hmiPropChange('writeValueOn',isNaN(this.value)?this.value:parseFloat(this.value))"></div>`;
    }
    if ("writeValueOff" in el) {
        html += `<div class="form-group"><label>Write Value (OFF)</label><input type="text" value="${el.writeValueOff ?? 0}" onchange="hmiPropChange('writeValueOff',isNaN(this.value)?this.value:parseFloat(this.value))"></div>`;
    }
    if ("writeValueOpen" in el) {
        html += `<div class="form-group"><label>Write Value (Open)</label><input type="text" value="${el.writeValueOpen ?? 1}" onchange="hmiPropChange('writeValueOpen',isNaN(this.value)?this.value:parseFloat(this.value))"></div>`;
    }
    if ("writeValueClose" in el) {
        html += `<div class="form-group"><label>Write Value (Close)</label><input type="text" value="${el.writeValueClose ?? 0}" onchange="hmiPropChange('writeValueClose',isNaN(this.value)?this.value:parseFloat(this.value))"></div>`;
    }
    if ("confirmMessage" in el) {
        html += `<div class="form-group"><label>Confirm Message</label><input type="text" value="${escAttr(el.confirmMessage || "")}" onchange="hmiPropChange('confirmMessage',this.value)"></div>`;
    }

    // Alarm system integration
    if (el.type === "alarmBanner") {
        html += `<div class="form-group"><label><input type="checkbox" ${el.useAlarmSystem ? "checked" : ""} onchange="hmiPropChange('useAlarmSystem',this.checked)"> Use Alarm System</label></div>`;
        html += `<div class="form-group" style="font-size:0.8rem;color:var(--text-secondary)">When enabled, banner uses the alarm engine (with conditional profiles per product) instead of a fixed trigger value.</div>`;
    }
    if (el.type !== "label" && el.type !== "pipe" && el.type !== "alarmBanner") {
        html += `<div class="form-group"><label><input type="checkbox" ${el.alarmHighlight ? "checked" : ""} onchange="hmiPropChange('alarmHighlight',this.checked)"> Alarm Highlight</label></div>`;
        html += `<div class="form-group" style="font-size:0.8rem;color:var(--text-secondary)">When enabled, element glows red/orange when its tag has an active alarm (uses conditional profiles).</div>`;
    }

    html += `<div class="form-group"><label>X</label><input type="number" value="${el.x || 0}" onchange="hmiPropChange('x',parseInt(this.value))"></div>`;
    html += `<div class="form-group"><label>Y</label><input type="number" value="${el.y || 0}" onchange="hmiPropChange('y',parseInt(this.value))"></div>`;
    html += `<div class="form-group"><label>Width</label><input type="number" value="${el.width || 100}" onchange="hmiPropChange('width',parseInt(this.value))"></div>`;
    html += `<div class="form-group"><label>Height</label><input type="number" value="${el.height != null ? el.height : 50}" onchange="hmiPropChange('height',parseInt(this.value))"></div>`;

    html += `<button class="btn btn-danger btn-sm btn-delete" onclick="hmiDeleteSelectedElement()">Delete Element</button>`;

    panel.innerHTML = html;
}

function hmiPropChange(key, value) {
    if (!_hmiSelectedElement) return;
    _hmiSelectedElement[key] = value;
    // Rebuild the Konva node
    if (_hmiSelectedNode) {
        const newNode = hmiCreateElement(_hmiSelectedElement);
        if (newNode) {
            // Stop any running animations before destroying the old node
            _hmiSelectedNode.find("*").forEach(n => {
                if (n.getAttr("_rotAnim"))  { n.getAttr("_rotAnim").stop();  }
                if (n.getAttr("_blinkAnim")){ n.getAttr("_blinkAnim").stop(); }
                if (n.getAttr("_flowAnim")) { n.getAttr("_flowAnim").stop();  }
            });
            _hmiSelectedNode.destroy();
            _hmiElementsLayer.add(newNode);
            _hmiSelectedNode = newNode;
            _hmiTransformer.nodes([newNode]);
            _hmiElementsLayer.draw();
            _hmiUiLayer.draw();
        }
    }
    hmiAutoSave();
}

function hmiPropChangeDevice(deviceName) {
    if (!_hmiSelectedElement) return;
    if (deviceName) {
        _hmiSelectedElement.device = deviceName;
    } else {
        delete _hmiSelectedElement.device;
    }
    // Clear tag since device changed
    _hmiSelectedElement.tag = "";
    if (_hmiSelectedElement.flowActive !== undefined) _hmiSelectedElement.flowActive = "";
    hmiAutoSave();
    // Re-render properties to update tag dropdown
    hmiRenderProperties(_hmiSelectedElement);
}

function hmiPropThreshold(key, value) {
    if (!_hmiSelectedElement) return;
    if (!_hmiSelectedElement.thresholds) _hmiSelectedElement.thresholds = {};
    _hmiSelectedElement.thresholds[key] = value;
    // Rebuild node without adding spurious property
    if (_hmiSelectedNode && _hmiElementsLayer) {
        const newNode = hmiCreateElement(_hmiSelectedElement);
        if (newNode) {
            // Stop any running animations before destroying the old node
            _hmiSelectedNode.find("*").forEach(n => {
                if (n.getAttr("_rotAnim"))  { n.getAttr("_rotAnim").stop();  }
                if (n.getAttr("_blinkAnim")){ n.getAttr("_blinkAnim").stop(); }
                if (n.getAttr("_flowAnim")) { n.getAttr("_flowAnim").stop();  }
            });
            _hmiSelectedNode.destroy();
            _hmiElementsLayer.add(newNode);
            _hmiSelectedNode = newNode;
            if (_hmiTransformer) { _hmiTransformer.nodes([newNode]); _hmiUiLayer.draw(); }
            _hmiElementsLayer.draw();
        }
    }
    hmiAutoSave();
}

// --- Value Display Elements ---

function hmiCreateDisplay(el) {
    const w = el.width || 130, h = el.height || 55;
    const group = new Konva.Group({ x: el.x, y: el.y, width: w, height: h, id: el.id, draggable: _hmiEditMode });

    const bg = new Konva.Rect({ width: w, height: h, fill: "rgba(0,0,0,0.8)", cornerRadius: 6, stroke: "rgba(255,255,255,0.2)", strokeWidth: 1, name: "bg" });
    const labelText = new Konva.Text({ text: el.label || "", y: 4, width: w, fontSize: 9, fill: "rgba(255,255,255,0.6)", align: "center", name: "label" });
    const valueText = new Konva.Text({ text: "--", y: h * 0.25, width: w, fontSize: el.fontSize || 22, fontStyle: "bold", fontFamily: "'IBM Plex Mono', monospace", fill: "#fff", align: "center", name: "value" });
    const unitText = new Konva.Text({ text: el.unit || "", y: h - 14, width: w, fontSize: 10, fill: "rgba(255,255,255,0.5)", align: "center", name: "unit" });

    group.add(bg, labelText, valueText, unitText);
    return group;
}

function hmiCreateGauge(el) {
    const w = el.width || 120, h = el.height || 100;
    const group = new Konva.Group({ x: el.x, y: el.y, width: w, height: h, id: el.id, draggable: _hmiEditMode });

    const cx = w / 2, cy = h * 0.65, radius = Math.min(w, h) * 0.4;

    // Background arc (gray)
    const bgArc = new Konva.Arc({ x: cx, y: cy, innerRadius: radius - 8, outerRadius: radius, angle: 180, rotation: 180, fill: "rgba(255,255,255,0.1)", name: "bgArc" });
    // Value arc (colored)
    const valArc = new Konva.Arc({ x: cx, y: cy, innerRadius: radius - 8, outerRadius: radius, angle: 0, rotation: 180, fill: "#22c55e", name: "valArc" });
    // Value text
    const valText = new Konva.Text({ text: "--", x: 0, y: cy - 12, width: w, fontSize: 16, fontStyle: "bold", fontFamily: "'IBM Plex Mono', monospace", fill: "#fff", align: "center", name: "value" });
    // Label
    const labelText = new Konva.Text({ text: el.label || "", x: 0, y: cy + 4, width: w, fontSize: 9, fill: "rgba(255,255,255,0.6)", align: "center", name: "label" });
    // Unit
    const unitText = new Konva.Text({ text: el.unit || "", x: 0, y: cy + 16, width: w, fontSize: 9, fill: "rgba(255,255,255,0.4)", align: "center", name: "unit" });

    group.add(bgArc, valArc, valText, labelText, unitText);
    return group;
}

function hmiCreateTank(el) {
    const w = el.width || 80, h = el.height || 200;
    const group = new Konva.Group({ x: el.x, y: el.y, width: w, height: h, id: el.id, draggable: _hmiEditMode });

    // Border
    const border = new Konva.Rect({ width: w, height: h, stroke: "rgba(255,255,255,0.4)", strokeWidth: 2, cornerRadius: 4, name: "border" });
    // Fill (starts empty at bottom)
    const fill = new Konva.Rect({ x: 2, y: h - 2, width: w - 4, height: 0, fill: el.fillColor || "#38bdf8", opacity: 0.8, cornerRadius: [0, 0, 2, 2], name: "fill" });
    // Level text
    const levelText = new Konva.Text({ text: "0%", x: 0, y: h / 2 - 8, width: w, fontSize: 14, fontStyle: "bold", fontFamily: "'IBM Plex Mono', monospace", fill: "#fff", align: "center", name: "value" });

    group.add(border, fill, levelText);
    return group;
}

function hmiCreateBarGraph(el) {
    const w = el.width || 40, h = el.height || 180;
    const isVertical = (el.orientation || "vertical") === "vertical";
    const group = new Konva.Group({ x: el.x, y: el.y, width: w, height: h, id: el.id, draggable: _hmiEditMode });

    const border = new Konva.Rect({ width: w, height: h, stroke: "rgba(255,255,255,0.3)", strokeWidth: 1, cornerRadius: 3, name: "border" });
    const fill = new Konva.Rect({
        x: isVertical ? 1 : 1,
        y: isVertical ? h - 1 : 1,
        width: isVertical ? w - 2 : 0,
        height: isVertical ? 0 : h - 2,
        fill: el.fillColor || "#38bdf8",
        opacity: 0.8,
        name: "fill"
    });

    group.add(border, fill);
    return group;
}

function hmiCreateProgressBar(el) {
    const w = el.width || 250, h = el.height || 25;
    const group = new Konva.Group({ x: el.x, y: el.y, width: w, height: h, id: el.id, draggable: _hmiEditMode });

    const bg = new Konva.Rect({ width: w, height: h, fill: "rgba(255,255,255,0.1)", cornerRadius: h / 2, name: "bg" });
    const fill = new Konva.Rect({ x: 0, y: 0, width: 0, height: h, fill: el.fillColor || "#22c55e", cornerRadius: h / 2, name: "fill" });
    const pctText = new Konva.Text({ text: "0%", x: 0, y: 0, width: w, height: h, fontSize: 12, fontStyle: "bold", fontFamily: "'IBM Plex Mono', monospace", fill: "#fff", align: "center", verticalAlign: "middle", name: "value" });
    const labelText = new Konva.Text({ text: el.label || "", x: 0, y: -14, width: w, fontSize: 9, fill: "rgba(255,255,255,0.6)", name: "label" });

    group.add(bg, fill, pctText, labelText);
    return group;
}

// --- Status Indicator Elements ---

function hmiCreateIndicator(el) {
    const w = el.width || 30, h = el.height || 30;
    const group = new Konva.Group({ x: el.x, y: el.y, width: w, height: h + 16, id: el.id, draggable: _hmiEditMode });
    const circle = new Konva.Circle({ x: w/2, y: w/2, radius: w/2, fill: el.colorOff || "#ef4444", stroke: "rgba(255,255,255,0.3)", strokeWidth: 1, name: "led" });
    const label = new Konva.Text({ text: el.labelOff || "", x: -10, y: w + 3, width: w + 20, fontSize: 8, fill: "rgba(255,255,255,0.6)", align: "center", name: "label" });
    group.add(circle, label);
    return group;
}

function hmiCreateValve(el) {
    const w = el.width || 40, h = el.height || 40;
    const group = new Konva.Group({ x: el.x, y: el.y, width: w, height: h + 16, id: el.id, draggable: _hmiEditMode });
    const body = new Konva.Group({ x: w/2, y: h/2, name: "body" });
    // Left triangle
    body.add(new Konva.Line({ points: [-w/2, -h/2, 0, 0, -w/2, h/2], closed: true, fill: el.colorClosed || "#ef4444", stroke: "rgba(255,255,255,0.4)", strokeWidth: 1 }));
    // Right triangle
    body.add(new Konva.Line({ points: [w/2, -h/2, 0, 0, w/2, h/2], closed: true, fill: el.colorClosed || "#ef4444", stroke: "rgba(255,255,255,0.4)", strokeWidth: 1 }));
    const label = new Konva.Text({ text: el.label || "Valve", x: -10, y: h + 3, width: w + 20, fontSize: 9, fill: "rgba(255,255,255,0.6)", align: "center", name: "label" });
    group.add(body, label);

    group.on("click tap", () => {
        if (_hmiEditMode) return;
        if (typeof getRole === "function" && getRole() === "monitor") { toast("Write not allowed for monitor role", "error"); return; }
        if (!el.tag) { toast("No tag configured for this element", "error"); return; }
        const deviceName = hmiGetElementDeviceName(el);
        const dev = deviceName ? _hmiDeviceMap[deviceName] : null;
        if (!dev || !dev.allowWrite) { toast("Write not allowed on this device", "error"); return; }
        const tagData = dev.tags[el.tag];
        const isOpen = tagData && tagData.value && tagData.value !== 0;
        const newValue = isOpen ? (el.writeValueClose ?? 0) : (el.writeValueOpen ?? 1);
        if (!confirm((isOpen ? "Close" : "Open") + " valve " + (el.label || el.tag) + "?")) return;
        api("/api/live/write", "POST", { device: deviceName, tag: el.tag, value: newValue })
            .then(d => toast(d.message))
            .catch(e => toast(e.message, "error"));
    });
    return group;
}

function hmiCreateMotor(el) {
    const w = el.width || 60, h = el.height || 60;
    const r = Math.min(w, h) / 2;
    const group = new Konva.Group({ x: el.x, y: el.y, width: w, height: h, id: el.id, draggable: _hmiEditMode });
    const circle = new Konva.Circle({ x: r, y: r, radius: r - 2, stroke: "rgba(255,255,255,0.4)", strokeWidth: 2, fill: el.colorOff || "#6b7280", name: "body" });
    const blades = new Konva.Group({ x: r, y: r, name: "blades" });
    for (let i = 0; i < 4; i++) {
        const angle = (i * 90) * Math.PI / 180;
        blades.add(new Konva.Line({
            points: [0, 0, Math.cos(angle) * (r - 6), Math.sin(angle) * (r - 6)],
            stroke: "#fff", strokeWidth: 2, lineCap: "round"
        }));
    }
    const label = new Konva.Text({ text: el.label || "", x: -10, y: h + 3, width: w + 20, fontSize: 9, fill: "rgba(255,255,255,0.6)", align: "center", name: "label" });
    group.add(circle, blades, label);

    group.on("click tap", () => {
        if (_hmiEditMode) return;
        if (typeof getRole === "function" && getRole() === "monitor") { toast("Write not allowed for monitor role", "error"); return; }
        if (!el.tag) { toast("No tag configured for this element", "error"); return; }
        const deviceName = hmiGetElementDeviceName(el);
        const dev = deviceName ? _hmiDeviceMap[deviceName] : null;
        if (!dev || !dev.allowWrite) { toast("Write not allowed on this device", "error"); return; }
        const tagData = dev.tags[el.tag];
        const isOn = tagData && tagData.value && tagData.value !== 0;
        const newValue = isOn ? (el.writeValueOff ?? 0) : (el.writeValueOn ?? 1);
        if (!confirm((isOn ? "Stop" : "Start") + " " + (el.label || el.tag) + "?")) return;
        api("/api/live/write", "POST", { device: deviceName, tag: el.tag, value: newValue })
            .then(d => toast(d.message))
            .catch(e => toast(e.message, "error"));
    });
    return group;
}

function hmiCreateImageEl(el) {
    const w = el.width || 60, h = el.height || 60;
    const group = new Konva.Group({ x: el.x, y: el.y, width: w, height: h, id: el.id, draggable: _hmiEditMode });
    const imgNode = new Konva.Image({ width: w, height: h, name: "img" });
    group.add(imgNode);
    // Load default image
    const url = el.imageUrl || el.imageOff || "";
    if (url) {
        const img = new Image();
        img.onload = () => { imgNode.image(img); imgNode.getLayer()?.batchDraw(); };
        img.src = url;
    }
    return group;
}

function hmiCreateAlarmBanner(el) {
    const w = el.width || 300, h = el.height || 35;
    const group = new Konva.Group({ x: el.x, y: el.y, width: w, height: h, id: el.id, draggable: _hmiEditMode, visible: false });
    const bg = new Konva.Rect({ width: w, height: h, fill: "#ef4444", cornerRadius: 4, name: "bg" });
    const text = new Konva.Text({ text: el.text || "ALARM", x: 0, y: 0, width: w, height: h, fontSize: 14, fontStyle: "bold", fill: "#fff", align: "center", verticalAlign: "middle", name: "text" });
    group.add(bg, text);
    return group;
}

// --- Value Update Functions ---

function hmiUpdateElementValue(el, tagData) {
    if (!_hmiStage) return;
    const node = _hmiElementsLayer.findOne("#" + el.id);
    if (!node) return;

    const value = tagData ? tagData.value : null;

    // Alarm highlight: any element with alarmHighlight=true glows when tag is in alarm
    if (el.alarmHighlight) {
        const alarm = hmiGetElementAlarm(el);
        const existingGlow = node.getAttr("_alarmGlow");
        if (alarm && !existingGlow) {
            const color = alarm.severity === "critical" ? "#ef4444" : "#f59e0b";
            node.shadowColor(color);
            node.shadowBlur(15);
            node.shadowOpacity(0.8);
            node.setAttr("_alarmGlow", true);
        } else if (!alarm && existingGlow) {
            node.shadowBlur(0);
            node.shadowOpacity(0);
            node.setAttr("_alarmGlow", false);
        }
    }

    switch (el.type) {
        case "display":     hmiUpdateDisplay(node, el, value); break;
        case "gauge":       hmiUpdateGauge(node, el, value); break;
        case "tank":        hmiUpdateTank(node, el, value); break;
        case "bargraph":    hmiUpdateBarGraph(node, el, value); break;
        case "progressBar": hmiUpdateProgressBar(node, el, value); break;
        case "indicator":   hmiUpdateIndicator(node, el, value); break;
        case "valve":       hmiUpdateValve(node, el, value); break;
        case "motor":       hmiUpdateMotor(node, el, value); break;
        case "image":       hmiUpdateImageEl(node, el, value); break;
        case "alarmBanner":   hmiUpdateAlarmBanner(node, el, value); break;
        case "pipe":          /* special: uses flowActive tag — handled in live loop */ break;
        case "switch":        hmiUpdateSwitch(node, el, value); break;
        case "slider":        hmiUpdateSlider(node, el, value); break;
        case "numericInput":  hmiUpdateNumericInput(node, el, value); break;
    }
}

function hmiUpdateDisplay(node, el, value) {
    const valText = node.findOne(".value");
    const bg = node.findOne(".bg");
    if (!valText) return;
    valText.text(value != null ? String(typeof value === "number" ? (Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2)) : value) : "--");
    // Threshold colors
    if (bg && el.thresholds && typeof value === "number") {
        if (el.thresholds.critical && value >= el.thresholds.critical) {
            bg.stroke("#ef4444"); bg.strokeWidth(2);
        } else if (el.thresholds.warning && value >= el.thresholds.warning) {
            bg.stroke("#f59e0b"); bg.strokeWidth(2);
        } else {
            bg.stroke("rgba(255,255,255,0.2)"); bg.strokeWidth(1);
        }
    }
    node.getLayer()?.batchDraw();
}

function hmiUpdateGauge(node, el, value) {
    const valArc = node.findOne(".valArc");
    const valText = node.findOne(".value");
    if (!valArc || !valText) return;
    const min = el.min || 0, max = el.max || 100;
    const pct = value != null ? Math.max(0, Math.min(1, (value - min) / (max - min))) : 0;
    const angle = pct * 180;
    valArc.angle(angle);
    // Color
    let color = "#22c55e";
    if (el.thresholds && typeof value === "number") {
        if (el.thresholds.critical && value >= el.thresholds.critical) color = "#ef4444";
        else if (el.thresholds.warning && value >= el.thresholds.warning) color = "#f59e0b";
    }
    valArc.fill(color);
    valText.text(value != null ? (typeof value === "number" ? value.toFixed(1) : String(value)) : "--");
    node.getLayer()?.batchDraw();
}

function hmiUpdateTank(node, el, value) {
    const fill = node.findOne(".fill");
    const valText = node.findOne(".value");
    if (!fill || !valText) return;
    const min = el.min || 0, max = el.max || 100;
    const h = el.height || 200;
    const pct = value != null ? Math.max(0, Math.min(1, (value - min) / (max - min))) : 0;
    const fillH = pct * (h - 4);
    fill.y(h - 2 - fillH);
    fill.height(fillH);
    valText.text(Math.round(pct * 100) + "%");
    // Color
    if (el.thresholds && typeof value === "number") {
        if (el.thresholds.critical && value >= el.thresholds.critical) fill.fill("#ef4444");
        else if (el.thresholds.warning && value >= el.thresholds.warning) fill.fill("#f59e0b");
        else fill.fill(el.fillColor || "#38bdf8");
    }
    node.getLayer()?.batchDraw();
}

function hmiUpdateBarGraph(node, el, value) {
    const fill = node.findOne(".fill");
    if (!fill) return;
    const min = el.min || 0, max = el.max || 100;
    const pct = value != null ? Math.max(0, Math.min(1, (value - min) / (max - min))) : 0;
    const isVertical = (el.orientation || "vertical") === "vertical";
    const w = el.width || 40, h = el.height || 180;
    if (isVertical) {
        fill.height(pct * (h - 2));
        fill.y(h - 1 - pct * (h - 2));
    } else {
        fill.width(pct * (w - 2));
    }
    node.getLayer()?.batchDraw();
}

function hmiUpdateProgressBar(node, el, value) {
    const fill = node.findOne(".fill");
    const valText = node.findOne(".value");
    if (!fill || !valText) return;
    const min = el.min || 0, max = el.max || 100;
    const w = el.width || 250;
    const pct = value != null ? Math.max(0, Math.min(1, (value - min) / (max - min))) : 0;
    fill.width(pct * w);
    valText.text(Math.round(pct * 100) + "%");
    node.getLayer()?.batchDraw();
}

function hmiUpdateIndicator(node, el, value) {
    const led = node.findOne(".led");
    const label = node.findOne(".label");
    if (!led) return;
    const isOn = value && value !== 0 && value !== false;
    led.fill(isOn ? (el.colorOn || "#22c55e") : (el.colorOff || "#ef4444"));
    led.shadowBlur(isOn ? 12 : 0);
    led.shadowColor(isOn ? (el.colorOn || "#22c55e") : "transparent");
    if (label) label.text(isOn ? (el.labelOn || "") : (el.labelOff || ""));
    node.getLayer()?.batchDraw();
}

function hmiUpdateValve(node, el, value) {
    const body = node.findOne(".body");
    if (!body) return;
    const isOpen = value && value !== 0 && value !== false;
    const color = isOpen ? (el.colorOpen || "#22c55e") : (el.colorClosed || "#ef4444");
    body.find("Line").forEach(l => l.fill(color));
    // Rotate to show open/closed
    const targetRotation = isOpen ? 90 : 0;
    if (body.rotation() !== targetRotation) {
        new Konva.Tween({ node: body, rotation: targetRotation, duration: 0.3, easing: Konva.Easings.EaseInOut }).play();
    }
    node.getLayer()?.batchDraw();
}

function hmiUpdateMotor(node, el, value) {
    const body = node.findOne(".body");
    const blades = node.findOne(".blades");
    if (!body || !blades) return;
    const isOn = value && value !== 0 && value !== false;
    body.fill(isOn ? (el.colorOn || "#22c55e") : (el.colorOff || "#6b7280"));
    // Start/stop rotation animation
    const existingAnim = blades.getAttr("_rotAnim");
    if (isOn && !existingAnim) {
        const anim = new Konva.Animation((frame) => {
            blades.rotation(frame.time * 0.1);
        }, blades.getLayer());
        anim.start();
        blades.setAttr("_rotAnim", anim);
    } else if (!isOn && existingAnim) {
        existingAnim.stop();
        blades.setAttr("_rotAnim", null);
    }
    node.getLayer()?.batchDraw();
}

function hmiUpdateImageEl(node, el, value) {
    const imgNode = node.findOne(".img");
    if (!imgNode) return;
    const isOn = value && value !== 0 && value !== false;
    const url = isOn ? (el.imageOn || el.imageUrl || "") : (el.imageOff || el.imageUrl || "");
    const currentUrl = imgNode.getAttr("_currentUrl");
    if (url && url !== currentUrl) {
        const img = new Image();
        img.onload = () => { imgNode.image(img); imgNode.setAttr("_currentUrl", url); imgNode.getLayer()?.batchDraw(); };
        img.src = url;
    }
}

function hmiUpdateAlarmBanner(node, el, value) {
    let isActive;
    if (el.useAlarmSystem) {
        // Use the real alarm system (respects conditional profiles per product)
        const alarm = hmiGetElementAlarm(el);
        isActive = alarm != null;
        // Update banner color based on severity
        if (isActive) {
            const bg = node.findOne(".bg");
            if (bg) bg.fill(alarm.severity === "critical" ? "#ef4444" : "#f59e0b");
            const text = node.findOne(".text");
            if (text && el.text === "AUTO") {
                text.text(alarm.severity.toUpperCase() + ": " + el.tag + " = " + alarm.value);
            }
        }
    } else {
        // Legacy mode: fixed triggerValue comparison
        isActive = (value != null && value == el.triggerValue);
    }
    node.visible(isActive || _hmiEditMode);
    // Blink when active
    const existingAnim = node.getAttr("_blinkAnim");
    if (isActive && !existingAnim) {
        const anim = new Konva.Animation((frame) => {
            node.opacity(Math.sin(frame.time * 0.005) > 0 ? 1 : 0.3);
        }, node.getLayer());
        anim.start();
        node.setAttr("_blinkAnim", anim);
    } else if (!isActive && existingAnim) {
        existingAnim.stop();
        node.opacity(1);
        node.setAttr("_blinkAnim", null);
    }
    node.getLayer()?.batchDraw();
}

// =============================================
// Element creators — Tasks 9-10
// =============================================

function hmiCreatePipe(el) {
    const group = new Konva.Group({ x: el.x, y: el.y, id: el.id, draggable: _hmiEditMode });
    const points = el.points || [0, 0, 200, 0];
    const line = new Konva.Line({
        points: points,
        stroke: el.color || "#38bdf8",
        strokeWidth: 4,
        lineCap: "round",
        dash: [12, 6],
        name: "pipe"
    });
    group.add(line);
    return group;
}

function hmiUpdatePipe(node, el, value) {
    const pipe = node.findOne(".pipe");
    if (!pipe) return;
    const isActive = value && value !== 0 && value !== false;
    pipe.opacity(isActive ? 1 : 0.3);
    const existingAnim = pipe.getAttr("_flowAnim");
    if (isActive && !existingAnim) {
        const anim = new Konva.Animation((frame) => {
            pipe.dashOffset(-frame.time * 0.03);
        }, pipe.getLayer());
        anim.start();
        pipe.setAttr("_flowAnim", anim);
    } else if (!isActive && existingAnim) {
        existingAnim.stop();
        pipe.setAttr("_flowAnim", null);
    }
    node.getLayer()?.batchDraw();
}

function hmiCreateLabel(el) {
    const group = new Konva.Group({ x: el.x, y: el.y, width: el.width || 300, height: el.height || 40, id: el.id, draggable: _hmiEditMode });
    const text = new Konva.Text({
        text: el.text || "Label",
        width: el.width || 300,
        height: el.height || 40,
        fontSize: el.fontSize || 18,
        fill: el.color || "#fff",
        fontStyle: "bold",
        verticalAlign: "middle",
        name: "text"
    });
    group.add(text);
    return group;
}

function hmiCreateButton(el) {
    const w = el.width || 110, h = el.height || 40;
    const group = new Konva.Group({ x: el.x, y: el.y, width: w, height: h, id: el.id, draggable: _hmiEditMode });
    const bg = new Konva.Rect({ width: w, height: h, fill: "#c8102e", cornerRadius: 6, name: "bg" });
    const text = new Konva.Text({ text: el.label || "Action", width: w, height: h, fontSize: 13, fontStyle: "bold", fill: "#fff", align: "center", verticalAlign: "middle", name: "text" });
    group.add(bg, text);

    group.on("click tap", () => {
        if (_hmiEditMode) return;
        if (typeof getRole === "function" && getRole() === "monitor") { toast("Write not allowed for monitor role", "error"); return; }
        const deviceName = hmiGetElementDeviceName(el);
        const dev = deviceName ? _hmiDeviceMap[deviceName] : null;
        if (!dev || !dev.allowWrite) {
            toast("Write not allowed on this device (allowWrite is disabled)", "error");
            return;
        }
        if (!el.tag) { toast("No tag configured for this element", "error"); return; }
        if (!confirm("WARNING: Writing to PLC.\n\n" + (el.confirmMessage || "Execute?") + "\n\nDevice: " + deviceName + "\nTag: " + el.tag + "\nValue: " + el.writeValue + "\n\nContinue?")) return;
        api("/api/live/write", "POST", { device: deviceName, tag: el.tag, value: el.writeValue })
            .then(d => toast(d.message))
            .catch(e => toast(e.message, "error"));
    });
    return group;
}

function hmiCreateSwitch(el) {
    const w = el.width || 70, h = el.height || 35;
    const group = new Konva.Group({ x: el.x, y: el.y, width: w, height: h + 16, id: el.id, draggable: _hmiEditMode });

    const track = new Konva.Rect({ width: w, height: h, fill: "#6b7280", cornerRadius: h/2, name: "track" });
    const handle = new Konva.Circle({ x: h/2, y: h/2, radius: h/2 - 4, fill: "#fff", name: "handle" });
    const label = new Konva.Text({ text: el.label || "", x: 0, y: h + 3, width: w, fontSize: 9, fill: "rgba(255,255,255,0.6)", align: "center", name: "label" });

    group.add(track, handle, label);

    group.on("click tap", () => {
        if (_hmiEditMode) return;
        if (typeof getRole === "function" && getRole() === "monitor") { toast("Write not allowed for monitor role", "error"); return; }
        const deviceName = hmiGetElementDeviceName(el);
        const dev = deviceName ? _hmiDeviceMap[deviceName] : null;
        if (!dev || !dev.allowWrite) { toast("Write not allowed", "error"); return; }
        const tagData = dev.tags[el.tag];
        const currentlyOn = tagData && tagData.value && tagData.value !== 0;
        const newValue = currentlyOn ? (el.writeValueOff ?? 0) : (el.writeValueOn ?? 1);
        if (!confirm("Toggle " + (el.label || el.tag) + " to " + (currentlyOn ? "OFF" : "ON") + "?")) return;
        api("/api/live/write", "POST", { device: deviceName, tag: el.tag, value: newValue })
            .then(d => toast(d.message))
            .catch(e => toast(e.message, "error"));
    });
    return group;
}

function hmiUpdateSwitch(node, el, value) {
    const track = node.findOne(".track");
    const handle = node.findOne(".handle");
    if (!track || !handle) return;
    const w = el.width || 70, h = el.height || 35;
    const isOn = value && value !== 0 && value !== false;
    track.fill(isOn ? "#22c55e" : "#6b7280");
    const targetX = isOn ? w - h/2 : h/2;
    if (handle.x() !== targetX) {
        new Konva.Tween({ node: handle, x: targetX, duration: 0.2, easing: Konva.Easings.EaseInOut }).play();
    }
    node.getLayer()?.batchDraw();
}

function hmiCreateSlider(el) {
    const w = el.width || 300, h = el.height || 40;
    const group = new Konva.Group({ x: el.x, y: el.y, width: w, height: h + 16, id: el.id, draggable: _hmiEditMode });

    const trackY = h / 2;
    const track = new Konva.Rect({ x: 0, y: trackY - 3, width: w, height: 6, fill: "rgba(255,255,255,0.2)", cornerRadius: 3, name: "track" });
    const fill = new Konva.Rect({ x: 0, y: trackY - 3, width: 0, height: 6, fill: "#c8102e", cornerRadius: 3, name: "fill" });
    const handle = new Konva.Circle({ x: 0, y: trackY, radius: 10, fill: "#fff", stroke: "#c8102e", strokeWidth: 2, name: "handle" });
    const valText = new Konva.Text({ text: "0", x: 0, y: trackY - 20, width: w, fontSize: 11, fontFamily: "'IBM Plex Mono', monospace", fill: "#fff", align: "center", name: "value" });
    const label = new Konva.Text({ text: (el.label || "") + (el.unit ? " (" + el.unit + ")" : ""), x: 0, y: h + 3, width: w, fontSize: 9, fill: "rgba(255,255,255,0.6)", align: "center", name: "label" });

    group.add(track, fill, handle, valText, label);

    handle.draggable(true);
    handle.on("dragmove", () => {
        const x = Math.max(0, Math.min(w, handle.x()));
        handle.y(trackY);
        handle.x(x);
        fill.width(x);
        const pct = x / w;
        const val = (el.min || 0) + pct * ((el.max || 100) - (el.min || 0));
        valText.text(val.toFixed(1));
    });
    handle.on("dragend", () => {
        if (_hmiEditMode) return;
        if (typeof getRole === "function" && getRole() === "monitor") { toast("Write not allowed for monitor role", "error"); return; }
        const deviceName = hmiGetElementDeviceName(el);
        const dev = deviceName ? _hmiDeviceMap[deviceName] : null;
        if (!dev || !dev.allowWrite) { toast("Write not allowed", "error"); return; }
        const pct = handle.x() / w;
        const val = (el.min || 0) + pct * ((el.max || 100) - (el.min || 0));
        api("/api/live/write", "POST", { device: deviceName, tag: el.tag, value: Math.round(val * 10) / 10 })
            .then(d => toast(d.message))
            .catch(e => toast(e.message, "error"));
    });
    return group;
}

function hmiUpdateSlider(node, el, value) {
    const handle = node.findOne(".handle");
    const fill = node.findOne(".fill");
    const valText = node.findOne(".value");
    if (!handle || !fill || !valText) return;
    const w = el.width || 300;
    const min = el.min || 0, max = el.max || 100;
    const pct = value != null ? Math.max(0, Math.min(1, (value - min) / (max - min))) : 0;
    const x = pct * w;
    handle.x(x);
    fill.width(x);
    valText.text(value != null ? (typeof value === "number" ? value.toFixed(1) : String(value)) : "0");
    node.getLayer()?.batchDraw();
}

function hmiCreateNumericInput(el) {
    const w = el.width || 150, h = el.height || 40;
    const group = new Konva.Group({ x: el.x, y: el.y, width: w, height: h + 16, id: el.id, draggable: _hmiEditMode });

    const bg = new Konva.Rect({ width: w, height: h, fill: "rgba(0,0,0,0.6)", stroke: "rgba(255,255,255,0.3)", strokeWidth: 1, cornerRadius: 4, name: "bg" });
    const valText = new Konva.Text({ text: "--", x: 5, y: 0, width: w - 10, height: h, fontSize: 16, fontFamily: "'IBM Plex Mono', monospace", fill: "#fff", align: "center", verticalAlign: "middle", name: "value" });
    const unitText = new Konva.Text({ text: el.unit || "", x: w - 30, y: h/2 - 6, fontSize: 10, fill: "rgba(255,255,255,0.5)", name: "unit" });
    const label = new Konva.Text({ text: el.label || "", x: 0, y: h + 3, width: w, fontSize: 9, fill: "rgba(255,255,255,0.6)", align: "center", name: "label" });

    group.add(bg, valText, unitText, label);

    group.on("click tap", () => {
        if (_hmiEditMode) return;
        if (typeof getRole === "function" && getRole() === "monitor") { toast("Write not allowed for monitor role", "error"); return; }
        const deviceName = hmiGetElementDeviceName(el);
        const dev = deviceName ? _hmiDeviceMap[deviceName] : null;
        if (!dev || !dev.allowWrite) { toast("Write not allowed", "error"); return; }
        const input = prompt("Enter value for " + (el.label || el.tag) + (el.unit ? " (" + el.unit + ")" : "") + ":");
        if (input === null) return;
        let val = parseFloat(input);
        if (isNaN(val)) { toast("Invalid number", "error"); return; }
        if (el.min != null && val < el.min) { toast("Minimum is " + el.min, "error"); return; }
        if (el.max != null && val > el.max) { toast("Maximum is " + el.max, "error"); return; }
        if (!confirm("Write " + val + " to " + el.tag + "?")) return;
        api("/api/live/write", "POST", { device: deviceName, tag: el.tag, value: val })
            .then(d => toast(d.message))
            .catch(e => toast(e.message, "error"));
    });
    return group;
}

function hmiUpdateNumericInput(node, el, value) {
    const valText = node.findOne(".value");
    if (!valText) return;
    valText.text(value != null ? (typeof value === "number" ? value.toFixed(1) : String(value)) : "--");
    node.getLayer()?.batchDraw();
}

function hmiDeselectElement() {
    _hmiSelectedElement = null;
    _hmiSelectedNode = null;
    if (_hmiTransformer) {
        _hmiTransformer.nodes([]);
        _hmiTransformer.visible(false);
        _hmiUiLayer.draw();
    }
    const props = document.getElementById("hmiProperties");
    if (props) props.style.display = "none";
}

// =============================================
// Live Data (Task 12)
// =============================================

function hmiStartLiveUpdates() {
    if (_hmiReplayMode) return;
    hmiStopLiveUpdates();
    hmiRefreshLiveData();
    _hmiRefreshInterval = setInterval(hmiRefreshLiveData, 2000);
}

async function hmiRefreshLiveData() {
    if (_hmiReplayMode) return;
    try {
        const [data, alarmData] = await Promise.all([
            api("/api/live/read"),
            api("/api/alarms").catch(() => ({ active: {} }))
        ]);
        _hmiDeviceMap = {};
        for (const dev of data.devices || []) {
            _hmiDeviceMap[dev.name] = {
                tags: {},
                allowWrite: dev.allowWrite,
                status: dev.status
            };
            for (const tag of dev.tags || []) {
                _hmiDeviceMap[dev.name].tags[tag.alias] = tag;
            }
        }
        _hmiActiveAlarms = alarmData.active || {};
        hmiUpdateAllElements();
    } catch {}
}

function hmiUpdateAllElements() {
    const equip = hmiGetCurrentEquipment();
    if (!equip || !equip.screen) return;

    for (const el of equip.screen.elements) {
        const dev = hmiGetElementDevice(el);
        if (!dev) { hmiUpdateElementValue(el, null); continue; }
        let tagData = null;
        if (el.tag) {
            tagData = dev.tags[el.tag] || null;
        }
        // Special case: pipe uses flowActive instead of tag
        if (el.type === "pipe" && el.flowActive) {
            tagData = dev.tags[el.flowActive] || null;
            const node = _hmiElementsLayer ? _hmiElementsLayer.findOne("#" + el.id) : null;
            if (node) hmiUpdatePipe(node, el, tagData ? tagData.value : null);
            // fall through so hmiUpdateElementValue still runs for alarm highlight
        }
        hmiUpdateElementValue(el, tagData);
    }
    hmiEnsureReplayButton();
}

function hmiStopLiveUpdates() {
    if (_hmiRefreshInterval) { clearInterval(_hmiRefreshInterval); _hmiRefreshInterval = null; }
    // Stop all running animations to prevent memory leaks
    if (_hmiElementsLayer) {
        _hmiElementsLayer.find("*").forEach(node => {
            if (node.getAttr("_rotAnim"))  { node.getAttr("_rotAnim").stop();  }
            if (node.getAttr("_blinkAnim")){ node.getAttr("_blinkAnim").stop(); }
            if (node.getAttr("_flowAnim")) { node.getAttr("_flowAnim").stop();  }
        });
    }
}

// =============================================
// Fullscreen (Task 13)
// =============================================

function hmiFullscreen() {
    const canvasContainer = document.getElementById("hmiCanvasContainer");
    if (!canvasContainer || !_hmiStage) return;

    const overlay = document.createElement("div");
    overlay.className = "hmi-fullscreen";
    overlay.id = "hmiFullscreenOverlay";

    const exitBtn = document.createElement("button");
    exitBtn.className = "hmi-fullscreen-exit";
    exitBtn.textContent = "Exit Fullscreen (ESC)";
    exitBtn.onclick = hmiExitFullscreen;

    // Move stage container to overlay
    overlay.appendChild(exitBtn);
    overlay.appendChild(canvasContainer);
    document.body.appendChild(overlay);

    // Resize to fill screen
    setTimeout(() => {
        const equip = hmiGetCurrentEquipment();
        if (!equip || !equip.screen) return;
        const scaleX = window.innerWidth / equip.screen.width;
        const scaleY = window.innerHeight / equip.screen.height;
        const scale = Math.min(scaleX, scaleY);
        _hmiStage.width(window.innerWidth);
        _hmiStage.height(window.innerHeight);
        _hmiStage.scale({ x: scale, y: scale });
        // Center
        _hmiStage.position({
            x: (window.innerWidth - equip.screen.width * scale) / 2,
            y: (window.innerHeight - equip.screen.height * scale) / 2
        });
    }, 50);

    document.addEventListener("keydown", hmiEscHandler);
}

function hmiExitFullscreen() {
    const overlay = document.getElementById("hmiFullscreenOverlay");
    const canvasContainer = document.getElementById("hmiCanvasContainer");
    if (!overlay) return;

    // Move canvas back to wrapper
    const wrapper = document.querySelector(".hmi-canvas-wrapper");
    if (wrapper && canvasContainer) {
        wrapper.insertBefore(canvasContainer, wrapper.firstChild);
    }
    overlay.remove();
    document.removeEventListener("keydown", hmiEscHandler);

    // Restore scale
    hmiScaleToFit();
}

function hmiEscHandler(e) {
    if (e.key === "Escape") hmiExitFullscreen();
}

// =============================================
// Auto-save
// =============================================

function hmiAutoSave() {
    clearTimeout(_hmiSaveTimeout);
    _hmiSaveTimeout = setTimeout(async () => {
        try {
            await api("/api/hmi/config", "PUT", _hmiConfig);
        } catch {}
    }, 500);
}

// Auto-open HMI screen from URL parameter
(function() {
    const params = new URLSearchParams(window.location.search);
    const hmiEquipId = params.get("hmi");
    if (hmiEquipId) {
        // Wait for page load then navigate
        setTimeout(async () => {
            try {
                const config = await api("/api/hmi/config");
                _hmiConfig = config;
                // Find equipment and navigate
                for (const plant of config.plants || []) {
                    for (const area of plant.areas || []) {
                        for (const equip of area.equipment || []) {
                            if (equip.id === hmiEquipId) {
                                _hmiCurrentPlant = plant.id;
                                _hmiCurrentArea = area.id;
                                _hmiCurrentEquipment = equip.id;
                                showTab("hmi");
                                hmiShowScreen();
                                setTimeout(hmiFullscreen, 500);
                                return;
                            }
                        }
                    }
                }
            } catch {}
        }, 1000);
    }
})();

// =============================================
// Replay / Time Travel (Phase 1)
// =============================================

function hmiEnsureReplayButton() {
    const toolbar = document.getElementById("hmiToolbar");
    if (!toolbar) return;
    if (toolbar.querySelector(".hmi-replay-toolbar-btn")) return;
    toolbar.style.display = "";
    const btn = document.createElement("button");
    btn.className = "hmi-replay-toolbar-btn" + (_hmiReplayMode ? " replay-active" : "");
    btn.onclick = hmiToggleReplay;
    btn.title = "Replay / Time Travel";
    btn.innerHTML = "&#x1F553; Replay";
    toolbar.appendChild(btn);
}

function hmiToggleReplay() {
    if (_hmiReplayMode) {
        hmiExitReplay();
    } else {
        hmiEnterReplayPanel();
    }
}

function hmiEnterReplayPanel() {
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

    const now = new Date();
    const oneHourAgo = new Date(now.getTime() - 3600000);
    const fmt = (d) => d.toISOString().slice(0, 16);

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
            <button style="background:#c8102e;color:#fff;border:1px solid #c8102e;padding:4px 14px;border-radius:6px;cursor:pointer;font-weight:600" onclick="hmiReplayGo()">Load</button>
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
        if (snBtn) snBtn.classList.add("active");
        if (dvrBtn) dvrBtn.classList.remove("active");
        if (endLabel) endLabel.style.display = "none";
        if (endInput) endInput.style.display = "none";
        if (stepSelect) stepSelect.style.display = "none";
    } else {
        if (dvrBtn) dvrBtn.classList.add("active");
        if (snBtn) snBtn.classList.remove("active");
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
        const resp = await apiFetch(`/api/replay/snapshot?device=${encodeURIComponent(device)}&timestamp=${encodeURIComponent(ts)}`);
        const data = await resp.json();
        _hmiReplayMode = true;
        hmiStopLiveUpdates();
        hmiShowReplayBanner(data.actual_timestamp || ts);
        hmiReplayInjectTags(device, data.tags);
        hmiUpdateAllElements();
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
        const resp = await apiFetch(`/api/replay/range?device=${encodeURIComponent(device)}&start=${encodeURIComponent(startTs)}&end=${encodeURIComponent(endTs)}&step=${encodeURIComponent(step)}`);
        const data = await resp.json();

        if (!data.frames || data.frames.length === 0) {
            alert("No data found for the selected range");
            return;
        }

        _hmiReplayMode = true;
        _hmiReplayFrames = data.frames;
        _hmiReplayIndex = 0;
        _hmiReplaySpeed = 1;
        _hmiReplayPlaying = false;
        hmiStopLiveUpdates();

        const transport = document.getElementById("replayTransport");
        if (transport) transport.style.display = "flex";
        const scrubber = document.getElementById("replayScrubber");
        if (scrubber) {
            scrubber.max = _hmiReplayFrames.length - 1;
            scrubber.value = 0;
        }

        hmiShowReplayBanner(_hmiReplayFrames[0].timestamp);
        hmiReplayShowFrame(0, device);

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

    const banner = document.getElementById("hmiReplayBanner");
    if (banner) banner.remove();

    const bar = document.getElementById("hmiReplayBar");
    if (bar) bar.classList.remove("active");

    const btn = document.querySelector(".hmi-replay-toolbar-btn");
    if (btn) btn.classList.remove("replay-active");

    hmiStartLiveUpdates();
}

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
    if (btn) btn.innerHTML = "&#x23F8;";

    const intervalMs = Math.max(100, 2000 / _hmiReplaySpeed);

    if (_hmiReplayInterval) clearInterval(_hmiReplayInterval);
    _hmiReplayInterval = setInterval(() => {
        if (_hmiReplayIndex < _hmiReplayFrames.length - 1) {
            hmiReplayShowFrame(_hmiReplayIndex + 1);
        } else {
            hmiReplayPause();
        }
    }, intervalMs);
}

function hmiReplayPause() {
    _hmiReplayPlaying = false;
    if (_hmiReplayInterval) { clearInterval(_hmiReplayInterval); _hmiReplayInterval = null; }
    const btn = document.getElementById("replayPlayBtn");
    if (btn) btn.innerHTML = "&#x25B6;";
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

    if (_hmiReplayPlaying) {
        hmiReplayPause();
        hmiReplayPlay();
    }
}

function hmiReplayScrub(value) {
    hmiReplayPause();
    hmiReplayShowFrame(parseInt(value, 10));
}
