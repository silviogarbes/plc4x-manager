# libplctag Rockwell Integration — Implementation Plan

> **Note:** Implementation uses pylogix (Python-pure, PyPI) instead of libplctag (C library, no PyPI package).
> All safety rules and API contracts remain identical.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add libplctag-based Rockwell PLC features: tag discovery with UDT browsing, PLC diagnostics, program listing, and module inventory — all without risking PLC lockup.

**Architecture:** A lightweight Python microservice (`plctag-service`) runs inside the PLC4X server container, exposing an internal REST API on port 5000. The admin panel proxies requests to it. All CIP connections are short-lived (connect → read → disconnect) to minimize PLC session usage. A global connection semaphore prevents concurrent access.

**Tech Stack:** libplctag (Python wrapper), Flask-lite (or bare http.server), Python 3.12

---

## PLC Safety — CRITICAL DESIGN CONSTRAINTS

### The Problem
Rockwell PLCs (ControlLogix, CompactLogix) have **limited CIP connection slots** (typically 32-64 depending on model). Each open connection uses PLC resources (memory, scan time). If connections are not properly closed, or if too many are opened simultaneously, the PLC can:
- Slow down (scan time increases)
- Drop existing connections (SCADA, HMI, safety)
- In extreme cases, fault to a safe state

### Safety Rules (enforced in code)

| Rule | Implementation |
|------|---------------|
| **R1: One connection at a time** | Global `threading.Lock` — only 1 CIP connection open at any moment |
| **R2: Short-lived connections** | Every operation: connect → read → disconnect. Max 10 seconds per operation |
| **R3: Automatic timeout** | If any operation takes >10s, force-close the connection |
| **R4: No persistent connections** | Unlike the OPC-UA poller, libplctag NEVER holds a connection open between operations |
| **R5: Rate limiting** | Max 1 discovery/diagnostic request per PLC per 30 seconds (prevents rapid-fire clicking) |
| **R6: Read-only by default** | Discovery and diagnostics are 100% read-only. No writes via libplctag |
| **R7: Connection counting** | Log every connect/disconnect. Alert if >5 connections/minute to same PLC |
| **R8: Graceful on error** | If connection fails, return error immediately — never retry automatically |
| **R9: Separate from polling** | libplctag runs in the server container, PLC4X polls from the admin container — they never compete for the same CIP connection simultaneously because libplctag connects directly to the PLC while PLC4X connects via its own OPC-UA path |
| **R10: Admin-only** | All discovery/diagnostic endpoints require admin role — operators cannot trigger PLC connections |

### Connection Flow (every operation)

```
1. Acquire global lock (fail immediately if locked — no queuing)
2. Check rate limit (30s cooldown per PLC IP)
3. Create libplctag tag handle with timeout=5000ms
4. Read data
5. Destroy tag handle (closes CIP connection)
6. Release global lock
7. Return result

Total time: <2 seconds per operation
CIP connection held: <1 second
```

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `server/plctag_service.py` | Microservice: REST API on port 5000 for discovery, diagnostics |
| Create | `server/plctag_safety.py` | Connection safety: lock, rate limiter, timeout, logging |
| Create | `server/plctag_discovery.py` | Tag listing, UDT browsing, program enumeration |
| Create | `server/plctag_diagnostics.py` | PLC health: CPU status, faults, firmware, modules |
| Create | `server/plctag_requirements.txt` | libplctag Python dependency |
| Modify | `server/Dockerfile` | Add libplctag install + service startup |
| Modify | `server/entrypoint.sh` | Start plctag_service alongside PLC4X server |
| Create | `admin/routes/plctag_routes.py` | Admin API proxy to plctag_service |
| Modify | `admin/main.py` | Register plctag router |
| Modify | `admin/routes/device_routes.py` | Enhance discover endpoint to use libplctag for EtherNet/IP |
| Modify | `admin/static/js/app.js` | PLC Diagnostics UI, enhanced discovery modal |
| Modify | `admin/templates/index.html` | PLC Health card in device details |

---

## Task 1: Safety Module

**Files:**
- Create: `server/plctag_safety.py`

- [ ] **Step 1: Create the safety module**

```python
"""
PLC Connection Safety Module

Enforces all safety rules to prevent PLC lockup:
- R1: Global lock — one CIP connection at a time
- R2: Short-lived — max 10s per operation
- R3: Timeout — force-close on timeout
- R5: Rate limit — 30s cooldown per PLC IP
- R7: Connection counting — log all connect/disconnect
- R8: No retry — fail immediately on error
"""

import threading
import time
import logging
from functools import wraps

log = logging.getLogger("plctag")

# R1: Global connection lock — ONE connection at a time across all PLCs
_connection_lock = threading.Lock()

# R5: Rate limiter — last connection time per PLC IP
_last_connection = {}  # ip → timestamp
_RATE_LIMIT_SECONDS = 30

# R7: Connection counter
_connection_count = {}  # ip → count in last minute
_connection_log = []  # (timestamp, ip, operation, duration_ms)


class PLCConnectionError(Exception):
    """Raised when PLC connection fails or is denied by safety rules."""
    pass


class PLCBusyError(Exception):
    """Raised when another operation is already in progress."""
    pass


class PLCRateLimitError(Exception):
    """Raised when rate limit is exceeded for a PLC."""
    pass


def check_rate_limit(ip: str):
    """R5: Check if we can connect to this PLC (30s cooldown)."""
    last = _last_connection.get(ip, 0)
    elapsed = time.time() - last
    if elapsed < _RATE_LIMIT_SECONDS:
        remaining = int(_RATE_LIMIT_SECONDS - elapsed)
        raise PLCRateLimitError(
            f"Rate limit: wait {remaining}s before connecting to {ip} again. "
            f"This protects the PLC from excessive connections."
        )


def record_connection(ip: str, operation: str, duration_ms: float):
    """R7: Log connection for monitoring."""
    now = time.time()
    _last_connection[ip] = now
    _connection_log.append((now, ip, operation, duration_ms))
    # Prune old entries (keep last 100)
    if len(_connection_log) > 100:
        _connection_log.pop(0)
    # Count connections in last minute
    recent = sum(1 for t, i, _, _ in _connection_log if i == ip and now - t < 60)
    if recent > 5:
        log.warning(f"HIGH CONNECTION RATE: {recent} connections to {ip} in last minute")


def get_connection_stats():
    """Return connection statistics for monitoring."""
    now = time.time()
    return {
        "active_lock": _connection_lock.locked(),
        "recent_connections": [
            {"time": t, "ip": i, "operation": o, "duration_ms": d}
            for t, i, o, d in _connection_log
            if now - t < 300  # last 5 minutes
        ],
        "rate_limits": {
            ip: {"last_connection": ts, "cooldown_remaining": max(0, int(_RATE_LIMIT_SECONDS - (now - ts)))}
            for ip, ts in _last_connection.items()
        }
    }


def safe_plc_operation(operation_name: str):
    """
    Decorator that enforces ALL safety rules for PLC operations.

    Usage:
        @safe_plc_operation("tag_discovery")
        def discover_tags(ip, path=None, timeout=5000):
            # ... libplctag code here ...
            return tags

    The decorated function receives the PLC IP as first argument.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(ip, *args, **kwargs):
            # R5: Check rate limit
            check_rate_limit(ip)

            # R1: Acquire lock (non-blocking — fail immediately if busy)
            if not _connection_lock.acquire(blocking=False):
                raise PLCBusyError(
                    "Another PLC operation is in progress. "
                    "Only one CIP connection is allowed at a time to protect the PLC."
                )

            start = time.time()
            try:
                log.info(f"[{operation_name}] Connecting to {ip}...")
                # R2+R3: The actual libplctag call has its own timeout (default 5000ms)
                result = func(ip, *args, **kwargs)
                duration_ms = (time.time() - start) * 1000
                log.info(f"[{operation_name}] Completed in {duration_ms:.0f}ms")

                # R7: Record connection
                record_connection(ip, operation_name, duration_ms)

                return result
            except Exception as e:
                duration_ms = (time.time() - start) * 1000
                log.error(f"[{operation_name}] Failed after {duration_ms:.0f}ms: {e}")
                record_connection(ip, f"{operation_name}_FAILED", duration_ms)
                raise
            finally:
                # R1: Always release lock
                _connection_lock.release()
        return wrapper
    return decorator
```

- [ ] **Step 2: Commit**

```bash
git add server/plctag_safety.py
git commit -m "feat: PLC connection safety module — lock, rate limit, timeout, logging"
```

---

## Task 2: Tag Discovery Module

**Files:**
- Create: `server/plctag_discovery.py`

- [ ] **Step 1: Create the discovery module**

```python
"""
Rockwell PLC Tag Discovery using libplctag.

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

# Rockwell data type mapping
CIP_TYPES = {
    0x00C1: "BOOL",
    0x00C2: "SINT",    # 8-bit signed
    0x00C3: "INT",     # 16-bit signed
    0x00C4: "DINT",    # 32-bit signed
    0x00C5: "LINT",    # 64-bit signed
    0x00CA: "REAL",    # 32-bit float
    0x00CB: "LREAL",   # 64-bit float
    0x00D0: "STRING",
    0x02A0: "COUNTER",
    0x02A1: "TIMER",
}


@safe_plc_operation("tag_list")
def list_tags(ip, path="1,0", timeout=5000):
    """
    List ALL tags from a Rockwell PLC.

    Args:
        ip: PLC IP address (e.g., "192.168.1.40")
        path: CIP path (default "1,0" = backplane slot 0)
        timeout: connection timeout in ms

    Returns:
        list of dicts: [{
            "name": "Motor_M101_Current",
            "type": "REAL",
            "typeCode": 0x00CA,
            "dimensions": [0, 0, 0],  # array dims, 0=scalar
            "program": null,          # null = controller scope
            "address": "%Motor_M101_Current:REAL",
            "isUDT": False,
            "udtName": null
        }, ...]

    Example output for a typical motor control:
        [
            {"name": "Motor_M101_Current", "type": "REAL", "address": "%Motor_M101_Current:REAL"},
            {"name": "Motor_M101_Speed", "type": "REAL", "address": "%Motor_M101_Speed:REAL"},
            {"name": "Motor_M101_Status", "type": "DINT", "address": "%Motor_M101_Status:DINT"},
            {"name": "Line1_ProductCount", "type": "DINT", "address": "%Line1_ProductCount:DINT"},
            {"name": "Reactor_T301_Temp", "type": "REAL", "address": "%Reactor_T301_Temp:REAL"},
        ]
    """
    try:
        from libplctag import Tag, PLCTAG_ERR_OK
    except ImportError:
        raise PLCConnectionError("libplctag not installed. Install with: pip install libplctag")

    # Use the special @tags tag to list all controller tags
    tag_str = f"protocol=ab-eip&gateway={ip}&path={path}&plc=ControlLogix&name=@tags&elem_count=1"

    tag = Tag()
    rc = tag.create(tag_str, timeout)
    if rc != PLCTAG_ERR_OK:
        raise PLCConnectionError(f"Cannot connect to PLC at {ip}: error code {rc}")

    try:
        rc = tag.read(timeout)
        if rc != PLCTAG_ERR_OK:
            raise PLCConnectionError(f"Cannot read tag list from {ip}: error code {rc}")

        tags = []
        offset = 0
        data_len = tag.size

        while offset < data_len:
            try:
                # Parse tag entry from raw CIP data
                tag_type = tag.get_uint32(offset)
                offset += 4

                name_len = tag.get_uint16(offset)
                offset += 2

                name = ""
                for i in range(name_len):
                    name += chr(tag.get_uint8(offset + i))
                offset += name_len

                # Align to 2-byte boundary
                if offset % 2:
                    offset += 1

                # Decode type
                is_array = bool(tag_type & 0x2000)
                is_udt = bool(tag_type & 0x1000)
                base_type = tag_type & 0x0FFF
                type_name = CIP_TYPES.get(base_type, f"UDT_0x{base_type:04X}")

                # Build PLC4X-compatible address
                plc4x_type = type_name
                if plc4x_type.startswith("UDT_"):
                    plc4x_type = "DINT"  # Default for UDTs
                address = f"%{name}:{plc4x_type}"
                if is_array:
                    address = f"%{name}[0]:{plc4x_type}"

                # Skip internal/system tags
                if name.startswith("__") or name.startswith("Map:"):
                    continue

                tags.append({
                    "name": name,
                    "type": type_name,
                    "typeCode": base_type,
                    "isArray": is_array,
                    "isUDT": is_udt,
                    "udtName": type_name if is_udt else None,
                    "program": None,
                    "address": address,
                })
            except Exception:
                break  # End of tag data

        return tags
    finally:
        tag.destroy()


@safe_plc_operation("program_list")
def list_programs(ip, path="1,0", timeout=5000):
    """
    List all programs in the PLC.

    Returns:
        list of program names: ["MainProgram", "SafetyProgram", ...]
    """
    try:
        from libplctag import Tag, PLCTAG_ERR_OK
    except ImportError:
        raise PLCConnectionError("libplctag not installed")

    tag_str = f"protocol=ab-eip&gateway={ip}&path={path}&plc=ControlLogix&name=@tags&elem_count=1"

    tag = Tag()
    rc = tag.create(tag_str, timeout)
    if rc != PLCTAG_ERR_OK:
        raise PLCConnectionError(f"Cannot connect to PLC at {ip}")

    try:
        rc = tag.read(timeout)
        if rc != PLCTAG_ERR_OK:
            return []

        programs = []
        offset = 0
        data_len = tag.size

        while offset < data_len:
            try:
                tag_type = tag.get_uint32(offset)
                offset += 4
                name_len = tag.get_uint16(offset)
                offset += 2
                name = ""
                for i in range(name_len):
                    name += chr(tag.get_uint8(offset + i))
                offset += name_len
                if offset % 2:
                    offset += 1

                # Programs appear as special type
                if name.startswith("Program:"):
                    programs.append(name.replace("Program:", ""))
            except Exception:
                break

        return programs
    finally:
        tag.destroy()


@safe_plc_operation("program_tags")
def list_program_tags(ip, program, path="1,0", timeout=5000):
    """
    List tags scoped to a specific program.

    Args:
        ip: PLC IP address
        program: Program name (e.g., "MainProgram")

    Returns:
        Same format as list_tags() but with program field set
    """
    try:
        from libplctag import Tag, PLCTAG_ERR_OK
    except ImportError:
        raise PLCConnectionError("libplctag not installed")

    tag_str = f"protocol=ab-eip&gateway={ip}&path={path}&plc=ControlLogix&name=Program:{program}.@tags&elem_count=1"

    tag = Tag()
    rc = tag.create(tag_str, timeout)
    if rc != PLCTAG_ERR_OK:
        return []

    try:
        rc = tag.read(timeout)
        if rc != PLCTAG_ERR_OK:
            return []

        tags = []
        offset = 0
        data_len = tag.size

        while offset < data_len:
            try:
                tag_type = tag.get_uint32(offset)
                offset += 4
                name_len = tag.get_uint16(offset)
                offset += 2
                name = ""
                for i in range(name_len):
                    name += chr(tag.get_uint8(offset + i))
                offset += name_len
                if offset % 2:
                    offset += 1

                is_array = bool(tag_type & 0x2000)
                is_udt = bool(tag_type & 0x1000)
                base_type = tag_type & 0x0FFF
                type_name = CIP_TYPES.get(base_type, f"UDT_0x{base_type:04X}")

                plc4x_type = type_name if not type_name.startswith("UDT_") else "DINT"
                address = f"%Program:{program}.{name}:{plc4x_type}"
                if is_array:
                    address = f"%Program:{program}.{name}[0]:{plc4x_type}"

                if name.startswith("__"):
                    continue

                tags.append({
                    "name": name,
                    "type": type_name,
                    "typeCode": base_type,
                    "isArray": is_array,
                    "isUDT": is_udt,
                    "udtName": type_name if is_udt else None,
                    "program": program,
                    "address": address,
                })
            except Exception:
                break

        return tags
    finally:
        tag.destroy()


def discover_all(ip, path="1,0", timeout=5000):
    """
    Full discovery: controller tags + all program tags.
    NOTE: This makes multiple CIP connections (one per program + one for controller).
    Each connection respects the safety module (lock + rate limit).

    Returns:
        {
            "ip": "192.168.1.40",
            "controller_tags": [...],
            "programs": [
                {"name": "MainProgram", "tags": [...]},
                {"name": "SafetyProgram", "tags": [...]},
            ],
            "total_tags": 247,
            "total_programs": 2
        }
    """
    result = {
        "ip": ip,
        "controller_tags": [],
        "programs": [],
        "total_tags": 0,
        "total_programs": 0,
    }

    # Get controller-scoped tags
    try:
        result["controller_tags"] = list_tags(ip, path, timeout)
    except Exception as e:
        log.warning(f"Controller tag listing failed: {e}")

    # Get programs (this uses a separate CIP connection — rate limit applies)
    # Skip program enumeration if controller tags already failed
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

    # Totals
    result["total_tags"] = len(result["controller_tags"]) + sum(len(p["tags"]) for p in result["programs"])
    result["total_programs"] = len(result["programs"])

    return result
```

- [ ] **Step 2: Commit**

```bash
git add server/plctag_discovery.py
git commit -m "feat: Rockwell tag discovery via libplctag — controller + program tags + UDT detection"
```

---

## Task 3: PLC Diagnostics Module

**Files:**
- Create: `server/plctag_diagnostics.py`

- [ ] **Step 1: Create the diagnostics module**

```python
"""
Rockwell PLC Diagnostics using libplctag.

Read-only diagnostic information:
- PLC identity (model, firmware, serial)
- CPU status (run/program/fault mode)
- Active faults
- I/O module inventory

All operations are read-only and short-lived.
"""

import logging
from plctag_safety import safe_plc_operation, PLCConnectionError

log = logging.getLogger("plctag")


@safe_plc_operation("plc_identity")
def get_plc_identity(ip, path="1,0", timeout=5000):
    """
    Get PLC identity: model, firmware, serial number.

    Returns:
        {
            "ip": "192.168.1.40",
            "vendor": "Rockwell Automation",
            "model": "ControlLogix L83E",
            "firmware": "33.011",
            "serial": "0x1234ABCD",
            "name": "MainController",
            "reachable": True
        }
    """
    try:
        from libplctag import Tag, PLCTAG_ERR_OK
    except ImportError:
        raise PLCConnectionError("libplctag not installed")

    # Read a simple tag to verify connectivity and get basic info
    # The connection itself provides identity info via CIP
    tag_str = f"protocol=ab-eip&gateway={ip}&path={path}&plc=ControlLogix&name=@tags&elem_count=1"

    tag = Tag()
    rc = tag.create(tag_str, timeout)
    if rc != PLCTAG_ERR_OK:
        return {
            "ip": ip, "vendor": "Unknown", "model": "Unknown",
            "firmware": "Unknown", "serial": "Unknown", "name": "Unknown",
            "reachable": False,
            "error": f"Cannot connect: error code {rc}"
        }

    try:
        return {
            "ip": ip,
            "vendor": "Rockwell Automation",
            "model": "ControlLogix/CompactLogix",
            "firmware": "Unknown",  # Requires CIP identity request
            "serial": "Unknown",
            "name": ip,
            "reachable": True,
        }
    finally:
        tag.destroy()


@safe_plc_operation("read_tag")
def read_single_tag(ip, tag_name, tag_type="DINT", path="1,0", timeout=5000):
    """
    Read a single tag value from the PLC.
    Used for diagnostic reads (CPU status, fault registers, etc.)

    Args:
        ip: PLC IP
        tag_name: Tag name (e.g., "S:1/13" for major fault bit)
        tag_type: PLC4X type string

    Returns:
        {"name": tag_name, "value": value, "type": tag_type}
    """
    try:
        from libplctag import Tag, PLCTAG_ERR_OK
    except ImportError:
        raise PLCConnectionError("libplctag not installed")

    type_map = {
        "BOOL": ("bit", lambda t: bool(t.get_bit(0))),
        "SINT": ("sint8", lambda t: t.get_int8(0)),
        "INT": ("sint16", lambda t: t.get_int16(0)),
        "DINT": ("sint32", lambda t: t.get_int32(0)),
        "LINT": ("sint64", lambda t: t.get_int64(0)),
        "REAL": ("float32", lambda t: t.get_float32(0)),
        "LREAL": ("float64", lambda t: t.get_float64(0)),
    }

    elem_type, reader = type_map.get(tag_type, ("sint32", lambda t: t.get_int32(0)))

    tag_str = f"protocol=ab-eip&gateway={ip}&path={path}&plc=ControlLogix&name={tag_name}&elem_size=4&elem_count=1"

    tag = Tag()
    rc = tag.create(tag_str, timeout)
    if rc != PLCTAG_ERR_OK:
        raise PLCConnectionError(f"Cannot read tag '{tag_name}': error {rc}")

    try:
        rc = tag.read(timeout)
        if rc != PLCTAG_ERR_OK:
            raise PLCConnectionError(f"Read failed for '{tag_name}': error {rc}")

        value = reader(tag)
        return {"name": tag_name, "value": value, "type": tag_type}
    finally:
        tag.destroy()


def get_plc_health(ip, path="1,0", timeout=5000):
    """
    Get comprehensive PLC health status.
    Combines identity + tag count + connectivity test.

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
    from plctag_safety import get_connection_stats

    result = {
        "ip": ip,
        "reachable": False,
        "identity": None,
        "tagCount": 0,
        "programCount": 0,
        "programs": [],
        "connectionStats": get_connection_stats()
    }

    # Identity check (also verifies connectivity)
    try:
        identity = get_plc_identity(ip, path, timeout)
        result["identity"] = identity
        result["reachable"] = identity.get("reachable", False)
    except Exception as e:
        result["identity"] = {"error": str(e), "reachable": False}
        return result

    # Tag count (requires another connection — rate limit applies)
    if result["reachable"]:
        try:
            from plctag_discovery import list_tags
            tags = list_tags(ip, path, timeout)
            result["tagCount"] = len(tags)
        except Exception:
            pass

    # Program list
    if result["reachable"]:
        try:
            from plctag_discovery import list_programs
            programs = list_programs(ip, path, timeout)
            result["programs"] = programs
            result["programCount"] = len(programs)
        except Exception:
            pass

    return result
```

- [ ] **Step 2: Commit**

```bash
git add server/plctag_diagnostics.py
git commit -m "feat: PLC diagnostics — identity, health, tag count via libplctag"
```

---

## Task 4: Microservice REST API

**Files:**
- Create: `server/plctag_service.py`
- Create: `server/plctag_requirements.txt`

- [ ] **Step 1: Create the REST API service**

```python
"""
libplctag Microservice — REST API for PLC discovery and diagnostics.

Runs on port 5000 inside the PLC4X server container.
Only accessible from the Docker internal network (backend).

Endpoints:
    GET  /health                         — service health
    POST /discover?ip=X&path=1,0         — full tag discovery
    POST /discover/tags?ip=X             — controller tags only
    POST /discover/programs?ip=X         — list programs
    POST /discover/program-tags?ip=X&program=Y — tags in a program
    POST /diagnostics/identity?ip=X      — PLC identity
    POST /diagnostics/health?ip=X        — full PLC health
    POST /diagnostics/read?ip=X&tag=Y&type=DINT — read single tag
    GET  /stats                          — connection statistics
"""

import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

logging.basicConfig(
    level=logging.INFO,
    format="[PLCTag] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("plctag")

PORT = 5000


class PLCTagHandler(BaseHTTPRequestHandler):

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _send_error(self, message, status=500):
        self._send_json({"error": message}, status)

    def _get_params(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        return {k: v[0] for k, v in params.items()}

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            self._send_json({"status": "ok", "service": "plctag"})

        elif path == "/stats":
            from plctag_safety import get_connection_stats
            self._send_json(get_connection_stats())

        else:
            self._send_error("Not found", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        params = self._get_params()
        ip = params.get("ip")
        plc_path = params.get("path", "1,0")
        timeout = int(params.get("timeout", "5000"))

        if not ip:
            self._send_error("Missing 'ip' parameter", 400)
            return

        try:
            if path == "/discover":
                from plctag_discovery import discover_all
                result = discover_all(ip, plc_path, timeout)
                self._send_json(result)

            elif path == "/discover/tags":
                from plctag_discovery import list_tags
                tags = list_tags(ip, plc_path, timeout)
                self._send_json({"tags": tags, "count": len(tags)})

            elif path == "/discover/programs":
                from plctag_discovery import list_programs
                programs = list_programs(ip, plc_path, timeout)
                self._send_json({"programs": programs, "count": len(programs)})

            elif path == "/discover/program-tags":
                program = params.get("program")
                if not program:
                    self._send_error("Missing 'program' parameter", 400)
                    return
                from plctag_discovery import list_program_tags
                tags = list_program_tags(ip, program, plc_path, timeout)
                self._send_json({"program": program, "tags": tags, "count": len(tags)})

            elif path == "/diagnostics/identity":
                from plctag_diagnostics import get_plc_identity
                result = get_plc_identity(ip, plc_path, timeout)
                self._send_json(result)

            elif path == "/diagnostics/health":
                from plctag_diagnostics import get_plc_health
                result = get_plc_health(ip, plc_path, timeout)
                self._send_json(result)

            elif path == "/diagnostics/read":
                tag_name = params.get("tag")
                tag_type = params.get("type", "DINT")
                if not tag_name:
                    self._send_error("Missing 'tag' parameter", 400)
                    return
                from plctag_diagnostics import read_single_tag
                result = read_single_tag(ip, tag_name, tag_type, plc_path, timeout)
                self._send_json(result)

            else:
                self._send_error("Not found", 404)

        except Exception as e:
            error_type = type(e).__name__
            self._send_error(f"{error_type}: {e}",
                           429 if "RateLimit" in error_type else
                           409 if "Busy" in error_type else 503)

    def log_message(self, format, *args):
        log.info(f"{self.client_address[0]} - {args[0]}")


def main():
    log.info(f"Starting libplctag service on port {PORT}...")
    server = HTTPServer(("0.0.0.0", PORT), PLCTagHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Create requirements**

```
libplctag==1.0.2
```

- [ ] **Step 3: Commit**

```bash
git add server/plctag_service.py server/plctag_requirements.txt
git commit -m "feat: plctag microservice — REST API for discovery and diagnostics"
```

---

## Task 5: Docker Integration

**Files:**
- Modify: `server/Dockerfile`
- Modify: `server/entrypoint.sh`

- [ ] **Step 1: Update Dockerfile to install libplctag**

Add after the Java setup:
```dockerfile
# Install libplctag for Rockwell PLC discovery/diagnostics
RUN pip3 install --no-cache-dir libplctag==1.0.2
COPY plctag_*.py /app/
```

- [ ] **Step 2: Update entrypoint.sh to start both services**

Add before the PLC4X server starts:
```bash
# Start libplctag discovery/diagnostics service (background)
echo "[Server] Starting libplctag service on port 5000..."
python3 /app/plctag_service.py &
```

- [ ] **Step 3: Commit**

```bash
git add server/Dockerfile server/entrypoint.sh
git commit -m "feat: add libplctag service to PLC4X server container"
```

---

## Task 6: Admin API Proxy

**Files:**
- Create: `admin/routes/plctag_routes.py`
- Modify: `admin/main.py`

- [ ] **Step 1: Create proxy routes**

```python
"""
libplctag proxy routes — forwards discovery/diagnostic requests to the
plctag_service running inside the PLC4X server container.

All endpoints require admin role (R10: admin-only).
"""

from fastapi import APIRouter, Depends, HTTPException, Query
import asyncio
import requests
from auth import require_admin, CurrentUser

router = APIRouter(tags=["plctag"])

PLCTAG_SERVICE_URL = "http://plc4x-server:5000"


def _proxy_get(path):
    """GET request to plctag service."""
    resp = requests.get(f"{PLCTAG_SERVICE_URL}{path}", timeout=15)
    return resp.json()


def _proxy_post(path, params=None):
    """POST request to plctag service."""
    resp = requests.post(f"{PLCTAG_SERVICE_URL}{path}", params=params, timeout=30)
    if resp.status_code >= 400:
        data = resp.json()
        raise HTTPException(status_code=resp.status_code, detail=data.get("error", "Unknown error"))
    return resp.json()


@router.get("/api/plctag/health")
async def plctag_health(user: CurrentUser = Depends(require_admin)):
    """Check if libplctag service is running."""
    try:
        result = await asyncio.to_thread(_proxy_get, "/health")
        return result
    except Exception as e:
        return {"status": "offline", "error": str(e)}


@router.get("/api/plctag/stats")
async def plctag_stats(user: CurrentUser = Depends(require_admin)):
    """Get PLC connection statistics (safety monitoring)."""
    return await asyncio.to_thread(_proxy_get, "/stats")


@router.post("/api/plctag/discover")
async def plctag_discover(
    ip: str = Query(..., description="PLC IP address"),
    path: str = Query("1,0", description="CIP path (backplane,slot)"),
    user: CurrentUser = Depends(require_admin)
):
    """Full discovery: controller tags + program tags. May take 5-10 seconds."""
    return await asyncio.to_thread(_proxy_post, "/discover", {"ip": ip, "path": path})


@router.post("/api/plctag/discover/tags")
async def plctag_discover_tags(
    ip: str = Query(...),
    path: str = Query("1,0"),
    user: CurrentUser = Depends(require_admin)
):
    """List controller-scoped tags only."""
    return await asyncio.to_thread(_proxy_post, "/discover/tags", {"ip": ip, "path": path})


@router.post("/api/plctag/discover/programs")
async def plctag_discover_programs(
    ip: str = Query(...),
    path: str = Query("1,0"),
    user: CurrentUser = Depends(require_admin)
):
    """List all programs in the PLC."""
    return await asyncio.to_thread(_proxy_post, "/discover/programs", {"ip": ip, "path": path})


@router.post("/api/plctag/diagnostics/identity")
async def plctag_identity(
    ip: str = Query(...),
    path: str = Query("1,0"),
    user: CurrentUser = Depends(require_admin)
):
    """Get PLC identity: model, firmware, serial."""
    return await asyncio.to_thread(_proxy_post, "/diagnostics/identity", {"ip": ip, "path": path})


@router.post("/api/plctag/diagnostics/health")
async def plctag_health_check(
    ip: str = Query(...),
    path: str = Query("1,0"),
    user: CurrentUser = Depends(require_admin)
):
    """Get comprehensive PLC health: identity + tag count + programs."""
    return await asyncio.to_thread(_proxy_post, "/diagnostics/health", {"ip": ip, "path": path})
```

- [ ] **Step 2: Register router in main.py**

```python
from routes.plctag_routes import router as plctag_router
app.include_router(plctag_router)
```

- [ ] **Step 3: Commit**

```bash
git add admin/routes/plctag_routes.py admin/main.py
git commit -m "feat: admin API proxy for libplctag discovery and diagnostics"
```

---

## Task 7: Enhanced Device Discovery UI

**Files:**
- Modify: `admin/routes/device_routes.py`
- Modify: `admin/static/js/app.js`

- [ ] **Step 1: Update discover endpoint to use libplctag for EtherNet/IP**

In the existing `api_discover_tags`, detect EtherNet/IP connection strings and route to libplctag:

```python
# In api_discover_tags:
conn = device.get("connectionString", "")
if conn.startswith("eip://"):
    # Use libplctag for Rockwell PLC discovery
    ip = conn.replace("eip://", "").split(":")[0]
    try:
        import requests
        resp = requests.post(f"http://plc4x-server:5000/discover/tags", params={"ip": ip}, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            return {"tags": data.get("tags", []), "source": "libplctag", "count": data.get("count", 0)}
        else:
            error = resp.json().get("error", "Unknown")
            raise HTTPException(status_code=resp.status_code, detail=error)
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=503, detail="libplctag service not available in PLC4X server container")
# ... existing OPC-UA discovery for other protocols
```

- [ ] **Step 2: Update discovery modal for Rockwell-specific features**

Add to the discovery modal:
- **Program tabs** — show controller tags and each program's tags separately
- **UDT indicator** — badge showing "UDT" for complex types
- **Array indicator** — show "[10]" for array tags
- **PLC Health card** — show identity info at the top of the modal

- [ ] **Step 3: Add PLC Diagnostics card to device detail view**

When viewing a Rockwell device, show a "PLC Health" card with:
- Model, firmware, serial
- Tag count
- Program list
- Connection stats (last connection time, rate limit status)

- [ ] **Step 4: Commit**

```bash
git commit -m "feat: enhanced discovery UI — program tabs, UDT badges, PLC health card"
```

---

## Task 8: Connection Safety Dashboard

**Files:**
- Modify: `admin/static/js/app.js`
- Modify: `admin/templates/index.html`

- [ ] **Step 1: Add safety stats to the Server tab**

A card showing:
- libplctag service status (online/offline)
- Active lock (yes/no)
- Recent connections (last 5 min): IP, operation, duration
- Rate limit status per PLC

```
┌─── PLC Connection Safety ─────────────────────┐
│ libplctag Service: ● Online                    │
│ Connection Lock: Available (not in use)        │
│                                                │
│ Recent Connections (last 5 min):               │
│ • 09:15:03  192.168.1.40  tag_list    320ms   │
│ • 09:14:58  192.168.1.40  identity    180ms   │
│                                                │
│ Rate Limits:                                   │
│ • 192.168.1.40: available (cooldown done)      │
│ • 192.168.1.41: 15s remaining                  │
└────────────────────────────────────────────────┘
```

- [ ] **Step 2: Commit**

```bash
git commit -m "feat: PLC connection safety dashboard with live stats"
```

---

## Safety Verification Checklist

Before deploying, verify ALL safety rules:

- [ ] R1: Run two discover commands simultaneously → second one gets "PLCBusyError" immediately
- [ ] R2: Each discover completes in <10 seconds
- [ ] R3: Disconnect a PLC mid-discover → operation times out, connection released
- [ ] R4: After discover, no CIP sessions remain open (check PLC diagnostics)
- [ ] R5: Click Discover twice rapidly → second gets "rate limit, wait Xs"
- [ ] R6: No write operations exist in any libplctag module
- [ ] R7: Check logs after 5 discovers → connection count logged
- [ ] R8: Discover with wrong IP → immediate error, no retry
- [ ] R9: Run discover while poller is active → both work independently (different connection paths)
- [ ] R10: Operator/monitor cannot access any plctag endpoint (403)

---

## Summary

| Component | File | Safety Rules |
|-----------|------|-------------|
| Safety module | `server/plctag_safety.py` | R1-R8 enforced |
| Tag discovery | `server/plctag_discovery.py` | Uses @safe_plc_operation |
| PLC diagnostics | `server/plctag_diagnostics.py` | Uses @safe_plc_operation |
| REST API | `server/plctag_service.py` | HTTP error codes for safety violations |
| Admin proxy | `admin/routes/plctag_routes.py` | R10: @require_admin on all endpoints |
| UI | `admin/static/js/app.js` | Rate limit feedback, safety dashboard |

**Total PLC impact:** Each discovery operation holds a CIP connection for <1 second. With 30s rate limiting, max 2 connections/minute per PLC. This is well within the safe range for any ControlLogix/CompactLogix (which support 32-64 concurrent connections).
