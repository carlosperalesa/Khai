import asyncio
import json
import logging
import secrets
import time
import uuid
from pathlib import Path
import requests

from app.pixy.budget import current_budget
from app.pixy.control import ControlStore, InstanceLock, BudgetExceeded, TERMINAL
from app.voice.runtime.contract import digest, manifest_from_request, proxy_url, proxy_headers, verify_result, skill_metadata
from app.dreams.legacy_learning import LearningLedger

logger = logging.getLogger(__name__)


from app.voice.runtime.docker_runtime import DockerRuntime as ProxyClient


class Coordinator:
    def __init__(self, manager, proxy=None):
        self.manager = manager
        self.store = ControlStore(manager.state.db_path,getattr(manager,'events',None))
        self.learning = getattr(manager, "dreams", None) or LearningLedger(self.store)
        self.proxy = proxy or manager.runtime
        self.root = Path(getattr(manager, "data_dir", Path(manager.state.db_path).parent))
        self.lock = InstanceLock(self.root / 'coordinator.lock')
        self.stopping = False

    def notify(self, text):
        try:
            for offset in range(0, min(len(text), 64000), 3900):
                self.manager.telegram.send_message(text[offset:offset + 3900], parse_mode=None)
        except Exception:
            logger.warning('No se pudo notificar; el estado durable permanece disponible')

    def intake(self, source, text):
        return self.store.create(source, text)

    def cancel(self, task_id, source):
        def apply(task):
            if task['state'] in {'FAILED', 'CANCELLED'} or (task['state'] == 'COMPLETED' and task.get('mode') != 'AGENT'):
                return
            task['state'] = 'CANCELLING' if task.get('execution_id') else 'CANCELLED'
        task = self.store.command(source, task_id, apply)
        if task and task['state'] == 'CANCELLING':
            try:
                self.proxy.cancel(task['execution_id'])
            except requests.RequestException:
                pass  # Durable cancellation is retried by tick.
        return task

    def approval(self, task_id, token, approve, source):
        def apply(task):
            pending = task.get('approval', {})
            if task['state'] != 'WAITING_APPROVAL' or not secrets.compare_digest(pending.get('token', ''), token):
                raise ValueError('AprobaciÃ³n desconocida u obsoleta')
            if time.time() > pending['expires'] or pending['artifact_hash'] != task['artifact_hash']:
                raise ValueError('AprobaciÃ³n vencida o artefacto cambiado')
            if digest(Path(task['artifact']).read_bytes()) != pending['artifact_hash']:
                raise ValueError('El archivo cambiÃ³ despuÃ©s de la revisiÃ³n')
            if not approve:
                task['state'] = 'CANCELLED'
                return
            task['authorized_hash'] = task['artifact_hash']
            if pending.get('operation'):
                task.setdefault('grants', []).append(pending['operation'])
            task['state'] = 'READY'
        return self.store.command(source, task_id, apply)

    def verify(self, task_id, execution_id, success, source):
        def apply(task):
            if task['state'] != 'UNVERIFIED' or task.get('execution_id') != execution_id:
                raise ValueError('La verificaciÃ³n no corresponde al resultado pendiente')
            task['verification'] = {'success': success, 'evidence': {'owner': source, 'execution_id': execution_id}}
            task['state'] = 'VERIFYING'
        return self.store.command(source, task_id, apply)

    def set_criterion(self, task_id, relative, sha, source):
        import re
        if not re.fullmatch('[0-9a-f]{64}', sha) or Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise ValueError('Usa una ruta relativa y SHA256 hexadecimal')
        def apply(task):
            if task['state'] not in {'ROUTING', 'GENERATING', 'REVIEWING', 'WAITING_APPROVAL', 'READY'}:
                raise ValueError('El criterio debe fijarse antes de ejecutar')
            task['criterion'] = {'kind': 'file_sha256', 'path': relative, 'sha256': sha}
        return self.store.command(source, task_id, apply)

    def _wait_approval(self, task, reason, operation=None):
        if operation:
            reason += "\nReanudar vuelve a iniciar el script: revisa checkpoints y efectos anteriores."
        approval = dict(token=secrets.token_hex(12), artifact_hash=task['artifact_hash'],
                        expires=min(task['deadline'], time.time() + 900), reason=reason, operation=operation)
        self.store.update(task['id'], expected={task['state']}, state='WAITING_APPROVAL', approval=approval)
        self.notify(f"Permiso requerido: {task['id']}\n{reason}\nSHA256: {task['artifact_hash']}\n"
                    f"Revisar: /artifact {task['id']}\n/approve {task['id']} {approval['token']}\n"
                    f"/deny {task['id']} {approval['token']}")

    def tick(self):
        self.store.flush_events()
        for task in self.store.all():
            if task['state'] in TERMINAL:
                if task.get('mode') == 'AGENT' and (task.get('result') or {}).get('status') == 'started':
                    try:
                        result = self.proxy.get(task['execution_id'])
                        if result.get('status') not in {'started', 'running'}:
                            self.store.event(task['id'], 'service_stopped', result)
                            self.store.update(task['id'], result=result)
                            self.notify(f"{task['id']}: el servicio cambiÃ³ a {result.get('status')}. La verificaciÃ³n previa describe el resultado observado en su momento.")
                    except requests.RequestException:
                        pass
                continue
            if task['state'] == 'WAITING_APPROVAL' and time.time() < task['deadline']:
                if time.time() > task['approval']['expires']:
                    self.store.update(task['id'], expected={'WAITING_APPROVAL'}, state='EXHAUSTED', error='AprobaciÃ³n vencida')
                continue
            token = current_budget.set(lambda: self.store.consume(task['id'], 'llm_calls'))
            try:
                if time.time() >= task['deadline'] and task['state'] != 'CANCELLING':
                    if task.get('execution_id'):
                        self.store.update(task['id'], state='CANCELLING', stop_reason='deadline')
                    else:
                        self.store.update(task['id'], state='EXHAUSTED')
                    continue
                self.advance(task)
            except BudgetExceeded as exc:
                self.store.update(task['id'], state='EXHAUSTED', error=str(exc))
                self.notify(f"{task['id']}: presupuesto agotado. No se crean tareas sucesoras.")
            except (requests.RequestException, ConnectionError) as exc:
                # Submission/poll uncertainty must retain the SAME execution ID.
                self.store.event(task['id'], 'transport_error', {'type': type(exc).__name__})
            except Exception as exc:
                current = self.store.get(task['id'])
                if current['state'] not in {'CANCELLED', 'CANCELLING'}:
                    self.store.update(task['id'], state='FAILED', error=str(exc)[:1000])
                    self.notify(f"{task['id']}: detenido: {str(exc)[:700]}")
                logger.exception('Error de coordinaciÃ³n')
            finally:
                current_budget.reset(token)
        self.store.flush_events()

    def advance(self, task):
        tid, state = task['id'], task['state']
        if self.store.get(tid)['state'] != state:
            return
        if state == 'ROUTING':
            request = task['request']
            direct = request.startswith(('/make ', '/do '))
            if direct:
                request = request.split(' ', 1)[1]
                intent = {'action': 'ACTION_REQUIRED', 'extracted_goal': request}
            else:
                intent = self.manager.core_mind.process_message(request)
            action = intent.get('action')
            if action == 'ACTION_REQUIRED':
                goal = intent.get('extracted_goal') or request
                manifest = manifest_from_request(goal)
                if not manifest:
                    manifest = json.loads(self.manager.llm.generate_content(
                        'Devuelve JSON con purpose (objetivo concreto) y capabilities '
                        '(lista de Network, FileSystem, Database segÃºn necesidad). '
                        'Esto declara intenciÃ³n, no autoriza permisos. Solicitud: ' + request,
                        response_format={'type': 'json_object'}))
                if not isinstance(manifest.get('purpose'), str) or not isinstance(manifest.get('capabilities'), list):
                    raise ValueError('Manifiesto invÃ¡lido')
                if hasattr(self.manager, 'hand') and not self.manager.pixy.preflight(manifest)['safe']:
                    raise PermissionError('Pixy rejected the objective')
                context = manifest['purpose'][:160]
                hypotheses = [h for h in self.manager.memory.get_active_hypotheses() if h['topic'] == context]
                hypotheses.sort(key=lambda h: h['confidence'], reverse=True)
                self.store.update(tid, expected={'ROUTING'}, state='GENERATING', request=goal,
                                  manifest=manifest, context=context,
                                  hypothesis_id=hypotheses[0]['id'] if hypotheses else None,
                                  alternatives=[h['id'] for h in hypotheses[1:3]], diagnosed=bool(hypotheses))
                self.notify(f"Objetivo registrado: {tid}. /tasks para consultar su estado.")
            elif action == 'INVESTIGATE':
                output = self.manager.core_eyes.observe(intent.get('investigate_target', 'core_logs'), intent.get('investigate_reason', 'Consulta'))
                self.store.event(tid, 'observation', {'output': output})
                self.store.update(tid, expected={'ROUTING'}, state='COMPLETED')
                self.notify(output)
            elif action in {'PS', 'LOGS'}:
                resource = 'running_containers' if action == 'PS' else intent.get('target', 'core_logs')
                output = self.manager.core_eyes.observe(resource, 'Consulta explÃ­cita del usuario')
                self.store.update(tid, expected={'ROUTING'}, state='COMPLETED')
                self.notify(output)
            elif action in {'HEALTH_CHECK', 'FASTBEAT', 'SLOWBEAT', 'CLEANUP'}:
                self.store.update(tid, expected={'ROUTING'}, state='COMPLETED')
                self.notify(json.dumps(self.manager.core_heart.check_integrity(), ensure_ascii=False))
            else:
                self.store.update(tid, expected={'ROUTING'}, state='COMPLETED')
                self.notify(intent.get('reply') or 'No se ejecutÃ³ ninguna acciÃ³n.')
        elif state == 'GENERATING':
            self.store.consume(tid, 'generations')
            reusable = next((t for t in reversed(self.store.all())
                             if t['id'] != tid and t['state'] == 'COMPLETED' and t.get('verification', {}).get('success')
                             and t['request'] == task['request'] and t.get('code') and not task.get('feedback')), None)
            tags = [task.get('context', 'general')] + task['manifest'].get('capabilities', [])
            if hasattr(self.manager, 'rules'):
                rules = [dict(r, id=r['rule_id']) for r in self.manager.rules.active()]
            else:
                with self.store.connection() as conn:
                    eligible = {r[0] for r in conn.execute('SELECT rule_id FROM rule_proposals')}
                rules = [r for r in self.manager.memory.get_guidelines_by_tags(tags) if r['id'] in eligible]
            hypothesis = self.manager.memory.get_hypothesis(task['hypothesis_id']) if task.get('hypothesis_id') else None
            try:
                if reusable:
                    code = reusable['code']
                    mode, deps = skill_metadata(code)
                else:
                    code, mode, deps = self.manager.forge.generate(task, rules, hypothesis)
            except (ValueError, SyntaxError) as exc:
                self.store.update(tid, expected={'GENERATING'}, feedback=str(exc))
                return
            artifact, sha = self.manager.forge.snapshot(code)
            if hasattr(self.manager, 'hand'):
                existing = self.manager.hand.active(reusable['skill_name']) if reusable and reusable.get('skill_name') else None
                if existing and existing['version_id'] == reusable.get('version_id'):
                    name, version = reusable['skill_name'],existing
                else:
                    name = task.get('skill_name') or 'skill-' + tid.removeprefix('task_')
                    if task.get('version_id'):
                        prior = self.manager.hand.version(task['version_id'])
                        if prior['status'] in {'draft', 'approved'}:
                            self.manager.hand.reject(prior['version_id'], 'Replaced after diagnosis')
                    version = self.manager.hand.create(name, code, task['manifest'], correlation_id=tid)
                self.store.update(tid, skill_name=name, version_id=version['version_id'])
            self.store.update(tid, expected={'GENERATING'}, state='REVIEWING', code=code, artifact=artifact,
                              artifact_hash=sha, mode=mode, dependencies=deps, grants=[], authorized_hash=None,
                              guidelines=[r['id'] for r in rules])
        elif state == 'REVIEWING':
            review = self.manager.pixy.review(task['code'], task['manifest'])
            self.store.event(tid, 'review', {'artifact_hash': task['artifact_hash'], 'decision': review})
            if not review['safe']:
                self.store.update(tid, expected={'REVIEWING'}, state='GENERATING', feedback=review.get('reason', 'Rechazado'))
            elif task['mode'] != 'SAFE' or task.get('dependencies') or 'Network' in task['manifest'].get('capabilities', []) or review['requires_approval']:
                self._wait_approval(task, review.get('reason', '') + '\nModo: ' + task['mode'] + '; capacidades: ' + ','.join(task['manifest'].get('capabilities', [])) + '; dependencias: ' + ','.join(task.get('dependencies', [])))
            else:
                self.store.update(tid, expected={'REVIEWING'}, state='READY', authorized_hash=task['artifact_hash'])
        elif state == 'READY':
            if task.get('authorized_hash') != task['artifact_hash'] or digest(Path(task['artifact']).read_bytes()) != task['artifact_hash']:
                raise ValueError('El artefacto no coincide con la revisiÃ³n/autorizaciÃ³n')
            if hasattr(self.manager, 'hand'):
                self.manager.hand.approve(task['version_id'], user_approved=bool(task.get('approval')), correlation_id=tid)
                self.manager.hand.use(task['skill_name'], tid)
            self.store.consume(tid, 'executions')
            execution_id = 'run_' + uuid.uuid4().hex
            if hasattr(self.manager,'hand'):
                self.manager.hand.prepare_execution(task['version_id'],execution_id,task['artifact_hash'])
            payload = dict(execution_id=execution_id, task_id=tid, artifact_hash=task['artifact_hash'],
                           mode=task['mode'], dependencies=task['dependencies'], request=task['request'],
                           network=False, skill_name=task.get('skill_name'), version_id=task.get('version_id'),
                           grants=task.get('grants', []), deadline=task['deadline'])
            if hasattr(self.manager,'hand'):
                payload['skill_artifact_hash'] = self.manager.hand.version(task['version_id'])['artifact_hash']
            self.store.update(tid, expected={'READY'}, state='SUBMITTED', execution_id=execution_id, payload=payload)
        elif state == 'SUBMITTED':
            result = self.proxy.submit(task['payload'])
            self.store.update(tid, expected={'SUBMITTED'}, state='RUNNING', result=result)
        elif state == 'RUNNING':
            if hasattr(self.manager, 'gateway'):
                self.manager.gateway.process_mailbox(task, self.root/'workspaces'/tid/task['artifact_hash'])
            result = self.proxy.get(task['execution_id'])
            status = result.get('status')
            if status in {'queued', 'starting', 'running'}:
                return
            if status == 'approval_required':
                self.store.update(tid, expected={'RUNNING'}, result=result)
                self._wait_approval(task, 'La habilidad solicita: ' + str(result.get('approval')), result.get('approval'))
            elif status == 'unknown' or (status in {'failed', 'timeout', 'cancelled'} and (task['mode'] != 'SAFE' or task.get('payload', {}).get('network') or 'Network' in task.get('manifest',{}).get('capabilities',[]))):
                self.store.update(tid, expected={'RUNNING'}, state='UNKNOWN', result=result)
                self.notify(f"{tid}: resultado incierto o con posibles efectos parciales. No se reejecutarÃ¡ automÃ¡ticamente.")
            elif status in {'failed', 'timeout', 'cancelled'} or result.get('returncode', 0) != 0:
                if result.get('evidence_kind') == 'process_exit':
                    self.learning.record(task, False, result)
                self.store.update(tid, expected={'RUNNING'}, state='DIAGNOSING', result=result, feedback=result)
                self.store.event(tid, 'execution_failure', result)
            elif status in {'finished', 'started'}:
                workspace = self.root / 'workspaces' / tid / task['artifact_hash']
                success, evidence = verify_result(result, task.get('criterion'), workspace)
                if success is None:
                    self.store.update(tid, expected={'RUNNING'}, state='UNVERIFIED', result=result)
                    self.notify(f"{tid}: {'servicio iniciado' if status == 'started' else 'proceso terminado'}, resultado sin verificar.\n"
                                f"{str(result.get('stdout', ''))[:1800]}\n"
                                f"/verify {tid} {task['execution_id']} success|failure\n/cancel {tid}")
                else:
                    self.store.update(tid, expected={'RUNNING'}, state='VERIFYING', result=result,
                                      verification={'success': success, 'evidence': evidence})
            else:
                self.store.update(tid, expected={'RUNNING'}, state='UNKNOWN', result=result)
                self.notify(f'{tid}: respuesta del ejecutor desconocida; requiere inspecciÃ³n.')
        elif state == 'VERIFYING':
            v = task['verification']
            self.learning.record(task, v['success'], v['evidence'])
            self.store.update(tid, expected={'VERIFYING'}, state='COMPLETED' if v['success'] else 'DIAGNOSING', feedback=v)
            if v['success']:
                self.notify(f"{tid}: resultado verificado. Evidencia registrada para {task['execution_id']}.")
        elif state == 'DIAGNOSING':
            alternatives = list(task.get('alternatives', []))
            if not task.get('diagnosed'):
                intent = self.manager.core_mind.process_message(
                    '[SYSTEM_EVENT] Analiza el fallo sin ejecutar acciones. Objetivo: ' + task['request']
                    + '\nEvidencia: ' + json.dumps(task.get('feedback'), ensure_ascii=False)[:6000])
                causes = (intent.get('failure_analysis') or {}).get('possible_causes', [])[:3]
                alternatives = []
                for cause in sorted(causes, key=lambda c: c['confidence'], reverse=True):
                    alternatives.append(self.manager.memory.add_hypothesis(task.get('context', 'general'),
                        json.dumps({'cause': cause['cause'], 'fix': cause['proposed_fix'],
                                    'evidence': task.get('feedback')}, ensure_ascii=False),
                        initial_confidence=cause['confidence']))
            if not alternatives:
                self.store.update(tid, expected={'DIAGNOSING'}, state='FAILED', diagnosed=True)
                self.notify(f"{tid}: hipÃ³tesis agotadas. Se conserva la evidencia.")
            else:
                chosen = alternatives.pop(0)
                self.store.update(tid, expected={'DIAGNOSING'}, state='GENERATING', diagnosed=True,
                                  hypothesis_id=chosen, alternatives=alternatives)
        elif state == 'CANCELLING':
            result = self.proxy.cancel(task['execution_id'])
            if result.get('status') == 'cancelled':
                self.store.update(tid, expected={'CANCELLING'}, state='CANCELLED', result=result)
            elif result.get('status') == 'unknown':
                self.store.update(tid, expected={'CANCELLING'}, state='UNKNOWN', result=result)

    async def run(self):
        if self.lock.file is None:
            self.lock.acquire()
        try:
            last_health = 0
            while not self.stopping:
                await asyncio.to_thread(self.tick)
                if time.monotonic() - last_health > 60:
                    try:
                        await self.manager.core_heart._fast_beat_basal()
                    except Exception:
                        logger.exception('FallÃ³ el diagnÃ³stico; el coordinador continÃºa')
                    last_health = time.monotonic()
                await asyncio.sleep(1)
        finally:
            self.lock.release()
