from datetime import UTC, datetime, timedelta

import httpx
import pytest

from travel_agent.providers.services import CachedHTTP, FixtureProvider, LiveProvider, ProviderError
from travel_agent.schemas import Coordinate, TripRequest


def provider(handler):
    return LiveProvider(contact='test@example.invalid', ors_key='test-ors-key', allow_public_overpass=True,
                        client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_live_mode_requires_explicit_configuration():
    with pytest.raises(ProviderError): LiveProvider(contact='', ors_key='')
    with pytest.raises(ProviderError): LiveProvider(contact='test', ors_key='key')


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
