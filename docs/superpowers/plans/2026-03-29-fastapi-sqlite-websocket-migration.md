# Flask → FastAPI + SQLite + WebSocket Migration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the PLC4X Manager from Flask (sync) to FastAPI (async) + SQLite + WebSocket for 20-year industrial reliability at scale (60+ concurrent clients, 10 plants, 2 monitoring rooms).

**Architecture:** Three-phase incremental migration. Each phase produces working software with all 253+ tests passing. Phase 1 replaces Flask with FastAPI (same functionality). Phase 2 replaces JSONL files with SQLite. Phase 3 adds WebSocket for real-time data push. The poller (poller.py), frontend (app.js, hmi.js), templates (index.html, login.html), and Docker infrastructure remain largely unchanged — only the API layer and data storage change.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, SQLite (aiosqlite), WebSocket, Pydantic v2, asyncua, influxdb-client, paho-mqtt, docker SDK, Konva.js

---

## File Structure

### Phase 1: FastAPI Migration

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `admin/main.py` | FastAPI app entry point, middleware, static files |
| Create | `admin/auth.py` | JWT creation/validation, dependencies (get_current_user, require_admin, require_operator), brute-force protection |
| Create | `admin/routes/__init__.py` | Router registration |
| Create | `admin/routes/auth_routes.py` | Login, verify, refresh, password change (~6 endpoints) |
| Create | `admin/routes/config_routes.py` | Config GET/PUT, server settings (~3 endpoints) |
| Create | `admin/routes/device_routes.py` | Device CRUD, tags CRUD, calculated tags (~12 endpoints) |
| Create | `admin/routes/hmi_routes.py` | HMI config, plants/areas/equipment CRUD, screen save, image upload (~14 endpoints) |
| Create | `admin/routes/user_routes.py` | OPC-UA user CRUD (~4 endpoints) |
| Create | `admin/routes/security_routes.py` | Security status, certificates, keystore (~7 endpoints) |
| Create | `admin/routes/server_routes.py` | Server status, services health, restart, logs, backups (~12 endpoints) |
| Create | `admin/routes/live_routes.py` | Live data read, write, write-log (~3 endpoints) |
| Create | `admin/routes/alarm_routes.py` | Alarms list, acknowledge (~3 endpoints) |
| Create | `admin/routes/oee_routes.py` | OEE config, calculate, trend (~4 endpoints) |
| Create | `admin/routes/data_routes.py` | Tag history, export CSV/PDF, data write, Grafana proxy (~5 endpoints) |
| Create | `admin/routes/logbook_routes.py` | Logbook GET/POST (~2 endpoints) |
| Create | `admin/routes/audit_routes.py` | Audit trail GET (~1 endpoint) |
| Create | `admin/routes/version_routes.py` | Manager version, check-update, update, rollback (~5 endpoints) |
| Create | `admin/routes/template_routes.py` | Connection templates (~1 endpoint) |
| Create | `admin/config_manager.py` | Config load/save with FileLock (extracted from app.py) |
| Create | `admin/audit.py` | Audit trail append/trim/read (extracted from app.py) |
| Create | `admin/models.py` | Pydantic models for request/response validation |
| Modify | `admin/poller.py` | No changes (runs as separate process) |
| Modify | `admin/entrypoint.sh` | Replace gunicorn with uvicorn |
| Modify | `admin/requirements.txt` | Add fastapi, uvicorn, python-multipart; remove flask, flask-limiter |
| Modify | `admin/Dockerfile` | No changes needed |
| Keep | `admin/static/js/app.js` | No changes (same REST API) |
| Keep | `admin/static/js/hmi.js` | No changes |
| Keep | `admin/templates/index.html` | No changes |
| Keep | `admin/templates/login.html` | No changes |
| Delete | `admin/app.py` | Replaced by main.py + routes/* + auth.py + config_manager.py |
| Create | `tests/test_api.py` | pytest-based API tests (replaces bash tests for API) |
| Keep | `tests/test_all.sh` | Bash integration tests still work (same HTTP API) |

### Phase 2: SQLite Migration

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `admin/database.py` | SQLite connection, schema creation, migrations |
| Modify | `admin/audit.py` | JSONL → SQLite (audit_entries table) |
| Modify | `admin/routes/logbook_routes.py` | JSONL → SQLite (logbook_entries table) |
| Modify | `admin/routes/alarm_routes.py` | JSON → SQLite (alarms table, alarm_history table) |
| Modify | `admin/poller.py` | Alarm state → SQLite instead of .alarms.json |
| Create | `admin/migrations/001_initial.sql` | Schema: audit_entries, logbook_entries, alarms, alarm_history, write_log |

### Phase 3: WebSocket

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `admin/websocket.py` | WebSocket manager (connection registry, broadcast, rooms) |
| Modify | `admin/main.py` | Add WebSocket endpoint `/ws/live` |
| Modify | `admin/poller.py` | Push results via WebSocket after each poll cycle |
| Modify | `admin/static/js/app.js` | WebSocket client with auto-reconnect, fallback to REST |
| Modify | `admin/static/js/hmi.js` | Receive live data via WebSocket instead of polling |

---

## Phase 1: FastAPI Migration

### Task 1: Project Setup and Dependencies

**Files:**
- Modify: `admin/requirements.txt`
- Create: `admin/main.py`
- Create: `admin/models.py`

- [ ] **Step 1: Update requirements.txt**

Replace Flask dependencies with FastAPI:

```txt
fastapi==0.115.6
uvicorn[standard]==0.34.0
python-multipart==0.0.19
python-jose[cryptography]==3.3.0
slowapi==0.1.9
pyyaml==6.0.2
docker==7.1.0
filelock==3.16.0
asyncua==1.1.5
paho-mqtt==1.6.1
influxdb-client==1.40.0
reportlab==4.2.5
requests==2.32.3
aiosqlite==0.20.0
```

- [ ] **Step 2: Create minimal main.py with health check**

```python
"""PLC4X Manager — FastAPI Application."""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
import os

app = FastAPI(title="PLC4X Manager", version="1.0.0", docs_url=None, redoc_url=None)

# Static files
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    with open(os.path.join(TEMPLATE_DIR, "login.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(TEMPLATE_DIR, "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
```

- [ ] **Step 3: Create models.py with core Pydantic models**

```python
"""Pydantic models for request/response validation."""
from pydantic import BaseModel, Field
from typing import Optional

class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    token: str
    username: str
    role: str
    expiresIn: int
    plants: Optional[list[str]] = None

class TokenPayload(BaseModel):
    user: str
    role: str = "monitor"
    plants: Optional[list[str]] = None

class ErrorResponse(BaseModel):
    error: str

class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    connectionString: str = Field(min_length=1)
    tags: list[dict] = []
    plant: Optional[str] = None
    enabled: bool = True
    pollInterval: int = 5

class TagCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=100)
    address: str = Field(min_length=1)

class LogbookEntry(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    shift: str = ""
    category: str = "observation"
    priority: str = "normal"

class OEEConfig(BaseModel):
    enabled: bool = True
    runningTag: str
    productionCountTag: str
    rejectCountTag: Optional[str] = None
    idealCycleTime: float = Field(gt=0)
    plannedHoursPerDay: float = Field(gt=0, le=24)
```

- [ ] **Step 4: Run to verify FastAPI boots**

```bash
cd admin && pip install fastapi uvicorn python-multipart && python -c "from main import app; print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add admin/main.py admin/models.py admin/requirements.txt
git commit -m "feat: add FastAPI skeleton with models and dependencies"
```

---

### Task 2: Authentication System

**Files:**
- Create: `admin/auth.py`
- Create: `admin/routes/auth_routes.py`

- [ ] **Step 1: Create auth.py with JWT and dependency injection**

```python
"""Authentication: JWT tokens, role-based access, brute-force protection."""
import os
import hmac
import secrets
import datetime
import json
import threading
from typing import Optional
from fastapi import Depends, HTTPException, Request, Header
from jose import jwt, JWTError
from filelock import FileLock

# User database (loaded from env)
USERS: dict[str, dict] = {}
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
USERS[ADMIN_USERNAME] = {"password": ADMIN_PASSWORD, "role": "admin"}

# Operator and monitor
_op_user = os.environ.get("OPERATOR_USERNAME", "operator")
_op_pass = os.environ.get("OPERATOR_PASSWORD", "operator")
if _op_user and _op_user != ADMIN_USERNAME:
    USERS[_op_user] = {"password": _op_pass, "role": "operator"}
_mon_user = os.environ.get("MONITOR_USERNAME", "monitor")
_mon_pass = os.environ.get("MONITOR_PASSWORD", "monitor")
if _mon_user and _mon_user not in USERS:
    USERS[_mon_user] = {"password": _mon_pass, "role": "monitor"}

# USERS_JSON override
_users_json = os.environ.get("USERS_JSON", "")
if _users_json:
    try:
        for u in json.loads(_users_json):
            USERS[u["username"]] = {
                "password": u["password"],
                "role": u.get("role", "operator"),
                "plants": u.get("plants"),
            }
    except Exception:
        pass

JWT_SECRET = os.environ.get("JWT_SECRET") or secrets.token_hex(32)
JWT_EXPIRY_HOURS = int(os.environ.get("JWT_EXPIRY_HOURS", "24"))
JWT_ALGORITHM = "HS256"
API_KEY = os.environ.get("API_KEY", "")

# Brute-force protection
_LOGIN_LOCK = threading.Lock()
_LOGIN_FAILURES_PATH = "/tmp/plc4x_login_failures.json"


def create_token(username: str, role: str, plants: Optional[list] = None) -> str:
    payload = {
        "sub": username,
        "role": role,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=JWT_EXPIRY_HOURS),
    }
    if plants:
        payload["plants"] = plants
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


class CurrentUser:
    """Authenticated user context (replaces Flask g.user/g.role/g.plants)."""
    def __init__(self, username: str, role: str, plants: Optional[list] = None):
        self.username = username
        self.role = role
        self.plants = plants


async def get_current_user(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> CurrentUser:
    """FastAPI dependency: extract and validate auth from request."""
    # Skip auth for public paths
    if request.url.path in ("/login", "/healthz", "/api/auth/login"):
        return CurrentUser("anonymous", "monitor")

    # API key auth
    if x_api_key and API_KEY and hmac.compare_digest(x_api_key, API_KEY):
        return CurrentUser("api-key", "admin")

    # Token from query param (download endpoints)
    token = request.query_params.get("token", "")

    # Token from Authorization header
    if not token and authorization:
        if authorization.startswith("Bearer "):
            token = authorization[7:]
        elif authorization.startswith("Basic "):
            import base64
            try:
                decoded = base64.b64decode(authorization[6:]).decode()
                username, password = decoded.split(":", 1)
                for uname, udata in USERS.items():
                    if uname == username and hmac.compare_digest(password, udata["password"]):
                        return CurrentUser(uname, udata.get("role", "operator"), udata.get("plants"))
            except Exception:
                pass
            raise HTTPException(status_code=401, detail="Invalid credentials")

    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        payload = decode_token(token)
        return CurrentUser(
            username=payload.get("sub", "unknown"),
            role=payload.get("role", "monitor"),
            plants=payload.get("plants"),
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Dependency: require admin role."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_operator(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Dependency: require admin or operator role."""
    if user.role not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="Operator access required")
    return user
```

- [ ] **Step 2: Create auth_routes.py**

```python
"""Authentication routes: login, verify, refresh, password change."""
from fastapi import APIRouter, Depends, HTTPException, Request
import hmac
import datetime
from auth import (
    USERS, ADMIN_USERNAME, CurrentUser, create_token, get_current_user,
    require_admin, _LOGIN_LOCK, _LOGIN_FAILURES_PATH
)
from models import LoginRequest, LoginResponse, ErrorResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, request: Request):
    user_entry = USERS.get(req.username)
    stored_pass = user_entry["password"] if user_entry else "dummy"
    if user_entry and hmac.compare_digest(req.password, stored_pass):
        role = user_entry.get("role", "operator")
        plants = user_entry.get("plants")
        token = create_token(req.username, role, plants)
        resp = {"token": token, "username": req.username, "role": role,
                "expiresIn": 24 * 3600}
        if plants:
            resp["plants"] = plants
        return resp
    raise HTTPException(status_code=401, detail="Invalid credentials")


@router.get("/verify")
async def verify(user: CurrentUser = Depends(get_current_user)):
    return {"authenticated": True, "username": user.username, "role": user.role}


@router.post("/refresh")
async def refresh(user: CurrentUser = Depends(get_current_user)):
    token = create_token(user.username, user.role, user.plants)
    return {"token": token, "expiresIn": 24 * 3600}
```

- [ ] **Step 3: Wire auth routes into main.py**

Add to main.py:
```python
from routes.auth_routes import router as auth_router
app.include_router(auth_router)
```

- [ ] **Step 4: Run test — login returns JWT**

```bash
curl -s -X POST http://localhost:8080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | grep token
```

Expected: `{"token":"eyJ...","username":"admin","role":"admin","expiresIn":86400}`

- [ ] **Step 5: Commit**

```bash
git add admin/auth.py admin/routes/auth_routes.py admin/main.py
git commit -m "feat: FastAPI auth system with JWT, RBAC, brute-force protection"
```

---

### Task 3: Config Manager (extracted shared module)

**Files:**
- Create: `admin/config_manager.py`

- [ ] **Step 1: Extract config load/save logic**

```python
"""Configuration management: load, save, validate, backup."""
import os
import yaml
import datetime
import tempfile
import shutil
from filelock import FileLock

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config/config.yml")
ADMIN_CONFIG_PATH = CONFIG_PATH + ".admin"
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/app/config/backups")
BACKUP_MAX_FILES = int(os.environ.get("BACKUP_MAX_FILES", "50"))
CONFIG_LOCK = FileLock("/tmp/plc4x_config.lock", timeout=10)


def load_config() -> dict:
    """Load config from admin config (full) or base config."""
    path = ADMIN_CONFIG_PATH if os.path.exists(ADMIN_CONFIG_PATH) else CONFIG_PATH
    with CONFIG_LOCK:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}


def save_config(config: dict):
    """Save config atomically with automatic backup."""
    with CONFIG_LOCK:
        # Backup current
        _create_backup()
        # Write admin config (full)
        _atomic_yaml_write(ADMIN_CONFIG_PATH, config)
        # Write base config (stripped for PLC4X server)
        base = _strip_admin_fields(config)
        _atomic_yaml_write(CONFIG_PATH, base)


def _atomic_yaml_write(path: str, data: dict):
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".yml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


def _create_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    src = ADMIN_CONFIG_PATH if os.path.exists(ADMIN_CONFIG_PATH) else CONFIG_PATH
    if not os.path.exists(src):
        return
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"config_{ts}.yml")
    shutil.copy2(src, dst)


def _strip_admin_fields(config: dict) -> dict:
    """Remove admin-only fields before writing base config for PLC4X server."""
    import copy
    base = copy.deepcopy(config)
    for dev in base.get("devices", []):
        dev.pop("calculatedTags", None)
        dev.pop("oeeConfig", None)
        dev.pop("plant", None)
        dev.pop("pollInterval", None)
        dev.pop("enabled", None)
        for tag in dev.get("tags", []):
            tag.pop("alarmThresholds", None)
    return base


def find_device(config: dict, name: str) -> tuple:
    """Find device by name. Returns (index, device_dict) or (None, None)."""
    for i, d in enumerate(config.get("devices", [])):
        if d.get("name") == name:
            return i, d
    return None, None


def filter_by_plant(devices: list, allowed_plants: list | None) -> list:
    """Filter device list by user's allowed plants."""
    if not allowed_plants:
        return devices
    return [d for d in devices if d.get("plant") in allowed_plants]
```

- [ ] **Step 2: Commit**

```bash
git add admin/config_manager.py
git commit -m "feat: extract config_manager module for shared config operations"
```

---

### Task 4: Audit Module (extracted)

**Files:**
- Create: `admin/audit.py`

- [ ] **Step 1: Extract audit trail logic**

```python
"""Audit trail: append-only log with time-based retention."""
import os
import json
import datetime
import random
from filelock import FileLock

AUDIT_TRAIL_PATH = os.path.join(
    os.environ.get("CONFIG_PATH", "/app/config/config.yml").rsplit("/", 1)[0],
    "audit-trail.jsonl"
)
AUDIT_LOCK = FileLock("/tmp/plc4x_audit.lock", timeout=5)
AUDIT_MAX_LINES = 50000
AUDIT_MAX_DAYS = 365

# Endpoints to skip auditing (high-frequency reads)
AUDIT_SKIP = {
    "/api/live/read", "/api/alarms", "/api/server/status", "/api/server/logs",
    "/api/devices", "/api/backups", "/api/templates", "/api/security/status",
    "/api/security/certificates/trusted", "/api/security/certificates/rejected",
    "/api/users", "/api/hmi/config", "/api/services/status", "/api/plc4x/version",
    "/api/plc4x/latest-version", "/api/manager/version", "/api/manager/check-update",
    "/api/auth/verify", "/api/export/csv", "/api/export/pdf",
    "/api/oee/calculate", "/api/oee/trend", "/api/audit",
    "/api/live/write-log", "/api/data/query",
}


def audit_log(action: str, details: dict = None, user: str = "system", ip: str = ""):
    """Append an audit entry."""
    entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "user": user,
        "action": action,
        "ip": ip,
        "details": details or {},
    }
    try:
        with AUDIT_LOCK:
            with open(AUDIT_TRAIL_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        if random.random() < 0.01:
            _trim_audit()
    except Exception:
        pass


def read_audit(lines: int = 200, action_filter: str = None) -> tuple[list, int]:
    """Read audit entries. Returns (entries, total_count)."""
    try:
        with open(AUDIT_TRAIL_PATH, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
    except FileNotFoundError:
        return [], 0

    entries = []
    for line in reversed(all_lines):
        try:
            entry = json.loads(line.strip())
            if action_filter:
                action = entry.get("action", "")
                if action != action_filter and not action.startswith(action_filter + " "):
                    continue
            entries.append(entry)
            if len(entries) >= lines:
                break
        except Exception:
            continue
    return entries, len(all_lines)


def _trim_audit():
    """Trim by max lines and max age."""
    # ... same _trim_jsonl_file logic from current app.py ...
    pass
```

- [ ] **Step 2: Commit**

```bash
git add admin/audit.py
git commit -m "feat: extract audit module with time-based retention"
```

---

### Task 5-17: Route Migration (one task per route group)

Each task follows the same pattern:

1. Create `admin/routes/<group>_routes.py` with FastAPI router
2. Migrate all endpoints from the corresponding section of app.py
3. Convert `@app.route` → `@router.get/post/put/delete`
4. Convert `request.get_json()` → Pydantic model or `Body()`
5. Convert `jsonify()` → return dict
6. Convert `g.user/g.role/g.plants` → `user: CurrentUser = Depends(get_current_user)`
7. Convert `@require_admin` → `user: CurrentUser = Depends(require_admin)`
8. Register router in main.py
9. Run existing bash tests for that section
10. Commit

**Task 5:** Config routes (3 endpoints: GET/PUT config, PUT server settings)
**Task 6:** Device routes (4 endpoints: list, add, update, delete devices)
**Task 7:** Tag routes (3 endpoints + alarm thresholds)
**Task 8:** HMI routes (14 endpoints: plants/areas/equipment CRUD, screen, image upload)
**Task 9:** Calculated tag routes (5 endpoints: CRUD + formula validate)
**Task 10:** User routes (4 endpoints: list, add, update, delete OPC-UA users)
**Task 11:** Security routes (7 endpoints: status, password, certificates)
**Task 12:** Server control routes (12 endpoints: status, services, restart, logs, backups)
**Task 13:** Live data routes (3 endpoints: read, write, write-log)
**Task 14:** Alarm routes (3 endpoints: list, acknowledge, acknowledge-all)
**Task 15:** OEE routes (4 endpoints: config GET/PUT, calculate, trend)
**Task 16:** Data/export routes (5 endpoints: history, CSV, PDF, data write, Grafana proxy)
**Task 17:** Logbook + Audit + Version routes (8 endpoints)

---

### Task 18: Middleware (audit trail, CORS, error handling)

**Files:**
- Modify: `admin/main.py`

- [ ] **Step 1: Add audit middleware**

```python
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from audit import audit_log, AUDIT_SKIP

class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # Audit write operations
        if (request.method in ("POST", "PUT", "DELETE")
            and request.url.path.startswith("/api/")
            and request.url.path not in AUDIT_SKIP
            and response.status_code < 400):
            user = getattr(request.state, "user", None)
            audit_log(
                f"{request.method} {request.url.path}",
                user=user.username if user else "unknown",
                ip=request.client.host if request.client else "",
            )
        return response

app.add_middleware(AuditMiddleware)
```

- [ ] **Step 2: Add rate limiting**

```python
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
```

- [ ] **Step 3: Commit**

```bash
git commit -m "feat: add audit middleware and rate limiting"
```

---

### Task 19: Entrypoint Migration (gunicorn → uvicorn)

**Files:**
- Modify: `admin/entrypoint.sh`

- [ ] **Step 1: Replace gunicorn with uvicorn**

Change the entrypoint from:
```bash
gosu plc4x gunicorn --bind 0.0.0.0:8443 ... app:app &
gosu plc4x gunicorn --bind 0.0.0.0:8080 ... app:app &
```
to:
```bash
gosu plc4x uvicorn main:app --host 0.0.0.0 --port 8443 --ssl-keyfile "$CERT_DIR/key.pem" --ssl-certfile "$CERT_DIR/cert.pem" --workers 1 &
gosu plc4x uvicorn main:app --host 0.0.0.0 --port 8080 --workers 2 --timeout-keep-alive 120 &
```

- [ ] **Step 2: Rebuild and verify**

```bash
docker compose build plc4x-admin && docker compose up -d plc4x-admin
sleep 10
curl -s http://localhost:8080/healthz
```

Expected: `{"status":"ok"}`

- [ ] **Step 3: Run full bash test suite**

```bash
bash tests/test_all.sh
```

Expected: 253/253 PASS (same API, same responses)

- [ ] **Step 4: Commit**

```bash
git commit -m "feat: migrate entrypoint from gunicorn to uvicorn"
```

---

### Task 20: Delete old app.py, final verification

- [ ] **Step 1: Rename app.py to app.py.bak (keep as reference)**
- [ ] **Step 2: Run full test suite**
- [ ] **Step 3: Commit and tag**

```bash
git commit -m "feat: complete Flask → FastAPI migration (Phase 1)"
git tag -a v2.0.0-alpha -m "FastAPI migration complete, all tests passing"
```

---

## Phase 2: SQLite Migration

### Task 21: Database Module

**Files:**
- Create: `admin/database.py`
- Create: `admin/migrations/001_initial.sql`

- [ ] **Step 1: Create database.py**

```python
"""SQLite database: async connection, schema, migrations."""
import aiosqlite
import os

DB_PATH = os.path.join(
    os.environ.get("CONFIG_PATH", "/app/config/config.yml").rsplit("/", 1)[0],
    "plc4x_manager.db"
)

_db = None

async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")  # concurrent reads
        await _db.execute("PRAGMA busy_timeout=5000")
        await _run_migrations(_db)
    return _db

async def _run_migrations(db):
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS audit_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            user TEXT NOT NULL DEFAULT 'system',
            action TEXT NOT NULL,
            ip TEXT DEFAULT '',
            details TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_entries(timestamp);
        CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_entries(user);
        CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_entries(action);

        CREATE TABLE IF NOT EXISTS logbook_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            user TEXT NOT NULL,
            shift TEXT DEFAULT '',
            category TEXT DEFAULT 'observation',
            priority TEXT DEFAULT 'normal',
            message TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_logbook_ts ON logbook_entries(timestamp);
        CREATE INDEX IF NOT EXISTS idx_logbook_shift ON logbook_entries(shift);

        CREATE TABLE IF NOT EXISTS alarms (
            key TEXT PRIMARY KEY,
            device TEXT NOT NULL,
            tag TEXT NOT NULL,
            plant TEXT DEFAULT 'default',
            severity TEXT NOT NULL,
            condition TEXT NOT NULL,
            value REAL,
            threshold REAL,
            message TEXT,
            timestamp TEXT NOT NULL,
            acknowledged INTEGER DEFAULT 0,
            ack_user TEXT,
            ack_time TEXT
        );

        CREATE TABLE IF NOT EXISTS alarm_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            device TEXT NOT NULL,
            tag TEXT NOT NULL,
            plant TEXT DEFAULT 'default',
            severity TEXT NOT NULL,
            condition TEXT NOT NULL,
            value REAL,
            threshold REAL,
            message TEXT,
            start_time TEXT NOT NULL,
            end_time TEXT,
            acknowledged INTEGER DEFAULT 0,
            ack_user TEXT,
            duration_s REAL
        );
        CREATE INDEX IF NOT EXISTS idx_alarm_hist_ts ON alarm_history(start_time);

        CREATE TABLE IF NOT EXISTS write_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            user TEXT NOT NULL,
            device TEXT NOT NULL,
            tag TEXT NOT NULL,
            value TEXT,
            status TEXT DEFAULT 'ok'
        );
        CREATE INDEX IF NOT EXISTS idx_write_ts ON write_log(timestamp);
    """)
    await db.commit()
```

- [ ] **Step 2: Commit**

```bash
git commit -m "feat: add SQLite database module with schema"
```

---

### Task 22: Migrate Audit Trail to SQLite

**Files:**
- Modify: `admin/audit.py`
- Modify: `admin/routes/audit_routes.py`

- [ ] **Step 1: Update audit.py to use SQLite**

Replace JSONL file operations with:
```python
async def audit_log(action, details=None, user="system", ip=""):
    db = await get_db()
    await db.execute(
        "INSERT INTO audit_entries (user, action, ip, details) VALUES (?, ?, ?, ?)",
        (user, action, ip, json.dumps(details or {}))
    )
    await db.commit()

async def read_audit(lines=200, action_filter=None):
    db = await get_db()
    if action_filter:
        rows = await db.execute_fetchall(
            "SELECT * FROM audit_entries WHERE action LIKE ? ORDER BY id DESC LIMIT ?",
            (f"{action_filter}%", lines)
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM audit_entries ORDER BY id DESC LIMIT ?", (lines,)
        )
    total = (await db.execute_fetchone("SELECT COUNT(*) FROM audit_entries"))[0]
    return [dict(r) for r in rows], total
```

- [ ] **Step 2: Run audit tests, commit**

---

### Task 23: Migrate Logbook to SQLite

Same pattern as Task 22 for logbook_entries table.

### Task 24: Migrate Alarms to SQLite

Modify poller.py `evaluate_alarms()` and `save_alarms()` to use SQLite. This is the most complex migration because the poller runs as a separate process — use synchronous `sqlite3` in the poller (not aiosqlite).

### Task 25: Migrate Write Log to SQLite

Replace JSONL write-log with INSERT INTO write_log.

### Task 26: Data migration script

Create a one-time script that reads existing JSONL files and imports them into SQLite.

### Task 27: Phase 2 verification

- [ ] Run full test suite (253+ tests)
- [ ] Verify audit, logbook, alarms, write-log all work
- [ ] Commit and tag `v2.0.0-beta`

---

## Phase 3: WebSocket

### Task 28: WebSocket Manager

**Files:**
- Create: `admin/websocket.py`

- [ ] **Step 1: Create WebSocket connection manager**

```python
"""WebSocket manager: connection registry, broadcast, rooms."""
import asyncio
import json
from fastapi import WebSocket
from typing import dict, set

class ConnectionManager:
    def __init__(self):
        self.connections: dict[str, set[WebSocket]] = {}  # room → connections
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, room: str = "live"):
        await ws.accept()
        async with self._lock:
            if room not in self.connections:
                self.connections[room] = set()
            self.connections[room].add(ws)

    async def disconnect(self, ws: WebSocket, room: str = "live"):
        async with self._lock:
            self.connections.get(room, set()).discard(ws)

    async def broadcast(self, data: dict, room: str = "live"):
        msg = json.dumps(data)
        async with self._lock:
            dead = []
            for ws in self.connections.get(room, set()):
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.connections[room].discard(ws)

    @property
    def client_count(self) -> int:
        return sum(len(s) for s in self.connections.values())

manager = ConnectionManager()
```

- [ ] **Step 2: Commit**

---

### Task 29: WebSocket Endpoint

**Files:**
- Modify: `admin/main.py`

- [ ] **Step 1: Add /ws/live endpoint**

```python
from fastapi import WebSocket, WebSocketDisconnect
from websocket import manager
from auth import decode_token

@app.websocket("/ws/live")
async def ws_live(ws: WebSocket, token: str = ""):
    # Authenticate via token query param
    try:
        payload = decode_token(token)
        user = payload.get("sub", "anonymous")
        plants = payload.get("plants")
    except Exception:
        await ws.close(code=4001, reason="Invalid token")
        return

    await manager.connect(ws)
    try:
        while True:
            # Keep connection alive, receive client messages (e.g., subscribe to specific devices)
            data = await ws.receive_text()
            # Handle client commands if needed
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)
```

- [ ] **Step 2: Commit**

---

### Task 30: Poller → WebSocket Push

**Files:**
- Modify: `admin/poller.py`

- [ ] **Step 1: After each poll cycle, push results via WebSocket**

The poller runs as a separate process, so it can't directly call the WebSocket manager. Options:
1. Use MQTT as the bridge (poller → MQTT → FastAPI subscriber → WebSocket)
2. Use a shared Redis pub/sub
3. Use Unix socket IPC

**Recommended: MQTT bridge** (already have it)

In main.py, add an MQTT subscriber that forwards to WebSocket:
```python
import paho.mqtt.client as mqtt
import threading

def _start_mqtt_ws_bridge():
    def on_message(client, userdata, msg):
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"topic": msg.topic, "payload": msg.payload.decode()}),
            asyncio.get_event_loop()
        )
    client = mqtt.Client()
    client.on_message = on_message
    client.connect(os.environ.get("MQTT_BROKER", "mosquitto"), 1883)
    client.subscribe("plc4x/#")
    client.loop_start()

# Start on app startup
@app.on_event("startup")
async def startup():
    threading.Thread(target=_start_mqtt_ws_bridge, daemon=True).start()
```

- [ ] **Step 2: Commit**

---

### Task 31: Frontend WebSocket Client

**Files:**
- Modify: `admin/static/js/app.js`

- [ ] **Step 1: Add WebSocket client with auto-reconnect**

```javascript
// =============================================
// WebSocket (real-time data)
// =============================================

let _ws = null;
let _wsReconnectTimer = null;
const _WS_RECONNECT_DELAY = 3000;

function wsConnect() {
    const token = getToken();
    if (!token) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws/live?token=${encodeURIComponent(token)}`;

    _ws = new WebSocket(url);

    _ws.onopen = () => {
        console.log("[WS] Connected");
        clearTimeout(_wsReconnectTimer);
        document.getElementById("statusDot")?.classList.replace("offline", "online");
    };

    _ws.onmessage = (evt) => {
        try {
            const data = JSON.parse(evt.data);
            _handleWsMessage(data);
        } catch {}
    };

    _ws.onclose = () => {
        console.log("[WS] Disconnected, reconnecting...");
        document.getElementById("statusDot")?.classList.replace("online", "offline");
        _wsReconnectTimer = setTimeout(wsConnect, _WS_RECONNECT_DELAY);
    };

    _ws.onerror = () => { _ws.close(); };
}

function _handleWsMessage(data) {
    // Update live data cache
    if (data.type === "live") {
        _updateLiveDataFromWs(data);
    }
    // Update alarm state
    if (data.type === "alarm") {
        _updateAlarmsFromWs(data);
    }
}

// Start on login
// In DOMContentLoaded: wsConnect();
```

- [ ] **Step 2: Modify live data to prefer WebSocket, fallback to REST**
- [ ] **Step 3: Modify HMI to use WebSocket data**
- [ ] **Step 4: Commit**

---

### Task 32: Final Verification and Load Test

- [ ] **Step 1: Run full test suite (253+ tests)**
- [ ] **Step 2: Load test with 60 concurrent WebSocket clients**

```bash
# Simple load test script
for i in $(seq 1 60); do
    wscat -c "ws://localhost:8080/ws/live?token=$TOKEN" &
done
```

- [ ] **Step 3: Verify memory and CPU usage**
- [ ] **Step 4: Tag release**

```bash
git tag -a v2.0.0 -m "FastAPI + SQLite + WebSocket — production-ready"
```

---

## Test Strategy

### Unit Tests (pytest)

| Test file | Covers |
|-----------|--------|
| `tests/test_auth.py` | JWT creation/validation, role checks, brute-force |
| `tests/test_config.py` | Config load/save, backup, validation |
| `tests/test_audit.py` | Audit write/read/trim/retention |
| `tests/test_models.py` | Pydantic model validation |
| `tests/test_database.py` | SQLite schema, migrations, CRUD |

### Integration Tests (bash — existing)

The existing `tests/test_all.sh` (253 tests) continues to work unchanged because the HTTP API is identical. Same URLs, same request/response formats, same status codes.

### WebSocket Tests

| Test | Verifies |
|------|----------|
| Connect with valid token | Connection accepted |
| Connect without token | Connection rejected (4001) |
| Receive live data push | Data arrives within poll interval |
| Auto-reconnect after disconnect | Reconnects within 3s |
| 60 concurrent connections | No memory leak, CPU < 50% |

---

## Migration Checklist

- [ ] Phase 1: All 90+ endpoints migrated to FastAPI
- [ ] Phase 1: 253 bash tests passing
- [ ] Phase 1: gunicorn → uvicorn in entrypoint
- [ ] Phase 1: app.py deleted
- [ ] Phase 2: audit, logbook, alarms, write-log in SQLite
- [ ] Phase 2: JSONL files no longer used
- [ ] Phase 2: Data migration script works
- [ ] Phase 3: WebSocket /ws/live working
- [ ] Phase 3: Frontend uses WebSocket with REST fallback
- [ ] Phase 3: 60 concurrent clients verified
- [ ] Phase 3: Auto-reconnect works across network blips
- [ ] All 253+ tests passing
- [ ] Docker build succeeds
- [ ] 2 monitoring rooms can connect simultaneously
