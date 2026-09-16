from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from travel_agent.providers.opening_hours import parse_hours
from travel_agent.providers.services import parse_cost
from travel_agent.schemas import Coordinate, TripRequest

WHEN = datetime.fromisoformat("2026-09-16T10:00:00+02:00")


@pytest.mark.parametrize("raw", ["24/7", "10:00-18:00", "Mo-Fr 09:00-17:00", "Mo,We,Fr 09:00-12:00,13:00-18:00",
                                  "Mo-Fr 09:00-18:00; Sa-Su off", "We 22:00-02:00", "off"])
def test_supported_opening_grammar(raw):
    result = parse_hours(raw, WHEN, "Europe/Berlin")
    assert result is not None


@pytest.mark.parametrize("raw", [None, "", "PH off", "sunrise-sunset", '10:00-18:00 "by appointment"',
                                  "Jan-Mar 09:00-17:00", "25:00-26:00", "10:99-18:00", "Mo-Fr 09:00-17:00; We off"])
def test_unsupported_or_ambiguous_hours_are_unknown(raw):
    assert parse_hours(raw, WHEN, "Europe/Berlin") is None


def test_overnight_interval_crosses_midnight():
    result = parse_hours("We 22:00-02:00", WHEN, "Europe/Berlin")
    assert result[0].end.date() > result[0].start.date()
    assert result[0].end - result[0].start == timedelta(hours=4)


@pytest.mark.parametrize("kwargs", [
    {"budget_minor": -1}, {"max_walking_m": -1}, {"min_stops": 6, "target_stops": 3},
    {"start": "2026-09-16T10:00:00"}, {"transport_mode": "flying"}, {"max_candidates": 100},
    {"rain_threshold_pct": 101}, {"end": "2026-09-15T12:00:00Z"},
    {"required_place_ids": ["a"], "excluded_place_ids": ["a"]}])
def test_request_rejects_invalid_constraints(kwargs):
    with pytest.raises(ValidationError):
        TripRequest(**kwargs)


@pytest.mark.parametrize("kwargs", [{"lat": float('nan'), "lon": 0}, {"lat": 90, "lon": 0}, {"lat": 0, "lon": 200}])
def test_coordinates_are_finite_and_bounded(kwargs):
    with pytest.raises(ValidationError):
        Coordinate(**kwargs)


@pytest.mark.parametrize("tags,expected", [
    ({"fee": "yes"}, (None, "unknown")), ({"fee": "no"}, (0, "estimated")),
    ({"charge": "12.50 EUR"}, (1250, "estimated")), ({"charge": "12.50 USD"}, (None, "unknown")),
    ({"charge": "from 5 EUR"}, (None, "unknown")), ({}, (None, "unknown"))])
def test_osm_price_interpretation(tags, expected):
    assert parse_cost(tags, "EUR") == expected
