import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
import queue
import secrets
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
import re
import requests

from app.pixy.budget import current_budget, current_goal_revision, current_question_revision, StaleGoalRevision
from app.mind.conversation import ConversationStore, ReplyResult, current_turn
from app.pixy.control import ControlStore, InstanceLock, BudgetExceeded, TERMINAL, INITIAL_TASK_BUDGET
from app.voice.runtime.contract import digest, manifest_from_request, proxy_url, proxy_headers, skill_metadata
from app.mind.verifier import verify_result
from app.mind.recipe import RevisionPlanner, step_params, step_output
from app.mind.result_presenter import present_recipe
from app.hand.skill_spec import validate_recipe
from app.hand.agent_loop import AgentExhausted
from app.dreams.dreams import Dreams
from app.mind.requirements import RequirementsPlanner
from app.mind.skill_selector import validate_params
from app.kernel.tracing import trace_scope, get_trace_id, new_trace_id
from app.pixy.skill_approvals import scope_for

logger = logging.getLogger(__name__)


def _typing_loop(telegram, chat_id, thread_id, stop_event, delay=0.5, interval=4.0):
    if stop_event.wait(delay):
        return
    while not stop_event.is_set():
        try:
            telegram.send_chat_action(
                'typing', chat_id=chat_id, message_thread_id=thread_id)
        except Exception:
            logger.warning('No se pudo actualizar el indicador de escritura')
        if stop_event.wait(interval):
            return


def normalize_manifest(raw, default_purpose):
    """Limpia y valida el manifiesto tolerando markdown, JSON anidado y listas de capacidades en string."""
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            lines = [l for l in text.splitlines() if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            data = json.loads(text)
        except Exception:
            start = text.find('{')
            end = text.rfind('}')
            if start != -1 and end != -1 and end > start:
                try:
                    data = json.loads(text[start:end + 1])
                except Exception:
                    data = {}
            else:
                data = {}

    if not isinstance(data, dict):
        data = {}

    for key in ('manifest', 'data', 'result', 'response'):
        if isinstance(data.get(key), dict):
            data = data[key]
            break

    purpose = data.get('purpose')
    if not isinstance(purpose, str) or not purpose.strip():
        purpose = str(default_purpose or 'Objetivo no especificado').strip()

    caps = data.get('capabilities')
    if isinstance(caps, str):
        caps = [c.strip() for c in caps.split(',') if c.strip()]
    elif isinstance(caps, list):
        caps = [str(c).strip() for c in caps if str(c).strip()]
    else:
        caps = []

    allowed = {'Network', 'FileSystem', 'Database'}
    caps = [c for c in caps if c in allowed]

    lower_purpose = purpose.lower()
    if not caps and any(kw in lower_purpose for kw in (
            'clima', 'tiempo', 'api', 'http', 'web', 'descargar', 'buscar', 'url',
            'correo', 'email', 'mail', 'noticia', 'enviar')):
        caps = ['Network']

    return {'purpose': purpose, 'capabilities': caps}



from app.voice.runtime.docker_runtime import DockerRuntime as ProxyClient


class Coordinator:
    def __init__(self, manager, proxy=None):
        self.manager = manager
        self.store = getattr(manager, 'coordinator_control', None) or ControlStore(
            manager.state.db_path, getattr(manager,'events',None))
        self.learning = getattr(manager, "dreams", None) or Dreams(
            self.store.db_path, getattr(manager, 'rules', None), getattr(manager, 'events', None),
            memory=getattr(manager, 'memory', None))
        self.proxy = proxy or manager.runtime
        data_dir = getattr(manager, "data_dir", None)
        self.root = Path(data_dir) if data_dir is not None else Path(manager.state.db_path).parent
        self.lock = InstanceLock(self.root / 'coordinator.lock')
        self.stopping = False
        configured = int(os.getenv('KHAI_LLM_CONCURRENCY', '2'))
        self.llm_concurrency = max(1, min(4, configured))
        self._llm_executor = ThreadPoolExecutor(max_workers=self.llm_concurrency,
                                                thread_name_prefix='khai-llm')
        self._mailbox_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='khai-mailbox')
        self._llm_jobs = {}
        self._mailbox_job = None
        self._notifications = queue.Queue()
        self.requirements = RequirementsPlanner(getattr(manager, 'llm', None))
        self.revision_planner = RevisionPlanner(getattr(manager, 'llm', None))
        session_db = (getattr(manager, 'control_db', None) or getattr(self.store, 'db_path', None)
                      or manager.state.db_path)
        self.conversations = ConversationStore(session_db)
        mailbox = getattr(manager, 'voice_mailbox', None)
        if mailbox is not None:
            mailbox.on_delivered = self._notification_delivered
        if hasattr(manager, 'llm') and hasattr(manager.llm, '_gateway'):
            manager.llm._gateway.notifier = self.notify

    def notify(self, text, buttons=None, message_thread_id=None, chat_id=None):
        if not text or not str(text).strip():
            return
        cleaned = re.sub(r'^(task_[a-f0-9]{32}|run_[a-f0-9]{32}|auth_[a-f0-9]{32}|case-[a-f0-9]{16,32}):\s*', '', str(text).strip())
        dolphin = getattr(self.manager, 'dolphin', None)
        if dolphin is not None and not cleaned.startswith('[Mode =') and not cleaned.startswith('[Modo '):
            banner = dolphin.consume_transition_banner(dolphin.get_active_mode())
            if isinstance(banner, str):
                cleaned = banner + cleaned
        context = current_turn.get() or {}
        goal = current_goal_revision.get()
        routing_task = self.store.get(goal[0]) if goal else None
        if goal and routing_task and routing_task.get('goal_revision', 0) != goal[1]:
            return
        task_id = ((routing_task.get('response_task_id') or goal[0]) if goal
                   else context.get('task_id'))
        task = self.store.get(task_id) if task_id else None
        sid = (task or {}).get('conversation_id') or context.get('conversation_id')
        metadata = {'conversation_id': sid, 'task_id': task_id}
        if buttons:
            metadata['buttons'] = buttons
        th_id = message_thread_id or context.get('thread_id') or context.get('message_thread_id')
        ch_id = chat_id or context.get('chat_id')
        if sid and (not th_id or not ch_id) and hasattr(self, 'conversations'):
            conv = self.conversations.get_conversation(sid)
            if conv:
                ch_id = ch_id or conv.get('chat_id')
                th_id = th_id or conv.get('thread_id')
        if th_id:
            metadata['message_thread_id'] = th_id
        if ch_id:
            metadata['chat_id'] = ch_id
        if sid:
            kinds = {'WAITING_SCOPE': 'scope', 'WAITING_INPUT_CONFIRMATION': 'inputs',
                     'WAITING_CREATION_APPROVAL': 'creation', 'WAITING_APPROVAL': 'execution',
                     'WAITING_SERVICE_APPROVAL': 'service', 'UNVERIFIED': 'review',
                     'WAITING_FEEDBACK': 'review', 'WAITING_EFFECT_DETAIL': 'effect_detail',
                     'WAITING_EFFECT_DECISION': 'effect_decision'}
            if task and task['state'] in kinds:
                metadata['question_id'] = self.conversations.question(
                    sid, kinds[task['state']], task_id, task['revision'], task['state'])
            elif task and task.get('recovery_authorization_id'):
                auth = self.manager.pixy.authorizations.get(task['recovery_authorization_id'])
                if auth and auth['status'] == 'pending':
                    metadata['question_id'] = self.conversations.question(
                        sid, 'authorization', auth['authorization_id'], auth['plan_hash'], 'pending')
            elif context.get('source'):
                auths = (self.manager.pixy.authorizations.pending_all()
                         if getattr(getattr(self.manager, 'pixy', None), 'authorizations', None) else [])
                for auth in reversed(auths):
                    if auth.get('requested_by') == context['source']:
                        metadata['question_id'] = self.conversations.question(
                            sid, 'authorization', auth['authorization_id'], auth['plan_hash'], 'pending')
                        break
        mailbox = getattr(self.manager, 'voice_mailbox', None)
        if mailbox is not None:
            delivery = mailbox.enqueue(cleaned, metadata=metadata)
            if metadata.get('question_id'):
                self.conversations.attach(metadata['question_id'], delivery)
            self._schedule_mailbox()
            return
        self._notifications.put((cleaned, metadata))
        self._schedule_mailbox()

    def _deliver_notifications(self):
        mailbox = getattr(self.manager, 'voice_mailbox', None)
        if mailbox is not None:
            try:
                def _do_send(text, meta=None):
                    kwargs = {'parse_mode': None}
                    if meta:
                        if meta.get('buttons'):
                            kwargs['buttons'] = meta['buttons']
                        if meta.get('message_thread_id'):
                            kwargs['message_thread_id'] = meta['message_thread_id']
                        if meta.get('chat_id'):
                            kwargs['chat_id'] = meta['chat_id']
                    return self.manager.telegram.send_message(text, **kwargs)

                while mailbox.deliver_one(_do_send):
                    pass
            except Exception:
                logger.warning('Entrega Telegram incierta; revisar outbox durable')
            return
        while True:
            try:
                text, metadata = self._notifications.get_nowait()
            except queue.Empty:
                return
            try:
                message_ids = []
                for offset in range(0, min(len(text), 64000), 3900):
                    kwargs = {'parse_mode': None}
                    if metadata.get('buttons'):
                        kwargs['buttons'] = metadata['buttons']
                    if metadata.get('message_thread_id'):
                        kwargs['message_thread_id'] = metadata['message_thread_id']
                    if metadata.get('chat_id'):
                        kwargs['chat_id'] = metadata['chat_id']
                    receipt = self.manager.telegram.send_message(text[offset:offset + 3900], **kwargs)
                    if isinstance(receipt, dict) and type((receipt.get('result') or {}).get('message_id')) is int:
                        message_ids.append(receipt['result']['message_id'])
                if metadata.get('conversation_id'):
                    self.conversations.delivered(metadata['conversation_id'], 'delivery-' + uuid.uuid4().hex,
                        text, metadata.get('task_id'), metadata.get('question_id'), message_ids)
            except Exception:
                logger.warning('No se pudo notificar; el estado durable permanece disponible')

    def _notification_delivered(self, row, message_ids):
        metadata = json.loads(row['metadata'])
        if metadata.get('conversation_id'):
            self.conversations.delivered(metadata['conversation_id'], row['delivery_id'], row['text'],
                metadata.get('task_id'), metadata.get('question_id'), message_ids)

    def _schedule_mailbox(self):
        if self._mailbox_job is None or self._mailbox_job.done():
            self._mailbox_job = self._mailbox_executor.submit(self._deliver_notifications)

    def _run_task(self, task):
        typing_stop = threading.Event()
        typing_thread = None
        try:
            conversation = (self.conversations.get_conversation(task.get('conversation_id'))
                            if task.get('conversation_id') else None)
            send_action = getattr(getattr(self.manager, 'telegram', None), 'send_chat_action', None)
            if conversation and callable(send_action):
                typing_thread = threading.Thread(
                    target=_typing_loop,
                    args=(self.manager.telegram, conversation['chat_id'],
                          conversation.get('thread_id'), typing_stop),
                    daemon=True,
                    name='khai-telegram-typing',
                )
                typing_thread.start()
        except Exception:
            logger.warning('No se pudo iniciar el indicador de escritura')
        token = current_budget.set(lambda: self.store.consume(task['id'], 'llm_calls'))
        goal_token = current_goal_revision.set((task['id'], task.get('goal_revision', 0)))
        try:
            if time.time() >= task['deadline'] and task['state'] != 'CANCELLING':
                if task.get('execution_id'):
                    self.store.update(task['id'], state='CANCELLING', stop_reason='deadline')
                else:
                    self.store.update(task['id'], state='EXHAUSTED')
                return
            self.advance(task)
        except StaleGoalRevision:
            logger.info('Resultado obsoleto descartado para %s', task['id'])
        except BudgetExceeded as exc:
            try:
                self.store.update(task['id'], state='EXHAUSTED', error=str(exc))
                self.notify(f"{task['id']}: Presupuesto agotado ({str(exc)}). Registrado para diagnóstico y remediación de Heart.")
            except StaleGoalRevision:
                pass
        except (requests.RequestException, ConnectionError) as exc:
            self.store.event(task['id'], 'transport_error', {'type': type(exc).__name__})
        except Exception as exc:
            current = self.store.get(task['id'])
            if (current['state'] not in {'CANCELLED', 'CANCELLING'}
                    and current.get('goal_revision', 0) == task.get('goal_revision', 0)):
                self.store.update(task['id'], state='FAILED', error=str(exc)[:1000])
                self.notify(f"{task['id']}: detenido: {str(exc)[:700]}")
                logger.exception('Error de coordinación')
        finally:
            typing_stop.set()
            if typing_thread is not None:
                typing_thread.join(timeout=0.2)
            current_budget.reset(token)
            current_goal_revision.reset(goal_token)

    def _collect_llm_jobs(self):
        for task_id, future in list(self._llm_jobs.items()):
            if not future.done():
                continue
            del self._llm_jobs[task_id]
            try:
                future.result()
            except Exception:
                logger.exception('Falló el job aislado de LLM para %s', task_id)

    def intake(self, source, text, trace_id=None):
        tid = trace_id or get_trace_id() or new_trace_id()
        direct = str(text).lstrip().startswith(('/make ', '/do '))
        context = current_turn.get() or self.conversations.turn(source) or {}
        cid = context.get('conversation_id')
        if cid and not direct:
            for t in reversed(self.store.all()):
                if (t.get('conversation_id') == cid and t.get('state') == 'ROUTING'
                        and t['id'] not in self._llm_jobs
                        and (time.time() - t.get('created_at', 0)) < 3.0):
                    combined = f"{t['request']}\n\n{text}"
                    self.store.update(t['id'], expected={'ROUTING'}, request=combined,
                                      source_request=combined)
                    self.conversations.bind(source, t['id'])
                    return self.store.get(t['id'])
        task = self.store.create(source, text, trace_id=tid,
                                 conversation_id=cid,
                                 kind='objective' if direct else 'turn',
                                 input_confirmation_required=not direct)
        self.conversations.bind(source, task['id'])
        return task

    def handle_reply(self, source, text, conversation_id, reply_to=None):
        """Resolve one delivered question, never run successive reply handlers."""
        words = text.strip().split()
        decision_words = {'y', 'yes', 'sí', 'si', 'n', 'no', 'confirmo', 'confirmar'}
        pending = self.conversations.pending(conversation_id)
        explicit = next((q for q in pending if q['id'] in words or q['subject_id'] in words), None)
        if reply_to is not None:
            by_message = next((q for q in pending if reply_to in json.loads(q['message_ids'])), None)
            if explicit and by_message and explicit['id'] != by_message['id']:
                return ReplyResult(True, 'El mensaje señala dos preguntas distintas. Responde a la que quieres resolver.')
            explicit = by_message
            if explicit is None:
                if self.conversations.question_for_message(conversation_id, reply_to):
                    return ReplyResult(True, 'Esa pregunta ya no está pendiente; no apliqué la respuesta.')
                if words and words[0].casefold().strip('.!') in decision_words:
                    return ReplyResult(True, 'El mensaje al que respondes no tiene una decisión pendiente registrada.')
                return ReplyResult(False)
        decision = bool(words and len(words) <= 2 and words[0].casefold().strip('.!') in decision_words)
        bare = decision and len(words) == 1
        if decision and len(words) == 2 and explicit is None:
            return ReplyResult(True, 'No encontré una pregunta enviada pendiente con esa referencia.')
        if bare and explicit is None and self.conversations.uncertain_question(conversation_id):
            return ReplyResult(True, 'Hay una pregunta cuya entrega no pude confirmar. Responde al mensaje concreto o consulta /p antes de aprobar.')
        if not explicit and not bare:
            return ReplyResult(False)
        if not pending:
            return ReplyResult(True, 'No hay una pregunta enviada pendiente de respuesta.')
        question = explicit or pending[0]
        answer = ' '.join(word for word in words if word not in {question['id'], question['subject_id']})
        return self.answer_question(source, question, answer, conversation_id)

    def answer_question(self, source, question, answer, conversation_id):
        if (question['conversation_id'] != conversation_id or question['status'] != 'open'):
            return ReplyResult(True, 'La pregunta ya no está pendiente.')
        if self.store.has_command(source):
            return ReplyResult(True, 'La decisión ya quedó registrada.')
        kind, subject = question['kind'], question['subject_id']
        if kind == 'authorization':
            auth = self.manager.pixy.authorizations.get(subject)
            if not auth or auth['status'] != 'pending' or auth['plan_hash'] != question['revision']:
                self.conversations.stale(question['id'])
                return ReplyResult(True, 'La autorización cambió o venció; no apliqué la respuesta.')
            if answer.casefold().strip(' .!') not in {'y', 'yes', 'sí', 'si', 'n', 'no'}:
                return ReplyResult(True, 'Responde sí o no a la propuesta de Pixy.')
            reply = self.manager.pixy.direct_response(answer.strip(' .!') + ' ' + subject, source=source)
            self.conversations.answered(question['id'], source)
            self.conversations.complete(source, {'question_id': question['id'], 'result': reply})
            return ReplyResult(True, reply)
        task = self.store.get(subject)
        if (not task or task.get('conversation_id') != conversation_id
                or task['state'] != question['state'] or str(task['revision']) != question['revision']
                or time.time() >= task['deadline']):
            self.conversations.stale(question['id'])
            return ReplyResult(True, 'El objetivo o sus datos cambiaron; no apliqué una respuesta obsoleta.')
        self.conversations.bind(source, subject)
        context = current_turn.get()
        if context is not None:
            context['task_id'] = subject
        token = current_question_revision.set((subject, task['revision'], question['id']))
        try:
            if kind in {'creation', 'execution', 'service'}:
                normalized = answer.casefold().strip(' .!')
                if normalized not in {'y', 'yes', 'sí', 'si', 'n', 'no'}:
                    return ReplyResult(True, 'Responde sí o no; para cambiar el objetivo describe el cambio.', subject)
                approve = normalized in {'y', 'yes', 'sí', 'si'}
                if kind == 'creation':
                    reply = self.manager.pixy.direct_response(('y ' if approve else 'n ') + subject, source=source)
                elif kind == 'execution':
                    operation = (task.get('approval') or {}).get('operation')
                    if operation and set(operation) != {'action', 'details'}:
                        return ReplyResult(True, 'Esta operación necesita la orden de recuperación de Pixy; consulta /p.', subject)
                    self.approval(subject, task['approval']['token'], approve, source)
                    reply = 'Ejecución autorizada.' if approve else 'Ejecución cancelada.'
                else:
                    self.manager.pixy_services.decide(task['service_proposal'], approve, decided_by=source)
                    reply = 'Servicio autorizado.' if approve else 'Servicio rechazado.'
            else:
                handler = {'scope': self.accept_scope_reply, 'inputs': self.accept_input_confirmation,
                           'review': self.accept_review_reply, 'effect_detail': self.accept_effect_detail,
                           'effect_decision': self.accept_effect_decision}[kind]
                reply = handler(source, subject + ' ' + answer)
            after = self.store.get(subject)
            if after['revision'] != task['revision'] or kind == 'service':
                self.conversations.answered(question['id'], source)
                self.conversations.complete(source, {'question_id': question['id'], 'task_id': subject,
                                                       'state': after['state'], 'result': reply})
            return ReplyResult(True, reply, subject)
        except StaleGoalRevision:
            return ReplyResult(True, 'La pregunta cambió antes de aplicar tu respuesta.', subject)
        finally:
            current_question_revision.reset(token)

    def dispatch_tool(self, task, call):
        """Validated model calls use the same transitions as deterministic replies."""
        source, sid = task['source'], task.get('conversation_id')
        previous = self.conversations.turn(source)
        if previous and previous['status'] == 'completed':
            return json.loads(previous['receipt']).get('result')
        call_id = self.conversations.record_call(source, call)
        def finish(reply, status='completed'):
            self.conversations.complete(source, {'call_id': call_id, 'tool_call': call,
                                                   'status': status, 'result': reply})
            return reply
        name, args = call['name'], call['arguments']
        if name == 'answer_question':
            if args['answer'].strip() != task['request'].strip():
                return finish('Necesito tu respuesta literal a la pregunta; no inferiré una aprobación.', 'rejected')
            question = self.conversations.get_question(args['question_id'], sid)
            if not question:
                return finish('No encontré esa pregunta en esta conversación.', 'rejected')
            if previous and previous.get('reply_to') is not None and previous['reply_to'] not in json.loads(question['message_ids']):
                return finish('La respuesta no corresponde al mensaje al que contestaste.', 'rejected')
            result = self.answer_question(source, question, args['answer'], sid)
            reply = result.text
        elif name == 'set_default_provider':
            capability, provider = args['capability'], args['provider']
            catalog = getattr(getattr(self.manager, 'hand', None), 'catalog', None)
            available = catalog.providers(capability) if catalog else []
            if provider not in available:
                return finish('Ese proveedor no estÃ¡ disponible para la capacidad indicada.', 'rejected')
            receipt = {'provider': provider, 'source': source, 'approved_at': time.time()}
            self.manager.memory.set_preference('default_provider_' + capability, receipt)
            reply = (f'{provider} quedÃ³ como proveedor predeterminado para {capability}. '
                     'Esta preferencia es auditable y puede cambiarse.')
        elif name == 'get_system_status':
            reply = json.dumps(self.manager.heart.check_integrity(), ensure_ascii=False)
        else:
            target = self.store.get(args['task_id'])
            if not target or target.get('conversation_id') != sid or target.get('kind') == 'turn':
                return finish('Ese objetivo no pertenece a esta conversación.', 'rejected')
            if name == 'get_task_status':
                services = (self.manager.voice_services.active_services()
                            if getattr(self.manager, 'voice_services', None) else {})
                hosts = []
                for ref in target.get('services', []):
                    spec = services.get(ref['service_id'], {}).get('operations', {}).get(ref['operation'])
                    if spec and spec['version'] == ref['version']:
                        hosts.append(urlsplit(spec['url']).hostname or ref['service_id'])
                reply = (f"Solicitud: {target['request']}. "
                         f"Estado: {target['state']}. Ejecuciones registradas: {target['executions']}. "
                         f"Servicios aprobados: {', '.join(sorted(set(hosts))) or 'ninguno todavía'}.")
                if target.get('mode') == 'AGENT' and target.get('execution_id'):
                    reply += ' Estado observado del servicio: ' + str(target.get(
                        'runtime_status', (target.get('result') or {}).get('status', 'desconocido'))) + '.'
            elif name == 'cancel_task':
                self.cancel(target['id'], source)
                reply = 'Cancelación solicitada; los efectos ya enviados conservan sus recibos.'
            elif name == 'revise_task':
                if args['change'].strip() != task['request'].strip():
                    return finish('Necesito el cambio literal que pediste; no añadí una modificación inferida.', 'rejected')
                reply = self.revise_goal(target, args, source)
            else:
                raise ValueError('Operación conversacional no disponible')
        return finish(reply)

    def revise_goal(self, selected, args, source):
        if selected.get('goal_revision', 0) != args['goal_revision']:
            return 'El objetivo cambió mientras respondías; revisa su estado antes de corregirlo.'
        if selected['state'] in {'UNVERIFIED', 'WAITING_FEEDBACK'}:
            return self.accept_review_reply(source, selected['id'] + ' ' + args['change'])
        allowed = {'ROUTING', 'WAITING_CREATION_APPROVAL', 'SCOPE_PLANNING', 'WAITING_SCOPE',
                   'RESOLVING_SCOPE', 'RECIPE_STEP', 'GENERATING', 'REVIEWING', 'READY',
                   'WAITING_INPUT_CONFIRMATION', 'WAITING_APPROVAL', 'WAITING_SERVICE_APPROVAL'}
        if selected['state'] not in allowed or selected.get('execution_id'):
            return 'Ese trabajo ya se ejecutó o está en ejecución; consulta su estado antes de aplicar otro cambio.'
        def apply(current):
            if (current.get('goal_revision', 0) != args['goal_revision']
                    or current['state'] not in allowed or current.get('execution_id')):
                raise StaleGoalRevision('El objetivo avanzó antes de aplicar el cambio')
            current['goal_revision'] = current.get('goal_revision', 0) + 1
            extension = {'kind': 'owner_revision', 'source': source, 'created_at': time.time(),
                         **INITIAL_TASK_BUDGET}
            current.setdefault('budget_extensions', []).append(extension)
            for counter in INITIAL_TASK_BUDGET:
                current['max_' + counter] += extension[counter]
            current['deadline'] = max(current['deadline'], time.time() + 1800)
            current['revision_needs_creation_approval'] = (
                current['state'] == 'WAITING_CREATION_APPROVAL' or
                current.get('revision_needs_creation_approval', False))
            current.setdefault('scope_answers', []).append({
                'questions': ['¿Qué cambia en el objetivo?'], 'answer': args['change'],
                'source': source, 'revision': current['revision']})
            current['state'] = 'RESOLVING_SCOPE'
            current['manifest'] = current.get('manifest') or {'purpose': current['request'], 'capabilities': []}
            current['approval'] = None
            current['authorized_hash'] = None
            current['confirmed_input_signature'] = None
            current['selected_version_id'] = None
        try:
            self.store.command(source, selected['id'], apply)
        except StaleGoalRevision:
            return 'El objetivo avanzó mientras llegaba el cambio; no modifiqué una revisión distinta.'
        return 'Cambio registrado en el mismo objetivo; revisaré los datos antes de continuar.'

    def intake_recipe(self, source, recipe, inputs, trace_id=None):
        """Start a pinned composition; every execution remains under one task ID."""
        if not hasattr(self.manager, 'hand'):
            raise ValueError('Hand is required for recipe execution')
        records = {}
        for step in recipe.get('steps', []):
            version_id = step.get('version_id')
            version = self.manager.hand.version(version_id)
            record = self.manager.hand.catalog.get(version_id)
            if version['status'] != 'active' or not record or record['status'] != 'active':
                raise ValueError('Recipe requires active pinned skill versions')
            if record['spec']['runtime'] == 'AGENT':
                raise ValueError('Long-running skills cannot be recipe steps')
            records[version_id] = record['spec']
        validate_recipe(recipe, records)
        if not validate_params(recipe['input_schema'], inputs):
            raise ValueError('Recipe inputs do not match the contract')
        task = self.store.create(source, recipe['recipe_id'], trace_id=trace_id or new_trace_id(),
                                 state='RECIPE_STEP', recipe=recipe, recipe_input=inputs,
                                 original_request=recipe['recipe_id'],
                                 input_confirmation_required=True,
                                 recipe_outputs={'input': inputs}, recipe_receipts=[], recipe_step_index=0)
        return task

    def _recipe_step(self, task):
        index = task['recipe_step_index']
        step = task['recipe']['steps'][index]
        missing = step['version_id'].startswith('missing-')
        if missing:
            action = task['recipe_missing_specs'][step['id']]
            step_svcs = action.get('services', [])
            if not step_svcs:
                step_svcs = [p['ref'] for p in task.get('setup_proposals', [])
                             if p.get('step_id') in {step['id'], None} and p.get('ref')]
                if not step_svcs and task.get('creation_consent'):
                    step_svcs = list(task['creation_consent'].get('allowed_services', []))
            spec = {key: action[key] for key in ('skill_id', 'description', 'use_cases',
                                                 'input_schema', 'output_schema')}
            spec.update(runtime='SAFE', dependencies=[], services=step_svcs,
                        verifier={'kind': 'owner_criterion'})
            manifest = {'purpose': action['description'], 'capabilities': action['capabilities']}
            selected_version_id = None
            skill_name = action['skill_id']
        else:
            version = self.manager.hand.version(step['version_id'])
            record = self.manager.hand.catalog.get(step['version_id'])
            prior = task.get('reused_step_receipt')
            reusable_draft = (version['status'] == 'draft' and prior
                              and prior.get('version_id') == step['version_id']
                              and prior.get('skill_artifact_hash') == version['artifact_hash']
                              and record and record['status'] == 'candidate')
            if not record or not (version['status'] == 'active' and record['status'] == 'active'
                                  or reusable_draft):
                raise ValueError('Pinned recipe version is no longer active')
            spec = record['spec']
            manifest = json.loads(version['manifest'])
            selected_version_id = step['version_id']
            skill_name = version['skill_name']
        if not self.manager.pixy.preflight(manifest)['safe']:
            raise PermissionError('Pixy rejected a pinned recipe step')
        params = step_params(task['recipe'], index, task['recipe_outputs'], spec)
        prior = task.get('reused_step_receipt')
        reuse_hash = (prior.get('approved_hash') if prior and
                      prior.get('step_id') == step['id'] and
                      prior.get('version_id') == step['version_id'] and
                      prior.get('params') == params and
                      prior.get('services') == spec['services'] else None)
        resolved_svcs = spec.get('services') or [p['ref'] for p in task.get('setup_proposals', []) if p.get('ref')] or list((task.get('creation_consent') or {}).get('allowed_services', []))
        self.store.update(task['id'], expected={'RECIPE_STEP'}, state='GENERATING',
                          selected_version_id=selected_version_id,
                          version_id=selected_version_id,
                          skill_name=skill_name, manifest=manifest,
                          request=manifest.get('purpose', task['request']), params=params,
                          services=resolved_svcs, context=manifest.get('purpose', '')[:160],
                          planned_skill_spec=action if missing else None,
                          reuse_authorized_hash=reuse_hash, reused_step_receipt=None,
                          artifact=None, artifact_hash=None, authorized_hash=None,
                          approval=None, execution_id=None, result=None, grants=[])
        if len(task['recipe']['steps']) > 1:
            self.notify(f"{task['id']}: estoy realizando: {spec['description'][:180]}")

    def _complete_recipe_step(self, task, result):
        index = task['recipe_step_index']
        step = task['recipe']['steps'][index]
        record = self.manager.hand.catalog.get(step['version_id'])
        try:
            output = step_output(result, record['spec']['output_schema'])
        except ValueError as exc:
            self.store.update(task['id'], expected={'RUNNING'}, state='DIAGNOSING', result=result,
                              feedback={'error': str(exc), 'step': step['id']})
            self.store.event(task['id'], 'recipe_step_contract_failure',
                             {'step': step['id'], 'execution_id': task['execution_id'], 'error': str(exc)})
            self.notify(f"{task['id']}: el paso {step['id']} no cumplió su contrato: {exc}.")
            return
        version = self.manager.hand.version(step['version_id'])
        gateway = getattr(self.manager, 'gateway', None)
        transport = (gateway.technical_receipts(task['id'], task['execution_id'],
                                               task.get('services', []))
                     if gateway and hasattr(gateway, 'technical_receipts') else [])
        incomplete_batch = [item for item in transport
                            if item.get('parent_request_id') and item.get('status') != 'completed']
        incomplete_write = [item for item in transport
                            if item.get('effect') == 'write' and item.get('status') != 'completed']
        incomplete_effects = incomplete_batch or incomplete_write
        receipt = {'step_id': step['id'], 'version_id': step['version_id'],
                   'execution_id': task['execution_id'], 'artifact_hash': task['artifact_hash'],
                   'skill_artifact_hash': version['artifact_hash'],
                   'skill_spec_hash': task.get('skill_spec_hash'),
                   'approved_hash': (task.get('approval') or {}).get('artifact_hash'),
                   'params': task.get('params', {}), 'services': task.get('services', []),
                    'external_effects': [{'request_id': item['request_id'],
                                          'service_id': item['service_id'],
                                          'operation': item['operation'],
                                          'version': item['version'],
                                          'effect': item['effect'], 'status': item['status'],
                                          'effect_contract': item.get('effect_contract'),
                                          'summary': item.get('summary'),
                                          'parameter_hashes': item.get('parameter_hashes', {}),
                                          'new_effect_authorization_id': item.get('new_effect_authorization_id'),
                                          'parent_request_id': item.get('parent_request_id'),
                                          'item_index': item.get('item_index'),
                                          'resource_id': item.get('resource_id')}
                                        for item in transport],
                   'output': output, 'status': 'completed'}
        last = index == len(task['recipe']['steps']) - 1
        def apply(current):
            if current['state'] != 'RUNNING' or current.get('execution_id') != task['execution_id']:
                raise ValueError('Recipe step changed during completion')
            current.setdefault('recipe_receipts', []).append(receipt)
            current.setdefault('recipe_outputs', {})[step['id']] = output
            current['result'] = result
            if last and not incomplete_effects:
                source, field = current['recipe']['output'].split('.', 1)
                current['recipe_result'] = current['recipe_outputs'][source][field]
                current['machine_verification'] = {'success': None,
                    'evidence': {'kind': 'recipe_schema', 'schema_valid': True,
                                 'steps': len(current['recipe_receipts']),
                                 'owner_review_required': True}}
                current['state'] = 'UNVERIFIED'
            elif incomplete_effects:
                current['batch_incomplete'] = True
                current['machine_verification'] = {
                    'success': None,
                    'evidence': {'kind': 'batch_effects',
                                 'incomplete_items': [item['request_id'] for item in incomplete_effects]}}
                current['state'] = 'UNVERIFIED'
            else:
                current['recipe_step_index'] = index + 1
                current['state'] = 'RECIPE_STEP'
                current['execution_id'] = None
                current['result'] = None
        self.store.mutate(task['id'], apply)
        if incomplete_effects:
            statuses = ', '.join(f"{item.get('item_index', 0) + 1}:{item['status']}"
                                 for item in incomplete_effects)
            self.notify(f"{task['id']}: la operación del paso {step['id']} tiene efectos "
                        f"sin confirmar ({statuses}). No iniciaré el siguiente paso ni aceptaré "
                        "el objetivo como completo. Explica qué observaste o cancela.")
        elif last:
            completed = self.store.get(task['id'])
            self.notify(f"{task['id']}: " + present_recipe(completed))

    @staticmethod
    def latest_task(tasks):
        """Elemento pendiente más reciente; el propietario no necesita copiar IDs."""
        if not tasks:
            return None
        return max(enumerate(tasks), key=lambda pair: (
            pair[1].get('updated_at') or pair[1].get('created_at') or 0, pair[0]))[1]

    def accept_scope_reply(self, source, text):
        """Bind an ordinary reply to exactly one pending objective and revision."""
        if text.startswith('/'):
            return None
        pending = [task for task in self.store.all() if task['state'] == 'WAITING_SCOPE']
        if not pending:
            return None
        words = text.strip().split(maxsplit=1)
        selected = next((task for task in pending if words[0] == task['id']), None) if words else None
        if selected:
            answer = words[1].strip() if len(words) == 2 else ''
        else:
            selected, answer = self.latest_task(pending), text.strip()
        if not answer:
            return 'Falta la respuesta para ' + selected['id']
        cancelled = answer.casefold().strip(' .!') in {'cancelar', 'abortar', 'olvidalo', 'olvídalo'}
        def apply(task):
            if task['state'] != 'WAITING_SCOPE':
                raise ValueError('La pregunta ya no está pendiente')
            if cancelled:
                task['state'] = 'CANCELLED'
                return
            task.setdefault('scope_answers', []).append({
                'questions': task.get('scope_questions', []), 'answer': answer,
                'source': source, 'revision': task['revision']})
            task['state'] = 'RESOLVING_SCOPE'
        self.store.command(source, selected['id'], apply)
        return ('Objetivo cancelado.' if cancelled else None)

    @staticmethod
    def _input_values(task):
        return task.get('recipe_input', {}) if task.get('recipe') else task.get('params', {})

    @classmethod
    def _input_signature(cls, task):
        values = cls._input_values(task)
        subject = task.get('original_request') or task['request']
        return hashlib.sha256(json.dumps({'request': subject, 'values': values},
                                         sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def accept_input_confirmation(self, source, text):
        """Confirm the exact proposed values before any worker starts."""
        if text.startswith('/'):
            return None
        pending = [task for task in self.store.all()
                   if task['state'] == 'WAITING_INPUT_CONFIRMATION']
        if not pending:
            return None
        words = text.strip().split(maxsplit=1)
        selected = next((task for task in pending if words[0] == task['id']), None) if words else None
        if selected:
            answer = words[1].strip() if len(words) == 2 else ''
        else:
            selected, answer = self.latest_task(pending), text.strip()
        normalized = answer.casefold().strip(' .!')
        cancelled = normalized in {'cancelar', 'abortar', 'olvidalo', 'olvídalo'}
        accepted = normalized in {'y', 'yes', 'sí', 'si', 'confirmo', 'confirmar'}
        needs_change = normalized in {'n', 'no'}
        if not answer:
            return selected['id'] + ': confirma los datos o indica qué cambiar.'
        def apply(task):
            if task['state'] != 'WAITING_INPUT_CONFIRMATION':
                raise ValueError('La confirmación ya no está pendiente')
            if task['input_confirmation_signature'] != self._input_signature(task):
                raise ValueError('Los valores cambiaron antes de confirmarlos')
            if cancelled:
                task['state'] = 'CANCELLED'
            elif accepted:
                task['confirmed_input_signature'] = task['input_confirmation_signature']
                task['state'] = 'READY'
            elif needs_change:
                task['scope_questions'] = ['¿Qué dato quieres cambiar y cuál es el nuevo valor?']
                task['state'] = 'WAITING_SCOPE'
            else:
                task.setdefault('scope_answers', []).append({
                    'questions': ['¿Qué valores debo cambiar?'], 'answer': answer,
                    'source': source, 'revision': task['revision']})
                task['state'] = 'RESOLVING_SCOPE'
        self.store.command(source, selected['id'], apply)
        if cancelled:
            return 'Objetivo cancelado.'
        if accepted:
            return None
        if needs_change:
            return '¿Qué dato quieres cambiar y cuál es el nuevo valor?'
        return None

    def accept_effect_decision(self, source, text):
        """A new external effect requires an explicit decision tied to this objective."""
        pending = [task for task in self.store.all()
                   if task['state'] == 'WAITING_EFFECT_DECISION']
        if not pending or text.startswith('/'):
            return None
        words = text.strip().split()
        selected = next((task for task in pending if task['id'] in words), None)
        if not selected:
            selected = self.latest_task(pending)
        choice = next((word for word in words if word != selected['id']), '')
        options = selected.get('effect_options') or {}
        if choice not in options:
            return selected['id'] + ': elige una de las opciones ofrecidas para este efecto.'
        action = options[choice]
        new_effect = None
        if action == 'create':
            target_step = selected.get('effect_target_step')
            writes = self._challenged_writes(selected, target_step)
            if len(writes) != 1:
                return selected['id'] + ': no hay una escritura única identificada; aclara cuál cuestionas.'
            challenged = writes[0]
            if any(not challenged.get(key) for key in ('service_id', 'operation', 'version')):
                return selected['id'] + ': falta el servicio u operación del recibo; no puedo autorizar otra escritura.'
            if (challenged.get('effect_contract') or {}).get('kind') != 'create':
                return selected['id'] + ': la operación registrada no crea recursos; no puedo ofrecer crear otro.'
            if (challenged.get('effect_contract') or {}).get('batch'):
                return selected['id'] + ': una autorización individual no permite repetir un lote.'
            new_effect = {'id': uuid.uuid4().hex, 'task_id': selected['id'],
                          'challenged_request_id': challenged.get('request_id'),
                          'target_step': target_step, 'service_id': challenged['service_id'],
                          'operation': challenged['operation'], 'version': challenged['version'],
                          'source': source, 'issued_at': time.time(),
                          'risk': 'El recurso anterior puede existir; crear otro puede duplicarlo.'}
        def apply(task):
            if task['state'] != 'WAITING_EFFECT_DECISION':
                raise ValueError('La decisión sobre el efecto ya no está pendiente')
            if task.get('effect_options') != options:
                raise ValueError('Las opciones del efecto cambiaron')
            selected_action = options[choice]
            if selected_action == 'create':
                task['new_effect_authorization'] = new_effect
                task['effect_constraints'] = []
                task['revision_action_decision'] = 'create'
                task['state'] = 'RESOLVING_SCOPE'
            elif selected_action == 'update':
                task['new_effect_authorization'] = None
                task['effect_constraints'] = task.get('pending_effect_constraints', [])
                task['revision_action_decision'] = 'update'
                task['state'] = 'RESOLVING_SCOPE'
            else:
                task['state'] = 'CANCELLED'
        decided = self.store.command(source, selected['id'], apply)
        if decided is None:
            return selected['id'] + ': decisión ya registrada.'
        return (selected['id'] + ': revisaré la corrección del recurso registrado.' if action == 'update'
                else selected['id'] + ': autorizaste una nueva escritura del servicio registrado; '
                     'el recurso anterior puede existir y el nuevo puede duplicarlo.'
                if action == 'create' else selected['id'] + ': objetivo cancelado.')

    def accept_effect_detail(self, source, text):
        """Clarify an owner-reported missing effect before offering another write."""
        pending = [task for task in self.store.all() if task['state'] == 'WAITING_EFFECT_DETAIL']
        if not pending or text.startswith('/'):
            return None
        words = text.strip().split()
        selected = next((task for task in pending if task['id'] in words), None)
        if not selected:
            selected = self.latest_task(pending)
        answer = text.replace(selected['id'], '', 1).strip()
        if answer.casefold().strip(' .!') in {'cancelar', 'abortar', 'olvidalo', 'olvídalo'}:
            self.cancel(selected['id'], source)
            return selected['id'] + ': cancelación solicitada.'
        target_step = selected.get('effect_target_step')
        writes = self._target_writes(selected, target_step) if target_step else []
        if ((len(answer) < 5 and not selected.get('effect_disambiguation_round')
                and not selected.get('effect_required_fields')
                and not (len(writes) > 1 and answer.isdecimal()))
                or answer.casefold() in {'yes', 'sí', 'si'}):
            return selected['id'] + ': describe qué observaste y qué resultado esperabas.'
        target_request_id = selected.get('effect_target_request_id')
        position = selected.get('effect_selected_position')
        if len(writes) > 1 and not target_request_id:
            round_number = selected.get('effect_disambiguation_round', 0)
            token = current_budget.set(lambda: self.store.consume(selected['id'], 'llm_calls'))
            try:
                position = self.revision_planner.effect_index(
                    answer, len(writes), allow_llm=round_number > 0)
            finally:
                current_budget.reset(token)
            identities = [item.get('request_id') for item in writes]
            identifiable = all(identities) and len(set(identities)) == len(identities)
            if position is None or not identifiable:
                if round_number == 0:
                    self.store.command(source, selected['id'], lambda task: task.update(
                        effect_disambiguation_round=1,
                        effect_first_detail=answer))
                    count = {2: 'dos', 3: 'tres'}.get(len(writes), str(len(writes)))
                    labels = '; '.join(
                        f"{index}. {item.get('summary', {}).get('name', 'Escritura')} — "
                        f"{item.get('summary', {}).get('context', 'sin contexto declarado')}"
                        for index, item in enumerate(writes, 1))
                    return (f'Hay {count} escrituras en esa acción. ¿Cuál de ellas debo corregir? '
                            f'{labels}. Puedes decir la primera, la última o describirla de otra forma.')
                cause = 'owner_ambiguity' if identifiable else 'system_identity_gap'
                def close(task):
                    if task['state'] != 'WAITING_EFFECT_DETAIL':
                        raise ValueError('La aclaración ya no está pendiente')
                    task['state'] = 'UNFULFILLABLE'
                    task['stop_reason'] = cause
                    version_ids = {
                        receipt['version_id']
                        for attempt in task.get('recipe_attempts', [])
                        for receipt in attempt.get('receipts', [])
                        if receipt.get('version_id')}
                    if task.get('version_id'):
                        version_ids.add(task['version_id'])
                    task['candidate_rejection_pending'] = sorted(version_ids)
                    task.setdefault('effect_details', []).append({
                        'source': source, 'answer': answer,
                        'target_step': target_step})
                closed = self.store.command(source, selected['id'], close)
                if closed:
                    self._reject_unverified_recipe_versions(closed)
                return ('Lo siento, no puedo distinguir con seguridad cuál recurso modificar. '
                        'No haré otra escritura. ¿Puedo ayudarte en algo más?')
            target_request_id = identities[position - 1]
        constraints, issue = self._correction_constraints(
            selected, target_step=target_step, target_request_id=target_request_id)
        questions = constraints[0].get('questions', {}) if constraints and not issue else {}
        if questions:
            required = selected.get('effect_required_fields') or []
            detail = (answer if required else ' '.join(
                part for part in (selected.get('effect_first_detail'), answer) if part))
            token = current_budget.set(lambda: self.store.consume(selected['id'], 'llm_calls'))
            try:
                identified = self.revision_planner.effect_inputs(detail, questions, required)
            finally:
                current_budget.reset(token)
            if identified is None:
                return (selected['id'] + ': no puedo determinar todavía qué dato necesita la '
                        'corrección. Describe qué debería cambiar en el recurso registrado.')
            provided = {**selected.get('effect_provided_fields', {}),
                        **identified['provided']}
            missing = [field for field in identified['required'] if field not in provided]
            if missing:
                field = missing[0]
                self.store.command(source, selected['id'], lambda task: task.update(
                    effect_target_request_id=target_request_id,
                    effect_selected_position=position,
                    effect_required_fields=identified['required'],
                    effect_provided_fields=provided,
                    effect_pending_answer=(task.get('effect_pending_answer', '') + ' ' + detail).strip()))
                return selected['id'] + ': ' + questions[field]['question']
        else:
            provided = {}
        full_detail = ' '.join(part for part in (
            selected.get('effect_first_detail'), selected.get('effect_pending_answer'), answer)
            if part)
        options = self._effect_decision_options(
            selected, constraints if not issue else None, target_step,
            target_request_id)
        def apply(task):
            if task['state'] != 'WAITING_EFFECT_DETAIL':
                raise ValueError('La aclaración del efecto ya no está pendiente')
            task.setdefault('effect_details', []).append({'source': source, 'answer': full_detail,
                                                           'target_step': task.get('effect_target_step')})
            task.setdefault('scope_answers', []).append({'questions': [task.get('effect_question')],
                                                          'answer': full_detail, 'source': source})
            task['effect_options'] = options
            task['pending_effect_constraints'] = constraints
            task['effect_issue'] = issue
            task['effect_target_request_id'] = target_request_id
            task['effect_selected_position'] = position
            task['effect_provided_fields'] = provided
            task['state'] = 'WAITING_EFFECT_DECISION'
        self.store.command(source, selected['id'], apply)
        if 'update' in options.values():
            chosen = (f' Identifiqué la escritura {position} de {len(writes)}.'
                      if position is not None else '')
            return (selected['id'] + ': registré tu aclaración.' + chosen + ' El contrato permite '
                    + self._effect_options_message(options))
        return (selected['id'] + ': no hay una actualización declarada y verificable. '
                + self._effect_options_message(options))

    def accept_review_reply(self, source, text):
        """Owner satisfaction is distinct from a process or infrastructure failure."""
        pending = [task for task in self.store.all() if task['state'] in {'UNVERIFIED', 'WAITING_FEEDBACK'}
                   and task.get('execution_id')]
        if not pending or text.startswith('/'):
            return None
        words = text.strip().split(maxsplit=1)
        selected = next((task for task in pending if words[0] == task['id']), None) if words else None
        selected_by_suffix = (next((task for task in pending if words[1] == task['id']), None)
                              if len(words) == 2 else None)
        if selected_by_suffix:
            selected = selected_by_suffix
        answer = (words[1].strip() if selected and words[0] == selected['id'] and len(words) == 2
                  else words[0] if selected_by_suffix else text.strip())
        if not selected:
            selected = self.latest_task(pending)
        if selected['state'] == 'WAITING_FEEDBACK':
            if answer.casefold().strip(' .!') in {'cancelar', 'abortar', 'olvidalo', 'olvídalo'}:
                self.cancel(selected['id'], source)
                return selected['id'] + ': cancelación solicitada.'
            if len(answer) < 3:
                return 'Explica qué falta en el resultado para ' + selected['id']
            return self._record_scope_refinement(source, selected, answer)
        normalized = answer.casefold().lstrip()
        if normalized.strip(' .!') in {'cancelar', 'abortar', 'olvidalo', 'olvídalo'}:
            self.cancel(selected['id'], source)
            return selected['id'] + ': cancelación solicitada.'
        accepted = normalized in {'y', 'yes', 'sí', 'si', 'sí.', 'si.'}
        ask_feedback = normalized in {'n', 'no', 'n.', 'no.'}
        rejected = normalized.startswith('no,') or normalized.startswith('no ') or normalized.startswith('no:')
        if accepted:
            if selected.get('batch_incomplete'):
                return 'hay efectos externos sin confirmar; explica qué ocurrió o cancela.'
            self.verify(selected['id'], selected['execution_id'], True, source)
            return 'resultado aceptado.'
        if ask_feedback:
            def wait_for_detail(task):
                if task['state'] != 'UNVERIFIED':
                    raise ValueError('Resultado obsoleto')
                task['state'] = 'WAITING_FEEDBACK'
            self.store.command(source, selected['id'], wait_for_detail)
            return '¿Qué falta o qué debería cambiar en la respuesta?'
        feedback = (answer[3:].strip(' :,.') if normalized.startswith('no,') else
                    answer[2:].strip(' :,.') if rejected else answer.strip())
        if not feedback:
            return 'Indica qué falta en el resultado para ' + selected['id']
        return self._record_scope_refinement(source, selected, feedback)

    def _record_scope_refinement(self, source, selected, feedback):
        if (selected.get('mode') == 'AGENT' and selected.get('execution_id')
                and selected.get('runtime_status') == 'unknown'):
            return 'No pude confirmar la parada del servicio. Conservo su ejecución y no iniciaré otra.'
        if self._agent_active(selected):
            def stop_for_change(task):
                if (task['state'] not in {'UNVERIFIED', 'WAITING_FEEDBACK'}
                        or task.get('execution_id') != selected['execution_id']):
                    raise ValueError('La ejecución cambió antes de recibir la corrección')
                task.update(state='WAITING_FEEDBACK', runtime_status='cancelling',
                            pending_runtime_feedback={'source': source, 'feedback': feedback,
                                                      'execution_id': task['execution_id']})
            stopping = self.store.command(source, selected['id'], stop_for_change)
            if stopping:
                try:
                    self._poll_stop(stopping)
                except requests.RequestException:
                    pass
            return ('Corrección registrada en el mismo objetivo. '
                    'Revisaré las acciones cuando se confirme la parada del servicio actual.')
        def apply(task):
            if task['state'] not in {'UNVERIFIED', 'WAITING_FEEDBACK'} or task.get('execution_id') != selected['execution_id']:
                raise ValueError('El resultado cambió antes de recibir la corrección')
            task.pop('pending_runtime_feedback', None)
            task.setdefault('scope_answers', []).append({
                'questions': [], 'answer': feedback, 'source': source,
                'revision': task['revision'], 'previous_execution_id': task['execution_id']})
            task.setdefault('owner_reviews', []).append({
                'execution_id': task['execution_id'], 'goal_revision': task.get('goal_revision'),
                'satisfied': False, 'feedback': feedback, 'source': source})
            extension = {'kind': 'owner_revision', 'source': source,
                         'created_at': time.time(), **INITIAL_TASK_BUDGET}
            task.setdefault('budget_extensions', []).append(extension)
            task['max_generations'] += extension['generations']
            task['max_executions'] += extension['executions']
            task['max_llm_calls'] += extension['llm_calls']
            task['deadline'] = max(task['deadline'], time.time() + 1800)
            if task.get('recipe'):
                task.setdefault('recipe_attempts', []).append({
                    'recipe': task['recipe'], 'receipts': task.get('recipe_receipts', []),
                    'goal_revision': task.get('goal_revision'), 'owner_feedback': feedback})
            else:
                gateway = getattr(self.manager, 'gateway', None)
                transport = (gateway.technical_receipts(task['id'], task['execution_id'],
                                                       task.get('services', []))
                             if gateway and hasattr(gateway, 'technical_receipts') else [])
                task.setdefault('single_attempts', []).append({
                    'execution_id': task['execution_id'],
                    'external_effects': [{key: item.get(key) for key in (
                        'request_id', 'service_id', 'operation', 'version', 'effect',
                        'status', 'effect_contract', 'resource_id', 'summary',
                        'new_effect_authorization_id', 'parent_request_id',
                        'item_index')} for item in transport],
                    'goal_revision': task.get('goal_revision'), 'owner_feedback': feedback})
            task['state'] = 'RESOLVING_SCOPE'
            task['feedback'] = feedback
            task['revision_action_decision'] = None
            task['new_effect_authorization'] = None
            task['batch_incomplete'] = False
            task['effect_options'] = None
            task['pending_effect_constraints'] = []
            task['selected_version_id'] = None
            task['authorized_hash'] = None
            task['approval'] = None
            task['execution_id'] = None
        self.store.command(source, selected['id'], apply)
        return selected['id'] + ': corrección registrada; revisaré las acciones del mismo objetivo.'

    def _target_writes(self, task, target_step):
        attempt = (task.get('recipe_attempts') or [None])[-1]
        return [effect for receipt in (attempt or {}).get('receipts', [])
                if receipt.get('step_id') == target_step
                for effect in receipt.get('external_effects', [])
                if effect.get('effect') == 'write']

    def _challenged_writes(self, task, target_step=None):
        if target_step:
            writes = self._target_writes(task, target_step)
        elif task.get('single_attempts'):
            writes = [effect for effect in task['single_attempts'][-1].get('external_effects', [])
                      if effect.get('effect') == 'write']
        else:
            attempt = (task.get('recipe_attempts') or [{}])[-1]
            writes = [effect for receipt in attempt.get('receipts', [])
                      for effect in receipt.get('external_effects', [])
                      if effect.get('effect') == 'write']
        request_id = task.get('effect_target_request_id')
        return ([effect for effect in writes if effect.get('request_id') == request_id]
                if request_id else writes)

    def _effect_decision_options(self, task, constraints=None, target_step=None,
                                 target_request_id=None):
        writes = self._challenged_writes(task, target_step)
        if target_request_id is not None:
            writes = [effect for effect in writes
                      if effect.get('request_id') == target_request_id]
        create = (len(writes) == 1
                  and (writes[0].get('effect_contract') or {}).get('kind') == 'create'
                  and not (writes[0].get('effect_contract') or {}).get('batch')
                  and all(writes[0].get(key) for key in ('service_id', 'operation', 'version')))
        actions = (['update'] if constraints else []) + (['create'] if create else []) + ['cancel']
        return {str(index): action for index, action in enumerate(actions, 1)}

    @staticmethod
    def _effect_options_message(options):
        actions = set(options.values())
        if actions == {'update', 'create', 'cancel'}:
            return ('1 actualizar el recurso registrado, 2 crear uno nuevo '
                    '(puede duplicarse), o 3 cancelar. ¿Cuál eliges?')
        if actions == {'create', 'cancel'}:
            return '1 permite crear otro recurso (puede duplicarse), o 2 cancela.'
        if actions == {'update', 'cancel'}:
            return '1 permite actualizar el recurso registrado, o 2 cancela.'
        return 'No hay creación ni actualización verificable; responde 1 para cancelar.'

    def _reject_unverified_recipe_versions(self, task):
        """Keep candidate evidence, but do not reuse it after an unresolved effect."""
        try:
            for version_id in task.get('candidate_rejection_pending', []):
                version = self.manager.hand.version(version_id)
                if version and version['status'] in {'draft', 'approved'}:
                    self.manager.hand.reject(version_id, 'Unresolved external effect')
            self.store.update(task['id'], expected={'UNFULFILLABLE'},
                              candidate_rejection_pending=[])
        except Exception:
            logger.exception('Candidate rejection remains pending for %s', task['id'])
            return

    def _correction_constraints(self, task, target_step=None, target_request_id=None):
        """Pin only a challenged recipe effect, leaving other receipts provisional."""
        if target_step is not None:
            effects = self._target_writes(task, target_step)
        else:
            effects = [effect for attempt in task.get('recipe_attempts', [])
                       for receipt in attempt.get('receipts', [])
                       for effect in receipt.get('external_effects', [])]
            effects += [effect for attempt in task.get('single_attempts', [])
                        for effect in attempt.get('external_effects', [])]
        writes = [effect for effect in effects if effect.get('effect') == 'write']
        if target_request_id is not None:
            writes = [effect for effect in writes
                      if effect.get('request_id') == target_request_id]
            if len(writes) != 1:
                return [], 'No hay una escritura única para el recibo seleccionado'
        elif target_step is not None and len(writes) > 1:
            return [], 'El paso cuestionado tiene varias escrituras registradas'
        if not writes or (target_step is None and task.get('new_effect_authorization')):
            return [], None
        services = (self.manager.voice_services.active_services()
                    if getattr(self.manager, 'voice_services', None) else {})
        constraints = []
        for effect in writes:
            contract = effect.get('effect_contract') or {}
            correction = contract.get('correction')
            resource_id = effect.get('resource_id')
            if effect.get('status') != 'completed' or not resource_id or not correction:
                return [], 'Una escritura anterior no tiene identidad y corrección verificadas'
            operation = services.get(correction['service_id'], {}).get('operations', {}).get(
                correction['operation'], {})
            if (operation.get('version') != correction['version']
                    or operation.get('effect') != 'write'
                    or correction['identity_parameter'] not in operation.get('parameters', [])
                    or ('questions' in correction and
                        set(correction['questions']) !=
                        set(operation.get('parameters', [])) - {correction['identity_parameter']})):
                return [], 'La operación correctiva declarada ya no está aprobada o disponible'
            constraints.append({**correction, 'resource_id': resource_id})
        return constraints, None

    def _resume_recipe_action(self, task, resolved, latest):
        """Keep proven earlier steps when the challenged suffix has no write receipt."""
        attempt = (task.get('recipe_attempts') or [None])[-1]
        if not attempt or attempt.get('recipe') != task.get('recipe'):
            return False
        recipe = task['recipe']
        classification = self.revision_planner.target(
            task.get('original_request') or task['request'], latest['answer'],
            recipe, attempt['receipts'])
        target = classification['step_id']
        if target is None:
            options = ', '.join(step['id'] for step in recipe['steps'])
            self.store.update(task['id'], expected={'RESOLVING_SCOPE'},
                              state='WAITING_SCOPE',
                              scope_questions=[f'¿Qué acción debo corregir: {options}?'])
            self.notify(f"{task['id']}: indica qué acción debo corregir: {options}.")
            return True
        index = next(i for i, step in enumerate(recipe['steps']) if step['id'] == target)
        by_id = {receipt['step_id']: receipt for receipt in attempt['receipts']}
        if any(step['id'] not in by_id for step in recipe['steps'][:index]):
            return False
        old_inputs = task.get('recipe_input') or {}
        inputs = dict(old_inputs, **resolved['params'])
        if not validate_params(recipe['input_schema'], inputs):
            return False
        for step in recipe['steps'][:index]:
            for binding in step['bindings'].values():
                if binding.startswith('input.'):
                    field = binding.split('.', 1)[1]
                    if inputs.get(field) != old_inputs.get(field):
                        return False
        suffix = recipe['steps'][index:]
        active_services = (self.manager.voice_services.active_services()
                           if getattr(self.manager, 'voice_services', None) else {})
        for step in suffix:
            receipt = by_id.get(step['id'])
            if receipt and any(effect.get('effect') == 'write'
                               for effect in receipt.get('external_effects', [])):
                if classification['problem'] in {'missing', 'incorrect'}:
                    writes = self._target_writes(task, step['id'])
                    if classification['problem'] == 'incorrect' and len(writes) == 1:
                        constraints, issue = self._correction_constraints(
                            task, target_step=step['id'])
                        questions = (constraints[0].get('questions', {})
                                     if constraints and not issue else {})
                        if questions:
                            token = current_budget.set(
                                lambda: self.store.consume(task['id'], 'llm_calls'))
                            try:
                                identified = self.revision_planner.effect_inputs(
                                    latest['answer'], questions)
                            finally:
                                current_budget.reset(token)
                            if identified is None:
                                question = '¿Qué parte del recurso debería cambiar y cómo debería quedar?'
                                self.store.update(task['id'], expected={'RESOLVING_SCOPE'},
                                    state='WAITING_EFFECT_DETAIL', effect_target_step=step['id'],
                                    effect_question=question,
                                    effect_pending_answer=latest['answer'])
                                self.notify(f"{task['id']}: {question}")
                                return True
                            missing = [field for field in identified['required']
                                       if field not in identified['provided']]
                            if missing:
                                question = questions[missing[0]]['question']
                                self.store.update(task['id'], expected={'RESOLVING_SCOPE'},
                                    state='WAITING_EFFECT_DETAIL', effect_target_step=step['id'],
                                    effect_question=question,
                                    effect_required_fields=identified['required'],
                                    effect_provided_fields=identified['provided'],
                                    effect_pending_answer=latest['answer'])
                                self.notify(f"{task['id']}: {question}")
                                return True
                        options = self._effect_decision_options(
                            task, constraints if not issue else None, step['id'])
                        self.store.update(task['id'], expected={'RESOLVING_SCOPE'},
                            state='WAITING_EFFECT_DECISION', effect_target_step=step['id'],
                            effect_options=options, pending_effect_constraints=constraints,
                            effect_issue=issue,
                            effect_provided_fields=(identified['provided'] if questions else {}))
                        self.notify(f"{task['id']}: " +
                                    self._effect_options_message(options))
                        return True
                    if classification['problem'] == 'incorrect' and len(writes) > 1:
                        question = '¿Cuál de las escrituras de esta acción debo corregir?'
                        self.store.update(task['id'], expected={'RESOLVING_SCOPE'},
                            state='WAITING_EFFECT_DETAIL', effect_target_step=step['id'],
                            effect_question=question, effect_first_detail=latest['answer'])
                        self.notify(f"{task['id']}: {question}")
                        return True
                    question = (f'Voice registró una escritura en {step["id"]}. '
                                '¿Qué observaste en el servicio y qué resultado esperabas? '
                                'Con esa información te ofreceré la acción correctiva aplicable.')
                    self.store.update(task['id'], expected={'RESOLVING_SCOPE'},
                                      state='WAITING_EFFECT_DETAIL',
                                      effect_target_step=step['id'], effect_question=question,
                                      effect_issue='Voice registró una escritura; su efecto remoto debe aclararse')
                    self.notify(f"{task['id']}: {question}")
                    return True
                return False
            references = (receipt or {}).get('services', [])
            if not references:
                version = self.manager.hand.catalog.get(step['version_id'])
                references = version['spec']['services'] if version else []
            write_possible = any(
                active_services.get(ref['service_id'], {}).get('operations', {})
                .get(ref['operation'], {}).get('effect') == 'write' for ref in references)
            if write_possible and not getattr(self.manager, 'gateway', None):
                return False
        prefix_receipts = [by_id[step['id']] for step in recipe['steps'][:index]]
        outputs = {'input': inputs}
        outputs.update({receipt['step_id']: receipt['output'] for receipt in prefix_receipts})
        revisions = list(task.get('scope_revisions', []))
        revisions.append({'number': len(revisions) + 1, 'purpose': resolved['purpose'],
                          'required_output': resolved['required_output'],
                          'params': inputs, 'owner_source': latest['source']})
        self.store.update(task['id'], expected={'RESOLVING_SCOPE'}, state='RECIPE_STEP',
                          request=resolved['purpose'], recipe_input=inputs,
                          recipe_outputs=outputs, recipe_receipts=prefix_receipts,
                          recipe_step_index=index, recipe_result=None,
                          reused_step_receipt=by_id.get(target),
                          scope_revisions=revisions, goal_revision=max(task.get('goal_revision', 0), len(revisions)),
                          scope_questions=[], effect_constraints=[])
        self.notify(f"{task['id']}: revisaré desde la acción {target}; "
                    "las anteriores conservan sus recibos y esperan tu aceptación final.")
        return True

    def cancel(self, task_id, source):
        def apply(task):
            if (task['state'] in {'FAILED', 'CANCELLED'} and not self._agent_active(task)
                    or task['state'] == 'COMPLETED' and task.get('mode') != 'AGENT'):
                return
            if task.get('execution_id'):
                task['runtime_status'] = 'cancelling'
                if task['state'] != 'COMPLETED':
                    task['state'] = 'CANCELLING'
            else:
                task['state'] = 'CANCELLED'
        task = self.store.command(source, task_id, apply)
        if task and task.get('runtime_status') == 'cancelling':
            try:
                self._poll_stop(task)
            except requests.RequestException:
                pass  # Durable cancellation is retried by tick.
        return self.store.get(task_id) if task else None

    @staticmethod
    def _agent_active(task):
        return (task.get('mode') == 'AGENT' and task.get('execution_id')
                and task.get('runtime_status', (task.get('result') or {}).get('status'))
                in {'running', 'started', 'cancelling'})

    def _poll_stop(self, task):
        result = self.proxy.cancel(task['execution_id'])
        if result.get('status') not in {'cancelled', 'unknown', 'finished', 'failed', 'timeout'}:
            return
        current = self.store.observe_execution(task, result)
        if current and current['state'] == 'CANCELLING':
            self.store.update(task['id'], expected={'CANCELLING'},
                              state='UNKNOWN' if result['status'] == 'unknown' else 'CANCELLED')
        if current:
            self.store.event(task['id'], 'service_stop_result', result)
            pending = current.get('pending_runtime_feedback')
            if pending and result['status'] != 'unknown' and current['state'] == 'WAITING_FEEDBACK':
                token = current_question_revision.set(None)
                try:
                    self._record_scope_refinement(pending['source'] + ':stopped', current, pending['feedback'])
                finally:
                    current_question_revision.reset(token)
            self.notify('No pude confirmar la parada; las nuevas peticiones siguen bloqueadas.'
                        if result['status'] == 'unknown' else 'Parada del servicio confirmada.')

    def _supervise_agent(self, task):
        if not self._agent_active(task) or task['state'] in {'RUNNING', 'CANCELLING'}:
            return
        if task.get('runtime_status') == 'cancelling':
            self._poll_stop(task)
            return
        if time.time() >= task['deadline'] or task['state'] not in {
                'UNVERIFIED', 'WAITING_FEEDBACK', 'VERIFYING', 'COMPLETED'}:
            self.cancel(task['id'], 'runtime-stop:' + task['execution_id'])
            return
        result = self.proxy.get(task['execution_id'])
        current = self.store.observe_execution(task, result)
        if not current:
            return
        if result.get('status') in {'running', 'started'}:
            gateway = getattr(self.manager, 'gateway', None)
            if gateway:
                gateway.process_mailbox(current, self.root/'workspaces'/task['id']/task['artifact_hash'])
        else:
            self.store.event(task['id'], 'service_stopped', result)
            self.notify(f"{task['id']}: estado del servicio: {result.get('status')}. "
                        'La aceptación conserva la evidencia observada en su momento.')

    def retry_task(self, task_id, authorization_id=None, corrective_instruction=None,
                   proposed_steps=None, order=None):
        from app.mind.recovery_attempt import build_attempt
        if (not order or order.get('plan', {}).get('task_id') != task_id
                or order.get('header', {}).get('authorization_id') != authorization_id):
            raise PermissionError('An exact Heart order is required; an authorization ID alone is insufficient')
        plan = order['plan']
        if (corrective_instruction is not None and corrective_instruction != plan['corrective_instruction']
                or proposed_steps is not None and proposed_steps != plan['proposed_steps']):
            raise PermissionError('Remediation instructions differ from the approved plan')
        retried = self.manager.hand.remediate_task(order, lambda task: build_attempt(task, order))
        self.notify('Reparacion autorizada programada; su resultado sigue pendiente de verificacion.')
        return retried

    def approval(self, task_id, token, approve, source):
        def apply(task):
            pending = task.get('approval', {})
            if task['state'] != 'WAITING_APPROVAL' or not secrets.compare_digest(pending.get('token', ''), token):
                raise ValueError('AprobaciÃ³n desconocida u obsoleta')
            if time.time() > pending['expires'] or pending['artifact_hash'] != task['artifact_hash']:
                raise ValueError('AprobaciÃ³n vencida o artefacto cambiado')
            if digest(Path(task['artifact']).read_bytes()) != pending['artifact_hash']:
                raise ValueError('El archivo cambiÃ³ despuÃ©s de la revisiÃ³n')
            if pending.get('scope') is not None and pending['scope'] != scope_for(task):
                raise ValueError('El alcance cambió después de solicitar aprobación')
            if pending.get('version_id') != task.get('version_id'):
                raise ValueError('La versión cambió después de solicitar aprobación')
            if pending.get('skill_hash') is not None:
                version = self.manager.hand.version(task['version_id'])
                if version['artifact_hash'] != pending['skill_hash']:
                    raise ValueError('La versión de la skill cambió después de solicitar aprobación')
            if not approve:
                task['state'] = 'CANCELLED'
                return
            task['authorized_hash'] = task['artifact_hash']
            if pending.get('operation'):
                task.setdefault('grants', []).append(pending['operation'])
            task['state'] = 'READY'
        decided = self.store.command(source, task_id, apply)
        if (approve and decided.get('version_id') and not (decided.get('approval') or {}).get('operation')
                and self.manager.pixy.skill_approvals):
            version = self.manager.hand.version(decided['version_id'])
            if version['status'] in {'active', 'draft'}:
                self.manager.pixy.skill_approvals.approve(decided, version, source)
        return decided

    def verify(self, task_id, execution_id, success, source):
        gateway = getattr(self.manager, 'gateway', None)
        snapshot = self.store.get(task_id)
        receipts = (gateway.technical_receipts(task_id, execution_id, snapshot.get('services', []))
                    if gateway and hasattr(gateway, 'technical_receipts') else [])
        unresolved = any(item.get('effect') == 'write' and item.get('status') != 'completed'
                         for item in receipts)
        def apply(task):
            if task['state'] != 'UNVERIFIED' or task.get('execution_id') != execution_id:
                raise ValueError('La verificaciÃ³n no corresponde al resultado pendiente')
            if success and (task.get('batch_incomplete') or unresolved):
                raise PermissionError('hay efectos externos sin confirmar')
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
        scope = scope_for(task)
        version_id = task.get('version_id')
        skill_hash = (self.manager.hand.version(version_id)['artifact_hash']
                      if version_id and hasattr(self.manager, 'hand') else None)
        approval = dict(token=secrets.token_hex(12), artifact_hash=task['artifact_hash'], scope=scope,
                        version_id=version_id, skill_hash=skill_hash,
                        expires=min(task['deadline'], time.time() + 900), reason=reason, operation=operation)
        self.store.update(task['id'], expected={task['state']}, state='WAITING_APPROVAL', approval=approval)
        self.notify(f"Permiso requerido para ejecutar: {task.get('original_request') or task['request']}.\n"
                    f"{reason}\n¿Autorizas esta ejecución? Responde sí o no.")

    def tick(self, background=False):
        self._collect_llm_jobs()
        self._schedule_mailbox()
        self.store.flush_events()
        all_tasks = self.store.all()
        task_order = {task['id']: index for index, task in enumerate(all_tasks)}
        in_flight_states = {'ROUTING', 'SCOPE_PLANNING', 'RESOLVING_SCOPE', 'GENERATING', 'REVIEWING',
                            'SUBMITTED', 'READY', 'RUNNING', 'VERIFYING', 'DIAGNOSING'}
        active_by_conversation = {}
        for t in all_tasks:
            cid = t.get('conversation_id')
            if cid and t.get('state') in in_flight_states:
                active_by_conversation.setdefault(cid, []).append(t)

        for task in all_tasks:
            goal_token = current_goal_revision.set((task['id'], task.get('goal_revision', 0)))
            try:
                self._supervise_agent(task)
            except requests.RequestException:
                pass  # Dispatch requires a current runtime observation.
            finally:
                current_goal_revision.reset(goal_token)
            task = self.store.get(task['id'])
            if task['state'] in TERMINAL:
                if (task['state'] == 'UNFULFILLABLE'
                        and task.get('candidate_rejection_pending')):
                    self._reject_unverified_recipe_versions(task)
                continue
            if task['state'] == 'WAITING_APPROVAL' and time.time() < task['deadline']:
                if time.time() > task['approval']['expires']:
                    self.store.update(task['id'], expected={'WAITING_APPROVAL'}, state='EXHAUSTED', error='Aprobación vencida')
                continue
            if task['state'] == 'WAITING_CREATION_APPROVAL':
                if time.time() >= task['deadline']:
                    self.store.update(task['id'], expected={'WAITING_CREATION_APPROVAL'}, state='EXHAUSTED', error='Aprobación de creación vencida')
                continue
            if task['state'] == 'WAITING_SCOPE':
                if time.time() >= task['deadline']:
                    self.store.update(task['id'], expected={'WAITING_SCOPE'}, state='EXHAUSTED', error='Aclaración vencida')
                continue
            if task['state'] == 'WAITING_FEEDBACK':
                if time.time() >= task['deadline']:
                    self.store.update(task['id'], expected={'WAITING_FEEDBACK'}, state='EXHAUSTED', error='Feedback vencido')
                continue
            cid = task.get('conversation_id')
            if cid and task['state'] == 'ROUTING':
                active_tasks = active_by_conversation.get(cid, [])
                older_active = next((t for t in active_tasks if t['id'] != task['id']
                                     and task_order[t['id']] < task_order[task['id']]), None)
                if older_active:
                    continue
            if background and task['state'] in {'ROUTING', 'SCOPE_PLANNING', 'RESOLVING_SCOPE', 'GENERATING', 'REVIEWING'}:
                if task['id'] not in self._llm_jobs:
                    self._llm_jobs[task['id']] = self._llm_executor.submit(self._run_task, task)
                continue
            self._run_task(task)
        if hasattr(self.manager, 'heart') and hasattr(self.manager.heart, 'recovery') and self.manager.heart.recovery:
            try:
                for case in self.manager.heart.recovery.tick():
                    cid, state = case['case_id'], case['state']
                    self.manager.heart.recovery.mark_reported(cid)
                    err = f": {case['error']}" if case.get('error') else ""
                    self.notify(f"Pixy: Caso de recuperación {cid} finalizado con estado {state}{err}.")
            except Exception:
                logger.exception('Error en ciclo de recuperación de Heart')
        self.store.flush_events()

    def advance(self, task):
        tid, state = task['id'], task['state']
        trace = task.get('trace_id') or tid
        token = current_goal_revision.set((tid, task.get('goal_revision', 0)))
        try:
            with trace_scope(trace):
                self._advance_scoped(task)
        finally:
            current_goal_revision.reset(token)

    def _services_available(self, refs):
        port = getattr(self.manager, 'voice_services', None)
        if not refs:
            return True
        if port is None:
            return False
        active = port.active_services()
        return all(active.get(ref['service_id'], {}).get('operations', {})
                   .get(ref['operation'], {}).get('version') == ref['version'] for ref in refs)

    def _setup_service(self, request, manifest, step_id=None):
        """Stage a concrete provider before asking for the one-time setup decision."""
        descriptor, descriptor_hash = self.manager.hand.describe_service({
            'request': request, 'manifest': manifest})
        return {'step_id': step_id, 'hash': descriptor_hash,
                'effect': descriptor['effect'],
                'host': urlsplit(descriptor['url']).hostname,
                'name': (descriptor.get('effect_contract') or {}).get('summary', {}).get('name')
                        or descriptor['operation'],
                'ref': {'service_id': descriptor['service_id'],
                        'operation': descriptor['operation'], 'version': descriptor['version']}}

    @staticmethod
    def _setup_message(task, proposals):
        requested = (task.get('original_request') or task['request'])[:500]
        missing = task.get('recipe_missing_specs') or {}

        descriptions = [action['description'] for action in missing.values() if action.get('description')]
        skill_desc = '; '.join(descriptions) if descriptions else (
            task.get('planned_skill_spec', {}).get('description')
            or (task.get('manifest') or {}).get('purpose')
            or ''
        )
        clean_desc = skill_desc.strip()
        if clean_desc.lower() == requested.lower() or clean_desc.startswith("Capacidad reutilizable para"):
            clean_desc = ""

        caps = list((task.get('manifest') or {}).get('capabilities') or [])
        for action in missing.values():
            for c in action.get('capabilities', []):
                if c not in caps:
                    caps.append(c)

        network_items = [f"{item['name']} mediante {item['host']}" for item in (proposals or [])]
        access_parts = []
        if network_items:
            access_parts.append(', '.join(network_items))
        for c in caps:
            if c != 'Network' and c not in access_parts:
                access_parts.append(c)

        access_text = f" con acceso a {', '.join(access_parts)}" if access_parts else ""
        action_text = f" que {clean_desc}" if clean_desc else ""

        return (f"Para '{requested}' necesito preparar una capacidad nueva{access_text}{action_text}. "
                "¿Autorizas esta configuración para este objetivo y futuros usos de la misma capacidad "
                "y alcance una vez verificada? Responde y/n.")

    def _with_defaults(self, schema, supplied):
        """Use only explicit saved defaults that satisfy the declared input type."""
        values = dict(supplied or {})
        preferences = getattr(self.manager, 'memory', None)
        if preferences and hasattr(preferences, 'get_preference'):
            for field in schema.get('required', []):
                if field not in values:
                    default = preferences.get_preference('default_' + field)
                    candidate = {**values, field: default}
                    if default is not None and validate_params(schema, candidate, allow_missing=True):
                        values[field] = default
        return values

    def _advance_scoped(self, task):
        tid, state = task['id'], task['state']
        if (self.store.get(tid)['state'] != state or self.store.get(tid).get('goal_revision', 0) != task.get('goal_revision', 0)):
            return
        if state == 'RECIPE_STEP':
            self._recipe_step(task)
        elif state == 'SCOPE_PLANNING':
            questions = self.requirements.questions(task.get('original_request') or task['request'],
                                                    task.get('missing_input_schema'))
            if task.get('missing_input_schema'):
                schema = task['missing_input_schema']
                values = task.get('recipe_input') if task.get('recipe') else task.get('params')
                missing = sorted(set(schema.get('required', [])) - set(values or {}))
                if missing and not questions:
                    questions = ['¿Qué valores corresponden a ' + ', '.join(missing) + '?']
            if questions:
                self.store.update(tid, expected={'SCOPE_PLANNING'}, state='WAITING_SCOPE',
                                  scope_questions=questions)
                self.notify(f"{tid}: " + ' '.join(questions))
            else:
                revisions = list(task.get('scope_revisions', []))
                revisions.append({'number': len(revisions) + 1, 'purpose': task['manifest']['purpose'],
                                  'required_output': [],
                                  'params': task.get('recipe_input') if task.get('recipe') else task.get('params', {}),
                                  'owner_source': (task.get('creation_consent') or {}).get('source')})
                self.store.update(tid, expected={'SCOPE_PLANNING'},
                                  state='RECIPE_STEP' if task.get('recipe') else 'GENERATING',
                                  scope_revisions=revisions, goal_revision=max(task.get('goal_revision', 0), len(revisions)))
        elif state == 'RESOLVING_SCOPE':
            latest = task['scope_answers'][-1]
            answer_history = '\n'.join(entry['answer'] for entry in task['scope_answers'])
            resolved = self.requirements.resolve(task.get('original_request') or task['request'],
                                                 latest['questions'], answer_history,
                                                 (task.get('scope_revisions') or [None])[-1])
            if (task.get('recipe') and task.get('recipe_attempts')
                    and not resolved['missing_questions']
                    and not task.get('revision_action_decision')
                    and self._resume_recipe_action(task, resolved, latest)):
                return
            if task.get('revision_action_decision') == 'update':
                constraints, effect_issue = task.get('effect_constraints') or [], None
            else:
                constraints, effect_issue = self._correction_constraints(task)
            if effect_issue:
                options = self._effect_decision_options(task)
                self.store.update(tid, expected={'RESOLVING_SCOPE'},
                                  state='WAITING_EFFECT_DECISION', effect_issue=effect_issue,
                                  effect_options=options,
                                  pending_effect_constraints=[])
                self.notify(f"{tid}: {effect_issue}. No repetiré la escritura. "
                            + self._effect_options_message(options))
                return
            if task.get('recipe'):
                if resolved['missing_questions']:
                    self.store.update(tid, expected={'RESOLVING_SCOPE'}, state='WAITING_SCOPE',
                                      scope_questions=resolved['missing_questions'])
                    self.notify(f"{tid}: " + ' '.join(resolved['missing_questions']))
                    return
                revisions = list(task.get('scope_revisions', []))
                revisions.append({'number': len(revisions) + 1, 'purpose': resolved['purpose'],
                                  'required_output': resolved['required_output'],
                                  'params': resolved['params'], 'owner_source': latest['source']})
                self.store.update(tid, expected={'RESOLVING_SCOPE'}, state='ROUTING',
                                  request=resolved['purpose'], recipe=None,
                                  routing_intent={'action': 'ACTION_REQUIRED', 'extracted_goal': resolved['purpose']},
                                  scope_revisions=revisions, goal_revision=max(task.get('goal_revision', 0), len(revisions)),
                                   scope_questions=[], missing_input_schema=None,
                                   effect_constraints=constraints)
                # intermediate progress suppressed
                return
            if task.get('missing_input_schema'):
                values = task.get('recipe_input') if task.get('recipe') else task.get('params')
                merged = dict(values or {}, **resolved['params'])
                schema = task['missing_input_schema']
                if not validate_params(schema, merged):
                    missing = sorted(set(schema.get('required', [])) - set(merged))
                    if missing:
                        resolved['missing_questions'].append(
                            '¿Qué valores corresponden a ' + ', '.join(missing) + '?')
                    else:
                        resolved['missing_questions'].append(
                            'Los valores no cumplen los tipos esperados. Indica nuevamente: ' +
                            ', '.join(schema.get('required', [])))
                resolved['params'] = merged
            if resolved['missing_questions']:
                self.store.update(tid, expected={'RESOLVING_SCOPE'}, state='WAITING_SCOPE',
                                  scope_questions=resolved['missing_questions'])
                self.notify(f"{tid}: " + ' '.join(resolved['missing_questions']))
            else:
                revisions = list(task.get('scope_revisions', []))
                revisions.append({'number': len(revisions) + 1, 'purpose': resolved['purpose'],
                                  'required_output': resolved['required_output'],
                                  'params': resolved['params'], 'owner_source': latest['source']})
                manifest = dict(task['manifest'], purpose=resolved['purpose'])
                if task.get('recipe') and task.get('execution_id') is None:
                    self.store.update(tid, expected={'RESOLVING_SCOPE'}, state='RECIPE_STEP',
                                      request=resolved['purpose'], manifest=manifest,
                                      recipe_input=resolved['params'],
                                      recipe_outputs={'input': resolved['params']},
                                      scope_revisions=revisions, goal_revision=max(task.get('goal_revision', 0), len(revisions)),
                                      scope_questions=[])
                    # intermediate progress suppressed
                else:
                    needs_setup = task.get('revision_needs_creation_approval', False)
                    next_state = 'ROUTING' if constraints or needs_setup else 'GENERATING'
                    self.store.update(tid, expected={'RESOLVING_SCOPE'}, state=next_state,
                                      request=resolved['purpose'], manifest=manifest,
                                      routing_intent={'action': 'ACTION_REQUIRED', 'extracted_goal': resolved['purpose']},
                                      params=resolved['params'], scope_revisions=revisions,
                                       goal_revision=max(task.get('goal_revision', 0), len(revisions)), scope_questions=[],
                                       effect_constraints=constraints)
                    # intermediate progress suppressed
        elif state == 'ROUTING':
            request = task['request']
            direct = request.startswith(('/make ', '/do '))
            if direct:
                request = request.split(' ', 1)[1]
            recent = [item for item in self.store.all() if item['id'] != tid
                      and item.get('kind') != 'turn'
                      and item.get('conversation_id') == task.get('conversation_id')][-8:]
            task_context = [{key: item.get(key, 0 if key == 'goal_revision' else None) for key in (
                'id', 'request', 'state', 'goal_revision', 'routing_action', 'executions', 'execution_id')}
                for item in recent]
            for item in task_context:
                item['request'] = str(item['request'])[:1000]
            sid = task.get('conversation_id')
            history = self.conversations.history(sid, task['source']) if sid else None
            questions = [{key: question[key] for key in ('id', 'kind', 'subject_id', 'revision', 'state')}
                         for question in self.conversations.pending(sid)[:8]] if sid else []
            intent = task.get('routing_intent')
            if intent is None:
                intent = ({'action': 'ACTION_REQUIRED', 'extracted_goal': request} if direct else
                          self.manager.mind.process_message(request, task_context=task_context, persist=False,
                                                           conversation_history=history, questions=questions))
            if intent.get('error'):
                self.store.update(tid, expected={'ROUTING'}, state='FAILED', error=str(intent['error']))
                if intent.get('reply') is not None and intent['reply'].strip():
                    self.notify(intent['reply'])
                elif intent.get('reply') is None:
                    self.notify('No pude interpretar la solicitud; no ejecuté ninguna acción.')
                return
            action = intent.get('action')
            if action not in {'GENERAL_CHAT', 'ACTION_REQUIRED', 'TOOL_CALL'}:
                raise ValueError('Operaci?n de enrutamiento desconocida')
            if not task.get('routing_intent'):
                self.store.update(tid, expected={'ROUTING'}, routing_intent=intent)
            if action == 'TOOL_CALL':
                reply = self.dispatch_tool(task, intent['tool_call'])
                bound = self.conversations.turn(task['source'])
                response_task_id = (bound['task_id'] if bound and bound['task_id'] != tid
                                    and intent['tool_call']['name'] == 'answer_question' else None)
                self.store.update(tid, expected={'ROUTING'}, state='COMPLETED',
                                  routing_action='TOOL_CALL', tool_result=reply, response_task_id=response_task_id)
                if reply:
                    self.notify(reply)
                return
            if action == 'ACTION_REQUIRED':
                request = intent.get('extracted_goal') or request
                self.store.update(tid, expected={'ROUTING'}, kind='objective', request=request,
                                  original_request=request, source_request=task.get('source_request') or task['request'],
                                  requested_capability=intent.get('capability'),
                                  requested_provider=intent.get('provider'),
                                  explicit_creation=bool(intent.get('creation_requested')))
                if intent.get('tool_call'):
                    call_id = self.conversations.record_call(task['source'], intent['tool_call'])
                    self.conversations.complete(task['source'], {'call_id': call_id,
                        'tool_call': intent['tool_call'], 'status': 'started', 'task_id': tid})
            if action == 'ACTION_REQUIRED' and not direct:
                planner = getattr(self.manager, 'action_planner', None)
                if planner is not None:
                    proposal = planner.propose(intent.get('extracted_goal') or request, lambda spec:
                        spec['runtime'] != 'AGENT' and not spec['dependencies']
                        and self._services_available(spec['services']),
                        correction_context=task.get('effect_constraints'),
                        conversation_context=(history if history is not None else self.manager.memory.get_chat_history(limit=12)),
                        task_context=task_context)
                    if proposal:
                        recipe = proposal['recipe']
                        missing = proposal['missing_specs']
                        inputs = self._with_defaults(recipe['input_schema'], proposal['inputs'])
                        missing_values = bool(set(recipe['input_schema'].get('required', [])) -
                                              set(inputs))
                        consent = task.get('creation_consent') or {}
                        consent_needed = bool(missing and (
                            not consent or not {action['skill_id'] for action in missing.values()}
                            <= set(consent.get('allowed_skill_ids', []))))
                        setup = []
                        if consent_needed:
                            for step_id, action in missing.items():
                                if 'Network' in action['capabilities']:
                                    if action.get('services'):
                                        active = self.manager.voice_services.active_services()
                                        for ref in action['services']:
                                            operation = active.get(ref['service_id'], {}).get(
                                                'operations', {}).get(ref['operation'])
                                            if not operation or operation['version'] != ref['version']:
                                                raise ValueError('La operación correctiva ya no está activa')
                                            setup.append({'step_id': step_id, 'hash': None,
                                                'effect': operation['effect'],
                                                'host': urlsplit(operation['url']).hostname,
                                                'name': (operation.get('effect_contract') or {})
                                                    .get('summary', {}).get('name') or ref['operation'],
                                                'ref': ref})
                                    else:
                                        setup.append(self._setup_service(
                                            action['description'],
                                            {'purpose': action['description'],
                                             'capabilities': action['capabilities']}, step_id))
                        next_state = ('WAITING_CREATION_APPROVAL' if consent_needed else
                                      'SCOPE_PLANNING' if missing_values else 'RECIPE_STEP')
                        routed = self.store.update(tid, expected={'ROUTING'}, state=next_state,
                            request=request, original_request=task.get('original_request') or request,
                            recipe=recipe, recipe_input=inputs,
                            recipe_outputs={'input': inputs},
                            recipe_missing_specs=missing, recipe_receipts=[], recipe_step_index=0,
                            setup_proposals=setup if consent_needed else task.get('setup_proposals', []),
                            missing_input_schema=recipe['input_schema'] if missing_values else None,
                            manifest={'purpose': request, 'capabilities': []},
                            routing_action='ACTION_REQUIRED')
                        self.manager.memory.append_long_term('user_chat', task.get('source_request') or task['request'])
                        if consent_needed:
                            self.notify(self._setup_message(routed, setup))
                        return
                if task.get('effect_constraints'):
                    options = self._effect_decision_options(task)
                    self.store.update(tid, expected={'ROUTING'},
                                      state='WAITING_EFFECT_DECISION',
                                      effect_issue='No encontré una composición correctiva verificable',
                                      effect_options=options,
                                      pending_effect_constraints=[])
                    self.notify(f"{tid}: no encontré una composición correctiva verificable. "
                                "No repetiré la escritura. " +
                                self._effect_options_message(options))
                    return
            selector = getattr(self.manager, 'skill_selector', None)
            if action == 'ACTION_REQUIRED' and selector is not None and hasattr(self.manager, 'hand'):
                capability = intent.get('capability') or task.get('requested_capability')
                provider = intent.get('provider') or task.get('requested_provider')
                if capability and not provider:
                    preference = self.manager.memory.get_preference('default_provider_' + capability)
                    provider = preference.get('provider') if isinstance(preference, dict) else preference
                    available = self.manager.hand.catalog.providers(capability)
                    if not provider and available:
                        self.store.update(tid, expected={'ROUTING'}, state='COMPLETED',
                                          routing_action='GENERAL_CHAT')
                        reply = ('Â¿QuÃ© proveedor quieres usar para ' + capability + '? Opciones disponibles: '
                                 + ', '.join(available) + '. TambiÃ©n puedes aprobar uno como predeterminado.')
                        self.manager.memory.append_long_term('khai_chat', reply)
                        self.notify(reply)
                        return
                selected = selector.select(request, lambda spec:
                    spec['runtime'] in {'SAFE', 'AGENT', 'GOD'}
                    and not spec['dependencies'] and self._services_available(spec['services']),
                    capability=capability, provider=provider)
                if selected is not None:
                    version = self.manager.hand.version(selected['version_id'])
                    if version['status'] == 'active':
                        manifest = json.loads(version['manifest'])
                        if not self.manager.pixy.preflight(manifest)['safe']:
                            raise PermissionError('Pixy rejected the reused capability')
                        needs_values = bool(selected.get('missing_params'))
                        spec = self.manager.hand.catalog.get(version['version_id'])['spec']
                        params = self._with_defaults(spec['input_schema'], selected['params'])
                        needs_values = bool(set(spec['input_schema'].get('required', [])) - set(params))
                        self.store.update(tid, expected={'ROUTING'},
                            state='SCOPE_PLANNING' if needs_values else 'GENERATING', request=request,
                            original_request=request,
                            manifest=manifest, context=manifest.get('purpose', request)[:160],
                            selected_version_id=version['version_id'], params=params,
                            missing_input_schema=spec['input_schema'] if needs_values else None,
                            selection=selected['selection'], skill_name=version['skill_name'],
                            services=spec['services'], routing_action='ACTION_REQUIRED')
                        if not direct:
                            self.manager.memory.append_long_term('user_chat', task.get('source_request') or task['request'])
                        return
            self.store.update(tid, expected={'ROUTING'}, routing_action=action)
            if not direct:
                self.manager.memory.append_long_term('user_chat', task.get('source_request') or task['request'])
            if action == 'ACTION_REQUIRED':
                goal = intent.get('extracted_goal') or request
                manifest = manifest_from_request(goal)
                if not manifest:
                    try:
                        raw = self.manager.llm.generate_content(
                            'Devuelve JSON con purpose (objetivo concreto) y capabilities '
                            '(lista de Network, FileSystem, Database según necesidad). '
                            'Esto declara intención, no autoriza permisos. Solicitud: ' + request,
                            response_format={'type': 'json_object'})
                    except Exception as exc:
                        logger.warning('Fallo generando contenido para manifiesto con LLM: %s', exc)
                        raw = {}
                    manifest = normalize_manifest(raw, default_purpose=goal)
                else:
                    manifest = normalize_manifest(manifest, default_purpose=goal)
                if hasattr(self.manager, 'hand') and not self.manager.pixy.preflight(manifest)['safe']:
                    raise PermissionError('Pixy rejected the objective')
                context = manifest['purpose'][:160]
                hypotheses = [h for h in self.manager.memory.get_active_hypotheses() if h['topic'] == context]
                hypotheses.sort(key=lambda h: h['confidence'], reverse=True)

                if not direct and not task.get('creation_consent') and not intent.get('creation_requested'):
                    setup = ([self._setup_service(goal, manifest)]
                             if 'Network' in manifest['capabilities'] else [])
                    self.store.update(tid, expected={'ROUTING'}, state='WAITING_CREATION_APPROVAL', request=goal,
                                      original_request=request,
                                      setup_proposals=setup,
                                      manifest=manifest, context=context,
                                      hypothesis_id=hypotheses[0]['id'] if hypotheses else None,
                                      alternatives=[h['id'] for h in hypotheses[1:3]], diagnosed=bool(hypotheses))
                    self.notify(self._setup_message(task, setup))
                    return

                self.store.update(tid, expected={'ROUTING'}, state='SCOPE_PLANNING', request=goal,
                                  original_request=request,
                                  requested_capability=intent.get('capability'),
                                  requested_provider=intent.get('provider'),
                                  explicit_creation=bool(intent.get('creation_requested')),
                                  creation_consent=(
                                      {'source': task['source'] + ':explicit-create',
                                       'allowed_capabilities': list(manifest.get('capabilities', [])),
                                       'allowed_services': []}
                                      if intent.get('creation_requested') else task.get('creation_consent')),
                                  manifest=manifest, context=context,
                                  hypothesis_id=hypotheses[0]['id'] if hypotheses else None,
                                  alternatives=[h['id'] for h in hypotheses[1:3]], diagnosed=bool(hypotheses))
                # intermediate progress suppressed
            else:
                self.store.update(tid, expected={'ROUTING'}, state='COMPLETED')
                reply = intent.get('reply') or 'No se ejecutó ninguna acción.'
                self.conversations.complete(task['source'], {'result': reply})
                self.manager.memory.append_long_term('khai_chat', reply)
                self.notify(reply)
        elif state == 'GENERATING':
            selected_version_id = task.get('selected_version_id')
            if not task.get('services') and (task.get('creation_consent') or {}).get('allowed_services'):
                task['services'] = list(task['creation_consent']['allowed_services'])
                self.store.update(tid, expected={'GENERATING'}, services=task['services'])
            if ('Network' in task['manifest'].get('capabilities', [])
                    and not task.get('services') and not selected_version_id):
                try:
                    descriptor, descriptor_hash = self.manager.hand.describe_service(task)
                except AgentExhausted as exc:
                    run_id = (self.manager.hand.last_agent_run_id
                              or getattr(self.manager.hand.agent, 'last_run_id', None))
                    self.manager.memory.record_episode(tid, run_id, task['request'], 'failure',
                                                       'service_research', str(exc),
                                                       {'feedback': task.get('feedback')})
                    failures = task.get('hand_failures', 0) + 1
                    self.store.update(tid, expected={'GENERATING'},
                                      state='DIAGNOSING' if failures >= 2 else 'GENERATING',
                                      feedback={'error': str(exc), 'stage': 'service_research',
                                                'agent_run_id': run_id},
                                      agent_run_id=run_id, hand_failures=failures)
                    return
                self.store.update(tid, expected={'GENERATING'},
                                  agent_run_id=self.manager.hand.last_agent_run_id)
                if descriptor.get('effect') == 'read':
                    self.manager.pixy_services.decide(descriptor_hash, True, 'system:auto-read-policy')
                    ref = {'service_id': descriptor['service_id'], 'operation': descriptor['operation'],
                           'version': descriptor['version']}
                    changes = {'services': [ref]}
                    if task.get('creation_consent'):
                        consent = dict(task['creation_consent'])
                        consent['allowed_services'] = [ref]
                        changes['creation_consent'] = consent
                        task['creation_consent'] = consent
                    self.store.update(tid, expected={'GENERATING'}, **changes)
                    task['services'] = [ref]
                else:
                    self.store.update(tid, expected={'GENERATING'}, state='WAITING_SERVICE_APPROVAL',
                                      service_proposal=descriptor_hash)
                    self.notify(f"{tid}: Hand propone servicio de escritura {descriptor['service_id']}/"
                                f"{descriptor['operation']} {descriptor['version']} ({descriptor['effect']}) "
                                f"en {descriptor['url']}. Resumen que Pixy presenta para revisión: "
                                f"{json.dumps(descriptor['effect_contract']['summary'], ensure_ascii=False)}. "
                                "Comprueba que no incluya datos sensibles. Requiere aprobación humana: "
                                f"/service show {descriptor_hash}; /service approve {descriptor_hash}; "
                                f"/service deny {descriptor_hash}")
                    return

            if not selected_version_id:
                # intermediate progress suppressed
                self.store.consume(tid, 'generations')
            tags = [task.get('context', 'general')] + task['manifest'].get('capabilities', [])
            if hasattr(self.manager, 'rules'):
                rules = [dict(r, id=r['rule_id']) for r in self.manager.rules.active()]
            else:
                eligible = self.store.eligible_rule_ids()
                rules = [r for r in self.manager.memory.get_guidelines_by_tags(tags) if r['id'] in eligible]
            hypothesis = self.manager.memory.get_hypothesis(task['hypothesis_id']) if task.get('hypothesis_id') else None
            try:
                crafter = self.manager.hand
                if selected_version_id:
                    selected_version = self.manager.hand.version(selected_version_id)
                    if selected_version['status'] not in {'active', 'draft'}:
                        raise ValueError('La versión seleccionada ya no está activa')
                    if (selected_version['status'] == 'draft'
                            and not task.get('reuse_authorized_hash')):
                        raise ValueError('La candidata no tiene una aprobación reutilizable')
                    folder = (selected_version['skill_name'] if selected_version['status'] == 'active'
                              else '.' + selected_version['skill_name'])
                    code = (self.manager.hand.root/folder/'app'/'main.py').read_text(encoding='utf-8')
                    mode, deps = skill_metadata(code)
                else:
                    code, mode, deps = crafter.generate(task, rules, hypothesis)
            except (ValueError, SyntaxError, AgentExhausted) as exc:
                run_id = (self.manager.hand.last_agent_run_id
                          or getattr(self.manager.hand.agent, 'last_run_id', None))
                self.manager.memory.record_episode(tid, run_id, task['request'], 'failure',
                                                   'skill_generation', str(exc),
                                                   {'feedback': task.get('feedback')})
                failures = task.get('hand_failures', 0) + 1
                self.store.update(tid, expected={'GENERATING'},
                                  state='DIAGNOSING' if failures >= 2 else 'GENERATING',
                                  feedback={'error': str(exc), 'stage': 'skill_generation',
                                            'agent_run_id': run_id},
                                  agent_run_id=run_id, hand_failures=failures)
                return
            if not selected_version_id:
                self.store.update(tid, expected={'GENERATING'},
                                  agent_run_id=self.manager.hand.last_agent_run_id)
            current = self.store.get(tid)
            if current['state'] != 'GENERATING' or current.get('goal_revision', 0) != task.get('goal_revision', 0):
                raise StaleGoalRevision('La generación terminó para una revisión obsoleta')
            artifact, sha = crafter.snapshot(code)
            skill_spec_hash = None
            if hasattr(self.manager, 'hand'):
                if selected_version_id:
                    name, version = selected_version['skill_name'], selected_version
                    skill_spec_hash = self.manager.hand.catalog.get(selected_version_id)['spec_hash']
                else:
                    name = ((task.get('planned_skill_spec') or {}).get('skill_id') or
                            task.get('skill_name') or 'skill-' + tid.removeprefix('task_'))
                    if task.get('version_id'):
                        prior = self.manager.hand.version(task['version_id'])
                        if prior['status'] in {'draft', 'approved'}:
                            self.manager.hand.reject(prior['version_id'], 'Replaced after diagnosis')
                    version = self.manager.hand.create(
                        name, code, task['manifest'], correlation_id=tid,
                        skill_metadata={'capability': task.get('requested_capability'),
                                        'provider': task.get('requested_provider'),
                                        'aliases': task.get('provider_aliases', [])})
                if task.get('recipe') and task.get('planned_skill_spec'):
                    recipe = json.loads(json.dumps(task['recipe']))
                    recipe['steps'][task['recipe_step_index']]['version_id'] = version['version_id']
                    self.store.update(tid, skill_name=name, version_id=version['version_id'],
                                      recipe=recipe)
                else:
                    self.store.update(tid, skill_name=name, version_id=version['version_id'])
                if version['status'] == 'draft' and hasattr(crafter, 'describe'):
                    spec = crafter.describe(task, name, mode, deps)
                    if spec is not None:
                        skill_spec_hash = crafter.catalog.stage(version['version_id'], spec)
            reused = task.get('reuse_authorized_hash') == sha
            self.store.update(tid, expected={'GENERATING'},
                              state='READY' if reused else 'REVIEWING', code=code,
                              artifact=artifact, artifact_hash=sha, mode=mode,
                              dependencies=deps, grants=[],
                              authorized_hash=sha if reused else None,
                              approval={'artifact_hash': sha} if reused else None,
                              guidelines=[r['id'] for r in rules], skill_spec_hash=skill_spec_hash)
        elif state == 'WAITING_SERVICE_APPROVAL':
            record = self.manager.pixy_services.get(task['service_proposal'])
            if not record:
                raise ValueError('Propuesta de servicio perdida')
            if record['status'] == 'approved':
                spec = record['spec']
                ref = {'service_id': spec['service_id'], 'operation': spec['operation'],
                       'version': spec['version']}
                self.store.update(tid, expected={'WAITING_SERVICE_APPROVAL'}, state='GENERATING',
                                  services=[ref])
            elif record['status'] in {'denied', 'superseded'}:
                self.store.update(tid, expected={'WAITING_SERVICE_APPROVAL'}, state='CANCELLED',
                                  error='Descriptor de servicio rechazado u obsoleto')
        elif state == 'WAITING_SECRET':
            req_ids = task.get('secret_request_ids') or [task['secret_request_id']]
            receipts = [self.manager.pixy_control.secret_request(rid) for rid in req_ids]
            if not all(receipts):
                raise ValueError('Solicitud privada de credencial perdida')
            if all(r['status'] == 'captured' for r in receipts):
                for r in receipts:
                    credential = self.manager.voice_credentials.status(r['env_key'])
                    if (not credential['available'] or
                            credential['version'] < r['expected_version'] + 1):
                        raise ValueError('Recibo de credencial sin versión disponible en Voice')
                self.store.update(tid, expected={'WAITING_SECRET'}, state='READY',
                                  secret_receipt_id=receipts[-1]['receipt_id'])
                self.notify(f'{tid}: credencial recibida por Voice; reanudando tarea.')
            elif any(r['status'] == 'expired' or time.time() >= r['expires_at'] for r in receipts):
                self.store.update(tid, expected={'WAITING_SECRET'}, state='EXHAUSTED',
                                  error='Captura privada de credencial vencida')
        elif state == 'REVIEWING':
            review = self.manager.pixy.review(task['code'], task['manifest'],
                context={'request': task['request'][:2000],
                         'services': task.get('services', [])})
            self.store.event(tid, 'review', {'artifact_hash': task['artifact_hash'], 'decision': review})
            if not review['safe']:
                self.notify(f"Pixy rechazó el código generado ({review.get('reason')}). Reintentando generación...")
                self.store.update(tid, expected={'REVIEWING'}, state='GENERATING', feedback=review.get('reason', 'Rechazado'))
            elif (task.get('creation_consent') and not task.get('selected_version_id')
                  and task.get('version_id')
                  and self.manager.hand.version(task['version_id'])['status'] == 'draft'
                  and set(task['manifest'].get('capabilities', []))
                      <= set(task['creation_consent'].get('allowed_capabilities', []))
                  and {tuple(ref[key] for key in ('service_id', 'operation', 'version'))
                       for ref in task.get('services', [])}
                      <= {tuple(ref[key] for key in ('service_id', 'operation', 'version'))
                          for ref in task['creation_consent'].get('allowed_services', [])}):
                self.store.update(tid, expected={'REVIEWING'}, state='READY',
                                  authorized_hash=task['artifact_hash'],
                                  approval={'artifact_hash': task['artifact_hash'],
                                            'setup_consent_source': task['creation_consent']['source']})
                if self.manager.pixy.skill_approvals:
                    self.manager.pixy.skill_approvals.approve(
                        self.store.get(tid), self.manager.hand.version(task['version_id']),
                        task['creation_consent']['source'])
            elif (task.get('selected_version_id') and self.manager.pixy.skill_approvals
                  and self.manager.hand.version(task['selected_version_id'])['status'] == 'active'
                  and self.manager.hand.tree_hash(
                      self.manager.hand.root / task['skill_name']) ==
                      self.manager.hand.version(task['selected_version_id'])['artifact_hash']
                  and isinstance((prior := self.manager.pixy.skill_approvals.matching(
                      task, self.manager.hand.version(task['selected_version_id']))), dict)):
                self.store.event(tid, 'skill_approval_reused', {'approved_task_id': prior['task_id'],
                                                                 'scope': json.loads(prior['scope'])})
                self.store.update(tid, expected={'REVIEWING'}, state='READY',
                                  authorized_hash=task['artifact_hash'],
                                  approval={'artifact_hash': task['artifact_hash'],
                                            'reused_from_task': prior['task_id']})
            elif task['mode'] != 'SAFE' or task.get('dependencies') or 'Network' in task['manifest'].get('capabilities', []) or review['requires_approval']:
                self._wait_approval(task, review.get('reason', '') + '\nModo: ' + task['mode'] + '; capacidades: ' + ','.join(task['manifest'].get('capabilities', [])) + '; dependencias: ' + ','.join(task.get('dependencies', [])))
            else:
                # intermediate progress suppressed
                self.store.update(tid, expected={'REVIEWING'}, state='READY', authorized_hash=task['artifact_hash'])
        elif state == 'READY':
            if task.get('authorized_hash') != task['artifact_hash'] or digest(Path(task['artifact']).read_bytes()) != task['artifact_hash']:
                raise ValueError('El artefacto no coincide con la revisión/autorización')
            if not self._services_available(task.get('services', [])):
                raise PermissionError('Una versión de servicio aprobada ya no está activa')
            if (task.get('input_confirmation_required')
                    and task.get('confirmed_input_signature') != self._input_signature(task)):
                values = self._input_values(task)
                preferences = getattr(self.manager, 'memory', None)
                presented = []
                for key, value in values.items():
                    default = (preferences.get_preference('default_' + key)
                               if preferences and hasattr(preferences, 'get_preference') else None)
                    label = {'city': 'Ciudad', 'email': 'Correo', 'to': 'Destinatario',
                             'recipient': 'Destinatario'}.get(key, key.replace('_', ' ').capitalize())
                    shown = str(value) if isinstance(value, (str, int, float, bool)) else json.dumps(value, ensure_ascii=False)
                    suffix = ' (predeterminado)' if default is not None and default == value else ''
                    presented.append(f'{label}: {shown[:200]}{suffix}')
                signature = self._input_signature(task)
                self.store.update(tid, expected={'READY'}, state='WAITING_INPUT_CONFIRMATION',
                                  input_confirmation_signature=signature)
                details = '; '.join(presented) or f"objetivo: {(task.get('original_request') or task['request'])[:300]}"
                self.notify(f"Usaré estos datos: {details}. "
                            "¿Confirmas o quieres cambiar alguno? Responde sí o indica el cambio.")
                return
            active_services = (self.manager.voice_services.active_services()
                               if getattr(self.manager, 'voice_services', None) else {})
            for ref in task.get('services', []):
                operation = active_services[ref['service_id']]['operations'][ref['operation']]
                key = operation.get('credential')
                credential = self.manager.voice_credentials.status(key) if key else None
                if key and not credential['available']:
                    owner = str(self.manager.telegram.user_id)
                    request = self.manager.pixy.request_secret(
                        'api_key', key, ref['service_id'], task['request'], owner, owner,
                        expected_version=credential['version'])
                    self.store.update(tid, expected={'READY'}, state='WAITING_SECRET',
                                      secret_request_id=request['request_id'])
                    self.notify(f"{tid}: el servicio {ref['service_id']} requiere una credencial. "
                                f"En chat privado: {request['instruction']} "
                                f"Solicitud {request['request_id']}.")
                    return
            if hasattr(self.manager, 'hand') and task.get('version_id'):
                version = self.manager.hand.version(task['version_id'])
                if version['status'] == 'active':
                    self.manager.hand.use(task['skill_name'], tid)
            self.store.consume(tid, 'executions')
            execution_id = 'run_' + uuid.uuid4().hex
            if hasattr(self.manager, 'hand') and task.get('version_id'):
                approval = task.get('approval') or {}
                user_approved = (approval.get('artifact_hash') == task['artifact_hash']
                                 and task.get('authorized_hash') == task['artifact_hash'])
                self.manager.hand.prepare_execution(task['version_id'],execution_id,task['artifact_hash'],
                                                    user_approved=user_approved)
            payload = dict(execution_id=execution_id, task_id=tid, artifact_hash=task['artifact_hash'],
                           goal_revision=task.get('goal_revision', 0),
                           mode=task['mode'], dependencies=task['dependencies'], request=task['request'],
                           params=task.get('params', {}),
                           network=False, skill_name=task.get('skill_name'), version_id=task.get('version_id'),
                           grants=task.get('grants', []), deadline=task['deadline'])
            if hasattr(self.manager, 'hand') and task.get('version_id'):
                payload['skill_artifact_hash'] = self.manager.hand.version(task['version_id'])['artifact_hash']
            self.store.update(tid, expected={'READY'}, state='SUBMITTED', execution_id=execution_id,
                              runtime_status='queued', payload=payload)
        elif state == 'SUBMITTED':
            result = self.proxy.submit(task['payload'])
            self.store.update(tid, expected={'SUBMITTED'}, state='RUNNING', result=result,
                              runtime_status=result.get('status'))
        elif state == 'RUNNING':
            result = self.proxy.get(task['execution_id'])
            status = result.get('status')
            current = self.store.observe_execution(task, result)
            if not current or current['state'] != 'RUNNING':
                return
            if status in {'running', 'started'} and hasattr(self.manager, 'gateway'):
                self.manager.gateway.process_mailbox(current, self.root/'workspaces'/tid/task['artifact_hash'])
            if status in {'queued', 'starting', 'running'}:
                return
            if status == 'approval_required':
                self.store.update(tid, expected={'RUNNING'}, result=result)
                self._wait_approval(task, 'La habilidad solicita: ' + str(result.get('approval')), result.get('approval'))
            elif status == 'unknown' or (status in {'failed', 'timeout', 'cancelled'} and (task['mode'] != 'SAFE' or task.get('payload', {}).get('network') or 'Network' in task.get('manifest',{}).get('capabilities',[]))):
                self.store.update(tid, expected={'RUNNING'}, state='UNKNOWN', result=result)
                self.notify(f"{tid}: resultado incierto o con posibles efectos parciales. No se reejecutará automáticamente.")
            elif status in {'failed', 'timeout', 'cancelled'} or result.get('returncode', 0) != 0:
                if result.get('evidence_kind') == 'process_exit':
                    self.learning.record(task, False, result)
                case_id = None
                if hasattr(self.manager, 'heart') and hasattr(self.manager.heart, 'recovery') and self.manager.heart.recovery:
                    case = self.manager.heart.recovery.open_incident_case(task, result)
                    case_id = case.get('case_id')
                self.store.update(tid, expected={'RUNNING'}, state='DIAGNOSING', result=result, feedback=result, recovery_case_id=case_id)
                self.store.event(tid, 'execution_failure', result)
                err = (result.get('stderr') or '').strip() or result.get('status', 'error')
                self.notify(f"{tid}: Error en la ejecución de la skill ({err[:300]}). Diagnóstico delegado a Heart.")
            elif status in {'finished', 'started'}:
                if task.get('recipe'):
                    self._complete_recipe_step(task, result)
                    return
                workspace = self.root / 'workspaces' / tid / task['artifact_hash']
                gateway = getattr(self.manager, 'gateway', None)
                receipts = (gateway.technical_receipts(tid, task['execution_id'],
                                                     task.get('services', []))
                            if gateway and hasattr(gateway, 'technical_receipts') else [])
                incomplete_batch = any(item.get('parent_request_id') and
                                       item.get('status') != 'completed' for item in receipts)
                incomplete_write = any(item.get('effect') == 'write' and
                                       item.get('status') != 'completed' for item in receipts)
                if incomplete_batch or incomplete_write:
                    self.store.update(tid, expected={'RUNNING'}, state='UNVERIFIED',
                                      result=result, batch_incomplete=True,
                                      machine_verification={'success': None,
                                          'evidence': {'kind': 'external_effects',
                                                       'owner_review_required': True}})
                    self.notify(f"{tid}: hay efectos externos fallidos o inciertos. "
                                "No aceptaré el objetivo como completo. "
                                "Explica qué observaste o cancela.")
                    return
                success, evidence = verify_result(result, task.get('criterion'), workspace,
                                                  receipts, task['execution_id'])
                if success is None or success is True:
                    self.store.update(tid, expected={'RUNNING'}, state='UNVERIFIED', result=result,
                                      machine_verification={'success': success, 'evidence': evidence})
                    stdout_text = str(result.get('stdout', '')).strip()
                    if stdout_text:
                        msg = f"{stdout_text}\n\n¿Es este el resultado esperado? (y/n)"
                    else:
                        msg = "¿Es este el resultado esperado? (y/n)"
                    self.notify(msg)
                else:
                    self.store.update(tid, expected={'RUNNING'}, state='VERIFYING', result=result,
                                      verification={'success': success, 'evidence': evidence})
            else:
                self.store.update(tid, expected={'RUNNING'}, state='UNKNOWN', result=result)
                self.notify(f'{tid}: respuesta del ejecutor desconocida; requiere inspección.')
        elif state == 'VERIFYING':
            v = task['verification']
            if v['success'] and task.get('recipe'):
                for receipt in task.get('recipe_receipts', []):
                    version = self.manager.hand.version(receipt['version_id'])
                    if version['status'] == 'draft':
                        activated = self.manager.hand.approve(receipt['version_id'],
                            user_approved=receipt.get('approved_hash') == receipt['artifact_hash'],
                            correlation_id=tid)
                        self.manager.hand.catalog.promote(receipt['version_id'], tid,
                            receipt['execution_id'], activated['artifact_hash'])
            elif v['success'] and hasattr(self.manager, 'hand') and task.get('version_id'):
                version = self.manager.hand.version(task['version_id'])
                if version['status'] == 'draft':
                    if task.get('skill_spec_hash'):
                        approval = task.get('approval') or {}
                        user_approved = approval.get('artifact_hash') == task['artifact_hash']
                        activated = self.manager.hand.approve(task['version_id'],
                            user_approved=user_approved, correlation_id=tid)
                        self.manager.hand.catalog.promote(task['version_id'], tid, task['execution_id'],
                                                          activated['artifact_hash'])
                    else:
                        self.manager.hand.finish_one_off(task['version_id'])
            if task.get('recipe'):
                for receipt in task.get('recipe_receipts', []):
                    version = self.manager.hand.version(receipt['version_id'])
                    observed = dict(task, version_id=receipt['version_id'],
                                    skill_name=version['skill_name'],
                                    execution_id=receipt['execution_id'],
                                    artifact_hash=receipt['artifact_hash'])
                    self.learning.record(observed, v['success'],
                                         {'owner': v['evidence'], 'step': receipt['step_id']})
            else:
                self.learning.record(task, v['success'], v['evidence'])
            case_id = None
            if not v['success'] and hasattr(self.manager, 'heart') and hasattr(self.manager.heart, 'recovery') and self.manager.heart.recovery:
                case = self.manager.heart.recovery.open_incident_case(task, {'verification': v, 'error': 'Verificación de resultado fallida'})
                case_id = case.get('case_id')
            self.store.update(tid, expected={'VERIFYING'}, state='COMPLETED' if v['success'] else 'DIAGNOSING', feedback=v, recovery_case_id=case_id)
            if v['success']:
                stdout = (task.get('result') or {}).get('stdout', '')
                out_msg = f"\nResultado:\n{stdout.strip()[:2000]}" if stdout and stdout.strip() else ""
                if task.get('mode') == 'AGENT':
                    self.notify('Resultado aceptado. Estado actual del servicio: '
                                + str(self.store.get(tid).get('runtime_status', 'desconocido')) + '.' + out_msg)
                else:
                    self.notify(f"Ejecución completada con éxito.{out_msg}")
        elif state == 'DIAGNOSING':
            # Heart es la única autoridad médica del sistema
            case = None
            if hasattr(self.manager, 'heart') and hasattr(self.manager.heart, 'recovery') and self.manager.heart.recovery:
                cid = task.get('recovery_case_id')
                if cid:
                    try:
                        case = self.manager.heart.recovery.get(cid)
                    except KeyError:
                        case = None
                if not case and not task.get('diagnosed'):
                    case = self.manager.heart.recovery.open_incident_case(task, task.get('feedback') or task.get('result') or {})

            plan = case.get('plan') if case else None
            diagnosis = case.get('diagnosis') if case else None

            # Si Heart formuló un plan de autoreparación de tarea
            if plan and plan.get('target') == 'task':
                if plan.get('requires_approval'):
                    if case['state'] == 'SOLUTION_PROPOSED':
                        auth = self.manager.pixy.request_recovery_approval(case, task['source'])
                        self.store.update(tid, expected={'DIAGNOSING'},
                                          recovery_case_id=case['case_id'],
                                          recovery_authorization_id=auth['authorization_id'])
                        self.notify(f"Heart propone: {plan.get('summary', '')}. ¿Apruebas? Responde sí o no.")
                    elif case['state'] in {'DENIED', 'FAILED', 'UNKNOWN'}:
                        self.store.update(tid, expected={'DIAGNOSING'}, state='FAILED',
                                          error='La recuperación no fue autorizada o no pudo confirmarse')
                else:
                    feedback_msg = plan.get('reason') or 'Error diagnosticado por Heart'
                    if diagnosis and diagnosis.get('findings'):
                        feedback_msg += f". Detalle: {diagnosis['findings'][0].get('detail', '')}"
                    self.store.update(tid, expected={'DIAGNOSING'}, state='GENERATING', diagnosed=True,
                                      feedback=feedback_msg, selected_version_id=None, params={})
            else:
                self.store.update(tid, expected={'DIAGNOSING'}, state='FAILED', diagnosed=True)
                err_detail = ''
                if diagnosis and diagnosis.get('findings'):
                    err_detail = f": {diagnosis['findings'][0].get('detail', '')[:200]}"
                self.notify(f"{tid}: Fallo diagnosticado por Heart sin plan de autoreparación automático{err_detail}.")
        elif state == 'CANCELLING':
            self._poll_stop(task)

    async def run(self):
        if self.lock.file is None:
            self.lock.acquire()
        try:
            last_health = 0
            while not self.stopping:
                await asyncio.to_thread(self.tick, True)
                if time.monotonic() - last_health > 60:
                    try:
                        await self.manager.heart._fast_beat_basal()
                    except Exception:
                        logger.exception('Falló el diagnóstico; el coordinador continúa')
                    last_health = time.monotonic()
                await asyncio.sleep(1)
        finally:
            self.stopping = True
            for future in self._llm_jobs.values():
                future.cancel()
            self._llm_executor.shutdown(wait=False, cancel_futures=True)
            self._mailbox_executor.shutdown(wait=False, cancel_futures=True)
            self.lock.release()
