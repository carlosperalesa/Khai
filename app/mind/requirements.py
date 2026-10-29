"""Bounded, domain-neutral clarification of an owner's requested outcome."""
import json
import re


def _questions(value):
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value[:4]:
        if not isinstance(item, str):
            continue
        q = item.strip()[:300]
        if not q:
            continue
        if len(q) < 8:
            q = f'¿Puedes aclarar: {q}?'
        cleaned.append(q)
    return cleaned


class RequirementsPlanner:
    def __init__(self, llm):
        self.llm = llm

    def questions(self, request, input_schema=None):
        template = {"questions": []}
        prompt = (
            'Antes de crear o ejecutar una herramienta, identifica solo datos que falten '
            'para satisfacer el objetivo. No inventes valores ni hagas preguntas de permisos. '
            'Devuelve JSON siguiendo esta plantilla:\n'
            + json.dumps(template, ensure_ascii=False) + '\n'
            'Si la petición ya es completa, devuelve {"questions": []}. Máximo cuatro '
            'preguntas breves. Campos obligatorios de la skill: '
            + json.dumps(input_schema or {}, ensure_ascii=False)
            + '. Objetivo: ' + request
        )
        raw = self.llm.generate_content(prompt, response_format={'type': 'json_object'})
        result = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(result, dict):
            return []
        return _questions(result.get('questions', []))

    def resolve(self, request, questions, answer, previous=None):
        template = {
            "purpose": "Objetivo completo actualizado con la respuesta del propietario",
            "required_output": ["Resultado observable esperado"],
            "params": {},
            "missing_questions": []
        }
        prompt = (
            'Integra la respuesta del propietario con el objetivo sin añadir requisitos '
            'que no haya pedido. Conserva los requisitos de la revisión anterior '
            'salvo que el propietario los cambie explícitamente. Devuelve JSON siguiendo esta plantilla:\n'
            + json.dumps(template, ensure_ascii=False) + '\n'
            'Objetivo: ' + request + '\nPreguntas: '
            + json.dumps(questions, ensure_ascii=False) + '\nRevisión anterior: '
            + json.dumps(previous or {}, ensure_ascii=False) + '\nRespuesta: ' + answer
        )
        raw = self.llm.generate_content(prompt, response_format={'type': 'json_object'})
        result = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(result, dict):
            result = {}
        purpose = str(result.get('purpose') or f"{request} ({answer})").strip()[:3000]
        if len(purpose) < 8:
            purpose = f"Objetivo confirmado: {purpose or request}"[:3000]
        raw_outputs = result.get('required_output')
        if isinstance(raw_outputs, list) and raw_outputs:
            outputs = [str(item).strip()[:200] for item in raw_outputs[:12] if len(str(item).strip()) >= 2]
        else:
            outputs = []
        if not outputs:
            outputs = [purpose[:160]]
        raw_params = result.get('params') if isinstance(result.get('params'), dict) else {}
        params = {}
        for k, v in list(raw_params.items())[:32]:
            ck = re.sub(r'[^a-zA-Z0-9_]', '_', str(k).strip())
            if not ck or not ck.isidentifier() or v is None:
                continue
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            params[ck] = v
        return {
            'purpose': purpose,
            'required_output': outputs,
            'params': params,
            'missing_questions': _questions(result.get('missing_questions', [])),
        }
