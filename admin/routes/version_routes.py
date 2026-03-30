"""
Manager version management routes for PLC4X Manager FastAPI.

Endpoints:
  GET  /api/manager/version          — current git version info
  GET  /api/manager/check-update     — git fetch + compare (@require_admin)
  POST /api/manager/update           — git pull + docker-compose rebuild (@require_admin)
  POST /api/manager/rollback         — git reset --hard to previous tag (@require_admin)
  GET  /api/manager/update-status    — read update log (@require_admin)
"""

from __future__ import annotations

import subprocess
import threading

from fastapi import APIRouter, Depends, HTTPException

from audit import audit_log
from auth import CurrentUser, require_admin

router = APIRouter(tags=["version"])

# =============================================
# Constants
# =============================================

_REPO_DIR = "/app/repo"  # Host repo mounted into container

# Threading lock to prevent concurrent updates
_update_lock = threading.Lock()


# =============================================
# Git helpers
# =============================================

def _run_git(*args, timeout=10) -> str | None:
    """Run a git command and return stdout. Returns None on error."""
    try:
        result = subprocess.run(
            ["git"] + list(args),
            capture_output=True, text=True, timeout=timeout,
            cwd=_REPO_DIR
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


# =============================================
# Routes
# =============================================

@router.get("/api/manager/version")
async def api_manager_version():
    """Returns the current Manager version (git tag + commit)."""
    tag = _run_git("describe", "--tags", "--exact-match")
    commit = _run_git("rev-parse", "--short", "HEAD") or "unknown"
    full_commit = _run_git("rev-parse", "HEAD") or ""
    branch = _run_git("rev-parse", "--abbrev-ref", "HEAD") or "main"
    version = tag if tag else "dev"
    return {
        "version": version,
        "commit": commit,
        "fullCommit": full_commit,
        "branch": branch
    }


@router.get("/api/manager/check-update")
async def api_manager_check_update(user: CurrentUser = Depends(require_admin)):
    """Check GitHub for updates without applying them."""
    # Fetch latest from remote
    _run_git("fetch", "origin", "--tags", "--quiet", timeout=15)

    current_tag = _run_git("describe", "--tags", "--exact-match") or "dev"
    current_commit = _run_git("rev-parse", "--short", "HEAD") or "unknown"

    latest_tag = _run_git("tag", "-l", "v*", "--sort=-v:refname")
    if latest_tag:
        latest_tag = latest_tag.split("\n")[0]
    else:
        latest_tag = "unknown"

    latest_commit = _run_git("rev-parse", "--short", "origin/main") or "unknown"

    # Count commits behind
    behind = _run_git("rev-list", "--count", "HEAD..origin/main") or "0"

    # Get changelog
    changelog_raw = _run_git("log", "--oneline", "--no-decorate", "HEAD..origin/main")
    changelog = changelog_raw.split("\n") if changelog_raw else []

    has_update = int(behind) > 0 if behind.isdigit() else False

    return {
        "currentVersion": current_tag,
        "currentCommit": current_commit,
        "latestVersion": latest_tag,
        "latestCommit": latest_commit,
        "behind": int(behind) if behind.isdigit() else 0,
        "hasUpdate": has_update,
        "changelog": changelog[:20]
    }


@router.post("/api/manager/update")
async def api_manager_update(user: CurrentUser = Depends(require_admin)):
    """Pull latest changes from GitHub and rebuild containers."""
    if not _update_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="An update is already in progress")
    try:
        # Check for local uncommitted changes
        status = _run_git("status", "--porcelain")
        if status:
            raise HTTPException(
                status_code=409,
                detail="Local changes detected. Please commit or stash before updating."
            )

        # Pull latest
        pull_result = _run_git("pull", "origin", "main", "--ff-only", timeout=30)
        if pull_result is None:
            raise HTTPException(status_code=500, detail="Git pull failed. There may be local conflicts.")

        new_tag = _run_git("describe", "--tags", "--exact-match") or "dev"
        new_commit = _run_git("rev-parse", "--short", "HEAD") or "unknown"

        # Rebuild and restart in background
        try:
            subprocess.Popen(
                ["sh", "-c",
                 "sleep 2 && docker-compose -f /app/repo/docker-compose.yml build && "
                 "docker-compose -f /app/repo/docker-compose.yml up -d > /tmp/manager-update.log 2>&1"],
                cwd=_REPO_DIR
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Rebuild failed: {e}")

        audit_log("manager_update", {"version": new_tag, "commit": new_commit}, user=user.username)
        return {
            "message": f"Update applied. Rebuilding containers... (version: {new_tag})",
            "version": new_tag,
            "commit": new_commit
        }
    finally:
        _update_lock.release()


@router.post("/api/manager/rollback")
async def api_manager_rollback(user: CurrentUser = Depends(require_admin)):
    """Rollback to the previous tagged version."""
    if not _update_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="An update is already in progress")
    try:
        tags_raw = _run_git("tag", "-l", "v*", "--sort=-v:refname")
        if not tags_raw:
            raise HTTPException(status_code=404, detail="No tagged versions found")

        tags = tags_raw.split("\n")
        current_tag = _run_git("describe", "--tags", "--exact-match")

        # Find previous version
        prev_tag = None
        if current_tag and current_tag in tags:
            idx = tags.index(current_tag)
            if idx + 1 < len(tags):
                prev_tag = tags[idx + 1]
        elif len(tags) > 1:
            prev_tag = tags[1]

        if not prev_tag:
            raise HTTPException(status_code=404, detail="No previous version to rollback to")

        # Reset main branch to previous tag
        reset = _run_git("reset", "--hard", prev_tag)
        if reset is None:
            raise HTTPException(status_code=500, detail=f"Failed to reset to {prev_tag}")

        # Rebuild in background
        try:
            subprocess.Popen(
                ["sh", "-c",
                 "sleep 2 && docker-compose -f /app/repo/docker-compose.yml build && "
                 "docker-compose -f /app/repo/docker-compose.yml up -d > /tmp/manager-update.log 2>&1"],
                cwd=_REPO_DIR
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Rebuild failed: {e}")

        audit_log("manager_rollback", {"from": current_tag, "to": prev_tag}, user=user.username)
        return {
            "message": f"Rolling back to {prev_tag}. Rebuilding...",
            "version": prev_tag
        }
    finally:
        _update_lock.release()


@router.get("/api/manager/update-status")
async def api_manager_update_status(user: CurrentUser = Depends(require_admin)):
    """Poll the status of a background update/rollback rebuild."""
    try:
        with open("/tmp/manager-update.log", "r") as f:
            log_content = f.read()
        done = "up-d" in log_content or "Started" in log_content or "Running" in log_content
        failed = "error" in log_content.lower() or "failed" in log_content.lower()
        return {"log": log_content[-2000:], "done": done, "failed": failed}
    except FileNotFoundError:
        return {"log": "", "done": False, "failed": False}
