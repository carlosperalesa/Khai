"""Closed recovery catalog shared by Heart (planner), Pixy (authorization) and Hand (executor).

Recovery plans are declarative data. Nothing here executes anything: an action that is not
listed in ACTIONS or a target not listed in TARGETS cannot be planned, authorized or run.
"""
import hashlib
import json
import re

TARGETS = {'core': 'khai_core', 'proxy': 'khai_docker_proxy'}
ACTIONS = {
    'restart_container': {'capability': 'DockerRestart', 'mutating': True},
    'health_check': {'capability': 'HealthCheck', 'mutating': False},
}
CASE_ID = re.compile(r'case-[a-z0-9]{6,32}')
ACTION_ID = re.compile(r'rec_[a-f0-9]{32}')
FINAL_STEP = {'completed', 'failed', 'unknown'}


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
    required = sorted({ACTIONS[s['action']]['capability'] for s in steps})
    if sorted(plan.get('capabilities') or []) != required:
        raise ValueError('Las capacidades no coinciden exactamente con los pasos')
    return {'target': target, 'steps': [dict(s) for s in steps], 'capabilities': required}


def plan_hash(case_id, plan):
    core = validate_plan(plan)
    return hashlib.sha256(canonical(dict(core, case_id=validate_case_id(case_id))).encode()).hexdigest()


def requires_approval(plan):
    return any(ACTIONS[s['action']]['mutating'] for s in validate_plan(plan)['steps'])
