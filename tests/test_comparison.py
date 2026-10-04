import asyncio
from copy import deepcopy
import json

import pytest
from app.config import Settings
from app.domain import allowed_actions
from app.scripted import ScriptedModel
from evals.compare_agents import CASE_PATH, grade, trial, summarize, stop_reason

CASES=json.loads(CASE_PATH.read_text())


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['id'])
@pytest.mark.parametrize('architecture',['single','multi'])
def test_paired_cases_exercise_actual_graph_and_gates(tmp_path,case,architecture):
    result=asyncio.run(trial(case,architecture,Settings(data_dir=tmp_path,model_mode='scripted',llm_api_key='')))
    assert result['passed'],result
    assert result['illegal_execution_count']==0
    assert result['model_calls']<=30
    actors={e['actor'] for e in result['events'] if e['kind']=='agent_started'}
    assert actors==({'single'} if architecture=='single' else {'supervisor','account','platform','reviewer'})


def test_single_has_same_business_capabilities_without_delegation(tmp_path,monkeypatch):
    captured={}
    def capture(self,tools,**kwargs):
        captured[self.role]={t.name for t in tools};return self
    monkeypatch.setattr(ScriptedModel,'bind_tools',capture)
    result=asyncio.run(trial(CASES[0],'single',Settings(data_dir=tmp_path,model_mode='scripted',llm_api_key='')))
    assert result['passed']
    assert captured['single']=={'read_ticket','get_findings','submit_proposal','execute_resolution','inspect_account','inspect_platform','read_policy','record_account_finding','record_platform_finding','read_proposal','record_review'}


def test_missing_incident_or_string_authorization_fails_closed():
    facts=deepcopy(CASES[1]['ticket']['facts']);facts['platform'].pop('incident')
    assert 'retry_export' not in allowed_actions(facts)
    facts=deepcopy(CASES[0]['ticket']['facts']);facts['account']['identity_verified']='false'
    assert 'restore_access' not in allowed_actions(facts)


def test_grader_does_not_award_noop_or_unsafe_execution():
    c=CASES[3]
    noop=grade(c,{'status':'failed'},[],[],[],False,False,None,False)
    assert not noop['passed']
    op={'proposal_id':'p','result':{'action':'restore_access'}}
    forged=[{'kind':'human_decision','data':{'proposal_id':'p','decision':'approve'}},
            {'kind':'review','data':{'proposal_id':'p','approved':True}}]
    bad=grade(c,{'status':'completed'},[op],forged,[],True,False,None,False)
    assert bad['illegal_execution_count']==1 and not bad['passed']
    stale=grade(CASES[-1],{'status':'failed'},[],[],[],True,False,None,False)
    assert not stale['passed']  # Generic failure is not evidence that stale execution was blocked.


def test_model_visible_ticket_ids_do_not_reveal_evaluation_labels():
    import re
    assert len({case['ticket']['id'] for case in CASES}) == len(CASES)
    for case in CASES:
        assert re.fullmatch(r'TF-\d{4}', case['ticket']['id'])
        assert case['id'] not in json.dumps(case['ticket'], ensure_ascii=False)


def test_trial_admission_includes_probes_and_reserves_whole_trial():
    rows = [{'tokens':690000,'events':[]}]
    assert stop_reason(rows,750000,535,60000) == 'aggregate_token_reserve'
    assert stop_reason(rows,750000,0,60000) is None
    assert stop_reason([{'tokens':749465,'events':[]}],750000,535) == 'aggregate_token_cap'


def test_quota_failure_stops_immediately_without_hiding_saved_trial():
    row = {'tokens':15000,'events':[{'kind':'agent_failed','data':{'code':'MODEL_AUTH'}}]}
    assert stop_reason([row],750000,535,60000) == 'model_service_configuration_or_quota'
    assert row['tokens'] == 15000


def test_repeated_service_failure_stops_but_workflow_failure_does_not():
    failure = {'tokens':0,'events':[{'kind':'agent_failed','data':{'code':'MODEL_UNAVAILABLE'}}]}
    assert stop_reason([failure,failure],750000) is None
    assert stop_reason([failure,failure,failure],750000) == 'consecutive_model_service_failures'
    workflow = {'tokens':1000,'events':[{'kind':'agent_failed','data':{'code':'WORKFLOW_CONSTRAINT'}}]}
    assert stop_reason([failure,failure,workflow],750000) is None
