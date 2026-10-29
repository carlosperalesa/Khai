"""User-approved guidance cannot modify the structural policy."""
import hashlib
import json
import time
import uuid
from app.kernel.database import migrate, connection


class Rules:
    def __init__(self, path):
        self.path = path
        migrate(path, [[
            '''CREATE TABLE IF NOT EXISTS rules(rule_id TEXT PRIMARY KEY, rule_type TEXT, content TEXT,
               source TEXT, version INTEGER, status TEXT, created_at REAL, approved_by TEXT,
               approved_at REAL, replaced_rule_id TEXT, rollback_of TEXT, content_hash TEXT)''',
            'CREATE TABLE IF NOT EXISTS rule_commands(source TEXT PRIMARY KEY, rule_id TEXT)',
        ]], component='pixy-rules')

    def propose(self, content, source, rule_type='guidance', conn=None):
        if rule_type != 'guidance' or not isinstance(content,str) or not 1 <= len(content) <= 8000:
            raise ValueError('Only bounded advisory guidance can be proposed')
        rid = uuid.uuid4().hex
        now = time.time()
        chash = hashlib.sha256(content.encode()).hexdigest()
        if conn is not None:
            conn.execute('INSERT INTO rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (rid, rule_type, content, source, 1, 'proposed', now, None, None, None, None, chash))
        else:
            with connection(self.path) as c:
                c.execute('INSERT INTO rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (rid, rule_type, content, source, 1, 'proposed', now, None, None, None, None, chash))
        return rid

    def approve(self, rid, owner, replaces=None):
        if not owner:
            raise PermissionError('Explicit owner approval required')
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM rule_commands WHERE source=?',(owner,)).fetchone():
                return
            row = conn.execute('SELECT * FROM rules WHERE rule_id=?',(rid,)).fetchone()
            if not row or row['status'] != 'proposed' or row['rule_type'] != 'guidance':
                raise ValueError('Rule is not a valid proposal')
            if hashlib.sha256(row['content'].encode()).hexdigest() != row['content_hash']:
                raise ValueError('Rule changed after proposal')
            version = 1
            if replaces:
                prior = conn.execute('SELECT * FROM rules WHERE rule_id=? AND status="active"',(replaces,)).fetchone()
                if not prior:
                    raise ValueError('Replaced rule is not active')
                version = prior['version']+1
                conn.execute('UPDATE rules SET status="replaced" WHERE rule_id=?',(replaces,))
            conn.execute('UPDATE rules SET status="active",approved_by=?,approved_at=?,replaced_rule_id=?,version=? WHERE rule_id=?',
                         (owner,time.time(),replaces,version,rid))
            conn.execute('INSERT INTO rule_commands VALUES (?,?)',(owner,rid))

    def rollback(self, rid, owner):
        if not owner:
            raise PermissionError('Explicit owner approval required')
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM rules WHERE rule_id=? AND status="active"', (rid,)).fetchone()
            if not row:
                raise ValueError('Rule is not active')
            now = time.time()
            if row['replaced_rule_id']:
                previous = conn.execute('SELECT * FROM rules WHERE rule_id=?', (row['replaced_rule_id'],)).fetchone()
                if not previous:
                    raise ValueError('Prior rule version not found')
                new_id = uuid.uuid4().hex
                content = previous['content']
                chash = hashlib.sha256(content.encode()).hexdigest()
                version = row['version'] + 1
                conn.execute('UPDATE rules SET status="replaced" WHERE rule_id=?', (rid,))
                conn.execute(
                    'INSERT INTO rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (new_id, 'guidance', content, owner, version, 'active',
                     now, owner, now, rid, rid, chash)
                )
                conn.execute('INSERT OR REPLACE INTO rule_commands VALUES (?,?)', (owner, new_id))
                return new_id
            else:
                conn.execute('UPDATE rules SET status="rolled_back" WHERE rule_id=?', (rid,))
                conn.execute('INSERT OR REPLACE INTO rule_commands VALUES (?,?)', (owner, rid))
                return rid

    def get(self, rid):
        with connection(self.path) as conn:
            row = conn.execute('SELECT * FROM rules WHERE rule_id=?', (rid,)).fetchone()
            return dict(row) if row else None

    def deprecate(self, rid, reason='deprecated'):
        with connection(self.path) as conn:
            conn.execute('UPDATE rules SET status="deprecated" WHERE rule_id=?', (rid,))

    def active(self):
        with connection(self.path) as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM rules WHERE status="active"')]

    def all(self):
        with connection(self.path) as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM rules ORDER BY created_at DESC LIMIT 20')]
