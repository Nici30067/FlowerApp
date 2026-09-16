"""Fixture and live adapters with explicit provenance, bounded queries, and no silent fallback."""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from travel_agent.schemas import (
    Coordinate,
    Evidence,
    ForecastInterval,
    GeoPlace,
    Leg,
    Place,
    TripRequest,
    Weather,
)
from travel_agent.settings import (
    DEFAULT_GEOCODER_URL,
    DEFAULT_ORS_URL,
    DEFAULT_OSRM_URL_TEMPLATE,
    DEFAULT_OVERPASS_URL,
    DEFAULT_WEATHER_URL,
    ROUTERS,
    ProviderSettings,
)

from .opening_hours import parse_hours

# The defaults live in travel_agent.settings (stdlib only, shared with the AgentApp); these names are kept for callers.
OPEN_METEO_GEOCODING_URL = DEFAULT_GEOCODER_URL
OSRM_URL_TEMPLATE = DEFAULT_OSRM_URL_TEMPLATE
USER_AGENT = "OSMTravelCompanion/0.2"
FIXTURE_CENTER = Coordinate(lat=52.5225, lon=13.4024)
FIXTURE_RADIUS_M = 10000
_CODE = re.compile(r"[A-Za-z0-9_]{1,40}")


class ProviderError(RuntimeError):
    """A provider failed or is misconfigured. `status` is the HTTP status, `code` a short provider error code."""
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status, self.code = status, code


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


def fixture_supported(coordinate: Coordinate) -> bool:
    """True when the bundled synthetic fixture covers a trip origin (within 10 km of central Berlin)."""
    return haversine(FIXTURE_CENTER, coordinate) <= FIXTURE_RADIUS_M


class FixtureProvider:
    mode = "fixture"

    def search(self, request: TripRequest) -> list[Place]:
        if not fixture_supported(request.origin):
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


def error_code(response: httpx.Response | None) -> str | None:
    """A short provider error code from a small JSON error body (OSRM style); never free text."""
    if response is None or len(response.content) > 65536:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) and _CODE.fullmatch(code) else None


RETRY_STATUSES = {429, 502, 503, 504}
RETRY_DELAYS_S = (2.0, 5.0)


class CachedHTTP:
    """In-process TTL cache with bounded retries. Contact and provider endpoints are operator configuration."""
    def __init__(self, contact: str = "", client: httpx.Client | None = None, timeout: float = 12):
        agent = f"{USER_AGENT} ({contact.strip()})" if contact.strip() else USER_AGENT
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": agent})
        self.cache: dict[str, tuple[float, dict]] = {}
        self.lock = threading.Lock()
        self.requests = 0
        self.sleep = time.sleep  # replaced in tests

    def request(self, method: str, url: str, *, ttl: int, timeout: float | None = None, **kwargs) -> tuple[dict, bool]:
        # The per-request timeout is transport configuration, not part of the response identity.
        cache_key = hashlib.sha256(json.dumps([method, url, kwargs], sort_keys=True).encode()).hexdigest()
        with self.lock:
            cached = self.cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < ttl:
                return json.loads(json.dumps(cached[1])), True
        if timeout is not None:
            kwargs["timeout"] = timeout
        attempt = 0
        while True:
            try:
                response = self.client.request(method, url, **kwargs)
                self.requests += 1
                response.raise_for_status()
                if len(response.content) > 4_000_000:
                    raise ProviderError("Provider response exceeds the 4 MB limit")
                data = response.json()
                if not isinstance(data, dict):
                    raise ProviderError("Provider returned an unexpected JSON shape")
                break
            except (httpx.HTTPError, ValueError) as exc:
                # Shared public instances answer 429/5xx when busy; retry a bounded number of times with backoff.
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                budget = (len(RETRY_DELAYS_S) if status in RETRY_STATUSES
                          else 1 if isinstance(exc, httpx.TimeoutException) else 0)
                if attempt < budget:
                    self.sleep(RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S) - 1)])
                    attempt += 1
                    continue
                # Avoid displaying API keys, headers, or full request URLs in error messages.
                raise ProviderError(f"{urlparse(url).hostname} request failed ({status or type(exc).__name__})",
                                    status=status, code=error_code(response)) from None
        with self.lock:
            if len(self.cache) > 200:
                self.cache.clear()
            self.cache[cache_key] = (time.monotonic(), data)
        return json.loads(json.dumps(data)), False


class Geocoder:
    """Open-Meteo geocoding (no key). Populated places rank first, then the largest population."""
    def __init__(self, http: CachedHTTP, url: str = OPEN_METEO_GEOCODING_URL):
        if urlparse(url).scheme != "https":
            raise ProviderError("The geocoder URL (GEOCODER_URL or travel.geocoder-url) requires HTTPS")
        self.http, self.url = http, url

    @staticmethod
    def population(row: dict) -> int | None:
        value = row.get("population")
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @staticmethod
    def usable(row) -> bool:
        if not isinstance(row, dict) or not row.get("name") or not row.get("timezone"):
            return False
        return all(isinstance(row.get(k), (int, float)) and not isinstance(row.get(k), bool)
                   for k in ("latitude", "longitude"))

    @classmethod
    def rank(cls, item: tuple[int, dict]) -> tuple[int, int, int]:
        index, row = item
        populated = str(row.get("feature_code") or "").startswith("PPL")
        population = cls.population(row)
        return (0 if populated else 1, -(population if population is not None else -1), index)

    def search(self, query: str) -> GeoPlace | None:
        text = query.strip() if isinstance(query, str) else ""
        if not 2 <= len(text) <= 80:
            raise ValueError("Enter a place name of 2 to 80 characters")
        raw, cached = self.http.request("GET", self.url, ttl=86400,
                                        params={"name": text, "count": 5, "language": "en", "format": "json"})
        results = raw.get("results")
        rows = [row for row in results if self.usable(row)] if isinstance(results, list) else []
        if not rows:
            return None
        best = min(enumerate(rows), key=self.rank)[1]
        now = datetime.now(UTC)
        try:
            ZoneInfo(str(best["timezone"]))
            coordinate = Coordinate(lat=float(best["latitude"]), lon=float(best["longitude"]))
        except (KeyError, ValueError, TypeError):
            raise ProviderError("Geocoding result is incomplete") from None
        return GeoPlace(query=text, name=str(best["name"])[:200], country=str(best.get("country") or "")[:120],
            admin1=str(best.get("admin1") or "")[:120], coordinate=coordinate, timezone=str(best["timezone"]),
            population=self.population(best),
            source=Evidence(id="geocode-" + hashlib.sha256(text.lower().encode()).hexdigest()[:12],
                provider="Open-Meteo geocoding", status="cached" if cached else "live", retrieved_at=now,
                reference="https://open-meteo.com/en/docs/geocoding-api",
                note="Best of up to five matches: populated places first, then the largest population."))


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
# Building-bound categories are treated as sheltered; an explicit indoor=no or outdoor_seating=only overrides.
INDOOR_TAGS = {('tourism', 'museum'), ('tourism', 'gallery'), ('amenity', 'cafe'), ('amenity', 'restaurant'),
               ('shop', 'books'), ('shop', 'mall')}
OUTDOOR_TAGS = {('leisure', 'park')}
# Each tag filter gets its own Overpass output block so a dense city's cafes cannot crowd out museums and parks.
OVERPASS_PER_FILTER = 40


def infer_indoor(tags: dict) -> bool | None:
    """Rain exposure from OSM tags: explicit indoor tag first, then the category; None when nothing says."""
    if tags.get("indoor") in ("yes", "no"):
        return tags.get("indoor") == "yes"
    if tags.get("outdoor_seating") == "only" or any(tags.get(k) == v for k, v in OUTDOOR_TAGS):
        return False
    if any(tags.get(k) == v for k, v in INDOOR_TAGS):
        return True
    return None


def select_candidates(places: list[Place], request: TripRequest) -> list[Place]:
    """Nearest-first round robin over the requested interests, then the always-fetched indoor fallback.

    Sorting by interest overlap and distance alone lets one dense category fill the catalog, which leaves the
    rain fallback nothing to work with; interleaving keeps every category represented within max_candidates.
    """
    by_distance = sorted(places, key=lambda p: haversine(request.origin, p.coordinate))
    buckets = [[p for p in by_distance if label in p.categories] for label in dict.fromkeys(request.interests)]
    buckets.append([p for p in by_distance if not set(p.categories) & set(request.interests)])
    chosen: list[Place] = []
    seen: set[str] = set()
    while len(chosen) < request.max_candidates and any(buckets):
        for bucket in buckets:
            while bucket and bucket[0].id in seen:
                bucket.pop(0)
            if bucket and len(chosen) < request.max_candidates:
                place = bucket.pop(0)
                seen.add(place.id)
                chosen.append(place)
    return chosen


def parse_cost(tags: dict, currency: str) -> tuple[int | None, str]:
    if tags.get("fee") == "no":
        return 0, "estimated"
    match = re.fullmatch(r"(\d+(?:\.\d{1,2})?)\s*(EUR|USD|GBP|SEK)", tags.get("charge", "").strip())
    if match and match[2] == currency:
        return int(Decimal(match[1]) * 100), "estimated"
    return None, "unknown"


def valid_osrm_template(template: str) -> bool:
    if urlparse(template).scheme != "https" or "{profile}" not in template or "{service}" not in template:
        return False
    try:
        template.format(profile="foot", service="table")
    except (KeyError, IndexError, ValueError):
        return False
    return True


class LiveProvider:
    mode = "live"

    def __init__(self, *, contact: str, ors_key: str = "", router: str = "osrm",
                 osrm_url_template: str = OSRM_URL_TEMPLATE, allow_public_overpass: bool = False,
                 overpass_url: str = DEFAULT_OVERPASS_URL, ors_url: str = DEFAULT_ORS_URL,
                 weather_url: str = DEFAULT_WEATHER_URL, client: httpx.Client | None = None):
        # The messages name both spellings: the environment variable of the local server and the run-config key
        # of a Flower run, where no environment variables exist.
        if not contact.strip():
            raise ProviderError("Live mode requires a contact (TRAVEL_CONTACT or travel.contact)")
        if router not in ROUTERS:
            raise ProviderError("The router (TRAVEL_ROUTER or travel.router) must be osrm or ors")
        if router == "ors" and not ors_key.strip():
            raise ProviderError("Router ors requires an OpenRouteService key (ORS_API_KEY or travel.ors-api-key)")
        if router == "osrm" and not valid_osrm_template(osrm_url_template):
            raise ProviderError("The OSRM URL template (OSRM_URL_TEMPLATE or travel.osrm-url-template) must be an "
                                "https URL containing {profile} and {service}")
        if urlparse(overpass_url).hostname == "overpass-api.de" and not allow_public_overpass:
            raise ProviderError("Explicitly enable the public Overpass instance (TRAVEL_ALLOW_PUBLIC_OVERPASS or "
                                "travel.allow-public-overpass) or configure your own instance")
        for url in (overpass_url, ors_url, weather_url):
            if urlparse(url).scheme != "https":
                raise ProviderError("Live provider endpoints require HTTPS")
        self.http = CachedHTTP(contact, client)
        self.router, self.ors_key, self.osrm_url_template = router, ors_key, osrm_url_template
        self.overpass_url, self.ors_url, self.weather_url = overpass_url, ors_url.rstrip("/"), weather_url

    def search(self, request: TripRequest) -> list[Place]:
        filters = set()
        for category in request.interests:
            filters.update(CATEGORY_FILTERS.get(category, []))
        # Indoor alternatives are always retrieved for weather-driven repair.
        filters.update(CATEGORY_FILTERS["art"] + CATEGORY_FILTERS["coffee"])
        lat, lon = request.origin.lat, request.origin.lon
        radius = 2500
        queries = [f'{"node" if k in ("amenity", "shop") else "nwr"}["{k}"="{v}"](around:{radius},{lat:.6f},{lon:.6f});'
                   f'out center tags {OVERPASS_PER_FILTER};'
                   for k, v in sorted(filters)]
        query = '[out:json][timeout:25];' + ''.join(queries)
        raw, cached = self.http.request("POST", self.overpass_url, ttl=900, timeout=30, data={"data": query})
        now = datetime.now(UTC)
        found = []
        seen: set[str] = set()
        for el in raw.get("elements", [])[:len(queries) * OVERPASS_PER_FILTER]:
            tags = el.get("tags", {})
            coord = el.get("center", el)
            name = tags.get("name:en") or tags.get("name")
            if not isinstance(name, str) or not name.strip() or "lat" not in coord or "lon" not in coord:
                continue
            pid = f'osm-{el["type"]}-{el["id"]}'
            if pid in seen:
                continue  # An element matching two filters is printed by two output blocks.
            seen.add(pid)
            point = Coordinate(lat=coord["lat"], lon=coord["lon"])
            indoor = infer_indoor(tags)
            categories = [label for label, fs in CATEGORY_FILTERS.items() if any(tags.get(k) == v for k, v in fs)]
            cost, status = parse_cost(tags, request.currency)
            found.append(Place(id=pid, name=name.strip()[:200], coordinate=point, categories=categories,
                indoor=indoor, visit_duration_s=1800 if "coffee" in categories else 2700,
                cost_minor=cost, cost_status=status, currency=request.currency, opening_hours=tags.get("opening_hours"),
                opening_intervals=parse_hours(tags.get("opening_hours"), request.start, request.timezone),
                opening_status="tag" if tags.get("opening_hours") else "unknown",
                osm_type=el["type"], osm_id=el["id"], website=tags.get("website"),
                description="OSM record. Visit duration is an application planning assumption; entrance is unverified.",
                source=Evidence(id=pid, provider="OpenStreetMap via Overpass", status="cached" if cached else "live",
                    retrieved_at=now, reference=f'https://www.openstreetmap.org/{el["type"]}/{el["id"]}',
                    note="Cached responses may be up to 15 minutes old. Prices and access are unverified OSM tags.")))
        return select_candidates(found, request)

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

    # --- Routing: OpenRouteService (API key) or OSRM (public FOSSGIS instance, no key) -----------------------

    @staticmethod
    def profile(mode: str) -> str:
        return {"walking": "foot-walking", "cycling": "cycling-regular"}[mode]

    @staticmethod
    def osrm_profile(mode: str) -> str:
        return {"walking": "foot", "cycling": "bike"}[mode]

    def osrm_url(self, service: str, mode: str, coordinates: list[Coordinate], query: str) -> str:
        base = self.osrm_url_template.format(profile=self.osrm_profile(mode), service=service).rstrip("/")
        path = ";".join(f"{c.lon:.6f},{c.lat:.6f}" for c in coordinates)
        return f"{base}/{path}?{query}"

    def osrm_request(self, service: str, mode: str, coordinates: list[Coordinate], query: str) -> tuple[dict, bool]:
        """One OSRM call. Errors surface as the OSRM code (NoTable, NoRoute, InvalidQuery ...), never the URL."""
        try:
            raw, cached = self.http.request("GET", self.osrm_url(service, mode, coordinates, query), ttl=900)
        except ProviderError as exc:
            if exc.code:
                raise ProviderError(f"OSRM {service} request failed ({exc.code})",
                                    status=exc.status, code=exc.code) from None
            raise
        code = raw.get("code")
        if code != "Ok":
            code = code if isinstance(code, str) and _CODE.fullmatch(code) else "UnexpectedResponse"
            raise ProviderError(f"OSRM {service} request failed ({code})", code=code)
        return raw, cached

    def matrix(self, points: dict[str, Coordinate], mode: str) -> dict[str, Leg]:
        return self.ors_matrix(points, mode) if self.router == "ors" else self.osrm_matrix(points, mode)

    def osrm_matrix(self, points: dict[str, Coordinate], mode: str) -> dict[str, Leg]:
        ids = list(points)
        raw, cached = self.osrm_request("table", mode, list(points.values()), "annotations=duration,distance")
        result = {}
        try:
            for i, a in enumerate(ids):
                for j, b in enumerate(ids):
                    distance, duration = raw["distances"][i][j], raw["durations"][i][j]
                    if distance is None or duration is None:
                        continue  # Unroutable pair: no leg is invented.
                    result[key(a, b)] = Leg(from_id=a, to_id=b, distance_m=math.ceil(distance),
                        duration_s=math.ceil(duration), mode=mode, geometry=[],
                        source_status="cached" if cached else "live", evidence_id="osrm-table")
        except (KeyError, TypeError, IndexError) as exc:
            raise ProviderError("Routing matrix response is incomplete") from exc
        return result

    def ors_matrix(self, points: dict[str, Coordinate], mode: str) -> dict[str, Leg]:
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
            elif self.router == "ors":
                result.append(self.ors_directions(leg, a, b))
            else:
                result.append(self.osrm_route(leg, a, b))
        return result

    def osrm_route(self, leg: Leg, a: Coordinate, b: Coordinate) -> Leg:
        raw, cached = self.osrm_request("route", leg.mode, [a, b], "overview=full&geometries=geojson&steps=false")
        try:
            route = raw["routes"][0]
            return Leg(from_id=leg.from_id, to_id=leg.to_id, distance_m=math.ceil(route["distance"]),
                duration_s=math.ceil(route["duration"]), mode=leg.mode, geometry=route["geometry"]["coordinates"],
                source_status="cached" if cached else "live", evidence_id="osrm-route")
        except (KeyError, TypeError, IndexError, ValueError) as exc:
            raise ProviderError("Directions response is incomplete") from exc

    def ors_directions(self, leg: Leg, a: Coordinate, b: Coordinate) -> Leg:
        raw, cached = self.http.request("POST", f"{self.ors_url}/v2/directions/{self.profile(leg.mode)}/geojson",
            ttl=900, headers={"Authorization": self.ors_key},
            json={"coordinates": [a.geojson(), b.geojson()], "instructions": False})
        try:
            feature = raw["features"][0]
            summary = feature["properties"]["summary"]
            return Leg(from_id=leg.from_id, to_id=leg.to_id,
                distance_m=math.ceil(summary["distance"]), duration_s=math.ceil(summary["duration"]),
                mode=leg.mode, geometry=feature["geometry"]["coordinates"],
                source_status="cached" if cached else "live", evidence_id="ors-directions")
        except (KeyError, TypeError, IndexError) as exc:
            raise ProviderError("Directions response is incomplete") from exc


def resolve_router() -> str:
    """The router live mode will use: TRAVEL_ROUTER when set, else ors when ORS_API_KEY is set, else osrm."""
    return ProviderSettings.from_env().router


def make_provider(mode: str | None = None, settings: ProviderSettings | None = None) -> TravelProvider:
    """The provider for `mode` ("fixture" or "live"), configured from `settings`.

    Without `settings` the environment is read (TRAVEL_CONTACT, TRAVEL_ROUTER, ORS_API_KEY, ...), exactly as the
    local server always did; a Flower run passes `ProviderSettings.from_run_config(context.run_config)` instead,
    and the environment is never consulted then. `mode` overrides `settings.data_mode` when given.
    """
    settings = settings or ProviderSettings.from_env()
    mode = mode or settings.data_mode
    if mode == "fixture":
        return FixtureProvider()
    if mode != "live":
        raise ProviderError("Unknown data mode")
    return LiveProvider(contact=settings.contact, ors_key=settings.ors_key, router=settings.router,
                        osrm_url_template=settings.osrm_url_template,
                        allow_public_overpass=settings.allow_public_overpass, overpass_url=settings.overpass_url,
                        ors_url=settings.ors_url, weather_url=settings.weather_url)


def make_geocoder(settings: ProviderSettings | None = None) -> Geocoder:
    """Geocoding needs no key; the contact is optional but appended to the User-Agent when set.

    Without `settings` the environment is read (TRAVEL_CONTACT, GEOCODER_URL), as before.
    """
    settings = settings or ProviderSettings.from_env()
    return Geocoder(CachedHTTP(settings.contact), url=settings.geocoder_url)
