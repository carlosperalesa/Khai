"""Version-scoped verified observations; learning only proposes guidance."""
import json
import time
from app.common.database import migrate, connection


class Dreams:
    def __init__(self, path, rules, events):
        self.path, self.rules, self.events = path, rules, events
        migrate(path, [[
            '''CREATE TABLE IF NOT EXISTS observations(execution_id TEXT PRIMARY KEY, task_id TEXT,
               skill_name TEXT NOT NULL, version_id TEXT NOT NULL, artifact_hash TEXT NOT NULL,
               outcome INTEGER, duration REAL, resource_usage TEXT, evidence TEXT, created_at REAL)''',
            '''CREATE TABLE IF NOT EXISTS proposals(skill_name TEXT,version_id TEXT,rule_id TEXT,
               PRIMARY KEY(skill_name,version_id))''',
        ]])

    def record(self, task, success, evidence):
        required = ('execution_id','id','skill_name','version_id','artifact_hash')
        if type(success) is not bool or any(not task.get(k) for k in required):
            raise ValueError('Verified learning requires exact skill and execution identity')
        with connection(self.path) as conn:
            inserted = conn.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)',
                (task['execution_id'],task['id'],task['skill_name'],task['version_id'],task['artifact_hash'],
                 int(success),(task.get('result') or {}).get('duration'),
                 json.dumps((task.get('result') or {}).get('resource_usage',{})),json.dumps(evidence),time.time())).rowcount
        if inserted:
            self.events.emit('dreams','verified_result',correlation_id=task['id'],task_id=task['id'],
                execution_id=task['execution_id'],skill_name=task['skill_name'],version_id=task['version_id'],
                metadata={'success':success,'artifact_hash':task['artifact_hash']})
            metrics = self.metrics(task['skill_name'],task['version_id'])
            if metrics['samples']>=3 and metrics['success_rate']>=.8:
                self.consolidate(task['skill_name'],task['version_id'],
                    'For the verified objective '+task.get('context','recorded in the evidence')+
                    ', consider reusing skill '+task['skill_name']+' version '+task['version_id']+
                    '. Recheck applicability and permissions before executing.')
        return bool(inserted)

    def metrics(self, name, version):
        with connection(self.path) as conn:
            row = conn.execute('SELECT COUNT(*) samples,AVG(outcome) success_rate FROM observations WHERE skill_name=? AND version_id=?',
                               (name,version)).fetchone()
        return dict(row)

    def consolidate(self, name, version, guidance):
        metrics = self.metrics(name,version)
        if metrics['samples'] < 3 or metrics['success_rate'] < .8:
            raise ValueError('Insufficient verified evidence')
        with connection(self.path) as conn:
            row = conn.execute('SELECT rule_id FROM proposals WHERE skill_name=? AND version_id=?',(name,version)).fetchone()
            if row:
                return row[0]
        rid = self.rules.propose(guidance,'dreams:'+name+':'+version)
        with connection(self.path) as conn:
            conn.execute('INSERT INTO proposals VALUES (?,?,?)',(name,version,rid))
        return rid

    def activate(self, rule_id, supersedes=None, source=None):
        return self.rules.approve(str(rule_id),source,supersedes)

    def rollback(self, rule_id, source=None):
        return self.rules.rollback(str(rule_id),source)
