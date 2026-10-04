"""Require a durable decision before a reviewer returns to the supervisor."""
import json
import re

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage

from app.store import ReviewHandoffError


class PersistedReviewGate(AgentMiddleware):
    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id

    def proposal_id(self, state):
        # Deep Agents gives a named subagent a fresh HumanMessage with its task.
        # Bind to the requested proposal, never whichever proposal was last read/written.
        instruction = next((m.content for m in state['messages'] if isinstance(m, HumanMessage)), '')
        # Chinese text has no ASCII word boundary; task/evidence IDs can also
        # appear in a handoff. Resolve only proposals belonging to this run.
        ids = set(re.findall(r'(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])', str(instruction)))
        proposals = {p['id'] for p in self.store.proposals(self.run_id)}
        targets = ids & proposals
        if len(targets) != 1:
            raise ReviewHandoffError('审核委派必须包含唯一方案编号，已停止交接，请人工检查。')
        pid = targets.pop()
        try:
            self.store.proposal(self.run_id, pid)
        except KeyError as exc:
            raise ReviewHandoffError('审核方案不属于当前任务，已停止交接，请人工检查。') from exc
        return pid

    @hook_config(can_jump_to=['model'])
    async def aafter_model(self, state, runtime):
        last = state['messages'][-1]
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None
        self.store.assert_current_snapshot(self.run_id)
        pid = self.proposal_id(state)
        review = self.store.proposal(self.run_id, pid)['review']
        if review is not None:
            # Forward the stored decision, even if final model prose contradicts it.
            return {'messages':[last.model_copy(update={'content':json.dumps(
                {'proposal_id':pid, **review}, ensure_ascii=False)})]}
        if not last.id:
            raise ReviewHandoffError('审核返回缺少消息编号，无法安全补交，请人工检查。')
        if self.store.request_review_repair(self.run_id, pid, last.id):
            return {'messages':[HumanMessage(
                id=f'review-reminder:{pid}',
                content=f'流程校验：方案 {pid} 尚无持久化审核意见。这是唯一一次补交提醒。'
                        '请先用read_proposal核对该方案，再调用record_review保存真实的同意或拒绝及理由。'
                        '不得仅返回文字，不得自动同意；工具成功后结束审核。')], 'jump_to':'model'}
        raise ReviewHandoffError(f'方案 {pid} 经一次提醒仍未保存审核意见，已停止交接；需要人工处理。')
