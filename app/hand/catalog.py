"""Hand-owned versioned SkillSpec catalog in the common control store."""
import json
import time

from app.kernel.database import connection, migrate
from app.hand.skill_spec import spec_hash, validate_skill_spec


class SkillCatalog:
    def __init__(self, db_path):
        self.db_path = db_path
        migrate(db_path, [[
            '''CREATE TABLE IF NOT EXISTS skill_specs(
                 version_id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, spec_hash TEXT NOT NULL,
                 spec_json TEXT NOT NULL, artifact_hash TEXT NOT NULL, status TEXT NOT NULL,
                 created_at REAL NOT NULL, promoted_at REAL, verification_run_id TEXT,
                 FOREIGN KEY(version_id) REFERENCES versions(version_id))''',
            'CREATE INDEX IF NOT EXISTS skill_specs_active ON skill_specs(status,skill_id)',
        ]], component='hand-catalog')

    def stage(self, version_id, spec):
        validate_skill_spec(spec)
        digest = spec_hash(spec)
        with connection(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            version = conn.execute('SELECT skill_name,artifact_hash,status FROM versions WHERE version_id=?',
                                   (version_id,)).fetchone()
            if not version or version['skill_name'] != spec['skill_id'] or version['status'] != 'draft':
                raise ValueError('SkillSpec is not tied to this draft version')
            existing = conn.execute('SELECT spec_hash FROM skill_specs WHERE version_id=?',
                                    (version_id,)).fetchone()
            if existing:
                if existing['spec_hash'] != digest:
                    raise ValueError('SkillSpec changed after staging')
                return digest
            conn.execute('INSERT INTO skill_specs(version_id,skill_id,spec_hash,spec_json,artifact_hash,status,created_at) '
                         'VALUES (?,?,?,?,?,"candidate",?)',
                         (version_id, spec['skill_id'], digest, json.dumps(spec, ensure_ascii=False),
                          version['artifact_hash'], time.time()))
        return digest

    def promote(self, version_id, task_id, run_id, artifact_hash):
        if not isinstance(run_id, str) or not run_id.startswith('run_'):
            raise ValueError('Verified run ID required')
        with connection(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('''SELECT s.*,v.status AS version_status,v.artifact_hash AS current_hash
                                  FROM skill_specs s JOIN versions v USING(version_id)
                                  WHERE s.version_id=?''', (version_id,)).fetchone()
            if not row or row['artifact_hash'] != artifact_hash or row['current_hash'] != artifact_hash:
                raise ValueError('Candidate artifact mismatch')
            task_row = conn.execute('SELECT document FROM control_tasks WHERE id=?', (task_id,)).fetchone()
            task = json.loads(task_row['document']) if task_row else {}
            ordinary_run = (task.get('version_id') == version_id and
                            task.get('execution_id') == run_id and
                            task.get('skill_spec_hash') == row['spec_hash'])
            recipe_run = any(receipt.get('version_id') == version_id and
                             receipt.get('execution_id') == run_id and
                             receipt.get('skill_spec_hash') == row['spec_hash'] and
                             receipt.get('skill_artifact_hash') == artifact_hash and
                             receipt.get('status') == 'completed'
                             for receipt in task.get('recipe_receipts', []))
            if (not (ordinary_run or recipe_run)
                    or task.get('state') not in {'VERIFYING', 'COMPLETED'}
                    or (task.get('verification') or {}).get('success') is not True):
                raise ValueError('No matching verified objective for candidate')
            if row['status'] == 'active' and row['verification_run_id'] == run_id:
                return row['spec_hash']
            if row['status'] != 'candidate' or row['version_status'] != 'active':
                raise ValueError('Only an activated verified candidate can enter the catalog')
            conn.execute("UPDATE skill_specs SET status='superseded' WHERE skill_id=? AND status='active'",
                         (row['skill_id'],))
            conn.execute("UPDATE skill_specs SET status='active',promoted_at=?,verification_run_id=? "
                         'WHERE version_id=?', (time.time(), run_id, version_id))
        return row['spec_hash']

    def active(self):
        with connection(self.db_path) as conn:
            rows = conn.execute('''SELECT s.version_id,s.skill_id,s.spec_hash,s.spec_json,s.artifact_hash
                                   FROM skill_specs s JOIN versions v USING(version_id)
                                   WHERE s.status='active' AND v.status='active'
                                   ORDER BY s.skill_id''').fetchall()
        return [{**dict(row), 'spec': json.loads(row['spec_json'])} for row in rows]

    def providers(self, capability):
        return sorted({row['spec'].get('provider') for row in self.active()
                       if row['spec'].get('capability') == capability and row['spec'].get('provider')})

    def get(self, version_id):
        with connection(self.db_path) as conn:
            row = conn.execute('SELECT * FROM skill_specs WHERE version_id=?', (version_id,)).fetchone()
        return {**dict(row), 'spec': json.loads(row['spec_json'])} if row else None
