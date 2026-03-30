"""
HMI routes for PLC4X Manager FastAPI.

Endpoints:
    GET  /api/hmi/config                          — full HMI config
    PUT  /api/hmi/config                          — save full HMI config (@require_admin)
    POST /api/hmi/plants                          — create plant (@require_admin)
    PUT  /api/hmi/plants/{pid}                    — update plant name (@require_admin)
    DELETE /api/hmi/plants/{pid}                  — delete plant (@require_admin)
    POST /api/hmi/plants/{pid}/areas              — create area (@require_admin)
    PUT  /api/hmi/areas/{aid}                     — update area name (@require_admin)
    DELETE /api/hmi/areas/{aid}                   — delete area (@require_admin)
    POST /api/hmi/areas/{aid}/equipment           — create equipment (@require_admin)
    PUT  /api/hmi/equipment/{eid}                 — update equipment (@require_admin)
    DELETE /api/hmi/equipment/{eid}               — delete equipment (@require_admin)
    PUT  /api/hmi/equipment/{eid}/screen          — save screen config (@require_admin)
    POST /api/hmi/upload-image                    — upload background image (@require_admin)
    POST /api/hmi/load-demo                       — load demo HMI config (@require_admin)
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from auth import CurrentUser, get_current_user, require_admin

# =============================================
# HMI config helpers (ported from app.py)
# =============================================

_CONFIG_DIR = os.path.dirname(os.environ.get("CONFIG_PATH", "/app/config/config.yml"))
HMI_CONFIG_PATH = os.path.join(_CONFIG_DIR, "hmi-screens.json")
HMI_IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static", "hmi-images")
HMI_ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"}


def load_hmi_config() -> dict:
    """Loads HMI config from hmi-screens.json. Returns default if not found."""
    try:
        with open(HMI_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"plants": []}


def save_hmi_config(config: dict) -> None:
    """Saves HMI config to hmi-screens.json atomically."""
    tmp = HMI_CONFIG_PATH + ".tmp"
    os.makedirs(os.path.dirname(HMI_CONFIG_PATH), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, HMI_CONFIG_PATH)


def _find_plant(config: dict, pid: str) -> tuple[Optional[int], Optional[dict]]:
    for i, plant in enumerate(config.get("plants", [])):
        if plant.get("id") == pid:
            return i, plant
    return None, None


def _find_area(config: dict, aid: str) -> tuple[Optional[int], Optional[int], Optional[dict]]:
    for pi, plant in enumerate(config.get("plants", [])):
        for ai, area in enumerate(plant.get("areas", [])):
            if area.get("id") == aid:
                return pi, ai, area
    return None, None, None


def _find_equipment(config: dict, eid: str) -> tuple[Optional[int], Optional[int], Optional[int], Optional[dict]]:
    for pi, plant in enumerate(config.get("plants", [])):
        for ai, area in enumerate(plant.get("areas", [])):
            for ei, equip in enumerate(area.get("equipment", [])):
                if equip.get("id") == eid:
                    return pi, ai, ei, equip
    return None, None, None, None


def _safe_image_filename(filename: str) -> str:
    """Returns a safe version of the filename for HMI image uploads."""
    name, ext = os.path.splitext(filename)
    name = re.sub(r'[^a-zA-Z0-9._-]', '_', name)
    name = name[:80]
    return name + ext.lower()


# =============================================
# Router
# =============================================

router = APIRouter(prefix="/api/hmi", tags=["hmi"])


# =============================================
# GET /api/hmi/config
# =============================================

@router.get("/config")
async def api_hmi_get_config(user: CurrentUser = Depends(get_current_user)):
    """Returns the full HMI configuration."""
    return load_hmi_config()


# =============================================
# PUT /api/hmi/config
# =============================================

@router.put("/config")
async def api_hmi_put_config(data: dict, user: CurrentUser = Depends(require_admin)):
    """Saves the full HMI configuration (for auto-save from editor)."""
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid HMI config: expected a JSON object")
    if not isinstance(data.get("plants", []), list):
        raise HTTPException(status_code=400, detail="Invalid HMI config: 'plants' must be a list")
    save_hmi_config(data)
    return {"message": "HMI config saved"}


# =============================================
# POST /api/hmi/plants
# =============================================

@router.post("/plants", status_code=201)
async def api_hmi_create_plant(data: dict, user: CurrentUser = Depends(require_admin)):
    """Creates a new plant."""
    if not data or not data.get("name", "").strip():
        raise HTTPException(status_code=400, detail="Field 'name' is required")
    config = load_hmi_config()
    plant = {
        "id": f"plant-{int(time.time() * 1000)}",
        "name": data["name"].strip(),
        "areas": [],
    }
    config.setdefault("plants", []).append(plant)
    save_hmi_config(config)
    return plant


# =============================================
# PUT /api/hmi/plants/{pid}
# =============================================

@router.put("/plants/{pid}")
async def api_hmi_update_plant(pid: str, data: dict, user: CurrentUser = Depends(require_admin)):
    """Updates a plant's name."""
    if not data or not data.get("name", "").strip():
        raise HTTPException(status_code=400, detail="Field 'name' is required")
    config = load_hmi_config()
    i, plant = _find_plant(config, pid)
    if plant is None:
        raise HTTPException(status_code=404, detail=f"Plant '{pid}' not found")
    config["plants"][i]["name"] = data["name"].strip()
    save_hmi_config(config)
    return config["plants"][i]


# =============================================
# DELETE /api/hmi/plants/{pid}
# =============================================

@router.delete("/plants/{pid}")
async def api_hmi_delete_plant(pid: str, user: CurrentUser = Depends(require_admin)):
    """Deletes a plant and all its children."""
    config = load_hmi_config()
    i, plant = _find_plant(config, pid)
    if plant is None:
        raise HTTPException(status_code=404, detail=f"Plant '{pid}' not found")
    config["plants"].pop(i)
    save_hmi_config(config)
    return {"message": f"Plant '{pid}' deleted"}


# =============================================
# POST /api/hmi/plants/{pid}/areas
# =============================================

@router.post("/plants/{pid}/areas", status_code=201)
async def api_hmi_create_area(pid: str, data: dict, user: CurrentUser = Depends(require_admin)):
    """Creates a new area under a plant."""
    if not data or not data.get("name", "").strip():
        raise HTTPException(status_code=400, detail="Field 'name' is required")
    config = load_hmi_config()
    i, plant = _find_plant(config, pid)
    if plant is None:
        raise HTTPException(status_code=404, detail=f"Plant '{pid}' not found")
    area = {
        "id": f"area-{int(time.time() * 1000)}",
        "name": data["name"].strip(),
        "equipment": [],
    }
    config["plants"][i].setdefault("areas", []).append(area)
    save_hmi_config(config)
    return area


# =============================================
# PUT /api/hmi/areas/{aid}
# =============================================

@router.put("/areas/{aid}")
async def api_hmi_update_area(aid: str, data: dict, user: CurrentUser = Depends(require_admin)):
    """Updates an area's name."""
    if not data or not data.get("name", "").strip():
        raise HTTPException(status_code=400, detail="Field 'name' is required")
    config = load_hmi_config()
    pi, ai, area = _find_area(config, aid)
    if area is None:
        raise HTTPException(status_code=404, detail=f"Area '{aid}' not found")
    config["plants"][pi]["areas"][ai]["name"] = data["name"].strip()
    save_hmi_config(config)
    return config["plants"][pi]["areas"][ai]


# =============================================
# DELETE /api/hmi/areas/{aid}
# =============================================

@router.delete("/areas/{aid}")
async def api_hmi_delete_area(aid: str, user: CurrentUser = Depends(require_admin)):
    """Deletes an area and all its children."""
    config = load_hmi_config()
    pi, ai, area = _find_area(config, aid)
    if area is None:
        raise HTTPException(status_code=404, detail=f"Area '{aid}' not found")
    config["plants"][pi]["areas"].pop(ai)
    save_hmi_config(config)
    return {"message": f"Area '{aid}' deleted"}


# =============================================
# POST /api/hmi/areas/{aid}/equipment
# =============================================

@router.post("/areas/{aid}/equipment", status_code=201)
async def api_hmi_create_equipment(aid: str, data: dict, user: CurrentUser = Depends(require_admin)):
    """Creates new equipment under an area."""
    if not data or not data.get("name", "").strip():
        raise HTTPException(status_code=400, detail="Field 'name' is required")
    config = load_hmi_config()
    pi, ai, area = _find_area(config, aid)
    if area is None:
        raise HTTPException(status_code=404, detail=f"Area '{aid}' not found")
    equip = {
        "id": f"equip-{int(time.time() * 1000)}",
        "name": data["name"].strip(),
        "device": data.get("device", ""),
        "screen": {
            "elements": [],
            "backgroundImage": "",
            "width": 1280,
            "height": 720,
        },
    }
    config["plants"][pi]["areas"][ai].setdefault("equipment", []).append(equip)
    save_hmi_config(config)
    return equip


# =============================================
# PUT /api/hmi/equipment/{eid}
# =============================================

@router.put("/equipment/{eid}")
async def api_hmi_update_equipment(eid: str, data: dict, user: CurrentUser = Depends(require_admin)):
    """Updates equipment name and/or device."""
    if not data:
        raise HTTPException(status_code=400, detail="Request body is required")
    config = load_hmi_config()
    pi, ai, ei, equip = _find_equipment(config, eid)
    if equip is None:
        raise HTTPException(status_code=404, detail=f"Equipment '{eid}' not found")
    if "name" in data and data["name"].strip():
        config["plants"][pi]["areas"][ai]["equipment"][ei]["name"] = data["name"].strip()
    if "device" in data:
        config["plants"][pi]["areas"][ai]["equipment"][ei]["device"] = data["device"]
    save_hmi_config(config)
    return config["plants"][pi]["areas"][ai]["equipment"][ei]


# =============================================
# DELETE /api/hmi/equipment/{eid}
# =============================================

@router.delete("/equipment/{eid}")
async def api_hmi_delete_equipment(eid: str, user: CurrentUser = Depends(require_admin)):
    """Deletes equipment."""
    config = load_hmi_config()
    pi, ai, ei, equip = _find_equipment(config, eid)
    if equip is None:
        raise HTTPException(status_code=404, detail=f"Equipment '{eid}' not found")
    config["plants"][pi]["areas"][ai]["equipment"].pop(ei)
    save_hmi_config(config)
    return {"message": f"Equipment '{eid}' deleted"}


# =============================================
# PUT /api/hmi/equipment/{eid}/screen
# =============================================

@router.put("/equipment/{eid}/screen")
async def api_hmi_save_screen(eid: str, data: dict, user: CurrentUser = Depends(require_admin)):
    """Saves the screen configuration (elements array) for an equipment."""
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid screen config: expected a JSON object")
    config = load_hmi_config()
    pi, ai, ei, equip = _find_equipment(config, eid)
    if equip is None:
        raise HTTPException(status_code=404, detail=f"Equipment '{eid}' not found")
    screen = config["plants"][pi]["areas"][ai]["equipment"][ei].setdefault("screen", {})
    if "elements" in data:
        screen["elements"] = data["elements"]
    if "backgroundImage" in data:
        screen["backgroundImage"] = data["backgroundImage"]
    if "width" in data:
        screen["width"] = data["width"]
    if "height" in data:
        screen["height"] = data["height"]
    save_hmi_config(config)
    return config["plants"][pi]["areas"][ai]["equipment"][ei]


# =============================================
# POST /api/hmi/upload-image
# =============================================

@router.post("/upload-image")
async def api_hmi_upload_image(
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_admin),
):
    """Uploads a background image for HMI screens.

    Accepts multipart/form-data with a 'file' field.
    Saves to static/hmi-images/ with a safe filename.
    Returns: {"url": "/static/hmi-images/filename.ext"}
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in HMI_ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(HMI_ALLOWED_EXTENSIONS))
        raise HTTPException(status_code=400, detail=f"File type not allowed. Allowed: {allowed}")

    safe_name = _safe_image_filename(file.filename)
    images_dir = os.path.normpath(HMI_IMAGES_DIR)
    os.makedirs(images_dir, exist_ok=True)
    save_path = os.path.join(images_dir, safe_name)

    contents = await file.read()
    with open(save_path, "wb") as f:
        f.write(contents)

    return {"url": f"/static/hmi-images/{safe_name}"}


# =============================================
# POST /api/hmi/load-demo
# =============================================

@router.post("/load-demo")
async def api_hmi_load_demo(user: CurrentUser = Depends(require_admin)):
    """Loads demo HMI config from hmi-demo.json and merges into existing config.

    Does not overwrite existing plants — only adds plants from the demo
    that do not already exist (matched by name).
    """
    demo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hmi-demo.json")
    try:
        with open(demo_path, "r", encoding="utf-8") as f:
            demo = json.load(f)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="hmi-demo.json not found")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"hmi-demo.json is invalid JSON: {e}")

    config = load_hmi_config()
    existing_names = {p["name"] for p in config.get("plants", [])}
    added = 0
    for plant in demo.get("plants", []):
        if plant.get("name") not in existing_names:
            config.setdefault("plants", []).append(plant)
            existing_names.add(plant["name"])
            added += 1

    save_hmi_config(config)
    return {"message": f"Demo loaded: {added} plant(s) added", "plantsAdded": added}
