import os
import time
import logging
import requests
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
