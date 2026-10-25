import ast
import hashlib
import json
import re
import shutil
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from app.common.database import connection, migrate
from app.common.recovery import FINAL_STEP, canonical, plan_hash, validate_case_id, validate_plan
from app.pixy.control import InstanceLock
from app.voice.runtime.contract import digest, skill_metadata

RECOVERY_STEP_TIMEOUT = 300


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class Hand:
    def __init__(self, root, pixy, events=None, recovery_executor=None, llm=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root/'hand.sqlite3'
        self.pixy, self.events, self.llm = pixy, events, llm
        self.recovery_executor = recovery_executor
        self.recovery_root = self.root/'heart'
        self._mutex = threading.RLock()
        migrate(self.db, [[
            '''CREATE TABLE IF NOT EXISTS versions(version_id TEXT PRIMARY KEY, skill_name TEXT NOT NULL,
               version_number TEXT NOT NULL, status TEXT NOT NULL, artifact_hash TEXT, archive_name TEXT,
               archive_hash TEXT, archive_size INTEGER, created_at REAL, activated_at REAL, archived_at REAL,
               previous_version_id TEXT, pixy_audit_id TEXT, restored_from_version_id TEXT,
               manifest TEXT NOT NULL, UNIQUE(skill_name,version_number))''',
            '''CREATE TABLE IF NOT EXISTS operations(operation_id TEXT PRIMARY KEY, operation_type TEXT,
               skill_name TEXT, source_version_id TEXT, target_version_id TEXT, status TEXT,
               correlation_id TEXT, reason TEXT, created_at REAL, completed_at REAL, error TEXT)''',
            'CREATE UNIQUE INDEX IF NOT EXISTS active_skill ON versions(skill_name) WHERE status="active"',
            '''CREATE TRIGGER IF NOT EXISTS operations_no_delete BEFORE DELETE ON operations
               BEGIN SELECT RAISE(ABORT,'immutable operation history'); END''',
            '''CREATE TRIGGER IF NOT EXISTS operations_completed BEFORE UPDATE ON operations
               WHEN OLD.completed_at IS NOT NULL BEGIN SELECT RAISE(ABORT,'completed operation is immutable'); END''',
        ]])
        migrate(self.db, [[
            '''CREATE TABLE IF NOT EXISTS recovery_skills(case_id TEXT PRIMARY KEY, authorization_id TEXT NOT NULL,
               plan_hash TEXT NOT NULL, skill_hash TEXT NOT NULL, status TEXT NOT NULL,
               created_at REAL NOT NULL, updated_at REAL NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS recovery_steps(case_id TEXT NOT NULL, step_index INTEGER NOT NULL,
               action TEXT NOT NULL, target TEXT NOT NULL, action_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
               receipt TEXT, submitted_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(case_id,step_index))''',
            '''CREATE TRIGGER IF NOT EXISTS recovery_skills_no_delete BEFORE DELETE ON recovery_skills
               BEGIN SELECT RAISE(ABORT,'recovery history is immutable'); END''',
            '''CREATE TRIGGER IF NOT EXISTS recovery_steps_no_delete BEFORE DELETE ON recovery_steps
               BEGIN SELECT RAISE(ABORT,'recovery history is immutable'); END''',
        ]], component='hand-recovery')
        self.recover()

    def recover(self):
        """Reconcile a crash between directory rename and the SQLite activation."""
        with self.locked():
            with connection(self.db) as conn:
                rows = [dict(r) for r in conn.execute('SELECT * FROM versions WHERE status IN ("approved","active")')]
            for v in rows:
                folder = self.root/v['skill_name']
                draft = self.root/('.'+v['skill_name'])
                old = self.root/('.previous-'+v['version_id'])
                if v['status']=='approved':
                    if folder.exists() and self.tree_hash(folder)==v['artifact_hash'] and not draft.exists():
                        with connection(self.db) as conn:
                            conn.execute('UPDATE versions SET status="archived" WHERE skill_name=? AND status="active"', (v['skill_name'],))
                            conn.execute('UPDATE versions SET status="active",activated_at=? WHERE version_id=?',(time.time(),v['version_id']))
                            conn.execute('UPDATE operations SET status="completed",completed_at=? WHERE target_version_id=? AND completed_at IS NULL',
                                         (time.time(),v['version_id']))
                    elif not folder.exists() and old.exists() and draft.exists():
                        old.rename(folder)
                        continue
                    else:
                        continue
                if old.exists():
                    prior = self.version(v['previous_version_id'])
                    if not prior['archive_name']:
                        raise RuntimeError('Recovery requires a verified previous archive')
                    h,size = self._verify_zip(self.root/prior['archive_name'],prior['skill_name'])
                    if (h,size)!=(prior['archive_hash'],prior['archive_size']):
                        raise RuntimeError('Recovery archive has changed; old files retained')
                    self._remove(old)

    @contextmanager
    def locked(self):
        with self._mutex:
            lock = InstanceLock(self.root/'hand.lock')
            try:
                lock.acquire()
                yield
            finally:
                lock.release()

    def generate(self, task, guidelines, hypothesis=None):
        if not self.llm:
            raise RuntimeError('Hand requiere un proveedor LLM para generar código')
        prompt = f'''Escribe Python autónomo para el objetivo: {task['request']}
Devuelve SOLO un bloque de código Python. Primera línea: # MODE: SAFE (script corto),
# MODE: AGENT (servicio) o # MODE: GOD (DynamicSkill derivada de BaseSkill).
Segunda línea opcional: # PIP: paquete==version. No uses shell ni instrucciones Docker.
El entorno está aislado en contenedor; no uses subprocess, ctypes, multiprocessing ni librerías de escape del sistema.
Si el manifiesto declara la capacidad 'Network', puedes usar urllib.request, requests o httpx para consultar APIs públicas externas directamente.
El script debe manejar posibles errores e imprimir el resultado claro y formateado en stdout, o escribir en /app/data/public.
NO uses emoticones ni emojis en las salidas impresas ni en el código.
Los servicios AGENT escuchan en 8080 dentro del contenedor; no se publican puertos.
GOD permite la API BaseSkill desde modules.skill_forge; tampoco da permisos al host.
No uses BaseSkill en SAFE. No inventes aprobaciones o resultados verificados.
Manifiesto: {json.dumps(task.get('manifest', {}), ensure_ascii=False)}
Reglas aplicables: {json.dumps(guidelines, ensure_ascii=False)}
Hipótesis a probar: {hypothesis or ''}
Resultado/rechazo anterior: {str(task.get('feedback', ''))[:8000]}
Código anterior: {str(task.get('code', ''))[:24000]}
'''
        code = self.llm.generate_content(prompt)
        if '```python' in code:
            code = code.split('```python', 1)[1].split('```', 1)[0]
        elif '```' in code:
            code = code.split('```', 1)[1].split('```', 1)[0]
        code = code.strip()
        ast.parse(code)
        mode, deps = skill_metadata(code)
        return code, mode, deps

    def snapshot(self, code):
        root = self.root.parent / 'artifacts'
        root.mkdir(parents=True, exist_ok=True)
        raw = code.encode('utf-8')
        sha_val = digest(raw)
        path = root / (sha_val + '.py')
        try:
            with path.open('xb') as stream:
                stream.write(raw)
        except FileExistsError:
            if digest(path.read_bytes()) != sha_val:
                raise ValueError('Artefacto existente alterado')
        return str(path.resolve()), sha_val

    def _name(self, name):
        if not isinstance(name,str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}',name):
            raise ValueError('Invalid skill name')
        if name.lower() in {'con','prn','aux','nul','heart'} or re.fullmatch('(com|lpt)[0-9]',name):
            raise ValueError('Reserved skill name')
        return name

    def version(self, version_id):
        with connection(self.db) as conn:
            row = conn.execute('SELECT * FROM versions WHERE version_id=?',(version_id,)).fetchone()
        if not row:
            raise ValueError('Unknown skill version')
        return dict(row)

    def active(self, name):
        with connection(self.db) as conn:
            row = conn.execute('SELECT * FROM versions WHERE skill_name=? AND status="active"',(self._name(name),)).fetchone()
        return dict(row) if row else None

    def _operation(self, kind, name, source=None, target=None, correlation_id=None, reason=''):
        op = uuid.uuid4().hex
        with connection(self.db) as conn:
            conn.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (op,kind,name,source,target,'pending',correlation_id,reason,time.time(),None,None))
        return op

    def _finish(self, op, error=None):
        with connection(self.db) as conn:
            conn.execute('UPDATE operations SET status=?,completed_at=?,error=? WHERE operation_id=?',
                         ('failed' if error else 'completed',time.time(),error,op))

    def _files(self, folder):
        if folder.is_symlink() or {p.name for p in folder.iterdir()} != {'app','data','other'}:
            raise ValueError('A skill must contain exactly app/, data/ and other/')
        files = []
        for p in sorted(folder.rglob('*')):
            if p.is_symlink() or not p.resolve().is_relative_to(folder.resolve()):
                raise ValueError('Links are forbidden in skills')
            if p.is_file():
                if p.name.lower() in {'.env','secrets.env','memory.sqlite3','rules.sqlite3','logs.sqlite3','openclaw.db'} or p.suffix.lower() == '.log':
                    raise ValueError('Secrets, databases and logs cannot enter skill archives')
                if p.stat().st_size > 64*1024*1024:
                    raise ValueError('Skill file exceeds size limit')
                files.append(p)
        if sum(p.stat().st_size for p in files) > 256*1024*1024:
            raise ValueError('Skill exceeds archive size limit')
        return files

    def tree_hash(self, folder):
        return sha(json.dumps([(p.relative_to(folder).as_posix(),sha(p.read_bytes()))
                               for p in self._files(folder)],separators=(',',':')).encode())

    def create(self, name, code, manifest, *, correlation_id=None, restored_from=None, version_number=None):
        name = self._name(name)
        with self.locked():
            draft = self.root/('.'+name)
            if draft.exists():
                raise ValueError('An unfinished draft already exists')
            current = self.active(name)
            with connection(self.db) as conn:
                count = conn.execute('SELECT COUNT(*) FROM versions WHERE skill_name=?',(name,)).fetchone()[0]
            vid = uuid.uuid4().hex
            number = version_number or 'v'+str(count+1)
            op = self._operation('update' if current else 'create',name,current and current['version_id'],vid,correlation_id)
            try:
                draft.mkdir()
                for part in ('app','data','other'):
                    (draft/part).mkdir()
                (draft/'app'/'main.py').write_bytes(code.encode('utf-8'))
                with connection(self.db) as conn:
                    conn.execute('''INSERT INTO versions(version_id,skill_name,version_number,status,artifact_hash,
                        created_at,previous_version_id,restored_from_version_id,manifest) VALUES (?,?,?,?,?,?,?,?,?)''',
                        (vid,name,number,'draft',self.tree_hash(draft),time.time(),current and current['version_id'],
                         restored_from,json.dumps(manifest)))
                self._finish(op)
            except Exception as exc:
                self._finish(op,type(exc).__name__)
                raise
            return self.version(vid)

    def edit(self, vid, code):
        with self.locked():
            v = self.version(vid)
            if v['status'] != 'draft':
                raise ValueError('Only drafts can be edited')
            op = self._operation('edit',v['skill_name'],vid,vid)
            folder = self.root/('.'+v['skill_name'])
            (folder/'app'/'main.py').write_bytes(code.encode('utf-8'))
            with connection(self.db) as conn:
                conn.execute('UPDATE versions SET artifact_hash=? WHERE version_id=?',(self.tree_hash(folder),vid))
            self._finish(op)

    def update(self, name, code, manifest, **kwargs):
        if not self.active(name):
            raise ValueError('Update requires an active skill')
        return self.create(name,code,manifest,**kwargs)

    def reject(self, vid, reason='Rejected'):
        with self.locked():
            v = self.version(vid)
            if v['status'] not in {'draft','approved'}:
                raise ValueError('Only pending versions can be rejected')
            op = self._operation('reject',v['skill_name'],vid,reason=reason)
            folder = self.root/('.'+v['skill_name'])
            # Keep rejected material outside the active skill name for inspection.
            if folder.exists():
                folder.rename(self.root/('.rejected-'+vid))
            with connection(self.db) as conn:
                conn.execute('UPDATE versions SET status="rejected" WHERE version_id=?',(vid,))
            self._finish(op)

    def _verify_zip(self, path, name):
        if path.is_symlink() or path.stat().st_size>256*1024*1024:
            raise ValueError('Unsafe or oversized archive')
        with zipfile.ZipFile(path) as z:
            if len(z.infolist())>10000:
                raise ValueError('Too many archive entries')
            names = set()
            total = 0
            for item in z.infolist():
                p = PurePosixPath(item.filename)
                if ('\\' in item.filename or ':' in item.filename or p.is_absolute() or '..' in p.parts
                        or len(p.parts)<2 or p.parts[0] != name or p.parts[1] not in {'app','data','other'}
                        or (item.external_attr >> 16) & 0o170000 == 0o120000 or item.filename in names):
                    raise ValueError('Invalid archive structure')
                names.add(item.filename)
                total += item.file_size
                if total > 256*1024*1024:
                    raise ValueError('Archive too large')
            if name+'/app/main.py' not in names:
                raise ValueError('Archive has no entry point')
            if z.testzip() is not None:
                raise ValueError('Corrupt archive')
        return sha(path.read_bytes()),path.stat().st_size

    def _archive(self, v):
        folder = self.root/v['skill_name']
        if self.tree_hash(folder) != v['artifact_hash']:
            raise ValueError('Active skill changed outside Hand')
        if v['archive_name']:
            path = self.root/v['archive_name']
            h,size = self._verify_zip(path,v['skill_name'])
            if h != v['archive_hash'] or size != v['archive_size']:
                raise ValueError('Immutable archive has changed')
            return
        n = 1
        while (self.root/f"{v['skill_name']}{n:03d}.zip").exists():
            n += 1
        if n>999:
            raise ValueError('Three-digit archive namespace exhausted; no files removed')
        path = self.root/f"{v['skill_name']}{n:03d}.zip"
        with zipfile.ZipFile(path,'x',zipfile.ZIP_DEFLATED) as z:
            for part in ('app','data','other'):
                z.writestr(v['skill_name']+'/'+part+'/',b'')
            for p in self._files(folder):
                z.write(p,v['skill_name']+'/'+p.relative_to(folder).as_posix())
        h,size = self._verify_zip(path,v['skill_name'])
        with connection(self.db) as conn:
            conn.execute('UPDATE versions SET archive_name=?,archive_hash=?,archive_size=?,archived_at=? WHERE version_id=?',
                         (path.name,h,size,time.time(),v['version_id']))
        op = self._operation('archive',v['skill_name'],v['version_id'])
        self._finish(op)

    def approve(self, vid, *, user_approved=False, correlation_id=None):
        with self.locked():
            v = self.version(vid)
            if v['status'] == 'active':
                return v
            if v['status'] not in {'draft','approved'}:
                raise ValueError('Version cannot be activated')
            name = v['skill_name']
            draft, active = self.root/('.'+name),self.root/name
            if self.tree_hash(draft) != v['artifact_hash']:
                raise ValueError('Draft changed after review')
            audit = self.pixy.audit_artifact(draft,json.loads(v['manifest']),v['artifact_hash'])
            if not audit['safe']:
                raise PermissionError(audit.get('reason','Artifact not safe'))
            if audit.get('requires_approval') and not user_approved:
                from app.common.tracing import get_trace_id
                trace = correlation_id or get_trace_id()
                if trace and hasattr(self.pixy, 'check_authorization') and self.pixy.check_authorization(trace):
                    self.pixy.consume_authorization(trace, consumer='hand:' + vid)
                    user_approved = True
                else:
                    raise PermissionError(audit.get('reason','Approval required'))
            current = self.active(name)
            if active.exists() and not current:
                raise ValueError('Unregistered active directory')
            op = self._operation('approve',name,current and current['version_id'],vid,correlation_id)
            old = self.root/('.previous-'+vid)
            try:
                if current:
                    self._archive(current)
                with connection(self.db) as conn:
                    conn.execute('UPDATE versions SET status="approved",pixy_audit_id=? WHERE version_id=?',
                                 (audit['audit_id'],vid))
                if current:
                    active.rename(old)
                try:
                    draft.rename(active)
                except Exception:
                    if old.exists():
                        old.rename(active)
                    raise
                with connection(self.db) as conn:
                    if current:
                        conn.execute('UPDATE versions SET status="archived" WHERE version_id=?',(current['version_id'],))
                    conn.execute('UPDATE versions SET status="active",activated_at=? WHERE version_id=?',(time.time(),vid))
                if old.exists():
                    self._remove(old)
                self._finish(op)
                if self.events:
                    self.events.emit('hand','activated',skill_name=name,version_id=vid,correlation_id=correlation_id)
                return self.version(vid)
            except Exception as exc:
                self._finish(op,type(exc).__name__)
                raise

    def _remove(self, folder):
        # All deletions are confined to a direct child of the owned root.
        if folder.is_symlink() or folder.resolve().parent != self.root or not folder.is_dir():
            raise ValueError('Unsafe skill deletion')
        shutil.rmtree(folder)

    def archive(self, name):
        with self.locked():
            v = self.active(name)
            if not v:
                raise ValueError('No active skill')
            self._archive(v)
            return self.version(v['version_id'])

    def delete(self, name):
        with self.locked():
            v = self.active(name)
            if not v:
                raise ValueError('No active skill')
            op = self._operation('delete',name,v['version_id'])
            self._archive(v)
            self._remove(self.root/name)
            with connection(self.db) as conn:
                conn.execute('UPDATE versions SET status="archived" WHERE version_id=?',(v['version_id'],))
            self._finish(op)

    def restore(self, vid, *, user_approved=False, rollback=False):
        v = self.version(vid)
        if not v['archive_name']:
            raise ValueError('Version has no archive')
        path = self.root/v['archive_name']
        h,size = self._verify_zip(path,v['skill_name'])
        if (h,size) != (v['archive_hash'],v['archive_size']):
            raise ValueError('Immutable archive changed')
        current = self.active(v['skill_name'])
        number = None
        if rollback and current:
            base = current['version_number'].split('.')[0]
            with connection(self.db) as conn:
                used = {r[0] for r in conn.execute('SELECT version_number FROM versions WHERE skill_name=?',(v['skill_name'],))}
            n = 1
            while base+'.'+str(n) in used:
                n += 1
            number = base+'.'+str(n)
        with zipfile.ZipFile(path) as z:
            code = z.read(v['skill_name']+'/app/main.py').decode('utf-8')
            draft = self.create(v['skill_name'],code,json.loads(v['manifest']),restored_from=vid,version_number=number)
            folder = self.root/('.'+v['skill_name'])
            with self.locked():
                for item in z.infolist():
                    relative = PurePosixPath(item.filename).relative_to(v['skill_name'])
                    dest = folder/str(relative)
                    if item.is_dir():
                        dest.mkdir(parents=True,exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True,exist_ok=True)
                        dest.write_bytes(z.read(item))
                if self.tree_hash(folder) != v['artifact_hash']:
                    raise ValueError('Archive content hash mismatch')
                with connection(self.db) as conn:
                    conn.execute('UPDATE versions SET artifact_hash=? WHERE version_id=?',(v['artifact_hash'],draft['version_id']))
        op = self._operation('rollback' if rollback else 'restore',v['skill_name'],vid,draft['version_id'])
        try:
            result = self.approve(draft['version_id'],user_approved=user_approved)
            self._finish(op)
            return result
        except Exception as exc:
            self._finish(op,type(exc).__name__)
            raise

    def rollback(self, vid, **kwargs):
        return self.restore(vid,rollback=True,**kwargs)

    def use(self, name, correlation_id=None):
        v = self.active(name)
        if not v or self.tree_hash(self.root/name) != v['artifact_hash']:
            raise ValueError('No intact active version')
        op = self._operation('use',name,v['version_id'],correlation_id=correlation_id)
        self._finish(op)
        return v

    def prepare_execution(self, vid, eid, code_hash):
        if not re.fullmatch('run_[a-f0-9]{32}',eid):
            raise ValueError('Invalid execution ID')
        with self.locked():
            v = self.version(vid)
            if v['status']!='active':
                raise ValueError('Execution requires an active skill')
            source = self.root/v['skill_name']
            if self.tree_hash(source)!=v['artifact_hash'] or sha((source/'app'/'main.py').read_bytes())!=code_hash:
                raise ValueError('Execution artifact differs from approved skill')
            target = self.root.parent/'execution_inputs'/eid/'skill'
            if target.exists():
                if self.tree_hash(target)!=v['artifact_hash']:
                    raise ValueError('Execution snapshot changed')
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copytree(source,target,symlinks=False)
                if self.tree_hash(target)!=v['artifact_hash']:
                    raise ValueError('Skill changed during snapshot')
            return str(target)

    def publication(self, name, asset_id, published=True):
        v = self.active(name)
        if not v:
            raise ValueError('Unknown active skill')
        op = self._operation('publish' if published else 'unpublish',name,v['version_id'],reason=asset_id)
        self._finish(op)

    def publish(self, name, asset_id):
        self.publication(name,asset_id,True)

    def unpublish(self, name, asset_id):
        self.publication(name,asset_id,False)

    # ------------------------------------------------------------------ recovery
    def _recovery_order(self, order):
        """Validate a Heart order. The header carries the verifiable Pixy authorization reference."""
        if not isinstance(order,dict) or set(order) != {'header','plan'}:
            raise ValueError('Orden de recuperación inválida')
        header = order['header']
        if not isinstance(header,dict) or set(header) != {'case_id','issued_by','authorization_id','nonce','plan_hash'}:
            raise ValueError('Encabezado de recuperación inválido')
        if header['issued_by'] != 'heart':
            raise PermissionError('Solo Heart emite órdenes de recuperación')
        case_id = validate_case_id(header['case_id'])
        core = validate_plan(order['plan'])
        if set(order['plan']) != {'target','steps','capabilities'}:
            raise ValueError('La orden contiene campos fuera del contrato')
        h = plan_hash(case_id,core)
        if h != header['plan_hash']:
            raise PermissionError('El plan no coincide con el hash autorizado')
        return header,case_id,core,h

    def _authorization_terms(self, header, case_id, core, h):
        return (header['authorization_id'],header['nonce'],case_id,core['capabilities'],core['target'],h)

    def _recovery_row(self, case_id):
        with connection(self.db) as conn:
            row = conn.execute('SELECT * FROM recovery_skills WHERE case_id=?',(case_id,)).fetchone()
        return dict(row) if row else None

    def _recovery_document(self, case_id, core, h):
        return canonical({'case_id':case_id,'plan':core,'plan_hash':h}).encode('utf-8')

    def _verify_recovery_skill(self, row, case_id, core, h):
        folder = self.recovery_root/case_id
        if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != self.recovery_root.resolve():
            raise PermissionError('Skill de recuperación ausente o fuera de data/hand/heart')
        if self.tree_hash(folder) != row['skill_hash']:
            raise PermissionError('La skill de recuperación cambió fuera de Hand')
        if (folder/'app'/'recovery.json').read_bytes() != self._recovery_document(case_id,core,h):
            raise PermissionError('La skill de recuperación no coincide con la orden autorizada')
        return folder

    def create_recovery(self, order):
        header,case_id,core,h = self._recovery_order(order)
        authorizations = getattr(self.pixy,'authorizations',None)
        if authorizations is None:
            raise PermissionError('Pixy no tiene un registro de autorizaciones')
        with self.locked():
            row = self._recovery_row(case_id)
            if row:
                if row['plan_hash'] != h or row['authorization_id'] != header['authorization_id']:
                    raise PermissionError('Ya existe otra skill de recuperación para este caso')
                self._verify_recovery_skill(row,case_id,core,h)
                return {'case_id':case_id,'path':'hand/heart/'+case_id,'skill_hash':row['skill_hash'],'status':row['status']}
            authorizations.verify(*self._authorization_terms(header,case_id,core,h))
            self.recovery_root.mkdir(exist_ok=True)
            folder, draft = self.recovery_root/case_id, self.recovery_root/('.'+case_id)
            if folder.exists():
                raise ValueError('Carpeta de recuperación no registrada; se conserva para inspección')
            if draft.exists():
                if draft.is_symlink() or draft.resolve().parent != self.recovery_root.resolve():
                    raise ValueError('Borrador de recuperación inseguro')
                shutil.rmtree(draft)  # Unregistered draft from a crash; never executed.
            op = self._operation('recovery_create','heart/'+case_id,correlation_id=case_id,reason=header['authorization_id'])
            try:
                for part in ('app','data','other'):
                    (draft/part).mkdir(parents=True)
                (draft/'app'/'recovery.json').write_bytes(self._recovery_document(case_id,core,h))
                (draft/'other'/'manifest.json').write_bytes(canonical({
                    'purpose':'recovery','case_id':case_id,'issued_by':'heart','created_by':'hand',
                    'authorization_id':header['authorization_id'],'capabilities':core['capabilities'],
                    'target':core['target']}).encode('utf-8'))
                skill_hash = self.tree_hash(draft)
                draft.rename(folder)
                now = time.time()
                with connection(self.db) as conn:
                    conn.execute('INSERT INTO recovery_skills VALUES (?,?,?,?,?,?,?)',
                                 (case_id,header['authorization_id'],h,skill_hash,'created',now,now))
                self._finish(op)
            except Exception as exc:
                self._finish(op,type(exc).__name__)
                raise
        if self.events:
            self.events.emit('hand','recovery_created',correlation_id=case_id,metadata={'skill_hash':skill_hash})
        return {'case_id':case_id,'path':'hand/heart/'+case_id,'skill_hash':skill_hash,'status':'created'}

    def _set_recovery(self, case_id, status):
        with connection(self.db) as conn:
            conn.execute('UPDATE recovery_skills SET status=?,updated_at=? WHERE case_id=?',(status,time.time(),case_id))

    def _step(self, case_id, index):
        with connection(self.db) as conn:
            row = conn.execute('SELECT * FROM recovery_steps WHERE case_id=? AND step_index=?',(case_id,index)).fetchone()
        if not row:
            return None
        step = dict(row)
        step['receipt'] = json.loads(step['receipt']) if step['receipt'] else None
        return step

    def _save_step(self, case_id, index, status, receipt):
        with connection(self.db) as conn:
            conn.execute('UPDATE recovery_steps SET status=?,receipt=?,updated_at=? WHERE case_id=? AND step_index=?',
                         (status,json.dumps(receipt,ensure_ascii=False),time.time(),case_id,index))
        return self._step(case_id,index)

    def recovery_summary(self, case_id):
        row = self._recovery_row(case_id)
        with connection(self.db) as conn:
            steps = [dict(r) for r in conn.execute('SELECT * FROM recovery_steps WHERE case_id=? ORDER BY step_index',(case_id,))]
        for step in steps:
            step['receipt'] = json.loads(step['receipt']) if step['receipt'] else None
        return {'case_id':case_id,'status':row['status'] if row else 'missing','skill_hash':row and row['skill_hash'],
                'steps':[{'index':s['step_index'],'action':s['action'],'target':s['target'],'action_id':s['action_id'],
                          'status':s['status'],'receipt':s['receipt']} for s in steps]}

    def advance_recovery(self, order):
        """Idempotent: spend the authorization once, then submit/poll each step exactly once."""
        header,case_id,core,h = self._recovery_order(order)
        with self.locked():
            row = self._recovery_row(case_id)
            if not row:
                raise ValueError('Skill de recuperación no creada')
            if row['authorization_id'] != header['authorization_id'] or row['plan_hash'] != h:
                raise PermissionError('La orden no corresponde a la skill registrada')
            if row['status'] in {'completed','failed','unknown'}:
                return self.recovery_summary(case_id)
            self._verify_recovery_skill(row,case_id,core,h)
            if row['status'] in {'created','consuming'}:
                self._set_recovery(case_id,'consuming')
                self.pixy.authorizations.consume(*self._authorization_terms(header,case_id,core,h),consumer='hand:'+case_id)
                self._set_recovery(case_id,'executing')
        for index,spec in enumerate(core['steps']):
            step = self._step(case_id,index)
            if step is None:
                action_id = 'rec_'+uuid.uuid4().hex
                now = time.time()
                # Receipt before side effect: a crash resubmits the SAME id, which the executor deduplicates.
                with connection(self.db) as conn:
                    conn.execute('INSERT INTO recovery_steps VALUES (?,?,?,?,?,?,?,?,?)',
                                 (case_id,index,spec['action'],spec['target'],action_id,'submitting',None,now,now))
                step = self._step(case_id,index)
                if self.recovery_executor is None:
                    step = self._save_step(case_id,index,'failed',{'error':'No hay canal de recuperación configurado'})
            if step['status'] not in FINAL_STEP:
                from app.voice.runtime.recovery_runtime import RecoveryRejected
                try:
                    if step['status'] == 'submitting':
                        receipt = self.recovery_executor.submit(step['action_id'],spec['action'],spec['target'])
                    else:
                        receipt = self.recovery_executor.status(step['action_id'])
                    status = receipt.get('status')
                    if status not in FINAL_STEP:
                        status = 'running'
                    step = self._save_step(case_id,index,status,receipt)
                except RecoveryRejected as exc:
                    step = self._save_step(case_id,index,'failed',{'error':str(exc)[:300]})
                except OSError:
                    if time.time()-step['submitted_at'] > RECOVERY_STEP_TIMEOUT:
                        step = self._save_step(case_id,index,'unknown',{'error':'Canal sin respuesta; no se repite'})
            if step['status'] not in FINAL_STEP:
                return self.recovery_summary(case_id) | {'status':'running'}
            if step['status'] != 'completed':
                break  # Later steps never run after a failed or uncertain one.
        statuses = [s['status'] for s in self.recovery_summary(case_id)['steps']]
        final = ('completed' if len(statuses)==len(core['steps']) and set(statuses)=={'completed'}
                 else 'unknown' if 'unknown' in statuses else 'failed')
        self._set_recovery(case_id,final)
        op = self._operation('recovery_execute','heart/'+case_id,correlation_id=case_id,reason=final)
        self._finish(op,None if final=='completed' else final)
        if self.events:
            self.events.emit('hand','recovery_finished',correlation_id=case_id,metadata={'status':final})
        return self.recovery_summary(case_id)
