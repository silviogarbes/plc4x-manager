"""
PLC Connection Safety Module

Enforces all safety rules to prevent PLC lockup:
- R1: Global lock — one CIP connection at a time
- R2: Short-lived — max 10s per operation
- R3: Timeout — force-close on timeout (SocketTimeout=3s is the primary mechanism)
- R5: Rate limit — 30s cooldown per PLC IP
- R7: Connection counting — log all connect/disconnect
- R8: No retry — fail immediately on error
"""

import os
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
_connection_log = []   # (timestamp, ip, operation, duration_ms)

# Stats lock — separate from _connection_lock to avoid deadlock
# Use this for reads/writes of _last_connection and _connection_log
_stats_lock = threading.Lock()

# Thread-local flag set by composite operations (discover_all, get_plc_health)
# When active, check_rate_limit is skipped for individual sub-calls because the
# composite function already rate-checked the whole operation.
_composite_flag = threading.local()


class PLCConnectionError(Exception):
    """Raised when PLC connection fails or is denied by safety rules."""
    pass


class PLCBusyError(Exception):
    """Raised when another operation is already in progress."""
    pass


class PLCRateLimitError(Exception):
    """Raised when rate limit is exceeded for a PLC."""
    pass


class PLCReadOnlyError(Exception):
    """Raised when system is in read-only mode and a write is attempted."""
    pass


def is_plc_readonly():
    """Check if the system is in global read-only mode (PLC_READONLY=true)."""
    return os.environ.get("PLC_READONLY", "").lower() in ("true", "1", "yes")


def check_write_allowed():
    """Raise PLCReadOnlyError if system is in read-only mode.

    Reserved for future use — call before any PLC write operation added to
    the plctag_service. Currently all plctag endpoints are read-only
    (discovery/diagnostics). The primary write guard is in admin/routes/live_routes.py.
    """
    if is_plc_readonly():
        raise PLCReadOnlyError(
            "System is in read-only mode (PLC_READONLY=true). "
            "All PLC writes are blocked. Change PLC_READONLY in .env and restart to enable writes."
        )


def check_rate_limit(ip: str):
    """R5: Check if we can connect to this PLC (30s cooldown).

    Skipped when called from inside a composite operation that already
    performed a top-level rate-limit check (_composite_flag.active is True).
    """
    if getattr(_composite_flag, 'active', False):
        return  # composite operation already rate-checked

    with _stats_lock:
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
    with _stats_lock:
        _last_connection[ip] = now
        _connection_log.append((now, ip, operation, duration_ms))
        # Prune old entries (keep last 100)
        if len(_connection_log) > 100:
            _connection_log.pop(0)
        # Count connections in last minute (read under lock for consistency)
        recent = sum(1 for t, i, _, _ in _connection_log if i == ip and now - t < 60)
    if recent > 5:
        log.warning(f"HIGH CONNECTION RATE: {recent} connections to {ip} in last minute")


def get_connection_stats():
    """Return connection statistics for monitoring."""
    now = time.time()
    with _stats_lock:
        # Copy data under lock to avoid races
        log_snapshot = list(_connection_log)
        last_snapshot = dict(_last_connection)
    return {
        "active_lock": _connection_lock.locked(),
        "recent_connections": [
            {"time": t, "ip": i, "operation": o, "duration_ms": d}
            for t, i, o, d in log_snapshot
            if now - t < 300  # last 5 minutes
        ],
        "rate_limits": {
            ip: {
                "last_connection": ts,
                "cooldown_remaining": max(0, int(_RATE_LIMIT_SECONDS - (now - ts)))
            }
            for ip, ts in last_snapshot.items()
        }
    }


def safe_plc_operation(operation_name: str):
    """
    Decorator that enforces ALL safety rules for PLC operations.

    Usage:
        @safe_plc_operation("tag_discovery")
        def discover_tags(ip, path=None, timeout=5000):
            # ... pylogix code here ...
            return tags

    The decorated function receives the PLC IP as first argument.

    Timeout protection: SocketTimeout (set to 3–5s in each function) is the
    primary mechanism against hung TCP connections. If the PLC accepts TCP but
    hangs at the CIP layer, the socket timeout fires and raises an exception
    which propagates through here and releases the lock.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(ip, *args, **kwargs):
            # R5: Check rate limit (skipped inside composite operations)
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
                # R2+R3: The actual pylogix call has its own SocketTimeout (3-5s max)
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
