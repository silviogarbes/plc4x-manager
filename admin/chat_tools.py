"""
LLM function-calling tools for the chat assistant.

Each tool wraps an existing internal API and returns structured data.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from config_manager import load_config
from influx import _influx_query, _safe_flux_str

log = logging.getLogger("chat_tools")

_INFLUX_BUCKET = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")


TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "query_tag_history",
            "description": "Query historical values for a specific tag on a device. Returns timestamped values.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "Device name"},
                    "tag": {"type": "string", "description": "Tag alias"},
                    "hours": {"type": "integer", "description": "Hours of history (1-720)", "default": 1},
                },
                "required": ["device", "tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_values",
            "description": "Get current live values and status for all tags on a device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "Device name"},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_alarms",
            "description": "Get all currently active alarms across all devices.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_oee",
            "description": "Get OEE breakdown for a device: availability, performance, quality.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "Device name"},
                    "hours": {"type": "integer", "description": "Hours (1-720)", "default": 24},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ml_insights",
            "description": "Get ML insights for a device: anomalies, predictions, patterns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "Device name"},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_failure_history",
            "description": "Get failure history for a device, optionally filtered by type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "Device name"},
                    "failure_type": {"type": "string", "description": "Optional failure type filter"},
                },
                "required": ["device"],
            },
        },
    },
]


async def execute_tool(name: str, arguments: dict, db: Any = None) -> dict:
    """Execute a tool by name and return its result."""
    try:
        if name == "query_tag_history":
            return await _tool_query_tag_history(arguments.get("device", ""), arguments.get("tag", ""), arguments.get("hours", 1))
        elif name == "get_current_values":
            return await _tool_get_current_values(arguments.get("device", ""))
        elif name == "get_active_alarms":
            return await _tool_get_active_alarms(db)
        elif name == "get_oee":
            return await _tool_get_oee(arguments.get("device", ""), arguments.get("hours", 24))
        elif name == "get_ml_insights":
            return await _tool_get_ml_insights(arguments.get("device", ""))
        elif name == "get_failure_history":
            return await _tool_get_failure_history(arguments.get("device", ""), arguments.get("failure_type"), db)
        else:
            return {"error": f"Unknown tool: {name}"}
    except ValueError as e:
        return {"error": f"Invalid parameter: {e}"}
    except Exception as e:
        log.error("Tool %s failed: %s", name, e)
        return {"error": f"Tool execution failed: {e}"}


async def _tool_query_tag_history(device: str, tag: str, hours: int) -> dict:
    _safe_flux_str(device)
    _safe_flux_str(tag)
    hours = max(1, min(hours, 720))

    if hours <= 6:
        bucket = _INFLUX_BUCKET
        window = "5s" if hours <= 1 else "30s"
    elif hours <= 168:
        bucket = "plc4x_hourly"
        window = "1h"
    else:
        bucket = "plc4x_daily" if hours > 720 else "plc4x_hourly"
        window = "1d" if hours > 720 else "1h"

    flux = f'''
from(bucket: "{bucket}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "plc4x_tags")
  |> filter(fn: (r) => r.device == "{device}")
  |> filter(fn: (r) => r.alias == "{tag}")
  |> filter(fn: (r) => r._field == "value")
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
'''

    import asyncio
    records = await asyncio.to_thread(_influx_query, flux)

    points = []
    for r in records:
        t = r.get_time()
        v = r.get_value()
        if v is not None:
            try:
                points.append({"t": t.isoformat(), "v": round(float(v), 3)})
            except (TypeError, ValueError):
                continue

    values = [p["v"] for p in points]
    summary = {}
    if values:
        summary = {"min": round(min(values), 3), "max": round(max(values), 3), "avg": round(sum(values) / len(values), 3), "count": len(values)}

    return {
        "result": {"device": device, "tag": tag, "hours": hours, "points": points[-200:], "summary": summary},
        "chart_data": {"labels": [p["t"] for p in points[-200:]], "values": [p["v"] for p in points[-200:]], "label": f"{device}/{tag}"},
    }


async def _tool_get_current_values(device: str) -> dict:
    _safe_flux_str(device)
    from poller import get_cache
    cache = get_cache()
    for dev in cache.get("devices", []):
        if dev.get("name") == device:
            tags = {}
            for t in dev.get("tags", []):
                tags[t.get("alias", "")] = {"value": t.get("value"), "status": t.get("status", "unknown")}
            return {"result": {"device": device, "status": dev.get("status", "unknown"), "tags": tags}}
    return {"error": f"Device '{device}' not found in live data"}


async def _tool_get_active_alarms(db: Any) -> dict:
    if db is None:
        return {"error": "Database not available"}
    async with db.execute("SELECT * FROM alarms ORDER BY timestamp DESC LIMIT 50") as cursor:
        rows = await cursor.fetchall()
    alarms = []
    for row in rows:
        alarms.append({
            "device": row["device"], "tag": row["tag"], "severity": row["severity"],
            "value": row["value"], "threshold": row["threshold"],
            "message": row["message"] or "", "timestamp": row["timestamp"],
            "acknowledged": bool(row["acknowledged"]),
        })
    return {"result": {"count": len(alarms), "alarms": alarms}}


async def _tool_get_oee(device: str, hours: int) -> dict:
    _safe_flux_str(device)
    hours = max(1, min(hours, 720))
    flux = f'''
from(bucket: "{_INFLUX_BUCKET}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "oee")
  |> filter(fn: (r) => r.device == "{device}")
  |> last()
'''
    import asyncio
    records = await asyncio.to_thread(_influx_query, flux)
    oee_data = {}
    for r in records:
        field = r.get_field()
        value = r.get_value()
        if value is not None:
            oee_data[field] = round(float(value), 2)
    if not oee_data:
        return {"result": {"device": device, "hours": hours, "message": "No OEE data available."}}
    return {"result": {"device": device, "hours": hours, **oee_data}}


async def _tool_get_ml_insights(device: str) -> dict:
    _safe_flux_str(device)
    flux = f'''
from(bucket: "{_INFLUX_BUCKET}")
  |> range(start: -6h)
  |> filter(fn: (r) => r._measurement == "plc4x_ml")
  |> filter(fn: (r) => r.device == "{device}")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 50)
'''
    import asyncio
    records = await asyncio.to_thread(_influx_query, flux)
    insights = []
    for r in records:
        insights.append({"time": r.get_time().isoformat(), "field": r.get_field(), "value": r.get_value(), "analysis": r.values.get("analysis", "")})
    return {"result": {"device": device, "count": len(insights), "insights": insights[:30]}}


async def _tool_get_failure_history(device: str, failure_type: str | None, db: Any = None) -> dict:
    _safe_flux_str(device)
    if db is None:
        return {"error": "Database not available"}
    conditions = ["device = ?"]
    params: list = [device]
    if failure_type:
        conditions.append("failure_type = ?")
        params.append(failure_type)
    where = " AND ".join(conditions)
    params.append(50)
    async with db.execute(f"SELECT * FROM failure_log WHERE {where} ORDER BY occurred_at DESC LIMIT ?", params) as cursor:
        rows = await cursor.fetchall()
    failures = []
    for row in rows:
        failures.append({
            "occurred_at": row["occurred_at"], "failure_type": row["failure_type"],
            "severity": row["severity"], "description": row["description"] or "",
            "resolved_at": row["resolved_at"],
        })
    return {"result": {"device": device, "failure_type": failure_type, "count": len(failures), "failures": failures}}
