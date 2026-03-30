# Migration Plan — All Reviews Consolidated (6 rounds)

## Review History
- **Round 1-3**: Completeness, industrial reliability, code quality → 28 issues
- **Round 4**: Security (OWASP, IEC 62443) → 23 issues
- **Round 5**: Reliability engineering (20-year, 24/7) → 26 issues
- **Round 6**: Completeness gap analysis (91 routes audited) → 10 issues

**Total unique issues after dedup: ~65**

---

## MASTER FIX LIST (organized by phase)

### PRE-MIGRATION (before touching any code)

| # | Fix | Severity |
|---|-----|----------|
| P1 | Tag Docker image `plc4x-manager-plc4x-admin:flask-v1.0.0` as rollback point | HIGH |
| P2 | Document cold standby hardware requirement for monitoring room | CRITICAL |
| P3 | Configure Docker `live-restore: true` on host for daemon restarts | CRITICAL |
| P4 | Verify NTP is running on host (`timedatectl set-ntp true`) | MEDIUM |
| P5 | Set up host-level backup cron for Docker volumes (nightly tar) | HIGH |

### PHASE 1: FastAPI Migration

#### Architecture (apply to ALL tasks)

| # | Fix | Severity | Detail |
|---|-----|----------|--------|
| A1 | Single uvicorn worker (`--workers 1`) — async handles concurrency | CRITICAL | Multi-worker breaks WebSocket ConnectionManager |
| A2 | Add `--limit-max-requests 50000` (not 5000) to avoid frequent WS drops | HIGH | 5000 = restart every ~7 min with 60 clients |
| A3 | Use `lifespan` context manager, NOT `@app.on_event("startup")` | HIGH | Deprecated in FastAPI 0.93+ |
| A4 | Set `WORKDIR /app/admin` in Dockerfile, launch `uvicorn main:app` | HIGH | Import paths break without this |
| A5 | Create `admin/routes/__init__.py` (empty file for package) | CRITICAL | Imports fail without it |
| A6 | Rename `websocket.py` → `ws_manager.py` (stdlib name conflict) | CRITICAL | `import websocket` resolves wrong module |
| A7 | Use `datetime.now(timezone.utc)` everywhere (not `utcnow()`) | MEDIUM | Deprecated in Python 3.12 |
| A8 | Add HEALTHCHECK to Dockerfile (10s interval, 2 retries) | HIGH | Dead process not restarted |

#### Task 1: Dependencies

| # | Fix | Detail |
|---|-----|--------|
| D1 | Use `PyJWT` (current), NOT `python-jose` | python-jose has known CVEs; PyJWT already works |
| D2 | Remove `TokenPayload` from models.py | Dead code, never used |
| D3 | `expiresIn` must use `JWT_EXPIRY_HOURS * 3600`, not hardcoded 86400 | Bug |

#### Task 2: Authentication

| # | Fix | Severity | Detail |
|---|-----|----------|--------|
| AUTH1 | Implement full brute-force protection (not stub) | CRITICAL | Copy lines 446-528 from app.py: file-based failure counting, IP lockout after 5 attempts, 5-min cooldown |
| AUTH2 | Port `load_admin_credentials()` for password persistence | HIGH | Passwords lost on restart without this |
| AUTH3 | JWT secret auto-persist to config volume if env not set | HIGH | Random secret = all sessions invalidated on restart |
| AUTH4 | Support both USERS_JSON formats (list and dict) for backward compat | HIGH | Breaking change if not handled |
| AUTH5 | Remove dead path allowlist from `get_current_user` | LOW | `/login` and `/healthz` never use this dependency |
| AUTH6 | Remove unused imports `_LOGIN_LOCK`, `_LOGIN_FAILURES_PATH` from auth_routes | LOW | Dead code |
| AUTH7 | Add rate limiting decorator to login route + RateLimitExceeded handler | HIGH | Security control missing |
| AUTH8 | Add JWT revocation via `jti` claim + blocklist (migrate to SQLite in Phase 2) | MEDIUM | Stolen tokens valid for 24h |

#### Task 3: Config Manager

| # | Fix | Severity | Detail |
|---|-----|----------|--------|
| CFG1 | `_strip_admin_fields()` must also strip disabled devices from list | CRITICAL | PLC4X server gets disabled devices → crash |
| CFG2 | `_strip_admin_fields()` must remove VIRTUAL address tags | CRITICAL | PLC4X server can't read virtual tags |
| CFG3 | `_strip_admin_fields()` must remove `allowWrite` field | MEDIUM | Not a server field |
| CFG4 | Add config cache with mtime invalidation (avoid FileLock contention) | HIGH | 60 clients all hitting FileLock |
| CFG5 | Call `cleanup_old_backups()` after every save | MEDIUM | Backups grow unbounded |

#### Task 4: Audit Module

| # | Fix | Detail |
|---|-----|--------|
| AUD1 | Implement `_trim_audit()` fully (not `pass` stub) | Copy `_trim_jsonl_file` from app.py |
| AUD2 | Audit middleware must extract user from JWT BEFORE `call_next` | See Fix 10 in review-fixes.md |
| AUD3 | Audit should capture request body details (device name, tag alias) for context | Middleware-only approach loses this; consider explicit `audit_log()` calls in routes instead |

#### Tasks 5-17: Route Migration

| # | Fix | Severity | Detail |
|---|-----|----------|--------|
| RT1 | **`/api/demo/load`** — missing from ALL tasks | CRITICAL | Route will not exist after migration |
| RT2 | **`/api/manager/update-status`** — must be in version_routes.py | HIGH | Progress overlay depends on it |
| RT3 | **`/api/live/read` + `/api/live/read/<device_name>`** — dual registration | HIGH | FastAPI needs Optional path param or 2 routes |
| RT4 | PLC4X version routes need dedicated `plc4x_routes.py` | HIGH | 4 endpoints + templates (5 total) |
| RT5 | Preserve inline plant filter checks in 9 route bodies | CRITICAL | Not just `filter_by_plant()` — alarm ack, live write, tag history, CSV, PDF, OEE all have inline checks |
| RT6 | Specify role level per endpoint (8 routes need `@require_operator`, not admin) | HIGH | Operators lose access to logs, alarms, logbook, audit |
| RT7 | Port AST formula evaluator (~100 lines) for calculated tags | HIGH | `/api/formula/validate` silently broken without it |
| RT8 | Port HMI image upload sanitization (`_safe_image_filename`, extension whitelist) | HIGH | Security: arbitrary file upload |
| RT9 | Port `asyncio.new_event_loop()` → `await` conversion for `/api/live/write` | HIGH | Nested event loop error in FastAPI |
| RT10 | Port `UploadFile` handling for `/api/backups/upload` | MEDIUM | File upload needs `python-multipart` pattern |
| RT11 | Preserve Flux injection protection (`_safe_flux_str`) in all InfluxDB queries | HIGH | SQL-like injection into Flux queries |

#### Task 18: Middleware

| # | Fix | Detail |
|---|-----|--------|
| MW1 | Add CORS middleware configuration | Missing entirely from plan |
| MW2 | Grafana proxy must validate path (SSRF vector) | Restrict to `/d/`, `/api/`, `/public/` prefixes only |

#### Task 19: Entrypoint

| # | Fix | Detail |
|---|-----|--------|
| EP1 | Use `PRAGMA synchronous=FULL` (not NORMAL) for audit/safety-critical writes | Power loss can lose last transactions with NORMAL |
| EP2 | Stagger WebSocket close on graceful shutdown (send reconnectIn with jitter) | Thundering herd on process restart |

### PHASE 2: SQLite Migration

| # | Fix | Severity | Detail |
|---|-----|----------|--------|
| DB1 | Initialize DB in `lifespan`, store in `app.state.db` (not global `_db`) | CRITICAL | Global singleton not safe for concurrent async |
| DB2 | Use correct aiosqlite API: `async with db.execute() as cursor` | CRITICAL | `execute_fetchall/fetchone` don't exist |
| DB3 | Add `PRAGMA synchronous=FULL` + `PRAGMA wal_autocheckpoint=400` | HIGH | Crash safety + checkpoint stall prevention |
| DB4 | Add schema_version table for future migrations (REQUIRED, not suggestion) | HIGH | Schema evolution impossible without it |
| DB5 | Backup SQLite every 4 hours (not nightly) with 7-day retention | HIGH | 24h RPO unacceptable for audit trail |
| DB6 | Add periodic WAL checkpoint (hourly `PRAGMA wal_checkpoint(TRUNCATE)`) | MEDIUM | WAL file growth |
| DB7 | Add periodic `PRAGMA integrity_check` (weekly) | MEDIUM | Detect corruption early |
| DB8 | `audit_log` becomes async — update ALL callers including middleware | CRITICAL | Silent data loss: coroutine created but not awaited |
| DB9 | `read_audit` becomes async — update `audit_routes.py` to `await` | HIGH | Same issue |
| DB10 | Keep JSONL files for 30 days after migration (rollback safety) | MEDIUM | Emergency rollback path |
| DB11 | Consider separate DB files for high-frequency (alarms) vs low-frequency (audit/logbook) | HIGH | Cross-process WAL contention |
| DB12 | Add `alarm_history` retention/pruning (1 year) | HIGH | Unbounded table growth over 20 years |
| DB13 | Deploy Phase 2 during planned maintenance window (shift change) | HIGH | Alarm state transition can cause false alarm clears |
| DB14 | `admin/migrations/001_initial.sql` — create actual file (plan lists but never creates) | MEDIUM | Plan inconsistency |

### PHASE 3: WebSocket

| # | Fix | Severity | Detail |
|---|-----|----------|--------|
| WS1 | Accept WebSocket BEFORE auth check (Fix 5 in review-fixes) | CRITICAL | `ws.close()` before `accept()` crashes |
| WS2 | Capture event loop in main thread, pass to MQTT bridge thread | CRITICAL | `asyncio.get_event_loop()` crashes in Python 3.12 thread |
| WS3 | Add WebSocket connection limit (200 max) | HIGH | DoS: unlimited connections exhaust memory |
| WS4 | Add dead connection reaper (ping every 60s, remove unresponsive) | HIGH | Zombie connections accumulate over years |
| WS5 | Add WebSocket server-side ping for NAT/firewall timeout detection | HIGH | Silent dead connections after 30 min inactivity |
| WS6 | Client reconnect: exponential backoff with 5s jitter (not 1s) | HIGH | Thundering herd with 20 kiosks |
| WS7 | Client: cache last data in localStorage, show with age indicator on disconnect | HIGH | Kiosks show blank during 30-min partition |
| WS8 | Server: send active alarms on reconnect (alarm sync) | HIGH | Clients miss alarms during network partition |
| WS9 | Add REST polling fallback if WebSocket dead for >5s | HIGH | Graceful degradation |
| WS10 | Broadcast sends should be concurrent with per-client timeout (not sequential under lock) | MEDIUM | Slow client blocks all others |
| WS11 | MQTT bridge must authenticate to mosquitto (not anonymous) | HIGH | Unauthenticated MQTT = fake data injection |
| WS12 | Create `tests/test_websocket.py` (not just wscat manual test) | MEDIUM | No automated WS tests |

### OPERATIONAL (add to plan)

| # | Fix | Severity | Detail |
|---|-----|----------|--------|
| OP1 | Add InfluxDB write buffer in poller for InfluxDB downtime | HIGH | Data lost during InfluxDB restart |
| OP2 | Add MQTT down fallback: poll SQLite for alarm changes | HIGH | Alarms stop propagating when MQTT dies |
| OP3 | Add stale data indicator on dashboard when PLC4X server unreachable | MEDIUM | Operators see frozen data with no warning |
| OP4 | Add disk space monitoring endpoint (`/api/server/disk`) | MEDIUM | Disk fills up silently over years |
| OP5 | Add Docker log rotation config (`json-file`, 10m, 5 files) | LOW | Docker logs fill disk |
| OP6 | Add recovery script (`recover.sh`) for cold standby hardware | HIGH | RTO without it: 3-50 hours |
| OP7 | InfluxDB retention must be enforced (never set to 0/unlimited) | MEDIUM | Largest disk consumer |
| OP8 | Add clock skew detection between client and server | MEDIUM | JWT expiry, alarm timing affected |

---

## SUMMARY

| Phase | Critical | High | Medium | Low | Total |
|-------|----------|------|--------|-----|-------|
| Pre-migration | 2 | 2 | 1 | 0 | 5 |
| Phase 1 | 10 | 18 | 8 | 3 | 39 |
| Phase 2 | 3 | 7 | 4 | 0 | 14 |
| Phase 3 | 2 | 8 | 2 | 0 | 12 |
| Operational | 0 | 3 | 4 | 1 | 8 |
| **Total** | **17** | **38** | **19** | **4** | **78** |

All 78 issues are documented with fix code in this file and in `2026-03-29-migration-review-fixes.md`.

---

## VERDICT: Is the plan stable?

**YES, with the fixes above applied.** The core architecture is sound:
- FastAPI async handles 60+ concurrent clients
- SQLite WAL handles concurrent reads/writes across processes
- WebSocket via MQTT bridge is the right pattern for poller→client push
- The 3-phase incremental approach minimizes risk

The issues found are **implementation details**, not architectural flaws. Every one has a concrete fix with code. The plan is ready for implementation.

**Recommended order:**
1. Apply all PRE-MIGRATION fixes first
2. Implement Phase 1 with ALL fixes from this doc (AUTH, CFG, RT, MW, EP, UI sections)
3. Run 253 bash tests — must pass 100%
4. Deploy Phase 1 and run for 1-2 weeks in production
5. Then Phase 2 (SQLite) during maintenance window
6. Then Phase 3 (WebSocket) — most impactful for operators

---

### ROUND 7: UI/UX Gap Analysis (14 additional gaps)

Cross-referenced all 15 tabs, 91 routes, 50+ modals/forms against the plan.

#### Phase 1 Gaps

| # | Fix | Severity | Detail |
|---|-----|----------|--------|
| UI1 | `/api/auth/info` (GET) missing from plan | HIGH | Security tab shows blank admin username |
| UI2 | `/api/hmi/load-demo` (POST) missing — distinct from `/api/demo/load` | MEDIUM | HMI "Load Demo" button returns 404 |
| UI3 | `/api/backups/<filename>/content` (GET) missing | MEDIUM | "View Content" in Backups returns 404 |
| UI4 | `/api/backups/<filename>/download` (GET) missing — uses `send_file` | HIGH | "Download" backup returns 404 |
| UI5 | `/api/backups/<filename>/diff` (GET) missing | MEDIUM | "Diff" button in Backups returns 404 |
| UI6 | `/api/data/write` (POST) has no code sketch | MEDIUM | External integration write endpoint |
| UI7 | Plan code says `--workers 2` contradicting review fix A1 | HIGH | WebSocket breaks if not corrected to 1 |
| UI8 | `evaluate_formula` + `_process_calculated_tags` need a home file | HIGH | Create `admin/formula.py` for shared formula evaluator |
| UI9 | Flask `send_file` → FastAPI `StreamingResponse` for 3 endpoints | MEDIUM | CSV, PDF, backup download all use send_file |
| UI10 | Static `swagger.json` will be stale — consider auto-generate from FastAPI | LOW | API docs out of date |

#### Phase 2 Gaps

| # | Fix | Severity | Detail |
|---|-----|----------|--------|
| UI11 | Alarm history field names in SQLite differ from current JSON API | MEDIUM | `start_time` vs `timestamp`, `end_time` vs `cleared` — API must map back |

#### Phase 3 Gaps

| # | Fix | Severity | Detail |
|---|-----|----------|--------|
| UI12 | Background alarm poller + tab badge not in WebSocket migration | HIGH | Badge count + beep stops working outside Alarms tab |
| UI13 | Kiosk mode HMI cycling needs per-equipment WS subscription | MEDIUM | Stale data on non-current HMI screens |
| UI14 | Tag trend modal implicitly covered but needs test | LOW | No explicit test for inline chart |

---

## FINAL TOTALS (all 7 review rounds)

| Phase | Critical | High | Medium | Low | Total |
|-------|----------|------|--------|-----|-------|
| Pre-migration | 2 | 2 | 1 | 0 | 5 |
| Phase 1 | 10 | 23 | 12 | 5 | 50 |
| Phase 2 | 3 | 7 | 5 | 0 | 15 |
| Phase 3 | 2 | 10 | 4 | 1 | 17 |
| Operational | 0 | 3 | 4 | 1 | 8 |
| **Total** | **17** | **45** | **26** | **7** | **95** |

All 95 issues documented with fixes. Plan is stable and ready for implementation.
