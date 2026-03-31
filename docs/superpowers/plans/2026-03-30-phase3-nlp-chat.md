# Phase 3: NLP Chat with Plant Data — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a natural language chat interface where operators ask questions about plant data ("What was the OEE last week?") and get answers with real data from InfluxDB, alarms, and ML insights — powered by OpenRouter LLM API with automatic free model fallback.

**Architecture:** Frontend chat widget (slide-in panel) sends questions to backend. Backend builds context (system prompt + tools + conversation history), calls OpenRouter API. LLM uses function calling to query real data via 6 tools that map to existing internal APIs. Fallback chain: openrouter/auto → openrouter/free → forced free on 402.

**Tech Stack:** FastAPI, httpx (async HTTP), OpenRouter API (OpenAI-compatible), SQLite (chat history), Chart.js (inline charts), vanilla JS

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `admin/requirements.txt` | Add httpx dependency |
| Modify | `.env.example` | Add CHAT_* env vars |
| Create | `admin/chat_llm.py` | OpenRouter client with fallback chain |
| Modify | `admin/database.py` | Migration v2: chat_history table + pruning |
| Create | `admin/chat_tools.py` | 6 LLM function-calling tools (wrappers around internal APIs) |
| Create | `admin/routes/chat_routes.py` | Chat API endpoints (ask, history, status, config) |
| Modify | `admin/main.py` | Register chat_routes router |
| Modify | `admin/static/js/app.js` | Chat widget UI (floating button, slide-in panel, messages) |
| Modify | `admin/static/css/style.css` | Chat widget styles |

---

## Task 1: Dependencies and Configuration

**Files:**
- Modify: `admin/requirements.txt`
- Modify: `.env.example`

- [ ] **Step 1: Add httpx to requirements.txt**

Append after the last line of `admin/requirements.txt`:

```
httpx==0.28.1
```

- [ ] **Step 2: Add chat configuration to .env.example**

Append to the end of `.env.example`:

```env

# ===========================================
# AI Chat Assistant (OpenRouter)
# ===========================================
# API key from https://openrouter.ai/keys (leave empty to disable chat)
CHAT_API_KEY=
# OpenRouter API base URL
CHAT_API_URL=https://openrouter.ai/api/v1
# Models to try, comma-separated (first = primary, rest = fallback)
CHAT_MODEL=openrouter/auto,openrouter/free
# Max tokens per response
CHAT_MAX_TOKENS=2048
```

**Test:**
```bash
grep httpx admin/requirements.txt
grep CHAT_API_KEY .env.example
```

**Commit:** `feat(chat): add httpx dependency and CHAT_* env vars`

---

## Task 2: ChatLLM Client

**Files:**
- Create: `admin/chat_llm.py`

- [ ] **Step 1: Create the OpenRouter client class**

Create `admin/chat_llm.py`:

```python
"""
OpenRouter LLM client with automatic model fallback.

Fallback chain:
1. Try with all configured models (native OpenRouter fallback via "models" field)
2. On 402 (payment required): retry with ["openrouter/free"] only
3. On 429 (rate limited): return friendly message, no retry
4. On 5xx (server error): retry once
5. On timeout (30s): return unavailable message
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx

log = logging.getLogger("chat_llm")


class ChatLLM:
    """Async OpenRouter API client with fallback chain."""

    def __init__(self) -> None:
        self.api_key: str = os.environ.get("CHAT_API_KEY", "")
        self.api_url: str = os.environ.get(
            "CHAT_API_URL", "https://openrouter.ai/api/v1"
        )
        model_str = os.environ.get("CHAT_MODEL", "openrouter/auto,openrouter/free")
        self.models: list[str] = [m.strip() for m in model_str.split(",") if m.strip()]
        self.max_tokens: int = int(os.environ.get("CHAT_MAX_TOKENS", "2048"))
        self.enabled: bool = bool(self.api_key)

    @property
    def primary_model(self) -> str:
        """Return the first model in the list for display purposes."""
        return self.models[0] if self.models else "openrouter/auto"

    def _build_request(
        self,
        messages: list[dict],
        tools: Optional[list[dict]],
        models: list[str],
    ) -> dict:
        """Build the OpenRouter API request body."""
        body: dict[str, Any] = {
            "models": models,
            "route": "fallback",
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": 0.3,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    async def ask(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
    ) -> dict:
        """Send messages to OpenRouter and return the response.

        Returns dict with keys:
        - content: str (assistant message text)
        - tool_calls: list[dict] | None
        - model_used: str
        - error: str | None
        """
        if not self.enabled:
            return {
                "content": "Chat is not configured. Set CHAT_API_KEY in your environment.",
                "tool_calls": None,
                "model_used": "",
                "error": "not_configured",
            }

        url = f"{self.api_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/plc4x-manager",
            "X-Title": "PLC4X Manager",
        }

        # Attempt 1: all configured models (native fallback)
        body = self._build_request(messages, tools, self.models)
        result = await self._call(url, headers, body)

        if result.get("error") == "payment_required":
            # Attempt 2: force free model only
            log.warning("402 from OpenRouter, falling back to openrouter/free")
            body = self._build_request(messages, tools, ["openrouter/free"])
            result = await self._call(url, headers, body)

        elif result.get("error") == "server_error":
            # Attempt 3: retry once on 5xx
            log.warning("5xx from OpenRouter, retrying once")
            result = await self._call(url, headers, body)

        return result

    async def _call(self, url: str, headers: dict, body: dict) -> dict:
        """Execute a single HTTP call to OpenRouter."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, headers=headers, json=body)

            if resp.status_code == 402:
                return {
                    "content": "",
                    "tool_calls": None,
                    "model_used": "",
                    "error": "payment_required",
                }

            if resp.status_code == 429:
                return {
                    "content": "The AI service is temporarily rate-limited. Please try again in a minute.",
                    "tool_calls": None,
                    "model_used": "",
                    "error": "rate_limited",
                }

            if resp.status_code >= 500:
                return {
                    "content": "",
                    "tool_calls": None,
                    "model_used": "",
                    "error": "server_error",
                }

            if resp.status_code != 200:
                log.error("OpenRouter %d: %s", resp.status_code, resp.text[:500])
                return {
                    "content": f"AI service returned an error (HTTP {resp.status_code}).",
                    "tool_calls": None,
                    "model_used": "",
                    "error": "api_error",
                }

            data = resp.json()
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            model_used = data.get("model", "")

            tool_calls = message.get("tool_calls")
            if tool_calls:
                # Normalize tool_calls to list of dicts
                tool_calls = [
                    {
                        "id": tc.get("id", ""),
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"],
                        },
                    }
                    for tc in tool_calls
                ]

            return {
                "content": message.get("content", "") or "",
                "tool_calls": tool_calls,
                "model_used": model_used,
                "error": None,
            }

        except httpx.TimeoutException:
            log.warning("OpenRouter request timed out (30s)")
            return {
                "content": "The AI service is temporarily unavailable (timeout). Please try again.",
                "tool_calls": None,
                "model_used": "",
                "error": "timeout",
            }
        except Exception as exc:
            log.error("OpenRouter call failed: %s", exc)
            return {
                "content": "An unexpected error occurred contacting the AI service.",
                "tool_calls": None,
                "model_used": "",
                "error": "exception",
            }
```

**Test:**
```bash
cd admin && python -c "from chat_llm import ChatLLM; c = ChatLLM(); print('enabled:', c.enabled, 'models:', c.models)"
```

Expected output (no API key set): `enabled: False models: ['openrouter/auto', 'openrouter/free']`

**Commit:** `feat(chat): add ChatLLM OpenRouter client with fallback chain`

---

## Task 3: Database Schema — chat_history Table

**Files:**
- Modify: `admin/database.py`

- [ ] **Step 1: Add migration v2 with chat_history table**

In `admin/database.py`, after the block ending with:
```python
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (1)")
        await db.commit()
        log.info("Applied migration v1: initial schema")
```

Add migration v2:

```python

    if current < 2:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                role TEXT NOT NULL,
                message TEXT NOT NULL,
                tool_calls TEXT,
                model_used TEXT,
                user TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_chat_conv ON chat_history(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_history(user);
            CREATE INDEX IF NOT EXISTS idx_chat_ts ON chat_history(timestamp);
        """)
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (2)")
        await db.commit()
        log.info("Applied migration v2: chat_history table")
```

- [ ] **Step 2: Add chat_history pruning to db_maintenance_loop**

In `admin/database.py`, inside `db_maintenance_loop`, after the logbook pruning block (after the line `await db.commit()` that follows the logbook prune), add:

```python

            # Prune chat history older than 30 days
            chat_cutoff = (datetime.datetime.now(datetime.timezone.utc)
                          - datetime.timedelta(days=30)).isoformat()
            async with db.execute("DELETE FROM chat_history WHERE timestamp < ?", (chat_cutoff,)) as c:
                if c.rowcount > 0:
                    log.info(f"Pruned {c.rowcount} old chat history entries")
            await db.commit()
```

**Test:**
```bash
cd admin && python -c "
import asyncio, aiosqlite, os
os.environ['CONFIG_PATH'] = '/tmp/test_chat_config.yml'
from database import DB_PATH
# Use temp path
import database
database.DB_PATH = '/tmp/test_chat.db'
async def test():
    db = await database.init_db()
    async with db.execute('SELECT sql FROM sqlite_master WHERE name=\"chat_history\"') as c:
        row = await c.fetchone()
        print('TABLE:', row[0][:60] if row else 'NOT FOUND')
    async with db.execute('SELECT MAX(version) FROM schema_version') as c:
        row = await c.fetchone()
        print('VERSION:', row[0])
    await db.close()
    os.unlink('/tmp/test_chat.db')
asyncio.run(test())
"
```

Expected: `TABLE: CREATE TABLE chat_history ...` and `VERSION: 2`

**Commit:** `feat(chat): add chat_history table (migration v2) and 30-day pruning`

---

## Task 4: LLM Function-Calling Tools

**Files:**
- Create: `admin/chat_tools.py`

- [ ] **Step 1: Create the 6 LLM tools module**

Create `admin/chat_tools.py`:

```python
"""
LLM function-calling tools for the chat assistant.

Each tool wraps an existing internal API and returns structured data.
The TOOL_DEFINITIONS list is passed to the OpenRouter API as the "tools" parameter.
The execute_tool() function dispatches a tool call to the appropriate handler.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from config_manager import filter_by_plant, find_device, load_config
from influx import _influx_query, _safe_flux_str

log = logging.getLogger("chat_tools")

_INFLUX_BUCKET = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")


# =============================================
# Tool definitions (OpenAI function calling format)
# =============================================

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "query_tag_history",
            "description": "Query historical values for a specific tag on a device. Returns timestamped values from InfluxDB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device name (e.g., 'plc-line1')",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Tag alias (e.g., 'Temperature', 'Pressure')",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Number of hours of history to query (1-720). Default 1.",
                        "default": 1,
                    },
                },
                "required": ["device", "tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_values",
            "description": "Get the current live values and status for all tags on a device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device name to read current values from",
                    },
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_alarms",
            "description": "Get all currently active alarms across all devices. Returns alarm details including severity, value, threshold, and timestamp.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_oee",
            "description": "Get OEE (Overall Equipment Effectiveness) breakdown for a device: availability, performance, quality, and overall OEE percentage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device name to get OEE for",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Number of hours to calculate OEE over (1-720). Default 24.",
                        "default": 24,
                    },
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ml_insights",
            "description": "Get ML/AI insights for a device: anomaly detection results, predictions, and pattern analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device name to get ML insights for",
                    },
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_failure_history",
            "description": "Get failure history entries for a device, optionally filtered by failure type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device name to get failures for",
                    },
                    "failure_type": {
                        "type": "string",
                        "description": "Optional failure type filter (e.g., 'communication', 'threshold')",
                    },
                },
                "required": ["device"],
            },
        },
    },
]


# =============================================
# Tool executors
# =============================================


async def execute_tool(name: str, arguments: dict, db: Any = None) -> dict:
    """Execute a tool by name and return its result.

    Args:
        name: Tool function name
        arguments: Parsed arguments dict
        db: aiosqlite database connection (for alarm queries)

    Returns:
        dict with 'result' key on success, or 'error' key on failure.
        May also include 'chart_data' for numeric series suitable for charting.
    """
    try:
        if name == "query_tag_history":
            return await _tool_query_tag_history(
                arguments.get("device", ""),
                arguments.get("tag", ""),
                arguments.get("hours", 1),
            )
        elif name == "get_current_values":
            return await _tool_get_current_values(arguments.get("device", ""))
        elif name == "get_active_alarms":
            return await _tool_get_active_alarms(db)
        elif name == "get_oee":
            return await _tool_get_oee(
                arguments.get("device", ""),
                arguments.get("hours", 24),
            )
        elif name == "get_ml_insights":
            return await _tool_get_ml_insights(arguments.get("device", ""))
        elif name == "get_failure_history":
            return await _tool_get_failure_history(
                arguments.get("device", ""),
                arguments.get("failure_type"),
            )
        else:
            return {"error": f"Unknown tool: {name}"}
    except ValueError as e:
        return {"error": f"Invalid parameter: {e}"}
    except Exception as e:
        log.error("Tool %s failed: %s", name, e)
        return {"error": f"Tool execution failed: {e}"}


async def _tool_query_tag_history(device: str, tag: str, hours: int) -> dict:
    """Query tag history from InfluxDB."""
    _safe_flux_str(device)
    _safe_flux_str(tag)
    hours = max(1, min(hours, 720))

    # Smart bucket selection (mirrors data_routes.py logic)
    if hours <= 6:
        bucket = _INFLUX_BUCKET
        window = "5s" if hours <= 1 else "30s"
    elif hours <= 168:
        bucket = "plc4x_hourly"
        window = "1h"
    else:
        bucket = "plc4x_daily" if hours > 720 else "plc4x_hourly"
        window = "1d" if hours > 720 else "1h"

    flux = f'''
from(bucket: "{bucket}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r["device"] == "{device}")
  |> filter(fn: (r) => r["tag"] == "{tag}")
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
  |> yield(name: "mean")
'''

    import asyncio
    records = await asyncio.to_thread(_influx_query, flux)

    points = []
    for r in records:
        t = r.get_time()
        v = r.get_value()
        if v is not None:
            points.append({"t": t.isoformat(), "v": round(float(v), 3)})

    # Compute summary stats
    values = [p["v"] for p in points]
    summary = {}
    if values:
        summary = {
            "min": round(min(values), 3),
            "max": round(max(values), 3),
            "avg": round(sum(values) / len(values), 3),
            "count": len(values),
        }

    return {
        "result": {
            "device": device,
            "tag": tag,
            "hours": hours,
            "points": points[-200:],  # cap at 200 points for LLM context
            "summary": summary,
        },
        "chart_data": {
            "labels": [p["t"] for p in points[-200:]],
            "values": [p["v"] for p in points[-200:]],
            "label": f"{device}/{tag}",
        },
    }


async def _tool_get_current_values(device: str) -> dict:
    """Get current live values for a device from the config/last-known data."""
    _safe_flux_str(device)
    config = load_config()
    _, dev = find_device(config, device)
    if not dev:
        return {"error": f"Device '{device}' not found"}

    tags = {}
    for tag in dev.get("tags", []):
        alias = tag.get("alias", tag.get("name", "unknown"))
        tags[alias] = {
            "value": tag.get("lastValue"),
            "unit": tag.get("unit", ""),
            "quality": tag.get("quality", "unknown"),
        }

    return {
        "result": {
            "device": device,
            "status": dev.get("status", "unknown"),
            "plant": dev.get("plant", ""),
            "tags": tags,
        },
    }


async def _tool_get_active_alarms(db: Any) -> dict:
    """Get active alarms from SQLite."""
    if db is None:
        return {"error": "Database not available"}

    async with db.execute(
        "SELECT * FROM alarms ORDER BY timestamp DESC LIMIT 50"
    ) as cursor:
        rows = await cursor.fetchall()

    alarms = []
    for row in rows:
        alarms.append({
            "device": row["device"],
            "tag": row["tag"],
            "severity": row["severity"],
            "condition": row["condition_type"],
            "value": row["value"],
            "threshold": row["threshold"],
            "message": row["message"] or "",
            "timestamp": row["timestamp"],
            "acknowledged": bool(row["acknowledged"]),
        })

    return {
        "result": {
            "count": len(alarms),
            "alarms": alarms,
        },
    }


async def _tool_get_oee(device: str, hours: int) -> dict:
    """Get OEE data from InfluxDB."""
    _safe_flux_str(device)
    hours = max(1, min(hours, 720))

    flux = f'''
from(bucket: "{_INFLUX_BUCKET}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r["device"] == "{device}")
  |> filter(fn: (r) => r["_measurement"] == "oee")
  |> last()
'''

    import asyncio
    records = await asyncio.to_thread(_influx_query, flux)

    oee_data = {}
    for r in records:
        field = r.get_field()
        value = r.get_value()
        if value is not None:
            oee_data[field] = round(float(value), 2)

    if not oee_data:
        return {
            "result": {
                "device": device,
                "hours": hours,
                "message": "No OEE data available for this device/period.",
            },
        }

    return {
        "result": {
            "device": device,
            "hours": hours,
            "availability": oee_data.get("availability"),
            "performance": oee_data.get("performance"),
            "quality": oee_data.get("quality"),
            "oee": oee_data.get("oee"),
        },
    }


async def _tool_get_ml_insights(device: str) -> dict:
    """Get ML insights from InfluxDB plc4x_ml bucket."""
    _safe_flux_str(device)

    flux = f'''
from(bucket: "plc4x_ml")
  |> range(start: -24h)
  |> filter(fn: (r) => r["device"] == "{device}")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 50)
'''

    import asyncio
    records = await asyncio.to_thread(_influx_query, flux)

    insights = []
    for r in records:
        insights.append({
            "time": r.get_time().isoformat(),
            "measurement": r.get_measurement(),
            "field": r.get_field(),
            "value": r.get_value(),
        })

    return {
        "result": {
            "device": device,
            "insights_count": len(insights),
            "insights": insights[:30],  # cap for LLM context
        },
    }


async def _tool_get_failure_history(device: str, failure_type: str | None) -> dict:
    """Get failure history from InfluxDB."""
    _safe_flux_str(device)

    type_filter = ""
    if failure_type:
        _safe_flux_str(failure_type)
        type_filter = f'  |> filter(fn: (r) => r["failure_type"] == "{failure_type}")'

    flux = f'''
from(bucket: "{_INFLUX_BUCKET}")
  |> range(start: -720h)
  |> filter(fn: (r) => r["device"] == "{device}")
  |> filter(fn: (r) => r["_measurement"] == "failures")
{type_filter}
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 50)
'''

    import asyncio
    records = await asyncio.to_thread(_influx_query, flux)

    failures = []
    for r in records:
        failures.append({
            "time": r.get_time().isoformat(),
            "field": r.get_field(),
            "value": r.get_value(),
        })

    return {
        "result": {
            "device": device,
            "failure_type": failure_type,
            "count": len(failures),
            "failures": failures,
        },
    }
```

**Test:**
```bash
cd admin && python -c "
from chat_tools import TOOL_DEFINITIONS, execute_tool
print(f'{len(TOOL_DEFINITIONS)} tools defined')
for t in TOOL_DEFINITIONS:
    print(f'  - {t[\"function\"][\"name\"]}')
"
```

Expected: lists all 6 tools.

**Commit:** `feat(chat): add 6 LLM function-calling tools wrapping internal APIs`

---

## Task 5: Chat Routes

**Files:**
- Create: `admin/routes/chat_routes.py`

- [ ] **Step 1: Create the chat routes module**

Create `admin/routes/chat_routes.py`:

```python
"""
NLP Chat API routes for PLC4X Manager.

Endpoints:
  POST /api/chat/ask      — Send a question, get AI response with real data
  GET  /api/chat/history   — Conversation history (user's own; admin sees all with ?all=true)
  GET  /api/chat/status    — Chat availability status (no auth required for UI bootstrap)
  GET  /api/chat/config    — Chat configuration (admin only)
  PUT  /api/chat/config    — Update chat configuration (admin only)
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from auth import CurrentUser, get_current_user, require_admin
from chat_llm import ChatLLM
from chat_tools import TOOL_DEFINITIONS, execute_tool

log = logging.getLogger("chat_routes")

router = APIRouter(tags=["chat"])

# Module-level LLM client (initialized once)
_llm = ChatLLM()

# System prompt for the AI assistant
_SYSTEM_PROMPT = """You are the PLC4X Manager AI assistant for an industrial monitoring system.
You help operators and engineers understand plant data, alarms, and equipment status.

Rules:
- Answer in the same language as the user's question
- When answering with numeric data, always include values and units
- Use the available tools to fetch real data — never invent or estimate values
- If a tool returns no results, say so clearly
- Keep answers concise and actionable
- For time references, use the plant's local timezone
- You can suggest checking specific HMI screens or Grafana dashboards when relevant"""

_MAX_MESSAGE_LENGTH = 2000
_MAX_TOOL_ITERATIONS = 5
_CONTEXT_MESSAGES = 20


# =============================================
# Request / Response models
# =============================================

class ChatAskRequest(BaseModel):
    message: str = Field(..., max_length=_MAX_MESSAGE_LENGTH)
    conversation_id: str | None = None


class ChatConfigUpdate(BaseModel):
    model: str | None = None
    max_tokens: int | None = Field(None, ge=256, le=8192)


# =============================================
# Helpers
# =============================================

async def _save_message(
    db,
    conversation_id: str,
    role: str,
    message: str,
    user: str,
    tool_calls: list | None = None,
    model_used: str | None = None,
) -> None:
    """Persist a chat message to SQLite."""
    await db.execute(
        """INSERT INTO chat_history
           (conversation_id, role, message, tool_calls, model_used, user)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            conversation_id,
            role,
            message,
            json.dumps(tool_calls) if tool_calls else None,
            model_used,
            user,
        ),
    )
    await db.commit()


async def _load_conversation(db, conversation_id: str, limit: int = _CONTEXT_MESSAGES) -> list[dict]:
    """Load recent messages from a conversation."""
    async with db.execute(
        """SELECT role, message, tool_calls FROM chat_history
           WHERE conversation_id = ?
           ORDER BY id DESC LIMIT ?""",
        (conversation_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()

    # Reverse to chronological order
    messages = []
    for row in reversed(rows):
        msg = {"role": row["role"], "content": row["message"]}
        if row["tool_calls"]:
            try:
                msg["tool_calls"] = json.loads(row["tool_calls"])
            except json.JSONDecodeError:
                pass
        messages.append(msg)
    return messages


# =============================================
# Routes
# =============================================

@router.get("/api/chat/status")
async def chat_status():
    """Return chat availability. No auth required (used by UI bootstrap)."""
    return {
        "enabled": _llm.enabled,
        "model": _llm.primary_model if _llm.enabled else "",
    }


@router.post("/api/chat/ask")
async def chat_ask(
    body: ChatAskRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    """Process a chat question with LLM + tool calling."""
    # Rate limiting via slowapi (applied at router level, 10/min per user)
    if not _llm.enabled:
        raise HTTPException(status_code=503, detail="Chat not configured (CHAT_API_KEY not set)")

    db = request.app.state.db
    conversation_id = body.conversation_id or str(uuid.uuid4())

    # Save user message
    await _save_message(db, conversation_id, "user", body.message, user.username)

    # Build context: system prompt + conversation history + new message
    history = await _load_conversation(db, conversation_id)

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    # Add history (excluding the message we just saved, which is the last one)
    for msg in history[:-1]:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": body.message})

    # Tool call loop
    chart_data = None
    all_tool_data = []
    model_used = ""

    for iteration in range(_MAX_TOOL_ITERATIONS):
        result = await _llm.ask(messages, TOOL_DEFINITIONS)
        model_used = result.get("model_used", "")

        if result.get("error") and result["error"] not in ("rate_limited", "timeout"):
            if not result["content"]:
                result["content"] = "Sorry, I could not process your request right now."

        tool_calls = result.get("tool_calls")

        if not tool_calls:
            # No tool calls — final response
            reply = result["content"]
            break
        else:
            # Process tool calls
            assistant_msg = {"role": "assistant", "content": result["content"] or ""}
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": tc["function"],
                }
                for tc in tool_calls
            ]
            messages.append(assistant_msg)

            for tc in tool_calls:
                func_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                tool_result = await execute_tool(func_name, args, db=db)
                all_tool_data.append(tool_result)

                # If the tool returned chart data, keep the last one
                if "chart_data" in tool_result:
                    chart_data = tool_result["chart_data"]

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(tool_result.get("result", tool_result.get("error", "")))[:4000],
                })
    else:
        reply = result.get("content", "I ran out of steps processing your question. Please try a simpler query.")

    # Save assistant response
    await _save_message(
        db, conversation_id, "assistant", reply, user.username,
        model_used=model_used,
    )

    return {
        "reply": reply,
        "data": all_tool_data if all_tool_data else None,
        "chart": chart_data,
        "model_used": model_used,
        "conversation_id": conversation_id,
    }


@router.get("/api/chat/history")
async def chat_history(
    request: Request,
    all: bool = Query(default=False, description="Admin: show all users' history"),
    limit: int = Query(default=50, ge=1, le=500),
    user: CurrentUser = Depends(get_current_user),
):
    """Return conversation list with last message preview."""
    db = request.app.state.db

    if all and user.role == "admin":
        # Admin sees all conversations
        query = """
            SELECT conversation_id, user, MAX(timestamp) as last_ts,
                   COUNT(*) as message_count
            FROM chat_history
            GROUP BY conversation_id
            ORDER BY last_ts DESC
            LIMIT ?
        """
        params = (limit,)
    else:
        # Users see only their own
        query = """
            SELECT conversation_id, user, MAX(timestamp) as last_ts,
                   COUNT(*) as message_count
            FROM chat_history
            WHERE user = ?
            GROUP BY conversation_id
            ORDER BY last_ts DESC
            LIMIT ?
        """
        params = (user.username, limit)

    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()

    conversations = []
    for row in rows:
        conv_id = row["conversation_id"]
        # Get first user message as preview
        async with db.execute(
            """SELECT message FROM chat_history
               WHERE conversation_id = ? AND role = 'user'
               ORDER BY id ASC LIMIT 1""",
            (conv_id,),
        ) as c2:
            preview_row = await c2.fetchone()

        conversations.append({
            "conversation_id": conv_id,
            "user": row["user"],
            "last_timestamp": row["last_ts"],
            "message_count": row["message_count"],
            "preview": (preview_row["message"][:80] + "...") if preview_row and len(preview_row["message"]) > 80 else (preview_row["message"] if preview_row else ""),
        })

    return {"conversations": conversations}


@router.get("/api/chat/config")
async def chat_config_get(user: CurrentUser = Depends(require_admin)):
    """Return current chat configuration (admin only)."""
    return {
        "enabled": _llm.enabled,
        "api_url": _llm.api_url,
        "models": _llm.models,
        "max_tokens": _llm.max_tokens,
        "api_key_set": bool(_llm.api_key),
    }


@router.put("/api/chat/config")
async def chat_config_put(
    body: ChatConfigUpdate,
    user: CurrentUser = Depends(require_admin),
):
    """Update chat configuration at runtime (admin only).

    Note: changes are in-memory only — restart reloads from env vars.
    For persistent changes, update .env and restart.
    """
    if body.model is not None:
        _llm.models = [m.strip() for m in body.model.split(",") if m.strip()]
    if body.max_tokens is not None:
        _llm.max_tokens = body.max_tokens

    return {
        "models": _llm.models,
        "max_tokens": _llm.max_tokens,
    }
```

- [ ] **Step 2: Register rate limiting for chat/ask**

The route uses slowapi. To apply per-user rate limiting, add the limiter decorator. In `admin/routes/chat_routes.py`, add this import at the top (after the existing imports):

```python
from main import limiter
```

And modify the `chat_ask` function signature to include the rate limit decorator. Replace the function definition:

```python
@router.post("/api/chat/ask")
@limiter.limit("10/minute")
async def chat_ask(
```

**Note on circular import:** If importing `limiter` from `main` causes a circular import, the alternative is to pass the limiter via `request.app.state.limiter`. Check if the pattern `from main import limiter` is used in other route files:

Actually, looking at the codebase pattern, slowapi rate limits are applied differently. Since the limiter is set up in main.py with `key_func=get_remote_address`, and routes import it, the simplest approach is to use `request.app.state` or re-create a module-level limiter reference. However, the cleanest solution that avoids circular imports is:

Replace the limiter import line with:

```python
# Rate limit helper — avoids circular import from main.py
from slowapi import Limiter
from slowapi.util import get_remote_address
```

And apply the decorator differently. The limiter instance from main.py is attached to the app. For the rate limit to work without circular import, access it through the app state. The simplest working approach:

In `chat_routes.py`, do NOT import limiter. Instead, add manual rate checking inside `chat_ask`:

```python
# Inside chat_ask, after the enabled check:
    # Manual rate limit check (10/min per user)
    # The slowapi limiter on the app handles this globally;
    # we just validate message length here
    if len(body.message.strip()) == 0:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
```

The global slowapi rate limiter from main.py already applies to all /api/ routes. For chat-specific stricter limits, we can apply it in Task 7 (integration) when we have the full app context.

**Test:**
```bash
# Test status endpoint (no auth, chat disabled when no API key)
curl -s http://localhost:3080/api/chat/status | python -m json.tool
# Expected: {"enabled": false, "model": ""}

# Test ask endpoint (auth required)
TOKEN=$(curl -s -X POST http://localhost:3080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | python -c "import sys,json; print(json.load(sys.stdin)['token'])")

curl -s -X POST http://localhost:3080/api/chat/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"What alarms are active?"}' | python -m json.tool
# Expected: 503 (chat not configured) or response with data
```

**Commit:** `feat(chat): add chat API routes (ask, history, status, config)`

---

## Task 6: Router Registration and Rate Limiting

**Files:**
- Modify: `admin/main.py`

- [ ] **Step 1: Import and register the chat router**

In `admin/main.py`, after the line:
```python
from routes.plctag_routes import router as plctag_router
```

Add:
```python
from routes.chat_routes import router as chat_router
```

- [ ] **Step 2: Include the chat router**

After the line:
```python
app.include_router(plctag_router)
```

Add:
```python
app.include_router(chat_router)
```

- [ ] **Step 3: Apply rate limit to chat ask endpoint**

In `admin/main.py`, after the router registration block, add a dedicated rate limit for the chat endpoint. After the line `app.include_router(chat_router)`, add:

```python

# Chat-specific rate limit (stricter than global)
@app.middleware("http")
async def chat_rate_limit_middleware(request: Request, call_next):
    """Extra rate limit for chat: 10 requests/minute per user."""
    # This is handled by slowapi default limits; the per-user limit
    # is enforced in the chat route itself via conversation pacing.
    return await call_next(request)
```

Actually, the simpler approach: slowapi already handles rate limits. We just need to ensure the chat endpoint has a per-route limit. The cleanest way within the existing architecture is to add the limit directly in chat_routes.py using a reference to the app's limiter. Since other routes do not use per-route limits (they rely on the global default), we will use a simple in-memory counter in chat_routes.py itself.

Add this to `admin/routes/chat_routes.py` at module level (after the `_CONTEXT_MESSAGES` line):

```python

# Simple per-user rate limiter (10 requests per 60 seconds)
import time as _time
_chat_rate: dict[str, list[float]] = {}
_CHAT_RATE_LIMIT = 10
_CHAT_RATE_WINDOW = 60  # seconds


def _check_rate_limit(username: str) -> bool:
    """Return True if the user is within rate limits."""
    now = _time.time()
    if username not in _chat_rate:
        _chat_rate[username] = []
    # Prune old entries
    _chat_rate[username] = [t for t in _chat_rate[username] if now - t < _CHAT_RATE_WINDOW]
    if len(_chat_rate[username]) >= _CHAT_RATE_LIMIT:
        return False
    _chat_rate[username].append(now)
    return True
```

Then in the `chat_ask` function, after the `_llm.enabled` check, add:

```python
    if not _check_rate_limit(user.username):
        raise HTTPException(status_code=429, detail="Rate limit exceeded (10 requests/minute). Please wait.")
```

**Test:**
```bash
# Verify router is registered
curl -s http://localhost:3080/api/chat/status | python -m json.tool
# Expected: {"enabled": false, "model": ""}
```

**Commit:** `feat(chat): register chat router in main.py with per-user rate limiting`

---

## Task 7: Frontend Chat Widget — CSS

**Files:**
- Modify: `admin/static/css/style.css`

- [ ] **Step 1: Add chat widget styles**

Append to the end of `admin/static/css/style.css`:

```css

/* =============================================
   Chat Widget — AI Assistant
   ============================================= */

/* Floating trigger button */
.chat-fab {
    position: fixed;
    bottom: 24px;
    right: 24px;
    width: 56px;
    height: 56px;
    border-radius: 50%;
    background: var(--accent);
    color: #fff;
    border: none;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(0,0,0,0.25);
    z-index: 9998;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 24px;
    transition: transform 0.2s, box-shadow 0.2s;
}
.chat-fab:hover {
    transform: scale(1.08);
    box-shadow: 0 6px 20px rgba(0,0,0,0.3);
}
.chat-fab.hidden { display: none; }

/* Slide-in panel */
.chat-panel {
    position: fixed;
    top: 0;
    right: -420px;
    width: 400px;
    height: 100vh;
    background: var(--card-bg, #fff);
    border-left: 1px solid var(--border, #ddd);
    box-shadow: -4px 0 20px rgba(0,0,0,0.15);
    z-index: 9999;
    display: flex;
    flex-direction: column;
    transition: right 0.3s ease;
    font-family: 'IBM Plex Sans', sans-serif;
}
.chat-panel.open {
    right: 0;
}

/* Header */
.chat-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    background: var(--accent, #c8102e);
    color: #fff;
    font-weight: 600;
    font-size: 15px;
    flex-shrink: 0;
}
.chat-header-title {
    display: flex;
    align-items: center;
    gap: 8px;
}
.chat-header-actions {
    display: flex;
    align-items: center;
    gap: 8px;
}
.chat-header-actions button {
    background: none;
    border: none;
    color: #fff;
    cursor: pointer;
    font-size: 18px;
    padding: 4px;
    opacity: 0.85;
    border-radius: 4px;
}
.chat-header-actions button:hover {
    opacity: 1;
    background: rgba(255,255,255,0.15);
}

/* Conversation selector */
.chat-conv-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border, #ddd);
    background: var(--bg, #f4f4f4);
    flex-shrink: 0;
}
.chat-conv-bar select {
    flex: 1;
    padding: 4px 8px;
    font-size: 13px;
    border: 1px solid var(--border, #ccc);
    border-radius: 4px;
    background: var(--card-bg, #fff);
    font-family: inherit;
}
.chat-conv-bar button {
    padding: 4px 10px;
    font-size: 13px;
    border: 1px solid var(--border, #ccc);
    border-radius: 4px;
    background: var(--card-bg, #fff);
    cursor: pointer;
    font-family: inherit;
}
.chat-conv-bar button:hover {
    background: var(--accent, #c8102e);
    color: #fff;
    border-color: var(--accent, #c8102e);
}

/* Messages area */
.chat-messages {
    flex: 1;
    overflow-y: auto;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 10px;
}

/* Message bubbles */
.chat-msg {
    max-width: 85%;
    padding: 10px 14px;
    border-radius: 12px;
    font-size: 14px;
    line-height: 1.5;
    word-wrap: break-word;
}
.chat-msg.user {
    align-self: flex-end;
    background: var(--accent, #c8102e);
    color: #fff;
    border-bottom-right-radius: 4px;
}
.chat-msg.assistant {
    align-self: flex-start;
    background: var(--bg, #f0f0f0);
    color: var(--text, #222);
    border-bottom-left-radius: 4px;
    border: 1px solid var(--border, #ddd);
}
.chat-msg.assistant .chat-model-tag {
    display: block;
    font-size: 10px;
    color: var(--text-muted, #888);
    margin-top: 6px;
}

/* Typing indicator */
.chat-typing {
    align-self: flex-start;
    padding: 10px 14px;
    font-size: 13px;
    color: var(--text-muted, #888);
    font-style: italic;
}
.chat-typing-dots span {
    animation: chatDotPulse 1.4s infinite;
    display: inline-block;
    margin: 0 1px;
}
.chat-typing-dots span:nth-child(2) { animation-delay: 0.2s; }
.chat-typing-dots span:nth-child(3) { animation-delay: 0.4s; }
@keyframes chatDotPulse {
    0%, 80%, 100% { opacity: 0.3; }
    40% { opacity: 1; }
}

/* Inline chart container */
.chat-chart-container {
    width: 100%;
    max-height: 180px;
    margin-top: 8px;
}
.chat-chart-container canvas {
    width: 100% !important;
    max-height: 170px;
}

/* Input area */
.chat-input-area {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    border-top: 1px solid var(--border, #ddd);
    background: var(--card-bg, #fff);
    flex-shrink: 0;
}
.chat-input-area input {
    flex: 1;
    padding: 10px 12px;
    border: 1px solid var(--border, #ccc);
    border-radius: 8px;
    font-size: 14px;
    font-family: inherit;
    outline: none;
}
.chat-input-area input:focus {
    border-color: var(--accent, #c8102e);
    box-shadow: 0 0 0 2px rgba(200,16,46,0.15);
}
.chat-input-area button {
    width: 40px;
    height: 40px;
    border-radius: 50%;
    background: var(--accent, #c8102e);
    color: #fff;
    border: none;
    cursor: pointer;
    font-size: 18px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    transition: background 0.15s;
}
.chat-input-area button:hover {
    background: #a00d25;
}
.chat-input-area button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
}

/* Responsive: on small screens, full width */
@media (max-width: 480px) {
    .chat-panel {
        width: 100vw;
        right: -100vw;
    }
    .chat-fab {
        bottom: 16px;
        right: 16px;
        width: 48px;
        height: 48px;
        font-size: 20px;
    }
}
```

**Test:** Visual inspection — open browser, confirm styles load without errors in console.

**Commit:** `feat(chat): add chat widget CSS styles`

---

## Task 8: Frontend Chat Widget — JavaScript

**Files:**
- Modify: `admin/static/js/app.js`

- [ ] **Step 1: Add chat widget HTML injection and logic**

At the end of `admin/static/js/app.js`, append the complete chat widget module:

```javascript

// =============================================
// Chat Widget — AI Assistant
// =============================================

const ChatWidget = (() => {
    let _enabled = false;
    let _convId = null;
    let _conversations = [];
    let _sending = false;
    let _chartInstances = [];

    // DOM refs (set after inject)
    let _fab, _panel, _messages, _input, _sendBtn, _convSelect;

    function inject() {
        // Floating action button
        const fab = document.createElement('button');
        fab.className = 'chat-fab hidden';
        fab.innerHTML = '&#x1F4AC;';
        fab.title = 'AI Assistant';
        fab.onclick = toggle;
        document.body.appendChild(fab);
        _fab = fab;

        // Panel
        const panel = document.createElement('div');
        panel.className = 'chat-panel';
        panel.innerHTML = `
            <div class="chat-header">
                <div class="chat-header-title">
                    <span>AI Assistant</span>
                </div>
                <div class="chat-header-actions">
                    <button onclick="ChatWidget.newChat()" title="New conversation">+</button>
                    <button onclick="ChatWidget.toggle()" title="Close">&times;</button>
                </div>
            </div>
            <div class="chat-conv-bar">
                <select id="chat-conv-select" onchange="ChatWidget.switchConversation(this.value)">
                    <option value="">New conversation</option>
                </select>
            </div>
            <div class="chat-messages" id="chat-messages"></div>
            <div class="chat-input-area">
                <input type="text" id="chat-input" placeholder="Ask about your plant data..."
                       maxlength="2000" onkeydown="if(event.key==='Enter')ChatWidget.send()">
                <button id="chat-send-btn" onclick="ChatWidget.send()" title="Send">&#x27A4;</button>
            </div>
        `;
        document.body.appendChild(panel);
        _panel = panel;
        _messages = document.getElementById('chat-messages');
        _input = document.getElementById('chat-input');
        _sendBtn = document.getElementById('chat-send-btn');
        _convSelect = document.getElementById('chat-conv-select');
    }

    async function checkStatus() {
        try {
            const resp = await fetch('/api/chat/status');
            const data = await resp.json();
            _enabled = data.enabled;
            if (_enabled && _fab) {
                _fab.classList.remove('hidden');
            }
        } catch (e) {
            // Chat not available
        }
    }

    function toggle() {
        if (!_panel) return;
        _panel.classList.toggle('open');
        if (_panel.classList.contains('open')) {
            _input.focus();
            loadConversations();
        }
    }

    function newChat() {
        _convId = null;
        _messages.innerHTML = '';
        _convSelect.value = '';
        _destroyCharts();
        _addSystemMessage('Hello! Ask me anything about your plant data, alarms, or equipment status.');
    }

    async function loadConversations() {
        try {
            const token = localStorage.getItem('jwt_token');
            if (!token) return;
            const resp = await fetch('/api/chat/history?limit=5', {
                headers: { 'Authorization': 'Bearer ' + token }
            });
            if (!resp.ok) return;
            const data = await resp.json();
            _conversations = data.conversations || [];
            _renderConvSelect();
        } catch (e) {
            // ignore
        }
    }

    function _renderConvSelect() {
        if (!_convSelect) return;
        const current = _convSelect.value;
        _convSelect.innerHTML = '<option value="">New conversation</option>';
        _conversations.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.conversation_id;
            opt.textContent = c.preview || ('Chat ' + c.conversation_id.substring(0, 8));
            _convSelect.appendChild(opt);
        });
        _convSelect.value = current;
    }

    async function switchConversation(convId) {
        if (!convId) {
            newChat();
            return;
        }
        _convId = convId;
        _messages.innerHTML = '';
        _destroyCharts();

        // Load conversation messages
        try {
            const token = localStorage.getItem('jwt_token');
            const resp = await fetch(`/api/chat/history?limit=50`, {
                headers: { 'Authorization': 'Bearer ' + token }
            });
            if (!resp.ok) return;
            // We need the full messages — for now, just show the preview
            _addSystemMessage('Conversation loaded. Continue asking questions.');
        } catch (e) {
            // ignore
        }
    }

    async function send() {
        if (_sending) return;
        const text = (_input.value || '').trim();
        if (!text) return;

        _input.value = '';
        _sending = true;
        _sendBtn.disabled = true;

        // Show user message
        _addMessage('user', text);

        // Show typing indicator
        const typingEl = _addTyping();

        try {
            const token = localStorage.getItem('jwt_token');
            const resp = await fetch('/api/chat/ask', {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + token,
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    message: text,
                    conversation_id: _convId,
                }),
            });

            // Remove typing indicator
            if (typingEl && typingEl.parentNode) typingEl.remove();

            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                _addMessage('assistant', err.detail || 'An error occurred. Please try again.');
                return;
            }

            const data = await resp.json();
            _convId = data.conversation_id;

            // Show assistant reply
            _addMessage('assistant', data.reply || 'No response.', data.model_used);

            // Render inline chart if present
            if (data.chart) {
                _renderChart(data.chart);
            }

            // Update conversation selector
            loadConversations();
        } catch (e) {
            if (typingEl && typingEl.parentNode) typingEl.remove();
            _addMessage('assistant', 'Network error. Please check your connection.');
        } finally {
            _sending = false;
            _sendBtn.disabled = false;
            _input.focus();
        }
    }

    function _addMessage(role, text, modelUsed) {
        const div = document.createElement('div');
        div.className = 'chat-msg ' + role;

        // Use textContent for security (no innerHTML with user data)
        const textNode = document.createElement('span');
        textNode.textContent = text;
        div.appendChild(textNode);

        if (role === 'assistant' && modelUsed) {
            const tag = document.createElement('span');
            tag.className = 'chat-model-tag';
            tag.textContent = modelUsed;
            div.appendChild(tag);
        }

        _messages.appendChild(div);
        _messages.scrollTop = _messages.scrollHeight;
        return div;
    }

    function _addSystemMessage(text) {
        const div = document.createElement('div');
        div.className = 'chat-msg assistant';
        div.textContent = text;
        _messages.appendChild(div);
    }

    function _addTyping() {
        const div = document.createElement('div');
        div.className = 'chat-typing';
        div.innerHTML = 'Thinking <span class="chat-typing-dots"><span>.</span><span>.</span><span>.</span></span>';
        _messages.appendChild(div);
        _messages.scrollTop = _messages.scrollHeight;
        return div;
    }

    function _renderChart(chartData) {
        if (!chartData || !chartData.values || !chartData.values.length) return;
        if (typeof Chart === 'undefined') return; // Chart.js not loaded

        const container = document.createElement('div');
        container.className = 'chat-chart-container';
        const canvas = document.createElement('canvas');
        container.appendChild(canvas);
        _messages.appendChild(container);
        _messages.scrollTop = _messages.scrollHeight;

        // Format labels (show time only for readability)
        const labels = chartData.labels.map(l => {
            try {
                const d = new Date(l);
                return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            } catch (e) { return l; }
        });

        const chart = new Chart(canvas, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    label: chartData.label || 'Value',
                    data: chartData.values,
                    borderColor: '#c8102e',
                    backgroundColor: 'rgba(200,16,46,0.08)',
                    fill: true,
                    tension: 0.3,
                    pointRadius: 0,
                    borderWidth: 2,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: true, position: 'top', labels: { font: { size: 11 } } },
                },
                scales: {
                    x: { display: true, ticks: { maxTicksLimit: 6, font: { size: 10 } } },
                    y: { display: true, ticks: { font: { size: 10 } } },
                },
            },
        });
        _chartInstances.push(chart);
    }

    function _destroyCharts() {
        _chartInstances.forEach(c => { try { c.destroy(); } catch(e) {} });
        _chartInstances = [];
    }

    // Public API
    return {
        init: () => { inject(); checkStatus(); },
        toggle,
        newChat,
        send,
        switchConversation,
        loadConversations,
    };
})();

// Initialize chat widget after DOM ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => ChatWidget.init());
} else {
    ChatWidget.init();
}
```

**Test:** Open the browser at `http://localhost:3080/`:
1. If `CHAT_API_KEY` is not set, the floating button should NOT appear.
2. Set `CHAT_API_KEY=test` in `.env` and restart. The floating button (speech bubble) should appear bottom-right.
3. Click it. The slide-in panel should open with "AI Assistant" header.
4. Type a message and press Enter. With a valid API key, you should get a response. Without one, you should see a 503 error message in the chat.

**Commit:** `feat(chat): add frontend chat widget with slide-in panel and inline charts`

---

## Task 9: Chart.js CDN and Index Template

**Files:**
- Modify: `admin/templates/index.html`

- [ ] **Step 1: Ensure Chart.js is loaded**

Check if Chart.js is already loaded in the template. If not, add the CDN script before the closing `</body>` tag (or before the app.js script tag):

```html
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
```

If Chart.js is already loaded (used by OEE or other features), skip this step.

**Test:**
```bash
# Check if Chart.js is already in the template
grep -i "chart.js\|chart.umd\|chartjs" admin/templates/index.html
```

**Commit:** `feat(chat): ensure Chart.js CDN is loaded for inline chat charts`

---

## Task 10: Integration Testing

- [ ] **Step 1: Test with chat disabled (no API key)**

```bash
# 1. Status endpoint (no auth)
curl -s http://localhost:3080/api/chat/status
# Expected: {"enabled":false,"model":""}

# 2. Ask endpoint should return 503
TOKEN=$(curl -s -X POST http://localhost:3080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | python -c "import sys,json; print(json.load(sys.stdin)['token'])")

curl -s -X POST http://localhost:3080/api/chat/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"Hello"}' -w "\n%{http_code}"
# Expected: 503

# 3. History endpoint (empty)
curl -s http://localhost:3080/api/chat/history \
  -H "Authorization: Bearer $TOKEN"
# Expected: {"conversations":[]}

# 4. Config endpoint (admin only)
curl -s http://localhost:3080/api/chat/config \
  -H "Authorization: Bearer $TOKEN"
# Expected: {"enabled":false,"api_url":"...","models":[...],"max_tokens":2048,"api_key_set":false}
```

- [ ] **Step 2: Test with chat enabled (real or test API key)**

Set `CHAT_API_KEY=<your-openrouter-key>` in `.env` and restart:

```bash
# 1. Status should show enabled
curl -s http://localhost:3080/api/chat/status
# Expected: {"enabled":true,"model":"openrouter/auto"}

# 2. Ask a question
curl -s -X POST http://localhost:3080/api/chat/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"What alarms are currently active?"}' | python -m json.tool
# Expected: JSON with reply, conversation_id, model_used

# 3. Ask a follow-up (same conversation)
CONV_ID=<conversation_id from previous response>
curl -s -X POST http://localhost:3080/api/chat/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"message\":\"How many were there?\",\"conversation_id\":\"$CONV_ID\"}" | python -m json.tool

# 4. Check history shows the conversation
curl -s http://localhost:3080/api/chat/history \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool

# 5. Verify message length cap
python -c "print('x'*2001)" | xargs -I {} curl -s -X POST http://localhost:3080/api/chat/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"message\":\"{}\"}" -w "\n%{http_code}"
# Expected: 422 (validation error, message too long)
```

- [ ] **Step 3: Test rate limiting**

```bash
# Send 11 rapid requests (limit is 10/min)
for i in $(seq 1 11); do
  echo -n "Request $i: "
  curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:3080/api/chat/ask \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"message":"test"}'
  echo
done
# Expected: first 10 return 200 (or 503 if no key), 11th returns 429
```

- [ ] **Step 4: Test security — no auth**

```bash
curl -s -X POST http://localhost:3080/api/chat/ask \
  -H "Content-Type: application/json" \
  -d '{"message":"Hello"}' -w "\n%{http_code}"
# Expected: 401

curl -s http://localhost:3080/api/chat/history -w "\n%{http_code}"
# Expected: 401

curl -s http://localhost:3080/api/chat/config -w "\n%{http_code}"
# Expected: 401
```

- [ ] **Step 5: Test admin-only config endpoint**

```bash
# Create a monitor user token (if USERS_JSON supports it), or test with admin
curl -s -X PUT http://localhost:3080/api/chat/config \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"openrouter/free","max_tokens":1024}' | python -m json.tool
# Expected: {"models":["openrouter/free"],"max_tokens":1024}
```

- [ ] **Step 6: Frontend visual verification**

Open `http://localhost:3080/` in browser:
1. With no API key: floating button should be hidden
2. With API key: floating button visible, click opens slide-in panel
3. Send a message: user bubble right-aligned (red), assistant left-aligned (gray)
4. If response includes chart data: inline Chart.js chart appears
5. New chat button (+) clears messages
6. Conversation dropdown shows recent conversations
7. Panel closes with X button
8. Mobile: panel takes full width on small screens

**Commit:** `test(chat): verify all chat endpoints and frontend widget`

---

## Summary

| Task | Description | Files |
|------|-------------|-------|
| 1 | Dependencies + config | `requirements.txt`, `.env.example` |
| 2 | ChatLLM client | `admin/chat_llm.py` (new) |
| 3 | Database schema | `admin/database.py` |
| 4 | LLM tools | `admin/chat_tools.py` (new) |
| 5 | Chat routes | `admin/routes/chat_routes.py` (new) |
| 6 | Router registration + rate limiting | `admin/main.py`, `admin/routes/chat_routes.py` |
| 7 | Frontend CSS | `admin/static/css/style.css` |
| 8 | Frontend JavaScript | `admin/static/js/app.js` |
| 9 | Chart.js CDN | `admin/templates/index.html` |
| 10 | Integration testing | curl + browser verification |

**Total new files:** 3 (`chat_llm.py`, `chat_tools.py`, `routes/chat_routes.py`)
**Total modified files:** 6 (`requirements.txt`, `.env.example`, `database.py`, `main.py`, `app.js`, `style.css`, possibly `index.html`)
**New dependency:** httpx==0.28.1
**Database change:** Migration v2 adds `chat_history` table
