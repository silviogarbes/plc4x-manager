"""
Device, tag, calculated-tag, formula, and demo-loader routes for PLC4X Manager FastAPI.

Endpoints:
  Devices:
    GET  /api/devices                                    — list (plant-filtered)
    POST /api/devices                                    — add (@require_admin)
    PUT  /api/devices/{name}                             — update (@require_admin)
    DELETE /api/devices/{name}                           — delete (@require_admin)

  Tags:
    GET  /api/devices/{device_name}/tags                 — list (plant-filtered)
    POST /api/devices/{device_name}/tags                 — add (@require_admin)
    DELETE /api/devices/{device_name}/tags/{alias}       — delete (@require_admin)
    PUT  /api/devices/{device_name}/tags/{alias}/alarms  — set alarm thresholds (@require_admin)

  Calculated tags:
    GET  /api/devices/{device_name}/calculated           — list (plant-filtered)
    POST /api/devices/{device_name}/calculated           — add (@require_admin)
    PUT  /api/devices/{device_name}/calculated/{alias}   — update (@require_admin)
    DELETE /api/devices/{device_name}/calculated/{alias} — delete (@require_admin)

  Formula:
    POST /api/formula/validate                           — validate + optionally evaluate

  Demo:
    POST /api/demo/load                                  — load demo devices (@require_admin)
"""

from __future__ import annotations

import asyncio
import ast
import json
import math
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import CurrentUser, get_current_user, require_admin
from config_manager import CONFIG_LOCK, CONFIG_PATH, find_device, filter_by_plant, load_config, save_config
from formula import evaluate_formula
from validators import _validate_device_tags, _validate_name, _validate_tag_address

router = APIRouter(tags=["devices"])


def _validate_threshold_values(data: dict) -> tuple[dict, Optional[tuple[dict, int]]]:
    """Extract and validate threshold values. Returns (thresholds_dict, error_or_None)."""
    thresholds: dict = {}
    for key in ("warningHigh", "warningLow", "criticalHigh", "criticalLow"):
        val = data.get(key)
        if val is not None:
            try:
                thresholds[key] = float(val)
                if not math.isfinite(thresholds[key]):
                    return {}, ({"error": f"Invalid value for '{key}': must be a finite number"}, 400)
            except (ValueError, TypeError):
                return {}, ({"error": f"Invalid value for '{key}'"}, 400)

    cl = thresholds.get("criticalLow")
    wl = thresholds.get("warningLow")
    wh = thresholds.get("warningHigh")
    ch = thresholds.get("criticalHigh")
    if cl is not None and wl is not None and cl > wl:
        return {}, ({"error": "Critical Low must be <= Warning Low"}, 400)
    if wh is not None and ch is not None and wh > ch:
        return {}, ({"error": "Warning High must be <= Critical High"}, 400)
    if wl is not None and wh is not None and wl > wh:
        return {}, ({"error": "Warning Low must be <= Warning High"}, 400)

    return thresholds, None


# =============================================
# Devices
# =============================================

@router.get("/api/devices")
async def api_list_devices(user: CurrentUser = Depends(get_current_user)):
    """Lists all devices, filtered by the user's allowed plants."""
    devices = load_config().get("devices", [])
    return filter_by_plant(devices, user.plants)


@router.post("/api/devices", status_code=201)
async def api_add_device(device: dict, user: CurrentUser = Depends(require_admin)):
    """Adds a new device."""
    if not device or "name" not in device or "connectionString" not in device:
        raise HTTPException(status_code=400, detail="Fields 'name' and 'connectionString' are required")

    if not _validate_name(device["name"]):
        raise HTTPException(
            status_code=400,
            detail="Invalid device name. Use only letters, numbers, dots, hyphens, underscores (max 128 chars)",
        )

    if "tags" not in device:
        device["tags"] = []

    tag_errors = _validate_device_tags(device)
    if tag_errors:
        raise HTTPException(status_code=400, detail="Invalid tags: " + "; ".join(tag_errors))

    def _do_add_device():
        with CONFIG_LOCK:
            config = load_config()
            devices = config.get("devices", [])
            if any(d["name"] == device["name"] for d in devices):
                raise ValueError(f"Device '{device['name']}' already exists")
            devices.append(device)
            config["devices"] = devices
            save_config(config)

    try:
        await asyncio.to_thread(_do_add_device)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"message": f"Device '{device['name']}' added"}


@router.put("/api/devices/{name}")
async def api_update_device(name: str, data: dict, user: CurrentUser = Depends(require_admin)):
    """Updates an existing device."""
    if not data or "name" not in data or "connectionString" not in data:
        raise HTTPException(status_code=400, detail="Fields 'name' and 'connectionString' are required")

    if not _validate_name(data["name"]):
        raise HTTPException(status_code=400, detail="Invalid device name")

    tag_errors = _validate_device_tags(data)
    if tag_errors:
        raise HTTPException(status_code=400, detail="Invalid tags: " + "; ".join(tag_errors))

    def _do_update_device():
        with CONFIG_LOCK:
            config = load_config()
            i, _ = find_device(config, name)
            if i is None:
                raise LookupError(f"Device '{name}' not found")
            config["devices"][i] = data
            save_config(config)

    try:
        await asyncio.to_thread(_do_update_device)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"message": f"Device '{name}' updated"}


@router.delete("/api/devices/{name}")
async def api_delete_device(name: str, user: CurrentUser = Depends(require_admin)):
    """Removes a device."""
    def _do_delete_device():
        with CONFIG_LOCK:
            config = load_config()
            i, _ = find_device(config, name)
            if i is None:
                raise LookupError(f"Device '{name}' not found")
            config["devices"].pop(i)
            save_config(config)

    try:
        await asyncio.to_thread(_do_delete_device)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"message": f"Device '{name}' removed"}


# =============================================
# Tags
# =============================================

@router.get("/api/devices/{device_name}/tags")
async def api_list_tags(device_name: str, user: CurrentUser = Depends(get_current_user)):
    """Lists tags for a device (plant-access-checked)."""
    config = load_config()
    # Plant filter: check if the device's plant is accessible to this user
    if user.plants:
        _, device_check = find_device(config, device_name)
        if device_check is not None and device_check.get("plant") not in user.plants:
            raise HTTPException(status_code=403, detail="Access denied")

    _, device = find_device(config, device_name)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{device_name}' not found")

    return device.get("tags", [])


@router.post("/api/devices/{device_name}/tags", status_code=201)
async def api_add_tag(device_name: str, tag: dict, user: CurrentUser = Depends(require_admin)):
    """Adds a tag to a device."""
    if not tag or "alias" not in tag or "address" not in tag:
        raise HTTPException(status_code=400, detail="Fields 'alias' and 'address' are required")

    if not _validate_name(tag["alias"]):
        raise HTTPException(
            status_code=400,
            detail="Invalid tag alias. Use only letters, numbers, dots, hyphens, underscores (max 128 chars)",
        )

    def _do_add_tag():
        with CONFIG_LOCK:
            config = load_config()
            _, device = find_device(config, device_name)
            if device is None:
                raise LookupError(f"Device '{device_name}' not found")

            ok, msg = _validate_tag_address(tag.get("address", ""), device.get("connectionString", ""))
            if not ok:
                raise ValueError(msg)

            tags = device.get("tags", [])
            if any(t["alias"] == tag["alias"] for t in tags):
                raise KeyError(f"Tag '{tag['alias']}' already exists")

            tags.append(tag)
            device["tags"] = tags
            save_config(config)

    try:
        await asyncio.to_thread(_do_add_tag)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"message": f"Tag '{tag['alias']}' added"}


@router.delete("/api/devices/{device_name}/tags/{alias}")
async def api_delete_tag(device_name: str, alias: str, user: CurrentUser = Depends(require_admin)):
    """Removes a tag from a device."""
    def _do_delete_tag():
        with CONFIG_LOCK:
            config = load_config()
            _, device = find_device(config, device_name)
            if device is None:
                raise LookupError(f"Device '{device_name}' not found")

            tags = device.get("tags", [])
            original_len = len(tags)
            tags = [t for t in tags if t["alias"] != alias]
            if len(tags) == original_len:
                raise LookupError(f"Tag '{alias}' not found")

            device["tags"] = tags
            save_config(config)

    try:
        await asyncio.to_thread(_do_delete_tag)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"message": f"Tag '{alias}' removed"}


@router.put("/api/devices/{device_name}/tags/{alias}/alarms")
async def api_set_tag_alarms(
    device_name: str,
    alias: str,
    data: dict,
    user: CurrentUser = Depends(require_admin),
):
    """Sets alarm thresholds for a tag.

    Simple (fixed thresholds):
      {warningHigh, warningLow, criticalHigh, criticalLow}

    Conditional (thresholds depend on another tag's value):
      {
        conditionDevice: "DeviceName",
        conditionTag: "ProductCode",
        profiles: [
          {whenValue: 1, label: "Product A", warningLow: 20, warningHigh: 30, ...},
          ...
        ],
        warningHigh, warningLow, criticalHigh, criticalLow  // defaults
      }

    Pass empty object {} to clear all thresholds.
    """
    # Validate outside lock
    thresholds, err = _validate_threshold_values(data)
    if err:
        err_body, status_code = err
        raise HTTPException(status_code=status_code, detail=err_body["error"])

    condition_device = data.get("conditionDevice")
    condition_tag = data.get("conditionTag")
    profiles = data.get("profiles")

    if profiles is not None:
        if not isinstance(profiles, list):
            raise HTTPException(status_code=400, detail="'profiles' must be an array")
        validated_profiles: list[dict] = []
        for idx, p in enumerate(profiles):
            if "whenValue" not in p:
                raise HTTPException(status_code=400, detail=f"Profile {idx + 1}: 'whenValue' is required")
            if not isinstance(p["whenValue"], (str, int, float)):
                raise HTTPException(
                    status_code=400,
                    detail=f"Profile {idx + 1}: 'whenValue' must be a string or number",
                )
            p_thresholds, p_err = _validate_threshold_values(p)
            if p_err:
                p_err_body, p_status = p_err
                raise HTTPException(status_code=p_status, detail=p_err_body["error"])
            entry: dict = {"whenValue": p["whenValue"]}
            if p.get("label"):
                entry["label"] = str(p["label"])
            entry.update(p_thresholds)
            validated_profiles.append(entry)

        if validated_profiles and not condition_tag:
            raise HTTPException(
                status_code=400,
                detail="Condition Tag is required when profiles are defined",
            )
        thresholds["profiles"] = validated_profiles
        if condition_device:
            thresholds["conditionDevice"] = str(condition_device)
        if condition_tag:
            thresholds["conditionTag"] = str(condition_tag)

    def _do_set_alarms():
        with CONFIG_LOCK:
            config = load_config()
            _, device = find_device(config, device_name)
            if device is None:
                raise LookupError(f"Device '{device_name}' not found")

            tag = next((t for t in device.get("tags", []) if t["alias"] == alias), None)
            if not tag:
                raise LookupError(f"Tag '{alias}' not found")

            if thresholds:
                tag["alarmThresholds"] = thresholds
            else:
                tag.pop("alarmThresholds", None)

            save_config(config)

    try:
        await asyncio.to_thread(_do_set_alarms)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"message": f"Alarm thresholds updated for '{alias}'", "alarmThresholds": thresholds}


# =============================================
# Calculated tags
# =============================================

@router.get("/api/devices/{device_name}/calculated")
async def api_list_calculated_tags(device_name: str, user: CurrentUser = Depends(get_current_user)):
    """Lists calculated tags for a device (plant-access-checked)."""
    config = load_config()
    if user.plants:
        _, device_check = find_device(config, device_name)
        if device_check is not None and device_check.get("plant") not in user.plants:
            raise HTTPException(status_code=403, detail="Access denied")

    _, device = find_device(config, device_name)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{device_name}' not found")

    return device.get("calculatedTags", [])


@router.post("/api/devices/{device_name}/calculated", status_code=201)
async def api_add_calculated_tag(
    device_name: str,
    data: dict,
    user: CurrentUser = Depends(require_admin),
):
    """Adds a calculated tag to a device."""
    if not data or "alias" not in data or "formula" not in data:
        raise HTTPException(status_code=400, detail="Fields 'alias' and 'formula' are required")

    if not _validate_name(data["alias"]):
        raise HTTPException(
            status_code=400,
            detail="Invalid alias. Use only letters, numbers, dots, hyphens, underscores (max 128 chars)",
        )

    try:
        ast.parse(data["formula"], mode="eval")
    except SyntaxError:
        raise HTTPException(status_code=400, detail="Invalid formula syntax")

    def _do_add_calc_tag():
        with CONFIG_LOCK:
            config = load_config()
            idx, device = find_device(config, device_name)
            if device is None:
                raise LookupError(f"Device '{device_name}' not found")

            calc_tags = device.get("calculatedTags", [])
            all_aliases = [t["alias"] for t in device.get("tags", [])] + [t["alias"] for t in calc_tags]
            if data["alias"] in all_aliases:
                raise ValueError(f"Alias '{data['alias']}' already exists (in tags or calculated tags)")

            calc_tags.append({"alias": data["alias"], "formula": data["formula"]})
            device["calculatedTags"] = calc_tags
            config["devices"][idx] = device
            save_config(config)

    try:
        await asyncio.to_thread(_do_add_calc_tag)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"message": f"Calculated tag '{data['alias']}' added"}


@router.put("/api/devices/{device_name}/calculated/{alias}")
async def api_update_calculated_tag(
    device_name: str,
    alias: str,
    data: dict,
    user: CurrentUser = Depends(require_admin),
):
    """Updates a calculated tag's formula."""
    if not data or "formula" not in data:
        raise HTTPException(status_code=400, detail="Field 'formula' is required")

    try:
        ast.parse(data["formula"], mode="eval")
    except SyntaxError:
        raise HTTPException(status_code=400, detail="Invalid formula syntax")

    def _do_update_calc_tag():
        with CONFIG_LOCK:
            config = load_config()
            idx, device = find_device(config, device_name)
            if device is None:
                raise LookupError(f"Device '{device_name}' not found")

            calc_tags = device.get("calculatedTags", [])
            for ct in calc_tags:
                if ct["alias"] == alias:
                    ct["formula"] = data["formula"]
                    device["calculatedTags"] = calc_tags
                    config["devices"][idx] = device
                    save_config(config)
                    return
            raise LookupError(f"Calculated tag '{alias}' not found")

    try:
        await asyncio.to_thread(_do_update_calc_tag)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"message": f"Calculated tag '{alias}' updated"}


@router.delete("/api/devices/{device_name}/calculated/{alias}")
async def api_delete_calculated_tag(
    device_name: str,
    alias: str,
    user: CurrentUser = Depends(require_admin),
):
    """Removes a calculated tag from a device."""
    def _do_delete_calc_tag():
        with CONFIG_LOCK:
            config = load_config()
            idx, device = find_device(config, device_name)
            if device is None:
                raise LookupError(f"Device '{device_name}' not found")

            calc_tags = device.get("calculatedTags", [])
            original_len = len(calc_tags)
            calc_tags = [t for t in calc_tags if t["alias"] != alias]
            if len(calc_tags) == original_len:
                raise LookupError(f"Calculated tag '{alias}' not found")

            device["calculatedTags"] = calc_tags
            config["devices"][idx] = device
            save_config(config)

    try:
        await asyncio.to_thread(_do_delete_calc_tag)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"message": f"Calculated tag '{alias}' removed"}


# =============================================
# Formula validation
# =============================================

@router.post("/api/formula/validate")
async def api_validate_formula(
    data: dict,
    user: CurrentUser = Depends(get_current_user),
):
    """Validates a formula and optionally evaluates it with test values."""
    if not data or "formula" not in data:
        raise HTTPException(status_code=400, detail="Field 'formula' is required")

    formula = data["formula"]
    test_values: dict = data.get("testValues", {})

    try:
        ast.parse(formula, mode="eval")
    except SyntaxError as e:
        return {"valid": False, "error": f"Syntax error: {e.msg}"}

    if test_values:
        result, error = evaluate_formula(formula, test_values)
        return {"valid": True, "result": result, "error": error}

    return {"valid": True}


# =============================================
# Demo loader
# =============================================

@router.post("/api/demo/load")
async def api_load_demo_devices(user: CurrentUser = Depends(require_admin)):
    """Loads pre-configured demo devices from demo-devices.json."""
    demo_path = os.path.join(os.path.dirname(CONFIG_PATH), "demo-devices.json")
    # Also check the app directory
    if not os.path.exists(demo_path):
        demo_path = "/app/demo-devices.json"
    if not os.path.exists(demo_path):
        raise HTTPException(status_code=404, detail="Demo devices file not found")

    with open(demo_path, "r", encoding="utf-8") as f:
        demo_devices = json.load(f)

    result: dict = {}

    def _do_load_demo():
        with CONFIG_LOCK:
            config = load_config()
            existing_names = {d["name"] for d in config.get("devices", [])}
            added: list[str] = []
            skipped: list[str] = []

            for demo in demo_devices:
                if demo["name"] in existing_names:
                    skipped.append(demo["name"])
                else:
                    config.setdefault("devices", []).append(demo)
                    existing_names.add(demo["name"])
                    added.append(demo["name"])

            if added:
                save_config(config)

            result["added"] = added
            result["skipped"] = skipped

    await asyncio.to_thread(_do_load_demo)
    added = result["added"]
    skipped = result["skipped"]
    return {
        "message": f"Added {len(added)} demo devices, skipped {len(skipped)} (already exist)",
        "added": added,
        "skipped": skipped,
    }


# =============================================
# OPC-UA Tag Discovery
# =============================================

@router.post("/api/devices/{device_name}/test-connection")
async def api_test_connection(device_name: str, user: CurrentUser = Depends(require_admin)):
    """Test if the device is reachable via the PLC4X OPC-UA server.

    Checks three levels:
    1. OPC-UA server reachable
    2. Device node exists in OPC-UA namespace
    3. Device has readable tags with live data (from poller cache)
    """
    import asyncio
    from asyncua import Client as OpcClient

    config = load_config()
    _, device = find_device(config, device_name)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{device_name}' not found")

    conn_string = device.get("connectionString", "")
    enabled = device.get("enabled", True) is not False

    # Level 3: Check poller cache for live status (fastest, no OPC-UA call needed)
    try:
        from poller import get_cache
        cache = get_cache()
        for dev in cache.get("devices", []):
            if dev.get("name") == device_name:
                status = dev.get("status", "unknown")
                tags_ok = sum(1 for t in dev.get("tags", []) if t.get("status") == "ok")
                tags_err = sum(1 for t in dev.get("tags", []) if t.get("status") == "read_error")
                tags_total = len(dev.get("tags", []))
                return {
                    "connected": status == "online",
                    "status": status,
                    "connectionString": conn_string,
                    "enabled": enabled,
                    "tagsOk": tags_ok,
                    "tagsError": tags_err,
                    "tagsTotal": tags_total,
                    "message": f"Device is {status}. {tags_ok}/{tags_total} tags reading OK."
                        if status == "online"
                        else f"Device is {status}. Check connection string and PLC network."
                }
    except Exception:
        pass

    # Level 1+2: Connect to OPC-UA server and check device node
    opcua_port = config.get("tcpPort", 12687)
    opcua_url = f"opc.tcp://plc4x-server:{opcua_port}/plc4x"

    async def _do_test():
        client = OpcClient(url=opcua_url, timeout=5)
        client.set_security_string("None")
        await asyncio.wait_for(client.connect(), timeout=5)
        try:
            # Check if device node exists
            objects = client.nodes.objects
            children = await objects.get_children()
            device_found = False
            for child in children:
                dn = await child.read_display_name()
                if dn.Text == device_name:
                    device_found = True
                    break
            return {
                "connected": device_found,
                "status": "online" if device_found else "not_found",
                "connectionString": conn_string,
                "enabled": enabled,
                "tagsOk": 0,
                "tagsError": 0,
                "tagsTotal": 0,
                "message": f"Device node found in OPC-UA server." if device_found
                    else f"OPC-UA server reachable but device '{device_name}' not found. Check if device is enabled and connection string is correct."
            }
        finally:
            await client.disconnect()

    try:
        result = await asyncio.wait_for(_do_test(), timeout=10)
        return result
    except asyncio.TimeoutError:
        return {
            "connected": False, "status": "timeout",
            "connectionString": conn_string, "enabled": enabled,
            "tagsOk": 0, "tagsError": 0, "tagsTotal": 0,
            "message": "Connection timeout — OPC-UA server did not respond in 10 seconds"
        }
    except Exception as e:
        return {
            "connected": False, "status": "error",
            "connectionString": conn_string, "enabled": enabled,
            "tagsOk": 0, "tagsError": 0, "tagsTotal": 0,
            "message": f"Connection failed: {type(e).__name__}: {e}"
        }


@router.post("/api/devices/{device_name}/discover")
async def api_discover_tags(device_name: str, user: CurrentUser = Depends(require_admin)):
    """
    Browse available tags for a device.

    For EtherNet/IP devices (connection string starts with "eip://"), uses the
    pylogix microservice for native tag discovery.  Falls back to OPC-UA browse
    for all other protocol types.

    Returns a list of discovered tags with name, address, data type.
    """
    import asyncio
    from asyncua import Client as OpcClient, ua

    config = load_config()
    _, device = find_device(config, device_name)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{device_name}' not found")

    # ------------------------------------------------------------------
    # EtherNet/IP fast-path: use pylogix microservice
    # ------------------------------------------------------------------
    conn = device.get("connectionString", "")
    if conn.startswith("eip://"):
        ip = conn.replace("eip://", "").split(":")[0]

        def _do_eip_discover():
            import requests as _requests
            resp = _requests.post(
                "http://plc4x-server:5000/discover/tags",
                params={"ip": ip},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                tags = data.get("tags", [])
                mapped = []
                for t in tags:
                    mapped.append({
                        "alias": t.get("name", ""),
                        "address": t.get("address", ""),
                        "dataType": t.get("type", "Unknown"),
                        "nodeClass": "Variable",
                        "browsePath": t.get("program") or "Controller",
                        "isUDT": t.get("isUDT", False),
                        "isArray": t.get("isArray", False),
                        "program": t.get("program"),
                    })
                return {"tags": mapped, "source": "pylogix", "count": len(mapped)}
            elif resp.status_code == 429:
                raise HTTPException(status_code=429, detail=resp.json().get("error", "Rate limited"))
            elif resp.status_code == 409:
                raise HTTPException(status_code=409, detail=resp.json().get("error", "PLC busy"))
            else:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=resp.json().get("error", "Discovery failed"),
                )

        try:
            return await asyncio.to_thread(_do_eip_discover)
        except HTTPException:
            raise
        except Exception:
            # pylogix service unavailable — fall through to OPC-UA browse
            pass

    opcua_port = config.get("tcpPort", 12687)
    opcua_url = f"opc.tcp://plc4x-server:{opcua_port}/plc4x"

    async def _browse_node(client, node, ns_idx: int, path_parts: list, depth: int, results: list):
        """Recursively browse a node up to max_depth, collecting Variable nodes."""
        if depth > 5:
            return
        try:
            children = await asyncio.wait_for(node.get_children(), timeout=3)
        except Exception:
            return

        for child in children:
            try:
                node_class = await asyncio.wait_for(child.read_node_class(), timeout=2)
                browse_name = await asyncio.wait_for(child.read_browse_name(), timeout=2)
                display_name = await asyncio.wait_for(child.read_display_name(), timeout=2)
                node_id = child.nodeid
            except Exception:
                continue

            child_name = display_name.Text or browse_name.Name or ""
            child_path = path_parts + [child_name]

            if node_class == ua.NodeClass.Variable:
                # Get data type
                data_type_str = "Unknown"
                try:
                    vtype = await asyncio.wait_for(child.read_data_type_as_variant_type(), timeout=2)
                    data_type_str = vtype.name
                except Exception:
                    try:
                        dv = await asyncio.wait_for(child.read_data_value(), timeout=2)
                        if dv.Value is not None:
                            data_type_str = type(dv.Value.Value).__name__
                    except Exception:
                        pass

                # Build address string from NodeId
                nid = node_id
                if nid.NamespaceIndex == ns_idx and nid.NodeIdType.name == "String":
                    address = f"ns={nid.NamespaceIndex};s={nid.Identifier}"
                else:
                    address = nid.to_string()

                results.append({
                    "alias": child_name,
                    "address": address,
                    "dataType": data_type_str,
                    "nodeClass": "Variable",
                    "browsePath": "/".join(child_path),
                })
            elif node_class == ua.NodeClass.Object:
                # Recurse into folders / object nodes
                await _browse_node(client, child, ns_idx, child_path, depth + 1, results)

    async def _do_discover():
        client = OpcClient(url=opcua_url, timeout=5)
        client.set_security_string("None")
        await asyncio.wait_for(client.connect(), timeout=5)
        try:
            # Find namespace index for plc4x
            nsarray = await asyncio.wait_for(client.get_namespace_array(), timeout=5)
            ns_idx = 2  # default fallback
            for i, ns in enumerate(nsarray):
                if "plc4x" in ns.lower() or "middleware" in ns.lower():
                    ns_idx = i
                    break

            # Browse Objects folder → find device node by name
            objects_node = client.nodes.objects
            device_node = None
            top_children = await asyncio.wait_for(objects_node.get_children(), timeout=5)
            for child in top_children:
                try:
                    dn = await asyncio.wait_for(child.read_display_name(), timeout=2)
                    if dn.Text == device_name:
                        device_node = child
                        break
                except Exception:
                    continue

            results: list = []
            if device_node is not None:
                await _browse_node(client, device_node, ns_idx, [device_name], 1, results)
            else:
                # Fallback: browse entire Objects tree filtered to device_name path
                await _browse_node(client, objects_node, ns_idx, [], 0, results)
                # Filter to tags whose address contains the device name
                results = [r for r in results if device_name in r["address"] or device_name in r["browsePath"]]

            return results
        finally:
            await client.disconnect()

    try:
        tags = await asyncio.wait_for(_do_discover(), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Discovery timeout — OPC-UA server did not respond in 30 seconds")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Discovery failed: {type(e).__name__}: {e}")

    if not tags:
        return {
            "tags": [],
            "message": f"No readable tags found for device '{device_name}'. "
                       "The device may be offline or have no browsable variables.",
        }

    return {"tags": tags, "count": len(tags)}


@router.post("/api/demo/seed")
async def api_seed_demo(user: CurrentUser = Depends(require_admin)):
    """Seeds the full demo environment: devices, tags, alarms, calculated tags,
    OEE config, ML config, virtual tags, logbook entries. Overwrites existing config."""
    from seed_demo import run_seed
    await asyncio.to_thread(run_seed)
    from audit import audit_log
    audit_log("demo_seed", user=user.username)
    return {"message": "Demo data seeded: 2 devices, 10 tags, 5 alarms, 4 calculated tags, OEE, ML config, 5 logbook entries"}
