import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.config import Settings
from app.domain import ProposalInput
from app.engine import Engine
from app.store import Conflict, Store


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=tmp_path, model_mode='scripted', llm_api_key='')
    store = Store(tmp_path / 'tickets.sqlite')
    return store, settings


def advance(store, settings, run_id):
    assert store.claim() == run_id
    asyncio.run(Engine(store, settings).process(run_id))
    return store.run(run_id)


def waiting(env, ticket='TF-1001'):
    store, settings = env
    run = store.create_run(ticket, 'scripted')
    result = advance(store, settings, run['id'])
    assert result['status'] == 'waiting_approval', result['error']
    assert not store.operations(run['id'])
    return result


@pytest.mark.parametrize('ticket,action', [('TF-1001','restore_access'),('TF-1002','retry_export'),('TF-1003','escalate'),('TF-1004','escalate')])
def test_approval_required_then_exactly_one_operation(env, ticket, action):
    store, settings = env
    run = waiting(env, ticket)
    assert run['approval']['proposal']['action'] == action
    assert store.ticket(ticket)['version'] == 1
    store.decide(run['id'], run['approval']['id'], 'approve')
    completed = advance(store, settings, run['id'])
    assert completed['status'] == 'completed', completed['error']
    operations = store.operations(run['id'])
    assert len(operations) == 1 and operations[0]['result']['action'] == action
    replay = store.execute(run['id'], operations[0]['proposal_id'])
    assert replay['replayed'] and store.ticket(ticket)['version'] == 2
    assert len(store.operations(run['id'])) == 1


def test_rejection_revises_and_requires_new_approval(env):
    store, settings = env
    run = waiting(env)
    old = run['approval']
    store.decide(run['id'], old['id'], 'reject', '不要恢复访问，转人工核查。')
    revised = advance(store, settings, run['id'])
    assert revised['status'] == 'waiting_approval', revised['error']
    assert revised['approval']['proposal']['action'] == 'escalate'
    assert revised['approval']['id'] != old['id']
    assert not store.operations(run['id'])
    with pytest.raises(Conflict):
        store.decide(run['id'], old['id'], 'approve')
    store.decide(run['id'], revised['approval']['id'], 'approve')
    assert advance(store, settings, run['id'])['status'] == 'completed'
    assert store.operations(run['id'])[0]['result']['action'] == 'escalate'


def test_restart_at_approval_preserves_checkpoint(env):
    store, settings = env
    run = waiting(env)
    calls = run['model_calls']
    # New store and Engine objects reopen persisted SQLite files, not memory state.
    reopened = Store(store.path)
    reopened.recover()
    assert reopened.run(run['id'])['approval'] == run['approval']
    reopened.decide(run['id'], run['approval']['id'], 'approve')
    completed = advance(reopened, settings, run['id'])
    assert completed['status'] == 'completed'
    assert completed['model_calls'] == calls + 1  # Does not repeat the investigations.


def test_completed_checkpoint_recovers_without_second_action(env):
    store, settings = env
    run = waiting(env)
    store.decide(run['id'], run['approval']['id'], 'approve')
    done = advance(store, settings, run['id'])
    # Simulate interruption after graph checkpoint and before application status persistence.
    store.update(run['id'], status='running')
    store.recover()
    restored = advance(store, settings, run['id'])
    assert restored['status'] == 'completed'
    assert len(store.operations(run['id'])) == 1
    assert restored['model_calls'] == done['model_calls']


def test_committed_action_is_not_marked_failed_when_summary_budget_exhausted(env):
    store, settings = env
    run = waiting(env)
    settings.max_model_calls = run['model_calls']
    store.decide(run['id'], run['approval']['id'], 'approve')
    result = advance(store, settings, run['id'])
    assert result['status'] == 'completed_with_warning'
    assert len(store.operations(run['id'])) == 1
    assert '业务操作已完成' in result['answer']


def test_concurrent_approval_is_consumed_once(env):
    store, _ = env
    run = waiting(env)
    def decide():
        try:
            store.decide(run['id'], run['approval']['id'], 'approve')
            return True
        except Conflict:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: decide(), range(2))) == [False, True]


def test_execution_rejects_missing_human_decision(env):
    store, _ = env
    run = waiting(env)
    store.update(run['id'], status='running')
    with pytest.raises(Conflict, match='人工批准'):
        store.execute(run['id'], run['approval']['proposal_id'])
    assert not store.operations(run['id'])


def test_stale_data_cannot_execute_even_with_approval(env):
    store, settings = env
    run = waiting(env)
    with store.connect() as db:
        db.execute("UPDATE tickets SET version=version+1 WHERE id='TF-1001'")
    store.decide(run['id'], run['approval']['id'], 'approve')
    result = advance(store, settings, run['id'])
    assert result['status'] == 'needs_attention'
    assert not store.operations(run['id'])


def test_policy_is_enforced_even_if_reviewer_and_human_approve(env):
    store, _ = env
    run = waiting(env, 'TF-1004')
    proposal_id = run['approval']['proposal_id']
    # Simulate an incorrect model proposal; deterministic executor remains authoritative.
    with store.connect() as db:
        db.execute("UPDATE proposals SET action='restore_access' WHERE id=?", (proposal_id,))
    store.decide(run['id'], run['approval']['id'], 'approve')
    store.claim()
    with pytest.raises(Conflict, match='执行条件'):
        store.execute(run['id'], proposal_id)
    assert store.ticket('TF-1004')['facts']['account']['status'] == 'locked'


def test_cross_run_proposal_reference_rejected(env):
    store, _ = env
    first = waiting(env)
    other = store.create_run('TF-1002', 'scripted')
    with pytest.raises(KeyError):
        store.execute(other['id'], first['approval']['proposal_id'])


def test_cancellation_invalidates_approval(env):
    store, _ = env
    run = waiting(env)
    store.cancel(run['id'])
    with pytest.raises(Conflict):
        store.decide(run['id'], run['approval']['id'], 'approve')
    assert store.run(run['id'])['status'] == 'canceled'
    assert not store.operations(run['id'])


def test_duplicate_active_run_prevented(env):
    store, _ = env
    store.create_run('TF-1001', 'scripted')
    with pytest.raises(Conflict):
        store.create_run('TF-1001', 'scripted')


def test_global_budget_covers_all_agents(env):
    store, settings = env
    settings.max_model_calls = 4
    run = store.create_run('TF-1001', 'scripted')
    result = advance(store, settings, run['id'])
    assert result['status'] == 'failed'
    assert result['model_calls'] == 4
    assert not store.operations(run['id'])


def test_finding_requires_source_access_and_proposal_needs_both_roles(env):
    store, _ = env
    run = store.create_run('TF-1001', 'scripted')
    with pytest.raises(Conflict):
        store.finding(run['id'], 'account', '没有读取证据的结论')
    with pytest.raises(Conflict):
        store.propose(run['id'], ProposalInput(action='escalate', reason='证据不足请转人工处理', evidence_ids=['fake-a','fake-b']))


def test_specialists_cannot_delegate_or_execute(env, monkeypatch):
    from app.scripted import ScriptedModel
    tools_by_role = {}
    def capture(self, tools, **kwargs):
        tools_by_role[self.role] = {t.name for t in tools}
        return self
    monkeypatch.setattr(ScriptedModel, 'bind_tools', capture)
    waiting(env)
    assert tools_by_role['account'] == {'inspect_account','record_finding','read_policy'}
    assert tools_by_role['platform'] == {'inspect_platform','record_finding','read_policy'}
    assert tools_by_role['reviewer'] == {'read_proposal','record_review'}
    assert {'task','execute_resolution'} <= tools_by_role['supervisor']
    assert not {'write_file','execute','edit_file'} & tools_by_role['supervisor']


def test_premature_textual_approval_is_repaired_once(env, monkeypatch):
    from app.scripted import ScriptedModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    original = ScriptedModel._generate
    def premature(self, messages, *args, **kwargs):
        last = messages[-1]
        if self.role=='supervisor' and isinstance(last, ToolMessage) and last.name=='task' and 'approved' in str(last.content):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content='审核通过，请用户批准。'))])
        return original(self, messages, *args, **kwargs)
    monkeypatch.setattr(ScriptedModel, '_generate', premature)
    run = waiting(env)
    events = env[0].events(run['id'])
    assert sum(e['kind']=='workflow_repair' for e in events) == 1
    assert not env[0].operations(run['id'])


def test_retry_preserves_usage_and_rejects_active_or_completed_run(env):
    store, settings = env
    settings.max_model_calls = 1
    run = store.create_run('TF-1001','scripted')
    failed = advance(store, settings, run['id'])
    assert failed['status'] == 'failed'
    store.retry(run['id'])
    assert store.run(run['id'])['model_calls'] == failed['model_calls']
    with pytest.raises(Conflict):
        store.retry(run['id'])
    retried = advance(store, settings, run['id'])
    assert retried['status'] == 'failed' and not store.operations(run['id'])
