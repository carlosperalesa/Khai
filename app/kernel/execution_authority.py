"""Voice's scoped dispatch port; admission serializes with task cancellation."""
import hashlib
import json
import time

from app.kernel.database import connection, migrate
from app.pixy.skill_approvals import scope_for


class UnconfirmedDispatch(PermissionError):
    pass


class VoiceExecutionPort:
    def __init__(self, path):
        self.__path = path
        migrate(path, [[
            '''CREATE TABLE IF NOT EXISTS voice_dispatches(
               request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
               execution_id TEXT NOT NULL, goal_revision INTEGER NOT NULL,
               artifact_hash TEXT NOT NULL, scope_hash TEXT NOT NULL,
               fingerprint TEXT NOT NULL, service_id TEXT NOT NULL,
               operation TEXT NOT NULL, service_version TEXT NOT NULL,
               effect TEXT NOT NULL, status TEXT NOT NULL,
               dispatched_at REAL NOT NULL, completed_at REAL)''',
            'CREATE INDEX IF NOT EXISTS voice_dispatch_execution ON voice_dispatches(execution_id,status)',
        ]], component='voice-dispatch')

    def authorize_dispatch(self, snapshot, request_id, fingerprint, service, operation, version, effect):
        """A committed dispatch is in flight; replay never sends another request."""
        if not version or effect not in {'read', 'write'}:
            raise PermissionError('Operation must have a pinned version and effect')
        with connection(self.__path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT * FROM voice_dispatches WHERE request_id=?', (request_id,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint or previous['task_id'] != snapshot['id']:
                    raise PermissionError('Dispatch identity changed')
                raise UnconfirmedDispatch('El envío anterior necesita su recibo de Voice; no se repite')
            row = conn.execute('SELECT document FROM control_tasks WHERE id=?', (snapshot['id'],)).fetchone()
            if not row:
                raise PermissionError('Task authority is absent')
            current = json.loads(row[0])
            runtime_status = current.get('runtime_status', (current.get('result') or {}).get('status'))
            allowed = (current['state'] == 'RUNNING' or current.get('mode') == 'AGENT'
                       and current['state'] in {'UNVERIFIED', 'WAITING_FEEDBACK', 'VERIFYING', 'COMPLETED'})
            if not allowed or runtime_status not in {'running', 'started'}:
                raise PermissionError('La ejecución ya no admite peticiones externas')
            if time.time() >= min(current['deadline'], snapshot['deadline']):
                raise PermissionError('Execution deadline expired')
            if any(current.get(key) != snapshot.get(key) for key in
                   ('execution_id', 'artifact_hash', 'authorized_hash')):
                raise PermissionError('Execution or reviewed artifact changed')
            if (current.get('goal_revision', 0) != snapshot.get('goal_revision', 0)
                    or not current.get('artifact_hash')
                    or current.get('authorized_hash') != current['artifact_hash']):
                raise PermissionError('Goal revision or artifact approval changed')
            current_scope = scope_for(current)
            if current_scope != scope_for(snapshot) or 'Network' not in current_scope['capabilities']:
                raise PermissionError('Reviewed network scope changed')
            if [service, operation, version] not in current_scope['services']:
                raise PermissionError('Operation version is outside execution scope')
            if effect == 'write' and conn.execute(
                    "SELECT 1 FROM voice_dispatches WHERE execution_id=? AND effect='write' "
                    "AND status IN ('dispatched','uncertain') LIMIT 1", (current['execution_id'],)).fetchone():
                raise UnconfirmedDispatch('Hay una escritura incierta en esta ejecución')
            scope_hash = hashlib.sha256(json.dumps(current_scope, sort_keys=True).encode()).hexdigest()
            conn.execute('INSERT INTO voice_dispatches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
                         (request_id, current['id'], current['execution_id'], current.get('goal_revision', 0),
                          current['artifact_hash'], scope_hash, fingerprint, service, operation, version,
                          effect, 'dispatched', time.time()))

    def record_outcome(self, request_id, status):
        if status not in {'completed', 'failed', 'uncertain', 'denied', 'partial'}:
            raise ValueError('Invalid dispatch outcome')
        with connection(self.__path) as conn:
            conn.execute("UPDATE voice_dispatches SET status=?,completed_at=? WHERE request_id=? AND status='dispatched'",
                         (status, time.time(), request_id))
