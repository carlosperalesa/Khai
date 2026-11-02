"""Durable control plane. Workers never receive this database or its credentials."""
import contextlib
import json
import os
import sqlite3
from app.kernel.database import connect
import time
import uuid
from pathlib import Path
from app.kernel.journal import Journal
from app.kernel.journal import TERMINAL as TERMINAL_JOURNAL

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "EXHAUSTED", "UNKNOWN", "UNVERIFIED",
            "UNFULFILLABLE"}
INITIAL_TASK_BUDGET = {'generations': 4, 'executions': 4, 'llm_calls': 16}


class BudgetExceeded(RuntimeError):
    pass


class ControlStore:
    def __init__(self, db_path, events=None):
        self.events = events
        self.db_path = str(Path(db_path).resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS control_tasks (
                    id TEXT PRIMARY KEY, source TEXT UNIQUE NOT NULL, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS control_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT,
                    data TEXT, created_at REAL);
                CREATE TABLE IF NOT EXISTS control_feedback (
                    execution_id TEXT PRIMARY KEY, task_id TEXT, success INTEGER,
                    evidence TEXT, created_at REAL);
                CREATE TABLE IF NOT EXISTS control_commands (source TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS event_outbox(id TEXT PRIMARY KEY,document TEXT NOT NULL);
            ''')
        from app.kernel.database import adopt_schema
        adopt_schema(self.db_path,'control',['control_tasks','control_events','control_commands','event_outbox'])
        self.journal = Journal(self.db_path)

    @contextlib.contextmanager
    def connection(self):
        conn = connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def document(source, request, **extra):
        now = time.time()
        task = dict(id="task_" + uuid.uuid4().hex, source=source, request=request,
                    state="ROUTING", created_at=now, updated_at=now, deadline=now + 1800,
                    generations=0, executions=0, llm_calls=0,
                    max_generations=INITIAL_TASK_BUDGET['generations'],
                    max_executions=INITIAL_TASK_BUDGET['executions'],
                    max_llm_calls=INITIAL_TASK_BUDGET['llm_calls'], revision=0, guidelines=[],
                    hypothesis_id=None, alternatives=[], result=None)
        task.update(extra)
        if not task.get('trace_id'):
            from app.kernel.tracing import get_trace_id, new_trace_id
            task['trace_id'] = get_trace_id() or new_trace_id()
        return task

    def create(self, source, request, **extra):
        task = self.document(source, request, **extra)
        with self.connection() as conn:
            inserted = conn.execute("INSERT OR IGNORE INTO control_tasks VALUES (?, ?, ?)",
                         (task["id"], source, json.dumps(task)))
            if inserted.rowcount:
                self._audit_task(conn, task, 'created')
            return json.loads(conn.execute("SELECT document FROM control_tasks WHERE source=?", (source,)).fetchone()[0])

    def get(self, task_id):
        with self.connection() as conn:
            row = conn.execute("SELECT document FROM control_tasks WHERE id=?", (task_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def ensure_journal(self, task):
        with self.connection() as conn:
            self._audit_task(conn, task, 'observed_existing_state')

    def all(self):
        with self.connection() as conn:
            return [json.loads(r[0]) for r in conn.execute("SELECT document FROM control_tasks ORDER BY rowid")]

    def mutate(self, task_id, fn):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT document FROM control_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            task = json.loads(row[0])
            from app.pixy.budget import current_goal_revision, StaleGoalRevision
            expected_goal = current_goal_revision.get()
            if (expected_goal and expected_goal[0] == task_id
                    and expected_goal[1] != task.get('goal_revision', 0)):
                raise StaleGoalRevision('El objetivo cambió durante el trabajo')
            fn(task)
            task["revision"] += 1
            task["updated_at"] = time.time()
            conn.execute("UPDATE control_tasks SET document=? WHERE id=?", (json.dumps(task), task_id))
            self._queue_event(conn,task,'task_state')
            return task

    def _queue_event(self, conn, task, kind):
        self._audit_task(conn, task, kind)
        event = dict(event_id=uuid.uuid4().hex,correlation_id=task['id'],task_id=task['id'],
                     execution_id=task.get('execution_id'),skill_name=task.get('skill_name'),version_id=task.get('version_id'),
                     metadata={'state':task['state'],'revision':task['revision']},event_type=kind)
        conn.execute('INSERT INTO event_outbox VALUES (?,?)',(event['event_id'],json.dumps(event)))

    def _audit_task(self, conn, task, phase):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_questions'").fetchone():
            obsolete = conn.execute("SELECT id FROM conversation_questions WHERE subject_id=? "
                "AND status IN ('open','queued') AND (state!=? OR revision!=? OR ?<=?)",
                (task['id'], task['state'], str(task['revision']), task['deadline'], time.time())).fetchall()
            for question in obsolete:
                conn.execute("UPDATE conversation_questions SET status='stale' WHERE id=?", (question['id'],))
                qcycle = self.journal.cycle_id('question', question['id'])
                recorded = self.journal.get(qcycle, conn=conn)
                if recorded and recorded['status'] not in TERMINAL_JOURNAL:
                    self.journal.finish(qcycle, 'cancelled', phase='superseded', conn=conn)
        refs = {k: task[k] for k in ('execution_id', 'goal_revision', 'attempt_number',
                                     'skill_name', 'version_id') if task.get(k) is not None}
        refs['task_id'] = task['id']
        root = self.journal.start('interaction', task['source'], source=task['source'],
                                  trace_id=task.get('trace_id'), conn=conn)
        cid = self.journal.start('task', task['id'], parent_id=root, refs={'task_id': task['id']}, conn=conn)
        attempt_id = f"{task['id']}:{task.get('attempt_number', 1)}:{task.get('goal_revision', 0)}"
        aid = self.journal.start('task_attempt', attempt_id, parent_id=cid, refs=refs, conn=conn)
        state = task['state']
        status = ({'COMPLETED': 'completed', 'FAILED': 'failed', 'CANCELLED': 'cancelled',
                   'EXHAUSTED': 'exhausted', 'UNKNOWN': 'unknown', 'UNFULFILLABLE': 'failed'}
                  .get(state, 'waiting' if state.startswith('WAITING') or state == 'UNVERIFIED' else 'active'))
        # Each authorized revision/attempt is a separate branch. A closed attempt is never overwritten.
        self.journal.event(aid, phase, status=status, data={**refs, 'phase': state}, conn=conn)
        root_state = self.journal.get(cid, conn=conn)['status']
        recovery = (task.get('recovery_attempt') or {}).get('authorization_id')
        if phase == 'recovery_dispatched' and recovery:
            self.journal.event(cid, 'authorized_attempt_started', status=status,
                               data={**refs, 'authorization_id': recovery}, conn=conn)
        else:
            self.journal.event(cid, state, status=status if root_state not in TERMINAL_JOURNAL else None,
                               data=refs, conn=conn)
        if state == 'ROUTING':
            self.journal.start('routing', attempt_id, parent_id=aid, refs=refs, conn=conn)
        else:
            routing = self.journal.cycle_id('routing', attempt_id)
            row = self.journal.get(routing, conn=conn)
            if row and row['status'] == 'active':
                failed = (task.get('result') or {}).get('stage') == 'routing'
                self.journal.finish(routing, 'failed' if failed else 'completed', conn=conn)
        if state == 'GENERATING':
            self.journal.start('skill_creation', attempt_id, parent_id=aid, refs=refs, conn=conn)
        creation = self.journal.cycle_id('skill_creation', attempt_id)
        row = self.journal.get(creation, conn=conn)
        if row and row['status'] not in TERMINAL_JOURNAL and (task.get('artifact_hash') or state in TERMINAL):
            self.journal.finish(creation, 'completed' if task.get('artifact_hash') else status, conn=conn)
        if task.get('execution_id'):
            use = self.journal.start('skill_use', task['execution_id'], parent_id=aid, refs=refs, conn=conn)
            observed = task.get('runtime_status')
            self.journal.event(use, observed or state, status=status, data=refs, conn=conn)

    def flush_events(self):
        if not self.events:
            return
        with self.connection() as conn:
            rows = conn.execute('SELECT * FROM event_outbox').fetchall()
        for row in rows:
            event = json.loads(row['document'])
            self.events.emit('pixy',**event)
            with self.connection() as conn:
                conn.execute('DELETE FROM event_outbox WHERE id=?',(row['id'],))

    def update(self, task_id, expected=None, **changes):
        def apply(task):
            if expected is not None and task["state"] not in expected:
                raise ValueError("La tarea cambió de estado; operación obsoleta")
            if task["state"] == "CANCELLED" and changes.get("state", "CANCELLED") != "CANCELLED":
                raise ValueError("Tarea cancelada")
            task.update(changes)
        return self.mutate(task_id, apply)

    def consume(self, task_id, counter, allow_review=False):
        def apply(task):
            if (task["state"] in TERMINAL and not (allow_review and task['state'] == 'UNVERIFIED')) or time.time() >= task["deadline"]:
                raise BudgetExceeded("Objetivo detenido o plazo agotado")
            if task[counter] >= task["max_" + counter]:
                raise BudgetExceeded("Presupuesto agotado: " + counter)
            task[counter] += 1
        return self.mutate(task_id, apply)

    def observe_execution(self, snapshot, result):
        """Runtime facts do not invalidate an owner's pending question revision."""
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT document FROM control_tasks WHERE id=?', (snapshot['id'],)).fetchone()
            if not row:
                return None
            task = json.loads(row[0])
            if (task.get('execution_id') != snapshot.get('execution_id')
                    or task.get('goal_revision', 0) != snapshot.get('goal_revision', 0)):
                return None
            status = result.get('status')
            if ((task.get('runtime_status') in {'cancelling', 'cancelled', 'finished', 'failed', 'timeout', 'unknown'}
                 or task['state'] in {'CANCELLING', 'CANCELLED'})
                    and status in {'queued', 'starting', 'running', 'started'}):
                return task
            task.update(runtime_status=status, runtime_result=result, runtime_observed_at=time.time())
            conn.execute('UPDATE control_tasks SET document=? WHERE id=?', (json.dumps(task), task['id']))
            self._queue_event(conn, task, 'runtime_observed')
            return task

    def event(self, task_id, kind, data):
        with self.connection() as conn:
            conn.execute("INSERT INTO control_events(task_id,kind,data,created_at) VALUES (?,?,?,?)",
                         (task_id, kind, json.dumps(data), time.time()))

    def eligible_rule_ids(self):
        with self.connection() as conn:
            try:
                return {row[0] for row in conn.execute('SELECT rule_id FROM rule_proposals')}
            except sqlite3.OperationalError:
                return set()

    def command(self, source, task_id, fn):
        """Deduplicate command and state transition in the same transaction."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM control_commands WHERE source=?", (source,)).fetchone():
                return None
            row = conn.execute("SELECT document FROM control_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("Tarea desconocida")
            task = json.loads(row[0])
            from app.pixy.budget import current_question_revision, StaleGoalRevision
            expected_question = current_question_revision.get()
            if (expected_question and expected_question[0] == task_id
                    and expected_question[1] != task['revision']):
                raise StaleGoalRevision('La pregunta cambió antes de recibir la respuesta')
            if expected_question and expected_question[0] == task_id:
                question = conn.execute('SELECT status,subject_id,revision FROM conversation_questions WHERE id=?',
                                        (expected_question[2],)).fetchone()
                if (not question or question['status'] != 'open' or question['subject_id'] != task_id
                        or question['revision'] != str(task['revision']) or time.time() >= task['deadline']):
                    raise StaleGoalRevision('La pregunta dejó de estar vigente')
            fn(task)
            task["revision"] += 1
            task["updated_at"] = time.time()
            conn.execute("UPDATE control_tasks SET document=? WHERE id=?", (json.dumps(task), task_id))
            conn.execute("INSERT INTO control_commands VALUES (?)", (source,))
            if expected_question and expected_question[0] == task_id:
                conn.execute("UPDATE conversation_questions SET status='answered',answered_by=? WHERE id=?",
                             (source, expected_question[2]))
                conn.execute("UPDATE conversation_turns SET status='completed',task_id=?,receipt=? WHERE source=?",
                             (task_id, json.dumps({'question_id': expected_question[2],
                                                  'task_id': task_id, 'state': task['state']}), source))
                question_cycle = self.journal.cycle_id('question', expected_question[2])
                if self.journal.get(question_cycle, conn=conn):
                    self.journal.finish(question_cycle, phase='answered', data={'actor': source}, conn=conn)
                    incoming = self.journal.start('interaction', source, source=source, conn=conn)
                    self.journal.link(incoming, question_cycle, 'answers', conn=conn)
            self._queue_event(conn,task,'owner_command')
            return task

    def has_command(self, source):
        with self.connection() as conn:
            return conn.execute('SELECT 1 FROM control_commands WHERE source=?', (source,)).fetchone() is not None


class InstanceLock:
    """OS lock: only one coordinator owns a data directory, including after crashes."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            if self.path.stat().st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self):
        if self.file:
            self.file.close()
            self.file = None
