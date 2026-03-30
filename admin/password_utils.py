"""
Shared password file helpers for PLC4X Manager FastAPI.

Used by user_routes.py and security_routes.py.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import tempfile

import yaml
from filelock import FileLock

# =============================================
# Constants
# =============================================

SECURITY_DIR = os.environ.get("SECURITY_DIR", "/app/security")
PASSWORD_FILE = os.path.join(SECURITY_DIR, ".jibberish")
PASSWORD_LOCK = FileLock("/tmp/plc4x_password.lock", timeout=10)


# =============================================
# Helpers
# =============================================

def _load_password_config() -> dict | None:
    """Load the .jibberish password config."""
    try:
        with open(PASSWORD_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        return None


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


def _save_password_config(pwd_config: dict) -> None:
    """Save the .jibberish password config atomically."""
    _atomic_yaml_write(PASSWORD_FILE, pwd_config)


def _hash_password(plaintext: str, salt_bytes: bytes | None = None) -> tuple[str, str, bytes]:
    """Hash a password using PLC4X format: SHA-256(base64(salt) + ':' + password)."""
    if salt_bytes is None:
        salt_bytes = secrets.token_bytes(16)
    salt_b64 = base64.b64encode(salt_bytes).decode("utf-8")
    to_hash = f"{salt_b64}:{plaintext}"
    hash_hex = hashlib.sha256(to_hash.encode("utf-8")).hexdigest()
    return hash_hex, salt_b64, salt_bytes
