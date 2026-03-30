# Migration Plan — Review Fixes (3 rounds)

These corrections MUST be applied when implementing the migration plan.
Each fix references the original plan task number.

---

## CRITICAL FIXES (will crash or corrupt data if not applied)

### Fix 1: Python imports must use correct paths
**Affects: ALL route files (Tasks 2, 5-17, 29, 30)**

The app runs with `WORKDIR /app` and uvicorn launched as `uvicorn main:app` from `/app/admin/`.
All imports must be relative to the `admin/` directory.

- Create `admin/routes/__init__.py` (empty file — required for package)
- All route files use: `from auth import ...` (works because CWD = admin/)
- Rename `admin/websocket.py` → `admin/ws_manager.py` (avoid stdlib name conflict)
- Set in Dockerfile or entrypoint: `WORKDIR /app/admin`
- Uvicorn command: `uvicorn main:app` (not `admin.main:app`)

### Fix 2: `from typing import dict, set` is invalid
**Affects: Task 28 (websocket.py → ws_manager.py)**

Remove the import line entirely. Python 3.12 supports `dict[str, set[WebSocket]]` natively.

```python
# WRONG:
from typing import dict, set

# CORRECT (Python 3.12):
# No import needed — dict and set are builtins
class ConnectionManager:
    def __init__(self):
        self.connections: dict[str, set] = {}
```

### Fix 3: aiosqlite API does not have execute_fetchall/execute_fetchone
**Affects: Task 22 (audit SQLite migration)**

```python
# WRONG:
rows = await db.execute_fetchall("SELECT ...", params)
total = (await db.execute_fetchone("SELECT COUNT(*) ..."))[0]

# CORRECT:
async with db.execute("SELECT ...", params) as cursor:
    rows = await cursor.fetchall()

async with db.execute("SELECT COUNT(*) FROM audit_entries") as cursor:
    row = await cursor.fetchone()
    total = row[0]
```

### Fix 4: asyncio.get_event_loop() crashes in Python 3.12 from a thread
**Affects: Task 30 (MQTT → WebSocket bridge)**

```python
# WRONG:
def _start_mqtt_ws_bridge():
    def on_message(client, userdata, msg):
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(...),
            asyncio.get_event_loop()  # CRASHES in thread
        )

# CORRECT:
def _start_mqtt_ws_bridge(loop):
    def on_message(client, userdata, msg):
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(...),
            loop  # Captured from main thread
        )
    # ...

# In lifespan:
@asynccontextmanager
async def lifespan(app):
    loop = asyncio.get_running_loop()
    threading.Thread(target=_start_mqtt_ws_bridge, args=(loop,), daemon=True).start()
    yield
```

### Fix 5: WebSocket close before accept raises RuntimeError
**Affects: Task 29 (WebSocket endpoint)**

```python
# WRONG:
@app.websocket("/ws/live")
async def ws_live(ws: WebSocket, token: str = ""):
    try:
        payload = decode_token(token)
    except:
        await ws.close(code=4001)  # NOT ACCEPTED YET — crashes
        return
    await manager.connect(ws)  # accept() is inside connect()

# CORRECT:
@app.websocket("/ws/live")
async def ws_live(ws: WebSocket, token: str = ""):
    await ws.accept()
    try:
        payload = decode_token(token)
    except:
        await ws.close(code=4001, reason="Invalid token")
        return
    # ... proceed with authenticated connection
```

### Fix 6: Multi-worker uvicorn breaks WebSocket ConnectionManager
**Affects: Task 19 (entrypoint)**

Use `--workers 1` for both HTTP and HTTPS. FastAPI async handles concurrency within a single process.

```bash
# WRONG:
gosu plc4x uvicorn main:app --port 8080 --workers 2

# CORRECT:
gosu plc4x uvicorn main:app --port 8080 --workers 1 --limit-max-requests 5000
gosu plc4x uvicorn main:app --port 8443 --workers 1 --ssl-keyfile ... --limit-max-requests 5000
```

`--limit-max-requests 5000` replaces gunicorn's `--max-requests` for memory leak prevention.

### Fix 7: audit_log sync→async breakage in Phase 2
**Affects: Task 18 (middleware) + Task 22 (SQLite migration)**

When `audit_log` becomes async in Phase 2, the middleware must `await` it.

```python
# Phase 1 (sync):
audit_log(action, user=user, ip=ip)

# Phase 2 (async) — middleware must be updated:
await audit_log(action, user=user, ip=ip)
```

Add explicit note in Task 22: "Update AuditMiddleware in main.py to `await audit_log(...)`"

### Fix 8: Global _db singleton is unsafe for concurrent async
**Affects: Task 21 (database.py)**

```python
# WRONG:
_db = None
async def get_db():
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
    return _db

# CORRECT: Initialize in lifespan, store in app.state
@asynccontextmanager
async def lifespan(app):
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA wal_autocheckpoint=400")
    await _run_migrations(db)
    app.state.db = db
    yield
    await db.close()

# FastAPI dependency:
async def get_db(request: Request):
    return request.app.state.db
```

---

## IMPORTANT FIXES (will cause regressions or security issues)

### Fix 9: load_admin_credentials() not ported
**Affects: Task 2 (auth.py)**

Add startup credential loading:
```python
# In auth.py, add a function:
def load_admin_credentials():
    creds_path = os.path.join(
        os.environ.get("CONFIG_PATH", "/app/config/config.yml").rsplit("/", 1)[0],
        ".admin-credentials"
    )
    if os.path.exists(creds_path):
        import yaml
        with open(creds_path, "r") as f:
            creds = yaml.safe_load(f)
        if creds and "password" in creds:
            global ADMIN_PASSWORD
            ADMIN_PASSWORD = creds["password"]
            USERS[ADMIN_USERNAME]["password"] = creds["password"]

# Call in lifespan:
load_admin_credentials()
```

### Fix 10: AuditMiddleware cannot access authenticated user
**Affects: Task 18**

The dependency `get_current_user` runs inside route handlers, not middleware.
Solution: Run auth in middleware BEFORE call_next:

```python
class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Pre-authenticate for audit purposes
        if request.url.path.startswith("/api/"):
            try:
                auth_header = request.headers.get("authorization", "")
                if auth_header.startswith("Bearer "):
                    from auth import decode_token
                    payload = decode_token(auth_header[7:])
                    request.state.audit_user = payload.get("sub", "unknown")
                else:
                    request.state.audit_user = "unknown"
            except:
                request.state.audit_user = "unknown"

        response = await call_next(request)

        # Log write operations
        if (request.method in ("POST", "PUT", "DELETE")
            and request.url.path.startswith("/api/")
            and response.status_code < 400):
            audit_log(
                f"{request.method} {request.url.path}",
                user=getattr(request.state, "audit_user", "unknown"),
                ip=request.client.host if request.client else "",
            )
        return response
```

### Fix 11: Rate limiting not applied to login + missing exception handler
**Affects: Task 2 + Task 18**

```python
# In main.py:
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# In auth_routes.py:
from main import limiter

@router.post("/login")
@limiter.limit("30/minute")
async def login(req: LoginRequest, request: Request):
    ...
```

### Fix 12: Brute-force protection is a complete stub
**Affects: Task 2 (auth.py)**

The plan declares brute-force protection but never implements it. Copy the full logic from current app.py (lines 446-528): file-based failure counting, IP lockout after 5 attempts for 5 minutes.

### Fix 13: @app.on_event("startup") is deprecated
**Affects: Task 30**

Use `lifespan` context manager instead (shown in Fix 4 above).

### Fix 14: _trim_audit() is a pass stub
**Affects: Task 4 (audit.py)**

Copy the `_trim_jsonl_file` implementation from current app.py. Do not leave as `pass`.

### Fix 15: USERS_JSON format must remain backward-compatible
**Affects: Task 2 (auth.py)**

Support BOTH formats (list and dict):
```python
_users_json = os.environ.get("USERS_JSON", "")
if _users_json:
    parsed = json.loads(_users_json)
    if isinstance(parsed, list):
        for u in parsed:
            USERS[u["username"]] = {...}
    elif isinstance(parsed, dict):
        for uname, udata in parsed.items():
            USERS[uname] = {...}
```

### Fix 16: Missing routes — plc4x version + templates
**Affects: Task 17**

Create `admin/routes/plc4x_routes.py` for:
- GET /api/plc4x/version
- GET /api/plc4x/latest-version
- POST /api/plc4x/update
- POST /api/plc4x/rollback
- GET /api/templates

### Fix 17: expiresIn hardcoded, ignores JWT_EXPIRY_HOURS
**Affects: Task 2 (auth_routes.py)**

```python
# WRONG:
"expiresIn": 24 * 3600

# CORRECT:
from auth import JWT_EXPIRY_HOURS
"expiresIn": JWT_EXPIRY_HOURS * 3600
```

### Fix 18: No SQLite backup
**Affects: Task 21**

Add nightly SQLite backup:
```python
import sqlite3
def backup_database():
    src = sqlite3.connect(DB_PATH)
    ts = datetime.date.today().isoformat()
    dst = sqlite3.connect(f"{DB_PATH}.{ts}.bak")
    src.backup(dst)
    src.close()
    dst.close()
```

Run via a background task in the lifespan.

### Fix 19: No HEALTHCHECK in Docker
**Affects: Task 19 (Dockerfile)**

Add:
```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -f http://localhost:8080/healthz || exit 1
```

### Fix 20: JWT secret should be required or auto-persisted
**Affects: Task 2 (auth.py)**

Persist to config volume if not set:
```python
_jwt_path = os.path.join(CONFIG_DIR, ".jwt-secret")
if not os.environ.get("JWT_SECRET"):
    if os.path.exists(_jwt_path):
        with open(_jwt_path) as f:
            JWT_SECRET = f.read().strip()
    else:
        JWT_SECRET = secrets.token_hex(32)
        with open(_jwt_path, "w") as f:
            f.write(JWT_SECRET)
else:
    JWT_SECRET = os.environ["JWT_SECRET"]
```

### Fix 21: WebSocket reconnect needs exponential backoff + jitter
**Affects: Task 31 (app.js)**

```javascript
let _wsReconnectAttempts = 0;

_ws.onclose = () => {
    _wsReconnectAttempts++;
    const delay = Math.min(30000, 3000 * Math.pow(1.5, _wsReconnectAttempts - 1))
        + Math.random() * 1000;
    _wsReconnectTimer = setTimeout(wsConnect, delay);
};

_ws.onopen = () => {
    _wsReconnectAttempts = 0;
};
```

### Fix 22: datetime.utcnow() deprecated in Python 3.12
**Affects: Task 2, Task 4, everywhere**

```python
# WRONG:
datetime.datetime.utcnow()

# CORRECT:
datetime.datetime.now(datetime.timezone.utc)
```

---

## SUGGESTIONS (improve quality)

### S1: Add WebSocket server-side ping (Task 29)
Detect dead connections after NAT/firewall timeout:
```python
async def _ws_heartbeat(ws):
    while True:
        await asyncio.sleep(30)
        await ws.send_text('{"type":"ping"}')
```

### S2: Keep JSONL files for 30 days after Phase 2 migration
Allow emergency rollback to Phase 1 if SQLite has issues.

### S3: Add schema_version table for future migrations (Task 21)
```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
```

### S4: Docker image tag before Phase 1 starts
```bash
docker tag plc4x-manager-plc4x-admin:latest plc4x-manager-plc4x-admin:flask-v1.0.0
```

### S5: Document that bash tests only pass after ALL routes migrated (Task 20)
Tasks 5-17 will have partial failures. Full 253/253 only after Task 17.

### S6: Phase 2 deployment during planned maintenance window
Alarm state migration (Task 24) has a window where poller writes to old format but API reads new format. Deploy during shift change.

---

## Summary

| Category | Count |
|----------|-------|
| Critical fixes (crash/data loss) | 8 |
| Important fixes (regressions/security) | 14 |
| Suggestions | 6 |
| **Total issues found across 3 reviews** | **28** |

All fixes are documented above with correct code. Apply these when implementing each task.
