import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import Conflict, Store


def test_existing_database_migrates_and_deleted_seed_stays_deleted(tmp_path):
    path = tmp_path/'tickets.sqlite'
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tickets(id TEXT PRIMARY KEY,title TEXT,description TEXT,category TEXT,priority TEXT,facts TEXT,status TEXT DEFAULT 'open',version INTEGER DEFAULT 1)")
    store = Store(path)
    store.delete_ticket('TF-1001')
    reopened = Store(path)
    assert 'TF-1001' not in {t['id'] for t in reopened.tickets()}
    assert reopened.tickets(deleted=True)[0]['id'] == 'TF-1001'
    reopened.restore_ticket('TF-1001')
    assert len(reopened.tickets()) == 4


@pytest.mark.parametrize('status',['queued','running','waiting_approval'])
def test_cannot_delete_live_ticket(tmp_path, status):
    store = Store(tmp_path/'tickets.sqlite')
    run = store.create_run('TF-1001','scripted')
    store.update(run['id'],status=status)
    with pytest.raises(Conflict, match='活动任务'):
        store.delete_ticket('TF-1001')
    assert store.ticket('TF-1001')['deleted_at'] is None


def test_delete_api_preserves_history_and_restore_allows_retry(tmp_path):
    app = create_app(Settings(data_dir=tmp_path,model_mode='scripted',llm_api_key=''))
    # No lifespan worker: control states explicitly while testing the HTTP contract.
    client = TestClient(app)
    ticket = client.post('/api/tickets',json={'template_id':'TF-1002'}).json()
    run = app.state.store.create_run(ticket['id'],'scripted')
    app.state.store.update(run['id'],status='failed',error='test failure')
    path='/api/tickets/'+ticket['id']
    assert client.delete(path,headers={'Origin':'https://unrelated.example'}).status_code == 403
    assert client.delete(path).status_code == 200
    assert client.delete(path).status_code == 200
    assert ticket['id'] not in {t['id'] for t in client.get('/api/tickets').json()}
    assert client.get('/api/runs').json() == []
    assert client.get('/api/runs?deleted=true').json()[0]['id'] == run['id']
    assert client.get('/api/runs/'+run['id']).json()['error'] == 'test failure'
    assert client.post('/api/runs',json={'ticket_id':ticket['id']}).status_code == 409
    assert client.post('/api/runs/'+run['id']+'/retry').status_code == 409
    assert client.post(path+'/restore').status_code == 200
    assert client.post(path+'/restore').status_code == 200
    assert client.post('/api/runs/'+run['id']+'/retry').status_code == 202
    assert [e['kind'] for e in app.state.store.events(run['id'])].count('ticket_deleted') == 1
    assert client.delete('/api/tickets/missing').status_code == 404


@pytest.mark.parametrize('resume',[False,True])
def test_delete_racing_with_start_or_retry_never_leaves_deleted_live_task(tmp_path,resume):
    store = Store(tmp_path/'tickets.sqlite')
    if resume:
        run = store.create_run('TF-1001','scripted')
        store.update(run['id'],status='failed')
    barrier = Barrier(2)
    def perform(fn):
        barrier.wait()
        try:
            fn()
            return True
        except Conflict:
            return False
    start = (lambda:store.retry(run['id'])) if resume else (lambda:store.create_run('TF-1001','scripted'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(perform,lambda:store.delete_ticket('TF-1001'))
        b=pool.submit(perform,start)
        assert sorted([a.result(),b.result()]) == [False,True]
    deleted=store.ticket('TF-1001')['deleted_at'] is not None
    assert not (deleted and any(r['status']=='queued' for r in store.runs(deleted=True)))
