"""Common durable interaction tree. IDs correlate evidence; they grant no authority."""
import contextlib
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import time
import uuid

from app.kernel.database import connection, migrate
from app.kernel.tracing import get_trace_id

_active = ContextVar('journal_cycle', default=None)
STATES = {'active', 'waiting', 'completed', 'failed', 'rejected', 'cancelled', 'unknown', 'exhausted'}
TERMINAL = {'completed', 'failed', 'rejected', 'cancelled', 'exhausted'}
FIELDS = set(('task_id execution_id agent_run_id case_id authorization_id order_id delivery_id '
              'question_id goal_revision attempt_number model profile effective_mode skill_name '
              'version_id operation service_id phase status error_type http_status reason_code '
              'response_sha256 evidence_id provider_call_id provider tool_name elapsed_seconds '
              'message_id chunk_index offset length byte_count actor').split())


def _safe(data):
    """A technical envelope, not a second store of prompts/provider bodies/secrets."""
    if not isinstance(data or {}, dict):
        raise ValueError('Journal data must be a technical envelope')
    result = {}
    for key, value in (data or {}).items():
        if key not in FIELDS:
            raise ValueError('Unsupported journal field: ' + str(key))
        if value is not None and not isinstance(value, (str, bool, int, float)):
            raise ValueError('Journal values must be scalar')
        if isinstance(value, str) and len(value) > 256:
            raise ValueError('Journal field exceeds limit')
        result[key] = value
    return json.dumps(result, ensure_ascii=False, allow_nan=False)


class Journal:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        migrate(path, [[
            '''CREATE TABLE IF NOT EXISTS journal_cycles(
                cycle_id TEXT PRIMARY KEY, kind TEXT NOT NULL, object_id TEXT NOT NULL,
                root_id TEXT NOT NULL, parent_id TEXT, trace_id TEXT NOT NULL, source TEXT,
                status TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL, refs TEXT NOT NULL,
                UNIQUE(kind,object_id), FOREIGN KEY(parent_id) REFERENCES journal_cycles(cycle_id))''',
            '''CREATE TABLE IF NOT EXISTS journal_events(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                cycle_id TEXT NOT NULL REFERENCES journal_cycles(cycle_id),
                created_at REAL NOT NULL, phase TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS journal_links(
                from_id TEXT NOT NULL REFERENCES journal_cycles(cycle_id),
                to_id TEXT NOT NULL REFERENCES journal_cycles(cycle_id), relation TEXT NOT NULL,
                created_at REAL NOT NULL, PRIMARY KEY(from_id,to_id,relation))''',
            'CREATE INDEX IF NOT EXISTS journal_root ON journal_cycles(root_id,started_at)',
            'CREATE INDEX IF NOT EXISTS journal_trace ON journal_cycles(trace_id)',
            'CREATE INDEX IF NOT EXISTS journal_parent ON journal_cycles(parent_id)',
            'CREATE INDEX IF NOT EXISTS journal_event_cycle ON journal_events(cycle_id,sequence)',
            '''CREATE TRIGGER IF NOT EXISTS journal_cycle_identity BEFORE UPDATE ON journal_cycles
               WHEN NEW.cycle_id IS NOT OLD.cycle_id OR NEW.kind IS NOT OLD.kind
               OR NEW.object_id IS NOT OLD.object_id OR NEW.root_id IS NOT OLD.root_id
               OR NEW.parent_id IS NOT OLD.parent_id OR NEW.trace_id IS NOT OLD.trace_id
               OR NEW.source IS NOT OLD.source OR NEW.refs IS NOT OLD.refs
               OR NEW.started_at IS NOT OLD.started_at
               BEGIN SELECT RAISE(ABORT,'journal identity is immutable'); END''',
            *[f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table}
                  BEGIN SELECT RAISE(ABORT,'journal history is immutable'); END'''
              for table in ('journal_events', 'journal_links') for action in ('UPDATE', 'DELETE')],
            '''CREATE TRIGGER IF NOT EXISTS journal_cycles_no_delete BEFORE DELETE ON journal_cycles
               BEGIN SELECT RAISE(ABORT,'journal history is immutable'); END''',
        ]], component='interaction-journal')

    @staticmethod
    def cycle_id(kind, object_id):
        return 'cycle_' + hashlib.sha256((str(kind) + ':' + str(object_id)).encode()).hexdigest()[:32]

    def current(self):
        current = _active.get()
        return current[1] if current and current[0] == self.path else None

    @contextlib.contextmanager
    def _connection(self, conn=None):
        if conn is not None:
            yield conn
        else:
            with connection(self.path) as owned:
                owned.execute('BEGIN IMMEDIATE')
                yield owned

    def start(self, kind, object_id, *, parent_id=None, trace_id=None, source=None, refs=None, conn=None):
        if not kind or len(str(kind)) > 64 or not object_id or len(str(object_id)) > 256:
            raise ValueError('Invalid journal branch identity')
        data = _safe(refs)
        cid = self.cycle_id(kind, object_id)
        with self._connection(conn) as db:
            if db.execute('SELECT 1 FROM journal_cycles WHERE cycle_id=?', (cid,)).fetchone():
                return cid
            parent = parent_id
            if parent is None:
                current = _active.get()
                if current and current[0] == self.path:
                    parent = current[1]
                elif (refs or {}).get('task_id'):
                    candidate = self.cycle_id('task', refs['task_id'])
                    if candidate != cid and db.execute('SELECT 1 FROM journal_cycles WHERE cycle_id=?', (candidate,)).fetchone():
                        parent = candidate
            if parent == cid:
                parent = None
            root, inherited_trace, inherited_source = cid, '', None
            if parent:
                row = db.execute('SELECT root_id,trace_id,source FROM journal_cycles WHERE cycle_id=?', (parent,)).fetchone()
                if not row:
                    raise ValueError('Journal parent does not exist')
                root, inherited_trace, inherited_source = tuple(row)
                if trace_id and inherited_trace and trace_id != inherited_trace:
                    raise ValueError('A child must keep its parent trace; link independent interactions instead')
            now = time.time()
            db.execute('INSERT INTO journal_cycles VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (cid, str(kind), str(object_id), root, parent, trace_id or inherited_trace or get_trace_id(),
                        source or inherited_source, 'active', now, None, data))
            self.event(cid, 'started', data=refs, conn=db)
        return cid

    def event(self, cycle_id, phase, *, status=None, data=None, conn=None, event_id=None):
        if not phase or len(str(phase)) > 100 or (status is not None and status not in STATES):
            raise ValueError('Invalid journal transition')
        document = _safe(data)
        eid = event_id or 'event_' + uuid.uuid4().hex
        with self._connection(conn) as db:
            previous = db.execute('SELECT * FROM journal_events WHERE event_id=?', (eid,)).fetchone()
            if previous:
                if ((previous['cycle_id'], previous['phase'], previous['data']) != (cycle_id, phase, document)
                        or status is not None and previous['status'] != status):
                    raise ValueError('Journal event ID reused with changed content')
                return eid
            row = db.execute('SELECT status FROM journal_cycles WHERE cycle_id=?', (cycle_id,)).fetchone()
            if not row:
                raise KeyError(cycle_id)
            state = status or row['status']
            # Late evidence can be appended; it cannot silently reopen a closed operation.
            recovery = (phase == 'authorized_attempt_started' and (data or {}).get('authorization_id'))
            if row['status'] in TERMINAL and state != row['status'] and not recovery:
                raise ValueError('Journal cycle already closed')
            now = time.time()
            db.execute('INSERT INTO journal_events(event_id,cycle_id,created_at,phase,status,data) VALUES (?,?,?,?,?,?)',
                       (eid, cycle_id, now, str(phase), state, document))
            db.execute('UPDATE journal_cycles SET status=?,ended_at=? WHERE cycle_id=?',
                       (state, now if state in TERMINAL or state == 'unknown' else None, cycle_id))
        return eid

    def finish(self, cycle_id, status='completed', *, phase='finished', data=None, conn=None):
        return self.event(cycle_id, phase, status=status, data=data, conn=conn)

    @contextlib.contextmanager
    def activate(self, cycle_id):
        token = _active.set((self.path, cycle_id))
        try:
            yield cycle_id
        finally:
            _active.reset(token)

    @contextlib.contextmanager
    def scope(self, kind, object_id, **kwargs):
        cid = self.start(kind, object_id, **kwargs)
        with self.activate(cid):
            try:
                yield cid
            except Exception as exc:
                if self.get(cid)['status'] not in TERMINAL:
                    self.finish(cid, 'rejected' if isinstance(exc, PermissionError) else 'failed',
                                data={'error_type': type(exc).__name__})
                raise
            else:
                if self.get(cid)['status'] not in TERMINAL | {'waiting', 'unknown'}:
                    self.finish(cid)

    def link(self, from_id, to_id, relation, *, conn=None):
        if not relation or len(relation) > 64:
            raise ValueError('Invalid journal link')
        with self._connection(conn) as db:
            db.execute('INSERT OR IGNORE INTO journal_links VALUES (?,?,?,?)',
                       (from_id, to_id, relation, time.time()))

    def get(self, cycle_id, *, conn=None):
        with self._connection(conn) as db:
            row = db.execute('SELECT * FROM journal_cycles WHERE cycle_id=?', (cycle_id,)).fetchone()
        return dict(row) if row else None

    def tree(self, cycle_id, *, max_cycles=200, max_events=1000):
        max_cycles, max_events = max(1, min(500, max_cycles)), max(1, min(2000, max_events))
        with connection(self.path) as db:
            rows = db.execute('''WITH RECURSIVE branch(id) AS (
                SELECT cycle_id FROM journal_cycles WHERE cycle_id=? UNION ALL
                SELECT c.cycle_id FROM journal_cycles c JOIN branch p ON c.parent_id=p.id)
                SELECT c.* FROM journal_cycles c JOIN branch b ON c.cycle_id=b.id
                ORDER BY c.started_at LIMIT ?''', (cycle_id, max_cycles + 1)).fetchall()
            ids = [r['cycle_id'] for r in rows[:max_cycles]]
            if not ids:
                return {'cycles': [], 'events': [], 'links': [], 'truncated': False}
            placeholders = ','.join('?' for _ in ids)
            events = db.execute(f'SELECT * FROM journal_events WHERE cycle_id IN ({placeholders}) ORDER BY sequence LIMIT ?',
                                (*ids, max_events + 1)).fetchall()
            links = db.execute(f'SELECT * FROM journal_links WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders})',
                               (*ids, *ids)).fetchall()
        return {'cycles': [dict(r, refs=json.loads(r['refs'])) for r in rows[:max_cycles]],
                'events': [dict(r, data=json.loads(r['data'])) for r in events[:max_events]],
                'links': [dict(r) for r in links],
                'truncated': len(rows) > max_cycles or len(events) > max_events}

    def find(self, reference, limit=20):
        """Exact IDs only. An unrelated recent error is never a substitute."""
        with connection(self.path) as db:
            rows = db.execute('SELECT * FROM journal_cycles WHERE cycle_id=? OR object_id=? '
                              'OR trace_id=? OR source=? ORDER BY started_at LIMIT ?',
                              (reference, reference, reference, reference, max(1, min(100, limit)))).fetchall()
        return [dict(r) for r in rows]

    def interrupt_open(self):
        """Called once by the process owner on startup; uncertainty never triggers a replay."""
        with self._connection() as db:
            ids = [r[0] for r in db.execute("SELECT cycle_id FROM journal_cycles WHERE status='active'")]
            for cid in ids:
                self.event(cid, 'process_interrupted', status='unknown',
                           data={'reason_code': 'process_restart'}, conn=db)
        return len(ids)
