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

    def test_dolphin_controller_on_low_high_off_and_credit_tracking(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctrl = DolphinController(tmp, {'KHAI_DOLPHIN_CREDIT_CLP': '285000'})
            ctrl._access_token = MagicMock(return_value='fake-token')
            ctrl._wait_operation = MagicMock()
            ctrl._wait_ollama_ready = MagicMock(return_value=True)

            post_resp = MagicMock(ok=True, status_code=200)
            post_resp.json.return_value = {'name': 'op-insert-1'}

            with patch.object(ctrl, 'get_instance', return_value=None), \
                 patch('app.voice.gateway.dolphin_controller.requests.post', return_value=post_resp) as post_mock:
                msg_low = ctrl.start('low')
                self.assertIn('e2-standard-8', msg_low)
                self.assertIn('CUBIERTO POR CREDITOS', msg_low)
                self.assertTrue(post_mock.call_args.kwargs['json']['disks'][0]['boot'])
                self.assertFalse(post_mock.call_args.kwargs['json']['disks'][0]['autoDelete'])

                msg_high = ctrl.start('high')
                self.assertIn('g2-standard-4', msg_high)
                self.assertIn('NVIDIA L4', msg_high)

            del_resp = MagicMock(status_code=200)
            del_resp.json.return_value = {'name': 'op-del-1'}
            with patch.object(ctrl, 'get_instance', return_value={'machineType': 'zones/us-west1-a/machineTypes/g2-standard-4', 'status': 'RUNNING'}), \
                 patch('app.voice.gateway.dolphin_controller.requests.delete', return_value=del_resp):
                msg_off = ctrl.stop()
                self.assertIn('instancia Spot eliminada', msg_off)
                self.assertIn('Crédito estimado disponible', msg_off)

            mgr = SimpleNamespace(coordinator=MagicMock(), dolphin=ctrl)
            with patch.object(ctrl, 'status', return_value='Estado Dolphin: APAGADO'):
                self.assertEqual(_command(mgr, 'telegram:1', '/dolphin'), 'Estado Dolphin: APAGADO')
                self.assertEqual(_command(mgr, 'telegram:2', '/on status'), 'Estado Dolphin: APAGADO')


if __name__ == '__main__':
    unittest.main()
