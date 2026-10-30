"""Versioned service descriptors: Hand proposes, Pixy decides, Voice reads."""
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from app.kernel.database import connection, migrate


IDENTIFIER = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
SECRET_REF = re.compile(r'^[A-Z][A-Z0-9_]{2,127}$')
SUMMARY_TEXT = re.compile(r'^[\w .,:()/\-]+$', re.UNICODE)


def validate_write_summary(summary):
    """Allow only fixed, short display text in a reviewed write descriptor."""
    if not isinstance(summary, dict) or set(summary) != {'name', 'context'}:
        raise ValueError('Write summary must declare name and context')
    for field, limit in (('name', 60), ('context', 120)):
        value = summary[field]
        if (not isinstance(value, str) or not 3 <= len(value.strip()) <= limit
                or value != value.strip() or not SUMMARY_TEXT.fullmatch(value)
                or any(len(part) > 32 for part in value.split())):
            raise ValueError('Write summary must be fixed, short, non-sensitive text')
    return summary


def validate_descriptor(spec):
    fields = {'service_id', 'operation', 'version', 'url', 'method', 'parameters',
              'content_types', 'max_bytes', 'timeout', 'per_minute', 'effect',
              'credential_ref', 'credential_header', 'credential_prefix', 'effect_contract'}
    if not isinstance(spec, dict) or set(spec) != fields:
        missing = sorted(fields - set(spec)) if isinstance(spec, dict) else []
        extra = sorted(set(spec) - fields) if isinstance(spec, dict) else []
        details = []
        if missing:
            details.append(f'missing: {missing}')
        if extra:
            details.append(f'extra: {extra}')
        details_str = ', '.join(details)
        detail_msg = f' ({details_str})' if details else ''
        raise ValueError(f'ServiceDescriptor fields do not match the contract{detail_msg}')
    for key in ('service_id', 'operation', 'version'):
        if not isinstance(spec[key], str) or not IDENTIFIER.fullmatch(spec[key]):
            raise ValueError('Invalid service identifier')
    url = urlsplit(spec['url'])
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.fragment or url.port not in {None, 443} or len(spec['url']) > 2048):
        raise ValueError('Service must use a fixed public HTTPS URL')
    if spec['method'] not in {'GET', 'POST'} or spec['effect'] not in {'read', 'write'}:
        raise ValueError('Invalid service method or effect')
    if spec['method'] == 'GET' and spec['effect'] != 'read':
        raise ValueError('GET cannot declare a write effect')
    if not isinstance(spec['parameters'], list) or len(spec['parameters']) > 32 or any(
            not isinstance(p, str) or not IDENTIFIER.fullmatch(p) for p in spec['parameters']):
        raise ValueError('Invalid service parameters')
    if len(set(spec['parameters'])) != len(spec['parameters']):
        raise ValueError('Duplicate service parameters')
    if not isinstance(spec['content_types'], list) or not spec['content_types'] or any(
            mime not in {'application/json', 'text/plain'} for mime in spec['content_types']):
        raise ValueError('Unsupported service response type')
    if (type(spec['max_bytes']) is not int or not 1 <= spec['max_bytes'] <= 2 * 1024 * 1024
            or type(spec['per_minute']) is not int or not 1 <= spec['per_minute'] <= 60
            or type(spec['timeout']) not in {int, float} or not 1 <= spec['timeout'] <= 30):
        raise ValueError('Invalid service limits')
    if spec['credential_ref'] is not None and (
            not isinstance(spec['credential_ref'], str) or not SECRET_REF.fullmatch(spec['credential_ref'])):
        raise ValueError('Credential must be a reference, never a value')
    if (not isinstance(spec['credential_header'], str)
            or not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,63}', spec['credential_header'])
            or spec['credential_header'].lower() in {'host', 'content-length', 'transfer-encoding'}):
        raise ValueError('Invalid credential header')
    if not isinstance(spec['credential_prefix'], str) or len(spec['credential_prefix']) > 32:
        raise ValueError('Invalid credential prefix')
    contract = spec.get('effect_contract')
    if spec['effect'] == 'read':
        if contract is not None:
            raise ValueError('Read operation cannot declare a write effect contract')
    else:
        base_fields = {'kind', 'identity', 'correction', 'summary'}
        if not isinstance(contract, dict) or set(contract) not in (base_fields, base_fields | {'batch'}):
            raise ValueError('Invalid write effect contract')
        validate_write_summary(contract['summary'])
        if contract['kind'] not in {'create', 'update', 'delete', 'other'}:
            raise ValueError('Invalid write effect kind')
        identity = contract['identity']
        batch = contract.get('batch')
        if batch is not None:
            batch_fields = {'request_field', 'response_field', 'index_field',
                            'identity_field', 'status_field', 'success_value', 'failure_value'}
            if (not isinstance(batch, dict) or set(batch) != batch_fields
                    or any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value)
                           for value in batch.values())
                    or batch['request_field'] not in spec['parameters']
                    or len({batch['index_field'], batch['identity_field'],
                            batch['status_field']}) != 3
                    or batch['success_value'] == batch['failure_value']
                    or identity is not None):
                raise ValueError('Invalid per-item batch identity declaration')
        if identity is not None and (not isinstance(identity, dict)
                or set(identity) != {'source', 'field'}
                or identity['source'] not in {'request', 'response'}
                or not isinstance(identity['field'], str)
                or not IDENTIFIER.fullmatch(identity['field'])
                or (identity['source'] == 'request' and identity['field'] not in spec['parameters'])):
            raise ValueError('Invalid resource identity declaration')
        correction = contract['correction']
        if correction is not None and (identity is None and batch is None or not isinstance(correction, dict)
                or set(correction) != {'service_id', 'operation', 'version', 'identity_parameter', 'questions'}
                or any(not isinstance(correction[key], str) or not IDENTIFIER.fullmatch(correction[key])
                       for key in ('service_id', 'operation', 'version', 'identity_parameter'))
                or not isinstance(correction['questions'], dict)
                or len(correction['questions']) > 16
                or any(not isinstance(field, str) or not IDENTIFIER.fullmatch(field)
                       or field == correction['identity_parameter']
                       or not isinstance(question, dict)
                       or set(question) != {'description', 'question'}
                       or any(not isinstance(question[key], str)
                              or not 8 <= len(question[key].strip()) <= 200
                              for key in ('description', 'question'))
                       for field, question in correction['questions'].items())):
            raise ValueError('Invalid correction operation declaration')
    return spec


def descriptor_hash(spec):
    validate_descriptor(spec)
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class ServiceRegistry:
    def __init__(self, db_path):
        self.db_path = db_path
        migrate(db_path, [[
            '''CREATE TABLE IF NOT EXISTS service_descriptors(
                 descriptor_hash TEXT PRIMARY KEY, service_id TEXT NOT NULL,
                 operation TEXT NOT NULL, version TEXT NOT NULL, descriptor TEXT NOT NULL,
                 status TEXT NOT NULL, proposed_at REAL NOT NULL,
                 decided_at REAL, decided_by TEXT,
                 UNIQUE(service_id,operation,version))''',
            'CREATE INDEX IF NOT EXISTS service_descriptors_active ON service_descriptors(status,service_id)',
        ]], component='voice-services')

    def propose(self, spec):
        validate_descriptor(spec)
        digest = descriptor_hash(spec)
        with connection(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT descriptor_hash FROM service_descriptors WHERE '
                               'service_id=? AND operation=? AND version=?',
                               (spec['service_id'], spec['operation'], spec['version'])).fetchone()
            if row:
                if row['descriptor_hash'] != digest:
                    raise ValueError('Service version already has different content')
                return digest
            conn.execute('INSERT INTO service_descriptors '
                         '(descriptor_hash,service_id,operation,version,descriptor,status,proposed_at) '
                         'VALUES (?,?,?,?,?,"proposed",?)',
                         (digest, spec['service_id'], spec['operation'], spec['version'],
                          json.dumps(spec), time.time()))
        return digest

    def decide(self, digest, approve, decided_by):
        if not decided_by or type(approve) is not bool:
            raise ValueError('Owner decision required')
        with connection(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM service_descriptors WHERE descriptor_hash=?',
                               (digest,)).fetchone()
            if not row:
                raise ValueError('Unknown service proposal')
            if row['status'] != 'proposed':
                raise ValueError('Service proposal already decided')
            spec = json.loads(row['descriptor'])
            validate_descriptor(spec)
            if approve and spec['effect'] == 'write' and decided_by.startswith('system:'):
                raise PermissionError('A write summary requires owner review by Pixy')
            if approve:
                conn.execute("UPDATE service_descriptors SET status='superseded' "
                             "WHERE service_id=? AND operation=? AND status='approved'",
                             (row['service_id'], row['operation']))
            conn.execute('UPDATE service_descriptors SET status=?,decided_at=?,decided_by=? '
                         'WHERE descriptor_hash=?',
                         ('approved' if approve else 'denied', time.time(), decided_by, digest))
        return self.get(digest)

    def get(self, digest):
        with connection(self.db_path) as conn:
            row = conn.execute('SELECT * FROM service_descriptors WHERE descriptor_hash=?',
                               (digest,)).fetchone()
        return {**dict(row), 'spec': json.loads(row['descriptor'])} if row else None

    def all(self):
        with connection(self.db_path) as conn:
            rows = conn.execute('SELECT * FROM service_descriptors ORDER BY proposed_at ASC').fetchall()
        return [{**dict(row), 'spec': json.loads(row['descriptor'])} for row in rows]

    def active_services(self):
        with connection(self.db_path) as conn:
            rows = conn.execute("SELECT descriptor FROM service_descriptors WHERE status='approved'").fetchall()
        services = {}
        for row in rows:
            spec = json.loads(row['descriptor'])
            service = services.setdefault(spec['service_id'], {'operations': {}})
            service['operations'][spec['operation']] = {
                'version': spec['version'], 'url': spec['url'], 'method': spec['method'],
                'parameters': spec['parameters'], 'content_types': spec['content_types'],
                'max_bytes': spec['max_bytes'], 'timeout': spec['timeout'],
                'per_minute': spec['per_minute'], 'effect': spec['effect'],
                'credential': spec['credential_ref'],
                'credential_header': spec['credential_header'],
                'credential_prefix': spec['credential_prefix'],
                'effect_contract': spec.get('effect_contract')}
        return services


class HandServicePort:
    def __init__(self, registry):
        self.__registry = registry

    def propose(self, spec):
        return self.__registry.propose(spec)


class PixyServicePort:
    def __init__(self, registry):
        self.__registry = registry

    def get(self, digest):
        return self.__registry.get(digest)

    def all(self):
        return self.__registry.all()

    def decide(self, digest, approve, decided_by):
        return self.__registry.decide(digest, approve, decided_by)


class VoiceServicePort:
    def __init__(self, registry):
        self.__registry = registry

    def active_services(self):
        return self.__registry.active_services()
