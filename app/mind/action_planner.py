"""Propose action-sized compositions; model output never grants authority."""
import json
import re

from app.hand.skill_spec import validate_recipe, validate_skill_spec
from app.mind.skill_selector import validate_params


class ActionPlanner:
    def __init__(self, llm, catalog):
        self.llm = llm
        self.catalog = catalog

    def propose(self, request, compatible, correction_context=None):
        active = self.catalog.active()
        records = [r for r in active if compatible(r['spec'])]
        options = [{'version_id': r['version_id'], 'skill_id': r['skill_id'],
                    'description': r['spec']['description'],
                    'input_schema': r['spec']['input_schema'],
                    'output_schema': r['spec']['output_schema']} for r in records[:20]]
        prompt = (
            'Decide si esta solicitud necesita dos o más acciones útiles encadenadas. '
            'Una acción tiene entradas y salidas propias; acceso a red, autenticación y '
            'credenciales no son acciones. Reutiliza versiones existentes compatibles. '
            'Si falta una acción, descríbela como una capacidad nueva, sin inventar '
            'permisos, servicios ni credenciales. Si no hacen falta al menos dos '
            'acciones y no hay una corrección obligatoria, devuelve {"recipe":null}. '
            'Una corrección puede tener una sola acción. De otro modo devuelve JSON con '
            'recipe {recipe_id,input_schema,steps,output}, inputs y missing_specs. '
            'Cada step usa {id,version_id,bindings}; una acción faltante usa '
            'version_id="missing-<id>" y missing_specs[id] contiene skill_id, '
            'description, use_cases, input_schema, output_schema y capabilities '
            '(lista de Network, FileSystem, Database). Los bindings tienen forma '
            'input.campo o paso.campo. No inventes valores: omite inputs ausentes. '
            'Los schemas son objetos con properties de tipos simples, required y '
            'additionalProperties=false. Una receta debe representar TODO el objetivo, '
            'no solo una parte. Máximo ocho pasos.\nSolicitud: ' + request[:3000] +
            '\nAcciones disponibles: ' + json.dumps(options, ensure_ascii=False) +
            '\nCorrecciones obligatorias de recursos existentes: ' +
            json.dumps(correction_context or [], ensure_ascii=False) +
            '. Si hay correcciones, usa sus operaciones y resource_id; jamás repitas una creación.')
        raw = self.llm.generate_content(prompt, response_format={'type': 'json_object'})
        proposal = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(proposal, dict) or proposal.get('recipe') is None:
            return None
        recipe = proposal['recipe']
        minimum = 1 if correction_context else 2
        if not isinstance(recipe, dict) or not isinstance(recipe.get('steps'), list) or not minimum <= len(recipe['steps']) <= 8:
            raise ValueError('Invalid action composition size')
        missing = proposal.get('missing_specs', {})
        if not isinstance(missing, dict) or len(missing) > 8:
            raise ValueError('Invalid missing action set')
        specs = {r['version_id']: r['spec'] for r in records}
        proposed_names = set()
        for step in recipe['steps']:
            if not isinstance(step, dict):
                raise ValueError('Invalid action step')
            version_id = step.get('version_id')
            if isinstance(version_id, str) and version_id.startswith('missing-'):
                action = missing.get(step.get('id'))
                if version_id != 'missing-' + str(step.get('id')) or not isinstance(action, dict):
                    raise ValueError('Missing action does not match its step')
                if set(action) != {'skill_id', 'description', 'use_cases', 'input_schema',
                                   'output_schema', 'capabilities'}:
                    raise ValueError('Invalid missing action contract')
                caps = action['capabilities']
                if not isinstance(caps, list) or set(caps) - {'Network', 'FileSystem', 'Database'}:
                    raise ValueError('Invalid action capabilities')
                spec = {key: action[key] for key in ('skill_id', 'description', 'use_cases',
                                                     'input_schema', 'output_schema')}
                spec.update(runtime='SAFE', dependencies=[], services=[],
                            verifier={'kind': 'owner_criterion'})
                specs[version_id] = validate_skill_spec(spec)
                if (not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', spec['skill_id']) or
                        spec['skill_id'] in {'con', 'prn', 'aux', 'nul', 'heart'} or
                        re.fullmatch(r'(com|lpt)[0-9]', spec['skill_id'])):
                    raise ValueError('Action identity cannot be packaged by Hand')
                if spec['skill_id'] in proposed_names or spec['skill_id'] in {
                        r['skill_id'] for r in active}:
                    raise ValueError('Missing actions require distinct new skill identities')
                proposed_names.add(spec['skill_id'])
            elif version_id not in specs:
                raise ValueError('Unpinned or unavailable action')
        if set(missing) != {s['id'] for s in recipe['steps']
                            if s['version_id'].startswith('missing-')}:
            raise ValueError('Unreferenced missing action')
        unmatched = []
        for correction in correction_context or []:
            ref = {key: correction[key] for key in ('service_id', 'operation', 'version')}
            if not any(ref in specs[step['version_id']]['services']
                       for step in recipe['steps'] if not step['version_id'].startswith('missing-')):
                unmatched.append(ref)
        if unmatched:
            network_missing = [step for step in recipe['steps']
                               if step['version_id'].startswith('missing-')
                               and 'Network' in missing[step['id']]['capabilities']]
            if len(network_missing) != 1:
                return None
            selected = network_missing[0]
            missing[selected['id']]['services'] = unmatched
            specs[selected['version_id']]['services'] = unmatched
        validate_recipe(recipe, specs)
        inputs = proposal.get('inputs')
        if not validate_params(recipe['input_schema'], inputs, allow_missing=True):
            raise ValueError('Invalid composition inputs')
        for correction in correction_context or []:
            ref = {key: correction[key] for key in ('service_id', 'operation', 'version')}
            matched = next((step for step in recipe['steps']
                            if ref in specs[step['version_id']]['services']), None)
            if matched is None:
                return None
            binding = matched['bindings'].get(correction['identity_parameter'])
            if not isinstance(binding, str) or not binding.startswith('input.'):
                return None
            field = binding.split('.', 1)[1]
            kind = recipe['input_schema']['properties'][field]['type']
            identity = correction['resource_id']
            if kind == 'integer':
                try:
                    identity = int(identity)
                except ValueError:
                    return None
            elif kind != 'string':
                return None
            if field in inputs and inputs[field] != identity:
                return None
            inputs[field] = identity
        if not validate_params(recipe['input_schema'], inputs, allow_missing=True):
            return None
        return {'recipe': recipe, 'inputs': inputs, 'missing_specs': missing}
