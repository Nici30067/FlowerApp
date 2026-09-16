"""Shared-state, dependency-aware collaboration and deterministic proposal generation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import uuid4

from travel_agent.agents import Emit, ExecutionBudget, ToolDispatcher
from travel_agent.planning.engine import points_for, schedule_order, search, validate
from travel_agent.providers.services import TravelProvider, key
from travel_agent.schemas import (
    AgentMessage,
    Evidence,
    ForecastInterval,
    Issue,
    Progress,
    Proposal,
    Reservation,
    TripEvent,
    TripRequest,
    TripSnapshot,
    Validation,
    Weather,
)


def apply_event(snapshot: TripSnapshot, event: TripEvent) -> TripSnapshot:
    result = snapshot.model_copy(deep=True)
    req = result.request.model_dump(mode="json")
    p = event.payload
    if event.kind == "rain":
        if not event.simulated:
            raise ValueError("Rain scenario injection must be labeled simulated")
        start = result.progress.now or result.request.start
        result.weather_override = Weather(intervals=[ForecastInterval(start=start, end=result.request.end,
            precipitation_probability_pct=95, temperature_c=16)],
            source=Evidence(id="scenario-" + event.id, provider="User-injected weather scenario", status="fixture",
                retrieved_at=start, note="Simulated event, not an observed forecast update."))
    elif event.kind == "weather_updated":
        # Refresh the provider; arbitrary caller-supplied forecasts are not accepted.
        if p:
            raise ValueError("weather_updated takes no payload")
        result.weather_override = None
    elif event.kind == "pace_changed":
        req["max_walking_m"] = p["max_walking_m"]
        if "max_continuous_walking_s" in p:
            req["max_continuous_walking_s"] = p["max_continuous_walking_s"]
    elif event.kind == "budget_changed":
        req["budget_minor"] = p["budget_minor"]
    elif event.kind == "place_unavailable":
        pid = p["place_id"]
        if pid not in {x.id for x in result.places}:
            raise ValueError("Unknown place ID")
        if pid not in result.closed_place_ids:
            result.closed_place_ids.append(pid)
    elif event.kind in ("lock_stop", "unlock_stop"):
        if not result.itinerary:
            raise ValueError("Create an itinerary before locking a stop")
        pid = p["place_id"]
        stop = next((s for s in result.itinerary.stops if s.place_id == pid), None)
        if not stop:
            raise ValueError("Stop is not in the current itinerary")
        reservations = [r.model_dump(mode="json") for r in result.request.reservations if r.place_id != pid]
        if event.kind == "lock_stop":
            reservations.append(Reservation(place_id=pid, start=stop.start,
                duration_s=int((stop.end - stop.start).total_seconds())).model_dump(mode="json"))
        req["reservations"] = reservations
    elif event.kind == "stop_completed":
        if not result.itinerary:
            raise ValueError("No itinerary to update")
        future = [s for s in result.itinerary.stops if s.place_id not in result.progress.completed_place_ids]
        if not future or p["place_id"] != future[0].place_id:
            raise ValueError("Complete the next planned stop in sequence")
        stop = future[0]
        actual = int(p["actual_spent_minor"])
        if actual < 0:
            raise ValueError("Actual spending cannot be negative")
        moment = datetime.fromisoformat(p["completed_at"])
        if not moment.tzinfo or moment < stop.start or moment > result.request.end:
            raise ValueError("Completion time must be timezone-aware and inside the trip period")
        if result.progress.now and moment < result.progress.now:
            raise ValueError("Progress cannot move backwards")
        # Record actual finish before preserving this activity in future revisions.
        stop.end = moment
        stop.completed = True
        place = next(x for x in result.places if x.id == stop.place_id)
        result.progress = Progress(now=moment, location=place.coordinate,
            spent_minor=result.progress.spent_minor + actual,
            completed_place_ids=[*result.progress.completed_place_ids, stop.place_id])
        # A finished reservation is historical, so its old planned end no longer constrains actual progress.
        req["reservations"] = [r for r in req["reservations"] if r["place_id"] != stop.place_id]
    elif event.kind == "user_running_late":
        moment = datetime.fromisoformat(p["now"])
        if not moment.tzinfo or moment < (result.progress.now or result.request.start) or moment >= result.request.end:
            raise ValueError("Updated time must move forward and remain inside the trip period")
        from travel_agent.schemas import Coordinate
        result.progress.now = moment
        result.progress.location = Coordinate.model_validate(p["location"])
    elif event.kind == "preferences_changed":
        allowed = {"interests", "max_walking_m", "budget_minor", "max_continuous_walking_s", "break_duration_s",
                   "target_stops", "min_stops", "rain_threshold_pct", "avoid_rain_outdoor_visits", "transport_mode",
                   "origin", "destination", "required_place_ids", "excluded_place_ids"}
        if not set(p) <= allowed:
            raise ValueError("Unsupported preference change")
        req.update(p)
    result.request = TripRequest.model_validate(req)
    return result


class Coordinator:
    def __init__(self, provider: TravelProvider, runner, emit: Emit, budget: ExecutionBudget, connectors=None):
        self.provider, self.runner, self.emit, self.budget = provider, runner, emit, budget
        self.connectors = connectors

    def plan(self, original: TripSnapshot, event: TripEvent | None = None) -> Proposal:
        work = apply_event(original, event) if event else original.model_copy(deep=True)
        work.reports, work.messages = [], []
        work.agent_mode = self.runner.mode
        self.emit("coordinator.started", {"base_revision": original.revision,
            "data_mode": self.provider.mode, "agent_mode": self.runner.mode,
            "trigger": event.kind if event else "initial_plan", "simulated": event.simulated if event else False})
        # These independent read-only provider calls are genuinely concurrent.
        with ThreadPoolExecutor(max_workers=2) as pool:
            place_job = pool.submit(self.provider.search, work.request)
            weather_job = pool.submit(self.provider.weather, work.request) if not work.weather_override else None
            fresh = place_job.result()
            # Preserve evidence for completed/locked places when refreshing a changing catalog.
            retained_ids = set(work.progress.completed_place_ids) | {r.place_id for r in work.request.reservations}
            present = {p.id for p in fresh}
            fresh += [p for p in work.places if p.id in retained_ids and p.id not in present]
            work.places = fresh
            work.weather = work.weather_override or weather_job.result()
        self.emit("evidence.loaded", {"place_count": len(work.places), "weather_status": work.weather.source.status,
                                      "data_mode": self.provider.mode})

        def run(role, matrix=None):
            report = self.runner.run(role, work, ToolDispatcher(work, matrix, self.connectors))
            work.reports.append(report)
            for message in report.requests:
                work.messages.append(message)
                self.emit("collaboration.message", message.model_dump(mode="json"))
            return report

        # Discovery and Conditions read the same evidence and neither depends on the other's first report,
        # so they run concurrently. Their reports are appended in a fixed order for deterministic state.
        if self.runner.mode == "model":
            frozen = work.model_copy(deep=True)
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = {role: pool.submit(self.runner.run, role, frozen, ToolDispatcher(frozen, None, self.connectors))
                        for role in ("discovery", "conditions")}
                discovery, conditions = jobs["discovery"].result(), jobs["conditions"].result()
            for report in (discovery, conditions):
                work.reports.append(report)
                for message in report.requests:
                    work.messages.append(message)
                    self.emit("collaboration.message", message.model_dump(mode="json"))
        else:
            discovery = run("discovery")
            conditions = run("conditions")
        if any(m.recipient == "discovery" for m in conditions.requests):
            discovery = run("discovery")
        self.budget.consume("tool")
        points = points_for(work)
        matrix = self.provider.matrix(points, work.request.transport_mode)
        mobility = run("mobility", matrix)
        ranking = list(dict.fromkeys(mobility.candidate_ids + discovery.candidate_ids + [p.id for p in work.places]))
        self.emit("planner.started", {"algorithm": "bounded_beam_search", "beam_width": 64,
                                      "candidate_count": len(ranking)})
        itinerary = search(work, matrix, ranking)
        work.itinerary = itinerary
        budget_review = run("budget_pace", matrix)
        # A reviewer can request one extra candidate-order repair. It cannot relax user constraints.
        if budget_review.requests and budget_review.candidate_ids:
            preferred = list(dict.fromkeys(budget_review.candidate_ids + ranking))
            alternative = search(work, matrix, preferred)
            if alternative.validation.valid and (not itinerary.validation.valid or alternative.score >= itinerary.score):
                itinerary = alternative
        if itinerary.validation.valid:
            completed_count = len(work.progress.completed_place_ids)
            precise = self.provider.geometry(itinerary.legs[completed_count:], points)
            for leg in precise:
                matrix[key(leg.from_id, leg.to_id)] = leg
            order = [s.place_id for s in itinerary.stops if not s.completed]
            # Keep the original completed prefix when reconstructing against directions metrics.
            work.itinerary = original.itinerary if not event or event.kind != "stop_completed" else work.itinerary
            # For progress events, apply_event already updated the historical activity.
            if event and event.kind == "stop_completed":
                work.itinerary = apply_event(original, event).itinerary
            rebuilt = schedule_order(work, matrix, order)
            if rebuilt is None:
                itinerary.validation = Validation(valid=False, status="infeasible", issues=[Issue(
                    code="DIRECTIONS_CONFLICT", severity="error",
                    message="Final directions metrics conflict with the schedule. Refresh conditions or adjust constraints.")])
            else:
                itinerary = rebuilt
                itinerary.validation = validate(work, itinerary)
        work.itinerary = itinerary
        work.revision = original.revision + 1
        self.emit("validation.completed", itinerary.validation.model_dump(mode="json"))
        message = AgentMessage(sender="coordinator", recipient="user", kind="proposal",
            summary=f"Proposed revision {work.revision}; {len(itinerary.stops)} stops; status {itinerary.validation.status}.",
            place_ids=[s.place_id for s in itinerary.stops])
        work.messages.append(message)
        self.emit("collaboration.message", message.model_dump(mode="json"))
        previous = [s.place_id for s in original.itinerary.stops] if original.itinerary else []
        current = [s.place_id for s in itinerary.stops]
        proposal = Proposal(id=uuid4().hex, trip_id=original.id, base_revision=original.revision,
            trigger=event.kind if event else "initial_plan", proposed=work,
            added=[pid for pid in current if pid not in previous], removed=[pid for pid in previous if pid not in current],
            preserved=[pid for pid in previous if pid in current])
        self.emit("proposal.ready", {"proposal_id": proposal.id, "added": proposal.added,
                                    "removed": proposal.removed, "preserved": proposal.preserved,
                                    "metrics": self.budget.metrics()})
        return proposal
