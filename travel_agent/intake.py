"""Deterministic, rules-based conversational intake for trip planning.

Pure functions: no I/O, no FastAPI, no model calls. Given free text and a
partially-filled TripBrief, extract whatever can be extracted, decide what is
still missing, ask one clarifying question at a time, and finally build a
concrete TripRequest once the brief is complete.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from travel_agent.schemas import TripBrief, TripRequest

# Only Berlin has fixture place data; there is no geocoder in this codebase.
SUPPORTED_CITIES: dict[str, str] = {"berlin": "Berlin"}

_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})

_WEEKDAYS = {name.lower(): i for i, name in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}

# Interest vocabulary matches the chip set in travel_agent/web/app.js's TYPES array.
_INTEREST_TAGS = ("art", "architecture", "parks", "coffee", "history", "food", "books", "shopping")

_INTEREST_WORDS: dict[str, str] = {
    "art": "art", "arts": "art", "museum": "art", "museums": "art", "gallery": "art", "galleries": "art",
    "architecture": "architecture", "buildings": "architecture",
    "park": "parks", "parks": "parks", "garden": "parks", "gardens": "parks", "nature": "parks",
    "coffee": "coffee", "cafe": "coffee", "cafes": "coffee", "cafés": "coffee",
    "history": "history", "historic": "history", "historical": "history", "heritage": "history",
    "food": "food", "eating": "food", "restaurants": "food", "cuisine": "food",
    "book": "books", "books": "books", "bookshop": "books", "bookshops": "books", "reading": "books",
    "shopping": "shopping", "shops": "shopping", "shop": "shopping", "markets": "shopping", "market": "shopping",
    "views": "architecture", "view": "architecture",
}

_REPLACE_SIGNALS = re.compile(r"\b(actually|instead|not\s+\w+\s+but|just|only|rather)\b", re.IGNORECASE)


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _parse_city(text: str, brief: TripBrief) -> str | None:
    lower = text.lower()
    for key, name in SUPPORTED_CITIES.items():
        if key in lower:
            return name
    # Capture an unsupported city mention too, e.g. "I want to go to Paris".
    match = re.search(r"\b(?:to|visit|visiting|in)\s+([A-Z][a-zA-Z]+)\b", text)
    if match:
        candidate = match.group(1)
        if candidate.lower() not in SUPPORTED_CITIES and candidate.lower() not in ("berlin",):
            return candidate
    return brief.city


def is_supported_city(city: str | None) -> bool:
    return city is not None and city.lower() in SUPPORTED_CITIES


def _next_on_or_after(now: date, month: int, day: int, year: int | None) -> date:
    if year is not None:
        return date(year, month, day)
    candidate = date(now.year, month, day)
    if candidate < now:
        candidate = date(now.year + 1, month, day)
    return candidate


def _parse_date(text: str, now: datetime) -> str | None:
    lower = text.lower()
    today = now.date()
    if re.search(r"\btoday\b", lower):
        return today.isoformat()
    if re.search(r"\btomorrow\b", lower):
        return (today + timedelta(days=1)).isoformat()
    next_weekday = re.search(r"\bnext\s+(" + "|".join(_WEEKDAYS) + r")\b", lower)
    if next_weekday:
        target = _WEEKDAYS[next_weekday.group(1)]
        delta = (target - today.weekday()) % 7
        delta = delta + 7 if delta == 0 else delta
        return (today + timedelta(days=delta)).isoformat()
    bare_weekday = re.search(r"\bon\s+(" + "|".join(_WEEKDAYS) + r")\b", lower)
    if bare_weekday:
        target = _WEEKDAYS[bare_weekday.group(1)]
        delta = (target - today.weekday()) % 7
        return (today + timedelta(days=delta)).isoformat()

    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso:
        return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).isoformat()

    # "5 jan[uary] [2027]" / "5th of january"
    day_month = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?(" + "|".join(_MONTHS) + r")\.?\s*(\d{4})?\b", lower)
    if day_month:
        day = int(day_month.group(1))
        month = _MONTHS[day_month.group(2)]
        year = int(day_month.group(3)) if day_month.group(3) else None
        try:
            return _next_on_or_after(today, month, day, year).isoformat()
        except ValueError:
            return None

    # "jan 5[, 2027]" / "january 5th"
    month_day = re.search(
        r"\b(" + "|".join(_MONTHS) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?\b", lower)
    if month_day:
        month = _MONTHS[month_day.group(1)]
        day = int(month_day.group(2))
        year = int(month_day.group(3)) if month_day.group(3) else None
        try:
            return _next_on_or_after(today, month, day, year).isoformat()
        except ValueError:
            return None
    return None


def _parse_days(text: str) -> int | None:
    match = re.search(r"\b(\d{1,2})\s*(day|days|night|nights)\b", text.lower())
    if match:
        return _clamp(int(match.group(1)), 1, 14)
    return None


def _to_24h(hour: int, minute: int, meridiem: str | None) -> tuple[int, int]:
    hour = hour % 12
    if meridiem and meridiem.lower() == "pm":
        hour += 12
    elif meridiem is None:
        pass
    return hour, minute


def _parse_times(text: str) -> tuple[str | None, str | None]:
    pattern = re.compile(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:to|until|-|–)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
        re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return None, None
    h1, m1, ap1, h2, m2, ap2 = match.groups()
    start_h, start_m = int(h1), int(m1) if m1 else 0
    end_h, end_m = int(h2), int(m2) if m2 else 0
    if ap1:
        start_h = start_h % 12 + (12 if ap1.lower() == "pm" else 0)
    if ap2:
        end_h = end_h % 12 + (12 if ap2.lower() == "pm" else 0)
    if not ap1 and not ap2 and end_h <= start_h:
        end_h = end_h % 12 + 12
    return f"{start_h:02d}:{start_m:02d}", f"{end_h:02d}:{end_m:02d}"


def _parse_single_time_correction(text: str) -> str | None:
    """A bare time mention with no range ("actually make it 3pm") is treated as a
    correction to the start time — the common case when refining a previously-set
    window rather than giving a fresh one."""
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.IGNORECASE)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == "pm" else 0)
    minute = int(minute) if minute else 0
    return f"{hour:02d}:{minute:02d}"


def _parse_budget(text: str) -> int | None:
    lower = text.lower()
    match = re.search(r"€\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:eur|euros?)\b", lower)
    if not match:
        match = re.search(r"budget\s*(?:of|is|:)?\s*(\d+(?:\.\d+)?)", lower)
    if match:
        amount = float(match.group(1) or match.group(2))
        return _clamp(int(round(amount * 100)), 0, 1000000)
    if re.search(r"\bcheap\b", lower):
        return 4000
    return None


def _parse_interests(text: str, existing: list[str]) -> list[str]:
    lower = text.lower()
    found: set[str] = set()
    for word, tag in _INTEREST_WORDS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", lower):
            found.add(tag)
    if not found:
        return existing
    if _REPLACE_SIGNALS.search(text):
        return sorted(found)
    return sorted(set(existing) | found)


def _parse_walking(text: str) -> int | None:
    lower = text.lower()
    match = re.search(r"(\d+(?:\.\d+)?)\s*km\b", lower)
    if match:
        return _clamp(int(round(float(match.group(1)) * 1000)), 0, 50000)
    if re.search(r"no long walks|not much walking|don't want to walk much|minimal walking", lower):
        return 3000
    return None


def _parse_stops(text: str) -> int | None:
    match = re.search(r"\b(\d{1,2})\s*(?:stops|places)\b", text.lower())
    if match:
        # Clamped to 2..8: TripRequest's default min_stops is 2, and target_stops
        # must be >= min_stops, so anything lower would fail validation downstream.
        return _clamp(int(match.group(1)), 2, 8)
    return None


def _parse_transport(text: str) -> str | None:
    if re.search(r"\b(bike|bikes|biking|cycle|cycling)\b", text.lower()):
        return "cycling"
    return None


def _parse_rain(text: str) -> bool | None:
    if re.search(r"don'?t care about (the )?rain|rain doesn'?t matter|rain does not matter", text.lower()):
        return False
    return None


def parse(message: str, brief: TripBrief, now: datetime | None = None) -> TripBrief:
    """Extract whatever can be extracted from `message` and merge it onto `brief`,
    returning a new TripBrief. Newly parsed non-null scalar values overwrite the
    old ones; interests are unioned unless the message signals a replacement."""
    now = now or datetime.now()
    data = brief.model_dump()

    city = _parse_city(message, brief)
    if city is not None:
        data["city"] = city

    parsed_date = _parse_date(message, now)
    if parsed_date is not None:
        data["date"] = parsed_date

    days = _parse_days(message)
    if days is not None:
        data["days"] = days

    start_time, end_time = _parse_times(message)
    if start_time is not None:
        data["start_time"] = start_time
    if end_time is not None:
        data["end_time"] = end_time
    if start_time is None and end_time is None and brief.start_time and brief.end_time:
        correction = _parse_single_time_correction(message)
        if correction is not None:
            data["start_time"] = correction

    budget = _parse_budget(message)
    if budget is not None:
        data["budget_minor"] = budget

    data["interests"] = _parse_interests(message, brief.interests)

    walking = _parse_walking(message)
    if walking is not None:
        data["max_walking_m"] = walking

    stops = _parse_stops(message)
    if stops is not None:
        data["target_stops"] = stops

    transport = _parse_transport(message)
    if transport is not None:
        data["transport_mode"] = transport

    rain = _parse_rain(message)
    if rain is not None:
        data["avoid_rain_outdoor_visits"] = rain

    return TripBrief(**data)


REQUIRED_FIELDS = ("city", "date", "time_window", "interests")


def missing_fields(brief: TripBrief) -> list[str]:
    missing = []
    if not brief.city:
        missing.append("city")
    if not brief.date:
        missing.append("date")
    if not brief.start_time or not brief.end_time:
        missing.append("time_window")
    if not brief.interests:
        missing.append("interests")
    return missing


def next_question(brief: TripBrief) -> str | None:
    missing = missing_fields(brief)
    if not missing:
        return None
    if "city" in missing:
        return "Which city are you visiting? Right now I can plan trips to Berlin."
    if brief.city and not is_supported_city(brief.city):
        supported = ", ".join(SUPPORTED_CITIES.values())
        return f"I can only plan {supported} right now — want a Berlin day instead?"
    if "date" in missing:
        return "What date would you like to travel?"
    if "time_window" in missing:
        return "What time should the day start and end?"
    if "interests" in missing:
        return "What are you interested in — art, history, parks, food, coffee, books, shopping, architecture?"
    return None


def to_trip_request(brief: TripBrief, defaults: TripRequest) -> TripRequest | None:
    """Build a full TripRequest from a complete brief, layered onto `defaults`
    for anything the brief left unset. Returns None if the brief is still
    incomplete or its city is unsupported — never produces a broken request."""
    if missing_fields(brief) or not is_supported_city(brief.city):
        return None

    tz_name = brief.timezone or defaults.timezone or "Europe/Berlin"
    tz = ZoneInfo(tz_name)
    year, month, day = (int(part) for part in brief.date.split("-"))
    start_h, start_m = (int(part) for part in brief.start_time.split(":"))
    end_h, end_m = (int(part) for part in brief.end_time.split(":"))
    start = datetime(year, month, day, start_h, start_m, tzinfo=tz)
    end = datetime(year, month, day, end_h, end_m, tzinfo=tz)
    if end <= start:
        end = end + timedelta(days=1)

    update = {
        "city": brief.city,
        "timezone": tz_name,
        "start": start,
        "end": end,
        "interests": brief.interests or list(defaults.interests),
    }
    if brief.days is not None:
        update["days"] = brief.days
    if brief.budget_minor is not None:
        update["budget_minor"] = brief.budget_minor
    if brief.max_walking_m is not None:
        update["max_walking_m"] = brief.max_walking_m
    if brief.transport_mode is not None:
        update["transport_mode"] = brief.transport_mode
    if brief.target_stops is not None:
        update["target_stops"] = brief.target_stops
    if brief.avoid_rain_outdoor_visits is not None:
        update["avoid_rain_outdoor_visits"] = brief.avoid_rain_outdoor_visits
    if brief.title is not None:
        update["title"] = brief.title

    return defaults.model_copy(update=update)
