"""
Configuration load/save/backup for PLC4X Manager FastAPI.

Manages two config files:
- config.yml        : Clean config for PLC4X server (no admin-only fields)
- config.yml.admin  : Full config with calculatedTags, oeeConfig, etc.

Uses FileLock for write operations and mtime-based cache for reads
to avoid contention with 60+ concurrent clients.
"""

from __future__ import annotations

import copy
import os
import tempfile
import time
from typing import Optional

import yaml
from filelock import FileLock

# =============================================
# Paths and locks
# =============================================

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config/config.yml")
ADMIN_CONFIG_PATH = CONFIG_PATH + ".admin"
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/app/config/backups")
BACKUP_MAX_FILES = int(os.environ.get("BACKUP_MAX_FILES", "50"))

CONFIG_LOCK = FileLock("/tmp/plc4x_config.lock", timeout=10)

# =============================================
# Config cache (mtime-based to avoid FileLock contention)
# =============================================

_config_cache: Optional[dict] = None
_config_cache_mtime: float = 0.0
_config_cache_path: str = ""
_CONFIG_CACHE_TTL = 1.0  # seconds — short enough to be fresh


def _get_config_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# =============================================
# Default config
# =============================================

def get_default_config() -> dict:
    """Return a minimal default configuration (no devices)."""
    return {
        "version": "0.8",
        "dir": "/app/security",
        "name": "Plc4xOPCUAServer",
        "disableInsecureEndpoint": False,
        "tcpPort": 12687,
        "devices": [],
    }


# =============================================
# Load
# =============================================

def load_config() -> dict:
    """Load config from admin file (has calculatedTags), falls back to server config.

    Uses mtime-based cache to avoid excessive FileLock acquisition with many clients.
    """
    global _config_cache, _config_cache_mtime, _config_cache_path

    # Determine which path exists
    for path in [ADMIN_CONFIG_PATH, CONFIG_PATH]:
        mtime = _get_config_mtime(path)
        if mtime > 0:
            # Cache hit: same file, same mtime
            if (
                path == _config_cache_path
                and mtime == _config_cache_mtime
                and _config_cache is not None
            ):
                return copy.deepcopy(_config_cache)
            # Cache miss: read file (no lock needed for reads)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if data:
                    _config_cache = data
                    _config_cache_mtime = mtime
                    _config_cache_path = path
                    return copy.deepcopy(data)
            except Exception:
                continue

    return get_default_config()


def _invalidate_config_cache() -> None:
    """Invalidate the in-process config cache after a write."""
    global _config_cache, _config_cache_mtime, _config_cache_path
    _config_cache = None
    _config_cache_mtime = 0.0
    _config_cache_path = ""


# =============================================
# Atomic YAML write helper
# =============================================

def _atomic_yaml_write(filepath: str, data: dict) -> None:
    """Write YAML data to filepath atomically via tempfile + os.replace."""
    dir_name = os.path.dirname(filepath)
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".yml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# =============================================
# Strip admin fields (for server config)
# =============================================

def _strip_admin_fields(config: dict) -> dict:
    """Return a copy of config with all admin-only fields removed.

    Specifically:
    - Remove entire disabled devices (enabled: false)
    - Remove calculatedTags, oeeConfig, plant, pollInterval, enabled, allowWrite from devices
    - Remove tags whose address starts with VIRTUAL (case-insensitive)
    - Remove allowWrite field from tags (alarmThresholds is also removed)
    """
    server_config = copy.deepcopy(config)

    # Remove top-level admin-only keys
    server_config.pop("mlConfig", None)

    # Remove disabled devices entirely
    server_config["devices"] = [
        d for d in server_config.get("devices", [])
        if d.get("enabled", True) is not False
    ]

    for device in server_config["devices"]:
        # Remove admin-only device fields
        device.pop("calculatedTags", None)
        device.pop("oeeConfig", None)
        device.pop("plant", None)
        device.pop("pollInterval", None)
        device.pop("enabled", None)
        device.pop("allowWrite", None)

        # Remove VIRTUAL tags and admin-only tag fields
        device["tags"] = [
            t for t in device.get("tags", [])
            if not t.get("address", "").upper().startswith("VIRTUAL")
        ]
        for tag in device["tags"]:
            tag.pop("alarmThresholds", None)
            tag.pop("allowWrite", None)

    return server_config


# =============================================
# Save
# =============================================

def save_config(config: dict) -> None:
    """Save configuration to both admin and server config files.

    - config.yml.admin : full config with calculatedTags, oeeConfig, etc.
    - config.yml        : stripped config for PLC4X server process

    Callers MUST hold CONFIG_LOCK for read-modify-write operations.
    Automatically creates backup of previous admin config and trims old backups.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)

    # Backup existing admin config before overwriting
    if os.path.exists(ADMIN_CONFIG_PATH):
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(BACKUP_DIR, f"config_{ts}.yml")
        with open(ADMIN_CONFIG_PATH, "r", encoding="utf-8") as src:
            with open(backup_path, "w", encoding="utf-8") as dst:
                dst.write(src.read())

    # Save full admin config
    _atomic_yaml_write(ADMIN_CONFIG_PATH, config)

    # Save stripped server config
    server_config = _strip_admin_fields(config)
    _atomic_yaml_write(CONFIG_PATH, server_config)

    _invalidate_config_cache()
    _cleanup_old_backups()


def _cleanup_old_backups() -> None:
    """Keep only the most recent BACKUP_MAX_FILES backup files."""
    if not os.path.exists(BACKUP_DIR):
        return
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".yml")],
        reverse=True,
    )
    for old_file in files[BACKUP_MAX_FILES:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old_file))
        except OSError:
            pass


# =============================================
# Lookup helpers
# =============================================

def find_device(config: dict, name: str) -> tuple[Optional[int], Optional[dict]]:
    """Find a device by name. Returns (index, device_dict) or (None, None)."""
    for i, device in enumerate(config.get("devices", [])):
        if device.get("name") == name:
            return i, device
    return None, None


def filter_by_plant(items: list, plants: Optional[list], plant_key: str = "plant") -> list:
    """Filter a list of dicts by allowed plants.

    Args:
        items: List of dicts with a plant field.
        plants: List of allowed plant names, or None for all.
        plant_key: Key name for the plant field.
    """
    if not plants:
        return items
    return [item for item in items if item.get(plant_key) in plants]


def is_plc_readonly() -> bool:
    """Check if the system is in global read-only mode (PLC_READONLY=true).

    Blocks ALL writes to ALL PLCs. Requires container restart to change.
    """
    return os.environ.get("PLC_READONLY", "true").lower() in ("true", "1", "yes")


# =============================================
# Initialization
# =============================================

def init_config() -> None:
    """Create default config if it does not exist."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        default = get_default_config()
        _atomic_yaml_write(CONFIG_PATH, default)
