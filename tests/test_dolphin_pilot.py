"""Piloto: Dolphin redacta charla; Gemini conserva la clasificación y Voice controla la VM Spot."""
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.mind.mind import Mind
from app.voice.gateway.dolphin_controller import DolphinController
from app.voice.gateway.llm_gateway import LLMManager
from app.voice.voice import _command


class _Memory:
    def __init__(self):
        self.saved = []

    def append_long_term(self, kind, value):
        self.saved.append((kind, value))

    def get_chat_history(self, limit=100):
        return ''

    def get_system_prompt(self):
        return 'Habla de forma concisa.'


class _Provider:
    def __init__(self, action):
        self.action = action
        self.calls = []

    def generate_content(self, prompt, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get('purpose') == 'conversation':
            return 'Respuesta de Dolphin.'
        return json.dumps({'is_failure_analysis': False, 'normal_intent': {
            'reasoning_summary': 'Clasificación', 'action': self.action,
            'reply': 'Respuesta de Gemini.', 'extracted_goal': 'Objetivo' if self.action == 'ACTION_REQUIRED' else '',
        }})


class DolphinPilotTests(unittest.TestCase):
    def test_only_conversation_is_sent_to_dolphin(self):
        chat = _Provider('GENERAL_CHAT')
        self.assertEqual(Mind(chat, _Memory()).process_message('Explícame esto')['reply'],
                         'Respuesta de Dolphin.')
        self.assertEqual([call.get('purpose') for call in chat.calls], [None, 'conversation'])

        action = _Provider('ACTION_REQUIRED')
        self.assertEqual(Mind(action, _Memory()).process_message('Haz una tarea')['reply'],
                         'Respuesta de Gemini.')
        self.assertEqual(len(action.calls), 1)

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

            post_resp = MagicMock(ok=True, status_code=200)
            post_resp.json.return_value = {'name': 'op-insert-1'}

            with patch.object(ctrl, 'get_instance', return_value=None), \
                 patch('app.voice.gateway.dolphin_controller.requests.post', return_value=post_resp) as post_mock:
                msg_mid = ctrl.set_mode('mid')
                self.assertTrue(msg_mid.startswith('[Mode = mid]'))
                self.assertIn('e2-standard-8', msg_mid)
                self.assertIn('CUBIERTO POR CREDITOS', msg_mid)
                self.assertTrue(post_mock.call_args.kwargs['json']['disks'][0]['boot'])
                self.assertFalse(post_mock.call_args.kwargs['json']['disks'][0]['autoDelete'])

                msg_high = ctrl.set_mode('high')
                self.assertTrue(msg_high.startswith('[Mode = high]'))
                self.assertIn('g2-standard-4', msg_high)
                self.assertIn('NVIDIA L4', msg_high)

            del_resp = MagicMock(status_code=200)
            del_resp.json.return_value = {'name': 'op-del-1'}
            with patch.object(ctrl, 'get_instance', return_value={'machineType': 'zones/us-west1-a/machineTypes/g2-standard-4', 'status': 'RUNNING'}), \
                 patch('app.voice.gateway.dolphin_controller.requests.delete', return_value=del_resp):
                msg_low = ctrl.set_mode('low')
                self.assertTrue(msg_low.startswith('[Mode = low]'))
                self.assertIn('Instancia Spot de Dolphin eliminada', msg_low)
                self.assertIn('Crédito estimado disponible', msg_low)

            # Fallback automático de mid/high -> low cuando Dolphin se apaga por inactividad
            ctrl._set_mode_state('high', mark_notified=True)
            gateway = LLMManager.__new__(LLMManager)
            gateway.dolphin = ctrl
            gateway.api_keys_pool = [{'type': 'gemini', 'key': 'fake', 'id': 'GEMINI_API_KEY'}]
            gateway._load_status = MagicMock(return_value={})
            gateway._generate_dolphin = MagicMock(side_effect=__import__('requests').ConnectionError('VM apagada'))
            gateway._generate_gemini = MagicMock(return_value='Respuesta desde Gemini.')

            with patch.dict('os.environ', {'KHAI_DOLPHIN_URL': 'http://10.200.0.2:11434'}):
                first_reply = gateway.generate_content('Hola', purpose='conversation')
                self.assertEqual(first_reply, '[Mode = low]\nRespuesta desde Gemini.')
                self.assertEqual(ctrl.get_active_mode(), 'low')

                # El siguiente turno ya está en low: no repite el banner ni intenta contactar a Dolphin
                gateway._generate_dolphin.reset_mock()
                second_reply = gateway.generate_content('Sigue', purpose='conversation')
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
                self.assertEqual(_command(mgr, 'telegram:2', '/mode ?'), '[Mode = low]\nMotor activo: Gemini')
            self.assertIn('Objetivos activos detenidos: task_1', _command(mgr, 'telegram:3', '/stop'))
            self.assertIn('Estado Heart: ok', _command(mgr, 'telegram:4', '/health'))
            self.assertIn('Reinicio de contenedores Docker (core, proxy) programado', _command(mgr, 'telegram:5', '/restart'))
            self.assertEqual(_command(mgr, 'telegram:6', '/tasks'), 'Comando no disponible. Consulta /help.')

            from app.pixy.pixy import PixyMind
            from app.voice.voice import secret_command
            self.assertIn('ingresa tu usuario con /us y tu contraseña con /pas',
                          PixyMind.secret_instruction(['username', 'password']).lower())
            sec_reply = secret_command(mgr, '/us carlos /pas clave123', 'owner', 'chat1')
            self.assertEqual(sec_reply, 'Credenciales guardadas. Recibos: secret_u1, secret_p1')


if __name__ == '__main__':
    unittest.main()
