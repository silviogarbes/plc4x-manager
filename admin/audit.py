"""
Audit trail and logbook module for PLC4X Manager FastAPI.

Provides:
- audit_log(): append structured events to audit-trail.jsonl (JSONL fallback)
- audit_log_db(): async write to SQLite (primary path)
- read_audit(): read audit entries with optional filtering
- _trim_jsonl_file(): trim JSONL files by line count and age
- Logbook constants (paths, locks)
"""

from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from filelock import FileLock

# =============================================
# Paths and locks
# =============================================

_CONFIG_DIR = os.path.dirname(os.environ.get("CONFIG_PATH", "/app/config/config.yml"))

AUDIT_TRAIL_PATH = os.path.join(_CONFIG_DIR, "audit-trail.jsonl")
AUDIT_TRAIL_MAX_LINES = 50000
AUDIT_TRAIL_MAX_DAYS = 365
AUDIT_LOCK = FileLock("/tmp/plc4x_audit.lock", timeout=5)

LOGBOOK_PATH = os.path.join(_CONFIG_DIR, "logbook.jsonl")
LOGBOOK_LOCK = FileLock("/tmp/plc4x_logbook.lock", timeout=5)
LOGBOOK_MAX_LINES = 50000
LOGBOOK_MAX_DAYS = 365

# High-frequency read paths skipped by AuditMiddleware
AUDIT_SKIP: frozenset[str] = frozenset({
    "/healthz",
    "/api/live/data",
    "/api/live/status",
    "/api/oee/current",
    "/api/alarms/active",
})


# =============================================
# Core audit functions
# =============================================

def audit_log(
    action: str,
    details: Optional[dict] = None,
    user: Optional[str] = None,
    ip: Optional[str] = None,
) -> None:
    """Append a structured audit event to the audit trail JSONL file (fallback).

    Primary path is audit_log_db() which writes to SQLite.
    This JSONL version is kept as fallback and for backward compatibility.

    Args:
        action: Short action name, e.g. "POST /api/devices" or "login".
        details: Optional dict of additional details to record.
        user: Username performing the action. Defaults to "system".
        ip: Client IP address. Defaults to None.
    """
    entry: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user": user or "system",
        "action": action,
        "ip": ip,
    }
    if details:
        entry["details"] = details

    try:
        with AUDIT_LOCK:
            with open(AUDIT_TRAIL_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# Alias for explicitness
audit_log_file = audit_log


async def audit_log_db(
    db,
    action: str,
    details: Optional[dict] = None,
    user: str = "system",
    ip: str = "",
) -> None:
    """Write an audit event to SQLite (async, primary path).

    Args:
        db: aiosqlite connection (from app.state.db).
        action: Short action name, e.g. "POST /api/devices" or "login".
        details: Optional dict of additional details to record.
        user: Username performing the action.
        ip: Client IP address.
    """
    try:
        await db.execute(
            "INSERT INTO audit_entries (user, action, ip, details) VALUES (?, ?, ?, ?)",
            (user or "system", action, ip or "", json.dumps(details or {}))
        )
        await db.commit()
    except Exception:
        # Fallback to JSONL on DB error
        audit_log(action, details=details, user=user, ip=ip)


def read_audit(
    action_filter: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list, int]:
    """Read audit trail entries.

    Args:
        action_filter: If provided, only return entries where action contains this string.
        limit: Maximum number of entries to return.
        offset: Number of entries to skip from the end (newest-first pagination).

    Returns:
        (entries, total) where entries is a list of dicts and total is the unfiltered count.
    """
    try:
        with AUDIT_LOCK:
            with open(AUDIT_TRAIL_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
    except FileNotFoundError:
        return [], 0

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if action_filter and action_filter not in entry.get("action", ""):
                continue
            entries.append(entry)
        except json.JSONDecodeError:
            continue

    total = len(entries)
    # Return newest first
    entries.reverse()
    entries = entries[offset: offset + limit]
    return entries, total


# =============================================
# Trim logic
# =============================================

def _trim_jsonl_file(
    filepath: str,
    lock: FileLock,
    max_lines: int,
    max_days: int,
) -> None:
    """Trim a JSONL file by maximum line count and maximum age.

    Strategy:
    - If lines <= max_lines: apply only the time-based filter.
    - If lines > max_lines: keep only the last max_lines, then apply time filter.

    Entries that cannot be parsed are kept (safe default).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).isoformat()

    try:
        with lock:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except FileNotFoundError:
                return

            if len(lines) <= max_lines:
                # Under line cap: only apply time-based retention
                kept = []
                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        entry = json.loads(stripped)
                        if entry.get("timestamp", "") >= cutoff:
                            kept.append(line)
                    except json.JSONDecodeError:
                        kept.append(line)  # Keep unparseable lines

                if len(kept) < len(lines):
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.writelines(kept)
            else:
                # Over line cap: trim to max_lines first, then apply time filter
                recent = lines[-max_lines:]
                kept = []
                for line in recent:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        entry = json.loads(stripped)
                        if entry.get("timestamp", "") >= cutoff:
                            kept.append(line)
                    except json.JSONDecodeError:
                        kept.append(line)

                with open(filepath, "w", encoding="utf-8") as f:
                    f.writelines(kept)
    except Exception:
        pass


def _trim_audit_trail() -> None:
    """Trim the audit trail file by line count and age."""
    _trim_jsonl_file(AUDIT_TRAIL_PATH, AUDIT_LOCK, AUDIT_TRAIL_MAX_LINES, AUDIT_TRAIL_MAX_DAYS)


def _trim_logbook() -> None:
    """Trim the logbook file by line count and age."""
    _trim_jsonl_file(LOGBOOK_PATH, LOGBOOK_LOCK, LOGBOOK_MAX_LINES, LOGBOOK_MAX_DAYS)


def maybe_trim_audit() -> None:
    """Probabilistically trim audit trail (1% chance). Call after write operations."""
    if random.random() < 0.01:
        _trim_audit_trail()


def maybe_trim_logbook() -> None:
    """Probabilistically trim logbook (1% chance). Call after write operations."""
    if random.random() < 0.01:
        _trim_logbook()


# =============================================
# Logbook helpers
# =============================================

def logbook_append(entry: dict) -> None:
    """Append an entry to the logbook JSONL file."""
    try:
        with LOGBOOK_LOCK:
            with open(LOGBOOK_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def read_logbook(
    limit: int = 100,
    offset: int = 0,
    device_filter: Optional[str] = None,
    severity_filter: Optional[str] = None,
) -> tuple[list, int]:
    """Read logbook entries (newest first).

    Returns (entries, total).
    """
    try:
        with LOGBOOK_LOCK:
            with open(LOGBOOK_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
    except FileNotFoundError:
        return [], 0

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if device_filter and entry.get("device") != device_filter:
                continue
            if severity_filter and entry.get("severity") != severity_filter:
                continue
            entries.append(entry)
        except json.JSONDecodeError:
            continue

    total = len(entries)
    entries.reverse()
    entries = entries[offset: offset + limit]
    return entries, total
