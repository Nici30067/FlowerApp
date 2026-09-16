"""Conservative subset of OSM opening_hours. Unsupported expressions return unknown.

Supports 24/7, daily HH:MM-HH:MM, weekday lists/ranges, semicolon rules, off,
and overnight intervals. Public holidays, sunrise/sunset, comments, week and
month rules require a full parser and are deliberately left unknown.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from travel_agent.schemas import Interval

DAYS = {d: i for i, d in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"))}
RULE = re.compile(r"(?:(?P<days>(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:[-,](?:Mo|Tu|We|Th|Fr|Sa|Su))*)\s+)?(?P<times>off|\d{2}:\d{2}-\d{2}:\d{2}(?:,\d{2}:\d{2}-\d{2}:\d{2})*)$")


def _days(value: str | None) -> set[int]:
    if not value:
        return set(range(7))
    result = set()
    for part in value.split(","):
        if "-" in part:
            a, b = [DAYS[x] for x in part.split("-")]
            result.update((a + k) % 7 for k in range((b - a) % 7 + 1))
        else:
            result.add(DAYS[part])
    return result


def parse_hours(raw: str | None, when: datetime, timezone: str) -> list[Interval] | None:
    if not raw:
        return None
    day = when.astimezone(ZoneInfo(timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
    if raw.strip() == "24/7":
        return [Interval(start=day - timedelta(days=1), end=day + timedelta(days=2))]
    parts = [RULE.fullmatch(p.strip()) for p in raw.split(";")]
    if not parts or not all(parts):
        return None
    # A full evaluator is required for overlapping rule overrides. Reject those safely.
    occupied: set[int] = set()
    rules = []
    try:
        for match in parts:
            days = _days(match["days"])
            if occupied & days:
                return None
            occupied |= days
            ranges = []
            if match["times"] != "off":
                for pair in match["times"].split(","):
                    start, end = pair.split("-")
                    sh, sm = map(int, start.split(":"))
                    eh, em = map(int, end.split(":"))
                    if sh > 23 or eh > 24 or sm > 59 or em > 59 or (eh == 24 and em):
                        return None
                    ranges.append((sh * 60 + sm, eh * 60 + em))
            rules.append((days, ranges))
        result = []
        for offset in (-1, 0, 1):
            date = day + timedelta(days=offset)
            for days, ranges in rules:
                if date.weekday() not in days:
                    continue
                for a, b in ranges:
                    if b <= a:
                        b += 1440
                    result.append(Interval(start=date + timedelta(minutes=a), end=date + timedelta(minutes=b)))
        return sorted(result, key=lambda x: x.start)
    except (ValueError, KeyError):
        return None
