"""Proxy-side recovery channel. Closed catalog, durable receipts, never replays an action.

Only two containers can be touched (TARGETS) and only two actions exist (ACTIONS). The proxy
already owns the Docker socket; this module narrows what Core may ask it to do with it.
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from app.common.recovery import ACTION_ID, ACTIONS, TARGETS


def docker_time(value):
    match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)', value or '')
    if not match:
        return None
    tz = timezone.utc if match[3] == 'Z' else datetime.strptime(match[3].replace(':', ''), '%z').tzinfo
    base = datetime.strptime(match[1], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=tz)
    return base.timestamp() + (float('0' + match[2]) if match[2] else 0.0)


class RecoveryEngine:
    def __init__(self, engine, delay=2.0, exit_process=None, spawn=None):
        self.engine, self.delay = engine, delay
        self.exit_process = exit_process or os._exit
        self.spawn = spawn or (lambda fn, *args: threading.Thread(target=fn, args=args, daemon=True).start())
        self.boot = time.time()
        with engine.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS recovery_actions(id TEXT PRIMARY KEY, action TEXT NOT NULL,
                            target TEXT NOT NULL, receipt TEXT NOT NULL, created_at REAL NOT NULL)''')
        self.reconcile()

    def _save(self, action_id, **receipt):
        with self.engine.connect() as conn:
            row = conn.execute('SELECT receipt FROM recovery_actions WHERE id=?', (action_id,)).fetchone()
            current = json.loads(row['receipt'])
            current.update(receipt)
            conn.execute('UPDATE recovery_actions SET receipt=? WHERE id=?', (json.dumps(current), action_id))
        return current

    def get(self, action_id):
        with self.engine.connect() as conn:
            row = conn.execute('SELECT receipt FROM recovery_actions WHERE id=?', (action_id,)).fetchone()
        if not row:
            raise KeyError(action_id)
        return json.loads(row['receipt'])

    def state(self, target):
        if target not in TARGETS:
            raise ValueError('Objetivo no permitido')
        container = TARGETS[target]
        item = self.engine.inspect(container)
        result = {'target': target, 'container': container, 'observed_at': time.time()}
        if item is None:
            return dict(result, status='absent', running=False)
        state = item.get('State', {})
        result.update(status=state.get('Status'), running=bool(state.get('Running')),
                      started_at=docker_time(state.get('StartedAt')), exit_code=state.get('ExitCode'),
                      oom_killed=bool(state.get('OOMKilled')), restart_count=item.get('RestartCount'),
                      health=(state.get('Health') or {}).get('Status'))
        if target == 'proxy':
            last = getattr(self.engine, 'last_tick', None)
            result['scheduler_age'] = None if last is None else max(0.0, time.time() - last)
        return result

    def submit(self, action_id, action, target):
        if not isinstance(action_id, str) or not ACTION_ID.fullmatch(action_id):
            raise ValueError('Identificador de acción inválido')
        if action not in ACTIONS or target not in TARGETS:
            raise ValueError('Acción u objetivo fuera del catálogo')
        with self.engine.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM recovery_actions WHERE id=?', (action_id,)).fetchone()
            if row:
                if (row['action'], row['target']) != (action, target):
                    raise ValueError('Identificador reutilizado con otro contenido')
                return json.loads(row['receipt'])
            now = time.time()
            receipt = {'action_id': action_id, 'action': action, 'target': target, 'status': 'scheduled',
                       'scheduled_at': now}
            conn.execute('INSERT INTO recovery_actions VALUES (?,?,?,?,?)',
                         (action_id, action, target, json.dumps(receipt), now))
        if action == 'health_check':
            try:
                return self._save(action_id, status='completed', state=self.state(target), checked_at=time.time())
            except Exception as exc:
                return self._save(action_id, status='failed', error=type(exc).__name__)
        # Respond first; the restart runs outside the request so Core can persist the receipt.
        self.spawn(self._restart, action_id)
        return receipt

    def _restart(self, action_id):
        time.sleep(self.delay)
        receipt = self.get(action_id)
        if receipt['status'] != 'scheduled':
            return
        target = receipt['target']
        # Durable intent BEFORE the side effect; reconcile() decides the outcome after a crash.
        self._save(action_id, status='restarting', requested_at=time.time())
        if target == 'proxy':
            # A process cannot reliably wait for its own restart: exit and let the
            # compose restart policy start a fresh proxy. reconcile() confirms it.
            self.exit_process(75)
            return
        try:
            result = self.engine.docker(['restart', '--time', '10', TARGETS[target]], timeout=90)
        except Exception as exc:
            self._save(action_id, status='unknown', error=type(exc).__name__)
            return
        if result.returncode:
            self._save(action_id, status='failed', returncode=result.returncode, stderr=result.stderr[-2000:])
        else:
            self._save(action_id, status='completed', completed_at=time.time(), state=self.state(target))

    def reconcile(self):
        with self.engine.connect() as conn:
            rows = [dict(r) for r in conn.execute('SELECT * FROM recovery_actions')]
        for row in rows:
            receipt = json.loads(row['receipt'])
            if receipt['status'] == 'scheduled':
                # Never run a stale restart after an unrelated proxy restart.
                self._save(row['id'], status='failed', error='Proxy reiniciado antes de ejecutar; no se ejecutó')
            elif receipt['status'] == 'restarting':
                requested = receipt.get('requested_at', 0)
                try:
                    if row['target'] == 'proxy':
                        restarted = self.boot >= requested
                        state = self.state('proxy')
                    else:
                        state = self.state(row['target'])
                        restarted = (state.get('started_at') or 0) >= requested and state.get('running')
                except Exception as exc:
                    self._save(row['id'], status='unknown', error=type(exc).__name__)
                    continue
                self._save(row['id'], status='completed' if restarted else 'unknown', completed_at=time.time(), state=state)
