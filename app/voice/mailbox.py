"""Durable ordinary-message ingress and conservative Telegram delivery receipts."""
import time
import uuid

from app.kernel.database import connection, migrate


class VoiceMailbox:
    def __init__(self, db_path):
        self.db_path = db_path
        migrate(db_path, [[
            '''CREATE TABLE IF NOT EXISTS voice_inbox(
                 source TEXT PRIMARY KEY, trace_id TEXT NOT NULL, text TEXT NOT NULL,
                 status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
                 accepted_at REAL NOT NULL, completed_at REAL,
                 error_type TEXT)''',
            '''CREATE TABLE IF NOT EXISTS voice_outbox(
                 delivery_id TEXT PRIMARY KEY, text TEXT NOT NULL, status TEXT NOT NULL,
                 created_at REAL NOT NULL, sent_at REAL, error_type TEXT)''',
            'CREATE INDEX IF NOT EXISTS voice_inbox_pending ON voice_inbox(status,priority,accepted_at)',
            'CREATE INDEX IF NOT EXISTS voice_outbox_pending ON voice_outbox(status,created_at)',
        ]], component='voice-mailbox')
        # A process may have completed a side effect before dying. Its receipt
        # cannot be inferred from a pending SQLite row alone.
        with connection(db_path) as conn:
            conn.execute("UPDATE voice_inbox SET status='unknown' WHERE status='processing'")
            conn.execute("UPDATE voice_outbox SET status='unknown' WHERE status='sending'")

    def accept(self, source, trace_id, text, *, urgent=False):
        if not source or not trace_id or not text or len(text) > 32000:
            raise ValueError('Invalid ordinary message')
        now = time.time()
        with connection(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT trace_id,text,status FROM voice_inbox WHERE source=?', (source,)).fetchone()
            if row:
                if row['text'] != text:
                    raise ValueError('Update ID reused with different content')
                return row['status']
            conn.execute('INSERT INTO voice_inbox(source,trace_id,text,status,priority,accepted_at) '
                         'VALUES (?,?,?,"pending",?,?)', (source, trace_id, text, int(urgent), now))
        return 'pending'

    def process_one(self, handler, *, urgent=False):
        with connection(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT source,trace_id,text FROM voice_inbox "
                               "WHERE status='pending' AND priority=? "
                               'ORDER BY accepted_at LIMIT 1', (int(urgent),)).fetchone()
            if not row:
                return False
            conn.execute("UPDATE voice_inbox SET status='processing' WHERE source=?", (row['source'],))
        try:
            handler(row['source'], row['trace_id'], row['text'])
        except Exception as exc:
            # The handler may have crossed a side-effect boundary. Inspect
            # its durable receipts before deciding whether it can run again.
            with connection(self.db_path) as conn:
                conn.execute("UPDATE voice_inbox SET status='unknown',error_type=? WHERE source=?",
                             (type(exc).__name__, row['source']))
            raise
        with connection(self.db_path) as conn:
            conn.execute("UPDATE voice_inbox SET status='completed',completed_at=? WHERE source=?",
                         (time.time(), row['source']))
        return True

    def enqueue(self, text):
        if not isinstance(text, str) or not text or len(text) > 64000:
            raise ValueError('Invalid notification')
        delivery_id = 'delivery-' + uuid.uuid4().hex
        with connection(self.db_path) as conn:
            conn.execute('INSERT INTO voice_outbox(delivery_id,text,status,created_at) '
                         'VALUES (?, ?, "pending", ?)', (delivery_id, text, time.time()))
        return delivery_id

    def deliver_one(self, sender):
        with connection(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT delivery_id,text FROM voice_outbox WHERE status='pending' "
                               'ORDER BY created_at LIMIT 1').fetchone()
            if not row:
                return False
            conn.execute("UPDATE voice_outbox SET status='sending' WHERE delivery_id=?", (row['delivery_id'],))
        try:
            for offset in range(0, len(row['text']), 3900):
                sender(row['text'][offset:offset + 3900])
        except Exception as exc:
            with connection(self.db_path) as conn:
                conn.execute("UPDATE voice_outbox SET status='unknown',error_type=? WHERE delivery_id=?",
                             (type(exc).__name__, row['delivery_id']))
            raise
        with connection(self.db_path) as conn:
            conn.execute("UPDATE voice_outbox SET status='delivered',sent_at=? WHERE delivery_id=?",
                         (time.time(), row['delivery_id']))
        return True

    def pending_counts(self):
        with connection(self.db_path) as conn:
            incoming = dict(conn.execute('SELECT status,COUNT(*) FROM voice_inbox GROUP BY status'))
            outgoing = dict(conn.execute('SELECT status,COUNT(*) FROM voice_outbox GROUP BY status'))
        return {'inbox': incoming, 'outbox': outgoing}
