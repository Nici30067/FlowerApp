import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from travel_agent.api import create_app
from travel_agent.schemas import TripEvent
from travel_agent.store import Conflict, Store


def wait(client, job_id):
    for _ in range(200):
        data = client.get('/api/jobs/' + job_id).json()
        if data['status'] not in ('queued', 'running'):
            return data
        time.sleep(.025)
    raise AssertionError('Job did not finish')


def ready_client(tmp_path):
    return TestClient(create_app(str(tmp_path / 'api.sqlite3'), data_mode='fixture', agent_mode='rules'))


def test_create_and_replan_requires_approval(tmp_path):
    with ready_client(tmp_path) as c:
        assert c.get('/').status_code == 200
        created = c.post('/api/trips', json={}).json()
        job = wait(c, created['job']['id'])
        assert job['status'] == 'completed'
        tid = created['trip_id']
        before = c.get('/api/trips/' + tid).json()['trip']
        response = c.post(f'/api/trips/{tid}/events', json={'base_revision': 1,
            'event': {'id': 'rain-one', 'kind': 'rain', 'simulated': True}})
        assert response.status_code == 200
        job = wait(c, response.json()['job']['id'])
        assert job['status'] == 'awaiting_review'
        data = c.get('/api/trips/' + tid).json()
        assert data['trip'] == before
        proposal = data['proposals'][0]
        assert proposal['added'] and proposal['removed']
        result = c.post('/api/proposals/' + proposal['id'] + '/accept', json={})
        assert result.status_code == 200 and result.json()['revision'] == 2
        assert c.post('/api/proposals/' + proposal['id'] + '/accept', json={}).status_code == 409


def test_event_idempotency_and_payload_mismatch(tmp_path):
    with ready_client(tmp_path) as c:
        new = c.post('/api/trips', json={}).json(); wait(c, new['job']['id']); tid = new['trip_id']
        body = {'base_revision': 1, 'event': {'id': 'same', 'kind': 'budget_changed', 'payload': {'budget_minor': 3000}}}
        first = c.post(f'/api/trips/{tid}/events', json=body).json()
        second = c.post(f'/api/trips/{tid}/events', json=body).json()
        assert first['job']['id'] == second['job']['id']
        body['event']['payload']['budget_minor'] = 2500
        assert c.post(f'/api/trips/{tid}/events', json=body).status_code == 409


def test_reject_preserves_committed_revision(tmp_path):
    with ready_client(tmp_path) as c:
        new = c.post('/api/trips', json={}).json(); wait(c, new['job']['id']); tid = new['trip_id']
        res = c.post(f'/api/trips/{tid}/events', json={'base_revision': 1,
            'event': {'id': 'r', 'kind': 'rain', 'simulated': True}}).json()
        j = wait(c, res['job']['id'])
        assert c.post('/api/proposals/' + j['proposal_id'] + '/reject', json={}).status_code == 200
        assert c.get('/api/trips/' + tid).json()['trip']['revision'] == 1


def test_stale_client_request_is_rejected(tmp_path):
    with ready_client(tmp_path) as c:
        new = c.post('/api/trips', json={}).json(); wait(c, new['job']['id'])
        res = c.post(f'/api/trips/{new["trip_id"]}/events', json={'base_revision': 0,
            'event': {'id': 'r', 'kind': 'rain', 'simulated': True}})
        assert res.status_code == 409


def test_infeasible_revision_cannot_be_committed(tmp_path):
    with ready_client(tmp_path) as c:
        new = c.post('/api/trips', json={}).json(); wait(c, new['job']['id'])
        res = c.post(f'/api/trips/{new["trip_id"]}/events', json={'base_revision': 1,
            'event': {'id': 'zero', 'kind': 'pace_changed', 'payload': {'max_walking_m': 0}}}).json()
        j = wait(c, res['job']['id'])
        assert c.post('/api/proposals/' + j['proposal_id'] + '/accept', json={}).status_code == 409


def test_export_and_import_are_validated(tmp_path):
    with ready_client(tmp_path) as c:
        new = c.post('/api/trips', json={}).json(); wait(c, new['job']['id'])
        export = c.get(f'/api/trips/{new["trip_id"]}/export')
        assert 'attachment' in export.headers['content-disposition']
        state = export.json()
        assert c.post('/api/trips/import', json=state).status_code == 200
        state['itinerary']['walking_m'] = 1
        assert c.post('/api/trips/import', json=state).status_code == 400


def test_sse_events_include_real_stage_activity(tmp_path):
    with ready_client(tmp_path) as c:
        new = c.post('/api/trips', json={}).json(); wait(c, new['job']['id'])
        response = c.get('/api/jobs/' + new['job']['id'] + '/events')
        assert 'text/event-stream' in response.headers['content-type']
        assert 'agent.completed' in response.text and 'event: done' in response.text
        assert 'coordinator.started' not in c.get('/api/jobs/' + new['job']['id'] + '/events?since=999').text


def test_cross_origin_writes_are_blocked(tmp_path):
    with ready_client(tmp_path) as c:
        res = c.post('/api/trips', json={}, headers={'Origin': 'https://attacker.invalid'})
        assert res.status_code == 403


def test_api_token_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv('TRAVEL_API_TOKEN', 'test-secret')
    with ready_client(tmp_path) as c:
        assert c.get('/api/trips').status_code == 401
        assert c.post('/api/login', json={'token': 'wrong'}).status_code == 401
        assert c.post('/api/login', json={'token': 'test-secret'}).status_code == 200
        assert c.get('/api/trips').status_code == 200
        assert c.get('/api/config').json()['auth_required'] is True
        assert 'test-secret' not in c.get('/api/config').text


def test_two_proposals_cannot_overwrite_same_revision(tmp_path, baseline, make_plan):
    store = Store(str(tmp_path / 'race.sqlite3')); store.create_trip(baseline)
    proposals = []
    for index, cost in enumerate((2500, 3000)):
        event = TripEvent(id='cost' + str(index), kind='budget_changed', payload={'budget_minor': cost})
        job, _ = store.enqueue(baseline, event, False); store.start_job(job['id'])
        proposal = make_plan(baseline, event)[0]; store.finish_job(job['id'], proposal); proposals.append(proposal)
    def accept(p):
        try: store.accept(p.id); return True
        except Conflict: return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(accept, proposals)) == [False, True]
    assert store.trip(baseline.id).revision == 2


def test_worker_capability_is_scoped_and_single_claim(tmp_path, baseline):
    store = Store(str(tmp_path / 'worker.sqlite3')); store.create_trip(baseline)
    job, token = store.enqueue(baseline, None, True)
    with pytest.raises(PermissionError): store.verify_worker(job['id'], 'wrong', claim=True)
    with pytest.raises(Conflict): store.verify_worker(job['id'], token)
    store.verify_worker(job['id'], token, claim=True)
    with pytest.raises(Conflict): store.verify_worker(job['id'], token, claim=True)
    store.verify_worker(job['id'], token)
    assert 'token_hash' not in store.job(job['id'])


def test_worker_cannot_relax_constraints(tmp_path, baseline, make_plan):
    store = Store(str(tmp_path / 'worker2.sqlite3')); store.create_trip(baseline)
    job, _ = store.enqueue(baseline, None, False); store.start_job(job['id'])
    p = make_plan(baseline)[0]; p.proposed.request.budget_minor = 999999
    with pytest.raises(Conflict, match='constraints'):
        store.finish_job(job['id'], p)


def test_restart_preserves_trip(tmp_path, baseline):
    path = str(tmp_path / 'persistent.sqlite3')
    Store(path).create_trip(baseline)
    assert Store(path).trip(baseline.id) == baseline
