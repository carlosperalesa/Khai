"""Transactional Core control plane with capability-scoped organ ports.

The ports are the authority boundary used by organs.  Repositories are an
internal ownership boundary and never escape this module.  IDs locate durable
records; they are not bearer credentials.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import secrets
import time
import uuid

from app.kernel.database import connect, connection, migrate
from app.pixy.authorization import AuthorizationStore
from app.pixy.control import ControlStore
from app.kernel.execution_authority import VoiceExecutionPort


class Authority(str, Enum):
    COORDINATOR = "coordinator"
    PIXY = "pixy"
    HEART = "heart"
    HAND = "hand"


@dataclass(frozen=True)
class ControlPorts:
    coordinator: "CoordinatorPort"
    pixy: "PixyPort"
    heart: "HeartPort"
    hand: "HandPort"


class ControlUnitOfWork:
    """One SQLite transaction shared by logically owned repositories."""

    def __init__(self, path):
        self.path = str(Path(path).resolve())

    @contextmanager
    def transaction(self):
        conn = connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield ControlRepositories(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class DecisionRepository:
    """Pixy-owned authorization decisions."""

    def __init__(self, conn):
        self.conn = conn

    def get(self, authorization_id):
        return self.conn.execute(
            "SELECT * FROM pixy_authorizations WHERE authorization_id=?",
            (authorization_id,),
        ).fetchone()


class OrderRepository:
    """Hand-owned execution orders, enabled only by an exact Pixy decision."""

    def __init__(self, conn):
        self.conn = conn

    def get(self, order_id):
        return self.conn.execute(
            "SELECT * FROM execution_orders WHERE order_id=?", (order_id,)
        ).fetchone()


class TaskRepository:
    """Coordinator-owned objectives and attempts."""

    def __init__(self, conn):
        self.conn = conn

    def get(self, task_id):
        return self.conn.execute(
            "SELECT * FROM control_tasks WHERE id=?", (task_id,)
        ).fetchone()


class RecoveryRepository:
    """Heart-owned recovery cases."""

    def __init__(self, conn):
        self.conn = conn

    def get(self, case_id):
        return self.conn.execute(
            "SELECT * FROM recovery_cases WHERE case_id=?", (case_id,)
        ).fetchone()


class SecretRequestRepository:
    def __init__(self, conn):
        self.conn = conn

    def pending(self, user_id, chat_id, kind, now):
        return self.conn.execute(
            """SELECT * FROM secret_requests
               WHERE user_id=? AND chat_id=? AND kind=? AND status IN ('pending','capturing')
                 AND expires_at>? ORDER BY created_at""",
            (str(user_id), str(chat_id), kind, now),
        ).fetchall()


class ControlRepositories:
    def __init__(self, conn):
        self.tasks = TaskRepository(conn)
        self.decisions = DecisionRepository(conn)
        self.recoveries = RecoveryRepository(conn)
        self.orders = OrderRepository(conn)
        self.secrets = SecretRequestRepository(conn)


class CoordinatorPort:
    """Only Coordinator receives task mutation and budget operations."""

    authority = Authority.COORDINATOR

    def __init__(self, store):
        self.__store = store

    def create(self, *args, **kwargs):
        return self.__store.create(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self.__store.get(*args, **kwargs)

    def all(self, *args, **kwargs):
        return self.__store.all(*args, **kwargs)

    def mutate(self, *args, **kwargs):
        return self.__store.mutate(*args, **kwargs)

    def update(self, *args, **kwargs):
        return self.__store.update(*args, **kwargs)

    def observe_execution(self, *args, **kwargs):
        return self.__store.observe_execution(*args, **kwargs)

    def consume(self, *args, **kwargs):
        return self.__store.consume(*args, **kwargs)

    def event(self, *args, **kwargs):
        return self.__store.event(*args, **kwargs)

    def command(self, *args, **kwargs):
        return self.__store.command(*args, **kwargs)

    def has_command(self, source):
        return self.__store.has_command(source)

    def flush_events(self, *args, **kwargs):
        return self.__store.flush_events(*args, **kwargs)

    def eligible_rule_ids(self):
        return self.__store.eligible_rule_ids()


class PixyPort:
    """Pixy alone may issue and resolve authorization decisions."""

    authority = Authority.PIXY

    def __init__(self, plane):
        self.__plane = plane
        self.__authorizations = plane.authorizations

    def issue(self, *args, **kwargs):
        return self.__authorizations.issue(*args, **kwargs)

    def pending(self):
        return self.__authorizations.pending()

    def pending_all(self):
        return self.__authorizations.pending_all()

    def decide(self, *args, **kwargs):
        return self.__plane.decide_and_enable_order(*args, **kwargs)

    def request_secret(self, *args, **kwargs):
        return self.__plane.create_secret_request(*args, **kwargs)

    def secret_request(self, request_id):
        return self.__plane.get_secret_request(request_id)


class VoiceSecretPort:
    authority = "voice"

    def __init__(self, plane):
        self.__plane = plane

    def pending(self, *args, **kwargs):
        return self.__plane.pending_secret_requests(*args, **kwargs)

    def capture(self, *args, **kwargs):
        return self.__plane.capture_secret_request(*args, **kwargs)

    def complete(self, *args, **kwargs):
        return self.__plane.complete_secret_request(*args, **kwargs)

    def abandon(self, *args, **kwargs):
        return self.__plane.abandon_secret_request(*args, **kwargs)

    def outbox(self, *args, **kwargs):
        return self.__plane.secret_outbox(*args, **kwargs)

    def acknowledge(self, *args, **kwargs):
        return self.__plane.acknowledge_secret_event(*args, **kwargs)

class HeartPort:
    """Heart can inspect decisions but cannot issue, approve or consume them."""

    authority = Authority.HEART

    def __init__(self, authorizations):
        self.__authorizations = authorizations

    def get(self, *args, **kwargs):
        return self.__authorizations.get(*args, **kwargs)

    def verify(self, *args, **kwargs):
        return self.__authorizations.verify(*args, **kwargs)


class HandPort:
    """Hand can verify and atomically consume an exact authorization."""

    authority = Authority.HAND

    def __init__(self, plane):
        self.__plane = plane
        self.__authorizations = plane.authorizations

    def verify(self, *args, **kwargs):
        return self.__authorizations.verify(*args, **kwargs)

    def consume(self, *args, **kwargs):
        return self.__authorizations.consume(*args, **kwargs)

    def claim_order(self, order_id, authorization_id, nonce, case_id,
                    capabilities, target, plan_hash, consumer):
        return self.__plane.claim_authorized_order(
            order_id, authorization_id, nonce, case_id, capabilities,
            target, plan_hash, consumer)

    def dispatch_task_recovery(self, header, plan, transition):
        return self.__plane.dispatch_task_recovery(header, plan, transition)


class ControlPlane:
    """Composition-only owner of the common store and its scoped ports."""

    def __init__(self, path, events=None, authorization_ttl=900):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        migrate(self.path, [[
            """CREATE TABLE IF NOT EXISTS execution_orders(
                order_id TEXT PRIMARY KEY,
                authorization_id TEXT NOT NULL UNIQUE,
                case_id TEXT NOT NULL,
                operation_id TEXT NOT NULL UNIQUE,
                subject TEXT NOT NULL,
                artifact_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                receipt TEXT,
                created_at REAL NOT NULL,
                claimed_at REAL,
                completed_at REAL,
                FOREIGN KEY(authorization_id) REFERENCES pixy_authorizations(authorization_id)
            )""",
        ]], component="control-plane")
        migrate(self.path, [[
            """CREATE TABLE IF NOT EXISTS secret_requests(
                request_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                kind TEXT NOT NULL, env_key TEXT NOT NULL, provider TEXT NOT NULL,
                purpose TEXT NOT NULL, expected_version INTEGER NOT NULL,
                status TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                captured_at REAL, receipt_id TEXT)""",
            """CREATE INDEX IF NOT EXISTS secret_requests_pending
               ON secret_requests(user_id,chat_id,kind,status,expires_at)""",
            """CREATE TABLE IF NOT EXISTS secret_outbox(
                event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, event_type TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, delivered_at REAL)""",
        ]], component="secret-capture")
        self.tasks = ControlStore(self.path, events)
        self.authorizations = AuthorizationStore(self.path, ttl=authorization_ttl)
        self.uow = ControlUnitOfWork(self.path)
        self.ports = ControlPorts(
            coordinator=CoordinatorPort(self.tasks),
            pixy=PixyPort(self),
            heart=HeartPort(self.authorizations),
            hand=HandPort(self),
        )
        self.voice_secrets = VoiceSecretPort(self)
        self.voice_execution = VoiceExecutionPort(self.path)

    def create_secret_request(self, user_id, chat_id, kind, env_key, provider,
                              purpose, expected_version=0, ttl=900):
        if kind not in {"username", "password", "api_key"}:
            raise ValueError("Tipo de credencial inválido")
        if not all(isinstance(value, str) and value.strip() for value in
                   (user_id, chat_id, env_key, provider, purpose)):
            raise ValueError("Solicitud de credencial inválida")
        if not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError("Versión de credencial inválida")
        now = time.time()
        request = {
            "request_id": "sr_" + uuid.uuid4().hex,
            "user_id": str(user_id), "chat_id": str(chat_id), "kind": kind,
            "env_key": env_key, "provider": provider, "purpose": purpose,
            "expected_version": expected_version, "status": "pending",
            "created_at": now, "expires_at": now + max(1, int(ttl)),
        }
        with self.uow.transaction() as repos:
            repos.secrets.conn.execute(
                """INSERT INTO secret_requests
                   (request_id,user_id,chat_id,kind,env_key,provider,purpose,
                    expected_version,status,created_at,expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(request[key] for key in (
                    "request_id", "user_id", "chat_id", "kind", "env_key",
                    "provider", "purpose", "expected_version", "status",
                    "created_at", "expires_at")),
            )
        return request

    def get_secret_request(self, request_id):
        with self.uow.transaction() as repos:
            row = repos.secrets.conn.execute(
                'SELECT * FROM secret_requests WHERE request_id=?', (request_id,)).fetchone()
            return dict(row) if row else None

    def pending_secret_requests(self, user_id, chat_id, kind):
        with self.uow.transaction() as repos:
            now = time.time()
            repos.secrets.conn.execute(
                "UPDATE secret_requests SET status='expired' "
                "WHERE status='pending' AND expires_at<=?", (now,))
            return [dict(row) for row in repos.secrets.pending(user_id, chat_id, kind, now)]

    def capture_secret_request(self, request_id, user_id, chat_id, kind):
        with self.uow.transaction() as repos:
            row = repos.secrets.conn.execute(
                "SELECT * FROM secret_requests WHERE request_id=? AND user_id=? AND chat_id=?",
                (request_id, str(user_id), str(chat_id)),
            ).fetchone()
            if not row or row["kind"] != kind or row["status"] not in {"pending", "capturing"}:
                raise PermissionError("No hay una solicitud de credencial compatible")
            if row["expires_at"] <= time.time():
                repos.secrets.conn.execute(
                    "UPDATE secret_requests SET status='expired' WHERE request_id=?",
                    (request_id,),
                )
                raise PermissionError("No hay una solicitud de credencial compatible")
            if row["status"] == "pending":
                repos.secrets.conn.execute(
                    "UPDATE secret_requests SET status='capturing' "
                    "WHERE request_id=? AND status='pending'", (request_id,))
            return dict(row)

    def complete_secret_request(self, request_id, receipt_id):
        with self.uow.transaction() as repos:
            row = repos.secrets.conn.execute(
                "SELECT * FROM secret_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if not row:
                raise ValueError("Solicitud de credencial desconocida")
            if row["status"] == "captured":
                return dict(row)
            if row["status"] not in {"pending", "capturing"}:
                raise PermissionError("Solicitud de credencial no disponible")
            now = time.time()
            repos.secrets.conn.execute(
                "UPDATE secret_requests SET status='captured',captured_at=?,receipt_id=? "
                "WHERE request_id=? AND status IN ('pending','capturing')",
                (now, receipt_id, request_id),
            )
            repos.secrets.conn.execute(
                """INSERT INTO secret_outbox
                   (event_id,request_id,event_type,payload,status,created_at)
                   VALUES (?,?,?,?,?,?)""",
                ("evt_" + uuid.uuid4().hex, request_id, "SecretCaptured",
                 json.dumps({"request_id": request_id, "receipt_id": receipt_id}),
                 "pending", now),
            )
            return dict(repos.secrets.conn.execute(
                "SELECT * FROM secret_requests WHERE request_id=?", (request_id,)
            ).fetchone())

    def abandon_secret_request(self, request_id):
        with connection(self.path) as conn:
            conn.execute(
                "UPDATE secret_requests SET status='pending' "
                "WHERE request_id=? AND status='capturing'", (request_id,))

    def secret_outbox(self, limit=50):
        with connection(self.path) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM secret_outbox WHERE status='pending' "
                "ORDER BY created_at LIMIT ?", (int(limit),))]

    def acknowledge_secret_event(self, event_id):
        with connection(self.path) as conn:
            conn.execute(
                "UPDATE secret_outbox SET status='delivered',delivered_at=? "
                "WHERE event_id=? AND status='pending'", (time.time(), event_id))

    @staticmethod
    def _authorization(record):
        if not record:
            return None
        result = dict(record)
        result['capabilities'] = json.loads(result['capabilities'])
        return result

    def decide_and_enable_order(self, authorization_id, approve, decided_by):
        """Resolve Pixy's decision and enable Hand's exact order atomically."""
        now = time.time()
        with self.uow.transaction() as repos:
            conn = repos.decisions.conn
            conn.execute(
                'UPDATE pixy_authorizations SET status="expired" '
                'WHERE status IN ("pending","approved") AND expires_at<=?',
                (now,),
            )
            record = self._authorization(repos.decisions.get(authorization_id))
            if not record or record['status'] not in {'pending', 'approved', 'denied'}:
                raise PermissionError('Autorización desconocida, vencida o ya consumida')
            desired = 'approved' if approve else 'denied'
            if record['status'] == 'pending':
                conn.execute(
                    'UPDATE pixy_authorizations SET status=?,decided_at=?,decided_by=? '
                    'WHERE authorization_id=? AND status="pending"',
                    (desired, now, str(decided_by), authorization_id),
                )
                record.update(status=desired, decided_at=now, decided_by=str(decided_by))
            elif record['status'] != desired:
                raise PermissionError('La decisión ya fue resuelta con otro resultado')
            if approve:
                operation_id = 'recovery:' + record['case_id'] + ':' + record['plan_hash']
                existing = conn.execute(
                    'SELECT * FROM execution_orders WHERE authorization_id=?',
                    (authorization_id,),
                ).fetchone()
                if not existing:
                    order_id = 'order_' + uuid.uuid4().hex
                    conn.execute(
                        'INSERT INTO execution_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (order_id, authorization_id, record['case_id'], operation_id,
                         record['case_id'], record['plan_hash'], 'enabled', None,
                         now, None, None),
                    )
                else:
                    order_id = existing['order_id']
                record['order_id'] = order_id
                record['operation_id'] = operation_id
        return record

    def claim_authorized_order(self, order_id, authorization_id, nonce, case_id,
                               capabilities, target, plan_hash, consumer):
        """Atomically consume Pixy's grant and claim the matching Hand order."""
        with self.uow.transaction() as repos:
            return self._claim_order(repos, order_id, authorization_id, nonce, case_id,
                                     capabilities, target, plan_hash, consumer)

    def _claim_order(self, repos, order_id, authorization_id, nonce, case_id,
                     capabilities, target, plan_hash, consumer):
        now = time.time()
        conn = repos.decisions.conn
        conn.execute(
            'UPDATE pixy_authorizations SET status="expired" '
            'WHERE status IN ("pending","approved") AND expires_at<=?',
            (now,),
        )
        auth = self._authorization(repos.decisions.get(authorization_id))
        order = repos.orders.get(order_id)
        resumed = auth and auth['status'] == 'consumed' and auth['consumed_by'] == consumer
        self.authorizations._check(dict(auth, expires_at=float('inf')) if resumed else auth,
                                   nonce, case_id, capabilities, target, plan_hash)
        if not order:
            raise PermissionError('Orden de ejecución inexistente')
        if (order['authorization_id'] != authorization_id
                or order['case_id'] != case_id
                or order['artifact_hash'] != plan_hash):
            raise PermissionError('La orden no corresponde a la autorización presentada')
        expected_operation = 'recovery:' + case_id + ':' + plan_hash
        if not secrets.compare_digest(order['operation_id'], expected_operation):
            raise PermissionError('La identidad de operación no coincide')
        if auth['status'] == 'consumed' and auth['consumed_by'] == consumer:
            if order['status'] not in {'claimed', 'completed'}:
                raise PermissionError('Autorización consumida sin orden reconciliable')
            return dict(order)
        if auth['status'] != 'approved' or order['status'] != 'enabled':
            raise PermissionError('Autorización u orden no disponible')
        conn.execute(
            'UPDATE pixy_authorizations SET status="consumed",consumed_at=?,consumed_by=? '
            'WHERE authorization_id=? AND status="approved"',
            (now, consumer, authorization_id),
        )
        conn.execute(
            'UPDATE execution_orders SET status="claimed",claimed_at=? '
            'WHERE order_id=? AND status="enabled"',
            (now, order_id),
        )
        return dict(repos.orders.get(order_id))

    def dispatch_task_recovery(self, header, plan, transition):
        """Spend Hand's order and schedule the new attempt in the same transaction."""
        from app.heart.catalog import validate_plan, plan_hash
        core = validate_plan(plan)
        h = plan_hash(header['case_id'], core)
        if header.get('issued_by') != 'heart' or core['target'] != 'task' or h != header['plan_hash']:
            raise PermissionError('Orden de tarea alterada')
        with self.uow.transaction() as repos:
            conn = repos.tasks.conn
            order = self._claim_order(repos, header['order_id'], header['authorization_id'],
                                      header['nonce'], header['case_id'], core['capabilities'],
                                      core['target'], h, 'hand:' + header['case_id'])
            if order['status'] == 'completed':
                return json.loads(order['receipt'])['task']
            row = repos.tasks.get(core['task_id'])
            task = json.loads(row['document']) if row else None
            updated = transition(task)
            if updated['id'] != core['task_id']:
                raise PermissionError('La transición cambió el sujeto de la orden')
            if row:
                updated['revision'] += 1
                updated['updated_at'] = time.time()
                conn.execute('UPDATE control_tasks SET document=? WHERE id=?',
                             (json.dumps(updated), updated['id']))
            else:
                conn.execute('INSERT INTO control_tasks VALUES (?,?,?)',
                             (updated['id'], updated['source'], json.dumps(updated)))
            self.tasks._queue_event(conn, updated, 'recovery_dispatched')
            conn.execute('UPDATE execution_orders SET status="completed",receipt=?,completed_at=? WHERE order_id=?',
                         (json.dumps({'task': updated}), time.time(), header['order_id']))
            return updated
