"""
Live OPC-UA data routes for PLC4X Manager FastAPI.

Endpoints:
  GET  /api/live/read              — cached poller data + calculated tags (plant-filtered)
  GET  /api/live/read/{device_name} — device-specific live data (plant-filtered)
  GET  /api/live/write-log         — read write log from SQLite (@require_operator)
  POST /api/live/write             — write tag via OPC-UA (@require_operator)

The write endpoint uses asyncua Client with `await` directly (FastAPI is async).
"""

from __future__ import annotations

import datetime
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import CurrentUser, get_current_user, require_operator
from config_manager import filter_by_plant, load_config
from formula import _process_calculated_tags

router = APIRouter(tags=["live"])

# =============================================
# Constants
# =============================================

_CONFIG_DIR = os.path.dirname(os.environ.get("CONFIG_PATH", "/app/config/config.yml"))
_WRITE_AUDIT_LOG = os.path.join(_CONFIG_DIR, "write-audit.log")


def _cast_to_opcua_type(value, variant_type, ua):
    """Casts a JSON value to the correct OPC-UA variant type."""
    type_map = {
        ua.VariantType.Boolean: bool,
        ua.VariantType.SByte: int,
        ua.VariantType.Byte: int,
        ua.VariantType.Int16: int,
        ua.VariantType.UInt16: int,
        ua.VariantType.Int32: int,
        ua.VariantType.UInt32: int,
        ua.VariantType.Int64: int,
        ua.VariantType.UInt64: int,
        ua.VariantType.Float: float,
        ua.VariantType.Double: float,
        ua.VariantType.String: str,
    }
    cast_fn = type_map.get(variant_type)
    if cast_fn:
        return cast_fn(value)
    return value


def _audit_write_file(device: str, tag: str, value, username: str, error=None):
    """Appends a write operation to the write audit log file (fallback)."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "OK" if not error else f"FAILED: {error}"
    line = f"[{ts}] user={username} device={device} tag={tag} value={value} status={status}\n"
    try:
        with open(_WRITE_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


async def _audit_write_db(db, device: str, tag: str, value, username: str, error=None):
    """Insert a write operation into the SQLite write_log table (primary)."""
    status = "ok" if not error else f"failed: {error}"
    try:
        await db.execute(
            "INSERT INTO write_log (user, device, tag, value, status) VALUES (?, ?, ?, ?, ?)",
            (username, device, tag, str(value), status)
        )
        await db.commit()
    except Exception:
        # Fallback to file on DB error
        _audit_write_file(device, tag, value, username, error)


# =============================================
# Routes
# =============================================

@router.get("/api/live/read")
@router.get("/api/live/read/{device_name}")
async def api_live_read(
    request: Request,
    device_name: str | None = None,
    device: str | None = Query(default=None),
    user: CurrentUser = Depends(get_current_user),
):
    """Returns live tag values from the background poller cache."""
    from poller import get_cache

    device_filter = device_name or device
    data = get_cache()

    # Apply calculated tags from admin config
    config = load_config()
    config_devices = {d["name"]: d for d in config.get("devices", [])}
    for dev in data.get("devices", []):
        cfg = config_devices.get(dev["name"])
        if cfg:
            dev["allowWrite"] = cfg.get("allowWrite", False)
            dev["pollInterval"] = cfg.get("pollInterval", 5)
            _process_calculated_tags(dev, cfg)

    # Filter by user's allowed plants
    data["devices"] = filter_by_plant(data.get("devices", []), user.plants)

    # Filter by device if requested
    if device_filter:
        filtered = [d for d in data.get("devices", []) if d["name"] == device_filter]
        if not filtered:
            data["error"] = f"Device '{device_filter}' not found"
        data["devices"] = filtered

    return data


@router.get("/api/live/write-log")
async def api_write_log(
    request: Request,
    lines: int = Query(default=100, ge=1, le=1000),
    user: CurrentUser = Depends(require_operator),
):
    """Returns the write audit log from SQLite (newest first)."""
    db = request.app.state.db

    try:
        async with db.execute(
            "SELECT * FROM write_log ORDER BY id DESC LIMIT ?", (lines,)
        ) as cursor:
            rows = await cursor.fetchall()

        async with db.execute("SELECT COUNT(*) FROM write_log") as c:
            total = (await c.fetchone())[0]

        entries = []
        for row in rows:
            entries.append({
                "timestamp": row["timestamp"],
                "user": row["user"],
                "device": row["device"],
                "tag": row["tag"],
                "value": row["value"],
                "status": row["status"],
            })

        return {"entries": entries, "total": total}

    except Exception:
        # Fallback: return raw text from file for backward compat
        try:
            with open(_WRITE_AUDIT_LOG, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
            return {"log": "".join(all_lines[-lines:]), "total": len(all_lines)}
        except FileNotFoundError:
            return {"entries": [], "total": 0}


@router.post("/api/live/write")
async def api_live_write(
    request: Request,
    body: dict,
    user: CurrentUser = Depends(require_operator),
):
    """Writes a value to a PLC tag via the OPC-UA server."""
    import asyncio
    from asyncua import Client as OpcClient, ua

    # Defense in depth: block writes early so user gets immediate feedback
    from config_manager import is_plc_readonly
    if is_plc_readonly():
        raise HTTPException(
            status_code=403,
            detail="System is in read-only mode (PLC_READONLY=true). All PLC writes are blocked."
        )

    if not body or "device" not in body or "tag" not in body or "value" not in body:
        raise HTTPException(status_code=400, detail="Fields 'device', 'tag', and 'value' are required")

    device_name = body["device"]
    tag_alias = body["tag"]
    write_value = body["value"]

    config = load_config()
    opcua_port = config.get("tcpPort", 12687)
    opcua_url = f"opc.tcp://plc4x-server:{opcua_port}/plc4x"

    # Verify device exists
    device = None
    for d in config.get("devices", []):
        if d["name"] == device_name:
            device = d
            break
    if not device:
        raise HTTPException(status_code=404, detail=f"Device '{device_name}' not found")

    # Plant filter
    if user.plants and device.get("plant") not in user.plants:
        raise HTTPException(status_code=403, detail="Access denied")

    if device.get("enabled", True) is False:
        raise HTTPException(status_code=400, detail=f"Device '{device_name}' is disabled")
    if not device.get("allowWrite", False):
        raise HTTPException(
            status_code=403,
            detail=f"Write is disabled for device '{device_name}'. Enable it in the device settings (allowWrite)."
        )

    # Verify tag exists
    tag = None
    for t in device.get("tags", []):
        if t["alias"] == tag_alias:
            tag = t
            break
    if not tag:
        raise HTTPException(status_code=404, detail=f"Tag '{tag_alias}' not found in device '{device_name}'")

    # Virtual tags: write directly to store, skip OPC-UA
    if tag.get("address", "").upper().startswith("VIRTUAL"):
        from poller import set_virtual_tag
        set_virtual_tag(device_name, tag_alias, write_value)
        db = request.app.state.db
        await _audit_write_db(db, device_name, tag_alias, write_value, user.username)
        return {"message": f"Virtual tag '{tag_alias}' set to {write_value}"}

    # Write via OPC-UA (async — FastAPI is already in an async context)
    async def write_tag():
        client = OpcClient(url=opcua_url, timeout=3)
        client.set_security_string("None")
        await asyncio.wait_for(client.connect(), timeout=3)
        try:
            nsarray = await client.get_namespace_array()
            ns_idx = None
            for i, ns in enumerate(nsarray):
                if "plc4x" in ns.lower() or "middleware" in ns.lower():
                    ns_idx = i
                    break

            node_ids_to_try = []
            if ns_idx is not None:
                node_ids_to_try.append(f"ns={ns_idx};s={device_name}/{tag_alias}")
                node_ids_to_try.append(f"ns={ns_idx};s={tag_alias}")
            node_ids_to_try.append(f"ns=2;s={device_name}/{tag_alias}")
            node_ids_to_try.append(f"ns=2;s={tag_alias}")

            for nid in node_ids_to_try:
                try:
                    node = client.get_node(nid)
                    dv = await asyncio.wait_for(node.read_data_value(), timeout=2)
                    current_type = dv.Value.VariantType
                    typed_value = _cast_to_opcua_type(write_value, current_type, ua)
                    await asyncio.wait_for(
                        node.write_value(ua.DataValue(ua.Variant(typed_value, current_type))),
                        timeout=2
                    )
                    return {"success": True, "node": nid, "value": write_value}
                except ua.UaStatusCodeError as e:
                    return {"success": False, "error": f"Write rejected: {e}"}
                except Exception:
                    continue

            return {"success": False, "error": "Tag node not found on OPC-UA server"}
        finally:
            await client.disconnect()

    db = request.app.state.db
    try:
        result = await write_tag()
        if result["success"]:
            await _audit_write_db(db, device_name, tag_alias, write_value, user.username)
            return {"message": f"Value {write_value} written to {device_name}/{tag_alias}"}
        else:
            await _audit_write_db(db, device_name, tag_alias, write_value, user.username, error=result["error"])
            raise HTTPException(status_code=400, detail=result["error"])
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Connection timeout - OPC-UA server may not be running")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Write failed: {type(e).__name__}")
