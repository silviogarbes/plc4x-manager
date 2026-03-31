"""
PLC4X Manager - FastAPI entry point.

Replaces Flask app.py as the ASGI application. Routes are registered
in the routes/ package (Task 1.2+). This module handles:
- App lifecycle (lifespan context manager)
- Static file serving
- HTML page routes (/ and /login)
- Health check endpoint
- Rate limiting setup
- OpenAPI docs disabled (custom Swagger via static/swagger.json)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from audit import audit_log, AUDIT_SKIP
from database import init_db, close_db, db_maintenance_loop, db_backup_loop
from ws_manager import manager

log = logging.getLogger(__name__)

# =============================================
# Rate limiter (shared across routers)
# =============================================

limiter = Limiter(key_func=get_remote_address, default_limits=[])


# =============================================
# Audit middleware
# =============================================

class AuditMiddleware(BaseHTTPMiddleware):
    """Log write operations (POST/PUT/DELETE) that succeed, with the calling user."""

    async def dispatch(self, request: Request, call_next):
        # Pre-extract user from JWT before the route handler runs
        request.state.audit_user = "unknown"
        if request.url.path.startswith("/api/"):
            try:
                auth_header = request.headers.get("authorization", "")
                if auth_header.startswith("Bearer "):
                    from auth import verify_jwt_token
                    payload = verify_jwt_token(auth_header[7:])
                    if payload:
                        request.state.audit_user = payload.get("sub", "unknown")
            except Exception:
                pass

        response = await call_next(request)

        # Log successful write operations
        if (
            request.method in ("POST", "PUT", "DELETE")
            and request.url.path.startswith("/api/")
            and request.url.path not in AUDIT_SKIP
            and response.status_code < 400
        ):
            _user = request.state.audit_user
            _ip = request.client.host if request.client else ""
            _action = f"{request.method} {request.url.path}"
            try:
                db = request.app.state.db
                from audit import audit_log_db
                await audit_log_db(db, _action, user=_user, ip=_ip)
            except Exception:
                # Fallback to JSONL if DB not ready
                audit_log(_action, user=_user, ip=_ip)

        return response

# =============================================
# Template paths
# =============================================

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _read_template(name: str) -> str:
    """Read an HTML template file and return its contents."""
    path = os.path.join(_TEMPLATES_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# =============================================
# Lifespan
# =============================================

def _start_mqtt_ws_bridge(loop: asyncio.AbstractEventLoop) -> None:
    """Subscribe to MQTT topics and forward messages to WebSocket clients.

    Runs in a daemon thread so it does not block the asyncio event loop.
    Uses asyncio.run_coroutine_threadsafe to bridge into the event loop.
    """
    import paho.mqtt.client as mqtt

    def on_message(client, userdata, msg):  # noqa: ANN001
        try:
            data = {"type": "mqtt", "topic": msg.topic, "payload": msg.payload.decode()}
            asyncio.run_coroutine_threadsafe(manager.broadcast(data), loop)
        except Exception:
            pass

    client = mqtt.Client(client_id="ws-bridge")
    mqtt_user = os.environ.get("MQTT_USERNAME", "")
    mqtt_pass = os.environ.get("MQTT_PASSWORD", "")
    if mqtt_user:
        client.username_pw_set(mqtt_user, mqtt_pass)
    client.on_message = on_message
    try:
        client.connect(
            os.environ.get("MQTT_BROKER", "mosquitto"),
            int(os.environ.get("MQTT_PORT", "1883")),
        )
        client.subscribe("plc4x/#")
        log.info("MQTT WS bridge connected and subscribed to plc4x/#")
        client.loop_forever()
    except Exception as exc:
        log.error("MQTT WS bridge failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown tasks."""
    from auth import load_admin_credentials
    from config_manager import init_config

    init_config()
    load_admin_credentials()

    db = await init_db()
    app.state.db = db

    # Only start background tasks on the main HTTP instance (port 8080).
    # The HTTPS instance (port 8443) runs the same app object but must not
    # start duplicate background tasks (MQTT bridge, DB maintenance, etc.).
    if os.environ.get("UVICORN_PORT", "8080") == "8080":
        # Seed demo data on first deploy (no config exists yet).
        # Gate on UVICORN_PORT so only the HTTP instance seeds (avoids race
        # when both HTTP and HTTPS instances start simultaneously).
        from seed_demo import is_first_deploy, run_seed
        if is_first_deploy():
            log.info("First deploy detected — seeding demo data...")
            run_seed()
        asyncio.create_task(db_maintenance_loop(db))
        asyncio.create_task(db_backup_loop())

        # Start WebSocket dead-connection reaper
        manager.start_reaper()

        # Start MQTT→WebSocket bridge in a daemon thread
        loop = asyncio.get_running_loop()
        threading.Thread(target=_start_mqtt_ws_bridge, args=(loop,), daemon=True).start()
    else:
        log.info("HTTPS instance: skipping background tasks (handled by HTTP instance)")

    yield

    # --- Shutdown ---
    if os.environ.get("UVICORN_PORT", "8080") == "8080":
        await manager.stop_reaper()
    await close_db(db)


# =============================================
# FastAPI application
# =============================================

app = FastAPI(
    title="PLC4X Manager",
    description="Industrial OPC-UA middleware management panel",
    version="1.0.0",
    lifespan=lifespan,
    # Disable built-in docs — we serve custom Swagger via static/swagger.json
    docs_url=None,
    redoc_url=None,
    openapi_url="/openapi.json",
)

# Rate limiting middleware
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Convert FastAPI HTTPException from {"detail": ...} to {"error": ...} format."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
    )

# CORS — Bearer token auth, no cookies, so allow_credentials is not needed
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Audit middleware — must be added AFTER CORS so it runs inside the CORS layer
app.add_middleware(AuditMiddleware)

# Static files
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# =============================================
# HTML page routes
# =============================================

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> HTMLResponse:
    """Serve the main application HTML page."""
    return HTMLResponse(content=_read_template("index.html"))


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page() -> HTMLResponse:
    """Serve the login page."""
    return HTMLResponse(content=_read_template("login.html"))


# =============================================
# Health check
# =============================================

@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    """Kubernetes/Docker health check endpoint."""
    from datetime import datetime, timezone
    from config_manager import is_plc_readonly
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ws_clients": manager.client_count,
        "plcReadOnly": is_plc_readonly(),
    }


# =============================================
# WebSocket — real-time data push (Phase 3)
# =============================================

_WS_MAX_CONNECTIONS = 200


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket, token: str = "") -> None:
    """Real-time data push endpoint.

    Authentication flow:
    1. ws.accept() MUST be called before any auth check (FastAPI requirement).
    2. Validate the JWT query-param token.
    3. Close with code 4001 if invalid.
    4. Register with manager; send initial alarm sync; then loop forever.
    """
    # Accept FIRST — required before any send/close operation
    await ws.accept()

    # Connection limit guard
    if manager.client_count >= _WS_MAX_CONNECTIONS:
        await ws.close(code=4008, reason="Connection limit reached")
        return

    # Authenticate
    try:
        from auth import verify_jwt_token
        payload = verify_jwt_token(token)
        if not payload:
            await ws.close(code=4001, reason="Invalid token")
            return
        user: str = payload.get("sub", "anonymous")
        plants = payload.get("plants")  # noqa: F841  (reserved for future filtering)
    except Exception:
        await ws.close(code=4001, reason="Invalid token")
        return

    # Register connection
    accepted = await manager.add_connection(ws)
    if not accepted:
        await ws.close(code=4008, reason="Connection limit reached")
        return

    log.debug("WS connect: user=%s total=%d", user, manager.client_count)

    # Send current unacknowledged alarms so reconnecting clients stay in sync
    try:
        db = app.state.db
        async with db.execute("SELECT * FROM alarms WHERE acknowledged = 0") as c:
            active = [dict(r) for r in await c.fetchall()]
        if active:
            await ws.send_text(json.dumps({"type": "alarm_sync", "alarms": active}))
    except Exception:
        pass

    # Keep the connection alive; handle any client→server messages
    try:
        while True:
            await ws.receive_text()  # blocks until message or disconnect
    except WebSocketDisconnect:
        pass
    finally:
        await manager.remove_connection(ws)
        log.debug("WS disconnect: user=%s total=%d", user, manager.client_count)


# =============================================
# Route registration (Phase 1.2 + 1.3)
# =============================================

from routes.alarm_routes import router as alarm_router
from routes.audit_routes import router as audit_router
from routes.auth_routes import router as auth_router
from routes.config_routes import router as config_router
from routes.data_routes import router as data_router
from routes.device_routes import router as device_router
from routes.hmi_routes import router as hmi_router
from routes.live_routes import router as live_router
from routes.logbook_routes import router as logbook_router
from routes.oee_routes import router as oee_router
from routes.plc4x_routes import router as plc4x_router
from routes.security_routes import router as security_router
from routes.server_routes import router as server_router
from routes.user_routes import router as user_router
from routes.version_routes import router as version_router
from routes.ml_routes import router as ml_router
from routes.plctag_routes import router as plctag_router
from routes.replay_routes import router as replay_router
from routes.failure_routes import router as failure_router
from routes.chat_routes import router as chat_router

# Phase 1.2
app.include_router(auth_router)
app.include_router(config_router)
app.include_router(device_router)
app.include_router(hmi_router)

# Phase 1.3
app.include_router(user_router)
app.include_router(security_router)
app.include_router(server_router)
app.include_router(live_router)
app.include_router(alarm_router)
app.include_router(oee_router)
app.include_router(data_router)
app.include_router(logbook_router)
app.include_router(audit_router)
app.include_router(version_router)
app.include_router(plc4x_router)
app.include_router(ml_router)
app.include_router(plctag_router)
app.include_router(replay_router)
app.include_router(failure_router)
app.include_router(chat_router)
