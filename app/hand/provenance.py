"""Deployment-owned skill origins. Model metadata cannot grant protection or trust."""
import json
from pathlib import Path
from app.kernel.database import connection, migrate

MANIFEST = Path(__file__).resolve().parents[2] / 'config' / 'primitive_skills.json'


def primitives():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def assert_mutable(name):
    if name in primitives():
        raise PermissionError('Primitive skill is deployment-owned; create a separately named derivative')


def initialize(db):
    migrate(db, [[
        '''CREATE TABLE skill_origins(version_id TEXT PRIMARY KEY REFERENCES versions(version_id),
           kind TEXT NOT NULL, source_version TEXT, parent_version_id TEXT,
           origin_hash TEXT NOT NULL)''',
    ]], component='skill-origins')
    with connection(db) as conn:
        conn.execute('''INSERT OR IGNORE INTO skill_origins(version_id,kind,origin_hash)
                        SELECT version_id,'legacy',artifact_hash FROM versions''')
        for name, entry in primitives().items():
            for digest in entry['artifact_hashes']:
                conn.execute('''UPDATE skill_origins SET kind='primitive',source_version=?
                    WHERE version_id IN (SELECT version_id FROM versions WHERE skill_name=? AND artifact_hash=?)''',
                    (entry['source_version'], name, digest))


def register_generated(db, version_id, artifact_hash, parent=None):
    with connection(db) as conn:
        if parent and not conn.execute('SELECT 1 FROM versions WHERE version_id=?', (parent,)).fetchone():
            raise ValueError('Derivative base version does not exist')
        conn.execute('INSERT INTO skill_origins VALUES (?,?,?,?,?)',
                     (version_id, 'generated', None, parent, artifact_hash))
