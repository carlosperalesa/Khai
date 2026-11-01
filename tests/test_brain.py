"""Enrutamiento LLM unificado por modo (low=Gemini/Cloud, mid/high=Dolphin) con cascada completa."""
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.mind.mind import Mind
from app.mind.brain import Brain, ModelController, DolphinController
from app.voice.gateway.llm_gateway import LLMManager
from app.voice.voice import _command, urgent_command
from app.voice.gateway.telegram_gateway import TelegramHandler
from app.voice.help import HELP_TEXT


class _Memory:
    def __init__(self):
        self.saved = []

    def append_long_term(self, kind, value):
        self.saved.append((kind, value))

    def get_chat_history(self, limit=12):
        lines = []
        for kind, val in self.saved[-limit:]:
            if kind == 'user_chat':
                lines.append(f'Usuario: {val}')
            elif kind == 'khai_chat':
                lines.append(f'Khai: {val}')
        return '\n'.join(lines)

    def get_system_prompt(self):
        return 'Habla de forma concisa.'


class BrainTests(unittest.TestCase):
    def test_common_model_tree_contains_cascade_stop_and_cleanup_failure(self):
        from app.kernel.journal import Journal
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            root = controller.journal.start('interaction', 'telegram:model-audit')
            with controller.journal.activate(root):
                with patch.object(controller, '_start', side_effect=[TimeoutError('cold'), 'connected']):
                    self.assertEqual(controller.set_mode('high'), 'connected')
                with patch.object(controller, 'get_instance', return_value=None):
                    controller.set_mode('nano')
                with patch.object(controller, 'get_instance', side_effect=TimeoutError('provider body')):
                    controller._rollback_activation()
            tree = Journal(controller.journal.path).tree(root)
            changes = [c for c in tree['cycles'] if c['kind'] == 'model_change']
            self.assertEqual(len(changes), 2)
            attempts = [c for c in tree['cycles'] if c['kind'] == 'model_attempt']
            self.assertEqual([c['status'] for c in attempts], ['failed', 'completed'])
            self.assertEqual({c['parent_id'] for c in attempts}, {changes[0]['cycle_id']})
            self.assertEqual([c['status'] for c in tree['cycles'] if c['kind'] == 'model_stop'], ['completed'])
            self.assertEqual([c['status'] for c in tree['cycles'] if c['kind'] == 'model_vm_delete'], ['failed'])
            self.assertTrue(all(c['ended_at'] is not None for c in tree['cycles'] if c['cycle_id'] != root))
            self.assertNotIn('provider body', json.dumps(tree))

    def test_mode_command_cascade_retains_attempts_after_controller_restart(self):
        """One request can fail a candidate and connect another without losing either receipt."""
        from app.kernel.tracing import trace_scope

        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            manager = SimpleNamespace(coordinator=MagicMock(), dolphin=controller)
            with patch.object(controller, '_start', side_effect=[
                    TimeoutError('fixture cold start'), '[Modo mid conectado]']), \
                 trace_scope('req_model-cascade'):
                result = _command(manager, 'telegram:model-cascade', '/mode high')

            self.assertEqual(result, '[Modo mid conectado]')
            restarted = ModelController(directory, {})
            events = list(reversed(restarted.transition_snapshot()['recent_transitions']))
            self.assertEqual([(event['profile'], event['phase']) for event in events], [
                ('high', 'activation_started'), ('high', 'activation_failed'),
                ('mid', 'activation_started'), ('mid', 'activation_completed'),
            ])
            self.assertEqual({event['trace_id'] for event in events}, {'req_model-cascade'})
            self.assertEqual(events[0]['attempt_id'], events[1]['attempt_id'])
            self.assertEqual(events[2]['attempt_id'], events[3]['attempt_id'])
            self.assertNotEqual(events[0]['attempt_id'], events[2]['attempt_id'])
            self.assertEqual(events[1]['error_type'], 'TimeoutError')
            self.assertGreaterEqual(events[1]['elapsed_seconds'], 0)
            self.assertGreaterEqual(events[3]['elapsed_seconds'], 0)

    def test_parallel_activation_journals_do_not_mix_request_contexts(self):
        """Telemetry from overlapping requests retains its own trace and activation ID."""
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from app.kernel.tracing import get_trace_id, trace_scope

        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            barrier = Barrier(2)

            def activate(profile):
                barrier.wait(timeout=5)
                controller._transition_event(profile, 'warmup_started')
                return 'connected'

            def request(profile):
                with trace_scope('req_parallel-' + profile):
                    return controller.start(profile)

            with patch.object(controller, '_start', side_effect=activate), \
                 ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(request, profile) for profile in ('high', 'mid')]
                self.assertEqual([future.result(timeout=10) for future in futures],
                                 ['connected', 'connected'])

            events = controller.transition_snapshot()['recent_transitions']
            attempts = {}
            for profile in ('high', 'mid'):
                branch = [event for event in events if event['profile'] == profile]
                self.assertEqual([event['phase'] for event in branch],
                                 ['activation_completed', 'warmup_started', 'activation_started'])
                self.assertEqual({event['trace_id'] for event in branch}, {'req_parallel-' + profile})
                attempts[profile] = {event['attempt_id'] for event in branch}
                self.assertEqual(len(attempts[profile]), 1)
                self.assertNotIn('', attempts[profile])
            self.assertTrue(attempts['high'].isdisjoint(attempts['mid']))
            self.assertEqual(get_trace_id(), '')

    def test_activation_telemetry_correlates_failure_and_warmup_stage(self):
        from app.kernel.tracing import trace_scope
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            def fail(profile):
                controller._transition_event(profile, 'warmup_started')
                raise TimeoutError('fixture')
            with patch.object(controller, '_start', side_effect=fail), trace_scope('activation-test'):
                with self.assertRaises(TimeoutError):
                    controller.start('high')
            events = controller.transition_snapshot()['recent_transitions']
            self.assertEqual([e['phase'] for e in events],
                             ['activation_failed', 'warmup_started', 'activation_started'])
            self.assertEqual(len({e['attempt_id'] for e in events}), 1)
            self.assertEqual(events[0]['trace_id'], 'activation-test')
            self.assertEqual(events[0]['error_type'], 'TimeoutError')

    def test_unknown_intent_is_rejected_without_saving_a_promise(self):
        mem = _Memory()
        llm = MagicMock()
        llm.dolphin = None
        llm.generate_content.return_value = json.dumps({
            'is_failure_analysis': False, 'normal_intent': {
                'reasoning_summary': 'Hace falta actuar.', 'action': 'FETCH_WEATHER',
                'reply': 'Estoy consultando el clima.'}})
        result = Mind(llm, mem).process_message('Consulta el clima')
        self.assertIn('error', result)
        self.assertEqual(mem.saved, [])

    def test_help_buttons_carry_complete_mode_commands(self):
        with patch('app.voice.gateway.telegram_gateway.requests.post') as post:
            telegram = TelegramHandler('test-token', 'owner')
            telegram.send_message(HELP_TEXT, parse_mode=None)
        payload = post.call_args.kwargs['json']
        buttons = json.loads(payload['reply_markup'])['inline_keyboard']
        callbacks = [button['callback_data'] for row in buttons for button in row]
        self.assertEqual(callbacks, ['/mode nano', '/mode low', '/mode mid', '/mode high', '/mode ?', '/c'])
        manager = SimpleNamespace(coordinator=MagicMock(), dolphin=MagicMock())
        for row in buttons:
            for button in row:
                command = button['callback_data']
                self.assertIn(command, HELP_TEXT)
                _command(manager, 'owner:button', command)
                if command.startswith('/mode'):
                    manager.dolphin.set_mode.assert_called_with(command.split()[1], notifier=manager.coordinator.notify)

    def test_unified_routing_by_mode_and_cascade(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctrl = DolphinController(tmp, {'KHAI_DOLPHIN_CREDIT_CLP': '285000'})
            gateway = LLMManager.__new__(LLMManager)
            gateway.dolphin = ctrl
            gateway.secrets = {}
            gateway.api_keys_pool = [
                {'type': 'gemini', 'key': 'k1', 'id': 'GEMINI_API_KEY'},
                {'type': 'gemini', 'key': 'k2', 'id': 'GEMINI_API_KEY_FALLBACK_2'},
            ]
            gateway._load_status = MagicMock(return_value={})
            gateway._mark_key_failed = MagicMock()

            # 1. En modo low (por defecto), todo va a Gemini; si k1 falla (429 o bloqueo), cascada a k2
            ctrl._set_mode_state('nano', mark_notified=True)
            gateway._generate_dolphin = MagicMock(return_value='{"action": "GENERAL_CHAT", "reply": "Dolphin"}')
            gateway._generate_gemini = MagicMock(side_effect=[
                ValueError('Gemini no devolvió candidatos (SAFETY)'),
                json.dumps({'reply': 'Respuesta desde k2.', 'tool_calls': []}),
            ])
            res_nano = Mind(gateway, _Memory()).process_message('Explícame esto')
            self.assertEqual(res_nano['reply'], 'Respuesta desde k2.')
            gateway._generate_dolphin.assert_not_called()
            self.assertEqual(gateway._generate_gemini.call_count, 2)

            # 2. En modo mid/high, toda llamada (JSON de Mind, texto de Pixy, etc.) va primero a Dolphin
            ctrl._set_mode_state('mid', mark_notified=True)
            gateway._generate_gemini.reset_mock()
            gateway._generate_dolphin = MagicMock(return_value=json.dumps({
                'reply': 'Respuesta directa de Dolphin en mid.', 'tool_calls': [],
            }))
            res_mid = Mind(gateway, _Memory()).process_message('Cuenta una historia')
            self.assertEqual(res_mid['reply'], 'Respuesta directa de Dolphin en mid.')
            gateway._generate_dolphin.assert_called_once()
            gateway._generate_gemini.assert_not_called()

    def test_failed_turn_does_not_poison_history_and_reports_error(self):
        mem = _Memory()
        llm = MagicMock()
        llm.dolphin = None
        llm.generate_content = MagicMock(side_effect=[
            ValueError('Unified Fallback agotó todos los intentos y llaves.'),
            json.dumps({
                'reply': 'Seguimos operando normalmente.', 'tool_calls': [],
            }),
        ])
        mind = Mind(llm, mem)
        failed = mind.process_message('Prompt bloqueado o fallido')
        self.assertIn('error', failed)
        self.assertEqual(mem.saved, [])

        ok = mind.process_message('Siguiente mensaje sano')
        self.assertNotIn('error', ok)
        self.assertEqual(ok['reply'], 'Seguimos operando normalmente.')
        self.assertNotIn('Prompt bloqueado o fallido', llm.generate_content.call_args_list[1].args[0])
        self.assertEqual(mem.saved[0], ('user_chat', 'Siguiente mensaje sano'))

    def test_gateway_rejects_public_or_redirected_endpoint(self):
        gateway = LLMManager.__new__(LLMManager)
        with self.assertRaises(ValueError):
            gateway._generate_dolphin('mensaje', 'http://8.8.8.8:11434')

        class _Redirect:
            def raise_for_status(self):
                raise __import__('requests').HTTPError('redirect')

        with patch('app.voice.gateway.llm_gateway.requests.post', return_value=_Redirect()) as send:
            with self.assertRaises(__import__('requests').HTTPError):
                gateway._generate_dolphin('mensaje', 'http://10.1.2.3:11434')
            self.assertFalse(send.call_args.kwargs['allow_redirects'])

    def test_dolphin_controller_mode_low_mid_high_and_fallback_banner(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctrl = DolphinController(tmp, {'KHAI_DOLPHIN_CREDIT_CLP': '285000'})
            ctrl._access_token = MagicMock(return_value='fake-token')
            ctrl._wait_operation = MagicMock()
            ctrl._wait_ollama_ready = MagicMock(return_value=True)
            ctrl._warmup_model = MagicMock()

            post_resp = MagicMock(ok=True, status_code=200)
            post_resp.json.return_value = {'name': 'op-insert-1'}

            with patch.object(ctrl, 'get_instance', return_value=None), \
                 patch('app.mind.brain.controller.requests.post', return_value=post_resp) as post_mock:
                msg_mid = ctrl.set_mode('mid')
                self.assertEqual(msg_mid, '[Modo mid conectado]')
                self.assertTrue(post_mock.call_args.kwargs['json']['disks'][0]['boot'])
                self.assertFalse(post_mock.call_args.kwargs['json']['disks'][0]['autoDelete'])

                msg_high = ctrl.set_mode('high')
                self.assertEqual(msg_high, '[Modo high conectado]')

            del_resp = MagicMock(status_code=200)
            del_resp.json.return_value = {'name': 'op-del-1'}
            with patch.object(ctrl, 'get_instance', return_value={'machineType': 'zones/us-west1-a/machineTypes/g2-standard-4', 'status': 'RUNNING'}), \
                 patch('app.mind.brain.controller.requests.delete', return_value=del_resp):
                msg_nano = ctrl.set_mode('nano')
                self.assertEqual(msg_nano, '[Modo nano conectado]')
                status_text = ctrl.status()
                self.assertTrue(status_text.startswith('[Modo '))
                self.assertIn('Crédito estimado disponible', status_text)

            # Fallback automático de mid/high -> low cuando Dolphin se apaga por inactividad
            ctrl._set_mode_state('high', mark_notified=True)
            ctrl._delete_instance = MagicMock()
            gateway = LLMManager.__new__(LLMManager)
            gateway.dolphin = ctrl
            gateway.secrets = {}
            gateway.api_keys_pool = [{'type': 'gemini', 'key': 'fake', 'id': 'GEMINI_API_KEY'}]
            gateway._load_status = MagicMock(return_value={})
            gateway._generate_dolphin = MagicMock(side_effect=__import__('requests').ConnectionError('VM apagada'))
            gateway._generate_gemini = MagicMock(return_value='Respuesta desde Gemini.')

            with patch.dict('os.environ', {'KHAI_DOLPHIN_URL': 'http://10.200.0.2:11434'}):
                first_reply = gateway.generate_content('Hola')
                self.assertEqual(
                    first_reply,
                    '[Modo nano conectado]\nRespuesta desde Gemini.',
                )
                self.assertEqual(ctrl.get_active_mode(), 'nano')
                ctrl._delete_instance.assert_called_once_with(wait=False)

                # El siguiente turno ya está en low: no repite el banner ni intenta contactar a Dolphin
                gateway._generate_dolphin.reset_mock()
                second_reply = gateway.generate_content('Sigue')
                self.assertEqual(second_reply, 'Respuesta desde Gemini.')
                gateway._generate_dolphin.assert_not_called()

            mgr = SimpleNamespace(
                coordinator=MagicMock(),
                dolphin=ctrl,
                heart=SimpleNamespace(run_diagnostic_fast=lambda: {
                    'vitals': {'status': 'ok', 'ram_percent': 31.2, 'disk_percent': 24.0, 'disk_free': 10 * 1024**3},
                    'integrity': {'status': 'ok', 'databases': {'control.sqlite3': 'ok'}},
                }),
                recovery_executor=MagicMock(),
                secret_capture=SimpleNamespace(capture=MagicMock(side_effect=['secret_u1', 'secret_p1'])),
            )
            mgr.coordinator.store.all.return_value = [{'id': 'task_1', 'state': 'RUNNING'}]
            with patch.object(ctrl, 'status', return_value='[Mode = low]\nMotor activo: Gemini'):
                self.assertEqual(_command(mgr, 'telegram:1', '/m ?'), '[Mode = low]\nMotor activo: Gemini')
                self.assertEqual(_command(mgr, 'telegram:1b', '/M?'), '[Mode = low]\nMotor activo: Gemini')
                self.assertEqual(_command(mgr, 'telegram:2', '/mode ?'), '[Mode = low]\nMotor activo: Gemini')
                self.assertTrue(urgent_command('/M?'))
            self.assertIn('Cancelación solicitada: task_1', _command(mgr, 'telegram:3', '/stop'))
            self.assertIn('Estado Heart: ok', _command(mgr, 'telegram:4', '/health'))
            self.assertIn('Estado Heart: ok', _command(mgr, 'telegram:4b', '/health@Khaiiabot'))
            self.assertTrue(urgent_command('/health@Khaiiabot'))
            self.assertIn('Reinicio de contenedores Docker (core, proxy) programado', _command(mgr, 'telegram:5', '/restart'))
            self.assertEqual(_command(mgr, 'telegram:6', '/tasks'), 'Comando no disponible. Consulta /help.')

            from app.pixy.pixy import PixyMind
            from app.voice.voice import secret_command
            self.assertIn('ingresa tu usuario con /us y tu contraseña con /pas',
                          PixyMind.secret_instruction(['username', 'password']).lower())
            sec_reply = secret_command(mgr, '/us carlos /pas clave123', 'owner', 'chat1')
            self.assertEqual(sec_reply, 'Credenciales guardadas. Recibos: secret_u1, secret_p1')

    def test_dolphin_activation_requires_successful_warmup(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctrl = DolphinController(tmp, {'KHAI_DOLPHIN_CREDIT_CLP': '285000'})
            ctrl._access_token = MagicMock(return_value='fake-token')
            ctrl._wait_operation = MagicMock()
            ctrl._confirm_ready = MagicMock(side_effect=TimeoutError('cold start'))
            ctrl._delete_instance = MagicMock()
            create = MagicMock(ok=True, status_code=200)
            create.json.return_value = {'name': 'op-create'}

            with patch.object(ctrl, 'get_instance', return_value=None), \
                 patch('app.mind.brain.controller.requests.post', return_value=create):
                with self.assertRaises(ValueError) as raised:
                    ctrl.set_mode('mid')

            self.assertIn('[Modo nano conectado]', str(raised.exception))
            self.assertIn('continúa con Gemini', str(raised.exception))
            self.assertEqual(ctrl.get_active_mode(), 'nano')
            ctrl._delete_instance.assert_called_once_with(wait=True)

    def test_failed_cascade_notifies_each_failure_and_nano_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctrl = DolphinController(tmp, {})
            ctrl.start = MagicMock(side_effect=TimeoutError('cold start'))
            ctrl._rollback_activation = MagicMock()
            notify = MagicMock()
            with self.assertRaises(ValueError) as raised:
                ctrl.set_mode('mid', notifier=notify)
            sent = [call.args[0] for call in notify.call_args_list]
            self.assertEqual(sum('Modo mid falló' in s for s in sent), 1)
            self.assertEqual(sum('Modo low falló' in s for s in sent), 1)
            self.assertFalse(any('Modo nano conectado' in s for s in sent))
            self.assertEqual(str(raised.exception), '[Modo nano conectado]')

    def test_dolphin_warmup_uses_minimal_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctrl = DolphinController(tmp, {
                'KHAI_DOLPHIN_WARMUP_TIMEOUT': '120',
                'KHAI_DOLPHIN_MODEL': 'dolphin3:8b',
            })
            response = MagicMock()
            response.json.return_value = {'response': 'OK', 'load_duration': 123,
                                          'eval_count': 1, 'total_duration': True,
                                          'prompt_eval_duration': 'not a metric'}
            with patch('app.mind.brain.controller.requests.post', return_value=response) as post:
                metrics = ctrl._warmup_model()
            self.assertEqual(metrics, {'load_duration': 123, 'eval_count': 1})
            payload = post.call_args.kwargs['json']
            self.assertEqual(payload['model'], 'dolphin3:8b')
            self.assertEqual(payload['options']['num_predict'], 1)
            self.assertEqual(post.call_args.kwargs['timeout'], 120)
            self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_pixy_natural_language_and_llm_fallback(self):
        from app.pixy.pixy import PixyMind
        recovery_mock = SimpleNamespace(
            recent=lambda limit=3: [{'case_id': 'case-1', 'state': 'DIAGNOSED', 'request': 'clima',
                                'diagnosis': {'findings': [{'kind': 'task_failure', 'detail': 'Missing action'}]},
                                'plan': {'action': 'retry_task', 'target': 'task', 'summary': 'Reintentar', 'requires_approval': False}}],
            open_case=lambda req, requested_by='telegram', propose=False, kind='pixy_report': {
                'case_id': 'case-2',
                'diagnosis': {'findings': [{'kind': 'task_failure', 'detail': 'Missing action'}]},
                'plan': {'action': 'retry_task', 'target': 'task', 'summary': 'Reintentar', 'requires_approval': False},
            },
        )
        llm_mock = SimpleNamespace(generate_content=MagicMock(side_effect=[
            json.dumps({'action': 'REPLY', 'reply': 'Significa que falló la planificación de la tarea.'}),
            json.dumps({'action': 'DIAGNOSE', 'reply': ''}),
            ValueError('Unified Fallback agotó todos los intentos y llaves.'),
        ]))
        mgr = SimpleNamespace(
            llm=llm_mock,
            telegram=MagicMock(),
            audit_db=None,
            state=None,
            pixy_control=None,
            authorizations=None,
            coordinator=SimpleNamespace(store=SimpleNamespace(
                all=lambda: [{'id': 'task_1', 'state': 'FAILED', 'request': 'Va a llover?', 'error': 'Missing action'}],
                has_command=lambda s: False,
            )),
            heart=SimpleNamespace(recovery=recovery_mock),
            rules=None,
        )
        pixy = PixyMind(mgr)
        self.assertEqual(pixy.direct_response('que significa eso?'), 'Significa que falló la planificación de la tarea.')
        self.assertEqual(pixy.direct_response('que paso recien?'), 'task_failure: Missing action')
        fallback_reply = pixy.direct_response('dime algo cuando el LLM cae')
        self.assertIn('Pixy (modo contingencia sin LLM)', fallback_reply)
        self.assertIn('task_1', fallback_reply)

    def test_zero_or_n_action_planner_and_heart_llm_remediation(self):
        from app.mind.action_planner import ActionPlanner
        from app.heart.recovery import Recovery
        from app.pixy.authorization import AuthorizationStore
        from app.mind.coordinator import Coordinator

        catalog = SimpleNamespace(active=lambda: [])
        llm_zero = SimpleNamespace(generate_content=MagicMock(return_value=json.dumps({'steps': []})))
        self.assertIsNone(ActionPlanner(llm_zero, catalog).propose('Hola como estas', lambda _: True))

        llm_one = SimpleNamespace(generate_content=MagicMock(return_value=json.dumps({
            'steps': [{
                'id': 'clima',
                'use_existing_skill': None,
                'new_skill': {
                    'skill_id': 'clima-actual',
                    'description': 'Consulta el pronóstico de lluvia para la ciudad indicada',
                    'capabilities': ['Network'],
                    'inputs': {'ciudad': 'string'},
                    'outputs': {'pronostico': 'string'},
                },
            }],
            'inputs': {'ciudad': 'Santiago'},
        })))
        plan_one = ActionPlanner(llm_one, catalog).propose('Va a llover?', lambda _: True)
        self.assertIsNotNone(plan_one)
        self.assertEqual(len(plan_one['recipe']['steps']), 1)
        self.assertIn('clima', plan_one['missing_specs'])

        with tempfile.TemporaryDirectory() as tmp:
            db_path = f'{tmp}/control.sqlite3'
            auth_store = AuthorizationStore(db_path)
            llm_heart = SimpleNamespace(generate_content=MagicMock(return_value=json.dumps({
                'cause_code': 'syntax_or_code',
                'root_cause': 'Fallo en ROUTING antes de generar manifest',
                'corrective_instruction': 'Crear skill de clima con Open-Meteo',
                'steps': [{'id': 'p1', 'new_skill': {'skill_id': 'fix-clima', 'description': 'Repara consulta de clima'}}],
            })))
            hand_mock = SimpleNamespace(llm=llm_heart, catalog=catalog)
            rec = Recovery(db_path, MagicMock(), hand_mock, auth_store)
            evidence = {
                'storage': {'vitals': {'ram_percent': 30, 'disk_free': 10 * 1024**3}, 'integrity': {'status': 'ok'}},
                'container_core': {'running': True},
                'container_proxy': {'running': True, 'scheduler_age': 2},
                'core_logs': {'text': 'ERROR Mind fallo'},
                'pixy': {'recent_tasks': [
                    {'id': 'task_' + '1' * 32, 'state': 'FAILED', 'request': 'vieja', 'created_at': 100, 'updated_at': 100},
                    {'id': 'task_' + '2' * 32, 'state': 'FAILED', 'request': 'nueva sin manifest', 'created_at': 200, 'updated_at': 300},
                ]},
            }
            diag = rec.diagnose(evidence)
            tf = next(f for f in diag['findings'] if f['code'] == 'task_failure')
            self.assertEqual(tf['remedy'], 'retry_task:task_' + '2' * 32)
            self.assertIn('Open-Meteo', tf['corrective_instruction'])

            # Verificar que retry_task sin manifest reanuda en ROUTING (sin KeyError)
            mgr = SimpleNamespace(
                state=SimpleNamespace(db_path=db_path),
                events=None,
                rules=None,
                memory=None,
                runtime=MagicMock(),
                data_dir=tmp,
                llm=llm_heart,
                telegram=MagicMock(),
            )
            coord = Coordinator(mgr)
            t_no_manifest = coord.store.create('tg:1', 'Va a llover?', state='FAILED', error='Missing action')
            from app.heart.catalog import plan_hash, task_binding
            binding = task_binding(t_no_manifest)
            plan = {
                'target': 'task',
                'steps': [{'action': 'remediate_task', 'target': 'task'}],
                'capabilities': ['TaskRemediate'],
                'task_id': t_no_manifest['id'],
                'task_binding': binding,
                'corrective_instruction': 'Usar plantilla de 1 paso',
                'proposed_steps': [],
            }
            cid = 'case-1111222233334444'
            h = plan_hash(cid, plan)
            order = {
                'header': {'case_id': cid, 'issued_by': 'heart', 'authorization_id': 'auth_1',
                           'order_id': 'order_1', 'nonce': 'nonce_1', 'plan_hash': h},
                'plan': plan
            }
            mgr.hand = MagicMock()
            mgr.hand.remediate_task.side_effect = lambda ord, tr: tr(t_no_manifest)
            retried = coord.retry_task(t_no_manifest['id'], authorization_id='auth_1',
                                       corrective_instruction='Usar plantilla de 1 paso',
                                       proposed_steps=[], order=order)
            self.assertEqual(retried['state'], 'ROUTING')
            self.assertIn('Usar plantilla de 1 paso', retried['feedback'])
            coord._llm_executor.shutdown(wait=True, cancel_futures=True)
            coord._mailbox_executor.shutdown(wait=True, cancel_futures=True)


    def test_pixy_governance_without_copying_ids(self):
        from app.pixy.pixy import PixyMind
        from app.pixy.rules import Rules
        from app.voice.gateway.service_registry import ServiceRegistry, PixyServicePort
        from app.mind.recipe import RevisionPlanner
        from app.heart.recovery import Recovery
        with tempfile.TemporaryDirectory() as tmp:
            rules = Rules(tmp + '/rules.sqlite3')
            registry = ServiceRegistry(tmp + '/services.sqlite3')
            spec = {'service_id': 'weather', 'operation': 'forecast', 'version': 'v1',
                    'url': 'https://weather.example.org/forecast', 'method': 'GET',
                    'parameters': ['city'], 'content_types': ['application/json'],
                    'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
                    'effect': 'read', 'credential_ref': None, 'credential_header': 'Authorization',
                    'credential_prefix': '', 'effect_contract': None}
            registry.propose(spec)
            rid = rules.propose('Responder siempre en Celsius', 'owner')
            llm = MagicMock()
            mgr = SimpleNamespace(llm=llm, telegram=MagicMock(), rules=rules,
                                  pixy_services=PixyServicePort(registry),
                                  audit_db=tmp + '/pixy.sqlite3')
            pixy = PixyMind(mgr)
            reply = pixy.direct_response('aprueba el servicio propuesto', source='owner:svc')
            self.assertIn('aprobado', reply)
            self.assertEqual(registry.all()[0]['status'], 'approved')
            reply = pixy.direct_response('aprueba la regla', source='owner:rule')
            self.assertIn('Celsius', reply)
            self.assertEqual(rules.get(rid)['status'], 'active')
            llm.generate_content.return_value = json.dumps({'action': 'REPLY', 'reply': 'Hola, dime.'})
            self.assertEqual(pixy.direct_response('hola', source='owner:hi'), 'Hola, dime.')
            # RevisionPlanner acepta valores parafraseados por el LLM
            planner = RevisionPlanner(MagicMock())
            planner.llm.generate_content.return_value = json.dumps(
                {'required': ['folder'], 'provided': {'folder': 'Trabajo'}})
            self.assertEqual(planner.effect_inputs('ponla en la carpeta trabajo',
                                                   {'folder': {}})['provided'], {'folder': 'Trabajo'})
            self.assertEqual(Recovery.select_evidence_resources('memoria'),
                             ['storage', 'container_state_core', 'container_state_proxy', 'core_logs', 'pixy'])

    def test_help_command_returns_structured_buttons(self):
        from app.voice.voice import _command
        from app.voice.help import HELP_TEXT, HELP_BUTTONS
        mgr = SimpleNamespace(coordinator=MagicMock())
        res = _command(mgr, 'telegram:1', '/help')
        self.assertIsInstance(res, dict)
        self.assertEqual(res['text'], HELP_TEXT)
        self.assertEqual(res['buttons'], HELP_BUTTONS)

        res_q = _command(mgr, 'telegram:2', '/?')
        self.assertIsInstance(res_q, dict)
        self.assertEqual(res_q['text'], HELP_TEXT)
        self.assertEqual(res_q['buttons'], HELP_BUTTONS)

    def test_mode_command_notifies_transition_before_activation(self):
        from app.voice.voice import _command
        ctrl = MagicMock()
        ctrl.set_mode.return_value = '[Modo mid conectado]'
        coord = MagicMock()
        mgr = SimpleNamespace(coordinator=coord, dolphin=ctrl)
        res = _command(mgr, 'telegram:1', '/mode mid')
        coord.notify.assert_called_once_with('[Conectando con Modo mid]')
        ctrl.set_mode.assert_called_once_with('mid', notifier=coord.notify)
        self.assertEqual(res, '[Modo mid conectado]')

    def test_transparent_llm_cascade_and_busy_notification(self):
        notified = []
        gateway = LLMManager.__new__(LLMManager)
        gateway.dolphin = None
        gateway.secrets = {}
        gateway.notifier = lambda msg: notified.append(msg)
        gateway.api_keys_pool = [
            {'type': 'gemini', 'key': 'k1', 'id': 'GEMINI_API_KEY'},
            {'type': 'gemini', 'key': 'k2', 'id': 'GEMINI_API_KEY_FALLBACK_2'},
        ]
        gateway._load_status = MagicMock(return_value={})
        gateway._mark_key_failed = MagicMock()
        gateway._generate_gemini = MagicMock(side_effect=Exception('429 Quota Exceeded'))
        gateway._dolphin_endpoint = MagicMock(return_value='')

        with self.assertRaises(ValueError) as ctx:
            gateway.generate_content('hola')

        self.assertIn('Todos los LLM están ocupados', str(ctx.exception))
        self.assertIn('Gemini no responde, pasando a Gemini Fallback', notified)
        self.assertIn('Gemini Fallback no responde, pasando a Dolphin Medium', notified)
        self.assertIn('Todos los LLM están ocupados, por favor espere.', notified)

    def test_terminal_content_rejection_does_not_start_dolphin(self):
        from app.voice.gateway.llm_gateway import LLMContentRejected

        notified = []
        gateway = LLMManager.__new__(LLMManager)
        gateway.dolphin = MagicMock()
        gateway.dolphin.get_active_mode.return_value = 'nano'
        gateway.secrets = {}
        gateway.notifier = notified.append
        gateway.api_keys_pool = [
            {'type': 'gemini', 'key': 'k1', 'id': 'GEMINI_API_KEY'},
            {'type': 'gemini', 'key': 'k2', 'id': 'GEMINI_API_KEY_FALLBACK_2'},
        ]
        gateway._load_status = MagicMock(return_value={})
        gateway._generate_gemini = MagicMock(side_effect=LLMContentRejected('SAFETY'))
        gateway._dolphin_endpoint = MagicMock(return_value='http://10.200.0.2:11434')

        with self.assertRaises(LLMContentRejected):
            gateway.generate_content('contenido rechazado')

        gateway.dolphin.start.assert_not_called()
        self.assertTrue(any('rechazada' in message for message in notified))

    def test_provider_cooldown_prevents_immediate_retry(self):
        gateway = LLMManager.__new__(LLMManager)
        gateway.dolphin = None
        gateway.secrets = {}
        gateway.notifier = MagicMock()
        gateway.api_keys_pool = [
            {'type': 'gemini', 'key': 'k1', 'id': 'GEMINI_API_KEY'},
        ]
        gateway._load_status = MagicMock(return_value={'GEMINI_API_KEY': time.time() + 30})
        gateway._generate_gemini = MagicMock()
        gateway._dolphin_endpoint = MagicMock(return_value='')

        with self.assertRaisesRegex(ValueError, 'Todos los LLM están ocupados'):
            gateway.generate_content('hola')

        gateway._generate_gemini.assert_not_called()



    def test_brain_mode_aliases(self):
        ctrl = ModelController(data_dir=tempfile.gettempdir())
        ctrl.stop = MagicMock(return_value='[Modo nano conectado]')
        ctrl.start = MagicMock(side_effect=lambda mode: f"[Modo {mode} conectado]")

        self.assertEqual(ctrl.set_mode('l'), '[Modo low conectado]')
        self.assertEqual(ctrl.set_mode('m'), '[Modo mid conectado]')
        self.assertEqual(ctrl.set_mode('h'), '[Modo high conectado]')

    def test_brain_invalid_mode_handling(self):
        ctrl = ModelController(data_dir=tempfile.gettempdir())
        mgr = SimpleNamespace(brain=ctrl, coordinator=MagicMock())
        with self.assertRaises(ValueError):
            _command(mgr, 'telegram:1', '/mode invalid_foo')


class CancelableTransitionTests(unittest.TestCase):
    @staticmethod
    def wait_until(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError('Transition did not reach the expected state')

    def test_cancel_during_warmup_preserves_nano_and_fences_late_publish(self):
        from threading import Event
        from app.mind.brain.transitions import TransitionManager
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            entered, release = Event(), Event()
            notifications = []
            manager = TransitionManager(controller, lambda: True)
            def warmup(profile):
                entered.set()
                self.assertTrue(release.wait(5))
                return {'load_duration': 42}
            try:
                with patch.object(controller, 'get_instance', return_value={'status': 'RUNNING', 'machineType': 'g2-standard-8'}), \
                     patch.object(controller, '_wait_ollama_ready', return_value=True), \
                     patch.object(controller, '_warmup_model', side_effect=warmup), \
                     patch('app.mind.brain.controller.requests.delete') as delete:
                    receipt = manager.request('high', notifications.append)
                    self.assertTrue(entered.wait(5))
                    start = time.monotonic()
                    cancelled = manager.cancel()
                    self.assertLess(time.monotonic() - start, 1)
                    self.assertEqual(cancelled['effective_mode'], 'nano')
                    release.set()
                    manager._executor.shutdown(wait=True)
                    self.assertEqual(controller.get_active_mode(), 'nano')
                    self.assertNotIn('[Modo high conectado]', notifications)
                    self.assertEqual(manager.snapshot()['transitions'][0]['status'], 'cancelled')
                    tree = manager.journal.tree(manager._cycle(receipt['id']))
                    attempts = [c for c in tree['cycles'] if c['kind'] == 'model_attempt']
                    self.assertEqual(attempts[0]['status'], 'cancelled')
                    delete.assert_not_called()
            finally:
                release.set()
                manager.shutdown(wait=True)

    def test_newer_request_supersedes_slow_candidate_and_only_it_connects(self):
        from threading import Event
        from app.mind.brain.transitions import TransitionManager
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            entered, release = Event(), Event()
            manager = TransitionManager(controller, lambda: True)
            sent = []
            def start(profile):
                if profile == 'high':
                    entered.set()
                    self.assertTrue(release.wait(5))
                controller._set_mode_state(profile)
                return 'connected'
            try:
                with patch.object(controller, '_start', side_effect=start):
                    first = manager.request('high', sent.append)
                    self.assertTrue(entered.wait(5))
                    second = manager.request('mid', sent.append)
                    self.assertGreater(second['generation'], first['generation'])
                    release.set()
                    self.wait_until(lambda: manager.snapshot()['transitions'][0]['status'] == 'completed')
                    self.assertEqual(controller.get_active_mode(), 'mid')
                    self.assertEqual([r['status'] for r in manager.snapshot()['transitions']], ['completed', 'cancelled'])
                    self.assertNotIn('[Modo high conectado]', sent)
                    self.assertEqual(sent.count('[Modo mid conectado]'), 1)
            finally:
                release.set()
                manager.shutdown(wait=True)

    def test_nano_failure_keeps_current_service_and_never_mutates_gcp(self):
        from app.mind.brain.transitions import TransitionManager
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            controller._set_mode_state('low')
            manager = TransitionManager(controller, MagicMock(side_effect=TimeoutError('secret transport details')))
            sent = []
            try:
                with patch.object(controller, 'start') as start, patch.object(controller, 'stop') as stop, \
                     patch.object(controller, 'get_instance', return_value={'status': 'RUNNING', 'machineType': 'e2-standard-8'}):
                    manager.request('high', sent.append)
                    self.wait_until(lambda: not manager.is_active() and sent and 'Nano no responde' in sent[-1])
                    self.assertEqual(controller.get_active_mode(), 'low')
                    self.assertEqual(manager.snapshot()['transitions'][0]['error_type'], 'NanoUnavailable')
                    start.assert_not_called()
                    stop.assert_not_called()
                    self.assertIn('Nano no responde', sent[-1])
                    self.assertNotIn('secret transport details', sent[-1])
            finally:
                manager.shutdown(wait=True)

    def test_owner_selected_local_profile_can_start_when_nano_and_vm_are_absent(self):
        from app.mind.brain.transitions import TransitionManager
        for remote in (None, {'status': 'TERMINATED', 'machineType': 'e2-standard-8'}):
            with self.subTest(remote=remote), tempfile.TemporaryDirectory() as directory:
                controller = ModelController(directory, {})
                manager = TransitionManager(controller, MagicMock(side_effect=TimeoutError('Nano down')))
                def start(profile):
                    controller._set_mode_state(profile)
                    return 'connected'
                try:
                    with patch.object(controller, 'get_instance', return_value=remote), \
                         patch.object(controller, '_start', side_effect=start) as activate:
                        receipt = manager.request('low')
                        self.wait_until(lambda: not manager.is_active())
                        self.assertEqual(manager.snapshot()['transitions'][0]['status'], 'completed')
                        self.assertEqual(controller.get_active_mode(), 'low')
                        activate.assert_called_once_with('low')
                        events = manager.journal.tree(manager._cycle(receipt['id']))['events']
                        self.assertTrue(any(e['phase'] == 'nano_unavailable_owner_selected_profile' for e in events))
                        self.assertFalse(any(e['phase'] == 'nano_bridge_ready' for e in events))
                finally:
                    manager.shutdown(wait=True)

    def test_same_healthy_profile_validates_without_nano_or_vm_replacement(self):
        from app.mind.brain.transitions import TransitionManager
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            controller._set_mode_state('low')
            probe = MagicMock(side_effect=AssertionError('Nano must not be needed'))
            manager = TransitionManager(controller, probe)
            try:
                with patch.object(controller, 'get_instance', return_value={'status': 'RUNNING', 'machineType': 'e2-standard-8'}), \
                     patch.object(controller, '_wait_ollama_ready', return_value=True), \
                     patch.object(controller, '_warmup_model', return_value={'load_duration': 100}), \
                     patch('app.mind.brain.controller.requests.post') as post:
                    manager.request('low')
                    self.wait_until(lambda: not manager.is_active())
                    self.assertEqual(manager.snapshot()['transitions'][0]['status'], 'completed')
                    self.assertEqual(controller.get_active_mode(), 'low')
                    probe.assert_not_called()
                    post.assert_not_called()
                    warmups = [e for e in controller.transition_snapshot()['recent_transitions'] if e['phase'] == 'warmup_completed']
                    self.assertEqual(warmups[0]['load_duration'], 100)
            finally:
                manager.shutdown(wait=True)

    def test_same_running_profile_with_failed_readiness_does_not_destroy_service(self):
        from app.mind.brain.transitions import TransitionManager
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            controller._set_mode_state('low')
            probe = MagicMock()
            manager = TransitionManager(controller, probe)
            try:
                with patch.object(controller, 'get_instance', return_value={'status': 'RUNNING', 'machineType': 'e2-standard-8'}), \
                     patch.object(controller, '_wait_ollama_ready', return_value=True), \
                     patch.object(controller, '_warmup_model', side_effect=TimeoutError('not ready')), \
                     patch('app.mind.brain.controller.requests.delete') as delete, \
                     patch('app.mind.brain.controller.requests.post') as post:
                    manager.request('low')
                    self.wait_until(lambda: not manager.is_active())
                    self.assertEqual(manager.snapshot()['transitions'][0]['status'], 'failed')
                    self.assertEqual(controller.get_active_mode(), 'low')
                    probe.assert_not_called()
                    delete.assert_not_called()
                    post.assert_not_called()
            finally:
                manager.shutdown(wait=True)

    def test_failed_inference_does_not_try_other_machine_types(self):
        from app.mind.brain.transitions import TransitionManager
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            manager = TransitionManager(controller, lambda: True)
            inserted = MagicMock(ok=True, status_code=200)
            inserted.json.return_value = {'name': 'op-created'}
            try:
                with patch.object(controller, 'get_instance', return_value=None), \
                     patch.object(controller, '_headers', return_value={}), \
                     patch.object(controller, '_wait_operation'), \
                     patch.object(controller, '_confirm_ready', side_effect=RuntimeError('invalid warmup response')), \
                     patch('app.mind.brain.controller.requests.post', return_value=inserted) as post:
                    manager.request('high')
                    self.wait_until(lambda: not manager.is_active())
                    self.assertEqual(manager.snapshot()['transitions'][0]['status'], 'failed')
                    self.assertEqual(controller.get_active_mode(), 'nano')
                    self.assertEqual(post.call_count, 1)
            finally:
                manager.shutdown(wait=True)

    def test_mode_command_uses_managed_brain_and_stop_cancels_without_llm_wait(self):
        from threading import Event
        from app.mind.brain.transitions import TransitionManager
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            transitions = TransitionManager(controller, lambda: True)
            brain = Brain(SimpleNamespace(transitions=transitions), controller)
            entered, release = Event(), Event()
            sent = []
            coordinator = SimpleNamespace(notify=sent.append, store=SimpleNamespace(all=lambda: []),
                                          cancel=MagicMock(), _agent_active=lambda _: False)
            manager = SimpleNamespace(brain=brain, dolphin=controller, coordinator=coordinator,
                                      model_transitions=transitions)
            def prepare(profile):
                entered.set()
                self.assertTrue(release.wait(5))
                controller._set_mode_state(profile)
            try:
                with patch.object(controller, '_start', side_effect=prepare), \
                     patch.object(controller, 'set_mode', side_effect=AssertionError('Legacy cascade must not run')):
                    requested = _command(manager, 'telegram:managed-mode', '/mode high')
                    self.assertIn('transition_', requested)
                    self.assertTrue(entered.wait(5))
                    start = time.monotonic()
                    stopped = _command(manager, 'telegram:managed-stop', '/stop')
                    self.assertLess(time.monotonic() - start, 1)
                    self.assertIn('Cambio de modelo cancelado', stopped)
                    release.set()
                    transitions._executor.shutdown(wait=True)
                    self.assertEqual(controller.get_active_mode(), 'nano')
                    self.assertNotIn('[Modo high conectado]', sent)
                    coordinator.cancel.assert_not_called()
                    self.assertTrue(urgent_command('/mode high'))
                    self.assertTrue(urgent_command('/stop'))
            finally:
                release.set()
                transitions.shutdown(wait=True)

    def test_cancelled_remote_post_receipt_is_reconciled_without_replaying(self):
        from threading import Event
        from app.mind.brain.transitions import TransitionManager
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            manager = TransitionManager(controller, lambda: True)
            entered, release = Event(), Event()
            response = MagicMock(ok=True, status_code=200)
            response.json.return_value = {'name': 'op-late'}
            def submit(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return response
            done = MagicMock()
            done.json.return_value = {'status': 'DONE'}
            try:
                with patch.object(controller, 'get_instance', return_value=None), \
                     patch.object(controller, '_headers', return_value={}), \
                     patch('app.mind.brain.controller.requests.post', side_effect=submit) as post, \
                     patch('app.mind.brain.transitions.requests.get', return_value=done) as read:
                    manager.request('high')
                    self.assertTrue(entered.wait(5))
                    manager.cancel()
                    release.set()
                    manager.request('nano')
                    self.wait_until(lambda: manager.snapshot()['transitions'][0]['status'] == 'completed')
                    self.assertEqual(post.call_count, 1)
                    self.assertIn('operations/op-late', read.call_args.args[0])
                    self.assertEqual(manager.snapshot()['pending_operations'], [])
                    self.assertEqual(controller.get_active_mode(), 'nano')
            finally:
                release.set()
                manager.shutdown(wait=True)

    def test_transport_timeout_without_receipt_blocks_next_mutation(self):
        from app.mind.brain.transitions import TransitionManager
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            manager = TransitionManager(controller, lambda: True)
            try:
                with patch.object(controller, 'get_instance', return_value=None), \
                     patch.object(controller, '_headers', return_value={}), \
                     patch('app.mind.brain.controller.requests.post', side_effect=__import__('requests').Timeout('unknown')) as post:
                    manager.request('high')
                    self.wait_until(lambda: not manager.is_active())
                    manager.request('mid')
                    self.wait_until(lambda: not manager.is_active())
                    self.assertEqual(post.call_count, 1)
                    self.assertEqual(manager.snapshot()['transitions'][0]['status'], 'unknown')
                    self.assertEqual(manager.snapshot()['transitions'][0]['error_type'], 'RemoteOutcomeUnknown')
                    self.assertEqual(controller.get_active_mode(), 'nano')
            finally:
                manager.shutdown(wait=True)

    def test_restart_marks_incomplete_change_uncertain_without_replay(self):
        from app.kernel.database import connection
        from app.mind.brain.transitions import TransitionManager
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            manager = TransitionManager(controller, lambda: True)
            manager.shutdown(wait=True)
            with connection(manager.path) as db:
                identifier, now = 'transition-interrupted', time.time()
                db.execute('INSERT INTO model_transitions VALUES (?,?,?,?,?,?,?,?,NULL)',
                           (identifier, 1, 'high', 'nano', 'active', 'preparing_target', now, now))
                manager.journal.start('model_transition', identifier, conn=db)
            controller2 = ModelController(directory, {})
            with patch.object(controller2, 'get_instance') as read, patch.object(controller2, 'start') as start:
                restarted = TransitionManager(controller2, MagicMock())
                try:
                    self.assertEqual(restarted.snapshot()['transitions'][0]['status'], 'unknown')
                    self.assertEqual(controller2.get_active_mode(), 'nano')
                    read.assert_not_called()
                    start.assert_not_called()
                    self.assertEqual(restarted.journal.get(restarted._cycle(identifier))['status'], 'unknown')
                finally:
                    restarted.shutdown(wait=True)

    def test_transition_worker_keeps_parent_trace_and_late_fallback_cannot_override(self):
        from app.kernel.tracing import trace_scope
        from app.mind.brain.transitions import TransitionManager
        with tempfile.TemporaryDirectory() as directory:
            controller = ModelController(directory, {})
            manager = TransitionManager(controller, lambda: True)
            def start(profile):
                controller._set_mode_state(profile)
                return 'connected'
            try:
                old_generation = manager.snapshot()['generation']
                parent = manager.journal.start('interaction', 'fixture-parent', trace_id='req_transition-trace')
                with trace_scope('req_transition-trace'), manager.journal.activate(parent), \
                     patch.object(controller, '_start', side_effect=start):
                    receipt = manager.request('high')
                    self.wait_until(lambda: not manager.is_active())
                tree = manager.journal.tree(parent)
                transitions = [c for c in tree['cycles'] if c['object_id'] == receipt['id']]
                self.assertEqual(transitions[0]['parent_id'], parent)
                self.assertTrue(all(c['trace_id'] == 'req_transition-trace' for c in tree['cycles']))
                self.assertFalse(manager.publish_nano_fallback('high', old_generation))
                self.assertEqual(controller.get_active_mode(), 'high')
                generation = manager.snapshot()['generation']
                self.assertTrue(manager.publish_nano_fallback('high', generation))
                self.assertEqual(controller.get_active_mode(), 'nano')
            finally:
                manager.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
