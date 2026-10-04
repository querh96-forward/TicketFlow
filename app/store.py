import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.domain import ProposalInput, allowed_actions, demo_tickets, SOURCE_FIELDS, action_checks, EVIDENCE_RULE


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str)


class Conflict(ValueError):
    pass


class StaleSnapshot(Conflict):
    """The immutable run snapshot must not be reused after business data changes."""

    def __init__(self):
        super().__init__('工单数据已变更，当前任务已停止；请启动新任务以读取最新数据，不能恢复旧任务。')


class ReviewHandoffError(RuntimeError):
    """A reviewer cannot hand off a result without a stored decision."""


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tickets (
              id TEXT PRIMARY KEY, title TEXT, description TEXT, category TEXT, priority TEXT,
              facts TEXT, status TEXT DEFAULT 'open', version INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, ticket_id TEXT, status TEXT, mode TEXT, created REAL, updated REAL,
              ticket_version INTEGER, facts TEXT, feedback TEXT, approval TEXT, resume TEXT,
              answer TEXT DEFAULT '', error TEXT DEFAULT '', model_calls INTEGER DEFAULT 0,
              tokens INTEGER DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(ticket_id)
              WHERE status IN ('queued','running','waiting_approval');
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, created REAL,
              kind TEXT, actor TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS findings (
              run_id TEXT, role TEXT, evidence_id TEXT, summary TEXT,
              PRIMARY KEY(run_id,role));
            CREATE TABLE IF NOT EXISTS proposals (
              id TEXT PRIMARY KEY, run_id TEXT, action TEXT, reason TEXT,
              evidence_ids TEXT, findings TEXT, review TEXT);
            CREATE TABLE IF NOT EXISTS review_repairs (
              run_id TEXT, proposal_id TEXT, message_id TEXT, created REAL,
              PRIMARY KEY(run_id,proposal_id));
            CREATE TABLE IF NOT EXISTS decisions (
              approval_id TEXT PRIMARY KEY, run_id TEXT, proposal_id TEXT,
              decision TEXT, feedback TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS operations (
              proposal_id TEXT PRIMARY KEY, run_id TEXT, result TEXT, created REAL);
            """)
            columns = {row['name'] for row in db.execute('PRAGMA table_info(tickets)')}
            if 'deleted_at' not in columns:
                db.execute('ALTER TABLE tickets ADD COLUMN deleted_at REAL')
            for item in demo_tickets():
                db.execute("INSERT OR IGNORE INTO tickets(id,title,description,category,priority,facts) VALUES(?,?,?,?,?,?)",
                           (item['id'], item['title'], item['description'], item['category'], item['priority'], encode(item['facts'])))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            raise KeyError('record_not_found')
        result = dict(row)
        for key in ('facts', 'approval', 'resume', 'data', 'evidence_ids', 'findings', 'review', 'result'):
            if key in result and result[key] is not None:
                result[key] = json.loads(result[key])
        return result

    def ticket(self, ticket_id):
        with self.connect() as db:
            return self.decode(db.execute('SELECT * FROM tickets WHERE id=?', (ticket_id,)).fetchone())

    def tickets(self, deleted=False):
        with self.connect() as db:
            condition = 'IS NOT NULL' if deleted else 'IS NULL'
            return [self.decode(row) for row in db.execute(f'SELECT * FROM tickets WHERE deleted_at {condition} ORDER BY id')]

    def delete_ticket(self, ticket_id):
        # Serialize against create_run/retry/claim: a live task must never lose its ticket.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            ticket = self.decode(db.execute('SELECT * FROM tickets WHERE id=?', (ticket_id,)).fetchone())
            if ticket['deleted_at'] is not None:
                return
            if db.execute("SELECT 1 FROM runs WHERE ticket_id=? AND status IN ('queued','running','waiting_approval')", (ticket_id,)).fetchone():
                raise Conflict('该工单有活动任务，请先完成任务或取消排队/待审批任务，再删除。')
            db.execute('UPDATE tickets SET deleted_at=? WHERE id=?', (time.time(), ticket_id))
            for run in db.execute('SELECT id FROM runs WHERE ticket_id=?', (ticket_id,)).fetchall():
                self._event(db, run['id'], 'ticket_deleted', 'human', {'ticket_id':ticket_id})

    def restore_ticket(self, ticket_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            ticket = self.decode(db.execute('SELECT * FROM tickets WHERE id=?', (ticket_id,)).fetchone())
            if ticket['deleted_at'] is None:
                return
            db.execute('UPDATE tickets SET deleted_at=NULL WHERE id=?', (ticket_id,))
            for run in db.execute('SELECT id FROM runs WHERE ticket_id=?', (ticket_id,)).fetchall():
                self._event(db, run['id'], 'ticket_restored', 'human', {'ticket_id':ticket_id})

    def run(self, run_id):
        with self.connect() as db:
            return self.decode(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())

    def runs(self, deleted=False):
        with self.connect() as db:
            condition = 'IS NOT NULL' if deleted else 'IS NULL'
            return [self.decode(row) for row in db.execute(f'SELECT r.* FROM runs r JOIN tickets t ON r.ticket_id=t.id WHERE t.deleted_at {condition} ORDER BY r.created DESC LIMIT 100')]

    def create_run(self, ticket_id, mode, feedback=''):
        run_id, now = uuid.uuid4().hex, time.time()
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                ticket = self.decode(db.execute('SELECT * FROM tickets WHERE id=?', (ticket_id,)).fetchone())
                if ticket['deleted_at'] is not None:
                    raise Conflict('工单已在回收站，请先恢复。')
                db.execute('INSERT INTO runs(id,ticket_id,status,mode,created,updated,ticket_version,facts,feedback) VALUES(?,?,?,?,?,?,?,?,?)',
                           (run_id, ticket_id, 'queued', mode, now, now, ticket['version'], encode(ticket['facts']), feedback))
                self._event(db, run_id, 'queued', 'system', {'ticket_id': ticket_id, 'mode': mode})
        except sqlite3.IntegrityError as exc:
            raise Conflict('该工单已有活动任务，请先完成审批或取消。') from exc
        return self.run(run_id)

    def update(self, run_id, **fields):
        allowed = {'status', 'approval', 'resume', 'answer', 'error'}
        if not set(fields) <= allowed:
            raise ValueError('invalid_update')
        fields['updated'] = time.time()
        with self.connect() as db:
            db.execute(f"UPDATE runs SET {','.join(key+'=?' for key in fields)} WHERE id=?",
                       (*[encode(v) if k in ('approval', 'resume') and v is not None else v for k, v in fields.items()], run_id))

    def _event(self, db, run_id, kind, actor, data):
        db.execute('INSERT INTO events(run_id,created,kind,actor,data) VALUES(?,?,?,?,?)',
                   (run_id, time.time(), kind, actor, encode(data)))

    def event(self, run_id, kind, actor, data):
        with self.connect() as db:
            self._event(db, run_id, kind, actor, data)

    def events(self, run_id, after=0):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute('SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id', (run_id, after))]

    def reserve_model_call(self, run_id, max_calls, max_tokens):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self.decode(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
            if run['model_calls'] >= max_calls or run['tokens'] >= max_tokens:
                raise Conflict('模型调用或Token预算已耗尽。')
            db.execute('UPDATE runs SET model_calls=model_calls+1 WHERE id=?', (run_id,))

    def add_tokens(self, run_id, tokens):
        with self.connect() as db:
            db.execute('UPDATE runs SET tokens=tokens+? WHERE id=?', (tokens, run_id))

    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id FROM runs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE runs SET status='running', updated=? WHERE id=?", (time.time(), row['id']))
                return row['id']
        return None

    def recover(self):
        # This application uses one worker process. Its OS lock prevents competing recovery.
        with self.connect() as db:
            for row in db.execute("SELECT id FROM runs WHERE status='running'").fetchall():
                db.execute("UPDATE runs SET status='queued' WHERE id=?", (row['id'],))
                self._event(db, row['id'], 'recovered', 'system', {'reason': 'worker_restart'})

    def finding(self, run_id, role, summary):
        if role not in ('account', 'platform') or not 5 <= len(summary) <= 400:
            raise ValueError('invalid_finding')
        evidence_id = f'{role}:{run_id}'
        with self.connect() as db:
            # A specialist must actually inspect its scoped source before submitting.
            seen = db.execute("SELECT 1 FROM events WHERE run_id=? AND actor=? AND kind='evidence_read'", (run_id, role)).fetchone()
            if not seen:
                raise Conflict('请先读取本角色的数据工具，再提交调查结论。')
            db.execute('INSERT OR REPLACE INTO findings VALUES(?,?,?,?)', (run_id, role, evidence_id, summary))
            self._event(db, run_id, 'finding', role, {'evidence_id': evidence_id, 'summary': summary})
        return {'evidence_id': evidence_id, 'summary': summary}

    def findings(self, run_id):
        facts = self.run(run_id)['facts']
        checks = action_checks(facts)
        with self.connect() as db:
            return [{**dict(row), 'facts': facts[row['role']],
                     'source_scope': list(SOURCE_FIELDS[row['role']]),
                     'policy_checks': {a: c for a, c in checks.items() if c['source'] == row['role']},
                     'summary_is_model_generated': True}
                    for row in db.execute('SELECT * FROM findings WHERE run_id=? ORDER BY role', (run_id,))]

    def propose(self, run_id, proposal: ProposalInput):
        findings = self.findings(run_id)
        if {f['role'] for f in findings} != {'account', 'platform'}:
            raise Conflict('账号与平台两个专家均须完成调查。')
        if set(proposal.evidence_ids) != {f['evidence_id'] for f in findings}:
            raise Conflict('方案必须引用本任务两位专家实际提交的证据编号。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._assert_current_snapshot(db, run_id)
            # Deduplicate tool retries before an external checkpoint is committed.
            for row in db.execute('SELECT * FROM proposals WHERE run_id=?', (run_id,)):
                item = self.decode(row)
                if item['action'] == proposal.action and item['reason'] == proposal.reason and item['evidence_ids'] == proposal.evidence_ids:
                    return item
            if db.execute('SELECT COUNT(*) FROM proposals WHERE run_id=?', (run_id,)).fetchone()[0] >= 3:
                raise Conflict('方案修订次数已达上限。')
            pid = uuid.uuid4().hex
            db.execute('INSERT INTO proposals VALUES(?,?,?,?,?,?,NULL)',
                       (pid, run_id, proposal.action, proposal.reason, encode(proposal.evidence_ids), encode(findings)))
            self._event(db, run_id, 'proposal', 'supervisor', {'proposal_id': pid, **proposal.model_dump()})
        return self.proposal(run_id, pid)

    def proposal(self, run_id, proposal_id):
        with self.connect() as db:
            return self.decode(db.execute('SELECT * FROM proposals WHERE id=? AND run_id=?', (proposal_id, run_id)).fetchone())

    def request_review_repair(self, run_id, proposal_id, message_id):
        """One durable reminder per proposal; replay of the same checkpoint is idempotent."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._assert_current_snapshot(db, run_id)
            proposal = self.decode(db.execute('SELECT * FROM proposals WHERE run_id=? AND id=?',
                                             (run_id, proposal_id)).fetchone())
            if proposal['review'] is not None:
                return False
            prior = db.execute('SELECT message_id FROM review_repairs WHERE run_id=? AND proposal_id=?',
                               (run_id, proposal_id)).fetchone()
            if prior:
                return prior['message_id'] == message_id
            db.execute('INSERT INTO review_repairs VALUES(?,?,?,?)', (run_id, proposal_id, message_id, time.time()))
            self._event(db, run_id, 'review_repair_requested', 'reviewer',
                        {'proposal_id':proposal_id, 'reason':'review_not_persisted', 'attempt':1})
            return True

    def review_context(self, run_id, proposal_id):
        # Validate ownership before exposing decisions, even for an invalid/cross-run ID.
        proposal = self.proposal(run_id, proposal_id)
        with self.connect() as db:
            decisions = [dict(row) for row in db.execute(
                'SELECT d.approval_id,d.proposal_id,p.action,d.decision,d.feedback,d.created '
                'FROM decisions d JOIN proposals p ON p.id=d.proposal_id AND p.run_id=d.run_id '
                'WHERE d.run_id=? ORDER BY d.created,d.rowid', (run_id,))]
        return {'proposal': proposal, 'human_decisions': decisions,
                'fact_assessment': {'source_scope': SOURCE_FIELDS,
                                    'policy_checks': action_checks(self.run(run_id)['facts']),
                                    'rule': EVIDENCE_RULE}}

    def _assert_current_snapshot(self, db, run_id):
        run = self.decode(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
        # A committed receipt takes precedence: execution itself increments the version.
        if db.execute('SELECT 1 FROM operations WHERE run_id=?', (run_id,)).fetchone():
            return
        ticket = db.execute('SELECT version FROM tickets WHERE id=?', (run['ticket_id'],)).fetchone()
        if run['error'].startswith('STALE_SNAPSHOT:') or ticket['version'] != run['ticket_version']:
            raise StaleSnapshot()

    def assert_current_snapshot(self, run_id):
        with self.connect() as db:
            self._assert_current_snapshot(db, run_id)

    def request_approval(self, run_id, approval):
        # Version validation and publishing approval must be one transaction.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._assert_current_snapshot(db, run_id)
            db.execute("UPDATE runs SET status='waiting_approval',approval=?,resume=NULL,updated=? WHERE id=?",
                       (encode(approval), time.time(), run_id))
            self._event(db, run_id, 'approval_requested', 'system', approval)

    def proposals(self, run_id):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute('SELECT * FROM proposals WHERE run_id=?', (run_id,))]

    def unrequested_reviewed_proposal(self, run_id):
        with self.connect() as db:
            rows = db.execute('SELECT p.* FROM proposals p WHERE p.run_id=? AND p.review IS NOT NULL AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.proposal_id=p.id) ORDER BY p.rowid DESC', (run_id,)).fetchall()
            for row in rows:
                proposal = self.decode(row)
                if proposal['review']['approved']:
                    return proposal
        return None

    def review(self, run_id, proposal_id, approved, reason):
        self.proposal(run_id, proposal_id)
        review = {'approved': approved, 'reason': reason}
        with self.connect() as db:
            prior = db.execute('SELECT review FROM proposals WHERE id=?', (proposal_id,)).fetchone()[0]
            if prior:
                return json.loads(prior)
            db.execute('UPDATE proposals SET review=? WHERE id=?', (encode(review), proposal_id))
            self._event(db, run_id, 'review', 'reviewer', {'proposal_id': proposal_id, **review})
        return review

    def decide(self, run_id, approval_id, decision, feedback=''):
        if decision not in ('approve', 'reject'):
            raise ValueError('invalid_decision')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self.decode(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
            pending = run['approval']
            if run['status'] != 'waiting_approval' or not pending or pending['id'] != approval_id:
                raise Conflict('审批已失效或已经处理，请刷新任务。')
            pid = pending['proposal_id']
            db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?)', (approval_id, run_id, pid, decision, feedback, time.time()))
            value = {'type': decision}
            if decision == 'reject':
                value['message'] = feedback or '人工拒绝：不要执行原方案；请修订方案或转人工。'
            resume = {pending['interrupt_id']: {'decisions': [value]}}
            db.execute("UPDATE runs SET status='queued', resume=?, approval=NULL, updated=? WHERE id=?", (encode(resume), time.time(), run_id))
            self._event(db, run_id, 'human_decision', 'human', {'proposal_id': pid, 'decision': decision, 'feedback': feedback})

    def execute(self, run_id, proposal_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT result FROM operations WHERE proposal_id=? AND run_id=?', (proposal_id, run_id)).fetchone()
            if prior:
                return {**json.loads(prior[0]), 'replayed': True}
            proposal = self.decode(db.execute('SELECT * FROM proposals WHERE id=? AND run_id=?', (proposal_id, run_id)).fetchone())
            run = self.decode(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
            ticket = self.decode(db.execute('SELECT * FROM tickets WHERE id=?', (run['ticket_id'],)).fetchone())
            if run['status'] != 'running':
                raise Conflict('仅运行中的任务可以执行业务操作。')
            if not proposal['review'] or not proposal['review']['approved']:
                raise Conflict('方案尚未通过独立审核。')
            decision = db.execute('SELECT decision FROM decisions WHERE run_id=? AND proposal_id=? ORDER BY created DESC LIMIT 1', (run_id, proposal_id)).fetchone()
            if not decision or decision[0] != 'approve':
                raise Conflict('缺少该方案的有效人工批准。')
            if db.execute('SELECT 1 FROM operations WHERE run_id=?', (run_id,)).fetchone():
                raise Conflict('该任务已执行一个业务操作，不能重复执行其他方案。')
            if ticket['version'] != run['ticket_version']:
                raise StaleSnapshot()
            if proposal['action'] not in allowed_actions(ticket['facts']):
                raise Conflict('当前业务事实不满足动作的执行条件。')
            facts = ticket['facts']
            if proposal['action'] == 'restore_access':
                facts['account']['status'] = 'active'
            elif proposal['action'] == 'retry_export':
                facts['platform']['job_status'] = 'queued'
            status = 'escalated' if proposal['action'] == 'escalate' else 'resolved'
            result = {'action': proposal['action'], 'ticket_id': ticket['id'], 'ticket_status': status, 'demo': True}
            db.execute('UPDATE tickets SET facts=?,status=?,version=version+1 WHERE id=?', (encode(facts), status, ticket['id']))
            db.execute('INSERT INTO operations VALUES(?,?,?,?)', (proposal_id, run_id, encode(result), time.time()))
            self._event(db, run_id, 'executed', 'executor', {'proposal_id': proposal_id, **result})
            return result

    def operations(self, run_id):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute('SELECT * FROM operations WHERE run_id=?', (run_id,))]

    def cancel(self, run_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            status = db.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()
            if status is None:
                raise KeyError('record_not_found')
            if status[0] not in ('waiting_approval', 'queued'):
                raise Conflict('仅支持取消排队中或等待审批的任务。')
            db.execute("UPDATE runs SET status='canceled',approval=NULL,resume=NULL WHERE id=?", (run_id,))
            self._event(db, run_id, 'canceled', 'human', {})

    def retry(self, run_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self.decode(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
            ticket = db.execute('SELECT deleted_at FROM tickets WHERE id=?', (run['ticket_id'],)).fetchone()
            if ticket['deleted_at'] is not None:
                raise Conflict('工单已在回收站，请先恢复。')
            if run['status'] not in ('failed', 'needs_attention'):
                raise Conflict('仅可恢复失败或需要人工处理的任务。')
            if db.execute('SELECT 1 FROM operations WHERE run_id=?', (run_id,)).fetchone():
                raise Conflict('业务操作已经执行，不能重新执行。')
            self._assert_current_snapshot(db, run_id)
            try:
                db.execute("UPDATE runs SET status='queued',error='',updated=? WHERE id=?", (time.time(), run_id))
            except sqlite3.IntegrityError as exc:
                raise Conflict('该工单已有其他活动任务。') from exc
            self._event(db, run_id, 'retry_requested', 'human', {'budgets':'preserved'})
