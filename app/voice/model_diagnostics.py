"""Durable, redacted model-contract evidence owned by Voice, never worker input."""
import hashlib
import json
import time
import uuid
from app.kernel.database import connection, migrate
from app.kernel.tracing import get_trace_id
from app.pixy.budget import current_goal_revision


class ModelDiagnostics:
    def __init__(self, path, secrets=None):
        self.path, self.secrets = path, secrets
        migrate(path, [[
            'CREATE TABLE IF NOT EXISTS model_rejections(id TEXT PRIMARY KEY, created_at REAL NOT NULL, task_id TEXT, document TEXT NOT NULL)',
            'CREATE TRIGGER IF NOT EXISTS model_rejections_no_update BEFORE UPDATE ON model_rejections BEGIN SELECT RAISE(ABORT,\'model evidence is immutable\'); END',
            'CREATE TRIGGER IF NOT EXISTS model_rejections_no_delete BEFORE DELETE ON model_rejections BEGIN SELECT RAISE(ABORT,\'model evidence is immutable\'); END',
        ]], component='model-diagnostics')

    def _redact(self, text):
        for key, value in (self.secrets.items() if self.secrets else []):
            if (value and len(str(value)) >= 8
                    and any(word in key.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'SA_JSON'))):
                text = text.replace(str(value), '[REDACTED]')
                text = text.replace(json.dumps(str(value))[1:-1], '[REDACTED]')
        return text

    def record(self, contract, raw, errors, schema, model_context=None):
        raw = raw if isinstance(raw, str) else repr(raw)
        bounded = self._redact(raw)[:16000]
        goal = current_goal_revision.get()
        entry = {'id': 'rejection_' + uuid.uuid4().hex, 'created_at': time.time(),
                 'task_id': goal[0] if goal else None, 'goal_revision': goal[1] if goal else None,
                 'trace_id': get_trace_id(), 'contract': contract, 'untrusted': True,
                 'response_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                 'response_excerpt': bounded, 'truncated': len(raw) > 16000,
                 'schema': schema, 'model_context': model_context or {},
                 'errors': [{'type': str(e.get('type', 'unknown'))[:100],
                             'loc': [str(x)[:100] for x in e.get('loc', [])[:12]]}
                            for e in errors[:25]]}
        # Redact the entire record as well; never persist provider secrets in errors or metadata.
        document = self._redact(json.dumps(entry, ensure_ascii=False))
        if len(document.encode()) > 64000:
            raise ValueError('Model rejection evidence exceeds limit')
        with connection(self.path) as conn:
            conn.execute('INSERT INTO model_rejections VALUES (?,?,?,?)',
                         (entry['id'], entry['created_at'], entry['task_id'], document))
        safe = json.loads(document)
        return {'id': entry['id'], 'response_sha256': entry['response_sha256'],
                'errors': safe['errors'], 'trace_id': entry['trace_id']}

    def recent(self, limit=10):
        with connection(self.path) as conn:
            rows = conn.execute('SELECT document FROM model_rejections ORDER BY created_at DESC LIMIT ?',
                                (max(1, min(20, limit)),)).fetchall()
        return [json.loads(r['document']) for r in rows]

    def get(self, reference):
        with connection(self.path) as conn:
            row = conn.execute('SELECT document FROM model_rejections WHERE id=?', (reference,)).fetchone()
        return json.loads(row['document']) if row else None
