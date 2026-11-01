"""Durable, bounded tool loop shared by Hand construction activities."""
import hashlib
import json
import time
import uuid

from pathlib import Path
from app.kernel.database import connection, migrate
from app.kernel.journal import Journal


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


class AgentExhausted(RuntimeError):
    pass


class AgentReconciliationRequired(RuntimeError):
    pass


class AgentLoop:
    def __init__(self, db_path, llm, *, max_turns=16, max_tools=40, timeout=600, journal=None):
        self.db_path, self.llm = db_path, llm
        self.journal = journal or Journal(db_path)
        self.max_turns, self.max_tools, self.timeout = max_turns, max_tools, timeout
        migrate(db_path, [[
            '''CREATE TABLE IF NOT EXISTS agent_runs(
               run_id TEXT PRIMARY KEY, task_id TEXT, purpose TEXT NOT NULL, status TEXT NOT NULL,
               turns INTEGER NOT NULL, tool_calls INTEGER NOT NULL, result TEXT, error TEXT,
               created_at REAL NOT NULL, updated_at REAL NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS agent_messages(
               id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
               role TEXT NOT NULL, content TEXT, document TEXT NOT NULL, created_at REAL NOT NULL,
               UNIQUE(run_id,sequence))''',
            '''CREATE TABLE IF NOT EXISTS agent_tool_calls(
               call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, name TEXT NOT NULL, arguments TEXT NOT NULL,
               result TEXT NOT NULL, ok INTEGER NOT NULL, error_fingerprint TEXT, created_at REAL NOT NULL)''',
        ], ['ALTER TABLE agent_tool_calls ADD COLUMN provider_call_id TEXT',
            'UPDATE agent_tool_calls SET provider_call_id=call_id']], component='hand-agent-loop')

    def _append(self, run_id, message):
        with connection(self.db_path) as conn:
            seq = conn.execute('SELECT COALESCE(MAX(sequence),-1)+1 FROM agent_messages WHERE run_id=?',
                               (run_id,)).fetchone()[0]
            conn.execute('INSERT INTO agent_messages(run_id,sequence,role,content,document,created_at) '
                         'VALUES (?,?,?,?,?,?)', (run_id, seq, message['role'], message.get('content'),
                                                 _canonical(message), time.time()))

    def _messages(self, run_id):
        with connection(self.db_path) as conn:
            rows = conn.execute('SELECT document FROM agent_messages WHERE run_id=? ORDER BY sequence',
                                (run_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _journal_conn(self, conn):
        return conn if Path(self.journal.path).resolve() == Path(self.db_path).resolve() else None

    def run(self, task_id, purpose, system_prompt, tools, *, run_id=None):
        run_id = run_id or ('agent_' + uuid.uuid4().hex)
        self.last_run_id = run_id
        cycle = self.journal.start('agent', run_id, refs={'task_id': task_id, 'agent_run_id': run_id})
        with self.journal.activate(cycle):
            try:
                result = self._run(task_id, purpose, system_prompt, tools, run_id=run_id)
            except Exception as exc:
                status = 'exhausted' if isinstance(exc, AgentExhausted) else 'failed'
                if isinstance(exc, AgentReconciliationRequired):
                    previous = self.journal.get(cycle)['status']
                    status = previous if previous in {'completed', 'failed', 'exhausted', 'rejected', 'cancelled'} else 'unknown'
                with connection(self.db_path) as conn:
                    if not isinstance(exc, AgentReconciliationRequired) or status == 'unknown':
                        conn.execute('UPDATE agent_runs SET status=?,error=?,updated_at=? WHERE run_id=?',
                                     (status, type(exc).__name__, time.time(), run_id))
                    self.journal.finish(cycle, status, data={'error_type': type(exc).__name__},
                                        conn=self._journal_conn(conn))
                raise
            else:
                if self.journal.get(cycle)['status'] != 'completed':
                    self.journal.finish(cycle)
                return result

    def _run(self, task_id, purpose, system_prompt, tools, *, run_id=None):
        handlers = {tool['name']: tool['handler'] for tool in tools}
        schemas = [{'name': tool['name'], 'description': tool['description'],
                    'parameters': tool['parameters']} for tool in tools]
        run_id = run_id or ('agent_' + uuid.uuid4().hex)
        self.last_run_id = run_id
        now = time.time()
        created = False
        with connection(self.db_path) as conn:
            existing = conn.execute('SELECT * FROM agent_runs WHERE run_id=?', (run_id,)).fetchone()
            if existing and existing['status'] == 'completed':
                return json.loads(existing['result']), run_id
            if existing:
                raise AgentReconciliationRequired('An existing unfinished agent run requires reconciliation')
            if not existing:
                conn.execute('INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?,?,?)',
                             (run_id, task_id, purpose, 'running', 0, 0, None, None, now, now))
                created = True
        if created:
            self._append(run_id, {'role': 'system', 'content': system_prompt})
            self._append(run_id, {'role': 'user', 'content': purpose})
        started = now
        repeated = {}
        while True:
            with connection(self.db_path) as conn:
                state = dict(conn.execute('SELECT * FROM agent_runs WHERE run_id=?', (run_id,)).fetchone())
            if state['turns'] >= self.max_turns or state['tool_calls'] >= self.max_tools \
                    or time.time() - started >= self.timeout:
                error = 'Agent budget exhausted'
                with connection(self.db_path) as conn:
                    conn.execute('UPDATE agent_runs SET status="exhausted",error=?,updated_at=? WHERE run_id=?',
                                 (error, time.time(), run_id))
                raise AgentExhausted(error)
            with self.journal.scope('agent_model_call', run_id + ':' + str(state['turns']),
                                    refs={'task_id': task_id, 'agent_run_id': run_id}):
                turn = self.llm.complete(self._messages(run_id), schemas)
                if not isinstance(turn, dict) or not isinstance(turn.get('tool_calls') or [], list):
                    raise ValueError('Invalid agent turn envelope')
                if any(not isinstance(call, dict) for call in turn.get('tool_calls') or []):
                    raise ValueError('Invalid agent tool envelope')
            calls = turn.get('tool_calls') or []
            assistant = {'role': 'assistant', 'content': turn.get('content') or '', 'tool_calls': calls,
                         'provider': turn.get('provider'), 'model': turn.get('model')}
            self._append(run_id, assistant)
            with connection(self.db_path) as conn:
                conn.execute('UPDATE agent_runs SET turns=turns+1,updated_at=? WHERE run_id=?',
                             (time.time(), run_id))
            if not calls:
                fingerprint = hashlib.sha256((turn.get('content') or '').encode()).hexdigest()[:16]
                repeated[fingerprint] = repeated.get(fingerprint, 0) + 1
                if repeated[fingerprint] >= 2:
                    raise AgentExhausted('El agente repitió una respuesta sin ejecutar una herramienta')
                self._append(run_id, {'role': 'user', 'content':
                    'Aún no terminaste. Usa una herramienta y entrega el artefacto mediante finish.'})
                continue
            for call in calls:
                name, arguments = call.get('name'), call.get('arguments') or {}
                call_id = 'call_' + uuid.uuid4().hex
                tool_cycle = self.journal.start('agent_tool', call_id, refs={
                    'task_id': task_id, 'agent_run_id': run_id,
                    'tool_name': str(name) if name in handlers else 'unknown',
                    'provider_call_id': str(call.get('id') or '')[:256]})
                ok = True
                try:
                    if name not in handlers:
                        raise ValueError('Unknown tool: ' + str(name))
                    with self.journal.activate(tool_cycle):
                        result = handlers[name](arguments)
                    if not isinstance(result, dict):
                        result = {'result': result}
                    result = {'ok': True, **result}
                except Exception as exc:
                    ok = False
                    result = {'ok': False, 'error_type': type(exc).__name__, 'error': str(exc)}
                raw = _canonical(result)
                fingerprint = None if ok else hashlib.sha256(raw.encode()).hexdigest()[:16]
                with connection(self.db_path) as conn:
                    conn.execute('INSERT INTO agent_tool_calls VALUES (?,?,?,?,?,?,?,?,?)',
                                 (call_id, run_id, str(name), _canonical(arguments), raw, int(ok),
                                  fingerprint, time.time(), call.get('id')))
                    self.journal.finish(tool_cycle, 'completed' if ok else 'failed',
                                        data={} if ok else {'error_type': result['error_type']},
                                        conn=self._journal_conn(conn))
                    conn.execute('UPDATE agent_runs SET tool_calls=tool_calls+1,updated_at=? WHERE run_id=?',
                                 (time.time(), run_id))
                self._append(run_id, {'role': 'tool', 'tool_call_id': call.get('id'),
                                      'name': name, 'content': raw})
                if not ok:
                    repeated[fingerprint] = repeated.get(fingerprint, 0) + 1
                    if repeated[fingerprint] >= 2:
                        with connection(self.db_path) as conn:
                            conn.execute('UPDATE agent_runs SET status="exhausted",error=?,updated_at=? '
                                         'WHERE run_id=?', (raw, time.time(), run_id))
                        raise AgentExhausted('Mismo error repetido sin una corrección efectiva: ' + raw)
                if ok and result.get('finished'):
                    payload = result.get('value')
                    with connection(self.db_path) as conn:
                        conn.execute('UPDATE agent_runs SET status="completed",result=?,updated_at=? WHERE run_id=?',
                                     (_canonical(payload), time.time(), run_id))
                        self.journal.finish(Journal.cycle_id('agent', run_id),
                                            conn=self._journal_conn(conn))
                    return payload, run_id
