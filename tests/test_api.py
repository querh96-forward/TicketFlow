import asyncio

from fastapi.testclient import TestClient

from app.config import Settings
from app.engine import Engine
from app.main import create_app


def test_api_create_validation_and_conflicts(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, model_mode='scripted', llm_api_key=''))
    with TestClient(app) as client:
        assert client.get('/api/health').json()['status'] == 'ok'
        assert client.get('/').status_code == 200
        ticket = client.post('/api/tickets', json={'template_id':'TF-1002'}).json()
        assert ticket['id'] != 'TF-1002'
        response = client.post('/api/runs', json={'ticket_id':ticket['id']})
        assert response.status_code == 202
        assert client.post('/api/runs', json={'ticket_id':ticket['id']}).status_code == 409
        assert client.post('/api/runs', json={'ticket_id':'missing'}).status_code == 404
        assert client.post('/api/runs/'+response.json()['id']+'/decision', json={'approval_id':'fake','decision':'approve'}).status_code == 409
        assert client.post('/api/tickets', headers={'Origin':'https://unrelated.example'}, json={'template_id':'TF-1001'}).status_code == 403
        assert client.post('/api/tickets', json={'template_id':'unknown'}).status_code == 422


def test_real_mode_requires_key(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, model_mode='real', llm_api_key=''))
    with TestClient(app) as client:
        assert not client.get('/api/health').json()['model_ready']
        assert client.post('/api/runs', json={'ticket_id':'TF-1001'}).status_code == 422


def test_sse_replay_respects_cursor(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, model_mode='scripted', llm_api_key=''))
    store = app.state.store
    run = store.create_run('TF-1001','scripted')
    store.cancel(run['id'])
    events = store.events(run['id'])
    with TestClient(app) as client:
        response = client.get('/api/runs/'+run['id']+'/events', headers={'Last-Event-ID':str(events[0]['id'])})
        assert '"kind": "queued"' not in response.text
        assert '"kind": "canceled"' in response.text
        assert 'event: done' in response.text
