"""Durable control plane. Workers never receive this database or its credentials."""
import contextlib
import json
import os
import sqlite3
from app.common.database import connect
import time
import uuid
from pathlib import Path

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "EXHAUSTED", "UNKNOWN", "UNVERIFIED"}


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
        from app.common.database import adopt_schema
        adopt_schema(self.db_path,'control',['control_tasks','control_events','control_commands','event_outbox'])

    @contextlib.contextmanager
    def connection(self):
        conn = connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, source, request, **extra):
        task = dict(id="task_" + uuid.uuid4().hex, source=source, request=request,
                    state="ROUTING", created_at=time.time(), deadline=time.time() + 1800,
                    generations=0, executions=0, llm_calls=0, max_generations=4,
                    max_executions=4, max_llm_calls=16, revision=0, guidelines=[],
                    hypothesis_id=None, alternatives=[], result=None)
        task.update(extra)
        if not task.get('trace_id'):
            from app.common.tracing import get_trace_id, new_trace_id
            task['trace_id'] = get_trace_id() or new_trace_id()
        with self.connection() as conn:
            conn.execute("INSERT OR IGNORE INTO control_tasks VALUES (?, ?, ?)",
                         (task["id"], source, json.dumps(task)))
            return json.loads(conn.execute("SELECT document FROM control_tasks WHERE source=?", (source,)).fetchone()[0])

    def get(self, task_id):
        with self.connection() as conn:
            row = conn.execute("SELECT document FROM control_tasks WHERE id=?", (task_id,)).fetchone()
            return json.loads(row[0]) if row else None

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
            fn(task)
            task["revision"] += 1
            conn.execute("UPDATE control_tasks SET document=? WHERE id=?", (json.dumps(task), task_id))
            self._queue_event(conn,task,'task_state')
            return task

    def _queue_event(self, conn, task, kind):
        event = dict(event_id=uuid.uuid4().hex,correlation_id=task['id'],task_id=task['id'],
                     execution_id=task.get('execution_id'),skill_name=task.get('skill_name'),version_id=task.get('version_id'),
                     metadata={'state':task['state'],'revision':task['revision']},event_type=kind)
        conn.execute('INSERT INTO event_outbox VALUES (?,?)',(event['event_id'],json.dumps(event)))

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

    def consume(self, task_id, counter):
        def apply(task):
            if task["state"] in TERMINAL or time.time() >= task["deadline"]:
                raise BudgetExceeded("Objetivo detenido o plazo agotado")
            if task[counter] >= task["max_" + counter]:
                raise BudgetExceeded("Presupuesto agotado: " + counter)
            task[counter] += 1
        return self.mutate(task_id, apply)

    def event(self, task_id, kind, data):
        with self.connection() as conn:
            conn.execute("INSERT INTO control_events(task_id,kind,data,created_at) VALUES (?,?,?,?)",
                         (task_id, kind, json.dumps(data), time.time()))

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
            fn(task)
            task["revision"] += 1
            conn.execute("UPDATE control_tasks SET document=? WHERE id=?", (json.dumps(task), task_id))
            conn.execute("INSERT INTO control_commands VALUES (?)", (source,))
            self._queue_event(conn,task,'owner_command')
            return task


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
