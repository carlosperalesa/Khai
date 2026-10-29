"""Closed recovery catalog owned by Heart (medical organ) and shared with Pixy and Hand.

Recovery plans are declarative data. Nothing here executes anything: an action that is not
listed in ACTIONS or a target not listed in TARGETS cannot be planned, authorized or run.
"""
import hashlib
import json
import re

TARGETS = {'core': 'khai_core', 'proxy': 'khai_docker_proxy', 'hand': 'khai_hand', 'skill': 'khai_skill', 'task': 'task'}
ACTIONS = {
    'restart_container': {'capability': 'DockerRestart', 'mutating': True, 'targets': {'core', 'proxy'}},
    'health_check': {'capability': 'HealthCheck', 'mutating': False, 'targets': {'core', 'proxy'}},
    'retry_task': {'capability': 'TaskRemediate', 'mutating': True, 'targets': {'task'}},
    'remediate_task': {'capability': 'TaskRemediate', 'mutating': False, 'targets': {'task'}},
    'create_skill': {'capability': 'TaskRemediate', 'mutating': True, 'targets': {'task'}},
}
CAUSE_TAXONOMY = {
    'syntax_or_code': {
        'description': 'Error sintáctico o excepción de ejecución en la skill',
        'remediable': True,
        'requires_new_version': True,
    },
    'missing_capability': {
        'description': 'Rechazo por falta de capacidad o servicio no registrado',
        'remediable': True,
        'requires_new_version': False,
    },
    'dependency_error': {
        'description': 'Módulo o import no disponible en las capacidades del runtime',
        'remediable': True,
        'requires_new_version': True,
    },
    'temporary_provider': {
        'description': 'Error transitorio de proveedor HTTP 429/5xx o timeout',
        'remediable': True,
        'requires_new_version': False,
    },
    'missing_credential': {
        'description': 'Credencial ausente o rechazada (401/403 tipado)',
        'remediable': False,
        'requires_new_version': False,
    },
    'invalid_result': {
        'description': 'Resultado no satisfizo el criterio del verificador',
        'remediable': True,
        'requires_new_version': True,
    },
    'uncertain_effect': {
        'description': 'Ejecución sin confirmación clara de término o estado incierto',
        'remediable': False,
        'requires_new_version': False,
    },
    'infrastructure': {
        'description': 'Fallo en contenedor Core o Proxy, o presión crítica de memoria',
        'remediable': True,
        'requires_new_version': False,
    },
    'budget_exhausted': {
        'description': 'Presupuesto de generaciones/ejecuciones/tiempo agotado',
        'remediable': False,
        'requires_new_version': False,
    },
}
CASE_ID = re.compile(r'(case|req|tr|trace|task)-[a-zA-Z0-9_-]{6,64}')
TASK_ID = re.compile(r'task_[a-f0-9]{32}')
ACTION_ID = re.compile(r'rec_[a-f0-9]{32}')
FINAL_STEP = {'completed', 'failed', 'unknown'}


def cause_fingerprint(cause_code, detail_or_error):
    norm = (str(detail_or_error) or '').strip()
    norm_sample = re.sub(r'0x[a-fA-F0-9]+', '0x...', norm)[:300]
    return hashlib.sha256(f"{cause_code}:{norm_sample}".encode()).hexdigest()[:16]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def validate_case_id(case_id):
    if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id):
        raise ValueError('Identificador de caso inválido')
    return case_id


def validate_plan(plan):
    """Return the executable core of a plan or raise. Extra or unknown fields are rejected."""
    if not isinstance(plan, dict):
        raise ValueError('Plan de recuperación inválido')
    target, steps = plan.get('target'), plan.get('steps')
    if target not in TARGETS:
        raise ValueError('Objetivo de recuperación no permitido')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 5:
        raise ValueError('El plan requiere entre 1 y 5 pasos')
    for step in steps:
        if not isinstance(step, dict) or set(step) != {'action', 'target'}:
            raise ValueError('Paso de recuperación inválido')
        if step['action'] not in ACTIONS:
            raise ValueError('Acción fuera del catálogo: ' + str(step['action']))
        if step['target'] != target:
            raise ValueError('Un paso apunta a un objetivo distinto del autorizado')
        if target not in ACTIONS[step['action']]['targets']:
            raise ValueError('Par acción/objetivo fuera del catálogo')
    required = sorted({ACTIONS[s['action']]['capability'] for s in steps})
    if sorted(plan.get('capabilities') or []) != required:
        raise ValueError('Las capacidades no coinciden exactamente con los pasos')
    core = {'target': target, 'steps': [dict(s) for s in steps], 'capabilities': required}
    if target == 'task':
        task_id = plan.get('task_id')
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise ValueError('La remediación debe identificar una task_id exacta')
        core['task_id'] = task_id
    elif 'task_id' in plan:
        raise ValueError('task_id solo es válido para remediar tareas')
    return core


def plan_hash(case_id, plan):
    core = validate_plan(plan)
    return hashlib.sha256(canonical(dict(core, case_id=validate_case_id(case_id))).encode()).hexdigest()


def requires_approval(plan):
    return any(ACTIONS[s['action']]['mutating'] for s in validate_plan(plan)['steps'])
