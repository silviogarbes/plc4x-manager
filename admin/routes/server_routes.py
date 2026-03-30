"""
Server control and backup management routes for PLC4X Manager FastAPI.

Endpoints:
  GET    /api/server/status                   — Docker container status
  GET    /api/services/status                 — health check ALL services
  POST   /api/server/restart                  — restart PLC4X container (@require_admin)
  GET    /api/server/logs                     — container logs (@require_operator)

  GET    /api/backups                          — list backups (@require_admin)
  GET    /api/backups/{filename}/changes       — diff summary vs. previous (@require_admin)
  GET    /api/backups/{filename}/content       — raw YAML content (@require_admin)
  GET    /api/backups/{filename}/download      — download as attachment (@require_admin)
  GET    /api/backups/{filename}/diff          — unified diff vs. current (@require_admin)
  POST   /api/backups/{filename}/restore       — restore from backup (@require_admin)
  POST   /api/backups/cleanup                  — trim old backups (@require_admin)
  POST   /api/backups/create                  — create manual backup (@require_admin)
  POST   /api/backups/upload                  — restore from uploaded YAML (@require_admin)
"""

from __future__ import annotations

import asyncio
import datetime
import difflib
import io
import os
import re
import urllib.request

import docker
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from auth import CurrentUser, get_current_user, require_admin, require_operator
from docker_utils import get_container_by_service, get_docker_client
from config_manager import (
    BACKUP_DIR,
    BACKUP_MAX_FILES,
    CONFIG_LOCK,
    CONFIG_PATH,
    _atomic_yaml_write,
    load_config,
    save_config,
)

router = APIRouter(tags=["server"])

# =============================================
# Constants
# =============================================

_SAFE_FILENAME_RE = re.compile(r'^[a-zA-Z0-9._-]+$')


# =============================================
# Docker helpers
# =============================================

def _get_server_status() -> dict:
    """Check the PLC4X Server container status."""
    try:
        client = get_docker_client()
        container = get_container_by_service(client, "plc4x-server")
        if container is None:
            return {"status": "not_found", "running": False, "id": None, "image": None}
        return {
            "status": container.status,
            "running": container.status == "running",
            "id": container.short_id,
            "image": container.image.tags[0] if container.image.tags else "unknown"
        }
    except docker.errors.NotFound:
        return {"status": "not_found", "running": False, "id": None, "image": None}
    except Exception as e:
        return {"status": "error", "running": False, "id": None, "image": None, "error": str(e)}


# =============================================
# Backup helpers
# =============================================

def _safe_filename(filename: str) -> bool:
    return bool(filename) and bool(_SAFE_FILENAME_RE.match(filename))


def _cleanup_old_backups() -> None:
    """Keep only the most recent BACKUP_MAX_FILES backup files."""
    if not os.path.exists(BACKUP_DIR):
        return
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".yml")],
        reverse=True,
    )
    for old_file in files[BACKUP_MAX_FILES:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old_file))
        except OSError:
            pass


def _summarize_diff(added: list, removed: list) -> list:
    """Creates a human-readable summary of YAML diff changes."""
    changes = []
    for line in removed:
        line = line.strip()
        if line and not line.startswith("#"):
            if "name:" in line or "alias:" in line:
                changes.append(f"Removed: {line}")
            elif "connectionString:" in line:
                changes.append(f"Removed connection: {line}")
    for line in added:
        line = line.strip()
        if line and not line.startswith("#"):
            if "name:" in line or "alias:" in line:
                changes.append(f"Added: {line}")
            elif "connectionString:" in line:
                changes.append(f"Added connection: {line}")
            elif "tcpPort:" in line or "disableInsecure" in line:
                changes.append(f"Changed: {line}")
    if not changes and (added or removed):
        changes.append(f"{len(added)} lines added, {len(removed)} lines removed")
    return changes[:10]


# =============================================
# Server routes
# =============================================

@router.get("/api/server/status")
async def api_server_status(user: CurrentUser = Depends(get_current_user)):
    """Returns the PLC4X server container status."""
    return await asyncio.to_thread(_get_server_status)


def _get_services_status_sync() -> list:
    """Synchronous helper that checks all service statuses (called via asyncio.to_thread)."""
    services = []

    # PLC4X Server
    srv = _get_server_status()
    services.append({
        "name": "PLC4X OPC-UA Server",
        "container": "plc4x-server",
        "port": 12687,
        "status": "online" if srv.get("running") else "offline",
        "url": "opc.tcp://<host>:12687/plc4x",
        "description": "Reads PLCs and exposes tags as OPC-UA nodes"
    })

    # InfluxDB
    try:
        req = urllib.request.Request(
            os.environ.get("INFLUXDB_URL", "http://influxdb:8086") + "/health",
            headers={"User-Agent": "plc4x-manager"}
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            influx_ok = resp.status == 200
    except Exception:
        influx_ok = False
    services.append({
        "name": "InfluxDB",
        "container": "influxdb",
        "port": 8086,
        "status": "online" if influx_ok else "offline",
        "url": "http://<host>:8086",
        "description": "Time-series database for tag history (90d raw, 2y hourly, daily forever)"
    })

    # Grafana
    try:
        req = urllib.request.Request(
            "http://grafana:3000/api/health",
            headers={"User-Agent": "plc4x-manager"}
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            grafana_ok = resp.status == 200
    except Exception:
        grafana_ok = False
    services.append({
        "name": "Grafana",
        "container": "grafana",
        "port": 3000,
        "status": "online" if grafana_ok else "offline",
        "url": "http://<host>:3000",
        "description": "Dashboards: Overview, Device Detail, AI Predictions"
    })

    # Mosquitto
    mqtt_ok = False
    try:
        client = get_docker_client()
        c = get_container_by_service(client, "mosquitto")
        mqtt_ok = c is not None and c.status == "running"
    except Exception:
        pass
    services.append({
        "name": "MQTT Broker (Mosquitto)",
        "container": "mosquitto",
        "port": 1883,
        "status": "online" if mqtt_ok else "offline",
        "url": "mqtt://<host>:1883",
        "description": "Real-time tag publishing. Subscribe to plc4x/# for all tags"
    })

    # ML Predictor
    ml_ok = False
    try:
        client = get_docker_client()
        c = get_container_by_service(client, "plc4x-ml")
        ml_ok = c is not None and c.status == "running"
    except Exception:
        pass
    services.append({
        "name": "AI Predictions (Prophet + sklearn)",
        "container": "plc4x-ml",
        "port": None,
        "status": "online" if ml_ok else "offline",
        "url": None,
        "description": "Forecast, anomaly detection, trend analysis. Results in Grafana"
    })

    return services


@router.get("/api/services/status")
async def api_services_status(user: CurrentUser = Depends(get_current_user)):
    """Returns status of all services in the stack."""
    return await asyncio.to_thread(_get_services_status_sync)


def _restart_server_sync():
    """Synchronous helper to restart the PLC4X container."""
    client = get_docker_client()
    container = get_container_by_service(client, "plc4x-server")
    if container is None:
        raise docker.errors.NotFound("plc4x-server container not found")
    container.restart(timeout=10)


@router.post("/api/server/restart")
async def api_restart_server(user: CurrentUser = Depends(require_admin)):
    """Restart the PLC4X container."""
    try:
        await asyncio.to_thread(_restart_server_sync)
        return {"message": "Server restarted successfully"}
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="Server container not found")
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")


def _get_server_logs_sync(lines: int) -> str:
    """Synchronous helper to fetch container logs."""
    client = get_docker_client()
    container = get_container_by_service(client, "plc4x-server")
    if container is None:
        raise docker.errors.NotFound("plc4x-server container not found")
    return container.logs(tail=lines, timestamps=True).decode("utf-8", errors="replace")


@router.get("/api/server/logs")
async def api_server_logs(
    lines: int = Query(default=50, ge=1, le=5000),
    user: CurrentUser = Depends(require_operator),
):
    """Get container logs."""
    try:
        logs = await asyncio.to_thread(_get_server_logs_sync, lines)
        return {"logs": logs}
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="Container not found")
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")


# =============================================
# Backup routes
# =============================================

@router.get("/api/backups")
async def api_list_backups(user: CurrentUser = Depends(require_admin)):
    """List backups with metadata."""
    if not os.path.exists(BACKUP_DIR):
        return []
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".yml")],
        reverse=True
    )
    backups = []
    for fname in files:
        fpath = os.path.join(BACKUP_DIR, fname)
        stat = os.stat(fpath)
        backups.append({
            "filename": fname,
            "date": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "size": stat.st_size,
        })
    return backups


@router.get("/api/backups/{filename}/changes")
async def api_backup_changes(filename: str, user: CurrentUser = Depends(require_admin)):
    """Returns a summary of changes between this backup and the previous one."""
    if not _safe_filename(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    fpath = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="Backup not found")

    files = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith(".yml")])
    idx = files.index(filename) if filename in files else -1
    if idx <= 0:
        return {"changes": [], "devices": 0, "tags": 0}

    prev_file = os.path.join(BACKUP_DIR, files[idx - 1])
    try:
        with open(prev_file, "r", encoding="utf-8") as f:
            prev_content = f.read()
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
        parsed = yaml.safe_load(content) or {}
        devices = parsed.get("devices", [])
        tag_count = sum(len(d.get("tags", [])) for d in devices)

        diff_lines = list(difflib.unified_diff(
            prev_content.splitlines(), content.splitlines(), lineterm=""
        ))
        added = [l[1:] for l in diff_lines if l.startswith("+") and not l.startswith("+++")]
        removed = [l[1:] for l in diff_lines if l.startswith("-") and not l.startswith("---")]
        changes = _summarize_diff(added, removed) if (added or removed) else []
        return {"changes": changes, "devices": len(devices), "tags": tag_count}
    except Exception:
        return {"changes": [], "devices": 0, "tags": 0}


@router.get("/api/backups/{filename}/content")
async def api_backup_content(filename: str, user: CurrentUser = Depends(require_admin)):
    """Returns the raw content of a backup file."""
    if not _safe_filename(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    backup_path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(backup_path):
        raise HTTPException(status_code=404, detail="Backup not found")
    with open(backup_path, "r", encoding="utf-8") as f:
        content = f.read()
    return {"filename": filename, "content": content}


@router.get("/api/backups/{filename}/download")
async def api_backup_download(filename: str, user: CurrentUser = Depends(require_admin)):
    """Downloads a backup file as YAML attachment."""
    if not _safe_filename(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    backup_path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(backup_path):
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(
        path=backup_path,
        media_type="text/yaml",
        filename=filename,
    )


@router.get("/api/backups/{filename}/diff")
async def api_backup_diff(filename: str, user: CurrentUser = Depends(require_admin)):
    """Returns a unified diff between a backup and the current config."""
    if not _safe_filename(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    backup_path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(backup_path):
        raise HTTPException(status_code=404, detail="Backup not found")
    with open(backup_path, "r", encoding="utf-8") as f:
        backup_lines = f.read().splitlines()
    current_lines = []
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            current_lines = f.read().splitlines()
    diff = list(difflib.unified_diff(
        backup_lines, current_lines,
        fromfile=f"backup ({filename})",
        tofile="current (config.yml)",
        lineterm=""
    ))
    return {"filename": filename, "diff": "\n".join(diff)}


@router.post("/api/backups/{filename}/restore")
async def api_restore_backup(filename: str, user: CurrentUser = Depends(require_admin)):
    """Restore configuration from a backup file."""
    if not _safe_filename(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    backup_path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(backup_path):
        raise HTTPException(status_code=404, detail="Backup not found")

    def _do_restore():
        with CONFIG_LOCK:
            # Backup current config before restoring
            if os.path.exists(CONFIG_PATH):
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                bk_path = os.path.join(BACKUP_DIR, f"config_{ts}.yml")
                os.makedirs(BACKUP_DIR, exist_ok=True)
                with open(CONFIG_PATH, "r", encoding="utf-8") as src:
                    with open(bk_path, "w", encoding="utf-8") as dst:
                        dst.write(src.read())
            # Restore from backup
            with open(backup_path, "r", encoding="utf-8") as f:
                restored = yaml.safe_load(f)
            save_config(restored)
            _cleanup_old_backups()

    await asyncio.to_thread(_do_restore)
    return {"message": f"Backup '{filename}' restored"}


@router.post("/api/backups/cleanup")
async def api_cleanup_backups(user: CurrentUser = Depends(require_admin)):
    """Trim old backups, keeping only the most recent ones."""
    _cleanup_old_backups()
    if not os.path.exists(BACKUP_DIR):
        return {"message": "No backups to clean up", "remaining": 0}
    remaining = len([f for f in os.listdir(BACKUP_DIR) if f.endswith(".yml")])
    return {"message": f"Cleanup complete. Keeping last {BACKUP_MAX_FILES} backups.", "remaining": remaining}


@router.post("/api/backups/create", status_code=201)
async def api_create_backup(user: CurrentUser = Depends(require_admin)):
    """Creates a manual backup of the current configuration."""
    result: dict = {}

    def _do_create_backup():
        with CONFIG_LOCK:
            config = load_config()
            os.makedirs(BACKUP_DIR, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(BACKUP_DIR, f"config_{ts}.yml")
            _atomic_yaml_write(backup_path, config)
            result["ts"] = ts

    await asyncio.to_thread(_do_create_backup)
    ts = result["ts"]
    return {"message": f"Backup created: config_{ts}.yml", "filename": f"config_{ts}.yml"}


@router.post("/api/backups/upload")
async def api_upload_restore(
    file: UploadFile,
    user: CurrentUser = Depends(require_admin),
):
    """Restore configuration from an uploaded YAML file."""
    if not file.filename or not file.filename.endswith((".yml", ".yaml")):
        raise HTTPException(status_code=400, detail="File must be a .yml or .yaml file")

    try:
        content = (await file.read()).decode("utf-8")
        restored = yaml.safe_load(content)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid YAML file")

    if not isinstance(restored, dict):
        raise HTTPException(status_code=400, detail="Invalid config format: expected a YAML mapping")
    if not isinstance(restored.get("devices"), list):
        raise HTTPException(status_code=400, detail="Invalid config: 'devices' must be a list")

    def _do_upload_restore():
        with CONFIG_LOCK:
            current = load_config()
            os.makedirs(BACKUP_DIR, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(BACKUP_DIR, f"config_{ts}.yml")
            _atomic_yaml_write(backup_path, current)
            save_config(restored)
            _cleanup_old_backups()

    await asyncio.to_thread(_do_upload_restore)
    dev_count = len(restored.get("devices", []))
    return {"message": f"Configuration restored from '{file.filename}' ({dev_count} devices)"}
