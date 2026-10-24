"""Skill lifecycle, immutable archives and recoverable activation."""
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
from app.pixy.control import InstanceLock


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class Hand:
    def __init__(self, root, pixy, events=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root/'hand.sqlite3'
        self.pixy, self.events = pixy, events
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

    def _name(self, name):
        if not isinstance(name,str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}',name):
            raise ValueError('Invalid skill name')
        if name.lower() in {'con','prn','aux','nul'} or re.fullmatch('(com|lpt)[0-9]',name):
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
            if not audit['safe'] or (audit.get('requires_approval') and not user_approved):
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
