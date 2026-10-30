import os
import time
import logging
import requests
import json
import uuid
from typing import Dict
from app.pixy.budget import current_budget

logger = logging.getLogger("LLMManager")


class LLMContentRejected(ValueError):
    """The provider made a terminal content decision, not an availability failure."""


class LLMManager:
    """Fachada global del sistema LLM con Unified Fallback."""
    
    def __init__(self, config_dir: str, data_dir: str, memory=None, secrets=None, provider_db=None, dolphin=None):
        self.secrets = secrets
        self.provider_db = provider_db
        self.dolphin = dolphin
        if provider_db:
            from app.kernel.database import connection
            with connection(provider_db) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS provider_status(key_id TEXT PRIMARY KEY,cooldown_until REAL)")
            from app.kernel.database import adopt_schema
            adopt_schema(provider_db,'provider-status',['provider_status'])
        self.config_dir = config_dir
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

    def _notify(self, message: str) -> None:
        notifier = getattr(self, 'notifier', None)
        if callable(notifier):
            try:
                notifier(message)
            except Exception as exc:
                logger.warning('No se pudo enviar notificación de LLM: %s', exc)

    def generate_content(self, prompt: str, **kwargs) -> str:
        """Genera contenido en cascada según el modo activo (mid/high -> Dolphin -> Cloud; low -> Cloud -> Dolphin)."""
        rf = kwargs.get('response_format')
        is_json = isinstance(rf, dict) and rf.get('type') == 'json_object'
        dolphin_ctrl = getattr(self, 'dolphin', None)
        active_mode = (dolphin_ctrl.get_active_mode()
                       if dolphin_ctrl is not None
                       else os.getenv('KHAI_DOLPHIN_MODE', 'low').strip().lower())
        endpoint = self._dolphin_endpoint()
        conv_banner = ''
        tried_dolphin = False

        if endpoint and active_mode in {'mid', 'high'}:
            tried_dolphin = True
            primary_label = 'Medium' if active_mode == 'mid' else 'High'
            secondary_profile = 'high' if active_mode == 'mid' else 'mid'
            secondary_label = 'High' if active_mode == 'mid' else 'Medium'
            try:
                answer = self._call_dolphin(prompt, endpoint, kwargs)
                if dolphin_ctrl is not None and not is_json:
                    conv_banner = dolphin_ctrl.consume_transition_banner(active_mode)
                return (conv_banner + answer) if conv_banner and not is_json else answer
            except Exception as exc:
                logger.warning('Dolphin %s no disponible (%s); se usará el proveedor de respaldo en cascada',
                               active_mode, type(exc).__name__)
                answer = None
                if active_mode == 'mid' and dolphin_ctrl is not None:
                    self._notify(f"Dolphin {primary_label} no responde, pasando a Dolphin {secondary_label}")
                    self._notify(f"Conectando con Dolphin {secondary_label}")
                    try:
                        dolphin_ctrl.start(secondary_profile)
                        answer = self._call_dolphin(prompt, endpoint, kwargs)
                    except Exception as exc_alt:
                        logger.warning('Dolphin %s también falló (%s)', secondary_profile, type(exc_alt).__name__)

                if answer is not None:
                    if dolphin_ctrl is not None and not is_json:
                        conv_banner = dolphin_ctrl.consume_transition_banner(secondary_profile)
                    return (conv_banner + answer) if conv_banner and not is_json else answer

                if dolphin_ctrl is not None:
                    dolphin_ctrl.mark_fallback_to_low()
                self._notify("Dolphin no respondió; pasando a Gemini")
                if not is_json:
                    conv_banner = (dolphin_ctrl.consume_transition_banner('low')
                                   + 'Dolphin no respondió; continué con Gemini.\n')
        elif dolphin_ctrl is not None and not is_json:
            conv_banner = dolphin_ctrl.consume_transition_banner('low')

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
        endpoint = self._dolphin_endpoint()
        dolphin_ctrl = getattr(self, 'dolphin', None)
        if endpoint:
            if dolphin_ctrl is not None:
                for profile in ('mid', 'high'):
                    try:
                        self._notify(f"Conectando con Dolphin {profile.capitalize()}")
                        dolphin_ctrl.start(profile)
                        return self._complete_dolphin(messages, tools, endpoint, kwargs)
                    except Exception as exc:
                        last_error = exc
                        logger.warning('Dolphin tool-calling %s unavailable (%s)', profile, type(exc).__name__)
            else:
                try:
                    return self._complete_dolphin(messages, tools, endpoint, kwargs)
                except Exception as exc:
                    last_error = exc
                    logger.warning('Dolphin tool-calling unavailable (%s)', type(exc).__name__)
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
                parts.append({'functionCall': {'name': call['name'], 'args': call.get('arguments', {})}})
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
        model = kwargs.get('model', 'gemini-2.5-flash')
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
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}'
        response = requests.post(url, json=payload, timeout=kwargs.get('_timeout', 60))
        response.raise_for_status()
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
                calls.append({'id': 'call_' + uuid.uuid4().hex, 'name': call.get('name'),
                              'arguments': call.get('args') or {}})
        return {'content': '\n'.join(text).strip(), 'tool_calls': calls, 'model': model}

    def _complete_dolphin(self, messages, tools, endpoint, kwargs):
        payload = {'model': os.getenv('KHAI_DOLPHIN_MODEL', 'dolphin3:8b'),
                   'messages': self._openai_messages(messages), 'tools': tools,
                   'stream': False, 'options': {'num_predict': 4096, 'temperature': 0.2}}
        response = requests.post(endpoint + '/api/chat', json=payload,
                                 timeout=min(180, kwargs.get('_timeout', 60)), allow_redirects=False)
        response.raise_for_status()
        message = response.json().get('message') or {}
        calls = [{'id': 'call_' + uuid.uuid4().hex,
                  'name': call.get('function', {}).get('name'),
                  'arguments': call.get('function', {}).get('arguments') or {}}
                 for call in message.get('tool_calls') or []]
        return {'content': message.get('content') or '', 'tool_calls': calls,
                'provider': 'dolphin', 'model': payload['model']}

    def web_search(self, query, *, limit=8):
        """Platform research. Responses without sources are failures."""
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError('Invalid research query')
        status, now = self._load_status(), time.time()
        gemini_pool = [item for item in self.api_keys_pool if item['type'] == 'gemini' and now > status.get(item['id'], 0)]
        if not gemini_pool:
            gemini_pool = [item for item in self.api_keys_pool if item['type'] == 'gemini']
        if not gemini_pool:
            raise ValueError('No research provider is configured')
        model = os.getenv('KHAI_RESEARCH_MODEL', 'gemini-2.5-flash')
        payload = {'contents': [{'parts': [{'text': query}]}], 'tools': [{'googleSearch': {}}],
                   'generationConfig': {'maxOutputTokens': 2048, 'temperature': 0.1}}
        last_error = None
        for item in gemini_pool:
            try:
                url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={item["key"]}'
                response = requests.post(url, json=payload, timeout=60)
                response.raise_for_status()
                candidate = (response.json().get('candidates') or [{}])[0]
                results = []
                for chunk in (candidate.get('groundingMetadata') or {}).get('groundingChunks') or []:
                    web = chunk.get('web') or {}
                    if web.get('uri') and web.get('title'):
                        results.append({'title': web['title'], 'url': web['uri'], 'description': ''})
                if not results:
                    raise ValueError('El proveedor de investigación no entregó fuentes verificables')
                return {'provider': 'google', 'query': query,
                        'results': results[:max(1, min(10, int(limit)))]}
            except Exception as exc:
                last_error = exc
                code = getattr(getattr(exc, 'response', None), 'status_code', None)
                self._mark_key_failed(item['id'], cooldown=300 if code in {401, 403, 429} else None)
        raise ValueError('El proveedor de investigación no entregó fuentes verificables') from last_error

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
        timeout = min(180.0, max(1.0, float(os.getenv('KHAI_DOLPHIN_TIMEOUT', '90'))))
        num_predict = max(256, int(os.getenv('KHAI_DOLPHIN_NUM_PREDICT', '2048')))
        payload = {
            'model': os.getenv('KHAI_DOLPHIN_MODEL', 'dolphin3:8b'),
            'prompt': prompt, 'stream': False,
            'options': {'num_predict': num_predict},
        }
        rf = (kwargs or {}).get('response_format')
        if isinstance(rf, dict) and rf.get('type') == 'json_object':
            payload['format'] = 'json'
        response = requests.post(endpoint + '/api/generate', json=payload, timeout=timeout, allow_redirects=False)
        response.raise_for_status()
        answer = response.json()['response']
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('Dolphin devolvió una respuesta vacía')
        return answer.strip()

    def _generate_gemini(self, prompt: str, key: str, kwargs: Dict) -> str:
        model_name = kwargs.get("model", "gemini-2.5-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}"
        
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
        response = requests.post(url, json=payload, headers=headers, timeout=kwargs.get("_timeout", 60))
        response.raise_for_status()
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
