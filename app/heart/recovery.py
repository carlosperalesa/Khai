"""Heart owns health cases: evidence (via Senses), diagnosis, plan, authorization check,
hand-off to Hand, post-repair verification (via Senses) and the durable record of all of it.

Heart never writes skills and never calls Docker. It only produces declarative plans.
"""
import json
import logging
import re
import time
import uuid
from app.kernel.database import connection, migrate
from app.heart.catalog import (
    plan_hash, requires_approval, validate_plan, validate_case_id,
    CAUSE_TAXONOMY, cause_fingerprint
)
from app.kernel.tracing import parse_log_errors, BOOT_TIME

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
    'AUTHORIZED': {'HAND_CREATING', 'HAND_EXECUTING', 'FAILED', 'DENIED'},
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
        ], [
            'ALTER TABLE recovery_cases ADD COLUMN execution_order_id TEXT',
        ], [
            'ALTER TABLE recovery_cases ADD COLUMN incident_key TEXT',
            'ALTER TABLE recovery_cases ADD COLUMN cause_code TEXT',
            'ALTER TABLE recovery_cases ADD COLUMN task_id TEXT',
            'ALTER TABLE recovery_cases ADD COLUMN run_id TEXT',
            'CREATE INDEX IF NOT EXISTS idx_recovery_cases_incident ON recovery_cases(incident_key, state)',
            'CREATE INDEX IF NOT EXISTS idx_recovery_cases_task ON recovery_cases(task_id, state)',
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
            return ['storage', 'core_logs', 'pixy']
        if any(term in q for term in ('proxy', 'docker', 'contenedor', 'container')):
            return ['container_state_proxy', 'container_state_core', 'core_logs', 'pixy']
        if any(term in q for term in ('db', 'database', 'sqlite', 'tabla', 'integridad')):
            return ['storage', 'pixy']
        # Basal diagnostic profile determined by Heart
        return ['storage', 'container_state_core', 'container_state_proxy', 'core_logs', 'pixy']

    # ------------------------------------------------------------- case intake
    # ------------------------------------------------------------- case intake
    @staticmethod
    def classify_cause(failure_info, evidence=None):
        """Clasifica un fallo de tarea o infraestructura según la taxonomía determinista de Khai."""
        if isinstance(failure_info, dict):
            err_str = failure_info.get('error') or failure_info.get('stderr') or failure_info.get('feedback') or ''
            status = failure_info.get('status') or ''
            returncode = failure_info.get('returncode')
            details = f"{status} {err_str} {failure_info.get('stdout', '')}"
        else:
            err_str = str(failure_info or '')
            status = ''
            returncode = None
            details = err_str

        low = details.lower()

        # 1. Presupuesto agotado
        if any(term in low for term in ('presupuesto agotado', 'budget exceeded', 'budgetexceeded')):
            return 'budget_exhausted', err_str or 'Presupuesto de tarea agotado'

        # 2. Credencial ausente / rechazada
        if any(term in low for term in ('401 unauthorized', '403 forbidden', 'api key missing', 'missing secret', 'credencial faltante', 'api_key')):
            return 'missing_credential', err_str or 'Credencial o clave de acceso ausente o rechazada'

        # 3. Efecto incierto
        if status == 'unknown' or 'resultado incierto' in low or 'uncertain' in low:
            return 'uncertain_effect', err_str or 'Efecto o estado de ejecución incierto; no se repite a ciegas'

        # 4. Capacidad o servicio faltante
        if any(term in low for term in ('capabilityrejected', 'missing capability', 'capacidad ausente', 'servicio no registrado', 'unregistered service')):
            return 'missing_capability', err_str or 'Capacidad o servicio no registrado en la política de Voice/Runtime'

        # 5. Dependencia ausente
        if any(term in low for term in ('modulenotfounderror', 'importerror', 'no module named')):
            return 'dependency_error', err_str or 'Dependencia o módulo no disponible en el runtime'

        # 6. Sintaxis o excepción en código de skill
        if any(term in low for term in ('syntaxerror', 'indentationerror', 'nameerror', 'typeerror', 'attributeerror', 'zerodivisionerror', 'indexerror', 'keyerror')) or 'traceback (most recent call last)' in low or (returncode is not None and returncode != 0):
            return 'syntax_or_code', err_str or f'Error de sintaxis o excepción durante la ejecución (código {returncode})'

        # 7. Proveedor temporal
        if any(term in low for term in ('429 too many requests', '502 bad gateway', '503 service unavailable', '504 gateway timeout', 'retry-after', 'timeout', 'timed out', 'connection reset')):
            return 'temporary_provider', err_str or 'Error temporal de red o proveedor (timeout/rate-limit)'

        # 8. Verificador inválido
        if isinstance(failure_info, dict) and failure_info.get('verification', {}).get('success') is False:
            return 'invalid_result', err_str or 'El resultado de la tarea no satisfizo el verificador objetivo'

        return 'syntax_or_code', err_str or 'Fallo no especificado durante la ejecución'

    def open_incident_case(self, task, failure_info, requested_by='system'):
        """Heart abre o deduplica un caso de incidente ante un fallo tipado, evitando bucles."""
        task_id = task.get('id') if isinstance(task, dict) else str(task)
        run_id = task.get('execution_id') if isinstance(task, dict) else None
        cause_code, detail = self.classify_cause(failure_info)
        fprint = cause_fingerprint(cause_code, detail)
        incident_key = f"{task_id}:{run_id or 'none'}:{fprint}"

        # Deduplicación: no abrir caso redundante si ya existe uno activo para este incidente exacto
        with connection(self.path) as conn:
            row = conn.execute(
                'SELECT case_id FROM recovery_cases WHERE incident_key=? AND state NOT IN ("RECOVERED", "FAILED", "DENIED", "UNKNOWN") ORDER BY created_at DESC LIMIT 1',
                (incident_key,)
            ).fetchone()
            if row:
                return self.get(row['case_id'])

        case_id = 'case-' + time.strftime('%Y%m%d') + uuid.uuid4().hex[:8]
        now = time.time()
        question = f"Incidente en tarea {task_id}: {cause_code} ({detail[:150]})"

        with connection(self.path) as conn:
            conn.execute('''INSERT INTO recovery_cases(case_id,question,requested_by,state,created_at,updated_at,
                                                      incident_key,cause_code,task_id,run_id)
                            VALUES (?,?,?,?,?,?,?,?,?,?)''',
                         (case_id, question[:2000], str(requested_by), 'RECEIVED', now, now,
                          incident_key, cause_code, task_id, run_id))
            conn.execute('INSERT INTO recovery_transitions(case_id,from_state,to_state,detail,created_at) VALUES (?,?,?,?,?)',
                         (case_id, None, 'RECEIVED', json.dumps({'requested_by': str(requested_by), 'incident_key': incident_key, 'cause': cause_code}), now))

        self._move(case_id, 'COLLECTING_EVIDENCE', expected={'RECEIVED'})
        try:
            needed = self.select_evidence_resources(question)
            evidence = self.senses.evidence('Heart ' + case_id + ': ' + str(question)[:200], 'before', resources=needed)
        except Exception as exc:
            return self._move(case_id, 'FAILED', error='Senses no entregó evidencia: ' + type(exc).__name__)

        diagnosis = self.diagnose(evidence, failure_override={'cause_code': cause_code, 'detail': detail, 'task_id': task_id})
        case = self._move(case_id, 'DIAGNOSED', expected={'COLLECTING_EVIDENCE'}, evidence=evidence, diagnosis=diagnosis)
        plan = self.design(case_id, diagnosis)
        if not plan:
            return case
        return self._move(case_id, 'SOLUTION_PROPOSED', expected={'DIAGNOSED'}, plan=plan,
                          plan_hash=plan_hash(case_id, plan))

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
    def diagnose(evidence, failure_override=None):
        findings = []
        def add(code, severity, detail, remedy=None):
            findings.append({'code': code, 'severity': severity, 'detail': detail, 'remedy': remedy})

        if failure_override:
            cause_code = failure_override.get('cause_code', 'syntax_or_code')
            detail = failure_override.get('detail', '')
            tid = failure_override.get('task_id')
            action = 'retry_task' if cause_code == 'budget_exhausted' else 'remediate_task'
            remedy = f"{action}:{tid}" if tid else None
            add(cause_code, 'high', detail, remedy=remedy)

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
        boot_epoch = evidence.get('boot_time') if 'boot_time' in evidence else BOOT_TIME
        parsed = parse_log_errors(text, boot_time=boot_epoch)
        active_errors = [e for e in parsed if e['is_active']]
        raw_errors = [line for line in text.splitlines() if LOG_ERRORS.search(line)]
        
        # Considerar ráfaga si hay 5 o más errores activos en la sesión actual
        count = len(active_errors) if parsed else len(raw_errors)
        if count >= 5:
            last_msg = active_errors[-1]['summary'] if active_errors else (raw_errors[-1][-300:] if raw_errors else '')
            add('core_error_burst', 'warning', f'{count} errores recientes en core_logs. Último: {last_msg}')

        # Observar fallos en tareas recientes (ControlStore vía Pixy) si no hay override
        if not failure_override:
            pixy_data = evidence.get('pixy') or {}
            tasks = pixy_data.get('recent_tasks') or evidence.get('tasks') or []
            failed_tasks = [t for t in tasks if t.get('state') in {'EXHAUSTED', 'FAILED'}]
            if failed_tasks:
                # Tomar la tarea fallida más reciente (A07: nunca tomar la más vieja)
                latest = sorted(failed_tasks, key=lambda t: t.get('updated_at', 0), reverse=True)[0]
                err = latest.get('error') or latest.get('feedback') or 'Fallo no especificado'
                add('task_failure', 'high',
                    f"La tarea {latest['id']} ('{latest.get('request', '')[:80]}') terminó con estado {latest['state']}: {str(err)[:300]}.",
                    remedy=f"retry_task:{latest['id']}")

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

        # Si el fallo corresponde a una tarea detenida/fallida, formular plan de reactivación y remediación
        task_remedy = next((f for f in remedies if f['remedy'].startswith(('remediate_task:', 'retry_task:'))), None)
        if task_remedy:
            action, tid = task_remedy['remedy'].split(':', 1)
            steps = [{'action': action, 'target': 'task'}]
            plan = {'target': 'task', 'steps': steps, 'capabilities': ['TaskRemediate'], 'task_id': tid}
            validate_plan(plan)
            plan.update(reason=task_remedy['code'],
                        summary=f'Corregir o reintentar la tarea {tid} tras solventar el diagnóstico ({task_remedy["code"]}).',
                        risk='low', impact=f'La tarea {tid} se reactivará y generará una corrección con feedback causal.',
                        reversible=True, requires_approval=requires_approval(plan))
            return plan

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
                          authorization_nonce=authorization['nonce'],
                          execution_order_id=authorization.get('order_id'))

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
        plan_keys = ('target', 'steps', 'capabilities', 'task_id') if case['plan']['target'] == 'task' else ('target', 'steps', 'capabilities')
        return {'header': {'case_id': case['case_id'], 'issued_by': 'heart', 'authorization_id': case['authorization_id'],
                           'order_id': case.get('execution_order_id'),
                           'nonce': case['authorization_nonce'], 'plan_hash': case['plan_hash']},
                'plan': {k: case['plan'][k] for k in plan_keys}}

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
            if case['plan']['target'] == 'task':
                tid = case['plan'].get('task_id')
                if not tid and case.get('diagnosis'):
                    for f in case['diagnosis'].get('findings', []):
                        if f.get('remedy', '').startswith(('retry_task:', 'remediate_task:')):
                            tid = f['remedy'].split(':', 1)[1]
                            break
                coord = getattr(self, 'coordinator', None) or (getattr(self.hand, 'coordinator', None) if hasattr(self, 'hand') else None)
                if tid and coord:
                    try:
                        coord.retry_task(tid, authorization_id=case['authorization_id'])
                        execution = {'status': 'running', 'steps': [{'action': 'retry_task', 'target': 'task', 'status': 'running', 'task_id': tid}]}
                        return self._move(case_id, 'HAND_EXECUTING', expected={'AUTHORIZED'}, execution=execution)
                    except Exception as exc:
                        return self._move(case_id, 'FAILED', expected={'AUTHORIZED'}, error=f'Fallo al reintentar la tarea: {exc}')
                else:
                    return self._move(case_id, 'FAILED', expected={'AUTHORIZED'}, error='Coordinador o task_id no disponible para reactivar la tarea.')

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
            if case['plan']['target'] == 'task':
                tid = case['plan'].get('task_id')
                if not tid and case.get('diagnosis'):
                    for f in case['diagnosis'].get('findings', []):
                        if f.get('remedy', '').startswith(('retry_task:', 'remediate_task:')):
                            tid = f['remedy'].split(':', 1)[1]
                            break
                coord = getattr(self, 'coordinator', None) or (getattr(self.hand, 'coordinator', None) if hasattr(self, 'hand') else None)
                if not coord:
                    return self._move(case_id, 'FAILED', expected={'HAND_EXECUTING'}, error='Coordinador no disponible.')
                current_task = coord.store.get(tid)
                if not current_task:
                    return self._move(case_id, 'UNKNOWN', expected={'HAND_EXECUTING'}, error=f'Tarea {tid} no encontrada.')
                t_state = current_task.get('state')
                if t_state in {'COMPLETED', 'FAILED', 'EXHAUSTED', 'CANCELLED'}:
                    execution = case.get('execution') or {}
                    execution['status'] = 'completed' if t_state == 'COMPLETED' else 'failed'
                    execution['final_task_state'] = t_state
                    self._move(case_id, 'VERIFYING', expected={'HAND_EXECUTING'}, execution=execution,
                               detail={'task_state': t_state})
                    state = 'VERIFYING'
                else:
                    if time.time() - case['updated_at'] > HAND_TIMEOUT:
                        return self._move(case_id, 'UNKNOWN', expected={'HAND_EXECUTING'},
                                          error='La tarea no confirmó el resultado dentro del plazo.')
                    return case
            else:
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
        if target == 'task':
            tid = plan.get('task_id')
            if not tid and case.get('diagnosis'):
                for f in case['diagnosis'].get('findings', []):
                    if f.get('remedy', '').startswith(('retry_task:', 'remediate_task:')):
                        tid = f['remedy'].split(':', 1)[1]
                        break
            coord = getattr(self, 'coordinator', None) or (getattr(self.hand, 'coordinator', None) if hasattr(self, 'hand') else None)
            if not coord:
                return self._move(case_id, 'FAILED', expected={'VERIFYING'}, error='Coordinador no disponible para verificación.')
            current_task = coord.store.get(tid)
            if not current_task:
                return self._move(case_id, 'UNKNOWN', expected={'VERIFYING'}, error=f'Tarea {tid} no encontrada.')
            t_state = current_task.get('state')
            v = current_task.get('verification') or current_task.get('feedback') or {}
            v_success = v.get('success', True) if isinstance(v, dict) else True
            checks = {'task_state': t_state, 'verified_success': v_success, 'task_id': tid}
            if t_state == 'COMPLETED' and v_success:
                outcome, error = 'RECOVERED', None
            elif t_state == 'COMPLETED' and not v_success:
                outcome, error = 'FAILED', f'Tarea {tid} completada pero la verificación de éxito falló o fue omitida.'
            elif t_state in {'FAILED', 'EXHAUSTED', 'CANCELLED'}:
                outcome, error = 'FAILED', f'Tarea {tid} terminó en estado {t_state}.'
            else:
                outcome, error = 'UNKNOWN', f'Estado de tarea {t_state} indeterminado.'
            return self._move(case_id, outcome, expected={'VERIFYING'}, verification=checks,
                              error=error, detail={'checks': checks})

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
