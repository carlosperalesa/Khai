import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.pixy.control import ControlStore, BudgetExceeded, InstanceLock
from app.mind.memory import MemoryEngine
from app.mind.state import StateManager
from app.mind.coordinator import Coordinator
from app.hand.hand import Hand
from app.pixy.pixy import PixyMind
from app.pixy.rules import Rules
from app.dreams.dreams import Dreams


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'KHAI_DB_PATH': str(self.root / 'openclaw.db')})
        self.env.start()
        self.memory = MemoryEngine(str(self.root / 'memory'))
        self.state = StateManager(str(self.root / 'memory'))
        self.store = ControlStore(self.state.db_path)
        self.llm = Mock()
        self.rules = Rules(self.state.db_path)
        self.events = Mock()
        self.dreams = Dreams(self.state.db_path, self.rules, self.events, memory=self.memory)
        self.manager = SimpleNamespace(
            state=self.state,
            memory=self.memory,
            telegram=Mock(),
            llm=self.llm,
            core_mind=Mock(),
            core_heart=Mock(),
            rules=self.rules,
            dreams=self.dreams,
            events=self.events,
        )
        pixy_audit = Mock(audit_artifact=Mock(return_value={'safe': True, 'requires_approval': False}))
        self.manager.hand = Hand(self.root / 'skills', pixy_audit, llm=self.llm, db_path=self.state.db_path)
        self.manager.pixy = PixyMind(self.manager)
        self.proxy = Mock()
        self.c = Coordinator(self.manager, self.proxy)
        self.manager.coordinator = self.c

    def tearDown(self):
        self.c._llm_executor.shutdown(wait=True)
        self.c._mailbox_executor.shutdown(wait=True)
        self.env.stop()
        self.tmp.cleanup()

    def task(self, **kwargs):
        return self.store.create('source:' + str(time.time_ns()), 'compute', **kwargs)

    def artifact(self, **kwargs):
        code = '# MODE: SAFE\nprint("result")'
        path, sha = self.manager.hand.snapshot(code)
        defaults = dict(
            state='READY',
            code=code,
            mode='SAFE',
            dependencies=[],
            artifact=path,
            artifact_hash=sha,
            authorized_hash=sha,
            manifest={'purpose': 'compute', 'capabilities': []},
        )
        defaults.update(kwargs)
        return self.task(**defaults)

    def delivered_question(self, task, kind, message_id=800):
        self.manager.telegram.user_id = 'owner'
        sid = self.c.conversations.session('owner', 'owner')
        task = self.store.update(task['id'], conversation_id=sid, kind='objective')
        qid = self.c.conversations.question(sid, kind, task['id'], task['revision'], task['state'])
        self.c.conversations.delivered(
            sid, 'fixture:' + task['id'], 'Pregunta enviada', task['id'], qid, [message_id]
        )
        return sid, qid

