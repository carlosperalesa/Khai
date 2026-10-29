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
            'Objetivo: ' + goal + '\nCorrección: ' + feedback +
            '\nAcciones y recibos: ' + json.dumps(steps, ensure_ascii=False))
        raw = self.llm.generate_content(prompt, response_format={'type': 'json_object'})
        result = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(result, dict):
            raise ValueError('Invalid owner correction classification')
        step_id = result.get('step_id')
        if step_id is not None and step_id not in {step['id'] for step in recipe['steps']}:
            raise ValueError('Correction targets an unknown action')
        if result.get('problem') not in {'missing', 'incorrect', 'other'}:
            raise ValueError('Invalid correction problem')
        return {'step_id': step_id, 'problem': result['problem']}

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
        template = {"index": 1}
        prompt = (
            'Interpreta exclusivamente si la respuesta del propietario identifica '
            'por ORDEN una de las escrituras registradas (desde 1 hasta ' + str(count) + '). '
            'Si la respuesta no identifica una posición por orden, devuelve {"index": null}.\n'
            'Plantilla JSON:\n' + json.dumps(template) + '\n'
            'Respuesta: ' + answer)
        try:
            raw = self.llm.generate_content(prompt, response_format={'type': 'json_object'})
            result = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(result, dict):
                return None
            index = result.get('index')
            if type(index) is not int or not 1 <= index <= count:
                return None
            evidence = result.get('evidence')
            if isinstance(evidence, str) and not re.search(
                    r'\b(despu[eé]s|antes|final|inicio|reciente|anterior|posterior|últim[ao]|ultim[ao]|primer[ao]?|segund[ao]|tercer[ao]?|[0-9]+)\b',
                    evidence.casefold()):
                return None
            return index
        except Exception:
            return None

    def effect_inputs(self, answer, questions, required=None):
        """Identify relevant correction fields and extracted values using a JSON template."""
        if not questions:
            return {'required': [], 'provided': {}}
        if self.llm is None:
            return None
        template = {
            "required": list(questions.keys())[:1],
            "provided": {}
        }
        prompt = (
            'La persona describe una corrección de un efecto externo. Selecciona los '
            'parámetros necesarios para esa corrección y extrae los valores ya indicados '
            'por el propietario en provided. Si ya se preguntó por parámetros concretos, '
            'consérvalos en required. Devuelve JSON siguiendo esta plantilla:\n'
            + json.dumps(template, ensure_ascii=False)
            + '\nParámetros: ' + json.dumps(questions, ensure_ascii=False)
            + '\nYa preguntados: ' + json.dumps(required or [], ensure_ascii=False)
            + '\nRespuesta: ' + answer)
        try:
            raw = self.llm.generate_content(prompt, response_format={'type': 'json_object'})
            result = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(result, dict):
                return None
            fields = result.get('required')
            provided = result.get('provided')
            if (not isinstance(fields, list) or not fields
                    or any(field not in questions for field in fields)
                    or not set(required or []).issubset(fields)
                    or not isinstance(provided, dict)
                    or not set(provided).issubset(fields)
                    or any(v is None or not str(v).strip() for v in provided.values())):
                return None
            return {
                'required': list(dict.fromkeys(fields)),
                'provided': {k: str(v).strip() for k, v in provided.items()},
            }
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
