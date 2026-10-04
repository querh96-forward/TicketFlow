"""Summarize every trial in a completed repeated comparison; never invoke models."""
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import statistics


def percentile(values, fraction):
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def escaped(value):
    return str(value).replace('|', '\\|').replace('\n', ' ')


def render(source, output):
    report = json.loads(source.read_text())
    expected = {(c, a, n) for c in report['case_ids'] for a in ('single', 'multi')
                for n in range(1, report['repeats'] + 1)}
    rows = report['results']
    actual = [(r['case_id'], r['architecture'], r['repeat']) for r in rows]
    if not report['complete'] or len(actual) != len(expected) or set(actual) != expected:
        raise ValueError('Expected a complete report with every planned trial exactly once.')
    groups = {a: [r for r in rows if r['architecture'] == a] for a in ('single', 'multi')}
    per_case = {(c, a): sorted([r for r in rows if r['case_id'] == c and r['architecture'] == a],
                              key=lambda r: r['repeat']) for c in report['case_ids'] for a in groups}
    lines = ['# 单／多 Agent 重复评测结果 · 2026-10-03', '',
             f"模型：`{report['model']}`。{len(report['case_ids'])}个场景 × 每组{report['repeats']}次 × 两种架构，共{len(rows)}次真实任务。全部尝试均纳入汇总。", '',
             '场景通过包括正确阻止过期操作；业务完成另计。人工决定由脚本在隔离演示库中模拟。', '',
             '| 指标 | 单Agent | 多Agent |', '|---|---:|---:|']
    infra = [r for r in rows if r['error'].startswith(('MODEL_AUTH:', 'MODEL_CONNECTION:', 'MODEL_UNAVAILABLE:', 'MODEL_RATE_LIMIT:'))]
    if infra:
        lines[2:2] = [f"**服务异常提示：本报告有{len(infra)}条模型服务失败。调度器完成所有记录，不代表完成了有效的模型能力评测。以下原始通过率包含服务失败，不能用于判断架构优劣。**", '']
    metrics = [
        ('场景通过', lambda rs: f"{sum(r['passed'] for r in rs)}/{len(rs)}（{sum(r['passed'] for r in rs)/len(rs):.2%}）"),
        ('业务完成', lambda rs: sum(r['business_completed'] for r in rs)),
        ('违规执行', lambda rs: sum(r['illegal_execution_count'] for r in rs)),
        ('违规方案', lambda rs: sum(r['unsafe_proposal_count'] for r in rs)),
        ('总Token', lambda rs: f"{sum(r['tokens'] for r in rs):,}"),
        ('平均Token／任务', lambda rs: f"{statistics.mean(r['tokens'] for r in rs):,.1f}"),
        ('平均模型调用／任务', lambda rs: f"{statistics.mean(r['model_calls'] for r in rs):.2f}"),
        ('耗时中位数／秒', lambda rs: f"{statistics.median(r['elapsed_ms'] for r in rs)/1000:.2f}"),
        ('耗时P95／秒（线性插值）', lambda rs: f"{percentile([r['elapsed_ms'] for r in rs],.95)/1000:.2f}"),
    ]
    for label, fn in metrics:
        lines.append(f"| {label} | {fn(groups['single'])} | {fn(groups['multi'])} |")
    lines += ['', '## 各场景重复结果', '', '括号内依次为第1、2、3次结果；通过记✓，未通过记×。', '',
              '| 场景 | 单Agent | 多Agent |', '|---|---|---|']
    for case in report['case_ids']:
        cells = []
        for a in groups:
            rs = per_case[case, a]
            cells.append(f"{sum(r['passed'] for r in rs)}/{len(rs)}（{' '.join('✓' if r['passed'] else '×' for r in rs)}）")
        lines.append(f"| {case} | {' | '.join(cells)} |")
    lines += ['', '三次均通过只是本轮观察，不能保证后续一定成功。', '']
    for a in groups:
        stable = sum(all(r['passed'] for r in per_case[c, a]) for c in report['case_ids'])
        variable = sum(0 < sum(r['passed'] for r in per_case[c, a]) < report['repeats'] for c in report['case_ids'])
        lines.append(f"- {a}：全部重复均通过的场景{stable}/{len(report['case_ids'])}；通过与失败混合的场景{variable}个。")
    lines += ['', '## 审核补交与安全控制', '',
              '| 指标 | 单Agent | 多Agent |', '|---|---:|---:|']
    controls = {}
    for a, rs in groups.items():
        reminders = [(r, e) for r in rs for e in r['events'] if e['kind'] == 'review_repair_requested']
        saved = sum(any(e['kind'] == 'review' and e['id'] > reminder['id'] and
                        e['data']['proposal_id'] == reminder['data']['proposal_id'] for e in r['events'])
                    for r, reminder in reminders)
        controls[a] = {
            '补交提醒次数': len(reminders),
            '提醒后同方案保存审核的次数': saved,
            '触发补交的任务数': sum(any(e['kind'] == 'review_repair_requested' for e in r['events']) for r in rs),
            '触发补交且最终场景通过的任务数': sum(r['passed'] and any(e['kind'] == 'review_repair_requested' for e in r['events']) for r in rs),
            '交接被阻止的任务数': sum(any(e['kind'] == 'review_handoff_blocked' for e in r['events']) for r in rs),
            '审批前执行的任务数': sum(not r['checks']['no_execution_before_approval'] for r in rs),
            '多条操作回执的任务数': sum(len(r['operations']) > 1 for r in rs),
            '版本变化场景中的操作回执数': sum(len(r['operations']) for r in rs if r['case_id'] == 'stale-after-approval'),
        }
    for label in controls['single']:
        lines.append(f"| {label} | {controls['single'][label]} | {controls['multi'][label]} |")
    lines += ['', '保存审核不等于审核同意，也不等于最终任务通过。单Agent没有独立审核交接补交机制，两组实现差异见实验方法。', '',
              '## 完整失败清单', '', '| 场景／架构／轮次 | 状态 | 未满足断言 | 错误 |', '|---|---|---|---|']
    failures = [r for r in rows if not r['passed']]
    for r in failures:
        checks = ', '.join(k for k,v in r['checks'].items() if not v)
        lines.append(f"| {r['case_id']} / {r['architecture']} / {r['repeat']} | {r['status']} | {checks} | {escaped(r['error'] or '无系统异常，需结合轨迹分析')} |")
    if not failures:
        lines.append('| 无 | — | — | — |')
    lines += ['', '## 范围与追溯', '',
              f"全轮服务返回用量：**{sum(r['tokens'] for r in rows):,} Token**。不是账单核对；失败尝试的返回用量全部计入。", '',
              '这是已知合成场景的重复回归，12个场景不是72个独立样本；未做盲测，不能称为生产成功率。所有任务成本包含失败；提前失败会影响平均成本，必须结合逐题结果解读。', '',
              f"[原始72次轨迹]({os.path.relpath(source,output.parent)}) · [预先固定的协议](evaluation-protocol-20261003.md) · [两种架构与评分规则](comparison-method.md)", '',
              f"源码指纹：`{report['source_sha256']}`。案例指纹：`{report['cases_sha256']}`。", '']
    output.write_text('\n'.join(lines))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    render(args.source, args.output)
    print(args.output)
