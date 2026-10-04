import asyncio
import contextlib
import json
import uuid

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from langchain_core.messages import HumanMessage

from app.agents import build_graph
from app.observability import error_info
from app.store import StaleSnapshot, ReviewHandoffError


class Engine:
    def __init__(self, store, settings, model_factory=None, architecture='multi'):
        self.store, self.settings, self.model_factory = store, settings, model_factory
        self.architecture = architecture

    async def process(self, run_id):
        run = self.store.run(run_id)
        config = {'configurable': {'thread_id': run_id}, 'recursion_limit': 100}
        try:
            self.store.assert_current_snapshot(run_id)
            async with AsyncSqliteSaver.from_conn_string(str(self.settings.data_dir / 'checkpoints.sqlite')) as saver:
                graph = build_graph(self.store, run_id, self.settings, saver, self.model_factory, self.architecture)
                snapshot = await graph.aget_state(config)
                if snapshot.values:
                    pending = snapshot.interrupts
                    graph_input = Command(resume=run['resume']) if pending and run['resume'] else None
                else:
                    ticket = self.store.ticket(run['ticket_id'])
                    graph_input = {'messages': [{'role': 'user', 'content': f"处理工单 {ticket['id']}：{ticket['title']}。{ticket['description']}\n补充要求：{run['feedback']}"}]}
                async def execute_graph():
                    result = await graph.ainvoke(graph_input, config)
                    self.store.assert_current_snapshot(run_id)
                    # Repair one premature textual stop; never fabricate a tool call or an approval.
                    if not result.get('__interrupt__') and not self.store.operations(run_id):
                        proposal = self.store.unrequested_reviewed_proposal(run_id)
                        if proposal:
                            self.store.event(run_id, 'workflow_repair', 'system', {'reason':'reviewed_proposal_without_approval_request', 'proposal_id':proposal['id']})
                            result = await graph.ainvoke({'messages':[HumanMessage(content=f"流程校验：方案 {proposal['id']} 已审核，但尚未发起审批。请立即调用execute_resolution，该调用由框架中断以生成审批卡，并不会绕过人工直接执行。不要仅用文字等待批准。") ]}, config)
                    return result
                result = await asyncio.wait_for(execute_graph(), self.settings.run_timeout_seconds)
                interruptions = result.get('__interrupt__', [])
                if interruptions:
                    if len(interruptions) != 1:
                        raise ValueError('仅支持一个待审批中断。')
                    interruption = interruptions[0]
                    requests = interruption.value.get('action_requests', [])
                    if len(requests) != 1 or requests[0]['name'] != 'execute_resolution':
                        raise ValueError('待审批工具不符合执行协议。')
                    pid = requests[0]['args']['proposal_id']
                    proposal = self.store.proposal(run_id, pid)
                    if not proposal['review'] or not proposal['review']['approved']:
                        raise ValueError('待审批方案尚未通过独立审核。')
                    approval = {'id': uuid.uuid4().hex, 'interrupt_id': interruption.id,
                                'proposal_id': pid, 'proposal': proposal}
                    self.store.request_approval(run_id, approval)
                else:
                    messages = result.get('messages', [])
                    answer = str(messages[-1].content) if messages else ''
                    status = 'completed' if self.store.operations(run_id) else 'needs_attention'
                    self.store.update(run_id, status=status, answer=answer, resume=None, approval=None)
                    self.store.event(run_id, status, 'system', {'answer': answer})
        except ReviewHandoffError as exc:
            reason = str(exc)
            operations = self.store.operations(run_id)
            status, answer = 'needs_attention', reason
            if operations:
                receipt = operations[0]['result']
                status = 'completed_with_warning'
                answer = f"业务操作已完成：{receipt['action']}，工单状态：{receipt['ticket_status']}。后续审核交接异常，请以操作记录为准。"
            self.store.update(run_id, status=status, answer=answer,
                              error=f'REVIEW_NOT_RECORDED: {reason}', resume=None, approval=None)
            self.store.event(run_id, 'review_handoff_blocked', 'reviewer', {'reason':reason})
            self.store.event(run_id, status, 'system', {'answer':answer, 'code':'REVIEW_NOT_RECORDED'})
        except StaleSnapshot as exc:
            answer = str(exc)
            self.store.update(run_id, status='needs_attention', answer=answer,
                              error=f'STALE_SNAPSHOT: {answer}', resume=None, approval=None)
            self.store.event(run_id, 'tool_error', 'system',
                             {'message':answer, 'error_type':'StaleSnapshot', 'code':'STALE_SNAPSHOT', 'retryable':False})
            self.store.event(run_id, 'needs_attention', 'system', {'answer':answer, 'code':'STALE_SNAPSHOT'})
        except asyncio.CancelledError:
            # Leave running state for the next worker to recover from its checkpoint.
            raise
        except Exception as exc:
            diagnostic = error_info(exc, self.settings.llm_api_key.get_secret_value())
            error = f"{diagnostic['code']}: {diagnostic['message']}"
            operations = self.store.operations(run_id)
            if operations:
                # The committed business receipt is authoritative, even if final LLM narration fails.
                receipt = operations[0]['result']
                answer = f"业务操作已完成：{receipt['action']}，工单状态：{receipt['ticket_status']}。模型总结未完成，请以操作记录为准。"
                self.store.update(run_id, status='completed_with_warning', answer=answer, error=error, resume=None, approval=None)
                self.store.event(run_id, 'completed_with_warning', 'system', {'answer': answer, 'error': error, 'diagnostic':diagnostic})
            else:
                self.store.update(run_id, status='failed', error=error, resume=None, approval=None)
                self.store.event(run_id, 'failed', 'system', {'error': error, 'diagnostic':diagnostic})

    async def worker(self):
        self.store.recover()
        while True:
            run_id = self.store.claim()
            if run_id:
                await self.process(run_id)
            else:
                await asyncio.sleep(0.25)
