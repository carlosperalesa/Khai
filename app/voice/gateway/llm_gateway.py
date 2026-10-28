import os
import time
import logging
import requests
from typing import Dict
from app.pixy.budget import current_budget

logger = logging.getLogger("LLMManager")

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
        if self.provider_db:
            from app.kernel.database import connection
            with connection(self.provider_db) as conn:
                return dict(conn.execute("SELECT key_id,cooldown_until FROM provider_status"))
        return self.memory.get_provider_status()

    def _mark_key_failed(self, key_id: str):
        cooldown_until = time.time() + self.cooldown_seconds
        if self.provider_db:
            from app.kernel.database import connection
            with connection(self.provider_db) as conn:
                conn.execute("INSERT OR REPLACE INTO provider_status VALUES (?,?)",(key_id,cooldown_until))
        else:
            self.memory.set_provider_cooldown(key_id, cooldown_until)

    def generate_content(self, prompt: str, **kwargs) -> str:
        """Genera contenido iterando sobre el pool global de llaves (Unified Fallback)."""
        if len(prompt) > 100000:
            raise ValueError('Contexto excede el límite de 100000 caracteres por llamada')
        is_conv = kwargs.get('purpose') == 'conversation'
        conv_banner = ''
        if is_conv:
            dolphin_ctrl = getattr(self, 'dolphin', None)
            active_mode = dolphin_ctrl.get_active_mode() if dolphin_ctrl is not None else None
            endpoint = os.getenv('KHAI_DOLPHIN_URL', '').strip().rstrip('/')
            if endpoint and (active_mode in {None, 'mid', 'high'}):
                try:
                    answer = self._generate_dolphin(prompt, endpoint)
                    if dolphin_ctrl is not None and active_mode in {'mid', 'high'}:
                        conv_banner = dolphin_ctrl.consume_transition_banner(active_mode)
                    return conv_banner + answer
                except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                    logger.warning('Dolphin no disponible (%s); se usará el proveedor de respaldo',
                                   type(exc).__name__)
                    if dolphin_ctrl is not None:
                        dolphin_ctrl.mark_fallback_to_low()
                        conv_banner = dolphin_ctrl.consume_transition_banner('low')
            elif dolphin_ctrl is not None:
                conv_banner = dolphin_ctrl.consume_transition_banner('low')
        status = self._load_status()
        now = time.time()
        
        # Opciones avanzadas o forzadas (ej: para Pixy VIP channel)
        forced_key = kwargs.get("forced_key")
        forced_type = kwargs.get("forced_type")
        
        if forced_key and forced_type:
            pool = [{"type": forced_type, "key": forced_key, "id": "FORCED_KEY"}]
        else:
            pool = self.api_keys_pool

        for current_cred in pool:
            if time.time() <= status.get(current_cred['id'], 0):
                continue
            budget = current_budget.get()
            if budget is not None:
                task = budget()
                kwargs['_timeout'] = max(1, min(60, task['deadline'] - time.time()))
            else:
                kwargs['_timeout'] = min(60, kwargs.get('_timeout', 60))
            try:
                generate = getattr(self, '_generate_' + current_cred['type'])
                result = generate(prompt, current_cred['key'], kwargs)
                return (conv_banner + result) if is_conv and conv_banner else result
            except Exception as exc:
                # Never print credentials, URLs with keys, or full SDK exception payloads.
                logger.warning('Proveedor %s falló (%s)', current_cred['id'], type(exc).__name__)
                self._mark_key_failed(current_cred['id'])
        raise ValueError("Unified Fallback agotó todos los intentos y llaves.")

    def _generate_dolphin(self, prompt: str, endpoint: str) -> str:
        """Dolphin solo redacta conversación; el enrutamiento y las decisiones usan Gemini."""
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
        timeout = min(30.0, max(1.0, float(os.getenv('KHAI_DOLPHIN_TIMEOUT', '8'))))
        response = requests.post(endpoint + '/api/generate', json={
            'model': os.getenv('KHAI_DOLPHIN_MODEL', 'dolphin3:8b'),
            'prompt': prompt, 'stream': False,
            'options': {'num_predict': 400},
        }, timeout=timeout, allow_redirects=False)
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
        return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

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
