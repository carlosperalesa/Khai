"""First setup is one owner decision; actual values are confirmed before execution."""
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.bootstrap import ModuleManager
from app.mind.coordinator import Coordinator
from app.mind.result_presenter import present_recipe
from tests.test_control import Fixture


class InputConfirmationTests(Fixture):
    def test_saved_default_is_shown_and_change_prevents_execution(self):
        self.manager.memory.set_preference('default_city', 'Santiago')
        messages = []
        self.c.notify = messages.append
        task = self.artifact(input_confirmation_required=True,
                             params={'city': 'Santiago'})
        self.c.advance(task)
        waiting = self.store.get(task['id'])
        self.assertEqual(waiting['state'], 'WAITING_INPUT_CONFIRMATION')
        self.assertIn('Santiago', messages[-1])
        self.assertIn('predeterminado', messages[-1])
        self.assertIsNone(waiting.get('execution_id'))
        self.c.accept_input_confirmation('owner:change-city', 'Valparaíso')
        self.assertEqual(self.store.get(task['id'])['state'], 'RESOLVING_SCOPE')
        self.assertIsNone(self.store.get(task['id']).get('execution_id'))

    def test_confirmed_values_allow_execution_only_while_unchanged(self):
        self.c.notify = Mock()
        task = self.artifact(input_confirmation_required=True,
                             params={'city': 'Santiago'})
        self.c.advance(task)
        self.c.accept_input_confirmation('owner:confirm-city', 'sí')
        confirmed = self.store.get(task['id'])
        self.assertEqual(confirmed['state'], 'READY')
        self.assertEqual(confirmed['confirmed_input_signature'],
                         self.c._input_signature(confirmed))
        self.store.update(task['id'], params={'city': 'Valparaíso'})
        self.c.advance(self.store.get(task['id']))
        self.assertEqual(self.store.get(task['id'])['state'], 'WAITING_INPUT_CONFIRMATION')
        self.proxy.submit.assert_not_called()


class SetupTests(unittest.TestCase):
    def test_one_setup_decision_covers_service_and_reviewed_draft(self):
        descriptor = {'service_id': 'mail', 'operation': 'send', 'version': 'v1',
            'url': 'https://example.org/send', 'method': 'POST',
            'parameters': ['to', 'body'], 'content_types': ['application/json'],
            'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
            'effect': 'write', 'credential_ref': None,
            'effect_contract': {'kind': 'create', 'identity': None, 'correction': None,
                                'summary': {'name': 'Envío de correo', 'context': 'Mensaje solicitado'}}}
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data'
            (data / 'memory').mkdir(parents=True)
            (data / 'memory' / 'agent.md').write_text('Test identity', encoding='utf-8')
            with patch.dict('os.environ', {'KHAI_RUNTIME': 'local', 'TELEGRAM_BOT_TOKEN': 'token',
                                           'TELEGRAM_USER_ID': 'owner'}):
                manager = ModuleManager(data_dir=str(data))
                manager.telegram = Mock()
                manager.action_planner = None
                manager.skill_selector = None
                manager.mind.process_message = Mock(return_value={
                    'action': 'ACTION_REQUIRED', 'extracted_goal': 'Enviar correo'})
                manager.llm.generate_content = Mock(side_effect=[
                    json.dumps({'purpose': 'Enviar correo', 'capabilities': ['Network']}),
                    json.dumps(descriptor)])
                coordinator = Coordinator(manager, Mock())
                manager.coordinator = coordinator
                coordinator.notify = Mock()
                try:
                    task = coordinator.intake('owner:setup', 'Enviar correo')
                    coordinator.advance(task)
                    waiting = coordinator.store.get(task['id'])
                    self.assertEqual(waiting['state'], 'WAITING_CREATION_APPROVAL')
                    self.assertIn('example.org', coordinator.notify.call_args.args[0])
                    self.assertEqual(manager.voice_services.active_services(), {})
                    manager.pixy.direct_response('y', source='owner:approve-setup')
                    approved = coordinator.store.get(task['id'])
                    self.assertEqual(approved['state'], 'SCOPE_PLANNING')
                    self.assertEqual(approved['services'], [
                        {'service_id': 'mail', 'operation': 'send', 'version': 'v1'}])
                    self.assertIn('mail', manager.voice_services.active_services())
                    coordinator.requirements.questions = Mock(return_value=[])
                    coordinator.advance(approved)
                    manager.hand.generate = Mock(return_value=(
                        '# MODE: SAFE\nprint("sent")', 'SAFE', []))
                    manager.hand.describe = Mock(return_value=None)
                    manager.pixy.review = Mock(return_value={
                        'safe': True, 'requires_approval': True})
                    coordinator.advance(coordinator.store.get(task['id']))
                    coordinator.advance(coordinator.store.get(task['id']))
                    reviewed = coordinator.store.get(task['id'])
                    self.assertEqual(reviewed['state'], 'READY')
                    self.assertEqual(reviewed['approval']['setup_consent_source'],
                                     'owner:approve-setup')
                    self.assertIsNotNone(manager.pixy.skill_approvals.matching(
                        dict(reviewed, artifact_hash=reviewed['artifact_hash']),
                        dict(manager.hand.version(reviewed['version_id']), status='active')))
                    coordinator.advance(reviewed)
                    self.assertEqual(coordinator.store.get(task['id'])['state'],
                                     'WAITING_INPUT_CONFIRMATION')
                    coordinator.proxy.submit.assert_not_called()
                    manager.llm.generate_content = Mock(side_effect=[
                        json.dumps({'purpose': 'Enviar correo', 'capabilities': ['Network']}),
                        json.dumps(descriptor)])
                    second = coordinator.intake('owner:setup-again', 'Enviar correo')
                    coordinator.advance(second)
                    self.assertEqual(coordinator.store.get(second['id'])['state'],
                                     'WAITING_CREATION_APPROVAL')
                    manager.pixy.direct_response('y', source='owner:approve-again')
                    self.assertEqual(coordinator.store.get(second['id'])['state'],
                                     'SCOPE_PLANNING')
                finally:
                    coordinator._llm_executor.shutdown(wait=True, cancel_futures=True)
                    coordinator._mailbox_executor.shutdown(wait=True, cancel_futures=True)


class ResultPresenterTests(unittest.TestCase):
    def test_news_and_confirmed_mail_are_rendered_without_invented_items(self):
        address_hash = hashlib.sha256(json.dumps('carlos@ejemplo.com',
            ensure_ascii=False).encode()).hexdigest()
        text = present_recipe({'recipe_input': {'email': 'carlos@ejemplo.com'},
            'recipe_receipts': [
                {'step_id': 'buscar_noticias', 'output': {'noticias': [
                    {'titulo': 'Titular verificado', 'resumen': 'Resumen devuelto',
                     'url': 'https://example.org/noticia'}]}, 'external_effects': []},
                {'step_id': 'enviar_correo', 'output': {'enviado': True},
                 'external_effects': [{'effect': 'write', 'status': 'completed',
                                       'summary': {'name': 'Envío de correo'},
                                       'parameter_hashes': {'to': address_hash}}]}]})
        self.assertIn('Titular verificado', text)
        self.assertIn('Resumen devuelto', text)
        self.assertIn('carlos@ejemplo.com', text)
        self.assertIn('¿Es correcto el resultado completo?', text)

    def test_failed_mail_receipt_cannot_be_presented_as_confirmed(self):
        text = present_recipe({'recipe_input': {'email': 'carlos@ejemplo.com'},
            'recipe_receipts': [{'step_id': 'enviar_correo', 'output': {'enviado': False},
                 'external_effects': [{'effect': 'write', 'status': 'failed',
                                       'summary': {'name': 'Envío de correo'}}]}]})
        self.assertNotIn('confirmó el envío', text)

    def test_mail_receipt_with_different_recipient_does_not_claim_default(self):
        text = present_recipe({'recipe_input': {'email': 'carlos@ejemplo.com'},
            'recipe_receipts': [{'step_id': 'enviar_correo', 'output': {'enviado': True},
                 'external_effects': [{'effect': 'write', 'status': 'completed',
                                       'summary': {'name': 'Envío de correo'},
                                       'parameter_hashes': {'to': 'different'}}]}]})
        self.assertNotIn('confirmó el envío a carlos@ejemplo.com', text)


if __name__ == '__main__':
    unittest.main()
