"""Persistent Telegram sessions and references; task authority stays in Coordinator."""
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import time

from app.kernel.database import connection, migrate


current_turn = ContextVar('khai_conversation_turn', default=None)


@dataclass(frozen=True)
class ReplyResult:
    handled: bool
    text: str | None = None
    task_id: str | None = None


class ConversationStore:
    def __init__(self, path):
        self.path = path
        migrate(path, [[
            '''CREATE TABLE IF NOT EXISTS conversations(
               id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, chat_id TEXT NOT NULL,
               created_at REAL NOT NULL, UNIQUE(owner_id,chat_id))''',
            '''CREATE TABLE IF NOT EXISTS conversation_turns(
               source TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
               trace_id TEXT NOT NULL, message_id INTEGER, reply_to INTEGER,
               task_id TEXT, status TEXT NOT NULL, receipt TEXT)''',
            '''CREATE TABLE IF NOT EXISTS conversation_messages(
               id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL,
               source TEXT UNIQUE NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
               task_id TEXT, created_at REAL NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS conversation_questions(
               id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
               kind TEXT NOT NULL, subject_id TEXT NOT NULL, revision TEXT NOT NULL,
               state TEXT NOT NULL, delivery_id TEXT, status TEXT NOT NULL,
               message_ids TEXT NOT NULL DEFAULT '[]', sent_at REAL,
               answered_by TEXT, created_at REAL NOT NULL)''',
            '''CREATE INDEX IF NOT EXISTS conversation_question_pending
               ON conversation_questions(conversation_id,status,sent_at)''',
        ]], component='conversations')

    def session(self, owner_id, chat_id):
        owner_id, chat_id = str(owner_id), str(chat_id)
        sid = 'session_' + hashlib.sha256(json.dumps([owner_id, chat_id]).encode()).hexdigest()[:32]
        with connection(self.path) as conn:
            conn.execute('INSERT OR IGNORE INTO conversations VALUES (?,?,?,?)',
                         (sid, owner_id, chat_id, time.time()))
        return sid

    def begin(self, source, trace_id, text, owner_id, chat_id, message_id=None, reply_to=None):
        sid = self.session(owner_id, chat_id)
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR IGNORE INTO conversation_turns VALUES (?,?,?,?,?,NULL,?,NULL)',
                         (source, sid, trace_id, message_id, reply_to, 'pending'))
            row = conn.execute('SELECT * FROM conversation_turns WHERE source=?', (source,)).fetchone()
            if row['conversation_id'] != sid:
                raise PermissionError('El mensaje pertenece a otra conversación')
            old = conn.execute('SELECT content FROM conversation_messages WHERE source=?', (source,)).fetchone()
            if old and old['content'] != text:
                raise ValueError('El mensaje cambió después de su recepción')
            conn.execute('INSERT OR IGNORE INTO conversation_messages '
                         '(conversation_id,source,role,content,created_at) VALUES (?,?,?,?,?)',
                         (sid, source, 'user', text, time.time()))
            return dict(row)

    def turn(self, source):
        with connection(self.path) as conn:
            row = conn.execute('SELECT * FROM conversation_turns WHERE source=?', (source,)).fetchone()
            return dict(row) if row else None

    def bind(self, source, task_id):
        with connection(self.path) as conn:
            conn.execute('UPDATE conversation_turns SET task_id=?,status=? WHERE source=?',
                         (task_id, 'routed', source))
            conn.execute('UPDATE conversation_messages SET task_id=? WHERE source=?', (task_id, source))

    def complete(self, source, receipt):
        with connection(self.path) as conn:
            conn.execute('UPDATE conversation_turns SET status=?,receipt=? WHERE source=?',
                         ('completed', json.dumps(receipt, ensure_ascii=False), source))

    def record_call(self, source, call):
        call_id = 'call_' + hashlib.sha256(source.encode()).hexdigest()[:32]
        with connection(self.path) as conn:
            conn.execute("UPDATE conversation_turns SET status='calling',receipt=? "
                         "WHERE source=? AND status!='completed'",
                         (json.dumps({'call_id': call_id, 'tool_call': call, 'status': 'pending'}), source))
        return call_id

    def history(self, sid, exclude_source=None, limit=16, max_chars=16000):
        with connection(self.path) as conn:
            rows = conn.execute('SELECT role,content,task_id FROM conversation_messages '
                                'WHERE conversation_id=? AND source!=? ORDER BY id DESC LIMIT ?',
                                (sid, exclude_source or '', limit)).fetchall()
        result, used = [], 0
        for row in rows:
            item = dict(row)
            content = item['content'][:4000]
            if used + len(content) > max_chars:
                break
            item['content'] = content
            result.append(item)
            used += len(content)
        return list(reversed(result))

    def question(self, sid, kind, subject_id, revision, state):
        revision = str(revision)
        qid = 'question_' + hashlib.sha256(json.dumps(
            [sid, kind, subject_id, revision, state]).encode()).hexdigest()[:32]
        with connection(self.path) as conn:
            conn.execute('INSERT OR IGNORE INTO conversation_questions '
                         '(id,conversation_id,kind,subject_id,revision,state,status,created_at) '
                         'VALUES (?,?,?,?,?,?,?,?)',
                         (qid, sid, kind, subject_id, revision, state, 'queued', time.time()))
        return qid

    def attach(self, question_id, delivery_id):
        with connection(self.path) as conn:
            conn.execute('UPDATE conversation_questions SET delivery_id=? WHERE id=?',
                         (delivery_id, question_id))

    def delivered(self, sid, delivery_id, text, task_id=None, question_id=None, message_ids=()):
        with connection(self.path) as conn:
            conn.execute('INSERT OR IGNORE INTO conversation_messages '
                         '(conversation_id,source,role,content,task_id,created_at) VALUES (?,?,?,?,?,?)',
                         (sid, delivery_id, 'assistant', text, task_id, time.time()))
            if question_id:
                conn.execute("UPDATE conversation_questions SET status='open',sent_at=?,message_ids=? "
                             "WHERE id=? AND status='queued'",
                             (time.time(), json.dumps(list(message_ids)), question_id))

    def pending(self, sid):
        self.reconcile()
        with connection(self.path) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM conversation_questions WHERE conversation_id=? AND status='open' "
                'ORDER BY sent_at DESC,created_at DESC', (sid,))]

    def uncertain_question(self, sid):
        with connection(self.path) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='voice_outbox'").fetchone():
                return False
            return bool(conn.execute(
                "SELECT 1 FROM conversation_questions q JOIN voice_outbox o ON q.delivery_id=o.delivery_id "
                "WHERE q.conversation_id=? AND q.status='queued' AND o.status IN ('sending','unknown') LIMIT 1",
                (sid,)).fetchone())

    def reconcile(self):
        with connection(self.path) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='voice_outbox'").fetchone():
                return
            rows = conn.execute("SELECT o.* FROM voice_outbox o JOIN conversation_questions q "
                                "ON q.delivery_id=o.delivery_id WHERE q.status='queued' "
                                "AND o.status='delivered'").fetchall()
        for row in rows:
            data = json.loads(row['metadata'])
            self.delivered(data['conversation_id'], row['delivery_id'], row['text'],
                           data.get('task_id'), data.get('question_id'), json.loads(row['message_ids']))

    def get_question(self, qid, sid):
        with connection(self.path) as conn:
            row = conn.execute('SELECT * FROM conversation_questions WHERE id=? AND conversation_id=?',
                               (qid, sid)).fetchone()
            return dict(row) if row else None

    def question_for_message(self, sid, message_id):
        with connection(self.path) as conn:
            rows = conn.execute('SELECT * FROM conversation_questions WHERE conversation_id=?', (sid,)).fetchall()
        return next((dict(row) for row in rows if message_id in json.loads(row['message_ids'])), None)

    def answered(self, qid, source):
        with connection(self.path) as conn:
            conn.execute("UPDATE conversation_questions SET status='answered',answered_by=? "
                         "WHERE id=? AND status='open'", (source, qid))

    def stale(self, qid):
        with connection(self.path) as conn:
            conn.execute("UPDATE conversation_questions SET status='stale' WHERE id=? AND status='open'", (qid,))
