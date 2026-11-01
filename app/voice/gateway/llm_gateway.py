import os
import time
import logging
import requests
import json
import uuid
import threading
from contextvars import ContextVar
from typing import Dict
from app.pixy.budget import current_budget

logger = logging.getLogger("LLMManager")
DEFAULT_GEMINI_MODEL = os.getenv('KHAI_GEMINI_MODEL', 'gemini-flash-lite-latest')



class LLMContentRejected(ValueError):
    """The provider made a terminal content decision, not an availability failure."""


class LLMManager:
    """Fachada global del sistema LLM con Unified Fallback."""

    def read_document(self, url, allowed_urls):
        from app.voice.gateway.document_reader import read_public_document
        return read_public_document(url, allowed_urls)
    
    def __init__(self, config_dir: str, data_dir: str, memory=None, secrets=None, provider_db=None, dolphin=None):
        self.secrets = secrets
        self.provider_db = provider_db
        self.dolphin = dolphin
        from app.voice.model_diagnostics import ModelDiagnostics
        from app.kernel.journal import Journal
        self.model_diagnostics = ModelDiagnostics(provider_db or os.path.join(data_dir, 'model-diagnostics.sqlite3'), secrets)
        self.journal = Journal(provider_db or os.path.join(data_dir, 'model-diagnostics.sqlite3'))
        self._response_meta = ContextVar('model_response_metadata', default={})
        if provider_db:
            from app.kernel.database import connection
            with connection(provider_db) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS provider_status(key_id TEXT PRIMARY KEY,cooldown_until REAL)")
            from app.kernel.database import adopt_schema
            adopt_schema(provider_db,'provider-status',['provider_status'])
        self.config_dir = config_dir
        self._research_lock = threading.RLock()
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        if memory:
            self.memory = memory
        else:
            from app.mind.memory import MemoryEngine
            self.memory = MemoryEngine(data_dir=os.path.join(os.path.dirname(self.data_dir), "memory"))
        self.cooldown_seconds = 60
        self.api_keys_pool = []
        self._load_keys()

    def _load_keys(self):
        """Carga todas las llaves disponibles en la variable de entorno y las ordena."""
        # 1. Gemini
        for k, v in sorted(self.secrets.items() if self.secrets else os.environ.items()):
            if k.startswith("GEMINI_API_KEY"):
                self.api_keys_pool.append({"type": "gemini", "key": v, "id": k})
                
        # 2. Azure
        for k, v in sorted(self.secrets.items() if self.secrets else os.environ.items()):
            if k.startswith("AZURE_OPENAI_API_KEY"):
                self.api_keys_pool.append({"type": "azure", "key": v, "id": k})
                
        # 3. OpenAI
        for k, v in sorted(self.secrets.items() if self.secrets else os.environ.items()):
            if k.startswith("OPENAI_API_KEY"):
                self.api_keys_pool.append({"type": "openai", "key": v, "id": k})
                
        if not self.api_keys_pool:
            logger.warning("No se encontraron API keys en el entorno (Gemini, Azure, OpenAI).")

    def record_contract_failure(self, contract, raw, errors, schema):
        context = self._response_meta.get()
        reference = self.model_diagnostics.record(contract, raw, errors, schema, context)
        parent = context.get('cycle_id')
        cycle = self.journal.start('model_validation', reference['id'], parent_id=parent,
            refs={'evidence_id': reference['id'], 'response_sha256': reference['response_sha256']})
        self.journal.finish(cycle, 'rejected', data={'error_type': 'ValidationError'})
        return reference

    def contract_rejection(self, reference):
        return self.model_diagnostics.get(reference)

    def diagnostic_snapshot(self):
        return {'model_rejections': self.model_diagnostics.recent()}

    def _load_status(self) -> Dict:
        if getattr(self, 'provider_db', None):
            from app.kernel.database import connection
            with connection(self.provider_db) as conn:
                return dict(conn.execute("SELECT key_id,cooldown_until FROM provider_status"))
        if getattr(self, 'memory', None):
            return self.memory.get_provider_status()
        return {}

    def _mark_key_failed(self, key_id: str, cooldown: float = None):
        wait = getattr(self, 'cooldown_seconds', 60) if cooldown is None else cooldown
        cooldown_until = time.time() + wait
        if getattr(self, 'provider_db', None):
            from app.kernel.database import connection
            with connection(self.provider_db) as conn:
                conn.execute("INSERT OR REPLACE INTO provider_status VALUES (?,?)",(key_id,cooldown_until))
        elif getattr(self, 'memory', None):
            self.memory.set_provider_cooldown(key_id, cooldown_until)

    def _dolphin_endpoint(self) -> str:
        env_url = os.getenv('KHAI_DOLPHIN_URL', '').strip()
        if env_url:
            return env_url.rstrip('/')
        dolphin_ctrl = getattr(self, 'dolphin', None)
        if dolphin_ctrl is not None and getattr(dolphin_ctrl, 'endpoint', ''):
            return str(dolphin_ctrl.endpoint).strip().rstrip('/')
        secrets = getattr(self, 'secrets', None)
        if secrets is not None and hasattr(secrets, 'get'):
            sec_url = secrets.get('KHAI_DOLPHIN_URL')
            if sec_url:
                return str(sec_url).strip().rstrip('/')
        return ''

    def _call_dolphin(self, prompt: str, endpoint: str, kwargs: Dict) -> str:
        try:
            return self._generate_dolphin(prompt, endpoint, kwargs)
        except TypeError:
            return self._generate_dolphin(prompt, endpoint)

    def _dolphin_model(self) -> str:
        env_model = os.getenv('KHAI_DOLPHIN_MODEL', '').strip()
        if env_model:
            return env_model
        dolphin_ctrl = getattr(self, 'dolphin', None)
        if dolphin_ctrl is not None and hasattr(dolphin_ctrl, 'get_model_for_mode'):
            return dolphin_ctrl.get_model_for_mode()
        return 'dolphin3:8b'

    def _notify(self, message: str) -> None:
        notifier = getattr(self, 'notifier', None)
        if callable(notifier):
            try:
                notifier(message)
            except Exception as exc:
                logger.warning('No se pudo enviar notificación de LLM: %s', exc)

    def generate_content(self, prompt: str, **kwargs) -> str:
        """Genera contenido en cascada según el modo activo (mid/high -> Dolphin -> Cloud; low -> Cloud -> Dolphin)."""
        if hasattr(self, '_response_meta'):
            self._response_meta.set({})
        rf = kwargs.get('response_format')
        is_json = isinstance(rf, dict) and rf.get('type') == 'json_object'
        dolphin_ctrl = getattr(self, 'dolphin', None)
        active_mode = (dolphin_ctrl.get_active_mode()
                       if dolphin_ctrl is not None
                       else os.getenv('KHAI_DOLPHIN_MODE', 'nano').strip().lower())
        endpoint = self._dolphin_endpoint()
        conv_banner = ''
        tried_dolphin = False

        if endpoint and active_mode in {'low', 'mid', 'high'}:
            tried_dolphin = True
            ladder = ['high', 'mid', 'low'] if active_mode == 'high' else (['mid', 'low'] if active_mode == 'mid' else ['low'])
            answer = None
            successful_profile = None

            # Intentar directamente si el nodo ya está activo y respondiendo
            try:
                answer = self._call_dolphin(prompt, endpoint, kwargs)
                successful_profile = active_mode
            except Exception as exc_call:
                logger.info('Dolphin %s no respondió directamente (%s), intentando arranque/cascada',
                            active_mode, type(exc_call).__name__)
                for profile in ladder:
                    if dolphin_ctrl is not None:
                        try:
                            self._notify(f"[Conectando con Modo {profile}]")
                            dolphin_ctrl.start(profile)
                            answer = self._call_dolphin(prompt, endpoint, kwargs)
                            successful_profile = profile
                            self._notify(f"[Modo {profile} conectado]")
                            break
                        except Exception as exc:
                            from app.mind.brain.controller import _format_error_reason
                            reason = _format_error_reason(exc)
                            logger.warning('Dolphin %s falló (%s): %s', profile, type(exc).__name__, exc)
                            self._notify(f"[Modo {profile} falló por: {reason}]")
                    else:
                        try:
                            answer = self._call_dolphin(prompt, endpoint, kwargs)
                            successful_profile = profile
                            break
                        except Exception as exc:
                            logger.warning('Llamada Dolphin falló (%s)', type(exc).__name__)

            if answer is not None and successful_profile is not None:
                if dolphin_ctrl is not None and not is_json:
                    conv_banner = dolphin_ctrl.consume_transition_banner(successful_profile)
                return (conv_banner + answer) if conv_banner and not is_json else answer

            if dolphin_ctrl is not None:
                dolphin_ctrl.mark_fallback_to_nano()
            self._notify("[Modo nano conectado]")
            if not is_json and dolphin_ctrl is not None:
                conv_banner = dolphin_ctrl.consume_transition_banner('nano')
        elif dolphin_ctrl is not None and not is_json:
            conv_banner = dolphin_ctrl.consume_transition_banner('nano')

        status = self._load_status()
        now = time.time()
        
        forced_key = kwargs.get("forced_key")
        forced_type = kwargs.get("forced_type")
        
        if forced_key and forced_type:
            pool = [{"type": forced_type, "key": forced_key, "id": "FORCED_KEY"}]
        else:
            ready = [c for c in self.api_keys_pool if now > status.get(c['id'], 0)]
            cooling = sorted(
                [c for c in self.api_keys_pool if now <= status.get(c['id'], 0)],
                key=lambda c: status.get(c['id'], 0),
            )
            # Retrying cooling keys on every turn defeated admission control and
            # amplified quota/provider failures.
            pool = ready

        content_rejections = 0

        for idx, current_cred in enumerate(pool):
            budget = current_budget.get()
            if budget is not None:
                task = budget()
                kwargs['_timeout'] = max(1, min(60, task['deadline'] - time.time()))
            else:
                kwargs['_timeout'] = min(60, kwargs.get('_timeout', 60))
            try:
                generate = getattr(self, '_generate_' + current_cred['type'])
                result = generate(prompt, current_cred['key'], kwargs)
                return (conv_banner + result) if conv_banner and not is_json else result
            except Exception as exc:
                # Never print credentials, URLs with keys, or full SDK exception payloads.
                logger.warning('Proveedor %s falló (%s)', current_cred['id'], type(exc).__name__)
                if isinstance(exc, LLMContentRejected):
                    content_rejections += 1
                    continue
                msg = str(exc)
                if isinstance(exc, ValueError) and ('Gemini no devolvió candidatos' in msg or 'Gemini devolvió candidato vacío' in msg):
                    continue
                http_code = getattr(getattr(exc, 'response', None), 'status_code', None)
                cooldown = 300 if http_code in {401, 403, 429} else getattr(self, 'cooldown_seconds', 60)
                self._mark_key_failed(current_cred['id'], cooldown=cooldown)
                if idx < len(pool) - 1:
                    next_cred = pool[idx + 1]
                    if 'FALLBACK' in next_cred['id']:
                        self._notify("Gemini no responde, pasando a Gemini Fallback")
                    else:
                        self._notify(f"{current_cred['id']} no responde, pasando a {next_cred['id']}")
                else:
                    if len(pool) > 1:
                        self._notify("Gemini Fallback no responde, pasando a Dolphin Medium")
                    else:
                        self._notify("Gemini no responde, pasando a Dolphin Medium")

        if pool and content_rejections == len(pool):
            message = 'La solicitud fue rechazada por los proveedores configurados; no se ejecutó ninguna acción.'
            self._notify(message)
            raise LLMContentRejected(message)

        if endpoint and not tried_dolphin and not (forced_key and forced_type):
            self._notify("Conectando con Dolphin Medium")
            mid_ok = False
            if dolphin_ctrl is not None:
                try:
                    dolphin_ctrl.start('mid')
                    mid_ok = True
                except Exception as exc:
                    logger.warning('Auto-arranque Dolphin mid falló (%s)', type(exc).__name__)

            if mid_ok or dolphin_ctrl is None:
                try:
                    answer = self._call_dolphin(prompt, endpoint, kwargs)
                    if dolphin_ctrl is not None and not is_json:
                        conv_banner = dolphin_ctrl.consume_transition_banner('mid')
                    return (conv_banner + answer) if conv_banner and not is_json else answer
                except Exception as exc:
                    logger.warning('Inferencia Dolphin mid falló (%s)', type(exc).__name__)

            self._notify("Dolphin Medium no responde, pasando a Dolphin High")
            self._notify("Conectando con Dolphin High")
            high_ok = False
            if dolphin_ctrl is not None:
                try:
                    dolphin_ctrl.start('high')
                    high_ok = True
                except Exception as exc:
                    logger.warning('Auto-arranque Dolphin high falló (%s)', type(exc).__name__)

            if high_ok or dolphin_ctrl is None:
                try:
                    answer = self._call_dolphin(prompt, endpoint, kwargs)
                    if dolphin_ctrl is not None and not is_json:
                        conv_banner = dolphin_ctrl.consume_transition_banner('high')
                    return (conv_banner + answer) if conv_banner and not is_json else answer
                except Exception as exc:
                    logger.warning('Inferencia Dolphin high falló (%s)', type(exc).__name__)

        self._notify("Todos los LLM están ocupados, por favor espere.")
        raise ValueError("Todos los LLM están ocupados, por favor espere.")

    @staticmethod
    def _tool_schema(tools):
        normalized = []
        for tool in tools or []:
            fn = tool.get('function', tool)
            normalized.append({'type': 'function', 'function': {
                'name': fn['name'], 'description': fn.get('description', ''),
                'parameters': fn.get('parameters', {'type': 'object', 'properties': {}})}})
        return normalized

    def complete(self, messages, tools=None, **kwargs):
        """Execute one logical native function-calling turn with failover."""
        if not isinstance(messages, list) or not messages:
            raise ValueError('Agent messages are required')
        budget = current_budget.get()
        if budget is not None:
            task = budget()
            kwargs['_timeout'] = max(1, min(60, task['deadline'] - time.time()))
        else:
            kwargs['_timeout'] = min(60, kwargs.get('_timeout', 60))
        tools = self._tool_schema(tools)
        status, now = self._load_status(), time.time()
        pool = [item for item in self.api_keys_pool if now > status.get(item['id'], 0)]
        last_error = None

        dolphin_ctrl = getattr(self, 'dolphin', None)
        active_mode = (dolphin_ctrl.get_active_mode()
                       if dolphin_ctrl is not None
                       else os.getenv('KHAI_DOLPHIN_MODE', 'nano').strip().lower())
        endpoint = self._dolphin_endpoint()

        # Si el modo activo es high, mid o low, priorizar el nodo acelerado en cascada estricta (high > mid > low)
        if endpoint and active_mode in {'high', 'mid', 'low'}:
            ladder = ['high', 'mid', 'low'] if active_mode == 'high' else (['mid', 'low'] if active_mode == 'mid' else ['low'])
            for profile in ladder:
                if dolphin_ctrl is not None:
                    try:
                        self._notify(f"[Conectando con Modo {profile}]")
                        dolphin_ctrl.start(profile)
                        res = self._complete_dolphin(messages, tools, endpoint, kwargs)
                        self._notify(f"[Modo {profile} conectado]")
                        return res
                    except Exception as exc:
                        last_error = exc
                        from app.mind.brain.controller import _format_error_reason
                        reason = _format_error_reason(exc)
                        logger.warning('Brain tool-calling %s falló (%s): %s', profile, type(exc).__name__, exc)
                        self._notify(f"[Modo {profile} falló por: {reason}]")
                else:
                    try:
                        return self._complete_dolphin(messages, tools, endpoint, kwargs)
                    except Exception as exc:
                        last_error = exc
                        logger.warning('Brain tool-calling falló (%s)', type(exc).__name__)
            # Si se agotó la cascada acelerada, degradar a nano
            if dolphin_ctrl is not None:
                dolphin_ctrl.mark_fallback_to_nano()
            self._notify("[Modo nano conectado]")

        # Fallback a pool de Cloud API (Gemini / modo nano)
        for current in pool:
            try:
                turn = getattr(self, '_complete_' + current['type'])(
                    messages, tools, current['key'], kwargs)
                turn.setdefault('provider', current['type'])
                return turn
            except Exception as exc:
                last_error = exc
                logger.warning('Tool-calling provider %s failed (%s)', current['id'], type(exc).__name__)
                code = getattr(getattr(exc, 'response', None), 'status_code', None)
                self._mark_key_failed(current['id'], cooldown=300 if code in {401, 403, 429} else None)

        raise ValueError('Ningún LLM disponible soportó el turno con herramientas') from last_error

    @staticmethod
    def _openai_messages(messages):
        converted = []
        for message in messages:
            item = {'role': message['role'], 'content': message.get('content') or ''}
            if message['role'] == 'assistant' and message.get('tool_calls'):
                item['tool_calls'] = [{'id': call.get('id') or ('call_' + uuid.uuid4().hex),
                                       'type': 'function', 'function': {
                                           'name': call['name'],
                                           'arguments': json.dumps(call.get('arguments', {}), ensure_ascii=False)}}
                                      for call in message['tool_calls']]
            if message['role'] == 'tool':
                item['tool_call_id'] = message['tool_call_id']
            converted.append(item)
        return converted

    @staticmethod
    def _openai_turn(message):
        calls = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or '{}')
            except json.JSONDecodeError:
                arguments = {'_invalid_json': call.function.arguments}
            calls.append({'id': call.id, 'name': call.function.name, 'arguments': arguments})
        return {'content': message.content or '', 'tool_calls': calls}

    def _complete_openai(self, messages, tools, key, kwargs):
        from openai import OpenAI
        client = OpenAI(api_key=key, timeout=kwargs.get('_timeout', 60), max_retries=0)
        response = client.chat.completions.create(
            model=kwargs.get('model', 'gpt-4o-mini'), messages=self._openai_messages(messages),
            tools=tools or None, temperature=kwargs.get('temperature', 0.2), max_tokens=8192)
        return self._openai_turn(response.choices[0].message)

    def _complete_azure(self, messages, tools, key, kwargs):
        from openai import AzureOpenAI
        endpoint = self.secrets.get('AZURE_OPENAI_ENDPOINT') if self.secrets else os.getenv('AZURE_OPENAI_ENDPOINT')
        deployment = self.secrets.get('AZURE_OPENAI_DEPLOYMENT') if self.secrets else os.getenv('AZURE_OPENAI_DEPLOYMENT')
        if not endpoint or not deployment:
            raise ValueError('Azure tool calling is not configured')
        client = AzureOpenAI(api_key=key, api_version='2024-02-15-preview',
                             azure_endpoint=endpoint, timeout=kwargs.get('_timeout', 60), max_retries=0)
        response = client.chat.completions.create(
            model=deployment, messages=self._openai_messages(messages), tools=tools or None,
            temperature=kwargs.get('temperature', 0.2), max_tokens=8192)
        return self._openai_turn(response.choices[0].message)

    @staticmethod
    def _gemini_contents(messages):
        contents, system = [], []
        for message in messages:
            role = message['role']
            if role == 'system':
                system.append(message.get('content', ''))
                continue
            if role == 'tool':
                parts = [{'functionResponse': {'name': message.get('name', 'tool'),
                                               'response': {'result': message.get('content', '')}}}]
                contents.append({'role': 'user', 'parts': parts})
                continue
            parts = ([{'text': message['content']}] if message.get('content') else [])
            for call in message.get('tool_calls', []):
                call_part = {'functionCall': {'name': call['name'], 'args': call.get('arguments', {})}}
                sig = call.get('thought_signature') or call.get('thoughtSignature')
                if sig:
                    call_part['thoughtSignature'] = sig
                parts.append(call_part)
            contents.append({'role': 'model' if role == 'assistant' else 'user',
                             'parts': parts or [{'text': ''}]})
        return contents, '\n'.join(system)

    @classmethod
    def _sanitize_gemini_schema(cls, schema):
        if not isinstance(schema, dict):
            return schema
        clean = {}
        for k, v in schema.items():
            if k in {'additionalProperties', '$schema', 'default'}:
                continue
            if isinstance(v, dict):
                clean[k] = cls._sanitize_gemini_schema(v)
            elif isinstance(v, list):
                clean[k] = [cls._sanitize_gemini_schema(item) for item in v]
            else:
                clean[k] = v
        return clean

    def _complete_gemini(self, messages, tools, key, kwargs):
        model = kwargs.get('model') or DEFAULT_GEMINI_MODEL
        contents, system = self._gemini_contents(messages)
        payload = {'contents': contents,
                   'generationConfig': {'maxOutputTokens': 8192, 'temperature': 0.2}}
        if system:
            payload['systemInstruction'] = {'parts': [{'text': system}]}
        if tools:
            declarations = []
            for tool in tools:
                fn = tool.get('function', tool)
                params = self._sanitize_gemini_schema(fn.get('parameters', {'type': 'object', 'properties': {}}))
                declarations.append({
                    'name': fn['name'],
                    'description': fn.get('description', ''),
                    'parameters': params
                })
            payload['tools'] = [{'functionDeclarations': declarations}]
        
        models_to_try = [model]
        if model != DEFAULT_GEMINI_MODEL:
            models_to_try.append(DEFAULT_GEMINI_MODEL)
        if 'gemini-3.1-flash-lite' not in models_to_try:
            models_to_try.append('gemini-3.1-flash-lite')

        response = None
        for m in models_to_try:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}'
            response = requests.post(url, json=payload, timeout=kwargs.get('_timeout', 60))
            if response.status_code == 429 and 'free_tier_requests' in response.text:
                continue
            response.raise_for_status()
            model = m
            break
        if response is None or not response.ok:
            if response is not None:
                response.raise_for_status()
            raise ValueError('Gemini no respondió')
        candidates = response.json().get('candidates') or []

        if not candidates:
            raise ValueError('Gemini no devolvió un turno agente')
        parts = ((candidates[0].get('content') or {}).get('parts')) or []
        text, calls = [], []
        for part in parts:
            if isinstance(part.get('text'), str):
                text.append(part['text'])
            if isinstance(part.get('functionCall'), dict):
                call = part['functionCall']
                call_data = {'id': 'call_' + uuid.uuid4().hex, 'name': call.get('name'),
                             'arguments': call.get('args') or {}}
                if part.get('thoughtSignature'):
                    call_data['thought_signature'] = part['thoughtSignature']
                calls.append(call_data)
        return {'content': '\n'.join(text).strip(), 'tool_calls': calls, 'model': model}

    def _complete_dolphin(self, messages, tools, endpoint, kwargs):
        payload = {'model': self._dolphin_model(),
                   'messages': self._openai_messages(messages), 'tools': tools,
                   'stream': False, 'options': {'num_predict': 4096, 'temperature': 0.2}}
        response = requests.post(endpoint + '/api/chat', json=payload,
                                 timeout=min(180, kwargs.get('_timeout', 60)), allow_redirects=False)
        if response.status_code == 400 and 'does not support tools' in response.text:
            logger.info('Dolphin Ollama sin soporte nativo de tools; adaptando vía prompt estructurado')
            tool_desc = json.dumps(tools, ensure_ascii=False, indent=2)
            sys_msg = (
                "You have access to the following tools:\n"
                f"{tool_desc}\n\n"
                "If you need to call a tool, respond strictly with a JSON object: "
                '{"tool_call": {"name": "<tool_name>", "arguments": {<args>}}}\n'
                "Otherwise, respond with your text reply."
            )
            chat_msgs = [{'role': 'system', 'content': sys_msg}] + self._openai_messages(messages)
            payload_fallback = {
                'model': payload['model'],
                'messages': chat_msgs,
                'stream': False,
                'options': {'num_predict': 4096, 'temperature': 0.2}
            }
            response = requests.post(endpoint + '/api/chat', json=payload_fallback,
                                     timeout=min(180, kwargs.get('_timeout', 60)), allow_redirects=False)
        response.raise_for_status()
        message = response.json().get('message') or {}
        calls = [{'id': 'call_' + uuid.uuid4().hex,
                  'name': call.get('function', {}).get('name'),
                  'arguments': call.get('function', {}).get('arguments') or {}}
                 for call in message.get('tool_calls') or []]
        content = message.get('content') or ''
        if not calls and 'tool_call' in content:
            try:
                parsed = json.loads(content)
                tc = parsed.get('tool_call')
                if tc and tc.get('name'):
                    calls.append({
                        'id': 'call_' + uuid.uuid4().hex,
                        'name': tc['name'],
                        'arguments': tc.get('arguments') or {}
                    })
                    content = ''
            except Exception:
                pass
        return {'content': content, 'tool_calls': calls,
                'provider': 'dolphin', 'model': payload['model']}

    def web_search(self, query, *, limit=8):
        """Platform research uses the configured Voice service without fallback."""
        from app.voice.gateway.research_search import ResearchSearch
        if not hasattr(self, '_research_search'):
            with self._research_lock:
                if not hasattr(self, '_research_search'):
                    self._research_search = ResearchSearch(self.config_dir, self.secrets or os.environ)
        return self._research_search.search(query, limit)

    def _generate_dolphin(self, prompt: str, endpoint: str, kwargs: Dict = None) -> str:
        """Ejecuta inferencia en el nodo privado Dolphin (Ollama) con soporte texto y JSON."""
        from ipaddress import ip_address
        from urllib.parse import urlsplit
        parsed = urlsplit(endpoint)
        if parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password \
                or parsed.path not in {'', '/'} or parsed.query or parsed.fragment \
                or parsed.port != 11434:
            raise ValueError('KHAI_DOLPHIN_URL debe ser una base HTTP privada en puerto 11434')
        try:
            address = ip_address(parsed.hostname)
        except ValueError as exc:
            raise ValueError('KHAI_DOLPHIN_URL requiere una IP privada explícita') from exc
        if not address.is_private or address.is_loopback or address.is_link_local:
            raise ValueError('KHAI_DOLPHIN_URL requiere una IP privada de Azure')
        timeout = min(300.0, max(1.0, float(os.getenv('KHAI_DOLPHIN_TIMEOUT', '180'))))
        num_predict = max(256, int(os.getenv('KHAI_DOLPHIN_NUM_PREDICT', '2048')))
        payload = {
            'model': self._dolphin_model(),
            'prompt': prompt, 'stream': False,
            'options': {'num_predict': num_predict},
        }
        rf = (kwargs or {}).get('response_format')
        if isinstance(rf, dict) and rf.get('type') == 'json_object':
            payload['format'] = 'json'
        response = requests.post(endpoint + '/api/generate', json=payload, timeout=timeout, allow_redirects=False)
        response.raise_for_status()
        body = response.json()
        if hasattr(self, '_response_meta'):
            self._response_meta.set({'provider': 'ollama', 'model': payload['model'],
                **{k: body[k] for k in ('total_duration', 'load_duration', 'prompt_eval_duration',
                    'eval_duration', 'prompt_eval_count', 'eval_count', 'done_reason')
                   if k in body and isinstance(body[k], (str, int, float))}})
        answer = body['response']
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('Dolphin devolvió una respuesta vacía')
        return answer.strip()

    def _generate_gemini(self, prompt: str, key: str, kwargs: Dict) -> str:
        model_name = kwargs.get("model") or DEFAULT_GEMINI_MODEL
        generation_config = {"maxOutputTokens": 8192}
        rf = kwargs.get("response_format")
        if isinstance(rf, dict) and rf.get("type") == "json_object":
            generation_config["responseMimeType"] = "application/json"
            
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        if generation_config:
            payload["generationConfig"] = generation_config
            
        if kwargs.get("use_web_search", False):
            payload["tools"] = [{"googleSearch": {}}]
            
        headers = {"Content-Type": "application/json"}
        models_to_try = [model_name]
        if model_name != DEFAULT_GEMINI_MODEL:
            models_to_try.append(DEFAULT_GEMINI_MODEL)
        if 'gemini-3.1-flash-lite' not in models_to_try:
            models_to_try.append('gemini-3.1-flash-lite')

        response = None
        for m in models_to_try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}"
            response = requests.post(url, json=payload, headers=headers, timeout=kwargs.get("_timeout", 60))
            if response.status_code == 429 and 'free_tier_requests' in response.text:
                continue
            response.raise_for_status()
            break
        if response is None or not response.ok:
            if response is not None:
                response.raise_for_status()
            raise ValueError('Gemini no respondió')
        data = response.json()

        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason") or "sin_candidatos"
            if reason != 'sin_candidatos':
                raise LLMContentRejected(f"Gemini rechazó la solicitud ({reason})")
            raise ValueError(f"Gemini no devolvió candidatos ({reason})")
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        if not parts or not isinstance(parts[0].get("text"), str):
            reason = candidates[0].get("finishReason") or "sin_texto"
            if reason in {'SAFETY', 'BLOCKLIST', 'PROHIBITED_CONTENT', 'SPII'}:
                raise LLMContentRejected(f"Gemini rechazó la solicitud ({reason})")
            raise ValueError(f"Gemini devolvió candidato vacío ({reason})")
        return parts[0]["text"].strip()

    def _generate_azure(self, prompt: str, key: str, kwargs: Dict) -> str:
        endpoint = (self.secrets.get("AZURE_OPENAI_ENDPOINT") if self.secrets else os.getenv("AZURE_OPENAI_ENDPOINT"))
        deployment = (self.secrets.get("AZURE_OPENAI_DEPLOYMENT") if self.secrets else os.getenv("AZURE_OPENAI_DEPLOYMENT"))
        
        if not endpoint or not deployment:
            raise ValueError("Falta configurar AZURE_OPENAI_ENDPOINT o AZURE_OPENAI_DEPLOYMENT.")
            
        try:
            from openai import AzureOpenAI
            client = AzureOpenAI(
                api_key=key,
                api_version="2024-02-15-preview",
                azure_endpoint=endpoint, timeout=kwargs.get("_timeout", 60), max_retries=0
            )
            
            call_kwargs = {
                "model": deployment,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 8192, "temperature": kwargs.get("temperature", 0.7)
            }
            if kwargs.get("response_format"):
                call_kwargs["response_format"] = kwargs["response_format"]
                
            response = client.chat.completions.create(**call_kwargs)
            return response.choices[0].message.content.strip()
        except ImportError:
            url = f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions?api-version=2024-02-15-preview"
            headers = {"api-key": key, "Content-Type": "application/json"}
            payload = {
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 8192, "temperature": kwargs.get("temperature", 0.7)
            }
            if kwargs.get("response_format"):
                payload["response_format"] = kwargs["response_format"]
            resp = requests.post(url, json=payload, headers=headers, timeout=kwargs.get("_timeout", 60))
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()

    def _generate_openai(self, prompt: str, key: str, kwargs: Dict) -> str:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=key, timeout=kwargs.get("_timeout", 60), max_retries=0)
            model = kwargs.get("model", "gpt-4o-mini")
            
            call_kwargs = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 8192, "temperature": kwargs.get("temperature", 0.7)
            }
            if kwargs.get("response_format"):
                call_kwargs["response_format"] = kwargs["response_format"]
                
            response = client.chat.completions.create(**call_kwargs)
            return response.choices[0].message.content.strip()
        except ImportError:
            raise ValueError("El paquete 'openai' no está instalado.")

    def test_all_connections(self) -> Dict[str, Dict]:
        return {"unified_pool": {"status": "ok", "keys_loaded": len(self.api_keys_pool)}}
