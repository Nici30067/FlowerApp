from datetime import UTC, datetime

import pytest

from travel_agent.agents import ExecutionBudget, RulesRunner
from travel_agent.coordinator import Coordinator
from travel_agent.providers.services import FixtureProvider
from travel_agent.schemas import Coordinate, Evidence, GeoPlace, TripRequest, TripSnapshot


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


def geoplace(*, name, lat, lon, timezone, country="", admin1="", population=None, status="live", query=None):
    """A GeoPlace as the Geocoder builds it from one Open-Meteo record."""
    return GeoPlace(query=query or name, name=name, country=country, admin1=admin1,
                    coordinate=Coordinate(lat=lat, lon=lon), timezone=timezone, population=population,
                    source=Evidence(id="geocode-" + name.lower().replace(" ", "-"), provider="Open-Meteo geocoding",
                                    status=status, retrieved_at=datetime.now(UTC),
                                    reference="https://open-meteo.com/en/docs/geocoding-api"))


class FakeGeocoder:
    """Answers every query with one fixed result (or raises one error) and records the queries it received."""
    def __init__(self, result=None, error=None):
        self.result, self.error, self.queries = result, error, []

    def search(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def geo():
    """Builder for GeoPlace test values: geo(name=..., lat=..., lon=..., timezone=...)."""
    return geoplace


@pytest.fixture
def fake_geocoder():
    """The FakeGeocoder class: fake_geocoder(result) or fake_geocoder(error=ProviderError(...))."""
    return FakeGeocoder
