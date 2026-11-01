"""Cancelable, durable model changes. Only the current generation may publish."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import threading
import time
import uuid
from urllib.parse import quote

import requests

from app.kernel.database import connection, migrate
from app.mind.brain.controller import PROFILES, transition_token


class TransitionCancelled(Exception):
    pass


class RemoteOutcomeUnknown(Exception):
    pass


class NanoUnavailable(Exception):
    pass


class _Token:
    def __init__(self, manager, transition_id, generation):
        self.manager, self.id, self.generation = manager, transition_id, generation

    def check(self):
        with self.manager._lock:
            self.manager._check(self)

    def publish(self, mode):
        self.manager._publish(self, mode)

    def operation_done(self, name, failed=False):
        with connection(self.manager.path) as db:
            db.execute("UPDATE model_remote_operations SET status=? WHERE operation_name=? AND status='pending'",
                       ('failed' if failed else 'completed', name))

    def mutate(self, method, url, **kwargs):
        """Persist intent before I/O; timeout without an operation name stays uncertain."""
        identifier = 'mutation_' + uuid.uuid4().hex
        with self.manager._lock, connection(self.manager.path) as db:
            self.manager._check(self)
            db.execute('INSERT INTO model_remote_operations VALUES (?,?,?,?,?,?)',
                       (identifier, self.id, None, 'unknown', method, time.time()))
            self.manager.journal.event(self.manager._cycle(self.id), 'remote_mutation_submitted',
                                       data={'operation': method}, conn=db)
        # Cancellation after submission cannot undo provider I/O, only fence publication.
        response = getattr(requests, method)(url, **kwargs)
        try:
            operation = response.json().get('name') if response.ok else None
        except (ValueError, AttributeError):
            operation = None
        definitive_failure = 400 <= response.status_code < 500
        with connection(self.manager.path) as db:
            db.execute('UPDATE model_remote_operations SET operation_name=?,status=? WHERE id=?',
                       (operation, 'pending' if operation else ('failed' if definitive_failure else 'unknown'), identifier))
            self.manager.journal.event(self.manager._cycle(self.id), 'remote_mutation_receipt',
                                       data={'operation': operation or method, 'http_status': response.status_code}, conn=db)
        self.check()
        return response


class TransitionManager:
    """A single VM owner; requests/cancel are fast even while provider calls block.

    control.sqlite3 owns generation AND effective mode in one transaction. The old
    usage database is a compatibility mirror and is never read as routing authority.
    Restart records uncertainty and never replays a mutation or activates a candidate.
    """
    def __init__(self, controller, probe_nano, journal=None):
        self.controller, self.probe_nano = controller, probe_nano
        self.journal = journal or controller.journal
        self.path = self.journal.path
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='model-transition')
        self._closed = False
        initial = controller.get_active_mode()
        migrate(self.path, [[
            'CREATE TABLE IF NOT EXISTS model_transition_control(id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL, effective_mode TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS model_transitions(id TEXT PRIMARY KEY, generation INTEGER NOT NULL UNIQUE, requested_mode TEXT NOT NULL, previous_mode TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, error_type TEXT)',
            'CREATE TABLE IF NOT EXISTS model_remote_operations(id TEXT PRIMARY KEY, transition_id TEXT NOT NULL REFERENCES model_transitions(id), operation_name TEXT, status TEXT NOT NULL, method TEXT NOT NULL, created_at REAL NOT NULL)',
        ]], component='cancelable-model-transitions')
        with connection(self.path) as db:
            db.execute('INSERT OR IGNORE INTO model_transition_control VALUES (1,0,?)', (initial,))
            interrupted = db.execute("SELECT id FROM model_transitions WHERE status='active'").fetchall()
            for row in interrupted:
                db.execute("UPDATE model_transitions SET status='unknown',phase='process_interrupted',updated_at=? WHERE id=?", (time.time(), row['id']))
                self.journal.finish(self._cycle(row['id']), 'unknown', phase='process_interrupted',
                                    data={'reason_code': 'restart_no_replay'}, conn=db)
            if interrupted:
                db.execute('UPDATE model_transition_control SET generation=generation+1 WHERE id=1')
        controller._transition_manager = self

    def _cycle(self, identifier):
        return self.journal.cycle_id('model_transition', identifier)

    def effective_mode(self):
        with connection(self.path) as db:
            return db.execute('SELECT effective_mode FROM model_transition_control WHERE id=1').fetchone()[0]

    def publish_nano_fallback(self, expected_mode, expected_generation=None):
        """Publish a successfully served Nano call, without mutating any VM.

        Callers should capture snapshot()['generation'] before inference. A late
        response cannot override an intervening transition, even to the same mode.
        """
        with self._lock, connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT generation,effective_mode FROM model_transition_control WHERE id=1').fetchone()
            if (self._closed or row['effective_mode'] != expected_mode
                    or expected_generation is not None and row['generation'] != expected_generation
                    or db.execute("SELECT 1 FROM model_transitions WHERE status='active'").fetchone()):
                return False
            cycle = self.journal.start('model_fallback', uuid.uuid4().hex,
                                       refs={'effective_mode': expected_mode}, conn=db)
            db.execute("UPDATE model_transition_control SET effective_mode='nano',generation=generation+1 WHERE id=1")
            self.journal.finish(cycle, data={'effective_mode': 'nano', 'reason_code': 'nano_inference_verified'}, conn=db)
        self.controller._mirror_mode_state('nano', mark_notified=False)
        return True

    def _check(self, token):
        if self._closed:
            raise TransitionCancelled()
        with connection(self.path) as db:
            row = db.execute('SELECT generation FROM model_transition_control WHERE id=1').fetchone()
            transition = db.execute('SELECT status FROM model_transitions WHERE id=?', (token.id,)).fetchone()
        if row[0] != token.generation or not transition or transition[0] != 'active':
            raise TransitionCancelled()

    def _cancel_rows(self, db, reason):
        rows = db.execute("SELECT id FROM model_transitions WHERE status='active'").fetchall()
        for row in rows:
            db.execute("UPDATE model_transitions SET status='cancelled',phase=?,updated_at=? WHERE id=?", (reason, time.time(), row['id']))
            self.journal.finish(self._cycle(row['id']), 'cancelled', phase=reason,
                                data={'reason_code': reason}, conn=db)
        return [r['id'] for r in rows]

    def request(self, mode, notifier=None):
        mode = str(mode or '').strip().lower()
        mode = {'h': 'high', 'm': 'mid', 'l': 'low', 'medium': 'mid', 'n': 'nano', 'off': 'nano'}.get(mode, mode)
        if mode not in {*PROFILES, 'nano'}:
            raise ValueError('Modo invalido: nano, low, mid o high')
        with self._lock:
            if self._closed:
                raise RuntimeError('Transition manager is closed')
            identifier, now = 'transition_' + uuid.uuid4().hex, time.time()
            with connection(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                self._cancel_rows(db, 'superseded')
                row = db.execute('SELECT generation,effective_mode FROM model_transition_control WHERE id=1').fetchone()
                generation, previous = row[0] + 1, row[1]
                db.execute('UPDATE model_transition_control SET generation=? WHERE id=1', (generation,))
                db.execute('INSERT INTO model_transitions VALUES (?,?,?,?,?,?,?,?,NULL)',
                           (identifier, generation, mode, previous, 'active', 'requested', now, now))
                self.journal.start('model_transition', identifier, refs={'profile': mode, 'effective_mode': previous}, conn=db)
            token = _Token(self, identifier, generation)
            context = copy_context()
            self._executor.submit(context.run, self._run, token, mode, previous, notifier)
            return {'id': identifier, 'generation': generation, 'status': 'active', 'requested_mode': mode,
                    'effective_mode': previous}

    def cancel(self, notifier=None):
        with self._lock, connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            identifiers = self._cancel_rows(db, 'cancel_requested')
            db.execute('UPDATE model_transition_control SET generation=generation+1 WHERE id=1')
            effective = db.execute('SELECT effective_mode FROM model_transition_control WHERE id=1').fetchone()[0]
        if notifier and identifiers:
            notifier(f'[Cambio cancelado; modo {effective} conservado]')
        return {'status': 'cancelled' if identifiers else 'idle', 'ids': identifiers, 'effective_mode': effective}

    def _phase(self, token, phase):
        with self._lock:
            self._check(token)
            with connection(self.path) as db:
                db.execute('UPDATE model_transitions SET phase=?,updated_at=? WHERE id=?', (phase, time.time(), token.id))
                self.journal.event(self._cycle(token.id), phase, conn=db)

    def _publish(self, token, mode):
        with self._lock:
            self._check(token)
            with connection(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                changed = db.execute('UPDATE model_transition_control SET effective_mode=? WHERE id=1 AND generation=?', (mode, token.generation)).rowcount
                if not changed:
                    raise TransitionCancelled()
                self.journal.event(self._cycle(token.id), 'effective_mode_published', data={'effective_mode': mode}, conn=db)

    def _notify(self, token, notifier, message):
        with self._lock:
            self._check(token)
            if notifier:
                notifier(message)

    def _reconcile(self, token):
        with connection(self.path) as db:
            pending = db.execute("SELECT * FROM model_remote_operations WHERE status IN ('unknown','pending') ORDER BY created_at").fetchall()
        for row in pending:
            token.check()
            name = row['operation_name']
            if not name:
                raise RemoteOutcomeUnknown('Mutation receipt missing; manual reconciliation required')
            # Read only. Never repeat an uncertain request or delete a resource by its name.
            response = requests.get(self.controller._gce_url(f'operations/{quote(name)}'), headers=self.controller._headers(), timeout=15)
            response.raise_for_status()
            data = response.json()
            if data.get('status') != 'DONE':
                raise RemoteOutcomeUnknown('Remote operation is still pending')
            token.operation_done(name, bool(data.get('error')))
            self.journal.event(self._cycle(token.id), 'prior_remote_operation_reconciled', data={'operation': name})

    def _run(self, token, mode, previous, notifier):
        context_token = transition_token.set(token)
        try:
            with self.journal.activate(self._cycle(token.id)):
                token.check()
                self._phase(token, 'reconciling')
                self._reconcile(token)
                self._notify(token, notifier, f'[Conectando con Modo {mode}]')
                same_live_profile = False
                instance, instance_observed = None, False
                if mode == previous and mode != 'nano':
                    instance = self.controller.get_instance()
                    instance_observed = True
                    token.check()
                    same_live_profile = bool(instance and instance.get('status') == 'RUNNING'
                                             and instance.get('machineType', '').rsplit('/', 1)[-1] == PROFILES[mode]['machine_type'])
                if not same_live_profile:
                    self._phase(token, 'probing_nano')
                    nano_ready = False
                    try:
                        if self.probe_nano() is not True:
                            raise NanoUnavailable()
                        nano_ready = True
                    except Exception as exc:
                        # An explicit local selection may start when there is no
                        # running service to destroy. This is not provider failover.
                        try:
                            if mode != 'nano' and not instance_observed:
                                instance = self.controller.get_instance()
                                instance_observed = True
                        except Exception:
                            instance_observed = False
                        no_service_to_destroy = (instance_observed and
                                                 (instance is None or instance.get('status') == 'TERMINATED'))
                        if mode == 'nano' or not no_service_to_destroy:
                            raise NanoUnavailable() from exc
                        self._phase(token, 'nano_unavailable_owner_selected_profile')
                    token.check()
                    if nano_ready:
                        self._publish(token, 'nano')
                        self.controller._mirror_mode_state('nano', mark_notified=True)
                        self._phase(token, 'nano_bridge_ready')
                self._phase(token, 'preparing_target')
                if mode == 'nano':
                    self.controller.stop()
                else:
                    self.controller.start(mode)  # Exactly the requested profile; no cascade.
                token.check()
                if self.effective_mode() != mode:
                    raise RuntimeError('Target was not published after readiness')
                with self._lock:
                    self._check(token)
                    if notifier:
                        notifier(f'[Modo {mode} conectado]')
                    with connection(self.path) as db:
                        db.execute("UPDATE model_transitions SET status='completed',phase='connected',updated_at=? WHERE id=?", (time.time(), token.id))
                        self.journal.finish(self._cycle(token.id), data={'effective_mode': mode}, conn=db)
        except TransitionCancelled:
            pass  # request/cancel already closed this branch; late evidence stays attached.
        except Exception as exc:
            with self._lock:
                try:
                    self._check(token)
                except TransitionCancelled:
                    return
                state = 'unknown' if isinstance(exc, RemoteOutcomeUnknown) else 'failed'
                with connection(self.path) as db:
                    db.execute('UPDATE model_transitions SET status=?,phase=?,error_type=?,updated_at=? WHERE id=?',
                               (state, 'needs_decision', type(exc).__name__, time.time(), token.id))
                    self.journal.finish(self._cycle(token.id), state, data={'error_type': type(exc).__name__, 'effective_mode': self.effective_mode()}, conn=db)
                if notifier:
                    reason = 'Nano no responde' if isinstance(exc, NanoUnavailable) else type(exc).__name__
                    notifier(f'[Modo {mode} fallo: {reason}; modo {self.effective_mode()} conservado. Elige el modo al que deseas cambiar. ID: {token.id}]')
        finally:
            transition_token.reset(context_token)

    def is_active(self):
        with connection(self.path) as db:
            return bool(db.execute("SELECT 1 FROM model_transitions WHERE status='active'").fetchone())

    def snapshot(self):
        with connection(self.path) as db:
            generation = db.execute('SELECT generation FROM model_transition_control WHERE id=1').fetchone()[0]
            rows = db.execute('SELECT * FROM model_transitions ORDER BY generation DESC LIMIT 20').fetchall()
            operations = db.execute("SELECT * FROM model_remote_operations WHERE status IN ('unknown','pending') ORDER BY created_at").fetchall()
        return {'effective_mode': self.effective_mode(), 'generation': generation, 'active': self.is_active(),
                'transitions': [dict(row) for row in rows], 'pending_operations': [dict(row) for row in operations]}

    def shutdown(self, wait=False):
        self.cancel()
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=True)
