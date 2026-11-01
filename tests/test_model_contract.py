"""Native router schema and production inference continuity boundaries."""
import json
import tempfile
import unittest
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from app.mind.mind import Mind
from app.mind.turn import AssistantTurn
from app.mind.brain import Brain
from app.voice.gateway.llm_gateway import LLMManager, ModelUnavailable


class ModelContractTests(unittest.TestCase):
    def gateway(self):
        gateway = object.__new__(LLMManager)
        gateway.dolphin = Mock()
        gateway.dolphin.get_model_for_mode.return_value = 'dolphin3:8b'
        gateway.dolphin.endpoint = 'http://10.200.0.2:11434'
        gateway._response_meta = ContextVar('test-response', default={})
        gateway.api_keys_pool = [{'type': 'gemini', 'key': 'fixture-only', 'id': 'GEMINI_API_KEY'}]
        gateway._load_status = Mock(return_value={})
        gateway._mark_key_failed = Mock()
        gateway._notify = Mock()
        gateway.transitions = Mock()
        gateway.transitions.snapshot.return_value = {'effective_mode': 'low', 'generation': 7}
        gateway.transitions.publish_nano_fallback.return_value = True
        return gateway

    def test_current_order_is_separated_from_stale_creation_and_schema_is_native(self):
        llm, memory = Mock(), Mock()
        memory.get_system_prompt.return_value = 'Identity'
        llm.generate_content.return_value = json.dumps({'tool_calls': [{'name': 'start_task',
            'arguments': {'goal': 'busca nueva consulta', 'capability': 'web_search'}}]})
        result = Mind(llm, memory).process_message('busca nueva consulta',
            conversation_history=['crea una skill antigua'])
        prompt = llm.generate_content.call_args.args[0]
        kwargs = llm.generate_content.call_args.kwargs
        self.assertIn('crea una skill antigua', prompt.split('FIN_CONTEXTO_DE_REFERENCIA')[0])
        self.assertNotIn('crea una skill antigua', prompt.split('MENSAJE_ACTUAL:')[1])
        self.assertIn('MENSAJE_ACTUAL', kwargs['system_instruction'])
        self.assertEqual(kwargs['response_schema'], AssistantTurn.model_json_schema())
        self.assertEqual(kwargs['temperature'], 0)
        self.assertNotIn('"$defs"', kwargs['system_instruction'])
        self.assertFalse(result['creation_requested'])

    def test_transport_type_error_after_submission_does_not_replay(self):
        gateway = self.gateway()
        gateway._generate_dolphin = Mock(side_effect=TypeError('response decoding failed'))
        with self.assertRaises(TypeError):
            gateway._call_dolphin('prompt', 'http://10.200.0.2:11434', {})
        gateway._generate_dolphin.assert_called_once()

    def test_ollama_receives_schema_system_and_exact_model_with_metrics(self):
        gateway = self.gateway()
        response = Mock()
        response.json.return_value = {'response': '{"reply":"Hola"}', 'total_duration': 123,
                                      'done_reason': 'stop'}
        with patch('app.voice.gateway.llm_gateway.requests.post', return_value=response) as post:
            result = gateway.generate_content('MENSAJE_ACTUAL: hola',
                response_format={'type': 'json_object'}, response_schema=AssistantTurn.model_json_schema(),
                system_instruction='router', temperature=0)
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['format'], AssistantTurn.model_json_schema())
        self.assertEqual(payload['system'], 'router')
        self.assertEqual(payload['model'], 'dolphin3:8b')
        self.assertEqual(payload['options']['temperature'], 0)
        self.assertEqual(gateway._response_meta.get()['total_duration'], 123)
        self.assertEqual(json.loads(result)['reply'], 'Hola')
        gateway.dolphin.start.assert_not_called()

    def test_invalid_envelope_is_evidence_not_silently_wrapped_or_retried(self):
        llm, memory = Mock(), Mock()
        memory.get_system_prompt.return_value = 'Identity'
        llm.generate_content.return_value = '{"name":"start_task","arguments":{"goal":"old"}}'
        llm.record_contract_failure.return_value = {'id': 'rejection_fixture'}
        result = Mind(llm, memory).process_message('busca nueva consulta', conversation_history=[])
        self.assertEqual(result['action'], 'ROUTING_ERROR')
        self.assertEqual(result['diagnostic']['id'], 'rejection_fixture')
        llm.generate_content.assert_called_once()
        self.assertIn('extra_forbidden', [e['type'] for e in llm.record_contract_failure.call_args.args[2]])

    def test_local_failure_uses_nano_once_and_fences_late_publication(self):
        gateway = self.gateway()
        gateway._call_dolphin = Mock(side_effect=requests.ReadTimeout())
        gateway._generate_gemini = Mock(return_value='nano answer')
        self.assertEqual(gateway.generate_content('hello'), 'nano answer')
        gateway._call_dolphin.assert_called_once()
        gateway._generate_gemini.assert_called_once()
        gateway.transitions.publish_nano_fallback.assert_called_once_with('low', 7)
        gateway.dolphin.start.assert_not_called()
        gateway.dolphin.mark_fallback_to_nano.assert_not_called()

    def test_nano_failure_never_starts_another_profile_or_provider(self):
        gateway = self.gateway()
        gateway.transitions.snapshot.return_value = {'effective_mode': 'nano', 'generation': 8}
        gateway._generate_gemini = Mock(side_effect=requests.ReadTimeout())
        gateway._generate_openai = Mock()
        gateway.api_keys_pool.append({'type': 'openai', 'key': 'fixture-only', 'id': 'OPENAI_API_KEY'})
        with self.assertRaisesRegex(ModelUnavailable, 'Elige el modelo'):
            gateway.generate_content('hello')
        gateway.dolphin.start.assert_not_called()
        gateway._generate_openai.assert_not_called()
        gateway.transitions.publish_nano_fallback.assert_not_called()

    def test_native_tool_turn_uses_live_local_without_restart(self):
        gateway = self.gateway()
        gateway._complete_dolphin = Mock(return_value={'content': 'ready', 'tool_calls': []})
        self.assertEqual(gateway.complete([{'role': 'user', 'content': 'hello'}])['content'], 'ready')
        gateway._complete_dolphin.assert_called_once()
        gateway.dolphin.start.assert_not_called()

    def test_nano_probe_requires_valid_receipt(self):
        gateway = self.gateway()
        gateway._generate_gemini = Mock(return_value='{"ready":false}')
        with self.assertRaises(ModelUnavailable):
            gateway.probe_nano()
        gateway.dolphin.start.assert_not_called()

    def test_brain_request_returns_immediate_identifier_and_cancel(self):
        gateway = self.gateway()
        gateway.transitions.request.return_value = {'id': 'transition_fixture', 'requested_mode': 'high',
                                                     'effective_mode': 'low'}
        brain = Brain(gateway, gateway.dolphin)
        self.assertIn('transition_fixture', brain.set_mode('high'))
        gateway.dolphin.set_mode.assert_not_called()
        brain.cancel_transition()
        gateway.transitions.cancel.assert_called_once()
    def test_thought_signature_uniformity_and_gemini_serialization(self):
        gateway = self.gateway()
        # Test serialization of tool call without thought_signature
        messages = [
            {'role': 'user', 'content': 'search weather'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'name': 'web_search', 'arguments': {'query': 'weather'}}]},
            {'role': 'tool', 'name': 'web_search', 'tool_call_id': 'call_1', 'content': 'sunny'}
        ]
        contents, system = gateway._gemini_contents(messages)
        self.assertEqual(len(contents), 3)
        self.assertEqual(contents[1]['role'], 'model')
        self.assertEqual(contents[1]['parts'][0]['thoughtSignature'], 'CgA=')
        self.assertEqual(contents[2]['role'], 'user')
        self.assertEqual(contents[2]['parts'][0]['functionResponse']['name'], 'web_search')

    def test_local_failure_notifies_nano_fallback_with_mode_buttons(self):
        gateway = self.gateway()
        from app.voice.help import MODE_SELECTION_BUTTONS
        gateway._call_dolphin = Mock(side_effect=requests.ReadTimeout())
        gateway._generate_gemini = Mock(return_value='nano answer')
        gateway.generate_content('hello')
        gateway._notify.assert_called_once_with(
            '[Error en modo low, pasando a Nano ¿deseas otro modo?]',
            buttons=MODE_SELECTION_BUTTONS
        )

