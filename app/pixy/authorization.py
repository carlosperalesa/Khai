"""Durable, single-use recovery authorizations issued by Pixy and verified by Hand."""
import json
import secrets
import time
import uuid
from functools import wraps
from app.kernel.database import connection, migrate
from app.kernel.journal import Journal
from app.heart.catalog import validate_case_id, TARGETS

STATUSES = {'pending', 'approved', 'denied', 'consumed', 'expired', 'superseded'}


def _audited_operation(method):
    """Rejected authority checks are operations too; never persist their terms."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        refs = {'operation': method.__name__}
        key = 'case_id' if method.__name__ == 'issue' else 'authorization_id'
        if args or kwargs.get(key):
            refs[key] = args[0] if args else kwargs[key]
        cycle = self.journal.start('pixy_request', uuid.uuid4().hex, refs=refs)
        if refs.get('authorization_id'):
            linked = Journal.cycle_id('authorization', refs['authorization_id'])
            if self.journal.get(linked):
                self.journal.link(cycle, linked, 'checks_authorization')
        try:
            with self.journal.activate(cycle):
                result = method(self, *args, **kwargs)
        except Exception as exc:
            self.journal.finish(cycle, 'rejected' if isinstance(exc, PermissionError) else 'failed',
                                data={'error_type': type(exc).__name__, 'operation': method.__name__})
            raise
        self.journal.finish(cycle)
        return result
    return wrapped


class AuthorizationStore:
    def __init__(self, path, ttl=900):
        self.path, self.ttl = path, ttl
        self.journal = Journal(path)
        migrate(path, [
            [
                '''CREATE TABLE IF NOT EXISTS pixy_authorizations(authorization_id TEXT PRIMARY KEY,
                   case_id TEXT NOT NULL UNIQUE, issued_by TEXT NOT NULL, authorized_for TEXT NOT NULL,
                   requested_by TEXT NOT NULL, capabilities TEXT NOT NULL, target TEXT NOT NULL,
                   plan_hash TEXT NOT NULL, nonce TEXT NOT NULL, status TEXT NOT NULL,
                   created_at REAL NOT NULL, expires_at REAL NOT NULL, decided_at REAL, decided_by TEXT,
                   consumed_at REAL, consumed_by TEXT)''',
                '''CREATE TRIGGER IF NOT EXISTS pixy_authorizations_no_delete BEFORE DELETE ON pixy_authorizations
                   BEGIN SELECT RAISE(ABORT,'authorization history is immutable'); END''',
                '''CREATE TRIGGER IF NOT EXISTS pixy_authorizations_terms BEFORE UPDATE ON pixy_authorizations
                   WHEN NEW.capabilities IS NOT OLD.capabilities OR NEW.target IS NOT OLD.target
                     OR NEW.plan_hash IS NOT OLD.plan_hash OR NEW.nonce IS NOT OLD.nonce
                     OR NEW.case_id IS NOT OLD.case_id OR NEW.expires_at IS NOT OLD.expires_at
                   BEGIN SELECT RAISE(ABORT,'authorization terms are immutable'); END''',
            ],
            [
                'ALTER TABLE pixy_authorizations ADD COLUMN trace_id TEXT',
                'CREATE INDEX IF NOT EXISTS idx_pixy_auth_trace ON pixy_authorizations(trace_id)',
            ]
        ], component='pixy-authorizations')

    @staticmethod
    def _row(row):
        if not row:
            return None
        record = dict(row)
        record['capabilities'] = json.loads(record['capabilities'])
        return record

    def _expire(self, conn):
        expired = conn.execute('SELECT authorization_id,case_id,trace_id FROM pixy_authorizations '
                               'WHERE status IN ("pending","approved") AND expires_at<=?',
                               (time.time(),)).fetchall()
        conn.execute('UPDATE pixy_authorizations SET status="expired" WHERE status IN ("pending","approved") AND expires_at<=?',
                     (time.time(),))
        for row in expired:
            cycle = self.journal.start('authorization', row['authorization_id'], trace_id=row['trace_id'],
                                      refs={'authorization_id': row['authorization_id'], 'case_id': row['case_id']}, conn=conn)
            self.journal.finish(cycle, 'cancelled', phase='expired', data={'reason_code': 'expired'}, conn=conn)

    def get(self, authorization_id):
        with connection(self.path) as conn:
            self._expire(conn)
            return self._row(conn.execute('SELECT * FROM pixy_authorizations WHERE authorization_id=?',
                                          (authorization_id,)).fetchone())

    def get_by_trace(self, trace_id):
        if not trace_id:
            return None
        with connection(self.path) as conn:
            self._expire(conn)
            row = conn.execute(
                'SELECT * FROM pixy_authorizations WHERE (trace_id=? OR case_id=?) ORDER BY created_at DESC LIMIT 1',
                (str(trace_id), str(trace_id))).fetchone()
            return self._row(row)

    def verify_by_trace(self, trace_id, capability=None, target=None):
        raise PermissionError('trace_id solo correlaciona; no concede autoridad')

    def consume_by_trace(self, trace_id, capability=None, consumer='hand'):
        raise PermissionError('trace_id solo correlaciona; presenta authorization_id y términos completos')

    @_audited_operation
    def issue(self, case_id, capabilities, target, plan_hash, requested_by, trace_id=None):
        validate_case_id(case_id)
        if target not in TARGETS or not isinstance(capabilities, list) or not capabilities:
            raise ValueError('Autorización sin alcance válido')
        now = time.time()
        tid = trace_id or (case_id if str(case_id).startswith(('req-', 'trace-', 'tr-')) else None)
        if not tid:
            try:
                from app.kernel.tracing import get_trace_id
                repair = self.journal.get(Journal.cycle_id('repair', case_id))
                tid = get_trace_id() or (repair or {}).get('trace_id') or case_id
            except Exception:
                tid = case_id
        record = dict(authorization_id='auth_' + uuid.uuid4().hex, case_id=case_id, trace_id=tid, issued_by='pixy',
                      authorized_for='hand', requested_by=str(requested_by), capabilities=sorted(capabilities),
                      target=target, plan_hash=plan_hash, nonce=secrets.token_hex(16), status='pending',
                      created_at=now, expires_at=now + self.ttl)
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._expire(conn)
            conn.execute('''INSERT INTO pixy_authorizations(authorization_id,case_id,trace_id,issued_by,authorized_for,
                requested_by,capabilities,target,plan_hash,nonce,status,created_at,expires_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (record['authorization_id'], case_id, tid, 'pixy', 'hand', record['requested_by'],
                 json.dumps(record['capabilities']), target, plan_hash, record['nonce'], 'pending', now,
                 record['expires_at']))
            cycle = self.journal.start('authorization', record['authorization_id'], trace_id=tid,
                source=str(requested_by), refs={'authorization_id': record['authorization_id'], 'case_id': case_id}, conn=conn)
            self.journal.event(cycle, 'issued', status='waiting', conn=conn)
        return record

    def pending(self):
        records = self.pending_all()
        return records[-1] if records else None

    def pending_all(self):
        with connection(self.path) as conn:
            self._expire(conn)
            return [self._row(row) for row in conn.execute(
                'SELECT * FROM pixy_authorizations WHERE status="pending" ORDER BY created_at')]

    @_audited_operation
    def decide(self, authorization_id, approve, decided_by):
        # Expiration must commit even when the attempted decision is rejected.
        self.get(authorization_id)
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._expire(conn)
            row = conn.execute('SELECT status FROM pixy_authorizations WHERE authorization_id=?',
                               (authorization_id,)).fetchone()
            if not row or row['status'] != 'pending':
                raise PermissionError('Autorización desconocida, vencida o ya resuelta')
            conn.execute('UPDATE pixy_authorizations SET status=?,decided_at=?,decided_by=? WHERE authorization_id=?',
                         ('approved' if approve else 'denied', time.time(), str(decided_by), authorization_id))
            cycle = self.journal.start('authorization', authorization_id, refs={'authorization_id': authorization_id}, conn=conn)
            if approve:
                self.journal.event(cycle, 'approved', status='waiting', conn=conn)
            else:
                self.journal.finish(cycle, 'rejected', phase='denied', conn=conn)
        return self.get(authorization_id)

    def _check(self, record, nonce, case_id, capabilities, target, plan_hash):
        if not record:
            raise PermissionError('Autorización inexistente')
        if record['issued_by'] != 'pixy' or record['authorized_for'] != 'hand':
            raise PermissionError('Emisor o destinatario de la autorización inválido')
        if not secrets.compare_digest(record['nonce'], str(nonce)):
            raise PermissionError('Nonce de autorización inválido')
        if record['case_id'] != case_id:
            raise PermissionError('La autorización pertenece a otro caso')
        if record['capabilities'] != sorted(capabilities):
            raise PermissionError('Las capacidades no coinciden exactamente con la autorización')
        if record['target'] != target:
            raise PermissionError('El objetivo no coincide con la autorización')
        if not secrets.compare_digest(record['plan_hash'], str(plan_hash)):
            raise PermissionError('El plan fue alterado después de la autorización')
        if record['expires_at'] <= time.time():
            raise PermissionError('Autorización vencida')

    @_audited_operation
    def verify(self, authorization_id, nonce, case_id, capabilities, target, plan_hash):
        """Non-consuming check used by Heart (on decision) and Hand (on skill creation)."""
        record = self.get(authorization_id)
        self._check(record, nonce, case_id, capabilities, target, plan_hash)
        if record['status'] != 'approved':
            raise PermissionError('Autorización no aprobada (' + record['status'] + ')')
        return record

    @_audited_operation
    def consume(self, authorization_id, nonce, case_id, capabilities, target, plan_hash, consumer):
        """Atomically spend the authorization. A second call with another consumer fails."""
        self.get(authorization_id)
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._expire(conn)
            record = self._row(conn.execute('SELECT * FROM pixy_authorizations WHERE authorization_id=?',
                                            (authorization_id,)).fetchone())
            if record and record['status'] == 'consumed' and record['consumed_by'] == consumer:
                # Crash-resume of the same consumer: terms are checked again, never re-spent.
                self._check(dict(record, expires_at=float('inf')), nonce, case_id, capabilities, target, plan_hash)
                return record
            self._check(record, nonce, case_id, capabilities, target, plan_hash)
            if record['status'] != 'approved':
                raise PermissionError('Autorización no disponible (' + record['status'] + ')')
            conn.execute('UPDATE pixy_authorizations SET status="consumed",consumed_at=?,consumed_by=? '
                         'WHERE authorization_id=? AND status="approved"', (time.time(), consumer, authorization_id))
            cycle = self.journal.start('authorization', authorization_id, refs={'authorization_id': authorization_id}, conn=conn)
            self.journal.finish(cycle, phase='consumed', conn=conn)
        return self.get(authorization_id)
