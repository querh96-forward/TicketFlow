import asyncio
import json

from fastapi.testclient import TestClient

from app.config import Settings
from app.engine import Engine
from app.main import create_app
from app.observability import error_info, trace_summary
from app.scripted import ScriptedModel
from app.store import Store


def test_provider_failure_is_attributed_without_raw_request_or_secret(tmp_path,monkeypatch):
    secret='private-provider-key-for-test'
    class ProviderError(Exception):
        status_code=400
    def fail(self,*args,**kwargs):
        raise ProviderError('raw request contains '+secret)
    monkeypatch.setattr(ScriptedModel,'_generate',fail)
    settings=Settings(data_dir=tmp_path,model_mode='scripted',llm_api_key=secret)
    store=Store(tmp_path/'tickets.sqlite')
    run=store.create_run('TF-1001','scripted');store.claim()
    asyncio.run(Engine(store,settings).process(run['id']))
    events=store.events(run['id'])
    failed=next(e for e in events if e['kind']=='agent_failed')
    started=next(e for e in events if e['kind']=='agent_started')
    assert failed['actor']=='supervisor'
    assert failed['data']['code']=='MODEL_REQUEST' and failed['data']['http_status']==400
    assert not failed['data']['retryable']
    assert failed['data']['call_id']==started['data']['call_id']
    assert secret not in json.dumps(events)
    assert 'raw request' not in json.dumps(events)
    assert store.run(run['id'])['status']=='failed'
    summary=trace_summary(events)['roles']['supervisor']
    assert summary['calls']==summary['failures']==1
    assert summary['models']==['scripted']


def test_model_rate_limit_is_distinct_from_auth_failure():
    class ProviderError(Exception): pass
    error=ProviderError('sensitive request')
    error.status_code=429
    assert error_info(error)['code']=='MODEL_RATE_LIMIT' and error_info(error)['retryable']
    error.status_code=401
    assert error_info(error)['code']=='MODEL_AUTH' and not error_info(error)['retryable']


def test_export_keeps_evidence_and_redacts_configured_and_common_secrets(tmp_path):
    secret='a-private-key-for-export'
    app=create_app(Settings(data_dir=tmp_path,model_mode='scripted',llm_api_key=secret))
    store=app.state.store
    run=store.create_run('TF-1001','scripted',feedback='accidental '+secret+' sk-examplecredential')
    store.event(run['id'],'agent_started','account',{'model':'test-model','call_id':'c1'})
    store.event(run['id'],'agent_finished','account',{'call_id':'c1','tokens':12,'latency_ms':250})
    store.event(run['id'],'example','system',{'Authorization':'Bearer private','nested':{'api_key':secret}})
    response=TestClient(app).get('/api/runs/'+run['id']+'/report')
    assert response.status_code==200
    assert 'attachment' in response.headers['content-disposition']
    assert secret not in response.text and 'sk-examplecredential' not in response.text
    assert 'Bearer private' not in response.text
    data=response.json()
    assert data['run']['id']==run['id'] and data['ticket']['id']=='TF-1001'
    assert data['run']['trace']['roles']['account']['tokens']==12
    assert data['run']['trace']['roles']['account']['latency_ms']==250
    assert data['events'][-1]['data']['nested']['api_key']=='[redacted]'
