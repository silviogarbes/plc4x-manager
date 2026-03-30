"""
pylogix proxy routes — forwards discovery/diagnostic requests to the
plctag_service running inside the PLC4X server container.

All endpoints require admin role (R10: admin-only).

Endpoints:
  GET  /api/plctag/health                   — service health
  GET  /api/plctag/stats                    — connection statistics
  POST /api/plctag/discover                 — full tag discovery
  POST /api/plctag/discover/tags            — controller tags only
  POST /api/plctag/discover/programs        — list programs
  POST /api/plctag/diagnostics/identity     — PLC identity
  POST /api/plctag/diagnostics/health       — full PLC health
"""

from __future__ import annotations

import asyncio

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import CurrentUser, require_admin

router = APIRouter(tags=["plctag"])

PLCTAG_SERVICE_URL = "http://plc4x-server:5000"


def _proxy_get(path: str) -> dict:
    """Synchronous GET request to plctag service."""
    resp = requests.get(f"{PLCTAG_SERVICE_URL}{path}", timeout=15)
    return resp.json()


def _proxy_post(path: str, params: dict | None = None) -> dict:
    """Synchronous POST request to plctag service."""
    resp = requests.post(f"{PLCTAG_SERVICE_URL}{path}", params=params, timeout=30)
    if resp.status_code >= 400:
        data = resp.json()
        raise HTTPException(status_code=resp.status_code, detail=data.get("error", "Unknown error"))
    return resp.json()


@router.get("/api/plctag/health")
async def plctag_health(user: CurrentUser = Depends(require_admin)):
    """Check if pylogix service is running."""
    try:
        return await asyncio.to_thread(_proxy_get, "/health")
    except Exception as e:
        return {"status": "offline", "error": str(e)}


@router.get("/api/plctag/stats")
async def plctag_stats(user: CurrentUser = Depends(require_admin)):
    """Get PLC connection statistics (safety monitoring)."""
    try:
        return await asyncio.to_thread(_proxy_get, "/stats")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/api/plctag/discover")
async def plctag_discover(
    ip: str = Query(..., description="PLC IP address"),
    path: str = Query("1,0", description="CIP path (backplane,slot)"),
    user: CurrentUser = Depends(require_admin),
):
    """Full discovery: controller tags + program tags. May take 5-10 seconds."""
    return await asyncio.to_thread(_proxy_post, "/discover", {"ip": ip, "path": path})


@router.post("/api/plctag/discover/tags")
async def plctag_discover_tags(
    ip: str = Query(..., description="PLC IP address"),
    path: str = Query("1,0", description="CIP path (backplane,slot)"),
    user: CurrentUser = Depends(require_admin),
):
    """List controller-scoped tags only."""
    return await asyncio.to_thread(_proxy_post, "/discover/tags", {"ip": ip, "path": path})


@router.post("/api/plctag/discover/programs")
async def plctag_discover_programs(
    ip: str = Query(..., description="PLC IP address"),
    path: str = Query("1,0", description="CIP path (backplane,slot)"),
    user: CurrentUser = Depends(require_admin),
):
    """List all programs in the PLC."""
    return await asyncio.to_thread(_proxy_post, "/discover/programs", {"ip": ip, "path": path})


@router.post("/api/plctag/diagnostics/identity")
async def plctag_identity(
    ip: str = Query(..., description="PLC IP address"),
    path: str = Query("1,0", description="CIP path (backplane,slot)"),
    user: CurrentUser = Depends(require_admin),
):
    """Get PLC identity: model, firmware, serial."""
    return await asyncio.to_thread(_proxy_post, "/diagnostics/identity", {"ip": ip, "path": path})


@router.post("/api/plctag/diagnostics/health")
async def plctag_health_check(
    ip: str = Query(..., description="PLC IP address"),
    path: str = Query("1,0", description="CIP path (backplane,slot)"),
    user: CurrentUser = Depends(require_admin),
):
    """Get comprehensive PLC health: identity + tag count + programs."""
    return await asyncio.to_thread(_proxy_post, "/diagnostics/health", {"ip": ip, "path": path})


@router.post("/api/plctag/diagnostics/read")
async def plctag_read_tag(
    ip: str = Query(..., description="PLC IP address"),
    tag: str = Query(..., description="Tag name"),
    type: str = Query("DINT", description="Tag data type"),
    path: str = Query("1,0", description="CIP path (backplane,slot)"),
    user: CurrentUser = Depends(require_admin),
):
    """Read a single tag value directly via pylogix."""
    return await asyncio.to_thread(
        _proxy_post, "/diagnostics/read", {"ip": ip, "tag": tag, "type": type, "path": path}
    )


@router.post("/api/plctag/diagnostics/write")
async def plctag_write_tag(
    ip: str = Query(..., description="PLC IP address"),
    tag: str = Query(..., description="Tag name"),
    value: str = Query(..., description="Value to write"),
    path: str = Query("1,0", description="CIP path (backplane,slot)"),
    user: CurrentUser = Depends(require_admin),
):
    """Write a single tag value directly via pylogix."""
    return await asyncio.to_thread(
        _proxy_post, "/diagnostics/write", {"ip": ip, "tag": tag, "value": value, "path": path}
    )


@router.post("/api/plctag/diagnostics/batch-read")
async def plctag_batch_read(
    ip: str = Query(..., description="PLC IP address"),
    tags: str = Query(..., description="Comma-separated tag names"),
    path: str = Query("1,0", description="CIP path (backplane,slot)"),
    user: CurrentUser = Depends(require_admin),
):
    """Read multiple tags in a single PLC connection."""
    return await asyncio.to_thread(
        _proxy_post, "/diagnostics/batch-read", {"ip": ip, "tags": tags, "path": path}
    )


@router.post("/api/plctag/diagnostics/batch-write")
async def plctag_batch_write(
    request: Request,
    ip: str = Query(..., description="PLC IP address"),
    path: str = Query("1,0", description="CIP path (backplane,slot)"),
    user: CurrentUser = Depends(require_admin),
):
    """Write multiple tags in a single PLC connection. Body: [{tag, value}, ...]"""
    import json
    from fastapi import Request as _Req

    body = await request.json()

    def _do_batch_write():
        resp = requests.post(
            f"{PLCTAG_SERVICE_URL}/diagnostics/batch-write",
            params={"ip": ip, "path": path},
            json=body,
            timeout=30,
        )
        if resp.status_code >= 400:
            data = resp.json()
            raise HTTPException(status_code=resp.status_code, detail=data.get("error", "Unknown error"))
        return resp.json()

    return await asyncio.to_thread(_do_batch_write)
