"""Independent, deterministic evaluation of criteria fixed before execution."""
import json
import math
from pathlib import Path

from app.voice.runtime.contract import digest


MAX_EVIDENCE_BYTES = 64 * 1024 * 1024


def _regular_file(workspace, relative):
    root = Path(workspace).resolve()
    if not isinstance(relative, str) or not relative:
        return None
    path_part = Path(relative)
    if path_part.is_absolute() or '..' in path_part.parts:
        return None
    path = (root / path_part).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
        return None
    return path


def _matches_schema(value, schema):
    if not isinstance(schema, dict) or schema.get('type') != 'object':
        raise ValueError('Unsupported verification schema')
    properties = schema.get('properties', {})
    required = schema.get('required', [])
    if not isinstance(properties, dict) or not isinstance(required, list) or len(properties) > 64:
        raise ValueError('Invalid verification schema')
    if not isinstance(value, dict) or set(required) - set(value):
        return False
    if schema.get('additionalProperties') is False and set(value) - set(properties):
        return False
    kinds = {'string': lambda v: isinstance(v, str),
             'number': lambda v: type(v) in {int, float} and math.isfinite(v),
             'integer': lambda v: type(v) is int,
             'boolean': lambda v: type(v) is bool,
             'object': lambda v: isinstance(v, dict),
             'array': lambda v: isinstance(v, list)}
    for field, spec in properties.items():
        if not isinstance(spec, dict) or spec.get('type') not in kinds:
            raise ValueError('Unsupported verification field')
        if field in value and not kinds[spec['type']](value[field]):
            return False
    return True


def matches_object_schema(value, schema):
    """Check a SkillSpec object without treating process completion as success."""
    return _matches_schema(value, schema)


def verify_result(result, criterion, workspace, transport_receipts=None, execution_id=None):
    """Return (True/False/None, evidence); None means insufficient proof."""
    if result.get('status') != 'finished' or result.get('returncode') != 0:
        return None, 'La ejecución no terminó correctamente'
    if not isinstance(criterion, dict):
        return None, 'Falta un criterio independiente; requiere revisión del propietario'
    kind = criterion.get('kind')
    if kind == 'process_returncode':
        return None, 'El código de salida solo acredita fin de proceso, no el objetivo'
    if kind == 'http_schema':
        target = (criterion.get('service_id'), criterion.get('operation'), criterion.get('version'))
        if any(not isinstance(part, str) or not part for part in target) or not execution_id:
            return None, 'Falta la identidad fijada de la operación externa'
        receipts = [item for item in (transport_receipts or [])
                    if (item.get('service_id'), item.get('operation'), item.get('version')) == target
                    and item.get('execution_id') == execution_id]
        if not receipts:
            return None, 'No existe un recibo de Voice ligado a esta ejecución y operación'
        if any(item.get('status') != 'completed' for item in receipts):
            return None, 'La operación externa conserva un resultado incierto'
        evidence = {'kind': kind, 'service_id': target[0], 'operation': target[1],
                    'version': target[2], 'receipts': []}
        valid = True
        for item in receipts:
            reply = item.get('reply')
            value = reply.get('result') if isinstance(reply, dict) else None
            matched = _matches_schema(value, criterion.get('schema')) if value is not None else False
            valid = valid and matched
            evidence['receipts'].append({'request_id': item['request_id'],
                                         'reply_sha256': digest(json.dumps(
                                             reply, sort_keys=True, ensure_ascii=False).encode('utf-8')),
                                         'schema_valid': matched})
        return valid, evidence
    if kind not in {'file_sha256', 'file_exists', 'json_schema', 'oracle_diff'}:
        return None, 'Criterio no soportado'
    path = _regular_file(workspace, criterion.get('path'))
    if path is None:
        return False, 'Artefacto ausente o fuera del workspace'
    size = path.stat().st_size
    if size > MAX_EVIDENCE_BYTES:
        return None, 'Artefacto demasiado grande para este verificador'
    evidence = {'kind': kind, 'path': criterion['path'], 'size': size,
                'sha256': digest(path.read_bytes())}
    if kind == 'file_exists':
        expected_type = criterion.get('content_type')
        if expected_type == 'json':
            try:
                json.loads(path.read_text(encoding='utf-8'))
            except (UnicodeError, ValueError):
                return False, evidence
        elif expected_type not in {None, 'file'}:
            return None, 'Tipo de archivo no soportado'
        return True, evidence
    if kind == 'file_sha256':
        return evidence['sha256'] == criterion.get('sha256'), evidence
    if kind == 'oracle_diff':
        expected = criterion.get('expected_sha256')
        if not isinstance(expected, str) or len(expected) != 64:
            return None, 'Falta el hash del oráculo independiente'
        return evidence['sha256'] == expected, evidence
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return _matches_schema(value, criterion.get('schema')), evidence
    except (UnicodeError, ValueError) as exc:
        return False, {'kind': kind, 'path': criterion['path'], 'error': type(exc).__name__}
