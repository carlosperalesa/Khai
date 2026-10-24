import hashlib
import json
import mimetypes
import re
import time
import uuid
from pathlib import Path
from app.common.database import migrate, connection
from app.common.files import read_regular


class AssetGateway:
    def __init__(self, root, db_path, pixy, events, hand=None):
        self.root = Path(root).resolve()
        self.db, self.pixy, self.events, self.hand = db_path,pixy,events,hand
        for part in ('gallery','exports','quarantine'):
            (self.root/part).mkdir(parents=True,exist_ok=True)
        migrate(db_path, [[
            '''CREATE TABLE IF NOT EXISTS assets(asset_id TEXT PRIMARY KEY, filename TEXT, mime TEXT,
               size INTEGER, hash TEXT, status TEXT, owner TEXT, expires_at REAL, skill_name TEXT, audit_id TEXT)''',
        ],[
            'CREATE TABLE IF NOT EXISTS voice_commands(source TEXT PRIMARY KEY,status TEXT,reply TEXT)',
        ]])
        self.recover()

    def recover(self):
        with connection(self.db) as conn:
            rows = [dict(r) for r in conn.execute('SELECT * FROM assets WHERE status="approved"')]
        for row in rows:
            aid = row['asset_id']
            if (self.root/'gallery'/aid).exists():
                self._file(row,'gallery')
            else:
                self._file(row,'quarantine')
                (self.root/'quarantine'/aid).rename(self.root/'gallery'/aid)
            with connection(self.db) as conn:
                conn.execute('UPDATE assets SET status="published" WHERE asset_id=?',(aid,))

    def quarantine(self, raw, filename, owner, *, mime=None, ttl=None, skill_name=None):
        if not owner or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,100}',filename):
            raise ValueError('Invalid asset metadata')
        if len(raw)>64*1024*1024 or Path(filename).suffix.lower() in {'.db','.sqlite3','.env','.py','.yaml','.yml','.zip'}:
            raise ValueError('Forbidden asset type or size')
        mime = mime or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        if mime.startswith('image/'):
            valid = (mime=='image/png' and raw.startswith(b'\x89PNG\r\n\x1a\n')) or (mime=='image/jpeg' and raw.startswith(b'\xff\xd8\xff'))
            if not valid: raise ValueError('Image signature does not match MIME')
        elif mime=='application/pdf' and not raw.startswith(b'%PDF-'):
            raise ValueError('Invalid PDF signature')
        elif mime in {'text/plain','text/csv','application/json'}:
            text = raw.decode('utf-8')
            if '\x00' in text: raise ValueError('Binary content disguised as text')
            if mime=='application/json': json.loads(text)
        aid = uuid.uuid4().hex
        folder = self.root/'quarantine'/aid
        folder.mkdir()
        (folder/filename).write_bytes(raw)
        with connection(self.db) as conn:
            conn.execute('INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?,?)',
                (aid,filename,mime,len(raw),hashlib.sha256(raw).hexdigest(),'quarantine',str(owner),
                 time.time()+ttl if ttl else None,skill_name,None))
        self.events.emit('voice','asset_quarantined',metadata={'asset_id':aid,'size':len(raw)})
        return aid

    def _row(self, aid, owner):
        if not re.fullmatch('[a-f0-9]{32}',aid):
            raise ValueError('Invalid asset ID')
        with connection(self.db) as conn:
            row = conn.execute('SELECT * FROM assets WHERE asset_id=?',(aid,)).fetchone()
        if not row or row['owner']!=str(owner):
            raise PermissionError('Asset unavailable')
        if row['expires_at'] and row['expires_at']<=time.time():
            raise PermissionError('Asset expired')
        return dict(row)

    def _file(self, row, area):
        path = self.root/area/row['asset_id']/row['filename']
        if path.is_symlink() or not path.resolve().is_relative_to(self.root/area) or not path.is_file():
            raise PermissionError('Unsafe asset path')
        if path.stat().st_size!=row['size'] or hashlib.sha256(read_regular(path,64*1024*1024)).hexdigest()!=row['hash']:
            raise ValueError('Asset was modified')
        return path

    def publish(self, aid, owner, approved=False):
        row = self._row(aid,owner)
        if row['status']=='published':
            return aid
        if row['status']!='quarantine':
            raise ValueError('Asset cannot be published')
        self._file(row,'quarantine')
        audit = self.pixy.authorize_publication(row,approved)
        if not audit['safe']:
            raise PermissionError(audit['reason'])
        with connection(self.db) as conn:
            conn.execute('UPDATE assets SET status="approved",audit_id=? WHERE asset_id=?',(audit['audit_id'],aid))
        (self.root/'quarantine'/aid).rename(self.root/'gallery'/aid)
        with connection(self.db) as conn:
            conn.execute('UPDATE assets SET status="published",audit_id=? WHERE asset_id=?',(audit['audit_id'],aid))
        self.events.emit('voice','asset_published',metadata={'asset_id':aid,'owner':str(owner)})
        if self.hand and row['skill_name']:
            self.hand.publication(row['skill_name'],aid)
        return aid

    def deliver(self, aid, owner, channel):
        row = self._row(aid,owner)
        if row['status']!='published':
            raise PermissionError('Asset is not published')
        path = self._file(row,'gallery')
        result = channel.send_document(path)
        self.events.emit('voice','asset_delivered',metadata={'asset_id':aid,'owner':str(owner)})
        return result

    def unpublish(self, aid, owner):
        row = self._row(aid,owner)
        if row['status']!='published':
            raise ValueError('Asset is not published')
        with connection(self.db) as conn:
            conn.execute('UPDATE assets SET status="unpublished" WHERE asset_id=?',(aid,))
        if self.hand and row['skill_name']:
            self.hand.publication(row['skill_name'],aid,False)
        self.events.emit('voice','asset_unpublished',metadata={'asset_id':aid})

    def download(self, gateway, service, operation, params, filename, owner):
        result = gateway.request(service,operation,params)
        return self.quarantine(result['body'],filename,owner,mime=result['content_type'])

    def import_result(self, task, workspace, relative, owner):
        if task.get('state') not in {'COMPLETED','UNVERIFIED'} or not task.get('execution_id'):
            raise PermissionError('No completed execution artifact')
        root = Path(workspace).resolve()
        relative = Path(relative)
        if relative.is_absolute() or len(relative.parts)!=1 or '..' in relative.parts or not relative.name or relative.name.startswith('.'):
            raise ValueError('Invalid result path')
        path = root/relative
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise PermissionError('Result outside the execution workspace')
        if path.stat().st_size>64*1024*1024:
            raise ValueError('Result too large')
        return self.quarantine(read_regular(path,64*1024*1024),path.name,owner,skill_name=task.get('skill_name'))
