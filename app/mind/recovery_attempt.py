"""Pure Coordinator transition, committed together with Hand's authorization claim."""
import copy
import time

from app.heart.catalog import task_binding
from app.pixy.control import ControlStore, INITIAL_TASK_BUDGET


def build_attempt(task, order):
    plan, header = order['plan'], order['header']
    if task_binding(task) != plan['task_binding']:
        raise PermissionError('El objetivo o el intento cambió desde el diagnóstico de Heart')
    instruction = plan['corrective_instruction']
    if task is None:
        task = ControlStore.document('heart:' + header['authorization_id'],
            instruction or 'Remediación autorizada por Heart', id=plan['task_id'], kind='objective')
    else:
        if task['state'] not in {'FAILED', 'EXHAUSTED', 'DIAGNOSING'}:
            raise ValueError('La orden no puede revocar una cancelación ni repetir resultados pendientes o inciertos')
        if (task.get('runtime_status') in {'running', 'started', 'cancelling', 'unknown'}
                or task.get('batch_incomplete')):
            raise PermissionError('La ejecución anterior requiere reconciliación antes de repararla')
        archived = {key: copy.deepcopy(task.get(key)) for key in (
            'execution_id', 'artifact_hash', 'version_id', 'result', 'runtime_status',
            'runtime_result', 'error', 'feedback', 'verification', 'machine_verification',
            'manifest', 'authorized_hash', 'approval', 'goal_revision', 'attempt_number',
            'generations', 'executions', 'llm_calls', 'deadline', 'params', 'services',
            'hypothesis_id', 'guidelines', 'recipe_receipts')}
        archived['authorization_id'] = header['authorization_id']
        task.setdefault('recovery_history', []).append(archived)
        extension = dict(INITIAL_TASK_BUDGET, kind='heart_recovery',
                         authorization_id=header['authorization_id'], created_at=time.time())
        task.setdefault('budget_extensions', []).append(extension)
        for key, value in INITIAL_TASK_BUDGET.items():
            task['max_' + key] += value
    task['attempt_number'] = task.get('attempt_number', 1) + 1
    task['goal_revision'] = task.get('goal_revision', 0) + 1
    task['recovery_attempt'] = {'case_id': header['case_id'], 'order_id': header['order_id'],
        'authorization_id': header['authorization_id'], 'goal_revision': task['goal_revision'],
        'attempt_number': task['attempt_number']}
    previous = str(task.get('feedback') or task.get('error') or '').strip()
    task['feedback'] = '\n'.join(part for part in (previous, '[Heart]: ' + instruction if instruction else '') if part)
    reroute = not task.get('manifest') or bool(plan['proposed_steps'])
    task.update(state='ROUTING' if reroute else 'GENERATING',
                execution_id=None, result=None, runtime_status=None, runtime_result=None,
                artifact=None, artifact_hash=None, authorized_hash=None, approval=None,
                selected_version_id=None, version_id=None, verification=None, machine_verification=None,
                diagnosed=False, recovery_case_id=None, recovery_authorization_id=None, alternatives=[], hypothesis_id=None, guidelines=[],
                routing_intent=None, error=None,
                deadline=max(task['deadline'], time.time() + 1800))
    if reroute:
        task.update(manifest=None, recipe=None)
    task['recovery_plan_steps'] = copy.deepcopy(plan['proposed_steps'])
    return task
