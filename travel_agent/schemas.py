"""Typed contracts. Distances: meters. Durations: seconds. Money: minor units."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Coordinate(Contract):
    lat: float = Field(ge=-85, le=85, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)

    def geojson(self) -> list[float]:
        return [self.lon, self.lat]


class Interval(Contract):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def valid_interval(self):
        if not self.start.tzinfo or not self.end.tzinfo:
            raise ValueError("Intervals require timezone-aware timestamps")
        if self.end <= self.start:
            raise ValueError("Interval end must follow start")
        return self


class Evidence(Contract):
    id: str
    provider: str
    status: Literal["fixture", "live", "cached", "user", "unknown"]
    retrieved_at: datetime
    reference: str = ""
    note: str = ""


class Place(Contract):
    id: str
    name: str = Field(max_length=200)
    coordinate: Coordinate
    categories: list[str]
    indoor: bool | None = None
    visit_duration_s: int = Field(default=2700, ge=300, le=14400)
    cost_minor: int | None = Field(default=None, ge=0)
    cost_status: Literal["fixture", "verified", "estimated", "unknown"] = "unknown"
    currency: Literal["EUR", "USD", "GBP", "SEK"] = "EUR"
    opening_hours: str | None = None
    opening_intervals: list[Interval] | None = None
    opening_status: Literal["fixture", "tag", "unknown"] = "unknown"
    source: Evidence
    osm_type: str | None = None
    osm_id: int | None = None
    website: str | None = None
    description: str = ""


class ForecastInterval(Interval):
    precipitation_probability_pct: float = Field(ge=0, le=100, allow_inf_nan=False)
    temperature_c: float | None = Field(default=None, allow_inf_nan=False)


class Weather(Contract):
    intervals: list[ForecastInterval]
    source: Evidence


class Reservation(Contract):
    place_id: str
    start: datetime
    duration_s: int = Field(ge=300, le=14400)

    @field_validator("start")
    @classmethod
    def aware(cls, value):
        if not value.tzinfo:
            raise ValueError("Reservation time requires a timezone")
        return value


class TripRequest(Contract):
    title: str = Field(default="A day in Berlin", min_length=1, max_length=120)
    city: str = Field(default="Berlin", max_length=80)
    timezone: str = "Europe/Berlin"
    # start/end describe day 1's planning window; day_window()/day_request() project it forward.
    start: datetime = datetime.fromisoformat("2026-09-16T10:00:00+02:00")
    end: datetime = datetime.fromisoformat("2026-09-16T17:00:00+02:00")
    days: int = Field(default=1, ge=1, le=14)
    origin: Coordinate = Field(default_factory=lambda: Coordinate(lat=52.5225, lon=13.4024))
    destination: Coordinate = Field(default_factory=lambda: Coordinate(lat=52.5225, lon=13.4024))
    interests: list[str] = Field(default_factory=lambda: ["art", "architecture", "parks", "coffee"])
    currency: Literal["EUR", "USD", "GBP", "SEK"] = "EUR"
    # budget_minor is a whole-trip total; every other limit below is per day.
    budget_minor: int = Field(default=6000, ge=0, le=1000000)
    max_walking_m: int = Field(default=5000, ge=0, le=50000)
    max_continuous_walking_s: int = Field(default=1800, ge=60, le=14400)
    break_duration_s: int = Field(default=900, ge=300, le=7200)
    min_stops: int = Field(default=2, ge=0, le=8)
    target_stops: int = Field(default=5, ge=1, le=8)
    rain_threshold_pct: int = Field(default=60, ge=0, le=100)
    avoid_rain_outdoor_visits: bool = True
    transport_mode: Literal["walking", "cycling"] = "walking"
    require_verified_facts: bool = False
    required_place_ids: list[str] = Field(default_factory=list, max_length=8)
    excluded_place_ids: list[str] = Field(default_factory=list, max_length=100)
    reservations: list[Reservation] = Field(default_factory=list, max_length=8)
    max_candidates: int = Field(default=12, ge=3, le=16)

    @model_validator(mode="after")
    def bounds(self):
        ZoneInfo(self.timezone)
        if not self.start.tzinfo or not self.end.tzinfo:
            raise ValueError("Trip times require timezone information")
        if self.end <= self.start or self.end - self.start > timedelta(hours=18):
            raise ValueError("Use one planning period of more than zero and at most 18 hours")
        if self.min_stops > self.target_stops:
            raise ValueError("min_stops must not exceed target_stops")
        if len(set(self.required_place_ids) & set(self.excluded_place_ids)):
            raise ValueError("A required place cannot also be excluded")
        if len({r.place_id for r in self.reservations}) != len(self.reservations):
            raise ValueError("Duplicate reservations for a place are unsupported")
        return self

    def day_window(self, index: int) -> tuple[datetime, datetime]:
        """The localized start/end datetimes for day `index` (0-based). Adds whole
        local calendar days (via ZoneInfo), not a fixed-offset timedelta, so DST
        transitions never silently shift the schedule by an hour."""
        tz = ZoneInfo(self.timezone)
        shift = timedelta(days=index)

        def shifted(moment: datetime) -> datetime:
            return moment.astimezone(tz) + shift

        return shifted(self.start), shifted(self.end)

    def day_request(self, index: int) -> "TripRequest":
        """This request projected onto a single day, for reuse by the single-day engine."""
        start, end = self.day_window(index)
        return self.model_copy(update={"start": start, "end": end, "days": 1})

    @property
    def trip_start(self) -> datetime:
        return self.start

    @property
    def trip_end(self) -> datetime:
        return self.day_window(self.days - 1)[1]


class Leg(Contract):
    from_id: str
    to_id: str
    distance_m: int = Field(ge=0)
    duration_s: int = Field(ge=0)
    mode: Literal["walking", "cycling"]
    geometry: list[list[float]]
    source_status: Literal["fixture", "live", "cached"]
    evidence_id: str


class Stop(Contract):
    place_id: str
    name: str
    arrival: datetime
    start: datetime
    end: datetime
    cost_minor: int | None
    cost_status: str
    locked: bool = False
    completed: bool = False
    reason: str = ""


class Break(Contract):
    location_id: str
    start: datetime
    end: datetime
    reason: str = "Walking recovery break"


class Issue(Contract):
    code: str
    severity: Literal["error", "warning"]
    message: str
    place_id: str | None = None
    day_index: int | None = None


class Validation(Contract):
    valid: bool
    status: Literal["valid", "provisional", "infeasible"]
    issues: list[Issue] = Field(default_factory=list)


class DayPlan(Contract):
    """A single day's schedule. This is what the single-day engine (planning/engine.py)
    builds and validates; a multi-day Itinerary is a list of these."""
    index: int = Field(default=0, ge=0)
    date: str = ""  # ISO date (YYYY-MM-DD), local to the trip timezone; set by the coordinator.
    stops: list[Stop] = Field(default_factory=list)
    legs: list[Leg] = Field(default_factory=list)
    breaks: list[Break] = Field(default_factory=list)
    end_arrival: datetime
    walking_m: int = 0
    travel_duration_s: int = 0
    cost_minor: int = 0
    unknown_cost_count: int = 0
    validation: Validation = Field(default_factory=lambda: Validation(valid=False, status="infeasible"))
    score: float = 0


_DAY_PLAN_FIELDS = {"stops", "legs", "breaks", "end_arrival", "walking_m", "travel_duration_s",
                    "cost_minor", "unknown_cost_count", "validation", "score"}


class Itinerary(Contract):
    """The whole trip: one DayPlan per day, plus trip-level totals and validation."""
    days: list[DayPlan] = Field(default_factory=list)
    walking_m: int = 0
    travel_duration_s: int = 0
    cost_minor: int = 0
    unknown_cost_count: int = 0
    validation: Validation = Field(default_factory=lambda: Validation(valid=False, status="infeasible"))
    score: float = 0

    @model_validator(mode="before")
    @classmethod
    def _wrap_single_day(cls, data: Any) -> Any:
        """Accept a bare DayPlan (used internally when the coordinator drives one day),
        and upgrade a legacy single-day Itinerary payload (persisted before multi-day
        support existed, or a single-day dict built by the engine) into a 1-day trip."""
        if isinstance(data, DayPlan):
            day = data
            return {"days": [day], "walking_m": day.walking_m, "travel_duration_s": day.travel_duration_s,
                    "cost_minor": day.cost_minor, "unknown_cost_count": day.unknown_cost_count,
                    "validation": day.validation, "score": day.score}
        if isinstance(data, dict) and "days" not in data and "end_arrival" in data:
            day = {k: v for k, v in data.items() if k in _DAY_PLAN_FIELDS}
            day["index"] = 0
            end = data["end_arrival"]
            day["date"] = (end[:10] if isinstance(end, str) else end.date().isoformat())
            trip_totals = {k: data[k] for k in _DAY_PLAN_FIELDS if k in data
                           and k not in ("stops", "legs", "breaks", "end_arrival")}
            return {**trip_totals, "days": [day]}
        return data

    @property
    def stops(self) -> list[Stop]:
        return [s for d in self.days for s in d.stops]

    @property
    def legs(self) -> list[Leg]:
        return [l for d in self.days for l in d.legs]

    @property
    def breaks(self) -> list[Break]:
        return [b for d in self.days for b in d.breaks]

    @property
    def end_arrival(self) -> datetime:
        return self.days[-1].end_arrival


class Progress(Contract):
    now: datetime | None = None
    location: Coordinate | None = None
    spent_minor: int = Field(default=0, ge=0)
    completed_place_ids: list[str] = Field(default_factory=list)


class AgentMessage(Contract):
    sender: str
    recipient: str
    kind: Literal["finding", "request", "proposal", "validation"]
    summary: str
    place_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class AgentReport(Contract):
    role: Literal["discovery", "conditions", "mobility", "budget_pace"]
    summary: str = Field(max_length=1800)
    candidate_ids: list[str] = Field(default_factory=list, max_length=16)
    avoid_ids: list[str] = Field(default_factory=list, max_length=16)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    requests: list[AgentMessage] = Field(default_factory=list, max_length=6)


class TripSnapshot(Contract):
    id: str
    revision: int = Field(default=0, ge=0)
    request: TripRequest
    places: list[Place] = Field(default_factory=list)
    weather: Weather | None = None
    itinerary: Itinerary | None = None
    progress: Progress = Field(default_factory=Progress)
    closed_place_ids: list[str] = Field(default_factory=list)
    weather_override: Weather | None = None
    reports: list[AgentReport] = Field(default_factory=list)
    messages: list[AgentMessage] = Field(default_factory=list)
    data_mode: Literal["fixture", "live"] = "fixture"
    agent_mode: Literal["rules", "model"] = "rules"


class TripEvent(Contract):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    kind: Literal["rain", "weather_updated", "pace_changed", "budget_changed", "place_unavailable",
                  "stop_completed", "lock_stop", "unlock_stop", "user_running_late", "preferences_changed"]
    payload: dict[str, Any] = Field(default_factory=dict)
    simulated: bool = False


class Proposal(Contract):
    id: str
    trip_id: str
    base_revision: int
    trigger: str
    proposed: TripSnapshot
    added: list[str]
    removed: list[str]
    preserved: list[str]
    status: Literal["pending", "accepted", "rejected"] = "pending"
