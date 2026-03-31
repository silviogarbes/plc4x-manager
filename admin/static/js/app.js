/**
 * PLC4X Manager - Frontend
 * Licensed under Apache License 2.0
 */

let config = {};
let templates = [];
let editingDevice = null;
let editingUser = null;
let dashboardInterval = null;
let _dashboardGeneration = 0;
let _liveReadInFlight = false;
let _lastDashboardData = null;

// =============================================
// WebSocket (real-time data)
// =============================================

let _ws = null;
let _wsReconnectTimer = null;
let _wsReconnectAttempts = 0;
let _wsConnected = false;
let _restFallbackTimer = null;

function wsConnect() {
    const token = getToken();
    if (!token) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws/live?token=${encodeURIComponent(token)}`;

    try { _ws = new WebSocket(url); } catch { return; }

    _ws.onopen = () => {
        _wsConnected = true;
        _wsReconnectAttempts = 0;
        console.log("[WS] Connected");
        // Stop REST polling now that WS is live
        clearTimeout(_restFallbackTimer);
        _restFallbackTimer = null;
        stopLiveRefresh();
        _hideStaleIndicator();
    };

    _ws.onmessage = (evt) => {
        try {
            const data = JSON.parse(evt.data);
            _handleWsMessage(data);
        } catch {}
    };

    _ws.onclose = () => {
        _wsConnected = false;
        _wsReconnectAttempts++;
        _showStaleIndicator();
        // Fall back to REST polling after 5 seconds if WS is still down
        _restFallbackTimer = setTimeout(() => {
            if (!_wsConnected) {
                console.log("[WS] Fallback to REST polling");
                startLiveRefresh();
            }
        }, 5000);
        // Exponential backoff with 5s jitter
        const delay = Math.min(30000, 3000 * Math.pow(1.5, _wsReconnectAttempts - 1))
            + Math.random() * 5000;
        console.log(`[WS] Reconnecting in ${Math.round(delay)}ms (attempt ${_wsReconnectAttempts})`);
        _wsReconnectTimer = setTimeout(wsConnect, delay);
    };

    _ws.onerror = () => { if (_ws) _ws.close(); };
}

function _handleWsMessage(data) {
    // Cache in localStorage for offline resilience
    if (data.type === "mqtt") {
        localStorage.setItem("plc4x_last_ws", JSON.stringify({data, ts: Date.now()}));
    }
    if (data.type === "alarm_sync" || (data.type === "mqtt" && data.topic && data.topic.includes("_alarms"))) {
        _updateAlarmBadgeFromWs(data);
    }
    if (data.type === "chat_notification" && data.notification) {
        console.log("[WS] Chat notification received:", data.notification.title);
        if (typeof ChatWidget !== "undefined" && ChatWidget.onNotification) {
            ChatWidget.onNotification(data.notification);
        } else {
            console.warn("[WS] ChatWidget not ready for notification");
        }
    }
}

function _updateAlarmBadgeFromWs(data) {
    const badge = document.getElementById("alarmsActiveCount");
    if (badge && data.alarms) {
        badge.textContent = data.alarms.length;
    }
}

function _showStaleIndicator() {
    let banner = document.getElementById("staleBanner");
    if (!banner) {
        banner = document.createElement("div");
        banner.id = "staleBanner";
        banner.style.cssText = "position:fixed;top:0;left:0;right:0;background:#c8102e;color:white;text-align:center;padding:4px;font-size:12px;z-index:9999;display:none";
        document.body.prepend(banner);
    }
    banner.style.display = "block";
    banner.textContent = "Connection lost \u2014 showing cached data";
}

function _hideStaleIndicator() {
    const banner = document.getElementById("staleBanner");
    if (banner) banner.style.display = "none";
}


// =============================================
// Init
// =============================================

document.addEventListener("DOMContentLoaded", () => {
    // Check authentication before loading anything
    if (!getToken()) {
        window.location.href = "/login";
        return;
    }
    // Verify token is still valid
    api("/api/auth/verify").then(() => {
        updateAdminMenuVisibility();
        loadDashboard();
        loadServiceStatus();
        loadConfig();
        loadTemplates();
        loadStatus();
        setInterval(loadStatus, 10000);
        dashboardInterval = setInterval(loadDashboard, 10000);

        // Start WebSocket for real-time data push
        wsConnect();

        // Auto-start kiosk mode if ?kiosk in URL
        if (new URLSearchParams(window.location.search).has("kiosk")) {
            // Wait for HMI config to load
            setTimeout(() => {
                if (typeof loadHMI === "function") loadHMI().then(() => toggleKiosk()).catch(() => toggleKiosk());
                else toggleKiosk();
            }, 2000);
        }
    }).catch(() => {
        logout();
    });

    // Show username, role indicator, and logout button in header
    const user = sessionStorage.getItem("jwt_user") || "admin";
    const role = getRole();
    const headerUser = document.getElementById("headerUser");
    if (headerUser) {
        const plants = getUserPlants();
        const plantLabel = plants ? ` (${plants.join(", ")})` : "";
        headerUser.innerHTML = `<span>${escHtml(user)}</span><span class="header-role-badge header-role-${escHtml(role)}">${escHtml(role)}${escHtml(plantLabel)}</span>
            <button class="btn btn-outline btn-sm" id="kioskBtn" onclick="toggleKiosk()" title="Kiosk Mode — auto-cycle screens for TV monitors">Kiosk</button>
            <button class="header-btn" onclick="logout()">Logout</button>`;
    }

    // Hide admin-only tabs for operators
    const adminTabs = ["devices", "server", "security", "backups", "apidocs"];
    if (!isAdmin()) {
        adminTabs.forEach(t => {
            const btn = document.querySelector(`.tab[onclick*="'${t}'"]`);
            if (btn) btn.style.display = "none";
        });
    }

    // Listen for port changes to update preview
    const portInput = document.getElementById("serverPort");
    if (portInput) portInput.addEventListener("input", updateEndpointPreview);
});

// =============================================
// Authentication
// =============================================

function getToken() {
    return sessionStorage.getItem("jwt_token");
}

function getRole() { return sessionStorage.getItem("jwt_role") || "admin"; }
function isAdmin() { return getRole() === "admin"; }
function canWrite() { return getRole() !== "monitor"; }
function getUserPlants() {
    const p = sessionStorage.getItem("jwt_plants");
    return p ? JSON.parse(p) : null;
}

function logout() {
    sessionStorage.removeItem("jwt_token");
    sessionStorage.removeItem("jwt_user");
    sessionStorage.removeItem("jwt_role");
    sessionStorage.removeItem("jwt_plants");
    window.location.href = "/login";
}

// =============================================
// API Helpers
// =============================================

async function api(url, method = "GET", body = null) {
    const token = getToken();
    const opts = {
        method,
        headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${token}`
        }
    };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);

    // Redirect to login on authentication failure
    if (res.status === 401) {
        logout();
        throw new Error("Session expired");
    }

    let data;
    try { data = await res.json(); } catch { data = {}; }
    if (!res.ok) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
    return data;
}

async function apiFetch(url, opts = {}) {
    const token = getToken();
    if (!opts.headers) opts.headers = {};
    if (!opts.headers["Authorization"]) opts.headers["Authorization"] = `Bearer ${token}`;
    if (!opts.headers["Content-Type"] && opts.body) opts.headers["Content-Type"] = "application/json";
    const res = await fetch(url, opts);
    if (res.status === 401) { logout(); throw new Error("Session expired"); }
    return res;
}

function toast(message, type = "success") {
    const container = document.getElementById("toastContainer");
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => el.remove(), 4000);
}

// =============================================
// Tabs
// =============================================

function toggleMoreMenu(evt) {
    if (evt) evt.stopPropagation();
    const menu = document.getElementById("moreMenu");
    if (menu) menu.classList.toggle("open");
}

// Close More menu when clicking outside
document.addEventListener("click", function(e) {
    const menu = document.getElementById("moreMenu");
    if (menu && menu.classList.contains("open")) {
        if (!e.target.closest(".tab-more-wrapper")) {
            menu.classList.remove("open");
        }
    }
});

function showTabFromMore(name, evt) {
    // Close the dropdown
    const menu = document.getElementById("moreMenu");
    if (menu) menu.classList.remove("open");

    // Clear active state from all More items and set on clicked
    document.querySelectorAll(".tab-more-item").forEach(el => el.classList.remove("active-more"));
    if (evt && evt.target) evt.target.classList.add("active-more");

    // Highlight the "More" button to show a sub-tab is active
    const moreBtn = document.querySelector(".tab-more-btn");
    if (moreBtn) { moreBtn.classList.add("active"); moreBtn.textContent = "More \u25BE"; }

    showTab(name, { target: moreBtn });
}

function updateAdminMenuVisibility() {
    const isAdmin = (getRole() === "admin");
    document.querySelectorAll(".tab-admin-only").forEach(el => {
        el.style.display = isAdmin ? "" : "none";
    });
    // Hide the "Administration" group label if not admin
    document.querySelectorAll(".tab-more-group-label").forEach(el => {
        if (el.textContent.trim() === "Administration") {
            el.style.display = isAdmin ? "" : "none";
        }
    });
}

function showTab(name, evt) {
    document.querySelectorAll(".tab-content").forEach(el => el.style.display = "none");
    document.querySelectorAll(".tab").forEach(el => el.classList.remove("active"));
    document.querySelectorAll(".tab-more-item").forEach(el => el.classList.remove("active-more"));
    document.getElementById(`tab-${name}`).style.display = "block";
    if (evt && evt.target) evt.target.classList.add("active");

    // Clear alarm polling when leaving alarms tab
    if (typeof _alarmRefreshInterval !== "undefined" && _alarmRefreshInterval) {
        clearInterval(_alarmRefreshInterval); _alarmRefreshInterval = null;
    }

    // Stop live data interval when leaving live tab
    if (name !== "live") stopLiveRefresh();

    if (name === "dashboard") { loadDashboard(); loadServiceStatus(); }
    if (name === "live") loadLiveData();
    if (name === "backups") loadBackups();
    if (name === "hmi") loadHMI();
    if (name === "alarms") loadAlarms();
    if (name === "oee") loadOEE();
    if (name === "audit") loadAudit();
    if (name === "logbook") {
        loadLogbook();
        const newEntryBtn = document.getElementById("logbookNewEntryBtn");
        if (newEntryBtn) newEntryBtn.style.display = canWrite() ? "" : "none";
    }
    if (name === "analytics") loadGrafanaDashboard();
    if (name === "ml") loadML();
    if (name === "maintenance") loadMaintenance();
    if (name === "reports") loadReportFilters();
    if (name === "apidocs") loadSwaggerUI();
    if (name === "logs") loadLogs();
    if (name === "server") {
        loadManagerVersion();
        loadPlc4xVersion();
        // PLC Connection Safety card — admin only
        const plcSafetyCard = document.getElementById("plcSafetyCard");
        if (plcSafetyCard) plcSafetyCard.style.display = isAdmin() ? "" : "none";
    }
    if (name === "security") { loadSecurityStatus(); loadUsers(); loadCertificates(); loadAdminInfo(); }
}

// =============================================
// Services Status
// =============================================

async function loadServiceStatus() {
    const container = document.getElementById("servicesContent");
    if (!container) return;
    try {
        const services = await api("/api/services/status");
        const host = location.hostname;
        let html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px">';
        for (const svc of services) {
            const isOnline = svc.status === "online";
            const badge = isOnline ? "badge-ok" : "badge-err";
            const displayUrl = svc.url ? svc.url.replace("<host>", host) : null;
            const portStr = svc.port ? `:${svc.port}` : "";

            // Only HTTP/HTTPS URLs get an "Open" link
            let openLink = "";
            if (svc.port && (svc.url || "").startsWith("http")) {
                const href = location.protocol + "//" + host + ":" + svc.port;
                openLink = `<a href="${href}" target="_blank" style="color:var(--primary);font-weight:500">Open</a>`;
            }

            // Copyable endpoint for non-HTTP services
            let copyBtn = "";
            if (displayUrl && !(svc.url || "").startsWith("http")) {
                copyBtn = `<button class="btn btn-outline btn-sm" style="font-size:0.65rem;padding:1px 6px" onclick="navigator.clipboard.writeText('${escAttr(displayUrl)}');this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',1500)">Copy</button>`;
            }

            html += `<div style="padding:12px 14px;border:1px solid var(--border);border-radius:var(--radius-sm);border-left:3px solid ${isOnline ? 'var(--success)' : 'var(--danger)'}">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                    <strong style="font-size:0.88rem">${escHtml(svc.name)}</strong>
                    <span class="status-badge ${badge}" style="font-size:0.7rem">${isOnline ? 'Online' : 'Offline'}</span>
                </div>
                <div class="text-muted" style="font-size:0.78rem;margin-bottom:6px">${escHtml(svc.description)}</div>
                <div style="display:flex;gap:8px;align-items:center;font-size:0.75rem;flex-wrap:wrap">
                    ${displayUrl ? `<code style="color:var(--text-secondary);font-size:0.72rem">${escHtml(displayUrl)}</code>` : `<code style="color:var(--text-muted)">${escHtml(svc.container)}</code>`}
                    ${openLink}${copyBtn}
                </div>
            </div>`;
        }
        html += '</div>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = `<p class="text-muted">Unable to load service status</p>`;
    }
}

// =============================================
// Dashboard
// =============================================

function dashboardDeviceStatus(devStatus) {
    const map = {
        online:   { label: "Online",   cls: "badge-ok",   dot: "running" },
        error:    { label: "Error",    cls: "badge-err",  dot: "stopped" },
        disabled: { label: "Disabled", cls: "badge-muted", dot: "unknown" },
        no_tags:  { label: "No Tags",  cls: "badge-warn", dot: "unknown" },
        unknown:  { label: "Offline",  cls: "badge-muted", dot: "unknown" },
    };
    return map[devStatus] || map.unknown;
}

function renderDashboard(devices, status, liveMap, alarmData) {
    const totalDevices = devices.length;
    const hasLive = Object.keys(liveMap).length > 0;
    let totalTags = 0, onlineCount = 0, errorCount = 0;
    devices.forEach(d => {
        totalTags += (d.tags || []).length + (d.calculatedTags || []).length;
        const live = liveMap[d.name];
        if (live) {
            if (live.status === "online") onlineCount++;
            else if (live.status === "error") errorCount++;
        }
    });

    const activeAlarms = alarmData ? Object.values(alarmData.active || {}) : [];
    const unackAlarms = activeAlarms.filter(a => !a.acknowledged).length;
    const criticalAlarms = activeAlarms.filter(a => a.severity === "critical").length;

    const serverState = status.running ? "Running" : "Stopped";
    const serverClass = status.running ? "running" : "stopped";

    const summaryBar = `
        <div style="display:flex;gap:16px;margin-bottom:24px;padding:16px;background:var(--bg-card,#fff);border-radius:8px;border:1px solid var(--border,#e0e0e0);flex-wrap:wrap">
            <div style="text-align:center;flex:1;min-width:70px">
                <div style="font-size:26px;font-weight:700"><span class="status-dot ${serverClass}" style="display:inline-block;margin-right:4px"></span>${escHtml(serverState)}</div>
                <div style="font-size:12px;color:var(--text-secondary)">Server</div>
            </div>
            <div style="text-align:center;flex:1;min-width:70px">
                <div style="font-size:26px;font-weight:700">${totalDevices}</div>
                <div style="font-size:12px;color:var(--text-secondary)">Devices</div>
            </div>
            <div style="text-align:center;flex:1;min-width:70px">
                <div style="font-size:26px;font-weight:700;color:var(--success)">${hasLive ? onlineCount : "-"}</div>
                <div style="font-size:12px;color:var(--text-secondary)">Online</div>
            </div>
            <div style="text-align:center;flex:1;min-width:70px">
                <div style="font-size:26px;font-weight:700;color:var(--danger)">${hasLive ? errorCount : "-"}</div>
                <div style="font-size:12px;color:var(--text-secondary)">Error</div>
            </div>
            <div style="text-align:center;flex:1;min-width:70px">
                <div style="font-size:26px;font-weight:700">${totalTags}</div>
                <div style="font-size:12px;color:var(--text-secondary)">Tags</div>
            </div>
            <div style="text-align:center;flex:1;min-width:70px">
                <div style="font-size:26px;font-weight:700;color:${criticalAlarms > 0 ? "var(--danger)" : unackAlarms > 0 ? "var(--warning)" : "var(--success)"}">${activeAlarms.length}</div>
                <div style="font-size:12px;color:var(--text-secondary)">Alarms${unackAlarms > 0 ? ` (${unackAlarms})` : ""}</div>
            </div>
        </div>
    `;

    // Pre-group alarms by device
    const alarmsByDevice = {};
    activeAlarms.forEach(a => {
        if (!alarmsByDevice[a.device]) alarmsByDevice[a.device] = [];
        alarmsByDevice[a.device].push(a);
    });

    // Group devices by plant
    const plantMap = {};
    devices.forEach(d => {
        const plant = d.plant || "Default";
        if (!plantMap[plant]) plantMap[plant] = [];
        plantMap[plant].push(d);
    });
    const plantNames = Object.keys(plantMap).sort();

    let plantsHtml = "";
    if (totalDevices === 0) {
        plantsHtml = '<p class="text-muted" style="text-align:center;padding:32px">No devices configured yet.</p>';
    } else {
        for (const plantName of plantNames) {
            const plantDevices = plantMap[plantName];
            let pOnline = 0, pError = 0, pTags = 0, pAlarms = 0;
            plantDevices.forEach(d => {
                pTags += (d.tags || []).length + (d.calculatedTags || []).length;
                const live = liveMap[d.name];
                if (live) {
                    if (live.status === "online") pOnline++;
                    else if (live.status === "error") pError++;
                }
                pAlarms += (alarmsByDevice[d.name] || []).length;
            });

            const plantStatus = pError > 0 ? "badge-error" : pOnline > 0 ? "badge-ok" : "badge-warn";
            const plantStatusLabel = pError > 0 ? `${pError} Error` : pOnline > 0 ? `${pOnline} Online` : "Offline";

            plantsHtml += `<div style="border:1px solid var(--border);border-radius:8px;margin-bottom:16px;overflow:hidden">
                <div style="padding:12px 16px;background:var(--bg-secondary);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
                    <div style="display:flex;align-items:center;gap:10px">
                        <strong style="font-size:1.1rem">${escHtml(plantName)}</strong>
                        <span class="status-badge ${plantStatus}">${plantStatusLabel}</span>
                        ${pAlarms > 0 ? `<span class="status-badge badge-error">${pAlarms} alarm${pAlarms > 1 ? "s" : ""}</span>` : ""}
                    </div>
                    <span class="text-muted" style="font-size:0.85rem">${plantDevices.length} device${plantDevices.length > 1 ? "s" : ""} · ${pTags} tags</span>
                </div>
                <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;padding:12px 16px">`;

            for (const d of plantDevices) {
                const live = liveMap[d.name];
                const devStatus = live ? live.status : "unknown";
                const st = dashboardDeviceStatus(devStatus);
                let tagsOk = 0, tagsErr = 0;
                if (live && live.tags) {
                    live.tags.forEach(t => {
                        if (t.status === "ok") tagsOk++;
                        else if (t.status === "read_error") tagsErr++;
                    });
                }
                const totalDevTags = (d.tags || []).length + (d.calculatedTags || []).length;
                const tagSummary = live
                    ? `<span style="color:var(--success)">${tagsOk} ok</span>${tagsErr ? ` <span style="color:var(--danger)">${tagsErr} err</span>` : ""}`
                    : `${totalDevTags} configured`;

                const latency = live && live.read_latency_ms ? `${live.read_latency_ms}ms` : "-";
                const devAlarms = alarmsByDevice[d.name] || [];
                const hasOee = d.oeeConfig && d.oeeConfig.enabled;

                plantsHtml += `<div class="plc-card" style="padding:12px 14px">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                        <strong>${escHtml(d.name)}</strong>
                        <span class="status-badge ${st.cls}">${st.label}</span>
                    </div>
                    <div class="text-muted" style="font-family:var(--font-mono);font-size:10px;word-break:break-all;margin-bottom:4px">${escHtml(d.connectionString)}</div>
                    <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-secondary)">
                        <span>Tags: ${tagSummary}</span>
                        <span>Latency: ${latency}</span>
                    </div>`;

                if (devAlarms.length > 0) {
                    plantsHtml += `<div style="margin-top:6px;display:flex;gap:4px;flex-wrap:wrap">`;
                    for (const a of devAlarms.slice(0, 3)) {
                        const cls = a.severity === "critical" ? "badge-error" : "badge-warn";
                        plantsHtml += `<span class="status-badge ${cls}" style="font-size:0.65rem">${escHtml(a.tag)}: ${escHtml(String(a.value))}</span>`;
                    }
                    if (devAlarms.length > 3) plantsHtml += `<span class="text-muted" style="font-size:0.65rem">+${devAlarms.length - 3} more</span>`;
                    plantsHtml += `</div>`;
                }

                if (hasOee) {
                    plantsHtml += `<div style="margin-top:6px;font-size:11px;color:var(--text-secondary)" id="dash-oee-${d.name.replace(/[^a-zA-Z0-9]/g,"_")}">OEE: loading...</div>`;
                }

                plantsHtml += `</div>`;
            }
            plantsHtml += `</div></div>`;
        }
    }

    const container = document.getElementById("dashboardContent");
    if (container) container.innerHTML = summaryBar + plantsHtml;

    // Load OEE for devices that have it configured (async, non-blocking)
    devices.filter(d => d.oeeConfig && d.oeeConfig.enabled).forEach(d => {
        const el = document.getElementById(`dash-oee-${d.name.replace(/[^a-zA-Z0-9]/g,"_")}`);
        if (!el) return;
        api(`/api/oee/calculate?device=${encodeURIComponent(d.name)}&hours=24`).then(data => {
            const c = v => Math.max(0, Math.min(100, Math.round((v || 0) * 100)));
            const pct = c(data.oee);
            const color = pct >= 85 ? "var(--success)" : pct >= 60 ? "var(--warning)" : "var(--danger)";
            el.innerHTML = `OEE: <strong style="color:${color}">${pct}%</strong> (A:${c(data.availability)}% P:${c(data.performance)}% Q:${c(data.quality)}%)`;
        }).catch(() => { el.textContent = "OEE: —"; });
    });
}

function reRenderDashboard() {
    if (_lastDashboardData) {
        const { devices, status, liveMap, alarmData } = _lastDashboardData;
        renderDashboard(devices, status, liveMap, alarmData);
    }
}

async function loadDashboard() {
    const gen = ++_dashboardGeneration;
    try {
        const [devices, status, alarmData] = await Promise.all([
            api("/api/devices"),
            api("/api/server/status"),
            api("/api/alarms").catch(() => ({ active: {}, history: [] }))
        ]);
        if (gen !== _dashboardGeneration) return;

        _lastDashboardData = { devices, status, alarmData, liveMap: _lastDashboardData?.liveMap || {} };
        renderDashboard(devices, status, _lastDashboardData.liveMap, alarmData);

        if (status.running && !_liveReadInFlight) {
            _liveReadInFlight = true;
            api("/api/live/read").then(liveData => {
                if (gen !== _dashboardGeneration) return;
                if (liveData && liveData.devices) {
                    const liveMap = {};
                    liveData.devices.forEach(d => { liveMap[d.name] = d; });
                    _lastDashboardData = { ..._lastDashboardData, liveMap };
                    renderDashboard(devices, status, liveMap, alarmData);
                }
            }).catch(() => {}).finally(() => { _liveReadInFlight = false; });
        }
    } catch (e) {
        if (gen !== _dashboardGeneration) return;
        const container = document.getElementById("dashboardContent");
        if (container) {
            container.innerHTML = `<p style="color:var(--danger,red)">Error loading dashboard: ${escHtml(e.message)}</p>`;
        }
    }
}

// =============================================
// Live OPC-UA Data (Supervisory View)
// =============================================

let liveInterval = null;
let liveRefreshMs = 5000;
const liveExpandedDevices = new Set();
let _liveDeviceNames = [];
let _liveFirstLoad = true;

function toggleLivePanel(name) {
    if (liveExpandedDevices.has(name)) {
        liveExpandedDevices.delete(name);
    } else {
        liveExpandedDevices.add(name);
    }
    loadLiveData();
}

function liveExpandAll() {
    _liveDeviceNames.forEach(name => liveExpandedDevices.add(name));
    loadLiveData();
}

function liveCollapseAll() {
    liveExpandedDevices.clear();
    loadLiveData();
}

async function loadLiveData() {
    const container = document.getElementById("liveContent");
    if (!container) return;

    try {
        const data = await api("/api/live/read");

        // Cache device names for expand/collapse all
        if (data.devices) {
            _liveDeviceNames = data.devices.map(d => d.name);
        }

        // Auto-expand online devices only on first load
        if (_liveFirstLoad && data.devices) {
            _liveFirstLoad = false;
            data.devices.forEach(d => { if (d.status === "online") liveExpandedDevices.add(d.name); });
        }

        let html = "";

        if (data.error) {
            html += `<div class="live-error-banner">${escHtml(data.error)}</div>`;
        }

        if (data.devices && data.devices.length > 0) {
            // Summary bar with counts and expand/collapse buttons
            const online = data.devices.filter(d => d.status === "online").length;
            const disabled = data.devices.filter(d => d.status === "disabled").length;
            const errors = data.devices.filter(d => d.status === "error").length;
            html += `<div style="display:flex;gap:16px;margin-bottom:12px;font-size:13px;flex-wrap:wrap;align-items:center">
                <span style="color:var(--success)">Online: ${online}</span>
                <span style="color:var(--danger)">Error: ${errors}</span>
                <span style="color:var(--text-muted)">Disabled: ${disabled}</span>
                <span style="flex:1"></span>
                <button class="btn btn-outline btn-sm" onclick="liveExpandAll()">Expand All</button>
                <button class="btn btn-outline btn-sm" onclick="liveCollapseAll()">Collapse All</button>
            </div>`;

            for (const dev of data.devices) {
                const statusMap = {
                    online: { cls: "online", label: "Online" },
                    error: { cls: "error", label: "Error" },
                    disabled: { cls: "unknown", label: "Disabled" }
                };
                const st = statusMap[dev.status] || { cls: "unknown", label: dev.status };
                const isDisabled = dev.status === "disabled";
                const isExpanded = liveExpandedDevices.has(dev.name);
                const tagCount = (dev.tags || []).length;
                const okCount = (dev.tags || []).filter(t => t.status === "ok").length;
                const arrow = isExpanded ? "&#x25BE;" : "&#x25B8;";

                html += `<div class="live-device${isDisabled ? ' device-disabled' : ''}" data-live-device="${escAttr(dev.name)}" style="margin-bottom:16px">
                    <div class="live-device-header" onclick="toggleLivePanel('${escAttr(dev.name)}')" style="cursor:pointer;user-select:none">
                        <div style="display:flex;align-items:center;gap:10px;flex:1;min-width:0">
                            <span style="font-size:14px;width:14px;text-align:center">${arrow}</span>
                            <div style="min-width:0">
                                <span style="font-weight:600;font-size:14px">${escHtml(dev.name)}</span>
                                <span class="live-device-conn" style="margin-left:8px">${escHtml(dev.connectionString)}</span>
                            </div>
                        </div>
                        <div style="display:flex;align-items:center;gap:10px;flex-shrink:0">
                            <span class="text-muted" style="font-size:12px">${okCount}/${tagCount} tags</span>
                            <span class="live-device-status ${st.cls}">${st.label}</span>
                        </div>
                    </div>`;

                if (isExpanded && dev.tags && dev.tags.length > 0) {
                    const plcTags = dev.tags.filter(t => !t.calculated);
                    const calcTags = dev.tags.filter(t => t.calculated);

                    html += `<div class="live-device-body">`;
                    if (plcTags.length > 0) {
                        html += `<div class="live-tags-grid">`;
                        for (const tag of plcTags) {
                            const isOk = tag.status === "ok";
                            const statusMsg = tag.status === "disabled" ? "Disabled" : tag.status === "read_error" ? "No data" : tag.status;
                            const valStr = isOk ? formatLiveValue(tag.value) : `<span class="error">${escHtml(statusMsg)}</span>`;
                            const timeStr = tag.timestamp ? new Date(tag.timestamp).toLocaleTimeString() : "";
                            const canWrite = isOk && !isDisabled && dev.allowWrite && getRole() !== "monitor";
                            html += `<div class="live-tag">
                                <span class="live-tag-label">Tag</span>
                                <span class="live-tag-name">${escHtml(tag.alias)}</span>
                                <span class="live-tag-label">Value</span>
                                <span class="live-tag-value${!isOk ? ' error' : ''}" onclick="showTagTrend('${escAttr(dev.name)}','${escAttr(tag.alias)}')" style="cursor:pointer" title="Click for trend">${valStr}</span>
                                <span class="live-tag-label">Address</span>
                                <span class="live-tag-address">${escHtml(tag.address)}</span>
                                ${timeStr ? `<span class="live-tag-label">Last Read</span><span class="live-tag-time">${timeStr}</span>` : ""}
                                ${canWrite ? `<button class="btn btn-outline btn-sm live-write-btn" data-device="${escAttr(dev.name)}" data-tag="${escAttr(tag.alias)}" data-value="${escAttr(String(tag.value ?? ''))}" onclick="openWriteDialog(this.dataset.device,this.dataset.tag,this.dataset.value)">Write</button>` : ""}
                            </div>`;
                        }
                        html += `</div>`;
                    }
                    if (calcTags.length > 0) {
                        html += `<div style="margin-top:16px;margin-bottom:8px;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--primary);font-weight:600">Calculated Tags</div>`;
                        html += `<div class="live-tags-grid">`;
                        for (const tag of calcTags) {
                            const isOk = tag.status === "ok";
                            const calcErr = tag.error || (tag.status === "disabled" ? "Device disabled" : tag.status);
                            const valStr = isOk ? formatLiveValue(tag.value) : `<span class="error">${escHtml(calcErr)}</span>`;
                            const timeStr = tag.timestamp ? new Date(tag.timestamp).toLocaleTimeString() : "";
                            html += `<div class="live-tag live-tag-calc">
                                <span class="live-tag-label">Tag</span>
                                <span class="live-tag-name"><span class="badge-calc">CALC</span>${escHtml(tag.alias)}</span>
                                <span class="live-tag-label">Value</span>
                                <span class="live-tag-value${!isOk ? ' error' : ''}" onclick="showTagTrend('${escAttr(dev.name)}','${escAttr(tag.alias)}')" style="cursor:pointer" title="Click for trend">${valStr}</span>
                                <span class="live-tag-label">Formula</span>
                                <span class="live-tag-address">${escHtml(tag.formula || tag.address)}</span>
                                ${timeStr ? `<span class="live-tag-label">Last Read</span><span class="live-tag-time">${timeStr}</span>` : ""}
                            </div>`;
                        }
                        html += `</div>`;
                    }
                    html += `</div>`;
                } else if (isExpanded) {
                    html += `<div class="live-device-body text-muted">No tags configured</div>`;
                }
                html += `</div>`;
            }
        } else if (!data.error) {
            html += `<p class="text-muted" style="text-align:center;padding:32px">No devices configured. Go to Devices tab to add PLCs.</p>`;
        }

        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = `<div class="live-error-banner">Failed to load live data: ${escHtml(e.message)}</div>`;
    }
}

function formatLiveValue(val) {
    if (val === null || val === undefined) return "-";
    if (typeof val === "number") {
        if (Number.isInteger(val)) return val.toLocaleString();
        // For very large/small numbers use exponential, otherwise fixed
        if (Math.abs(val) > 1e9 || (Math.abs(val) < 0.001 && val !== 0)) {
            return val.toExponential(3);
        }
        return val.toFixed(3);
    }
    if (typeof val === "boolean") return val ? "TRUE" : "FALSE";
    return escHtml(String(val));
}

function openWriteDialog(device, tag, currentValue) {
    const newVal = prompt(`Write to ${device} / ${tag}\n\nCurrent value: ${currentValue}\n\nEnter new value:`);
    if (newVal === null) return;
    if (!confirm(`WARNING: You are about to write to a PLC.\n\nDevice: ${device}\nTag: ${tag}\nNew value: ${newVal}\n\nThis action will change a real value on the physical device. Continue?`)) return;
    writeTagValue(device, tag, newVal);
}

async function writeTagValue(device, tag, value) {
    // Try to parse as number or boolean
    let parsed = value;
    if (value === "true" || value === "TRUE") parsed = true;
    else if (value === "false" || value === "FALSE") parsed = false;
    else if (!isNaN(value) && value.trim() !== "") {
        parsed = value.includes(".") ? parseFloat(value) : parseInt(value, 10);
    }

    try {
        const data = await api("/api/live/write", "POST", { device, tag, value: parsed });
        toast(data.message);
        loadLiveData();
    } catch (e) {
        toast(e.message, "error");
    }
}

function toggleLiveRefresh() {
    const checked = document.getElementById("liveAutoRefresh").checked;
    if (checked) {
        startLiveRefresh();
    } else {
        stopLiveRefresh();
    }
}

function setLiveInterval() {
    liveRefreshMs = parseInt(document.getElementById("liveInterval").value) || 5000;
    if (document.getElementById("liveAutoRefresh").checked) {
        stopLiveRefresh();
        startLiveRefresh();
    }
}

function startLiveRefresh() {
    stopLiveRefresh();
    liveInterval = setInterval(loadLiveData, liveRefreshMs);
}

function stopLiveRefresh() {
    if (liveInterval) {
        clearInterval(liveInterval);
        liveInterval = null;
    }
}

// =============================================
// Status
// =============================================

async function loadStatus() {
    try {
        const status = await api("/api/server/status");
        const dot = document.getElementById("statusDot");
        const text = document.getElementById("statusText");
        dot.className = `status-dot ${status.running ? "running" : "stopped"}`;
        text.textContent = status.running ? "Running" : status.status;
    } catch {
        document.getElementById("statusDot").className = "status-dot unknown";
        document.getElementById("statusText").textContent = "Unavailable";
    }
}

async function restartServer() {
    if (!confirm("Restart the PLC4X server?")) return;
    try {
        showVersionOverlay("Restarting server", "Sending restart command...", 1, 3, "&#128260;");
        await api("/api/server/restart", "POST");

        showVersionOverlay("Restarting server", "Stopping PLC4X server...", 2, 3, "&#9203;");
        await new Promise(r => setTimeout(r, 5000));

        showVersionOverlay("Restarting server", "Starting PLC4X server...", 3, 3, "&#9203;");
        // Poll status until running or timeout
        let running = false;
        for (let i = 0; i < 12; i++) {
            await new Promise(r => setTimeout(r, 2500));
            try {
                const status = await api("/api/server/status");
                if (status.running) { running = true; break; }
            } catch {}
        }

        hideVersionOverlay();
        if (running) {
            toast("Server is running");
        } else {
            toast("Server restarted but may not be healthy. Check logs.", "error");
        }
        loadStatus();
    } catch (e) {
        hideVersionOverlay();
        toast(e.message, "error");
    }
}

// =============================================
// Config
// =============================================

async function loadConfig() {
    try {
        config = await api("/api/config");
        renderDevices();
        renderServerConfig();
    } catch (e) {
        toast("Error loading configuration: " + e.message, "error");
    }
}

async function loadTemplates() {
    try {
        templates = await api("/api/templates");
        const select = document.getElementById("deviceProtocol");
        templates.forEach(t => {
            const opt = document.createElement("option");
            opt.value = t.protocol;
            opt.textContent = t.protocol;
            select.appendChild(opt);
        });
    } catch (e) {
        console.error("Error loading templates:", e);
    }
}

// =============================================
// Server Config
// =============================================

function renderServerConfig() {
    document.getElementById("serverName").value = config.name || "";
    document.getElementById("serverPort").value = config.tcpPort || 12687;
    document.getElementById("serverDir").value = config.dir || "/app/security";
    document.getElementById("serverVersion").value = config.version || "0.8";
    document.getElementById("serverInsecure").value = String(config.disableInsecureEndpoint || false);
    updateEndpointPreview();
}

// =============================================
// Manager Version
// =============================================

async function loadManagerVersion() {
    try {
        const data = await api("/api/manager/version");
        const el = document.getElementById("managerCurrentVersion");
        const commitEl = document.getElementById("managerCurrentCommit");
        if (el) el.textContent = data.version || "dev";
        if (commitEl) commitEl.textContent = data.commit ? `(${data.commit})` : "";
        // Always show rollback button for admins
        const btnRollback = document.getElementById("btnManagerRollback");
        if (btnRollback && isAdmin()) btnRollback.style.display = "";
    } catch {}
}

async function checkManagerUpdate() {
    const statusEl = document.getElementById("managerUpdateStatus");
    statusEl.innerHTML = '<span class="text-muted">Checking for updates...</span>';

    try {
        const data = await api("/api/manager/check-update");

        // Update current info
        const curEl = document.getElementById("managerCurrentVersion");
        const curCommit = document.getElementById("managerCurrentCommit");
        if (curEl) curEl.textContent = data.currentVersion || "dev";
        if (curCommit) curCommit.textContent = data.currentCommit ? `(${data.currentCommit})` : "";

        const banner = document.getElementById("managerUpdateBanner");
        const latestInfo = document.getElementById("managerLatestInfo");
        const btnUpdate = document.getElementById("btnManagerUpdate");
        const changelog = document.getElementById("managerChangelog");

        if (data.hasUpdate) {
            // Show update available
            document.getElementById("managerLatestVersion").textContent = data.latestVersion || data.latestCommit;
            document.getElementById("managerNewCommits").textContent = `(${data.behind} new commits)`;
            latestInfo.style.display = "";

            document.getElementById("managerBannerText").textContent =
                `Version ${data.latestVersion || data.latestCommit} is available with ${data.behind} new commits.`;
            banner.style.display = "";
            btnUpdate.style.display = "";

            // Show changelog
            if (data.changelog && data.changelog.length > 0) {
                changelog.innerHTML = data.changelog.map(c => escHtml(c)).join("<br>");
                changelog.style.display = "";
            }

            statusEl.innerHTML = `<span style="color:var(--warning)">${data.behind} updates available.</span>`;
        } else {
            banner.style.display = "none";
            latestInfo.style.display = "none";
            btnUpdate.style.display = "none";
            changelog.style.display = "none";
            statusEl.innerHTML = '<span style="color:var(--success)">You are up to date.</span>';
        }

        // Always show rollback button for admins after a check
        const btnRollback = document.getElementById("btnManagerRollback");
        if (btnRollback && isAdmin()) btnRollback.style.display = "";
    } catch (e) {
        statusEl.innerHTML = `<span style="color:var(--danger)">Check failed: ${escHtml(e.message)}</span>`;
    }
}

async function updateManager() {
    const curVer = document.getElementById("managerCurrentVersion")?.textContent || "current";
    const newVer = document.getElementById("managerLatestVersion")?.textContent || "latest";

    if (!confirm(`Update PLC4X Manager?\n\nFrom: ${curVer}\nTo: ${newVer}\n\nThe system will pull changes from GitHub, rebuild containers, and restart.\nThis may take 1-2 minutes.`)) return;

    const statusEl = document.getElementById("managerUpdateStatus");

    try {
        showVersionOverlay("Updating Manager", "Pulling changes from GitHub...", 1, 3, "&#11015;");
        const data = await api("/api/manager/update", "POST");

        showVersionOverlay("Updating Manager", "Rebuilding containers... (this may take a minute)", 2, 3, "&#128260;");
        statusEl.innerHTML = `<span style="color:var(--success)">${escHtml(data.message)}</span>`;

        // Poll for rebuild status instead of blind wait
        for (let i = 0; i < 60; i++) {
            await new Promise(r => setTimeout(r, 3000));
            try {
                const status = await api("/api/manager/update-status");
                showVersionOverlay("Updating Manager", status.log ? status.log.split("\n").pop() : "Rebuilding...", 2, 3, "&#128260;");
                if (status.done || status.failed) break;
            } catch { break; } // container restarted, we lost connection
        }

        hideVersionOverlay();
        toast("Manager updated! Reloading...");
        setTimeout(() => window.location.reload(), 2000);
    } catch (e) {
        hideVersionOverlay();
        statusEl.innerHTML = `<span style="color:var(--danger)">Update failed: ${escHtml(e.message)}</span>`;
        toast("Update failed: " + e.message, "error");
    }
}

async function rollbackManager() {
    if (!confirm("Rollback to the previous version?\n\nThe system will checkout the previous tag, rebuild, and restart.")) return;

    const statusEl = document.getElementById("managerUpdateStatus");
    try {
        showVersionOverlay("Rolling Back", "Reverting to previous version...", 1, 2, "&#128260;");
        const data = await api("/api/manager/rollback", "POST");

        showVersionOverlay("Rolling Back", "Rebuilding containers...", 2, 2, "&#9989;");
        statusEl.innerHTML = `<span style="color:var(--success)">${escHtml(data.message)}</span>`;

        // Poll for rebuild status instead of blind wait
        for (let i = 0; i < 60; i++) {
            await new Promise(r => setTimeout(r, 3000));
            try {
                const status = await api("/api/manager/update-status");
                showVersionOverlay("Rolling Back", status.log ? status.log.split("\n").pop() : "Rebuilding...", 2, 2, "&#128260;");
                if (status.done || status.failed) break;
            } catch { break; } // container restarted, we lost connection
        }

        hideVersionOverlay();
        toast("Rollback complete! Reloading...");
        setTimeout(() => window.location.reload(), 2000);
    } catch (e) {
        hideVersionOverlay();
        statusEl.innerHTML = `<span style="color:var(--danger)">Rollback failed: ${escHtml(e.message)}</span>`;
    }
}


async function loadPlc4xVersion() {
    try {
        const data = await api("/api/plc4x/version");
        const el = document.getElementById("plc4xCurrentVersion");
        if (el) el.value = data.currentVersion || "unknown";
        // Show/hide rollback button based on backup availability
        const btnRollback = document.getElementById("btnRollback");
        const rollbackLabel = document.getElementById("rollbackVersionLabel");
        if (btnRollback) {
            if (data.backupVersion) {
                btnRollback.style.display = "";
                if (rollbackLabel) rollbackLabel.textContent = data.backupVersion;
            } else {
                btnRollback.style.display = "none";
            }
        }
        checkLatestPlc4xVersion(data.currentVersion);
    } catch {}
}

async function checkLatestPlc4xVersion(currentVersion) {
    try {
        const data = await api("/api/plc4x/latest-version");
        const banner = document.getElementById("plc4xUpdateBanner");
        if (!banner || !data.latestVersion) return;
        if (currentVersion && currentVersion !== "unknown" && data.latestVersion !== currentVersion) {
            document.getElementById("plc4xBannerText").textContent =
                `Version ${data.latestVersion} is available (you have ${currentVersion}).`;
            banner.style.display = "block";
            banner.dataset.latestVersion = data.latestVersion;
        } else {
            banner.style.display = "none";
        }
    } catch {}
}

function applyLatestVersion() {
    const banner = document.getElementById("plc4xUpdateBanner");
    const version = banner && banner.dataset.latestVersion;
    if (version) {
        document.getElementById("plc4xNewVersion").value = version;
        updatePlc4x();
    }
}

function showVersionOverlay(title, text, step, totalSteps, icon) {
    const o = document.getElementById("versionUpdateOverlay");
    document.getElementById("versionOverlayTitle").textContent = title;
    document.getElementById("versionOverlayText").textContent = text;
    document.getElementById("versionOverlayStep").textContent = `Step ${step} of ${totalSteps}`;
    document.getElementById("versionOverlayIcon").innerHTML = icon || "&#9881;";
    document.getElementById("versionProgressBar").style.width = Math.round((step / totalSteps) * 100) + "%";
    o.classList.add("open");
}

function hideVersionOverlay() {
    document.getElementById("versionUpdateOverlay").classList.remove("open");
}

async function updatePlc4x() {
    const version = document.getElementById("plc4xNewVersion").value.trim();
    if (!version) { toast("Enter a version number", "error"); return; }
    const currentVer = document.getElementById("plc4xCurrentVersion").value || "unknown";
    if (!confirm(`Change PLC4X from ${currentVer} to ${version}?\n\nThe new jar will be downloaded from Maven Central and the server will restart.\nThe current version will be backed up so you can undo this change.`)) return;

    const statusEl = document.getElementById("plc4xUpdateStatus");
    statusEl.innerHTML = "";

    try {
        showVersionOverlay(`Updating to ${version}`, "Checking version on Maven Central...", 1, 4, "&#128269;");
        await new Promise(r => setTimeout(r, 300));

        showVersionOverlay(`Updating to ${version}`, "Downloading jar and backing up current version...", 2, 4, "&#11015;");
        const data = await api("/api/plc4x/update", "POST", { version });

        showVersionOverlay(`Updating to ${version}`, "Restarting PLC4X server...", 3, 4, "&#128260;");
        await new Promise(r => setTimeout(r, 2000));

        showVersionOverlay("Update complete!", `PLC4X is now on version ${version}. Waiting for server...`, 4, 4, "&#9989;");
        await new Promise(r => setTimeout(r, 13000));

        hideVersionOverlay();
        statusEl.innerHTML = `<span style="color:var(--success)">${escHtml(data.message)}</span>`;
        toast(data.message);
        document.getElementById("plc4xNewVersion").value = "";
        loadPlc4xVersion();
        loadStatus();
    } catch (e) {
        hideVersionOverlay();
        statusEl.innerHTML = `<span style="color:var(--danger)">${escHtml(e.message)}</span>`;
        toast(e.message, "error");
    }
}

async function rollbackPlc4x() {
    const rollbackVer = document.getElementById("rollbackVersionLabel")?.textContent || "previous";
    const currentVer = document.getElementById("plc4xCurrentVersion").value || "unknown";
    if (!confirm(`Undo last version change?\n\nThis will switch from ${currentVer} back to ${rollbackVer}.\nThe server will restart.`)) return;

    try {
        showVersionOverlay(`Rolling back to ${rollbackVer}`, "Swapping jar files...", 1, 3, "&#128260;");
        const data = await api("/api/plc4x/rollback", "POST");

        showVersionOverlay(`Rolling back to ${rollbackVer}`, "Restarting PLC4X server...", 2, 3, "&#128260;");
        await new Promise(r => setTimeout(r, 2000));

        showVersionOverlay("Rollback complete!", `PLC4X restored to ${rollbackVer}. Waiting for server...`, 3, 3, "&#9989;");
        await new Promise(r => setTimeout(r, 13000));

        hideVersionOverlay();
        toast(data.message);
        loadPlc4xVersion();
        loadStatus();
    } catch (e) {
        hideVersionOverlay();
        toast(e.message, "error");
    }
}

function updateEndpointPreview() {
    const port = document.getElementById("serverPort").value || "12687";
    document.getElementById("endpointPreview").textContent = `opc.tcp://<host>:${port}/plc4x`;
}

async function saveServerConfig() {
    try {
        await api("/api/config/server", "PUT", {
            name: document.getElementById("serverName").value,
            tcpPort: parseInt(document.getElementById("serverPort").value),
            dir: document.getElementById("serverDir").value,
            version: document.getElementById("serverVersion").value,
            disableInsecureEndpoint: document.getElementById("serverInsecure").value === "true"
        });
        toast("Server configuration saved");
        loadConfig();
    } catch (e) {
        toast(e.message, "error");
    }
}

// =============================================
// Devices
// =============================================

function renderDevices() {
    const container = document.getElementById("deviceList");
    const devices = config.devices || [];

    if (devices.length === 0) {
        container.innerHTML = '<p class="text-muted" style="text-align:center;padding:32px">No devices configured. Click "+ Add Device" to get started.</p>';
        return;
    }

    const pgDev = paginate(devices, "devices");
    container.innerHTML = pgDev.items.map((device, i) => {
        const idx = pgDev.start - 1 + i;
        const isEnabled = device.enabled !== false;
        const enabledClass = isEnabled ? "" : " device-disabled";
        const enabledLabel = isEnabled ? "Active" : "Inactive";
        const enabledBadge = isEnabled
            ? '<span class="badge-ok" style="font-size:11px;padding:2px 8px;border-radius:4px">Active</span>'
            : '<span class="badge-error" style="font-size:11px;padding:2px 8px;border-radius:4px">Inactive</span>';
        return `
        <div class="device-card${enabledClass}">
            <div class="device-header" onclick="toggleDevice(${idx})">
                <div>
                    <span class="device-name">${escHtml(device.name)}</span>
                    ${enabledBadge}
                    <span class="device-conn">${escHtml(device.connectionString)}</span>
                </div>
                <div style="display:flex;gap:8px;align-items:center">
                    <button class="btn btn-outline btn-sm" onclick="event.stopPropagation();toggleDeviceEnabled('${escAttr(device.name)}',${!isEnabled})" title="${isEnabled ? 'Deactivate' : 'Activate'}">${isEnabled ? 'Disable' : 'Enable'}</button>
                    <button class="btn btn-outline btn-sm" onclick="event.stopPropagation();editDevice('${escAttr(device.name)}')">Edit</button>
                    <button class="btn btn-danger btn-sm" onclick="event.stopPropagation();deleteDevice('${escAttr(device.name)}')">Remove</button>
                </div>
            </div>
            <div class="device-body" id="device-body-${idx}">
                <div class="device-meta">
                    <span>Tags: ${(device.tags || []).length}</span>
                </div>
                ${renderTagsTable(device.tags || [], device.name, device.calculatedTags)}
            </div>
        </div>
    `}).join("") + renderPagination("devices", pgDev.totalPages, pgDev.total, pgDev.start, pgDev.end, "renderDevices");
}

function renderTagsTable(tags, deviceName, calculatedTags) {
    const allTags = [
        ...(tags || []).map(t => ({...t, type: "plc"})),
        ...(calculatedTags || []).map(t => ({alias: t.alias, address: `calc: ${t.formula}`, type: "calc"}))
    ];
    if (allTags.length === 0) return '<p class="text-muted" style="font-size:13px">No tags configured</p>';
    const key = "tags_" + (deviceName || "default");
    const pg = paginate(allTags, key);
    let html = `<table class="table">
        <thead><tr><th>Alias</th><th>Address / Formula</th><th>Type</th></tr></thead>
        <tbody>
            ${pg.items.map(t => `<tr>
                <td class="tag-alias">${escHtml(t.alias)}</td>
                <td class="tag-address">${escHtml(t.address)}</td>
                <td>${t.type === "calc" ? '<span class="badge-calc">CALC</span>' : '<span class="badge-plc">PLC</span>'}</td>
            </tr>`).join("")}
        </tbody>
    </table>`;
    html += renderPagination(key, pg.totalPages, pg.total, pg.start, pg.end, "renderDevices");
    return html;
}

function toggleDevice(idx) {
    const body = document.getElementById(`device-body-${idx}`);
    body.classList.toggle("open");
}

// =============================================
// Add / Edit Device
// =============================================

function openAddDevice() {
    editingDevice = null;
    document.getElementById("deviceModalTitle").textContent = "Add Device";
    document.getElementById("devicePlant").value = "";
    document.getElementById("deviceName").value = "";
    document.getElementById("deviceName").disabled = false;
    document.getElementById("deviceConn").value = "";
    document.getElementById("deviceProtocol").value = "";
    document.getElementById("deviceAllowWrite").checked = false;
    document.getElementById("devicePollInterval").value = "5";
    document.getElementById("tagEditor").innerHTML = "";
    document.getElementById("calcTagEditor").innerHTML = "";
    const connResult = document.getElementById("connTestResult");
    if (connResult) connResult.textContent = "";
    addTagRow();
    openModal("deviceModal");
}

function editDevice(name) {
    const device = (config.devices || []).find(d => d.name === name);
    if (!device) return;

    editingDevice = name;
    document.getElementById("deviceModalTitle").textContent = "Edit Device";
    document.getElementById("devicePlant").value = device.plant || "";
    document.getElementById("deviceName").value = device.name;
    document.getElementById("deviceName").disabled = true;
    document.getElementById("deviceConn").value = device.connectionString;
    document.getElementById("deviceAllowWrite").checked = !!device.allowWrite;
    document.getElementById("devicePollInterval").value = device.pollInterval || 5;
    const connResult = document.getElementById("connTestResult");
    if (connResult) connResult.textContent = "";

    // Auto-detect protocol from connection string
    const conn = device.connectionString || "";
    const protocolSelect = document.getElementById("deviceProtocol");
    protocolSelect.value = "";
    for (const tmpl of templates) {
        if (conn.startsWith(tmpl.example.split("://")[0] + "://")) {
            protocolSelect.value = tmpl.protocol;
            break;
        }
    }

    const editor = document.getElementById("tagEditor");
    editor.innerHTML = "";
    (device.tags || []).forEach(tag => addTagRow(tag.alias, tag.address));
    if ((device.tags || []).length === 0) addTagRow();

    const calcEditor = document.getElementById("calcTagEditor");
    calcEditor.innerHTML = "";
    (device.calculatedTags || []).forEach(ct => addCalcTagRow(ct.alias, ct.formula));

    openModal("deviceModal");
}

async function saveDevice() {
    const name = document.getElementById("deviceName").value.trim();
    const conn = document.getElementById("deviceConn").value.trim();

    if (!name || !conn) {
        toast("Name and Connection String are required", "error");
        return;
    }

    if (/\s/.test(name)) {
        toast("Name cannot contain spaces", "error");
        return;
    }

    const tags = [];
    document.querySelectorAll("#tagEditor .tag-row").forEach(row => {
        const alias = row.querySelector(".tag-alias-input").value.trim();
        const address = row.querySelector(".tag-address-input").value.trim();
        if (alias && address) tags.push({ alias, address });
    });

    const calculatedTags = [];
    document.querySelectorAll("#calcTagEditor .calc-tag-row").forEach(row => {
        const alias = row.querySelector(".calc-alias-input").value.trim();
        const formula = row.querySelector(".calc-formula-input").value.trim();
        if (alias && formula) calculatedTags.push({ alias, formula });
    });

    const plant = document.getElementById("devicePlant").value.trim();
    const allowWrite = document.getElementById("deviceAllowWrite").checked;
    const pollInterval = parseInt(document.getElementById("devicePollInterval").value) || 5;
    const device = { name, connectionString: conn, tags, allowWrite, pollInterval };
    if (plant) device.plant = plant;
    if (calculatedTags.length > 0) device.calculatedTags = calculatedTags;

    try {
        if (editingDevice) {
            await api(`/api/devices/${editingDevice}`, "PUT", device);
            toast(`Device '${name}' updated`);
        } else {
            await api("/api/devices", "POST", device);
            toast(`Device '${name}' added`);
        }
        closeModal("deviceModal");
        loadConfig();
    } catch (e) {
        toast(e.message, "error");
    }
}

async function toggleDeviceEnabled(name, enabled) {
    try {
        const device = (config.devices || []).find(d => d.name === name);
        if (!device) return;
        device.enabled = enabled;
        await api(`/api/devices/${name}`, "PUT", device);
        toast(`Device '${name}' ${enabled ? 'activated' : 'deactivated'}. Restart server to apply.`);
        loadConfig();
    } catch (e) {
        toast(e.message, "error");
    }
}

async function loadDemoDevices() {
    if (!confirm("Load pre-configured demo/test devices? This includes:\n\n- Simulated PLC (with calculated tags)\n- OPC-UA Demo Servers (Milo, Prosys)\n- Siemens S7 Template (with calculated tags)\n- Modbus TCP Template (with calculated tags)\n- Allen-Bradley ControlLogix Template (with calculated tags)\n- EtherNet/IP Template\n\nTemplates are disabled by default. Enable them after setting the correct IP.\nExisting devices with the same name will not be overwritten.")) return;
    try {
        const data = await api("/api/demo/load", "POST");
        toast(data.message);
        loadConfig();
        loadDashboard();
    } catch (e) {
        toast(e.message, "error");
    }
}

async function deleteDevice(name) {
    if (!confirm(`Remove device '${name}' and all its tags?`)) return;
    try {
        await api(`/api/devices/${name}`, "DELETE");
        toast(`Device '${name}' removed`);
        loadConfig();
    } catch (e) {
        toast(e.message, "error");
    }
}

// =============================================
// Tags Editor
// =============================================

function addTagRow(alias = "", address = "") {
    const editor = document.getElementById("tagEditor");
    const row = document.createElement("div");
    row.className = "tag-row";
    const isVirtual = address.toUpperCase().startsWith("VIRTUAL");
    row.innerHTML = `
        <input type="text" class="tag-alias-input" placeholder="Alias (e.g.: Temperature)" value="${escHtml(alias)}"
               style="padding:6px 10px;border:1px solid var(--border);border-radius:4px;font-size:13px">
        <div style="display:flex;align-items:center;gap:6px">
            <input type="text" class="tag-address-input" placeholder="Address (e.g.: %DB1:0:REAL or VIRTUAL)" value="${escHtml(address)}"
                   style="padding:6px 10px;border:1px solid var(--border);border-radius:4px;font-size:13px;font-family:monospace;flex:1"
                   oninput="this.nextElementSibling.style.display=this.value.toUpperCase().startsWith('VIRTUAL')?'inline':'none'">
            <span class="status-badge badge-ok" style="font-size:0.7rem;white-space:nowrap;display:${isVirtual ? "inline" : "none"}">VIRTUAL</span>
        </div>
        <button class="btn btn-danger btn-sm" onclick="this.parentElement.remove()">X</button>
    `;
    editor.appendChild(row);
}

async function testFormula() {
    const formula = document.getElementById("formulaTestExpr").value.trim();
    const varsStr = document.getElementById("formulaTestVars").value.trim();
    const resultEl = document.getElementById("formulaTestResult");

    if (!formula) {
        resultEl.innerHTML = '<span style="color:var(--warning)">Enter a formula to test</span>';
        return;
    }

    let testValues = {};
    if (varsStr) {
        try {
            testValues = JSON.parse(varsStr);
        } catch {
            resultEl.innerHTML = '<span style="color:var(--danger)">Invalid JSON in variables field. Use format: {"a": 10, "b": 20}</span>';
            return;
        }
    }

    try {
        const data = await api("/api/formula/validate", "POST", { formula, testValues });
        if (!data.valid) {
            resultEl.innerHTML = `<span style="color:var(--danger)">Syntax Error: ${escHtml(data.error)}</span>`;
        } else if (data.error) {
            resultEl.innerHTML = `<span style="color:var(--warning)">Valid syntax, but evaluation error: ${escHtml(data.error)}</span>`;
        } else if (data.result !== undefined && data.result !== null) {
            resultEl.innerHTML = `<span style="color:var(--success)">Result = <strong>${escHtml(String(data.result))}</strong></span>`;
        } else {
            resultEl.innerHTML = `<span style="color:var(--success)">Valid formula syntax</span>`;
        }
    } catch (e) {
        resultEl.innerHTML = `<span style="color:var(--danger)">Error: ${escHtml(e.message)}</span>`;
    }
}

function addCalcTagRow(alias = "", formula = "") {
    const editor = document.getElementById("calcTagEditor");
    const row = document.createElement("div");
    row.className = "calc-tag-row tag-row";
    row.innerHTML = `
        <input type="text" class="calc-alias-input" placeholder="Alias (e.g.: TempAvg)" value="${escHtml(alias)}"
               style="padding:6px 10px;border:1px solid var(--border);border-radius:4px;font-size:13px">
        <input type="text" class="calc-formula-input" placeholder="Formula (e.g.: (Temp1 + Temp2) / 2)" value="${escHtml(formula)}"
               style="padding:6px 10px;border:1px solid var(--border);border-radius:4px;font-size:13px;font-family:monospace;flex:2">
        <button class="btn btn-danger btn-sm" onclick="this.parentElement.remove()">X</button>
    `;
    editor.appendChild(row);
}

function onProtocolChange() {
    const protocol = document.getElementById("deviceProtocol").value;
    const tmpl = templates.find(t => t.protocol === protocol);
    if (!tmpl) return;

    document.getElementById("deviceConn").value = tmpl.example;

    const editor = document.getElementById("tagEditor");
    editor.innerHTML = "";
    tmpl.tagExamples.forEach(t => addTagRow(t.alias, t.address));
}

// =============================================
// Security Status
// =============================================

async function loadSecurityStatus() {
    try {
        const status = await api("/api/security/status");
        const container = document.getElementById("securityStatus");
        container.innerHTML = `
            <table class="table">
                <tbody>
                    <tr>
                        <td><strong>Initialized</strong></td>
                        <td><span class="status-badge ${status.initialized ? 'badge-ok' : 'badge-warn'}">${status.initialized ? 'Yes' : 'No'}</span></td>
                    </tr>
                    <tr>
                        <td><strong>Password File (.jibberish)</strong></td>
                        <td><span class="status-badge ${status.passwordFile ? 'badge-ok' : 'badge-warn'}">${status.passwordFile ? 'Present' : 'Missing'}</span></td>
                    </tr>
                    <tr>
                        <td><strong>Keystore (plc4x-opcuaserver.pfx)</strong></td>
                        <td><span class="status-badge ${status.keystore ? 'badge-ok' : 'badge-warn'}">${status.keystore ? 'Present' : 'Missing'}</span>
                            ${status.keystoreSize ? ` (${(status.keystoreSize / 1024).toFixed(1)} KB)` : ''}</td>
                    </tr>
                    <tr>
                        <td><strong>PKI Directory</strong></td>
                        <td><span class="status-badge ${status.pkiDirectory ? 'badge-ok' : 'badge-warn'}">${status.pkiDirectory ? 'Present' : 'Missing'}</span></td>
                    </tr>
                    <tr>
                        <td><strong>Keystore Type</strong></td>
                        <td>PKCS12</td>
                    </tr>
                    <tr>
                        <td><strong>Security Policy</strong></td>
                        <td>Basic256Sha256 (SignAndEncrypt)</td>
                    </tr>
                    <tr>
                        <td><strong>Token Policies</strong></td>
                        <td>Anonymous, Username/Password, X.509 Certificate</td>
                    </tr>
                </tbody>
            </table>
        `;
    } catch (e) {
        document.getElementById("securityStatus").innerHTML = `<p style="color:var(--danger)">Error: ${escHtml(e.message)}</p>`;
    }
}

// =============================================
// Users
// =============================================

async function loadUsers() {
    try {
        const users = await api("/api/users");
        const container = document.getElementById("userList");
        if (users.length === 0) {
            container.innerHTML = '<p class="text-muted">No users configured. Security may not be initialized yet.</p>';
            return;
        }
        const pgUsers = paginate(users, "users");
        container.innerHTML = `
            <table class="table">
                <thead><tr><th>Username</th><th>Security Group</th><th>Actions</th></tr></thead>
                <tbody>
                    ${pgUsers.items.map(u => `
                        <tr>
                            <td><strong>${escHtml(u.username)}</strong></td>
                            <td><code>${escHtml(u.security)}</code></td>
                            <td style="display:flex;gap:6px">
                                <button class="btn btn-outline btn-sm" onclick="editUser('${escAttr(u.username)}', '${escAttr(u.security)}')">Edit</button>
                                <button class="btn btn-danger btn-sm" onclick="deleteUser('${escAttr(u.username)}')">Remove</button>
                            </td>
                        </tr>
                    `).join("")}
                </tbody>
            </table>
        ` + renderPagination("users", pgUsers.totalPages, pgUsers.total, pgUsers.start, pgUsers.end, "loadUsers");
    } catch (e) {
        document.getElementById("userList").innerHTML = `<p style="color:var(--danger)">Error: ${escHtml(e.message)}</p>`;
    }
}

function openAddUser() {
    editingUser = null;
    document.getElementById("userModalTitle").textContent = "Add User";
    document.getElementById("userName").value = "";
    document.getElementById("userName").disabled = false;
    document.getElementById("userPassword").value = "";
    document.getElementById("userPassword").placeholder = "Enter password";
    document.getElementById("userPasswordLabel").textContent = "Password";
    document.getElementById("userSecurity").value = "admin-group";
    openModal("userModal");
}

function editUser(username, security) {
    editingUser = username;
    document.getElementById("userModalTitle").textContent = "Edit User";
    document.getElementById("userName").value = username;
    document.getElementById("userName").disabled = true;
    document.getElementById("userPassword").value = "";
    document.getElementById("userPassword").placeholder = "Leave blank to keep current";
    document.getElementById("userPasswordLabel").textContent = "New Password (optional)";
    document.getElementById("userSecurity").value = security || "admin-group";
    openModal("userModal");
}

async function saveUser() {
    const username = document.getElementById("userName").value.trim();
    const password = document.getElementById("userPassword").value;
    const security = document.getElementById("userSecurity").value;

    if (!username) {
        toast("Username is required", "error");
        return;
    }

    try {
        if (editingUser) {
            const data = { security };
            if (password) data.password = password;
            await api(`/api/users/${editingUser}`, "PUT", data);
            toast(`User '${username}' updated`);
        } else {
            if (!password) {
                toast("Password is required for new users", "error");
                return;
            }
            await api("/api/users", "POST", { username, password, security });
            toast(`User '${username}' added`);
        }
        closeModal("userModal");
        loadUsers();
    } catch (e) {
        toast(e.message, "error");
    }
}

async function deleteUser(username) {
    if (!confirm(`Remove user '${username}'?`)) return;
    try {
        await api(`/api/users/${username}`, "DELETE");
        toast(`User '${username}' removed`);
        loadUsers();
    } catch (e) {
        toast(e.message, "error");
    }
}

// =============================================
// Keystore Password
// =============================================

async function loadAdminInfo() {
    try {
        const data = await api("/api/auth/info");
        const el = document.getElementById("adminUsername");
        if (el) el.value = data.username || "admin";
    } catch {}
}

async function changeAdminPassword() {
    const newPass = document.getElementById("newAdminPassword").value;
    const confirm = document.getElementById("confirmAdminPassword").value;

    if (!newPass) {
        toast("Enter a new password", "error");
        return;
    }
    if (newPass.length < 4) {
        toast("Password must be at least 4 characters", "error");
        return;
    }
    if (newPass !== confirm) {
        toast("Passwords do not match", "error");
        return;
    }

    try {
        await api("/api/auth/password", "PUT", { password: newPass });
        toast("Admin password changed. Logging out...");
        document.getElementById("newAdminPassword").value = "";
        document.getElementById("confirmAdminPassword").value = "";
        setTimeout(logout, 2000);
    } catch (e) {
        toast(e.message, "error");
    }
}

async function changeKeystorePassword() {
    const password = document.getElementById("newKeystorePassword").value;
    if (!password) {
        toast("Enter a new password", "error");
        return;
    }
    if (!confirm("Change the keystore password? You will need to restart the server and re-initialize security.")) return;
    try {
        await api("/api/security/password", "PUT", { password });
        toast("Keystore password updated. Restart the server to apply.");
        document.getElementById("newKeystorePassword").value = "";
    } catch (e) {
        toast(e.message, "error");
    }
}

// =============================================
// PKI Certificates
// =============================================

async function loadCertificates() {
    try {
        const trusted = await api("/api/security/certificates/trusted");
        const rejected = await api("/api/security/certificates/rejected");
        renderCertList("trustedCerts", trusted, "trusted");
        renderCertList("rejectedCerts", rejected, "rejected");
    } catch (e) {
        document.getElementById("trustedCerts").innerHTML = `<p style="color:var(--text-secondary);font-size:13px">Unable to load certificates</p>`;
        document.getElementById("rejectedCerts").innerHTML = `<p style="color:var(--text-secondary);font-size:13px">Unable to load certificates</p>`;
    }
}

function renderCertList(containerId, certs, type) {
    const container = document.getElementById(containerId);
    if (certs.length === 0) {
        container.innerHTML = `<p class="text-muted" style="font-size:13px">No ${type} certificates</p>`;
        return;
    }
    const pgCerts = paginate(certs, "certs_" + type);
    container.innerHTML = `
        <table class="table">
            <thead><tr><th>Filename</th><th>Size</th><th>Actions</th></tr></thead>
            <tbody>
                ${pgCerts.items.map(c => `
                    <tr>
                        <td><code>${escHtml(c.filename)}</code></td>
                        <td>${(c.size / 1024).toFixed(1)} KB</td>
                        <td style="display:flex;gap:6px">
                            ${type === "rejected"
                                ? `<button class="btn btn-success btn-sm" onclick="trustCert('${escAttr(c.filename)}')">Trust</button>`
                                : `<button class="btn btn-outline btn-sm" onclick="rejectCert('${escAttr(c.filename)}')">Reject</button>`
                            }
                            <button class="btn btn-danger btn-sm" onclick="deleteCert('${escAttr(c.filename)}')">Delete</button>
                        </td>
                    </tr>
                `).join("")}
            </tbody>
        </table>
    ` + renderPagination("certs_" + type, pgCerts.totalPages, pgCerts.total, pgCerts.start, pgCerts.end, "loadCertificates");
}

async function trustCert(filename) {
    try {
        await api(`/api/security/certificates/trust/${filename}`, "POST");
        toast(`Certificate '${filename}' trusted`);
        loadCertificates();
    } catch (e) {
        toast(e.message, "error");
    }
}

async function rejectCert(filename) {
    try {
        await api(`/api/security/certificates/reject/${filename}`, "POST");
        toast(`Certificate '${filename}' rejected`);
        loadCertificates();
    } catch (e) {
        toast(e.message, "error");
    }
}

async function deleteCert(filename) {
    if (!confirm(`Delete certificate '${filename}'?`)) return;
    try {
        await api(`/api/security/certificates/${filename}`, "DELETE");
        toast(`Certificate '${filename}' deleted`);
        loadCertificates();
    } catch (e) {
        toast(e.message, "error");
    }
}

// =============================================
// Logs
// =============================================

async function loadLogs() {
    const lines = document.getElementById("logLines").value;
    try {
        const data = await api(`/api/server/logs?lines=${lines}`);
        document.getElementById("logContent").textContent = data.logs || "No logs";
    } catch (e) {
        document.getElementById("logContent").textContent = "Error: " + e.message;
    }
}

// =============================================
// Backups
// =============================================

async function loadBackups() {
    try {
        const backups = await api("/api/backups");
        const container = document.getElementById("backupList");
        if (backups.length === 0) {
            container.innerHTML = '<p class="text-muted" style="text-align:center;padding:32px">No backups available. Backups are created automatically when saving configurations.</p>';
            return;
        }

        const pgBackup = paginate(backups, "backups");

        let html = `<table class="table">
            <thead>
                <tr>
                    <th>Date / Time</th>
                    <th>Size</th>
                    <th>Changes</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>`;

        pgBackup.items.forEach((b) => {
            const dateStr = b.date ? formatBackupDate(b.date) : "—";
            const sizeStr = b.size ? formatFileSize(b.size) : "—";
            const safeFilename = escAttr(b.filename);

            html += `<tr>
                <td>
                    <div style="font-weight:500">${dateStr}</div>
                    <div class="text-muted" style="font-size:11px;font-family:var(--font-mono)">${escHtml(b.filename)}</div>
                </td>
                <td>${sizeStr}</td>
                <td style="max-width:300px"><span class="text-muted" style="font-size:11px;cursor:pointer" id="changes-${safeFilename}" onclick="loadBackupChanges('${safeFilename}')">Show changes</span></td>
                <td>
                    <div style="display:flex;gap:6px">
                        <button class="btn btn-outline btn-sm" onclick="viewDiff('${safeFilename}')">Diff</button>
                        <button class="btn btn-outline btn-sm" onclick="downloadBackup('${safeFilename}')">Download</button>
                        <button class="btn btn-primary btn-sm" onclick="restoreBackup('${safeFilename}')">Restore</button>
                    </div>
                </td>
            </tr>`;
        });

        html += `</tbody></table>`;
        html += renderPagination("backups", pgBackup.totalPages, pgBackup.total, pgBackup.start, pgBackup.end, "loadBackups");
        container.innerHTML = html;
    } catch (e) {
        toast("Error loading backups: " + e.message, "error");
    }
}

function formatBackupDate(dateStr) {
    try {
        const d = new Date(dateStr);
        if (isNaN(d.getTime())) return dateStr;
        return d.toLocaleString("en-US", {
            year: "numeric", month: "short", day: "numeric",
            hour: "2-digit", minute: "2-digit", second: "2-digit"
        });
    } catch {
        return dateStr;
    }
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

async function loadBackupChanges(filename) {
    const el = document.getElementById("changes-" + filename);
    if (!el) return;
    el.textContent = "Loading...";
    el.onclick = null;
    try {
        const data = await api(`/api/backups/${encodeURIComponent(filename)}/changes`);
        if (data.changes && data.changes.length > 0) {
            el.innerHTML = data.changes.map(c => {
                const cs = String(c);
                if (cs.startsWith("Added")) return `<span class="badge-change badge-added">${escHtml(cs)}</span>`;
                if (cs.startsWith("Removed")) return `<span class="badge-change badge-removed">${escHtml(cs)}</span>`;
                if (cs.startsWith("Changed")) return `<span class="badge-change badge-changed">${escHtml(cs)}</span>`;
                return `<span class="badge-change">${escHtml(cs)}</span>`;
            }).join("");
            if (data.devices != null) el.innerHTML += ` <span class="text-muted" style="font-size:10px">${data.devices} dev, ${data.tags} tags</span>`;
        } else {
            el.innerHTML = '<span class="text-muted" style="font-size:11px">No changes</span>';
        }
    } catch {
        el.innerHTML = '<span class="text-muted" style="font-size:11px">—</span>';
    }
}

async function createBackupNow() {
    try {
        const data = await api("/api/backups/create", "POST");
        toast(data.message);
        loadBackups();
    } catch (e) {
        toast(e.message, "error");
    }
}

async function restoreFromFile(input) {
    const file = input.files[0];
    if (!file) return;
    if (!confirm(`Restore configuration from '${file.name}'?\n\nThe current configuration will be backed up first.`)) {
        input.value = "";
        return;
    }

    const formData = new FormData();
    formData.append("file", file);

    try {
        const token = getToken();
        const res = await fetch("/api/backups/upload", {
            method: "POST",
            headers: { "Authorization": `Bearer ${token}` },
            body: formData
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.detail || "Upload failed");
        toast(data.message);
        loadConfig();
        loadBackups();
    } catch (e) {
        toast(e.message, "error");
    }
    input.value = "";
}

async function cleanupBackups() {
    if (!confirm("Remove old backups? Only the most recent 50 will be kept.")) return;
    try {
        const data = await api("/api/backups/cleanup", "POST");
        toast(data.message);
        loadBackups();
    } catch (e) {
        toast(e.message, "error");
    }
}

function downloadBackup(filename) {
    const token = getToken();
    const a = document.createElement("a");
    a.href = `/api/backups/${encodeURIComponent(filename)}/download?token=${encodeURIComponent(token)}`;
    a.download = filename;
    a.click();
}

async function restoreBackup(filename) {
    if (!confirm(`Restore backup '${filename}'? The current configuration will be saved as a backup.`)) return;
    try {
        await api(`/api/backups/${encodeURIComponent(filename)}/restore`, "POST");
        toast("Backup restored. Restart the server to apply.");
        loadConfig();
    } catch (e) {
        toast(e.message, "error");
    }
}

async function viewDiff(filename) {
    try {
        const data = await api(`/api/backups/${encodeURIComponent(filename)}/diff`);
        const diffText = data.diff || data.content || "";
        const lines = diffText.split("\n");
        const coloredLines = lines.map(line => {
            if (line.startsWith("+")) {
                return `<div class="diff-add">${escHtml(line)}</div>`;
            } else if (line.startsWith("-")) {
                return `<div class="diff-remove">${escHtml(line)}</div>`;
            } else {
                return `<div>${escHtml(line)}</div>`;
            }
        }).join("");

        const diffContent = document.getElementById("diffContent");
        if (diffContent) {
            diffContent.innerHTML = `<pre style="margin:0;font-size:13px;line-height:1.5;overflow-x:auto">${coloredLines}</pre>`;
        }
        openModal("diffModal");
    } catch (e) {
        toast("Error loading diff: " + e.message, "error");
    }
}

// =============================================
// API Documentation (Swagger UI)
// =============================================

let _swaggerLoaded = false;

function loadSwaggerUI() {
    if (_swaggerLoaded) return;
    _swaggerLoaded = true;
    const container = document.getElementById("swaggerContainer");

    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css";
    document.head.appendChild(link);

    const script = document.createElement("script");
    script.src = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js";
    script.onload = function() {
        SwaggerUIBundle({
            url: "/static/swagger.json",
            domNode: container,
            presets: [SwaggerUIBundle.presets.apis],
            layout: "BaseLayout",
            deepLinking: true,
            requestInterceptor: function(req) {
                const token = sessionStorage.getItem("jwt_token");
                if (token) req.headers["Authorization"] = "Bearer " + token;
                return req;
            }
        });
    };
    document.body.appendChild(script);
}

// =============================================
// Modal
// =============================================

function openModal(id) {
    document.getElementById(id).classList.add("open");
}

function closeModal(id) {
    document.getElementById(id).classList.remove("open");
}

// =============================================
// Pagination
// =============================================

const PAGE_SIZE = 15;
const pageState = {};

function getPage(key) {
    return pageState[key] || 1;
}

function setPage(key, page) {
    pageState[key] = page;
}

function paginate(items, key) {
    const page = getPage(key);
    const total = items.length;
    const totalPages = Math.ceil(total / PAGE_SIZE);
    const start = (page - 1) * PAGE_SIZE;
    const end = start + PAGE_SIZE;
    return {
        items: items.slice(start, end),
        page,
        totalPages,
        total,
        start: start + 1,
        end: Math.min(end, total)
    };
}

function renderPagination(key, totalPages, total, start, end, reloadFn) {
    if (totalPages <= 1) return "";
    const page = getPage(key);
    let html = `<div class="pagination">`;
    html += `<span class="pagination-info">Showing ${start}-${end} of ${total}</span>`;
    html += `<div class="pagination-buttons">`;
    html += `<button class="btn btn-outline btn-sm" ${page <= 1 ? 'disabled' : ''} onclick="setPage('${key}',1);${reloadFn}()">First</button>`;
    html += `<button class="btn btn-outline btn-sm" ${page <= 1 ? 'disabled' : ''} onclick="setPage('${key}',${page - 1});${reloadFn}()">Prev</button>`;

    // Page numbers - show up to 5 around current page
    const startP = Math.max(1, page - 2);
    const endP = Math.min(totalPages, page + 2);
    for (let i = startP; i <= endP; i++) {
        html += `<button class="btn btn-sm ${i === page ? 'btn-primary' : 'btn-outline'}" onclick="setPage('${key}',${i});${reloadFn}()">${i}</button>`;
    }

    html += `<button class="btn btn-outline btn-sm" ${page >= totalPages ? 'disabled' : ''} onclick="setPage('${key}',${page + 1});${reloadFn}()">Next</button>`;
    html += `<button class="btn btn-outline btn-sm" ${page >= totalPages ? 'disabled' : ''} onclick="setPage('${key}',${totalPages});${reloadFn}()">Last</button>`;
    html += `</div></div>`;
    return html;
}

// =============================================
// Audit Trail
// =============================================

let _auditEntries = [];
let _auditPage = 1;
const _AUDIT_PER_PAGE = 25;

async function loadAudit(page) {
    const container = document.getElementById("auditContent");
    if (!container) return;
    const lines = document.getElementById("auditLines")?.value || 200;
    const filter = document.getElementById("auditFilter")?.value || "";

    if (page) {
        _auditPage = page;
    } else {
        _auditPage = 1;
        try {
            let url = `/api/audit?lines=${lines}`;
            if (filter) url += `&action=${encodeURIComponent(filter)}`;
            const data = await api(url);
            _auditEntries = data.entries || [];
        } catch (e) {
            container.innerHTML = `<p class="text-muted">Error loading audit trail: ${escHtml(e.message)}</p>`;
            return;
        }
    }

    if (_auditEntries.length === 0) {
        container.innerHTML = '<p class="text-muted">No audit entries found.</p>';
        return;
    }

    const totalPages = Math.ceil(_auditEntries.length / _AUDIT_PER_PAGE);
    _auditPage = Math.max(1, Math.min(_auditPage, totalPages));
    const start = (_auditPage - 1) * _AUDIT_PER_PAGE;
    const pageEntries = _auditEntries.slice(start, start + _AUDIT_PER_PAGE);

    let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">`;
    html += `<span class="text-muted" style="font-size:0.8rem">${_auditEntries.length} entries</span>`;
    html += _renderPagination(_auditPage, totalPages, "loadAudit");
    html += `</div>`;

    html += `<table class="table"><thead><tr><th>Timestamp</th><th>User</th><th>Action</th><th>Details</th><th>IP</th></tr></thead><tbody>`;
    for (const e of pageEntries) {
        const ts = e.timestamp ? new Date(e.timestamp).toLocaleString() : "-";
        const action = escHtml(e.action || "");
        const user = escHtml(e.user || "");
        const ip = escHtml(e.ip || "");

        let actionCls = "";
        if (e.action === "login_failed") actionCls = "badge-error";
        else if (e.action === "login") actionCls = "badge-ok";
        else if (e.action?.startsWith("DELETE")) actionCls = "badge-error";
        else if (e.action?.startsWith("POST")) actionCls = "badge-ok";
        else if (e.action?.startsWith("PUT")) actionCls = "badge-warn";

        let details = "";
        if (e.details) {
            if (e.details.message) details = escHtml(e.details.message);
            else if (e.details.status) details = escHtml(e.details.status);
            else if (e.details.username) details = `user: ${escHtml(e.details.username)}`;
            else details = escHtml(JSON.stringify(e.details).substring(0, 100));
        }

        html += `<tr>
            <td style="white-space:nowrap;font-size:0.8rem">${ts}</td>
            <td><strong>${user}</strong></td>
            <td><span class="status-badge ${actionCls}" style="font-size:0.7rem">${action}</span></td>
            <td style="font-size:0.8rem">${details}</td>
            <td style="font-size:0.8rem;font-family:var(--font-mono)">${ip}</td>
        </tr>`;
    }
    html += "</tbody></table>";

    html += `<div style="display:flex;justify-content:flex-end;margin-top:8px">`;
    html += _renderPagination(_auditPage, totalPages, "loadAudit");
    html += `</div>`;

    container.innerHTML = html;
}


// =============================================
// OEE
// =============================================

let _oeeDevices = [];

async function loadOEE() {
    try {
        _oeeDevices = await api("/api/devices");
        const sel = document.getElementById("oeeDevice");
        if (!sel) return;
        sel.innerHTML = _oeeDevices.map(d => {
            const hasOee = d.oeeConfig && d.oeeConfig.enabled;
            return `<option value="${escAttr(d.name)}">${escHtml(d.name)}${hasOee ? " ✓" : ""}</option>`;
        }).join("");
        // Select first device with OEE enabled
        const oeeDevice = _oeeDevices.find(d => d.oeeConfig && d.oeeConfig.enabled);
        if (oeeDevice) sel.value = oeeDevice.name;
        loadOEEData();
    } catch (e) { console.error("loadOEE failed", e); }
}

async function loadOEEData() {
    const device = document.getElementById("oeeDevice")?.value;
    const hours = document.getElementById("oeeHours")?.value || 24;
    if (!device) return;

    const gauges = document.getElementById("oeeGauges");
    const details = document.getElementById("oeeDetails");
    const trend = document.getElementById("oeeTrend");

    // Show loading state
    gauges.innerHTML = '<div class="text-muted" style="grid-column:1/-1;text-align:center;padding:32px">Loading OEE data...</div>';
    details.innerHTML = "";
    trend.innerHTML = '<p class="text-muted" style="text-align:center;padding:16px">Loading trend...</p>';

    // Run both queries in parallel
    const [calcResult, trendResult] = await Promise.allSettled([
        api(`/api/oee/calculate?device=${encodeURIComponent(device)}&hours=${hours}`),
        api(`/api/oee/trend?device=${encodeURIComponent(device)}&hours=${hours}`)
    ]);

    // Render trend (even if calculate failed)
    if (trendResult.status === "fulfilled") {
        renderOEETrend((trendResult.value.trend || []));
    } else {
        trend.innerHTML = '<p class="text-muted" style="text-align:center">No trend data.</p>';
    }

    if (calcResult.status === "rejected") {
        gauges.innerHTML = `<div class="text-muted" style="grid-column:1/-1;text-align:center;padding:24px">${escHtml(calcResult.reason?.message || "OEE not configured for this device")}</div>`;
        details.innerHTML = "";
        return;
    }

    try {
        const data = calcResult.value;

        // Clamp to 0-1 range (negatives from non-monotonic data, >1 from counter rollover)
        const clamp01 = v => Math.max(0, Math.min(1, v || 0));
        const oee = clamp01(data.oee);
        const avail = clamp01(data.availability);
        const perf = clamp01(data.performance);
        const qual = clamp01(data.quality);

        gauges.innerHTML = `
            ${renderOEEGauge("OEE", oee)}
            ${renderOEEGauge("Availability", avail)}
            ${renderOEEGauge("Performance", perf)}
            ${renderOEEGauge("Quality", qual)}
        `;

        const d = data.details || {};
        if (d.plannedTime_h != null) {
            details.innerHTML = `<div style="display:flex;gap:16px;flex-wrap:wrap;font-size:0.85rem">
                <span><strong>Planned:</strong> ${Math.max(0, d.plannedTime_h)}h</span>
                <span><strong>Running:</strong> ${Math.max(0, d.runTime_h || 0)}h</span>
                <span><strong>Downtime:</strong> ${Math.max(0, d.downTime_h || 0)}h</span>
                <span><strong>Total Count:</strong> ${Math.max(0, d.totalCount || 0).toLocaleString()}</span>
                <span><strong>Good:</strong> ${Math.max(0, d.goodCount || 0).toLocaleString()}</span>
                <span><strong>Rejects:</strong> ${Math.max(0, d.rejectCount || 0).toLocaleString()}</span>
                <span><strong>Cycle Time:</strong> ${d.idealCycleTime_s || 0}s</span>
                <span><strong>Samples:</strong> ${(d.samples || 0).toLocaleString()}</span>
            </div>`;
        }

    } catch (e) {
        gauges.innerHTML = `<div class="text-muted" style="grid-column:1/-1;text-align:center;padding:24px">${escHtml(e.message || "OEE not configured for this device")}</div>`;
        details.innerHTML = "";
    }
}

function renderOEEGauge(label, value) {
    const pct = Math.round((value || 0) * 100);
    const color = pct >= 85 ? "#22c55e" : pct >= 60 ? "#f59e0b" : "#ef4444";
    const r = 54, c = 2 * Math.PI * r;
    const offset = c - (c * Math.min(pct, 100) / 100);
    return `<div style="text-align:center;padding:12px;border:1px solid var(--border);border-radius:8px">
        <svg width="130" height="130" viewBox="0 0 130 130">
            <circle cx="65" cy="65" r="${r}" fill="none" stroke="var(--border)" stroke-width="10"/>
            <circle cx="65" cy="65" r="${r}" fill="none" stroke="${color}" stroke-width="10"
                stroke-dasharray="${c}" stroke-dashoffset="${offset}"
                stroke-linecap="round" transform="rotate(-90 65 65)"
                style="transition:stroke-dashoffset 0.5s ease"/>
            <text x="65" y="60" text-anchor="middle" font-size="28" font-weight="bold" fill="var(--text)">${pct}%</text>
            <text x="65" y="80" text-anchor="middle" font-size="11" fill="var(--text-muted)">${label}</text>
        </svg>
    </div>`;
}

function renderOEETrend(trend) {
    const container = document.getElementById("oeeTrend");
    if (!container || !trend.length) {
        container.innerHTML = '<p class="text-muted" style="text-align:center">No trend data.</p>';
        return;
    }
    const w = container.clientWidth || 800, h = 180;
    const pad = { l: 40, r: 10, t: 10, b: 30 };
    const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;

    // Build points
    const points = trend.map((t, i) => {
        const x = pad.l + (i / (trend.length - 1 || 1)) * pw;
        const y = pad.t + ph - (Math.min(t.availability || 0, 1) * ph);
        return `${x},${y}`;
    });

    // Time labels
    const labels = [];
    const step = Math.max(1, Math.floor(trend.length / 6));
    for (let i = 0; i < trend.length; i += step) {
        const x = pad.l + (i / (trend.length - 1 || 1)) * pw;
        const t = trend[i].time ? new Date(trend[i].time) : null;
        const lbl = t ? t.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
        labels.push(`<text x="${x}" y="${h - 5}" text-anchor="middle" font-size="9" fill="var(--text-muted)">${lbl}</text>`);
    }

    // Y axis
    const yLabels = [0, 25, 50, 75, 100].map(v => {
        const y = pad.t + ph - (v / 100 * ph);
        return `<text x="${pad.l - 5}" y="${y + 3}" text-anchor="end" font-size="9" fill="var(--text-muted)">${v}%</text>
                <line x1="${pad.l}" y1="${y}" x2="${w - pad.r}" y2="${y}" stroke="var(--border)" stroke-width="0.5"/>`;
    });

    container.innerHTML = `<svg width="${w}" height="${h}" style="display:block">
        ${yLabels.join("")}
        <polyline points="${points.join(" ")}" fill="none" stroke="#c8102e" stroke-width="2"/>
        ${labels.join("")}
    </svg>`;
}

async function showOEEConfigModal() {
    const deviceName = document.getElementById("oeeDevice")?.value;
    if (!deviceName) return;
    const dev = _oeeDevices.find(d => d.name === deviceName);
    if (!dev) return;

    const tags = (dev.tags || []).concat(dev.calculatedTags || []);
    const cfg = dev.oeeConfig || {};
    const tagOpts = (sel) => tags.map(t => `<option value="${escAttr(t.alias)}"${t.alias === sel ? " selected" : ""}>${escHtml(t.alias)}</option>`).join("");

    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.style.display = "flex";
    overlay.innerHTML = `<div class="modal" style="max-width:480px">
        <h3>OEE Configuration — ${escHtml(deviceName)}</h3>
        <div class="form-group">
            <label>Running Tag <span class="help-icon" tabindex="0">?<span class="tooltip-text">Boolean tag that indicates the machine is running (true/1 = running).</span></span></label>
            <select id="oeeCfgRunning"><option value="">Select...</option>${tagOpts(cfg.runningTag)}</select>
        </div>
        <div class="form-group">
            <label>Production Count Tag <span class="help-icon" tabindex="0">?<span class="tooltip-text">Cumulative counter tag. The difference between first and last value in the period = total produced.</span></span></label>
            <select id="oeeCfgCount"><option value="">Select...</option>${tagOpts(cfg.productionCountTag)}</select>
        </div>
        <div class="form-group">
            <label>Reject Count Tag (optional) <span class="help-icon" tabindex="0">?<span class="tooltip-text">Cumulative reject counter. Leave empty if quality tracking is not needed.</span></span></label>
            <select id="oeeCfgReject"><option value="">(none)</option>${tagOpts(cfg.rejectCountTag)}</select>
        </div>
        <div class="form-row" style="display:flex;gap:12px">
            <div class="form-group" style="flex:1">
                <label>Ideal Cycle Time (s) <span class="help-icon" tabindex="0">?<span class="tooltip-text">Ideal time to produce one unit in seconds. E.g.: 2.5 = one part every 2.5 seconds.</span></span></label>
                <input type="number" id="oeeCfgCycleTime" value="${cfg.idealCycleTime || ""}" step="0.1" min="0.01" placeholder="2.5">
            </div>
            <div class="form-group" style="flex:1">
                <label>Planned Hours/Day <span class="help-icon" tabindex="0">?<span class="tooltip-text">Planned production hours per day. E.g.: 16 for two 8-hour shifts.</span></span></label>
                <input type="number" id="oeeCfgPlannedHours" value="${cfg.plannedHoursPerDay || ""}" step="0.5" min="0.5" max="24" placeholder="16">
            </div>
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
            <button class="btn btn-outline oee-cancel">Cancel</button>
            <button class="btn btn-primary oee-save">Save</button>
        </div>
    </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector(".oee-cancel").addEventListener("click", () => overlay.remove());
    overlay.querySelector(".oee-save").addEventListener("click", () => saveOEEConfig(deviceName));
    overlay.addEventListener("click", e => { if (e.target === overlay) overlay.remove(); });
}

async function saveOEEConfig(deviceName) {
    const body = {
        enabled: true,
        runningTag: document.getElementById("oeeCfgRunning").value,
        productionCountTag: document.getElementById("oeeCfgCount").value,
        rejectCountTag: document.getElementById("oeeCfgReject").value || null,
        idealCycleTime: parseFloat(document.getElementById("oeeCfgCycleTime").value),
        plannedHoursPerDay: parseFloat(document.getElementById("oeeCfgPlannedHours").value)
    };
    if (!body.runningTag) { toast("Running Tag is required", "error"); return; }
    if (!body.productionCountTag) { toast("Production Count Tag is required", "error"); return; }
    if (!body.idealCycleTime || body.idealCycleTime <= 0) { toast("Ideal Cycle Time must be > 0", "error"); return; }
    if (!body.plannedHoursPerDay || body.plannedHoursPerDay <= 0) { toast("Planned Hours/Day must be > 0", "error"); return; }

    try {
        await api(`/api/devices/${encodeURIComponent(deviceName)}/oee-config`, "PUT", body);
        toast("OEE configuration saved");
        document.querySelector(".modal-overlay")?.remove();
        loadOEE();
    } catch (e) {
        toast(e.message, "error");
    }
}


// =============================================
// Reports & Export
// =============================================

async function loadReportFilters() {
    try {
        const devices = await api("/api/devices");
        const devNames = devices.map(d => d.name);
        const plants = [...new Set(devices.map(d => d.plant).filter(Boolean))];

        // Populate device dropdowns
        for (const id of ["reportPdfDevice", "reportCsvDevice"]) {
            const sel = document.getElementById(id);
            if (!sel) continue;
            sel.innerHTML = '<option value="">All devices</option>' +
                devNames.map(n => `<option value="${escAttr(n)}">${escHtml(n)}</option>`).join("");
        }

        // Populate plant dropdowns
        for (const id of ["reportPdfPlant"]) {
            const sel = document.getElementById(id);
            if (!sel) continue;
            sel.innerHTML = '<option value="">All plants</option>' +
                plants.map(p => `<option value="${escAttr(p)}">${escHtml(p)}</option>`).join("");
        }

        // CSV tag dropdown: update when device changes
        const csvDevSel = document.getElementById("reportCsvDevice");
        const csvTagSel = document.getElementById("reportCsvTag");
        if (csvDevSel && csvTagSel) {
            csvDevSel.onchange = () => {
                const selDev = csvDevSel.value;
                const dev = devices.find(d => d.name === selDev);
                const tags = dev ? (dev.tags || []) : [];
                csvTagSel.innerHTML = '<option value="">All tags</option>' +
                    tags.map(t => `<option value="${escAttr(t.alias)}">${escHtml(t.alias)}</option>`).join("");
            };
        }
    } catch {}
}

function downloadReport(type, event) {
    const token = getToken();
    if (!token) { toast("Not logged in", "error"); return; }

    let url;
    if (type === "pdf") {
        const hours = document.getElementById("reportPdfHours").value;
        const device = document.getElementById("reportPdfDevice").value;
        const plant = document.getElementById("reportPdfPlant").value;
        url = `/api/export/pdf?hours=${hours}`;
        if (device) url += `&device=${encodeURIComponent(device)}`;
        if (plant) url += `&plant=${encodeURIComponent(plant)}`;
    } else {
        const hours = document.getElementById("reportCsvHours").value;
        const device = document.getElementById("reportCsvDevice").value;
        const tag = document.getElementById("reportCsvTag").value;
        url = `/api/export/csv?hours=${hours}`;
        if (device) url += `&device=${encodeURIComponent(device)}`;
        if (tag) url += `&tag=${encodeURIComponent(tag)}`;
    }

    // Download via hidden link with auth header
    const btn = event && event.target ? event.target : null;
    if (btn) { btn.disabled = true; btn.textContent = "Generating..."; }
    toast("Generating report...");
    fetch(url, { headers: { "Authorization": "Bearer " + token } })
        .then(resp => {
            if (!resp.ok) return resp.json().then(d => { throw new Error(d.error || "Failed"); });
            return resp.blob().then(blob => {
                const a = document.createElement("a");
                a.href = URL.createObjectURL(blob);
                a.download = resp.headers.get("Content-Disposition")?.match(/filename="(.+)"/)?.[1] || `report.${type}`;
                a.click();
                setTimeout(() => URL.revokeObjectURL(a.href), 60000);
                toast("Report downloaded");
            });
        })
        .catch(e => toast(e.message, "error"))
        .finally(() => { if (btn) { btn.disabled = false; btn.textContent = type === "pdf" ? "Download PDF" : "Download CSV"; } });
}


// =============================================
// Inline Tag Trending
// =============================================

function showTagTrend(device, tag, hours) {
    hours = hours || 1;
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.style.cssText = "display:flex;align-items:center;justify-content:center;z-index:9000;";
    overlay.innerHTML = `
        <div class="modal" style="width:700px;max-width:96vw;padding:24px;position:relative;" onclick="event.stopPropagation()">
            <button onclick="this.closest('.modal-overlay').remove()" style="position:absolute;top:12px;right:14px;background:none;border:none;font-size:20px;cursor:pointer;color:var(--muted);line-height:1;" title="Close">&times;</button>
            <div style="margin-bottom:16px;">
                <div style="font-size:13px;color:var(--muted);margin-bottom:2px;">Tag Trend</div>
                <div style="font-weight:700;font-size:16px;">${escHtml(device)} / ${escHtml(tag)}</div>
            </div>
            <div style="display:flex;gap:8px;margin-bottom:16px;">
                ${["1h","6h","24h","7d"].map(p => {
                    const h = p === "7d" ? 168 : parseInt(p);
                    return `<button class="btn btn-sm${h === hours ? " btn-primary" : " btn-outline"} trend-period-btn" data-hours="${h}" onclick="loadTrendData('${escAttr(device)}','${escAttr(tag)}',${h},this.closest('.modal-overlay'))">${p}</button>`;
                }).join("")}
            </div>
            <div id="trendChart" style="width:100%;height:230px;background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden;display:flex;align-items:center;justify-content:center;">
                <span style="color:var(--muted);font-size:13px;">Loading…</span>
            </div>
            <div id="trendStats" style="margin-top:10px;font-size:12px;color:var(--muted);min-height:18px;display:flex;gap:20px;"></div>
        </div>`;
    overlay.addEventListener("click", () => overlay.remove());
    document.body.appendChild(overlay);
    loadTrendData(device, tag, hours, overlay);
}

async function loadTrendData(device, tag, hours, overlay) {
    const chartEl = overlay.querySelector("#trendChart");
    const statsEl = overlay.querySelector("#trendStats");
    // Update active period button
    overlay.querySelectorAll(".trend-period-btn").forEach(btn => {
        const active = parseInt(btn.dataset.hours) === hours;
        btn.className = "btn btn-sm " + (active ? "btn-primary" : "btn-outline") + " trend-period-btn";
    });
    chartEl.innerHTML = `<span style="color:var(--muted);font-size:13px;">Loading…</span>`;
    statsEl.innerHTML = "";
    try {
        const data = await api(`/api/tags/history?device=${encodeURIComponent(device)}&tag=${encodeURIComponent(tag)}&hours=${hours}`);
        if (data.error) { chartEl.innerHTML = `<span style="color:var(--danger);font-size:13px;">${escHtml(data.error)}</span>`; return; }
        const points = data.points || [];
        if (points.length === 0) {
            chartEl.innerHTML = `<span style="color:var(--muted);font-size:13px;">No data in this period.</span>`;
            return;
        }
        const vals = points.map(p => p.v);
        const dataMin = Math.min(...vals);
        const dataMax = Math.max(...vals);
        const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
        const last = vals[vals.length - 1];
        const fmt = v => (typeof v === "number") ? (Number.isInteger(v) ? v.toString() : v.toFixed(3)) : String(v);
        statsEl.innerHTML = `
            <span><strong>Min:</strong> ${fmt(dataMin)}</span>
            <span><strong>Max:</strong> ${fmt(dataMax)}</span>
            <span><strong>Avg:</strong> ${fmt(avg)}</span>
            <span><strong>Last:</strong> ${fmt(last)}</span>
            <span style="margin-left:auto;"><strong>Points:</strong> ${points.length}</span>`;
        renderTrendChart(chartEl, points, dataMin, dataMax);
    } catch (e) {
        chartEl.innerHTML = `<span style="color:var(--danger);font-size:13px;">${escHtml(e.message)}</span>`;
    }
}

function renderTrendChart(container, points, dataMin, dataMax) {
    const W = container.clientWidth || 660;
    const H = 230;
    const padL = 55, padR = 16, padT = 14, padB = 32;
    const chartW = W - padL - padR;
    const chartH = H - padT - padB;

    const margin = (dataMax - dataMin) * 0.10 || Math.abs(dataMax) * 0.05 || 1;
    const yMin = dataMin - margin;
    const yMax = dataMax + margin;
    const yRange = yMax - yMin || 1;

    const xScale = i => padL + (i / (points.length - 1 || 1)) * chartW;
    const yScale = v => padT + chartH - ((v - yMin) / yRange) * chartH;

    const fmt = v => (typeof v === "number") ? (Math.abs(v) >= 1000 ? v.toFixed(0) : v.toFixed(2)) : String(v);
    const fmtTime = iso => { const d = new Date(iso); return d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}); };

    // Y-axis steps
    const ySteps = 5;
    let gridLines = "";
    let yLabels = "";
    for (let i = 0; i <= ySteps; i++) {
        const val = yMin + (yRange * i / ySteps);
        const y = yScale(val);
        gridLines += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="var(--border)" stroke-width="1"/>`;
        yLabels += `<text x="${padL - 5}" y="${y + 4}" text-anchor="end" font-size="10" fill="var(--muted)">${fmt(val)}</text>`;
    }

    // X-axis labels (6 evenly spaced)
    let xLabels = "";
    const xTicks = 6;
    for (let i = 0; i < xTicks; i++) {
        const idx = Math.round(i * (points.length - 1) / (xTicks - 1));
        const x = xScale(idx);
        xLabels += `<text x="${x}" y="${H - padB + 16}" text-anchor="middle" font-size="10" fill="var(--muted)">${fmtTime(points[idx].t)}</text>`;
    }

    // Polyline
    const polyPts = points.map((p, i) => `${xScale(i)},${yScale(p.v)}`).join(" ");

    container.innerHTML = `<svg width="${W}" height="${H}" style="display:block;">
        ${gridLines}
        ${yLabels}
        ${xLabels}
        <polyline points="${polyPts}" fill="none" stroke="#c8102e" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    </svg>`;
}


// =============================================
// Kiosk Mode
// =============================================

let _kioskInterval = null;
let _kioskScreens = [];
let _kioskIndex = 0;
const _kioskDelay = 30000; // 30 seconds per screen

function toggleKiosk() {
    if (_kioskInterval) {
        exitKiosk();
        return;
    }
    // Build screen list: dashboard + HMI equipment screens + OEE
    _kioskScreens = ["dashboard"];

    // Add HMI equipment screens if available
    if (typeof _hmiConfig !== "undefined" && _hmiConfig && _hmiConfig.plants) {
        for (const plant of _hmiConfig.plants) {
            for (const area of (plant.areas || [])) {
                for (const eq of (area.equipment || [])) {
                    _kioskScreens.push({
                        type: "hmi",
                        plantId: plant.id,
                        areaId: area.id,
                        equipId: eq.id,
                        label: eq.name
                    });
                }
            }
        }
    }

    _kioskScreens.push("oee");
    _kioskIndex = 0;

    document.body.classList.add("kiosk-mode");

    // Add kiosk indicator to header
    let indicator = document.querySelector(".kiosk-indicator");
    if (!indicator) {
        indicator = document.createElement("span");
        indicator.className = "kiosk-indicator";
        indicator.innerHTML = '<span>Kiosk Mode</span> <button class="btn btn-outline btn-sm" style="font-size:0.7rem;padding:1px 6px" onclick="exitKiosk()">Exit (ESC)</button>';
        document.querySelector(".header").appendChild(indicator);
    }
    indicator.style.display = "inline-flex";

    showKioskScreen();
    _kioskInterval = setInterval(() => {
        _kioskIndex = (_kioskIndex + 1) % _kioskScreens.length;
        showKioskScreen();
    }, _kioskDelay);

    document.addEventListener("keydown", kioskEscHandler);
}

function kioskEscHandler(e) {
    if (e.key === "Escape") exitKiosk();
}

function exitKiosk() {
    if (_kioskInterval) { clearInterval(_kioskInterval); _kioskInterval = null; }
    document.body.classList.remove("kiosk-mode");
    document.removeEventListener("keydown", kioskEscHandler);
    const indicator = document.querySelector(".kiosk-indicator");
    if (indicator) indicator.style.display = "none";
    showTab("dashboard");
}

function showKioskScreen() {
    const screen = _kioskScreens[_kioskIndex];
    if (screen === "dashboard") {
        showTab("dashboard");
    } else if (screen === "oee") {
        showTab("oee");
    } else if (screen && screen.type === "hmi") {
        showTab("hmi");
        // Navigate to the specific equipment screen
        if (typeof _hmiCurrentPlant !== "undefined") {
            _hmiCurrentPlant = screen.plantId;
            _hmiCurrentArea = screen.areaId;
            _hmiCurrentEquipment = screen.equipId;
            if (typeof hmiShowScreen === "function") hmiShowScreen();
        }
    }

    // Update indicator
    const indicator = document.querySelector(".kiosk-indicator span");
    if (indicator) {
        const label = typeof screen === "string" ? screen.charAt(0).toUpperCase() + screen.slice(1) : (screen.label || "HMI");
        const total = _kioskScreens.length;
        indicator.textContent = `Kiosk: ${label} (${_kioskIndex + 1}/${total})`;
    }
}

// =============================================
// Alarms
// =============================================

let _alarmRefreshInterval = null;
let _alarmAudioCtx = null;
let _alarmHistoryPage = 0;
const _alarmHistoryPageSize = 20;

async function loadAlarms() {
    if (_alarmRefreshInterval) clearInterval(_alarmRefreshInterval);
    _alarmHistoryPage = 0;
    await renderAlarms();
    await renderAlarmConfig();
    _alarmRefreshInterval = setInterval(renderAlarms, 3000);
}

async function renderAlarms() {
    try {
        const data = await api("/api/alarms");
        const active = Object.values(data.active || {});
        const history = (data.history || []).slice(-50).reverse();

        // Update tab badge
        const tabBtn = document.getElementById("alarmsTabBtn");
        if (tabBtn) {
            const unack = active.filter(a => !a.acknowledged).length;
            tabBtn.innerHTML = unack > 0 ? `Alarms <span style="background:#c8102e;color:#fff;border-radius:10px;padding:1px 6px;font-size:0.7rem;margin-left:4px">${unack}</span>` : "Alarms";
        }

        // Update active count badge
        const countBadge = document.getElementById("alarmsActiveCount");
        if (countBadge) {
            countBadge.textContent = active.length;
            countBadge.className = "status-badge " + (active.some(a => a.severity === "critical") ? "badge-error" : active.length > 0 ? "badge-warn" : "badge-ok");
        }

        // Sound for unacknowledged alarms (with cooldown)
        const soundEnabled = document.getElementById("alarmSoundEnabled");
        if (soundEnabled && soundEnabled.checked) {
            const hasCriticalUnack = active.some(a => a.severity === "critical" && !a.acknowledged);
            const now = Date.now();
            if (hasCriticalUnack && now > _alarmBeepCooldown) {
                alarmBeep();
                _alarmBeepCooldown = now + 30000;
            }
        }

        // Active alarms table
        const activeEl = document.getElementById("alarmsActiveList");
        if (activeEl) {
            if (active.length === 0) {
                activeEl.innerHTML = '<p class="text-muted">No active alarms.</p>';
            } else {
                active.sort((a, b) => (a.severity === "critical" ? 0 : 1) - (b.severity === "critical" ? 0 : 1));
                let html = `<table class="table"><thead><tr><th>Severity</th><th>Device</th><th>Tag</th><th>Value</th><th>Threshold</th><th>Time</th><th>Status</th><th>Action</th></tr></thead><tbody>`;
                for (const a of active) {
                    const cls = a.severity === "critical" ? "badge-error" : "badge-warn";
                    const ackLabel = a.acknowledged ? '<span class="status-badge badge-ok">ACK</span>' : '<span class="status-badge badge-error">UNACK</span>';
                    const time = a.timestamp ? new Date(a.timestamp).toLocaleString() : "-";
                    const ackBtn = a.acknowledged ? "" : `<button class="btn btn-outline btn-sm" onclick="alarmAcknowledge('${escAttr(a.key)}')">ACK</button>`;
                    html += `<tr>
                        <td><span class="status-badge ${cls}">${a.severity.toUpperCase()}</span></td>
                        <td>${escHtml(a.device)}</td>
                        <td>${escHtml(a.tag)}</td>
                        <td><strong>${escHtml(String(a.value))}</strong></td>
                        <td>${a.condition === "high" ? "≥" : "≤"} ${escHtml(String(a.threshold))}</td>
                        <td>${time}</td>
                        <td>${ackLabel}</td>
                        <td>${ackBtn}</td>
                    </tr>`;
                }
                html += "</tbody></table>";
                activeEl.innerHTML = html;
            }
        }

        // History table with pagination
        const histEl = document.getElementById("alarmsHistoryList");
        if (histEl) {
            if (history.length === 0) {
                histEl.innerHTML = '<p class="text-muted">No alarm history.</p>';
            } else {
                const totalPages = Math.ceil(history.length / _alarmHistoryPageSize);
                if (_alarmHistoryPage >= totalPages) _alarmHistoryPage = totalPages - 1;
                if (_alarmHistoryPage < 0) _alarmHistoryPage = 0;
                const start = _alarmHistoryPage * _alarmHistoryPageSize;
                const page = history.slice(start, start + _alarmHistoryPageSize);

                let html = `<table class="table"><thead><tr><th>Severity</th><th>Device</th><th>Tag</th><th>Value</th><th>Status</th><th>Triggered</th><th>Cleared</th></tr></thead><tbody>`;
                for (const a of page) {
                    const cls = a.severity === "critical" ? "badge-error" : "badge-warn";
                    const triggered = a.timestamp ? new Date(a.timestamp).toLocaleString() : "-";
                    const cleared = a.clearedAt ? new Date(a.clearedAt).toLocaleString() : "-";
                    const status = a.status === "escalated" ? '<span class="status-badge badge-warn">ESCALATED</span>' : '<span class="status-badge badge-ok">CLEARED</span>';
                    html += `<tr>
                        <td><span class="status-badge ${cls}">${a.severity.toUpperCase()}</span></td>
                        <td>${escHtml(a.device)}</td>
                        <td>${escHtml(a.tag)}</td>
                        <td>${escHtml(String(a.value))}</td>
                        <td>${status}</td>
                        <td>${triggered}</td>
                        <td>${cleared}</td>
                    </tr>`;
                }
                html += "</tbody></table>";

                // Pagination controls
                html += `<div style="display:flex;align-items:center;justify-content:space-between;margin-top:8px">
                    <span class="text-muted" style="font-size:0.85rem">${history.length} total — page ${_alarmHistoryPage + 1} of ${totalPages}</span>
                    <div style="display:flex;gap:6px">
                        <button class="btn btn-outline btn-sm" onclick="alarmHistoryPage(0)" ${_alarmHistoryPage === 0 ? "disabled" : ""}>First</button>
                        <button class="btn btn-outline btn-sm" onclick="alarmHistoryPage(${_alarmHistoryPage - 1})" ${_alarmHistoryPage === 0 ? "disabled" : ""}>Prev</button>
                        <button class="btn btn-outline btn-sm" onclick="alarmHistoryPage(${_alarmHistoryPage + 1})" ${_alarmHistoryPage >= totalPages - 1 ? "disabled" : ""}>Next</button>
                        <button class="btn btn-outline btn-sm" onclick="alarmHistoryPage(${totalPages - 1})" ${_alarmHistoryPage >= totalPages - 1 ? "disabled" : ""}>Last</button>
                    </div>
                </div>`;
                histEl.innerHTML = html;
            }
        }
    } catch (e) {
        console.error("Failed to load alarms", e);
    }
}

let _alarmConfigIdx = 0;  // unique ID counter for alarm config inputs
let _alarmConfigRegistry = {};  // idx -> {device, tag}
let _alarmConfigDevices = [];   // cached device list for condition dropdowns

async function renderAlarmConfig() {
    const container = document.getElementById("alarmConfigList");
    if (!container) return;
    _alarmConfigIdx = 0;
    _alarmConfigRegistry = {};
    try {
        const devices = await api("/api/devices");
        if (!devices || devices.length === 0) {
            container.innerHTML = '<p class="text-muted">No devices configured.</p>';
            return;
        }

        _alarmConfigDevices = devices;

        // Build device/tag options for condition tag dropdowns
        const allTags = [];
        for (const d of devices) {
            for (const t of (d.tags || [])) {
                allTags.push({ device: d.name, tag: t.alias });
            }
        }

        let html = "";
        for (const dev of devices) {
            const tags = dev.tags || [];
            if (tags.length === 0) continue;
            const configuredCount = tags.filter(t => t.alarmThresholds && Object.keys(t.alarmThresholds).length > 0).length;
            const badge = configuredCount > 0 ? ` <span class="status-badge badge-ok">${configuredCount} configured</span>` : "";
            const devId = "alarmCfg-" + dev.name.replace(/[^a-zA-Z0-9]/g, "_");
            html += `<div style="border:1px solid var(--border);border-radius:6px;margin-bottom:8px">
                <div style="padding:10px 14px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;background:var(--bg-secondary);border-radius:6px" onclick="document.getElementById('${devId}').style.display=document.getElementById('${devId}').style.display==='none'?'block':'none';this.querySelector('.chevron').textContent=document.getElementById('${devId}').style.display==='none'?'▶':'▼'">
                    <span><strong>${escHtml(dev.name)}</strong> <span class="text-muted">(${tags.length} tags)</span>${badge}</span>
                    <span class="chevron" style="font-size:0.75rem">▶</span>
                </div>
                <div id="${devId}" style="display:none;padding:0 14px 10px">`;
            for (const tag of tags) {
                const idx = _alarmConfigIdx++;
                _alarmConfigRegistry[idx] = { device: dev.name, tag: tag.alias };
                const th = tag.alarmThresholds || {};
                const hasProfiles = th.profiles && th.profiles.length > 0;
                const condDevice = th.conditionDevice || "";
                const condTag = th.conditionTag || "";

                html += `<div style="border:1px solid var(--border);border-radius:4px;padding:10px;margin-top:8px" id="acfg-${idx}">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                        <strong>${escHtml(tag.alias)}</strong>
                        <div style="display:flex;gap:6px">
                            <button class="btn btn-outline btn-sm" onclick="event.stopPropagation();alarmAddProfile(${idx})" title="Add condition profile">+ Profile</button>
                            <button class="btn btn-primary btn-sm" onclick="event.stopPropagation();alarmSaveThresholds(${idx})">Save</button>
                        </div>
                    </div>
                    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:4px">
                        <label style="font-size:0.8rem;display:flex;align-items:center;gap:4px">WL <input type="number" data-field="wl" value="${th.warningLow ?? ""}" placeholder="-" style="width:75px"></label>
                        <label style="font-size:0.8rem;display:flex;align-items:center;gap:4px">WH <input type="number" data-field="wh" value="${th.warningHigh ?? ""}" placeholder="-" style="width:75px"></label>
                        <label style="font-size:0.8rem;display:flex;align-items:center;gap:4px">CL <input type="number" data-field="cl" value="${th.criticalLow ?? ""}" placeholder="-" style="width:75px"></label>
                        <label style="font-size:0.8rem;display:flex;align-items:center;gap:4px">CH <input type="number" data-field="ch" value="${th.criticalHigh ?? ""}" placeholder="-" style="width:75px"></label>
                    </div>`;

                // Condition tag selector (shown if profiles exist)
                const devNames = [...new Set(allTags.map(t => t.device))];
                const condDevOpts = devNames.map(d => `<option value="${escAttr(d)}"${d === condDevice ? " selected" : ""}>${escHtml(d)}</option>`).join("");
                const condTagOpts = allTags.filter(t => t.device === (condDevice || devNames[0] || "")).map(t => `<option value="${escAttr(t.tag)}"${t.tag === condTag ? " selected" : ""}>${escHtml(t.tag)}</option>`).join("");
                html += `<div data-profiles style="display:${hasProfiles ? "block" : "none"};margin-top:8px;border-top:1px solid var(--border);padding-top:8px">
                    <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
                        <label style="font-size:0.8rem;white-space:nowrap">Condition Device:</label>
                        <select data-field="condDevice" style="flex:1" onchange="this.style.border='';alarmCondDeviceChanged(${idx})"><option value="">(none)</option>${condDevOpts}</select>
                        <label style="font-size:0.8rem;white-space:nowrap">Tag:</label>
                        <select data-field="condTag" style="flex:1" onchange="this.style.border=''"><option value="">(none)</option>${condTagOpts}</select>
                    </div>
                    <div data-profile-list>`;

                // Render existing profiles
                if (hasProfiles) {
                    for (const p of th.profiles) {
                        html += alarmProfileRowHtml(p);
                    }
                }
                html += `</div></div></div>`;
            }
            html += "</div></div>";
        }
        container.innerHTML = html || '<p class="text-muted">No tags configured.</p>';
    } catch (e) {
        container.innerHTML = '<p class="text-muted">Error loading devices.</p>';
    }
}

function alarmProfileRowHtml(p) {
    p = p || {};
    return `<div data-profile-row style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:4px;padding:6px;background:var(--bg-secondary);border-radius:4px">
        <label style="font-size:0.8rem;display:flex;align-items:center;gap:4px">When = <input type="text" data-field="pv" value="${escAttr(String(p.whenValue ?? ""))}" style="width:60px" placeholder="value"></label>
        <label style="font-size:0.8rem;display:flex;align-items:center;gap:4px">Label <input type="text" data-field="pl" value="${escAttr(p.label || "")}" style="width:80px" placeholder="optional"></label>
        <label style="font-size:0.8rem;display:flex;align-items:center;gap:4px">WL <input type="number" data-field="pwl" value="${p.warningLow ?? ""}" placeholder="-" style="width:65px"></label>
        <label style="font-size:0.8rem;display:flex;align-items:center;gap:4px">WH <input type="number" data-field="pwh" value="${p.warningHigh ?? ""}" placeholder="-" style="width:65px"></label>
        <label style="font-size:0.8rem;display:flex;align-items:center;gap:4px">CL <input type="number" data-field="pcl" value="${p.criticalLow ?? ""}" placeholder="-" style="width:65px"></label>
        <label style="font-size:0.8rem;display:flex;align-items:center;gap:4px">CH <input type="number" data-field="pch" value="${p.criticalHigh ?? ""}" placeholder="-" style="width:65px"></label>
        <button class="btn btn-danger btn-sm" onclick="this.closest('[data-profile-row]').remove()" style="padding:2px 6px">&times;</button>
    </div>`;
}

function alarmCondDeviceChanged(idx) {
    const el = document.getElementById(`acfg-${idx}`);
    if (!el) return;
    const devSelect = el.querySelector('[data-field="condDevice"]');
    const tagSelect = el.querySelector('[data-field="condTag"]');
    if (!devSelect || !tagSelect) return;
    const selectedDev = devSelect.value;
    const dev = _alarmConfigDevices.find(d => d.name === selectedDev);
    const tags = dev ? (dev.tags || []) : [];
    tagSelect.innerHTML = '<option value="">(none)</option>' + tags.map(t => `<option value="${escAttr(t.alias)}">${escHtml(t.alias)}</option>`).join("");
}

function alarmAddProfile(idx) {
    const container = document.querySelector(`#acfg-${idx} [data-profile-list]`);
    const profilesDiv = document.querySelector(`#acfg-${idx} [data-profiles]`);
    if (!container || !profilesDiv) return;
    profilesDiv.style.display = "block";
    container.insertAdjacentHTML("beforeend", alarmProfileRowHtml({}));
}

async function alarmSaveThresholds(idx) {
    const reg = _alarmConfigRegistry[idx];
    if (!reg) return;
    const el = document.getElementById(`acfg-${idx}`);
    if (!el) return;

    const getVal = (field) => {
        const input = el.querySelector(`[data-field="${field}"]`);
        return input && input.value !== "" ? parseFloat(input.value) : null;
    };
    const body = {
        warningLow: getVal("wl"),
        warningHigh: getVal("wh"),
        criticalLow: getVal("cl"),
        criticalHigh: getVal("ch")
    };

    // Collect profiles
    const profileRows = el.querySelectorAll("[data-profile-row]");
    if (profileRows.length > 0) {
        const condDevSelect = el.querySelector('[data-field="condDevice"]');
        const condTagSelect = el.querySelector('[data-field="condTag"]');
        const condDev = condDevSelect ? condDevSelect.value : "";
        const condTag = condTagSelect ? condTagSelect.value : "";
        if (!condDev || !condTag) {
            toast("Select Condition Device and Tag before saving profiles", "error");
            if (condDevSelect && !condDev) { condDevSelect.style.border = "2px solid #c8102e"; condDevSelect.focus(); }
            else if (condTagSelect && !condTag) { condTagSelect.style.border = "2px solid #c8102e"; condTagSelect.focus(); }
            return;
        }
        body.conditionDevice = condDev;
        body.conditionTag = condTag;
        body.profiles = [];
        for (const row of profileRows) {
            const pv = row.querySelector('[data-field="pv"]');
            const pl = row.querySelector('[data-field="pl"]');
            const pvVal = pv ? pv.value.trim() : "";
            if (!pvVal) {
                if (pv) { pv.style.border = "2px solid #c8102e"; pv.focus(); }
                toast("'When' value is required for each profile", "error");
                return;
            }
            pv.style.border = "";
            const profile = { whenValue: isNaN(pvVal) || pvVal === "" ? pvVal : parseFloat(pvVal) };
            if (pl && pl.value) profile.label = pl.value;
            const pwl = row.querySelector('[data-field="pwl"]');
            const pwh = row.querySelector('[data-field="pwh"]');
            const pcl = row.querySelector('[data-field="pcl"]');
            const pch = row.querySelector('[data-field="pch"]');
            if (pwl && pwl.value !== "") profile.warningLow = parseFloat(pwl.value);
            if (pwh && pwh.value !== "") profile.warningHigh = parseFloat(pwh.value);
            if (pcl && pcl.value !== "") profile.criticalLow = parseFloat(pcl.value);
            if (pch && pch.value !== "") profile.criticalHigh = parseFloat(pch.value);
            body.profiles.push(profile);
        }
    }

    try {
        await api(`/api/devices/${encodeURIComponent(reg.device)}/tags/${encodeURIComponent(reg.tag)}/alarms`, "PUT", body);
        toast("Alarm thresholds saved for " + reg.tag);
    } catch (e) {
        toast(e.message, "error");
    }
}

async function alarmAcknowledge(key) {
    try {
        await api("/api/alarms/acknowledge", "POST", { key });
        toast("Alarm acknowledged");
        renderAlarms();
    } catch (e) {
        toast(e.message, "error");
    }
}

function alarmHistoryPage(page) {
    _alarmHistoryPage = Math.max(0, page);
    renderAlarms();
}

async function alarmAcknowledgeAll() {
    try {
        const resp = await api("/api/alarms/acknowledge-all", "POST");
        toast(resp.message);
        renderAlarms();
    } catch (e) {
        toast(e.message, "error");
    }
}

function alarmBeep() {
    try {
        if (!_alarmAudioCtx) _alarmAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
        if (_alarmAudioCtx.state === "suspended") _alarmAudioCtx.resume();
        const osc = _alarmAudioCtx.createOscillator();
        const gain = _alarmAudioCtx.createGain();
        osc.connect(gain);
        gain.connect(_alarmAudioCtx.destination);
        osc.frequency.value = 880;
        osc.type = "square";
        gain.gain.value = 0.15;
        osc.start();
        osc.stop(_alarmAudioCtx.currentTime + 0.15);
    } catch {}
}

// Background alarm checker (runs even when not on Alarms tab)
let _alarmBeepCooldown = 0;
setInterval(async () => {
    if (!getToken()) return;  // Don't poll if not logged in
    if (_alarmRefreshInterval) return;  // Alarms tab is handling it
    try {
        const data = await api("/api/alarms");
        const active = Object.values(data.active || {});
        const unack = active.filter(a => !a.acknowledged).length;

        // Update tab badge
        const tabBtn = document.getElementById("alarmsTabBtn");
        if (tabBtn) {
            tabBtn.innerHTML = unack > 0 ? `Alarms <span style="background:#c8102e;color:#fff;border-radius:10px;padding:1px 6px;font-size:0.7rem;margin-left:4px">${unack}</span>` : "Alarms";
        }

        // Sound alert (with 30s cooldown)
        const soundCb = document.getElementById("alarmSoundEnabled");
        const soundEnabled = soundCb ? soundCb.checked : false;
        const now = Date.now();
        if (soundEnabled && active.some(a => a.severity === "critical" && !a.acknowledged) && now > _alarmBeepCooldown) {
            alarmBeep();
            _alarmBeepCooldown = now + 30000;
        }
    } catch {}
}, 5000);


// =============================================
// Shift Logbook
// =============================================

function showLogbookForm() {
    if (!canWrite()) { toast("Write not allowed for monitor role", "error"); return; }
    const form = document.getElementById("logbookForm");
    if (!form) return;
    form.style.display = "block";

    // Auto-detect current shift from local hour
    const hour = new Date().getHours();
    let shift = "night";
    if (hour >= 6 && hour < 14) shift = "morning";
    else if (hour >= 14 && hour < 22) shift = "afternoon";
    const shiftSel = document.getElementById("logbookShift");
    if (shiftSel) shiftSel.value = shift;

    const ta = document.getElementById("logbookMessage");
    if (ta) ta.focus();
}

async function saveLogbookEntry() {
    const message = (document.getElementById("logbookMessage")?.value || "").trim();
    if (!message) {
        toast("Message is required.", "error");
        return;
    }
    const shift = document.getElementById("logbookShift")?.value || "morning";
    const category = document.getElementById("logbookCategory")?.value || "observation";
    const priority = document.getElementById("logbookPriority")?.value || "normal";
    try {
        await api("/api/logbook", "POST", { shift, category, priority, message });
        document.getElementById("logbookForm").style.display = "none";
        const ta = document.getElementById("logbookMessage");
        if (ta) ta.value = "";
        toast("Entry saved.", "success");
        loadLogbook();
    } catch (e) {
        toast("Failed to save entry: " + e.message, "error");
    }
}

let _logbookEntries = [];
let _logbookPage = 1;
const _LOGBOOK_PER_PAGE = 15;

async function loadLogbook(page) {
    const container = document.getElementById("logbookEntries");
    if (!container) return;
    const shiftFilter = document.getElementById("logbookShiftFilter")?.value || "";

    if (page) {
        _logbookPage = page;
    } else {
        _logbookPage = 1;
        try {
            let url = "/api/logbook?lines=1000";
            if (shiftFilter) url += `&shift=${encodeURIComponent(shiftFilter)}`;
            const data = await api(url);
            _logbookEntries = data.entries || [];
        } catch (e) {
            container.innerHTML = `<p class="text-muted">Error loading logbook: ${escHtml(e.message)}</p>`;
            return;
        }
    }

    if (_logbookEntries.length === 0) {
        container.innerHTML = '<p class="text-muted">No logbook entries found.</p>';
        return;
    }

    const totalPages = Math.ceil(_logbookEntries.length / _LOGBOOK_PER_PAGE);
    _logbookPage = Math.max(1, Math.min(_logbookPage, totalPages));
    const start = (_logbookPage - 1) * _LOGBOOK_PER_PAGE;
    const pageEntries = _logbookEntries.slice(start, start + _LOGBOOK_PER_PAGE);

    const priorityBorder = { critical: "#c8102e", important: "#e07b00" };
    const priorityBadge = { critical: "badge-error", important: "badge-warn", normal: "badge-ok" };
    const categoryBadge = { incident: "badge-error", alarm: "badge-warn", maintenance: "badge-info", handover: "badge-info", observation: "" };

    let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">`;
    html += `<span class="text-muted" style="font-size:0.8rem">${_logbookEntries.length} entries</span>`;
    html += _renderPagination(_logbookPage, totalPages, "loadLogbook");
    html += `</div>`;

    for (const e of pageEntries) {
        const ts = e.timestamp ? e.timestamp.replace("T", " ").replace("Z", " UTC") : "";
        const border = priorityBorder[e.priority] ? `border-left:4px solid ${priorityBorder[e.priority]};` : "";
        const pBadge = priorityBadge[e.priority] || "";
        const cBadge = categoryBadge[e.category] || "badge-neutral";
        html += `<div style="padding:10px 12px;margin-bottom:8px;background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);${border}">`;
        html += `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px">`;
        html += `<strong style="font-size:0.85rem">${escHtml(e.user || "unknown")}</strong>`;
        html += `<span class="status-badge ${cBadge}" style="font-size:0.7rem">${escHtml(e.category || "")}</span>`;
        if (e.priority && e.priority !== "normal") {
            html += `<span class="status-badge ${pBadge}" style="font-size:0.7rem">${escHtml(e.priority)}</span>`;
        }
        html += `<span style="font-size:0.75rem;color:var(--text-secondary)">${escHtml(e.shift || "")}</span>`;
        html += `<span style="font-size:0.75rem;color:var(--text-secondary);margin-left:auto">${escHtml(ts)}</span>`;
        html += `</div>`;
        html += `<div style="font-size:0.88rem;white-space:pre-wrap;line-height:1.5">${escHtml(e.message || "")}</div>`;
        html += `</div>`;
    }

    html += `<div style="display:flex;justify-content:flex-end;margin-top:8px">`;
    html += _renderPagination(_logbookPage, totalPages, "loadLogbook");
    html += `</div>`;

    container.innerHTML = html;
}


// =============================================
// Analytics (Grafana embedded)
// =============================================

function _grafanaBaseUrl() {
    // Grafana runs on port 3000 on the same host
    return window.location.protocol + "//" + window.location.hostname + ":3000";
}

function loadGrafanaDashboard() {
    const frame = document.getElementById("grafanaFrame");
    if (!frame) return;
    const uid = document.getElementById("grafanaDashboard")?.value || "plc4x-overview";
    const timeRange = document.getElementById("grafanaTimeRange")?.value || "now-24h";
    const theme = "light";
    frame.src = `${_grafanaBaseUrl()}/d/${uid}/?orgId=1&from=${timeRange}&to=now&theme=${theme}&kiosk`;
}

function openGrafanaFullscreen() {
    const uid = document.getElementById("grafanaDashboard")?.value || "plc4x-overview";
    const timeRange = document.getElementById("grafanaTimeRange")?.value || "now-24h";
    window.open(`${_grafanaBaseUrl()}/d/${uid}/?orgId=1&from=${timeRange}&to=now`, "_blank");
}


// =============================================
// Utils
// =============================================

function _renderPagination(currentPage, totalPages, fnName) {
    if (totalPages <= 1) return "";
    let html = `<div style="display:flex;gap:4px;align-items:center;font-size:0.8rem">`;
    html += `<button class="btn btn-outline btn-sm" onclick="${fnName}(1)" ${currentPage === 1 ? "disabled" : ""} style="padding:2px 8px">&laquo;</button>`;
    html += `<button class="btn btn-outline btn-sm" onclick="${fnName}(${currentPage - 1})" ${currentPage === 1 ? "disabled" : ""} style="padding:2px 8px">&lsaquo;</button>`;

    const maxVisible = 5;
    let startPage = Math.max(1, currentPage - Math.floor(maxVisible / 2));
    let endPage = Math.min(totalPages, startPage + maxVisible - 1);
    if (endPage - startPage < maxVisible - 1) startPage = Math.max(1, endPage - maxVisible + 1);

    if (startPage > 1) html += `<span style="color:var(--text-secondary);padding:0 4px">...</span>`;
    for (let i = startPage; i <= endPage; i++) {
        if (i === currentPage) {
            html += `<button class="btn btn-primary btn-sm" style="padding:2px 8px;min-width:28px">${i}</button>`;
        } else {
            html += `<button class="btn btn-outline btn-sm" onclick="${fnName}(${i})" style="padding:2px 8px;min-width:28px">${i}</button>`;
        }
    }
    if (endPage < totalPages) html += `<span style="color:var(--text-secondary);padding:0 4px">...</span>`;

    html += `<button class="btn btn-outline btn-sm" onclick="${fnName}(${currentPage + 1})" ${currentPage === totalPages ? "disabled" : ""} style="padding:2px 8px">&rsaquo;</button>`;
    html += `<button class="btn btn-outline btn-sm" onclick="${fnName}(${totalPages})" ${currentPage === totalPages ? "disabled" : ""} style="padding:2px 8px">&raquo;</button>`;
    html += `<span style="color:var(--text-secondary);margin-left:8px">Page ${currentPage} of ${totalPages}</span>`;
    html += `</div>`;
    return html;
}

function escHtml(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

function escAttr(str) {
    if (!str) return "";
    return escHtml(str).replace(/'/g, "&#39;");
}

// =============================================
// AI / Machine Learning
// =============================================

async function loadML() {
    // Hide config section for non-admin users
    const cfgCard = document.getElementById("mlConfigCard");
    if (cfgCard) cfgCard.style.display = isAdmin() ? "" : "none";

    // Populate device selector from live cache
    _mlPopulateDeviceSelector();

    await Promise.all([
        loadMLStatus(),
        loadMLAlerts(),
        loadMLConfig(),
    ]);
    loadMLResults();
}

function _mlPopulateDeviceSelector() {
    const sel = document.getElementById("mlResultsDevice");
    if (!sel) return;
    // Reuse devices from the live cache if available
    try {
        const cache = window._liveCache || {};
        const devices = cache.devices || [];
        if (devices.length > 0) {
            const current = sel.value;
            sel.innerHTML = '<option value="">Select device...</option>';
            devices.forEach(d => {
                const opt = document.createElement("option");
                opt.value = d.name;
                opt.textContent = d.name + (d.plant ? ` (${d.plant})` : "");
                sel.appendChild(opt);
            });
            if (current) sel.value = current;
            return;
        }
    } catch (_) {}
    // Fallback: fetch from API
    apiFetch("/api/live/status").then(data => {
        const devices = (data && data.devices) ? data.devices : [];
        const current = sel.value;
        sel.innerHTML = '<option value="">Select device...</option>';
        devices.forEach(d => {
            const opt = document.createElement("option");
            opt.value = d.name;
            opt.textContent = d.name + (d.plant ? ` (${d.plant})` : "");
            sel.appendChild(opt);
        });
        if (current) sel.value = current;
    }).catch(() => {});
}

async function loadMLStatus() {
    const body = document.getElementById("mlStatusBody");
    try {
        const data = await apiFetch("/api/ml/status");

        // Engine status indicator
        const indicator = document.getElementById("mlEngineIndicator");
        const label = document.getElementById("mlEngineLabel");
        if (indicator && label) {
            indicator.style.background = data.engine_online ? "#27ae60" : "#c8102e";
            label.textContent = data.engine_online ? "Engine Online" : "Engine Offline";
            label.style.color = data.engine_online ? "#27ae60" : "#c8102e";
        }

        // Last run
        const lastRunEl = document.getElementById("mlLastRun");
        if (lastRunEl) {
            if (data.last_run) {
                const d = new Date(data.last_run);
                lastRunEl.textContent = d.toLocaleString();
            } else {
                lastRunEl.textContent = "Never";
            }
        }

        const tagsEl = document.getElementById("mlTagsAnalyzed");
        if (tagsEl) tagsEl.textContent = data.tags_analyzed != null ? data.tags_analyzed : "—";

        const durEl = document.getElementById("mlCycleDuration");
        if (durEl) durEl.textContent = data.cycle_duration_s != null ? `${data.cycle_duration_s}s` : "—";

        const ciEl = document.getElementById("mlCycleInterval");
        if (ciEl) ciEl.textContent = data.cycle_interval_minutes != null ? `${data.cycle_interval_minutes} min` : "—";

        const fhEl = document.getElementById("mlForecastHours");
        if (fhEl) fhEl.textContent = data.forecast_hours != null ? `${data.forecast_hours}h` : "—";

        const mpEl = document.getElementById("mlMinPoints");
        if (mpEl) mpEl.textContent = data.min_points != null ? data.min_points : "—";

        // Module toggles
        const modList = document.getElementById("mlModuleList");
        if (modList && data.modules) {
            const moduleLabels = {
                anomaly: "Anomaly",
                explainability: "SHAP",
                correlation: "Correlation",
                changepoint: "Change Points",
                pattern: "Patterns",
            };
            modList.innerHTML = Object.entries(data.modules).map(([key, enabled]) => {
                const color = enabled ? "#27ae60" : "#999";
                const bg = enabled ? "rgba(39,174,96,0.1)" : "rgba(153,153,153,0.1)";
                return `<span style="font-size:0.75rem;padding:2px 8px;border-radius:12px;background:${bg};color:${color};border:1px solid ${color}">${escHtml(moduleLabels[key] || key)}</span>`;
            }).join("");
        }
    } catch (e) {
        if (body) body.innerHTML = `<div style="color:var(--text-muted);font-size:0.85rem">ML engine status unavailable. Is the ML container running?</div>`;
    }
}

async function loadMLAlerts() {
    const container = document.getElementById("mlAlerts");
    if (!container) return;
    const hours = document.getElementById("mlAlertHours")?.value || "24";
    container.innerHTML = '<div style="color:var(--text-muted);font-size:0.85rem">Loading alerts...</div>';
    try {
        const data = await apiFetch(`/api/ml/alerts?hours=${hours}`);
        const alerts = data.alerts || [];
        if (!alerts.length) {
            container.innerHTML = '<div style="color:var(--text-muted);font-size:0.85rem">No ML alerts in the selected period. The engine may still be warming up.</div>';
            return;
        }
        container.innerHTML = alerts.map(a => _renderMLAlert(a)).join("");
    } catch (e) {
        container.innerHTML = `<div style="color:var(--danger);font-size:0.85rem">Failed to load alerts: ${escHtml(e.message)}</div>`;
    }
}

function _renderMLAlert(a) {
    let borderColor, icon, typeLabel;
    if (a.type === "anomaly") {
        borderColor = "#c8102e";
        icon = "&#9888;";
        typeLabel = "Anomaly Detected";
    } else if (a.type === "corr_broken") {
        borderColor = "#e67e22";
        icon = "&#128279;";
        typeLabel = "Broken Correlation";
    } else {
        borderColor = "#2980b9";
        icon = "&#128200;";
        typeLabel = "Change Point";
    }

    const timeStr = a.time ? new Date(a.time).toLocaleString() : "";
    let detail = "";

    if (a.type === "anomaly") {
        if (a.confidence != null) detail += `<span>Confidence: <strong>${escHtml(String(a.confidence))}%</strong></span>`;
        if (a.agreeing != null) detail += `<span style="margin-left:12px">Algorithms: <strong>${escHtml(String(a.agreeing))}/3</strong></span>`;
        if (a.shap_contributions) {
            const contribs = Object.entries(a.shap_contributions)
                .sort((x, y) => Math.abs(y[1]) - Math.abs(x[1]))
                .slice(0, 3)
                .map(([k, v]) => `${escHtml(k)} (${v > 0 ? "+" : ""}${escHtml(String(Math.round(v * 100) / 100))})`).join(", ");
            detail += `<div style="margin-top:4px;font-size:0.78rem;color:var(--text-muted)">Root cause: ${contribs}</div>`;
        }
    } else if (a.type === "corr_broken") {
        const tagB = a.tag_b ? ` &harr; <strong>${escHtml(a.tag_b)}</strong>` : "";
        detail += `<span>${tagB}</span>`;
        if (a.baseline_corr != null && a.recent_corr != null) {
            detail += `<span style="margin-left:12px">Was: <strong>${Math.round(a.baseline_corr * 100) / 100}</strong> &rarr; Now: <strong>${Math.round(a.recent_corr * 100) / 100}</strong></span>`;
        }
    } else if (a.type === "change_point") {
        if (a.mean_before != null && a.mean_after != null) {
            const pct = a.mean_before !== 0 ? Math.round(((a.mean_after - a.mean_before) / Math.abs(a.mean_before)) * 100) : null;
            detail += `<span>Mean: <strong>${Math.round(a.mean_before * 100) / 100}</strong> &rarr; <strong>${Math.round(a.mean_after * 100) / 100}</strong>`;
            if (pct != null) detail += ` (${pct > 0 ? "+" : ""}${pct}%)`;
            detail += `</span>`;
        }
        if (a.severity != null) {
            detail += `<span style="margin-left:12px">Severity: <strong>${Math.round(a.severity * 100) / 100}</strong></span>`;
        }
    }

    return `<div style="border-left:4px solid ${borderColor};padding:10px 14px;background:rgba(0,0,0,0.02);border-radius:0 6px 6px 0;margin-bottom:2px">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span style="color:${borderColor};font-size:0.85rem;font-weight:700">${icon} ${escHtml(typeLabel)}</span>
            <span style="font-weight:600;font-size:0.85rem">${escHtml(a.device || "")} / ${escHtml(a.tag || "")}</span>
            ${a.plant ? `<span style="font-size:0.75rem;color:var(--text-muted)">${escHtml(a.plant)}</span>` : ""}
            <span style="margin-left:auto;font-size:0.75rem;color:var(--text-muted)">${escHtml(timeStr)}</span>
        </div>
        <div style="margin-top:4px;font-size:0.82rem;color:var(--text-secondary)">${detail}</div>
    </div>`;
}

async function loadMLResults() {
    const device = document.getElementById("mlResultsDevice")?.value;
    const hours = document.getElementById("mlResultsHours")?.value || "6";

    const corrEl = document.getElementById("mlCorrMatrix");
    const cpEl = document.getElementById("mlChangePoints");
    const patEl = document.getElementById("mlPatterns");

    if (!device) {
        if (corrEl) corrEl.innerHTML = '<div style="color:var(--text-muted);font-size:0.85rem">Select a device above to view analysis results.</div>';
        return;
    }

    // Load correlation matrix in parallel with full results
    const [corrData, resultsData] = await Promise.allSettled([
        apiFetch(`/api/ml/correlation?device=${encodeURIComponent(device)}&hours=${hours}`),
        apiFetch(`/api/ml/results?device=${encodeURIComponent(device)}&hours=${hours}`),
    ]);

    // Render correlation matrix
    if (corrEl) {
        if (corrData.status === "fulfilled" && corrData.value && corrData.value.tags && corrData.value.tags.length > 0) {
            corrEl.innerHTML = _renderCorrMatrix(corrData.value);
        } else {
            corrEl.innerHTML = '<div style="color:var(--text-muted);font-size:0.85rem">No correlation data available for this device and period.</div>';
        }
    }

    // Render change points and patterns from results
    if (resultsData.status === "fulfilled" && resultsData.value && resultsData.value.results) {
        const r = resultsData.value.results;
        if (cpEl) cpEl.innerHTML = _renderChangePoints(r.change_point || []);
        if (patEl) patEl.innerHTML = _renderPatterns(r.motif || [], r.discord || []);
    } else {
        if (cpEl) cpEl.innerHTML = '<div style="color:var(--text-muted);font-size:0.85rem">No change point data available.</div>';
        if (patEl) patEl.innerHTML = '<div style="color:var(--text-muted);font-size:0.85rem">No pattern data available.</div>';
    }
}

function _renderCorrMatrix(data) {
    const tags = data.tags || [];
    const matrix = data.matrix || {};
    if (!tags.length) return '<div style="color:var(--text-muted);font-size:0.85rem">No correlation data.</div>';

    let html = '<table style="border-collapse:collapse;font-size:0.75rem;white-space:nowrap">';
    // Header row
    html += '<tr><th style="padding:4px 6px;text-align:right;min-width:80px"></th>';
    tags.forEach(t => {
        html += `<th style="padding:4px 6px;text-align:center;font-weight:600;max-width:80px;overflow:hidden;text-overflow:ellipsis" title="${escAttr(t)}">${escHtml(t.length > 10 ? t.slice(0, 10) + "…" : t)}</th>`;
    });
    html += "</tr>";
    // Data rows
    tags.forEach(rowTag => {
        html += `<tr><td style="padding:4px 6px;font-weight:600;text-align:right;max-width:100px;overflow:hidden;text-overflow:ellipsis" title="${escAttr(rowTag)}">${escHtml(rowTag.length > 12 ? rowTag.slice(0, 12) + "…" : rowTag)}</td>`;
        tags.forEach(colTag => {
            const val = (matrix[rowTag] || {})[colTag];
            if (val == null) {
                html += '<td style="padding:4px 6px;text-align:center;color:var(--text-muted)">—</td>';
                return;
            }
            const abs = Math.abs(val);
            let bg = "transparent";
            let color = "var(--text-primary)";
            let fw = "normal";
            if (rowTag === colTag) {
                bg = "#f0f0f0";
            } else if (val > 0) {
                const intensity = Math.round(abs * 50);
                bg = `rgba(39,174,96,${Math.min(abs * 0.8, 0.7)})`;
                color = abs > 0.6 ? "#fff" : "inherit";
            } else {
                bg = `rgba(200,16,46,${Math.min(abs * 0.8, 0.7)})`;
                color = abs > 0.6 ? "#fff" : "inherit";
            }
            if (abs > 0.7) fw = "700";
            html += `<td style="padding:4px 8px;text-align:center;background:${bg};color:${color};font-weight:${fw};border-radius:3px" title="${escAttr(rowTag + ' ↔ ' + colTag + ': ' + val)}">${Math.round(val * 100) / 100}</td>`;
        });
        html += "</tr>";
    });
    html += "</table>";
    return html;
}

function _renderChangePoints(records) {
    // records: array of {time, field, value, tags}
    const cpRecords = records.filter(r => r.field === "severity" && r.value > 0);
    if (!cpRecords.length) return '<div style="color:var(--text-muted);font-size:0.85rem">No change points detected in selected period.</div>';

    let html = '<div style="display:flex;flex-direction:column;gap:6px">';
    cpRecords.slice(0, 20).forEach(r => {
        const tag = r.tags.alias || "";
        const time = r.time ? new Date(r.time).toLocaleString() : "";
        const severity = Math.round(r.value * 100) / 100;
        const meanBefore = r.tags.mean_before != null ? Math.round(r.tags.mean_before * 100) / 100 : null;
        const meanAfter = r.tags.mean_after != null ? Math.round(r.tags.mean_after * 100) / 100 : null;
        html += `<div style="display:flex;align-items:center;gap:10px;padding:6px 10px;border-left:3px solid #2980b9;background:rgba(41,128,185,0.05);border-radius:0 4px 4px 0;font-size:0.82rem">
            <span style="color:#2980b9;font-size:1rem">&#128200;</span>
            <span style="font-weight:600">${escHtml(tag)}</span>
            ${meanBefore != null && meanAfter != null ? `<span style="color:var(--text-secondary)">${meanBefore} &rarr; ${meanAfter}</span>` : ""}
            <span style="color:var(--text-muted)">Severity: ${severity}</span>
            <span style="margin-left:auto;color:var(--text-muted)">${escHtml(time)}</span>
        </div>`;
    });
    html += "</div>";
    return html;
}

function _renderPatterns(motifs, discords) {
    if (!motifs.length && !discords.length) {
        return '<div style="color:var(--text-muted);font-size:0.85rem">No pattern data available for selected period.</div>';
    }

    let html = "";

    if (motifs.length) {
        html += '<div style="margin-bottom:14px"><div style="font-size:0.8rem;font-weight:600;color:var(--text-secondary);margin-bottom:6px">MOTIFS (Recurring Patterns)</div>';
        html += '<table style="width:100%;border-collapse:collapse;font-size:0.82rem">';
        html += '<tr style="border-bottom:1px solid var(--border)"><th style="text-align:left;padding:4px 8px">Tag</th><th style="text-align:center;padding:4px 8px">Similarity</th><th style="text-align:center;padding:4px 8px">Rank</th><th style="text-align:right;padding:4px 8px">Time</th></tr>';
        motifs.slice(0, 10).forEach(r => {
            const tag = r.tags.alias || "";
            const sim = r.field === "similarity" ? `${Math.round(r.value * 100)}%` : "—";
            const rank = r.tags.motif_rank || "—";
            const time = r.time ? new Date(r.time).toLocaleString() : "";
            html += `<tr style="border-bottom:1px solid var(--border)">
                <td style="padding:4px 8px;font-weight:600">${escHtml(tag)}</td>
                <td style="padding:4px 8px;text-align:center;color:#27ae60;font-weight:600">${escHtml(String(sim))}</td>
                <td style="padding:4px 8px;text-align:center">${escHtml(String(rank))}</td>
                <td style="padding:4px 8px;text-align:right;color:var(--text-muted)">${escHtml(time)}</td>
            </tr>`;
        });
        html += "</table></div>";
    }

    if (discords.length) {
        html += '<div><div style="font-size:0.8rem;font-weight:600;color:var(--text-secondary);margin-bottom:6px">DISCORDS (Unusual Patterns)</div>';
        html += '<table style="width:100%;border-collapse:collapse;font-size:0.82rem">';
        html += '<tr style="border-bottom:1px solid var(--border)"><th style="text-align:left;padding:4px 8px">Tag</th><th style="text-align:center;padding:4px 8px">Unusualness</th><th style="text-align:center;padding:4px 8px">Rank</th><th style="text-align:right;padding:4px 8px">Time</th></tr>';
        discords.slice(0, 10).forEach(r => {
            const tag = r.tags.alias || "";
            const score = r.field === "discord_score" ? Math.round(r.value * 1000) / 1000 : "—";
            const rank = r.tags.discord_rank || "—";
            const time = r.time ? new Date(r.time).toLocaleString() : "";
            html += `<tr style="border-bottom:1px solid var(--border)">
                <td style="padding:4px 8px;font-weight:600">${escHtml(tag)}</td>
                <td style="padding:4px 8px;text-align:center;color:#c8102e;font-weight:600">${escHtml(String(score))}</td>
                <td style="padding:4px 8px;text-align:center">${escHtml(String(rank))}</td>
                <td style="padding:4px 8px;text-align:right;color:var(--text-muted)">${escHtml(time)}</td>
            </tr>`;
        });
        html += "</table></div>";
    }

    return html;
}

async function loadMLConfig() {
    try {
        const data = await apiFetch("/api/ml/config");
        const cfg = data.mlConfig || {};

        const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
        const setChk = (id, val) => { const el = document.getElementById(id); if (el) el.checked = !!val; };

        set("mlCfgCycleInterval", cfg.cycleIntervalMinutes ?? 5);
        set("mlCfgForecastHours", cfg.forecastHours ?? 2);
        set("mlCfgMinPoints", cfg.minPoints ?? 100);

        const anomaly = cfg.anomaly || {};
        setChk("mlCfgAnomalyEnabled", anomaly.enabled !== false);
        set("mlCfgAnomalyContamination", anomaly.contamination ?? 0.05);
        set("mlCfgAnomalyMinAgreement", anomaly.minAgreement ?? 2);

        const shap = cfg.explainability || {};
        setChk("mlCfgShapEnabled", shap.enabled !== false);
        set("mlCfgShapTop", shap.topContributors ?? 5);

        const corr = cfg.correlation || {};
        setChk("mlCfgCorrEnabled", corr.enabled !== false);
        set("mlCfgCorrBaseline", corr.baselineHours ?? 6);
        set("mlCfgCorrRecent", corr.recentMinutes ?? 30);
        set("mlCfgCorrBreak", corr.breakThreshold ?? 0.4);

        const cp = cfg.changepoint || {};
        setChk("mlCfgCpEnabled", cp.enabled !== false);
        set("mlCfgCpMinSeg", cp.minSegmentSize ?? 60);
        set("mlCfgCpPenalty", cp.penalty ?? 10);

        const pat = cfg.pattern || {};
        setChk("mlCfgPatEnabled", pat.enabled !== false);
        set("mlCfgPatWindow", pat.windowSize ?? 60);
        set("mlCfgPatTopK", pat.topK ?? 3);
    } catch (_) {
        // Config load failure is non-critical; form retains defaults
    }
}

async function saveMLConfig() {
    const get = id => { const el = document.getElementById(id); return el ? el.value : null; };
    const getChk = id => { const el = document.getElementById(id); return el ? el.checked : true; };
    const getNum = (id, fallback) => { const v = parseFloat(get(id)); return isNaN(v) ? fallback : v; };
    const getInt = (id, fallback) => { const v = parseInt(get(id), 10); return isNaN(v) ? fallback : v; };

    const payload = {
        mlConfig: {
            cycleIntervalMinutes: getInt("mlCfgCycleInterval", 5),
            forecastHours: getInt("mlCfgForecastHours", 2),
            minPoints: getInt("mlCfgMinPoints", 100),
            anomaly: {
                enabled: getChk("mlCfgAnomalyEnabled"),
                contamination: getNum("mlCfgAnomalyContamination", 0.05),
                minAgreement: getInt("mlCfgAnomalyMinAgreement", 2),
            },
            explainability: {
                enabled: getChk("mlCfgShapEnabled"),
                topContributors: getInt("mlCfgShapTop", 5),
            },
            correlation: {
                enabled: getChk("mlCfgCorrEnabled"),
                baselineHours: getInt("mlCfgCorrBaseline", 6),
                recentMinutes: getInt("mlCfgCorrRecent", 30),
                breakThreshold: getNum("mlCfgCorrBreak", 0.4),
            },
            changepoint: {
                enabled: getChk("mlCfgCpEnabled"),
                minSegmentSize: getInt("mlCfgCpMinSeg", 60),
                penalty: getNum("mlCfgCpPenalty", 10),
            },
            pattern: {
                enabled: getChk("mlCfgPatEnabled"),
                windowSize: getInt("mlCfgPatWindow", 60),
                topK: getInt("mlCfgPatTopK", 3),
            },
        }
    };

    try {
        await apiFetch("/api/ml/config", { method: "PUT", body: JSON.stringify(payload) });
        showToast("ML configuration saved successfully.", "success");
    } catch (e) {
        showToast("Failed to save ML config: " + e.message, "error");
    }
}

async function triggerMLRun() {
    try {
        await apiFetch("/api/ml/run-now", { method: "POST" });
        showToast("ML cycle triggered — predictor will run within 30 seconds.", "success");
    } catch (e) {
        showToast("Failed to trigger ML run: " + e.message, "error");
    }
}

// =============================================
// OPC-UA Tag Discovery
// =============================================

async function testConnection(evt) {
    const deviceName = document.getElementById("deviceName")?.value.trim();
    if (!deviceName) { toast("Enter a device name and save first", "error"); return; }

    const btn = evt ? evt.target : document.querySelector("[onclick*='testConnection']");
    const origText = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "Testing..."; btn.style.minWidth = btn.offsetWidth + "px"; }

    // Show inline result next to button
    let resultEl = document.getElementById("connTestResult");
    if (!resultEl) {
        resultEl = document.createElement("span");
        resultEl.id = "connTestResult";
        resultEl.style.cssText = "font-size:0.8rem;margin-left:8px;font-weight:600";
        if (btn) btn.parentNode.appendChild(resultEl);
    }
    resultEl.textContent = "";

    try {
        const data = await api(`/api/devices/${encodeURIComponent(deviceName)}/test-connection`, "POST");
        if (data.connected) {
            const tagsInfo = data.tagsTotal > 0 ? ` (${data.tagsOk}/${data.tagsTotal} tags OK)` : "";
            resultEl.innerHTML = `<span style="color:var(--success)">Online${escHtml(tagsInfo)}</span>`;
            toast(data.message || "Device is online", "success");
        } else {
            resultEl.innerHTML = `<span style="color:var(--danger)">${escHtml(data.status || "offline")}</span>`;
            toast(data.message || "Device is not reachable", "error");
        }
    } catch (e) {
        resultEl.innerHTML = `<span style="color:var(--danger)">Failed: ${escHtml(e.message)}</span>`;
        toast("Connection test failed: " + e.message, "error");
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = origText; }
    }
}

async function discoverTags(evt) {
    const deviceName = document.getElementById("deviceName")?.value.trim();
    if (!deviceName) { toast("Save the device first, then discover tags", "error"); return; }

    const btn = evt ? evt.target : null;
    const origText = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "Discovering..."; }

    try {
        const data = await api(`/api/devices/${encodeURIComponent(deviceName)}/discover`, "POST");
        const tags = data.tags || [];

        if (tags.length === 0) {
            toast("No tags discovered. Device may be offline or have no readable variables.", "error");
            return;
        }

        showDiscoveryModal(tags, deviceName, data.source || "opcua");
    } catch (e) {
        toast("Discovery failed: " + e.message, "error");
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = origText; }
    }
}

function showDiscoveryModal(tags, deviceName, source) {
    // Collect already-configured tag addresses to mark duplicates
    const existingAddresses = new Set();
    const existingAliases = new Set();
    document.querySelectorAll("#tagEditor .tag-row").forEach(row => {
        const addr = row.querySelector(".tag-address-input")?.value.trim();
        const alias = row.querySelector(".tag-alias-input")?.value.trim();
        if (addr) existingAddresses.add(addr);
        if (alias) existingAliases.add(alias);
    });

    // Remove any previous discovery modal
    const prev = document.getElementById("discoveryModal");
    if (prev) prev.remove();

    const isEip = (source === "pylogix");

    // Collect unique program values for filter dropdown
    const programs = [];
    if (isEip) {
        const seen = new Set();
        tags.forEach(t => {
            const p = t.program || "";
            if (!seen.has(p)) { seen.add(p); programs.push(p); }
        });
    }

    // Source indicator HTML
    const sourceLabel = isEip
        ? `<span style="font-size:0.78rem;padding:3px 8px;border-radius:12px;background:#e8f5e9;color:#2e7d32;font-weight:600">Source: pylogix (EtherNet/IP)</span>`
        : `<span style="font-size:0.78rem;padding:3px 8px;border-radius:12px;background:#e3f2fd;color:#1565c0;font-weight:600">Source: OPC-UA Browse</span>`;

    // PLC Health button (EtherNet/IP only)
    const healthBtnHtml = isEip
        ? `<button class="btn btn-outline btn-sm" onclick="loadPLCTagHealth(${JSON.stringify(deviceName)})" id="discHealthBtn">PLC Identity</button>`
        : "";

    // Program filter dropdown (EtherNet/IP only, when multiple programs)
    let programFilterHtml = "";
    if (isEip && programs.length > 1) {
        const opts = programs.map(p => `<option value="${escHtml(p)}">${escHtml(p || "Controller")}</option>`).join("");
        programFilterHtml = `
            <select id="discProgramFilter" onchange="filterDiscoveryByProgram(this.value)"
                    style="padding:6px 10px;border:1px solid var(--border);border-radius:4px;font-size:13px">
                <option value="">All Programs</option>
                ${opts}
            </select>`;
    }

    const overlay = document.createElement("div");
    overlay.id = "discoveryModal";
    overlay.className = "modal-overlay";
    overlay.style.cssText = "display:flex;z-index:10000";

    const rows = tags.map((t, i) => {
        const alreadyConfigured = existingAddresses.has(t.address);
        const rowClass = alreadyConfigured ? "style=\"background:var(--bg);opacity:0.55\"" : "";

        // Type cell — add UDT and array badges for pylogix results
        let typeBadge = `<span class="status-badge" style="font-size:0.7rem">${escHtml(t.dataType || "Unknown")}</span>`;
        if (t.isUDT) typeBadge += ` <span style="font-size:0.65rem;padding:1px 5px;border-radius:8px;background:#fff3e0;color:#e65100;font-weight:600;vertical-align:middle">UDT</span>`;
        if (t.isArray) typeBadge += ` <span style="font-size:0.65rem;padding:1px 5px;border-radius:8px;background:#f3e5f5;color:#6a1b9a;font-weight:600;vertical-align:middle">[]</span>`;

        const programAttr = isEip ? ` data-program="${escHtml(t.program || "")}"` : "";
        return `
        <tr ${rowClass}${programAttr}>
            <td style="text-align:center">
                <input type="checkbox" class="disc-check" data-idx="${i}" ${!alreadyConfigured ? "checked" : ""} ${alreadyConfigured ? "disabled title=\"Already configured\"" : ""}>
            </td>
            <td>
                <input type="text" class="disc-alias" data-idx="${i}" value="${escHtml(t.alias)}"
                       style="padding:3px 6px;border:1px solid var(--border);border-radius:4px;font-size:12px;width:100%">
            </td>
            <td><code style="font-size:11px">${escHtml(t.address)}</code></td>
            <td>${typeBadge}</td>
            <td style="font-size:11px;color:var(--text-secondary)">${escHtml(t.browsePath || "")}</td>
            ${alreadyConfigured ? "<td><span class=\"status-badge badge-ok\" style=\"font-size:0.68rem\">configured</span></td>" : "<td></td>"}
        </tr>`;
    }).join("");

    overlay.innerHTML = `
    <div class="modal" style="max-width:860px;width:95vw;max-height:90vh;display:flex;flex-direction:column">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;flex-wrap:wrap;gap:6px">
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
                <h3 style="margin:0">Discovered Tags — <span style="color:var(--primary)">${escHtml(deviceName)}</span></h3>
                ${sourceLabel}
            </div>
            <div style="display:flex;align-items:center;gap:8px">
                ${healthBtnHtml}
                <span style="font-size:0.82rem;color:var(--text-secondary)">Found <strong>${tags.length}</strong> tag${tags.length !== 1 ? "s" : ""}</span>
            </div>
        </div>

        <div id="discHealthResult" style="display:none;margin-bottom:10px;padding:8px 12px;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:0.82rem"></div>

        <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
            <input type="text" id="discFilter" placeholder="Filter by name or path..."
                   oninput="filterDiscoveryRows(this.value)"
                   style="flex:1;min-width:160px;padding:6px 10px;border:1px solid var(--border);border-radius:4px;font-size:13px">
            ${programFilterHtml}
            <button class="btn btn-outline btn-sm" onclick="selectAllDisc(true)">Select All</button>
            <button class="btn btn-outline btn-sm" onclick="selectAllDisc(false)">Deselect All</button>
        </div>

        <div style="overflow-y:auto;flex:1;border:1px solid var(--border);border-radius:4px">
            <table class="table" style="margin:0;font-size:13px">
                <thead style="position:sticky;top:0;background:var(--surface)">
                    <tr>
                        <th style="width:36px"></th>
                        <th>Alias</th>
                        <th>Address</th>
                        <th>Type</th>
                        <th>Browse Path</th>
                        <th></th>
                    </tr>
                </thead>
                <tbody id="discTableBody">${rows}</tbody>
            </table>
        </div>

        <div class="modal-actions" style="margin-top:14px">
            <span id="discSelCount" style="font-size:0.82rem;color:var(--text-secondary);margin-right:auto"></span>
            <button class="btn btn-outline" onclick="document.getElementById('discoveryModal').remove()">Cancel</button>
            <button class="btn btn-primary" onclick="importDiscoveredTags(${JSON.stringify(tags).replace(/"/g, '&quot;')})">Import Selected</button>
        </div>
    </div>`;

    document.body.appendChild(overlay);
    _updateDiscSelCount();

    // Update count whenever a checkbox changes
    overlay.addEventListener("change", e => {
        if (e.target.classList.contains("disc-check")) _updateDiscSelCount();
    });
}

function _updateDiscSelCount() {
    const checked = document.querySelectorAll("#discTableBody .disc-check:checked").length;
    const el = document.getElementById("discSelCount");
    if (el) el.textContent = `${checked} selected`;
}

function filterDiscoveryRows(query) {
    const q = query.toLowerCase();
    const programFilter = document.getElementById("discProgramFilter")?.value || "";
    document.querySelectorAll("#discTableBody tr").forEach(row => {
        const text = row.textContent.toLowerCase();
        const prog = row.dataset.program !== undefined ? row.dataset.program : null;
        const textMatch = !q || text.includes(q);
        const progMatch = !programFilter || prog === null || prog === programFilter;
        row.style.display = (textMatch && progMatch) ? "" : "none";
    });
}

function filterDiscoveryByProgram(program) {
    const q = document.getElementById("discFilter")?.value || "";
    document.querySelectorAll("#discTableBody tr").forEach(row => {
        const text = row.textContent.toLowerCase();
        const prog = row.dataset.program !== undefined ? row.dataset.program : null;
        const textMatch = !q || text.includes(q.toLowerCase());
        const progMatch = !program || prog === null || prog === program;
        row.style.display = (textMatch && progMatch) ? "" : "none";
    });
    _updateDiscSelCount();
}

function selectAllDisc(checked) {
    document.querySelectorAll("#discTableBody .disc-check:not([disabled])").forEach(cb => {
        // Only toggle visible rows
        if (cb.closest("tr").style.display !== "none") cb.checked = checked;
    });
    _updateDiscSelCount();
}

// =============================================
// PLC Identity (discovery modal health button)
// =============================================

async function loadPLCTagHealth(deviceName) {
    const resultEl = document.getElementById("discHealthResult");
    const btn = document.getElementById("discHealthBtn");
    if (!resultEl) return;

    resultEl.style.display = "block";
    resultEl.innerHTML = `<span style="color:var(--text-secondary)">Reading PLC identity...</span>`;
    if (btn) { btn.disabled = true; }

    // Extract IP from the connection string shown in the device form
    const connStr = document.getElementById("connectionString")?.value.trim() || "";
    const ip = connStr.replace("eip://", "").split(":")[0];
    if (!ip) {
        resultEl.innerHTML = `<span style="color:var(--danger)">Cannot determine PLC IP address</span>`;
        if (btn) { btn.disabled = false; }
        return;
    }

    try {
        const data = await api(`/api/plctag/diagnostics/identity?ip=${encodeURIComponent(ip)}`, "POST");
        const identity = data.identity || data;
        let html = `<strong>PLC Identity</strong> &mdash; `;
        if (identity.vendor)   html += `Vendor: <strong>${escHtml(identity.vendor)}</strong> &nbsp;`;
        if (identity.product)  html += `Model: <strong>${escHtml(identity.product)}</strong> &nbsp;`;
        if (identity.revision) html += `Firmware: <strong>${escHtml(identity.revision)}</strong> &nbsp;`;
        if (identity.serial)   html += `S/N: <code>${escHtml(identity.serial)}</code>`;
        if (!identity.vendor && !identity.product) html += JSON.stringify(identity);
        resultEl.innerHTML = html;
    } catch (e) {
        resultEl.innerHTML = `<span style="color:var(--danger)">Identity failed: ${escHtml(e.message)}</span>`;
    } finally {
        if (btn) { btn.disabled = false; }
    }
}

// =============================================
// PLC Connection Safety Dashboard
// =============================================

async function loadPLCTagStats() {
    const el = document.getElementById("plctagStats");
    if (!el) return;
    el.innerHTML = `<p class="text-muted">Loading...</p>`;

    try {
        const data = await api("/api/plctag/stats", "GET");

        // Service status
        const statusColor = data.service_status === "online" ? "var(--success, #2e7d32)" : "var(--danger)";
        const lockColor = data.lock_available === false ? "var(--warning, #e65100)" : "var(--success, #2e7d32)";
        let html = `
        <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:14px">
            <div>
                <span style="font-size:0.78rem;font-weight:600;color:var(--text-secondary);display:block">Service Status</span>
                <span style="font-weight:700;color:${statusColor}">${escHtml(data.service_status || "unknown")}</span>
            </div>
            <div>
                <span style="font-size:0.78rem;font-weight:600;color:var(--text-secondary);display:block">Lock</span>
                <span style="font-weight:700;color:${lockColor}">${data.lock_available === false ? "In Use" : "Available"}</span>
            </div>
            ${data.active_ip ? `<div><span style="font-size:0.78rem;font-weight:600;color:var(--text-secondary);display:block">Active PLC</span><code>${escHtml(data.active_ip)}</code></div>` : ""}
        </div>`;

        // Rate limits per PLC
        if (data.rate_limits && Object.keys(data.rate_limits).length > 0) {
            html += `<h4 style="font-size:0.82rem;font-weight:600;margin-bottom:6px;color:var(--text-secondary)">Rate Limits by PLC</h4>
            <table class="table" style="font-size:12px;margin-bottom:14px">
                <thead><tr><th>PLC IP</th><th>Requests</th><th>Limit</th><th>Status</th></tr></thead>
                <tbody>`;
            for (const [ip, rl] of Object.entries(data.rate_limits)) {
                const pct = rl.limit > 0 ? Math.round((rl.requests / rl.limit) * 100) : 0;
                const rlColor = pct >= 80 ? "var(--danger)" : pct >= 50 ? "var(--warning, #e65100)" : "var(--success, #2e7d32)";
                html += `<tr>
                    <td><code>${escHtml(ip)}</code></td>
                    <td>${escHtml(String(rl.requests || 0))}</td>
                    <td>${escHtml(String(rl.limit || "—"))}</td>
                    <td><span style="color:${rlColor};font-weight:600">${pct}%</span></td>
                </tr>`;
            }
            html += `</tbody></table>`;
        }

        // Recent connections table
        const conns = data.recent_connections || [];
        if (conns.length > 0) {
            html += `<h4 style="font-size:0.82rem;font-weight:600;margin-bottom:6px;color:var(--text-secondary)">Recent Connections</h4>
            <div style="overflow-x:auto">
            <table class="table" style="font-size:12px">
                <thead><tr><th>Time</th><th>PLC IP</th><th>Operation</th><th>Duration</th><th>Status</th></tr></thead>
                <tbody>`;
            conns.forEach(c => {
                const ts = c.time ? new Date(c.time).toLocaleTimeString() : "—";
                const dur = c.duration_ms != null ? `${escHtml(String(c.duration_ms))} ms` : "—";
                const stColor = c.status === "ok" ? "var(--success, #2e7d32)" : "var(--danger)";
                html += `<tr>
                    <td style="white-space:nowrap">${escHtml(ts)}</td>
                    <td><code>${escHtml(c.ip || "—")}</code></td>
                    <td>${escHtml(c.operation || "—")}</td>
                    <td>${dur}</td>
                    <td><span style="color:${stColor};font-weight:600">${escHtml(c.status || "—")}</span></td>
                </tr>`;
            });
            html += `</tbody></table></div>`;
        } else {
            html += `<p class="text-muted" style="font-size:0.82rem">No recent connections recorded.</p>`;
        }

        el.innerHTML = html;
    } catch (e) {
        el.innerHTML = `<p style="color:var(--danger);font-size:0.88rem">Failed to load stats: ${escHtml(e.message)}</p>`;
    }
}

function importDiscoveredTags(tags) {
    const checkboxes = document.querySelectorAll("#discTableBody .disc-check:checked");
    if (checkboxes.length === 0) {
        toast("No tags selected", "error");
        return;
    }

    // Collect existing addresses to avoid strict duplicates
    const existingAddresses = new Set();
    document.querySelectorAll("#tagEditor .tag-row").forEach(row => {
        const addr = row.querySelector(".tag-address-input")?.value.trim();
        if (addr) existingAddresses.add(addr);
    });

    let added = 0;
    let skipped = 0;
    checkboxes.forEach(cb => {
        const idx = parseInt(cb.dataset.idx, 10);
        const tag = tags[idx];
        if (!tag) return;

        if (existingAddresses.has(tag.address)) {
            skipped++;
            return;
        }

        // Use the (possibly edited) alias from the input
        const aliasInput = document.querySelector(`.disc-alias[data-idx="${idx}"]`);
        const alias = aliasInput ? aliasInput.value.trim() : tag.alias;

        addTagRow(alias || tag.alias, tag.address);
        existingAddresses.add(tag.address);
        added++;
    });

    document.getElementById("discoveryModal")?.remove();

    if (added > 0 && skipped === 0) {
        toast(`${added} tag${added !== 1 ? "s" : ""} imported`);
    } else if (added > 0) {
        toast(`${added} imported, ${skipped} skipped (already configured)`);
    } else {
        toast("All selected tags are already configured", "warn");
    }
}

// =============================================
// Maintenance Tab
// =============================================

async function loadMaintenance() {
    const isAdmin = (window._userRole === "admin");
    const isOperator = (window._userRole === "admin" || window._userRole === "operator");
    const catalogSection = document.getElementById("maintenanceCatalogSection");
    const trainSection = document.getElementById("maintenanceTrainSection");
    const reportBtn = document.getElementById("reportFailureBtn");
    if (catalogSection) catalogSection.style.display = isAdmin ? "" : "none";
    if (trainSection) trainSection.style.display = isAdmin ? "" : "none";
    if (reportBtn) reportBtn.style.display = isOperator ? "" : "none";
    await Promise.all([loadPredictions(), loadFailureLog(), loadFailureCatalog(), loadFailureModels()]);
}

async function loadPredictions() {
    try {
        const resp = await apiFetch("/api/failures/predictions");
        const data = await resp.json();
        const container = document.getElementById("maintenancePredictions");
        if (!data.predictions || data.predictions.length === 0) {
            container.innerHTML = '<p style="color:var(--text-muted)">No predictions available. Train a model first.</p>';
            return;
        }
        container.innerHTML = data.predictions.map(p => {
            const pct = (p.probability * 100).toFixed(1);
            const color = p.alert ? "#c8102e" : pct > 40 ? "#e8a317" : "#28a745";
            const bg = p.alert ? "rgba(200,16,46,0.08)" : "transparent";
            return `<div style="border:1px solid var(--border);border-radius:8px;padding:16px;background:${bg};border-left:4px solid ${color}">
                <div style="display:flex;justify-content:space-between;align-items:center">
                    <strong>${p.failure_type.replace(/_/g, " ")}</strong>
                    <span style="font-size:1.4rem;font-weight:700;color:${color}">${pct}%</span>
                </div>
                <div style="font-size:0.85rem;color:var(--text-muted);margin-top:4px">${p.device}${p.alert ? ' \u2014 <strong style="color:#c8102e">ALERT</strong>' : ""}</div>
                <div style="font-size:0.75rem;color:var(--text-muted);margin-top:4px">${new Date(p.timestamp).toLocaleString()}</div>
            </div>`;
        }).join("");
    } catch (e) { console.error("Failed to load predictions:", e); }
}

async function loadFailureLog() {
    try {
        const device = document.getElementById("failureLogDeviceFilter")?.value || "";
        const ftype = document.getElementById("failureLogTypeFilter")?.value || "";
        let url = "/api/failures?lines=100";
        if (device) url += "&device=" + encodeURIComponent(device);
        if (ftype) url += "&failure_type=" + encodeURIComponent(ftype);
        const resp = await apiFetch(url);
        const data = await resp.json();
        const tbody = document.querySelector("#failureLogTable tbody");
        if (!data.entries || data.entries.length === 0) {
            tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-muted)">No failures reported yet</td></tr>';
            return;
        }
        tbody.innerHTML = data.entries.map(e => {
            const sevColor = e.severity === "critical" ? "#c8102e" : e.severity === "major" ? "#e8a317" : "#6c757d";
            const isOp = (window._userRole === "admin" || window._userRole === "operator");
            const actions = isOp && !e.resolved_at ? `<button class="btn btn-sm" onclick="resolveFailure(${e.id})" style="font-size:0.7rem">Resolve</button>` : "";
            return `<tr>
                <td style="white-space:nowrap">${new Date(e.occurred_at).toLocaleString()}</td>
                <td>${e.device}</td>
                <td>${e.failure_type.replace(/_/g, " ")}</td>
                <td><span style="color:${sevColor};font-weight:600">${e.severity}</span></td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${e.description || "-"}</td>
                <td>${e.resolved_at ? new Date(e.resolved_at).toLocaleString() : '<span style="color:#c8102e">Open</span>'}</td>
                <td>${e.reported_by || "-"}</td>
                <td>${actions}</td>
            </tr>`;
        }).join("");
    } catch (e) { console.error("Failed to load failure log:", e); }
}

async function loadFailureCatalog() {
    try {
        const resp = await apiFetch("/api/failures/catalog");
        const data = await resp.json();
        const tbody = document.querySelector("#catalogTable tbody");
        const trainSelect = document.getElementById("trainFailureType");
        const rfSelect = document.getElementById("rfFailureType");
        const typeFilter = document.getElementById("failureLogTypeFilter");
        const currentFilter = typeFilter ? typeFilter.value : "";
        if (typeFilter) {
            typeFilter.innerHTML = '<option value="">All Types</option>';
            (data.catalog || []).forEach(c => { typeFilter.innerHTML += `<option value="${c.name}" ${c.name === currentFilter ? "selected" : ""}>${c.display_name}</option>`; });
        }
        if (trainSelect) {
            trainSelect.innerHTML = '<option value="">Select failure type...</option>';
            (data.catalog || []).forEach(c => { trainSelect.innerHTML += `<option value="${c.name}">${c.display_name}</option>`; });
        }
        if (rfSelect) {
            rfSelect.innerHTML = '<option value="">Select...</option>';
            (data.catalog || []).forEach(c => { rfSelect.innerHTML += `<option value="${c.name}">${c.display_name}</option>`; });
        }
        if (!data.catalog || data.catalog.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-muted)">No failure types defined</td></tr>';
            return;
        }
        tbody.innerHTML = data.catalog.map(c => `<tr>
            <td><code>${c.name}</code></td><td>${c.display_name}</td><td>${c.lookback_hours}</td>
            <td>${(c.related_tags || []).join(", ") || "-"}</td>
            <td><button class="btn btn-sm" onclick="deleteCatalogEntry(${c.id},'${c.name}')" style="font-size:0.7rem;color:#c8102e">Delete</button></td>
        </tr>`).join("");
    } catch (e) { console.error("Failed to load catalog:", e); }
}

async function loadFailureModels() {
    try {
        const resp = await apiFetch("/api/failures/models");
        const data = await resp.json();
        const tbody = document.querySelector("#modelsTable tbody");
        if (!data.models || data.models.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted)">No trained models</td></tr>';
            return;
        }
        tbody.innerHTML = data.models.map(m => {
            const statusColor = m.status === "active" ? "#28a745" : "#6c757d";
            return `<tr>
                <td>${m.failure_type.replace(/_/g, " ")}</td><td>${m.device}</td>
                <td>${new Date(m.trained_at).toLocaleString()}</td><td>${m.sample_count}</td>
                <td>${m.accuracy !== null ? (m.accuracy * 100).toFixed(1) + "%" : "-"}</td>
                <td><span style="color:${statusColor};font-weight:600">${m.status}</span></td>
            </tr>`;
        }).join("");
    } catch (e) { console.error("Failed to load models:", e); }
}

function showReportFailureModal() {
    document.getElementById("reportFailureModal").style.display = "flex";
    const now = new Date(); now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
    document.getElementById("rfOccurredAt").value = now.toISOString().slice(0, 16);
}
function closeReportFailureModal() { document.getElementById("reportFailureModal").style.display = "none"; }

async function submitFailureReport() {
    const occurredAt = document.getElementById("rfOccurredAt").value;
    const device = document.getElementById("rfDevice").value.trim();
    const failureType = document.getElementById("rfFailureType").value;
    const severity = document.getElementById("rfSeverity").value;
    const equipment = document.getElementById("rfEquipment").value.trim();
    const description = document.getElementById("rfDescription").value.trim();
    if (!occurredAt || !device || !failureType) { alert("Occurred At, Device, and Failure Type are required."); return; }
    try {
        const resp = await apiFetch("/api/failures", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ occurred_at: new Date(occurredAt).toISOString(), device, failure_type: failureType, severity, equipment, description })
        });
        if (!resp.ok) { const err = await resp.json(); alert(err.detail || "Failed"); return; }
        closeReportFailureModal(); loadFailureLog();
    } catch (e) { alert("Error: " + e.message); }
}

async function resolveFailure(id) {
    if (!confirm("Mark this failure as resolved now?")) return;
    try {
        await apiFetch(`/api/failures/${id}`, { method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({resolved_at: new Date().toISOString()}) });
        loadFailureLog();
    } catch (e) { alert("Error: " + e.message); }
}

function showAddCatalogModal() { document.getElementById("addCatalogModal").style.display = "flex"; }
function closeAddCatalogModal() { document.getElementById("addCatalogModal").style.display = "none"; }

async function submitCatalogEntry() {
    const name = document.getElementById("acName").value.trim();
    const displayName = document.getElementById("acDisplayName").value.trim();
    const description = document.getElementById("acDescription").value.trim();
    const lookback = parseInt(document.getElementById("acLookback").value) || 72;
    const tagsStr = document.getElementById("acRelatedTags").value.trim();
    const relatedTags = tagsStr ? tagsStr.split(",").map(t => t.trim()).filter(Boolean) : [];
    if (!name || !displayName) { alert("Name and Display Name are required."); return; }
    try {
        const resp = await apiFetch("/api/failures/catalog", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name, display_name: displayName, description, lookback_hours: lookback, related_tags: relatedTags})
        });
        if (!resp.ok) { const err = await resp.json(); alert(err.detail || "Failed"); return; }
        closeAddCatalogModal(); loadFailureCatalog();
    } catch (e) { alert("Error: " + e.message); }
}

async function deleteCatalogEntry(id, name) {
    if (!confirm(`Delete failure type "${name}"?`)) return;
    try { await apiFetch(`/api/failures/catalog/${id}`, {method: "DELETE"}); loadFailureCatalog(); }
    catch (e) { alert("Error: " + e.message); }
}

async function triggerTraining() {
    const failureType = document.getElementById("trainFailureType").value;
    const device = document.getElementById("trainDevice").value.trim();
    const statusEl = document.getElementById("trainStatus");
    const btn = document.getElementById("trainBtn");
    if (!failureType || !device) { alert("Select a failure type and enter a device name."); return; }
    btn.disabled = true;
    statusEl.textContent = "Training in progress... This may take a few minutes.";
    statusEl.style.color = "var(--text-muted)";
    try {
        const resp = await apiFetch("/api/failures/train", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({failure_type: failureType, device})
        });
        const data = await resp.json();
        if (!resp.ok) { statusEl.textContent = "Error: " + (data.detail || "Training failed"); statusEl.style.color = "#c8102e"; }
        else { statusEl.textContent = `Done! Accuracy: ${(data.accuracy * 100).toFixed(1)}%, Samples: ${data.samples}`; statusEl.style.color = "#28a745"; loadFailureModels(); }
    } catch (e) { statusEl.textContent = "Error: " + e.message; statusEl.style.color = "#c8102e"; }
    finally { btn.disabled = false; }
}

// =============================================
// Chat Widget — AI Assistant
// =============================================

const ChatWidget = (() => {
    let _enabled = false;
    let _convId = null;
    let _conversations = [];
    let _sending = false;
    let _chartInstances = [];
    let _fab, _panel, _messages, _input, _sendBtn, _convSelect;

    function inject() {
        const fab = document.createElement('button');
        fab.className = 'chat-fab hidden';
        fab.innerHTML = '&#x1F4AC;';
        fab.title = 'AI Assistant';
        fab.onclick = toggle;
        document.body.appendChild(fab);
        _fab = fab;

        const panel = document.createElement('div');
        panel.className = 'chat-panel';
        panel.innerHTML = `
            <div class="chat-header">
                <div class="chat-header-title"><span>AI Assistant</span></div>
                <div class="chat-header-actions">
                    <button onclick="ChatWidget.newChat()" title="New conversation">+</button>
                    <button onclick="ChatWidget.toggle()" title="Close">&times;</button>
                </div>
            </div>
            <div class="chat-conv-bar">
                <select id="chat-conv-select" onchange="ChatWidget.switchConversation(this.value)">
                    <option value="">New conversation</option>
                </select>
            </div>
            <div class="chat-messages" id="chat-messages"></div>
            <div class="chat-input-area">
                <input type="text" id="chat-input" placeholder="Ask about your plant data..." maxlength="2000" onkeydown="if(event.key==='Enter')ChatWidget.send()">
                <button id="chat-send-btn" onclick="ChatWidget.send()" title="Send">&#x27A4;</button>
            </div>
        `;
        document.body.appendChild(panel);
        _panel = panel;
        _messages = document.getElementById('chat-messages');
        _input = document.getElementById('chat-input');
        _sendBtn = document.getElementById('chat-send-btn');
        _convSelect = document.getElementById('chat-conv-select');
    }

    async function checkStatus() {
        try {
            const resp = await fetch('/api/chat/status');
            const data = await resp.json();
            _enabled = data.enabled;
        } catch (e) {}
        // Always show the chat button — if not configured, show setup message on open
        if (_fab) _fab.classList.remove('hidden');
    }

    function toggle() {
        if (!_panel) return;
        _panel.classList.toggle('open');
        if (_panel.classList.contains('open')) { _input.focus(); loadConversations(); }
    }

    const _SUGGESTIONS = [
        "What alarms are currently active?",
        "Show me the OEE for Demo-Simulated last 24h",
        "What is the current temperature?",
        "Are there any anomalies detected?",
        "Show failure history for Demo-Simulated",
        "What ML insights do we have?",
        "List all devices and their status",
        "Compare RandomFloat and StateFloat trends",
    ];

    function newChat() {
        _convId = null;
        _messages.innerHTML = '';
        _convSelect.value = '';
        _destroyCharts();
        if (_enabled) {
            _addSystemMessage('Hello! Ask me anything about your plant data, alarms, or equipment status.');
            _addSuggestions();
        } else {
            _addSystemMessage('AI Assistant is available but needs an API key to answer questions. Set CHAT_API_KEY in your .env file and restart to enable live responses.\n\nBrowse previous conversations from the dropdown above to see examples of what the assistant can do.');
        }
    }

    function _addSuggestions() {
        const container = document.createElement('div');
        container.className = 'chat-suggestions';
        _SUGGESTIONS.forEach(text => {
            const chip = document.createElement('button');
            chip.className = 'chat-suggestion-chip';
            chip.textContent = text;
            chip.onclick = () => {
                _input.value = text;
                container.remove();
                send();
            };
            container.appendChild(chip);
        });
        _messages.appendChild(container);
        _messages.scrollTop = _messages.scrollHeight;
    }

    async function loadConversations() {
        try {
            const token = getToken();
            if (!token) return;
            const resp = await fetch('/api/chat/history?limit=5', { headers: { 'Authorization': 'Bearer ' + token } });
            if (!resp.ok) return;
            const data = await resp.json();
            _conversations = data.conversations || [];
            _renderConvSelect();
        } catch (e) {}
    }

    function _renderConvSelect() {
        if (!_convSelect) return;
        const current = _convSelect.value;
        _convSelect.innerHTML = '<option value="">New conversation</option>';
        _conversations.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.conversation_id;
            opt.textContent = c.preview || ('Chat ' + c.conversation_id.substring(0, 8));
            _convSelect.appendChild(opt);
        });
        _convSelect.value = current;
    }

    async function switchConversation(convId) {
        if (!convId) { newChat(); return; }
        _convId = convId;
        _messages.innerHTML = '';
        _destroyCharts();

        try {
            const token = getToken();
            const resp = await fetch(`/api/chat/messages?conversation_id=${encodeURIComponent(convId)}`, {
                headers: { 'Authorization': 'Bearer ' + token }
            });
            if (!resp.ok) {
                _addSystemMessage('Could not load conversation.');
                return;
            }
            const data = await resp.json();
            if (data.messages && data.messages.length > 0) {
                data.messages.forEach(m => {
                    _addMessage(m.role, m.message, m.model_used || '');
                });
            } else {
                _addSystemMessage('Empty conversation.');
            }
        } catch (e) {
            _addSystemMessage('Failed to load conversation.');
        }
    }

    async function send() {
        if (_sending) return;
        const text = (_input.value || '').trim();
        if (!text) return;
        _input.value = '';
        _sending = true;
        _sendBtn.disabled = true;
        // Remove suggestion chips when user sends a message
        const suggestions = _messages.querySelector('.chat-suggestions');
        if (suggestions) suggestions.remove();
        _addMessage('user', text);
        const typingEl = _addTyping();

        try {
            const token = getToken();
            const resp = await fetch('/api/chat/ask', {
                method: 'POST',
                headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text, conversation_id: _convId }),
            });
            if (typingEl && typingEl.parentNode) typingEl.remove();

            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                _addMessage('assistant', err.detail || 'An error occurred.');
                return;
            }

            const data = await resp.json();
            _convId = data.conversation_id;
            _addMessage('assistant', data.reply || 'No response.', data.model_used);
            if (data.chart) _renderChart(data.chart);
            loadConversations();
        } catch (e) {
            if (typingEl && typingEl.parentNode) typingEl.remove();
            _addMessage('assistant', 'Network error. Please check your connection.');
        } finally {
            _sending = false;
            _sendBtn.disabled = false;
            _input.focus();
        }
    }

    function _addMessage(role, text, modelUsed) {
        const div = document.createElement('div');
        div.className = 'chat-msg ' + role;
        const textNode = document.createElement('span');
        textNode.textContent = text;
        div.appendChild(textNode);
        if (role === 'assistant' && modelUsed) {
            const tag = document.createElement('span');
            tag.className = 'chat-model-tag';
            tag.textContent = modelUsed;
            div.appendChild(tag);
        }
        _messages.appendChild(div);
        _messages.scrollTop = _messages.scrollHeight;
        return div;
    }

    function _addSystemMessage(text) {
        const div = document.createElement('div');
        div.className = 'chat-msg assistant';
        div.textContent = text;
        _messages.appendChild(div);
    }

    function _addTyping() {
        const div = document.createElement('div');
        div.className = 'chat-typing';
        div.innerHTML = 'Thinking <span class="chat-typing-dots"><span>.</span><span>.</span><span>.</span></span>';
        _messages.appendChild(div);
        _messages.scrollTop = _messages.scrollHeight;
        return div;
    }

    function _renderChart(chartData) {
        if (!chartData || !chartData.values || !chartData.values.length) return;
        if (typeof Chart === 'undefined') return;
        const container = document.createElement('div');
        container.className = 'chat-chart-container';
        const canvas = document.createElement('canvas');
        container.appendChild(canvas);
        _messages.appendChild(container);
        _messages.scrollTop = _messages.scrollHeight;
        const labels = chartData.labels.map(l => { try { return new Date(l).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}); } catch(e) { return l; } });
        const chart = new Chart(canvas, {
            type: 'line',
            data: { labels, datasets: [{ label: chartData.label || 'Value', data: chartData.values, borderColor: '#c8102e', backgroundColor: 'rgba(200,16,46,0.08)', fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 }] },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: true, position: 'top', labels: { font: { size: 11 } } } }, scales: { x: { display: true, ticks: { maxTicksLimit: 6, font: { size: 10 } } }, y: { display: true, ticks: { font: { size: 10 } } } } },
        });
        _chartInstances.push(chart);
    }

    function _destroyCharts() { _chartInstances.forEach(c => { try { c.destroy(); } catch(e) {} }); _chartInstances = []; }

    let _unreadCount = 0;
    let _pendingNotifications = [];

    function onNotification(notif) {
        _pendingNotifications.push(notif);
        _unreadCount++;
        _updateBadge();

        // If panel is open, show immediately
        if (_panel && _panel.classList.contains('open')) {
            _showNotification(notif);
            _unreadCount = 0;
            _updateBadge();
        }
    }

    function _showNotification(notif) {
        const sevColor = notif.severity === 'critical' ? '#c8102e' : notif.severity === 'warning' ? '#e8a317' : '#3b82f6';
        const div = document.createElement('div');
        div.className = 'chat-msg assistant';
        div.style.borderLeft = `4px solid ${sevColor}`;
        const title = document.createElement('strong');
        title.textContent = notif.title;
        title.style.color = sevColor;
        div.appendChild(title);
        div.appendChild(document.createElement('br'));
        const body = document.createElement('span');
        body.textContent = notif.message;
        div.appendChild(body);
        const ts = document.createElement('span');
        ts.className = 'chat-model-tag';
        ts.textContent = new Date(notif.timestamp).toLocaleTimeString();
        div.appendChild(ts);
        _messages.appendChild(div);
        _messages.scrollTop = _messages.scrollHeight;
    }

    function _updateBadge() {
        let badge = _fab ? _fab.querySelector('.chat-badge') : null;
        if (_unreadCount > 0) {
            if (!badge && _fab) {
                badge = document.createElement('span');
                badge.className = 'chat-badge';
                _fab.appendChild(badge);
            }
            if (badge) badge.textContent = _unreadCount > 9 ? '9+' : _unreadCount;
            if (_fab) _fab.classList.add('chat-fab-pulse');
        } else {
            if (badge) badge.remove();
            if (_fab) _fab.classList.remove('chat-fab-pulse');
        }
    }

    // Override toggle to clear badge and show pending notifications
    const _origToggle = toggle;
    function toggleWithBadge() {
        _origToggle();
        if (_panel && _panel.classList.contains('open') && _pendingNotifications.length > 0) {
            _pendingNotifications.forEach(n => _showNotification(n));
            _pendingNotifications = [];
            _unreadCount = 0;
            _updateBadge();
        }
    }

    return { init: () => { inject(); checkStatus(); }, toggle: toggleWithBadge, newChat, send, switchConversation, loadConversations, onNotification };
})();

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => ChatWidget.init());
} else {
    ChatWidget.init();
}
