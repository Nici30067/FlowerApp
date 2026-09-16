"""Fixture and live adapters with explicit provenance, bounded queries, and no silent fallback."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx

from travel_agent.schemas import Coordinate, Evidence, ForecastInterval, Leg, Place, TripRequest, Weather

from .opening_hours import parse_hours


class ProviderError(RuntimeError):
    pass


class TravelProvider(Protocol):
    mode: str
    def search(self, request: TripRequest) -> list[Place]: ...
    def weather(self, request: TripRequest) -> Weather: ...
    def matrix(self, points: dict[str, Coordinate], mode: str) -> dict[str, Leg]: ...
    def geometry(self, legs: list[Leg], points: dict[str, Coordinate]) -> list[Leg]: ...


def key(a: str, b: str) -> str:
    return a + "|" + b


def haversine(a: Coordinate, b: Coordinate) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    value = math.sin(dp / 2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2)**2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(value)))


class FixtureProvider:
    mode = "fixture"

    def search(self, request: TripRequest) -> list[Place]:
        center = Coordinate(lat=52.5225, lon=13.4024)
        if haversine(center, request.origin) > 10000:
            raise ProviderError("The bundled fixture covers central Berlin only. Choose live mode for another city.")
        raw = json.loads((Path(__file__).parents[1] / "fixtures" / "berlin.json").read_text())
        places = []
        for row in raw["places"]:
            places.append(Place(
                id=row["id"], name=row["name"], coordinate=Coordinate(lat=row["lat"], lon=row["lon"]),
                categories=row["categories"], indoor=row["indoor"], visit_duration_s=row["duration_s"],
                cost_minor=row["cost_minor"], cost_status="fixture", currency=request.currency,
                opening_hours=row["hours"], opening_intervals=parse_hours(row["hours"], request.start, request.timezone),
                opening_status="fixture", description=row["description"],
                source=Evidence(id="fixture-" + row["id"], provider="Bundled synthetic fixture", status="fixture",
                                retrieved_at=request.start, reference="fixtures/berlin.json", note=raw["notice"])))
        return places[:request.max_candidates]

    def weather(self, request: TripRequest) -> Weather:
        t = request.start.replace(minute=0, second=0, microsecond=0)
        intervals = []
        while t < request.end:
            intervals.append(ForecastInterval(start=t, end=t + timedelta(hours=1),
                                              precipitation_probability_pct=10, temperature_c=21))
            t += timedelta(hours=1)
        return Weather(intervals=intervals, source=Evidence(
            id="fixture-weather", provider="Synthetic weather", status="fixture", retrieved_at=request.start,
            note="Scenario fixture. No live forecast has been requested."))

    def matrix(self, points: dict[str, Coordinate], mode: str) -> dict[str, Leg]:
        result = {}
        for a, ca in points.items():
            for b, cb in points.items():
                distance = math.ceil(haversine(ca, cb) * 1.28)
                duration = math.ceil(distance / (1.2 if mode == "walking" else 4.0))
                result[key(a, b)] = Leg(from_id=a, to_id=b, distance_m=distance, duration_s=duration,
                    mode=mode, geometry=[ca.geojson(), cb.geojson()], source_status="fixture",
                    evidence_id="fixture-route-matrix")
        return result

    def geometry(self, legs: list[Leg], points: dict[str, Coordinate]) -> list[Leg]:
        return [leg.model_copy(deep=True) for leg in legs]


class CachedHTTP:
    """In-process TTL cache. Contact and provider endpoints are operator configuration."""
    def __init__(self, contact: str, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=12, follow_redirects=False,
            headers={"User-Agent": f"OSMTravelCompanion/0.1 ({contact})"})
        self.cache: dict[str, tuple[float, dict]] = {}
        self.lock = threading.Lock()
        self.requests = 0

    def request(self, method: str, url: str, *, ttl: int, **kwargs) -> tuple[dict, bool]:
        cache_key = hashlib.sha256(json.dumps([method, url, kwargs], sort_keys=True).encode()).hexdigest()
        with self.lock:
            cached = self.cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < ttl:
                return json.loads(json.dumps(cached[1])), True
        try:
            response = self.client.request(method, url, **kwargs)
            self.requests += 1
            response.raise_for_status()
            if len(response.content) > 4_000_000:
                raise ProviderError("Provider response exceeds the 4 MB limit")
            data = response.json()
            if not isinstance(data, dict):
                raise ProviderError("Provider returned an unexpected JSON shape")
        except (httpx.HTTPError, ValueError) as exc:
            # Avoid displaying API keys, headers, or full request URLs in error messages.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise ProviderError(f"{urlparse(url).hostname} request failed ({status or type(exc).__name__})") from None
        with self.lock:
            if len(self.cache) > 200:
                self.cache.clear()
            self.cache[cache_key] = (time.monotonic(), data)
        return json.loads(json.dumps(data)), False


CATEGORY_FILTERS = {
    "art": [('tourism', 'museum'), ('tourism', 'gallery')],
    "architecture": [('tourism', 'attraction')],
    "history": [('tourism', 'museum'), ('historic', 'memorial')],
    "parks": [('leisure', 'park')],
    "coffee": [('amenity', 'cafe')],
    "food": [('amenity', 'restaurant'), ('amenity', 'cafe')],
    "books": [('shop', 'books')],
    "shopping": [('shop', 'mall')],
}


def parse_cost(tags: dict, currency: str) -> tuple[int | None, str]:
    if tags.get("fee") == "no":
        return 0, "estimated"
    match = re.fullmatch(r"(\d+(?:\.\d{1,2})?)\s*(EUR|USD|GBP|SEK)", tags.get("charge", "").strip())
    if match and match[2] == currency:
        return int(Decimal(match[1]) * 100), "estimated"
    return None, "unknown"


class LiveProvider:
    mode = "live"

    def __init__(self, *, contact: str, ors_key: str, allow_public_overpass: bool = False,
                 overpass_url: str = "https://overpass-api.de/api/interpreter",
                 ors_url: str = "https://api.openrouteservice.org",
                 weather_url: str = "https://api.open-meteo.com/v1/forecast",
                 client: httpx.Client | None = None):
        if not contact.strip() or not ors_key.strip():
            raise ProviderError("Live mode requires TRAVEL_CONTACT and ORS_API_KEY")
        if urlparse(overpass_url).hostname == "overpass-api.de" and not allow_public_overpass:
            raise ProviderError("Explicitly enable TRAVEL_ALLOW_PUBLIC_OVERPASS or configure your own instance")
        for url in (overpass_url, ors_url, weather_url):
            if urlparse(url).scheme != "https":
                raise ProviderError("Live provider endpoints require HTTPS")
        self.http = CachedHTTP(contact, client)
        self.ors_key = ors_key
        self.overpass_url, self.ors_url, self.weather_url = overpass_url, ors_url.rstrip("/"), weather_url

    def search(self, request: TripRequest) -> list[Place]:
        filters = set()
        for category in request.interests:
            filters.update(CATEGORY_FILTERS.get(category, []))
        # Indoor alternatives are always retrieved for weather-driven repair.
        filters.update(CATEGORY_FILTERS["art"] + CATEGORY_FILTERS["coffee"])
        lat, lon = request.origin.lat, request.origin.lon
        radius = 2500
        queries = [f'nwr["{k}"="{v}"](around:{radius},{lat:.6f},{lon:.6f});' for k, v in sorted(filters)]
        query = '[out:json][timeout:12];(' + ''.join(queries) + ');out center tags 120;'
        raw, cached = self.http.request("POST", self.overpass_url, ttl=900, data={"data": query})
        now = datetime.now(UTC)
        found = []
        for el in raw.get("elements", [])[:120]:
            tags = el.get("tags", {})
            coord = el.get("center", el)
            if not tags.get("name") or "lat" not in coord or "lon" not in coord:
                continue
            point = Coordinate(lat=coord["lat"], lon=coord["lon"])
            indoor = None
            if tags.get("indoor") == "yes" or tags.get("tourism") in ("museum", "gallery"):
                indoor = True
            elif tags.get("leisure") == "park" or tags.get("indoor") == "no":
                indoor = False
            categories = [name for name, fs in CATEGORY_FILTERS.items() if any(tags.get(k) == v for k, v in fs)]
            cost, status = parse_cost(tags, request.currency)
            pid = f'osm-{el["type"]}-{el["id"]}'
            found.append(Place(id=pid, name=tags["name"][:200], coordinate=point, categories=categories,
                indoor=indoor, visit_duration_s=1800 if "coffee" in categories else 2700,
                cost_minor=cost, cost_status=status, currency=request.currency, opening_hours=tags.get("opening_hours"),
                opening_intervals=parse_hours(tags.get("opening_hours"), request.start, request.timezone),
                opening_status="tag" if tags.get("opening_hours") else "unknown",
                osm_type=el["type"], osm_id=el["id"], website=tags.get("website"),
                description="OSM record. Visit duration is an application planning assumption; entrance is unverified.",
                source=Evidence(id=pid, provider="OpenStreetMap via Overpass", status="cached" if cached else "live",
                    retrieved_at=now, reference=f'https://www.openstreetmap.org/{el["type"]}/{el["id"]}',
                    note="Cached responses may be up to 15 minutes old. Prices and access are unverified OSM tags.")))
        found.sort(key=lambda p: (-len(set(p.categories) & set(request.interests)), haversine(request.origin, p.coordinate)))
        return found[:request.max_candidates]

    def weather(self, request: TripRequest) -> Weather:
        now = datetime.now(UTC)
        if request.start.date() < now.date() or request.end.date() > (now + timedelta(days=15)).date():
            return Weather(intervals=[], source=Evidence(id="weather-outside-horizon", provider="Open-Meteo",
                status="unknown", retrieved_at=now, note="Trip falls outside the supported forecast window."))
        raw, cached = self.http.request("GET", self.weather_url, ttl=600, params={
            "latitude": request.origin.lat, "longitude": request.origin.lon,
            "hourly": "temperature_2m,precipitation_probability", "timezone": "UTC", "timeformat": "unixtime",
            "start_date": request.start.date().isoformat(), "end_date": request.end.date().isoformat()})
        hourly = raw.get("hourly", {})
        result = []
        for i, ts in enumerate(hourly.get("time", [])):
            probabilities = hourly.get("precipitation_probability", [])
            temperatures = hourly.get("temperature_2m", [])
            if i >= len(probabilities) or probabilities[i] is None:
                continue
            # Provider defines precipitation probability over the preceding hour.
            end = datetime.fromtimestamp(ts, UTC)
            result.append(ForecastInterval(start=end - timedelta(hours=1), end=end,
                precipitation_probability_pct=probabilities[i],
                temperature_c=temperatures[i] if i < len(temperatures) else None))
        return Weather(intervals=result, source=Evidence(id="weather-" + now.strftime("%Y%m%dT%H%M%S"),
            provider="Open-Meteo", status="cached" if cached else "live", retrieved_at=now,
            reference="https://open-meteo.com/", note="Forecast near trip origin; cache lifetime 10 minutes."))

    @staticmethod
    def profile(mode: str) -> str:
        return {"walking": "foot-walking", "cycling": "cycling-regular"}[mode]

    def matrix(self, points: dict[str, Coordinate], mode: str) -> dict[str, Leg]:
        ids = list(points)
        raw, cached = self.http.request("POST", f"{self.ors_url}/v2/matrix/{self.profile(mode)}", ttl=900,
            headers={"Authorization": self.ors_key}, json={"locations": [p.geojson() for p in points.values()],
                                                         "metrics": ["distance", "duration"], "units": "m"})
        result = {}
        try:
            for i, a in enumerate(ids):
                for j, b in enumerate(ids):
                    distance, duration = raw["distances"][i][j], raw["durations"][i][j]
                    if distance is None or duration is None:
                        continue
                    result[key(a, b)] = Leg(from_id=a, to_id=b, distance_m=math.ceil(distance),
                        duration_s=math.ceil(duration), mode=mode, geometry=[],
                        source_status="cached" if cached else "live", evidence_id="ors-matrix")
        except (KeyError, TypeError, IndexError) as exc:
            raise ProviderError("Routing matrix response is incomplete") from exc
        return result

    def geometry(self, legs: list[Leg], points: dict[str, Coordinate]) -> list[Leg]:
        if not legs:
            return []
        # Coincident points are legal in the matrix, but some directions servers reject them.
        result = []
        for leg in legs:
            a, b = points[leg.from_id], points[leg.to_id]
            if haversine(a, b) < 1:
                result.append(leg.model_copy(update={"geometry": [a.geojson(), b.geojson()]}))
                continue
            raw, cached = self.http.request("POST", f"{self.ors_url}/v2/directions/{self.profile(leg.mode)}/geojson",
                ttl=900, headers={"Authorization": self.ors_key},
                json={"coordinates": [a.geojson(), b.geojson()], "instructions": False})
            try:
                feature = raw["features"][0]
                summary = feature["properties"]["summary"]
                result.append(Leg(from_id=leg.from_id, to_id=leg.to_id,
                    distance_m=math.ceil(summary["distance"]), duration_s=math.ceil(summary["duration"]),
                    mode=leg.mode, geometry=feature["geometry"]["coordinates"],
                    source_status="cached" if cached else "live", evidence_id="ors-directions"))
            except (KeyError, TypeError, IndexError) as exc:
                raise ProviderError("Directions response is incomplete") from exc
        return result


def make_provider(mode: str) -> TravelProvider:
    if mode == "fixture":
        return FixtureProvider()
    if mode != "live":
        raise ProviderError("Unknown data mode")
    return LiveProvider(contact=os.getenv("TRAVEL_CONTACT", ""), ors_key=os.getenv("ORS_API_KEY", ""),
        allow_public_overpass=os.getenv("TRAVEL_ALLOW_PUBLIC_OVERPASS", "false").lower() == "true",
        overpass_url=os.getenv("TRAVEL_OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
        ors_url=os.getenv("ORS_BASE_URL", "https://api.openrouteservice.org"),
        weather_url=os.getenv("OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast"))
