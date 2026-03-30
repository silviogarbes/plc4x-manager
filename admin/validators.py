"""
Shared validation helpers for PLC4X Manager FastAPI.

Used by config_routes.py and device_routes.py.
"""

from __future__ import annotations

import re


_NAME_RE = re.compile(r'^[a-zA-Z0-9._-]{1,128}$')


def _validate_name(name: str) -> bool:
    """Validates device/tag/user names. Returns True if valid."""
    return bool(name) and bool(_NAME_RE.match(name))


def _validate_tag_address(address: str, connection_string: str = "") -> tuple[bool, str | None]:
    """Validates a PLC4X tag address. Returns (ok, error_message)."""
    if not address or not isinstance(address, str):
        return False, "Tag address cannot be empty"
    address = address.strip()

    # Modbus register address must be >= 1
    modbus_pattern = re.match(
        r'^(holding-register|input-register|coil|discrete-input):(\d+)', address
    )
    if modbus_pattern:
        reg_type = modbus_pattern.group(1)
        reg_addr = int(modbus_pattern.group(2))
        if reg_addr < 1:
            return False, f"Modbus {reg_type} address must be >= 1 (got {reg_addr})"

    # OPC-UA format validation
    conn_lower = connection_string.lower() if connection_string else ""
    if "opcua" in conn_lower or address.startswith("ns=") or address.startswith("nsu="):
        if address.startswith("nsu="):
            return False, "PLC4X does not support 'nsu=' format. Use 'ns=N;i=ID' or 'ns=N;s=NAME' instead"
        if address.startswith("ns=") and not re.match(r'^ns=\d+;[isgb]=.+', address):
            return False, "Invalid OPC-UA address format. Use 'ns=N;i=ID' or 'ns=N;s=NAME'"

    return True, None


def _validate_device_tags(device: dict) -> list[str]:
    """Validates all tags in a device. Returns list of error strings."""
    errors: list[str] = []
    conn = device.get("connectionString", "")
    for tag in device.get("tags", []):
        alias = tag.get("alias", "unknown")
        address = tag.get("address", "")
        ok, msg = _validate_tag_address(address, conn)
        if not ok:
            errors.append(f"Tag '{alias}': {msg}")
    return errors
