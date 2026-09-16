"""OSRM routing, Open-Meteo geocoding, and live-provider configuration. Offline: every HTTP call is mocked."""
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest

from travel_agent.agents import ExecutionBudget, RulesRunner
from travel_agent.coordinator import Coordinator
from travel_agent.planning.engine import rainy
from travel_agent.providers.services import (
    OPEN_METEO_GEOCODING_URL,
    OSRM_URL_TEMPLATE,
    OVERPASS_PER_FILTER,
    CachedHTTP,
    FixtureProvider,
    Geocoder,
    LiveProvider,
    ProviderError,
    fixture_supported,
    haversine,
    infer_indoor,
    make_geocoder,
    make_provider,
    resolve_router,
)
from travel_agent.schemas import Coordinate, Leg, TripEvent, TripRequest, TripSnapshot

TOKYO = Coordinate(lat=35.6895, lon=139.6917)
SHINJUKU = Coordinate(lat=35.6935, lon=139.7017)
POINTS = {'a': TOKYO, 'b': SHINJUKU}
TABLE = {'code': 'Ok', 'durations': [[0, 1020.9], [1020.9, 0]], 'distances': [[0, 1276.8], [1276.8, 0]]}
ROUTE = {'code': 'Ok', 'routes': [{'distance': 1276.8, 'duration': 1020.9, 'geometry': {
    'type': 'LineString', 'coordinates': [[139.691932, 35.68954], [139.6957, 35.6912], [139.7017, 35.6935]]}}]}
GEOCODE = {'results': [
    {'name': 'Tokyo', 'country': 'Papua New Guinea', 'latitude': -5.6, 'longitude': 147.3,
     'timezone': 'Pacific/Port_Moresby', 'population': None, 'feature_code': 'PPL'},
    {'name': 'Tokyo', 'country': 'Japan', 'admin1': 'Tokyo', 'latitude': 35.6895, 'longitude': 139.69171,
     'timezone': 'Asia/Tokyo', 'population': 9733276, 'feature_code': 'PPLC'}]}


def osrm(handler, **overrides):
    options = {'contact': 'test@example.invalid', 'allow_public_overpass': True,
               'client': httpx.Client(transport=httpx.MockTransport(handler))}
    options.update(overrides)
    return LiveProvider(**options)


def geocoder(handler, url=OPEN_METEO_GEOCODING_URL):
    return Geocoder(CachedHTTP('', httpx.Client(transport=httpx.MockTransport(handler))), url=url)


def leg(mode='walking'):
    return Leg(from_id='a', to_id='b', distance_m=1, duration_s=1, mode=mode, geometry=[], source_status='live',
               evidence_id='osrm-table')


# --- OSRM table -------------------------------------------------------------------------------------------

def test_osrm_table_request_shape_and_parsing():
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=TABLE)
    result = osrm(handler).matrix(POINTS, 'walking')
    url = str(captured[0].url)
    assert captured[0].method == 'GET'
    assert url.startswith('https://routing.openstreetmap.de/routed-foot/table/v1/driving/')
    assert '139.691700,35.689500;139.701700,35.693500' in url  # lon,lat order, semicolon separated
    assert 'annotations=duration,distance' in url
    assert result['a|b'].distance_m == 1277 and result['a|b'].duration_s == 1021  # ceil()
    assert result['a|a'].distance_m == 0 and result['b|a'].duration_s == 1021
    assert result['a|b'].geometry == [] and result['a|b'].evidence_id == 'osrm-table'
    assert result['a|b'].source_status == 'live'


def test_osrm_table_skips_null_cells():
    table = {'code': 'Ok', 'durations': [[0, None], [1020.9, 0]], 'distances': [[0, None], [1276.8, 0]]}
    result = osrm(lambda r: httpx.Response(200, json=table)).matrix(POINTS, 'walking')
    assert 'a|b' not in result and 'b|a' in result and 'a|a' in result


def test_osrm_table_non_ok_code_is_a_provider_error_without_url():
    p = osrm(lambda r: httpx.Response(200, json={'code': 'NoTable', 'message': 'No table found'}))
    with pytest.raises(ProviderError) as exc:
        p.matrix(POINTS, 'walking')
    assert 'NoTable' in str(exc.value) and exc.value.code == 'NoTable'
    assert 'routing.openstreetmap.de/' not in str(exc.value) and '139.69' not in str(exc.value)


def test_osrm_http_error_body_code_is_reported():
    p = osrm(lambda r: httpx.Response(400, json={'code': 'InvalidQuery', 'message': 'Query string malformed'}))
    with pytest.raises(ProviderError) as exc:
        p.matrix(POINTS, 'walking')
    assert 'InvalidQuery' in str(exc.value) and exc.value.status == 400
    assert 'malformed' not in str(exc.value) and '/table/' not in str(exc.value)


def test_osrm_http_error_without_json_body_keeps_generic_message():
    p = osrm(lambda r: httpx.Response(503, text='<html>busy</html>'))
    with pytest.raises(ProviderError) as exc:
        p.matrix(POINTS, 'walking')
    assert '503' in str(exc.value) and 'busy' not in str(exc.value) and exc.value.code is None


def test_osrm_incomplete_table_is_an_error():
    p = osrm(lambda r: httpx.Response(200, json={'code': 'Ok', 'durations': [[0, 1]]}))
    with pytest.raises(ProviderError):
        p.matrix(POINTS, 'walking')


def test_osrm_table_second_call_is_cached():
    calls = []
    def handler(r): calls.append(r); return httpx.Response(200, json=TABLE)
    p = osrm(handler)
    assert p.matrix(POINTS, 'walking')['a|b'].source_status == 'live'
    assert p.matrix(POINTS, 'walking')['a|b'].source_status == 'cached'
    assert len(calls) == 1


# --- OSRM route -------------------------------------------------------------------------------------------

def test_osrm_route_parsing():
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=ROUTE)
    legs = osrm(handler).geometry([leg()], POINTS)
    url = str(captured[0].url)
    assert url.startswith('https://routing.openstreetmap.de/routed-foot/route/v1/driving/139.691700,35.689500;')
    assert 'overview=full' in url and 'geometries=geojson' in url and 'steps=false' in url
    assert legs[0].geometry == ROUTE['routes'][0]['geometry']['coordinates']
    assert legs[0].distance_m == 1277 and legs[0].duration_s == 1021
    assert legs[0].evidence_id == 'osrm-route' and legs[0].source_status == 'live'
    assert legs[0].from_id == 'a' and legs[0].to_id == 'b' and legs[0].mode == 'walking'


def test_osrm_route_no_route_code():
    p = osrm(lambda r: httpx.Response(400, json={'code': 'NoRoute', 'message': 'Impossible route.'}))
    with pytest.raises(ProviderError) as exc:
        p.geometry([leg()], POINTS)
    assert 'NoRoute' in str(exc.value)


def test_osrm_route_incomplete_geometry_is_an_error():
    p = osrm(lambda r: httpx.Response(200, json={'code': 'Ok', 'routes': [{'distance': 1, 'duration': 1}]}))
    with pytest.raises(ProviderError):
        p.geometry([leg()], POINTS)


def test_coincident_points_short_circuit_without_a_request():
    def no_request(r): raise AssertionError('No request should be sent for coincident points')
    points = {'a': TOKYO, 'b': TOKYO}
    legs = osrm(no_request).geometry([leg()], points)
    assert legs[0].geometry == [TOKYO.geojson(), TOKYO.geojson()]
    assert osrm(no_request).geometry([], points) == []


# --- Profiles and templates -------------------------------------------------------------------------------

def test_profile_and_template_formatting():
    captured = []
    def handler(request): captured.append(str(request.url)); return httpx.Response(200, json=TABLE)
    p = osrm(handler)
    p.matrix(POINTS, 'walking')
    p.matrix(POINTS, 'cycling')
    assert captured[0].startswith('https://routing.openstreetmap.de/routed-foot/table/v1/driving/')
    assert captured[1].startswith('https://routing.openstreetmap.de/routed-bike/table/v1/driving/')
    captured.clear()
    custom = osrm(handler, osrm_url_template='https://osrm.example.invalid/{service}/v1/{profile}/')
    custom.matrix(POINTS, 'cycling')
    assert captured[0].startswith('https://osrm.example.invalid/table/v1/bike/139.691700,35.689500;')
    assert OSRM_URL_TEMPLATE == 'https://routing.openstreetmap.de/routed-{profile}/{service}/v1/driving'
    assert LiveProvider.osrm_profile('walking') == 'foot' and LiveProvider.osrm_profile('cycling') == 'bike'
    with pytest.raises(KeyError):
        LiveProvider.osrm_profile('driving')


def test_construction_rules():
    ok = LiveProvider(contact='test', allow_public_overpass=True)  # osrm needs no key
    assert ok.router == 'osrm' and ok.mode == 'live'
    assert LiveProvider(contact='test', ors_key='k', router='ors', allow_public_overpass=True).router == 'ors'
    with pytest.raises(ProviderError):
        LiveProvider(contact='test', router='ors', allow_public_overpass=True)  # ors requires a key
    with pytest.raises(ProviderError):
        LiveProvider(contact='test', router='graphhopper', allow_public_overpass=True)
    with pytest.raises(ProviderError):
        LiveProvider(contact='', allow_public_overpass=True)
    for bad in ('http://routing.openstreetmap.de/routed-{profile}/{service}/v1/driving',
                'https://routing.openstreetmap.de/routed-foot/{service}/v1/driving',
                'https://routing.openstreetmap.de/routed-{profile}/table/v1/driving',
                'https://example.invalid/{profile}/{service}/{other}', ''):
        with pytest.raises(ProviderError):
            LiveProvider(contact='test', allow_public_overpass=True, osrm_url_template=bad)
    # A bad OSRM template is irrelevant when ORS routes.
    assert LiveProvider(contact='test', ors_key='k', router='ors', allow_public_overpass=True,
                        osrm_url_template='nonsense').router == 'ors'


def test_ors_router_is_unchanged():
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={'distances': [[0, 100], [100, 0]], 'durations': [[0, 85], [85, 0]]})
    p = osrm(handler, ors_key='test-ors-key', router='ors')
    result = p.matrix(POINTS, 'walking')
    assert captured[0].method == 'POST' and captured[0].url.path == '/v2/matrix/foot-walking'
    assert captured[0].headers['authorization'] == 'test-ors-key'
    assert result['a|b'].evidence_id == 'ors-matrix'


def test_make_provider_reads_router_environment(monkeypatch):
    for name in ('TRAVEL_ROUTER', 'ORS_API_KEY', 'OSRM_URL_TEMPLATE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TRAVEL_CONTACT', 'test@example.invalid')
    monkeypatch.setenv('TRAVEL_ALLOW_PUBLIC_OVERPASS', 'true')
    assert resolve_router() == 'osrm'
    p = make_provider('live')
    assert p.router == 'osrm' and p.osrm_url_template == OSRM_URL_TEMPLATE
    assert make_provider('fixture').mode == 'fixture'
    monkeypatch.setenv('ORS_API_KEY', 'k')
    assert resolve_router() == 'ors' and make_provider('live').router == 'ors'
    monkeypatch.setenv('TRAVEL_ROUTER', 'osrm')
    monkeypatch.setenv('OSRM_URL_TEMPLATE', 'https://osrm.example.invalid/{profile}/{service}/v1/driving')
    p = make_provider('live')
    assert p.router == 'osrm' and p.osrm_url_template.startswith('https://osrm.example.invalid/')
    monkeypatch.setenv('OSRM_URL_TEMPLATE', '')
    assert make_provider('live').osrm_url_template == OSRM_URL_TEMPLATE
    monkeypatch.setenv('TRAVEL_ROUTER', 'ors')
    monkeypatch.delenv('ORS_API_KEY')
    with pytest.raises(ProviderError):
        make_provider('live')
    with pytest.raises(ProviderError):
        make_provider('other')


# --- HTTP client ------------------------------------------------------------------------------------------

def test_per_request_timeout_is_forwarded():
    captured = []
    def handler(r): captured.append(r); return httpx.Response(200, json={'ok': True})
    http = CachedHTTP('', httpx.Client(transport=httpx.MockTransport(handler), timeout=12))
    http.request('GET', 'https://example.invalid/a', ttl=10)
    http.request('GET', 'https://example.invalid/b', ttl=10, timeout=30)
    assert captured[0].extensions['timeout']['read'] == 12
    assert captured[1].extensions['timeout']['read'] == 30 and captured[1].extensions['timeout']['connect'] == 30
    # The timeout is not part of the cache identity.
    assert http.request('GET', 'https://example.invalid/b', ttl=10, timeout=5)[1] is True


def test_user_agent_with_and_without_contact():
    assert CachedHTTP('').client.headers['user-agent'] == 'OSMTravelCompanion/0.2'
    assert CachedHTTP('ops@example.invalid').client.headers['user-agent'] == 'OSMTravelCompanion/0.2 (ops@example.invalid)'
    assert CachedHTTP('x', timeout=3).client.timeout.read == 3


def test_overpass_prefers_english_name_and_uses_long_timeout():
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={'elements': [
            {'type': 'node', 'id': 1, 'lat': 35.7188, 'lon': 139.7765,
             'tags': {'name': '東京国立博物館', 'name:en': 'Tokyo National Museum', 'tourism': 'museum'}},
            {'type': 'way', 'id': 2, 'center': {'lat': 35.71, 'lon': 139.77},
             'tags': {'name': '上野恩賜公園', 'leisure': 'park'}},
            {'type': 'node', 'id': 3, 'lat': 35.71, 'lon': 139.77, 'tags': {'tourism': 'museum'}}]})
    request = TripRequest(city='Tokyo', timezone='Asia/Tokyo', origin=TOKYO, destination=TOKYO, interests=['art', 'parks'])
    places = {p.name: p for p in osrm(handler).search(request)}
    assert set(places) == {'Tokyo National Museum', '上野恩賜公園'}  # name:en preferred; unnamed element skipped
    assert places['Tokyo National Museum'].indoor is True and places['上野恩賜公園'].indoor is False
    assert places['Tokyo National Museum'].id == 'osm-node-1'
    query = parse_qs(captured[0].content.decode())['data'][0]
    assert query.startswith('[out:json][timeout:25];')
    assert captured[0].extensions['timeout']['read'] == 30
    assert captured[0].extensions['timeout']['connect'] == 30


# --- Geocoder ---------------------------------------------------------------------------------------------

def test_geocoder_prefers_populated_place_with_population():
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=GEOCODE)
    place = geocoder(handler).search('  Tokyo ')
    params = dict(captured[0].url.params)
    assert params == {'name': 'Tokyo', 'count': '5', 'language': 'en', 'format': 'json'}
    assert place.query == 'Tokyo' and place.name == 'Tokyo' and place.country == 'Japan' and place.admin1 == 'Tokyo'
    assert place.coordinate == Coordinate(lat=35.6895, lon=139.69171)
    assert place.timezone == 'Asia/Tokyo' and place.population == 9733276
    assert place.source.provider == 'Open-Meteo geocoding' and place.source.status == 'live'
    assert place.source.reference == 'https://open-meteo.com/en/docs/geocoding-api'


def test_geocoder_prefers_populated_places_over_regions():
    rows = {'results': [
        {'name': 'Bavaria', 'country': 'Germany', 'latitude': 49.0, 'longitude': 11.5, 'timezone': 'Europe/Berlin',
         'population': 13000000, 'feature_code': 'ADM1'},
        {'name': 'Munich', 'country': 'Germany', 'latitude': 48.137, 'longitude': 11.575,
         'timezone': 'Europe/Berlin', 'population': 1260000, 'feature_code': 'PPLA'},
        {'name': 'Munich', 'country': 'United States', 'latitude': 48.7, 'longitude': -98.8,
         'timezone': 'America/Chicago', 'feature_code': 'PPL'}]}
    place = geocoder(lambda r: httpx.Response(200, json=rows)).search('Munich')
    assert place.country == 'Germany' and place.population == 1260000


def test_geocoder_keeps_api_order_on_ties():
    rows = {'results': [{'name': 'First', 'latitude': 1.0, 'longitude': 1.0, 'timezone': 'UTC', 'feature_code': 'PPL'},
                        {'name': 'Second', 'latitude': 2.0, 'longitude': 2.0, 'timezone': 'UTC', 'feature_code': 'PPL'}]}
    assert geocoder(lambda r: httpx.Response(200, json=rows)).search('tie').name == 'First'


def test_geocoder_returns_none_without_results():
    assert geocoder(lambda r: httpx.Response(200, json={'generationtime_ms': 0.4})).search('Nowhere') is None
    assert geocoder(lambda r: httpx.Response(200, json={'results': []})).search('Nowhere') is None
    # Rows without coordinates or a timezone are unusable, not guessed.
    partial = {'results': [{'name': 'Ghost', 'latitude': 1.0, 'feature_code': 'PPL'}]}
    assert geocoder(lambda r: httpx.Response(200, json=partial)).search('Ghost') is None


def test_geocoder_validates_query_length_before_any_request():
    def no_request(r): raise AssertionError('Invalid queries must not reach the network')
    g = geocoder(no_request)
    for bad in ('', ' ', 'a', ' a ', 'x' * 81, None, 42):
        with pytest.raises(ValueError):
            g.search(bad)


def test_geocoder_second_call_is_cached():
    calls = []
    def handler(r): calls.append(r); return httpx.Response(200, json=GEOCODE)
    g = geocoder(handler)
    assert g.search('Tokyo').source.status == 'live'
    assert g.search('Tokyo').source.status == 'cached'
    assert len(calls) == 1


def test_geocoder_failures_are_provider_errors():
    with pytest.raises(ProviderError):
        geocoder(lambda r: httpx.Response(503, text='down')).search('Tokyo')
    bad_zone = {'results': [{'name': 'X', 'latitude': 1.0, 'longitude': 1.0, 'timezone': 'Mars/Olympus'}]}
    with pytest.raises(ProviderError):
        geocoder(lambda r: httpx.Response(200, json=bad_zone)).search('Tokyo')
    with pytest.raises(ProviderError):
        geocoder(lambda r: httpx.Response(200, json={}), url='http://geocoding.example.invalid/v1/search')


def test_make_geocoder_reads_environment(monkeypatch):
    monkeypatch.delenv('GEOCODER_URL', raising=False)
    monkeypatch.setenv('TRAVEL_CONTACT', '')
    g = make_geocoder()
    assert g.url == OPEN_METEO_GEOCODING_URL and g.http.client.headers['user-agent'] == 'OSMTravelCompanion/0.2'
    monkeypatch.setenv('GEOCODER_URL', 'https://geocoding.example.invalid/v1/search')
    assert make_geocoder().url == 'https://geocoding.example.invalid/v1/search'
    monkeypatch.setenv('GEOCODER_URL', 'http://geocoding.example.invalid/v1/search')
    with pytest.raises(ProviderError):
        make_geocoder()


# --- Fixture scope ----------------------------------------------------------------------------------------

def test_fixture_supported():
    assert fixture_supported(Coordinate(lat=52.5225, lon=13.4024))
    assert fixture_supported(Coordinate(lat=52.5163, lon=13.3777))  # Brandenburg Gate
    assert not fixture_supported(Coordinate(lat=52.3667, lon=13.5033))  # Schoenefeld, ~18 km
    assert not fixture_supported(TOKYO)
    with pytest.raises(ProviderError):
        FixtureProvider().search(TripRequest(city='Tokyo', timezone='Asia/Tokyo', origin=TOKYO, destination=TOKYO))


# --- Dense-city catalog and rain -------------------------------------------------------------------------
# Regression for the live Tokyo check: on a rainy day every cafe came back indoor=None (rain-exposed) and the
# single 120-element Overpass cap was 104 cafes, so no indoor diversity survived and the plan was infeasible.

def test_infer_indoor_from_tags():
    assert infer_indoor({'amenity': 'cafe'}) is True
    assert infer_indoor({'amenity': 'restaurant'}) is True
    assert infer_indoor({'shop': 'books'}) is True and infer_indoor({'shop': 'mall'}) is True
    assert infer_indoor({'tourism': 'museum'}) is True and infer_indoor({'tourism': 'gallery'}) is True
    assert infer_indoor({'leisure': 'park'}) is False
    assert infer_indoor({'amenity': 'cafe', 'indoor': 'no'}) is False  # explicit tag beats the category
    assert infer_indoor({'amenity': 'cafe', 'outdoor_seating': 'only'}) is False
    assert infer_indoor({'amenity': 'cafe', 'outdoor_seating': 'yes'}) is True  # terrace plus a room
    assert infer_indoor({'leisure': 'park', 'indoor': 'yes'}) is True
    assert infer_indoor({'tourism': 'attraction'}) is None and infer_indoor({}) is None


def offset(meters_north, meters_east):
    return {'lat': TOKYO.lat + meters_north / 111320, 'lon': TOKYO.lon + meters_east / 91290}


def dense_city_elements():
    """104 cafes ring the origin closely; 10 museums and 6 parks sit further out, as in central Tokyo."""
    elements = []
    for i in range(104):
        elements.append({'type': 'node', 'id': 1000 + i, **offset(300 + 5 * i, 100 + 3 * i),
                         'tags': {'name': f'Cafe {i}', 'amenity': 'cafe'}})
    for i in range(10):
        elements.append({'type': 'node', 'id': 2000 + i, **offset(700 + 80 * i, -400),
                         'tags': {'name': f'Museum {i}', 'tourism': 'museum'}})
    for i in range(6):
        elements.append({'type': 'way', 'id': 3000 + i, 'center': offset(-800 - 100 * i, 300),
                         'tags': {'name': f'Park {i}', 'leisure': 'park'}})
    return elements


def tokyo_request(**overrides):
    zone = ZoneInfo('Asia/Tokyo')
    day = datetime.now(zone).date() + timedelta(days=1)  # inside the 15-day forecast horizon
    options = {'title': 'Tokyo test', 'city': 'Tokyo', 'timezone': 'Asia/Tokyo',
               'start': datetime(day.year, day.month, day.day, 10, 0, tzinfo=zone),
               'end': datetime(day.year, day.month, day.day, 17, 0, tzinfo=zone),
               'origin': TOKYO, 'destination': TOKYO, 'interests': ['art', 'coffee', 'parks']}
    options.update(overrides)
    return TripRequest(**options)


def test_overpass_outputs_each_filter_separately_and_dedupes():
    captured = []
    def handler(request):
        captured.append(request)
        shared = {'type': 'node', 'id': 7, 'lat': 35.69, 'lon': 139.69,
                  'tags': {'name': 'Museum cafe', 'amenity': 'cafe', 'tourism': 'museum'}}
        return httpx.Response(200, json={'elements': [shared, dict(shared)]})  # printed by two output blocks
    places = osrm(handler).search(tokyo_request())
    assert [p.id for p in places] == ['osm-node-7']
    query = parse_qs(captured[0].content.decode())['data'][0]
    assert query.startswith('[out:json][timeout:25];')
    # art + coffee + parks -> museum, gallery, cafe, park: one bounded output block per tag filter.
    assert query.count(f'out center tags {OVERPASS_PER_FILTER};') == 4
    assert 'out center tags 120;' not in query
    for filter_ in ('["tourism"="museum"]', '["tourism"="gallery"]', '["amenity"="cafe"]', '["leisure"="park"]'):
        assert filter_ in query


def test_dense_city_catalog_keeps_every_interest_category():
    places = osrm(lambda r: httpx.Response(200, json={'elements': dense_city_elements()})).search(tokyo_request())
    by_category = {}
    for p in places:
        by_category.setdefault(p.categories[0], []).append(p)
    assert len(places) == 12
    assert set(by_category) == {'coffee', 'art', 'parks'}
    assert len(by_category['coffee']) == 4 and len(by_category['art']) >= 4 and len(by_category['parks']) >= 3
    # Within a category the nearest survive, so the catalog stays walkable.
    assert [p.name for p in by_category['coffee']] == ['Cafe 0', 'Cafe 1', 'Cafe 2', 'Cafe 3']
    assert all(p.indoor is True for p in by_category['coffee'] + by_category['art'])
    assert all(p.indoor is False for p in by_category['parks'])
    # The always-fetched indoor fallback is retained when the interests do not ask for it.
    parks_only = osrm(lambda r: httpx.Response(200, json={'elements': dense_city_elements()})).search(
        tokyo_request(interests=['parks']))
    assert sum(p.indoor is True for p in parks_only) >= 4 and sum(p.indoor is False for p in parks_only) == 6


def fake_live_network(elements, precipitation_pct):
    """Overpass, Open-Meteo and OSRM responses derived from the request, so no fixture data is involved."""
    counts = {'overpass': 0, 'weather': 0, 'table': 0, 'route': 0}

    def coordinates(request):
        return [Coordinate(lon=float(a), lat=float(b)) for a, b in
                (pair.split(',') for pair in request.url.path.rsplit('/', 1)[1].split(';'))]

    def handler(request):
        host = request.url.host
        if host == 'overpass-api.de':
            counts['overpass'] += 1
            return httpx.Response(200, json={'elements': elements})
        if host == 'api.open-meteo.com':
            counts['weather'] += 1
            first = datetime.fromisoformat(request.url.params['start_date']).replace(tzinfo=UTC)
            last = datetime.fromisoformat(request.url.params['end_date']).replace(tzinfo=UTC) + timedelta(days=1)
            times = [int((first + timedelta(hours=h)).timestamp()) for h in range(1, int((last - first).total_seconds() // 3600) + 1)]
            return httpx.Response(200, json={'hourly': {'time': times, 'temperature_2m': [20] * len(times),
                                                        'precipitation_probability': [precipitation_pct] * len(times)}})
        assert host == 'routing.openstreetmap.de', host
        points = coordinates(request)
        if '/table/' in request.url.path:
            counts['table'] += 1
            distances = [[round(haversine(a, b) * 1.3, 1) for b in points] for a in points]
            return httpx.Response(200, json={'code': 'Ok', 'distances': distances,
                                             'durations': [[round(d / 1.2, 1) for d in row] for row in distances]})
        counts['route'] += 1
        a, b = points
        distance = round(haversine(a, b) * 1.3, 1)
        mid = [(a.lon + b.lon) / 2, (a.lat + b.lat) / 2]
        return httpx.Response(200, json={'code': 'Ok', 'routes': [{'distance': distance, 'duration': distance / 1.2,
                                         'geometry': {'type': 'LineString', 'coordinates': [a.geojson(), mid, b.geojson()]}}]})
    return handler, counts


def plan(provider, snapshot, event=None):
    events, budget = [], ExecutionBudget()
    emit = lambda kind, data: events.append({'type': kind, 'data': data})
    return Coordinator(provider, RulesRunner(emit, budget), emit, budget).plan(snapshot, event), budget


def test_rainy_dense_city_plans_indoor_stops_with_osrm_legs():
    handler, counts = fake_live_network(dense_city_elements(), precipitation_pct=100)
    provider = osrm(handler)
    request = tokyo_request()  # defaults: avoid_rain_outdoor_visits=True, min_stops=2, max_candidates=12
    proposal, budget = plan(provider, TripSnapshot(id='tokyo-rain', request=request, data_mode='live'))
    state = proposal.proposed
    itinerary = state.itinerary
    catalog = {p.id: p for p in state.places}
    assert state.weather.source.status == 'live' and rainy(state.weather, request.start, request.end, 60)
    assert 'NO_FEASIBLE_PLAN' not in [i.code for i in itinerary.validation.issues]
    assert itinerary.validation.valid and len(itinerary.stops) >= request.min_stops
    assert all(s.place_id.startswith('osm-node-') for s in itinerary.stops)  # real OSM ids, no fixture data
    assert all(catalog[s.place_id].indoor is True for s in itinerary.stops)
    assert itinerary.legs and all(leg.evidence_id == 'osrm-route' and leg.source_status == 'live' for leg in itinerary.legs)
    assert all(len(leg.geometry) == 3 for leg in itinerary.legs)
    assert budget.metrics()['model_calls'] == 0
    assert counts == {'overpass': 1, 'weather': 1, 'table': 1, 'route': len(itinerary.legs)}

    # A simulated rain event on the proposal keeps an indoor plan and reuses the cached catalog.
    replan, _ = plan(provider, state, TripEvent(id='rain', kind='rain', simulated=True))
    again = replan.proposed.itinerary
    assert again.validation.valid and len(again.stops) >= request.min_stops
    assert all(leg.evidence_id == 'osrm-route' for leg in again.legs)
    assert counts['overpass'] == 1
