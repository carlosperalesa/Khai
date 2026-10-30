from app.mind.coordinator import Coordinator
import tempfile
from pathlib import Path
import unittest
import threading
import time
"""One durable session, one routed operation, and replies bound to delivered questions."""
import itertools
import json
from types import SimpleNamespace
from unittest.mock import Mock

from tests.fixtures import Fixture
from app.mind.conversation import ConversationStore, current_turn
from app.mind.mind import Mind
from app.pixy.authorization import AuthorizationStore
from app.pixy.budget import current_goal_revision, StaleGoalRevision
from app.voice.voice import process_ordinary
from app.voice.mailbox import VoiceMailbox
from app.kernel.database import connection
from unittest.mock import patch


class ConversationFlowTests(Fixture):
    def setUp(self):
        super().setUp()
        self.manager.telegram.user_id = 'owner'
        self.ids = itertools.count(100)
        self.manager.telegram.send_message.side_effect = lambda *_a, **_k: {
            'ok': True, 'result': {'message_id': next(self.ids)}}
        self.sid = self.c.conversations.session('owner', 'owner')
        self.manager.mind = Mind(self.llm, self.memory)
        self.manager.action_planner = None
        self.manager.skill_selector = None

    def tearDown(self):
        self.c._llm_executor.shutdown(wait=True)
        self.c._mailbox_executor.shutdown(wait=True)
        super().tearDown()

    def goal(self, state='WAITING_INPUT_CONFIRMATION', **kwargs):
        task = self.store.create('goal:' + str(next(self.ids)), 'Consultar clima en Santiago',
            state=state, conversation_id=self.sid, kind='objective',
            manifest={'purpose': 'Consultar clima en Santiago', 'capabilities': []},
            params={'city': 'Santiago'}, **kwargs)
        if state == 'WAITING_INPUT_CONFIRMATION':
            task = self.store.update(task['id'], input_confirmation_signature=self.c._input_signature(task))
        return task

    def ask(self, task, text='¿Confirmas los datos?'):
        token = current_goal_revision.set((task['id'], task.get('goal_revision', 0)))
        try:
            self.c.notify(text)
        finally:
            current_goal_revision.reset(token)
        self.c._mailbox_job.result(timeout=5)
        return next(q for q in self.c.conversations.pending(self.sid) if q['subject_id'] == task['id'])

    def receive(self, source, text, reply_to=None):
        self.c.conversations.begin(source, 'trace-' + source, text, 'owner', 'owner', reply_to=reply_to)
        process_ordinary(self.manager, source, 'trace-' + source, text)

    def advance_turn(self, source):
        task = next(t for t in self.store.all() if t['source'] == source)
        self.c.advance(task)
        return self.store.get(task['id'])

    def test_confirmation_is_consumed_without_creating_another_goal(self):
        task = self.goal(); self.ask(task)
        self.receive('owner:yes', 'sí')
        self.assertEqual(self.store.get(task['id'])['state'], 'READY')
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(self.c.conversations.turn('owner:yes')['status'], 'completed')
        self.receive('owner:yes', 'sí')
        self.assertEqual(len(self.store.all()), 1)

    def test_last_delivered_question_wins_across_question_kinds(self):
        inputs = self.goal(); self.ask(inputs)
        creation = self.goal('WAITING_CREATION_APPROVAL'); self.ask(creation, '¿Creo la capacidad?')
        self.receive('owner:create', 'y')
        self.assertEqual(self.store.get(inputs['id'])['state'], 'WAITING_INPUT_CONFIRMATION')
        self.assertEqual(self.store.get(creation['id'])['state'], 'SCOPE_PLANNING')

    def test_latest_pixy_authorization_does_not_confirm_older_inputs(self):
        task = self.goal(); self.ask(task)
        self.manager.pixy.authorizations = AuthorizationStore(self.state.db_path)
        self.manager.heart = SimpleNamespace(recovery=Mock())
        auth = self.manager.pixy.authorizations.issue('case-20261029' + 'a' * 8,
            ['RestartCore'], 'core', 'f' * 64, 'owner:diagnose')
        context = {'conversation_id': self.sid, 'source': 'owner:diagnose'}
        token = current_turn.set(context)
        try:
            self.c.notify('Pixy propone una recuperación. ¿Apruebas?')
        finally:
            current_turn.reset(token)
        self.c._mailbox_job.result(timeout=5)
        self.receive('owner:recovery', 'y')
        self.assertEqual(self.store.get(task['id'])['state'], 'WAITING_INPUT_CONFIRMATION')
        self.assertEqual(self.manager.pixy.authorizations.get(auth['authorization_id'])['status'], 'approved')

    def test_queued_question_cannot_authorize_execution(self):
        task = self.goal()
        self.c.conversations.question(self.sid, 'inputs', task['id'], task['revision'], task['state'])
        self.receive('owner:undelivered', 'sí')
        self.assertEqual(self.store.get(task['id'])['state'], 'WAITING_INPUT_CONFIRMATION')

    def test_reply_to_message_selects_exact_question(self):
        older = self.goal(); question = self.ask(older)
        newer = self.goal('WAITING_CREATION_APPROVAL'); self.ask(newer)
        self.receive('owner:explicit', 'sí', reply_to=json.loads(question['message_ids'])[0])
        self.assertEqual(self.store.get(older['id'])['state'], 'READY')
        self.assertEqual(self.store.get(newer['id'])['state'], 'WAITING_CREATION_APPROVAL')

    def test_old_question_cannot_apply_to_changed_values(self):
        task = self.goal(); self.ask(task)
        self.store.update(task['id'], params={'city': 'Valparaíso'})
        self.receive('owner:stale', 'sí')
        self.assertEqual(self.store.get(task['id'])['state'], 'WAITING_INPUT_CONFIRMATION')

    def test_yes_reply_to_unregistered_message_does_not_confirm_another_question(self):
        task = self.goal(); self.ask(task)
        self.receive('owner:unregistered-reply', 'sí', reply_to=999999)
        self.assertEqual(self.store.get(task['id'])['state'], 'WAITING_INPUT_CONFIRMATION')
        self.assertEqual(len(self.store.all()), 1)

    def test_execution_approval_accepts_yes_without_retired_commands(self):
        task = self.artifact(conversation_id=self.sid, kind='objective')
        token = current_goal_revision.set((task['id'], 0))
        try:
            self.c._wait_approval(task, 'Ejecutar el artefacto revisado')
        finally:
            current_goal_revision.reset(token)
        self.c._mailbox_job.result(timeout=5)
        self.receive('owner:execute', 'sí')
        approved = self.store.get(task['id'])
        self.assertEqual(approved['state'], 'READY')
        self.assertEqual(approved['authorized_hash'], approved['artifact_hash'])

    def test_task_recovery_uses_pixy_question_once(self):
        self.manager.pixy.authorizations = AuthorizationStore(self.state.db_path)
        recovery = Mock()
        self.manager.heart = SimpleNamespace(recovery=recovery)
        case = {'case_id': 'case-20261029' + 'b' * 8, 'state': 'SOLUTION_PROPOSED',
                'plan_hash': 'e' * 64, 'diagnosis': {},
                'plan': {'target': 'task', 'capabilities': ['TaskRemediate'],
                         'requires_approval': True, 'summary': 'Reintentar este objetivo'}}
        recovery.get.side_effect = lambda _: dict(case)
        recovery.await_approval.side_effect = lambda *_: case.update(state='WAITING_USER_APPROVAL')
        task = self.goal('DIAGNOSING', recovery_case_id=case['case_id'])
        self.c.advance(task)
        self.c._mailbox_job.result(timeout=5)
        self.c.advance(self.store.get(task['id']))
        recovery.await_approval.assert_called_once()
        question = self.c.conversations.pending(self.sid)[0]
        self.assertEqual(question['kind'], 'authorization')
        self.receive('owner:task-recovery', 'sí')
        self.assertEqual(self.manager.pixy.authorizations.get(question['subject_id'])['status'], 'approved')
        recovery.receive_decision.assert_called_once()

    def test_chat_during_generation_keeps_goal_and_uses_one_model_call(self):
        task = self.goal('GENERATING')
        self.llm.generate_content.return_value = '{"reply":"Estoy preparando la capacidad solicitada.","tool_calls":[]}'
        self.manager.action_planner = Mock()
        self.receive('owner:chat', '¿Qué estás preparando?')
        self.advance_turn('owner:chat')
        self.assertEqual(self.store.get(task['id'])['state'], 'GENERATING')
        self.manager.action_planner.propose.assert_not_called()
        self.llm.generate_content.assert_called_once()

    def test_new_goal_does_not_change_pending_goal(self):
        original = self.goal(); self.ask(original)
        self.llm.generate_content.side_effect = [json.dumps({'tool_calls': [
            {'name': 'start_task', 'arguments': {'goal': 'Convertir el archivo PDF a texto'}}]}),
            '{"purpose":"Convertir el archivo PDF a texto","capabilities":["FileSystem"]}']
        self.receive('owner:pdf', 'Además convierte este PDF a texto')
        created = self.advance_turn('owner:pdf')
        self.assertEqual(self.store.get(original['id'])['state'], 'WAITING_INPUT_CONFIRMATION')
        self.assertEqual(created['state'], 'WAITING_CREATION_APPROVAL')
        self.assertEqual(created['conversation_id'], original['conversation_id'])
        self.assertNotEqual(created['id'], original['id'])
        receipt = json.loads(self.c.conversations.turn('owner:pdf')['receipt'])
        self.assertEqual(receipt['tool_call']['name'], 'start_task')

    def test_change_during_generation_keeps_id_and_rejects_stale_work(self):
        task = self.goal('GENERATING', goal_revision=2)
        self.llm.generate_content.return_value = json.dumps({'tool_calls': [{
            'name': 'revise_task', 'arguments': {'task_id': task['id'], 'goal_revision': 2,
                                              'change': 'Mejor para Valparaíso'}}]})
        self.receive('owner:change', 'Mejor para Valparaíso')
        self.advance_turn('owner:change')
        changed = self.store.get(task['id'])
        self.assertEqual(changed['state'], 'RESOLVING_SCOPE')
        self.assertEqual(changed['goal_revision'], 3)
        self.assertEqual(changed['scope_answers'][-1]['source'], 'owner:change')
        self.assertEqual(changed['max_generations'], task['max_generations'] + 4)
        self.assertEqual(changed['max_llm_calls'], task['max_llm_calls'] + 16)
        self.assertEqual(len([t for t in self.store.all() if t.get('kind') == 'objective']), 1)
        token = current_goal_revision.set((task['id'], 2))
        try:
            with self.assertRaises(StaleGoalRevision):
                self.store.update(task['id'], state='READY', authorized_hash='obsolete')
        finally:
            current_goal_revision.reset(token)
        self.assertEqual(self.store.get(task['id'])['state'], 'RESOLVING_SCOPE')

    def test_session_and_delivered_history_survive_reopening(self):
        self.llm.generate_content.return_value = '{"reply":"Hola.","tool_calls":[]}'
        self.receive('owner:hello', 'Hola'); self.advance_turn('owner:hello')
        self.c._mailbox_job.result(timeout=5)
        reopened = ConversationStore(self.state.db_path)
        self.assertEqual(reopened.session('owner', 'owner'), self.sid)
        self.assertEqual([(r['role'], r['content']) for r in reopened.history(self.sid)],
                         [('user', 'Hola'), ('assistant', 'Hola.')])

    def test_question_reconciles_delivered_outbox_after_restart(self):
        mailbox = VoiceMailbox(self.state.db_path)
        task = self.goal()
        qid = self.c.conversations.question(self.sid, 'inputs', task['id'], task['revision'], task['state'])
        did = mailbox.enqueue('¿Confirmas?', {'conversation_id': self.sid, 'task_id': task['id'], 'question_id': qid})
        self.c.conversations.attach(qid, did)
        mailbox.deliver_one(lambda _: {'result': {'message_id': 900}})
        reopened = ConversationStore(self.state.db_path)
        self.assertEqual(reopened.pending(self.sid)[0]['id'], qid)
        self.assertEqual(json.loads(reopened.pending(self.sid)[0]['message_ids']), [900])

    def test_live_outbox_delivery_opens_question_before_owner_confirmation(self):
        self.manager.voice_mailbox = VoiceMailbox(self.state.db_path)
        self.manager.voice_mailbox.on_delivered = self.c._notification_delivered
        task = self.goal()
        question = self.ask(task)
        self.assertTrue(json.loads(question['message_ids']))
        self.assertEqual(self.manager.voice_mailbox.pending_counts()['outbox'], {'delivered': 1})
        self.receive('owner:live-confirmation', 'sí')
        self.assertEqual(self.store.get(task['id'])['state'], 'READY')

    def test_mode_transition_banner_is_preserved_in_delivered_history(self):
        self.manager.dolphin = Mock()
        self.manager.dolphin.get_active_mode.return_value = 'low'
        self.manager.dolphin.consume_transition_banner.return_value = '[Mode = low]\n'
        self.llm.generate_content.return_value = '{"reply":"Hola.","tool_calls":[]}'
        self.receive('owner:mode-banner', 'Hola')
        self.advance_turn('owner:mode-banner')
        self.c._mailbox_job.result(timeout=5)
        self.assertEqual(self.c.conversations.history(self.sid)[-1]['content'], '[Mode = low]\nHola.')

    def test_status_during_creation_reports_approved_provider_without_claiming_execution(self):
        task = self.goal('GENERATING', services=[{'service_id': 'weather', 'operation': 'forecast', 'version': 'v1'}])
        self.manager.voice_services = SimpleNamespace(active_services=lambda: {'weather': {'operations': {
            'forecast': {'version': 'v1', 'url': 'https://api.example.org/forecast?hidden=fixture'}}}})
        self.llm.generate_content.return_value = json.dumps({'tool_calls': [{
            'name': 'get_task_status', 'arguments': {'task_id': task['id']}}]})
        self.receive('owner:provider', '¿Qué proveedor estás usando?')
        routed = self.advance_turn('owner:provider')
        self.assertIn('api.example.org', routed['tool_result'])
        self.assertIn('Ejecuciones registradas: 0', routed['tool_result'])
        self.assertNotIn('hidden', routed['tool_result'])
        self.assertEqual(self.store.get(task['id'])['state'], 'GENERATING')

    def test_uncertain_new_question_cannot_redirect_yes_to_older_goal(self):
        old = self.goal(); self.ask(old)
        newer = self.goal('WAITING_CREATION_APPROVAL')
        mailbox = VoiceMailbox(self.state.db_path)
        qid = self.c.conversations.question(self.sid, 'creation', newer['id'], newer['revision'], newer['state'])
        did = mailbox.enqueue('¿Creo otra capacidad?', {'conversation_id': self.sid, 'question_id': qid})
        self.c.conversations.attach(qid, did)
        with self.assertRaises(RuntimeError):
            mailbox.deliver_one(lambda _: (_ for _ in ()).throw(RuntimeError('entrega incierta')))
        self.receive('owner:uncertain', 'y')
        self.assertEqual(self.store.get(old['id'])['state'], 'WAITING_INPUT_CONFIRMATION')
        self.assertEqual(self.store.get(newer['id'])['state'], 'WAITING_CREATION_APPROVAL')

    def test_generation_finishing_after_change_never_reaches_hand_snapshot(self):
        task = self.goal('GENERATING', goal_revision=2)
        def generated(*_):
            self.c.revise_goal(self.store.get(task['id']), {
                'goal_revision': 2, 'change': 'Mejor para Valparaíso'}, 'owner:during-generation')
            return '# MODE: SAFE\nprint("Santiago")', 'SAFE', []
        with patch.object(self.manager.hand, 'generate', side_effect=generated), \
                patch.object(self.manager.hand, 'snapshot') as snapshot:
            self.c._run_task(task)
            snapshot.assert_not_called()
        self.assertEqual(self.store.get(task['id'])['state'], 'RESOLVING_SCOPE')
        self.assertEqual(self.store.get(task['id'])['goal_revision'], 3)

    def test_change_before_creation_consent_does_not_authorize_generation(self):
        task = self.goal('WAITING_CREATION_APPROVAL')
        self.c.revise_goal(task, {'goal_revision': 0, 'change': 'Para Valparaíso'}, 'owner:before-consent')
        self.c.requirements.resolve = Mock(return_value={
            'purpose': 'Consultar clima en Valparaíso', 'required_output': ['pronóstico'],
            'params': {'city': 'Valparaíso'}, 'missing_questions': []})
        self.c.advance(self.store.get(task['id']))
        current = self.store.get(task['id'])
        self.assertEqual(current['state'], 'ROUTING')
        self.assertFalse(current.get('creation_consent'))

    def test_model_cannot_revise_another_chat(self):
        foreign_sid = self.c.conversations.session('owner', 'another-chat')
        task = self.store.create('foreign-goal', 'Otro objetivo', state='GENERATING',
                                 conversation_id=foreign_sid, kind='objective')
        self.llm.generate_content.return_value = json.dumps({'tool_calls': [{
            'name': 'revise_task', 'arguments': {'task_id': task['id'],
                                               'goal_revision': 0, 'change': 'Otro cambio'}}]})
        self.receive('owner:foreign', 'Cambia ese objetivo')
        self.advance_turn('owner:foreign')
        self.assertEqual(self.store.get(task['id'])['state'], 'GENERATING')
        receipt = json.loads(self.c.conversations.turn('owner:foreign')['receipt'])
        self.assertEqual(receipt['status'], 'rejected')

    def test_tool_answer_uses_literal_owner_input_and_registers_followup_question(self):
        task = self.goal('UNVERIFIED', execution_id='run_' + 'd' * 32)
        question = self.ask(task, '¿Es correcto el resultado?')
        self.llm.generate_content.return_value = json.dumps({'tool_calls': [{
            'name': 'answer_question', 'arguments': {'question_id': question['id'], 'answer': 'no.'}}]})
        # Exercise the structured tool path rather than the deterministic bare yes/no path.
        source = 'owner:tool-answer'
        turn = self.c.conversations.begin(source, 'trace-tool', 'no.', 'owner', 'owner')
        token = current_turn.set(turn)
        try:
            routed = self.c.intake(source, 'no.')
        finally:
            current_turn.reset(token)
        self.c.advance(routed)
        self.c._mailbox_job.result(timeout=5)
        pending = self.c.conversations.pending(self.sid)
        self.assertEqual(self.store.get(task['id'])['state'], 'WAITING_FEEDBACK')
        self.assertTrue(any(q['subject_id'] == task['id'] and q['state'] == 'WAITING_FEEDBACK' for q in pending))


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
        self.c._supervise_agent = Mock()

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


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.started = threading.Event()
        self.release = threading.Event()

        def blocked_generate(*_args):
            self.started.set()
            self.release.wait(5)
            return "# MODE: SAFE\nprint('ok')", "SAFE", []

        manager = SimpleNamespace(
            state=SimpleNamespace(db_path=str(root / "control.sqlite3")),
            data_dir=root,
            runtime=Mock(),
            telegram=Mock(),
            memory=Mock(get_active_hypotheses=Mock(return_value=[])),
            hand=Mock(generate=blocked_generate, snapshot=Mock(return_value=(str(root / "a.py"), "a" * 64))),
            rules=Mock(active=Mock(return_value=[])),
            pixy=Mock(preflight=Mock(return_value={"safe": True})),
            mind=Mock(),
            heart=SimpleNamespace(recovery=None),
        )
        self.coordinator = Coordinator(manager)
        self.task = self.coordinator.store.create("scheduler-source", "goal")
        self.coordinator.store.update(
            self.task["id"], state="GENERATING",
            manifest={"purpose": "goal", "capabilities": []},
            deadline=time.time() + 300,
        )

    def tearDown(self):
        self.release.set()
        self.coordinator._llm_executor.shutdown(wait=True, cancel_futures=True)
        self.coordinator._mailbox_executor.shutdown(wait=True, cancel_futures=True)
        self.tmp.cleanup()

    def test_background_tick_does_not_wait_for_llm_job_and_cancel_wins(self):
        started = time.monotonic()
        self.coordinator.tick(background=True)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(self.started.wait(1))

        cancelled = self.coordinator.cancel(self.task["id"], "cancel-command")
        self.assertEqual(cancelled["state"], "CANCELLED")
        self.release.set()

    def test_configured_concurrency_is_bounded(self):
        self.assertEqual(self.coordinator.llm_concurrency, 2)


if __name__ == "__main__":
    unittest.main()


if __name__ == '__main__':
    unittest.main()
