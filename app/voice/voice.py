"""Authenticated Telegram ingress. Persist before acknowledging; no model calls here."""
import asyncio
import hmac
import logging
import os
import re
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request

from app.bootstrap import ModuleManager
from app.mind.coordinator import Coordinator
from app.pixy.control import InstanceLock
from app.kernel.tracing import TracingFilter, trace_scope, new_trace_id
from app.voice.help import HELP_TEXT

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
    matches = list(re.finditer(r'(?:^|\s+)(/us|/pas|/api)(?=\s|$)', text, flags=re.IGNORECASE))
    if not matches:
        raise ValueError('Captura de credencial inválida')
    pairs = []
    for idx, match in enumerate(matches):
        cmd = match.group(1).lower()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        value = text[start:end].strip()
        if not value:
            raise ValueError('Captura de credencial inválida')
        pairs.append((cmd, value))
    receipts = [manager.secret_capture.capture(cmd, val, user_id, chat_id) for cmd, val in pairs]
    if len(receipts) == 1:
        return 'Credencial guardada. Recibo: ' + receipts[0]
    return 'Credenciales guardadas. Recibos: ' + ', '.join(receipts)


def urgent_command(text):
    name = text.split(maxsplit=1)[0].lower()
    return name in {'/cancel', '/stop', '/health', '/restart', '/mode', '/m', '/mode?', '/m?',
                    'y', 'yes', 'sí', 'si', 'n', 'no'}


def process_ordinary(manager, source, trace_id, text):
    from app.mind.conversation import current_turn
    c = manager.coordinator
    turn = c.conversations.turn(source)
    if turn is None:
        owner = str(manager.telegram.user_id)
        turn = c.conversations.begin(source, trace_id, text, owner, owner)
    if turn['status'] in {'completed', 'routed', 'calling'}:
        return
    token = current_turn.set(turn)
    try:
        with trace_scope(trace_id):
            decision_text = text
            if text.split(maxsplit=1)[0].lower() in {'/p', '/pixy'}:
                decision_text = text.partition(' ')[2].strip()
                if not decision_text or decision_text.split()[0].casefold() not in {'y','yes','s?','si','n','no'}:
                    decision_text = ''
            result = (c.handle_reply(source, decision_text, turn['conversation_id'], turn['reply_to'])
                      if decision_text and not decision_text.startswith('/') else None)
            if result is not None and result.handled:
                c.conversations.complete(source, {'task_id': result.task_id, 'result': result.text})
                if result.text:
                    c.notify(result.text)
                return
            reply = command(manager, source, text)
            if reply is not None:
                c.conversations.complete(source, {'result': reply})
                c.notify(reply)
                return
            c.intake(source, text, trace_id=trace_id)
    finally:
        current_turn.reset(token)


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
    return _command(manager, source, text)


def _format_health(manager):
    if not hasattr(manager, 'heart') or manager.heart is None:
        return 'Heart no está disponible.'
    if hasattr(manager.heart, 'run_diagnostic_fast'):
        report = manager.heart.run_diagnostic_fast()
        vitals = report.get('vitals') or {}
        integrity = report.get('integrity') or {}
        dbs = integrity.get('databases') or {}
        db_summary = ', '.join(f'{k}={v}' for k, v in sorted(dbs.items())) or 'sin bases'
        free_gb = round((vitals.get('disk_free') or 0) / (1024 ** 3), 2)
        overall = 'ok' if vitals.get('status') == 'ok' and integrity.get('status') == 'ok' else 'alerta'
        return (f"Estado Heart: {overall} | RAM: {vitals.get('ram_percent', '?')}% | "
                f"Disco: {vitals.get('disk_percent', '?')}% ({free_gb} GB libres) | "
                f"SQLite ({integrity.get('status', '?')}): {db_summary}")
    return str(manager.heart.check_integrity())


def _command(manager, source, text):
    c = manager.coordinator
    parts = text.split()
    name = parts[0].lower()
    if name in {'/help', '/?'}:
        return HELP_TEXT
    if name in {'/mode', '/m', '/mode?', '/m?'}:
        if not hasattr(manager, 'dolphin') or manager.dolphin is None:
            raise ValueError('Controlador Dolphin no configurado')
        arg = '?' if name.endswith('?') else (parts[1].lower() if len(parts) >= 2 else '?')
        return manager.dolphin.set_mode(arg)
    if name in {'/pixy', '/p', '/pixy?', '/p?'}:
        payload = '?' if name.endswith('?') and len(parts) == 1 else text.partition(' ')[2]
        return manager.pixy.direct_response(payload, source=source)
    if name in {'y', 'yes', 'sí', 'si', 'n', 'no'} and len(parts) <= 2:
        return manager.pixy.direct_response(text, source=source)
    if name == '/health':
        return _format_health(manager)
    if name in {'/cancel', '/stop'}:
        if len(parts) == 2:
            c.cancel(parts[1], source)
            return f'Cancelación solicitada para {parts[1]}.'
        active = [t for t in c.store.all()
                  if t.get('state') not in {'COMPLETED', 'FAILED', 'CANCELLED', 'EXHAUSTED', 'UNFULFILLABLE'}
                  or c._agent_active(t)]
        if not active:
            return 'No hay objetivos en curso para detener.'
        for idx, t in enumerate(active):
            c.cancel(t['id'], f'{source}:stop:{idx}')
        return 'Cancelación solicitada: ' + ', '.join(t['id'] for t in active)
    if name == '/restart':
        targets = ([parts[1].lower()]
                   if len(parts) >= 2 and parts[1].lower() in {'core', 'proxy'}
                   else ['core', 'proxy'])
        if hasattr(manager, 'recovery_executor') and manager.recovery_executor is not None:
            try:
                ids = []
                for t in targets:
                    aid = 'rec_' + uuid.uuid4().hex
                    manager.recovery_executor.submit(aid, 'restart_container', t)
                    ids.append(f'{t}:{aid}')
                return f"Reinicio de contenedores Docker ({', '.join(targets)}) programado ({', '.join(ids)})."
            except Exception as exc:
                logger.warning('Proxy de recuperación no disponible para /restart (%s); forzando salida local', type(exc).__name__)
        threading.Timer(1.0, lambda: os._exit(75)).start()
        return 'Reiniciando proceso principal de Khai...'
    if name.startswith('/'):
        return 'Comando no disponible. Consulta /help.'
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
                msg = data.get('message') or (data.get('callback_query') or {}).get('message') or {}
                conversation_chat = str((msg.get('chat') or {}).get('id', user_id))
                manager.coordinator.conversations.begin(
                    source, trace_id, text, user_id, conversation_chat,
                    msg.get('message_id') if 'message' in data else None,
                    (msg.get('reply_to_message') or {}).get('message_id'))
                manager.voice_mailbox.accept(source, trace_id, text, urgent=urgent_command(text))
            elif hasattr(manager, 'coordinator') and manager.coordinator:
                await asyncio.to_thread(process_ordinary, manager, source, trace_id, text)
            callback_id = (data.get('callback_query') or {}).get('id')
            if callback_id:
                try:
                    await asyncio.to_thread(manager.telegram.answer_callback_query, callback_id)
                except Exception:
                    logger.warning('No se pudo confirmar el botón; la entrada permanece registrada')
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
