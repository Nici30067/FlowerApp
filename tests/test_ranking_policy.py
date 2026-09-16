"""The specialists' ranking, avoid list and repair requests are load-bearing for the planner."""
from travel_agent.agents import ExecutionBudget
from travel_agent.coordinator import Coordinator
from travel_agent.providers.services import FixtureProvider
from travel_agent.schemas import AgentMessage, AgentReport, TripEvent, TripRequest, TripSnapshot

CATALOG = [p.id for p in FixtureProvider().search(TripRequest())]
POOR = ["demo-alexanderplatz", "demo-cathedral", "demo-bebelplatz", "demo-coffee", "demo-bookshop"]
GOOD = ["demo-museum-walk", "demo-gallery", "demo-courtyards", "demo-monbijou", "demo-forum"]


class ScriptedRunner:
    """A model-mode stand-in whose specialists return scripted reports. Zero model calls, zero network."""
    mode = "model"

    def __init__(self, emit, budget, discovery=None, avoid=(), review=None, review_to="discovery",
                 review_avoid=()):
        self.emit, self.budget = emit, budget
        self.discovery, self.avoid, self.review, self.review_to = discovery, list(avoid), review, review_to
        self.review_avoid = list(review_avoid)
        self.seen_messages = {}

    def run(self, role, snapshot, tools):
        self.emit("agent.started", {"role": role, "engine": "model", "model": "scripted"})
        self.seen_messages[role] = [(m.sender, m.recipient) for m in snapshot.messages]
        ids, avoid, requests = [p.id for p in snapshot.places], [], []
        if role == "discovery" and self.discovery is not None:
            ids = list(self.discovery)
        elif role == "conditions":
            avoid = self.avoid
        elif role == "mobility":
            discovery = next((r for r in reversed(snapshot.reports) if r.role == "discovery"), None)
            ids = list(discovery.candidate_ids) if discovery else ids
        elif role == "budget_pace":
            ids, avoid = list(self.review or []), self.review_avoid
            if self.review is not None:
                requests.append(AgentMessage(sender="budget_pace", recipient=self.review_to, kind="request",
                                             summary="Consider these candidates instead."))
        report = AgentReport(role=role, summary="scripted", candidate_ids=ids, avoid_ids=avoid, requests=requests)
        self.emit("agent.completed", {"role": role, "engine": "model", "report": report.model_dump(mode="json")})
        return report


def scripted_plan(snapshot=None, event=None, **script):
    events = []
    emit = lambda kind, data: events.append({"type": kind, "data": data})
    budget = ExecutionBudget()
    runner = ScriptedRunner(emit, budget, **script)
    proposal = Coordinator(FixtureProvider(), runner, emit, budget).plan(
        snapshot or TripSnapshot(id="policy", request=TripRequest()), event)
    return proposal, events, runner


def stops(proposal):
    return [s.place_id for s in proposal.proposed.itinerary.stops]


def planner_events(events, kind):
    return [e["data"] for e in events if e["type"] == kind]


def test_different_specialist_rankings_produce_different_itineraries():
    forward, forward_events, _ = scripted_plan(discovery=CATALOG)
    reverse, reverse_events, _ = scripted_plan(discovery=list(reversed(CATALOG)))
    assert forward.proposed.itinerary.validation.valid and reverse.proposed.itinerary.validation.valid
    assert stops(forward) != stops(reverse)
    for events in (forward_events, reverse_events):
        started = planner_events(events, "planner.started")
        assert started == [{"algorithm": "bounded_beam_search", "beam_width": 64, "candidate_count": len(CATALOG),
                            "avoided_count": 0, "ranking_source": "specialists"}]
        assert not planner_events(events, "planner.fallback")


def test_avoided_place_is_excluded_unless_required():
    control, _, _ = scripted_plan(discovery=CATALOG)
    assert "demo-neues" in stops(control)
    avoided, events, _ = scripted_plan(discovery=CATALOG, avoid=["demo-neues"])
    assert avoided.proposed.itinerary.validation.valid
    assert "demo-neues" not in stops(avoided)
    assert planner_events(events, "planner.started")[0]["avoided_count"] == 1
    required = TripSnapshot(id="policy", request=TripRequest(required_place_ids=["demo-neues"]))
    kept, events, _ = scripted_plan(required, discovery=CATALOG, avoid=["demo-neues"])
    assert kept.proposed.itinerary.validation.valid
    assert "demo-neues" in stops(kept)
    assert planner_events(events, "planner.started")[0]["avoided_count"] == 0


def test_empty_ranking_falls_back_to_catalog_and_discloses_it():
    proposal, events, _ = scripted_plan(discovery=[])
    assert proposal.proposed.itinerary.validation.valid
    assert len(stops(proposal)) == proposal.proposed.request.target_stops
    assert planner_events(events, "planner.fallback") == [
        {"reason": "no specialist candidates", "candidate_count": len(CATALOG)}]
    started = planner_events(events, "planner.started")[0]
    assert started["ranking_source"] == "catalog" and started["candidate_count"] == len(CATALOG)
    types = [e["type"] for e in events]
    assert types.index("planner.fallback") < types.index("planner.started")


def test_infeasible_specialist_ranking_is_widened_to_the_catalog():
    proposal, events, _ = scripted_plan(discovery=["demo-coffee"])
    assert proposal.proposed.itinerary.validation.valid
    assert len(stops(proposal)) >= proposal.proposed.request.min_stops
    assert planner_events(events, "planner.started")[0]["candidate_count"] == 1
    assert planner_events(events, "planner.fallback") == [
        {"reason": "specialist ranking infeasible", "candidate_count": len(CATALOG)}]


def test_avoided_places_are_reconsidered_only_as_a_last_resort():
    avoid = [pid for pid in CATALOG if pid != "demo-coffee"]
    proposal, events, _ = scripted_plan(discovery=CATALOG, avoid=avoid)
    assert proposal.proposed.itinerary.validation.valid
    started = planner_events(events, "planner.started")[0]
    assert started["candidate_count"] == 1 and started["avoided_count"] == len(avoid)
    assert planner_events(events, "planner.fallback") == [
        {"reason": "avoided places reconsidered", "candidate_count": len(CATALOG)}]


def test_repair_adopts_a_better_alternative_and_never_an_identical_one():
    control, control_events, _ = scripted_plan(discovery=POOR)
    first = stops(control)
    assert set(first) == set(POOR) and not planner_events(control_events, "planner.repair")

    better, events, _ = scripted_plan(discovery=POOR, review=GOOD)
    repair = planner_events(events, "planner.repair")
    assert len(repair) == 1 and repair[0]["adopted"] and repair[0]["reason"] == "alternative adopted"
    assert repair[0]["alternative_score"] > repair[0]["proposal_score"]
    assert better.proposed.itinerary.validation.valid
    assert set(stops(better)) == set(GOOD)
    # Better by the planner's own neutral measure too, not only by the reviewer's rank bonus.
    assert better.proposed.itinerary.score > control.proposed.itinerary.score

    same, events, _ = scripted_plan(discovery=POOR, review=first)
    repair = planner_events(events, "planner.repair")
    assert len(repair) == 1 and not repair[0]["adopted"]
    assert repair[0]["reason"] == "alternative identical to the proposal"
    assert stops(same) == first


def test_all_specialist_candidates_avoided_is_labeled_and_reconsidered_last():
    proposal, events, _ = scripted_plan(discovery=CATALOG, avoid=CATALOG)
    assert proposal.proposed.itinerary.validation.valid
    started = planner_events(events, "planner.started")[0]
    assert started["ranking_source"] == "catalog" and started["avoided_count"] == len(CATALOG)
    assert planner_events(events, "planner.fallback") == [
        {"reason": "no unavoided specialist candidates", "candidate_count": 0},
        {"reason": "avoided places reconsidered", "candidate_count": len(CATALOG)}]


def test_repair_alternative_honours_the_reviewers_own_avoid_list():
    leaked, _, _ = scripted_plan(discovery=POOR, review=GOOD)
    assert "demo-gallery" in stops(leaked)
    proposal, events, _ = scripted_plan(discovery=POOR, review=GOOD, review_avoid=["demo-gallery"])
    repair = planner_events(events, "planner.repair")
    assert len(repair) == 1 and repair[0]["adopted"]
    assert proposal.proposed.itinerary.validation.valid
    assert "demo-gallery" not in stops(proposal)
    assert set(stops(proposal)) & set(GOOD) == set(GOOD) - {"demo-gallery"}
    # A required place is protected from the reviewer's avoid list, like every other specialist's.
    required = TripSnapshot(id="policy", request=TripRequest(required_place_ids=["demo-gallery"]))
    kept, events, _ = scripted_plan(required, discovery=POOR, review=GOOD, review_avoid=["demo-gallery"])
    assert kept.proposed.itinerary.validation.valid
    assert "demo-gallery" in stops(kept)


def test_repair_requires_a_request_addressed_to_discovery_or_coordinator():
    ungated, events, _ = scripted_plan(discovery=POOR, review=GOOD, review_to="mobility")
    assert not planner_events(events, "planner.repair")
    assert set(stops(ungated)) == set(POOR)
    gated, events, _ = scripted_plan(discovery=POOR, review=GOOD, review_to="coordinator")
    assert planner_events(events, "planner.repair")[0]["adopted"]
    assert set(stops(gated)) == set(GOOD)


def test_repair_is_scored_against_the_same_previous_plan(baseline):
    # The first search saw the accepted itinerary as "previous"; the alternative must be judged the same way,
    # so a reviewer cannot win merely because the proposal it criticises has become the reference plan.
    proposal, events, _ = scripted_plan(baseline, discovery=POOR, review=GOOD)
    repair = planner_events(events, "planner.repair")[0]
    assert repair["adopted"]
    assert proposal.proposed.itinerary.validation.valid and set(stops(proposal)) == set(GOOD)
    assert proposal.proposed.revision == baseline.revision + 1


def test_rain_runs_conditions_before_discovery(baseline, make_plan):
    proposal, events, _ = make_plan(baseline, TripEvent(id="rain", kind="rain", simulated=True))
    assert [r.role for r in proposal.proposed.reports] == ["conditions", "discovery", "mobility", "budget_pace"]
    assert [e["data"]["role"] for e in events if e["type"] == "agent.started"] == [
        "conditions", "discovery", "mobility", "budget_pace"]
    discovery = next(r for r in proposal.proposed.reports if r.role == "discovery")
    assert "Consumed the Conditions request" in discovery.summary
    started = planner_events(events, "planner.started")[0]
    assert started["avoided_count"] > 0 and started["ranking_source"] == "specialists"
    catalog = {p.id: p for p in proposal.proposed.places}
    assert all(catalog[pid].indoor for pid in stops(proposal))


def test_model_runner_follows_the_same_rain_protocol():
    rain = TripEvent(id="rain", kind="rain", simulated=True)
    proposal, events, runner = scripted_plan(event=rain, discovery=CATALOG, avoid=["demo-monbijou"])
    assert [r.role for r in proposal.proposed.reports] == ["conditions", "discovery", "mobility", "budget_pace"]
    assert runner.seen_messages["conditions"] == []
    assert [e["data"]["role"] for e in events if e["type"] == "agent.completed"] == [
        "conditions", "discovery", "mobility", "budget_pace"]
    assert "demo-monbijou" not in stops(proposal)


def test_dry_weather_keeps_discovery_and_conditions_concurrent_for_every_runner(make_plan):
    proposal, events, budget = make_plan()
    roles = [r.role for r in proposal.proposed.reports]
    assert roles == ["discovery", "conditions", "mobility", "budget_pace"]
    assert budget.model_calls == 0
    assert proposal.proposed.itinerary.validation.valid
