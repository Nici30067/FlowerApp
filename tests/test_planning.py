from datetime import timedelta

import pytest

from travel_agent.coordinator import apply_event
from travel_agent.planning.engine import points_for, search, validate, weather_covered
from travel_agent.providers.services import FixtureProvider
from travel_agent.schemas import TripEvent, TripRequest, TripSnapshot


def test_baseline_has_valid_timing_and_zero_model_calls(make_plan):
    proposal, events, budget = make_plan()
    state = proposal.proposed
    assert state.itinerary.validation.valid
    assert state.itinerary.validation.status == "provisional"
    assert len(state.itinerary.stops) == 5
    assert state.itinerary.end_arrival <= state.request.end
    assert state.itinerary.walking_m <= state.request.max_walking_m
    assert budget.model_calls == 0
    assert len([e for e in events if e["type"] == "agent.completed"]) == 4
    assert "SYNTHETIC_ROUTES" in {x.code for x in state.itinerary.validation.issues}


def test_rain_collaboration_changes_itinerary(baseline, make_plan):
    proposal, events, budget = make_plan(baseline, TripEvent(id="rain", kind="rain", simulated=True))
    assert proposal.removed and proposal.added
    assert proposal.proposed.itinerary.validation.valid
    catalog = {p.id: p for p in proposal.proposed.places}
    assert all(catalog[s.place_id].indoor for s in proposal.proposed.itinerary.stops)
    # Rain is known before the specialists speak, so Conditions reports first and Discovery consumes its
    # request in one pass instead of being re-run afterwards.
    roles = [e["data"]["role"] for e in events if e["type"] == "agent.completed"]
    assert roles == ["conditions", "discovery", "mobility", "budget_pace"]
    assert any(e["type"] == "collaboration.message" and e["data"]["recipient"] == "discovery" for e in events)
    assert baseline.weather_override is None


def test_rain_simulation_must_be_explicit(baseline):
    with pytest.raises(ValueError):
        apply_event(baseline, TripEvent(id="x", kind="rain"))


def test_walking_limit_replans(baseline, make_plan):
    proposal, _, _ = make_plan(baseline, TripEvent(id="less", kind="pace_changed", payload={"max_walking_m": 1100}))
    assert proposal.proposed.itinerary.validation.valid
    assert proposal.proposed.itinerary.walking_m <= 1100
    assert proposal.proposed.request.max_walking_m == 1100


def test_impossible_walk_limit_is_reported(baseline, make_plan):
    p, _, _ = make_plan(baseline, TripEvent(id="zero", kind="pace_changed", payload={"max_walking_m": 0}))
    assert not p.proposed.itinerary.validation.valid
    assert p.proposed.itinerary.validation.issues[0].code == "NO_FEASIBLE_PLAN"


def test_locked_reservation_is_preserved(baseline, make_plan):
    pid = baseline.itinerary.stops[2].place_id  # any planned stop; the policy decides which places are planned
    locked, _, _ = make_plan(baseline, TripEvent(id="lock", kind="lock_stop", payload={"place_id": pid}))
    before = next(s for s in baseline.itinerary.stops if s.place_id == pid)
    after = next(s for s in locked.proposed.itinerary.stops if s.place_id == pid)
    assert before.start == after.start and before.end == after.end
    assert after.locked


def test_closed_locked_place_is_not_silently_replaced(baseline, make_plan):
    pid = baseline.itinerary.stops[2].place_id
    locked = make_plan(baseline, TripEvent(id="lock", kind="lock_stop", payload={"place_id": pid}))[0].proposed
    closed = make_plan(locked, TripEvent(id="closed", kind="place_unavailable", payload={"place_id": pid}))[0]
    assert not closed.proposed.itinerary.validation.valid
    assert any(r.place_id == pid for r in closed.proposed.request.reservations)


def test_completed_stop_preserves_actual_cost_and_time(baseline, make_plan):
    stop = baseline.itinerary.stops[0]
    moment = stop.end + timedelta(minutes=10)
    event = TripEvent(id="completed", kind="stop_completed", payload={"place_id": stop.place_id,
        "actual_spent_minor": 730, "completed_at": moment.isoformat()})
    proposal, _, _ = make_plan(baseline, event)
    result = proposal.proposed
    assert result.itinerary.validation.valid
    assert result.itinerary.stops[0].completed
    assert result.itinerary.stops[0].end == moment
    assert result.progress.spent_minor == 730
    expected = 730 + sum(s.cost_minor or 0 for s in result.itinerary.stops if not s.completed)
    assert result.itinerary.cost_minor == expected
    assert all(s.arrival >= moment for s in result.itinerary.stops if not s.completed)


def test_cannot_complete_out_of_order(baseline):
    with pytest.raises(ValueError):
        apply_event(baseline, TripEvent(id="bad", kind="stop_completed", payload={
            "place_id": baseline.itinerary.stops[1].place_id, "actual_spent_minor": 0,
            "completed_at": baseline.itinerary.stops[1].end.isoformat()}))


def test_unknown_price_is_not_treated_as_verified_free(baseline):
    state = baseline.model_copy(deep=True)
    pid = state.itinerary.stops[0].place_id
    place = next(p for p in state.places if p.id == pid)
    place.cost_minor = None; place.cost_status = "unknown"
    state.itinerary.stops[0].cost_minor = None; state.itinerary.stops[0].cost_status = "unknown"
    checked = validate(state, state.itinerary)
    assert checked.status == "provisional"
    assert "PRICE_UNKNOWN" in {x.code for x in checked.issues}


def test_unknown_hours_remain_explicit(baseline):
    state = baseline.model_copy(deep=True)
    next(p for p in state.places if p.id == state.itinerary.stops[0].place_id).opening_intervals = None
    assert "HOURS_UNKNOWN" in {x.code for x in validate(state, state.itinerary).issues}


def test_verified_facts_mode_rejects_fixtures(baseline):
    state = baseline.model_copy(deep=True)
    state.request.require_verified_facts = True
    assert not validate(state, state.itinerary).valid


@pytest.mark.parametrize("field,value,code", [
    ("walking_m", 1, "TOTALS_MISMATCH"), ("cost_minor", 999999, "TOTALS_MISMATCH"),
    ("travel_duration_s", 0, "TOTALS_MISMATCH")])
def test_independent_totals_recomputed(baseline, field, value, code):
    itinerary = baseline.itinerary.model_copy(deep=True)
    setattr(itinerary, field, value)
    assert code in {x.code for x in validate(baseline, itinerary).issues}


def test_full_visit_must_fit_opening_interval(baseline):
    state = baseline.model_copy(deep=True)
    stop = state.itinerary.stops[0]
    place = next(p for p in state.places if p.id == stop.place_id)
    from travel_agent.schemas import Interval
    place.opening_intervals = [Interval(start=stop.start, end=stop.end - timedelta(minutes=1))]
    assert "CLOSED" in {x.code for x in validate(state, state.itinerary).issues}


def test_missing_route_cannot_be_replaced_by_direct_distance(baseline):
    result = search(baseline, {}, [p.id for p in baseline.places])
    assert not result.validation.valid


def test_required_unknown_place_is_infeasible(baseline):
    state = baseline.model_copy(deep=True); state.request.required_place_ids = ["missing"]
    matrix = FixtureProvider().matrix(points_for(state), "walking")
    result = search(state, matrix, [p.id for p in state.places])
    assert not result.validation.valid


def test_budget_reduction_is_respected(baseline, make_plan):
    result = make_plan(baseline, TripEvent(id="budget", kind="budget_changed", payload={"budget_minor": 0}))[0]
    assert result.proposed.itinerary.validation.valid
    assert result.proposed.itinerary.cost_minor == 0


def test_unrecognized_preference_is_rejected(baseline):
    with pytest.raises(ValueError):
        apply_event(baseline, TripEvent(id="x", kind="preferences_changed", payload={"ignore_validation": True}))


def test_progress_cannot_move_backwards(baseline):
    with pytest.raises(ValueError):
        apply_event(baseline, TripEvent(id="late", kind="user_running_late", payload={
            "now": (baseline.request.start - timedelta(minutes=1)).isoformat(),
            "location": baseline.request.origin.model_dump()}))


def test_weather_refresh_clears_simulation(baseline):
    wet = apply_event(baseline, TripEvent(id="r", kind="rain", simulated=True))
    refreshed = apply_event(wet, TripEvent(id="w", kind="weather_updated"))
    assert refreshed.weather_override is None


def test_forecast_coverage_gap_is_unknown(baseline):
    weather = baseline.weather.model_copy(deep=True)
    weather.intervals.pop(1)
    assert not weather_covered(weather, baseline.request.start, baseline.request.end)


def test_cycling_profile_is_separate_from_walking(make_plan):
    request = TripRequest(transport_mode="cycling")
    p, _, _ = make_plan(TripSnapshot(id="bike", request=request))
    assert p.proposed.itinerary.validation.valid
    assert p.proposed.itinerary.walking_m == 0
    assert all(l.mode == "cycling" for l in p.proposed.itinerary.legs)
