"""Deterministic bindings and typed receipts for a recipe within one objective."""
import json
import re

from app.mind.skill_selector import validate_params
from app.mind.verifier import matches_object_schema


class RevisionPlanner:
    """Locate the action challenged by owner feedback; never authorize an effect."""

    def __init__(self, llm):
        self.llm = llm

    def target(self, goal, feedback, recipe, receipts):
        steps = []
        by_id = {item['step_id']: item for item in receipts}
        for step in recipe['steps']:
            receipt = by_id.get(step['id'], {})
            steps.append({'id': step['id'], 'output': receipt.get('output'),
                          'external_effects': [{key: effect.get(key) for key in (
                              'effect', 'status', 'resource_id', 'summary',
                              'parent_request_id', 'item_index')}
                              for effect in receipt.get('external_effects', [])]})
        prompt = (
            'Interpreta solo el feedback final del propietario e identifica qué acción cuestiona. '
            'No investigues ni juzgues por tu cuenta la calidad de las otras acciones. '
            'Devuelve JSON {"step_id":string|null,"problem":"missing"|"incorrect"|"other"}. '
            'Usa null si la corrección afecta varias acciones o no es clara. '
            '"missing" significa que el propietario afirma que el efecto no ocurrió; '
            'no implica que sea seguro repetirlo. Conserva las demás como provisionales '
            'hasta el y final del propietario. '
            'Objetivo: ' + goal[:2000] + '\nCorrección: ' + feedback[:2000] +
            '\nAcciones y recibos: ' + json.dumps(steps, ensure_ascii=False)[:5000])
        raw = self.llm.generate_content(prompt, response_format={'type': 'json_object'})
        result = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(result, dict) or set(result) != {'step_id', 'problem'}:
            raise ValueError('Invalid owner correction classification')
        step_id = result['step_id']
        if step_id is not None and step_id not in {step['id'] for step in recipe['steps']}:
            raise ValueError('Correction targets an unknown action')
        if result['problem'] not in {'missing', 'incorrect', 'other'}:
            raise ValueError('Invalid correction problem')
        return result

    def effect_index(self, answer, count, allow_llm=True):
        """Interpret an owner's relative-order answer, never infer resource contents."""
        if count < 2:
            return None
        normalized = answer.casefold()
        words = {
            1: ('primera', 'primero', 'primer'),
            2: ('segunda', 'segundo'),
            3: ('tercera', 'tercero', 'tercer'),
        }
        matches = {index for index, aliases in words.items() if index <= count
                   and any(re.search(r'\b' + word + r'\b', normalized) for word in aliases)}
        if re.search(r'\b(última|ultimo|último|ultima)\b', normalized):
            matches.add(count)
        if len(matches) == 1:
            return matches.pop()
        if answer.strip() in {str(i) for i in range(1, count + 1)}:
            return int(answer.strip())
        if matches or not allow_llm or self.llm is None:
            return None
        prompt = (
            'Interpreta exclusivamente si la respuesta del propietario identifica '
            'por ORDEN una de las escrituras registradas. No conoces sus títulos '
            'ni contenidos. Si la respuesta menciona solo un título, una fecha o '
            'cualquier atributo sin vincularlo a una posición, devuelve null. '
            'No elijas por defecto. Devuelve exactamente JSON '
            '{"index":entero|null,"evidence":texto|null}; evidence debe ser '
            'una cita literal breve de la expresión de orden del propietario. '
            'con índice desde 1 hasta ' + str(count) + '. Respuesta: ' + answer[:500])
        try:
            raw = self.llm.generate_content(prompt, response_format={'type': 'json_object'})
            result = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(result, dict) or set(result) != {'index', 'evidence'}:
                return None
            index, evidence = result['index'], result['evidence']
            if (type(index) is not int or not 1 <= index <= count
                    or not isinstance(evidence, str)
                    or evidence.casefold() not in normalized
                    or not re.search(r'\b(despu[eé]s|antes|final|inicio|reciente|anterior|posterior|últim[ao]|ultim[ao]|primer[ao]?|segund[ao]|tercer[ao]?)\b',
                                     evidence.casefold())):
                return None
            return index
        except Exception:
            return None

    def effect_inputs(self, answer, questions, required=None):
        """Identify relevant correction fields, accepting only literal owner evidence."""
        if not questions:
            return {'required': [], 'provided': {}}
        if self.llm is None:
            return None
        prompt = (
            'La persona describe una corrección de un efecto externo. Selecciona solo '
            'los parámetros necesarios para esa corrección, según sus descripciones. '
            'No selecciones otros parámetros de la misma operación. Si ya se preguntó '
            'por parámetros concretos, consérvalos en required. Para cada valor ya '
            'proporcionado, copia una cita literal breve de la respuesta en provided; '
            'no inventes valores ni infieras identificadores. Devuelve exactamente JSON '
            '{"required":[nombres],"provided":{nombre:cita_literal}}. Parámetros: ' +
            json.dumps(questions, ensure_ascii=False)[:3000] +
            '. Ya preguntados: ' + json.dumps(required or [], ensure_ascii=False) +
            '. Respuesta: ' + answer[:1000])
        try:
            raw = self.llm.generate_content(prompt, response_format={'type': 'json_object'})
            result = json.loads(raw) if isinstance(raw, str) else raw
            fields = result['required']
            provided = result['provided']
            if (set(result) != {'required', 'provided'} or not isinstance(fields, list)
                    or not fields or len(fields) != len(set(fields))
                    or any(field not in questions for field in fields)
                    or not set(required or []).issubset(fields)
                    or not isinstance(provided, dict) or not set(provided).issubset(fields)
                    or any(not isinstance(quote, str) or not quote.strip()
                           or quote.casefold() not in answer.casefold()
                           for quote in provided.values())):
                return None
            return {'required': fields, 'provided': provided}
        except (ValueError, TypeError, KeyError, AttributeError):
            return None


def step_params(recipe, index, outputs, spec):
    step = recipe['steps'][index]
    params = {}
    for field, binding in step['bindings'].items():
        source, name = binding.split('.', 1)
        params[field] = outputs[source][name]
    if not validate_params(spec['input_schema'], params):
        raise ValueError('Recipe step inputs no longer match the pinned contract')
    return params


def step_output(result, schema):
    """Only a complete typed JSON object can feed another skill."""
    if result.get('status') != 'finished' or result.get('returncode') != 0:
        raise ValueError('Recipe step did not finish successfully')
    raw = result.get('stdout')
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 65536:
        raise ValueError('Recipe step output is absent or too large')
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError('Recipe step did not return a JSON object') from exc
    if not matches_object_schema(value, schema):
        raise ValueError('Recipe step output differs from its pinned schema')
    return value
