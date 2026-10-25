"""Heart owns health cases: evidence (via Senses), diagnosis, plan, authorization check,
hand-off to Hand, post-repair verification (via Senses) and the durable record of all of it.

Heart never writes skills and never calls Docker. It only produces declarative plans.
"""
import json
import logging
import re
import time
import uuid
from app.common.database import connection, migrate
from app.common.recovery import plan_hash, requires_approval, validate_plan, validate_case_id

ACTIVE = ('AUTHORIZED', 'HAND_CREATING', 'HAND_EXECUTING', 'VERIFYING')
TERMINAL = {'RECOVERED', 'FAILED', 'DENIED', 'UNKNOWN', 'DIAGNOSED'}
STATES = {'RECEIVED', 'COLLECTING_EVIDENCE', 'DIAGNOSED', 'SOLUTION_PROPOSED', 'WAITING_USER_APPROVAL',
          'AUTHORIZED', 'HAND_CREATING', 'HAND_EXECUTING', 'VERIFYING', 'RECOVERED', 'FAILED', 'DENIED', 'UNKNOWN'}
TRANSITIONS = {
    'RECEIVED': {'COLLECTING_EVIDENCE', 'FAILED'},
    'COLLECTING_EVIDENCE': {'DIAGNOSED', 'FAILED'},
    'DIAGNOSED': {'SOLUTION_PROPOSED'},
    'SOLUTION_PROPOSED': {'WAITING_USER_APPROVAL', 'AUTHORIZED', 'DENIED'},
    'WAITING_USER_APPROVAL': {'AUTHORIZED', 'DENIED'},
    'AUTHORIZED': {'HAND_CREATING', 'FAILED', 'DENIED'},
    'HAND_CREATING': {'HAND_EXECUTING', 'FAILED'},
    'HAND_EXECUTING': {'VERIFYING', 'FAILED', 'UNKNOWN'},
    'VERIFYING': {'RECOVERED', 'FAILED', 'UNKNOWN'},
}
HAND_TIMEOUT = 600
LOG_ERRORS = re.compile(r'\b(ERROR|CRITICAL)\b|Traceback \(most recent call last\)')
logger = logging.getLogger(__name__)


class Recovery:
    def __init__(self, path, senses, hand, authorizations, events=None):
        self.path, self.senses, self.hand = path, senses, hand
        self.authorizations, self.events = authorizations, events
        migrate(path, [[
            '''CREATE TABLE IF NOT EXISTS recovery_cases(case_id TEXT PRIMARY KEY, question TEXT NOT NULL,
               requested_by TEXT NOT NULL, state TEXT NOT NULL, evidence TEXT, diagnosis TEXT, plan TEXT,
               plan_hash TEXT, authorization_id TEXT, authorization_nonce TEXT, skill TEXT, execution TEXT,
               evidence_after TEXT, verification TEXT, error TEXT, created_at REAL NOT NULL,
               updated_at REAL NOT NULL, reported_at REAL)''',
            '''CREATE TABLE IF NOT EXISTS recovery_transitions(id INTEGER PRIMARY KEY AUTOINCREMENT,
               case_id TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, detail TEXT, created_at REAL NOT NULL)''',
            '''CREATE TRIGGER IF NOT EXISTS recovery_transitions_no_delete BEFORE DELETE ON recovery_transitions
               BEGIN SELECT RAISE(ABORT,'recovery history is immutable'); END''',
            '''CREATE TRIGGER IF NOT EXISTS recovery_transitions_no_update BEFORE UPDATE ON recovery_transitions
               BEGIN SELECT RAISE(ABORT,'recovery history is immutable'); END''',
        ]], component='heart-recovery')

    # ------------------------------------------------------------------ storage
    _JSON = ('evidence', 'diagnosis', 'plan', 'skill', 'execution', 'evidence_after', 'verification')

    def get(self, case_id):
        with connection(self.path) as conn:
            row = conn.execute('SELECT * FROM recovery_cases WHERE case_id=?', (case_id,)).fetchone()
        if not row:
            raise KeyError(case_id)
        case = dict(row)
        for key in self._JSON:
            case[key] = json.loads(case[key]) if case[key] else None
        return case

    def recent(self, limit=10):
        with connection(self.path) as conn:
            ids = [r[0] for r in conn.execute('SELECT case_id FROM recovery_cases ORDER BY created_at DESC LIMIT ?', (limit,))]
        return [self.get(i) for i in ids]

    def transitions(self, case_id):
        with connection(self.path) as conn:
            return [dict(r) for r in conn.execute(
                'SELECT from_state,to_state,detail,created_at FROM recovery_transitions WHERE case_id=? ORDER BY id', (case_id,))]

    def _move(self, case_id, to_state, expected=None, detail=None, **fields):
        if to_state not in STATES:
            raise ValueError('Estado de caso inválido')
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT state FROM recovery_cases WHERE case_id=?', (case_id,)).fetchone()
            if not row:
                raise KeyError(case_id)
            current = row['state']
            if expected and current not in expected:
                raise ValueError('El caso cambió de estado; operación obsoleta')
            if current != to_state and to_state not in TRANSITIONS.get(current, set()):
                raise ValueError(f'Transición no permitida: {current} -> {to_state}')
            sets, values = ['state=?', 'updated_at=?'], [to_state, time.time()]
            for key, value in fields.items():
                sets.append(key + '=?')
                values.append(json.dumps(value, ensure_ascii=False) if key in self._JSON else value)
            conn.execute('UPDATE recovery_cases SET ' + ','.join(sets) + ' WHERE case_id=?', (*values, case_id))
            if current != to_state:
                conn.execute('INSERT INTO recovery_transitions(case_id,from_state,to_state,detail,created_at) VALUES (?,?,?,?,?)',
                             (case_id, current, to_state, json.dumps(detail or {}, ensure_ascii=False), time.time()))
        if self.events and current != to_state:
            self.events.emit('heart', 'recovery_state', correlation_id=case_id,
                             metadata={'from': current, 'to': to_state})
        return self.get(case_id)

    @staticmethod
    def select_evidence_resources(question):
        """Heart decides which telemetry resources to request from Senses based on the inquiry."""
        q = (question or '').lower()
        if any(term in q for term in ('memoria', 'ram', 'disco', 'disk', 'espacio')):
            return ['storage', 'core_logs']
        if any(term in q for term in ('proxy', 'docker', 'contenedor', 'container')):
            return ['container_state_proxy', 'container_state_core', 'core_logs']
        if any(term in q for term in ('db', 'database', 'sqlite', 'tabla', 'integridad')):
            return ['storage']
        # Basal diagnostic profile determined by Heart
        return ['storage', 'container_state_core', 'container_state_proxy', 'core_logs']

    # ------------------------------------------------------------- case intake
    def open_case(self, question, requested_by):
        case_id = 'case-' + time.strftime('%Y%m%d') + uuid.uuid4().hex[:8]
        now = time.time()
        with connection(self.path) as conn:
            conn.execute('''INSERT INTO recovery_cases(case_id,question,requested_by,state,created_at,updated_at)
                            VALUES (?,?,?,?,?,?)''', (case_id, str(question)[:2000], str(requested_by), 'RECEIVED', now, now))
            conn.execute('INSERT INTO recovery_transitions(case_id,from_state,to_state,detail,created_at) VALUES (?,?,?,?,?)',
                         (case_id, None, 'RECEIVED', json.dumps({'requested_by': str(requested_by)}), now))
        self._move(case_id, 'COLLECTING_EVIDENCE', expected={'RECEIVED'})
        try:
            needed = self.select_evidence_resources(question)
            evidence = self.senses.evidence('Heart ' + case_id + ': ' + str(question)[:200], 'before', resources=needed)
        except Exception as exc:
            return self._move(case_id, 'FAILED', error='Senses no entregó evidencia: ' + type(exc).__name__)
        diagnosis = self.diagnose(evidence)
        case = self._move(case_id, 'DIAGNOSED', expected={'COLLECTING_EVIDENCE'}, evidence=evidence, diagnosis=diagnosis)
        plan = self.design(case_id, diagnosis)
        if not plan:
            return case  # DIAGNOSED is terminal when no automated, safe plan exists.
        return self._move(case_id, 'SOLUTION_PROPOSED', expected={'DIAGNOSED'}, plan=plan,
                          plan_hash=plan_hash(case_id, plan))

    # --------------------------------------------------------------- diagnosis
    @staticmethod
    def diagnose(evidence):
        findings = []
        def add(code, severity, detail, remedy=None):
            findings.append({'code': code, 'severity': severity, 'detail': detail, 'remedy': remedy})
        storage = evidence.get('storage') or {}
        vitals, integrity = storage.get('vitals'), storage.get('integrity')
        if not isinstance(vitals, dict) or not isinstance(integrity, dict):
            add('storage_unobserved', 'warning', 'Senses no pudo leer signos vitales ni integridad SQLite.')
        else:
            if integrity.get('status') != 'ok':
                bad = {k: v for k, v in (integrity.get('databases') or {}).items() if v != 'ok'}
                add('sqlite_integrity', 'critical', 'PRAGMA quick_check falló: ' + json.dumps(bad, ensure_ascii=False)[:400])
            if isinstance(vitals.get('disk_free'), (int, float)) and vitals['disk_free'] < 1024 ** 3:
                add('disk_low', 'critical', f"Disco libre {vitals['disk_free'] // 1024 ** 2} MiB (< 1 GiB).")
            if isinstance(vitals.get('ram_percent'), (int, float)) and vitals['ram_percent'] >= 90:
                add('memory_pressure', 'high', f"RAM al {vitals['ram_percent']}%.", 'restart:core')
        proxy = evidence.get('container_proxy') or {}
        if proxy.get('status') == 'unavailable':
            add('proxy_unreachable', 'critical',
                'El proxy de ejecución no responde; es el único canal de reparación y no puede reiniciarse a sí mismo desde aquí.')
        else:
            if proxy.get('running') is False:
                add('proxy_not_running', 'critical', 'Docker informa el proxy detenido.')
            age = proxy.get('scheduler_age')
            if isinstance(age, (int, float)) and age > 60:
                add('proxy_scheduler_stalled', 'high', f'El planificador del proxy no avanza hace {int(age)} s.', 'restart:proxy')
        core = evidence.get('container_core') or {}
        if core.get('oom_killed'):
            add('core_oom_history', 'warning', 'El último cierre del Core fue por falta de memoria (OOMKilled).')
        text = (evidence.get('core_logs') or {}).get('text', '')
        errors = [line for line in text.splitlines() if LOG_ERRORS.search(line)]
        if len(errors) >= 5:
            add('core_error_burst', 'warning', f'{len(errors)} errores recientes en core_logs. Último: {errors[-1][-300:]}')
        status = 'critical' if any(f['severity'] == 'critical' for f in findings) else \
                 'degraded' if findings else 'ok'
        return {'status': status, 'findings': findings}

    @staticmethod
    def design(case_id, diagnosis):
        findings = diagnosis.get('findings', [])
        # Never restart over corruption, a full disk or a missing channel: that destroys evidence.
        if any(f['severity'] == 'critical' and not f['remedy'] for f in findings):
            return None
        remedies = [f for f in findings if f['remedy']]
        if not remedies:
            return None
        chosen = next((f for f in remedies if f['remedy'] == 'restart:proxy'), remedies[0])
        target = chosen['remedy'].split(':', 1)[1]
        steps = [{'action': 'restart_container', 'target': target}, {'action': 'health_check', 'target': target}]
        plan = {'target': target, 'steps': steps, 'capabilities': ['DockerRestart', 'HealthCheck']}
        validate_plan(plan)
        impact = ('El webhook y las tareas quedan interrumpidos ~10-30 s; las ejecuciones en curso se reconcilian y no se repiten.'
                  if target == 'core' else
                  'Las ejecuciones nuevas esperan ~5-15 s; las existentes se reconcilian por recibo y no se repiten.')
        plan.update(reason=chosen['code'], summary=f'Reiniciar el contenedor {target} y comprobar su estado.',
                    risk='medium', impact=impact, reversible=True, requires_approval=requires_approval(plan))
        return plan

    # ----------------------------------------------------------- authorization
    def await_approval(self, case_id, authorization):
        case = self.get(case_id)
        self._check_terms(case, authorization)
        return self._move(case_id, 'WAITING_USER_APPROVAL', expected={'SOLUTION_PROPOSED'},
                          authorization_id=authorization['authorization_id'])

    def _check_terms(self, case, authorization):
        plan = case['plan'] or {}
        if (authorization['case_id'] != case['case_id'] or authorization['target'] != plan.get('target')
                or authorization['capabilities'] != sorted(plan.get('capabilities', []))
                or authorization['plan_hash'] != case['plan_hash']):
            raise PermissionError('La autorización no corresponde al plan de Heart')

    def receive_decision(self, case_id, authorization):
        """Pixy hands over the user's decision. Heart verifies it against its own plan."""
        case = self.get(case_id)
        if case['authorization_id'] and case['authorization_id'] != authorization['authorization_id']:
            raise PermissionError('Decisión para otra autorización')
        if authorization['status'] == 'denied':
            return self._move(case_id, 'DENIED', expected={'WAITING_USER_APPROVAL', 'SOLUTION_PROPOSED'},
                              detail={'decided_by': authorization.get('decided_by')},
                              authorization_id=authorization['authorization_id'])
        self._check_terms(case, authorization)
        self.authorizations.verify(authorization['authorization_id'], authorization['nonce'], case_id,
                                   case['plan']['capabilities'], case['plan']['target'], case['plan_hash'])
        return self._move(case_id, 'AUTHORIZED', expected={'WAITING_USER_APPROVAL', 'SOLUTION_PROPOSED'},
                          detail={'decided_by': authorization.get('decided_by')},
                          authorization_id=authorization['authorization_id'],
                          authorization_nonce=authorization['nonce'])

    def expire_waiting(self):
        """Close cases whose question expired or was superseded in Pixy's ledger."""
        with connection(self.path) as conn:
            rows = conn.execute('SELECT case_id,authorization_id FROM recovery_cases WHERE state="WAITING_USER_APPROVAL"').fetchall()
        for row in rows:
            record = self.authorizations.get(row['authorization_id'])
            if not record or record['status'] in {'expired', 'superseded', 'denied'}:
                self._move(row['case_id'], 'DENIED', expected={'WAITING_USER_APPROVAL'},
                           detail={'reason': (record or {}).get('status', 'missing')},
                           error='Autorización ' + (record or {}).get('status', 'inexistente') + '; no se ejecutó nada.')

    # ------------------------------------------------------------- hand-off
    def _order(self, case):
        return {'header': {'case_id': case['case_id'], 'issued_by': 'heart', 'authorization_id': case['authorization_id'],
                           'nonce': case['authorization_nonce'], 'plan_hash': case['plan_hash']},
                'plan': {k: case['plan'][k] for k in ('target', 'steps', 'capabilities')}}

    def advance(self, case_id):
        case = self.get(case_id)
        state = case['state']
        if state == 'AUTHORIZED':
            # Verificación explícita de la autorización de Pixy antes de ordenar a Hand
            if not case.get('authorization_id') or not case.get('authorization_nonce'):
                return self._move(case_id, 'FAILED', expected={'AUTHORIZED'},
                                  error='Pase a Hand denegado: falta la autorización emitida por Pixy.')
            try:
                self.authorizations.verify(case['authorization_id'], case['authorization_nonce'],
                                           case_id, case['plan']['capabilities'],
                                           case['plan']['target'], case['plan_hash'])
            except PermissionError as exc:
                return self._move(case_id, 'DENIED', expected={'AUTHORIZED'},
                                  error='Pase a Hand denegado: autorización de Pixy no válida: ' + str(exc))
            self._move(case_id, 'HAND_CREATING', expected={'AUTHORIZED'})
            state = 'HAND_CREATING'
        if state == 'HAND_CREATING':
            try:
                skill = self.hand.create_recovery(self._order(case))
            except (PermissionError, ValueError) as exc:
                return self._move(case_id, 'FAILED', expected={'HAND_CREATING'}, error='Hand rechazó la orden: ' + str(exc)[:500])
            self._move(case_id, 'HAND_EXECUTING', expected={'HAND_CREATING'}, skill=skill)
            state = 'HAND_EXECUTING'
        if state == 'HAND_EXECUTING':
            try:
                execution = self.hand.advance_recovery(self._order(case))
            except (PermissionError, ValueError) as exc:
                return self._move(case_id, 'FAILED', expected={'HAND_EXECUTING'}, error='Hand no ejecutó: ' + str(exc)[:500])
            if execution['status'] == 'running':
                if time.time() - case['updated_at'] > HAND_TIMEOUT:
                    return self._move(case_id, 'UNKNOWN', expected={'HAND_EXECUTING'}, execution=execution,
                                      error='Hand no confirmó el resultado dentro del plazo.')
                with connection(self.path) as conn:
                    conn.execute('UPDATE recovery_cases SET execution=? WHERE case_id=?',
                                 (json.dumps(execution, ensure_ascii=False), case_id))
                return self.get(case_id)
            self._move(case_id, 'VERIFYING', expected={'HAND_EXECUTING'}, execution=execution,
                       detail={'hand_status': execution['status']})
            state = 'VERIFYING'
        if state == 'VERIFYING':
            return self.verify(case_id)
        return case

    # ------------------------------------------------------------ verification
    def verify(self, case_id):
        case = self.get(case_id)
        plan, execution = case['plan'], case['execution'] or {}
        target = plan['target']
        # Heart determina exactamente los parámetros necesarios para verificar la reparación
        check_resources = ['container_state_' + target]
        if plan.get('reason') == 'memory_pressure':
            check_resources.append('storage')
        after = self.senses.evidence('Heart verificación ' + case_id, 'after', resources=check_resources)
        state = after.get('container_' + target) or {}
        restart = next((s for s in execution.get('steps', []) if s['action'] == 'restart_container'), {})
        requested = (restart.get('receipt') or {}).get('requested_at')
        checks = {'hand_status': execution.get('status'), 'container_observed': state.get('status') != 'unavailable',
                  'running': state.get('running'), 'started_at': state.get('started_at'), 'restart_requested_at': requested}
        restarted = (isinstance(requested, (int, float)) and isinstance(state.get('started_at'), (int, float))
                     and state['started_at'] >= requested)
        checks['restarted'] = restarted
        remaining = [f['code'] for f in self.diagnose(after)['findings']]
        checks['reason_persists'] = plan['reason'] in remaining
        checks['findings_after'] = remaining
        if not checks['container_observed'] or (execution.get('status') == 'unknown' and not restarted):
            outcome, error = 'UNKNOWN', 'No hay evidencia suficiente del estado posterior.'
        elif execution.get('status') == 'failed' and not restarted:
            outcome, error = 'FAILED', 'Hand informó fallo y Senses no observa el reinicio.'
        elif state.get('running') is not True:
            outcome, error = 'FAILED', 'El contenedor no está en ejecución tras la reparación.'
        elif not restarted:
            outcome, error = 'UNKNOWN', 'Senses no puede confirmar que el reinicio ocurrió.'
        elif checks['reason_persists']:
            outcome, error = 'FAILED', 'El reinicio ocurrió, pero el hallazgo original persiste: ' + plan['reason']
        else:
            outcome, error = 'RECOVERED', None
        return self._move(case_id, outcome, expected={'VERIFYING'}, evidence_after=after, verification=checks,
                          error=error, detail={'checks': checks})

    # -------------------------------------------------------------- scheduler
    def tick(self):
        """Advance active cases and return terminal cases not yet reported to Pixy."""
        self.expire_waiting()
        with connection(self.path) as conn:
            active = [r[0] for r in conn.execute(
                'SELECT case_id FROM recovery_cases WHERE state IN (%s) ORDER BY created_at' % ','.join('?' * len(ACTIVE)), ACTIVE)]
        for case_id in active:
            try:
                self.advance(case_id)
            except (ConnectionError, OSError, TimeoutError):
                continue  # Transport uncertainty: keep state and retry; never replay a side effect.
            except Exception:
                logger.exception('Heart no pudo avanzar el caso %s; se reintenta', case_id)
        with connection(self.path) as conn:
            ids = [r[0] for r in conn.execute(
                'SELECT case_id FROM recovery_cases WHERE reported_at IS NULL AND state IN ("RECOVERED","FAILED","UNKNOWN","DENIED") '
                'AND authorization_id IS NOT NULL ORDER BY created_at')]
        return [self.get(i) for i in ids]

    def mark_reported(self, case_id):
        validate_case_id(case_id)
        with connection(self.path) as conn:
            conn.execute('UPDATE recovery_cases SET reported_at=? WHERE case_id=? AND reported_at IS NULL', (time.time(), case_id))
