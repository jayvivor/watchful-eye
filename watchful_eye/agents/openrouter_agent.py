"""An LLM player backed by the official OpenRouter Python SDK (`pip install openrouter`).

Each agent keeps its own conversation: the system prompt, then one record per past decision — the
events the player saw before it, and the model's reply (its reasoning + decision) — followed by the
current request (events since, position, legal options). Once a decision is settled the engine calls
commit(), which strips that turn's position/legal options out of the record so only the events and the
reply remain. The prefix is append-only apart from that one trailing edit, so prompt caching covers
nearly all of it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..config import LLMConfig, ModelConfig
from ..decisions import json_schema
from .base import AgentResponse, DecisionRequest


class OpenRouterAgent:
    def __init__(self, name: str, model: ModelConfig, llm: LLMConfig, api_key: str):
        from openrouter import OpenRouter  # imported lazily so random-only runs need no key

        self.name = name
        self.model = model
        self.llm = llm
        self.client = OpenRouter(api_key=api_key, timeout_ms=int(llm.request_timeout_s * 1000))
        self._response_format_ok = llm.response_format != "none"
        self.system_prompt: str | None = None
        # One record per host message: {"user": text, "assistant": reply or None}
        self.records: list[dict[str, str | None]] = []
        self._pending: list[str] = []  # observations to prepend to the next host message
        self._turn_start: int | None = None  # index in records where the current decision began

    # ------------------------------------------------------------------ public

    def observe(self, text: str) -> None:
        self._pending.append(text)

    def commit(self, history_text: str, keep_reply: bool) -> None:
        if self._turn_start is None:
            return
        reply = self.records[-1]["assistant"] if keep_reply and self.records else None
        del self.records[self._turn_start :]
        self.records.append({"user": history_text, "assistant": reply})
        self._turn_start = None

    def decide(self, request: DecisionRequest) -> AgentResponse:
        self.system_prompt = request.system_prompt
        if self._turn_start is None:
            self._turn_start = len(self.records)
        content = request.user_prompt
        if self._pending:
            content = "\n".join(self._pending) + "\n\n" + content
            self._pending.clear()
        self.records.append({"user": content, "assistant": None})

        kwargs: dict[str, Any] = {
            "model": self.model.model,
            "messages": self.messages(),
            "x_open_router_title": self.llm.app_title,
        }
        if self.model.temperature is not None:
            kwargs["temperature"] = self.model.temperature
        if self.model.max_tokens is not None:
            kwargs["max_tokens"] = self.model.max_tokens
        if self.model.reasoning:
            kwargs["reasoning"] = self.model.reasoning
        if self.model.provider:
            kwargs["provider"] = self.model.provider
        if self.llm.cache_control:
            kwargs["cache_control"] = {"type": "ephemeral"}
        kwargs.update(self.model.extra)
        if self._response_format_ok:
            kwargs["response_format"] = self._response_format(request)

        try:
            result = self._send(kwargs)
        except Exception as e:  # noqa: BLE001 — any SDK/transport error becomes a decision error
            return AgentResponse(data=None, error=f"OpenRouter error: {type(e).__name__}: {e}", transport_error=True)

        text = _content_text(result)
        self.records[-1]["assistant"] = text or "{}"
        cost, tokens, prompt, cached, written = 0.0, 0, 0, 0, 0
        usage = getattr(result, "usage", None)
        if usage is not None:
            cost = float(getattr(usage, "cost", 0) or 0)
            tokens = int(getattr(usage, "total_tokens", 0) or 0)
            prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None and not isinstance(details, (str, int)):
                cached = int(getattr(details, "cached_tokens", 0) or 0)
                written = int(getattr(details, "cache_write_tokens", 0) or 0)
        data, err = _parse_json(text)
        return AgentResponse(data=data, raw=text, cost=cost, tokens=tokens, prompt_tokens=prompt,
                             cached_tokens=cached, cache_write_tokens=written, error=err)

    def messages(self) -> list[dict[str, str]]:
        """The conversation as sent: system, then records flattened with consecutive user turns merged."""
        recs = self.records
        n = self.llm.max_history_messages
        if n is not None and len(recs) > n:
            recs = recs[-n:]
        out: list[dict[str, str]] = [{"role": "system", "content": self.system_prompt or ""}]
        for r in recs:
            if out[-1]["role"] == "user":
                out[-1]["content"] += "\n\n" + (r["user"] or "")
            else:
                out.append({"role": "user", "content": r["user"] or ""})
            if r["assistant"]:
                out.append({"role": "assistant", "content": r["assistant"]})
        return out

    # ----------------------------------------------------------------- helpers

    def _response_format(self, request: DecisionRequest) -> dict[str, Any]:
        if self.llm.response_format == "json_object":
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": request.kind,
                "strict": self.llm.strict_json,
                "schema": json_schema(request.schema_model, strict=self.llm.strict_json),
            },
        }

    def _send(self, kwargs: dict[str, Any]):
        from openrouter.errors import OpenRouterError

        try:
            return self.client.chat.send(**kwargs)
        except OpenRouterError as e:
            # Some providers reject structured outputs; drop it and try plain JSON-in-text once.
            if "response_format" in kwargs and _looks_like_format_rejection(e):
                self._response_format_ok = False
                kwargs.pop("response_format", None)
                return self.client.chat.send(**kwargs)
            raise


def _looks_like_format_rejection(e: Exception) -> bool:
    msg = str(e).lower()
    status = getattr(e, "status_code", None)
    return status == 400 or "response_format" in msg or "json_schema" in msg or "structured" in msg


def _content_text(result: Any) -> str:
    try:
        content = result.choices[0].message.content
    except (AttributeError, IndexError):
        return ""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if text:
            parts.append(text)
    return "\n".join(parts)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _parse_json(text: str) -> tuple[dict[str, Any] | None, str | None]:
    if not text.strip():
        return None, "empty response"
    candidates = [text.strip()]
    candidates += [m.strip() for m in _FENCE.findall(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj, None
    return None, "response was not a JSON object"
