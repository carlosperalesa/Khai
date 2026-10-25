import os
import logging
import json
from app.voice.runtime.contract import proxy_url, proxy_headers

logger = logging.getLogger("CoreEyes")

class CoreEyes:
    """
    CoreEyes V3: Módulo de observabilidad segura estricta.
    Khai NO puede ejecutar comandos shell ni proveer rutas de archivos.
    Solo puede solicitar IDs abstractos de recursos (resource_id).
    """
    
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        # Lista Blanca Estricta de recursos estáticos
        self.static_resources = {
            "core_logs": os.path.join(data_dir, "logs", "openclaw.log"),
            "publisher_logs": os.path.join(data_dir, "logs", "publisher.log"),
            "proxy_config": "config/proxy.json",
            "system_config": "config/config.yaml"
        }

    def observe(self, resource_id: str, reason: str = "Unspecified") -> str:
        """Khai solicita leer un recurso seguro usando su ID y proveyendo una razón."""
        logger.info(f"[CoreEyes] Solicitud de lectura: {resource_id} | Razón: {reason}")
        
        resource_id = resource_id.strip()
        
        # 1. Logs dinámicos (agentes u openclaw core proxy)
        if resource_id.startswith("agent_logs_") or resource_id == "proxy_logs":
            return self._read_docker_logs(resource_id)
        
        # 2. Listado de todos los contenedores activos (para diagnóstico)
        if resource_id == "running_containers":
            return self._list_containers()

        # 2b. Estado de solo lectura de los contenedores propios (core/proxy)
        if resource_id in ("container_state_core", "container_state_proxy"):
            return self._container_state(resource_id.rsplit("_", 1)[1])
        
        # 3. Recursos de la whitelist estática (rutas locales)
        if resource_id not in self.static_resources:
            logger.warning(f"[CoreEyes] Acceso denegado a recurso: {resource_id}")
            return f"❌ [CoreEyes] Acceso denegado o recurso inexistente: '{resource_id}'. IDs válidos: {', '.join(list(self.static_resources.keys()) + ['agent_logs_<nombre>', 'running_containers', 'container_state_core', 'container_state_proxy'])}"
            
        path = self.static_resources[resource_id]
        
        if not os.path.exists(path):
            return f"❌ [CoreEyes] El recurso '{resource_id}' no contiene datos actualmente (archivo no existe: {path})."
            
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
                if not content.strip():
                    return f"[CoreEyes] Recurso '{resource_id}' está vacío."
                # Tail de 4000 chars para no desbordar contexto
                return content[-4000:]
                
        except Exception as e:
            logger.error(f"[CoreEyes] Error leyendo {path}: {e}")
            return f"❌ [CoreEyes] Error interno de lectura: {e}"

    def observe_multiple(self, resource_ids: list, reason: str = "Unspecified") -> dict:
        """Lee múltiples recursos y retorna un dict con los resultados."""
        return {rid: self.observe(rid, reason) for rid in resource_ids}

    def _read_docker_logs(self, resource_id: str) -> str:
        """Obtiene logs de un contenedor usando Docker Proxy mediante resource_id abstracto."""
        import requests
        import re
        if not re.match(r"^[a-zA-Z0-9_.-]+$", resource_id):
            return "❌ [CoreEyes] Resource ID inválido."
            
        endpoint = proxy_url("docker-logs")
        try:
            resp = requests.post(endpoint, headers=proxy_headers(), json={"resource_id": resource_id, "tail": 100}, timeout=15)
            if resp.status_code == 200:
                output = resp.json().get("logs", "").strip()
                if not output:
                    return f"[CoreEyes] Logs de '{resource_id}' están vacíos."
                return output[-4000:]
            return f"❌ [CoreEyes] No se pudo leer '{resource_id}'. Puede que no exista o el proxy falló."
        except requests.exceptions.Timeout:
            return "❌ [CoreEyes] Timeout leyendo logs vía proxy."
        except Exception as e:
            return f"❌ [CoreEyes] Error leyendo docker logs: {e}"

    def _container_state(self, target: str) -> str:
        """docker inspect resumido vía proxy; nunca modifica el contenedor."""
        import requests
        try:
            resp = requests.get(proxy_url("containers/" + target + "/state"), headers=proxy_headers(), timeout=15)
            if resp.status_code == 200:
                return json.dumps(resp.json(), ensure_ascii=False)
            return f"❌ [CoreEyes] Estado de '{target}' no disponible: Status {resp.status_code}"
        except Exception as e:
            return f"❌ [CoreEyes] Error conectando con el proxy: {type(e).__name__}"

    def _list_containers(self) -> str:
        """Lista contenedores activos y muertos recientes vía proxy."""
        import requests
        endpoint = proxy_url("docker-ps")
        try:
            resp = requests.get(endpoint, headers=proxy_headers(), timeout=15)
            if resp.status_code == 200:
                return json.dumps(resp.json().get("ps", []), ensure_ascii=False)
            return f"❌ [CoreEyes] Error listando contenedores: Status {resp.status_code}"
        except Exception as e:
            return f"❌ [CoreEyes] Error conectando con el proxy: {e}"
