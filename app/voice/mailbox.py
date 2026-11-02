"""Durable ordinary-message ingress and conservative Telegram delivery receipts."""
import time
import uuid
import json
import inspect

from app.kernel.database import connection, migrate
from app.kernel.journal import Journal


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
        ], [
            "ALTER TABLE voice_outbox ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE voice_outbox ADD COLUMN message_ids TEXT NOT NULL DEFAULT '[]'",
        ]], component='voice-mailbox')
        self.journal = Journal(db_path)
        self.on_delivered = None
        # A process may have completed a side effect before dying. Its receipt
        # cannot be inferred from a pending SQLite row alone.
        with connection(db_path) as conn:
            for row in conn.execute("SELECT source,trace_id FROM voice_inbox WHERE status='processing'").fetchall():
                cycle = self.journal.start('interaction', row['source'], trace_id=row['trace_id'],
                                           source=row['source'], conn=conn)
                self.journal.finish(cycle, 'unknown', phase='interrupted', conn=conn)
            for row in conn.execute("SELECT delivery_id,metadata FROM voice_outbox WHERE status='sending'").fetchall():
                meta = json.loads(row['metadata'])
                cycle = self._delivery_cycle(row['delivery_id'], meta, conn=conn)
                self.journal.finish(cycle, 'unknown', phase='interrupted', conn=conn)
            conn.execute("UPDATE voice_inbox SET status='unknown' WHERE status='processing'")
            conn.execute("UPDATE voice_outbox SET status='unknown' WHERE status='sending'")

    def _delivery_cycle(self, delivery_id, metadata, *, conn=None):
        refs = {key: metadata[key] for key in ('task_id', 'execution_id', 'case_id',
                'authorization_id', 'question_id') if metadata.get(key)}
        refs['delivery_id'] = delivery_id
        parent = metadata.get('cycle_id')
        source = metadata.get('source')
        if not parent and source:
            parent = self.journal.start('interaction', source,
                trace_id=metadata.get('trace_id'), source=source, conn=conn)
        return self.journal.start('delivery', delivery_id, parent_id=parent,
            trace_id=(None if parent else metadata.get('trace_id')), source=source, refs=refs, conn=conn)

    def accept(self, source, trace_id, text, *, urgent=False):
        if not source or not trace_id or not text or len(text) > 32000:
            raise ValueError('Invalid ordinary message')
        now = time.time()
        with connection(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            cycle = self.journal.start('interaction', source, trace_id=trace_id,
                                       source=source, conn=conn)
            row = conn.execute('SELECT trace_id,text,status FROM voice_inbox WHERE source=?', (source,)).fetchone()
            if row:
                if row['text'] != text:
                    raise ValueError('Update ID reused with different content')
                return row['status']
            conn.execute('INSERT INTO voice_inbox(source,trace_id,text,status,priority,accepted_at) '
                         'VALUES (?,?,?,"pending",?,?)', (source, trace_id, text, int(urgent), now))
            self.journal.event(cycle, 'accepted', conn=conn)
        return 'pending'

    def process_one(self, handler, *, urgent=False):
        with connection(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT source,trace_id,text FROM voice_inbox "
                               "WHERE status='pending' AND priority=? "
                               'ORDER BY accepted_at LIMIT 1', (int(urgent),)).fetchone()
            if not row:
                return False
            cycle = self.journal.start('interaction', row['source'], trace_id=row['trace_id'],
                                       source=row['source'], conn=conn)
            conn.execute("UPDATE voice_inbox SET status='processing' WHERE source=?", (row['source'],))
            self.journal.event(cycle, 'processing', conn=conn)
        try:
            with self.journal.activate(cycle):
                handler(row['source'], row['trace_id'], row['text'])
        except Exception as exc:
            # The handler may have crossed a side-effect boundary. Inspect
            # its durable receipts before deciding whether it can run again.
            with connection(self.db_path) as conn:
                conn.execute("UPDATE voice_inbox SET status='unknown',error_type=? WHERE source=?",
                             (type(exc).__name__, row['source']))
                self.journal.finish(cycle, 'failed', data={'error_type': type(exc).__name__}, conn=conn)
            raise
        with connection(self.db_path) as conn:
            conn.execute("UPDATE voice_inbox SET status='completed',completed_at=? WHERE source=?",
                         (time.time(), row['source']))
            self.journal.finish(cycle, conn=conn)
        return True

    def enqueue(self, text, metadata=None):
        if not isinstance(text, str) or not text or len(text) > 64000:
            raise ValueError('Invalid notification')
        delivery_id = 'delivery-' + uuid.uuid4().hex
        with connection(self.db_path) as conn:
            self._delivery_cycle(delivery_id, metadata or {}, conn=conn)
            conn.execute('INSERT INTO voice_outbox(delivery_id,text,status,created_at,metadata) '
                         'VALUES (?, ?, "pending", ?, ?)',
                         (delivery_id, text, time.time(), json.dumps(metadata or {})))
        return delivery_id

    def deliver_one(self, sender):
        with connection(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT delivery_id,text,metadata FROM voice_outbox WHERE status='pending' "
                               'ORDER BY created_at LIMIT 1').fetchone()
            if not row:
                return False
            meta = json.loads(row['metadata']) if row['metadata'] else {}
            cycle = self._delivery_cycle(row['delivery_id'], meta, conn=conn)
            conn.execute("UPDATE voice_outbox SET status='sending' WHERE delivery_id=?", (row['delivery_id'],))
            self.journal.event(cycle, 'sending', conn=conn)
        try:
            message_ids = []
            for index, offset in enumerate(range(0, len(row['text']), 3900)):
                chunk = row['text'][offset:offset + 3900]
                with connection(self.db_path) as conn:
                    self.journal.event(cycle, 'chunk_sending', data={'chunk_index': index,
                        'offset': offset, 'length': len(chunk)}, conn=conn)
                try:
                    inspect.signature(sender).bind(chunk, meta)
                except TypeError:
                    one_argument = True
                except ValueError:
                    one_argument = False
                else:
                    one_argument = False
                with self.journal.activate(cycle):
                    receipt = sender(chunk) if one_argument else sender(chunk, meta)
                result = receipt.get('result') if isinstance(receipt, dict) else None
                mid = result.get('message_id') if isinstance(result, dict) else None
                if (not isinstance(receipt, dict) or receipt.get('ok') is False
                        or type(mid) is not int or mid <= 0):
                    raise ValueError('Telegram delivery requires a valid message receipt')
                message_ids.append(mid)
                with connection(self.db_path) as conn:
                    conn.execute('UPDATE voice_outbox SET message_ids=? WHERE delivery_id=?',
                                 (json.dumps(message_ids), row['delivery_id']))
                    self.journal.event(cycle, 'chunk_delivered', data={'chunk_index': index,
                        'message_id': mid, 'offset': offset, 'length': len(chunk)}, conn=conn,
                        event_id=row['delivery_id'] + ':chunk:' + str(index))
        except Exception as exc:
            with connection(self.db_path) as conn:
                conn.execute("UPDATE voice_outbox SET status='unknown',error_type=? WHERE delivery_id=?",
                             (type(exc).__name__, row['delivery_id']))
                self.journal.finish(cycle, 'unknown', data={'error_type': type(exc).__name__}, conn=conn)
            raise
        with connection(self.db_path) as conn:
            conn.execute("UPDATE voice_outbox SET status='delivered',sent_at=?,message_ids=? WHERE delivery_id=?",
                         (time.time(), json.dumps(message_ids), row['delivery_id']))
            self.journal.finish(cycle, conn=conn)
        if self.on_delivered:
            self.on_delivered(dict(row), message_ids)
        return True

    def pending_counts(self):
        with connection(self.db_path) as conn:
            incoming = dict(conn.execute('SELECT status,COUNT(*) FROM voice_inbox GROUP BY status'))
            outgoing = dict(conn.execute('SELECT status,COUNT(*) FROM voice_outbox GROUP BY status'))
        return {'inbox': incoming, 'outbox': outgoing}
