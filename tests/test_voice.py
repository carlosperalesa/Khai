"""Voice organ tests: mailbox durability, transport facts/HTTP receipts, Telegram gateway, and conversation routing."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from tests.fixtures import Fixture
from app.mind.conversation import current_turn
from app.mind.coordinator import _typing_loop
from app.mind.verifier import verify_result
from app.voice.gateway.external_gateway import ExternalGateway
from app.voice.gateway.service_registry import validate_descriptor
from app.voice.gateway.telegram_gateway import TelegramHandler
from app.voice.help import HELP_BUTTONS, HELP_TEXT
from app.voice.mailbox import VoiceMailbox
from app.voice.voice import command, process_ordinary, urgent_command


class VoiceMailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'control.sqlite3'
        self.mailbox = VoiceMailbox(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_repeated_update_is_deduplicated_and_different_body_rejected(self):
        self.assertEqual(self.mailbox.accept('telegram:7', 'trace-7', '/make one'), 'pending')
        self.assertEqual(self.mailbox.accept('telegram:7', 'trace-other', '/make one'), 'pending')
        with self.assertRaises(ValueError):
            self.mailbox.accept('telegram:7', 'trace-7', '/make two')
        called = []
        self.mailbox.process_one(lambda source, trace, text: called.append((source, text)))
        self.assertEqual(called, [('telegram:7', '/make one')])
        self.assertEqual(self.mailbox.accept('telegram:7', 'trace-7', '/make one'), 'completed')

    def test_urgent_control_runs_while_normal_handler_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        self.mailbox.accept('telegram:1', 'trace-1', '/p diagnóstico lento')
        self.mailbox.accept('telegram:2', 'trace-2', '/cancel task-1', urgent=True)

        def slow(*_):
            entered.set()
            release.wait(2)

        thread = threading.Thread(target=lambda: self.mailbox.process_one(slow))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            actions = []
            self.assertTrue(self.mailbox.process_one(lambda source, *_: actions.append(source), urgent=True))
            self.assertEqual(actions, ['telegram:2'])
        finally:
            release.set()
            thread.join(2)

    def test_uncertain_handler_and_telegram_delivery_are_not_replayed(self):
        self.mailbox.accept('telegram:3', 'trace-3', '/publish asset')
        with self.assertRaises(RuntimeError):
            self.mailbox.process_one(lambda *_: (_ for _ in ()).throw(RuntimeError('lost reply')))
        self.assertFalse(self.mailbox.process_one(lambda *_: self.fail('replayed command')))
        delivery = self.mailbox.enqueue('resultado')
        with self.assertRaises(RuntimeError):
            self.mailbox.deliver_one(lambda _: (_ for _ in ()).throw(RuntimeError('lost receipt')))
        self.assertFalse(self.mailbox.deliver_one(lambda _: self.fail('replayed delivery')))
        self.assertEqual(self.mailbox.pending_counts()['outbox']['unknown'], 1)
        self.assertEqual(self.mailbox.pending_counts()['inbox']['unknown'], 1)
        self.assertTrue(delivery.startswith('delivery-'))


class HttpReceiptTests(unittest.TestCase):
    def test_failed_write_is_not_recorded_as_completed(self):
        contract = {'kind': 'create', 'identity': None, 'correction': None,
                    'summary': {'name': 'Envío de correo', 'context': 'Correo solicitado'}}
        with tempfile.TemporaryDirectory() as folder:
            registry = Mock()
            registry.active_services.return_value = {'mail': {'operations': {
                'send': {'version': 'v1', 'effect': 'write', 'effect_contract': contract}}}}
            gateway = ExternalGateway(Path(folder) / 'absent.yaml', Mock(),
                                      db=Path(folder) / 'voice.sqlite3', registry=registry, execution_authority=Mock())
            gateway.request = Mock(side_effect=RuntimeError('provider unavailable'))
            task = {'id': 'task_' + 'a' * 32, 'execution_id': 'run_' + 'b' * 32,
                    'deadline': time.time() + 60}
            reply = gateway._mailbox_request(task, 'send-1', {
                'service': 'mail', 'operation': 'send', 'params': {'to': 'test@example.org'}})
            receipt = gateway.technical_receipts(task['id'], task['execution_id'], [
                {'service_id': 'mail', 'operation': 'send', 'version': 'v1'}])[0]
            self.assertEqual(reply, {'error': 'RuntimeError'})
            self.assertEqual(receipt['status'], 'uncertain')

    def test_declared_write_identity_and_correction_guard(self):
        contract = {'kind': 'create', 'identity': {'source': 'response', 'field': 'id'},
                    'summary': {'name': 'Nota', 'context': 'Nota creada en Notes'},
                    'correction': {'service_id': 'notes', 'operation': 'update',
                                   'version': 'v1', 'identity_parameter': 'id',
                                   'questions': {'body': {
                                       'description': 'Contenido corregido de la nota',
                                       'question': '¿Qué debería decir la nota?'}}}}
        descriptor = {'service_id': 'notes', 'operation': 'create', 'version': 'v1',
                      'url': 'https://example.org/notes', 'method': 'POST',
                      'parameters': ['body'], 'content_types': ['application/json'],
                      'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
                      'effect': 'write', 'credential_ref': None,
                      'credential_header': 'Authorization', 'credential_prefix': '',
                      'effect_contract': contract}
        self.assertEqual(validate_descriptor(descriptor), descriptor)
        with self.assertRaises(ValueError):
            validate_descriptor({**descriptor, 'effect_contract': None})
        with self.assertRaises(ValueError):
            validate_descriptor({**descriptor, 'effect_contract': {
                **contract, 'summary': {'name': 'Nota', 'context': 'token=secreto'}}})
        with self.assertRaises(ValueError):
            validate_descriptor({**descriptor, 'effect_contract': {
                **contract, 'correction': {
                    'service_id': 'notes', 'operation': 'update',
                    'version': 'v1', 'identity_parameter': 'id'}}})
        with tempfile.TemporaryDirectory() as folder:
            registry = Mock()
            registry.active_services.return_value = {'notes': {'operations': {
                'create': {'version': 'v1', 'effect': 'write', 'effect_contract': contract},
                'update': {'version': 'v1', 'effect': 'write',
                           'effect_contract': {'kind': 'update',
                                               'identity': {'source': 'request', 'field': 'id'},
                                               'summary': {'name': 'Nota', 'context': 'Nota actualizada en Notes'},
                                               'correction': contract['correction']}}}}}
            gateway = ExternalGateway(Path(folder) / 'absent.yaml', Mock(),
                                      db=Path(folder) / 'voice.sqlite3', registry=registry, execution_authority=Mock())
            gateway.request = Mock(return_value={
                'content_type': 'application/json', 'body': b'{"id":"note-1"}'})
            task = {'id': 'task_' + 'a' * 32, 'execution_id': 'run_' + 'b' * 32,
                    'deadline': time.time() + 60}
            gateway._mailbox_request(task, 'create', {
                'service': 'notes', 'operation': 'create', 'params': {'body': 'draft'}})
            refs = [{'service_id': 'notes', 'operation': 'create', 'version': 'v1'}]
            receipt = gateway.technical_receipts(task['id'], task['execution_id'], refs)[0]
            self.assertEqual(receipt['resource_id'], 'note-1')
            self.assertEqual(receipt['effect_contract'], contract)
            self.assertEqual(receipt['summary'], contract['summary'])
            corrected = {**task, 'execution_id': 'run_' + 'c' * 32,
                         'effect_constraints': [{**contract['correction'], 'resource_id': 'note-1'}]}
            with self.assertRaises(PermissionError):
                gateway._mailbox_request(corrected, 'duplicate', {
                    'service': 'notes', 'operation': 'create', 'params': {'body': 'again'}})
            with self.assertRaises(PermissionError):
                gateway._mailbox_request(corrected, 'wrong-id', {
                    'service': 'notes', 'operation': 'update',
                    'params': {'id': 'note-2', 'body': 'fixed'}})
            gateway._mailbox_request(corrected, 'update', {
                'service': 'notes', 'operation': 'update',
                'params': {'id': 'note-1', 'body': 'fixed'}})
            gateway.request.assert_called_with('notes', 'update',
                                               {'id': 'note-1', 'body': 'fixed'}, expected_version='v1')

    def test_receipt_is_bound_to_execution_operation_and_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = Mock()
            registry.active_services.return_value = {'weather': {'operations': {
                'forecast': {'version': 'v1', 'effect': 'read'}}}}
            gateway = ExternalGateway(Path(folder) / 'absent.yaml', Mock(),
                                      db=Path(folder) / 'voice.sqlite3', registry=registry, execution_authority=Mock())
            gateway.request = Mock(return_value={
                'content_type': 'application/json',
                'body': b'{"temperature": 20, "rain": true}'})
            task = {'id': 'task_' + 'a' * 32, 'execution_id': 'run_' + 'b' * 32,
                    'deadline': time.time() + 60}
            gateway._mailbox_request(task, '.voice-1.request.json', {
                'service': 'weather', 'operation': 'forecast', 'params': {'city': 'Santiago'}})
            refs = [{'service_id': 'weather', 'operation': 'forecast', 'version': 'v1'}]
            facts = gateway.technical_receipts(task['id'], task['execution_id'], refs)
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0]['effect'], 'read')
            criterion = {'kind': 'http_schema', 'service_id': 'weather',
                         'operation': 'forecast', 'version': 'v1', 'schema': {
                             'type': 'object', 'properties': {
                                 'temperature': {'type': 'number'},
                                 'rain': {'type': 'boolean'}},
                             'required': ['temperature', 'rain'],
                             'additionalProperties': False}}
            result = {'status': 'finished', 'returncode': 0}
            success, evidence = verify_result(result, criterion, folder, facts,
                                              task['execution_id'])
            self.assertTrue(success)
            self.assertEqual(evidence['receipts'][0]['schema_valid'], True)
            self.assertIsNone(verify_result(result, criterion, folder, facts,
                                             'run_' + 'c' * 32)[0])
            wrong = dict(criterion, schema={**criterion['schema'],
                         'required': ['temperature', 'rain', 'missing']})
            self.assertFalse(verify_result(result, wrong, folder, facts,
                                           task['execution_id'])[0])

    def test_owner_authorized_duplicate_risk_allows_one_pinned_write(self):
        contract = {'kind': 'create', 'identity': None, 'correction': None,
                    'summary': {'name': 'Nota', 'context': 'Creación en Notes'}}
        with tempfile.TemporaryDirectory() as folder:
            registry = Mock()
            registry.active_services.return_value = {'notes': {'operations': {
                'create': {'version': 'v1', 'effect': 'write', 'effect_contract': contract},
                'other': {'version': 'v1', 'effect': 'write', 'effect_contract': contract}}}}
            gateway = ExternalGateway(Path(folder) / 'absent.yaml', Mock(),
                                      db=Path(folder) / 'voice.sqlite3', registry=registry, execution_authority=Mock())
            gateway.request = Mock(return_value={
                'content_type': 'application/json', 'body': b'{"id":"note-2"}'})
            task = {'id': 'task_' + 'a' * 32, 'execution_id': 'run_' + 'b' * 32,
                    'deadline': time.time() + 60, 'state': 'RUNNING',
                    'new_effect_authorization': {
                        'id': 'choice-1', 'task_id': 'task_' + 'a' * 32,
                        'service_id': 'notes', 'operation': 'create', 'version': 'v1'}}
            with self.assertRaises(PermissionError):
                gateway._mailbox_request(task, 'wrong-operation', {
                    'service': 'notes', 'operation': 'other', 'params': {}})
            gateway._mailbox_request(task, 'first', {
                'service': 'notes', 'operation': 'create', 'params': {}})
            self.assertEqual(gateway.technical_receipts(task['id'], task['execution_id'],
                [{'service_id': 'notes', 'operation': 'create', 'version': 'v1'}]
                )[0]['new_effect_authorization_id'], 'choice-1')
            with self.assertRaises(PermissionError):
                gateway._mailbox_request(task, 'second', {
                    'service': 'notes', 'operation': 'create', 'params': {}})
            self.assertEqual(gateway.request.call_count, 1)

    def test_batch_write_has_one_receipt_per_explicit_item_identity(self):
        batch = {'request_field': 'items', 'response_field': 'results',
                 'index_field': 'index', 'identity_field': 'id',
                 'status_field': 'status', 'success_value': 'created',
                 'failure_value': 'failed'}
        contract = {'kind': 'create', 'identity': None, 'batch': batch,
                    'summary': {'name': 'Nota', 'context': 'Lote de Notes'},
                    'correction': {'service_id': 'notes', 'operation': 'update',
                                   'version': 'v1', 'identity_parameter': 'id',
                                   'questions': {'body': {
                                       'description': 'Contenido corregido de la nota',
                                       'question': '¿Qué debería decir la nota?'}}}}
        descriptor = {'service_id': 'notes', 'operation': 'create_batch', 'version': 'v1',
                      'url': 'https://example.org/batch', 'method': 'POST',
                      'parameters': ['items'], 'content_types': ['application/json'],
                      'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
                      'effect': 'write', 'credential_ref': None,
                      'credential_header': 'Authorization', 'credential_prefix': '',
                      'effect_contract': contract}
        self.assertEqual(validate_descriptor(descriptor), descriptor)
        with self.assertRaises(ValueError):
            validate_descriptor({**descriptor, 'effect_contract': {
                **contract, 'batch': {**batch, 'request_field': 'undeclared'}}})
        with tempfile.TemporaryDirectory() as folder:
            registry = Mock()
            registry.active_services.return_value = {'notes': {'operations': {
                'create_batch': {'version': 'v1', 'effect': 'write',
                                 'effect_contract': contract}}}}
            gateway = ExternalGateway(Path(folder) / 'absent.yaml', Mock(),
                                      db=Path(folder) / 'voice.sqlite3', registry=registry, execution_authority=Mock())
            task = {'id': 'task_' + 'a' * 32, 'execution_id': 'run_' + 'b' * 32,
                    'deadline': time.time() + 60, 'state': 'RUNNING'}
            request = {'service': 'notes', 'operation': 'create_batch',
                       'params': {'items': [{'body': 'first'}, {'body': 'second'}]}}
            refs = [{'service_id': 'notes', 'operation': 'create_batch', 'version': 'v1'}]
            gateway.request = Mock(return_value={'content_type': 'application/json',
                'body': b'{"results":[{"index":1,"status":"created","id":"note-2"},'
                        b'{"index":0,"status":"created","id":"note-1"}]}'})
            gateway._mailbox_request(task, 'batch-1', request)
            receipts = gateway.technical_receipts(task['id'], task['execution_id'], refs)
            self.assertEqual([item['resource_id'] for item in receipts], ['note-1', 'note-2'])
            self.assertEqual([item['item_index'] for item in receipts], [0, 1])
            self.assertEqual(len({item['request_id'] for item in receipts}), 2)
            self.assertTrue(all(item['parent_request_id'] == task['execution_id'] + ':batch-1'
                                for item in receipts))
            criterion = {'kind': 'http_schema', 'service_id': 'notes',
                         'operation': 'create_batch', 'version': 'v1',
                         'schema': {'type': 'object', 'properties': {
                             'results': {'type': 'array'}}, 'required': ['results'],
                             'additionalProperties': False}}
            verified, _ = verify_result({'status': 'finished', 'returncode': 0},
                                        criterion, folder, receipts, task['execution_id'])
            self.assertTrue(verified)
            gateway.request = Mock(return_value={'content_type': 'application/json',
                'body': b'{"results":[{"index":0,"status":"created","id":"note-3"},'
                        b'{"index":1,"status":"failed"}]}'})
            gateway._mailbox_request(task, 'batch-2', request)
            partial = gateway.technical_receipts(task['id'], task['execution_id'], refs)[2:]
            self.assertEqual([(item['status'], item['resource_id']) for item in partial],
                             [('completed', 'note-3'), ('failed', None)])
            verified, _ = verify_result({'status': 'finished', 'returncode': 0},
                                        criterion, folder, partial, task['execution_id'])
            self.assertIsNone(verified)
            gateway.request = Mock(return_value={'content_type': 'application/json',
                'body': b'{"results":[{"index":0,"status":"created","id":"same"},'
                        b'{"index":0,"status":"created","id":"other"}]}'})
            gateway._mailbox_request(task, 'batch-3', request)
            uncertain = gateway.technical_receipts(task['id'], task['execution_id'], refs)[4:]
            self.assertEqual([item['status'] for item in uncertain], ['unknown', 'unknown'])
            self.assertTrue(all(item['resource_id'] is None for item in uncertain))


class MultiConversationVoiceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.owner = 'owner_123'
        self.chat = self.owner
        self.manager.telegram.user_id = self.owner
        self.sid = self.c.conversations.session(self.owner, self.chat)

    def tearDown(self):
        self.c._llm_executor.shutdown(wait=True)
        self.c._mailbox_executor.shutdown(wait=True)
        super().tearDown()

    def test_urgent_command_classification(self):
        """Conversation commands must be classified as urgent to avoid queue delays."""
        for cmd in ['/c', '/chats', '/new', '/clear', '/delete']:
            self.assertTrue(urgent_command(cmd), f"{cmd} should be urgent")
            self.assertTrue(urgent_command(f"{cmd} 2"), f"{cmd} with args should be urgent")
        self.assertFalse(urgent_command('/archive'))

    def test_help_buttons_and_text(self):
        self.assertEqual([button['label'] for button in HELP_BUTTONS[0]],
                         ['Mode nano', 'Mode low', 'Mode medium', 'Mode high'])
        self.assertEqual(HELP_BUTTONS[1][0]['label'], 'Ver modo actual')
        self.assertEqual(HELP_BUTTONS[2][0]['label'], 'Mis Chats')
        self.assertNotIn('/archive', HELP_TEXT)
        self.assertIn('/delete [número]', HELP_TEXT)

    def test_new_and_list_conversations_with_buttons(self):
        """Test creating multiple conversations and listing with interactive inline buttons."""
        active = self.c.conversations.list_conversations(self.owner, self.chat)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['title'], 'Chat Principal')
        self.assertEqual(active[0]['is_current'], 1)

        c2 = self.c.conversations.new_conversation(self.owner, self.chat, title='Análisis de rendimiento')
        self.assertIsNotNone(c2)
        self.assertEqual(c2['title'], 'Análisis de rendimiento')
        self.assertEqual(c2['is_current'], 1)

        active = self.c.conversations.list_conversations(self.owner, self.chat)
        self.assertEqual(len(active), 2)
        self.assertEqual(active[0]['id'], c2['id'])
        self.assertEqual(active[0]['is_current'], 1)
        self.assertEqual(active[1]['is_current'], 0)

        res = command(self.manager, 'telegram:msg1', '/chats')
        self.assertIsInstance(res, dict)
        self.assertIn('text', res)
        self.assertIn('buttons', res)

        text = res['text']
        self.assertIn('Conversaciones activas:', text)
        self.assertIn('1. [Actual] Análisis de rendimiento', text)
        self.assertIn('2. Chat Principal', text)

        buttons = res['buttons']
        self.assertEqual(len(buttons), 3)
        self.assertEqual(buttons[0][0]['value'], '/c 1')
        self.assertIn('[Actual]', buttons[0][0]['label'])
        self.assertEqual(buttons[1][0]['value'], '/c 2')
        self.assertEqual(buttons[2][0]['value'], '/new')
        self.assertEqual(buttons[2][1]['value'], '/clear')
        self.assertEqual(buttons[2][2]['value'], '/delete')

    def test_switch_conversation(self):
        """Test switching conversations via /c <index> and /c <name>."""
        c1_id = self.sid
        c2 = self.c.conversations.new_conversation(self.owner, self.chat, title='Docker logs')
        
        self.assertEqual(self.c.conversations.session(self.owner, self.chat), c2['id'])

        res = command(self.manager, 'telegram:msg2', '/c 2')
        self.assertIn('Chat activo: Chat Principal', res)
        self.assertEqual(self.c.conversations.session(self.owner, self.chat), c1_id)

        res = command(self.manager, 'telegram:msg3', '/c docker')
        self.assertIn('Chat activo: Docker logs', res)
        self.assertEqual(self.c.conversations.session(self.owner, self.chat), c2['id'])

        res = command(self.manager, 'telegram:msg4', '/c fantasma')
        self.assertIn('No se encontró la conversación', res)

    def test_switching_does_not_renumber_conversations(self):
        """Stable numeric commands must keep pointing at the listed conversation."""
        second = self.c.conversations.new_conversation(self.owner, self.chat, title='Segundo')
        before = self.c.conversations.list_conversations(self.owner, self.chat)
        self.assertEqual([c['title'] for c in before], ['Segundo', 'Chat Principal'])

        self.c.conversations.switch_conversation(self.owner, self.chat, '2')
        after = self.c.conversations.list_conversations(self.owner, self.chat)
        self.assertEqual([c['id'] for c in after], [c['id'] for c in before])
        self.assertEqual(after[0]['id'], second['id'])

    def test_clear_and_delete_current_conversation(self):
        """Test /clear and immediate deletion of the current chat."""
        res = command(self.manager, 'telegram:msg5', '/clear')
        self.assertIn('Conversación reiniciada.', res)

        active = self.c.conversations.list_conversations(self.owner, self.chat)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['title'], 'Nuevo chat')

        deleted_id = active[0]['id']
        res = command(self.manager, 'telegram:msg6', '/delete')
        self.assertEqual(res['text'], 'Chat eliminado: Nuevo chat')
        self.assertIsNone(self.c.conversations.get_conversation(deleted_id))
        active = self.c.conversations.list_conversations(self.owner, self.chat)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['title'], 'Chat Principal')

    def test_delete_conversation_by_number_only(self):
        self.c.conversations.new_conversation(self.owner, self.chat, title='Proyecto largo')
        active = self.c.conversations.list_conversations(self.owner, self.chat)
        target = active[1]
        res = command(self.manager, 'telegram:delete-number', '/delete 2')
        self.assertEqual(res['text'], f"Chat eliminado: {target['title']}")
        self.assertIsNone(self.c.conversations.get_conversation(target['id']))
        self.assertEqual(command(self.manager, 'telegram:delete-name', '/delete Proyecto largo'),
                         'Uso: /delete [número]')

    def test_delete_rejects_conversation_with_active_task(self):
        current = self.c.conversations.list_conversations(self.owner, self.chat)[0]
        self.store.create('telegram:active-delete', 'trabajo',
                          conversation_id=current['id'], state='RUNNING')
        result = command(self.manager, 'telegram:delete-active', '/delete')
        self.assertIn('tarea activa', result)
        self.assertIsNotNone(self.c.conversations.get_conversation(current['id']))

    def test_delete_response_uses_replacement_conversation(self):
        doomed = self.c.conversations.new_conversation(
            self.owner, self.chat, title='Temporal')
        source = 'telegram:delete-routing'
        self.c.conversations.begin(source, 'trace-delete', '/delete', self.owner, self.chat)
        captured = {}

        def capture(text, buttons=None):
            captured.update(text=text, turn=dict(current_turn.get() or {}))

        with patch.object(self.c, 'notify', side_effect=capture):
            process_ordinary(self.manager, source, 'trace-delete', '/delete')

        active = self.c.conversations.list_conversations(self.owner, self.chat)[0]
        self.assertIsNone(self.c.conversations.get_conversation(doomed['id']))
        self.assertIsNone(self.c.conversations.turn(source))
        self.assertEqual(captured['text'], 'Chat eliminado: Temporal')
        self.assertEqual(captured['turn']['conversation_id'], active['id'])
        self.assertEqual(captured['turn']['title'], active['title'])

    def test_message_history_isolation(self):
        """Verify message history does not leak between distinct conversation sessions."""
        turn1 = self.c.conversations.begin('telegram:t1', 'trace1', 'Mensaje secreto en sesion 1', self.owner, self.chat)
        s1_id = turn1['conversation_id']
        self.c.conversations.delivered(s1_id, 'deliv-1', 'Respuesta secreta en sesion 1')

        history1 = self.c.conversations.history(s1_id)
        self.assertEqual(len(history1), 2)
        self.assertEqual(history1[0]['content'], 'Mensaje secreto en sesion 1')
        self.assertEqual(history1[1]['content'], 'Respuesta secreta en sesion 1')

        c2 = self.c.conversations.new_conversation(self.owner, self.chat, title='Sesion 2')
        s2_id = c2['id']

        history2 = self.c.conversations.history(s2_id)
        self.assertEqual(len(history2), 0)

        turn2 = self.c.conversations.begin('telegram:t2', 'trace2', 'Hola sesion 2', self.owner, self.chat)
        self.assertEqual(turn2['conversation_id'], s2_id)
        self.c.conversations.delivered(s2_id, 'deliv-2', 'Respuesta sesion 2')

        history2 = self.c.conversations.history(s2_id)
        self.assertEqual(len(history2), 2)
        self.assertEqual(history2[0]['content'], 'Hola sesion 2')
        self.assertEqual(history2[1]['content'], 'Respuesta sesion 2')

        history1 = self.c.conversations.history(s1_id)
        self.assertEqual(len(history1), 2)
        self.assertEqual(history1[0]['content'], 'Mensaje secreto en sesion 1')

    def test_supergroup_forum_topic_binding(self):
        """Verify Telegram Supergroup topics (thread_id) get isolated sessions and routing."""
        turn_t1 = self.c.conversations.begin('telegram:topic1', 'tr-top1', 'Pregunta en topico 100', self.owner, self.chat, thread_id=100)
        s_top100 = turn_t1['conversation_id']
        self.assertEqual(turn_t1['thread_id'], 100)

        turn_t2 = self.c.conversations.begin('telegram:topic2', 'tr-top2', 'Pregunta en topico 200', self.owner, self.chat, thread_id=200)
        s_top200 = turn_t2['conversation_id']
        self.assertEqual(turn_t2['thread_id'], 200)

        self.assertNotEqual(s_top100, s_top200)

        conv100 = self.c.conversations.get_conversation(s_top100)
        conv200 = self.c.conversations.get_conversation(s_top200)
        self.assertEqual(conv100['thread_id'], 100)
        self.assertEqual(conv200['thread_id'], 200)

        token = current_turn.set(turn_t1)
        try:
            self.manager.telegram.send_message.reset_mock()
            self.c.notify('Mensaje para topico 100')
            if self.c._mailbox_job:
                self.c._mailbox_job.result(timeout=5)
            self.manager.telegram.send_message.assert_called()
            kwargs = self.manager.telegram.send_message.call_args.kwargs
            self.assertEqual(kwargs.get('message_thread_id'), 100)
        finally:
            current_turn.reset(token)

    def test_auto_naming_from_first_message(self):
        """Initial generic conversation should auto-name from first non-command user message."""
        c = self.c.conversations.new_conversation(self.owner, self.chat, title='Nuevo chat')
        sid = c['id']

        self.c.conversations.begin('telegram:naming_test', 'tr_name', '¿Cómo configuro Brain en GCP?', self.owner, self.chat)
        
        updated = self.c.conversations.get_conversation(sid)
        self.assertEqual(updated['title'], '¿Cómo configuro Brain en GCP?')

    def test_auto_naming_from_conversacion_n(self):
        """Conversations titled 'Conversación N' from /new should auto-name on first user message."""
        c = self.c.conversations.new_conversation(self.owner, self.chat)
        sid = c['id']
        self.assertTrue(c['title'].startswith('Conversación '))

        self.c.conversations.begin('telegram:auto_name2', 'tr_name2', 'Explica los permisos de WireGuard', self.owner, self.chat)
        updated = self.c.conversations.get_conversation(sid)
        self.assertEqual(updated['title'], 'Explica los permisos de WireGuard')

    def test_rename_conversation(self):
        """Test /rename command for current conversation and by index."""
        res = command(self.manager, 'telegram:ren1', '/rename Proyecto Alpha')
        self.assertIn('Conversación renombrada a: Proyecto Alpha', res)

        c2 = self.c.conversations.new_conversation(self.owner, self.chat, title='Beta')
        
        res = command(self.manager, 'telegram:ren2', '/rename 2 Proyecto Omega')
        self.assertIn('Conversación renombrada a: Proyecto Omega', res)

        active = self.c.conversations.list_conversations(self.owner, self.chat)
        self.assertEqual(active[1]['title'], 'Proyecto Omega')


class TelegramGatewayTests(unittest.TestCase):
    def test_typing_indicator_targets_same_chat_and_topic(self):
        stop = threading.Event()
        telegram = Mock()
        telegram.send_chat_action.side_effect = lambda *args, **kwargs: stop.set()
        _typing_loop(telegram, '-100123', 77, stop, delay=0, interval=0.01)
        telegram.send_chat_action.assert_called_once_with(
            'typing', chat_id='-100123', message_thread_id=77)

    @patch('app.voice.gateway.telegram_gateway.requests.post')
    def test_telegram_chat_action_payload(self, post):
        post.return_value.json.return_value = {'ok': True}
        handler = TelegramHandler('token', 'owner')
        handler.send_chat_action('typing', chat_id='-100123', message_thread_id=77)
        self.assertEqual(post.call_args.kwargs['json'], {
            'chat_id': '-100123', 'action': 'typing', 'message_thread_id': 77})


if __name__ == '__main__':
    unittest.main()
