"""Combine disjoint frozen batches and keep provenance, failures and probe usage."""
import argparse
import copy
import hashlib
import json
import statistics
from pathlib import Path


def summarize_batches(paths, probe_path, output, markdown):
    batches = [json.loads(path.read_text()) for path in paths]
    probe = json.loads(probe_path.read_text())
    first = batches[0]
    keys = ('model', 'mode', 'source_sha256', 'cases_sha256', 'sampling', 'provider_fingerprint', 'repeats')
    for batch in batches:
        if any(batch[key] != first[key] for key in keys):
            raise ValueError('Batches have different frozen configurations')
    if probe['model'] != first['model']:
        raise ValueError('Probe and experiment must use the same model')
    prior = sum(row.get('usage', {}).get('total_tokens', 0) for row in probe['results'])
    probe_tokens = prior
    report = copy.deepcopy(first)
    report.update(results=[], cases=[], case_ids=[], source_reports=[])
    report['budgets']['prior_tokens'] = probe_tokens
    seen = set()
    for path, batch in zip(paths, batches):
        if batch['budgets']['prior_tokens'] != prior:
            raise ValueError('Batch admission did not include all previous usage')
        report['source_reports'].append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        report['cases'].extend(batch['cases'])
        report['case_ids'].extend(batch['case_ids'])
        for original in batch['results']:
            identity = (original['case_id'], original['architecture'], original['repeat'])
            if identity in seen:
                raise ValueError('Duplicate trial; cannot select or replace results')
            seen.add(identity)
            row = copy.deepcopy(original)
            row['source_report'] = str(path)
            row['source_execution_index'] = row['execution_index']
            row['execution_index'] = len(report['results']) + 1
            report['results'].append(row)
            prior += row['tokens']
    expected = {(c, a, n) for c in report['case_ids'] for a in ('single', 'multi')
                for n in range(1, report['repeats'] + 1)}
    report['complete'] = all(b['complete'] for b in batches) and seen == expected
    report['stopped_reasons'] = [b['stopped_reason'] for b in batches if b.get('stopped_reason')]
    report['total_returned_tokens_including_probe'] = prior
    report['probe_report'] = {'path': str(probe_path), 'tokens': probe_tokens,
                              'sha256': hashlib.sha256(probe_path.read_bytes()).hexdigest()}
    # Derived artifact only; preserve each raw batch and every original row.
    report.pop('summary', None)
    groups = {a: [r for r in report['results'] if r['architecture'] == a] for a in ('single', 'multi')}
    report['summary'] = {a: {'trials':len(rows), 'passes':sum(r['passed'] for r in rows),
                              'tokens':sum(r['tokens'] for r in rows)} for a, rows in groups.items()}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    lines = [f"# {report['model']} 单／多Agent限额评测", '',
             f"完成 {len(seen)}/{len(expected)} 次计划任务；完整批次：{report['complete']}。每题每组仅一次。", '',
             '| 指标 | 单Agent | 多Agent |', '|---|---:|---:|']
    metrics = [
        ('严格通过', lambda rs: f"{sum(r['passed'] for r in rs)}/{len(rs)}"),
        ('业务操作完成', lambda rs: sum(r['business_completed'] for r in rs)),
        ('违规执行', lambda rs: sum(r['illegal_execution_count'] for r in rs)),
        ('违规方案', lambda rs: sum(r['unsafe_proposal_count'] for r in rs)),
        ('返回Token总量', lambda rs: sum(r['tokens'] for r in rs)),
        ('平均Token/任务', lambda rs: round(statistics.mean(r['tokens'] for r in rs), 1) if rs else '—'),
        ('耗时中位数/秒', lambda rs: round(statistics.median(r['elapsed_ms'] for r in rs)/1000, 2) if rs else '—'),
    ]
    for title, fn in metrics:
        lines.append(f"| {title} | {fn(groups['single'])} | {fn(groups['multi'])} |")
    lines += ['', '## 全部场景', '', '| 场景 | 单Agent | 多Agent |', '|---|---|---|']
    for cid in report['case_ids']:
        cells = []
        for architecture in groups:
            rows = [r for r in groups[architecture] if r['case_id'] == cid]
            cells.append(', '.join('通过' if r['passed'] else '失败' for r in rows) or '未运行')
        lines.append(f"| {cid} | {' | '.join(cells)} |")
    lines += ['', '## 失败记录', '']
    for row in report['results']:
        if not row['passed']:
            failed = [k for k, v in row['checks'].items() if not v]
            lines.append(f"- {row['case_id']} / {row['architecture']}：{row['status']}；断言 {', '.join(failed)}；异常 {row['error'] or '无系统异常，业务目标未满足'}。")
    lines += ['', '## 额度与边界', '',
              f"包含接口探测 {probe_tokens:,} Token，本轮已返回总用量 **{prior:,} Token**。相对用户所述1,000,000额度的算术差额为 **{1000000-prior:,} Token**，不是平台实时余额；其他调用、未返回usage及平台计费规则可能影响余额。", '',
              '审批由脚本在临时演示库模拟。业务操作完成不等于选对动作；过期场景以安全拦截为通过条件。每场景仅运行一次，不代表生产准确率或重复稳定性，不与其他模型报告混算。', '',
              '原始报告与来源SHA256保存在同名汇总JSON；未删失败、未重新评分、未替换结果。预算协议见 kimi-evaluation-protocol-20261003.md。', '']
    markdown.write_text('\n'.join(lines))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--reports', nargs='+', type=Path, required=True)
    parser.add_argument('--probe', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--markdown', type=Path, required=True)
    args = parser.parse_args()
    summarize_batches(args.reports, args.probe, args.output, args.markdown)
