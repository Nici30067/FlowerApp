"""The Chat Completions adapter exposes the Responses subset the specialists use."""
import json
from types import SimpleNamespace

import pytest

from travel_agent.agents import ExecutionBudget, ModelRunner, ToolDispatcher, openai_client, output_items
from travel_agent.model_adapter import (
    ChatCompletionsClient,
    from_chat_completion,
    to_chat_messages,
    to_chat_tools,
)


def completion(content=None, tool_calls=(), finish="stop", model="gemini-3.8-flash"):
    calls = [SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=args)) for cid, name, args in tool_calls]
    return SimpleNamespace(id="chatcmpl-1", model=model, choices=[SimpleNamespace(finish_reason=finish,
                           message=SimpleNamespace(content=content, tool_calls=calls or None))],
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))


class FakeChat:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


def test_messages_preserve_tool_call_pairing():
    items = [{"role": "user", "content": "context"},
             {"type": "function_call", "call_id": "c1", "name": "search_places", "arguments": "{}",
              "provider_tool_call": {"id": "c1", "type": "function", "function": {"name": "search_places", "arguments": "{}"},
                                     "extra_content": {"google": {"thought_signature": "sig"}}}},
             {"type": "function_call_output", "call_id": "c1", "output": "[]"},
             {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "draft"}]},
             {"type": "reasoning", "summary": []}]
    messages = to_chat_messages("be brief", items)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "assistant"]
    assert messages[2]["tool_calls"][0]["id"] == "c1" and messages[3]["tool_call_id"] == "c1"
    assert messages[2]["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "sig"}}
    assert messages[4]["content"] == "draft"
    with pytest.raises(ValueError):
        to_chat_messages(None, [{"type": "image_generation_call"}])


def test_tools_convert_to_chat_shape_and_reject_non_functions():
    tools = to_chat_tools([{"type": "function", "name": "f", "description": "d", "parameters": {"type": "object", "properties": {}}}])
    assert tools == [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
    assert to_chat_tools(None) is None
    with pytest.raises(ValueError):
        to_chat_tools([{"type": "web_search"}])


def test_completion_becomes_responses_items():
    response = from_chat_completion(completion("hello", [("c9", "get_place_details", '{"place_id": "x"}')]))
    assert response.output_text == "hello" and response.status == "completed" and response.usage.total_tokens == 15
    assert [i["type"] for i in response.output] == ["message", "function_call"]
    assert response.output[1]["call_id"] == "c9" and output_items(response) == response.output
    assert from_chat_completion(completion("cut", finish="length")).status == "incomplete"


def test_runner_completes_a_tool_turn_through_the_adapter(baseline):
    pid = baseline.places[0].id
    report = json.dumps({"role": "discovery", "summary": "ok", "candidate_ids": [pid], "avoid_ids": [], "evidence_ids": [], "requests": []})
    fake = FakeChat([completion(None, [("call_1", "search_places", "{}")], finish="tool_calls"), completion(report)])
    runner = ModelRunner(ChatCompletionsClient(fake), "gemini-3.8-flash", lambda t, d: None, ExecutionBudget(), 1, "")
    result = runner.run("discovery", baseline, ToolDispatcher(baseline))
    assert result.candidate_ids == [pid]
    first, second = fake.calls
    assert first["tools"][0]["function"]["name"] == "search_places" and first["tool_choice"] == "auto" and first["max_tokens"] >= 1500
    assert "tools" not in second and "reasoning_effort" not in second
    roles = [m["role"] for m in second["messages"]]
    assert roles == ["system", "user", "assistant", "tool"] and second["messages"][3]["tool_call_id"] == "call_1"


def test_openai_client_selects_the_adapter():
    client = openai_client("https://example.invalid/v1/", "key", 30, "chat")
    assert isinstance(client, ChatCompletionsClient)
    with pytest.raises(ValueError):
        openai_client("https://example.invalid/v1/", "key", 30, "grpc")
