"""
OPC-UA user management routes for PLC4X Manager FastAPI.

Endpoints:
  GET    /api/users                  — list OPC-UA users (@require_admin)
  POST   /api/users                  — add user (@require_admin)
  PUT    /api/users/{username}        — update password/security group (@require_admin)
  DELETE /api/users/{username}        — remove user (@require_admin)

Users are stored in the .jibberish password file (YAML).
"""

from __future__ import annotations

import asyncio
import os
import re

from fastapi import APIRouter, Depends, HTTPException

from auth import CurrentUser, require_admin
from password_utils import PASSWORD_LOCK, _hash_password, _load_password_config, _save_password_config

router = APIRouter(tags=["users"])

# =============================================
# Constants
# =============================================

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")

_NAME_RE = re.compile(r'^[a-zA-Z0-9._-]{1,128}$')


def _validate_name(name: str) -> bool:
    return bool(name) and bool(_NAME_RE.match(name))


# =============================================
# Routes
# =============================================

@router.get("/api/users")
async def api_list_users(user: CurrentUser = Depends(require_admin)):
    """Lists all OPC-UA users (without password hashes)."""
    pwd_config = _load_password_config()
    if not pwd_config or "users" not in pwd_config:
        return []
    users = []
    for username, user_data in pwd_config["users"].items():
        users.append({
            "username": username,
            "security": user_data.get("security", ""),
            "hasPassword": bool(user_data.get("password"))
        })
    return users


@router.post("/api/users", status_code=201)
async def api_add_user(body: dict, user: CurrentUser = Depends(require_admin)):
    """Adds a new OPC-UA user."""
    if not body or "username" not in body or "password" not in body:
        raise HTTPException(status_code=400, detail="Fields 'username' and 'password' are required")

    username = body["username"].strip()
    password = body["password"]
    security = body.get("security", "admin-group")

    if not username:
        raise HTTPException(status_code=400, detail="Username cannot be empty")
    if not password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")
    if not _validate_name(username):
        raise HTTPException(status_code=400, detail="Invalid username. Use only letters, numbers, dots, hyphens, underscores (max 128 chars)")

    hash_hex, salt_b64, salt_bytes = _hash_password(password)

    def _do_add_user():
        with PASSWORD_LOCK:
            pwd_config = _load_password_config()
            if not pwd_config:
                raise RuntimeError("Security not initialized. Start the server first.")

            if username in pwd_config.get("users", {}):
                raise ValueError(f"User '{username}' already exists")

            if "users" not in pwd_config:
                pwd_config["users"] = {}

            pwd_config["users"][username] = {
                "username": username,
                "password": hash_hex,
                "security": security,
                "salt": list(salt_bytes)
            }

            _save_password_config(pwd_config)

    try:
        await asyncio.to_thread(_do_add_user)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"message": f"User '{username}' added"}


@router.put("/api/users/{username}")
async def api_update_user(username: str, body: dict, user: CurrentUser = Depends(require_admin)):
    """Updates an existing OPC-UA user (password and/or security group)."""
    if not body:
        raise HTTPException(status_code=400, detail="Missing JSON body")

    new_hash = None
    new_salt = None
    if "password" in body and body["password"]:
        new_hash, _, new_salt_bytes = _hash_password(body["password"])
        new_salt = list(new_salt_bytes)

    def _do_update_user():
        with PASSWORD_LOCK:
            pwd_config = _load_password_config()
            if not pwd_config or username not in pwd_config.get("users", {}):
                raise LookupError(f"User '{username}' not found")

            u = pwd_config["users"][username]
            if "security" in body:
                u["security"] = body["security"]
            if new_hash is not None:
                u["password"] = new_hash
                u["salt"] = new_salt

            pwd_config["users"][username] = u
            _save_password_config(pwd_config)

    try:
        await asyncio.to_thread(_do_update_user)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"message": f"User '{username}' updated"}


@router.delete("/api/users/{username}")
async def api_delete_user(username: str, user: CurrentUser = Depends(require_admin)):
    """Removes an OPC-UA user. Cannot delete the default admin user."""
    if username == ADMIN_USERNAME:
        raise HTTPException(status_code=403, detail="Cannot delete the default admin user")

    def _do_delete_user():
        with PASSWORD_LOCK:
            pwd_config = _load_password_config()
            if not pwd_config or username not in pwd_config.get("users", {}):
                raise LookupError(f"User '{username}' not found")

            del pwd_config["users"][username]
            _save_password_config(pwd_config)

    try:
        await asyncio.to_thread(_do_delete_user)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"message": f"User '{username}' removed"}
