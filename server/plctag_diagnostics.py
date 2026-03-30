"""
Rockwell PLC Diagnostics.

Uses pylogix exclusively for connectivity checks and tag reads.

Read-only diagnostic information:
- PLC reachability / connectivity test
- Tag count and program inventory
- Single tag value reads

All operations are read-only and short-lived.
"""

import logging
from plctag_safety import safe_plc_operation, PLCConnectionError
from plctag_discovery import _create_plc

log = logging.getLogger("plctag")


@safe_plc_operation("plc_identity")
def get_plc_identity(ip, path="1,0", timeout=5000):
    """
    Get PLC identity / reachability via pylogix GetDeviceProperties.

    Returns:
        {
            "ip": "192.168.1.40",
            "vendor": "Rockwell Automation",
            "model": "1769-L33ER",
            "firmware": "30.11",
            "serial": "0x00c3ad12",
            "name": "1769-L33ER",
            "reachable": True
        }
    """
    with _create_plc(ip, path, timeout) as plc:
        props = plc.GetDeviceProperties()
        if props.Status != "Success":
            return {
                "ip": ip,
                "vendor": "Unknown",
                "model": "Unknown",
                "firmware": "Unknown",
                "serial": "Unknown",
                "name": "Unknown",
                "reachable": False,
                "error": props.Status,
            }

        v = props.Value
        return {
            "ip": ip,
            "reachable": True,
            "vendor": v.Vendor or "Rockwell Automation",
            "model": v.ProductName or "Unknown",
            "firmware": getattr(v, 'Revision', None) or f"{getattr(v, 'Major', '?')}.{getattr(v, 'Minor', '?')}",
            "serial": hex(v.SerialNumber) if isinstance(getattr(v, 'SerialNumber', None), int) else str(getattr(v, 'SerialNumber', 'Unknown')),
            "name": v.ProductName or ip,
        }


@safe_plc_operation("read_tag")
def read_single_tag(ip, tag_name, tag_type="DINT", path="1,0", timeout=5000):
    """
    Read a single tag value from the PLC using pylogix.

    Args:
        ip: PLC IP address
        tag_name: Tag name (e.g., "Motor_Speed")
        tag_type: PLC4X type string (BOOL, SINT, INT, DINT, LINT, REAL, LREAL)
        path: CIP path
        timeout: connection timeout in ms

    Returns:
        {"name": tag_name, "value": value, "type": tag_type}
    """
    with _create_plc(ip, path, timeout) as plc:
        result = plc.Read(tag_name)
        if result.Status != "Success":
            raise PLCConnectionError(f"Read failed: {result.Status}")

        return {"name": tag_name, "value": result.Value, "type": tag_type}


@safe_plc_operation("batch_read")
def batch_read_tags(ip, tag_names, path="1,0", timeout=5000):
    """
    Read multiple tags in a single PLC connection.

    Args:
        ip: PLC IP address
        tag_names: list of tag name strings
        path: CIP path
        timeout: connection timeout in ms

    Returns:
        {"results": [{"name": ..., "value": ..., "status": ...}, ...]}
    """
    with _create_plc(ip, path, timeout) as plc:
        results = []
        for tag_name in tag_names:
            try:
                result = plc.Read(tag_name)
                results.append({
                    "name": tag_name,
                    "value": result.Value,
                    "status": result.Status,
                })
            except Exception as e:
                results.append({
                    "name": tag_name,
                    "value": None,
                    "status": str(e),
                })
        return {"results": results}


@safe_plc_operation("batch_write")
def batch_write_tags(ip, tag_value_pairs, path="1,0", timeout=5000):
    """
    Write multiple tags in a single PLC connection.

    Args:
        ip: PLC IP address
        tag_value_pairs: list of (tag_name, value) tuples
        path: CIP path
        timeout: connection timeout in ms
    """
    with _create_plc(ip, path, timeout) as plc:
        results = []
        for tag_name, value in tag_value_pairs:
            try:
                result = plc.Write(tag_name, value)
                results.append({
                    "name": tag_name,
                    "value": value,
                    "status": result.Status,
                })
            except Exception as e:
                results.append({
                    "name": tag_name,
                    "value": value,
                    "status": str(e),
                })
        return {"results": results}


@safe_plc_operation("write_tag")
def write_single_tag(ip, tag_name, value, path="1,0", timeout=5000):
    """
    Write a single tag value to the PLC using pylogix.

    Auto-detects numeric vs string vs bool values.
    """
    # Auto-cast value
    if isinstance(value, str):
        if value.lower() in ("true", "false"):
            value = value.lower() == "true"
        else:
            try:
                if "." in value:
                    value = float(value)
                else:
                    value = int(value)
            except ValueError:
                pass  # keep as string

    with _create_plc(ip, path, timeout) as plc:
        result = plc.Write(tag_name, value)
        if result.Status != "Success":
            raise PLCConnectionError(f"Write failed: {result.Status}")

        return {"name": tag_name, "value": value, "status": "ok"}


def get_plc_health(ip, path="1,0", timeout=5000):
    """
    Get comprehensive PLC health status.

    Rate-limits the composite operation as a whole (one check at the top),
    then sets _composite_flag.active so individual sub-calls (get_plc_identity,
    list_tags, list_programs) skip their own rate-limit checks. Each sub-call
    still acquires the global _connection_lock normally.

    Returns:
        {
            "ip": "192.168.1.40",
            "reachable": True,
            "identity": {...},
            "tagCount": 247,
            "programCount": 2,
            "programs": ["MainProgram", "SafetyProgram"],
            "connectionStats": {...}
        }
    """
    from plctag_safety import _composite_flag, check_rate_limit, get_connection_stats
    from plctag_discovery import list_tags, list_programs

    # Rate-limit the entire composite operation as one unit
    check_rate_limit(ip)

    result = {
        "ip": ip,
        "reachable": False,
        "identity": None,
        "tagCount": 0,
        "programCount": 0,
        "programs": [],
        "connectionStats": get_connection_stats(),
    }

    _composite_flag.active = True
    try:
        # Identity (also verifies connectivity)
        try:
            identity = get_plc_identity(ip, path, timeout)
            result["identity"] = identity
            result["reachable"] = identity.get("reachable", False)
        except Exception as e:
            result["identity"] = {"error": str(e), "reachable": False}
            return result

        # Tag count
        if result["reachable"]:
            try:
                tags = list_tags(ip, path, timeout)
                result["tagCount"] = len(tags)
            except Exception as e:
                log.warning(f"[get_plc_health] tag count failed: {e}")

        # Program list
        if result["reachable"]:
            try:
                programs = list_programs(ip, path, timeout)
                result["programs"] = programs
                result["programCount"] = len(programs)
            except Exception as e:
                log.warning(f"[get_plc_health] program list failed: {e}")
    finally:
        _composite_flag.active = False

    return result
