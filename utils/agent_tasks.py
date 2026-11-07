"""Durable Agent tasks, independent of the ComfyUI execution queue."""

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4


TERMINAL = {"completed", "failed", "cancelled", "recovery_required"}
ACTIVE = {"running", "delivering", "cancel_requested"}


class AgentTaskStore:
    def __init__(self, path, concurrency=4, per_user=2, pending_per_user=20):
        self.path = str(path)
        self.concurrency = max(1, int(concurrency))
        self.per_user = max(1, int(per_user))
        self.pending_per_user = max(1, int(pending_per_user))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, submission_key TEXT NOT NULL,
                    job TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                    updated REAL NOT NULL, lease TEXT, lease_until REAL NOT NULL DEFAULT 0,
                    checkpoint TEXT, result TEXT, error TEXT NOT NULL DEFAULT '',
                    UNIQUE(owner, submission_key));
                CREATE TABLE IF NOT EXISTS agent_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    event TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS agent_events_task ON agent_events(task_id, seq);
                CREATE TABLE IF NOT EXISTS agent_children (
                    task_id TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    prompt_id TEXT NOT NULL UNIQUE, response TEXT,
                    PRIMARY KEY(task_id, request_key));
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("job", "checkpoint", "result"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def create(self, owner, submission_key, job):
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM agent_tasks WHERE owner=? AND submission_key=?",
                             (owner, submission_key)).fetchone()
            if old:
                return self.decode(old)
            count = db.execute("SELECT COUNT(*) FROM agent_tasks WHERE owner=? AND state NOT IN "
                               "('completed','failed','cancelled','recovery_required')", (owner,)).fetchone()[0]
            if count >= self.pending_per_user:
                raise ValueError("独立 Agent 待处理任务已达账户上限，请等待已有任务完成。")
            task_id = uuid4().hex
            db.execute("INSERT INTO agent_tasks(id,owner,submission_key,job,state,created,updated) "
                       "VALUES(?,?,?,?,'queued',?,?)", (task_id, owner, submission_key, json.dumps(job), now, now))
            return self.decode(db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())

    def get(self, task_id):
        with closing(self.connect()) as db:
            return self.decode(db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())

    def list(self, owner):
        with closing(self.connect()) as db:
            return [self.decode(row) for row in db.execute(
                "SELECT * FROM agent_tasks WHERE owner=? ORDER BY created DESC LIMIT 100", (owner,))]

    def expire(self, db, now):
        # Never replay an interrupted model/tool operation: it may already have charged money.
        db.execute("UPDATE agent_tasks SET state=CASE WHEN state='delivering' THEN 'queued' "
                   "ELSE 'recovery_required' END, lease=NULL, updated=?, "
                   "error='执行服务失联；已保留产物和子任务，未自动重新生成。' "
                   "WHERE state IN ('running','delivering','cancel_requested') AND lease_until<?", (now, now))

    def claim(self):
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db, now)
            active = db.execute("SELECT COUNT(*) FROM agent_tasks WHERE lease IS NOT NULL").fetchone()[0]
            if active >= self.concurrency:
                return None
            row = db.execute("SELECT * FROM agent_tasks t WHERE state='queued' AND "
                             "(SELECT COUNT(*) FROM agent_tasks a WHERE a.owner=t.owner AND a.lease IS NOT NULL)<? "
                             "ORDER BY created LIMIT 1", (self.per_user,)).fetchone()
            if not row:
                return None
            lease = uuid4().hex
            state = 'delivering' if row['checkpoint'] else 'running'
            db.execute("UPDATE agent_tasks SET state=?,lease=?,lease_until=?,updated=?,error='' WHERE id=?",
                       (state, lease, now + 90, now, row['id']))
            return self.decode(db.execute("SELECT * FROM agent_tasks WHERE id=?", (row['id'],)).fetchone())

    def update(self, task_id, lease, *, events=(), checkpoint=None, state=None, result=None, error=""):
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM agent_tasks WHERE id=? AND lease=? AND lease_until>=?",
                             (task_id, lease, now)).fetchone()
            if not row:
                raise ValueError("Agent 执行租约已失效。")
            if state is not None and state not in TERMINAL | {'delivering'}:
                raise ValueError("Invalid Agent state")
            target = state or row['state']
            if row['state'] == 'cancel_requested' and target == 'completed':
                target = 'cancelled'
            for event in events:
                db.execute("INSERT INTO agent_events(task_id,event) VALUES(?,?)", (task_id, json.dumps(event)))
            db.execute("UPDATE agent_tasks SET state=?,updated=?,lease_until=?,lease=?, "
                       "checkpoint=COALESCE(?,checkpoint),result=COALESCE(?,result),error=? WHERE id=?",
                       (target, now, now + 90, None if target in TERMINAL else lease,
                        json.dumps(checkpoint) if checkpoint is not None else None,
                        json.dumps(result) if result is not None else None, error[:1000], task_id))
            return target

    def cancel(self, task_id, owner):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE agent_tasks SET state=CASE WHEN state='queued' THEN 'cancelled' "
                       "ELSE 'cancel_requested' END,updated=? WHERE id=? AND owner=? "
                       "AND state IN ('queued','running','delivering')", (time.time(), task_id, owner))

    def recover(self, task_id, owner):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db, time.time())
            row = db.execute('SELECT * FROM agent_tasks WHERE id=? AND owner=?', (task_id, owner)).fetchone()
            if not row or row['state'] != 'recovery_required':
                raise ValueError('Only interrupted tasks can be recovered')
            job = json.loads(row['job'])
            if not row['checkpoint']:
                if not db.execute('SELECT 1 FROM agent_children WHERE task_id=?', (task_id,)).fetchone():
                    raise ValueError('No saved video task or delivery checkpoint; inspect the interrupted Agent manually')
                job['recovery_only'] = True
            db.execute("UPDATE agent_tasks SET state='queued',lease=NULL,error='',job=?,updated=? WHERE id=?",
                       (json.dumps(job), time.time(), task_id))

    def events(self, task_id, after=0):
        with closing(self.connect()) as db:
            return [{"seq": row['seq'], **json.loads(row['event'])} for row in db.execute(
                "SELECT * FROM agent_events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 200", (task_id, after))]

    def children(self, task_id):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM agent_children WHERE task_id=?", (task_id,))]

    def reserve_child(self, task_id, key, fingerprint):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,lease_until FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row['state'] != 'running' or row['lease_until'] < time.time():
                raise ValueError("Agent task cannot submit new children")
            old = db.execute("SELECT * FROM agent_children WHERE task_id=? AND request_key=?", (task_id, key)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise ValueError("Submission key reused with different workflow")
                return dict(old), False
            prompt_id = str(uuid4())
            db.execute("INSERT INTO agent_children VALUES(?,?,?,?,NULL)", (task_id, key, fingerprint, prompt_id))
            return {'prompt_id': prompt_id, 'response': None}, True

    def child_response(self, prompt_id, response):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE agent_children SET response=? WHERE prompt_id=?", (json.dumps(response), prompt_id))
