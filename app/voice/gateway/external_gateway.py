"""Named operations, pinned public DNS, bounded responses and private credentials."""
import http.client
import hashlib
import ipaddress
import json
import socket
import ssl
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlencode, urlsplit
import yaml
from app.common.files import read_regular
from app.common.database import connection, migrate


class ExternalGateway:
    def __init__(self, policy_path, secrets, events=None, db=None):
        path = Path(policy_path)
        self.services = (yaml.safe_load(path.read_text(encoding='utf-8')) or {}).get('services',{}) if path.exists() else {}
        self.secrets, self.events = secrets, events
        self.calls = defaultdict(deque)
        self.db = db
        if db:
            migrate(db,[[
                'CREATE TABLE IF NOT EXISTS gateway_requests(request_id TEXT PRIMARY KEY,task_id TEXT,fingerprint TEXT,status TEXT,reply TEXT)',
            ]],component='gateway')

    def _mailbox_request(self, task, request_id, request):
        if not self.db:
            raise RuntimeError('Durable Voice receipt storage is required')
        fingerprint = hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
        rid = task['execution_id']+':'+request_id
        with connection(self.db) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM gateway_requests WHERE request_id=?',(rid,)).fetchone()
            if row:
                if row['fingerprint']!=fingerprint:
                    raise PermissionError('Gateway request ID changed')
                return json.loads(row['reply']) if row['status']=='completed' else {'error':'UnconfirmedPreviousRequest'}
            count = conn.execute('SELECT COUNT(*) FROM gateway_requests WHERE task_id=?',(task['id'],)).fetchone()[0]
            if count>=32 or time.time()>=task['deadline']:
                raise PermissionError('Gateway task budget exceeded')
            conn.execute('INSERT INTO gateway_requests VALUES (?,?,?,?,?)',(rid,task['id'],fingerprint,'sending',None))
        try:
            result = self.request(request['service'],request['operation'],request.get('params'))
            if result['content_type']!='application/json':
                raise PermissionError('Worker mailbox only returns JSON; downloads use AssetGateway')
            reply = {'result':json.loads(result['body'])}
        except Exception as exc:
            reply = {'error':type(exc).__name__}
        with connection(self.db) as conn:
            conn.execute('UPDATE gateway_requests SET status="completed",reply=? WHERE request_id=?',(json.dumps(reply),rid))
        return reply

    def request(self, service, operation, params=None):
        spec = self.services.get(service,{}).get('operations',{}).get(operation)
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
                reply = self._mailbox_request(task,path.name,request)
            except Exception as exc:
                reply = {'error':type(exc).__name__}
            try:
                # Exclusive creation refuses worker-created symlinks and overwrites.
                with response.open('x',encoding='utf-8') as stream:
                    json.dump(reply,stream)
            except FileExistsError:
                pass
