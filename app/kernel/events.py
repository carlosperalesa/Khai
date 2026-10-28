import json
import time
import uuid
from app.kernel.database import migrate, connection


class Events:
    def __init__(self, path):
        self.path = path
        migrate(path, [["""CREATE TABLE IF NOT EXISTS events(
            event_id TEXT PRIMARY KEY, created_at REAL NOT NULL, module TEXT NOT NULL,
            event_type TEXT NOT NULL, level TEXT, correlation_id TEXT, task_id TEXT,
            execution_id TEXT, skill_name TEXT, version_id TEXT, message TEXT, metadata TEXT)""",
            'CREATE INDEX IF NOT EXISTS events_correlation ON events(correlation_id,created_at)']], component='events')

    def emit(self, module, event_type, *, message='', metadata=None, **ids):
        event = ids.get('event_id') or uuid.uuid4().hex
        # Callers supply summaries and IDs, never raw provider bodies or credentials.
        with connection(self.path) as conn:
            conn.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (event, time.time(), module, event_type, ids.get('level', 'INFO'),
                 ids.get('correlation_id'), ids.get('task_id'), ids.get('execution_id'),
                 ids.get('skill_name'), ids.get('version_id'), message,
                 json.dumps(metadata or {}, ensure_ascii=False)))
        return event
