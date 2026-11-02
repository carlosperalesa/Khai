"""Regressions from production: topics, durable origins, deletion and goal coverage."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.fixtures import Fixture
from app.kernel.database import connection
from app.mind.conversation import ConversationStore, current_turn
from app.mind.mind import Mind
from app.mind.quality import assess_result
from app.mind.brain.controller import ModelController
from app.mind.brain.transitions import TransitionManager
from app.voice.mailbox import VoiceMailbox
from app.voice.voice import command


class ConversationIntegrityTests(Fixture):
    def proposal(self, source='proposal', thread=53):
        self.manager.telegram.user_id = 'owner'
        sid = self.c.conversations.session('owner', 'owner', thread)
        task = self.store.create(source, 'consulta autorizable', conversation_id=sid,
                                 kind='objective', state='WAITING_CREATION_APPROVAL')
        qid = self.c.conversations.question(sid, 'creation', task['id'], task['revision'], task['state'])
        self.c.conversations.delivered(sid, source + ':delivery', '¿Autorizas la capacidad?', task['id'], qid, [221])
        return sid, task, qid

    def test_topic_root_is_not_a_reply_to_an_unrelated_message(self):
        sid, task, qid = self.proposal()
        with patch.object(self.c, 'answer_question', return_value='resolved') as answer:
            self.assertEqual(self.c.handle_reply('answer', 'Y', sid, 53), 'resolved')
        self.assertEqual(answer.call_args.args[1]['id'], qid)

    def test_real_reply_to_other_message_never_approves_latest(self):
        sid, _, _ = self.proposal()
        with patch.object(self.c, 'answer_question') as answer:
            self.c.handle_reply('answer', 'Y', sid, 999)
        answer.assert_not_called()

    def test_multiple_decisions_require_reference(self):
        sid, _, _ = self.proposal()
        self.proposal('second')
        with patch.object(self.c, 'answer_question') as answer:
            result = self.c.handle_reply('answer', 'Y', sid, 53)
        answer.assert_not_called()
        self.assertIn('varias', result.text)

    def test_task_expiry_closes_question_in_same_transaction(self):
        sid, task, qid = self.proposal()
        self.store.update(task['id'], state='EXHAUSTED')
        self.assertEqual(self.c.conversations.get_question(qid, sid)['status'], 'stale')
        self.assertEqual(self.c.conversations.journal.get(self.c.journal.cycle_id('question', qid))['status'], 'cancelled')

    def test_classifier_receives_actual_delivered_question(self):
        sid, _, _ = self.proposal()
        class LLM:
            prompt = ''
            def generate_content(inner, prompt):
                inner.prompt = prompt
                return '{"intent":"approve"}'
        self.manager.llm = LLM()
        self.c.classify_reply_intent(self.c.conversations.pending(sid)[0], 'me parece correcto')
        self.assertIn('¿Autorizas la capacidad?', self.manager.llm.prompt)

    def test_report_without_task_reaches_diagnostic_handler(self):
        sid = self.c.conversations.session('owner', 'owner')
        self.c.conversations.begin('complaint', 'trace-complaint', 'eso está mal', 'owner', 'owner')
        task = self.store.create('complaint', 'eso está mal', conversation_id=sid, kind='turn')
        with patch.object(self.c, 'report_cognitive_issue', return_value='registrado') as report:
            self.assertEqual(self.c.dispatch_tool(task, {'name':'report_issue', 'arguments':{
                'issue':'eso está mal', 'task_id':None, 'skill_name':None}}), 'registrado')
        report.assert_called_once()

    def test_delete_cancels_work_and_suppresses_late_history(self):
        sid, task, _ = self.proposal(thread=None)
        result = command(self.manager, 'delete', '/delete')
        self.assertIn('Chat eliminado', result['text'])
        self.assertEqual(self.store.get(task['id'])['state'], 'CANCELLED')
        self.c.conversations.delivered(sid, 'late', 'terminado')
        self.assertEqual(self.c.conversations.history(sid), [])
        self.assertEqual(self.c.conversations.get_conversation(sid)['status'], 'deleted')

    def test_cross_conversation_context_preserves_owner_and_provenance(self):
        a = self.c.conversations.begin('a', 'ta', 'pronóstico Santiago', 'owner', 'owner')
        b = self.c.conversations.new_conversation('owner', 'owner')
        self.c.conversations.begin('private', 'tp', 'pronóstico Santiago secreto', 'other', 'other')
        found = self.c.conversations.shared_context(b['id'], 'pronóstico Santiago')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['conversation_id'], a['conversation_id'])

    def test_preference_and_main_goal_survive_same_turn(self):
        message = 'Vivo en Santiago, recuérdalo y consulta el pronóstico'
        llm = Mock(generate_content=Mock(return_value=json.dumps({
            'reply':'', 'preference_updates':[{'key':'city','value':'Santiago','source_text':'Vivo en Santiago'}],
            'tool_calls':[{'name':'start_task','arguments':{'goal':'Consulta el pronóstico en Santiago'}}]})))
        intent = Mind(llm, self.memory).process_message(message)
        self.assertEqual(intent['action'], 'ACTION_REQUIRED')
        self.assertEqual(self.memory.get_user_profile()['city'], 'Santiago')

    def test_unattributed_preference_is_not_saved(self):
        llm = Mock(generate_content=Mock(return_value=json.dumps({'reply':'hola', 'tool_calls':[],
            'preference_updates':[{'key':'city','value':'Lima','source_text':'Vivo en Lima'}]})))
        before = self.memory.get_user_profile()
        self.assertEqual(Mind(llm, self.memory).process_message('hola')['action'], 'ROUTING_ERROR')
        self.assertEqual(self.memory.get_user_profile(), before)

    def test_current_weather_does_not_pass_forecast_coverage(self):
        llm = Mock(generate_content=Mock(return_value=json.dumps({
            'satisfied':False,'missing':['pronóstico futuro','viento'],'reason':'Solo contiene temperatura actual'})))
        ok, evidence = assess_result(llm, '¿Va a llover y habrá viento?', {'temperature':16})
        self.assertFalse(ok)
        self.assertEqual(evidence['missing'], ['pronóstico futuro','viento'])


class DurableModeTests(unittest.TestCase):
    def test_ready_notice_is_durable_before_vm_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            conversations = ConversationStore(controller.journal.path)
            turn = conversations.begin('mode-input', 'trace-mode', '/mode nano', 'owner', 'owner', thread_id=53)
            mailbox = VoiceMailbox(controller.journal.path)
            manager = TransitionManager(controller, lambda: True)
            manager.mailbox = mailbox
            observed = []
            def stop():
                with connection(manager.path) as db:
                    observed.extend(dict(r) for r in db.execute('SELECT * FROM voice_outbox'))
            token = current_turn.set(turn)
            try:
                with patch.object(controller, 'stop', side_effect=stop):
                    receipt = manager.request('nano')
                    manager._executor.shutdown(wait=True)
                ready = [r for r in observed if r['text'] == '[Modo nano conectado]']
                self.assertEqual(len(ready), 1)
                self.assertEqual(json.loads(ready[0]['metadata'])['conversation_id'], turn['conversation_id'])
                with connection(manager.path) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM voice_outbox WHERE text='[Modo nano conectado]'").fetchone()[0], 1)
                    self.assertEqual(db.execute('SELECT conversation_id FROM conversation_requests WHERE object_id=?', (receipt['id'],)).fetchone()[0], turn['conversation_id'])
            finally:
                current_turn.reset(token)
                manager.shutdown(wait=True)

    def test_plain_text_warmup_cannot_announce_functional_model(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            response = Mock(json=Mock(return_value={'response':'OK'}))
            with patch('app.mind.brain.controller.requests.post', return_value=response):
                with self.assertRaises(ValueError):
                    controller._warmup_model('high')
