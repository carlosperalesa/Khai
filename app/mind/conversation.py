"""Persistent Telegram sessions and references; task authority stays in Coordinator."""
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import re
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
        migrate(path, [
            [
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
            ],
            [
                '''CREATE TABLE IF NOT EXISTS conversations_v2(
                   id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                   thread_id INTEGER, title TEXT, status TEXT NOT NULL DEFAULT 'active',
                   is_current INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
                   updated_at REAL NOT NULL)''',
                '''INSERT INTO conversations_v2(id, owner_id, chat_id, thread_id, title, status, is_current, created_at, updated_at)
                   SELECT id, owner_id, chat_id, NULL, 'Chat Principal', 'active', 1, created_at, created_at FROM conversations''',
                '''DROP TABLE conversations''',
                '''ALTER TABLE conversations_v2 RENAME TO conversations''',
                '''CREATE INDEX IF NOT EXISTS idx_conversations_owner_chat
                   ON conversations(owner_id, chat_id, is_current, status)''',
                '''CREATE INDEX IF NOT EXISTS idx_conversations_thread
                   ON conversations(owner_id, chat_id, thread_id)''',
            ]
        ], component='conversations')

    def get_conversation(self, sid):
        with connection(self.path) as conn:
            row = conn.execute('SELECT * FROM conversations WHERE id=?', (sid,)).fetchone()
            return dict(row) if row else None

    def session(self, owner_id, chat_id, thread_id=None):
        owner_id, chat_id = str(owner_id), str(chat_id)
        thread_id = int(thread_id) if thread_id is not None else None
        now = time.time()
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if thread_id is not None:
                row = conn.execute(
                    "SELECT id FROM conversations WHERE owner_id=? AND chat_id=? AND thread_id=? AND status='active' "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (owner_id, chat_id, thread_id)).fetchone()
                if row:
                    conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, row['id']))
                    return row['id']
                sid = 'session_' + hashlib.sha256(f"{owner_id}:{chat_id}:{thread_id}:{now}".encode()).hexdigest()[:32]
                title = f"Tema {thread_id}"
                conn.execute(
                    "INSERT INTO conversations (id, owner_id, chat_id, thread_id, title, status, is_current, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', 1, ?, ?)",
                    (sid, owner_id, chat_id, thread_id, title, now, now))
                return sid

            row = conn.execute(
                "SELECT id FROM conversations WHERE owner_id=? AND chat_id=? AND thread_id IS NULL AND is_current=1 AND status='active' "
                "ORDER BY updated_at DESC LIMIT 1",
                (owner_id, chat_id)).fetchone()
            if row:
                conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, row['id']))
                return row['id']

            row = conn.execute(
                "SELECT id FROM conversations WHERE owner_id=? AND chat_id=? AND thread_id IS NULL AND status='active' "
                "ORDER BY updated_at DESC LIMIT 1",
                (owner_id, chat_id)).fetchone()
            if row:
                conn.execute("UPDATE conversations SET is_current=1, updated_at=? WHERE id=?", (now, row['id']))
                return row['id']

            sid = 'session_' + hashlib.sha256(json.dumps([owner_id, chat_id]).encode()).hexdigest()[:32]
            title = 'Chat Principal'
            conn.execute(
                "INSERT INTO conversations (id, owner_id, chat_id, thread_id, title, status, is_current, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, ?, 'active', 1, ?, ?)",
                (sid, owner_id, chat_id, title, now, now))
            return sid

    def begin(self, source, trace_id, text, owner_id, chat_id, message_id=None, reply_to=None, thread_id=None):
        sid = self.session(owner_id, chat_id, thread_id=thread_id)
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR IGNORE INTO conversation_turns VALUES (?,?,?,?,?,NULL,?,NULL)',
                         (source, sid, trace_id, message_id, reply_to, 'pending'))
            row = conn.execute(
                'SELECT t.*, c.chat_id, c.thread_id, c.title FROM conversation_turns t '
                'JOIN conversations c ON t.conversation_id = c.id WHERE t.source=?', (source,)).fetchone()
            if not row or row['conversation_id'] != sid:
                raise PermissionError('El mensaje pertenece a otra conversación')
            old = conn.execute('SELECT content FROM conversation_messages WHERE source=?', (source,)).fetchone()
            if old and old['content'] != text:
                raise ValueError('El mensaje cambió después de su recepción')
            conn.execute('INSERT OR IGNORE INTO conversation_messages '
                         '(conversation_id,source,role,content,created_at) VALUES (?,?,?,?,?)',
                         (sid, source, 'user', text, time.time()))

            is_generic = (
                row['title'] in {'Chat Principal', 'Nuevo chat'} or
                row['title'].startswith('Conversación ') or
                row['title'] == f"Tema {thread_id}"
            )
            if is_generic and not text.startswith('/'):
                short_title = re.sub(r'[\r\n\t]+', ' ', text).strip()
                if short_title:
                    short_title = short_title[:35]
                    conn.execute('UPDATE conversations SET title=? WHERE id=? AND title=?',
                                 (short_title, sid, row['title']))
            return dict(row)

    def turn(self, source):
        with connection(self.path) as conn:
            row = conn.execute(
                'SELECT t.*, c.chat_id, c.thread_id, c.title FROM conversation_turns t '
                'JOIN conversations c ON t.conversation_id = c.id WHERE t.source=?', (source,)).fetchone()
            return dict(row) if row else None

    def new_conversation(self, owner_id, chat_id, title=None, thread_id=None):
        owner_id, chat_id = str(owner_id), str(chat_id)
        thread_id = int(thread_id) if thread_id is not None else None
        now = time.time()
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if thread_id is None:
                conn.execute(
                    "UPDATE conversations SET is_current=0 WHERE owner_id=? AND chat_id=? AND thread_id IS NULL",
                    (owner_id, chat_id))
            else:
                conn.execute(
                    "UPDATE conversations SET status='archived', is_current=0 "
                    "WHERE owner_id=? AND chat_id=? AND thread_id=? AND status='active'",
                    (owner_id, chat_id, thread_id))
            
            count = conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE owner_id=? AND chat_id=?",
                (owner_id, chat_id)).fetchone()[0]
            title = title or f"Conversación {count + 1}"
            sid = 'session_' + hashlib.sha256(f"{owner_id}:{chat_id}:{thread_id}:{count}:{now}".encode()).hexdigest()[:32]
            conn.execute(
                "INSERT INTO conversations (id, owner_id, chat_id, thread_id, title, status, is_current, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', 1, ?, ?)",
                (sid, owner_id, chat_id, thread_id, title, now, now))
            row = conn.execute("SELECT * FROM conversations WHERE id=?", (sid,)).fetchone()
            return dict(row)

    def clear_conversation(self, owner_id, chat_id, thread_id=None):
        owner_id, chat_id = str(owner_id), str(chat_id)
        thread_id = int(thread_id) if thread_id is not None else None
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if thread_id is not None:
                conn.execute(
                    "UPDATE conversations SET status='archived', is_current=0 "
                    "WHERE owner_id=? AND chat_id=? AND thread_id=? AND status='active'",
                    (owner_id, chat_id, thread_id))
            else:
                conn.execute(
                    "UPDATE conversations SET status='archived', is_current=0 "
                    "WHERE owner_id=? AND chat_id=? AND thread_id IS NULL AND is_current=1",
                    (owner_id, chat_id))
        return self.new_conversation(owner_id, chat_id, title="Nuevo chat", thread_id=thread_id)

    def conversation_for_delete(self, owner_id, chat_id, target=None, thread_id=None):
        owner_id, chat_id = str(owner_id), str(chat_id)
        thread_id = int(thread_id) if thread_id is not None else None
        with connection(self.path) as conn:
            if thread_id is not None:
                active_list = [dict(r) for r in conn.execute(
                    "SELECT * FROM conversations WHERE owner_id=? AND chat_id=? AND thread_id=? AND status='active' "
                    "ORDER BY updated_at DESC", (owner_id, chat_id, thread_id)).fetchall()]
            else:
                active_list = [dict(r) for r in conn.execute(
                    "SELECT * FROM conversations WHERE owner_id=? AND chat_id=? AND thread_id IS NULL AND status='active' "
                    "ORDER BY updated_at DESC", (owner_id, chat_id)).fetchall()]
            if not active_list:
                return None
            if target is None:
                return next((c for c in active_list if c['is_current']), active_list[0])
            value = str(target).strip()
            if not value.isdigit():
                return None
            idx = int(value) - 1
            return active_list[idx] if 0 <= idx < len(active_list) else None

    def delete_conversation(self, owner_id, chat_id, target=None, thread_id=None):
        owner_id, chat_id = str(owner_id), str(chat_id)
        thread_id = int(thread_id) if thread_id is not None else None
        target_conv = self.conversation_for_delete(owner_id, chat_id, target, thread_id)
        if not target_conv:
            return None
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("DELETE FROM conversation_questions WHERE conversation_id=?", (target_conv['id'],))
            conn.execute("DELETE FROM conversation_messages WHERE conversation_id=?", (target_conv['id'],))
            conn.execute("DELETE FROM conversation_turns WHERE conversation_id=?", (target_conv['id'],))
            conn.execute("DELETE FROM conversations WHERE id=? AND owner_id=? AND chat_id=?",
                         (target_conv['id'], owner_id, chat_id))

            if thread_id is not None:
                remaining = [dict(r) for r in conn.execute(
                    "SELECT * FROM conversations WHERE owner_id=? AND chat_id=? AND thread_id=? AND status='active' "
                    "ORDER BY updated_at DESC", (owner_id, chat_id, thread_id)).fetchall()]
            else:
                remaining = [dict(r) for r in conn.execute(
                    "SELECT * FROM conversations WHERE owner_id=? AND chat_id=? AND thread_id IS NULL AND status='active' "
                    "ORDER BY updated_at DESC", (owner_id, chat_id)).fetchall()]

            active = next((c for c in remaining if c['is_current']), None)
            if target_conv['is_current'] and remaining:
                if thread_id is None:
                    conn.execute("UPDATE conversations SET is_current=0 WHERE owner_id=? AND chat_id=? AND thread_id IS NULL",
                                 (owner_id, chat_id))
                else:
                    conn.execute("UPDATE conversations SET is_current=0 WHERE owner_id=? AND chat_id=? AND thread_id=?",
                                 (owner_id, chat_id, thread_id))
                active = remaining[0]
                conn.execute("UPDATE conversations SET is_current=1 WHERE id=?", (active['id'],))
                active['is_current'] = 1
            elif active is None and remaining:
                active = remaining[0]
                conn.execute("UPDATE conversations SET is_current=1 WHERE id=?", (active['id'],))
                active['is_current'] = 1
            elif not remaining:
                now = time.time()
                sid = 'session_' + hashlib.sha256(f"{owner_id}:{chat_id}:{now}".encode()).hexdigest()[:32]
                title = f"Tema {thread_id}" if thread_id is not None else 'Chat Principal'
                conn.execute(
                    "INSERT INTO conversations (id, owner_id, chat_id, thread_id, title, status, is_current, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', 1, ?, ?)",
                    (sid, owner_id, chat_id, thread_id, title, now, now))
                active = dict(conn.execute("SELECT * FROM conversations WHERE id=?", (sid,)).fetchone())
            return {'deleted': target_conv, 'active': active}

    def list_conversations(self, owner_id, chat_id, thread_id=None, limit=10):
        owner_id, chat_id = str(owner_id), str(chat_id)
        thread_id = int(thread_id) if thread_id is not None else None
        with connection(self.path) as conn:
            if thread_id is not None:
                rows = conn.execute(
                    "SELECT * FROM conversations WHERE owner_id=? AND chat_id=? AND thread_id=? AND status='active' "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (owner_id, chat_id, thread_id, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM conversations WHERE owner_id=? AND chat_id=? AND thread_id IS NULL AND status='active' "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (owner_id, chat_id, limit)).fetchall()
            return [dict(r) for r in rows]

    def switch_conversation(self, owner_id, chat_id, target, thread_id=None):
        owner_id, chat_id = str(owner_id), str(chat_id)
        thread_id = int(thread_id) if thread_id is not None else None
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if thread_id is not None:
                active = [dict(r) for r in conn.execute(
                    "SELECT * FROM conversations WHERE owner_id=? AND chat_id=? AND thread_id=? AND status='active' "
                    "ORDER BY updated_at DESC", (owner_id, chat_id, thread_id)).fetchall()]
            else:
                active = [dict(r) for r in conn.execute(
                    "SELECT * FROM conversations WHERE owner_id=? AND chat_id=? AND thread_id IS NULL AND status='active' "
                    "ORDER BY updated_at DESC", (owner_id, chat_id)).fetchall()]
            if not active:
                return None
            target_conv = None
            if str(target).strip().isdigit():
                idx = int(str(target).strip()) - 1
                if 0 <= idx < len(active):
                    target_conv = active[idx]
            else:
                target_str = str(target).strip().lower()
                target_conv = next((c for c in active if target_str in c['title'].lower() or target_str == c['id']), None)
            
            if not target_conv:
                return None
            
            now = time.time()
            if thread_id is None:
                conn.execute("UPDATE conversations SET is_current=0 WHERE owner_id=? AND chat_id=? AND thread_id IS NULL", (owner_id, chat_id))
            else:
                conn.execute("UPDATE conversations SET is_current=0 WHERE owner_id=? AND chat_id=? AND thread_id=?", (owner_id, chat_id, thread_id))
            conn.execute("UPDATE conversations SET is_current=1, updated_at=? WHERE id=?", (now, target_conv['id']))
            target_conv['is_current'] = 1
            target_conv['updated_at'] = now
            return target_conv

    def rename_conversation(self, owner_id, chat_id, new_title, target=None, thread_id=None):
        owner_id, chat_id = str(owner_id), str(chat_id)
        thread_id = int(thread_id) if thread_id is not None else None
        new_title = re.sub(r'[\r\n\t]+', ' ', str(new_title)).strip()[:35]
        if not new_title:
            return None
        with connection(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if thread_id is not None:
                active = [dict(r) for r in conn.execute(
                    "SELECT * FROM conversations WHERE owner_id=? AND chat_id=? AND thread_id=? AND status='active' "
                    "ORDER BY updated_at DESC", (owner_id, chat_id, thread_id)).fetchall()]
            else:
                active = [dict(r) for r in conn.execute(
                    "SELECT * FROM conversations WHERE owner_id=? AND chat_id=? AND thread_id IS NULL AND status='active' "
                    "ORDER BY updated_at DESC", (owner_id, chat_id)).fetchall()]
            if not active:
                return None
            target_conv = None
            if target is not None:
                if str(target).strip().isdigit():
                    idx = int(str(target).strip()) - 1
                    if 0 <= idx < len(active):
                        target_conv = active[idx]
                else:
                    target_str = str(target).strip().lower()
                    target_conv = next((c for c in active if target_str in c['title'].lower() or target_str == c['id']), None)
            else:
                target_conv = next((c for c in active if c['is_current']), active[0])
            
            if not target_conv:
                return None
            
            conn.execute("UPDATE conversations SET title=? WHERE id=?", (new_title, target_conv['id']))
            target_conv['title'] = new_title
            return target_conv

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
