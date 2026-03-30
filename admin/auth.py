"""
Authentication system for PLC4X Manager FastAPI.

Supports:
- JWT Bearer tokens (PyJWT HS256)
- HTTP Basic Auth
- API Key (X-API-Key header)
- Query param token (for file downloads)

Brute-force protection via file-based failure counting.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import yaml
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# =============================================
# Configuration constants
# =============================================

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config/config.yml")
SECURITY_DIR = os.environ.get("SECURITY_DIR", "/app/security")
JWT_EXPIRY_HOURS = int(os.environ.get("JWT_EXPIRY_HOURS", "24"))
API_KEY = os.environ.get("API_KEY", "")

# JWT secret: use env var or persist to file (survives restarts without env)
_JWT_SECRET_FILE = "/app/config/.jwt-secret"


def _load_or_create_jwt_secret() -> str:
    env_secret = os.environ.get("JWT_SECRET", "")
    if env_secret:
        return env_secret
    # Try to load persisted secret
    try:
        with open(_JWT_SECRET_FILE, "r", encoding="utf-8") as f:
            stored = f.read().strip()
            if stored:
                return stored
    except FileNotFoundError:
        pass
    # Generate and persist new secret
    new_secret = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(_JWT_SECRET_FILE), exist_ok=True)
        with open(_JWT_SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(new_secret)
    except Exception:
        pass
    return new_secret


JWT_SECRET: str = _load_or_create_jwt_secret()

# =============================================
# User database
# =============================================

ADMIN_USERNAME: str = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "admin")

# USERS is the authoritative in-memory user dict: {username: {password, role, plants?}}
USERS: dict = {}


def _build_users_from_env() -> dict:
    """Build user dict from environment variables. Supports USERS_JSON in two formats:
    - list-of-objects: [{"username": "x", "password": "y", "role": "admin"}, ...]
    - dict-of-dicts:   {"x": {"password": "y", "role": "admin"}, ...}
    Falls back to individual ADMIN/OPERATOR/MONITOR env vars.
    """
    users: dict = {}
    users_env = os.environ.get("USERS_JSON", "")
    if users_env:
        try:
            parsed = json.loads(users_env)
            if isinstance(parsed, list):
                # list-of-objects format
                for entry in parsed:
                    if isinstance(entry, dict) and "username" in entry:
                        uname = entry["username"]
                        users[uname] = {
                            "password": entry.get("password", ""),
                            "role": entry.get("role", "monitor"),
                        }
                        if "plants" in entry:
                            users[uname]["plants"] = entry["plants"]
            elif isinstance(parsed, dict):
                # dict-of-dicts format
                for uname, udata in parsed.items():
                    if isinstance(udata, dict):
                        users[uname] = {
                            "password": udata.get("password", ""),
                            "role": udata.get("role", "monitor"),
                        }
                        if "plants" in udata:
                            users[uname]["plants"] = udata["plants"]
        except Exception:
            pass

    if not users:
        # Fall back to individual env vars
        users[ADMIN_USERNAME] = {"password": ADMIN_PASSWORD, "role": "admin"}
        op_user = os.environ.get("OPERATOR_USERNAME", "operator")
        op_pass = os.environ.get("OPERATOR_PASSWORD", "operator")
        if op_user:
            users[op_user] = {"password": op_pass, "role": "operator"}
        mon_user = os.environ.get("MONITOR_USERNAME", "monitor")
        mon_pass = os.environ.get("MONITOR_PASSWORD", "monitor")
        if mon_user:
            users[mon_user] = {"password": mon_pass, "role": "monitor"}

    return users


USERS = _build_users_from_env()


def load_admin_credentials() -> None:
    """Load persisted admin credentials override from .admin-credentials file.

    Called at startup to restore password changes that were made via the API.
    The .env file is read-only inside the container, so password changes are
    stored in a YAML file that this function reads on boot.
    """
    global ADMIN_PASSWORD
    creds_path = os.path.join(os.path.dirname(CONFIG_PATH), ".admin-credentials")
    try:
        with open(creds_path, "r", encoding="utf-8") as f:
            creds = yaml.safe_load(f)
            if creds and "password" in creds:
                ADMIN_PASSWORD = creds["password"]
                if ADMIN_USERNAME in USERS:
                    USERS[ADMIN_USERNAME]["password"] = creds["password"]
                return
    except FileNotFoundError:
        pass

    if ADMIN_PASSWORD == "admin":
        print(
            "[WARNING] Using default admin password. "
            "Change it in Security > Admin Panel Credentials!",
            file=sys.stderr,
        )


# =============================================
# JWT helpers
# =============================================

def create_jwt_token(username: str, role: str = "admin", plants: Optional[list] = None) -> str:
    """Create a signed JWT token for the given user."""
    now = datetime.now(timezone.utc)
    payload: dict = {
        "sub": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    if plants:
        payload["plants"] = plants
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verify_jwt_token(token: str) -> Optional[dict]:
    """Verify a JWT token. Returns the payload dict or None if invalid/expired."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def verify_basic_auth(auth_header: str):
    """Verify HTTP Basic Auth credentials.

    Returns (username, role, plants) or (None, None, None).
    Checks all USERS entries with constant-time comparison.
    """
    try:
        scheme, credentials = auth_header.split(" ", 1)
        if scheme.lower() != "basic":
            return None, None, None
        decoded = base64.b64decode(credentials).decode("utf-8")
        username, password = decoded.split(":", 1)
        # Check against all users (constant-time compare for each)
        for uname, udata in USERS.items():
            if hmac.compare_digest(username, uname) and hmac.compare_digest(
                password, udata.get("password", "")
            ):
                return uname, udata.get("role", "operator"), udata.get("plants")
        # Legacy fallback: ADMIN_USERNAME/ADMIN_PASSWORD
        if hmac.compare_digest(username, ADMIN_USERNAME) and hmac.compare_digest(
            password, ADMIN_PASSWORD
        ):
            return username, "admin", None
    except (ValueError, UnicodeDecodeError):
        pass
    return None, None, None


def verify_api_key(key: str) -> bool:
    """Verify an API key using constant-time comparison."""
    return bool(API_KEY) and hmac.compare_digest(key, API_KEY)


# =============================================
# Brute-force protection (file-based, multi-worker safe)
# =============================================

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300  # 5 minutes
_LOGIN_FILE = "/tmp/plc4x_login_failures.json"
_LOGIN_FILE_LOCK = threading.Lock()


def _load_login_failures() -> dict:
    try:
        with open(_LOGIN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_login_failures(data: dict) -> None:
    with open(_LOGIN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def check_ip_locked(client_ip: str) -> tuple[bool, int]:
    """Check if IP is currently locked out.

    Returns (is_locked, seconds_remaining).
    """
    now = datetime.now(timezone.utc).timestamp()
    with _LOGIN_FILE_LOCK:
        failures = _load_login_failures()
    fail_info = failures.get(client_ip, {})
    locked_until = fail_info.get("locked_until", 0)
    if locked_until > now:
        return True, int(locked_until - now)
    return False, 0


def record_login_failure(client_ip: str) -> int:
    """Record a failed login attempt. Returns current attempt count."""
    now = datetime.now(timezone.utc).timestamp()
    with _LOGIN_FILE_LOCK:
        failures = _load_login_failures()
        if client_ip not in failures:
            failures[client_ip] = {"count": 0, "locked_until": 0}
        failures[client_ip]["count"] += 1
        attempts = failures[client_ip]["count"]
        if attempts >= LOGIN_MAX_ATTEMPTS:
            failures[client_ip]["locked_until"] = now + LOGIN_LOCKOUT_SECONDS
        _save_login_failures(failures)
    return attempts


def clear_login_failures(client_ip: str) -> None:
    """Clear failure record for IP on successful login."""
    with _LOGIN_FILE_LOCK:
        failures = _load_login_failures()
        failures.pop(client_ip, None)
        _save_login_failures(failures)


# =============================================
# CurrentUser class
# =============================================

class CurrentUser:
    """Represents the authenticated user for the current request."""

    __slots__ = ("username", "role", "plants")

    def __init__(self, username: str, role: str, plants: Optional[list]) -> None:
        self.username = username
        self.role = role
        self.plants = plants  # None means all plants allowed

    def can_access_plant(self, plant: Optional[str]) -> bool:
        """Return True if this user can access the given plant."""
        if self.plants is None:
            return True
        return plant in self.plants if plant else True


# =============================================
# FastAPI dependencies
# =============================================

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> CurrentUser:
    """FastAPI dependency: extract and validate authentication.

    Checks in order:
    1. Bearer JWT token (Authorization: Bearer ...)
    2. Basic Auth (Authorization: Basic ...)
    3. API Key header (X-API-Key: ...)
    4. Query param token= (for file downloads on paths containing /download)
    """
    # Method 4: query param token (for file downloads)
    query_token = request.query_params.get("token", "")
    if query_token and "/download" in str(request.url.path):
        payload = verify_jwt_token(query_token)
        if payload:
            return CurrentUser(
                username=payload.get("sub", "unknown"),
                role=payload.get("role", "monitor"),
                plants=payload.get("plants"),
            )

    auth_header = request.headers.get("Authorization", "")

    # Method 1: Bearer JWT
    if credentials is not None and credentials.scheme.lower() == "bearer":
        payload = verify_jwt_token(credentials.credentials)
        if payload:
            return CurrentUser(
                username=payload.get("sub", "unknown"),
                role=payload.get("role", "monitor"),
                plants=payload.get("plants"),
            )
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Method 2: Basic Auth
    if auth_header.lower().startswith("basic "):
        username, role, plants = verify_basic_auth(auth_header)
        if username:
            return CurrentUser(username=username, role=role, plants=plants)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Method 3: API Key
    api_key = request.headers.get("X-API-Key", "")
    if api_key:
        if verify_api_key(api_key):
            return CurrentUser(username="api-key", role="admin", plants=None)
        raise HTTPException(status_code=401, detail="Invalid API key")

    raise HTTPException(status_code=401, detail="Authentication required")


async def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """FastAPI dependency: require admin role."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def require_operator(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """FastAPI dependency: require admin or operator role (not monitor)."""
    if user.role not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="Operator access required")
    return user
