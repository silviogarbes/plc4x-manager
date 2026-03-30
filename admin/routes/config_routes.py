"""
Configuration routes for PLC4X Manager FastAPI.

Endpoints:
- GET /api/config         — return full config
- PUT /api/config         — save complete config (@require_admin)
- PUT /api/config/server  — update server settings only (@require_admin)
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from auth import CurrentUser, get_current_user, require_admin
from config_manager import CONFIG_LOCK, load_config, save_config
from validators import _validate_device_tags, _validate_name, _validate_tag_address


router = APIRouter(prefix="/api", tags=["config"])


# =============================================
# GET /api/config
# =============================================

@router.get("/config")
async def api_get_config(user: CurrentUser = Depends(get_current_user)):
    """Returns the current configuration."""
    return load_config()


# =============================================
# PUT /api/config
# =============================================

@router.put("/config")
async def api_save_config(
    config: dict,
    user: CurrentUser = Depends(require_admin),
):
    """Saves the complete configuration."""
    if not config:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Empty payload")

    if not isinstance(config.get("devices"), list):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid config: 'devices' must be a list")

    if "name" not in config or "tcpPort" not in config:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid config: 'name' and 'tcpPort' are required")

    all_errors: list[str] = []
    for dev in config.get("devices", []):
        if not isinstance(dev, dict) or "name" not in dev or "connectionString" not in dev:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=400,
                detail="Invalid config: each device must have 'name' and 'connectionString'",
            )
        tag_errors = _validate_device_tags(dev)
        if tag_errors:
            all_errors.extend([f"{dev['name']}: {e}" for e in tag_errors])

    if all_errors:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail="Invalid tag addresses: " + "; ".join(all_errors[:5]),
        )

    def _save():
        with CONFIG_LOCK:
            save_config(config)

    await asyncio.to_thread(_save)
    return {"message": "Configuration saved successfully"}


# =============================================
# PUT /api/config/server
# =============================================

@router.put("/config/server")
async def api_update_server_config(
    data: dict,
    user: CurrentUser = Depends(require_admin),
):
    """Updates the OPC-UA server settings (name, version, tcpPort, disableInsecureEndpoint, dir)."""
    if not data:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Missing JSON body")

    def _update_server():
        with CONFIG_LOCK:
            config = load_config()
            for key in ("version", "name", "tcpPort", "disableInsecureEndpoint", "dir"):
                if key in data:
                    config[key] = data[key]
            save_config(config)

    await asyncio.to_thread(_update_server)
    return {"message": "Server configuration updated"}
