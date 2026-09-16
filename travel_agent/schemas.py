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
    start: datetime = datetime.fromisoformat("2026-09-16T10:00:00+02:00")
    end: datetime = datetime.fromisoformat("2026-09-16T17:00:00+02:00")
    origin: Coordinate = Field(default_factory=lambda: Coordinate(lat=52.5225, lon=13.4024))
    destination: Coordinate = Field(default_factory=lambda: Coordinate(lat=52.5225, lon=13.4024))
    interests: list[str] = Field(default_factory=lambda: ["art", "architecture", "parks", "coffee"])
    currency: Literal["EUR", "USD", "GBP", "SEK"] = "EUR"
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


class Validation(Contract):
    valid: bool
    status: Literal["valid", "provisional", "infeasible"]
    issues: list[Issue] = Field(default_factory=list)


class Itinerary(Contract):
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
