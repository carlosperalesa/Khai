"""Durable Docker execution. Reconcile named containers; never replay an unknown run."""
import hashlib
import json
import os
import re
import sqlite3
from app.kernel.database import connect
from app.voice.runtime.contract import bundle_hash
import subprocess
import time
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

FINAL = {'finished', 'failed', 'timeout', 'cancelled', 'unknown', 'approval_required'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Engine:
    def __init__(self, data_dir, host_data_dir, host_runtime_dir, runner=None):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.host_data = host_data_dir.rstrip('/')
        self.host_runtime = host_runtime_dir.rstrip('/')
        self.db = self.root / 'voice' / 'docker-runtime.sqlite3'
        self.run = runner or subprocess.run
        self.launcher = ThreadPoolExecutor(max_workers=1, thread_name_prefix="khai-launch")
        self.inflight = {}
        self.last_tick = time.time()
        with self.connect() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE IF NOT EXISTS executions(id TEXT PRIMARY KEY, fingerprint TEXT, payload TEXT, result TEXT, cancel INTEGER DEFAULT 0)')
        from app.kernel.database import adopt_schema
        adopt_schema(self.db,'docker-runtime',['executions'])

    @contextmanager
    def connect(self):
        conn = connect(self.db)
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=5000')
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def submit(self, payload):
        if payload.get('network'):
            raise PermissionError('Direct skill network access is forbidden; use Voice Gateway')
        for key, prefix in [('execution_id', 'run'), ('task_id', 'task')]:
            if not re.fullmatch(prefix + r'_[a-f0-9]{32}', payload[key]):
                raise ValueError('Identificador inválido')
        sha = payload['artifact_hash']
        if not re.fullmatch('[a-f0-9]{64}', sha) or payload['mode'] not in {'SAFE', 'GOD', 'AGENT'}:
            raise ValueError('Artefacto/modo inválido')
        if len(payload['request']) > 64000 or len(payload.get('dependencies', [])) > 30:
            raise ValueError('Payload demasiado grande')
        if not isinstance(payload.get('params', {}), dict) or len(json.dumps(payload.get('params', {}))) > 16000:
            raise ValueError('Parámetros de skill inválidos o demasiado grandes')
        for dep in payload.get('dependencies', []):
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*(?:==[A-Za-z0-9][A-Za-z0-9_.+-]*)?', dep):
                raise ValueError('Dependencia inválida')
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM executions WHERE id=?', (payload['execution_id'],)).fetchone()
            if row:
                if row['fingerprint'] == 'cancelled':
                    return json.loads(row['result'])
                if row['fingerprint'] != fingerprint(payload):
                    raise ValueError('Execution ID reutilizado con otro contenido')
                return json.loads(row['result'])
            if payload['deadline'] <= time.time():
                raise ValueError('Objetivo vencido')
            artifact = self.root / 'artifacts' / (sha + '.py')
            if artifact.is_symlink() or not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != sha:
                raise ValueError('Hash del artefacto inválido')
            result = dict(status='queued', execution_id=payload['execution_id'])
            conn.execute('INSERT INTO executions(id,fingerprint,payload,result) VALUES (?,?,?,?)',
                         (payload['execution_id'], fingerprint(payload), json.dumps(payload), json.dumps(result)))
            return result

    def get(self, eid):
        with self.connect() as conn:
            row = conn.execute('SELECT result FROM executions WHERE id=?', (eid,)).fetchone()
            if not row:
                raise KeyError(eid)
            return json.loads(row[0])

    def save(self, eid, **result):
        result['execution_id'] = eid
        with self.connect() as conn:
            row = conn.execute('SELECT payload,result FROM executions WHERE id=?',(eid,)).fetchone()
        if row:
            payload = json.loads(row['payload'])
            previous = json.loads(row['result'])
            for key in ('skill_name','version_id','artifact_hash'):
                result[key] = payload.get(key)
            start = result.get('started_at',previous.get('started_at'))
            if start:
                result['started_at'] = start
                result['duration'] = max(0,time.time()-start)
            result.setdefault('resource_usage',{})
        with self.connect() as conn:
            conn.execute('UPDATE executions SET result=? WHERE id=?', (json.dumps(result), eid))
        return result

    def docker(self, args, timeout=20, **kwargs):
        return self.run(['docker'] + args, capture_output=True, text=True, timeout=timeout, **kwargs)

    def inspect(self, name):
        result = self.docker(['inspect', name])
        if result.returncode:
            # A daemon outage is not evidence of container absence.
            if 'No such' in result.stderr:
                return None
            raise RuntimeError('Docker inspect falló')
        return json.loads(result.stdout)[0]

    def remove(self, name):
        result = self.docker(['rm', '-f', name])
        if result.returncode and 'No such' not in result.stderr:
            raise RuntimeError('No se pudo confirmar la eliminación del contenedor')

    def cancel(self, eid):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM executions WHERE id=?', (eid,)).fetchone():
                # Tombstone prevents a delayed submit from starting after cancellation.
                conn.execute('INSERT INTO executions VALUES (?,?,?,?,1)',
                             (eid, 'cancelled', '{}', json.dumps({'status': 'cancelled', 'execution_id': eid})))
                return {'status': 'cancelled', 'execution_id': eid}
            current = json.loads(conn.execute('SELECT result FROM executions WHERE id=?', (eid,)).fetchone()[0])
            if current['status'] == 'cancelled':
                return current
            conn.execute('UPDATE executions SET cancel=1 WHERE id=?', (eid,))
        # tick is the only lifecycle owner. HTTP never races a Docker create/start.
        return {'status': 'cancelling', 'execution_id': eid}

    def _cancelled(self, eid):
        with self.connect() as conn:
            return bool(conn.execute('SELECT cancel FROM executions WHERE id=?', (eid,)).fetchone()[0])

    def create_command(self, p, image):
        rel = 'workspaces/' + p['task_id'] + '/' + p['artifact_hash']
        workspace = self.root / rel
        workspace.mkdir(parents=True, exist_ok=True)
        bundle_mounts = []
        if p.get('skill_name'):
            bundle = self.root/'execution_inputs'/p['execution_id']/'skill'
            if not bundle.is_dir() or bundle_hash(bundle)!=p.get('skill_artifact_hash') or hashlib.sha256((bundle/'app'/'main.py').read_bytes()).hexdigest()!=p['artifact_hash']:
                raise ValueError('Prepared skill bundle is missing or changed')
            if not (workspace/'data').exists():
                shutil.copytree(bundle/'data',workspace/'data')
            for part in ('app','other'):
                bundle_mounts += ['-v',self.host_data+'/execution_inputs/'+p['execution_id']+'/skill/'+part+':/skill/'+part+':ro']
        runtime_input = self.root / 'execution_inputs' / (p['execution_id'] + '.json')
        runtime_input.parent.mkdir(exist_ok=True)
        runtime_input.write_text(json.dumps(p), encoding='utf-8')
        return ['create', '--name', p['execution_id'], '--label', 'khai.execution=' + p['execution_id'],
                '--label', 'khai.artifact=' + p['artifact_hash'], '--network', 'none',
                '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--pids-limit', '128', '--memory', '512m', '--cpus', '1',
                '--log-opt', 'max-size=2m', '--log-opt', 'max-file=1',
                '--tmpfs', '/tmp:rw,nosuid,noexec,size=64m', '--workdir', '/app/data/public',
                '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONUNBUFFERED=1',
                '-v', self.host_data + '/' + rel + ':/app/data/public:rw',
                '-v', self.host_data + '/artifacts/' + p['artifact_hash'] + '.py:/skill/main.py:ro',
                '-v', self.host_data + '/execution_inputs/' + p['execution_id'] + '.json:/skill/input.json:ro',
                '-v', self.host_runtime + '/worker_runtime.py:/skill/runner.py:ro',
                *bundle_mounts, image, 'python', '/skill/runner.py']

    def launch(self, p):
        eid = p['execution_id']
        self.save(eid, status='starting', started_at=time.time())
        image = 'python:3.10-slim'
        if p.get('dependencies'):
            image = 'khai-worker:' + fingerprint(p['dependencies'])[:24]
            dockerfile = 'FROM python:3.10-slim\nRUN pip install --no-cache-dir ' + ' '.join(p['dependencies']) + '\n'
            built = self.docker(['build', '-t', image, '-'], timeout=180, input=dockerfile)
            if built.returncode:
                self.save(eid, status='failed', returncode=1, stderr=built.stderr[-4000:])
                return
        if self._cancelled(eid) or time.time() >= p['deadline']:
            self.save(eid, status='cancelled')
            return
        artifact = self.root / 'artifacts' / (p['artifact_hash'] + '.py')
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != p['artifact_hash']:
            self.save(eid, status='failed', returncode=1, stderr='El artefacto cambió antes del lanzamiento')
            return
        created = self.docker(self.create_command(p, image))
        if created.returncode:
            # Name collision may be a recovered create; reconcile, never create another ID.
            if self.inspect(eid) is None:
                self.save(eid, status='failed', returncode=1, stderr=created.stderr[-4000:])
                return
        if self._cancelled(eid):
            self.remove(eid)
            self.save(eid, status='cancelled')
            return
        started = self.docker(['start', eid])
        if started.returncode:
            self.save(eid, status='unknown', stderr=started.stderr[-4000:])
            return
        self.save(eid, status='running', started_at=time.time())

    def reconcile(self, p, previous):
        eid = p['execution_id']
        item = self.inspect(eid)
        if item is None:
            self.save(eid, status='unknown', stderr='Contenedor ausente; no se reproduce la ejecución')
            return
        labels = item.get('Config', {}).get('Labels', {}) or {}
        if labels.get('khai.execution') != eid or labels.get('khai.artifact') != p['artifact_hash']:
            self.save(eid, status='unknown', stderr='Identidad del contenedor no coincide')
            return
        state = item['State']
        if self._cancelled(eid):
            self.remove(eid)
            self.save(eid, status='cancelled')
            return
        start = previous.get('started_at', time.time())
        limit = p['deadline'] if p['mode'] == 'AGENT' else min(p['deadline'], start + 120)
        if time.time() >= limit:
            self.remove(eid)
            self.save(eid, status='timeout', returncode=124, stderr='Límite de ejecución alcanzado; contenedor eliminado')
            return
        if state.get('Status') == 'created':
            # Persisted create never started; starting this exact container is safe.
            result = self.docker(['start', eid])
            if result.returncode:
                raise RuntimeError('No se pudo iniciar el contenedor preparado')
            self.save(eid, status='running', started_at=start)
            return
        if state.get('Running'):
            self.save(eid, status='started' if p['mode'] == 'AGENT' else 'running', started_at=start)
            return
        logs = self.docker(['logs', '--tail', '200', eid])
        output, errors = logs.stdout[-16000:], logs.stderr[-8000:]
        code = state.get('ExitCode', 1)
        approval = None
        for line in output.splitlines():
            if line.startswith('KHAI_APPROVAL:'):
                try:
                    approval = json.loads(line.split(':', 1)[1])
                except ValueError:
                    pass
        if code == 75 and isinstance(approval, dict) and set(approval) == {'action', 'details'}:
            result = dict(status='approval_required', approval=approval, returncode=75, stdout=output)
        else:
            result = dict(status='finished' if code == 0 else 'failed', returncode=code, stdout=output, stderr=errors,
                          oom_killed=state.get('OOMKilled', False), evidence_kind='process_exit')
        # Persist receipt BEFORE removing the container; crash recovery reuses the receipt.
        self.save(eid, **result)
        self.remove(eid)

    def tick(self):
        self.last_tick = time.time()
        with self.connect() as conn:
            rows = list(conn.execute('SELECT * FROM executions'))
        for row in rows:
            p, result = json.loads(row['payload']), json.loads(row['result'])
            eid = row['id']
            try:
                future = self.inflight.get(eid)
                if future is not None:
                    if not future.done():
                        continue
                    del self.inflight[eid]
                    future.result()
                    result = self.get(eid)
                if row['cancel']:
                    if result['status'] != 'cancelled':
                        self.remove(eid)
                        self.save(eid, status='cancelled')
                    continue
                if result['status'] in FINAL:
                    if result['status']!='unknown' and not result.get('cleaned'):
                        self.remove(eid)
                        self.save(eid,**{k:v for k,v in dict(result,cleaned=True).items() if k!='execution_id'})
                    continue
                if result['status'] == 'queued':
                    if not self.inflight:
                        self.save(eid, status='starting', started_at=time.time())
                        self.inflight[eid] = self.launcher.submit(self.launch, p)
                else:
                    self.reconcile(p, result)
            except subprocess.TimeoutExpired:
                try:
                    self.remove(eid)
                    self.save(eid, status='timeout', returncode=124)
                except Exception:
                    self.save(eid, status='unknown', stderr='Timeout; limpieza sin confirmar')
            except Exception:
                # Keep active receipt on daemon/transport failure and reconcile next tick.
                import logging
                logging.getLogger('docker_proxy.engine').exception('No se pudo reconciliar %s', eid)

    def close(self):
        self.launcher.shutdown(wait=True)
