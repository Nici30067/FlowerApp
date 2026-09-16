"""Offline tests for the conversational intake: parsing, questions, city support through a fake lookup, and
the single-day TripRequest it builds. No test touches the network."""
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from travel_agent import intake
from travel_agent.providers.services import ProviderError
from travel_agent.schemas import TripBrief, TripRequest

NOW = datetime(2026, 9, 16, 9, 0)
TOKYO = {"name": "Tokyo", "lat": 35.6895, "lon": 139.69171, "timezone": "Asia/Tokyo", "country": "Japan"}
PARIS = {"name": "Paris", "lat": 48.85341, "lon": 2.3488, "timezone": "Europe/Paris", "country": "France"}
KREUZBERG = {"name": "Kreuzberg", "lat": 52.49973, "lon": 13.40338, "timezone": "Europe/Berlin", "country": "Germany"}
POTSDAM = {"name": "Potsdam", "lat": 52.39886, "lon": 13.06566, "timezone": "Europe/Berlin", "country": "Germany"}
COMPLETE = {"city": "Berlin", "date": "2027-01-05", "start_time": "10:00", "end_time": "17:00",
            "interests": ["art", "history"]}


def test_single_message_captures_most_fields():
    brief = intake.parse("Berlin on 5 jan for 3 days, budget 60 euros, I like art and history", TripBrief(), now=NOW)
    assert brief.city == "Berlin"
    assert brief.date == "2027-01-05"  # the next 5 January on or after NOW
    assert brief.budget_minor == 6000
    assert set(brief.interests) == {"art", "history"}
    assert intake.missing_fields(brief) == ["time_window"]


def test_day_counts_are_a_note_not_a_field():
    assert intake.parse("Two days in Berlin", TripBrief(), now=NOW).city == "Berlin"
    assert intake.multi_day_mention("Two days in Berlin") == 2
    assert intake.multi_day_mention("five nights in Berlin") == 5
    assert intake.multi_day_mention("3 days, budget 60") == 3
    assert intake.multi_day_mention("a day trip to Berlin") is None
    assert intake.multi_day_mention("one day in Berlin, 10 to 5") is None
    with pytest.raises(ValidationError):
        TripBrief(days=2)


def test_multi_turn_convergence():
    brief = TripBrief()
    assert intake.next_question(brief) is not None

    brief = intake.parse("Berlin", brief, now=NOW)
    assert brief.city == "Berlin"
    assert "date" in intake.missing_fields(brief)

    brief = intake.parse("5th of January", brief, now=NOW)
    assert brief.date == "2027-01-05"
    assert brief.city == "Berlin"
    assert "time_window" in intake.missing_fields(brief)

    brief = intake.parse("10 to 5", brief, now=NOW)
    assert brief.start_time == "10:00"
    assert brief.end_time == "17:00"
    assert "interests" in intake.missing_fields(brief)

    brief = intake.parse("art and parks", brief, now=NOW)
    assert set(brief.interests) == {"art", "parks"}
    assert intake.missing_fields(brief) == []
    assert intake.next_question(brief) is None


def test_correction_updates_only_that_field():
    brief = TripBrief(city="Berlin", date="2027-01-05", start_time="10:00", end_time="17:00", interests=["art"])
    updated = intake.parse("actually make it 3pm", brief, now=NOW)
    assert updated.start_time == "15:00"
    assert updated.city == "Berlin"
    assert updated.end_time == "17:00"
    assert updated.interests == ["art"]


@pytest.mark.parametrize("message, city", [
    ("I want to go to Paris", "Paris"),
    ("New York next Friday, 10 to 5", "New York"),
    ("Museums and parks in Zürich", "Zürich"),
    ("Let's do Lisbon", "Lisbon"),
    ("Munich please", "Munich"),
    ("paris", "Paris"),
    ("new york", "New York"),
    ("Sure, Lisbon", "Lisbon"),
    ("berlin tomorrow", "Berlin"),
    ("Two days in Berlin", "Berlin"),
    ("art and history", None),
    ("actually make it 3pm", None),
    ("5th of January", None),
    ("in January", None),
    ("On Monday", None),
    ("10 to 5", None),
    ("Tomorrow", None),
    ("something cheap", None),
])
def test_city_extraction(message, city):
    assert intake.parse(message, TripBrief(), now=NOW).city == city


def test_a_later_message_without_a_city_keeps_the_city():
    brief = intake.parse("art and coffee, 10 to 5", TripBrief(city="Paris"), now=NOW)
    assert brief.city == "Paris" and brief.start_time == "10:00" and brief.interests == ["art", "coffee"]


def test_a_bare_lower_case_answer_only_counts_while_no_city_is_known():
    assert intake.parse("lisbon", TripBrief(), now=NOW).city == "Lisbon"
    assert intake.parse("lisbon", TripBrief(city="Paris"), now=NOW).city == "Paris"
    assert intake.parse("Lisbon instead", TripBrief(city="Paris"), now=NOW).city == "Lisbon"


def test_iso_date_is_not_mistaken_for_a_time_window():
    brief = intake.parse("Berlin 2027-01-05", TripBrief(), now=NOW)
    assert brief.date == "2027-01-05" and brief.start_time is None and brief.end_time is None


def test_ambiguous_bare_date_resolves_on_or_after_now():
    brief = intake.parse("5 jan", TripBrief(), now=NOW)
    assert datetime.fromisoformat(brief.date).date() >= NOW.date()


def test_to_trip_request_builds_a_valid_single_day_request():
    request = intake.to_trip_request(TripBrief(**COMPLETE), TripRequest())
    assert isinstance(request, TripRequest)
    assert request.city == "Berlin" and request.title == "A day in Berlin" and request.timezone == "Europe/Berlin"
    assert request.start.isoformat() == "2027-01-05T10:00:00+01:00"
    assert request.end.isoformat() == "2027-01-05T17:00:00+01:00"
    assert request.interests == ["art", "history"]
    assert (request.origin.lat, request.origin.lon) == (52.5225, 13.4024)  # the fixture centre: no place given
    assert not hasattr(request, "days")


def test_to_trip_request_takes_origin_timezone_and_name_from_the_geocoded_place(geo):
    brief = TripBrief(**{**COMPLETE, "city": "tokyo", "budget_minor": 4000, "target_stops": 1,
                         "transport_mode": "cycling", "max_walking_m": 3000, "avoid_rain_outdoor_visits": False})
    request = intake.to_trip_request(brief, TripRequest(), geo(**TOKYO))
    assert request is not None
    assert request.city == "Tokyo" and request.title == "A day in Tokyo" and request.timezone == "Asia/Tokyo"
    assert request.start.utcoffset() == timedelta(hours=9)
    assert (request.origin.lat, request.origin.lon) == (35.6895, 139.69171) and request.destination == request.origin
    assert request.budget_minor == 4000 and request.transport_mode == "cycling" and request.max_walking_m == 3000
    assert request.avoid_rain_outdoor_visits is False
    assert request.target_stops == request.min_stops  # a one-stop wish is raised to the planner's minimum


def test_to_trip_request_keeps_an_explicit_title_and_timezone():
    brief = TripBrief(**{**COMPLETE, "title": "Museum crawl", "timezone": "Europe/Paris"})
    request = intake.to_trip_request(brief, TripRequest())
    assert request.title == "Museum crawl" and request.timezone == "Europe/Paris"
    assert request.start.isoformat() == "2027-01-05T10:00:00+01:00"


def test_to_trip_request_never_returns_a_broken_request():
    assert intake.to_trip_request(TripBrief(city="Berlin"), TripRequest()) is None  # incomplete
    overnight = TripBrief(**{**COMPLETE, "start_time": "09:00", "end_time": "08:00"})  # 23 hours
    assert intake.to_trip_request(overnight, TripRequest()) is None
    bad_zone = TripBrief(**{**COMPLETE, "timezone": "Mars/Olympus"})
    assert intake.to_trip_request(bad_zone, TripRequest()) is None


def test_an_end_before_the_start_rolls_into_the_next_day():
    late = TripBrief(**{**COMPLETE, "start_time": "20:00", "end_time": "01:00"})
    request = intake.to_trip_request(late, TripRequest())
    assert request is not None and request.end.isoformat() == "2027-01-06T01:00:00+01:00"


def test_city_support_in_fixture_mode(geo, fake_geocoder):
    # Berlin needs no lookup at all, so the offline demo keeps working without a network.
    assert intake.is_supported_city("Berlin") is True
    down = fake_geocoder(error=ProviderError("geocoding-api.open-meteo.com request failed (ConnectError)"))
    assert intake.resolve_city("berlin", down.search, "fixture").supported is True and down.queries == []
    # Other names are geocoded and must lie within the bundled fixture's radius of central Berlin.
    inside = intake.resolve_city("Kreuzberg", fake_geocoder(geo(**KREUZBERG)).search, "fixture")
    assert inside.supported and inside.status == "resolved" and inside.place.name == "Kreuzberg" and inside.note == ""
    outside = intake.resolve_city("Potsdam", fake_geocoder(geo(**POTSDAM)).search, "fixture")
    assert not outside.supported and outside.status == "outside_fixture" and "serve_live.sh" in outside.note
    paris = intake.resolve_city("Paris", fake_geocoder(geo(**PARIS)).search, "fixture")
    assert not paris.supported and "Paris, France" in paris.note and paris.place.name == "Paris"
    assert intake.is_supported_city("Paris", fake_geocoder(geo(**PARIS)).search, "fixture") is False
    # No match, an outage and a rejected query are three different answers.
    unknown = intake.resolve_city("Atlantis", fake_geocoder(None).search, "fixture")
    assert unknown.status == "unresolved" and "Atlantis" in unknown.note and not unknown.supported
    outage = intake.resolve_city("Paris", down.search, "fixture")
    assert outage.status == "unavailable" and "Berlin" in outage.note and not outage.supported
    refused = fake_geocoder(error=ValueError("Enter a place name of 2 to 80 characters"))
    rejected = intake.resolve_city("X", refused.search)
    assert rejected.status == "unresolved" and "2 to 80" in rejected.note
    assert intake.resolve_city(None).status == "missing" and intake.is_supported_city("  ") is False


def test_city_support_in_live_mode(geo, fake_geocoder):
    tokyo = intake.resolve_city("Tokyo", fake_geocoder(geo(**TOKYO)).search, "live")
    assert tokyo.supported and tokyo.status == "resolved" and tokyo.place.timezone == "Asia/Tokyo"
    assert intake.is_supported_city("Paris", fake_geocoder(geo(**PARIS)).search, "live") is True
    assert intake.is_supported_city("Atlantis", fake_geocoder(None).search, "live") is False
    # Live mode geocodes Berlin too, but an outage still leaves the default (central Berlin) request usable.
    down = fake_geocoder(error=ProviderError("down"))
    assert intake.resolve_city("Berlin", down.search, "live").supported is True and down.queries == ["Berlin"]
    outage = intake.resolve_city("Tokyo", down.search, "live")
    assert outage.status == "unavailable" and "Berlin" not in outage.note
    # Without any geocoder only the fixture city can be planned.
    assert intake.is_supported_city("Tokyo", None, "live") is False


def test_next_question_explains_city_problems(geo, fake_geocoder):
    paris = intake.resolve_city("Paris", fake_geocoder(geo(**PARIS)).search, "fixture")
    assert "Berlin" in intake.next_question(TripBrief(city="Paris"), paris)
    unknown = intake.resolve_city("Atlantis", fake_geocoder(None).search, "fixture")
    assert "Atlantis" in intake.next_question(TripBrief(), unknown)
    assert "Berlin" in intake.next_question(TripBrief(), data_mode="fixture")
    assert "any city" in intake.next_question(TripBrief(), data_mode="live").lower()
    assert "date" in intake.next_question(TripBrief(city="Berlin"))
    assert intake.next_question(TripBrief(**COMPLETE)) is None


def test_trip_brief_validates_its_shape():
    with pytest.raises(ValidationError):
        TripBrief(city="Berlin", unknown_field="x")
    for field, value in (("date", "January 5"), ("start_time", "25:00"), ("end_time", "9am"), ("budget_minor", -1),
                         ("target_stops", 9), ("transport_mode", "teleport"), ("title", "")):
        with pytest.raises(ValidationError):
            TripBrief(**{field: value})
    assert TripBrief(**COMPLETE).model_dump()["interests"] == ["art", "history"]
