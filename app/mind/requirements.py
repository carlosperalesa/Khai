"""Bounded, domain-neutral clarification of an owner's requested outcome."""
import json


def _questions(value):
    if not isinstance(value, list) or len(value) > 4:
        raise ValueError('Invalid clarification questions')
    if any(not isinstance(item, str) or not 8 <= len(item.strip()) <= 300 for item in value):
        raise ValueError('Invalid clarification question')
    return [item.strip() for item in value]


class RequirementsPlanner:
    def __init__(self, llm):
        self.llm = llm

    def questions(self, request, input_schema=None):
        prompt = (
            'Antes de crear o ejecutar una herramienta, identifica solo datos que falten '
            'para satisfacer el objetivo y criterios observables de salida. No inventes '
            'valores ni hagas preguntas de permisos. Devuelve JSON {"questions": [string]}. '
            'Si la petición ya es completa, devuelve una lista vacía. Máximo cuatro '
            'preguntas breves, agrupadas si es natural. Pide datos concretos, no '
            'una segunda confirmación de permiso. Campos obligatorios aún sin valor '
            'de una skill existente: ' + json.dumps(input_schema or {}, ensure_ascii=False)[:2000] +
            '. Objetivo: ' + request[:3000]
        )
        result = json.loads(self.llm.generate_content(prompt, response_format={'type': 'json_object'}))
        return _questions(result['questions'])

    def resolve(self, request, questions, answer, previous=None):
        prompt = (
            'Integra la respuesta del propietario con el objetivo sin añadir requisitos '
            'que no haya pedido. Conserva los requisitos de la revisión anterior '
            'salvo que el propietario los cambie explícitamente. Devuelve JSON con purpose (objetivo completo), '
            'required_output (lista de resultados observables), params (objeto de '
            'parámetros declarados), missing_questions (lista de preguntas si persiste '
            'una ambigüedad esencial). La respuesta del propietario confirma el alcance; '
            'no pidas una segunda confirmación. No inventes datos. Máximo cuatro '
            'preguntas adicionales. Objetivo: ' + request[:3000] + '\nPreguntas: ' +
            json.dumps(questions, ensure_ascii=False) + '\nRevisión anterior: ' +
            json.dumps(previous or {}, ensure_ascii=False)[:3000] + '\nRespuesta: ' + answer[:3000]
        )
        result = json.loads(self.llm.generate_content(prompt, response_format={'type': 'json_object'}))
        if not isinstance(result, dict) or not isinstance(result.get('purpose'), str):
            raise ValueError('Invalid resolved objective')
        purpose = result['purpose'].strip()
        outputs = result.get('required_output')
        params = result.get('params')
        if not 8 <= len(purpose) <= 3000 or not isinstance(outputs, list) or len(outputs) > 12:
            raise ValueError('Invalid objective scope')
        if any(not isinstance(item, str) or not 2 <= len(item.strip()) <= 200 for item in outputs):
            raise ValueError('Invalid required output')
        if not isinstance(params, dict) or len(params) > 32 or any(
                not isinstance(key, str) or not key.isidentifier()
                or isinstance(value, (dict, list)) or value is None
                for key, value in params.items()):
            raise ValueError('Invalid scope parameters')
        return {'purpose': purpose, 'required_output': [item.strip() for item in outputs],
                'params': params, 'missing_questions': _questions(result.get('missing_questions', []))}
