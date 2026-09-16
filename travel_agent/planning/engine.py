from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from travel_agent.providers.services import key
from travel_agent.schemas import (
    Break,
    Coordinate,
    Issue,
    Itinerary,
    Leg,
    Place,
    Stop,
    TripSnapshot,
    Validation,
    Weather,
)


def rainy(weather: Weather | None, start: datetime, end: datetime, threshold: int) -> bool:
    return bool(weather and any(i.start < end and i.end > start and
        i.precipitation_probability_pct >= threshold for i in weather.intervals))


def weather_covered(weather: Weather | None, start: datetime, end: datetime) -> bool:
    if not weather:
        return False
    cursor = start
    for interval in sorted(weather.intervals, key=lambda x: x.start):
        if interval.end <= cursor:
            continue
        if interval.start > cursor:
            return False
        cursor = max(cursor, interval.end)
        if cursor >= end:
            return True
    return False


def points_for(snapshot: TripSnapshot) -> dict[str, Coordinate]:
    return {"origin": snapshot.progress.location or snapshot.request.origin,
            "destination": snapshot.request.destination,
            **{p.id: p.coordinate for p in snapshot.places}}


def prefix(snapshot: TripSnapshot) -> tuple[list[Stop], list[Leg], list[Break]]:
    completed = snapshot.progress.completed_place_ids
    if not completed or not snapshot.itinerary:
        return [], [], []
    stops = [s.model_copy(deep=True, update={"completed": True}) for s in snapshot.itinerary.stops
             if s.place_id in completed]
    legs = snapshot.itinerary.legs[:len(stops)]
    last = stops[-1].end if stops else snapshot.request.start
    breaks = [b for b in snapshot.itinerary.breaks if b.end <= last]
    return stops, [x.model_copy(deep=True) for x in legs], [x.model_copy(deep=True) for x in breaks]


def _visit_start(place: Place, arrival: datetime, duration_s: int, snapshot: TripSnapshot,
                 locked_time: datetime | None) -> datetime | None:
    candidate = locked_time or arrival
    if locked_time and arrival > locked_time:
        return None
    intervals = place.opening_intervals
    for _ in range(30):
        finish = candidate + timedelta(seconds=duration_s)
        if intervals is not None:
            options = [i for i in intervals if max(candidate, i.start) + timedelta(seconds=duration_s) <= i.end]
            if not options:
                return None
            next_start = max(candidate, options[0].start)
            if locked_time and next_start != locked_time:
                return None
            candidate = next_start
            finish = candidate + timedelta(seconds=duration_s)
        if candidate >= snapshot.request.end:
            return None
        if (place.indoor is not True and snapshot.request.avoid_rain_outdoor_visits
                and rainy(snapshot.weather, candidate, finish, snapshot.request.rain_threshold_pct)):
            if locked_time:
                return None
            blocking = [i for i in snapshot.weather.intervals if i.start < finish and i.end > candidate
                        and i.precipitation_probability_pct >= snapshot.request.rain_threshold_pct]
            candidate = max(i.end for i in blocking)
            continue
        return candidate
    return None


def schedule_order(snapshot: TripSnapshot, matrix: dict[str, Leg], order: Iterable[str],
                   scores: dict[str, float] | None = None) -> Itinerary | None:
    """Construct a schedule from an order. Return None on a known hard conflict."""
    req = snapshot.request
    catalog = {p.id: p for p in snapshot.places}
    stops, legs, breaks = prefix(snapshot)
    prefix_count = len(stops)
    now = max(req.start, snapshot.progress.now or req.start)
    cur = "origin"
    cost = snapshot.progress.spent_minor
    walking = sum(l.distance_m for l in legs if l.mode == "walking")
    score = 0.0
    reservations = {r.place_id: r for r in req.reservations}
    order = list(order)
    if len(set(order)) != len(order):
        return None
    for n, pid in enumerate(order):
        if pid not in catalog or pid in snapshot.closed_place_ids or pid in req.excluded_place_ids:
            return None
        if pid in snapshot.progress.completed_place_ids:
            return None
        if n and n % 2 == 0:
            pause = Break(location_id=cur, start=now, end=now + timedelta(seconds=req.break_duration_s),
                          reason="Scheduled break after two activities")
            breaks.append(pause)
            now = pause.end
        leg = matrix.get(key(cur, pid))
        if not leg:
            return None
        if leg.mode == "walking" and leg.duration_s > req.max_continuous_walking_s:
            return None
        walking += leg.distance_m if leg.mode == "walking" else 0
        place = catalog[pid]
        arrival = now + timedelta(seconds=leg.duration_s)
        reservation = reservations.get(pid)
        duration = reservation.duration_s if reservation else place.visit_duration_s
        start = _visit_start(place, arrival, duration, snapshot, reservation.start if reservation else None)
        if start is None:
            return None
        end = start + timedelta(seconds=duration)
        cost += place.cost_minor or 0
        if walking > req.max_walking_m or cost > req.budget_minor or end > req.end:
            return None
        stops.append(Stop(place_id=pid, name=place.name, arrival=arrival, start=start, end=end,
                          cost_minor=place.cost_minor, cost_status=place.cost_status, locked=bool(reservation),
                          reason="Matches " + ", ".join(sorted(set(place.categories) & set(req.interests)))))
        legs.append(leg.model_copy(deep=True))
        now, cur = end, pid
        score += (scores or {}).get(pid, 1.0)
    home = matrix.get(key(cur, "destination"))
    if not home:
        return None
    if home.mode == "walking" and home.duration_s > req.max_continuous_walking_s:
        return None
    walking += home.distance_m if home.mode == "walking" else 0
    now += timedelta(seconds=home.duration_s)
    if now > req.end or walking > req.max_walking_m or cost > req.budget_minor:
        return None
    legs.append(home.model_copy(deep=True))
    unknown = sum(s.cost_minor is None for s in stops[prefix_count:])
    travel = sum(l.duration_s for l in legs)
    previous = set(s.place_id for s in snapshot.itinerary.stops) if snapshot.itinerary else set()
    score += 1.5 * len(set(order) & previous)
    categories = set(c for pid in order for c in catalog[pid].categories)
    score += 1.5 * len(categories & set(req.interests))
    score += len(order) * 2 - travel / 1500 - cost / 20000 - unknown * 0.5
    return Itinerary(stops=stops, legs=legs, breaks=breaks, end_arrival=now,
                     walking_m=walking, travel_duration_s=travel, cost_minor=cost,
                     unknown_cost_count=unknown, score=score)


def validate(snapshot: TripSnapshot, itinerary: Itinerary) -> Validation:
    """Independently inspect the persisted schedule and recompute reported totals."""
    req = snapshot.request
    places = {p.id: p for p in snapshot.places}
    issues: list[Issue] = []
    def issue(code, message, pid=None, warning=False):
        issues.append(Issue(code=code, message=message, place_id=pid, severity="warning" if warning else "error"))
    ids = [s.place_id for s in itinerary.stops]
    if len(ids) != len(set(ids)):
        issue("DUPLICATE_STOP", "A place occurs more than once")
    required = set(req.required_place_ids) | {r.place_id for r in req.reservations}
    if missing := required - set(ids):
        issue("MISSING_REQUIRED", "Required stops are missing: " + ", ".join(sorted(missing)))
    if len(ids) < req.min_stops:
        issue("TOO_FEW_STOPS", f"At least {req.min_stops} stops are required")
    if itinerary.end_arrival > req.end:
        issue("END_TIME", "Destination arrival exceeds the trip end time")
    if len(itinerary.legs) != len(itinerary.stops) + 1:
        issue("ROUTE_COUNT", "Every stop and the final destination require a travel leg")
    completed_stops, _, _ = prefix(snapshot)
    count_completed = len(completed_stops)
    for i, old in enumerate(completed_stops):
        if i >= len(itinerary.stops) or itinerary.stops[i] != old:
            issue("COMPLETED_CHANGED", "A completed activity was changed", old.place_id)
    future = itinerary.stops[count_completed:]
    moment = max(req.start, snapshot.progress.now or req.start)
    prev = "origin"
    for i, stop in enumerate(future, start=count_completed):
        if stop.place_id not in places:
            issue("UNKNOWN_PLACE", "Stop has no evidence record", stop.place_id)
            continue
        place = places[stop.place_id]
        if stop.place_id in set(req.excluded_place_ids) | set(snapshot.closed_place_ids):
            issue("UNAVAILABLE", "Stop is excluded or reported unavailable", stop.place_id)
        if stop.start < stop.arrival or stop.end <= stop.start:
            issue("TIME_ORDER", "Invalid arrival, visit start, or visit end", stop.place_id)
        if stop.end > req.end:
            issue("VISIT_END", "Visit ends after the available period", stop.place_id)
        if i < len(itinerary.legs):
            leg = itinerary.legs[i]
            if leg.from_id != prev or leg.to_id != stop.place_id:
                issue("ROUTE_LINK", "Travel leg endpoints do not match the schedule", stop.place_id)
            extra = sum((b.end - b.start).total_seconds() for b in itinerary.breaks
                        if b.start >= moment and b.end <= stop.arrival)
            if stop.arrival < moment + timedelta(seconds=leg.duration_s + extra):
                issue("TRAVEL_TIME", "Arrival omits part of travel or a scheduled break", stop.place_id)
        if place.opening_intervals is None:
            issue("HOURS_UNKNOWN", "Opening hours need verification", stop.place_id,
                  warning=not req.require_verified_facts)
        elif not any(x.start <= stop.start and stop.end <= x.end for x in place.opening_intervals):
            issue("CLOSED", "The full visit does not fit the recorded opening intervals", stop.place_id)
        if place.currency != req.currency and place.cost_minor is not None:
            issue("CURRENCY", "Cost currency differs from the trip currency", stop.place_id)
        if stop.cost_minor != place.cost_minor or stop.cost_status != place.cost_status:
            issue("COST_CHANGED", "Stop cost differs from its evidence record", stop.place_id)
        if place.cost_minor is None:
            issue("PRICE_UNKNOWN", "Price is unknown; budget verification is incomplete", stop.place_id,
                  warning=not req.require_verified_facts)
        elif place.cost_status in ("estimated", "fixture"):
            issue("PRICE_UNVERIFIED", "Cost is an estimate or fixture", stop.place_id,
                  warning=not req.require_verified_facts)
        if place.indoor is None:
            issue("EXPOSURE_UNKNOWN", "Indoor status needs verification", stop.place_id, warning=True)
        if req.avoid_rain_outdoor_visits and place.indoor is not True and rainy(
                snapshot.weather, stop.start, stop.end, req.rain_threshold_pct):
            issue("WEATHER_CONFLICT", "Visit overlaps the configured rain threshold", stop.place_id)
        if not weather_covered(snapshot.weather, stop.start, stop.end):
            issue("FORECAST_UNKNOWN", "Forecast coverage is incomplete", stop.place_id,
                  warning=not req.require_verified_facts)
        prev, moment = stop.place_id, stop.end
    for reservation in req.reservations:
        match = next((s for s in itinerary.stops if s.place_id == reservation.place_id), None)
        if match and (match.start != reservation.start or
                      match.end - match.start != timedelta(seconds=reservation.duration_s)):
            issue("RESERVATION_CHANGED", "A locked start time or duration changed", reservation.place_id)
    if itinerary.legs:
        last = itinerary.legs[-1]
        if last.to_id != "destination" or last.from_id != prev:
            issue("ENDPOINT", "Final travel leg does not reach the required destination")
        if itinerary.end_arrival < moment + timedelta(seconds=last.duration_s):
            issue("END_TRAVEL", "Destination arrival omits final travel time")
    walking = sum(x.distance_m for x in itinerary.legs if x.mode == "walking")
    duration = sum(x.duration_s for x in itinerary.legs)
    cost = snapshot.progress.spent_minor + sum(s.cost_minor or 0 for s in future)
    if walking != itinerary.walking_m or duration != itinerary.travel_duration_s or cost != itinerary.cost_minor:
        issue("TOTALS_MISMATCH", "Reported totals differ from independently recomputed totals")
    if walking > req.max_walking_m:
        issue("WALKING_LIMIT", "Total walking exceeds the configured limit")
    if cost > req.budget_minor:
        issue("BUDGET_LIMIT", "Accounted spending exceeds the budget")
    for leg in itinerary.legs[count_completed:]:
        if leg.mode != req.transport_mode:
            issue("TRANSPORT_MODE", "Travel leg uses a mode outside the request")
        if leg.mode == "walking" and leg.duration_s > req.max_continuous_walking_s:
            issue("CONTINUOUS_WALK", "A travel leg exceeds the continuous walking limit")
        if len(leg.geometry) < 2:
            issue("GEOMETRY_MISSING", "Route geometry has not been retrieved", warning=True)
    if any(l.source_status == "fixture" for l in itinerary.legs):
        issue("SYNTHETIC_ROUTES", "Travel times and direct-line geometry are synthetic demonstration values", warning=True)
    # Walking exposure is disclosed even when both destinations are indoors.
    if snapshot.weather and any(x.precipitation_probability_pct >= req.rain_threshold_pct
                                for x in snapshot.weather.intervals):
        issue("OUTDOOR_TRANSFERS", "Travel between indoor stops may still be exposed to rain", warning=True)
    valid = not any(i.severity == "error" for i in issues)
    return Validation(valid=valid, status="infeasible" if not valid else "provisional" if issues else "valid",
                      issues=issues)


def candidate_scores(snapshot: TripSnapshot, ranking: list[str], candidates: Iterable[str]) -> dict[str, float]:
    """Per-stop value: 1 + 3 per matched interest, plus up to 3 for the specialists' rank (0 when unranked)."""
    catalog = {p.id: p for p in snapshot.places}
    interests = set(snapshot.request.interests)
    position = {pid: idx for idx, pid in enumerate(ranking)}
    scores = {}
    for pid in candidates:
        score = 1 + 3 * len(set(catalog[pid].categories) & interests)
        if pid in position:
            score += 3 * (len(ranking) - position[pid]) / len(ranking)
        scores[pid] = score
    return scores


def search(snapshot: TripSnapshot, matrix: dict[str, Leg], ranking: list[str], beam_width: int = 64) -> Itinerary:
    """Bounded beam search. Failure is a search result, not an optimality proof."""
    req = snapshot.request
    completed = set(snapshot.progress.completed_place_ids)
    catalog = {p.id: p for p in snapshot.places}
    required = (set(req.required_place_ids) | {r.place_id for r in req.reservations}) - completed
    ranking = list(dict.fromkeys(ranking))
    # Ranked places expand before unranked required ones; a rank bonus is worth as much as one matched interest.
    candidates = [pid for pid in dict.fromkeys(ranking + sorted(required)) if pid in catalog and pid not in completed
                  and pid not in req.excluded_place_ids and pid not in snapshot.closed_place_ids]
    scores = candidate_scores(snapshot, ranking, candidates)
    beams = [()]
    best: Itinerary | None = None
    empty = schedule_order(snapshot, matrix, [])
    if empty and not required and len(completed) >= req.min_stops:
        best = empty
    for _ in range(max(0, req.target_stops - len(completed))):
        expanded = []
        for order in beams:
            for pid in candidates:
                if pid in order:
                    continue
                new_order = (*order, pid)
                itinerary = schedule_order(snapshot, matrix, new_order, scores)
                if itinerary is None:
                    continue
                priority = itinerary.score + 20 * len(required & set(new_order))
                expanded.append((priority, new_order))
                if required <= set(new_order) and len(itinerary.stops) >= req.min_stops:
                    if best is None or itinerary.score > best.score:
                        best = itinerary
        expanded.sort(key=lambda item: (-item[0], item[1]))
        beams = [order for _, order in expanded[:beam_width]]
        if not beams:
            break
    if best is None:
        return Itinerary(end_arrival=max(req.start, snapshot.progress.now or req.start),
            validation=Validation(valid=False, status="infeasible", issues=[Issue(code="NO_FEASIBLE_PLAN",
                severity="error", message="The bounded search found no feasible itinerary. Review required stops, "
                "reservations, available time, walking limits, budget, weather restrictions, and route availability.")]))
    best.validation = validate(snapshot, best)
    return best
