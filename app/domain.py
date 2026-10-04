from typing import Literal
from pydantic import BaseModel, Field, ConfigDict

Action = Literal["restore_access", "retry_export", "escalate"]


class ProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action
    reason: str = Field(min_length=5, max_length=400)
    evidence_ids: list[str] = Field(min_length=2, max_length=8)


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: str
    decision: Literal["approve", "reject"]
    feedback: str = Field(default="", max_length=1000)


POLICIES = {
    "restore_access": "仅对已核验身份、已有主管授权且账号被锁定的员工恢复访问；不得提升角色权限。",
    "retry_export": "仅在任务失败、允许重试且不存在平台事故时重试导出；一个方案至多执行一次。",
    "escalate": "事实缺失、策略不允许、平台事故或人工拒绝自动处理并要求转交时，可转人工支持；即使原动作满足条件也允许转交。转人工不执行权限变更或作业重试，新方案仍需审核及独立人工批准。",
}


SOURCE_FIELDS = {
    'account': ('status', 'identity_verified', 'manager_approved', 'role'),
    'platform': ('job_status', 'retryable', 'incident', 'error'),
}
ACTION_REQUIREMENTS = {
    'restore_access': ('account', {'identity_verified': True, 'manager_approved': True, 'status': 'locked'}),
    'retry_export': ('platform', {'job_status': 'failed', 'retryable': True, 'incident': False}),
}
EVIDENCE_RULE = ('字段仅由source_scope指定的数据源证明；其他源不含该字段不代表事实缺失或冲突。'
                 'summary是模型解释，不能覆盖结构化facts或规则检查。真正缺失的权威字段保持未知，不得猜测。')


def action_checks(facts: dict) -> dict:
    """Explain existing action prerequisites using only their authoritative source."""
    result = {}
    for action, (source, required) in ACTION_REQUIREMENTS.items():
        data = facts.get(source, {})
        conditions = []
        for field, expected in required.items():
            actual = data.get(field)
            matches = actual is expected if isinstance(expected, bool) else actual == expected
            status = 'missing' if actual is None else ('met' if matches else 'not_met')
            conditions.append({'field': field, 'actual': actual, 'expected': expected, 'status': status})
        result[action] = {'source': source, 'eligible': all(c['status'] == 'met' for c in conditions),
                          'conditions': conditions}
    return result


def allowed_actions(facts: dict) -> set[str]:
    # Escalation remains available; eligibility is not approval or automatic selection.
    return {'escalate'} | {action for action, check in action_checks(facts).items() if check['eligible']}


def demo_tickets() -> list[dict]:
    normal_account = {"status": "active", "identity_verified": True, "manager_approved": True, "role": "viewer"}
    normal_platform = {"job_status": "none", "retryable": False, "incident": False, "error": "none"}
    return [
        {"id": "TF-1001", "title": "新员工登录提示账号锁定", "description": "身份已核验，主管已批准只读账号。登录仍提示锁定，请恢复访问。", "category": "access", "priority": "P2", "facts": {"account": {**normal_account, "status": "locked"}, "platform": normal_platform}},
        {"id": "TF-1002", "title": "季度报表导出任务失败", "description": "导出任务因临时网络超时失败，请核查账号权限和作业状态，恢复导出。", "category": "export", "priority": "P2", "facts": {"account": normal_account, "platform": {**normal_platform, "job_status": "failed", "retryable": True, "error": "UPSTREAM_TIMEOUT"}}},
        {"id": "TF-1003", "title": "多名员工同时无法导出", "description": "多个账号报错。请调查是账号问题还是平台事故，给出处理方案。", "category": "incident", "priority": "P1", "facts": {"account": normal_account, "platform": {**normal_platform, "job_status": "failed", "retryable": True, "incident": True, "error": "STORAGE_UNAVAILABLE"}}},
        {"id": "TF-1004", "title": "申请紧急解除账号限制", "description": "身份核验尚未完成，要求跳过授权直接解锁。请按企业政策处理。", "category": "access", "priority": "P2", "facts": {"account": {**normal_account, "status": "locked", "identity_verified": False, "manager_approved": False}, "platform": normal_platform}},
    ]
