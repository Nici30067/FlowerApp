import json
from types import SimpleNamespace

import pytest

from travel_agent.agents import ExecutionBudget, ModelRunner, ToolDispatcher


class Item:
    def __init__(self, data): self.data = data
    def to_dict(self): return self.data


class FakeResponses:
    def __init__(self, responses): self.items = list(responses); self.calls = []
    def create(self, **kwargs): self.calls.append(kwargs); return self.items.pop(0)


def report(role, ids):
    return json.dumps({"role": role, "summary": "Mock protocol response for testing", "candidate_ids": ids,
                       "avoid_ids": [], "evidence_ids": [], "requests": []})


def test_model_loop_preserves_tool_call_ids_and_disables_final_tools(baseline):
    pid = baseline.places[0].id
    api = FakeResponses([
        SimpleNamespace(output=[Item({"type": "function_call", "name": "search_places", "arguments": "{}", "call_id": "c1"})]),
        SimpleNamespace(output_text=report("discovery", [pid]))])
    budget = ExecutionBudget(); events = []
    runner = ModelRunner(SimpleNamespace(responses=api), "test-model", lambda t, d: events.append((t, d)), budget, 1)
    result = runner.run("discovery", baseline, ToolDispatcher(baseline))
    assert result.candidate_ids == [pid]
    assert budget.model_calls == 2 and budget.tool_calls == 1
    assert "tools" not in api.calls[-1]
    assert any(x.get("call_id") == "c1" and x.get("type") == "function_call_output" for x in api.calls[-1]["input"])


def test_disallowed_tool_returns_error_to_model(baseline):
    api = FakeResponses([
        SimpleNamespace(output=[Item({"type": "function_call", "name": "execute_shell", "arguments": "{}", "call_id": "blocked"})]),
        SimpleNamespace(output_text=report("discovery", []))])
    runner = ModelRunner(SimpleNamespace(responses=api), "test-model", lambda t, d: None, ExecutionBudget(), 1)
    runner.run("discovery", baseline, ToolDispatcher(baseline))
    output = api.calls[-1]["input"][-1]
    assert output["call_id"] == "blocked" and "error" in json.loads(output["output"])


def test_unknown_model_place_id_is_rejected(baseline):
    api = FakeResponses([SimpleNamespace(output_text=report("discovery", ["fabricated-place"]))])
    runner = ModelRunner(SimpleNamespace(responses=api), "test-model", lambda t, d: None, ExecutionBudget(), 0)
    with pytest.raises(ValueError, match="unknown place"):
        runner.run("discovery", baseline, ToolDispatcher(baseline))


@pytest.mark.parametrize("role,name", [("conditions", "search_places"), ("mobility", "assess_costs"), ("budget_pace", "get_weather_forecast")])
def test_specialist_allowlists_are_distinct(baseline, role, name):
    with pytest.raises(ValueError):
        ToolDispatcher(baseline).execute({"name": name, "arguments": "{}", "call_id": "x"}, role)


def test_argument_schema_rejects_extra_fields(baseline):
    with pytest.raises(ValueError):
        ToolDispatcher(baseline).execute({"name": "search_places", "arguments": '{"query":"override"}', "call_id": "x"}, "discovery")


def test_tool_budget_is_finite():
    budget = ExecutionBudget(max_tool_calls=1)
    budget.consume("tool")
    with pytest.raises(RuntimeError): budget.consume("tool")


def test_model_budget_is_finite():
    budget = ExecutionBudget(max_model_calls=1)
    budget.consume("model")
    with pytest.raises(RuntimeError): budget.consume("model")


def test_global_time_budget_is_finite():
    budget = ExecutionBudget(wall_time_s=-1)
    with pytest.raises(RuntimeError): budget.consume("model")
