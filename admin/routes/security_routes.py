"""
Security management routes for PLC4X Manager FastAPI.

Endpoints:
  GET    /api/security/status                          — check file existence
  PUT    /api/security/password                        — change keystore password (@require_admin)
  GET    /api/security/certificates/trusted            — list trusted certs
  GET    /api/security/certificates/rejected           — list rejected certs
  POST   /api/security/certificates/trust/{filename}   — move rejected → trusted (@require_admin)
  POST   /api/security/certificates/reject/{filename}  — move trusted → rejected (@require_admin)
  DELETE /api/security/certificates/{filename}         — delete cert from either dir (@require_admin)
"""

from __future__ import annotations

import asyncio
import os
import re

from fastapi import APIRouter, Depends, HTTPException

from auth import CurrentUser, get_current_user, require_admin
from password_utils import (
    PASSWORD_FILE,
    PASSWORD_LOCK,
    SECURITY_DIR,
    _load_password_config,
    _save_password_config,
)

router = APIRouter(tags=["security"])

# =============================================
# Constants
# =============================================

KEYSTORE_FILE = os.path.join(SECURITY_DIR, "plc4x-opcuaserver.pfx")
PKI_DIR = os.path.join(SECURITY_DIR, "pki")

_SAFE_FILENAME_RE = re.compile(r'^[a-zA-Z0-9._-]+$')


# =============================================
# Helpers
# =============================================

def _safe_filename(filename: str) -> bool:
    """Validates filename to prevent path traversal."""
    return bool(filename) and bool(_SAFE_FILENAME_RE.match(filename))


def _jetty_obfuscate(password: str) -> str:
    """Obfuscates a password using Jetty's OBF encoding."""
    buf = []
    for i, c in enumerate(password):
        b1 = 127 + ord(c) + i
        b2 = 127 + ord(c) - i
        i1 = 0xff & (b1 >> 8)
        i2 = 0xff & b1
        i3 = 0xff & (b2 >> 8)
        i4 = 0xff & b2
        buf.append(f"{i1:04x}{i2:04x}{i3:04x}{i4:04x}")
    return "".join(buf)


def _list_pki_certificates(subdir: str) -> list:
    """Lists certificate files in a PKI subdirectory."""
    cert_dir = os.path.join(PKI_DIR, subdir)
    if not os.path.exists(cert_dir):
        return []
    certs = []
    for f in sorted(os.listdir(cert_dir)):
        if f.endswith((".der", ".pem", ".crt", ".cer")):
            filepath = os.path.join(cert_dir, f)
            stat = os.stat(filepath)
            certs.append({
                "filename": f,
                "size": stat.st_size,
                "modified": stat.st_mtime
            })
    return certs


def _move_certificate(filename: str, from_subdir: str, to_subdir: str) -> dict:
    """Moves a certificate between PKI subdirectories."""
    if not _safe_filename(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    src = os.path.join(PKI_DIR, from_subdir, filename)
    if not os.path.exists(src):
        raise HTTPException(status_code=404, detail=f"Certificate '{filename}' not found in {from_subdir}")
    dst_dir = os.path.join(PKI_DIR, to_subdir)
    os.makedirs(dst_dir, exist_ok=True)
    os.rename(src, os.path.join(dst_dir, filename))
    return {"message": f"Certificate '{filename}' moved to {to_subdir}"}


# =============================================
# Routes
# =============================================

@router.get("/api/security/status")
async def api_security_status(user: CurrentUser = Depends(get_current_user)):
    """Returns the security initialization status."""
    pwd_exists = os.path.exists(PASSWORD_FILE)
    ks_exists = os.path.exists(KEYSTORE_FILE)
    pki_exists = os.path.exists(PKI_DIR)
    result = {
        "initialized": pwd_exists and ks_exists,
        "passwordFile": pwd_exists,
        "keystore": ks_exists,
        "pkiDirectory": pki_exists
    }
    if ks_exists:
        stat = os.stat(KEYSTORE_FILE)
        result["keystoreSize"] = stat.st_size
        result["keystoreModified"] = stat.st_mtime
    return result


@router.put("/api/security/password")
async def api_change_security_password(body: dict, user: CurrentUser = Depends(require_admin)):
    """Changes the keystore security password."""
    if not body or "password" not in body:
        raise HTTPException(status_code=400, detail="Field 'password' is required")
    new_password = body["password"]
    if not new_password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")

    def _change_password():
        with PASSWORD_LOCK:
            pwd_config = _load_password_config()
            if not pwd_config:
                raise ValueError("Security not initialized. Start the server first.")
            pwd_config["securityPassword"] = _jetty_obfuscate(new_password)
            _save_password_config(pwd_config)

    try:
        await asyncio.to_thread(_change_password)
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return {"message": "Security password updated. Restart the server to apply."}


@router.get("/api/security/certificates/trusted")
async def api_trusted_certificates(user: CurrentUser = Depends(get_current_user)):
    """List trusted certificates in the PKI trusted directory."""
    return _list_pki_certificates("trusted")


@router.get("/api/security/certificates/rejected")
async def api_rejected_certificates(user: CurrentUser = Depends(get_current_user)):
    """List rejected certificates in the PKI rejected directory."""
    return _list_pki_certificates("rejected")


@router.post("/api/security/certificates/trust/{filename}")
async def api_trust_certificate(filename: str, user: CurrentUser = Depends(require_admin)):
    """Move a certificate from rejected to trusted."""
    return _move_certificate(filename, "rejected", "trusted")


@router.post("/api/security/certificates/reject/{filename}")
async def api_reject_certificate(filename: str, user: CurrentUser = Depends(require_admin)):
    """Move a certificate from trusted to rejected."""
    return _move_certificate(filename, "trusted", "rejected")


@router.delete("/api/security/certificates/{filename}")
async def api_delete_certificate(filename: str, user: CurrentUser = Depends(require_admin)):
    """Delete a certificate from either trusted or rejected directory."""
    if not _safe_filename(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    for subdir in ["trusted", "rejected"]:
        path = os.path.join(PKI_DIR, subdir, filename)
        if os.path.exists(path):
            os.remove(path)
            return {"message": f"Certificate '{filename}' deleted from {subdir}"}
    raise HTTPException(status_code=404, detail=f"Certificate '{filename}' not found")
