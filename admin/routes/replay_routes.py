"""
HMI Replay / Time Travel routes for PLC4X Manager FastAPI.

Endpoints:
  GET /api/replay/snapshot  — all tag values for a device at a specific timestamp
  GET /api/replay/range     — series of snapshots for DVR playback
"""

from __future__ import annotations

import asyncio
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import CurrentUser, get_current_user
from config_manager import load_config
from influx import _influx_query, _safe_flux_str

router = APIRouter(tags=["replay"])

_ISO8601_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$'
)

_ALLOWED_STEPS = {"5s", "10s", "30s", "1m", "5m", "15m", "30m", "1h", "6h", "1d"}

_RAW_RETENTION_HOURS = 2160


def _validate_timestamp(ts: str) -> str:
    if not ts or not _ISO8601_RE.match(ts):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ISO 8601 timestamp: {ts!r}. Expected format: 2026-03-30T14:30:00Z"
        )
    return ts


def _enforce_plant_access(device: str, user: CurrentUser) -> None:
    config = load_config()
    for d in config.get("devices", []):
        if d["name"] == device:
            if user.plants and d.get("plant") not in user.plants:
                raise HTTPException(status_code=403, detail="Access denied")
            return


def _select_bucket_for_age(hours_ago: float) -> str:
    if hours_ago <= _RAW_RETENTION_HOURS:
        return os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")
    return "plc4x_hourly"


@router.get("/api/replay/snapshot")
async def api_replay_snapshot(
    device: str = Query(..., description="Device name"),
    timestamp: str = Query(..., description="ISO 8601 timestamp, e.g. 2026-03-30T14:30:00Z"),
    user: CurrentUser = Depends(get_current_user),
):
    """Return all tag values for a device at a specific historical timestamp."""
    try:
        _safe_flux_str(device)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _validate_timestamp(timestamp)
    _enforce_plant_access(device, user)

    from datetime import datetime, timezone
    requested = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    hours_ago = (now - requested).total_seconds() / 3600
    bucket = _select_bucket_for_age(hours_ago)

    flux = f'''
import "experimental"

from(bucket: "{bucket}")
  |> range(start: experimental.subDuration(d: 5s, from: {timestamp}),
           stop:  experimental.addDuration(d: 5s, to:  {timestamp}))
  |> filter(fn: (r) => r._measurement == "plc4x_tags")
  |> filter(fn: (r) => r.device == "{device}")
  |> filter(fn: (r) => r._field == "value")
  |> group(columns: ["alias"])
  |> last()
'''
    try:
        records = await asyncio.to_thread(_influx_query, flux)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"InfluxDB query failed: {e}")

    tags = []
    actual_timestamp = None
    for r in records:
        alias = r.values.get("alias", "")
        value = r.get_value()
        ts = r.get_time()
        if ts and actual_timestamp is None:
            actual_timestamp = ts.isoformat().replace("+00:00", "Z")
        tags.append({"alias": alias, "value": value})

    return {
        "device": device,
        "timestamp": timestamp,
        "actual_timestamp": actual_timestamp,
        "tags": tags,
        "source_bucket": bucket,
    }


@router.get("/api/replay/range")
async def api_replay_range(
    device: str = Query(..., description="Device name"),
    start: str = Query(..., description="ISO 8601 start timestamp"),
    end: str = Query(..., description="ISO 8601 end timestamp"),
    step: str = Query(default="30s", description="Aggregation step (5s, 10s, 30s, 1m, 5m, 15m, 30m, 1h, 6h, 1d)"),
    user: CurrentUser = Depends(get_current_user),
):
    """Return a series of tag-value snapshots for DVR-style playback. Max 720 frames."""
    try:
        _safe_flux_str(device)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _validate_timestamp(start)
    _validate_timestamp(end)

    if step not in _ALLOWED_STEPS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid step: {step!r}. Allowed: {sorted(_ALLOWED_STEPS)}"
        )

    _enforce_plant_access(device, user)

    from datetime import datetime, timezone
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)

    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="end must be after start")

    hours_ago = (now - start_dt).total_seconds() / 3600
    duration_hours = (end_dt - start_dt).total_seconds() / 3600

    if hours_ago > _RAW_RETENTION_HOURS:
        bucket = "plc4x_hourly"
    elif duration_hours <= 6:
        bucket = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")
    elif duration_hours <= 168:
        bucket = "plc4x_hourly"
    else:
        bucket = "plc4x_daily" if duration_hours > 720 else "plc4x_hourly"

    flux = f'''
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {end})
  |> filter(fn: (r) => r._measurement == "plc4x_tags")
  |> filter(fn: (r) => r.device == "{device}")
  |> filter(fn: (r) => r._field == "value")
  |> aggregateWindow(every: {step}, fn: last, createEmpty: false)
  |> group(columns: ["_time"])
  |> sort(columns: ["alias"])
  |> limit(n: 720)
'''
    try:
        records = await asyncio.to_thread(_influx_query, flux)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"InfluxDB query failed: {e}")

    frame_map = {}
    for r in records:
        alias = r.values.get("alias", "")
        value = r.get_value()
        ts = r.get_time()
        if ts is None:
            continue
        ts_str = ts.isoformat().replace("+00:00", "Z")
        if ts_str not in frame_map:
            frame_map[ts_str] = []
        frame_map[ts_str].append({"alias": alias, "value": value})

    sorted_ts = sorted(frame_map.keys())[:720]
    frames = [{"timestamp": ts, "tags": frame_map[ts]} for ts in sorted_ts]

    return {
        "device": device,
        "start": start,
        "end": end,
        "step": step,
        "frames": frames,
        "frame_count": len(frames),
        "source_bucket": bucket,
    }
