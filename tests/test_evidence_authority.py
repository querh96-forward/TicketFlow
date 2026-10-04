"""Authoritative fields, incomplete facts and misleading expert summaries."""
import asyncio
from copy import deepcopy

import pytest
from langchain_core.messages import ToolMessage

from app.config import Settings
from app.domain import action_checks, allowed_actions, demo_tickets, ProposalInput
from app.engine import Engine
from app.scripted import ScriptedModel, unpack
from app.store import Store, encode


@pytest.mark.parametrize('value', [None, False, 'true', 1])
def test_authorization_is_not_inferred_from_platform_or_truthy_values(value):
    facts = deepcopy(demo_tickets()[0]['facts'])
    if value is None:
        facts['account'].pop('identity_verified')
    else:
        facts['account']['identity_verified'] = value
    facts['platform']['identity_verified'] = True
    check = action_checks(facts)['restore_access']
    assert not check['eligible']
    assert check['conditions'][0]['status'] == ('missing' if value is None else 'not_met')
    assert 'restore_access' not in allowed_actions(facts)


def test_missing_cross_domain_fields_do_not_invalidate_known_account_facts():
    facts = deepcopy(demo_tickets()[0]['facts'])
    facts['platform']['status'] = 'active'  # Non-authoritative duplicate must not override account.
    check = action_checks(facts)['restore_access']
    assert check['source'] == 'account' and check['eligible']
    assert all(c['status'] == 'met' for c in check['conditions'])
    assert 'restore_access' in allowed_actions(facts)
    del facts['account']['status']
    facts['platform']['status'] = 'locked'
    assert not action_checks(facts)['restore_access']['eligible']


def test_platform_incident_must_be_explicitly_false_in_its_own_source():
    facts = deepcopy(demo_tickets()[1]['facts'])
    assert action_checks(facts)['retry_export']['eligible']
    del facts['platform']['incident']
    facts['account']['incident'] = False
    check = action_checks(facts)['retry_export']
    assert not check['eligible']
    assert next(c for c in check['conditions'] if c['field'] == 'incident')['status'] == 'missing'
    assert allowed_actions(facts) == {'escalate'}


def test_review_checks_come_from_frozen_run_not_summary_or_legacy_proposal(tmp_path):
    store = Store(tmp_path / 'tickets.sqlite')
    run = store.create_run('TF-1001', 'scripted')
    for role in ('account', 'platform'):
        store.event(run['id'], 'evidence_read', role, {})
        store.finding(run['id'], role, '平台没有锁定记录，所以账号锁定事实缺失。')
    findings = store.findings(run['id'])
    account = next(f for f in findings if f['role'] == 'account')
    platform = next(f for f in findings if f['role'] == 'platform')
    assert account['policy_checks']['restore_access']['eligible']
    assert 'status' not in platform['source_scope']
    assert 'restore_access' not in platform['policy_checks']
    proposal = store.propose(run['id'], ProposalInput(action='escalate', reason='错误引用平台摘要称锁定事实缺失。',
                            evidence_ids=[f['evidence_id'] for f in findings]))
    # Older proposals lack metadata; even corrupted model-facing snapshots cannot
    # replace the run's authoritative facts when preparing review context.
    legacy = [{'role':'account', 'facts':{}, 'summary':'账号事实缺失'}]
    with store.connect() as db:
        db.execute('UPDATE proposals SET findings=? WHERE id=?', (encode(legacy), proposal['id']))
    context = store.review_context(run['id'], proposal['id'])
    assert context['fact_assessment']['policy_checks']['restore_access']['eligible']
    assert context['human_decisions'] == []
    assert 'escalate' in allowed_actions(run['facts'])  # Conservative escalation is still legal.


@pytest.mark.parametrize('architecture', ['single', 'multi'])
@pytest.mark.parametrize('missing_identity', [False, True])
def test_real_graph_delivers_scoped_checks_to_reviewer_without_relaxing_approval(tmp_path, monkeypatch, architecture, missing_identity):
    store = Store(tmp_path / 'tickets.sqlite')
    if missing_identity:
        facts = store.ticket('TF-1001')['facts']
        facts['account'].pop('identity_verified')
        with store.connect() as db:
            db.execute('UPDATE tickets SET facts=? WHERE id=?', (encode(facts), 'TF-1001'))
    original_finding = store.finding
    def misleading_finding(rid, role, summary):
        if role == 'platform':
            summary = '平台无登录或锁定记录，所以恢复账号的证据不足。'
        return original_finding(rid, role, summary)
    monkeypatch.setattr(store, 'finding', misleading_finding)
    seen = []
    original_generate = ScriptedModel._generate
    def inspect(self, messages, *args, **kwargs):
        last = messages[-1]
        if isinstance(last, ToolMessage) and last.name == 'read_proposal':
            context = unpack(last)
            check = context['fact_assessment']['policy_checks']['restore_access']
            assert check['eligible'] is not missing_identity
            assert check['source'] == 'account'
            assert 'status' not in context['fact_assessment']['source_scope']['platform']
            seen.append(context)
        return original_generate(self, messages, *args, **kwargs)
    monkeypatch.setattr(ScriptedModel, '_generate', inspect)
    settings = Settings(data_dir=tmp_path, model_mode='scripted', llm_api_key='')
    run = store.create_run('TF-1001', 'scripted')
    assert store.claim() == run['id']
    asyncio.run(Engine(store, settings, architecture=architecture).process(run['id']))
    pending = store.run(run['id'])
    assert seen and pending['status'] == 'waiting_approval', pending['error']
    expected = 'escalate' if missing_identity else 'restore_access'
    assert pending['approval']['proposal']['action'] == expected
    assert not store.operations(run['id'])
    store.decide(run['id'], pending['approval']['id'], 'approve')
    assert store.claim() == run['id']
    asyncio.run(Engine(store, settings, architecture=architecture).process(run['id']))
    assert store.run(run['id'])['status'] == 'completed'
    assert [op['result']['action'] for op in store.operations(run['id'])] == [expected]
