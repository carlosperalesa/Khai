"""Named operations, pinned public DNS, bounded responses and private credentials."""
import http.client
import hashlib
import ipaddress
import json
import logging
import socket
import ssl
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlencode, urlsplit
import yaml
from app.voice.files import read_regular
from app.kernel.database import connection, migrate
from app.kernel.execution_authority import UnconfirmedDispatch


class DispatchRejected(PermissionError):
    """The reviewed operation was rejected before calling its transport."""


def batch_effects(batch, request_items, reply):
    """Map explicit provider positions to bounded individual technical receipts."""
    unknown = [{'item_index': index, 'status': 'unknown', 'resource_id': None}
               for index in range(len(request_items))]
    result = reply.get('result')
    rows = result.get(batch['response_field']) if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) > len(request_items):
        return unknown
    indexed = {}
    for row in rows:
        if not isinstance(row, dict):
            return unknown
        index = row.get(batch['index_field'])
        if type(index) is not int or not 0 <= index < len(request_items) or index in indexed:
            return unknown
        indexed[index] = row
    effects, successful_ids = [], set()
    for index in range(len(request_items)):
        row = indexed.get(index, {})
        outcome = row.get(batch['status_field'])
        resource_id = None
        status = 'unknown'
        if outcome == batch['failure_value']:
            status = 'failed'
        elif outcome == batch['success_value']:
            value = row.get(batch['identity_field'])
            if type(value) in {str, int} and 0 < len(str(value)) <= 256:
                resource_id = str(value)
                status = 'completed'
                if resource_id in successful_ids:
                    return unknown
                successful_ids.add(resource_id)
        effects.append({'item_index': index, 'status': status, 'resource_id': resource_id})
    return effects


class ExternalGateway:
    def __init__(self, policy_path, secrets, events=None, db=None, registry=None, execution_authority=None):
        path = Path(policy_path)
        self._static_services = (yaml.safe_load(path.read_text(encoding='utf-8')) or {}).get('services',{}) if path.exists() else {}
        self.registry = registry
        self.secrets, self.events = secrets, events
        self.calls = defaultdict(deque)
        self.db = db
        self.execution_authority = execution_authority
        if db:
            migrate(db,[
                ['CREATE TABLE IF NOT EXISTS gateway_requests(request_id TEXT PRIMARY KEY,task_id TEXT,fingerprint TEXT,status TEXT,reply TEXT)'],
                [
                '''CREATE TABLE IF NOT EXISTS gateway_receipts(
                   request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                   execution_id TEXT NOT NULL, service_id TEXT, operation TEXT,
                   service_version TEXT, effect TEXT, status TEXT NOT NULL,
                   reply TEXT, created_at REAL NOT NULL,
                   effect_contract TEXT, resource_id TEXT)''',
                ],
                [
                'ALTER TABLE gateway_receipts ADD COLUMN new_effect_authorization_id TEXT',
                'CREATE UNIQUE INDEX IF NOT EXISTS gateway_receipts_new_effect_once '
                'ON gateway_receipts(new_effect_authorization_id) '
                'WHERE new_effect_authorization_id IS NOT NULL',
                ],
                [
                '''CREATE TABLE IF NOT EXISTS gateway_batch_effects(
                   parent_request_id TEXT NOT NULL, item_index INTEGER NOT NULL,
                   status TEXT NOT NULL, resource_id TEXT,
                   PRIMARY KEY(parent_request_id,item_index))''',
                ],
                ['ALTER TABLE gateway_receipts ADD COLUMN parameter_hashes TEXT'],
            ],component='gateway')

    @property
    def services(self):
        return {**self._static_services,
                **(self.registry.active_services() if self.registry is not None else {})}

    def _mailbox_request(self, task, request_id, request):
        if not self.db:
            raise RuntimeError('Durable Voice receipt storage is required')
        if self.execution_authority is None:
            raise PermissionError('Voice requires a scoped execution authority')
        fingerprint = hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
        parameter_hashes = {name: hashlib.sha256(json.dumps(value, sort_keys=True,
            ensure_ascii=False).encode()).hexdigest()
            for name, value in (request.get('params') or {}).items()}
        rid = task['execution_id']+':'+request_id
        service_id, operation_name = request['service'], request['operation']
        operation = self.services.get(service_id, {}).get('operations', {}).get(operation_name, {})
        effect = operation.get('effect')
        contract = operation.get('effect_contract')
        batch = contract.get('batch') if isinstance(contract, dict) else None
        request_items = None
        if batch:
            request_items = request.get('params', {}).get(batch['request_field'])
            if (not isinstance(request_items, list) or not 1 <= len(request_items) <= 32
                    or any(not isinstance(item, dict) for item in request_items)):
                raise ValueError('Batch request must contain 1 to 32 individual objects')
        new_effect = task.get('new_effect_authorization')
        if effect == 'write':
            if not contract:
                raise PermissionError('Write operation has no declared effect contract')
            constraints = task.get('effect_constraints') or []
            if constraints and not any(
                (service_id, operation_name, operation.get('version')) ==
                (item['service_id'], item['operation'], item['version'])
                and str(request.get('params', {}).get(item['identity_parameter'])) == item['resource_id']
                for item in constraints
            ):
                raise PermissionError('Write does not match a reviewed resource correction')
            if new_effect and (new_effect.get('task_id') != task['id']
                    or (service_id, operation_name, operation.get('version')) !=
                    (new_effect.get('service_id'), new_effect.get('operation'),
                     new_effect.get('version'))
                    or task.get('state') != 'RUNNING'):
                raise PermissionError('Write does not match the owner-authorized new effect')
            if batch and new_effect:
                raise PermissionError('A single new-effect authorization cannot create a batch')
        with connection(self.db) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM gateway_requests WHERE request_id=?',(rid,)).fetchone()
            if row:
                if row['fingerprint']!=fingerprint:
                    raise PermissionError('Gateway request ID changed')
                return json.loads(row['reply']) if row['status']=='completed' else {'error':'UnconfirmedPreviousRequest'}
            if effect == 'write' and new_effect and conn.execute(
                    'SELECT 1 FROM gateway_receipts WHERE new_effect_authorization_id=?',
                    (new_effect['id'],)).fetchone():
                raise PermissionError('Owner authorization already used for a new effect')
            count = conn.execute('SELECT COUNT(*) FROM gateway_requests WHERE task_id=?',(task['id'],)).fetchone()[0]
            if count>=32 or time.time()>=task['deadline']:
                raise PermissionError('Gateway task budget exceeded')
            conn.execute('INSERT INTO gateway_requests VALUES (?,?,?,?,?)',(rid,task['id'],fingerprint,'sending',None))
            conn.execute('''INSERT INTO gateway_receipts
                (request_id,task_id,execution_id,service_id,operation,service_version,effect,
                 status,reply,created_at,effect_contract,resource_id,new_effect_authorization_id,
                 parameter_hashes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                         (rid, task['id'], task['execution_id'], service_id, operation_name,
                          operation.get('version'), 'batch' if batch else effect, 'sending', None,
                          time.time(), json.dumps(contract) if contract else None, None,
                          new_effect['id'] if effect == 'write' and new_effect else None,
                          json.dumps(parameter_hashes)))
            if batch:
                for index in range(len(request_items)):
                    conn.execute('INSERT INTO gateway_batch_effects VALUES (?,?,?,NULL)',
                                 (rid, index, 'unknown'))
        admitted = False
        rejected = False
        uncertain = False
        try:
            self.execution_authority.authorize_dispatch(task, rid, fingerprint, service_id,
                operation_name, operation.get('version'), effect)
            admitted = True
            result = self.request(request['service'],request['operation'],request.get('params'),
                                  expected_version=operation.get('version'))
            if result['content_type']!='application/json':
                raise PermissionError('Worker mailbox only returns JSON; downloads use AssetGateway')
            reply = {'result':json.loads(result['body'])}
        except Exception as exc:
            reply = {'error':type(exc).__name__}
            uncertain = isinstance(exc, UnconfirmedDispatch) or (admitted and effect == 'write'
                        and not isinstance(exc, DispatchRejected))
            rejected = not admitted and not uncertain or isinstance(exc, DispatchRejected)
        resource_id = None
        if effect == 'write' and not batch and isinstance(reply.get('result'), dict):
            identity = contract.get('identity')
            if identity:
                value = (request.get('params', {}) if identity['source'] == 'request'
                         else reply['result']).get(identity['field'])
                if type(value) in {str, int} and 0 < len(str(value)) <= 256:
                    resource_id = str(value)
        with connection(self.db) as conn:
            conn.execute('UPDATE gateway_requests SET status="completed",reply=? WHERE request_id=?',(json.dumps(reply),rid))
            effects = batch_effects(batch, request_items, reply) if batch else []
            status = ('completed' if effects and all(item['status'] == 'completed' for item in effects)
                      else 'partial' if effects and all(item['status'] != 'unknown' for item in effects)
                      else 'uncertain' if batch else
                      'uncertain' if uncertain else 'denied' if rejected else
                      'failed' if 'error' in reply else 'completed')
            conn.execute('UPDATE gateway_receipts SET status=?,reply=?,resource_id=? WHERE request_id=?',
                         (status,json.dumps(reply),resource_id,rid))
            for item in effects:
                conn.execute('''UPDATE gateway_batch_effects SET status=?,resource_id=?
                                WHERE parent_request_id=? AND item_index=?''',
                             (item['status'], item['resource_id'],rid,item['item_index']))
        if admitted:
            try:
                self.execution_authority.record_outcome(rid, status)
            except Exception as exc:
                logging.getLogger(__name__).warning('Dispatch outcome remains in Voice receipts (%s)', type(exc).__name__)
        return reply

    def technical_receipts(self, task_id, execution_id, approved_refs):
        """Return transport facts only; Mind evaluates whether they satisfy an objective."""
        if not self.db:
            return []
        approved = {(ref['service_id'], ref['operation'], ref['version'])
                    for ref in approved_refs}
        with connection(self.db) as conn:
            rows = conn.execute('''SELECT * FROM gateway_receipts
                                   WHERE task_id=? AND execution_id=? ORDER BY created_at,request_id''',
                                (task_id, execution_id)).fetchall()
            receipts = []
            for row in rows:
                if (row['service_id'], row['operation'], row['service_version']) not in approved:
                    continue
                contract = json.loads(row['effect_contract']) if row['effect_contract'] else None
                item = {'request_id': row['request_id'], 'task_id': row['task_id'],
                        'execution_id': row['execution_id'], 'service_id': row['service_id'],
                        'operation': row['operation'], 'version': row['service_version'],
                        'effect': row['effect'], 'status': row['status'],
                        'effect_contract': contract,
                        'summary': contract['summary'] if contract else None,
                        'resource_id': row['resource_id'],
                        'new_effect_authorization_id': row['new_effect_authorization_id'],
                        'parameter_hashes': json.loads(row['parameter_hashes']) if row['parameter_hashes'] else {},
                        'reply': json.loads(row['reply']) if row['reply'] else None}
                if row['effect'] != 'batch':
                    receipts.append(item)
                    continue
                effects = conn.execute(
                    'SELECT * FROM gateway_batch_effects WHERE parent_request_id=? ORDER BY item_index',
                    (row['request_id'],)).fetchall()
                for effect_row in effects:
                    receipts.append({**item, 'request_id': f"{row['request_id']}#item{effect_row['item_index'] + 1}",
                                     'parent_request_id': row['request_id'],
                                     'item_index': effect_row['item_index'],
                                     'effect': 'write', 'status': effect_row['status'],
                                     'resource_id': effect_row['resource_id'],
                                     'new_effect_authorization_id': None})
        return receipts

    def request(self, service, operation, params=None, *, expected_version=None):
        spec = self.services.get(service,{}).get('operations',{}).get(operation)
        if expected_version is not None and (not spec or spec.get('version') != expected_version):
            raise DispatchRejected('Approved service version is no longer active')
        if not spec:
            raise PermissionError('Unknown Voice service/operation')
        params = params or {}
        if not isinstance(params,dict) or set(params)-set(spec.get('parameters',[])):
            raise PermissionError('Parameters are not allowed')
        if len(json.dumps(params)) > 16000:
            raise ValueError('Request too large')
        url = urlsplit(spec['url'])
        if url.scheme != 'https' or url.username or url.password or url.fragment or url.port not in {None,443}:
            raise PermissionError('Only fixed public HTTPS endpoints are permitted')
        method = spec.get('method','GET')
        if method not in {'GET','POST'}:
            raise PermissionError('HTTP method denied')
        calls = self.calls[(service,operation)]
        now = time.monotonic()
        while calls and calls[0] < now-60:
            calls.popleft()
        if len(calls) >= min(int(spec.get('per_minute',10)),60):
            raise PermissionError('Voice rate limit exceeded')
        calls.append(now)
        addresses = socket.getaddrinfo(url.hostname,443,type=socket.SOCK_STREAM)
        ips = {item[4][0] for item in addresses}
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise PermissionError('Private and special destinations are forbidden')
        timeout = max(1,min(float(spec.get('timeout',15)),30))
        max_bytes = min(int(spec.get('max_bytes',2*1024*1024)),64*1024*1024)
        connection = http.client.HTTPSConnection(url.hostname,timeout=timeout)
        try:
            # Pin the validated address while TLS verifies the configured hostname.
            sock = socket.create_connection((sorted(ips)[0],443),timeout=timeout)
            connection.sock = ssl.create_default_context().wrap_socket(sock,server_hostname=url.hostname)
            headers = {'Accept':','.join(spec.get('content_types',['application/json']))}
            secret_key = spec.get('credential')
            value = None
            if secret_key:
                value = self.secrets.get(secret_key)
                if not value:
                    raise PermissionError('Provider credential unavailable')
                header = spec.get('credential_header','Authorization')
                if header.lower() in {'host','content-length','transfer-encoding'}:
                    raise PermissionError('Invalid credential header')
                headers[header] = spec.get('credential_prefix','Bearer ')+value
            path = url.path or '/'
            query = url.query
            body = None
            if method == 'GET':
                extra = urlencode(params)
                query = '&'.join(x for x in (query,extra) if x)
            else:
                body = json.dumps(params).encode()
                headers['Content-Type'] = 'application/json'
            if query:
                path += '?'+query
            connection.request(method,path,body=body,headers=headers)
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise RuntimeError('Provider returned HTTP '+str(response.status))
            mime = (response.getheader('Content-Type') or '').split(';')[0].lower()
            if mime not in spec.get('content_types',['application/json']):
                raise PermissionError('Unexpected response type')
            chunks, length = [],0
            deadline = time.monotonic()+timeout
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Response deadline exceeded')
                chunk = response.read(min(65536,max_bytes-length+1))
                if not chunk:
                    break
                length += len(chunk)
                if length > max_bytes:
                    raise ValueError('Response exceeds size limit')
                chunks.append(chunk)
            raw = b''.join(chunks)
            if value and value.encode('utf-8') in raw:
                raise PermissionError('Provider response reflected a credential')
            if self.events:
                self.events.emit('voice','external_request',metadata={'service':service,'operation':operation,'size':length})
            return {'content_type':mime,'body':raw}
        except (PermissionError,ValueError):
            raise
        except Exception as exc:
            raise RuntimeError('Voice provider request failed: '+type(exc).__name__) from None
        finally:
            connection.close()

    def process_mailbox(self, task, workspace):
        """Workers exchange bounded JSON through their private workspace, never TCP."""
        root = Path(workspace)
        if not root.exists():
            return
        for path in list(root.glob('.voice-*.request.json'))[:8]:
            response = path.with_name(path.name.replace('.request.json','.response.json'))
            if response.exists():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or path.stat().st_size > 16384:
                continue
            try:
                if 'Network' not in task.get('manifest',{}).get('capabilities',[]) or task.get('authorized_hash') != task['artifact_hash']:
                    raise PermissionError('Task has no reviewed network capability')
                request = json.loads(read_regular(path,16384))
                if (request.get('execution_id') != task.get('execution_id')
                        or request.get('artifact_hash') != task.get('artifact_hash')
                        or request.get('goal_revision') != task.get('goal_revision', 0)):
                    raise PermissionError('Worker request belongs to another execution or revision')
                approved_refs = list(task.get('services') or [])
                if not approved_refs and task.get('creation_consent'):
                    approved_refs = list(task['creation_consent'].get('allowed_services', []))
                if not approved_refs and task.get('setup_proposals'):
                    approved_refs = [p['ref'] for p in task['setup_proposals'] if p.get('ref')]
                approved = {(ref['service_id'], ref['operation'], ref['version'])
                            for ref in approved_refs}
                requested = (request.get('service'), request.get('operation'))
                operation = self.services.get(requested[0], {}).get('operations', {}).get(requested[1])
                if not operation or (requested[0], requested[1], operation.get('version')) not in approved:
                    raise PermissionError('Service operation or version is outside task approval')
                reply = self._mailbox_request(task,path.name,request)
            except Exception as exc:
                reply = {'error':type(exc).__name__}
            try:
                # Exclusive creation refuses worker-created symlinks and overwrites.
                with response.open('x',encoding='utf-8') as stream:
                    json.dump(reply,stream)
            except FileExistsError:
                pass
