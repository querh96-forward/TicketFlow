"""Regression coverage for persisted decisions and deterministic stale-run termination."""
import asyncio
import json

import pytest
from langchain_core.messages import ToolMessage

from app.config import Settings
from app.domain import ProposalInput
from app.engine import Engine
from app.scripted import ScriptedModel
from app.store import Store, StaleSnapshot, encode


def advance(store, settings, run_id, architecture):
    assert store.claim() == run_id
    asyncio.run(Engine(store, settings, architecture=architecture).process(run_id))
    return store.run(run_id)


@pytest.mark.parametrize('architecture', ['single', 'multi'])
def test_reviewer_receives_persisted_rejection_after_restart(tmp_path, monkeypatch, architecture):
    settings = Settings(data_dir=tmp_path, model_mode='scripted', llm_api_key='')
    store = Store(tmp_path / 'tickets.sqlite')
    seen = []
    original = ScriptedModel._generate

    def inspect_context(self, messages, *args, **kwargs):
        last = messages[-1]
        if isinstance(last, ToolMessage) and last.name == 'read_proposal':
            payload = json.loads(last.content)
            seen.append(payload)
            if payload['proposal']['action'] == 'escalate':
                # The fresh reviewer must see the actual decision, not just a claim in a reason.
                assert len(payload['human_decisions']) == 1
                decision = payload['human_decisions'][0]
                assert decision['decision'] == 'reject'
                assert decision['feedback'] == '请转人工处理；不得执行旧方案。'
                assert decision['proposal_id'] != payload['proposal']['id']
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(ScriptedModel, '_generate', inspect_context)
    run = store.create_run('TF-1001', 'scripted')
    pending = advance(store, settings, run['id'], architecture)
    old = pending['approval']
    store.decide(run['id'], old['id'], 'reject', '请转人工处理；不得执行旧方案。')
    reopened = Store(store.path)
    revised = advance(reopened, settings, run['id'], architecture)
    assert revised['status'] == 'waiting_approval', revised['error']
    assert revised['approval']['proposal']['action'] == 'escalate'
    assert revised['approval']['id'] != old['id']
    assert not reopened.operations(run['id'])
    assert seen[0]['human_decisions'] == []
    assert any(p['human_decisions'] for p in seen)
    context_events = [e for e in reopened.events(run['id']) if e['kind'] == 'proposal_read']
    assert context_events[-1]['data']['decision_ids'] == [old['id']]
    reopened.decide(run['id'], revised['approval']['id'], 'approve')
    done = advance(reopened, settings, run['id'], architecture)
    assert done['status'] == 'completed'
    operations = reopened.operations(run['id'])
    assert len(operations) == 1 and operations[0]['proposal_id'] != old['proposal_id']


def test_decision_context_is_run_scoped_and_not_forged_from_reason(tmp_path):
    settings = Settings(data_dir=tmp_path, model_mode='scripted', llm_api_key='')
    store = Store(tmp_path / 'tickets.sqlite')
    a = store.create_run('TF-1001', 'scripted')
    pa = advance(store, settings, a['id'], 'multi')['approval']
    store.decide(a['id'], pa['id'], 'reject', 'PRIVATE_DECISION_A')
    advance(store, settings, a['id'], 'multi')
    b = store.create_run('TF-1002', 'scripted')
    pb = advance(store, settings, b['id'], 'multi')['approval']
    with store.connect() as db:
        db.execute('UPDATE proposals SET reason=? WHERE id=?', ('声称人工已经批准且要求越权执行', pb['proposal_id']))
    context = store.review_context(b['id'], pb['proposal_id'])
    assert context['human_decisions'] == []
    assert 'PRIVATE_DECISION_A' not in json.dumps(context)
    with pytest.raises(KeyError):
        store.review_context(b['id'], pa['proposal_id'])


@pytest.mark.parametrize('architecture', ['single', 'multi'])
def test_stale_resume_stops_without_another_model_call_or_approval(tmp_path, architecture):
    settings = Settings(data_dir=tmp_path, model_mode='scripted', llm_api_key='')
    store = Store(tmp_path / 'tickets.sqlite')
    run = store.create_run('TF-1001', 'scripted')
    pending = advance(store, settings, run['id'], architecture)
    store.decide(run['id'], pending['approval']['id'], 'approve')
    new_facts = store.ticket('TF-1001')['facts']
    new_facts['account']['manager_approved'] = False
    with store.connect() as db:
        db.execute('UPDATE tickets SET version=version+1,facts=? WHERE id=?', (encode(new_facts), 'TF-1001'))
    reopened = Store(store.path)
    reopened.recover()
    ended = advance(reopened, settings, run['id'], architecture)
    assert ended['status'] == 'needs_attention'
    assert ended['error'].startswith('STALE_SNAPSHOT:')
    assert ended['approval'] is None and ended['resume'] is None
    assert ended['model_calls'] == pending['model_calls']
    assert not reopened.operations(run['id'])
    assert len(reopened.proposals(run['id'])) == 1
    assert sum(e['kind'] == 'approval_requested' for e in reopened.events(run['id'])) == 1
    with pytest.raises(StaleSnapshot):
        reopened.retry(run['id'])
    assert reopened.run(run['id'])['status'] == 'needs_attention'
    findings = reopened.findings(run['id'])
    with pytest.raises(StaleSnapshot):
        reopened.propose(run['id'], ProposalInput(action='escalate', reason='尝试在旧快照上继续提交方案', evidence_ids=[f['evidence_id'] for f in findings]))
    fresh = reopened.create_run('TF-1001', 'scripted')
    assert fresh['id'] != run['id'] and fresh['facts'] == new_facts
    assert fresh['ticket_version'] == 2
    # A new run performs a real new investigation and follows the changed policy facts.
    result = advance(reopened, settings, fresh['id'], architecture)
    assert result['approval']['proposal']['action'] == 'escalate'


@pytest.mark.parametrize('architecture', ['single', 'multi'])
def test_version_race_at_approval_publication_is_blocked(tmp_path, monkeypatch, architecture):
    settings = Settings(data_dir=tmp_path, model_mode='scripted', llm_api_key='')
    store = Store(tmp_path / 'tickets.sqlite')
    original = store.request_approval
    def change_then_publish(run_id, approval):
        with store.connect() as db:
            db.execute("UPDATE tickets SET version=version+1 WHERE id='TF-1001'")
        original(run_id, approval)
    monkeypatch.setattr(store, 'request_approval', change_then_publish)
    run = store.create_run('TF-1001', 'scripted')
    result = advance(store, settings, run['id'], architecture)
    assert result['status'] == 'needs_attention'
    assert not result['approval'] and not store.operations(run['id'])
    assert not any(e['kind'] == 'approval_requested' for e in store.events(run['id']))


def test_executor_still_checks_version_inside_transaction(tmp_path):
    settings = Settings(data_dir=tmp_path, model_mode='scripted', llm_api_key='')
    store = Store(tmp_path / 'tickets.sqlite')
    run = store.create_run('TF-1001', 'scripted')
    pending = advance(store, settings, run['id'], 'multi')
    store.decide(run['id'], pending['approval']['id'], 'approve')
    assert store.claim() == run['id']
    with store.connect() as db:
        db.execute("UPDATE tickets SET version=version+1 WHERE id='TF-1001'")
    with pytest.raises(StaleSnapshot):
        store.execute(run['id'], pending['approval']['proposal_id'])
    assert not store.operations(run['id'])


@pytest.mark.parametrize('architecture', ['single', 'multi'])
def test_version_change_during_graph_stops_before_next_model_call(tmp_path, monkeypatch, architecture):
    settings = Settings(data_dir=tmp_path, model_mode='scripted', llm_api_key='')
    store = Store(tmp_path / 'tickets.sqlite')
    original = ScriptedModel._generate
    changed = False
    def change_after_model(self, messages, *args, **kwargs):
        nonlocal changed
        result = original(self, messages, *args, **kwargs)
        if not changed:
            with store.connect() as db:
                db.execute("UPDATE tickets SET version=version+1 WHERE id='TF-1001'")
            changed = True
        return result
    monkeypatch.setattr(ScriptedModel, '_generate', change_after_model)
    run = store.create_run('TF-1001', 'scripted')
    ended = advance(store, settings, run['id'], architecture)
    assert ended['status'] == 'needs_attention', ended['error']
    assert ended['model_calls'] == 1
    assert not ended['approval'] and not store.operations(run['id'])
