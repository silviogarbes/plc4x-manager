"""
OpenRouter LLM client with automatic model fallback.

Fallback chain:
1. Try with all configured models (native OpenRouter fallback)
2. On 402 (payment required): retry with ["openrouter/free"] only
3. On 429 (rate limited): return friendly message
4. On 5xx (server error): retry once
5. On timeout (30s): return unavailable message
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

log = logging.getLogger("chat_llm")


class ChatLLM:
    """Async OpenRouter API client with fallback chain."""

    def __init__(self) -> None:
        self.api_key: str = os.environ.get("CHAT_API_KEY", "")
        self.api_url: str = os.environ.get("CHAT_API_URL", "https://openrouter.ai/api/v1")
        model_str = os.environ.get("CHAT_MODEL", "openrouter/auto,openrouter/free")
        self.models: list[str] = [m.strip() for m in model_str.split(",") if m.strip()]
        self.max_tokens: int = int(os.environ.get("CHAT_MAX_TOKENS", "2048"))
        self.enabled: bool = bool(self.api_key)

    @property
    def primary_model(self) -> str:
        return self.models[0] if self.models else "openrouter/auto"

    def _build_request(self, messages: list[dict], tools: Optional[list[dict]], models: list[str]) -> dict:
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

    async def ask(self, messages: list[dict], tools: Optional[list[dict]] = None) -> dict:
        """Send messages to OpenRouter and return the response."""
        if not self.enabled:
            return {"content": "Chat is not configured. Set CHAT_API_KEY in your environment.", "tool_calls": None, "model_used": "", "error": "not_configured"}

        url = f"{self.api_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/plc4x-manager",
            "X-Title": "PLC4X Manager",
        }

        body = self._build_request(messages, tools, self.models)
        result = await self._call(url, headers, body)

        if result.get("error") == "payment_required":
            log.warning("402 from OpenRouter, falling back to openrouter/free")
            body = self._build_request(messages, tools, ["openrouter/free"])
            result = await self._call(url, headers, body)
        elif result.get("error") == "server_error":
            log.warning("5xx from OpenRouter, retrying once")
            result = await self._call(url, headers, body)

        return result

    async def _call(self, url: str, headers: dict, body: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, headers=headers, json=body)

            if resp.status_code == 402:
                return {"content": "", "tool_calls": None, "model_used": "", "error": "payment_required"}
            if resp.status_code == 429:
                return {"content": "The AI service is temporarily rate-limited. Please try again in a minute.", "tool_calls": None, "model_used": "", "error": "rate_limited"}
            if resp.status_code >= 500:
                return {"content": "", "tool_calls": None, "model_used": "", "error": "server_error"}
            if resp.status_code != 200:
                log.error("OpenRouter %d: %s", resp.status_code, resp.text[:500])
                return {"content": f"AI service returned an error (HTTP {resp.status_code}).", "tool_calls": None, "model_used": "", "error": "api_error"}

            data = resp.json()
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            model_used = data.get("model", "")

            tool_calls = message.get("tool_calls")
            if tool_calls:
                tool_calls = [{"id": tc.get("id", ""), "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}} for tc in tool_calls]

            return {"content": message.get("content", "") or "", "tool_calls": tool_calls, "model_used": model_used, "error": None}

        except httpx.TimeoutException:
            log.warning("OpenRouter request timed out (30s)")
            return {"content": "The AI service is temporarily unavailable (timeout). Please try again.", "tool_calls": None, "model_used": "", "error": "timeout"}
        except Exception as exc:
            log.error("OpenRouter call failed: %s", exc)
            return {"content": "An unexpected error occurred contacting the AI service.", "tool_calls": None, "model_used": "", "error": "exception"}
