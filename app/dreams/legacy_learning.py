"""One verified observation per execution; explicit, reversible rule activation."""
import json
import time


class LearningLedger:
    def __init__(self, store):
        self.store = store
        with store.connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS rule_uses (
                    execution_id TEXT, rule_id INTEGER, success INTEGER,
                    PRIMARY KEY(execution_id, rule_id));
                CREATE TABLE IF NOT EXISTS rule_proposals (
                    hypothesis_id INTEGER PRIMARY KEY, rule_id INTEGER UNIQUE);
            ''')

    def record(self, task, success, evidence):
        run = task.get("execution_id")
        if not run or not isinstance(success, bool):
            raise ValueError("La evidencia debe identificar una ejecución verificada")
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            inserted = conn.execute("INSERT OR IGNORE INTO control_feedback VALUES (?,?,?,?,?)",
                (run, task["id"], int(success), json.dumps(evidence), time.time())).rowcount
            if not inserted:
                return False
            hypothesis = task.get("hypothesis_id")
            if hypothesis:
                conn.execute("UPDATE hypotheses SET activations=activations+1, successes=successes+?, failures=failures+? WHERE id=?",
                             (int(success), int(not success), hypothesis))
                conn.execute("UPDATE hypotheses SET confidence=CAST(successes AS REAL)/MAX(1,successes+failures) WHERE id=?", (hypothesis,))
                context = task.get("context", "general")
                conn.execute("INSERT OR IGNORE INTO hypothesis_contexts(hypothesis_id,context,successes,failures) VALUES (?,?,0,0)", (hypothesis, context))
                conn.execute("UPDATE hypothesis_contexts SET successes=successes+?,failures=failures+? WHERE hypothesis_id=? AND context=?",
                             (int(success), int(not success), hypothesis, context))
            for rule_id in set(task.get("guidelines", [])):
                conn.execute("INSERT INTO rule_uses VALUES (?,?,?)", (run, rule_id, int(success)))
                score = conn.execute("SELECT AVG(success),COUNT(*) FROM rule_uses WHERE rule_id=?", (rule_id,)).fetchone()
                conn.execute("UPDATE dynamic_guidelines SET success_rate=?,confidence=?,last_used=?,status=CASE WHEN ? >= 3 AND ? <= 0.3 THEN 'deprecated' ELSE status END WHERE id=?",
                             (score[0], score[0], str(time.time()), score[1], score[0], rule_id))
            if hypothesis:
                related = [json.loads(r[0]) for r in conn.execute("SELECT document FROM control_tasks")]
                ids = {t['id'] for t in related if str(t.get('hypothesis_id')) == str(hypothesis)}
                verified = {r[0] for r in conn.execute("SELECT task_id FROM control_feedback WHERE success=1")}
                hyp = conn.execute("SELECT * FROM hypotheses WHERE id=?", (hypothesis,)).fetchone()
                if hyp and len(ids & verified) >= 3 and hyp['confidence'] >= .8:
                    if not conn.execute("SELECT 1 FROM rule_proposals WHERE hypothesis_id=?", (hypothesis,)).fetchone():
                        cur = conn.execute("INSERT INTO dynamic_guidelines(topic,scope,rule_text,status,confidence,success_rate,created_at) VALUES (?,?,?,'candidate',?,?,?)",
                            (hyp['topic'], task.get('context', 'general'), hyp['content'], hyp['confidence'], hyp['confidence'], str(time.time())))
                        conn.execute("INSERT INTO rule_proposals VALUES (?,?)", (hypothesis, cur.lastrowid))
            return True

    def activate(self, rule_id, supersedes=None, source=None):
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if source and conn.execute('SELECT 1 FROM control_commands WHERE source=?', (source,)).fetchone():
                return
            row = conn.execute("SELECT status FROM dynamic_guidelines WHERE id=?", (rule_id,)).fetchone()
            if not row or row[0] != 'candidate':
                raise ValueError('La regla no es una candidata validada')
            if supersedes:
                if not conn.execute("SELECT 1 FROM dynamic_guidelines WHERE id=? AND status='active'", (supersedes,)).fetchone():
                    raise ValueError('La regla sustituida no está activa')
                conn.execute("UPDATE dynamic_guidelines SET status='deprecated' WHERE id=?", (supersedes,))
            conn.execute("UPDATE dynamic_guidelines SET status='active',supersedes=? WHERE id=?", (supersedes, rule_id))
            if source:
                conn.execute('INSERT INTO control_commands VALUES (?)', (source,))

    def rollback(self, rule_id, source=None):
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if source and conn.execute('SELECT 1 FROM control_commands WHERE source=?', (source,)).fetchone():
                return
            row = conn.execute("SELECT supersedes,status FROM dynamic_guidelines WHERE id=?", (rule_id,)).fetchone()
            if not row or row[1] != 'active':
                raise ValueError('La regla no está activa')
            conn.execute("UPDATE dynamic_guidelines SET status='deprecated' WHERE id=?", (rule_id,))
            if row[0]:
                conn.execute("UPDATE dynamic_guidelines SET status='active' WHERE id=?", (row[0],))

            if source:
                conn.execute('INSERT INTO control_commands VALUES (?)', (source,))
