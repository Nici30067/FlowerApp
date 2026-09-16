import json
from types import SimpleNamespace

import pytest

from travel_agent.agents import (
    ExecutionBudget,
    ModelRunner,
    ToolDispatcher,
    extract_json_object,
    snapshot_context,
)
from travel_agent.providers.services import FixtureProvider


class Item:
    def __init__(self, data): self.data = data
    def to_dict(self): return self.data


class FakeResponses:
    def __init__(self, responses): self.items = list(responses); self.calls = []
    def create(self, **kwargs): self.calls.append(kwargs); return self.items.pop(0)


def report(role, ids, **extra):
    return json.dumps({"role": role, "summary": "Mock protocol response for testing", "candidate_ids": ids,
                       "avoid_ids": [], "evidence_ids": [], "requests": [], **extra})


def answer(text, **extra):
    return SimpleNamespace(output=[], output_text=text, **extra)


def runner_for(api, events=None, budget=None, turns=0):
    emit = (lambda t, d: events.append((t, d))) if events is not None else (lambda t, d: None)
    return ModelRunner(SimpleNamespace(responses=api), "test-model", emit, budget or ExecutionBudget(), turns)


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
    pid = baseline.places[0].id
    api = FakeResponses([
        SimpleNamespace(output=[Item({"type": "function_call", "name": "execute_shell", "arguments": "{}", "call_id": "blocked"})]),
        SimpleNamespace(output_text=report("discovery", [pid]))])
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


def test_consume_returns_the_new_count_per_kind():
    budget = ExecutionBudget()
    assert budget.consume("model") == 1 and budget.consume("model") == 2
    assert budget.consume("tool") == 1
    assert budget.consume("provider") == 1
    with pytest.raises(ValueError):
        budget.consume("network")


def test_provider_calls_are_counted_separately_from_tools():
    budget = ExecutionBudget(max_provider_calls=1)
    budget.consume("provider")
    budget.consume("tool")
    with pytest.raises(RuntimeError, match="Provider call budget"):
        budget.consume("provider")
    metrics = budget.metrics()
    assert metrics["provider_calls"] == 1 and metrics["tool_calls"] == 1 and metrics["model_calls"] == 0
    assert set(metrics) == {"model_calls", "tool_calls", "provider_calls", "elapsed_s"}


def test_last_report_wins_over_an_echoed_previous_report(baseline):
    first, second = baseline.places[0].id, baseline.places[1].id
    text = ("Previous report for reference:\n" + report("discovery", [first]) +
            "\nUpdated report:\n" + report("discovery", [second, first]))
    api = FakeResponses([answer(text)])
    result = runner_for(api).run("discovery", baseline, ToolDispatcher(baseline))
    assert result.candidate_ids == [second, first]
    assert extract_json_object(text)["candidate_ids"] == [second, first]


def test_echoed_report_from_another_role_is_ignored(baseline):
    pid = baseline.places[0].id
    text = report("discovery", [pid]) + "\n" + report("conditions", [])
    api = FakeResponses([answer(text)])
    result = runner_for(api).run("discovery", baseline, ToolDispatcher(baseline))
    assert result.role == "discovery" and result.candidate_ids == [pid]


def test_contract_example_is_never_accepted_as_a_report(baseline):
    pid = baseline.places[0].id
    example = {"role": "conditions", "summary": "Concise findings", "candidate_ids": [], "avoid_ids": [],
               "evidence_ids": [], "requests": []}
    real = report("conditions", [], avoid_ids=[pid])
    events = []
    api = FakeResponses([answer("```json\n" + json.dumps(example) + "\n```\n" + real)])
    result = runner_for(api, events).run("conditions", baseline, ToolDispatcher(baseline))
    assert result.avoid_ids == [pid] and not any(t == "model.repair" for t, _ in events)
    # The example alone is not a report: the bounded repair turn runs.
    events.clear()
    api = FakeResponses([answer(json.dumps(example)), answer(real)])
    result = runner_for(api, events).run("conditions", baseline, ToolDispatcher(baseline))
    assert result.avoid_ids == [pid] and sum(t == "model.repair" for t, _ in events) == 1
    assert api.calls[-1]["input"][-1]["content"].startswith(
        "Return only the JSON report object with exactly these keys: role, summary, candidate_ids, avoid_ids, "
        "evidence_ids, requests.")


def test_extra_keys_and_missing_sender_are_tolerated(baseline):
    pid = baseline.places[0].id
    text = json.dumps({"role": "conditions", "summary": "Rain expected", "candidate_ids": [], "avoid_ids": [pid],
                       "evidence_ids": [], "confidence": 0.9, "notes": ["ignored"],
                       "requests": [{"recipient": "discovery", "kind": "request", "summary": "Indoor options please",
                                     "place_ids": [pid], "priority": "high"}]})
    api = FakeResponses([answer(text)])
    result = runner_for(api).run("conditions", baseline, ToolDispatcher(baseline))
    assert result.avoid_ids == [pid] and len(result.requests) == 1
    assert result.requests[0].sender == "conditions" and result.requests[0].place_ids == [pid]
    assert "confidence" not in result.model_dump()


def test_degenerate_discovery_ranking_triggers_the_repair_turn(baseline):
    pid = baseline.places[0].id
    events = []
    api = FakeResponses([answer(report("discovery", [])), answer(report("discovery", [pid]))])
    result = runner_for(api, events).run("discovery", baseline, ToolDispatcher(baseline))
    assert result.candidate_ids == [pid] and sum(t == "model.repair" for t, _ in events) == 1
    api = FakeResponses([answer(report("mobility", [])), answer(report("mobility", []))])
    with pytest.raises(ValueError, match="valid specialist report"):
        runner_for(api).run("mobility", baseline, ToolDispatcher(baseline))


def test_truncated_final_answer_is_repaired_with_a_doubled_output_limit(baseline):
    pid = baseline.places[0].id
    events = []
    api = FakeResponses([answer('{"role": "discovery", "summary": "cut off mid', status="incomplete"),
                         answer(report("discovery", [pid]), status="completed")])
    result = runner_for(api, events).run("discovery", baseline, ToolDispatcher(baseline))
    assert result.candidate_ids == [pid]
    assert [c["max_output_tokens"] for c in api.calls] == [2000, 4000]
    incomplete = [d for t, d in events if t == "model.incomplete"]
    assert incomplete == [{"role": "discovery", "call_number": 1, "max_output_tokens": 2000}]
    repair = next(d for t, d in events if t == "model.repair")
    assert "truncated at 2000" in repair["reason"]


def test_truncated_repair_raises_an_actionable_error(baseline):
    api = FakeResponses([answer("{", status="incomplete"), answer("{", status="incomplete")])
    with pytest.raises(ValueError, match=r"truncated at 4000 output tokens; raise agent.max-output-tokens"):
        runner_for(api).run("discovery", baseline, ToolDispatcher(baseline))
    assert len(api.calls) == 2


def test_truncated_tool_turn_answer_doubles_the_contract_call_limit(baseline):
    pid = baseline.places[0].id
    events = []
    api = FakeResponses([answer('{"role": "discovery", "summary": "long analysis', status="incomplete"),
                         answer("still cut", status="incomplete"),
                         answer(report("discovery", [pid]))])
    result = runner_for(api, events, turns=1).run("discovery", baseline, ToolDispatcher(baseline))
    assert result.candidate_ids == [pid]
    assert [c["max_output_tokens"] for c in api.calls] == [2000, 4000, 8000]
    assert [d["max_output_tokens"] for t, d in events if t == "model.incomplete"] == [2000, 4000]
    assert "truncated at 4000" in next(d for t, d in events if t == "model.repair")["reason"]


def test_custom_output_limit_is_used_and_validated(baseline):
    pid = baseline.places[0].id
    api = FakeResponses([answer(report("discovery", [pid]))])
    runner = ModelRunner(SimpleNamespace(responses=api), "test-model", lambda t, d: None, ExecutionBudget(), 0,
                         "low", max_output_tokens=3000)
    runner.run("discovery", baseline, ToolDispatcher(baseline))
    assert api.calls[0]["max_output_tokens"] == 3000 and api.calls[0]["reasoning"] == {"effort": "low"}
    with pytest.raises(ValueError):
        ModelRunner(SimpleNamespace(responses=api), "test-model", lambda t, d: None, ExecutionBudget(), 0, "low", 10)


def test_usage_and_call_number_are_reported_on_model_completed(baseline):
    pid = baseline.places[0].id
    usage = SimpleNamespace(input_tokens=1200, output_tokens=80, total_tokens=1280,
                            output_tokens_details=SimpleNamespace(reasoning_tokens=32))
    events = []
    budget = ExecutionBudget()
    budget.consume("model")  # a previous specialist already used one call
    api = FakeResponses([answer(report("discovery", [pid]), usage=usage, model="served-model")])
    runner_for(api, events, budget).run("discovery", baseline, ToolDispatcher(baseline))
    completed = [d for t, d in events if t == "model.completed"]
    assert len(completed) == 1
    assert completed[0]["usage"] == {"input_tokens": 1200, "output_tokens": 80, "reasoning_tokens": 32}
    assert completed[0]["call_number"] == 2 and completed[0]["returned_model"] == "served-model"


def test_missing_usage_is_reported_as_null(baseline):
    pid = baseline.places[0].id
    events = []
    api = FakeResponses([answer(report("discovery", [pid]))])
    runner_for(api, events).run("discovery", baseline, ToolDispatcher(baseline))
    completed = next(d for t, d in events if t == "model.completed")
    assert completed["usage"] is None and completed["call_number"] == 1


def test_unknown_evidence_ids_are_pruned_not_fatal(baseline):
    place = baseline.places[0]
    weather_id = baseline.weather.source.id
    text = json.dumps({"role": "conditions", "summary": "Rain likely", "candidate_ids": [], "avoid_ids": [place.id],
                       "evidence_ids": [weather_id, "made-up-forecast", place.source.id],
                       "requests": [{"sender": "conditions", "recipient": "discovery", "kind": "request",
                                     "summary": "Indoor alternatives", "place_ids": [place.id],
                                     "evidence_ids": ["made-up-forecast", "another-fake", weather_id]}]})
    events = []
    api = FakeResponses([answer(text)])
    result = runner_for(api, events).run("conditions", baseline, ToolDispatcher(baseline))
    assert result.evidence_ids == [weather_id, place.source.id]
    assert result.requests[0].evidence_ids == [weather_id]
    completed = next(d for t, d in events if t == "agent.completed")
    assert completed["unknown_evidence_ids"] == ["made-up-forecast", "another-fake"]
    assert completed["report"]["evidence_ids"] == [weather_id, place.source.id]


def test_matrix_evidence_ids_are_known_to_mobility(baseline):
    provider = FixtureProvider()
    points = {"origin": baseline.request.origin, "destination": baseline.request.destination,
              **{p.id: p.coordinate for p in baseline.places}}
    matrix = provider.matrix(points, baseline.request.transport_mode)
    evidence_id = next(iter(matrix.values())).evidence_id
    pid = baseline.places[0].id
    api = FakeResponses([answer(report("mobility", [pid], evidence_ids=[evidence_id, "ghost"]))])
    events = []
    result = runner_for(api, events).run("mobility", baseline, ToolDispatcher(baseline, matrix))
    assert result.evidence_ids == [evidence_id]
    assert next(d for t, d in events if t == "agent.completed")["unknown_evidence_ids"] == ["ghost"]
    context = json.loads(api.calls[0]["input"][0]["content"])
    assert context["route_matrix"]["evidence_ids"] == [evidence_id]


def test_context_exposes_place_source_ids(baseline):
    context = snapshot_context(baseline)
    place = baseline.places[0]
    entry = next(p for p in context["places"] if p["id"] == place.id)
    assert entry["source_id"] == place.source.id and entry["source_status"] == place.source.status
    assert "source" not in entry and context["route_matrix"] is None


def test_valid_report_in_a_tool_turn_skips_the_contract_call(baseline):
    pid = baseline.places[0].id
    api = FakeResponses([
        SimpleNamespace(output=[Item({"type": "function_call", "name": "search_places", "arguments": "{}", "call_id": "c1"})]),
        answer(report("discovery", [pid]))])
    budget = ExecutionBudget(); events = []
    result = runner_for(api, events, budget, turns=3).run("discovery", baseline, ToolDispatcher(baseline))
    assert result.candidate_ids == [pid]
    assert budget.model_calls == 2 and len(api.calls) == 2 and "tools" in api.calls[-1]
    assert not any(t == "model.repair" for t, _ in events)


def test_invalid_text_in_a_tool_turn_still_gets_the_contract_call(baseline):
    pid = baseline.places[0].id
    api = FakeResponses([answer("I think the candidates look fine."), answer(report("discovery", [pid]))])
    budget = ExecutionBudget(); events = []
    result = runner_for(api, events, budget, turns=2).run("discovery", baseline, ToolDispatcher(baseline))
    assert result.candidate_ids == [pid] and budget.model_calls == 2
    assert "tools" not in api.calls[-1] and "Finalize now without tools" in api.calls[-1]["instructions"]
    assert not any(t == "model.repair" for t, _ in events)


def test_tool_turn_message_items_are_read_when_output_text_is_absent(baseline):
    pid = baseline.places[0].id
    message = {"type": "message", "role": "assistant",
               "content": [{"type": "output_text", "text": report("discovery", [pid])}]}
    api = FakeResponses([SimpleNamespace(output=[Item(message)])])
    result = runner_for(api, turns=1).run("discovery", baseline, ToolDispatcher(baseline))
    assert result.candidate_ids == [pid] and len(api.calls) == 1


def test_contract_call_never_ends_with_an_assistant_turn(baseline):
    """Gemini rejects requests whose last input item is a model turn; the contract call must follow a user message."""
    pid = baseline.places[0].id
    api = FakeResponses([
        SimpleNamespace(output=[Item({"type": "message", "role": "assistant",
                                      "content": [{"type": "output_text", "text": "Let me think about the candidates."}]})],
                        output_text="Let me think about the candidates."),
        SimpleNamespace(output=[], output_text=report("discovery", [pid]))])
    runner = ModelRunner(SimpleNamespace(responses=api), "test-model", lambda t, d: None, ExecutionBudget(), 1)
    result = runner.run("discovery", baseline, ToolDispatcher(baseline))
    assert result.candidate_ids == [pid]
    last_input = api.calls[-1]["input"][-1]
    assert last_input.get("role") == "user" and "Finalize" in last_input["content"]
