"""
Authentication routes for PLC4X Manager FastAPI.

Endpoints:
- POST /api/auth/login    — issue JWT (brute-force protected, rate limited)
- GET  /api/auth/verify   — verify current token
- POST /api/auth/refresh  — refresh JWT
- PUT  /api/auth/password — change admin password (@require_admin)
- GET  /api/auth/info     — return admin username (public)
"""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import (
    ADMIN_USERNAME,
    JWT_EXPIRY_HOURS,
    USERS,
    CurrentUser,
    check_ip_locked,
    clear_login_failures,
    create_jwt_token,
    get_current_user,
    record_login_failure,
    require_admin,
)
from audit import audit_log
from config_manager import CONFIG_PATH, _atomic_yaml_write
from models import ChangePasswordRequest, LoginRequest

router = APIRouter(prefix="/api/auth", tags=["auth"])


# =============================================
# POST /api/auth/login
# =============================================

@router.post("/login")
async def api_login(body: LoginRequest, request: Request):
    """Authenticates and returns a JWT token.

    Brute-force protection: after 5 failed attempts the IP is locked out
    for 5 minutes. Rate limiting also applies (30 requests/minute).
    """
    client_ip = request.client.host if request.client else "unknown"

    # Check lockout BEFORE validating credentials
    is_locked, remaining = check_ip_locked(client_ip)
    if is_locked:
        raise HTTPException(
            status_code=429,
            detail={
                "error": f"Too many failed attempts. Try again in {remaining} seconds.",
                "locked": True,
                "retryAfter": remaining,
            },
        )

    username = body.username
    password = body.password

    user_entry = USERS.get(username)
    # Constant-time compare even when user not found (prevent timing oracle)
    stored_pass = user_entry["password"] if user_entry else "dummy"

    if user_entry and hmac.compare_digest(password, stored_pass):
        # Successful login — clear brute-force counter
        clear_login_failures(client_ip)
        role = user_entry.get("role", "operator")
        plants = user_entry.get("plants")  # list of allowed plants, or None for all
        token = create_jwt_token(username, role, plants)
        audit_log("login", {"status": "success", "role": role, "plants": plants}, user=username, ip=client_ip)
        resp: dict = {
            "token": token,
            "username": username,
            "role": role,
            "expiresIn": JWT_EXPIRY_HOURS * 3600,
        }
        if plants:
            resp["plants"] = plants
        return resp

    # Failed login — record attempt
    attempts = record_login_failure(client_ip)

    from auth import LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT_SECONDS

    if attempts >= LOGIN_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail={
                "error": f"Account locked for {LOGIN_LOCKOUT_SECONDS // 60} minutes after {LOGIN_MAX_ATTEMPTS} failed attempts.",
                "locked": True,
                "retryAfter": LOGIN_LOCKOUT_SECONDS,
            },
        )

    remaining_attempts = LOGIN_MAX_ATTEMPTS - attempts
    audit_log("login_failed", {"username": username, "attempts": attempts}, user=username, ip=client_ip)
    raise HTTPException(
        status_code=401,
        detail={"error": f"Invalid credentials. {remaining_attempts} attempt(s) remaining before lockout."},
    )


# =============================================
# GET /api/auth/verify
# =============================================

@router.get("/verify")
async def api_verify(user: CurrentUser = Depends(get_current_user)):
    """Verifies the current authentication is valid. Returns the authenticated username."""
    return {"username": user.username, "authenticated": True}


# =============================================
# POST /api/auth/refresh
# =============================================

@router.post("/refresh")
async def api_refresh(user: CurrentUser = Depends(get_current_user)):
    """Refreshes the current JWT token (issues a new one)."""
    token = create_jwt_token(user.username, user.role, user.plants)
    return {
        "token": token,
        "username": user.username,
        "role": user.role,
        "expiresIn": JWT_EXPIRY_HOURS * 3600,
    }


# =============================================
# PUT /api/auth/password
# =============================================

@router.put("/password")
async def api_change_admin_password(
    body: ChangePasswordRequest,
    user: CurrentUser = Depends(require_admin),
):
    """Changes the admin panel password.

    Writes the new credentials to a persistent file so they survive restarts.
    The .env file is not writable from inside the container, so we store
    the override in a config file that gets loaded at startup.
    """
    import auth as auth_module

    new_password = body.password  # Already validated min_length=4 by Pydantic

    # Store credentials override in a persistent file
    creds_path = os.path.join(os.path.dirname(CONFIG_PATH), ".admin-credentials")
    creds = {"username": ADMIN_USERNAME, "password": new_password}
    _atomic_yaml_write(creds_path, creds)

    # Update in-memory password for immediate effect
    auth_module.ADMIN_PASSWORD = new_password
    if ADMIN_USERNAME in auth_module.USERS:
        auth_module.USERS[ADMIN_USERNAME]["password"] = new_password

    return {"message": "Admin password changed. You will need to log in again."}


# =============================================
# GET /api/auth/info
# =============================================

@router.get("/info")
async def api_auth_info():
    """Returns the current admin username (for UI display)."""
    return {"username": ADMIN_USERNAME}
