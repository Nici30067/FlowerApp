"""Responses-API facade over Chat Completions, for providers such as Google Gemini that lack `/responses`.

The specialists only use a small part of the Responses API: `instructions`, `input` items (user text,
function calls, function call outputs), function `tools`, `tool_choice`, `max_output_tokens` and, in replies,
`output` items plus `output_text`. This adapter maps exactly that subset onto `chat.completions.create`.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


@dataclass
class AdaptedUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass
class AdaptedResponse:
    """The subset of `openai.types.responses.Response` that the runner reads."""
    id: str
    model: str | None
    output: list[dict[str, Any]] = field(default_factory=list)
    output_text: str = ""
    status: str = "completed"
    usage: AdaptedUsage = field(default_factory=AdaptedUsage)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def to_chat_messages(instructions: str | None, items: list[dict[str, Any]] | str) -> list[dict[str, Any]]:
    """Translate Responses input items into Chat Completions messages, preserving tool-call pairing."""
    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if isinstance(items, str):
        return messages + [{"role": "user", "content": items}]
    pending_calls: list[dict[str, Any]] = []

    def flush_calls() -> None:
        if pending_calls:
            messages.append({"role": "assistant", "content": None, "tool_calls": list(pending_calls)})
            pending_calls.clear()

    for item in items:
        kind = item.get("type") or ("message" if "role" in item else "")
        if kind == "function_call":
            arguments = item.get("arguments", "{}")
            call = {"id": str(item.get("call_id", "")), "type": "function",
                    "function": {"name": str(item.get("name", "")),
                                 "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}}
            # Providers such as Gemini attach opaque data (thought signatures) that must be echoed back verbatim.
            raw = item.get("provider_tool_call")
            if isinstance(raw, dict):
                call = {**raw, **call, "function": {**raw.get("function", {}), **call["function"]}}
            pending_calls.append(call)
            continue
        flush_calls()
        if kind == "function_call_output":
            output = item.get("output", "")
            messages.append({"role": "tool", "tool_call_id": str(item.get("call_id", "")),
                             "content": output if isinstance(output, str) else json.dumps(output)})
        elif kind == "message":
            role = item.get("role", "user")
            messages.append({"role": "assistant" if role == "assistant" else "user", "content": _text_of(item.get("content"))})
        elif kind == "reasoning":
            continue  # Provider-private reasoning items are never replayed.
        else:
            raise ValueError(f"Unsupported input item for the chat adapter: {kind or 'unknown'}")
    flush_calls()
    return messages


def to_chat_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    converted = []
    for tool in tools:
        if tool.get("type") != "function" or not isinstance(tool.get("name"), str):
            raise ValueError("The chat adapter supports function tools only")
        converted.append({"type": "function", "function": {"name": tool["name"], "description": tool.get("description", ""),
                                                            "parameters": tool.get("parameters") or {"type": "object", "properties": {}}}})
    return converted


def from_chat_completion(completion: Any) -> AdaptedResponse:
    """Build Responses-style output items from the first chat choice."""
    choice = completion.choices[0]
    message = choice.message
    text = message.content or ""
    output: list[dict[str, Any]] = []
    if text:
        output.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
    for call in message.tool_calls or []:
        raw = call.model_dump(exclude_none=True) if hasattr(call, "model_dump") else {}
        output.append({"type": "function_call", "call_id": call.id or uuid.uuid4().hex, "name": call.function.name,
                       "arguments": call.function.arguments or "{}", "provider_tool_call": raw})
    usage = getattr(completion, "usage", None)
    status = "incomplete" if getattr(choice, "finish_reason", None) == "length" else "completed"
    return AdaptedResponse(id=getattr(completion, "id", "") or uuid.uuid4().hex, model=getattr(completion, "model", None),
                           output=output, output_text=text, status=status,
                           usage=AdaptedUsage(getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None),
                                              getattr(usage, "total_tokens", None)))


class _Responses:
    def __init__(self, client: Any, reasoning_param: bool):
        self._client, self._reasoning_param = client, reasoning_param

    def create(self, *, model: str, input: list[dict[str, Any]] | str, instructions: str | None = None,
               tools: list[dict[str, Any]] | None = None, tool_choice: str | None = None,
               max_output_tokens: int | None = None, reasoning: dict[str, Any] | None = None,
               timeout: float | None = None, **unsupported: Any) -> AdaptedResponse:
        if unsupported:
            raise ValueError("Unsupported Responses parameters for the chat adapter: " + ", ".join(sorted(unsupported)))
        kwargs: dict[str, Any] = {"model": model, "messages": to_chat_messages(instructions, input)}
        chat_tools = to_chat_tools(tools)
        if chat_tools:
            kwargs["tools"] = chat_tools
            kwargs["tool_choice"] = tool_choice or "auto"
        if max_output_tokens:
            # Chat-only thinking models (Gemini 3.x) count reasoning tokens against max_tokens, so the visible
            # answer needs headroom or it is cut off and reported as incomplete.
            import os
            headroom = int(os.environ.get("CHAT_ADAPTER_REASONING_HEADROOM", "4096"))
            kwargs["max_tokens"] = int(max_output_tokens) + headroom
        if reasoning and reasoning.get("effort") and self._reasoning_param:
            kwargs["reasoning_effort"] = reasoning["effort"]
        if timeout is not None:
            kwargs["timeout"] = timeout
        return from_chat_completion(self._client.chat.completions.create(**kwargs))


class ChatCompletionsClient:
    """Duck-types `OpenAI().responses.create` for the specialist runner."""

    def __init__(self, client: Any, *, reasoning_param: bool = False):
        self.responses = _Responses(client, reasoning_param)
