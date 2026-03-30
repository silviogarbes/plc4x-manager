# Security Review: FastAPI + SQLite + WebSocket Migration Plan

**Reviewer:** Application Security Engineer
**Date:** 2026-03-29
**Scope:** Migration plan + review-fixes document, cross-referenced against current `app.py`
**System classification:** Industrial monitoring (SCADA-adjacent), 24/7 operation, 20-year lifecycle

---

## Executive Summary

The migration plan is functionally sound but introduces **6 CRITICAL**, **9 HIGH**, and **8 MEDIUM** security findings. The most dangerous gaps are: (1) no JWT revocation mechanism, meaning a stolen token grants access until natural expiry; (2) the WebSocket endpoint accepts connections before authentication, creating a DoS surface; (3) the plan drops several security controls that exist today without explicitly replacing them; and (4) the subprocess/Docker socket exposure remains unmitigated. Every finding below includes the specific attack scenario and a concrete fix.

---

## Finding 1: No JWT Revocation / Logout Is a No-Op

**Severity:** CRITICAL
**Affects:** Task 2 (auth.py, auth_routes.py)
**Current state:** The existing `app.py` also lacks revocation, but the migration is an opportunity to fix this for a 20-year system.

**Attack scenario:** An operator's laptop is stolen from the monitoring room. The JWT token stored in the browser (localStorage) is valid for 24 hours. The attacker walks in with the laptop and has full operator access to write PLC values. There is no way to invalidate the token remotely.

**Fix:** Implement a server-side token blocklist using SQLite (Phase 2 provides it). Until Phase 2, use an in-memory set that survives within the single uvicorn worker.

```python
# In auth.py:
_revoked_jti: set[str] = set()

def create_token(username: str, role: str, plants: list | None = None) -> str:
    jti = secrets.token_hex(16)
    payload = {
        "sub": username,
        "role": role,
        "jti": jti,
        "iat": datetime.datetime.now(datetime.timezone.utc),
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=JWT_EXPIRY_HOURS),
    }
    if plants:
        payload["plants"] = plants
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def revoke_token(jti: str):
    _revoked_jti.add(jti)

def decode_token(token: str) -> dict:
    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    if payload.get("jti") in _revoked_jti:
        raise JWTError("Token has been revoked")
    return payload

# In auth_routes.py:
@router.post("/logout")
async def logout(user: CurrentUser = Depends(get_current_user), authorization: str = Header()):
    token = authorization[7:]  # Strip "Bearer "
    payload = decode_token(token)
    revoke_token(payload["jti"])
    audit_log("logout", user=user.username)
    return {"message": "Logged out"}
```

In Phase 2, migrate `_revoked_jti` to a SQLite table with TTL-based cleanup:
```sql
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti TEXT PRIMARY KEY,
    revoked_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    expires_at TEXT NOT NULL
);
```

---

## Finding 2: WebSocket Accept-Before-Auth Enables DoS

**Severity:** CRITICAL
**Affects:** Task 29 (WebSocket endpoint), Fix 5 in review-fixes

**Problem:** Fix 5 in the review-fixes document instructs: "accept() first, then validate token." This is correct for avoiding the RuntimeError, but it creates a DoS vector: an attacker can open thousands of WebSocket connections without providing any token, and each connection consumes server memory after `accept()`.

**Attack scenario:** An attacker on the plant network runs `for i in $(seq 1 10000); do wscat -c "ws://target:8080/ws/live" & done`. Each connection is accepted. The server runs out of memory, crashing the monitoring system. 60 shift operators lose visibility into PLC state.

**Fix:** Accept, then authenticate, but immediately close on failure AND enforce a global connection limit:

```python
# In ws_manager.py:
MAX_CONNECTIONS = 200  # More than enough for 60 clients + headroom
MAX_UNAUTHENTICATED_MS = 2000  # 2 seconds to authenticate

class ConnectionManager:
    def __init__(self):
        self.connections: dict[str, set] = {}
        self._lock = asyncio.Lock()
        self._total = 0

    async def connect(self, ws: WebSocket, room: str = "live") -> bool:
        async with self._lock:
            if self._total >= MAX_CONNECTIONS:
                return False
            self._total += 1
            if room not in self.connections:
                self.connections[room] = set()
            self.connections[room].add(ws)
        return True

    async def disconnect(self, ws: WebSocket, room: str = "live"):
        async with self._lock:
            if ws in self.connections.get(room, set()):
                self.connections[room].discard(ws)
                self._total -= 1

# In main.py WebSocket endpoint:
@app.websocket("/ws/live")
async def ws_live(ws: WebSocket, token: str = ""):
    await ws.accept()
    # Validate immediately after accept
    try:
        payload = decode_token(token)
    except Exception:
        await ws.close(code=4001, reason="Invalid token")
        return

    if not await manager.connect(ws):
        await ws.close(code=4002, reason="Connection limit reached")
        return

    try:
        while True:
            data = await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)
```

---

## Finding 3: Brute-Force Protection Is a Declared Stub

**Severity:** CRITICAL
**Affects:** Task 2 (auth.py), Fix 12 in review-fixes

**Problem:** The plan's auth.py declares `_LOGIN_FAILURES_PATH` and `_LOGIN_LOCK` but never implements the actual lockout logic. Fix 12 says "copy the full logic from current app.py" but does not provide the code. The current `app.py` has a complete implementation (lines 476-568) with file-based tracking, 5-attempt lockout, and 5-minute cooldown. If the implementer follows only the plan's code, the login endpoint has zero brute-force protection.

**Attack scenario:** Attacker runs hydra or a simple loop against `/api/auth/login` with a password dictionary. Even with slowapi rate limiting (30/min), that is 43,200 attempts per day from a single IP. With a weak password, access is gained in hours.

**Fix:** The plan's auth_routes.py `login()` function must include the full lockout logic. Here is the corrected version:

```python
@router.post("/login")
@limiter.limit("30/minute")
async def login(req: LoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()

    # Check lockout
    with _LOGIN_LOCK:
        failures = _load_login_failures()
    fail_info = failures.get(client_ip, {})
    if fail_info.get("locked_until", 0) > now:
        remaining = int(fail_info["locked_until"] - now)
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {remaining} seconds."
        )

    user_entry = USERS.get(req.username)
    stored_pass = user_entry["password"] if user_entry else "dummy"
    if user_entry and hmac.compare_digest(req.password, stored_pass):
        # Clear failures on success
        with _LOGIN_LOCK:
            failures = _load_login_failures()
            failures.pop(client_ip, None)
            _save_login_failures(failures)
        role = user_entry.get("role", "operator")
        plants = user_entry.get("plants")
        token = create_token(req.username, role, plants)
        return {"token": token, "username": req.username, "role": role,
                "expiresIn": JWT_EXPIRY_HOURS * 3600,
                **({"plants": plants} if plants else {})}

    # Track failure
    with _LOGIN_LOCK:
        failures = _load_login_failures()
        if client_ip not in failures:
            failures[client_ip] = {"count": 0, "locked_until": 0}
        failures[client_ip]["count"] += 1
        if failures[client_ip]["count"] >= 5:
            failures[client_ip]["locked_until"] = now + 300
        _save_login_failures(failures)

    raise HTTPException(status_code=401, detail="Invalid credentials")
```

---

## Finding 4: Timing Oracle in Basic Auth Credential Comparison

**Severity:** HIGH
**Affects:** Task 2 (auth.py, `get_current_user`)

**Problem:** In the plan's `get_current_user`, the Basic Auth branch compares passwords using `hmac.compare_digest(password, udata["password"])`. However, it iterates over USERS with `for uname, udata in USERS.items(): if uname == username and ...`. The username comparison `uname == username` is NOT constant-time. An attacker can enumerate valid usernames by measuring response times.

The current `app.py` (line 233) uses `hmac.compare_digest(username, uname)` for the username too, which is correct.

**Fix:**
```python
# In get_current_user, Basic Auth branch:
for uname, udata in USERS.items():
    if hmac.compare_digest(uname, username) and hmac.compare_digest(password, udata["password"]):
        return CurrentUser(uname, udata.get("role", "operator"), udata.get("plants"))
```

---

## Finding 5: JWT Secret Not Persisted Across Restarts (Default Path)

**Severity:** CRITICAL
**Affects:** Task 2 (auth.py), Fix 20 in review-fixes

**Problem:** The plan has `JWT_SECRET = os.environ.get("JWT_SECRET") or secrets.token_hex(32)`. If the environment variable is not set (which is the common case in Docker Compose when people forget), a new secret is generated on every container restart. This invalidates ALL existing JWT tokens, forcibly logging out every operator and monitor. In a 24/7 monitoring room during a shift with active alarms, this is a safety incident.

Fix 20 in the review-fixes provides a file-persistence mechanism, which is good. But it must be treated as CRITICAL, not just "important."

**Additional concern:** The persisted file (`/app/config/.jwt-secret`) is stored on a volume mount. If the volume is shared or readable by other containers, the JWT secret is exposed.

**Fix:** Apply Fix 20 exactly as written, AND:
```python
# After writing the file, set permissions:
os.chmod(_jwt_path, 0o600)
```

Also add to the Dockerfile:
```dockerfile
# Ensure config dir is owned by plc4x user
RUN chown -R plc4x:plc4x /app/config
```

---

## Finding 6: SQLite Injection via `executescript` in Migrations

**Severity:** HIGH
**Affects:** Task 21 (database.py)

**Problem:** The plan uses `db.executescript(...)` for schema creation. This is safe for static DDL. However, if anyone later adds a migration that interpolates user input (e.g., a data migration script in Task 26 that reads from JSONL files with user-provided data), `executescript` does not support parameterized queries. The pattern established here encourages unsafe habits.

**Fix:** Use individual `db.execute()` calls instead of `executescript`:
```python
async def _run_migrations(db):
    statements = [
        """CREATE TABLE IF NOT EXISTS audit_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            user TEXT NOT NULL DEFAULT 'system',
            action TEXT NOT NULL,
            ip TEXT DEFAULT '',
            details TEXT DEFAULT '{}'
        )""",
        "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_entries(timestamp)",
        # ... each statement separately
    ]
    for stmt in statements:
        await db.execute(stmt)
    await db.commit()
```

Also: The data migration script (Task 26) MUST use parameterized queries when inserting JSONL data:
```python
# WRONG:
await db.execute(f"INSERT INTO audit_entries (user, action) VALUES ('{entry['user']}', '{entry['action']}')")

# CORRECT:
await db.execute("INSERT INTO audit_entries (user, action, ip, details) VALUES (?, ?, ?, ?)",
                 (entry["user"], entry["action"], entry.get("ip", ""), json.dumps(entry.get("details", {}))))
```

---

## Finding 7: Flux Injection Protection Not Ported to Plan

**Severity:** HIGH
**Affects:** Tasks 15-16 (OEE routes, data/export routes)

**Problem:** The current `app.py` has `_safe_flux_str()` (line 2869) which validates that Flux string interpolation values match `^[\w\-\.]+$`. The migration plan does not mention this function anywhere. If the route migration (Tasks 15-16) does not port this validation, the new code will be vulnerable to Flux injection.

**Attack scenario:** An attacker sends `GET /api/export/csv?device=x" |> yield(name: "steal") //` which, without `_safe_flux_str`, could alter the Flux query to exfiltrate data from any bucket.

**Fix:** Add to the migration checklist for Tasks 15-16:
```python
# In a shared utils.py or in data_routes.py:
import re
_SAFE_FLUX_RE = re.compile(r'^[\w\-\.]+$')

def safe_flux_str(value: str) -> str:
    """Validate that a value is safe to embed in a Flux string literal."""
    if not value or not _SAFE_FLUX_RE.match(value):
        raise ValueError(f"Invalid filter value: {value!r}")
    return value
```

Every Flux query that interpolates user input MUST call this function. Add a code review checkpoint to Tasks 15-16 specifically for this.

---

## Finding 8: No CORS Configuration

**Severity:** HIGH
**Affects:** Task 18 (middleware)

**Problem:** The plan's Task 18 title says "CORS" but the code only shows audit middleware and rate limiting. No CORS middleware is actually configured. The current `app.py` also has no CORS headers. Since this is a same-origin application (SPA served from the same host), the lack of CORS is actually the CORRECT default -- but FastAPI's docs frequently suggest adding `CORSMiddleware` with `allow_origins=["*"]`, which would be catastrophic.

**Fix:** Explicitly configure restrictive CORS to prevent accidental permissive configuration later:

```python
# In main.py:
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],  # NO cross-origin requests allowed
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)
```

Add a comment: `# SECURITY: This is an industrial system. NEVER set allow_origins=["*"].`

---

## Finding 9: WebSocket Token Passed in URL Query Parameter

**Severity:** HIGH
**Affects:** Task 29 (WebSocket endpoint), Task 31 (frontend JS)

**Problem:** The plan passes the JWT token as `?token=...` in the WebSocket URL. This token will appear in:
- Server access logs (uvicorn logs the full URL)
- Proxy/load balancer logs
- Browser history
- Network monitoring tools

In a 24/7 monitoring room with shared workstations, a monitor-role user could see an admin token in the browser URL bar and escalate privileges.

**Fix:** WebSocket does not support custom headers in the browser API, so query params are the standard approach. Mitigate the log exposure:

```python
# In uvicorn config (entrypoint.sh):
gosu plc4x uvicorn main:app --host 0.0.0.0 --port 8080 --workers 1 \
    --access-log --log-level warning  # warning level does not log query params

# In the WebSocket endpoint, use short-lived exchange tokens:
@router.post("/api/auth/ws-ticket")
async def ws_ticket(user: CurrentUser = Depends(get_current_user)):
    """Issue a single-use, 30-second ticket for WebSocket auth."""
    ticket = secrets.token_hex(16)
    _ws_tickets[ticket] = {
        "user": user.username,
        "role": user.role,
        "plants": user.plants,
        "expires": time.time() + 30,
    }
    return {"ticket": ticket}

# In WebSocket endpoint, validate ticket instead of full JWT:
@app.websocket("/ws/live")
async def ws_live(ws: WebSocket, ticket: str = ""):
    await ws.accept()
    entry = _ws_tickets.pop(ticket, None)
    if not entry or entry["expires"] < time.time():
        await ws.close(code=4001, reason="Invalid or expired ticket")
        return
    # entry["user"], entry["role"], entry["plants"] are now available
```

---

## Finding 10: Docker Socket Exposure Enables Container Escape

**Severity:** CRITICAL
**Affects:** Task 12 (server_routes.py), Task 17 (version_routes.py)

**Problem:** The admin container mounts `/var/run/docker.sock`. The plan ports the Docker SDK calls for container restart, log viewing, PLC4X version update, and service status checks. Anyone with admin access to the web UI can:
1. Restart arbitrary containers
2. View logs of any container
3. Execute arbitrary operations via the Docker socket

The `PLC4X_CONTAINER` name is loaded from an environment variable but is used in `client.containers.get(PLC4X_CONTAINER)`. If an attacker can modify this env var (or if there is a bug allowing container name injection), they can interact with any container on the host.

**Attack scenario:** An admin-level compromise (stolen credentials) allows the attacker to use the Docker socket to create a new privileged container that mounts the host filesystem, achieving full host compromise.

**Fix:** This is an architecture-level issue. For the migration:

1. Validate container names against an allowlist:
```python
ALLOWED_CONTAINERS = {
    os.environ.get("PLC4X_CONTAINER", "plc4x-server"),
    "mosquitto",
    "influxdb",
    "grafana",
    "plc4x-ml",
}

def get_container_safe(name: str):
    if name not in ALLOWED_CONTAINERS:
        raise HTTPException(status_code=400, detail="Invalid container name")
    client = get_docker_client()
    return client.containers.get(name)
```

2. In Docker Compose, use `read_only: true` where possible.
3. Long-term: Consider a Docker socket proxy like [Tecnativa/docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) that restricts API calls to only `GET /containers`, `POST /containers/{id}/restart`, and `GET /containers/{id}/logs`.

---

## Finding 11: Subprocess Command Injection in Git/Docker-Compose Calls

**Severity:** HIGH
**Affects:** Task 17 (version_routes.py — manager update/rollback)

**Problem:** The current `app.py` (lines 3717-3718, 3767-3768) uses:
```python
_subprocess.Popen(["sh", "-c", "sleep 2 && docker-compose ... > /tmp/manager-update.log 2>&1"])
```

The `sh -c` invocation with string concatenation is safe in the current code because the command is hardcoded. However, `_run_git()` passes arguments as a list (safe), but the rollback endpoint (line 3761) passes `prev_tag` from git output to `git reset --hard`. If a malicious git tag contains shell metacharacters, this could be exploited (though `subprocess.run` with list args mitigates this).

**More concerning:** The docker-compose rebuild command runs `docker-compose build && docker-compose up -d` with access to the full Docker daemon. The plan does not mention sandboxing this.

**Fix:** For the migration, ensure:
```python
# Always use list-form subprocess, never shell=True:
def _run_rebuild():
    """Run rebuild in background. NEVER interpolate user input."""
    _subprocess.Popen(
        ["docker-compose", "-f", "/app/repo/docker-compose.yml", "build"],
        cwd=_REPO_DIR,
        stdout=open("/tmp/manager-update.log", "w"),
        stderr=_subprocess.STDOUT,
    )

# Validate git tag format before using in git reset:
def _is_safe_tag(tag: str) -> bool:
    return bool(re.match(r'^v[\d]+\.[\d]+\.[\d]+(-[a-zA-Z0-9.]+)?$', tag))
```

---

## Finding 12: No CSRF Protection on State-Changing Endpoints

**Severity:** MEDIUM
**Affects:** All POST/PUT/DELETE routes (Tasks 5-17)

**Problem:** The API uses JWT Bearer tokens, which provides implicit CSRF protection because browsers do not automatically attach Authorization headers on cross-origin requests. HOWEVER, the API also supports:
1. **Cookie-based sessions?** No, JWT is in localStorage. OK.
2. **Basic Auth** -- browsers CAN send Basic Auth headers automatically after a prompt.
3. **Query parameter tokens** for download endpoints.

If Basic Auth is cached by the browser and an attacker tricks a user into visiting a malicious page, the browser could send authenticated requests to the API.

**Fix:** Add `SameSite` headers and verify the `Origin` header on state-changing requests:

```python
# In AuditMiddleware or a dedicated CSRF middleware:
class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "DELETE"):
            origin = request.headers.get("origin", "")
            referer = request.headers.get("referer", "")
            host = request.headers.get("host", "")
            # Allow same-origin only
            if origin and not origin.endswith(host):
                return JSONResponse({"error": "CSRF check failed"}, status_code=403)
        return await call_next(request)
```

---

## Finding 13: Pydantic Models Missing for Many Endpoints

**Severity:** MEDIUM
**Affects:** Tasks 5-17 (route migration)

**Problem:** The plan's `models.py` (Task 1) defines models for `LoginRequest`, `DeviceCreate`, `TagCreate`, `LogbookEntry`, and `OEEConfig`. But the plan has 90+ endpoints. Many critical endpoints lack Pydantic models:
- PUT `/api/config` (accepts arbitrary config dict)
- PUT `/api/config/server` (accepts server settings)
- PUT `/api/hmi/config` (accepts full HMI config)
- POST `/api/live/write` (accepts device, tag, value)
- POST `/api/data/write` (accepts InfluxDB data points)
- PUT `/api/devices/<name>/tags/<alias>/alarms` (accepts threshold config)

Without Pydantic models, these endpoints accept arbitrary JSON, which means:
- No type validation (a string where a number is expected)
- No length limits (a 100MB JSON payload)
- No field allowlisting (unexpected fields are accepted)

**Fix:** Create Pydantic models for ALL write endpoints. At minimum:

```python
class LiveWriteRequest(BaseModel):
    device: str = Field(min_length=1, max_length=128, pattern=r'^[\w\-\.]+$')
    tag: str = Field(min_length=1, max_length=128, pattern=r'^[\w\-\.]+$')
    value: float | int | str | bool

class ConfigUpdate(BaseModel):
    version: str = Field(default="0.8", max_length=10)
    name: str = Field(min_length=1, max_length=100)
    tcpPort: int = Field(ge=1, le=65535)
    disableInsecureEndpoint: bool = False
    dir: str = Field(default="/app/security", max_length=255)
    devices: list[dict]  # Further validated in route handler

    model_config = {"extra": "forbid"}  # Reject unknown fields

class DataWriteRequest(BaseModel):
    measurement: str = Field(min_length=1, max_length=128, pattern=r'^[\w\-\.]+$')
    tags: dict[str, str] = {}
    fields: dict[str, float | int | str | bool]
    timestamp: str | None = None
```

Also add a global request size limit in uvicorn or middleware:
```python
# In main.py:
@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 10 * 1024 * 1024:  # 10MB
        return JSONResponse({"error": "Request too large"}, status_code=413)
    return await call_next(request)
```

---

## Finding 14: Path Parameter Injection in Device/Tag Routes

**Severity:** MEDIUM
**Affects:** Tasks 6-7 (device_routes.py, tag routes)

**Problem:** Endpoints like `PUT /api/devices/<name>` and `DELETE /api/devices/<device_name>/tags/<alias>` accept path parameters that are used to look up devices in the YAML config. The current `app.py` validates device names with `validate_name()` on creation but NOT on lookup (e.g., line 1036: `_find_device(config, name)` accepts whatever the URL provides).

In the FastAPI migration, path parameters are strings by default with no validation.

**Attack scenario:** While not directly exploitable for injection (the lookup is a dict/list iteration), malformed path parameters could cause unexpected behavior in logging, audit trails, or error messages (log injection).

**Fix:** Add path parameter validation using FastAPI's `Path`:
```python
from fastapi import Path

NAME_PATTERN = r'^[\w\-\.]{1,128}$'

@router.put("/api/devices/{name}")
async def update_device(
    name: str = Path(pattern=NAME_PATTERN),
    ...
):
```

---

## Finding 15: Audit Trail Integrity Not Protected

**Severity:** MEDIUM
**Affects:** Task 4 (audit.py), Task 22 (SQLite migration)

**Problem:** Audit entries are stored in a JSONL file (Phase 1) or SQLite database (Phase 2) with no integrity protection. An attacker who gains file-system access (via Docker socket, container escape, or compromised volume mount) can:
1. Delete audit entries to cover tracks
2. Modify timestamps to create false alibis
3. Insert fake entries to frame another user

For a 20-year industrial system subject to IEC 62443 compliance, audit trail integrity is essential.

**Fix (Phase 2 SQLite):**
```python
import hashlib

async def audit_log(action, details=None, user="system", ip=""):
    db = await get_db()
    # Get hash of previous entry for chain integrity
    async with db.execute("SELECT id, chain_hash FROM audit_entries ORDER BY id DESC LIMIT 1") as cursor:
        prev = await cursor.fetchone()
    prev_hash = prev["chain_hash"] if prev else "GENESIS"

    entry_data = f"{user}|{action}|{ip}|{json.dumps(details or {})}|{prev_hash}"
    chain_hash = hashlib.sha256(entry_data.encode()).hexdigest()

    await db.execute(
        "INSERT INTO audit_entries (user, action, ip, details, chain_hash) VALUES (?, ?, ?, ?, ?)",
        (user, action, ip, json.dumps(details or {}), chain_hash)
    )
    await db.commit()
```

Add `chain_hash TEXT` column to the audit_entries schema. This creates a hash chain that makes tampering detectable (any modification breaks the chain).

---

## Finding 16: SQLite Database Not Encrypted at Rest

**Severity:** MEDIUM
**Affects:** Task 21 (database.py)

**Problem:** The SQLite database will contain audit trails, alarm history, logbook entries, and write logs. This data is stored in plaintext on the Docker volume. Anyone with access to the volume (host administrator, backup operator, or via a container escape) can read all historical data.

For IEC 62443 SL-2+ compliance, data at rest should be encrypted.

**Fix:** Use SQLCipher instead of standard SQLite:
```txt
# In requirements.txt, replace aiosqlite with:
sqlcipher3==0.5.2
aiosqlite==0.20.0  # aiosqlite wraps whatever sqlite3 module is available
```

If SQLCipher is too complex for the deployment, at minimum ensure the volume is on an encrypted filesystem (LUKS on Linux, BitLocker on Windows) and document this as a deployment requirement.

---

## Finding 17: MQTT Bridge Has No Authentication

**Severity:** HIGH
**Affects:** Task 30 (MQTT -> WebSocket bridge)

**Problem:** The plan's MQTT bridge code (Task 30) connects to Mosquitto without credentials:
```python
client = mqtt.Client()
client.connect(os.environ.get("MQTT_BROKER", "mosquitto"), 1883)
client.subscribe("plc4x/#")
```

If Mosquitto is configured without authentication (common default), any device on the network can publish messages to `plc4x/#` topics. These messages are then forwarded to all WebSocket clients, meaning an attacker can inject fake PLC data into every operator's screen.

**Attack scenario:** Attacker on the plant network publishes `{"device":"Boiler-1","tag":"Temperature","value":25.0}` to `plc4x/data` while the actual boiler temperature is 400C. Operators see normal readings and miss a critical alarm.

**Fix:**
```python
def _start_mqtt_ws_bridge(loop):
    client = mqtt.Client()
    mqtt_user = os.environ.get("MQTT_USER", "")
    mqtt_pass = os.environ.get("MQTT_PASSWORD", "")
    if mqtt_user:
        client.username_pw_set(mqtt_user, mqtt_pass)
    # Use TLS if configured
    mqtt_tls = os.environ.get("MQTT_TLS", "false").lower() == "true"
    if mqtt_tls:
        client.tls_set()
    client.connect(os.environ.get("MQTT_BROKER", "mosquitto"),
                   int(os.environ.get("MQTT_PORT", "1883")))
    client.subscribe("plc4x/#")
    client.loop_start()
```

Also ensure the Mosquitto container is configured with ACLs:
```
# mosquitto.conf
allow_anonymous false
password_file /mosquitto/config/password_file
```

---

## Finding 18: `healthz` Endpoint Bypasses Auth but Leaks Info

**Severity:** LOW
**Affects:** Task 1 (main.py), Task 2 (auth.py)

**Problem:** The `/healthz` endpoint is unauthenticated (correct for Docker HEALTHCHECK). The plan's auth.py `get_current_user` also skips auth for `/healthz`. But in the plan's code, the healthz endpoint returns `{"status": "ok"}` which is fine.

**Risk:** Future developers might add database connection status, version info, or internal IP to the health endpoint. For an industrial system, even basic information disclosure helps attackers fingerprint the system.

**Fix:** Keep `/healthz` minimal and add a separate `/api/system/health` endpoint that requires authentication:
```python
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}  # NEVER add more fields here

@app.get("/api/system/health")
async def system_health(user: CurrentUser = Depends(require_admin)):
    # Detailed health for authenticated admins only
    return {"status": "ok", "db": "connected", "version": "..."}
```

---

## Finding 19: Supply Chain -- python-jose Has Known CVEs

**Severity:** HIGH
**Affects:** Task 1 (requirements.txt)

**Problem:** The plan specifies `python-jose[cryptography]==3.3.0`. This package:
1. Has had CVEs related to algorithm confusion attacks (CVE-2024-33664, CVE-2024-33663)
2. Is unmaintained (last release 2024, limited activity)
3. The `[cryptography]` extra pulls in the cryptography backend which mitigates some issues, but not all

For a 20-year system, depending on an unmaintained library is high risk.

**Fix:** Use PyJWT instead (actively maintained, used by the current `app.py`):
```txt
# In requirements.txt:
PyJWT[crypto]==2.10.1  # Instead of python-jose

# In auth.py:
import jwt  # PyJWT, same as current app.py
# jwt.encode() and jwt.decode() have the same API
```

This also eliminates the migration effort of changing from `import jwt` to `from jose import jwt`.

---

## Finding 20: No Rate Limiting on WebSocket Connections

**Severity:** MEDIUM
**Affects:** Task 29 (WebSocket endpoint)

**Problem:** The `slowapi` rate limiter only applies to HTTP endpoints. WebSocket connections bypass rate limiting entirely. An attacker can rapidly open and close connections, consuming server resources.

**Fix:** Add per-IP connection rate limiting in the WebSocket endpoint:
```python
_ws_connect_times: dict[str, list[float]] = {}

@app.websocket("/ws/live")
async def ws_live(ws: WebSocket, ticket: str = ""):
    client_ip = ws.client.host if ws.client else "unknown"
    now = time.time()

    # Rate limit: max 10 connections per minute per IP
    times = _ws_connect_times.setdefault(client_ip, [])
    times[:] = [t for t in times if now - t < 60]
    if len(times) >= 10:
        await ws.accept()
        await ws.close(code=4003, reason="Rate limit exceeded")
        return
    times.append(now)

    # ... proceed with normal auth and connection
```

---

## Finding 21: Admin Password Minimum Length Is 4 Characters

**Severity:** MEDIUM
**Affects:** Task 2 (password change, ported from current app.py line 611)

**Problem:** The current `app.py` allows passwords as short as 4 characters (`len(new_password) < 4`). For a system exposed on a plant network for 20 years, this is insufficient.

**Fix:**
```python
if not new_password or len(new_password) < 12:
    raise HTTPException(status_code=400, detail="Password must be at least 12 characters")
```

---

## Finding 22: Backup Download Path Traversal

**Severity:** MEDIUM
**Affects:** Task 12 (server_routes.py — backup download/restore)

**Problem:** The current `app.py` has `safe_filename()` validation and uses it in some places, but the backup download endpoint (line 2086) accepts `<filename>` as a path parameter. The migration must ensure path traversal is prevented.

**Fix:**
```python
@router.get("/api/backups/{filename}/download")
async def backup_download(
    filename: str = Path(pattern=r'^[\w\-\.]+\.yml$'),
    user: CurrentUser = Depends(require_admin),
):
    if not safe_filename(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    backup_path = os.path.join(BACKUP_DIR, filename)
    # Ensure resolved path is within BACKUP_DIR (defense in depth)
    if not os.path.realpath(backup_path).startswith(os.path.realpath(BACKUP_DIR)):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not os.path.exists(backup_path):
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(backup_path, filename=filename)
```

---

## Finding 23: Grafana Proxy is an SSRF Vector

**Severity:** HIGH
**Affects:** Task 16 (data_routes.py — Grafana proxy)

**Problem:** The current `app.py` (line 2839) has a Grafana proxy that takes a `<path:path>` parameter and forwards it to `http://grafana:3000/{path}`. The query string is also forwarded verbatim. This is a Server-Side Request Forgery (SSRF) vector. An attacker could potentially use this to reach internal services.

The current code limits the target to `http://grafana:3000/`, but the `path` parameter could contain `../` or be crafted to hit other endpoints.

**Fix:**
```python
GRAFANA_ALLOWED_PATHS = re.compile(r'^(render/d-solo|api/ds/query|d/)')

@router.get("/grafana/{path:path}")
async def grafana_proxy(
    path: str,
    user: CurrentUser = Depends(get_current_user),
):
    if not GRAFANA_ALLOWED_PATHS.match(path):
        raise HTTPException(status_code=403, detail="Path not allowed")
    # Prevent path traversal
    if ".." in path:
        raise HTTPException(status_code=400, detail="Invalid path")
    grafana_url = f"{_GRAFANA_URL}/{path}"
    # ... proxy the request
```

---

## Regulatory Compliance Notes

### IEC 62443 (Industrial Automation Security)

| Requirement | Status in Plan | Gap |
|---|---|---|
| SL-1: Identification and Authentication | Partial -- JWT exists but no MFA | Add MFA roadmap for SL-2+ |
| SL-1: Use Control (RBAC) | Good -- 3 roles + plant filter | Ensure all 90+ endpoints have correct role checks |
| SL-1: System Integrity | Missing -- no audit chain integrity | Finding 15 |
| SL-2: Software Process Integrity | Missing -- no dependency verification | Pin all deps with hashes |
| SL-2: Deterministic Output | Missing -- no input validation on many endpoints | Finding 13 |

### NIST Cybersecurity Framework

| Function | Status |
|---|---|
| Identify | Partially covered (asset inventory via config) |
| Protect | JWT + RBAC good; encryption at rest missing (Finding 16) |
| Detect | Audit trail exists but lacks integrity (Finding 15) |
| Respond | No incident response automation |
| Recover | Backup exists; SQLite backup added in Fix 18 |

### ISA-95

The plan correctly keeps the monitoring system (Level 3) separate from the control system (Rockwell handles Level 2). The `/api/live/write` endpoint that writes to PLCs should be carefully restricted -- it is currently `require_operator` which is correct.

---

## Summary Table

| # | Finding | Severity | Affected Task |
|---|---------|----------|---------------|
| 1 | No JWT revocation / logout is a no-op | CRITICAL | Task 2 |
| 2 | WebSocket accept-before-auth enables DoS | CRITICAL | Task 29, Fix 5 |
| 3 | Brute-force protection is a declared stub | CRITICAL | Task 2, Fix 12 |
| 4 | Timing oracle in Basic Auth comparison | HIGH | Task 2 |
| 5 | JWT secret not persisted across restarts | CRITICAL | Task 2, Fix 20 |
| 6 | SQLite injection via executescript pattern | HIGH | Task 21 |
| 7 | Flux injection protection not ported | HIGH | Tasks 15-16 |
| 8 | No CORS configuration (missing from Task 18) | HIGH | Task 18 |
| 9 | JWT token exposed in WebSocket URL | HIGH | Tasks 29, 31 |
| 10 | Docker socket enables container escape | CRITICAL | Tasks 12, 17 |
| 11 | Subprocess command injection risk | HIGH | Task 17 |
| 12 | No CSRF protection on state-changing endpoints | MEDIUM | Tasks 5-17 |
| 13 | Pydantic models missing for most endpoints | MEDIUM | Tasks 5-17 |
| 14 | Path parameter injection in device/tag routes | MEDIUM | Tasks 6-7 |
| 15 | Audit trail integrity not protected | MEDIUM | Tasks 4, 22 |
| 16 | SQLite not encrypted at rest | MEDIUM | Task 21 |
| 17 | MQTT bridge has no authentication | HIGH | Task 30 |
| 18 | healthz endpoint info leakage risk | LOW | Task 1 |
| 19 | python-jose has known CVEs, use PyJWT | HIGH | Task 1 |
| 20 | No rate limiting on WebSocket connections | MEDIUM | Task 29 |
| 21 | Admin password minimum length too low | MEDIUM | Task 2 |
| 22 | Backup download path traversal | MEDIUM | Task 12 |
| 23 | Grafana proxy is an SSRF vector | HIGH | Task 16 |

**Total: 6 CRITICAL, 9 HIGH, 8 MEDIUM, 0 LOW (1 LOW noted but not security-impacting)**

---

## Recommended Implementation Order for Fixes

1. **Before Phase 1 starts:** Fix 19 (replace python-jose with PyJWT), Fix 5 (persist JWT secret), Fix 8 (CORS)
2. **During Task 2:** Fixes 1, 3, 4, 21 (auth hardening)
3. **During Tasks 5-17:** Fixes 7, 13, 14, 22, 23 (input validation, SSRF)
4. **During Task 18:** Fixes 8, 12 (CORS, CSRF)
5. **During Task 21:** Fixes 6, 15, 16 (SQLite security)
6. **During Task 29:** Fixes 2, 9, 20 (WebSocket hardening)
7. **During Task 30:** Fix 17 (MQTT auth)
8. **Architecture review (separate task):** Fix 10 (Docker socket proxy)
