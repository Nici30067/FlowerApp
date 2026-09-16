"""Shared-state, dependency-aware collaboration and deterministic proposal generation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import uuid4

from travel_agent.agents import Emit, ExecutionBudget, ToolDispatcher
from travel_agent.planning.engine import points_for, schedule_order, search, validate, validate_trip
from travel_agent.providers.services import TravelProvider, key
from travel_agent.schemas import (
    AgentMessage,
    DayPlan,
    Evidence,
    ForecastInterval,
    Issue,
    Itinerary,
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
        result.weather_override = Weather(intervals=[ForecastInterval(start=start, end=result.request.trip_end,
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
        if not moment.tzinfo or moment < stop.start or moment > result.request.trip_end:
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
        if not moment.tzinfo or moment < (result.progress.now or result.request.start) or moment >= result.request.trip_end:
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

        # Multi-day plans reuse the same (trip-level, run-once) specialist reports and ranking
        # across every day; only the single-day search/schedule/refine steps repeat per day.
        # Determine which stored day (if any) the current progress belongs to, so its historical
        # completed prefix is preserved while other days start from a clean slate.
        completed_ids = set(work.progress.completed_place_ids)
        progress_day_index = 0
        if original.itinerary:
            for idx, day in enumerate(original.itinerary.days):
                if any(s.place_id in completed_ids for s in day.stops):
                    progress_day_index = idx
                    break
        event_itinerary = (apply_event(original, event).itinerary
                           if event and event.kind == "stop_completed" else None)

        days: list[DayPlan] = []
        scheduled_ids: list[str] = []
        cost_so_far = 0
        day0_places, day0_weather, day0_matrix = work.places, work.weather, matrix
        for k in range(work.request.days):
            day_req = work.request.day_request(k)
            day_req = day_req.model_copy(update={
                "excluded_place_ids": list(dict.fromkeys(day_req.excluded_place_ids + scheduled_ids)),
                "budget_minor": max(0, work.request.budget_minor - cost_so_far),
            })
            if k == 0:
                day_places, day_weather, day_matrix = day0_places, day0_weather, day0_matrix
            else:
                fresh = self.provider.search(day_req)
                retained_ids = set(work.progress.completed_place_ids) | {r.place_id for r in work.request.reservations}
                present = {p.id for p in fresh}
                day_places = fresh + [p for p in work.places if p.id in retained_ids and p.id not in present]
                day_weather = work.weather_override or self.provider.weather(day_req)
                day_points = points_for(TripSnapshot(id=work.id, request=day_req, places=day_places))
                day_matrix = self.provider.matrix(day_points, day_req.transport_mode)

            day_snapshot = work.model_copy(update={"request": day_req, "places": day_places, "weather": day_weather})
            day_snapshot.progress = work.progress if k == progress_day_index else Progress()
            if event_itinerary is not None and k == progress_day_index:
                day_snapshot.itinerary = event_itinerary.days[k] if k < len(event_itinerary.days) else None
            else:
                day_snapshot.itinerary = (original.itinerary.days[k]
                    if original.itinerary and k < len(original.itinerary.days) else None)

            day_place_ids = {p.id for p in day_places}
            day_ranking = [pid for pid in ranking if pid in day_place_ids]
            day_ranking += [pid for pid in day_place_ids if pid not in day_ranking]

            itinerary = search(day_snapshot, day_matrix, day_ranking)

            if k == 0:
                work.itinerary = itinerary
                budget_review = run("budget_pace", day_matrix)
                # A reviewer can request one extra candidate-order repair. It cannot relax user constraints.
                if budget_review.requests and budget_review.candidate_ids:
                    preferred = list(dict.fromkeys(budget_review.candidate_ids + day_ranking))
                    alternative = search(day_snapshot, day_matrix, preferred)
                    if alternative.validation.valid and (not itinerary.validation.valid
                                                          or alternative.score >= itinerary.score):
                        itinerary = alternative

            if itinerary.validation.valid:
                completed_count = len(day_snapshot.progress.completed_place_ids)
                day_points = points_for(day_snapshot)
                precise = self.provider.geometry(itinerary.legs[completed_count:], day_points)
                for leg in precise:
                    day_matrix[key(leg.from_id, leg.to_id)] = leg
                order = [s.place_id for s in itinerary.stops if not s.completed]
                rebuilt = schedule_order(day_snapshot, day_matrix, order)
                if rebuilt is None:
                    itinerary.validation = Validation(valid=False, status="infeasible", issues=[Issue(
                        code="DIRECTIONS_CONFLICT", severity="error",
                        message="Final directions metrics conflict with the schedule. Refresh conditions or adjust constraints.")])
                else:
                    itinerary = rebuilt
                    itinerary.validation = validate(day_snapshot, itinerary)

            itinerary = itinerary.model_copy(update={"index": k, "date": day_req.start.date().isoformat()})
            days.append(itinerary)
            scheduled_ids += [s.place_id for s in itinerary.stops]
            cost_so_far += itinerary.cost_minor

        trip_itinerary = Itinerary(days=days,
            walking_m=sum(d.walking_m for d in days), travel_duration_s=sum(d.travel_duration_s for d in days),
            cost_minor=sum(d.cost_minor for d in days), unknown_cost_count=sum(d.unknown_cost_count for d in days),
            score=sum(d.score for d in days))
        # A day that never produced a schedule at all (search/schedule_order gave up outright)
        # carries a specific diagnostic (e.g. NO_FEASIBLE_PLAN, DIRECTIONS_CONFLICT) that a
        # from-scratch validate_trip recompute cannot reconstruct (it only re-checks generic
        # schedule invariants against whatever stops exist, which is none here). Surface that
        # day's own diagnosis directly rather than losing it behind a generic TOO_FEW_STOPS.
        hard_fail = next((d for d in days if any(i.code in ("NO_FEASIBLE_PLAN", "DIRECTIONS_CONFLICT")
                                                  for i in d.validation.issues)), None)
        if hard_fail is not None:
            trip_itinerary.validation = Validation(valid=False, status="infeasible",
                issues=[i.model_copy(update={"day_index": hard_fail.index}) for i in hard_fail.validation.issues])
        else:
            trip_itinerary.validation = validate_trip(work, trip_itinerary)
        work.itinerary = trip_itinerary
        itinerary = trip_itinerary
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
