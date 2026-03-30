"""
Audit trail routes for PLC4X Manager FastAPI.

Endpoints:
  GET /api/audit  — audit trail with action filter + lines param (@require_operator)
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, Request

from auth import CurrentUser, require_operator

router = APIRouter(tags=["audit"])


# =============================================
# Routes
# =============================================

@router.get("/api/audit")
async def api_audit_trail(
    request: Request,
    lines: int = Query(default=200, ge=1, le=5000),
    action: str | None = Query(default=None),
    user: CurrentUser = Depends(require_operator),
):
    """Returns audit trail entries from SQLite. Newest first, with optional action filter."""
    db = request.app.state.db

    try:
        if action:
            # Support prefix match: "POST" matches "POST /api/...", exact match also works
            async with db.execute(
                "SELECT * FROM audit_entries WHERE action = ? OR action LIKE ? ORDER BY id DESC LIMIT ?",
                (action, f"{action} %", lines)
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM audit_entries ORDER BY id DESC LIMIT ?", (lines,)
            ) as cursor:
                rows = await cursor.fetchall()

        # Count total (unfiltered) for pagination metadata
        async with db.execute("SELECT COUNT(*) FROM audit_entries") as c:
            total = (await c.fetchone())[0]

        entries = []
        for row in rows:
            entry = {
                "timestamp": row["timestamp"],
                "user": row["user"],
                "action": row["action"],
                "ip": row["ip"],
            }
            # Parse details JSON blob back to dict
            try:
                details = json.loads(row["details"] or "{}")
                if details:
                    entry["details"] = details
            except (json.JSONDecodeError, TypeError):
                pass
            entries.append(entry)

        return {"entries": entries, "total": total}

    except Exception:
        # Fallback to JSONL if DB not available
        from audit import AUDIT_TRAIL_PATH
        try:
            with open(AUDIT_TRAIL_PATH, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
        except FileNotFoundError:
            return {"entries": [], "total": 0}

        entries = []
        for line in reversed(all_lines):
            try:
                entry = json.loads(line.strip())
                if action:
                    entry_action = entry.get("action", "")
                    if entry_action != action and not entry_action.startswith(action + " "):
                        continue
                entries.append(entry)
                if len(entries) >= lines:
                    break
            except Exception:
                continue

        return {"entries": entries, "total": len(all_lines)}
