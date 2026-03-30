"""
Alarm management routes for PLC4X Manager FastAPI.

Endpoints:
  GET  /api/alarms                       — active + history (plant-filtered)
  POST /api/alarms/acknowledge            — ack single alarm (@require_operator)
  POST /api/alarms/acknowledge-all        — ack all alarms (@require_operator)
"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import CurrentUser, get_current_user, require_operator

router = APIRouter(tags=["alarms"])


# =============================================
# Helper: row -> dict with backward-compat keys
# =============================================

def _active_alarm_row_to_dict(row) -> dict:
    """Convert an alarms table row to the API dict format.

    Maps condition_type -> condition to maintain frontend backward compatibility.
    """
    return {
        "key": row["key"],
        "device": row["device"],
        "tag": row["tag"],
        "plant": row["plant"],
        "severity": row["severity"],
        "condition": row["condition_type"],  # backward compat: DB stores condition_type
        "value": row["value"],
        "threshold": row["threshold"],
        "message": row["message"] or "",
        "timestamp": row["timestamp"],
        "acknowledged": bool(row["acknowledged"]),
        "ack_user": row["ack_user"],
        "ack_time": row["ack_time"],
    }


def _history_row_to_dict(row) -> dict:
    """Convert an alarm_history table row to the API dict format.

    Maps start_time -> timestamp for backward compat with frontend.
    """
    return {
        "key": row["key"],
        "device": row["device"],
        "tag": row["tag"],
        "plant": row["plant"],
        "severity": row["severity"],
        "condition": row["condition_type"],  # backward compat
        "value": row["value"],
        "threshold": row["threshold"],
        "message": row["message"] or "",
        "timestamp": row["start_time"],  # backward compat: frontend expects "timestamp"
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "acknowledged": bool(row["acknowledged"]),
        "ack_user": row["ack_user"],
        "duration_s": row["duration_s"],
    }


# =============================================
# Routes
# =============================================

@router.get("/api/alarms")
async def api_alarms(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    """Returns active alarms and recent history (filtered by user's plants)."""
    db = request.app.state.db
    allowed = user.plants

    try:
        # Active alarms
        async with db.execute("SELECT * FROM alarms ORDER BY timestamp DESC") as cursor:
            active_rows = await cursor.fetchall()

        # Alarm history (last 500 entries)
        async with db.execute(
            "SELECT * FROM alarm_history ORDER BY start_time DESC LIMIT 500"
        ) as cursor:
            history_rows = await cursor.fetchall()

        active = {}
        for row in active_rows:
            alarm = _active_alarm_row_to_dict(row)
            if allowed and alarm["plant"] not in allowed:
                continue
            active[alarm["key"]] = alarm

        history = []
        for row in history_rows:
            alarm = _history_row_to_dict(row)
            if allowed and alarm["plant"] not in allowed:
                continue
            history.append(alarm)

        return {"active": active, "history": history}

    except Exception:
        # Fallback to JSON file if DB not available
        from poller import get_alarms
        data = get_alarms()
        if allowed:
            data["active"] = {k: v for k, v in data.get("active", {}).items() if v.get("plant") in allowed}
            data["history"] = [h for h in data.get("history", []) if h.get("plant") in allowed]
        return data


@router.post("/api/alarms/acknowledge")
async def api_alarm_acknowledge(
    request: Request,
    body: dict,
    user: CurrentUser = Depends(require_operator),
):
    """Acknowledges an active alarm (silences the sound). Body: {key: "device/tag"}"""
    alarm_key = body.get("key") if body else None
    if not alarm_key:
        raise HTTPException(status_code=400, detail="Field 'key' is required")

    db = request.app.state.db

    try:
        async with db.execute("SELECT * FROM alarms WHERE key = ?", (alarm_key,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail=f"Alarm '{alarm_key}' not found")

        # Plant filter
        if user.plants and row["plant"] not in user.plants:
            raise HTTPException(status_code=403, detail="Access denied")

        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        await db.execute(
            "UPDATE alarms SET acknowledged = 1, ack_user = ?, ack_time = ? WHERE key = ?",
            (user.username, now, alarm_key)
        )
        await db.commit()

        return {"message": f"Alarm '{alarm_key}' acknowledged"}

    except HTTPException:
        raise
    except Exception:
        # Fallback to JSON file
        from poller import _alarm_lock, ALARM_PATH

        with _alarm_lock:
            try:
                with open(ALARM_PATH, "r", encoding="utf-8") as f:
                    disk_alarms = json.load(f)
            except Exception:
                disk_alarms = {"active": {}, "history": []}

            alarm = disk_alarms.get("active", {}).get(alarm_key)
            if not alarm:
                raise HTTPException(status_code=404, detail=f"Alarm '{alarm_key}' not found")

            if user.plants and alarm.get("plant") not in user.plants:
                raise HTTPException(status_code=403, detail="Access denied")

            alarm["acknowledged"] = True

            tmp = ALARM_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(disk_alarms, f)
            os.replace(tmp, ALARM_PATH)

        return {"message": f"Alarm '{alarm_key}' acknowledged"}


@router.post("/api/alarms/acknowledge-all")
async def api_alarm_acknowledge_all(
    request: Request,
    user: CurrentUser = Depends(require_operator),
):
    """Acknowledges all active alarms (filtered by user's plants)."""
    db = request.app.state.db

    try:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()

        if user.plants:
            # Only ack alarms in the user's allowed plants
            placeholders = ",".join("?" * len(user.plants))
            async with db.execute(
                f"SELECT COUNT(*) FROM alarms WHERE plant IN ({placeholders}) AND acknowledged = 0",
                list(user.plants)
            ) as cursor:
                count = (await cursor.fetchone())[0]

            await db.execute(
                f"UPDATE alarms SET acknowledged = 1, ack_user = ?, ack_time = ? "
                f"WHERE plant IN ({placeholders}) AND acknowledged = 0",
                [user.username, now] + list(user.plants)
            )
        else:
            async with db.execute(
                "SELECT COUNT(*) FROM alarms WHERE acknowledged = 0"
            ) as cursor:
                count = (await cursor.fetchone())[0]

            await db.execute(
                "UPDATE alarms SET acknowledged = 1, ack_user = ?, ack_time = ? WHERE acknowledged = 0",
                (user.username, now)
            )
        await db.commit()

        return {"message": f"{count} alarm(s) acknowledged"}

    except Exception:
        # Fallback to JSON file
        from poller import _alarm_lock, ALARM_PATH

        with _alarm_lock:
            try:
                with open(ALARM_PATH, "r", encoding="utf-8") as f:
                    disk_alarms = json.load(f)
            except Exception:
                disk_alarms = {"active": {}, "history": []}

            count = 0
            for alarm in disk_alarms.get("active", {}).values():
                if user.plants and alarm.get("plant") not in user.plants:
                    continue
                if not alarm.get("acknowledged"):
                    alarm["acknowledged"] = True
                    count += 1

            tmp = ALARM_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(disk_alarms, f)
            os.replace(tmp, ALARM_PATH)

        return {"message": f"{count} alarm(s) acknowledged"}
