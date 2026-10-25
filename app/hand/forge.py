"""Code generation only; Coordinator owns scheduling, approval and execution."""
import ast
import json
from pathlib import Path
from app.voice.runtime.contract import digest, skill_metadata


class ApprovalRequiredException(Exception):
    def __init__(self, action_type, message):
        self.action_type, self.message = action_type, message
        super().__init__(message)


class BaseSkill:
    """Compatibility API; the sandbox supplies a narrow implementation."""
    def __init__(self, task_id, state_manager, telegram):
        self.task_id, self.state, self.telegram = task_id, state_manager, telegram

    def run_step(self, name, fn, *args, **kwargs):
        if self.state.is_step_completed(self.task_id, name):
            return self.state.get_step_data(self.task_id, name)
        result = fn(*args, **kwargs)
        self.state.save_step(self.task_id, name, result)
        return result

    def require_approval(self, action_type, details):
        raise ApprovalRequiredException(action_type, details)


class SkillForge:
    def __init__(self, llm, data_dir='data/skills'):
        self.llm, self.data_dir = llm, Path(data_dir)

    def generate(self, task, guidelines, hypothesis=None):
        prompt = f'''Escribe Python autónomo para el objetivo: {task['request']}
Devuelve SOLO un bloque de código Python. Primera línea: # MODE: SAFE (script corto),
# MODE: AGENT (servicio) o # MODE: GOD (DynamicSkill derivada de BaseSkill).
Segunda línea opcional: # PIP: paquete==version. No uses shell ni instrucciones Docker.
El entorno está aislado en contenedor; no uses subprocess, ctypes, multiprocessing ni librerías de escape del sistema.
Si el manifiesto declara la capacidad 'Network', puedes usar urllib.request, requests o httpx para consultar APIs públicas externas directamente.
El script debe manejar posibles errores e imprimir el resultado claro y formateado en stdout, o escribir en /app/data/public.
NO uses emoticones ni emojis en las salidas impresas ni en el código.
Los servicios AGENT escuchan en 8080 dentro del contenedor; no se publican puertos.
GOD permite la API BaseSkill desde modules.skill_forge; tampoco da permisos al host.
No uses BaseSkill en SAFE. No inventes aprobaciones o resultados verificados.
Manifiesto: {json.dumps(task.get('manifest', {}), ensure_ascii=False)}
Reglas aplicables: {json.dumps(guidelines, ensure_ascii=False)}
Hipótesis a probar: {hypothesis or ''}
Resultado/rechazo anterior: {str(task.get('feedback', ''))[:8000]}
Código anterior: {str(task.get('code', ''))[:24000]}
'''
        code = self.llm.generate_content(prompt)
        if '```python' in code:
            code = code.split('```python', 1)[1].split('```', 1)[0]
        elif '```' in code:
            code = code.split('```', 1)[1].split('```', 1)[0]
        code = code.strip()
        ast.parse(code)
        mode, deps = skill_metadata(code)
        return code, mode, deps

    def snapshot(self, code):
        root = self.data_dir.parent / 'artifacts'
        root.mkdir(parents=True, exist_ok=True)
        raw = code.encode('utf-8')
        sha = digest(raw)
        path = root / (sha + '.py')
        try:
            with path.open('xb') as stream:
                stream.write(raw)
        except FileExistsError:
            if digest(path.read_bytes()) != sha:
                raise ValueError('Artefacto existente alterado')
        return str(path.resolve()), sha
