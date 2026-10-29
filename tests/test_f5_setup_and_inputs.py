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

    def test_controlled_read_write_recipe_reuse_and_partial_batch_guard(self):
        news_desc = {
            'service_id': 'news', 'operation': 'latest', 'version': 'v1',
            'url': 'https://news.example.org/latest', 'method': 'GET',
            'parameters': ['topic'], 'content_types': ['application/json'],
            'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
            'effect': 'read', 'credential_ref': None, 'effect_contract': None,
        }
        mail_desc = {
            'service_id': 'mail', 'operation': 'send', 'version': 'v1',
            'url': 'https://mail.example.org/send', 'method': 'POST',
            'parameters': ['to', 'noticias'], 'content_types': ['application/json'],
            'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
            'effect': 'write', 'credential_ref': None,
            'effect_contract': {
                'kind': 'create', 'identity': {'source': 'response', 'field': 'message_id'},
                'correction': None,
                'summary': {'name': 'Envío de correo', 'context': 'Resumen de noticias'},
            },
        }
        batch_desc = {
            'service_id': 'mail', 'operation': 'send_batch', 'version': 'v1',
            'url': 'https://mail.example.org/send-batch', 'method': 'POST',
            'parameters': ['items'], 'content_types': ['application/json'],
            'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
            'effect': 'write', 'credential_ref': None,
            'effect_contract': {
                'kind': 'create', 'identity': None, 'correction': None,
                'summary': {'name': 'Lote de correos', 'context': 'Boletín por lote'},
                'batch': {
                    'request_field': 'items', 'response_field': 'results',
                    'index_field': 'index', 'identity_field': 'id',
                    'status_field': 'status', 'success_value': 'sent',
                    'failure_value': 'failed',
                },
            },
        }
        recipe_schema = {
            'type': 'object',
            'properties': {'topic': {'type': 'string'}, 'email': {'type': 'string'}},
            'required': ['topic', 'email'],
            'additionalProperties': False,
        }
        news_spec = {
            'skill_id': 'buscar-noticias',
            'description': 'Consultar titulares recientes por tema.',
            'use_cases': ['Buscar noticias y enviarlas por correo'],
            'input_schema': {
                'type': 'object', 'properties': {'topic': {'type': 'string'}},
                'required': ['topic'], 'additionalProperties': False,
            },
            'output_schema': {
                'type': 'object', 'properties': {'noticias': {'type': 'array'}},
                'required': ['noticias'], 'additionalProperties': False,
            },
            'capabilities': ['Network'],
        }
        mail_spec = {
            'skill_id': 'enviar-correo',
            'description': 'Enviar resumen de noticias al destinatario.',
            'use_cases': ['Buscar noticias y enviarlas por correo'],
            'input_schema': {
                'type': 'object',
                'properties': {'to': {'type': 'string'}, 'noticias': {'type': 'array'}},
                'required': ['to', 'noticias'], 'additionalProperties': False,
            },
            'output_schema': {
                'type': 'object', 'properties': {'enviado': {'type': 'boolean'}},
                'required': ['enviado'], 'additionalProperties': False,
            },
            'capabilities': ['Network'],
        }
        recipe = {
            'recipe_id': 'noticias-y-correo',
            'input_schema': recipe_schema,
            'steps': [
                {'id': 'buscar_noticias', 'version_id': 'missing-news',
                 'bindings': {'topic': 'input.topic'}},
                {'id': 'enviar_correo', 'version_id': 'missing-mail',
                 'bindings': {'to': 'input.email', 'noticias': 'buscar_noticias.noticias'}},
            ],
            'output': 'enviar_correo.enviado',
        }
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data'
            (data / 'memory').mkdir(parents=True)
            (data / 'memory' / 'agent.md').write_text('Test identity', encoding='utf-8')
            with patch.dict('os.environ', {'KHAI_RUNTIME': 'local', 'TELEGRAM_BOT_TOKEN': 'token',
                                           'TELEGRAM_USER_ID': 'owner'}):
                manager = ModuleManager(data_dir=str(data))
                manager.telegram = Mock()
                manager.memory.set_preference('default_email', 'carlos@ejemplo.com')
                manager.mind.process_message = Mock(return_value={
                    'action': 'ACTION_REQUIRED',
                    'extracted_goal': 'Busca noticias sobre energía solar y envíalas por correo'})
                manager.action_planner = Mock()
                manager.action_planner.propose = Mock(return_value={
                    'recipe': json.loads(json.dumps(recipe)),
                    'inputs': {'topic': 'energía solar'},
                    'missing_specs': {
                        'buscar_noticias': json.loads(json.dumps(news_spec)),
                        'enviar_correo': json.loads(json.dumps(mail_spec)),
                    },
                })
                manager.llm.generate_content = Mock(side_effect=[
                    json.dumps(news_desc),
                    json.dumps(mail_desc),
                ])
                proxy = Mock()
                proxy.submit.return_value = {'status': 'queued'}
                coordinator = Coordinator(manager, proxy)
                manager.coordinator = coordinator
                notifications = []
                coordinator.notify = notifications.append
                try:
                    # 1. Primera configuración: una sola decisión aprueba creación y servicios
                    task = coordinator.intake('owner:req-1',
                                              'Busca noticias sobre energía solar y envíalas por correo')
                    coordinator.advance(task)
                    waiting_setup = coordinator.store.get(task['id'])
                    self.assertEqual(waiting_setup['state'], 'WAITING_CREATION_APPROVAL')
                    self.assertIn('news.example.org', notifications[-1])
                    self.assertIn('mail.example.org', notifications[-1])
                    manager.pixy.direct_response('y', source='owner:setup-1')
                    after_setup = coordinator.store.get(task['id'])
                    self.assertEqual(after_setup['state'], 'SCOPE_PLANNING')
                    self.assertIn('news', manager.voice_services.active_services())
                    self.assertIn('mail', manager.voice_services.active_services())

                    # 2. Avance al paso 1 (lectura) y confirmación de variables con predeterminado
                    coordinator.requirements.questions = Mock(return_value=[])
                    coordinator.advance(after_setup)
                    self.assertEqual(coordinator.store.get(task['id'])['state'], 'RECIPE_STEP')
                    coordinator.advance(coordinator.store.get(task['id']))
                    manager.hand.generate = Mock(return_value=(
                        '# MODE: SAFE\nfrom khai_gateway import request\nprint("{}")', 'SAFE', []))
                    manager.pixy.review = Mock(return_value={'safe': True, 'requires_approval': True})
                    coordinator.advance(coordinator.store.get(task['id']))  # GENERATING -> REVIEWING
                    coordinator.advance(coordinator.store.get(task['id']))  # REVIEWING -> READY
                    coordinator.advance(coordinator.store.get(task['id']))  # READY -> WAITING_INPUT_CONFIRMATION
                    waiting_input = coordinator.store.get(task['id'])
                    self.assertEqual(waiting_input['state'], 'WAITING_INPUT_CONFIRMATION')
                    self.assertIn('energía solar', notifications[-1])
                    self.assertIn('carlos@ejemplo.com (predeterminado)', notifications[-1])

                    coordinator.accept_input_confirmation('owner:confirm-1', 'sí')
                    coordinator.advance(coordinator.store.get(task['id']))  # READY -> SUBMITTED
                    coordinator.advance(coordinator.store.get(task['id']))  # SUBMITTED -> RUNNING
                    running_step1 = coordinator.store.get(task['id'])
                    self.assertEqual(running_step1['state'], 'RUNNING')

                    news_items = [{'titulo': 'Planta solar récord',
                                   'resumen': 'Generación limpia en el norte',
                                   'url': 'https://news.example.org/solar-1'}]
                    manager.gateway.request = Mock(return_value={
                        'content_type': 'application/json',
                        'body': json.dumps({'noticias': news_items}).encode('utf-8'),
                    })
                    running_step1 = coordinator.store.observe_execution(running_step1, {'status': 'running'})
                    manager.gateway._mailbox_request(running_step1, 'req-news-1', {
                        'service': 'news', 'operation': 'latest',
                        'params': {'topic': 'energía solar'},
                    })
                    proxy.get.return_value = {
                        'status': 'finished', 'returncode': 0,
                        'stdout': json.dumps({'noticias': news_items}, ensure_ascii=False),
                    }
                    coordinator.advance(running_step1)

                    # 3. Paso 2 (escritura): usa la misma confirmación y el consentimiento inicial
                    step2 = coordinator.store.get(task['id'])
                    self.assertEqual(step2['state'], 'RECIPE_STEP')
                    self.assertEqual(step2['recipe_step_index'], 1)
                    coordinator.advance(step2)  # RECIPE_STEP -> GENERATING
                    manager.hand.generate = Mock(return_value=(
                        '# MODE: SAFE\nfrom khai_gateway import request\nprint("{\\"enviado\\": true}")',
                        'SAFE', []))
                    coordinator.advance(coordinator.store.get(task['id']))  # GENERATING -> REVIEWING
                    coordinator.advance(coordinator.store.get(task['id']))  # REVIEWING -> READY
                    coordinator.advance(coordinator.store.get(task['id']))  # READY -> SUBMITTED (sin pedir otra confirmación)
                    coordinator.advance(coordinator.store.get(task['id']))  # SUBMITTED -> RUNNING
                    running_step2 = coordinator.store.get(task['id'])
                    self.assertEqual(running_step2['state'], 'RUNNING')

                    manager.gateway.request = Mock(return_value={
                        'content_type': 'application/json',
                        'body': b'{"message_id":"msg-001"}',
                    })
                    running_step2 = coordinator.store.observe_execution(running_step2, {'status': 'running'})
                    manager.gateway._mailbox_request(running_step2, 'req-mail-1', {
                        'service': 'mail', 'operation': 'send',
                        'params': {'to': 'carlos@ejemplo.com', 'noticias': news_items},
                    })
                    proxy.get.return_value = {
                        'status': 'finished', 'returncode': 0,
                        'stdout': '{"enviado": true}',
                    }
                    coordinator.advance(running_step2)

                    unverified = coordinator.store.get(task['id'])
                    self.assertEqual(unverified['state'], 'UNVERIFIED')
                    self.assertIn('Planta solar récord', notifications[-1])
                    self.assertIn('Generación limpia en el norte', notifications[-1])
                    self.assertIn('confirmó el envío a carlos@ejemplo.com', notifications[-1])

                    # 4. Único 'y' final acepta el objetivo y promueve ambas skills a activas
                    reply = coordinator.accept_review_reply('owner:final-y', 'y')
                    self.assertIn('resultado aceptado', reply)
                    coordinator.advance(coordinator.store.get(task['id']))
                    completed = coordinator.store.get(task['id'])
                    self.assertEqual(completed['state'], 'COMPLETED')
                    news_vid = completed['recipe']['steps'][0]['version_id']
                    mail_vid = completed['recipe']['steps'][1]['version_id']
                    self.assertEqual(manager.hand.version(news_vid)['status'], 'active')
                    self.assertEqual(manager.hand.version(mail_vid)['status'], 'active')

                    # 5. Segunda petición reutiliza versiones activas y su aprobación de alcance
                    reused_recipe = {
                        'recipe_id': 'noticias-y-correo',
                        'input_schema': recipe_schema,
                        'steps': [
                            {'id': 'buscar_noticias', 'version_id': news_vid,
                             'bindings': {'topic': 'input.topic'}},
                            {'id': 'enviar_correo', 'version_id': mail_vid,
                             'bindings': {'to': 'input.email', 'noticias': 'buscar_noticias.noticias'}},
                        ],
                        'output': 'enviar_correo.enviado',
                    }
                    manager.action_planner.propose = Mock(return_value={
                        'recipe': reused_recipe,
                        'inputs': {'topic': 'litio'},
                        'missing_specs': {},
                    })
                    second = coordinator.intake('owner:req-2',
                                                'Busca noticias sobre litio y envíalas por correo')
                    coordinator.advance(second)  # ROUTING -> RECIPE_STEP (sin WAITING_CREATION_APPROVAL)
                    self.assertEqual(coordinator.store.get(second['id'])['state'], 'RECIPE_STEP')
                    coordinator.advance(coordinator.store.get(second['id']))  # -> GENERATING
                    coordinator.advance(coordinator.store.get(second['id']))  # -> REVIEWING
                    coordinator.advance(coordinator.store.get(second['id']))  # -> READY (reutiliza skill_approvals)
                    reused_ready = coordinator.store.get(second['id'])
                    self.assertEqual(reused_ready['state'], 'READY')
                    self.assertEqual(reused_ready['approval']['reused_from_task'], task['id'])

                    # 6. Contraste de lote parcial: conserva éxitos, marca fallos/unknown y bloquea el 'y' final
                    batch_hash = manager.hand.propose_service(batch_desc)
                    manager.pixy_services.decide(batch_hash, True, decided_by='owner:batch-approve')
                    batch_task = coordinator.store.get(second['id'])
                    coordinator.accept_input_confirmation
                    coordinator.advance(reused_ready)  # READY -> WAITING_INPUT_CONFIRMATION
                    coordinator.accept_input_confirmation('owner:confirm-2', 'sí')
                    coordinator.advance(coordinator.store.get(second['id']))  # READY -> SUBMITTED
                    coordinator.advance(coordinator.store.get(second['id']))  # SUBMITTED -> RUNNING
                    running_batch = coordinator.store.get(second['id'])
                    coordinator.store.update(
                        second['id'],
                        services=[{'service_id': 'mail', 'operation': 'send_batch', 'version': 'v1'}],
                    )
                    running_batch = coordinator.store.get(second['id'])
                    manager.gateway.request = Mock(return_value={
                        'content_type': 'application/json',
                        'body': b'{"results":[{"index":0,"status":"sent","id":"msg-101"},'
                                b'{"index":1,"status":"failed"}]}',
                    })
                    running_batch = coordinator.store.observe_execution(running_batch, {'status': 'running'})
                    manager.gateway._mailbox_request(running_batch, 'req-batch-1', {
                        'service': 'mail', 'operation': 'send_batch',
                        'params': {'items': [{'to': 'a@ejemplo.com'}, {'to': 'b@ejemplo.com'}]},
                    })
                    proxy.get.return_value = {
                        'status': 'finished', 'returncode': 0,
                        'stdout': json.dumps({'noticias': news_items}),
                    }
                    coordinator.advance(running_batch)
                    halted = coordinator.store.get(second['id'])
                    self.assertEqual(halted['state'], 'UNVERIFIED')
                    self.assertTrue(halted.get('batch_incomplete'))
                    self.assertIn('efectos sin confirmar (2:failed)', notifications[-1])
                    blocked_reply = coordinator.accept_review_reply('owner:try-accept-partial', 'y')
                    self.assertIn('hay efectos externos sin confirmar', blocked_reply)
                    self.assertEqual(coordinator.store.get(second['id'])['state'], 'UNVERIFIED')
                finally:
                    coordinator._llm_executor.shutdown(wait=True, cancel_futures=True)
                    coordinator._mailbox_executor.shutdown(wait=True, cancel_futures=True)


if __name__ == '__main__':
    unittest.main()
