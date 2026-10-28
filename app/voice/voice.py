"""Authenticated Telegram ingress. Persist before acknowledging; no model calls here."""
import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request

from app.bootstrap import ModuleManager
from app.mind.coordinator import Coordinator
from app.pixy.control import InstanceLock
from app.kernel.database import connection
from app.kernel.tracing import TracingFilter, trace_scope, new_trace_id

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    if len(os.getenv('TELEGRAM_WEBHOOK_SECRET', '')) < 32:
        raise RuntimeError('Configura TELEGRAM_WEBHOOK_SECRET (mínimo 32 caracteres)')
    if os.getenv('KHAI_RUNTIME', 'docker') == 'docker' and len(os.getenv('PROXY_TOKEN', '')) < 32:
        raise RuntimeError('Configura PROXY_TOKEN (mínimo 32 caracteres)')
    log_path = Path(os.getenv('DATA_DIR', 'data')) / 'logs' / 'openclaw.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not any(isinstance(h, logging.FileHandler) and h.baseFilename == str(log_path.resolve()) for h in root_logger.handlers):
        handler = logging.FileHandler(log_path, encoding='utf-8')
        handler.addFilter(TracingFilter())
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(trace_id)s %(name)s %(message)s'))
        root_logger.addHandler(handler)
    owner_lock = InstanceLock(Path(os.getenv('DATA_DIR','data'))/'coordinator.lock')
    owner_lock.acquire()
    try:
        manager = ModuleManager(config_dir='config', data_dir=os.getenv('DATA_DIR', 'data'))
        manager.coordinator = Coordinator(manager)
        manager.coordinator.lock = owner_lock
        if hasattr(manager, 'heart') and hasattr(manager.heart, 'recovery') and manager.heart.recovery:
            manager.heart.recovery.coordinator = manager.coordinator
    except Exception:
        owner_lock.release()
        raise
    # Preserve legacy state, but never blindly replay a historical side effect.
    for tid, old in manager.state.get_pending_tasks().items():
        manager.coordinator.store.create('legacy:' + tid, old.get('request', ''),
            state='UNKNOWN', legacy_task_id=tid, error='Tarea anterior a la migración; requiere inspección manual')
    app.state.manager = manager
    worker = asyncio.create_task(manager.coordinator.run())
    inbox_workers = [asyncio.create_task(run_inbox(manager, urgent=urgent))
                     for urgent in (False, True)]
    try:
        yield
    finally:
        for inbox_worker in inbox_workers:
            inbox_worker.cancel()
        await asyncio.gather(*inbox_workers, return_exceptions=True)
        manager.coordinator.stopping = True
        await worker


app = FastAPI(title='Khai', lifespan=lifespan)

HELP_TEXT = """Comandos disponibles:
/help o /? - muestra esta ayuda.
/pixy <mensaje> o /p <mensaje> - habla directamente con Pixy.
/make <objetivo> - crea un objetivo ejecutable.
/tasks - consulta objetivos activos y recientes.
/artifact <ID> - muestra el artefacto de un objetivo.
/approve <ID> <TOKEN> - aprueba una ejecución solicitada.
/deny <ID> <TOKEN> - rechaza una ejecución solicitada.
/cancel <ID> - cancela un objetivo.
/expect <ID> <archivo> <SHA256> - fija una verificación antes de ejecutar.
/verify <ID> <RUN_ID> success|failure - registra el resultado observado.
/rules - consulta reglas de aprendizaje.
/rule approve <ID> [ID_SUSTITUIDA] - activa una regla propuesta.
/rule rollback <ID> - revierte una regla.
/export <TASK_ID> <archivo> - pone un resultado en cuarentena.
/fetch <servicio> <operación> <archivo> [JSON] - descarga mediante Voice.
/publish <ASSET_ID> - publica un archivo aprobado.
/asset <ASSET_ID> - entrega un archivo publicado.
/unpublish <ASSET_ID> - retira un archivo.
/mode <low|mid|high|?> o /m <low|mid|high|?> - cambia o consulta el modo LLM (low=Gemini, mid=Dolphin CPU Spot, high=Dolphin GPU L4 Spot, ?=status).
Las aprobaciones de Pixy se responden con y/n cuando ella lo solicite."""


def authenticated_message(data, user_id):
    if not isinstance(data, dict):
        raise ValueError("Se requiere un objeto JSON")
    update_id = data.get('update_id')
    if type(update_id) is not int:
        raise ValueError('update_id requerido')
    msg = data.get('callback_query') or data.get('message') or {}
    if not isinstance(msg, dict) or not isinstance(msg.get('from'), dict):
        raise ValueError('Identidad del mensaje ausente')
    if str(msg.get('from', {}).get('id')) != str(user_id):
        raise PermissionError('Usuario no autorizado')
    text = msg.get('data') if 'callback_query' in data else msg.get('text')
    if not isinstance(text, str) or not text.strip() or len(text) > 32000:
        raise ValueError('Mensaje de texto inválido')
    return 'telegram:' + str(update_id), text.strip()


def authenticated_private_context(data, user_id):
    source, text = authenticated_message(data, user_id)
    message = data.get('message') or {}
    chat = message.get('chat') or {}
    if chat.get('type') != 'private' or chat.get('id') is None:
        return source, text, str(user_id), None
    return source, text, str(user_id), str(chat['id'])


def secret_command(manager, text, user_id, chat_id):
    command_name = text.split(maxsplit=1)[0].lower() if text else ''
    if command_name not in {'/us', '/pas', '/api'}:
        return None
    if chat_id is None:
        raise PermissionError('La captura de credenciales requiere un chat privado')
    if ' ' not in text:
        raise ValueError('Captura de credencial inválida')
    command_name, value = text.split(' ', 1)
    value = value.strip()
    receipt = manager.secret_capture.capture(command_name, value, user_id, chat_id)
    return 'Credencial guardada. Recibo: ' + receipt


def urgent_command(text):
    name = text.split(maxsplit=1)[0].lower()
    return name in {'/cancel', '/tasks', '/approve', '/deny', '/verify', '/expect', '/service',
                    '/mode', '/m', '/on', '/off', '/dolphin', 'y', 'yes', 'sí', 'si', 'n', 'no'}


def process_ordinary(manager, source, trace_id, text):
    with trace_scope(trace_id):
        words = text.strip().split()
        if not text.startswith('/'):
            pending_questions = [task for task in manager.coordinator.store.all()
                                 if task['state'] in {'WAITING_SCOPE', 'WAITING_INPUT_CONFIRMATION', 'UNVERIFIED',
                                                       'WAITING_FEEDBACK', 'WAITING_CREATION_APPROVAL',
                                                       'WAITING_EFFECT_DECISION', 'WAITING_EFFECT_DETAIL'}]
            named = any(task['id'] in {words[0], words[-1]} for task in pending_questions) if words else False
            if len(pending_questions) > 1 and not named:
                manager.coordinator.notify(
                    'Hay varias preguntas pendientes. Incluye el ID del objetivo: ' +
                    ', '.join(task['id'] for task in pending_questions))
                return
        decision = bool(words and words[0].casefold() in {'y', 'yes', 'sí', 'si', 'n', 'no'}
                        and len(words) <= 2)
        reply = None
        if not text.startswith('/'):
            reply = manager.coordinator.accept_input_confirmation(source, text)
            if reply is None:
                reply = manager.coordinator.accept_effect_detail(source, text)
            if reply is None:
                reply = manager.coordinator.accept_effect_decision(source, text)
        if decision and reply is None:
            reviews = [task for task in manager.coordinator.store.all()
                       if task['state'] in {'UNVERIFIED', 'WAITING_FEEDBACK'}]
            approvals = [task for task in manager.coordinator.store.all()
                         if task['state'] == 'WAITING_CREATION_APPROVAL']
            authorizations = (manager.pixy.authorizations.pending_all()
                              if getattr(manager.pixy, 'authorizations', None) else [])
            target = words[1] if len(words) == 2 else None
            if any(task['id'] == target for task in reviews) or (reviews and not approvals
                    and not authorizations and target is None):
                reply = manager.coordinator.accept_review_reply(source, text)
            elif reviews and (approvals or authorizations) and target is None:
                reply = 'Hay decisiones y resultados pendientes. Responde y/n con el ID correspondiente.'
        if reply is None:
            reply = command(manager, source, text)
        if reply is None:
            reviews = [task for task in manager.coordinator.store.all()
                       if task['state'] in {'UNVERIFIED', 'WAITING_FEEDBACK'}]
            targets_review = bool(words and any(task['id'] in {words[0], words[-1]}
                                                for task in reviews))
            scope_reply = (None if targets_review else
                           manager.coordinator.accept_scope_reply(source, text))
            review_reply = (manager.coordinator.accept_review_reply(source, text)
                            if scope_reply is None else None)
            if scope_reply is None and review_reply is None:
                manager.coordinator.intake(source, text, trace_id=trace_id)
            else:
                manager.coordinator.notify(scope_reply or review_reply)
        else:
            manager.coordinator.notify(reply)


async def run_inbox(manager, *, urgent):
    while True:
        try:
            handled = await asyncio.to_thread(
                manager.voice_mailbox.process_one,
                lambda source, trace_id, text: process_ordinary(manager, source, trace_id, text),
                urgent=urgent)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('Mensaje ordinario requiere reconciliación: %s', type(exc).__name__)
            handled = False
        if not handled:
            await asyncio.sleep(0.5)


def command(manager, source, text):
    if not hasattr(manager,'assets') or text.split()[0].lower() not in {'/fetch','/export','/publish','/asset','/unpublish'}:
        return _command(manager,source,text)
    with connection(manager.assets.db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = conn.execute('SELECT * FROM voice_commands WHERE source=?',(source,)).fetchone()
        if previous:
            return previous['reply'] if previous['status']=='completed' else 'Operación anterior sin confirmar; no se repetirá automáticamente.'
        conn.execute('INSERT INTO voice_commands VALUES (?,"pending",NULL)',(source,))
    result = _command(manager,source,text)
    with connection(manager.assets.db) as conn:
        conn.execute('UPDATE voice_commands SET status="completed",reply=? WHERE source=?',(result,source))
    return result


def _command(manager, source, text):
    c = manager.coordinator
    parts = text.split()
    name = parts[0].lower()
    if name in {'/help', '/?'}:
        return HELP_TEXT
    if name in {'/mode', '/m'}:
        if not hasattr(manager, 'dolphin') or manager.dolphin is None:
            raise ValueError('Controlador Dolphin no configurado')
        arg = parts[1].lower() if len(parts) >= 2 else '?'
        return manager.dolphin.set_mode(arg)
    if name == '/on':
        if not hasattr(manager, 'dolphin') or manager.dolphin is None:
            raise ValueError('Controlador Dolphin no configurado')
        arg = parts[1].lower() if len(parts) >= 2 else 'mid'
        if arg == 'low':
            arg = 'mid'
        return manager.dolphin.set_mode(arg)
    if name == '/off':
        if not hasattr(manager, 'dolphin') or manager.dolphin is None:
            raise ValueError('Controlador Dolphin no configurado')
        return manager.dolphin.set_mode('low')
    if name == '/dolphin':
        if not hasattr(manager, 'dolphin') or manager.dolphin is None:
            raise ValueError('Controlador Dolphin no configurado')
        return manager.dolphin.set_mode('?')
    if name in {'/pixy', '/p'}:
        return manager.pixy.direct_response(text.partition(' ')[2], source=source)
    if name == '/service':
        return manager.pixy.direct_response(text[1:], source=source)
    if name in {'y', 'yes', 'sí', 'si', 'n', 'no'} and len(parts) <= 2:
        return manager.pixy.direct_response(text, source=source)
    if name == '/export' and len(parts) == 3:
        task = c.store.get(parts[1])
        if not task:
            raise ValueError('Tarea desconocida')
        workspace = manager.data_dir/'workspaces'/task['id']/task['artifact_hash']
        aid = manager.assets.import_result(task,workspace,parts[2],manager.telegram.user_id)
        return 'Archivo en cuarentena. Aprobar publicación: /publish '+aid
    if name == '/fetch' and len(parts) >= 4:
        import json
        values = text.split(maxsplit=4)
        params = json.loads(values[4]) if len(values)==5 else {}
        aid = manager.assets.download(manager.gateway,parts[1],parts[2],params,parts[3],manager.telegram.user_id)
        return 'Archivo en cuarentena. Aprobar publicación: /publish '+aid
    if name == '/asset' and len(parts) == 2:
        manager.assets.deliver(parts[1],manager.telegram.user_id,manager.telegram)
        return 'Archivo entregado.'
    if name == '/publish' and len(parts) == 2:
        manager.assets.publish(parts[1],manager.telegram.user_id,approved=True)
        return 'Archivo aprobado. /asset '+parts[1]
    if name == '/unpublish' and len(parts) == 2:
        manager.assets.unpublish(parts[1],manager.telegram.user_id)
        return 'Archivo retirado.'
    if name == '/tasks':
        return '\n'.join(t['id'] + ': ' + t['state'] for t in c.store.all()[-20:]) or 'Sin tareas.'
    if name == '/artifact' and len(parts) == 2:
        task = c.store.get(parts[1])
        if not task:
            raise ValueError('Tarea desconocida')
        return 'SHA256: ' + task.get('artifact_hash', '') + '\n' + task.get('code', 'Sin artefacto')
    if name in {'/approve', '/deny'} and len(parts) == 3:
        c.approval(parts[1], parts[2], name == '/approve', source)
        return 'Decisión registrada.'
    if name == '/cancel' and len(parts) == 2:
        c.cancel(parts[1], source)
        return 'Cancelación registrada; el estado final se confirma con /tasks.'
    if name == '/verify' and len(parts) == 4 and parts[3] in {'success', 'failure'}:
        c.verify(parts[1], parts[2], parts[3] == 'success', source)
        return 'Verificación del usuario registrada con su ejecución.'
    if name == '/expect' and len(parts) == 4:
        c.set_criterion(parts[1], parts[2], parts[3], source)
        return 'Criterio SHA256 registrado antes de ejecutar.'
    if name == '/rules':
        if hasattr(manager, 'rules'):
            return '\n'.join(f"{r['rule_id']} [{r['status']}]: {r['content']}" for r in manager.rules.all()) or 'Sin reglas.'
        return 'Sin reglas.'
    if name == '/rule' and len(parts) in {3, 4}:
        if parts[1] == 'approve':
            c.learning.activate(parts[2], parts[3] if len(parts) == 4 else None, source=source)
        elif parts[1] == 'rollback' and len(parts) == 3:
            c.learning.rollback(parts[2], source=source)
        else:
            raise ValueError('Usa /rule approve ID [ID_SUSTITUIDO] o /rule rollback ID')
        return 'Regla actualizada.'
    if name in {'/help', '/restart', '/new', '/compact'}:
        return ('/make objetivo; /tasks; /artifact ID; /approve ID TOKEN; /deny ID TOKEN; '
                '/cancel ID; /verify ID RUN_ID success|failure; /expect ID archivo SHA256; '
                '/rules; /rule approve ID [ID_SUSTITUIDO]; /rule rollback ID. '
                '/export TASK archivo; /fetch servicio operación archivo [JSON]; /publish ASSET; /asset ASSET; /unpublish ASSET. '
                'El historial de evidencia no se borra. Reinicios y administración del host se realizan fuera del agente.')
    if name in {'/approve', '/deny', '/cancel', '/verify', '/expect', '/rule', '/artifact',
                '/export','/fetch','/asset','/publish','/unpublish'}:
        raise ValueError('Comando incompleto; consulta /help')
    return None


@app.post('/webhook')
async def webhook(request: Request):
    configured = os.getenv('TELEGRAM_WEBHOOK_SECRET', '')
    provided = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if len(configured) < 32 or not hmac.compare_digest(configured, provided):
        raise HTTPException(401, 'Unauthorized')
    body = await request.body()
    if len(body) > 65536:
        raise HTTPException(413, 'Mensaje demasiado grande')
    try:
        import json
        data = json.loads(body)
        manager = request.app.state.manager
        update_id = data.get('update_id', 'msg')
        trace_id = new_trace_id(prefix=f"req_{update_id}")
        with trace_scope(trace_id):
            source, text, user_id, chat_id = authenticated_private_context(
                data, manager.telegram.user_id)
            if text.split(maxsplit=1)[0].lower() in {'/us', '/pas', '/api'}:
                reply = await asyncio.to_thread(secret_command, manager, text, user_id, chat_id)
                await asyncio.to_thread(manager.coordinator.notify, reply)
            elif hasattr(manager, 'voice_mailbox') and manager.voice_mailbox:
                manager.voice_mailbox.accept(source, trace_id, text, urgent=urgent_command(text))
            elif hasattr(manager, 'coordinator') and manager.coordinator:
                manager.coordinator.intake(source, text)
        return {'ok': True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("Error procesando webhook de Telegram: %s", exc)
        return {'ok': True}


if __name__ == '__main__':
    import uvicorn
    from dotenv import load_dotenv
    load_dotenv()
    kwargs = {}
    if os.path.isfile('data/certs/cert.pem') and os.path.isfile('data/certs/key.pem'):
        kwargs = {'ssl_certfile': 'data/certs/cert.pem', 'ssl_keyfile': 'data/certs/key.pem'}
    uvicorn.run(app, host='0.0.0.0', port=8443 if kwargs else 8000, **kwargs)
