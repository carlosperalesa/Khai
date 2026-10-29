"""Propose action-sized compositions; model output never grants authority."""
import json
import re

from app.hand.skill_spec import normalize_schema, validate_recipe, validate_skill_spec
from app.mind.skill_selector import validate_params


def _sanitize_skill_id(raw_id, fallback='accion-nueva'):
    cleaned = re.sub(r'[^a-z0-9-]', '-', str(raw_id or '').strip().lower()).strip('-')
    cleaned = re.sub(r'-+', '-', cleaned)[:63].rstrip('-')
    if not cleaned or not cleaned[0].isalpha():
        cleaned = 'sk-' + (cleaned or fallback)
    if cleaned in {'con', 'prn', 'aux', 'nul', 'heart'} or re.fullmatch(r'(com|lpt)[0-9]', cleaned):
        cleaned = 'sk-' + cleaned
    return cleaned[:64]


class ActionPlanner:
    def __init__(self, llm, catalog):
        self.llm = llm
        self.catalog = catalog

    @staticmethod
    def _convert_template_steps(proposal, records):
        """Convierte la plantilla simplificada {'steps': [...], 'inputs': {...}} al contrato de receta."""
        steps_list = proposal.get('steps')
        if not isinstance(steps_list, list) or not steps_list:
            return proposal
        by_skill = {r['skill_id']: r for r in records}
        by_ver = {r['version_id']: r for r in records}
        recipe_steps = []
        missing_specs = {}
        recipe_props = {}
        recipe_req = []
        prev_step_id = None
        prev_out_fields = []

        for idx, item in enumerate(steps_list, start=1):
            if not isinstance(item, dict):
                continue
            step_id = re.sub(r'[^a-z0-9_-]', '_', str(item.get('id') or f'step_{idx}').lower())
            if not step_id or not step_id[0].isalpha():
                step_id = f'step_{idx}'
            existing = item.get('use_existing_skill') or item.get('skill_id') or item.get('version_id')
            rec = by_skill.get(existing) or by_ver.get(existing)
            if rec and not item.get('new_skill'):
                ver_id = rec['version_id']
                in_schema = rec['spec']['input_schema']
                out_schema = rec['spec']['output_schema']
            else:
                new_sk = item.get('new_skill') if isinstance(item.get('new_skill'), dict) else item
                sk_id = _sanitize_skill_id(new_sk.get('skill_id') or step_id, fallback=f'skill-{idx}')
                desc = str(new_sk.get('description') or f'Ejecuta el paso {sk_id} para el objetivo solicitado.').strip()
                if len(desc) < 10:
                    desc = f'Capacidad reutilizable para {desc or sk_id}.'
                in_schema = normalize_schema(new_sk.get('input_schema') or new_sk.get('inputs') or {}, default_field='query')
                out_schema = normalize_schema(new_sk.get('output_schema') or new_sk.get('outputs') or {'result': 'string'}, default_field='result')
                raw_caps = new_sk.get('capabilities') or []
                if not isinstance(raw_caps, list):
                    raw_caps = [raw_caps]
                caps = []
                for c in raw_caps:
                    clean_c = re.sub(r'[^a-zA-Z0-9_-]', '_', str(c).strip()).strip('_')
                    if clean_c and clean_c not in caps:
                        caps.append(clean_c)
                use_cases = new_sk.get('use_cases')
                if not isinstance(use_cases, list) or not use_cases:
                    use_cases = [desc[:200]]
                ver_id = f'missing-{step_id}'
                missing_specs[step_id] = {
                    'skill_id': sk_id,
                    'description': desc[:1000],
                    'use_cases': [str(u).strip()[:300] for u in use_cases if str(u).strip()] or [desc[:200]],
                    'input_schema': in_schema,
                    'output_schema': out_schema,
                    'capabilities': caps,
                }
            bindings = item.get('bindings') if isinstance(item.get('bindings'), dict) else {}
            if not bindings:
                for field, fspec in in_schema['properties'].items():
                    if prev_step_id and field in prev_out_fields:
                        bindings[field] = f'{prev_step_id}.{field}'
                    else:
                        bindings[field] = f'input.{field}'
                        recipe_props[field] = fspec
                        if field in in_schema.get('required', []) and field not in recipe_req:
                            recipe_req.append(field)
            else:
                for field, bind in bindings.items():
                    if isinstance(bind, str) and bind.startswith('input.'):
                        inp_f = bind.split('.', 1)[1]
                        recipe_props[inp_f] = in_schema['properties'].get(field, {'type': 'string'})
                        if inp_f not in recipe_req:
                            recipe_req.append(inp_f)
            recipe_steps.append({'id': step_id, 'version_id': ver_id, 'bindings': bindings})
            prev_step_id = step_id
            prev_out_fields = list(out_schema['properties'].keys())

        if not recipe_steps:
            return {'recipe': None}
        last_out = prev_out_fields[0] if prev_out_fields else 'result'
        recipe = {
            'recipe_id': _sanitize_skill_id(proposal.get('recipe_id') or 'plan-compuesto'),
            'input_schema': {
                'type': 'object',
                'properties': recipe_props,
                'required': recipe_req,
                'additionalProperties': False,
            },
            'steps': recipe_steps,
            'output': f'{prev_step_id}.{last_out}',
        }
        return {
            'recipe': recipe,
            'inputs': proposal.get('supplied_inputs') or proposal.get('inputs') or {},
            'missing_specs': missing_specs,
        }

    def propose(self, request, compatible, correction_context=None, conversation_context='', task_context=None):
        active = self.catalog.active()
        records = [r for r in active if compatible(r['spec'])]
        skill_names = [{'skill_id': r['skill_id'], 'version_id': r['version_id'],
                        'description': r['spec']['description']} for r in records[:20]]
        template_example = {
            "steps": [
                {
                    "id": "paso_1",
                    "use_existing_skill": None,
                    "new_skill": {
                        "skill_id": "nombre-habilidad",
                        "description": "Descripción clara de lo que hace esta habilidad",
                        "capabilities": ["Network"],
                        "inputs": {"parametro": "string"},
                        "outputs": {"resultado": "string"}
                    }
                }
            ],
            "inputs": {}
        }
        prompt = (
            f'Objetivo del usuario: "{request}"\n'
            f'Conversación reciente (no acredita acciones): {conversation_context}\n'
            f'Estado real de tareas: {json.dumps(task_context or [], ensure_ascii=False)}\n'
            'Resuelve instrucciones breves como «hazlo» usando el objetivo del contexto. '
            'Las consultas de datos actuales externos requieren una habilidad, aunque sean preguntas. '
            'No consideres las promesas del historial como ejecuciones ni autorizaciones.\n'
            'Analiza si alguna de las habilidades disponibles resuelve este objetivo completo o parcial.\n'
            f'Habilidades disponibles: {json.dumps(skill_names, ensure_ascii=False)}\n'
            'Instrucciones de asignación:\n'
            '1. Si una habilidad existente resuelve el objetivo, asígnala en "use_existing_skill" con su skill_id y "new_skill": null.\n'
            '2. Si NINGUNA habilidad existente sirve o falta alguna funcionalidad, define una o varias en "new_skill" con una "description" clara de lo que hace (ej: "convierte las imágenes en 0 y 1") y declara en "capabilities" los accesos requeridos (ej: Network, BinaryIO, FileSystem, Database, etc.).\n'
            '3. El router ya confirmó este objetivo ejecutable. Construye sus acciones; '
            'no lo reclasifiques como charla ni respondas al propietario.\n'
            f'Plantilla JSON a seguir estrictamente:\n'
            f'{json.dumps(template_example, ensure_ascii=False)}\n'
            'Correcciones obligatorias de recursos existentes: '
            + json.dumps(correction_context or [], ensure_ascii=False)
            + '. Si hay correcciones, usa sus operaciones y resource_id; jamás repitas una creación.'
        )
        raw = self.llm.generate_content(prompt, response_format={'type': 'json_object'})
        proposal = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(proposal, dict):
            raise ValueError('El planificador no devolvió un objeto')
        if proposal.get('steps') == [] and 'recipe' not in proposal:
            return None
        if 'recipe' not in proposal and not isinstance(proposal.get('steps'), list):
            raise ValueError('El planificador no declaró pasos ni receta')
        if 'recipe' not in proposal and isinstance(proposal.get('steps'), list):
            proposal = self._convert_template_steps(proposal, records)
        if proposal.get('recipe') is None:
            raise ValueError('El planificador no produjo una receta válida')
        recipe = proposal['recipe']
        if not isinstance(recipe, dict) or not isinstance(recipe.get('steps'), list):
            raise ValueError('Contrato de receta inválido')
        if len(recipe['steps']) < 1:
            return None
        if len(recipe['steps']) > 8:
            raise ValueError('Invalid action composition size')
        if isinstance(recipe.get('input_schema'), dict) and 'additionalProperties' not in recipe['input_schema']:
            recipe['input_schema'] = normalize_schema(recipe['input_schema'])
        missing = proposal.get('missing_specs', {})
        if not isinstance(missing, dict) or len(missing) > 8:
            raise ValueError('Invalid missing action set')
        specs = {r['version_id']: r['spec'] for r in records}
        proposed_names = set()
        normalized_missing = {}
        for step in recipe['steps']:
            if not isinstance(step, dict):
                raise ValueError('Invalid action step')
            step_id = str(step.get('id') or '')
            version_id = step.get('version_id')
            if isinstance(version_id, str) and version_id.startswith('missing-'):
                suffix = version_id[len('missing-'):]
                action = (missing.get(step_id)
                          or missing.get(version_id)
                          or missing.get(suffix))
                if not isinstance(action, dict):
                    raise ValueError('Missing action does not match its step')
                step['version_id'] = 'missing-' + step_id
                version_id = step['version_id']
                if 'additionalProperties' not in (action.get('input_schema') or {}):
                    action['input_schema'] = normalize_schema(action.get('input_schema') or action.get('inputs') or {})
                if 'additionalProperties' not in (action.get('output_schema') or {}):
                    action['output_schema'] = normalize_schema(action.get('output_schema') or action.get('outputs') or {'result': 'string'})
                if not action.get('use_cases') and action.get('description'):
                    action['use_cases'] = [str(action['description'])[:200]]
                action = {k: action[k] for k in ('skill_id', 'description', 'use_cases',
                                                 'input_schema', 'output_schema', 'capabilities')
                          if k in action}
                if set(action) != {'skill_id', 'description', 'use_cases', 'input_schema',
                                   'output_schema', 'capabilities'}:
                    raise ValueError('Invalid missing action contract')
                raw_caps = action.get('capabilities') or []
                if not isinstance(raw_caps, list):
                    raw_caps = [raw_caps]
                caps = []
                for c in raw_caps:
                    clean_c = re.sub(r'[^a-zA-Z0-9_-]', '_', str(c).strip()).strip('_')
                    if clean_c and clean_c not in caps:
                        caps.append(clean_c)
                action['capabilities'] = caps
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
                normalized_missing[step_id] = action
            elif version_id not in specs:
                raise ValueError('Unpinned or unavailable action')
        missing.clear()
        missing.update(normalized_missing)
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
        inputs = proposal.get('inputs') if isinstance(proposal.get('inputs'), dict) else {}
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
