"""Version-scoped verified observations; learning only proposes guidance."""
import json
import time
from app.kernel.database import migrate, connection


class Dreams:
    def __init__(self, path, rules=None, events=None, memory=None):
        self.path, self.rules, self.events, self.memory = path, rules, events, memory
        migrate(path, [[
            '''CREATE TABLE IF NOT EXISTS observations(execution_id TEXT PRIMARY KEY, task_id TEXT,
               skill_name TEXT NOT NULL, version_id TEXT NOT NULL, artifact_hash TEXT NOT NULL,
               outcome INTEGER, duration REAL, resource_usage TEXT, evidence TEXT, created_at REAL)''',
            '''CREATE TABLE IF NOT EXISTS proposals(skill_name TEXT,version_id TEXT,rule_id TEXT,
               PRIMARY KEY(skill_name,version_id))''',
        ], [
            '''CREATE TABLE IF NOT EXISTS rule_uses(execution_id TEXT, rule_id TEXT, success INTEGER, created_at REAL,
               PRIMARY KEY(execution_id, rule_id))''',
            '''CREATE TABLE IF NOT EXISTS task_observations(execution_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
               outcome INTEGER, evidence TEXT, created_at REAL)''',
            '''CREATE TABLE IF NOT EXISTS rule_proposals(hypothesis_id TEXT PRIMARY KEY, rule_id TEXT UNIQUE)''',
        ]], component='dreams')

    def record(self, task, success, evidence):
        run = task.get('execution_id')
        tid = task.get('id')
        if run and isinstance(success, bool) and self.memory and hasattr(self.memory, 'record_episode'):
            self.memory.record_episode(
                tid, task.get('agent_run_id'), task.get('request', ''),
                'success' if success else 'failure', 'execution',
                ('Ejecucion verificada correctamente' if success
                 else 'Ejecucion verificada con fallo'), evidence)
        if not run or not isinstance(success, bool):
            raise ValueError('La evidencia debe identificar una ejecución verificada y un resultado booleano')

        degraded_rules = []
        skill_proposal_needed = False
        skill_name = task.get('skill_name')
        version_id = task.get('version_id')
        artifact_hash = task.get('artifact_hash')
        hypothesis = task.get('hypothesis_id')

        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            # 1. Deduplicar por ejecución y rechazar atribuciones contradictorias
            existing = conn.execute(
                'SELECT outcome FROM task_observations WHERE execution_id=?',
                (run,)
            ).fetchone()
            if existing:
                if existing[0] != int(success):
                    raise ValueError(f'Observación contradictoria para la misma ejecución: existente={existing[0]} nuevo={int(success)}')
                return False

            conn.execute(
                'INSERT INTO task_observations VALUES (?,?,?,?,?)',
                (run, tid or 'unknown', int(success),
                 json.dumps(evidence, ensure_ascii=False) if isinstance(evidence, (dict, list)) else str(evidence),
                 time.time())
            )
            try:
                conn.execute(
                    'INSERT OR IGNORE INTO control_feedback VALUES (?,?,?,?,?)',
                    (run, tid or 'unknown', int(success),
                     json.dumps(evidence, ensure_ascii=False) if isinstance(evidence, (dict, list)) else str(evidence),
                     time.time())
                )
            except Exception:
                pass

            # 2. Tracking de hipótesis en SQLite
            if hypothesis:
                try:
                    conn.execute(
                        'UPDATE hypotheses SET activations=activations+1, successes=successes+?, failures=failures+? WHERE id=?',
                        (int(success), int(not success), hypothesis)
                    )
                    conn.execute(
                        'UPDATE hypotheses SET confidence=CAST(successes AS REAL)/MAX(1,successes+failures) WHERE id=?',
                        (hypothesis,)
                    )
                    context = task.get('context', 'general')
                    conn.execute(
                        'INSERT OR IGNORE INTO hypothesis_contexts(hypothesis_id,context,successes,failures) VALUES (?,?,0,0)',
                        (hypothesis, context)
                    )
                    conn.execute(
                        'UPDATE hypothesis_contexts SET successes=successes+?,failures=failures+? WHERE hypothesis_id=? AND context=?',
                        (int(success), int(not success), hypothesis, context)
                    )
                except Exception:
                    pass

            # 3. Tracking de uso de directrices y detección de retiro de degradadas
            for rule_id in set(task.get('guidelines', [])):
                str_rid = str(rule_id)
                conn.execute('INSERT OR IGNORE INTO rule_uses VALUES (?,?,?,?)',
                             (run, str_rid, int(success), time.time()))
                score_row = conn.execute('SELECT AVG(success),COUNT(*) FROM rule_uses WHERE rule_id=?',
                                         (str_rid,)).fetchone()
                if score_row and score_row[1] >= 3 and score_row[0] <= 0.3:
                    degraded_rules.append((str_rid, score_row[0], score_row[1]))
                try:
                    if score_row:
                        conn.execute(
                            "UPDATE dynamic_guidelines SET success_rate=?,confidence=?,last_used=?,status=CASE WHEN ? >= 3 AND ? <= 0.3 THEN 'deprecated' ELSE status END WHERE id=?",
                            (score_row[0], score_row[0], str(time.time()), score_row[1], score_row[0], rule_id)
                        )
                except Exception:
                    pass

            # 4. Consolidación de hipótesis basada en 3 objetivos distintos
            if hypothesis:
                try:
                    try:
                        tasks_rows = conn.execute('SELECT document FROM control_tasks').fetchall()
                        ids = {json.loads(r[0])['id'] for r in tasks_rows if str(json.loads(r[0]).get('hypothesis_id')) == str(hypothesis)}
                    except Exception:
                        ids = {r[0] for r in conn.execute('SELECT DISTINCT task_id FROM task_observations').fetchall()}

                    verified = {r[0] for r in conn.execute('SELECT task_id FROM task_observations WHERE outcome=1').fetchall()}
                    try:
                        verified.update(r[0] for r in conn.execute('SELECT task_id FROM control_feedback WHERE success=1').fetchall())
                    except Exception:
                        pass

                    hyp = conn.execute('SELECT * FROM hypotheses WHERE id=?', (hypothesis,)).fetchone()
                    if hyp and len(ids & verified) >= 3 and hyp['confidence'] >= 0.8:
                        if not conn.execute('SELECT 1 FROM rule_proposals WHERE hypothesis_id=?', (str(hypothesis),)).fetchone():
                            cur = conn.execute(
                                "INSERT INTO dynamic_guidelines(topic,scope,rule_text,status,confidence,success_rate,created_at) VALUES (?,?,?,'candidate',?,?,?)",
                                (hyp['topic'], task.get('context', 'general'), hyp['content'], hyp['confidence'], hyp['confidence'], str(time.time()))
                            )
                            conn.execute('INSERT INTO rule_proposals VALUES (?,?)', (str(hypothesis), str(cur.lastrowid)))
                            if self.rules and hasattr(self.rules, 'propose'):
                                try:
                                    self.rules.propose(hyp['content'], f'dreams:hypothesis:{hypothesis}')
                                except Exception:
                                    pass
                except Exception:
                    pass

            # 5. Registro de Skill reusable
            if skill_name and version_id and artifact_hash:
                conn.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (run, tid, skill_name, version_id, artifact_hash, int(success),
                     (task.get('result') or {}).get('duration'),
                     json.dumps((task.get('result') or {}).get('resource_usage', {})),
                     json.dumps(evidence, ensure_ascii=False) if isinstance(evidence, (dict, list)) else str(evidence),
                     time.time()))
                m_row = conn.execute(
                    'SELECT COUNT(DISTINCT task_id) distinct_tasks, AVG(outcome) success_rate '
                    'FROM observations WHERE skill_name=? AND version_id=?',
                    (skill_name, version_id)
                ).fetchone()
                if m_row and m_row[0] >= 3 and m_row[1] >= 0.8:
                    already_prop = conn.execute(
                        'SELECT 1 FROM proposals WHERE skill_name=? AND version_id=?',
                        (skill_name, version_id)
                    ).fetchone()
                    if not already_prop:
                        guidance = (
                            'For the verified objective ' + task.get('context', 'recorded in the evidence') +
                            ', consider reusing skill ' + skill_name + ' version ' + version_id +
                            '. Recheck applicability and permissions before executing.'
                        )
                        rid = None
                        if self.rules and hasattr(self.rules, 'propose'):
                            try:
                                rid = self.rules.propose(guidance, 'dreams:' + skill_name + ':' + version_id, conn=conn)
                            except Exception:
                                rid = None
                        conn.execute('INSERT INTO proposals VALUES (?,?,?)', (skill_name, version_id, rid or 'rule_prop'))

        # --- Fin de la transacción principal: ejecutar side-effects externos sin locks ---

        # A. Retirar reglas degradadas
        for str_rid, s_rate, s_uses in degraded_rules:
            if self.rules and hasattr(self.rules, 'deprecate'):
                try:
                    self.rules.deprecate(str_rid, reason=f'Degraded success rate {s_rate:.2f} after {s_uses} uses')
                except Exception:
                    pass
            if self.memory and hasattr(self.memory, 'deprecate_guideline'):
                try:
                    self.memory.deprecate_guideline(int(str_rid) if str_rid.isdigit() else str_rid)
                except Exception:
                    pass
            if self.events and hasattr(self.events, 'emit'):
                self.events.emit('dreams', 'rule_deprecated', correlation_id=tid, task_id=tid,
                                 metadata={'rule_id': str_rid, 'success_rate': s_rate, 'uses': s_uses})

        # B. Proponer consolidación de skill
        if skill_name and version_id and artifact_hash:
            if self.events and hasattr(self.events, 'emit'):
                self.events.emit('dreams', 'verified_result', correlation_id=tid, task_id=tid,
                    execution_id=run, skill_name=skill_name, version_id=version_id,
                    metadata={'success': success, 'artifact_hash': artifact_hash})
            pass

        return True

    def metrics(self, name, version):
        with connection(self.path) as conn:
            row = conn.execute(
                'SELECT COUNT(*) samples, COUNT(DISTINCT task_id) distinct_tasks, AVG(outcome) success_rate '
                'FROM observations WHERE skill_name=? AND version_id=?',
                (name, version)
            ).fetchone()
        return dict(row) if row else {'samples': 0, 'distinct_tasks': 0, 'success_rate': 0.0}

    def consolidate(self, name, version, guidance):
        metrics = self.metrics(name, version)
        if metrics.get('distinct_tasks', 0) < 3 or metrics.get('success_rate', 0) < 0.8:
            raise ValueError('Insufficient verified evidence')
        with connection(self.path) as conn:
            row = conn.execute('SELECT rule_id FROM proposals WHERE skill_name=? AND version_id=?', (name, version)).fetchone()
            if row:
                return row[0]
        rid = None
        if self.rules and hasattr(self.rules, 'propose'):
            rid = self.rules.propose(guidance, 'dreams:' + name + ':' + version)
        with connection(self.path) as conn:
            conn.execute('INSERT INTO proposals VALUES (?,?,?)', (name, version, rid or 'rule_prop'))
        return rid

    def activate(self, rule_id, supersedes=None, source=None):
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if source:
                try:
                    conn.execute('CREATE TABLE IF NOT EXISTS control_commands(source TEXT PRIMARY KEY)')
                    if conn.execute('SELECT 1 FROM control_commands WHERE source=?', (source,)).fetchone():
                        return
                except Exception:
                    pass

            if self.rules and hasattr(self.rules, 'approve'):
                try:
                    self.rules.approve(str(rule_id), source or 'dreams', str(supersedes) if supersedes else None)
                except Exception:
                    pass

            try:
                row = conn.execute('SELECT status FROM dynamic_guidelines WHERE id=?', (rule_id,)).fetchone()
                if row:
                    if row[0] != 'candidate':
                        raise ValueError('La regla no es una candidata validada')
                    if supersedes:
                        if not conn.execute("SELECT 1 FROM dynamic_guidelines WHERE id=? AND status='active'", (supersedes,)).fetchone():
                            raise ValueError('La regla sustituida no está activa')
                        conn.execute("UPDATE dynamic_guidelines SET status='deprecated' WHERE id=?", (supersedes,))
                    conn.execute("UPDATE dynamic_guidelines SET status='active',supersedes=? WHERE id=?", (supersedes, rule_id))
            except Exception as exc:
                if 'La regla' in str(exc):
                    raise

            if source:
                try:
                    conn.execute('INSERT OR IGNORE INTO control_commands VALUES (?)', (source,))
                except Exception:
                    pass

    def rollback(self, rule_id, source=None):
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if source:
                try:
                    conn.execute('CREATE TABLE IF NOT EXISTS control_commands(source TEXT PRIMARY KEY)')
                    if conn.execute('SELECT 1 FROM control_commands WHERE source=?', (source,)).fetchone():
                        return
                except Exception:
                    pass

            if self.rules and hasattr(self.rules, 'rollback'):
                try:
                    self.rules.rollback(str(rule_id), source or 'dreams')
                except Exception:
                    pass

            try:
                row = conn.execute('SELECT supersedes,status FROM dynamic_guidelines WHERE id=?', (rule_id,)).fetchone()
                if row:
                    if row[1] != 'active':
                        raise ValueError('La regla no está activa')
                    conn.execute("UPDATE dynamic_guidelines SET status='deprecated' WHERE id=?", (rule_id,))
                    if row[0]:
                        conn.execute("UPDATE dynamic_guidelines SET status='active' WHERE id=?", (row[0],))
            except Exception as exc:
                if 'La regla' in str(exc):
                    raise

            if source:
                try:
                    conn.execute('INSERT OR IGNORE INTO control_commands VALUES (?)', (source,))
                except Exception:
                    pass
