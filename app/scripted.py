"""Deterministic model double: exercises the real graph, tools and interrupts without API calls.

This is explicitly labeled scripted in API/UI/reports. It is not an LLM benchmark.
"""
import json
import re
import uuid

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.domain import allowed_actions


def unpack(message):
    try:
        return json.loads(message.content)
    except (ValueError, TypeError):
        return {'text': str(message.content)}


def call(name, **args):
    return {'name': name, 'args': args, 'id': uuid.uuid4().hex, 'type': 'tool_call'}


class ScriptedModel(BaseChatModel):
    role: str = 'supervisor'
    model_name: str = 'ticketflow:scripted'

    @property
    def _llm_type(self):
        return 'ticketflow-scripted'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        tools = [m for m in messages if isinstance(m, ToolMessage)]
        last = tools[-1] if tools else None
        content, calls = '', []
        if self.role == 'single':
            names = [m.name for m in tools]
            findings = next((unpack(m) for m in reversed(tools) if m.name == 'get_findings'), [])
            if last is None:
                calls = [call('read_ticket')]
            elif last.name == 'read_ticket':
                calls = [call('inspect_account')]
            elif last.name == 'inspect_account':
                calls = [call('record_account_finding', summary=json.dumps(unpack(last), ensure_ascii=False)[:400])]
            elif last.name == 'record_account_finding':
                calls = [call('inspect_platform')]
            elif last.name == 'inspect_platform':
                calls = [call('record_platform_finding', summary=json.dumps(unpack(last), ensure_ascii=False)[:400])]
            elif last.name == 'record_platform_finding':
                calls = [call('read_policy')]
            elif last.name == 'read_policy':
                calls = [call('get_findings')]
            elif last.name == 'get_findings':
                allowed = allowed_actions({f['role']:f['facts'] for f in findings})
                action = next(a for a in ('restore_access','retry_export','escalate') if a in allowed)
                calls = [call('submit_proposal', action=action, reason='已核对两类数据及业务策略后制定方案。', evidence_ids=[f['evidence_id'] for f in findings])]
            elif last.name == 'submit_proposal' and 'id' in unpack(last):
                calls = [call('read_proposal', proposal_id=unpack(last)['id'])]
            elif last.name == 'read_proposal' and 'proposal' in unpack(last):
                p = unpack(last)['proposal']
                allowed = allowed_actions({f['role']:f['facts'] for f in p['findings']})
                calls = [call('record_review', proposal_id=p['id'], approved=p['action'] in allowed, reason='单Agent自检事实和策略，不代表人工批准。')]
            elif last.name == 'record_review' and unpack(last).get('approved'):
                pid = next(unpack(m)['id'] for m in reversed(tools) if m.name == 'submit_proposal' and 'id' in unpack(m))
                calls = [call('execute_resolution', proposal_id=pid)]
            elif last.name == 'execute_resolution':
                result = unpack(last)
                if result.get('action'):
                    content = '操作完成：'+json.dumps(result,ensure_ascii=False)
                elif 'error' not in result:
                    calls = [call('submit_proposal',action='escalate',reason='人工退回原方案，改为转人工支持。',evidence_ids=[f['evidence_id'] for f in findings])]
                else:
                    content = '执行被阻止，需人工进一步调查。'
            else:
                content = '当前流程需人工处理。'
        elif self.role in ('account', 'platform'):
            if last is None:
                calls = [call(f'inspect_{self.role}')]
            elif last.name == f'inspect_{self.role}':
                calls = [call('read_policy')]
            elif last.name == 'read_policy':
                facts = unpack(next(m for m in tools if m.name == f'inspect_{self.role}'))
                calls = [call('record_finding', summary=json.dumps(facts, ensure_ascii=False))]
            else:
                content = json.dumps(unpack(last), ensure_ascii=False)
        elif self.role == 'reviewer':
            if last is None:
                user_text = str(messages[-1].content)
                pid = re.search(r'\b[0-9a-f]{32}\b', user_text).group(0)
                calls = [call('read_proposal', proposal_id=pid)]
            elif last.name == 'read_proposal':
                proposal = unpack(last)['proposal']
                facts = {f['role']: f['facts'] for f in proposal['findings']}
                approved = proposal['action'] in allowed_actions(facts)
                calls = [call('record_review', proposal_id=proposal['id'], approved=approved,
                              reason='已核对两个专家的事实及执行策略。' if approved else '事实不满足业务策略，请转人工处理。')]
            else:
                content = json.dumps(unpack(last), ensure_ascii=False)
        else:
            proposals = [unpack(m) for m in tools if m.name == 'submit_proposal' and 'id' in unpack(m)]
            findings = next((unpack(m) for m in reversed(tools) if m.name == 'get_findings'), [])
            if last is None:
                calls = [call('read_ticket')]
            elif last.name == 'read_ticket':
                calls = [call('task', subagent_type=role, description='调查当前工单，读取授权范围的数据并保存证据摘要。') for role in ('account', 'platform')]
            elif last.name == 'task' and not proposals:
                calls = [call('get_findings')]
            elif last.name == 'get_findings':
                facts = {f['role']: f['facts'] for f in findings}
                allowed = allowed_actions(facts)
                action = next(a for a in ('restore_access', 'retry_export', 'escalate') if a in allowed)
                calls = [call('submit_proposal', action=action, reason='根据账号与平台专家的证据及业务策略制定处理方案。', evidence_ids=[f['evidence_id'] for f in findings])]
            elif last.name == 'submit_proposal' and 'id' in unpack(last):
                calls = [call('task', subagent_type='reviewer', description=f"独立审核方案 {unpack(last)['id']}，读取证据和策略后记录审核意见。")]
            elif last.name == 'task' and proposals:
                review = unpack(last)
                if review.get('approved'):
                    calls = [call('execute_resolution', proposal_id=proposals[-1]['id'])]
                else:
                    content = '方案未获审核通过，需要人工处理。'
            elif last.name == 'execute_resolution':
                result = unpack(last)
                if result.get('action'):
                    content = f"已依据两位专家的证据完成审核，并在人工批准后执行 {result['action']}。工单状态：{result['ticket_status']}。本次操作仅作用于演示业务库。"
                elif 'error' not in result and proposals[-1]['action'] != 'escalate':
                    calls = [call('submit_proposal', action='escalate', reason='人工拒绝原方案，转人工支持进一步处理。', evidence_ids=[f['evidence_id'] for f in findings])]
                else:
                    content = '操作未执行，需要人工进一步处理。'
            else:
                content = '流程未形成有效方案，需要人工处理。'
        message = AIMessage(content=content, tool_calls=calls)
        return ChatResult(generations=[ChatGeneration(message=message)])
