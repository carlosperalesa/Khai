import unittest
from unittest.mock import MagicMock, Mock

from tests.test_control import Fixture
from app.kernel.control_plane import ControlPlane, PixyPort
from app.mind.conversation import ReplyResult
from app.voice.gateway.dolphin_controller import DolphinController
from app.voice.voice import command


class TestIncidentFixes(Fixture):
    def setUp(self):
        super().setUp()
        self.plane = ControlPlane(self.state.db_path)
        self.manager.pixy.authorizations = self.plane.ports.pixy
        self.sid = self.c.conversations.session('owner', 'owner')

    def tearDown(self):
        self.c._llm_executor.shutdown(wait=True)
        self.c._mailbox_executor.shutdown(wait=True)
        super().tearDown()

    def test_pixy_port_get_and_get_by_trace(self):
        """A.1: PixyPort must expose get and get_by_trace from underlying store."""
        pixy_port = self.plane.ports.pixy
        self.assertIsInstance(pixy_port, PixyPort)
        
        # Issue an authorization with valid case_id
        case_id = 'case-20261029' + 'a' * 8
        auth = pixy_port.issue(case_id, ['DockerRestart'], 'core', 'hash123', 'telegram:test')
        auth_id = auth['authorization_id']
        self.assertTrue(auth_id.startswith('auth_'))

        # Fetch via PixyPort.get
        fetched = pixy_port.get(auth_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched['authorization_id'], auth_id)
        self.assertEqual(fetched['status'], 'pending')
        self.assertEqual(fetched['plan_hash'], 'hash123')

        # Fetch via PixyPort.get_by_trace
        by_trace = pixy_port.get_by_trace(auth.get('trace_id') or case_id)
        self.assertIsNotNone(by_trace)
        self.assertEqual(by_trace['authorization_id'], auth_id)

        # Non-existent auth returns None
        self.assertIsNone(pixy_port.get('auth_nonexistent'))

    def test_answer_question_with_pixy_port(self):
        """Verify Coordinator.answer_question succeeds when manager.pixy.authorizations is PixyPort."""
        self.manager.pixy.direct_response = Mock(return_value='Operación aprobada por Pixy')

        case_id = 'case-20261029' + 'b' * 8
        auth = self.plane.ports.pixy.issue(case_id, ['DockerRestart'], 'core', 'hash_abc', 'telegram:123')
        qid = self.c.conversations.question(self.sid, 'authorization', auth['authorization_id'], 'hash_abc', 'pending')
        self.c.conversations.delivered(self.sid, 'deliv-test-01', '¿Apruebas?', question_id=qid)

        question = self.c.conversations.pending(self.sid)[0]
        # User answers 'Y'
        res = self.c.answer_question('telegram:123', question, 'Y', self.sid)
        self.assertIsInstance(res, ReplyResult)
        self.assertTrue(res.handled)
        self.assertEqual(res.text, 'Operación aprobada por Pixy')

    def test_dolphin_aliases(self):
        """B.1: DolphinController accepts aliases 'h', 'm', 'l'."""
        ctrl = DolphinController(data_dir=str(self.root))
        ctrl.stop = MagicMock(return_value='[Mode = low]')
        ctrl.start = MagicMock(side_effect=lambda mode: f"[Mode = {mode}]")

        self.assertEqual(ctrl.set_mode('l'), '[Mode = low]')
        self.assertEqual(ctrl.set_mode('m'), '[Mode = mid]')
        self.assertEqual(ctrl.set_mode('h'), '[Mode = high]')

    def test_command_invalid_mode_handling(self):
        """B.1: command() raises ValueError on invalid mode which voice.py catches."""
        self.manager.dolphin = DolphinController(data_dir=str(self.root))
        
        with self.assertRaises(ValueError):
            command(self.manager, 'telegram:1', '/mode invalid_foo')

    def test_intake_debounce_coalescence(self):
        """C.3: intake() coalesces immediate consecutive messages into the same ROUTING task."""
        self.c.conversations.begin('telegram:turn1', 'trace_1', 'Primera parte', 'owner', 'owner')

        # First intake creates task in ROUTING
        task1 = self.c.intake('telegram:turn1', 'Primera parte', trace_id='trace_1')
        self.assertEqual(task1['request'], 'Primera parte')

        # Rapid second intake (same conversation, < 3s, not direct)
        self.c.conversations.begin('telegram:turn2', 'trace_2', 'Segunda parte', 'owner', 'owner')
        task2 = self.c.intake('telegram:turn2', 'Segunda parte', trace_id='trace_2')

        # Coalesced into task1
        self.assertEqual(task2['id'], task1['id'])
        self.assertIn('Primera parte\n\nSegunda parte', task2['request'])

    def test_tick_sequential_fifo_by_conversation(self):
        """C.1: Coordinator.tick() pauses second ROUTING task while first is in-flight."""
        t1 = self.store.create('telegram:t1', 'Tarea 1', conversation_id=self.sid, state='RUNNING')
        t2 = self.store.create('telegram:t2', 'Tarea 2', conversation_id=self.sid, state='ROUTING')

        # Mock _run_task to track execution
        executed = []
        self.c._run_task = lambda t: executed.append(t['id'])
        self.c._supervise_agent = MagicMock()

        # Run tick
        self.c.tick(background=False)

        # t2 should NOT have run because t1 is in RUNNING for the same conversation
        self.assertNotIn(t2['id'], executed)

        # Transition t1 to COMPLETED
        self.store.update(t1['id'], expected={'RUNNING'}, state='COMPLETED')

        # Run tick again
        self.c.tick(background=False)

        # Now t2 should have been admitted and executed
        self.assertIn(t2['id'], executed)


if __name__ == '__main__':
    unittest.main()
