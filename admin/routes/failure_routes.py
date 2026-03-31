"""
Predictive Maintenance failure routes for PLC4X Manager FastAPI.

Endpoints:
  GET    /api/failures/catalog          — list all failure types
  POST   /api/failures/catalog          — create failure type (@require_admin)
  PUT    /api/failures/catalog/{id}     — update failure type (@require_admin)
  DELETE /api/failures/catalog/{id}     — delete failure type (@require_admin)
  GET    /api/failures                  — list failure log entries
  POST   /api/failures                  — report a failure (@require_operator)
  PUT    /api/failures/{id}             — update failure entry (@require_operator)
  DELETE /api/failures/{id}             — delete failure entry (@require_admin)
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import CurrentUser, get_current_user, require_admin, require_operator

router = APIRouter(tags=["failures"])


# =============================================
# Failure Catalog CRUD
# =============================================

@router.get("/api/failures/catalog")
async def catalog_list(request: Request, user: CurrentUser = Depends(get_current_user)):
    """List all failure types in the catalog."""
    db = request.app.state.db
    async with db.execute("SELECT * FROM failure_catalog ORDER BY name") as cursor:
        rows = await cursor.fetchall()
    catalog = []
    for row in rows:
        catalog.append({
            "id": row["id"],
            "name": row["name"],
            "display_name": row["display_name"],
            "description": row["description"],
            "lookback_hours": row["lookback_hours"],
            "related_tags": json.loads(row["related_tags"]) if row["related_tags"] else [],
            "created_at": row["created_at"],
        })
    return {"catalog": catalog}


@router.post("/api/failures/catalog", status_code=201)
async def catalog_create(request: Request, body: dict, user: CurrentUser = Depends(require_admin)):
    """Create a new failure type in the catalog."""
    name = (body.get("name") or "").strip().lower().replace(" ", "_")
    display_name = (body.get("display_name") or "").strip()
    if not name or not display_name:
        raise HTTPException(status_code=400, detail="name and display_name are required")

    description = (body.get("description") or "").strip()
    lookback_hours = int(body.get("lookback_hours", 72))
    related_tags = json.dumps(body.get("related_tags", []))

    db = request.app.state.db
    try:
        async with db.execute(
            """INSERT INTO failure_catalog (name, display_name, description, lookback_hours, related_tags)
               VALUES (?, ?, ?, ?, ?)""",
            (name, display_name, description, lookback_hours, related_tags)
        ) as cursor:
            row_id = cursor.lastrowid
        await db.commit()
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=409, detail=f"Failure type '{name}' already exists")
        raise HTTPException(status_code=500, detail=str(e))

    return {"id": row_id, "name": name, "display_name": display_name}


@router.put("/api/failures/catalog/{catalog_id}")
async def catalog_update(catalog_id: int, request: Request, body: dict, user: CurrentUser = Depends(require_admin)):
    """Update a failure type in the catalog."""
    db = request.app.state.db
    async with db.execute("SELECT * FROM failure_catalog WHERE id = ?", (catalog_id,)) as c:
        existing = await c.fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Failure type not found")

    display_name = (body.get("display_name") or existing["display_name"]).strip()
    description = (body.get("description") if "description" in body else existing["description"]) or ""
    lookback_hours = int(body.get("lookback_hours", existing["lookback_hours"]))
    related_tags = json.dumps(body.get("related_tags")) if "related_tags" in body else existing["related_tags"]

    await db.execute(
        """UPDATE failure_catalog SET display_name = ?, description = ?, lookback_hours = ?, related_tags = ?
           WHERE id = ?""",
        (display_name, description, lookback_hours, related_tags, catalog_id)
    )
    await db.commit()
    return {"ok": True, "id": catalog_id}


@router.delete("/api/failures/catalog/{catalog_id}")
async def catalog_delete(catalog_id: int, request: Request, user: CurrentUser = Depends(require_admin)):
    """Delete a failure type from the catalog."""
    db = request.app.state.db
    async with db.execute("SELECT * FROM failure_catalog WHERE id = ?", (catalog_id,)) as c:
        existing = await c.fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Failure type not found")

    await db.execute("DELETE FROM failure_catalog WHERE id = ?", (catalog_id,))
    await db.commit()
    return {"ok": True, "deleted": existing["name"]}


# =============================================
# Failure Log CRUD
# =============================================

@router.get("/api/failures")
async def failure_log_list(
    request: Request,
    device: str = Query(default=""),
    failure_type: str = Query(default=""),
    lines: int = Query(default=100, ge=1, le=1000),
    user: CurrentUser = Depends(get_current_user),
):
    """List failure log entries with optional device/type filters."""
    db = request.app.state.db
    conditions = []
    params = []

    if device:
        conditions.append("device = ?")
        params.append(device)
    if failure_type:
        conditions.append("failure_type = ?")
        params.append(failure_type)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(lines)

    async with db.execute(
        f"SELECT * FROM failure_log {where} ORDER BY occurred_at DESC LIMIT ?", params
    ) as cursor:
        rows = await cursor.fetchall()

    entries = []
    for row in rows:
        entries.append({
            "id": row["id"],
            "timestamp": row["timestamp"],
            "occurred_at": row["occurred_at"],
            "device": row["device"],
            "equipment": row["equipment"],
            "failure_type": row["failure_type"],
            "severity": row["severity"],
            "description": row["description"],
            "resolved_at": row["resolved_at"],
            "reported_by": row["reported_by"],
            "tags_snapshot": json.loads(row["tags_snapshot"]) if row["tags_snapshot"] else None,
        })
    return {"entries": entries}


@router.post("/api/failures", status_code=201)
async def failure_log_create(request: Request, body: dict, user: CurrentUser = Depends(require_operator)):
    """Report a new equipment failure."""
    db = request.app.state.db

    occurred_at = (body.get("occurred_at") or "").strip()
    device = (body.get("device") or "").strip()
    failure_type = (body.get("failure_type") or "").strip()
    if not occurred_at or not device or not failure_type:
        raise HTTPException(status_code=400, detail="occurred_at, device, and failure_type are required")

    # Validate failure_type exists in catalog
    async with db.execute("SELECT id FROM failure_catalog WHERE name = ?", (failure_type,)) as c:
        if not await c.fetchone():
            raise HTTPException(status_code=400, detail=f"Unknown failure_type '{failure_type}'. Add it to the catalog first.")

    equipment = (body.get("equipment") or "").strip()
    severity = (body.get("severity") or "major").strip().lower()
    if severity not in ("minor", "major", "critical"):
        severity = "major"
    description = (body.get("description") or "").strip()[:5000]
    resolved_at = (body.get("resolved_at") or "").strip() or None
    tags_snapshot = json.dumps(body.get("tags_snapshot")) if body.get("tags_snapshot") else None

    async with db.execute(
        """INSERT INTO failure_log
           (occurred_at, device, equipment, failure_type, severity, description, resolved_at, reported_by, tags_snapshot)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (occurred_at, device, equipment, failure_type, severity, description, resolved_at, user.username, tags_snapshot)
    ) as cursor:
        row_id = cursor.lastrowid
    await db.commit()

    return {"id": row_id, "occurred_at": occurred_at, "device": device, "failure_type": failure_type, "severity": severity}


@router.put("/api/failures/{failure_id}")
async def failure_log_update(failure_id: int, request: Request, body: dict, user: CurrentUser = Depends(require_operator)):
    """Update a failure log entry (e.g., add resolved_at timestamp)."""
    db = request.app.state.db
    async with db.execute("SELECT * FROM failure_log WHERE id = ?", (failure_id,)) as c:
        existing = await c.fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Failure entry not found")

    resolved_at = body.get("resolved_at", existing["resolved_at"])
    severity = body.get("severity", existing["severity"])
    description = body.get("description", existing["description"])
    equipment = body.get("equipment", existing["equipment"])

    await db.execute(
        """UPDATE failure_log SET resolved_at = ?, severity = ?, description = ?, equipment = ?
           WHERE id = ?""",
        (resolved_at, severity, description, equipment, failure_id)
    )
    await db.commit()
    return {"ok": True, "id": failure_id}


@router.delete("/api/failures/{failure_id}")
async def failure_log_delete(failure_id: int, request: Request, user: CurrentUser = Depends(require_admin)):
    """Delete a failure log entry (admin only)."""
    db = request.app.state.db
    async with db.execute("SELECT * FROM failure_log WHERE id = ?", (failure_id,)) as c:
        existing = await c.fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Failure entry not found")

    await db.execute("DELETE FROM failure_log WHERE id = ?", (failure_id,))
    await db.commit()
    return {"ok": True, "deleted": failure_id}
