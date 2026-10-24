"""Docker-free Linux sandbox. No unsafe host-Python fallback."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from app.common.database import migrate, connection
from app.voice.runtime.runtime import Runtime
from app.voice.runtime.contract import bundle_hash


class LocalRuntime(Runtime):
    def __init__(self, data_dir, launcher=None):
        self.root = Path(data_dir).resolve()
        self.db = self.root/'voice'/'local-runtime.sqlite3'
        self.launcher = launcher or subprocess.Popen
        self.processes = {}
        migrate(self.db, [[
            'CREATE TABLE IF NOT EXISTS executions(execution_id TEXT PRIMARY KEY,payload TEXT,result TEXT)',
        ]])
        with connection(self.db) as conn:
            for row in conn.execute('SELECT * FROM executions').fetchall():
                r = json.loads(row['result'])
                if r['status'] in {'running','started'}:
                    r.update(status='unknown',stderr='Runtime restarted; execution is never replayed')
                    conn.execute('UPDATE executions SET result=? WHERE execution_id=?',(json.dumps(r),row['execution_id']))

    def prepare(self, payload):
        if payload.get('network') or payload.get('dependencies'):
            raise PermissionError('LocalRuntime requires offline skills and preinstalled dependencies')
        if payload.get('mode') not in {'SAFE','AGENT','GOD'}:
            raise ValueError('Invalid runtime mode')
        for key,prefix in [('task_id','task'),('execution_id','run')]:
            if not re.fullmatch(prefix+'_[a-f0-9]{32}',payload[key]):
                raise ValueError('Invalid execution identity')
        if not re.fullmatch('[a-f0-9]{64}',payload['artifact_hash']):
            raise ValueError('Invalid artifact hash')
        path = self.root/'artifacts'/(payload['artifact_hash']+'.py')
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != payload['artifact_hash']:
            raise ValueError('Artifact mismatch')
        return dict(payload,network=False)

    def command(self, payload, workspace, config):
        bwrap = shutil.which('bwrap')
        if os.name != 'posix' or not bwrap:
            raise RuntimeError('LocalRuntime requires Linux bubblewrap with user namespaces; refusing host execution')
        args = [bwrap,'--die-with-parent','--new-session','--unshare-all','--clearenv',
                '--proc','/proc','--dev','/dev','--tmpfs','/tmp']
        for part in ('/usr','/lib','/lib64','/bin'):
            if Path(part).exists():
                args += ['--ro-bind',part,part]
        runner = Path(__file__).with_name('worker_runtime.py')
        args += ['--dir','/app','--dir','/app/data','--dir','/skill',
                 '--bind',str(workspace),'/app/data/public',
                 '--ro-bind',str(self.root/'artifacts'/(payload['artifact_hash']+'.py')),'/skill/main.py',
                 '--ro-bind',str(config),'/skill/input.json',
                 '--ro-bind',str(runner),'/skill/runner.py']
        if payload.get('skill_name'):
            bundle = self.root/'execution_inputs'/payload['execution_id']/'skill'
            for part in ('app','other'):
                args += ['--ro-bind',str(bundle/part),'/skill/'+part]
        args += ['--chdir','/app/data/public','--setenv','PATH','/usr/bin:/bin',
                 '--setenv','PYTHONDONTWRITEBYTECODE','1','/usr/bin/python3','/skill/runner.py']
        return args

    def _save(self, eid, result):
        with connection(self.db) as conn:
            conn.execute('UPDATE executions SET result=? WHERE execution_id=?',(json.dumps(result),eid))
        return result

    def execute(self, payload):
        payload = self.prepare(payload)
        eid = payload['execution_id']
        canonical = json.dumps(payload,sort_keys=True)
        with connection(self.db) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM executions WHERE execution_id=?',(eid,)).fetchone()
            if row:
                if row['payload'] not in {'cancelled',canonical}:
                    raise ValueError('Execution ID reused with different content')
                return json.loads(row['result'])
            if payload['deadline']<=time.time():
                raise ValueError('Execution expired')
            workspace = self.root/'workspaces'/payload['task_id']/payload['artifact_hash']
            workspace.mkdir(parents=True,exist_ok=True)
            if payload.get('skill_name'):
                bundle = self.root/'execution_inputs'/eid/'skill'
                if not bundle.is_dir() or bundle_hash(bundle)!=payload.get('skill_artifact_hash') or hashlib.sha256((bundle/'app'/'main.py').read_bytes()).hexdigest()!=payload['artifact_hash']:
                    raise ValueError('Prepared skill bundle is missing or changed')
                if not (workspace/'data').exists():
                    shutil.copytree(bundle/'data',workspace/'data')
            config = self.root/'execution_inputs'/(eid+'.json')
            config.parent.mkdir(parents=True,exist_ok=True)
            config.write_text(json.dumps(payload),encoding='utf-8')
            cmd = self.command(payload,workspace,config)
            result = {'status':'running','execution_id':eid,'started_at':time.time()}
            conn.execute('INSERT INTO executions VALUES (?,?,?)',(eid,canonical,json.dumps(result)))
        # Receipt exists before any side effect. A launch failure is never replayed.
        try:
            # Temporary output files prevent stdout pipe deadlocks.
            out = (config.parent/(eid+'.stdout')).open('w+b')
            err = (config.parent/(eid+'.stderr')).open('w+b')
            process = self.launcher(cmd,stdout=out,stderr=err,env={'PATH':'/usr/bin:/bin'},start_new_session=True)
            self.processes[eid] = (process,out,err,payload)
        except Exception:
            if 'out' in locals(): out.close()
            if 'err' in locals(): err.close()
            self._save(eid,dict(result,status='unknown',stderr='Launch could not be confirmed'))
            raise
        return result

    def status(self, eid):
        with connection(self.db) as conn:
            row = conn.execute('SELECT result FROM executions WHERE execution_id=?',(eid,)).fetchone()
        if not row:
            raise KeyError(eid)
        result = json.loads(row[0])
        if eid not in self.processes:
            return result
        p,out,err,payload = self.processes[eid]
        timeout = min(payload['deadline'],result['started_at']+(1800 if payload['mode']=='AGENT' else 120))
        expired = time.time() >= timeout
        if p.poll() is None and expired:
            p.kill()
            p.wait(timeout=10)
        if p.poll() is None:
            if payload['mode']=='AGENT':
                result['status']='started'
                return self._save(eid,result)
            return result
        out.seek(0); err.seek(0)
        result.update(status='timeout' if expired else 'finished' if p.returncode==0 else 'failed',
                      returncode=p.returncode,stdout=out.read(16000).decode(errors='replace'),
                      stderr=err.read(8000).decode(errors='replace'),evidence_kind='process_exit',
                      duration=time.time()-result['started_at'],resource_usage={})
        for key in ('skill_name','version_id','artifact_hash'):
            result[key] = payload.get(key)
        for line in result['stdout'].splitlines():
            if p.returncode == 75 and line.startswith('KHAI_APPROVAL:'):
                result.update(status='approval_required',approval=json.loads(line.split(':',1)[1]))
        out.close(); err.close()
        del self.processes[eid]
        return self._save(eid,result)

    def cancel(self, eid):
        if not re.fullmatch('run_[a-f0-9]{32}',eid):
            raise ValueError('Invalid execution ID')
        if eid in self.processes:
            p,out,err,_ = self.processes.pop(eid)
            p.kill(); p.wait(timeout=10)
            out.close(); err.close()
        result = {'status':'cancelled','execution_id':eid}
        with connection(self.db) as conn:
            conn.execute('INSERT OR IGNORE INTO executions VALUES (?,?,?)',(eid,'cancelled',json.dumps(result)))
        return self._save(eid,result)

    def collect_result(self, execution_id):
        return self.status(execution_id)

    def cleanup(self, execution_id):
        return self.cancel(execution_id) if execution_id in self.processes else self.status(execution_id)
