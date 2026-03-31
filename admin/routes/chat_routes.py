"""
NLP Chat API routes for PLC4X Manager.

Endpoints:
  POST /api/chat/ask      — Send a question, get AI response
  GET  /api/chat/history   — Conversation history
  GET  /api/chat/status    — Chat availability (no auth)
  GET  /api/chat/config    — Chat config (admin)
  PUT  /api/chat/config    — Update chat config (admin)
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from auth import CurrentUser, get_current_user, require_admin
from chat_llm import ChatLLM
from chat_tools import TOOL_DEFINITIONS, execute_tool

log = logging.getLogger("chat_routes")

router = APIRouter(tags=["chat"])

_llm = ChatLLM()

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

# Simple per-user rate limiter (10 requests per 60 seconds)
import time as _time
_chat_rate: dict[str, list[float]] = {}
_CHAT_RATE_LIMIT = 10
_CHAT_RATE_WINDOW = 60


def _check_rate_limit(username: str) -> bool:
    now = _time.time()
    if username not in _chat_rate:
        _chat_rate[username] = []
    _chat_rate[username] = [t for t in _chat_rate[username] if now - t < _CHAT_RATE_WINDOW]
    if len(_chat_rate[username]) >= _CHAT_RATE_LIMIT:
        return False
    _chat_rate[username].append(now)
    return True


class ChatAskRequest(BaseModel):
    message: str = Field(..., max_length=_MAX_MESSAGE_LENGTH)
    conversation_id: str | None = None


class ChatConfigUpdate(BaseModel):
    model: str | None = None
    max_tokens: int | None = Field(None, ge=256, le=8192)


async def _save_message(db, conversation_id, role, message, user, tool_calls=None, model_used=None):
    await db.execute(
        """INSERT INTO chat_history (conversation_id, role, message, tool_calls, model_used, user)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (conversation_id, role, message, json.dumps(tool_calls) if tool_calls else None, model_used, user),
    )
    await db.commit()


async def _load_conversation(db, conversation_id, limit=_CONTEXT_MESSAGES):
    async with db.execute(
        "SELECT role, message, tool_calls FROM chat_history WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
        (conversation_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
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


@router.get("/api/chat/status")
async def chat_status():
    """Chat availability. No auth required."""
    return {"enabled": _llm.enabled, "model": _llm.primary_model if _llm.enabled else ""}


@router.post("/api/chat/ask")
async def chat_ask(body: ChatAskRequest, request: Request, user: CurrentUser = Depends(get_current_user)):
    """Process a chat question with LLM + tool calling."""
    if not _llm.enabled:
        raise HTTPException(status_code=503, detail="Chat not configured (CHAT_API_KEY not set)")

    if not _check_rate_limit(user.username):
        raise HTTPException(status_code=429, detail="Rate limit exceeded (10 requests/minute). Please wait.")

    if len(body.message.strip()) == 0:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    db = request.app.state.db
    conversation_id = body.conversation_id or str(uuid.uuid4())

    await _save_message(db, conversation_id, "user", body.message, user.username)

    history = await _load_conversation(db, conversation_id)
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for msg in history[:-1]:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": body.message})

    chart_data = None
    all_tool_data = []
    model_used = ""
    reply = ""

    for iteration in range(_MAX_TOOL_ITERATIONS):
        result = await _llm.ask(messages, TOOL_DEFINITIONS)
        model_used = result.get("model_used", "")

        if result.get("error") and result["error"] not in ("rate_limited", "timeout"):
            if not result["content"]:
                result["content"] = "Sorry, I could not process your request right now."

        tool_calls = result.get("tool_calls")

        if not tool_calls:
            reply = result["content"]
            break
        else:
            assistant_msg = {"role": "assistant", "content": result["content"] or ""}
            assistant_msg["tool_calls"] = [{"id": tc["id"], "type": "function", "function": tc["function"]} for tc in tool_calls]
            messages.append(assistant_msg)

            for tc in tool_calls:
                func_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                tool_result = await execute_tool(func_name, args, db=db)
                all_tool_data.append(tool_result)

                if "chart_data" in tool_result:
                    chart_data = tool_result["chart_data"]

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(tool_result.get("result", tool_result.get("error", "")))[:4000],
                })
    else:
        reply = result.get("content", "I ran out of steps processing your question.")

    await _save_message(db, conversation_id, "assistant", reply, user.username, model_used=model_used)

    return {"reply": reply, "data": all_tool_data if all_tool_data else None, "chart": chart_data, "model_used": model_used, "conversation_id": conversation_id}


@router.get("/api/chat/history")
async def chat_history(request: Request, all: bool = Query(default=False), limit: int = Query(default=50, ge=1, le=500), user: CurrentUser = Depends(get_current_user)):
    """Conversation list with previews."""
    db = request.app.state.db

    if all and user.role == "admin":
        query = "SELECT conversation_id, user, MAX(timestamp) as last_ts, COUNT(*) as message_count FROM chat_history GROUP BY conversation_id ORDER BY last_ts DESC LIMIT ?"
        params = (limit,)
    else:
        query = "SELECT conversation_id, user, MAX(timestamp) as last_ts, COUNT(*) as message_count FROM chat_history WHERE user = ? GROUP BY conversation_id ORDER BY last_ts DESC LIMIT ?"
        params = (user.username, limit)

    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()

    conversations = []
    for row in rows:
        conv_id = row["conversation_id"]
        async with db.execute("SELECT message FROM chat_history WHERE conversation_id = ? AND role = 'user' ORDER BY id ASC LIMIT 1", (conv_id,)) as c2:
            preview_row = await c2.fetchone()
        preview = (preview_row["message"][:80] + "...") if preview_row and len(preview_row["message"]) > 80 else (preview_row["message"] if preview_row else "")
        conversations.append({"conversation_id": conv_id, "user": row["user"], "last_timestamp": row["last_ts"], "message_count": row["message_count"], "preview": preview})

    return {"conversations": conversations}


@router.get("/api/chat/messages")
async def chat_messages(
    request: Request,
    conversation_id: str = Query(...),
    user: CurrentUser = Depends(get_current_user),
):
    """Return all messages for a specific conversation."""
    db = request.app.state.db

    # Verify user owns this conversation (or is admin)
    async with db.execute(
        "SELECT user FROM chat_history WHERE conversation_id = ? LIMIT 1",
        (conversation_id,),
    ) as c:
        row = await c.fetchone()
    if not row:
        return {"messages": []}
    if row["user"] != user.username and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    async with db.execute(
        "SELECT role, message, model_used, timestamp FROM chat_history WHERE conversation_id = ? ORDER BY id ASC",
        (conversation_id,),
    ) as cursor:
        rows = await cursor.fetchall()

    messages = []
    for r in rows:
        messages.append({
            "role": r["role"],
            "message": r["message"],
            "model_used": r["model_used"],
            "timestamp": r["timestamp"],
        })

    return {"messages": messages, "conversation_id": conversation_id}


@router.get("/api/chat/config")
async def chat_config_get(user: CurrentUser = Depends(require_admin)):
    return {"enabled": _llm.enabled, "api_url": _llm.api_url, "models": _llm.models, "max_tokens": _llm.max_tokens, "api_key_set": bool(_llm.api_key)}


@router.put("/api/chat/config")
async def chat_config_put(body: ChatConfigUpdate, user: CurrentUser = Depends(require_admin)):
    if body.model is not None:
        _llm.models = [m.strip() for m in body.model.split(",") if m.strip()]
    if body.max_tokens is not None:
        _llm.max_tokens = body.max_tokens
    return {"models": _llm.models, "max_tokens": _llm.max_tokens}
