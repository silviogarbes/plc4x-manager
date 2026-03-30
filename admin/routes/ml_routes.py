"""ML insights API: status, alerts, results, configuration.

Endpoints:
  GET  /api/ml/status         — Engine online/offline, last run, module toggles
  GET  /api/ml/alerts         — Recent ML alerts from InfluxDB (sorted by time desc)
  GET  /api/ml/results        — All plc4x_ml data for a device, grouped by type
  GET  /api/ml/correlation    — Correlation matrix for a device
  GET  /api/ml/config         — Read mlConfig from config.yml
  PUT  /api/ml/config         — Update mlConfig in config.yml (@require_admin)
  POST /api/ml/run-now        — Write trigger file for immediate ML cycle (@require_admin)
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import CurrentUser, get_current_user, require_admin
from config_manager import load_config, save_config
from influx import _get_influx_client, _influx_query, _safe_flux_str

router = APIRouter(tags=["ml"])

# Path for the ML trigger file (picked up by predictor.py)
_ML_TRIGGER_FILE = os.environ.get(
    "ML_TRIGGER_FILE",
    os.path.join(
        os.path.dirname(os.environ.get("CONFIG_PATH", "/app/config/config.yml")),
        ".ml-trigger"
    )
)

# Path for the ML status JSON written by predictor.py after each cycle
_ML_STATUS_FILE = os.path.join(
    os.path.dirname(os.environ.get("CONFIG_PATH", "/app/config/config.yml")),
    ".ml-status.json"
)

# Default module config mirrors predictor.py defaults
_DEFAULT_ML_CONFIG = {
    "cycleIntervalMinutes": 5,
    "forecastHours": 2,
    "minPoints": 100,
    "anomaly": {"enabled": True, "contamination": 0.05, "minAgreement": 2},
    "explainability": {"enabled": True, "topContributors": 5},
    "correlation": {"enabled": True, "baselineHours": 6, "recentMinutes": 30, "breakThreshold": 0.4},
    "changepoint": {"enabled": True, "minSegmentSize": 60, "penalty": 10.0},
    "pattern": {"enabled": True, "windowSize": 60, "topK": 3},
}

_INFLUX_BUCKET = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")


# =============================================
# Helpers
# =============================================

def _read_ml_status() -> dict:
    """Read .ml-status.json — returns empty dict if not found."""
    try:
        with open(_ML_STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _get_ml_config(config: dict) -> dict:
    """Extract mlConfig from the full config, merged with defaults."""
    ml = dict(_DEFAULT_ML_CONFIG)
    ml.update(config.get("mlConfig", {}))
    # Ensure nested dicts are also merged with defaults
    for key in ("anomaly", "explainability", "correlation", "changepoint", "pattern"):
        if key in _DEFAULT_ML_CONFIG:
            merged = dict(_DEFAULT_ML_CONFIG[key])
            merged.update(config.get("mlConfig", {}).get(key, {}))
            ml[key] = merged
    return ml


# =============================================
# GET /api/ml/status
# =============================================

@router.get("/api/ml/status")
async def api_ml_status(user: CurrentUser = Depends(get_current_user)):
    """Return ML engine status: online/offline, last run, module toggles."""
    status = _read_ml_status()
    config = load_config()
    ml_cfg = _get_ml_config(config)

    # Engine is considered online if last_run was within 3x cycle interval
    cycle_minutes = ml_cfg.get("cycleIntervalMinutes", 5)
    last_run_ts = status.get("last_run")
    engine_online = False
    if last_run_ts:
        try:
            from datetime import datetime, timezone
            if isinstance(last_run_ts, str):
                last_dt = datetime.fromisoformat(last_run_ts.replace("Z", "+00:00"))
            else:
                last_dt = datetime.fromtimestamp(last_run_ts, tz=timezone.utc)
            age_minutes = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
            engine_online = age_minutes < (cycle_minutes * 3 + 10)
        except Exception:
            pass

    modules = {
        "anomaly": ml_cfg.get("anomaly", {}).get("enabled", True),
        "explainability": ml_cfg.get("explainability", {}).get("enabled", True),
        "correlation": ml_cfg.get("correlation", {}).get("enabled", True),
        "changepoint": ml_cfg.get("changepoint", {}).get("enabled", True),
        "pattern": ml_cfg.get("pattern", {}).get("enabled", True),
    }

    return {
        "engine_online": engine_online,
        "last_run": last_run_ts,
        "tags_analyzed": status.get("tags_analyzed", 0),
        "tags_skipped": status.get("tags_skipped", 0),
        "errors": status.get("errors", 0),
        "cycle_duration_s": status.get("cycle_duration_s", 0),
        "cycle_interval_minutes": cycle_minutes,
        "forecast_hours": ml_cfg.get("forecastHours", 2),
        "min_points": ml_cfg.get("minPoints", 100),
        "modules": modules,
    }


# =============================================
# GET /api/ml/alerts
# =============================================

@router.get("/api/ml/alerts")
async def api_ml_alerts(
    hours: int = Query(default=24, ge=1, le=168),
    user: CurrentUser = Depends(get_current_user),
):
    """Return recent ML alerts sorted by time desc (max 50)."""

    def _query_alerts():
        alerts = []

        # 1) Anomaly ensemble alerts
        flux_anomaly = f'''
from(bucket: "{_INFLUX_BUCKET}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "plc4x_ml")
  |> filter(fn: (r) => r.analysis == "anomaly_ensemble")
  |> filter(fn: (r) => r._field == "is_anomaly")
  |> filter(fn: (r) => r._value == 1.0)
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 25)
'''
        try:
            records = _influx_query(flux_anomaly)
            for r in records:
                alerts.append({
                    "type": "anomaly",
                    "time": r.get_time().isoformat() if r.get_time() else None,
                    "device": r.values.get("device", ""),
                    "tag": r.values.get("alias", ""),
                    "plant": r.values.get("plant", ""),
                    "confidence": None,
                    "agreeing": None,
                    "shap_contributions": None,
                    "severity": "critical",
                })
        except Exception:
            pass

        # 2) Broken correlation alerts
        flux_corr = f'''
from(bucket: "{_INFLUX_BUCKET}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "plc4x_ml")
  |> filter(fn: (r) => r.analysis == "corr_broken")
  |> filter(fn: (r) => r._field == "change")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 15)
'''
        try:
            records = _influx_query(flux_corr)
            for r in records:
                alerts.append({
                    "type": "corr_broken",
                    "time": r.get_time().isoformat() if r.get_time() else None,
                    "device": r.values.get("device", ""),
                    "tag": r.values.get("tag1", ""),
                    "tag_b": r.values.get("tag2", ""),
                    "plant": r.values.get("plant", ""),
                    "baseline_corr": r.values.get("baseline_corr", None),
                    "recent_corr": r.values.get("recent_corr", None),
                    "delta": r.get_value(),
                    "severity": "warning",
                })
        except Exception:
            pass

        # 3) Change point alerts with severity > 0.5
        flux_cp = f'''
from(bucket: "{_INFLUX_BUCKET}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "plc4x_ml")
  |> filter(fn: (r) => r.analysis == "change_point")
  |> filter(fn: (r) => r._field == "severity_score")
  |> filter(fn: (r) => r._value > 0.5)
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 15)
'''
        try:
            records = _influx_query(flux_cp)
            for r in records:
                alerts.append({
                    "type": "change_point",
                    "time": r.get_time().isoformat() if r.get_time() else None,
                    "device": r.values.get("device", ""),
                    "tag": r.values.get("alias", ""),
                    "plant": r.values.get("plant", ""),
                    "mean_before": r.values.get("mean_before", None),
                    "mean_after": r.values.get("mean_after", None),
                    "severity": r.get_value(),
                })
        except Exception:
            pass

        # Sort all by time desc, limit to 50
        alerts.sort(key=lambda a: a.get("time") or "", reverse=True)
        return alerts[:50]

    try:
        result = await asyncio.to_thread(_query_alerts)
        return {"alerts": result, "count": len(result)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Alert query failed: {e}")


# =============================================
# GET /api/ml/results
# =============================================

@router.get("/api/ml/results")
async def api_ml_results(
    device: str = Query(..., description="Device name"),
    hours: int = Query(default=6, ge=1, le=168),
    user: CurrentUser = Depends(get_current_user),
):
    """Return all plc4x_ml data for a device grouped by analysis type."""
    try:
        _safe_flux_str(device)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    def _query_results():
        flux = f'''
from(bucket: "{_INFLUX_BUCKET}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "plc4x_ml")
  |> filter(fn: (r) => r.device == "{device}")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 500)
'''
        records = _influx_query(flux)
        grouped: dict[str, list] = {}
        for r in records:
            analysis = r.values.get("analysis", "unknown")
            if analysis not in grouped:
                grouped[analysis] = []
            grouped[analysis].append({
                "time": r.get_time().isoformat() if r.get_time() else None,
                "field": r.get_field(),
                "value": r.get_value(),
                "tags": {k: v for k, v in r.values.items() if not k.startswith("_") and k != "result" and k != "table"},
            })
        return grouped

    try:
        result = await asyncio.to_thread(_query_results)
        return {"device": device, "hours": hours, "results": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Results query failed: {e}")


# =============================================
# GET /api/ml/correlation
# =============================================

@router.get("/api/ml/correlation")
async def api_ml_correlation(
    device: str = Query(..., description="Device name"),
    hours: int = Query(default=24, ge=1, le=168),
    user: CurrentUser = Depends(get_current_user),
):
    """Return correlation matrix data for a device."""
    try:
        _safe_flux_str(device)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    def _query_corr():
        flux = f'''
from(bucket: "{_INFLUX_BUCKET}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "plc4x_ml")
  |> filter(fn: (r) => r.device == "{device}")
  |> filter(fn: (r) => r.analysis == "corr_pair")
  |> filter(fn: (r) => r._field == "correlation")
  |> last()
'''
        records = _influx_query(flux)

        # Build matrix
        tags_set: set = set()
        pairs: list = []
        for r in records:
            tag1 = r.values.get("tag1", "")
            tag2 = r.values.get("tag2", "")
            corr = r.get_value()
            if tag1 and tag2 and corr is not None:
                tags_set.add(tag1)
                tags_set.add(tag2)
                pairs.append({"tag1": tag1, "tag2": tag2, "correlation": round(float(corr), 4)})

        tags = sorted(tags_set)
        # Build matrix dict: matrix[tag1][tag2] = correlation
        matrix: dict = {t: {} for t in tags}
        for p in pairs:
            matrix[p["tag1"]][p["tag2"]] = p["correlation"]
            matrix[p["tag2"]][p["tag1"]] = p["correlation"]
        # Diagonal = 1.0
        for t in tags:
            matrix[t][t] = 1.0

        return {"tags": tags, "matrix": matrix, "pairs": pairs}

    try:
        result = await asyncio.to_thread(_query_corr)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Correlation query failed: {e}")


# =============================================
# GET /api/ml/config
# =============================================

@router.get("/api/ml/config")
async def api_ml_config_get(user: CurrentUser = Depends(get_current_user)):
    """Return current mlConfig from config.yml."""
    config = load_config()
    ml_cfg = _get_ml_config(config)
    return {"mlConfig": ml_cfg}


# =============================================
# PUT /api/ml/config
# =============================================

@router.put("/api/ml/config")
async def api_ml_config_put(
    request: Request,
    user: CurrentUser = Depends(require_admin),
):
    """Update mlConfig in config.yml (admin only)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    ml_cfg = body.get("mlConfig", body)  # Accept wrapped or bare config
    if not isinstance(ml_cfg, dict):
        raise HTTPException(status_code=400, detail="mlConfig must be an object")

    # Validate and clamp numeric ranges
    anomaly = ml_cfg.get("anomaly", {})
    if "contamination" in anomaly:
        anomaly["contamination"] = max(0.01, min(0.2, float(anomaly["contamination"])))
    if "minAgreement" in anomaly:
        anomaly["minAgreement"] = max(1, min(3, int(anomaly["minAgreement"])))

    changepoint = ml_cfg.get("changepoint", {})
    if "minSegmentSize" in changepoint:
        changepoint["minSegmentSize"] = max(10, min(1000, int(changepoint["minSegmentSize"])))
    if "penalty" in changepoint:
        changepoint["penalty"] = max(0.1, min(100.0, float(changepoint["penalty"])))

    correlation = ml_cfg.get("correlation", {})
    if "baselineHours" in correlation:
        correlation["baselineHours"] = max(1, min(168, int(correlation["baselineHours"])))
    if "recentMinutes" in correlation:
        correlation["recentMinutes"] = max(5, min(120, int(correlation["recentMinutes"])))
    if "breakThreshold" in correlation:
        correlation["breakThreshold"] = max(0.01, min(1.0, float(correlation["breakThreshold"])))

    pattern = ml_cfg.get("pattern", {})
    if "windowSize" in pattern:
        pattern["windowSize"] = max(10, min(500, int(pattern["windowSize"])))
    if "topK" in pattern:
        pattern["topK"] = max(1, min(10, int(pattern["topK"])))

    if "cycleIntervalMinutes" in ml_cfg:
        ml_cfg["cycleIntervalMinutes"] = max(1, min(60, int(ml_cfg["cycleIntervalMinutes"])))
    if "forecastHours" in ml_cfg:
        ml_cfg["forecastHours"] = max(1, min(24, int(ml_cfg["forecastHours"])))
    if "minPoints" in ml_cfg:
        ml_cfg["minPoints"] = max(10, min(10000, int(ml_cfg["minPoints"])))

    config = load_config()
    existing = config.get("mlConfig", {})
    existing.update(ml_cfg)
    config["mlConfig"] = existing

    try:
        save_config(config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save config: {e}")

    return {"message": "ML configuration saved", "mlConfig": config["mlConfig"]}


# =============================================
# POST /api/ml/run-now
# =============================================

@router.post("/api/ml/run-now")
async def api_ml_run_now(user: CurrentUser = Depends(require_admin)):
    """Write a trigger file so the ML predictor runs immediately on next poll."""
    try:
        os.makedirs(os.path.dirname(_ML_TRIGGER_FILE), exist_ok=True)
        with open(_ML_TRIGGER_FILE, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
        return {"message": "ML cycle triggered — predictor will run within 30 seconds"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write trigger file: {e}")
