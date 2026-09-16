import pytest

from travel_agent.agents import ExecutionBudget, RulesRunner
from travel_agent.coordinator import Coordinator
from travel_agent.providers.services import FixtureProvider
from travel_agent.schemas import TripRequest, TripSnapshot


@pytest.fixture
def make_plan():
    def execute(snapshot=None, event=None):
        events = []
        emit = lambda typ, data: events.append({"type": typ, "data": data})
        budget = ExecutionBudget()
        coordinator = Coordinator(FixtureProvider(), RulesRunner(emit, budget), emit, budget)
        proposal = coordinator.plan(snapshot or TripSnapshot(id="test-trip", request=TripRequest()), event)
        return proposal, events, budget
    return execute


@pytest.fixture
def baseline(make_plan):
    return make_plan()[0].proposed
