from datetime import datetime

import pytest
from pydantic import ValidationError

from travel_agent import intake
from travel_agent.schemas import TripBrief, TripRequest

NOW = datetime(2026, 9, 16, 9, 0)


def test_single_message_captures_most_fields():
    brief = intake.parse(
        "Berlin on 5 jan for 3 days, budget 60 euros, I like art and history", TripBrief(), now=NOW)
    assert brief.city == "Berlin"
    assert brief.date == "2027-01-05"  # next occurrence on/after NOW
    assert brief.days == 3
    assert brief.budget_minor == 6000
    assert set(brief.interests) == {"art", "history"}
    assert intake.missing_fields(brief) == ["time_window"]


def test_word_form_day_counts_are_parsed():
    assert intake.parse("Two days in Berlin", TripBrief(), now=NOW).days == 2
    assert intake.parse("a day trip to Berlin", TripBrief(), now=NOW).days == 1
    assert intake.parse("five nights in Berlin", TripBrief(), now=NOW).days == 5


def test_multi_turn_convergence():
    brief = TripBrief()
    assert intake.next_question(brief) is not None

    brief = intake.parse("Berlin", brief, now=NOW)
    assert brief.city == "Berlin"
    assert "date" in intake.missing_fields(brief)

    brief = intake.parse("5th of January", brief, now=NOW)
    assert brief.date == "2027-01-05"
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


def test_to_trip_request_builds_valid_request():
    brief = TripBrief(city="Berlin", date="2027-01-05", start_time="10:00", end_time="17:00",
                       interests=["art", "history"])
    request = intake.to_trip_request(brief, TripRequest())
    assert request is not None
    assert isinstance(request, TripRequest)
    assert request.start.tzinfo is not None
    assert str(request.start.tzinfo.key if hasattr(request.start.tzinfo, "key") else request.start.tzinfo) \
        or request.timezone == "Europe/Berlin"
    assert request.start.utcoffset() is not None
    assert request.interests == ["art", "history"]


def test_incomplete_brief_returns_none():
    brief = TripBrief(city="Berlin")
    assert intake.to_trip_request(brief, TripRequest()) is None


def test_ambiguous_bare_date_resolves_on_or_after_now():
    brief = intake.parse("5 jan", TripBrief(), now=NOW)
    resolved = datetime.fromisoformat(brief.date)
    assert resolved.date() >= NOW.date()


def test_unsupported_city_flagged():
    brief = intake.parse("I want to go to Paris", TripBrief(), now=NOW)
    assert brief.city == "Paris"
    assert not intake.is_supported_city(brief.city)


def test_trip_brief_forbids_extra_fields():
    with pytest.raises(ValidationError):
        TripBrief(city="Berlin", unknown_field="x")
