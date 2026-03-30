"""
Rockwell PLC Tag Discovery.

Uses pylogix for CIP tag browsing exclusively.

Connects directly to Rockwell PLCs via EtherNet/IP (CIP protocol) to:
- List all controller-scoped tags
- List program-scoped tags (per program)
- Browse UDT (User Defined Type) structures
- Detect array dimensions

All operations are read-only and short-lived (<2 seconds).
"""

import logging
from plctag_safety import safe_plc_operation, PLCConnectionError

log = logging.getLogger("plctag")


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _create_plc(ip, path="1,0", timeout=5000):
    """Create and configure a pylogix PLC instance."""
    from pylogix import PLC
    slot = int(path.split(",")[-1]) if "," in path else 0
    plc = PLC()
    plc.IPAddress = ip
    plc.ProcessorSlot = slot
    # SocketTimeout is the primary hard-timeout protection against hung CIP sessions.
    # Cap at 5 seconds regardless of the caller's timeout value.
    plc.SocketTimeout = min(timeout / 1000, 5)
    return plc


# ---------------------------------------------------------------------------
# Public API — all wrapped with @safe_plc_operation
# ---------------------------------------------------------------------------

@safe_plc_operation("tag_list")
def list_tags(ip, path="1,0", timeout=5000):
    """
    List ALL controller-scoped tags from a Rockwell PLC.

    Uses pylogix GetTagList.

    Args:
        ip: PLC IP address (e.g., "192.168.1.40")
        path: CIP path (default "1,0" = backplane slot 0)
        timeout: connection timeout in ms

    Returns:
        list of dicts: [{
            "name": "Motor_M101_Current",
            "type": "REAL",
            "isArray": False,
            "isUDT": False,
            "udtName": None,
            "program": None,
            "address": "%Motor_M101_Current:REAL",
        }, ...]
    """
    with _create_plc(ip, path, timeout) as plc:
        result = plc.GetTagList()
        if result.Status != "Success":
            raise PLCConnectionError(f"Tag list failed: {result.Status}")

        tags = []
        for t in result.Value:
            if t.TagName.startswith("__"):  # skip internal tags
                continue
            type_name = t.DataType or "Unknown"
            is_array = bool(t.Array)
            address = f"%{t.TagName}:{type_name}"
            if is_array:
                address = f"%{t.TagName}[0]:{type_name}"
            tags.append({
                "name": t.TagName,
                "type": type_name,
                "isArray": is_array,
                "isUDT": hasattr(t, 'DataTypeValue') and t.DataTypeValue > 0x0FFF,
                "udtName": type_name if hasattr(t, 'DataTypeValue') and t.DataTypeValue > 0x0FFF else None,
                "program": None,
                "address": address,
            })
        return tags


@safe_plc_operation("program_list")
def list_programs(ip, path="1,0", timeout=5000):
    """
    List all program names in the PLC.

    Uses pylogix GetProgramsList.

    Returns:
        list of program names: ["MainProgram", "SafetyProgram", ...]
    """
    with _create_plc(ip, path, timeout) as plc:
        result = plc.GetProgramsList()
        if result.Status != "Success":
            return []
        return result.Value or []


@safe_plc_operation("program_tags")
def list_program_tags(ip, program, path="1,0", timeout=5000):
    """
    List tags scoped to a specific program.

    Args:
        ip: PLC IP address
        program: Program name (e.g., "MainProgram")
        path: CIP path
        timeout: connection timeout in ms

    Returns:
        Same format as list_tags() but with program field set.
    """
    with _create_plc(ip, path, timeout) as plc:
        result = plc.GetProgramTagList(program)
        if result.Status != "Success":
            return []

        tags = []
        for t in result.Value:
            if t.TagName.startswith("__"):  # skip internal tags
                continue
            type_name = t.DataType or "Unknown"
            is_array = bool(t.Array)
            plc4x_type = type_name
            address = (
                f"%Program:{program}.{t.TagName}[0]:{plc4x_type}"
                if is_array
                else f"%Program:{program}.{t.TagName}:{plc4x_type}"
            )
            tags.append({
                "name": t.TagName,
                "type": type_name,
                "isArray": is_array,
                "isUDT": hasattr(t, 'DataTypeValue') and t.DataTypeValue > 0x0FFF,
                "udtName": type_name if hasattr(t, 'DataTypeValue') and t.DataTypeValue > 0x0FFF else None,
                "program": program,
                "address": address,
            })
        return tags


def discover_all(ip, path="1,0", timeout=5000):
    """
    Full discovery: controller tags + all program tags.

    Rate-limits the composite operation as a whole (one check at the top),
    then sets _composite_flag.active so individual sub-calls skip their own
    rate-limit checks. Each sub-call still acquires the global _connection_lock
    normally, ensuring only one CIP session is open at a time.

    Returns:
        {
            "ip": "192.168.1.40",
            "controller_tags": [...],
            "programs": [
                {"name": "MainProgram", "tags": [...]},
            ],
            "total_tags": 247,
            "total_programs": 2
        }
    """
    from plctag_safety import _composite_flag, check_rate_limit

    # Rate-limit the entire composite operation as one unit
    check_rate_limit(ip)

    result = {
        "ip": ip,
        "controller_tags": [],
        "programs": [],
        "total_tags": 0,
        "total_programs": 0,
    }

    _composite_flag.active = True
    try:
        # Get controller-scoped tags
        try:
            result["controller_tags"] = list_tags(ip, path, timeout)
        except Exception as e:
            log.warning(f"Controller tag listing failed: {e}")
            result["error"] = str(e)

        # Get programs only if controller tags succeeded
        if result["controller_tags"]:
            try:
                programs = list_programs(ip, path, timeout)
                for prog in programs:
                    try:
                        prog_tags = list_program_tags(ip, prog, path, timeout)
                        result["programs"].append({"name": prog, "tags": prog_tags})
                    except Exception as e:
                        log.warning(f"Program '{prog}' tag listing failed: {e}")
                        result["programs"].append({"name": prog, "tags": [], "error": str(e)})
            except Exception as e:
                log.warning(f"Program listing failed: {e}")
    finally:
        _composite_flag.active = False

    result["total_tags"] = len(result["controller_tags"]) + sum(
        len(p["tags"]) for p in result["programs"]
    )
    result["total_programs"] = len(result["programs"])

    return result
