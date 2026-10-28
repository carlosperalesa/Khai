"""Validated, provider-independent contracts for reusable skills and recipes."""
import hashlib
import json
import re


NAME = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
PRIMITIVES = {'string', 'number', 'integer', 'boolean', 'object', 'array'}
SLOT = re.compile(r'\{([a-z][a-z0-9_]*):(number|integer|currency|city|text)\}')
CAPTURE = {'number': r'(-?\d+(?:[.,]\d+)?)', 'integer': r'(-?\d+)',
           'currency': r'([A-Za-z]{3})', 'city': r'([\wÀ-ÿ .-]{2,80})',
           'text': r'(.{1,120})'}


def _name(value, label):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError(f'Invalid {label}')
    return value


def _schema(value, label):
    if not isinstance(value, dict) or value.get('type') != 'object':
        raise ValueError(f'{label} must be an object schema')
    if set(value) - {'type', 'properties', 'required', 'additionalProperties'}:
        raise ValueError(f'Unsupported {label} schema field')
    properties = value.get('properties', {})
    required = value.get('required', [])
    if not isinstance(properties, dict) or not isinstance(required, list) or len(set(required)) != len(required):
        raise ValueError(f'Invalid {label} properties')
    if value.get('additionalProperties', False) is not False:
        raise ValueError(f'{label} must reject unknown fields')
    for field, spec in properties.items():
        _name(field, 'field name')
        if not isinstance(spec, dict) or set(spec) != {'type'} or spec['type'] not in PRIMITIVES:
            raise ValueError(f'Unsupported field schema: {field}')
    if set(required) - set(properties):
        raise ValueError(f'{label} requires unknown fields')
    return value


def validate_skill_spec(spec):
    """The catalog describes capability, never a particular request's arguments."""
    required = {'skill_id', 'description', 'use_cases', 'input_schema', 'output_schema',
                'runtime', 'dependencies', 'services', 'verifier'}
    if not isinstance(spec, dict) or set(spec) - (required | {'extraction_templates'}) or not required <= set(spec):
        raise ValueError('SkillSpec fields do not match the contract')
    _name(spec['skill_id'], 'skill ID')
    if not isinstance(spec['description'], str) or not 10 <= len(spec['description'].strip()) <= 1000:
        raise ValueError('Invalid skill description')
    if not isinstance(spec['use_cases'], list) or not spec['use_cases'] or any(
            not isinstance(case, str) or not case.strip() or len(case) > 300 for case in spec['use_cases']):
        raise ValueError('Invalid use cases')
    _schema(spec['input_schema'], 'input')
    _schema(spec['output_schema'], 'output')
    if spec['runtime'] not in {'SAFE', 'AGENT', 'GOD'}:
        raise ValueError('Invalid runtime')
    if not isinstance(spec['dependencies'], list) or any(
            not isinstance(dep, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.+\-]*', dep)
            for dep in spec['dependencies']):
        raise ValueError('Dependencies must be pinned')
    if not isinstance(spec['services'], list) or any(
            not isinstance(service, dict) or set(service) != {'service_id', 'operation', 'version'}
            or any(not isinstance(v, str) or not v for v in service.values()) for service in spec['services']):
        raise ValueError('Invalid service references')
    if not isinstance(spec['verifier'], dict) or not isinstance(spec['verifier'].get('kind'), str):
        raise ValueError('Invalid verifier')
    templates = spec.get('extraction_templates', [])
    if not isinstance(templates, list) or len(templates) > 16:
        raise ValueError('Too many extraction templates')
    for template in templates:
        _template_regex(template, spec['input_schema'])
    return spec


def _template_regex(template, input_schema):
    if not isinstance(template, str) or not 5 <= len(template) <= 240:
        raise ValueError('Invalid extraction template')
    properties = input_schema['properties']
    parts, names, cursor = [], [], 0
    for match in SLOT.finditer(template):
        literal = template[cursor:match.start()]
        if '{' in literal or '}' in literal:
            raise ValueError('Invalid template slot')
        name, kind = match.groups()
        if name in names or name not in properties:
            raise ValueError('Duplicate or unknown template slot')
        expected = 'number' if kind == 'number' else 'integer' if kind == 'integer' else 'string'
        if properties[name]['type'] != expected:
            raise ValueError('Template slot type mismatch')
        parts.extend((re.escape(literal), CAPTURE[kind]))
        names.append((name, kind))
        cursor = match.end()
    tail = template[cursor:]
    if '{' in tail or '}' in tail or not names:
        raise ValueError('Invalid template structure')
    parts.append(re.escape(tail))
    if set(input_schema.get('required', [])) - {name for name, _ in names}:
        raise ValueError('Template omits required input')
    return re.compile(''.join(parts), re.IGNORECASE), names


def extract_params(spec, query):
    """Return unambiguous typed parameters or None to request model help."""
    validate_skill_spec(spec)
    if not isinstance(query, str) or len(query) > 300:
        return None
    query = query.strip().strip('¿?¡!').strip()
    candidates = []
    for template in spec.get('extraction_templates', []):
        pattern, names = _template_regex(template, spec['input_schema'])
        match = pattern.fullmatch(query)
        if not match:
            continue
        params = {}
        for (name, kind), value in zip(names, match.groups()):
            value = value.strip()
            if kind == 'currency':
                value = value.upper()
            elif kind == 'number':
                value = float(value.replace(',', '.'))
            elif kind == 'integer':
                value = int(value)
            params[name] = value
        candidates.append(params)
    return candidates[0] if candidates and all(candidate == candidates[0] for candidate in candidates) else None


def spec_hash(spec):
    validate_skill_spec(spec)
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def validate_recipe(recipe, specs):
    """Validate a bounded DAG of pinned skill versions and typed bindings."""
    if not isinstance(recipe, dict) or set(recipe) != {'recipe_id', 'input_schema', 'steps', 'output'}:
        raise ValueError('Invalid recipe fields')
    _name(recipe['recipe_id'], 'recipe ID')
    _schema(recipe['input_schema'], 'recipe input')
    steps = recipe['steps']
    if not isinstance(steps, list) or not 1 <= len(steps) <= 16:
        raise ValueError('Recipe must have 1–16 steps')
    available = {'input': recipe['input_schema']}
    for step in steps:
        if not isinstance(step, dict) or set(step) != {'id', 'version_id', 'bindings'}:
            raise ValueError('Invalid recipe step')
        step_id = _name(step['id'], 'step ID')
        if step_id in available or step['version_id'] not in specs:
            raise ValueError('Duplicate step or unknown pinned version')
        skill = validate_skill_spec(specs[step['version_id']])
        bindings = step['bindings']
        properties = skill['input_schema']['properties']
        if not isinstance(bindings, dict) or set(bindings) != set(properties):
            raise ValueError('Step inputs are not fully bound')
        for field, source in bindings.items():
            if not isinstance(source, str) or '.' not in source:
                raise ValueError('Invalid binding')
            source_step, source_field = source.split('.', 1)
            if source_step not in available:
                raise ValueError('Forward reference or cycle in recipe')
            produced = available[source_step]['properties'].get(source_field)
            if produced != properties[field]:
                raise ValueError('Recipe binding type mismatch')
        available[step_id] = skill['output_schema']
    output = recipe['output']
    if not isinstance(output, str) or '.' not in output:
        raise ValueError('Invalid recipe output')
    step_id, field = output.split('.', 1)
    if step_id not in available or field not in available[step_id]['properties']:
        raise ValueError('Unknown recipe output')
    return recipe
