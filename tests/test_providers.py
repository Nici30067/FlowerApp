from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from travel_agent.providers.services import (
    OPEN_METEO_GEOCODING_URL,
    OSRM_URL_TEMPLATE,
    CachedHTTP,
    FixtureProvider,
    LiveProvider,
    ProviderError,
    make_geocoder,
    make_provider,
)
from travel_agent.schemas import Coordinate, TripRequest
from travel_agent.settings import (
    DEFAULT_GEOCODER_URL,
    DEFAULT_ORS_URL,
    DEFAULT_OSRM_URL_TEMPLATE,
    DEFAULT_OVERPASS_URL,
    DEFAULT_WEATHER_URL,
    PROVIDER_RUN_CONFIG_KEYS,
    ProviderSettings,
    coerce_bool,
    default_router,
)

PROVIDER_ENV = ('TRAVEL_DATA_MODE', 'TRAVEL_CONTACT', 'TRAVEL_ALLOW_PUBLIC_OVERPASS', 'TRAVEL_ROUTER', 'OSRM_URL_TEMPLATE',
                'TRAVEL_OVERPASS_URL', 'ORS_BASE_URL', 'ORS_API_KEY', 'OPEN_METEO_URL', 'GEOCODER_URL')
CONTACT = 'osm-travel-companion Flower Hub AgentApp'
# The [tool.flwr.app.config.travel] table as Flower hands it to the AgentApp: TOML booleans arrive as bool, every
# other key as a string, and unrelated keys share the same flat mapping.
RUN_CONFIG = {'travel.data-mode': 'live', 'travel.contact': CONTACT, 'travel.allow-public-overpass': True,
              'travel.router': 'osrm', 'travel.osrm-url-template': '', 'travel.overpass-url': '', 'travel.ors-url': '',
              'travel.ors-api-key': '', 'travel.weather-url': '', 'travel.geocoder-url': '',
              'travel.api-url': '', 'travel.job-id': '', 'travel.job-token': '', 'agent.input': 'Plan a day in Tokyo'}


@pytest.fixture
def no_provider_env(monkeypatch):
    """No provider variable is set: what a Flower run looks like, and the baseline for the environment tests."""
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)


def provider(handler):
    """An ORS-routed live provider; OSRM routing is covered by tests/test_live_routing.py."""
    return LiveProvider(contact='test@example.invalid', ors_key='test-ors-key', router='ors', allow_public_overpass=True,
                        client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_live_mode_requires_explicit_configuration():
    with pytest.raises(ProviderError): LiveProvider(contact='', ors_key='')
    with pytest.raises(ProviderError): LiveProvider(contact='test', ors_key='key')
    with pytest.raises(ProviderError): LiveProvider(contact='test', ors_key='key', router='ors')
    with pytest.raises(ProviderError): LiveProvider(contact='', ors_key='', router='osrm', allow_public_overpass=True)


def test_no_fabricated_osm_identifiers_in_fixtures():
    assert all(p.osm_id is None and p.source.status == 'fixture' for p in FixtureProvider().search(TripRequest()))


def test_fixture_scope_is_explicit():
    with pytest.raises(ProviderError):
        FixtureProvider().search(TripRequest(origin=Coordinate(lat=48.86, lon=2.35)))


def test_overpass_uses_fixed_templates_and_normalizes_unknowns():
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={'elements': [{'type': 'node', 'id': 123, 'lat': 52.52, 'lon': 13.4,
            'tags': {'name': 'Test museum', 'tourism': 'museum', 'fee': 'yes'}}]})
    p = provider(handler)
    places = p.search(TripRequest(interests=['arbitrary injected syntax']))
    assert places[0].id == 'osm-node-123'
    assert places[0].cost_minor is None
    assert places[0].opening_intervals is None
    assert places[0].indoor is True
    assert b'arbitrary' not in captured[0].content
    assert b'timeout' in captured[0].content


def test_matrix_uses_geojson_coordinate_order_and_units():
    def handler(request):
        import json
        body = json.loads(request.content)
        assert body['locations'][0] == [13.4, 52.5]
        assert body['units'] == 'm'
        return httpx.Response(200, json={'distances': [[0, 100], [100, 0]], 'durations': [[0, 85], [85, 0]]})
    result = provider(handler).matrix({'a': Coordinate(lat=52.5, lon=13.4),
                                       'b': Coordinate(lat=52.6, lon=13.5)}, 'walking')
    assert result['a|b'].duration_s == 85 and result['a|b'].distance_m == 100
    assert result['a|b'].geometry == []


def test_unreachable_matrix_leg_is_not_invented():
    p = provider(lambda r: httpx.Response(200, json={'distances': [[0, None], [None, 0]],
                                                   'durations': [[0, None], [None, 0]]}))
    result = p.matrix({'a': Coordinate(lat=52.5, lon=13.4), 'b': Coordinate(lat=52.6, lon=13.5)}, 'walking')
    assert 'a|b' not in result


def test_live_failure_does_not_fall_back_to_fixtures():
    p = provider(lambda r: httpx.Response(503, text='unavailable'))
    with pytest.raises(ProviderError): p.search(TripRequest())


def test_cache_avoids_repeated_http_request():
    calls = []
    def handler(r): calls.append(r); return httpx.Response(200, json={'ok': True})
    cache = CachedHTTP('test', httpx.Client(transport=httpx.MockTransport(handler)))
    assert cache.request('GET', 'https://example.invalid', ttl=100)[1] is False
    assert cache.request('GET', 'https://example.invalid', ttl=100)[1] is True
    assert len(calls) == 1


def test_forecast_probability_is_aligned_to_preceding_hour():
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    end = now + timedelta(hours=1)
    p = provider(lambda r: httpx.Response(200, json={'hourly': {'time': [int(end.timestamp())],
        'temperature_2m': [20], 'precipitation_probability': [80]}}))
    req = TripRequest(start=now, end=now+timedelta(hours=4))
    weather = p.weather(req)
    assert weather.intervals[0].start == now and weather.intervals[0].end == end


def test_out_of_horizon_forecast_is_unknown():
    def no_request(r): raise AssertionError('No request should be sent for unsupported dates')
    req = TripRequest(start='2030-01-01T10:00:00Z', end='2030-01-01T17:00:00Z')
    w = provider(no_request).weather(req)
    assert w.source.status == 'unknown' and not w.intervals


def test_api_errors_do_not_leak_credentials():
    p = provider(lambda r: httpx.Response(401, text='test-ors-key'))
    with pytest.raises(ProviderError) as exc:
        p.matrix({'a': Coordinate(lat=52.5, lon=13.4)}, 'walking')
    assert 'test-ors-key' not in str(exc.value)


# --- Provider settings: run config (Flower) and environment (local server) ---------------------------------

def test_run_config_defaults():
    defaults = ProviderSettings()
    assert ProviderSettings.from_run_config({}) == defaults
    assert ProviderSettings.from_run_config(dict.fromkeys(PROVIDER_RUN_CONFIG_KEYS.values(), '')) == defaults
    assert defaults.data_mode == 'fixture' and defaults.live is False and defaults.router == 'osrm'
    assert defaults.contact == '' and defaults.ors_key == '' and defaults.allow_public_overpass is False
    assert defaults.osrm_url_template == DEFAULT_OSRM_URL_TEMPLATE == OSRM_URL_TEMPLATE
    assert defaults.geocoder_url == DEFAULT_GEOCODER_URL == OPEN_METEO_GEOCODING_URL
    assert defaults.overpass_url == DEFAULT_OVERPASS_URL == 'https://overpass-api.de/api/interpreter'
    assert defaults.ors_url == DEFAULT_ORS_URL == 'https://api.openrouteservice.org'
    assert defaults.weather_url == DEFAULT_WEATHER_URL == 'https://api.open-meteo.com/v1/forecast'
    assert set(PROVIDER_RUN_CONFIG_KEYS) == set(defaults.__dataclass_fields__)
    assert all(key.startswith('travel.') for key in PROVIDER_RUN_CONFIG_KEYS.values())


def test_run_config_of_the_flower_app_is_the_zero_key_live_stack():
    s = ProviderSettings.from_run_config(RUN_CONFIG)
    assert s.live and s.data_mode == 'live' and s.contact == CONTACT and s.allow_public_overpass is True
    assert s.router == 'osrm' and s.ors_key == ''
    # "" means the default endpoint, so pyproject.toml can declare every key without repeating the URLs.
    assert (s.osrm_url_template, s.overpass_url, s.ors_url, s.weather_url, s.geocoder_url) == (
        DEFAULT_OSRM_URL_TEMPLATE, DEFAULT_OVERPASS_URL, DEFAULT_ORS_URL, DEFAULT_WEATHER_URL, DEFAULT_GEOCODER_URL)
    assert ProviderSettings.from_run_config({**RUN_CONFIG, 'travel.data-mode': 'fixture'}).live is False


def test_run_config_overrides():
    s = ProviderSettings.from_run_config({
        'travel.data-mode': ' Live ', 'travel.contact': 'ops@example.invalid', 'travel.allow-public-overpass': 'false',
        'travel.router': ' ORS ', 'travel.osrm-url-template': ' https://osrm.example.invalid/{profile}/{service}/v1/x ',
        'travel.overpass-url': 'https://overpass.example.invalid/api/interpreter ',
        'travel.ors-url': 'https://ors.example.invalid', 'travel.ors-api-key': 'cfg-key',
        'travel.weather-url': 'https://weather.example.invalid/v1/forecast',
        'travel.geocoder-url': 'https://geocoding.example.invalid/v1/search'})
    assert s.data_mode == 'live' and s.router == 'ors' and s.contact == 'ops@example.invalid'
    assert s.allow_public_overpass is False and s.ors_key == 'cfg-key'
    assert s.osrm_url_template == 'https://osrm.example.invalid/{profile}/{service}/v1/x'
    assert s.overpass_url == 'https://overpass.example.invalid/api/interpreter'
    assert s.ors_url == 'https://ors.example.invalid'
    assert s.weather_url == 'https://weather.example.invalid/v1/forecast'
    assert s.geocoder_url == 'https://geocoding.example.invalid/v1/search'
    # Numbers are tolerated for string keys (a TOML author's slip), never a crash.
    assert ProviderSettings.from_run_config({'travel.contact': 42}).contact == '42'


def test_run_config_booleans_accept_bool_and_text():
    key = 'travel.allow-public-overpass'
    for value in (True, 'true', 'True', ' TRUE ', 'yes', 'on', '1', 1):
        assert ProviderSettings.from_run_config({key: value}).allow_public_overpass is True, value
    for value in (False, 'false', 'False', 'no', 'off', '0', 0):
        assert ProviderSettings.from_run_config({key: value}).allow_public_overpass is False, value
    assert ProviderSettings.from_run_config({key: ''}).allow_public_overpass is False  # "" = default
    for bad in ('maybe', 'yes please', 2, 0.5):
        with pytest.raises(ValueError):
            ProviderSettings.from_run_config({key: bad})
    assert coerce_bool(None, True) is True and coerce_bool('  ', True) is True and coerce_bool('', False) is False


def test_run_config_router_defaults_like_the_environment():
    assert ProviderSettings.from_run_config({'travel.router': ''}).router == 'osrm'
    assert ProviderSettings.from_run_config({'travel.ors-api-key': 'k'}).router == 'ors'
    assert ProviderSettings.from_run_config({'travel.ors-api-key': 'k', 'travel.router': 'osrm'}).router == 'osrm'
    assert default_router('', ' ') == 'osrm' and default_router('', 'k') == 'ors' and default_router(' OSRM ', 'k') == 'osrm'
    # An unknown router is kept verbatim (normalised) so LiveProvider reports it as a ProviderError, as today.
    assert ProviderSettings.from_run_config({'travel.router': 'GraphHopper'}).router == 'graphhopper'


def test_run_config_never_reads_the_environment(no_provider_env, monkeypatch):
    monkeypatch.setenv('TRAVEL_CONTACT', 'env@example.invalid')
    monkeypatch.setenv('ORS_API_KEY', 'env-key')
    monkeypatch.setenv('TRAVEL_ROUTER', 'ors')
    monkeypatch.setenv('GEOCODER_URL', 'https://env.example.invalid/v1/search')
    s = ProviderSettings.from_run_config(RUN_CONFIG)
    assert s.contact == CONTACT and s.ors_key == '' and s.router == 'osrm' and s.geocoder_url == DEFAULT_GEOCODER_URL
    assert ProviderSettings.from_run_config({}) == ProviderSettings()


def test_env_settings_defaults_overrides_and_empty_values(no_provider_env, monkeypatch):
    assert ProviderSettings.from_env() == ProviderSettings()
    monkeypatch.setenv('TRAVEL_DATA_MODE', 'live')
    monkeypatch.setenv('TRAVEL_CONTACT', 'ops@example.invalid')
    monkeypatch.setenv('TRAVEL_ALLOW_PUBLIC_OVERPASS', 'true')
    monkeypatch.setenv('TRAVEL_ROUTER', 'OSRM')
    monkeypatch.setenv('OSRM_URL_TEMPLATE', 'https://osrm.example.invalid/{profile}/{service}/v1/x')
    monkeypatch.setenv('TRAVEL_OVERPASS_URL', 'https://overpass.example.invalid/api/interpreter')
    monkeypatch.setenv('ORS_BASE_URL', 'https://ors.example.invalid')
    monkeypatch.setenv('ORS_API_KEY', 'env-key')
    monkeypatch.setenv('OPEN_METEO_URL', 'https://weather.example.invalid/v1/forecast')
    monkeypatch.setenv('GEOCODER_URL', 'https://geocoding.example.invalid/v1/search')
    s = ProviderSettings.from_env()
    assert s == ProviderSettings(data_mode='live', contact='ops@example.invalid', allow_public_overpass=True,
                                 router='osrm', osrm_url_template='https://osrm.example.invalid/{profile}/{service}/v1/x',
                                 overpass_url='https://overpass.example.invalid/api/interpreter',
                                 ors_url='https://ors.example.invalid', ors_key='env-key',
                                 weather_url='https://weather.example.invalid/v1/forecast',
                                 geocoder_url='https://geocoding.example.invalid/v1/search')
    monkeypatch.setenv('TRAVEL_ROUTER', '')
    assert ProviderSettings.from_env().router == 'ors'  # ORS_API_KEY set: the router defaults to ors, as before
    monkeypatch.delenv('ORS_API_KEY')
    assert ProviderSettings.from_env().router == 'osrm'
    # Empty variables mean the default, like unset ones.
    for name in ('TRAVEL_DATA_MODE', 'OSRM_URL_TEMPLATE', 'TRAVEL_OVERPASS_URL', 'ORS_BASE_URL', 'OPEN_METEO_URL',
                 'GEOCODER_URL', 'TRAVEL_CONTACT', 'TRAVEL_ALLOW_PUBLIC_OVERPASS'):
        monkeypatch.setenv(name, '')
    assert ProviderSettings.from_env() == ProviderSettings()


def test_env_public_overpass_flag_reads_exactly_true(no_provider_env, monkeypatch):
    for value, expected in (('true', True), ('TRUE', True), ('false', False), ('yes', False), ('1', False), ('', False)):
        monkeypatch.setenv('TRAVEL_ALLOW_PUBLIC_OVERPASS', value)
        assert ProviderSettings.from_env().allow_public_overpass is expected, value


def test_make_provider_from_live_settings_without_environment(no_provider_env):
    settings = ProviderSettings.from_run_config(RUN_CONFIG)
    p = make_provider(settings=settings)
    assert isinstance(p, LiveProvider) and p.mode == 'live' and p.router == 'osrm'
    assert p.osrm_url_template == DEFAULT_OSRM_URL_TEMPLATE and p.overpass_url == DEFAULT_OVERPASS_URL
    assert p.weather_url == DEFAULT_WEATHER_URL and p.ors_url == DEFAULT_ORS_URL and p.ors_key == ''
    assert p.http.client.headers['user-agent'] == f'OSMTravelCompanion/0.2 ({CONTACT})'
    # `mode` overrides settings.data_mode in both directions; fixture settings need nothing else.
    assert isinstance(make_provider('fixture', settings), FixtureProvider)
    assert isinstance(make_provider(settings=ProviderSettings()), FixtureProvider)
    assert isinstance(make_provider(settings=ProviderSettings(data_mode='fixture')), FixtureProvider)
    assert isinstance(make_provider('live', replace(settings, data_mode='fixture')), LiveProvider)
    with pytest.raises(ProviderError):
        make_provider('other', settings)
    with pytest.raises(ProviderError):
        make_provider(settings=replace(settings, data_mode='other'))


def test_make_provider_from_settings_validates_like_the_constructor(no_provider_env, monkeypatch):
    # A valid environment must never rescue invalid settings: inside a Flower run there is no environment at all.
    monkeypatch.setenv('TRAVEL_CONTACT', 'env@example.invalid')
    monkeypatch.setenv('TRAVEL_ALLOW_PUBLIC_OVERPASS', 'true')
    monkeypatch.setenv('ORS_API_KEY', 'env-key')
    live = ProviderSettings.from_run_config(RUN_CONFIG)
    with pytest.raises(ProviderError, match=r'TRAVEL_CONTACT or travel\.contact'):
        make_provider(settings=replace(live, contact=' '))
    with pytest.raises(ProviderError, match=r'travel\.allow-public-overpass'):
        make_provider(settings=replace(live, allow_public_overpass=False))
    with pytest.raises(ProviderError, match=r'travel\.ors-api-key'):
        make_provider(settings=replace(live, router='ors'))
    with pytest.raises(ProviderError, match='osrm or ors'):
        make_provider(settings=replace(live, router='graphhopper'))
    with pytest.raises(ProviderError, match=r'travel\.osrm-url-template'):
        make_provider(settings=replace(live, osrm_url_template='https://osrm.example.invalid/no-placeholders'))
    with pytest.raises(ProviderError, match='HTTPS'):
        make_provider(settings=replace(live, weather_url='http://api.open-meteo.com/v1/forecast'))
    # An own Overpass instance needs no flag; ors with a key routes through ors, trailing slash normalised.
    own = make_provider(settings=replace(live, allow_public_overpass=False,
                                         overpass_url='https://overpass.example.invalid/api/interpreter'))
    assert own.overpass_url == 'https://overpass.example.invalid/api/interpreter'
    ors = make_provider(settings=ProviderSettings.from_run_config({
        **RUN_CONFIG, 'travel.router': '', 'travel.ors-api-key': 'cfg-key', 'travel.ors-url': 'https://ors.example.invalid/'}))
    assert ors.router == 'ors' and ors.ors_key == 'cfg-key' and ors.ors_url == 'https://ors.example.invalid'


def test_make_geocoder_from_settings(no_provider_env, monkeypatch):
    monkeypatch.setenv('GEOCODER_URL', 'https://env.example.invalid/v1/search')
    monkeypatch.setenv('TRAVEL_CONTACT', 'env@example.invalid')
    g = make_geocoder(ProviderSettings.from_run_config(RUN_CONFIG))
    assert g.url == DEFAULT_GEOCODER_URL and g.http.client.headers['user-agent'] == f'OSMTravelCompanion/0.2 ({CONTACT})'
    custom = make_geocoder(ProviderSettings(geocoder_url='https://geocoding.example.invalid/v1/search'))
    assert custom.url == 'https://geocoding.example.invalid/v1/search'
    assert custom.http.client.headers['user-agent'] == 'OSMTravelCompanion/0.2'  # no contact: bare User-Agent
    with pytest.raises(ProviderError, match=r'travel\.geocoder-url'):
        make_geocoder(ProviderSettings(geocoder_url='http://geocoding.example.invalid/v1/search'))
    # Without settings the environment still decides, as before.
    env = make_geocoder()
    assert env.url == 'https://env.example.invalid/v1/search'
    assert env.http.client.headers['user-agent'] == 'OSMTravelCompanion/0.2 (env@example.invalid)'


def test_describe_hides_the_ors_key():
    view = ProviderSettings(contact='ops@example.invalid', ors_key='secret-key', router='ors').describe()
    assert 'ors_key' not in view and view['ors_key_set'] is True and 'secret-key' not in repr(view)
    assert view['contact'] == 'ops@example.invalid' and view['router'] == 'ors' and view['data_mode'] == 'fixture'
    assert ProviderSettings().describe()['ors_key_set'] is False
    assert ProviderSettings(ors_key='  ').describe()['ors_key_set'] is False
