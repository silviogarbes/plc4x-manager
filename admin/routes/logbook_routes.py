"""
Shift logbook routes for PLC4X Manager FastAPI.

Endpoints:
  GET  /api/logbook  — read entries with shift filter + lines param
  POST /api/logbook  — add entry (@require_operator)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import CurrentUser, get_current_user, require_operator

router = APIRouter(tags=["logbook"])


# =============================================
# Routes
# =============================================

@router.get("/api/logbook")
async def logbook_get(
    request: Request,
    lines: int = Query(default=100, ge=1, le=1000),
    shift: str = Query(default=""),
    user: CurrentUser = Depends(get_current_user),
):
    """Read logbook entries from SQLite with optional shift filter."""
    db = request.app.state.db
    shift_filter = shift.strip().lower()

    try:
        if shift_filter:
            async with db.execute(
                "SELECT * FROM logbook_entries WHERE LOWER(shift) = ? ORDER BY id DESC LIMIT ?",
                (shift_filter, lines)
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM logbook_entries ORDER BY id DESC LIMIT ?", (lines,)
            ) as cursor:
                rows = await cursor.fetchall()

        entries = []
        for row in rows:
            entries.append({
                "id": str(row["id"]),
                "timestamp": row["timestamp"],
                "user": row["user"],
                "shift": row["shift"],
                "category": row["category"],
                "priority": row["priority"],
                "message": row["message"],
            })

        return {"entries": entries}

    except Exception:
        # Fallback to JSONL if DB not available
        from audit import LOGBOOK_LOCK, LOGBOOK_PATH
        entries = []
        with LOGBOOK_LOCK:
            if os.path.exists(LOGBOOK_PATH):
                with open(LOGBOOK_PATH, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if shift_filter and entry.get("shift", "").lower() != shift_filter:
                                continue
                            entries.append(entry)
                        except Exception:
                            pass
        entries.reverse()
        return {"entries": entries[:lines]}


@router.post("/api/logbook", status_code=201)
async def logbook_post(
    request: Request,
    body: dict,
    user: CurrentUser = Depends(require_operator),
):
    """Add a logbook entry to SQLite."""
    body = body or {}
    message = (body.get("message") or "").strip()[:5000]
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    shift = (body.get("shift") or "").strip().lower()
    category = (body.get("category") or "observation").strip().lower()
    priority = (body.get("priority") or "normal").strip().lower()

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    db = request.app.state.db

    try:
        async with db.execute(
            "INSERT INTO logbook_entries (timestamp, user, shift, category, priority, message) VALUES (?, ?, ?, ?, ?, ?)",
            (timestamp, user.username, shift, category, priority, message)
        ) as cursor:
            row_id = cursor.lastrowid
        await db.commit()

        return {
            "id": str(row_id),
            "timestamp": timestamp,
            "user": user.username,
            "shift": shift,
            "category": category,
            "priority": priority,
            "message": message,
        }

    except Exception:
        # Fallback to JSONL if DB not available
        import random
        from audit import LOGBOOK_LOCK, LOGBOOK_MAX_DAYS, LOGBOOK_MAX_LINES, LOGBOOK_PATH, _trim_jsonl_file

        entry = {
            "id": now.strftime("%Y%m%dT%H%M%S%f"),
            "timestamp": timestamp,
            "user": user.username,
            "shift": shift,
            "category": category,
            "priority": priority,
            "message": message,
        }

        with LOGBOOK_LOCK:
            with open(LOGBOOK_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

        if random.random() < 0.01:
            _trim_jsonl_file(LOGBOOK_PATH, LOGBOOK_LOCK, LOGBOOK_MAX_LINES, LOGBOOK_MAX_DAYS)

        return entry
