"""Application-level trace summaries and bounded, sanitized diagnostics."""
import re


def sanitize(value, secret=''):
    if isinstance(value, dict):
        return {k: ('[redacted]' if re.search(r'api.?key|authorization|password|secret', k, re.I)
                    else sanitize(v, secret)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, secret) for v in value]
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, '[redacted]')
        value = re.sub(r'\bsk-[A-Za-z0-9_.-]+', '[redacted]', value)
        return re.sub(r'(?i)\bBearer\s+[A-Za-z0-9_.~+/-]+=*', 'Bearer [redacted]', value)
    return value


def error_info(exc, secret=''):
    # Do not log raw provider response bodies, request headers or request payloads.
    name = type(exc).__name__
    status = getattr(exc, 'status_code', None)
    if not isinstance(status, int):
        status = None
    retryable = False
    if status in (401, 403):
        code, message = 'MODEL_AUTH', '模型服务认证或权限失败，请检查配置。'
    elif status == 429:
        code, message, retryable = 'MODEL_RATE_LIMIT', '模型服务限流，请稍后再试。', True
    elif status and status >= 500:
        code, message, retryable = 'MODEL_UNAVAILABLE', '模型服务暂不可用。', True
    elif isinstance(exc, TimeoutError) or 'Timeout' in name:
        code, message, retryable = 'TIMEOUT', '执行超时，请检查服务状态及任务预算。', True
    elif 'Connection' in name:
        code, message, retryable = 'MODEL_CONNECTION', '模型服务连接失败。', True
    elif status in (400, 404, 422) or 'InvalidRequest' in name or 'BadRequest' in name:
        code, message = 'MODEL_REQUEST', '模型服务拒绝请求，请检查模型名称及工具调用兼容性。'
    elif isinstance(exc, ValueError):
        code, message = 'WORKFLOW_CONSTRAINT', str(exc)
    elif isinstance(exc, KeyError):
        code, message = 'NOT_FOUND', '引用的记录不存在。'
    else:
        code, message = 'EXECUTION_ERROR', '执行异常，请检查任务轨迹。'
    return {'code':code, 'message':sanitize(message, secret)[:500], 'retryable':retryable,
            'error_type':name, 'http_status':status}


def trace_summary(events):
    roles = {}
    for event in events:
        kind, data = event['kind'], event['data']
        if kind not in ('agent_started', 'agent_finished', 'agent_failed', 'agent_interrupted'):
            continue
        row = roles.setdefault(event['actor'], {'calls':0, 'responses':0, 'failures':0,
                                               'interrupted':0, 'tokens':0, 'latency_ms':0, 'models':[]})
        if kind == 'agent_started':
            row['calls'] += 1
            model = data.get('model')
            if model and model not in row['models']:
                row['models'].append(model)
        else:
            field = {'agent_finished':'responses', 'agent_failed':'failures', 'agent_interrupted':'interrupted'}[kind]
            row[field] += 1
            row['latency_ms'] += data.get('latency_ms', 0)
            row['tokens'] += data.get('tokens', 0)
    return {'roles':roles, 'event_count':len(events),
            'usage_note':'耗时为各角色模型调用累计耗时，并行调用不可相加为任务总时长；Token仅统计服务已返回的用量。'}
