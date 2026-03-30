"""
Data export, history, and Grafana proxy routes for PLC4X Manager FastAPI.

Endpoints:
  GET  /api/tags/history    — tag trend from InfluxDB (with smart bucket selection + cache)
  GET  /api/export/csv      — StreamingResponse with CSV data
  GET  /api/export/pdf      — StreamingResponse with PDF (ReportLab)
  POST /api/data/write      — write data to InfluxDB (@require_operator)
  GET  /grafana/{path}      — proxy to Grafana (restricted paths only)
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import re

from typing import Any, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from auth import CurrentUser, get_current_user, require_operator
from config_manager import filter_by_plant, find_device, load_config
from influx import _get_influx_client, _influx_query, _safe_flux_str, _trend_cache_get, _trend_cache_set

router = APIRouter(tags=["data"])


# =============================================
# Grafana proxy config
# =============================================

_GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://grafana:3000")
_GRAFANA_USER = os.environ.get("GRAFANA_ADMIN_USER", "admin")
_GRAFANA_PASS = os.environ.get("GRAFANA_ADMIN_PASSWORD", os.environ.get("GF_SECURITY_ADMIN_PASSWORD", "admin"))

# Allowed path prefixes for the Grafana proxy
_GRAFANA_ALLOWED_PREFIXES = ("/d/", "/api/", "/public/")


# =============================================
# Routes
# =============================================

@router.get("/api/tags/history")
async def api_tag_history(
    device: str = Query(..., description="Device name"),
    tag: str = Query(..., description="Tag alias"),
    hours: int = Query(default=1, ge=1, le=8760),
    user: CurrentUser = Depends(get_current_user),
):
    """Returns tag value history for trending."""
    try:
        _safe_flux_str(device)
        _safe_flux_str(tag)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Plant filter
    config = load_config()
    _, dev = find_device(config, device)
    if dev is not None:
        if user.plants and dev.get("plant") not in user.plants:
            raise HTTPException(status_code=403, detail="Access denied")

    # Smart bucket + window selection
    if hours <= 6:
        bucket = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")
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
  |> sort(columns: ["_time"])
  |> limit(n: 2000)
'''
    cache_key = f"{device}:{tag}:{hours}"
    cached = _trend_cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        records = await asyncio.to_thread(_influx_query, flux)

        # Fallback to raw bucket if aggregated bucket returned no data and
        # time range is within raw retention (90 days = 2160 hours)
        if not records and bucket != os.environ.get("INFLUXDB_BUCKET", "plc4x_raw") and hours <= 2160:
            raw_bucket = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")
            raw_window = "1m" if hours <= 24 else "5m" if hours <= 168 else "30m"
            flux_raw = f'''
from(bucket: "{raw_bucket}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "plc4x_tags")
  |> filter(fn: (r) => r.device == "{device}")
  |> filter(fn: (r) => r.alias == "{tag}")
  |> filter(fn: (r) => r._field == "value")
  |> aggregateWindow(every: {raw_window}, fn: mean, createEmpty: false)
  |> sort(columns: ["_time"])
  |> limit(n: 2000)
'''
            records = await asyncio.to_thread(_influx_query, flux_raw)

        points = [{"t": r.get_time().isoformat(), "v": r.get_value()} for r in records if r.get_value() is not None]
        result = {"device": device, "tag": tag, "hours": hours, "points": points}
        _trend_cache_set(cache_key, result)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Query failed: {e}")


@router.get("/api/export/csv")
async def api_export_csv(
    device: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    plant: str | None = Query(default=None),
    hours: int = Query(default=24, ge=1, le=8760),
    user: CurrentUser = Depends(get_current_user),
):
    """Exports tag history from InfluxDB as CSV."""
    # Validate filter inputs against injection
    try:
        if device: _safe_flux_str(device)
        if tag: _safe_flux_str(tag)
        if plant: _safe_flux_str(plant)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Plant filter enforcement
    plant_filter = None
    allowed = user.plants
    if allowed:
        if device:
            config = load_config()
            _, dev = find_device(config, device)
            if dev is not None and dev.get("plant") not in allowed:
                raise HTTPException(status_code=403, detail="Access denied")
        if plant and plant not in allowed:
            raise HTTPException(status_code=403, detail="Access denied")
        if not plant and len(allowed) > 1:
            plant_filter = " or ".join([f'r["plant"] == "{_safe_flux_str(p)}"' for p in allowed])
        elif not plant and len(allowed) == 1:
            plant = allowed[0]

    bucket = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")
    flux = f'''
    from(bucket: "{bucket}")
      |> range(start: -{hours}h)
      |> filter(fn: (r) => r._measurement == "plc4x_tags")
      |> filter(fn: (r) => r._field == "value" or r._field == "value_str")
    '''
    if device:
        flux += f'  |> filter(fn: (r) => r.device == "{device}")\n'
    if tag:
        flux += f'  |> filter(fn: (r) => r.alias == "{tag}")\n'
    if plant:
        flux += f'  |> filter(fn: (r) => r.plant == "{plant}")\n'
    elif plant_filter:
        flux += f'  |> filter(fn: (r) => {plant_filter})\n'
    flux += '  |> sort(columns: ["_time"])\n'
    flux += '  |> limit(n: 100000)\n'

    try:
        records = await asyncio.to_thread(_influx_query, flux)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"InfluxDB query failed: {e}")

    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Timestamp", "Plant", "Device", "Tag", "Value"])
        for r in records:
            writer.writerow([
                r.get_time().strftime("%Y-%m-%d %H:%M:%S") if r.get_time() else "",
                r.values.get("plant", ""),
                r.values.get("device", ""),
                r.values.get("alias", ""),
                r.get_value()
            ])
        yield output.getvalue()

    safe_device = re.sub(r'[^\w\-]', '_', device) if device else 'all'
    filename = f"plc4x_export_{safe_device}_{hours}h.csv"
    return StreamingResponse(
        generate_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/api/export/pdf")
async def api_export_pdf(
    device: str | None = Query(default=None),
    plant: str | None = Query(default=None),
    hours: int = Query(default=24, ge=1, le=8760),
    user: CurrentUser = Depends(get_current_user),
):
    """Generates a PDF report with device status, tag summary, and alarm history."""
    import datetime as _dt

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    try:
        if device: _safe_flux_str(device)
        if plant: _safe_flux_str(plant)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Plant filter enforcement
    allowed = user.plants
    if allowed:
        if device:
            config = load_config()
            _, dev = find_device(config, device)
            if dev is not None and dev.get("plant") not in allowed:
                raise HTTPException(status_code=403, detail="Access denied")
        if plant and plant not in allowed:
            raise HTTPException(status_code=403, detail="Access denied")

    now = _dt.datetime.now(_dt.timezone.utc)
    period_start = now - _dt.timedelta(hours=hours)

    # Gather data
    from poller import get_alarms, get_cache

    cache = get_cache()
    devices = cache.get("devices", [])
    if device:
        devices = [d for d in devices if d["name"] == device]
    if plant:
        devices = [d for d in devices if d.get("plant") == plant]

    alarm_data = get_alarms()
    active_alarms = list(alarm_data.get("active", {}).values())
    history_alarms = alarm_data.get("history", [])
    if allowed:
        active_alarms = [a for a in active_alarms if a.get("plant") in allowed]
        history_alarms = [a for a in history_alarms if a.get("plant") in allowed]

    # Tag statistics from InfluxDB
    bucket = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")
    stats = []
    try:
        flux = f'''
        from(bucket: "{bucket}")
          |> range(start: -{hours}h)
          |> filter(fn: (r) => r._measurement == "plc4x_tags")
          |> filter(fn: (r) => r._field == "value")
        '''
        if device:
            flux += f'  |> filter(fn: (r) => r.device == "{device}")\n'
        if plant:
            flux += f'  |> filter(fn: (r) => r.plant == "{plant}")\n'
        flux += '''
          |> group(columns: ["device", "alias"])
          |> reduce(fn: (r, accumulator) => ({
              count: accumulator.count + 1,
              sum: accumulator.sum + r._value,
              min: if r._value < accumulator.min then r._value else accumulator.min,
              max: if r._value > accumulator.max then r._value else accumulator.max
            }), identity: {count: 0, sum: 0.0, min: 1e20, max: -1e20})
        '''
        records = await asyncio.to_thread(_influx_query, flux)
        for r in records:
            count = r.values.get("count", 0)
            stats.append({
                "device": r.values.get("device", ""),
                "alias": r.values.get("alias", ""),
                "min": round(r.values.get("min", 0), 2),
                "max": round(r.values.get("max", 0), 2),
                "avg": round(r.values.get("sum", 0) / count, 2) if count > 0 else 0,
                "count": count
            })
    except Exception:
        pass

    # Build PDF
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), topMargin=15*mm, bottomMargin=15*mm,
                            leftMargin=15*mm, rightMargin=15*mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Title2", parent=styles["Title"], fontSize=18, spaceAfter=6)
    subtitle_style = ParagraphStyle("Sub", parent=styles["Heading2"], fontSize=12, spaceAfter=4)
    small_style = ParagraphStyle("Small", parent=styles["Normal"], fontSize=8)

    elements = []

    # Header
    elements.append(Paragraph("PLC4X Manager — Report", title_style))
    elements.append(Paragraph(
        f"Period: {period_start.strftime('%Y-%m-%d %H:%M')} to {now.strftime('%Y-%m-%d %H:%M UTC')} ({hours}h)"
        + (f" | Plant: {plant}" if plant else "")
        + (f" | Device: {device}" if device else ""),
        small_style))
    elements.append(Spacer(1, 8*mm))

    # Device Status
    elements.append(Paragraph("Device Status", subtitle_style))
    if devices:
        data = [["Device", "Plant", "Status", "Tags OK", "Tags Error", "Latency"]]
        for d in devices:
            ok = sum(1 for t in d.get("tags", []) if t.get("status") == "ok")
            err = sum(1 for t in d.get("tags", []) if t.get("status") == "read_error")
            data.append([d["name"], d.get("plant", ""), d.get("status", ""),
                        str(ok), str(err), f"{d.get('read_latency_ms', '-')} ms"])
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#c8102e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ]))
        elements.append(t)
    else:
        elements.append(Paragraph("No devices found.", small_style))
    elements.append(Spacer(1, 6*mm))

    # Tag Statistics
    elements.append(Paragraph(f"Tag Statistics (last {hours}h)", subtitle_style))
    if stats:
        data = [["Device", "Tag", "Min", "Max", "Avg", "Samples"]]
        for s in sorted(stats, key=lambda x: (x["device"], x["alias"])):
            data.append([s["device"], s["alias"], str(s["min"]), str(s["max"]),
                        str(s["avg"]), str(s["count"])])
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#c8102e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ]))
        elements.append(t)
    else:
        elements.append(Paragraph("No tag data available for this period.", small_style))
    elements.append(Spacer(1, 6*mm))

    # Active Alarms
    elements.append(Paragraph("Active Alarms", subtitle_style))
    if active_alarms:
        data = [["Severity", "Device", "Tag", "Value", "Threshold", "Since", "ACK"]]
        for a in active_alarms:
            ts = a.get("timestamp", "")[:19].replace("T", " ") if a.get("timestamp") else ""
            data.append([a.get("severity", "").upper(), a.get("device", ""), a.get("tag", ""),
                        str(a.get("value", "")), f"{a.get('condition','')}{a.get('threshold','')}",
                        ts, "Yes" if a.get("acknowledged") else "No"])
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#c8102e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ]))
        elements.append(t)
    else:
        elements.append(Paragraph("No active alarms.", small_style))
    elements.append(Spacer(1, 6*mm))

    # Alarm History (last 50)
    elements.append(Paragraph("Alarm History (recent)", subtitle_style))
    recent = list(reversed(history_alarms[-50:]))
    if recent:
        data = [["Severity", "Device", "Tag", "Value", "Status", "Triggered", "Cleared"]]
        for a in recent:
            ts = a.get("timestamp", "")[:19].replace("T", " ") if a.get("timestamp") else ""
            cl = a.get("clearedAt", "")[:19].replace("T", " ") if a.get("clearedAt") else ""
            data.append([a.get("severity", "").upper(), a.get("device", ""), a.get("tag", ""),
                        str(a.get("value", "")), a.get("status", ""), ts, cl])
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#c8102e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ]))
        elements.append(t)
    else:
        elements.append(Paragraph("No alarm history.", small_style))

    # Footer
    elements.append(Spacer(1, 10*mm))
    elements.append(Paragraph(
        f"Generated by PLC4X Manager on {now.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        ParagraphStyle("Footer", parent=styles["Normal"], fontSize=7, textColor=colors.grey)))

    doc.build(elements)
    buf.seek(0)
    pdf_bytes = buf.getvalue()

    filename = f"plc4x_report_{now.strftime('%Y%m%d_%H%M')}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.post("/api/data/write")
async def api_data_write(request: Request, user: CurrentUser = Depends(require_operator)):
    """Writes custom data points to InfluxDB."""
    import json as _json

    from influxdb_client import Point
    from influxdb_client.client.write_api import SYNCHRONOUS

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not body:
        raise HTTPException(status_code=400, detail="Empty payload")

    # Support single or batch
    if isinstance(body, list):
        points = body
    elif isinstance(body, dict):
        points = [body]
    else:
        raise HTTPException(status_code=400, detail="Invalid payload format")

    influx_bucket = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")

    try:
        client = _get_influx_client()
        write_api = client.write_api(write_options=SYNCHRONOUS)

        written = 0
        for item in points:
            measurement = item.get("measurement")
            fields = item.get("fields")
            if not measurement or not fields or not isinstance(fields, dict):
                continue

            p = Point(measurement)

            # Add tags
            for k, v in item.get("tags", {}).items():
                p = p.tag(str(k), str(v))

            # Add fields
            for k, v in fields.items():
                if isinstance(v, bool):
                    p = p.field(str(k), float(v))
                elif isinstance(v, (int, float)):
                    p = p.field(str(k), float(v))
                else:
                    p = p.field(str(k), str(v))

            # Timestamp
            ts = item.get("timestamp")
            if ts:
                p = p.time(ts)

            write_api.write(bucket=influx_bucket, record=p)
            written += 1

            # Also publish to MQTT if available
            try:
                from poller import MQTT_TOPIC_PREFIX, _mqtt_client
                if _mqtt_client:
                    topic = f"{MQTT_TOPIC_PREFIX}/_custom/{measurement}"
                    _mqtt_client.publish(topic, _json.dumps({"tags": item.get("tags", {}), "fields": fields}), qos=0)
            except Exception:
                pass

        write_api.close()
        return {"message": f"{written} data point(s) written", "written": written}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Write failed: {str(e)}")


@router.get("/grafana/{path:path}")
async def grafana_proxy(path: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    """Proxy Grafana requests through the authenticated admin panel.

    Restricted to /d/, /api/, and /public/ paths only.
    """
    import requests as _requests

    # Validate path: only allow certain prefixes
    if not any(path.startswith(p.lstrip("/")) for p in _GRAFANA_ALLOWED_PREFIXES):
        raise HTTPException(status_code=403, detail="Access to this Grafana path is not allowed")

    grafana_url = f"{_GRAFANA_URL}/{path}"
    query_string = request.url.query
    if query_string:
        grafana_url += f"?{query_string}"

    def _do_grafana_request():
        headers = {"Accept": request.headers.get("Accept", "*/*")}
        return _requests.get(
            grafana_url,
            auth=(_GRAFANA_USER, _GRAFANA_PASS),
            headers=headers,
            timeout=15,
            stream=False,
        )

    try:
        resp = await asyncio.to_thread(_do_grafana_request)
        excluded_headers = {"content-encoding", "transfer-encoding", "connection"}
        response_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded_headers}
        response_headers.pop("X-Frame-Options", None)
        return Response(content=resp.content, status_code=resp.status_code, headers=response_headers)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Grafana unavailable: {e}")
