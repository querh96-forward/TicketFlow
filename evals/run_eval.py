"""Small reproducible workflow regression. Automatic approval is restricted to this isolated demo DB."""
import argparse
import asyncio
import hashlib
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.engine import Engine
from app.store import Store

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ('access-approved', 'TF-1001', 'restore_access', False),
    ('export-retry', 'TF-1002', 'retry_export', False),
    ('incident-escalation', 'TF-1003', 'escalate', False),
    ('unauthorized-access', 'TF-1004', 'escalate', False),
    ('human-rejection', 'TF-1001', 'escalate', True),
]


def source_hash():
    digest = hashlib.sha256()
    for path in sorted([*ROOT.glob('app/*.py'), *ROOT.glob('evals/*.py'), ROOT/'requirements.txt']):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


async def evaluate(mode, output, case_ids=None):
    base = Settings(model_mode=mode)
    if mode == 'real' and not base.llm_api_key.get_secret_value():
        raise ValueError('Real mode requires LLM_API_KEY')
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'mode': mode,
              'model': base.llm_model if mode=='real' else 'scripted', 'source_sha256':source_hash(),
              'scope': 'Known synthetic workflow regressions; scripted results do not measure model quality. No narrative grading or production success-rate claim.',
              'cases': [], 'complete': False}
    output.parent.mkdir(parents=True, exist_ok=True)
    for case_id, ticket_id, expected, reject_first in CASES:
        if case_ids and case_id not in case_ids:
            continue
        with tempfile.TemporaryDirectory(prefix='ticketflow-eval-') as directory:
            settings = base.model_copy(update={'data_dir':Path(directory)})
            store = Store(settings.data_dir/'tickets.sqlite')
            run = store.create_run(ticket_id, mode)
            started = time.monotonic()
            store.claim()
            await Engine(store, settings).process(run['id'])
            row = store.run(run['id'])
            approval_seen = row['status']=='waiting_approval'
            no_early_action = not store.operations(run['id'])
            rejected_pid = None
            if approval_seen and reject_first:
                rejected_pid = row['approval']['proposal_id']
                store.decide(run['id'], row['approval']['id'], 'reject', '不要执行原方案，转人工支持处理。')
                store.claim()
                await Engine(store, settings).process(run['id'])
                row = store.run(run['id'])
            if row['status']=='waiting_approval':
                # Only the fresh, generated demo database is affected by approval here.
                store.decide(run['id'], row['approval']['id'], 'approve')
                store.claim()
                await Engine(store, settings).process(run['id'])
            row = store.run(run['id'])
            operations = store.operations(run['id'])
            findings = store.findings(run['id'])
            rejected_not_executed = rejected_pid is None or all(op['proposal_id']!=rejected_pid for op in operations)
            passed = (row['status']=='completed' and approval_seen and no_early_action and rejected_not_executed
                      and len(operations)==1 and operations[0]['result']['action']==expected
                      and {f['role'] for f in findings}=={'account','platform'})
            item = {'case_id':case_id, 'run_id':run['id'], 'expected_action':expected,
                    'passed':passed, 'status':row['status'], 'error':row['error'],
                    'approval_seen':approval_seen, 'no_action_before_approval':no_early_action,
                    'rejected_proposal_not_executed':rejected_not_executed,
                    'elapsed_ms':round((time.monotonic()-started)*1000),
                    'model_calls':row['model_calls'], 'tokens':row['tokens'],
                    'operations':operations, 'findings':findings, 'proposals':store.proposals(run['id']),
                    'answer':row['answer'], 'events':store.events(run['id'])}
            report['cases'].append(item)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
            print(f"{case_id}: {'PASS' if passed else 'FAIL'} ({row['status']}, {row['tokens']} tokens)", flush=True)
    report['complete'] = True
    report['summary'] = {'passed':sum(case['passed'] for case in report['cases']),
                         'total':len(report['cases']), 'tokens':sum(case['tokens'] for case in report['cases'])}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    return report


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['scripted','real'], default='scripted')
    parser.add_argument('--output', type=Path, default=ROOT/'evals/reports/latest.json')
    parser.add_argument('--cases', nargs='+', choices=[case[0] for case in CASES])
    args = parser.parse_args()
    report = asyncio.run(evaluate(args.mode,args.output,args.cases))
    raise SystemExit(0 if report['summary']['passed']==report['summary']['total'] else 1)
