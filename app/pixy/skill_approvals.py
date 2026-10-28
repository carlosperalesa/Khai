"""Durable owner approvals for reusing an unchanged active skill within its scope."""
import json
import time

from app.kernel.database import connection, migrate


def scope_for(task):
    """Only concrete capabilities and pinned service operations grant reusable scope."""
    return {
        'capabilities': sorted(set(task.get('manifest', {}).get('capabilities', []))),
        'services': [list(ref) for ref in sorted({
            (ref['service_id'], ref['operation'], ref['version'])
            for ref in task.get('services', [])})],
    }


class SkillApprovalStore:
    def __init__(self, path):
        self.path = path
        migrate(path, [[
            '''CREATE TABLE IF NOT EXISTS pixy_skill_approvals(
               version_id TEXT NOT NULL, skill_hash TEXT NOT NULL, code_hash TEXT NOT NULL,
               scope TEXT NOT NULL, approved_by TEXT NOT NULL, approved_at REAL NOT NULL,
               task_id TEXT NOT NULL, PRIMARY KEY(version_id,skill_hash,code_hash,scope))''',
        ]], component='pixy-skill-approvals')

    def approve(self, task, version, approved_by):
        scope = scope_for(task)
        if not approved_by or not version or task.get('artifact_hash') is None:
            raise ValueError('Skill approval requires an owner, version and reviewed code')
        with connection(self.path) as conn:
            conn.execute('''INSERT OR IGNORE INTO pixy_skill_approvals
                (version_id,skill_hash,code_hash,scope,approved_by,approved_at,task_id)
                VALUES (?,?,?,?,?,?,?)''',
                (version['version_id'], version['artifact_hash'], task['artifact_hash'],
                 json.dumps(scope, sort_keys=True), approved_by, time.time(), task['id']))

    def matching(self, task, version):
        if not version or version['status'] != 'active':
            return None
        requested = scope_for(task)
        with connection(self.path) as conn:
            rows = conn.execute('''SELECT * FROM pixy_skill_approvals
                WHERE version_id=? AND skill_hash=? AND code_hash=? ORDER BY approved_at''',
                (version['version_id'], version['artifact_hash'], task['artifact_hash'])).fetchall()
        for row in rows:
            approved = json.loads(row['scope'])
            if (set(requested['capabilities']) <= set(approved['capabilities'])
                    and {tuple(ref) for ref in requested['services']}
                    <= {tuple(ref) for ref in approved['services']}):
                return dict(row)
        return None
