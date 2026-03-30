"""
OEE (Overall Equipment Effectiveness) routes for PLC4X Manager FastAPI.

Endpoints:
  GET /api/devices/{device_name}/oee-config  — get OEE config (plant-filtered)
  PUT /api/devices/{device_name}/oee-config  — set OEE config (@require_admin)
  GET /api/oee/calculate                     — calculate OEE metrics from InfluxDB
  GET /api/oee/trend                         — hourly/daily availability trend
"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import CurrentUser, get_current_user, require_admin
from config_manager import CONFIG_LOCK, find_device, load_config, save_config
from influx import _influx_query, _safe_flux_str

router = APIRouter(tags=["oee"])


# =============================================
# Routes
# =============================================

@router.get("/api/devices/{device_name}/oee-config")
async def api_get_oee_config(device_name: str, user: CurrentUser = Depends(get_current_user)):
    """Returns OEE configuration for a device."""
    config = load_config()

    # Plant filter check
    if user.plants:
        _, device = find_device(config, device_name)
        if device is not None and device.get("plant") not in user.plants:
            raise HTTPException(status_code=403, detail="Access denied")

    _, device = find_device(config, device_name)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{device_name}' not found")
    return device.get("oeeConfig", {})


@router.put("/api/devices/{device_name}/oee-config")
async def api_set_oee_config(
    device_name: str,
    body: dict,
    user: CurrentUser = Depends(require_admin),
):
    """Sets OEE configuration for a device."""
    if body is None:
        raise HTTPException(status_code=400, detail="Request body is required")

    # Validate body before entering the lock
    oee: dict = {}
    oee["enabled"] = bool(body.get("enabled", True))

    for field in ("runningTag", "productionCountTag", "rejectCountTag"):
        val = body.get(field)
        if val:
            try:
                _safe_flux_str(val)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Tag alias '{val}' contains invalid characters")
            oee[field] = val

    if not oee.get("runningTag"):
        raise HTTPException(status_code=400, detail="'runningTag' is required")
    if not oee.get("productionCountTag"):
        raise HTTPException(status_code=400, detail="'productionCountTag' is required")

    if "runningValue" in body and body["runningValue"] is not None:
        rv = body["runningValue"]
        if not isinstance(rv, (str, int, float, bool)):
            raise HTTPException(status_code=400, detail="'runningValue' must be a string, number, or boolean")
        oee["runningValue"] = rv

    ict = body.get("idealCycleTime")
    if ict is not None:
        try:
            oee["idealCycleTime"] = float(ict)
            if oee["idealCycleTime"] <= 0:
                raise HTTPException(status_code=400, detail="'idealCycleTime' must be > 0")
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid idealCycleTime")
    else:
        raise HTTPException(status_code=400, detail="'idealCycleTime' is required")

    phd = body.get("plannedHoursPerDay")
    if phd is not None:
        try:
            oee["plannedHoursPerDay"] = float(phd)
            if oee["plannedHoursPerDay"] <= 0 or oee["plannedHoursPerDay"] > 24:
                raise HTTPException(status_code=400, detail="'plannedHoursPerDay' must be between 0 and 24")
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid plannedHoursPerDay")
    else:
        raise HTTPException(status_code=400, detail="'plannedHoursPerDay' is required")

    def _save_oee():
        with CONFIG_LOCK:
            config = load_config()
            idx, device = find_device(config, device_name)
            if device is None:
                raise LookupError(f"Device '{device_name}' not found")
            # Validate tag aliases exist in device
            tag_aliases = [t["alias"] for t in device.get("tags", [])]
            calc_aliases = [t["alias"] for t in device.get("calculatedTags", [])]
            all_aliases = tag_aliases + calc_aliases
            for field in ("runningTag", "productionCountTag", "rejectCountTag"):
                val = oee.get(field)
                if val and val not in all_aliases:
                    raise ValueError(f"Tag '{val}' not found in device '{device_name}'")
            device["oeeConfig"] = oee
            save_config(config)

    try:
        await asyncio.to_thread(_save_oee)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"message": "OEE config saved", "oeeConfig": oee}


@router.get("/api/oee/calculate")
async def api_oee_calculate(
    device: str = Query(..., description="Device name"),
    hours: int = Query(default=24, ge=1, le=8760),
    user: CurrentUser = Depends(get_current_user),
):
    """Calculates OEE for a device over a time range."""
    try:
        _safe_flux_str(device)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    config = load_config()
    _, dev = find_device(config, device)
    if dev is None:
        raise HTTPException(status_code=404, detail=f"Device '{device}' not found")

    # Plant filter
    if user.plants and dev.get("plant") not in user.plants:
        raise HTTPException(status_code=403, detail="Access denied")

    oee_cfg = dev.get("oeeConfig", {})
    if not oee_cfg.get("enabled"):
        raise HTTPException(status_code=400, detail=f"OEE not configured for device '{device}'")

    running_tag = oee_cfg.get("runningTag")
    count_tag = oee_cfg.get("productionCountTag")
    reject_tag = oee_cfg.get("rejectCountTag")
    ideal_cycle = oee_cfg.get("idealCycleTime", 1)
    planned_hours_day = oee_cfg.get("plannedHoursPerDay", 8)
    running_value = oee_cfg.get("runningValue")

    bucket = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")

    def _query_tag_sync(alias):
        flux = f'''
        from(bucket: "{bucket}")
          |> range(start: -{hours}h)
          |> filter(fn: (r) => r._measurement == "plc4x_tags")
          |> filter(fn: (r) => r.device == "{device}")
          |> filter(fn: (r) => r.alias == "{_safe_flux_str(alias)}")
          |> filter(fn: (r) => r._field == "value")
          |> sort(columns: ["_time"])
        '''
        return _influx_query(flux)

    async def query_tag(alias):
        return await asyncio.to_thread(_query_tag_sync, alias)

    try:
        # --- Availability ---
        running_records = await query_tag(running_tag) if running_tag else []
        run_time_s = 0
        if running_records:
            for i in range(len(running_records) - 1):
                val = running_records[i].get_value()
                is_running = (val == running_value) if running_value is not None else bool(val)
                if is_running:
                    t1 = running_records[i].get_time()
                    t2 = running_records[i + 1].get_time()
                    delta = (t2 - t1).total_seconds()
                    if delta < 300:  # max 5 min gap between samples
                        run_time_s += delta

        planned_time_s = planned_hours_day * 3600 * (hours / 24)
        availability = min(run_time_s / planned_time_s, 1.0) if planned_time_s > 0 else 0

        # --- Performance ---
        count_records = await query_tag(count_tag) if count_tag else []
        total_count = 0
        if count_records:
            first_val = count_records[0].get_value() or 0
            last_val = count_records[-1].get_value() or 0
            total_count = last_val - first_val
            if total_count < 0:
                total_count = last_val  # counter rollover

        theoretical_max = run_time_s / ideal_cycle if ideal_cycle > 0 else 0
        performance = min(total_count / theoretical_max, 1.0) if theoretical_max > 0 else 0

        # --- Quality ---
        reject_count = 0
        if reject_tag:
            reject_records = await query_tag(reject_tag)
            if reject_records:
                first_val = reject_records[0].get_value() or 0
                last_val = reject_records[-1].get_value() or 0
                reject_count = last_val - first_val
                if reject_count < 0:
                    reject_count = last_val

        good_count = max(total_count - reject_count, 0)
        quality = good_count / total_count if total_count > 0 else 1.0

        oee_value = availability * performance * quality

        return {
            "oee": round(oee_value, 4),
            "availability": round(availability, 4),
            "performance": round(performance, 4),
            "quality": round(quality, 4),
            "details": {
                "plannedTime_h": round(planned_time_s / 3600, 2),
                "runTime_h": round(run_time_s / 3600, 2),
                "downTime_h": round(max(planned_time_s - run_time_s, 0) / 3600, 2),
                "totalCount": int(round(total_count)),
                "goodCount": int(round(good_count)),
                "rejectCount": int(round(reject_count)),
                "idealCycleTime_s": ideal_cycle,
                "theoreticalMax": int(round(theoretical_max)),
                "samples": len(running_records)
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OEE calculation failed: {e}")


@router.get("/api/oee/trend")
async def api_oee_trend(
    device: str = Query(..., description="Device name"),
    hours: int = Query(default=24, ge=1, le=8760),
    user: CurrentUser = Depends(get_current_user),
):
    """Returns OEE trend data in hourly or daily buckets."""
    try:
        _safe_flux_str(device)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    config = load_config()
    _, dev = find_device(config, device)
    if dev is None:
        raise HTTPException(status_code=404, detail=f"Device '{device}' not found")

    # Plant filter
    if user.plants and dev.get("plant") not in user.plants:
        raise HTTPException(status_code=403, detail="Access denied")

    oee_cfg = dev.get("oeeConfig", {})
    if not oee_cfg.get("enabled"):
        raise HTTPException(status_code=400, detail="OEE not configured")

    running_tag = oee_cfg.get("runningTag")

    # Determine bucket interval
    if hours <= 24:
        interval_h = 1
    elif hours <= 168:
        interval_h = 4
    else:
        interval_h = 24

    bucket = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")

    try:
        flux = f'''
        from(bucket: "{bucket}")
          |> range(start: -{hours}h)
          |> filter(fn: (r) => r._measurement == "plc4x_tags")
          |> filter(fn: (r) => r.device == "{device}")
          |> filter(fn: (r) => r.alias == "{_safe_flux_str(running_tag)}")
          |> filter(fn: (r) => r._field == "value")
          |> aggregateWindow(every: {interval_h}h, fn: mean, createEmpty: true)
          |> sort(columns: ["_time"])
        '''
        records = await asyncio.to_thread(_influx_query, flux)

        trend = []
        for r in records:
            val = r.get_value()
            if val is None:
                val = 0
            avail = max(0, min(float(val), 1.0))
            trend.append({
                "time": r.get_time().isoformat() if r.get_time() else "",
                "availability": round(avail, 4)
            })

        return {"trend": trend, "interval_h": interval_h}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OEE trend query failed: {e}")
