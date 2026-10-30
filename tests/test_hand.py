"""Hand organ tests: skill contracts, recipe execution, setup/inputs, and service descriptions."""
import copy
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.fixtures import Fixture
from app.bootstrap import ModuleManager
from app.hand.hand import Hand
from app.hand.skill_spec import extract_params, spec_hash, validate_recipe, validate_skill_spec
from app.mind.action_planner import ActionPlanner
from app.mind.coordinator import Coordinator
from app.mind.result_presenter import present_recipe
from app.mind.skill_selector import SkillSelector
from app.pixy.control import ControlStore
from app.voice.gateway.external_gateway import ExternalGateway
from app.voice.gateway.service_registry import (
    HandServicePort,
    PixyServicePort,
    ServiceRegistry,
    VoiceServicePort,
)


def schema(**fields):
    return {'type': 'object', 'properties': {name: {'type': kind} for name, kind in fields.items()},
            'required': list(fields), 'additionalProperties': False}


def skill(skill_id, inputs, outputs, services=None):
    return {'skill_id': skill_id, 'description': 'Capacidad reutilizable con parámetros explícitos.',
            'use_cases': ['Resolver una solicitud equivalente con otros parámetros'],
            'input_schema': schema(**inputs), 'output_schema': schema(**outputs),
            'runtime': 'SAFE', 'dependencies': [], 'services': services or [],
            'verifier': {'kind': 'owner_criterion'}}


class SkillContractTests(unittest.TestCase):
    def test_network_task_waits_for_pixy_then_resumes_same_task(self):
        from app.bootstrap import ModuleManager
        from app.mind.coordinator import Coordinator
        from unittest.mock import patch
        descriptor = {'service_id': 'fx', 'operation': 'rate', 'version': 'v1',
            'url': 'https://example.org/rate', 'method': 'POST',
            'parameters': ['base', 'quote'], 'content_types': ['application/json'],
            'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
            'effect': 'write', 'credential_ref': None,
            'credential_header': 'Authorization', 'credential_prefix': '',
            'effect_contract': {'kind': 'other', 'identity': None, 'correction': None,
                                'summary': {'name': 'Tasa', 'context': 'Cambio de tasa'}}}
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data'
            (data / 'memory').mkdir(parents=True)
            (data / 'memory' / 'agent.md').write_text('Test identity', encoding='utf-8')
            with patch.dict('os.environ', {'KHAI_RUNTIME': 'local', 'TELEGRAM_BOT_TOKEN': 'token',
                                           'TELEGRAM_USER_ID': 'owner'}):
                manager = ModuleManager(data_dir=str(data))
                manager.telegram = Mock()
                manager.llm.generate_content = Mock(return_value=json.dumps(descriptor))
                manager.hand.describe_service = Mock(side_effect=lambda t: (descriptor, manager.hand.propose_service(descriptor)))
                coordinator = Coordinator(manager, Mock())
                manager.coordinator = coordinator
                try:
                    task = coordinator.store.create('service-task', 'modificar tasa',
                        state='GENERATING', manifest={'purpose': 'modificar tasa',
                                                      'capabilities': ['Network']})
                    coordinator.advance(task)
                    waiting = coordinator.store.get(task['id'])
                    self.assertEqual(waiting['state'], 'WAITING_SERVICE_APPROVAL')
                    self.assertEqual(manager.voice_services.active_services(), {})
                    digest = waiting['service_proposal']
                    review = json.loads(manager.pixy.direct_response('service show ' + digest,
                                                                     source='telegram:owner'))
                    self.assertEqual(review['write_summary_review'],
                                     descriptor['effect_contract']['summary'])
                    manager.pixy.direct_response('service approve ' + digest,
                                                 source='telegram:owner')
                    coordinator.advance(coordinator.store.get(task['id']))
                    resumed = coordinator.store.get(task['id'])
                    self.assertEqual(resumed['state'], 'GENERATING')
                    self.assertEqual(resumed['services'], [{
                        'service_id': 'fx', 'operation': 'rate', 'version': 'v1'}])
                    self.assertEqual(resumed['id'], task['id'])
                    self.assertTrue(coordinator._services_available(resumed['services']))
                finally:
                    coordinator._llm_executor.shutdown(wait=True)
                    coordinator._mailbox_executor.shutdown(wait=True)

    def test_network_task_auto_approves_public_read_service(self):
        from app.bootstrap import ModuleManager
        from app.mind.coordinator import Coordinator
        from unittest.mock import patch
        descriptor = {'service_id': 'fx', 'operation': 'rate', 'version': 'v1',
            'url': 'https://example.org/rate', 'method': 'GET',
            'parameters': ['base', 'quote'], 'content_types': ['application/json'],
            'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
            'effect': 'read', 'credential_ref': None,
            'credential_header': 'Authorization', 'credential_prefix': '',
            'effect_contract': None}
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data'
            (data / 'memory').mkdir(parents=True)
            (data / 'memory' / 'agent.md').write_text('Test identity', encoding='utf-8')
            with patch.dict('os.environ', {'KHAI_RUNTIME': 'local', 'TELEGRAM_BOT_TOKEN': 'token',
                                           'TELEGRAM_USER_ID': 'owner'}):
                manager = ModuleManager(data_dir=str(data))
                manager.telegram = Mock()
                manager.hand.describe_service = Mock(side_effect=lambda t: (descriptor, manager.hand.propose_service(descriptor)))
                manager.hand.generate = Mock(return_value=('# MODE: SAFE\nprint("ok")', 'SAFE', []))
                coordinator = Coordinator(manager, Mock())
                manager.coordinator = coordinator
                try:
                    task = coordinator.store.create('read-task', 'consulta tasa',
                        state='GENERATING', manifest={'purpose': 'consulta tasa',
                                                      'capabilities': ['Network']})
                    coordinator.advance(task)
                    res = coordinator.store.get(task['id'])
                    self.assertEqual(res['state'], 'REVIEWING')
                    self.assertEqual(res['services'], [{
                        'service_id': 'fx', 'operation': 'rate', 'version': 'v1'}])
                    self.assertTrue(coordinator._services_available(res['services']))
                finally:
                    coordinator._llm_executor.shutdown(wait=True)
                    coordinator._mailbox_executor.shutdown(wait=True)

    def test_runtime_capabilities_contract_and_hand_validation(self):
        from app.voice.runtime.contract import RuntimeCapabilities, default_runtime_capabilities
        caps_docker = default_runtime_capabilities('docker')
        self.assertEqual(caps_docker.runtime, 'docker')
        self.assertTrue(caps_docker.allowed_dependencies)
        caps_docker.validate_code('SAFE', ['requests==2.31.0'])

        caps_local = default_runtime_capabilities('local')
        self.assertEqual(caps_local.runtime, 'local')
        self.assertFalse(caps_local.allowed_dependencies)
        with self.assertRaises(ValueError):
            caps_local.validate_code('SAFE', ['requests==2.31.0'])
        with self.assertRaises(ValueError):
            caps_local.validate_code('UNSUPPORTED_MODE', [])

        llm = Mock()
        llm.complete.return_value = {
            'content': '',
            'tool_calls': [{'name': 'finish_code', 'arguments': {'code': '# MODE: SAFE\nprint("ok")'}}]
        }
        with tempfile.TemporaryDirectory() as folder:
            hand = Hand(Path(folder) / 'hand', Mock(), llm=llm, capabilities=caps_local)
            code, mode, deps = hand.generate({'request': 'test'}, [])
            self.assertEqual(mode, 'SAFE')
            prompt = llm.complete.call_args.args[0][0]['content']
            self.assertIn('Facultades del runtime:', prompt)
            self.assertIn('"runtime": "local"', prompt)
            self.assertIn('No declares dependencias externas # PIP', prompt)


    def test_missing_service_credential_waits_for_private_receipt(self):
        from app.bootstrap import ModuleManager
        from app.mind.coordinator import Coordinator
        from unittest.mock import patch
        from app.voice.runtime.contract import digest
        descriptor = {'service_id': 'fx', 'operation': 'rate', 'version': 'v1',
            'url': 'https://example.org/rate', 'method': 'GET',
            'parameters': [], 'content_types': ['application/json'],
            'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
            'effect': 'read', 'credential_ref': 'FX_API_KEY',
            'credential_header': 'Authorization', 'credential_prefix': '',
            'effect_contract': None}
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data'
            (data / 'memory').mkdir(parents=True)
            (data / 'memory' / 'agent.md').write_text('Test identity', encoding='utf-8')
            with patch.dict('os.environ', {'KHAI_RUNTIME': 'local', 'TELEGRAM_BOT_TOKEN': 'token',
                                           'TELEGRAM_USER_ID': 'owner'}):
                manager = ModuleManager(data_dir=str(data))
                manager.telegram = Mock(user_id='owner')
                self.assertFalse(hasattr(manager.voice_credentials, 'get'))
                coordinator = Coordinator(manager, Mock())
                manager.coordinator = coordinator
                try:
                    service_hash = manager.hand_services.propose(descriptor)
                    manager.pixy_services.decide(service_hash, True, 'telegram:owner')
                    artifact = data / 'artifact.py'
                    artifact.write_text('print(1)', encoding='utf-8')
                    artifact_hash = digest(artifact.read_bytes())
                    task = coordinator.store.create('secret-task', 'consulta tasa', state='READY',
                        artifact=str(artifact), artifact_hash=artifact_hash,
                        authorized_hash=artifact_hash, services=[{
                            'service_id': 'fx', 'operation': 'rate', 'version': 'v1'}])
                    coordinator.advance(task)
                    waiting = coordinator.store.get(task['id'])
                    self.assertEqual(waiting['state'], 'WAITING_SECRET')
                    self.assertNotIn('synthetic-key', json.dumps(waiting))
                    receipt_id = manager.secret_capture.capture('/api', 'synthetic-key',
                                                                'owner', 'owner')
                    coordinator.advance(waiting)
                    resumed = coordinator.store.get(task['id'])
                    self.assertEqual(resumed['state'], 'READY')
                    self.assertEqual(resumed['secret_receipt_id'], receipt_id)
                    self.assertNotIn('synthetic-key', json.dumps(resumed))
                finally:
                    coordinator._llm_executor.shutdown(wait=True)
                    coordinator._mailbox_executor.shutdown(wait=True)

    def test_service_proposal_requires_exact_owner_decision_and_gateway_scope(self):
        descriptor = {'service_id': 'fx', 'operation': 'rate', 'version': 'v1',
            'url': 'https://example.org/rate', 'method': 'GET',
            'parameters': ['base', 'quote'], 'content_types': ['application/json'],
            'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
            'effect': 'read', 'credential_ref': 'FX_API_KEY',
            'credential_header': 'Authorization', 'credential_prefix': '',
            'effect_contract': None}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            registry = ServiceRegistry(root / 'control.sqlite3')
            hand, pixy, voice = HandServicePort(registry), PixyServicePort(registry), VoiceServicePort(registry)
            digest = hand.propose(descriptor)
            self.assertEqual(voice.active_services(), {})
            self.assertFalse(hasattr(hand, 'decide'))
            self.assertFalse(hasattr(voice, 'propose'))
            malicious = copy.deepcopy(descriptor)
            malicious['credential_value'] = 'synthetic-secret'
            with self.assertRaises(ValueError):
                hand.propose(malicious)
            pixy.decide(digest, True, 'telegram:owner')
            self.assertEqual(voice.active_services()['fx']['operations']['rate']['version'], 'v1')
            gateway = ExternalGateway(root / 'missing.yaml', Mock(), db=root / 'voice.sqlite3', registry=voice)
            gateway._mailbox_request = Mock(return_value={'result': {'rate': 1.0}})
            workspace = root / 'workspace'
            workspace.mkdir()
            task = {'id': 'task_1', 'execution_id': 'run_1', 'deadline': 9999999999,
                    'manifest': {'capabilities': ['Network']}, 'artifact_hash': 'hash',
                    'authorized_hash': 'hash', 'services': []}
            request = workspace / '.voice-a.request.json'
            request.write_text(json.dumps({'service': 'fx', 'operation': 'rate', 'params': {},
                                           'execution_id': 'run_1', 'artifact_hash': 'hash',
                                           'goal_revision': 0}), encoding='utf-8')
            gateway.process_mailbox(task, workspace)
            self.assertEqual(json.loads((workspace / '.voice-a.response.json').read_text())['error'],
                             'PermissionError')
            gateway._mailbox_request.assert_not_called()

    def test_equivalent_request_reuses_verified_version_without_generation(self):
        from app.bootstrap import ModuleManager
        from app.mind.coordinator import Coordinator
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'data'
            (root / 'memory').mkdir(parents=True)
            (root / 'memory' / 'agent.md').write_text('Test identity', encoding='utf-8')
            with patch.dict('os.environ', {'KHAI_RUNTIME': 'local', 'TELEGRAM_BOT_TOKEN': 'token',
                                           'TELEGRAM_USER_ID': 'owner'}):
                manager = ModuleManager(data_dir=str(root))
                manager.telegram = Mock()
                code = '# MODE: SAFE\nfrom khai_input import params\nprint(params["city"])'
                version = manager.hand.create('weather', code, {'purpose': 'clima', 'capabilities': []})
                weather = skill('weather', {'city': 'string'}, {'temperature': 'number'})
                weather['extraction_templates'] = [
                    'qué tiempo hace en {city:city}', 'dime el clima actual de {city:city}']
                spec_digest = manager.hand.catalog.stage(version['version_id'], weather)
                manager.pixy.audit_artifact = Mock(return_value={
                    'safe': True, 'requires_approval': False, 'audit_id': 'test-audit'})
                manager.hand.approve(version['version_id'])
                run_id = 'run_' + 'a' * 32
                seed = manager.coordinator_control.create('seed:weather', 'clima', state='VERIFYING',
                    version_id=version['version_id'], execution_id=run_id,
                    skill_spec_hash=spec_digest, verification={'success': True})
                manager.hand.catalog.promote(version['version_id'], seed['id'], run_id,
                                             version['artifact_hash'])
                manager.coordinator_control.update(seed['id'], expected={'VERIFYING'}, state='COMPLETED')
                encoder = Mock()
                encoder.encode.return_value = [[1.0, 0.0], [0.99, 0.01]]
                manager.skill_selector = SkillSelector(manager.hand.catalog, encoder,
                    manager.llm, threshold=0.8, min_margin=0.1)
                manager.llm.generate_content = Mock(side_effect=AssertionError('LLM called'))
                manager.pixy.preflight = Mock(return_value={'safe': True})
                manager.pixy.review = Mock(return_value={'safe': True, 'requires_approval': False})
                coordinator = Coordinator(manager, Mock())
                manager.coordinator = coordinator
                try:
                    for index, request in enumerate(['/make qué tiempo hace en Santiago',
                                                     '/make dime el clima actual de Valparaíso']):
                        task = coordinator.intake(f'tg:{index}', request)
                        coordinator.tick()
                        coordinator.tick()
                        current = coordinator.store.get(task['id'])
                        self.assertEqual(current['state'], 'REVIEWING')
                        self.assertEqual(current['version_id'], version['version_id'])
                        self.assertEqual(current['generations'], 0)
                        self.assertEqual(current['params']['city'],
                                         'Santiago' if index == 0 else 'Valparaíso')
                    manager.llm.generate_content.assert_not_called()
                finally:
                    coordinator._llm_executor.shutdown(wait=True, cancel_futures=True)
                    coordinator._mailbox_executor.shutdown(wait=True, cancel_futures=True)

    def test_semantic_selector_skips_llm_only_for_confident_compatible_match(self):
        conversion = skill('convertir', {'monto': 'number', 'base': 'string', 'destino': 'string'},
                           {'resultado': 'number'})
        conversion['extraction_templates'] = ['convierte {monto:number} {base:currency} a {destino:currency}']
        catalog = Mock()
        catalog.active.return_value = [{'version_id': 'v1', 'skill_id': 'convertir', 'spec': conversion}]
        encoder = Mock()
        encoder.encode.return_value = [[1.0, 0.0], [0.95, 0.05]]
        llm = Mock()
        selector = SkillSelector(catalog, encoder, llm, threshold=0.8, min_margin=0.1)
        chosen = selector.select('convierte 20 USD a CLP', lambda _: True)
        self.assertEqual(chosen['params'], {'monto': 20.0, 'base': 'USD', 'destino': 'CLP'})
        self.assertEqual(chosen['selection'], 'local')
        llm.generate_content.assert_not_called()
        self.assertIsNone(selector.select('convierte 20 USD a CLP', lambda _: False))

    def test_semantic_selector_rejects_model_invented_fields(self):
        conversion = skill('convertir', {'monto': 'number'}, {'resultado': 'number'})
        catalog = Mock()
        catalog.active.return_value = [{'version_id': 'v1', 'skill_id': 'convertir', 'spec': conversion}]
        llm = Mock()
        llm.generate_content.return_value = '{"skill_id":"convertir","params":{"monto":20,"transferir":true}}'
        selector = SkillSelector(catalog, llm=llm)
        self.assertIsNone(selector.select('convierte 20', lambda _: True))

    def test_selector_keeps_compatible_skill_when_required_value_is_missing(self):
        weather = skill('weather', {'city': 'string', 'country': 'string'},
                        {'temperature': 'number'})
        catalog = Mock()
        catalog.active.return_value = [{'version_id': 'v1', 'skill_id': 'weather', 'spec': weather}]
        llm = Mock()
        llm.generate_content.return_value = '{"skill_id":"weather","params":{"city":"Santiago"}}'
        selected = SkillSelector(catalog, llm=llm).select('clima en Santiago', lambda _: True)
        self.assertEqual(selected['version_id'], 'v1')
        self.assertEqual(selected['missing_params'], ['country'])

    def test_local_parameter_extraction_and_ambiguous_fallback(self):
        conversion = skill('convertir', {'monto': 'number', 'base': 'string', 'destino': 'string'},
                           {'resultado': 'number'})
        conversion['extraction_templates'] = [
            'convierte {monto:number} {base:currency} a {destino:currency}',
            'cambia {monto:number} {base:currency} por {destino:currency}',
        ]
        self.assertEqual(extract_params(conversion, 'Convierte 20 USD a CLP'),
                         {'monto': 20.0, 'base': 'USD', 'destino': 'CLP'})
        self.assertIsNone(extract_params(conversion, 'Convierte unos dólares a pesos'))
        invalid = copy.deepcopy(conversion)
        invalid['extraction_templates'] = ['convierte {monto:number} {base:currency} a {otro:currency}']
        with self.assertRaises(ValueError):
            validate_skill_spec(invalid)

    def test_candidate_execution_keeps_previous_version_active(self):
        pixy = Mock()
        pixy.audit_artifact.return_value = {'safe': True, 'requires_approval': False, 'audit_id': 'test-audit'}
        with tempfile.TemporaryDirectory() as folder:
            hand = Hand(Path(folder) / 'hand', pixy)
            stable = hand.create('example', '# MODE: SAFE\nprint("stable")', {'purpose': 'example'})
            hand.approve(stable['version_id'])
            candidate = hand.create('example', '# MODE: SAFE\nprint("candidate")', {'purpose': 'example'})
            import hashlib
            code_hash = hashlib.sha256(b'# MODE: SAFE\nprint("candidate")').hexdigest()
            hand.prepare_execution(candidate['version_id'], 'run_' + 'a' * 32, code_hash)
            self.assertEqual(hand.active('example')['version_id'], stable['version_id'])
            hand.finish_one_off(candidate['version_id'])
            self.assertEqual(hand.active('example')['version_id'], stable['version_id'])
            self.assertEqual(hand.version(candidate['version_id'])['status'], 'one_off')

    def test_catalog_requires_explicit_verified_promotion(self):
        pixy = Mock()
        pixy.audit_artifact.return_value = {'safe': True, 'requires_approval': False, 'audit_id': 'test-audit'}
        with tempfile.TemporaryDirectory() as folder:
            hand = Hand(Path(folder) / 'hand', pixy)
            candidate = hand.create('example', '# MODE: SAFE\nprint("ok")', {'purpose': 'example'})
            spec = skill('example', {'value': 'string'}, {'result': 'string'})
            staged_hash = hand.catalog.stage(candidate['version_id'], spec)
            run_id = 'run_' + 'a' * 32
            task = ControlStore(hand.db).create('source:catalog-test', 'example', state='VERIFYING',
                version_id=candidate['version_id'], execution_id=run_id,
                skill_spec_hash=staged_hash, verification={'success': True, 'evidence': 'fixture'})
            self.assertEqual(hand.catalog.active(), [])
            with self.assertRaises(ValueError):
                hand.catalog.promote(candidate['version_id'], task['id'], run_id, candidate['artifact_hash'])
            hand.approve(candidate['version_id'])
            with self.assertRaises(ValueError):
                hand.catalog.promote(candidate['version_id'], task['id'], 'run_' + 'b' * 32,
                                     candidate['artifact_hash'])
            hand.catalog.promote(candidate['version_id'], task['id'], run_id, candidate['artifact_hash'])
            self.assertEqual(hand.catalog.active()[0]['version_id'], candidate['version_id'])
            self.assertEqual(hand.catalog.promote(candidate['version_id'], task['id'], run_id,
                                                  candidate['artifact_hash']), spec_hash(spec))

    def test_recipe_action_promotion_requires_its_own_receipt_and_owner_result(self):
        pixy = Mock()
        pixy.audit_artifact.return_value = {'safe': True, 'requires_approval': False, 'audit_id': 'audit'}
        with tempfile.TemporaryDirectory() as folder:
            hand = Hand(Path(folder) / 'hand', pixy)
            candidate = hand.create('small-action', '# MODE: SAFE\nprint("ok")',
                                    {'purpose': 'Perform one reusable action'})
            spec = skill('small-action', {'value': 'string'}, {'result': 'string'})
            spec_digest = hand.catalog.stage(candidate['version_id'], spec)
            run_id = 'run_' + 'c' * 32
            receipt = {'step_id': 'first', 'version_id': candidate['version_id'],
                       'execution_id': run_id, 'skill_spec_hash': spec_digest,
                       'skill_artifact_hash': candidate['artifact_hash'], 'status': 'completed'}
            store = ControlStore(hand.db)
            task = store.create('source:recipe-promotion', 'two actions', state='VERIFYING',
                                recipe_receipts=[receipt], verification={'success': False})
            hand.approve(candidate['version_id'])
            with self.assertRaises(ValueError):
                hand.catalog.promote(candidate['version_id'], task['id'], run_id,
                                     candidate['artifact_hash'])
            store.update(task['id'], verification={'success': True})
            self.assertEqual(hand.catalog.promote(candidate['version_id'], task['id'], run_id,
                                                  candidate['artifact_hash']), spec_digest)

    def test_hand_describes_the_real_network_boundary(self):
        llm = Mock()
        llm.complete.return_value = {
            'content': '',
            'tool_calls': [{'name': 'finish_code', 'arguments': {'code': '# MODE: SAFE\nprint("ok")'}}]
        }
        with tempfile.TemporaryDirectory() as folder:
            hand = Hand(Path(folder) / 'hand', Mock(), llm=llm)
            hand.generate({'request': 'consultar tasa', 'manifest': {'capabilities': ['Network']},
                           'services': [{'service_id': 'fx', 'operation': 'get-rate', 'version': 'v1'}]}, [])
        prompt = llm.complete.call_args.args[0][0]['content']
        self.assertIn('El worker no tiene red directa', prompt)
        self.assertIn('from khai_gateway import request', prompt)
        self.assertIn('get-rate', prompt)

    def test_spec_separates_service_reference_from_secret_value(self):
        rate = skill('consultar-tasa', {'base': 'string', 'destino': 'string'},
                     {'tasa': 'number'}, [{'service_id': 'fx', 'operation': 'get-rate', 'version': 'v1'}])
        self.assertIs(validate_skill_spec(rate), rate)
        first_hash = spec_hash(rate)
        altered = copy.deepcopy(rate)
        altered['services'][0]['version'] = 'v2'
        self.assertNotEqual(first_hash, spec_hash(altered))
        altered['services'][0]['credential'] = 'synthetic-secret'
        with self.assertRaises(ValueError):
            validate_skill_spec(altered)

    def test_recipe_binds_pinned_versions_and_rejects_cycles_or_type_changes(self):
        rate = skill('consultar-tasa', {'base': 'string', 'destino': 'string'}, {'tasa': 'number'})
        apply = skill('aplicar-tasa', {'monto': 'number', 'tasa': 'number'}, {'resultado': 'number'})
        specs = {'rate-v1': rate, 'apply-v1': apply}
        recipe = {
            'recipe_id': 'convertir-monto',
            'input_schema': schema(base='string', destino='string', monto='number'),
            'steps': [
                {'id': 'rate', 'version_id': 'rate-v1',
                 'bindings': {'base': 'input.base', 'destino': 'input.destino'}},
                {'id': 'apply', 'version_id': 'apply-v1',
                 'bindings': {'monto': 'input.monto', 'tasa': 'rate.tasa'}},
            ],
            'output': 'apply.resultado',
        }
        self.assertIs(validate_recipe(recipe, specs), recipe)
        wrong_type = copy.deepcopy(recipe)
        wrong_type['steps'][1]['bindings']['tasa'] = 'input.base'
        with self.assertRaises(ValueError):
            validate_recipe(wrong_type, specs)
        forward_reference = copy.deepcopy(recipe)
        forward_reference['steps'][0]['bindings']['base'] = 'apply.resultado'
        with self.assertRaises(ValueError):
            validate_recipe(forward_reference, specs)
        stale_version = copy.deepcopy(recipe)
        stale_version['steps'][0]['version_id'] = 'rate-v2'
        with self.assertRaises(ValueError):
            validate_recipe(stale_version, specs)


class RecipeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        hand = Mock()
        hand.last_agent_run_id = None
        hand.root = root / 'hand'
        self.specs = {
            'rate-v1': {'skill_id': 'rate', 'description': 'Read a current conversion rate.',
                        'use_cases': ['Convert a currency'], 'input_schema': schema(base='string'),
                        'output_schema': schema(rate='number'), 'runtime': 'SAFE',
                        'dependencies': [], 'services': [], 'verifier': {'kind': 'owner_criterion'}},
            'apply-v1': {'skill_id': 'apply', 'description': 'Apply a conversion rate.',
                         'use_cases': ['Convert a currency'],
                         'input_schema': schema(amount='number', rate='number'),
                         'output_schema': schema(total='number'), 'runtime': 'SAFE',
                         'dependencies': [], 'services': [], 'verifier': {'kind': 'owner_criterion'}},
        }
        versions = {}
        for version_id, spec in self.specs.items():
            code = '# MODE: SAFE\nprint("{}")\n'
            path = hand.root / spec['skill_id'] / 'app' / 'main.py'
            path.parent.mkdir(parents=True)
            path.write_text(code, encoding='utf-8')
            versions[version_id] = {'version_id': version_id, 'skill_name': spec['skill_id'],
                                    'status': 'active', 'artifact_hash': 'a' * 64,
                                    'manifest': json.dumps({'purpose': spec['description'], 'capabilities': []})}
        hand.version.side_effect = versions.get
        hand.catalog.get.side_effect = lambda vid: {'status': 'active', 'spec': self.specs[vid],
                                                     'spec_hash': 'b' * 64}
        def snapshot(code):
            path = root / 'artifact.py'
            path.write_text(code, encoding='utf-8')
            return str(path), hashlib.sha256(path.read_bytes()).hexdigest()
        hand.snapshot.side_effect = snapshot
        self.manager = SimpleNamespace(
            state=SimpleNamespace(db_path=root / 'control.sqlite3'), data_dir=root,
            runtime=Mock(), llm=Mock(), mind=Mock(), hand=hand, pixy=Mock(), rules=Mock(),
            memory=Mock(), telegram=Mock(), dreams=Mock())
        self.manager.pixy.preflight.return_value = {'safe': True}
        self.manager.pixy.review.return_value = {'safe': True, 'requires_approval': False}
        self.manager.rules.active.return_value = []
        self.proxy = Mock()
        self.proxy.submit.return_value = {'status': 'queued'}
        self.coordinator = Coordinator(self.manager, self.proxy)
        self.addCleanup(self.coordinator._llm_executor.shutdown, wait=True)
        self.addCleanup(self.coordinator._mailbox_executor.shutdown, wait=True)
        self.recipe = {'recipe_id': 'convert', 'input_schema': schema(base='string', amount='number'),
                       'steps': [{'id': 'rate', 'version_id': 'rate-v1',
                                  'bindings': {'base': 'input.base'}},
                                 {'id': 'apply', 'version_id': 'apply-v1',
                                  'bindings': {'amount': 'input.amount', 'rate': 'rate.rate'}}],
                       'output': 'apply.total'}

    def advance_to_running(self, task_id):
        for _ in range(9):
            task = self.coordinator.store.get(task_id)
            if task['state'] == 'RUNNING':
                return task
            if task['state'] == 'WAITING_INPUT_CONFIRMATION':
                self.coordinator.accept_input_confirmation(
                    'owner:confirm:' + str(task['revision']), 'sí')
                continue
            self.coordinator.advance(task)
        self.fail('Recipe step did not reach RUNNING')

    def test_two_steps_share_task_and_wait_for_owner(self):
        self.coordinator.notify = Mock()
        self.coordinator.revision_planner.target = Mock()
        task = self.coordinator.intake_recipe('owner:1', self.recipe,
                                               {'base': 'CLP', 'amount': 100})
        first = self.advance_to_running(task['id'])
        self.assertEqual(first['payload']['params'], {'base': 'CLP'})
        self.proxy.get.return_value = {'status': 'finished', 'returncode': 0,
                                       'stdout': '{"rate": 2}'}
        self.coordinator.advance(first)
        mid = self.coordinator.store.get(task['id'])
        self.assertEqual(mid['state'], 'RECIPE_STEP')
        self.assertEqual(len(mid['recipe_receipts']), 1)
        second = self.advance_to_running(task['id'])
        self.assertEqual(second['payload']['params'], {'amount': 100, 'rate': 2})
        self.assertNotEqual(first['execution_id'], second['execution_id'])
        self.proxy.get.return_value = {'status': 'finished', 'returncode': 0,
                                       'stdout': '{"total": 200}'}
        self.coordinator.advance(second)
        final = self.coordinator.store.get(task['id'])
        self.assertEqual(final['state'], 'UNVERIFIED')
        self.assertEqual(final['recipe_result'], 200)
        self.assertIsNone(final['machine_verification']['success'])
        self.assertEqual(len(final['recipe_receipts']), 2)
        self.coordinator.revision_planner.target.assert_not_called()
        self.assertEqual({r['execution_id'] for r in final['recipe_receipts']},
                         {first['execution_id'], second['execution_id']})
        delivered = self.coordinator.notify.call_args.args[0]
        self.assertIn('Rate:', delivered)
        self.assertIn('Rate: 2', delivered)
        self.assertIn('Apply:', delivered)
        self.assertIn('Total: 200', delivered)
        self.assertIn('¿Es correcto el resultado completo?', delivered)
        self.coordinator.verify(task['id'], second['execution_id'], True, 'owner:2')
        self.coordinator.advance(self.coordinator.store.get(task['id']))
        self.assertEqual(self.coordinator.store.get(task['id'])['state'], 'COMPLETED')

    def test_uncertain_step_does_not_start_next(self):
        task = self.coordinator.intake_recipe('owner:3', self.recipe,
                                               {'base': 'CLP', 'amount': 100})
        first = self.advance_to_running(task['id'])
        self.proxy.get.return_value = {'status': 'unknown'}
        self.coordinator.advance(first)
        stopped = self.coordinator.store.get(task['id'])
        self.assertEqual(stopped['state'], 'UNKNOWN')
        self.assertEqual(stopped['recipe_receipts'], [])
        self.assertEqual(stopped['recipe_step_index'], 0)
        self.proxy.submit.assert_called_once()

    def test_partial_batch_step_waits_for_correction_and_rejects_success(self):
        self.manager.gateway = Mock()
        self.manager.gateway.technical_receipts.return_value = [{
            'request_id': 'batch:item1', 'parent_request_id': 'batch',
            'item_index': 0, 'service_id': 'notes', 'operation': 'create_batch',
            'version': 'v1', 'effect': 'write', 'status': 'unknown',
            'resource_id': None, 'effect_contract': {'kind': 'create',
                'batch': {'request_field': 'items'}}}]
        task = self.coordinator.intake_recipe('owner:partial-batch', self.recipe,
                                             {'base': 'CLP', 'amount': 100})
        first = self.advance_to_running(task['id'])
        self.proxy.get.return_value = {'status': 'finished', 'returncode': 0,
                                       'stdout': '{"rate": 2}'}
        self.coordinator.advance(first)
        waiting = self.coordinator.store.get(task['id'])
        self.assertEqual(waiting['state'], 'UNVERIFIED')
        self.assertTrue(waiting['batch_incomplete'])
        self.assertEqual(waiting['recipe_step_index'], 0)
        self.assertEqual(self.coordinator.accept_review_reply('owner:premature-yes', 'y'),
                         'hay efectos externos sin confirmar; explica qué ocurrió o cancela.')
        with self.assertRaises(PermissionError):
            self.coordinator.verify(task['id'], first['execution_id'], True,
                                    'owner:premature-verify')

    def test_wrong_step_output_cannot_feed_next_skill(self):
        task = self.coordinator.intake_recipe('owner:4', self.recipe,
                                               {'base': 'CLP', 'amount': 100})
        first = self.advance_to_running(task['id'])
        self.proxy.get.return_value = {'status': 'finished', 'returncode': 0,
                                       'stdout': '{"rate": "unknown"}'}
        self.coordinator.advance(first)
        stopped = self.coordinator.store.get(task['id'])
        self.assertEqual(stopped['state'], 'DIAGNOSING')
        self.assertEqual(stopped['recipe_receipts'], [])
        self.proxy.submit.assert_called_once()

    def test_plain_text_feedback_revises_same_goal_without_promotion(self):
        task = self.coordinator.intake_recipe('owner:feedback', self.recipe,
                                               {'base': 'CLP', 'amount': 100})
        first = self.advance_to_running(task['id'])
        self.proxy.get.return_value = {'status': 'finished', 'returncode': 0,
                                       'stdout': '{"rate": 2}'}
        self.coordinator.advance(first)
        second = self.advance_to_running(task['id'])
        self.proxy.get.return_value = {'status': 'finished', 'returncode': 0,
                                       'stdout': '{"total": 200}'}
        self.coordinator.advance(second)
        before_feedback = self.coordinator.store.get(task['id'])
        reply = self.coordinator.accept_review_reply(
            'owner:correction', 'El resumen está incorrecto por tal razón, corrige')
        revised = self.coordinator.store.get(task['id'])
        self.assertIn('corrección registrada', reply)
        self.assertEqual(revised['state'], 'RESOLVING_SCOPE')
        self.assertEqual(revised['id'], task['id'])
        self.assertEqual(len(revised['recipe_attempts'][0]['receipts']), 2)
        self.assertEqual((revised['max_generations'], revised['max_executions'],
                          revised['max_llm_calls']), (8, 8, 32))
        self.assertEqual((revised['generations'], revised['executions'],
                          revised['llm_calls']),
                         (before_feedback['generations'], before_feedback['executions'],
                          before_feedback['llm_calls']))
        self.assertEqual(revised['budget_extensions'][0]['kind'], 'owner_revision')
        self.manager.hand.catalog.promote.assert_not_called()
        self.coordinator.store.update(task['id'], state='UNVERIFIED', execution_id='second-review')
        self.coordinator.accept_review_reply('owner:second-correction', 'Todavía falta un dato')
        again = self.coordinator.store.get(task['id'])
        self.assertEqual((again['max_generations'], again['max_executions'],
                          again['max_llm_calls']), (12, 12, 48))
        self.assertEqual(len(again['budget_extensions']), 2)

    def test_reviewed_write_revision_targets_original_resource(self):
        task = self.coordinator.intake('owner:write', 'Create a remote item')
        correction = {'service_id': 'records', 'operation': 'update',
                      'version': 'v1', 'identity_parameter': 'id'}
        effect = {'effect': 'write', 'status': 'completed', 'resource_id': 'item-1',
                  'effect_contract': {'kind': 'create',
                                      'identity': {'source': 'response', 'field': 'id'},
                                      'correction': correction}}
        self.coordinator.store.update(task['id'], state='RESOLVING_SCOPE',
            original_request='Create a remote item',
            scope_answers=[{'questions': [], 'answer': 'Fix its summary',
                            'source': 'owner:feedback'}],
            single_attempts=[{'external_effects': [effect]}],
            scope_revisions=[{'number': 1, 'purpose': 'Create a remote item'}],
            manifest={'purpose': 'Create a remote item', 'capabilities': ['Network']})
        self.coordinator.requirements.resolve = Mock(return_value={
            'purpose': 'Fix the summary of the same remote item',
            'required_output': ['Correct summary'], 'params': {},
            'missing_questions': []})
        self.manager.voice_services = Mock()
        self.manager.voice_services.active_services.return_value = {
            'records': {'operations': {'update': {'version': 'v1', 'effect': 'write',
                                                   'parameters': ['id', 'body']}}}}
        self.coordinator.advance(self.coordinator.store.get(task['id']))
        revised = self.coordinator.store.get(task['id'])
        self.assertEqual(revised['state'], 'ROUTING')
        self.assertEqual(revised['effect_constraints'], [{**correction,
                                                          'resource_id': 'item-1'}])
        self.assertEqual(revised['id'], task['id'])

    def test_write_without_correction_pauses_for_owner(self):
        task = self.coordinator.intake('owner:unsafe-write', 'Create a remote item')
        self.coordinator.store.update(task['id'], state='RESOLVING_SCOPE',
            original_request='Create a remote item',
            scope_answers=[{'questions': [], 'answer': 'Fix its summary',
                            'source': 'owner:feedback'}],
            single_attempts=[{'external_effects': [{
                'effect': 'write', 'status': 'completed', 'resource_id': 'item-1',
                'effect_contract': {'kind': 'create', 'identity': None,
                                    'correction': None}}]}],
            scope_revisions=[{'number': 1, 'purpose': 'Create a remote item'}],
            manifest={'purpose': 'Create a remote item', 'capabilities': ['Network']})
        self.coordinator.requirements.resolve = Mock(return_value={
            'purpose': 'Fix the summary of the same remote item',
            'required_output': ['Correct summary'], 'params': {},
            'missing_questions': []})
        self.coordinator.advance(self.coordinator.store.get(task['id']))
        self.assertEqual(self.coordinator.store.get(task['id'])['state'],
                         'WAITING_EFFECT_DECISION')
        self.coordinator.accept_effect_decision('owner:cancel-effect', '1')
        self.assertEqual(self.coordinator.store.get(task['id'])['state'], 'CANCELLED')

    def test_duplicate_risk_choice_is_durable_and_scoped_to_one_service(self):
        task = self.coordinator.intake('owner:retry-write', 'Create a remote item')
        self.coordinator.store.update(task['id'], state='WAITING_EFFECT_DECISION',
            effect_options={'1': 'create', '2': 'cancel'},
            single_attempts=[{'external_effects': [{
                'effect': 'write', 'status': 'completed', 'request_id': 'run-old:create',
                'service_id': 'notes', 'operation': 'create', 'version': 'v1',
                'resource_id': None, 'effect_contract': {'kind': 'create',
                    'identity': None, 'correction': None,
                    'summary': {'name': 'Nota', 'context': 'Creación en Notes'}}}]}])
        reply = self.coordinator.accept_effect_decision('owner:explicit-choice', '1')
        decided = self.coordinator.store.get(task['id'])
        grant = decided['new_effect_authorization']
        self.assertEqual(decided['state'], 'RESOLVING_SCOPE')
        self.assertEqual(grant['task_id'], task['id'])
        self.assertEqual(grant['challenged_request_id'], 'run-old:create')
        self.assertEqual((grant['service_id'], grant['operation'], grant['version']),
                         ('notes', 'create', 'v1'))
        self.assertIn('puede duplicarlo', reply)
        self.assertIsNone(decided.get('allow_new_effects'))

    def test_missing_last_action_reuses_earlier_receipt(self):
        self.manager.voice_services = Mock()
        self.manager.voice_services.active_services.return_value = {
            'records': {'operations': {'create': {'effect': 'write', 'version': 'v1'}}}}
        self.manager.gateway = Mock()
        service = {'service_id': 'records', 'operation': 'create', 'version': 'v1'}
        self.specs['apply-v1']['services'] = [service]
        code = (self.manager.hand.root / 'apply' / 'app' / 'main.py').read_bytes()
        approved_hash = hashlib.sha256(code).hexdigest()
        task = self.coordinator.intake_recipe('owner:partial', self.recipe,
                                             {'base': 'CLP', 'amount': 100})
        receipts = [
            {'step_id': 'rate', 'version_id': 'rate-v1', 'output': {'rate': 2},
             'external_effects': [], 'services': [], 'params': {'base': 'CLP'}},
            {'step_id': 'apply', 'version_id': 'apply-v1', 'output': {'total': 200},
             'external_effects': [], 'services': [service],
             'params': {'amount': 100, 'rate': 2},
             'approved_hash': approved_hash},
        ]
        self.coordinator.store.update(task['id'], state='RESOLVING_SCOPE',
            original_request='Convert and save', recipe_receipts=receipts,
            recipe_attempts=[{'recipe': self.recipe, 'receipts': receipts}],
            scope_answers=[{'questions': [], 'answer': 'The save did not happen',
                            'source': 'owner:correction'}])
        self.coordinator.requirements.resolve = Mock(return_value={
            'purpose': 'Convert and save correctly',
            'required_output': ['Saved result'],
            'params': {'base': 'CLP', 'amount': 100}, 'missing_questions': []})
        self.coordinator.revision_planner.target = Mock(return_value={
            'step_id': 'apply', 'problem': 'missing'})
        self.coordinator.advance(self.coordinator.store.get(task['id']))
        revised = self.coordinator.store.get(task['id'])
        self.assertEqual(revised['state'], 'RECIPE_STEP')
        self.assertEqual(revised['recipe_step_index'], 1)
        self.assertEqual([r['step_id'] for r in revised['recipe_receipts']], ['rate'])
        self.assertEqual(revised['recipe_outputs']['rate'], {'rate': 2})
        self.assertEqual(revised['id'], task['id'])
        self.coordinator.advance(revised)
        selected = self.coordinator.store.get(task['id'])
        self.assertEqual(selected['state'], 'GENERATING')
        self.coordinator.advance(selected)
        self.assertEqual(self.coordinator.store.get(task['id'])['state'], 'READY')
        self.manager.pixy.review.assert_not_called()

    def test_recorded_write_is_not_retried_on_missing_feedback(self):
        self.manager.voice_services = Mock()
        self.manager.voice_services.active_services.return_value = {
            'records': {'operations': {'update': {'version': 'v1', 'effect': 'write',
                                                   'parameters': ['id', 'body']}}}}
        task = self.coordinator.intake_recipe('owner:uncertain', self.recipe,
                                             {'base': 'CLP', 'amount': 100})
        receipts = [
            {'step_id': 'rate', 'version_id': 'rate-v1', 'output': {'rate': 2},
             'external_effects': [], 'services': []},
            {'step_id': 'apply', 'version_id': 'apply-v1', 'output': {'total': 200},
             'external_effects': [{'effect': 'write', 'status': 'completed',
                                   'resource_id': 'record-1',
                                   'effect_contract': {'kind': 'create',
                                       'correction': {'service_id': 'records',
                                                      'operation': 'update', 'version': 'v1',
                                                      'identity_parameter': 'id'}}}], 'services': []},
        ]
        self.coordinator.store.update(task['id'], state='RESOLVING_SCOPE',
            original_request='Convert and save', recipe_receipts=receipts,
            recipe_attempts=[{'recipe': self.recipe, 'receipts': receipts}],
            scope_answers=[{'questions': [], 'answer': 'The save did not happen',
                            'source': 'owner:correction'}])
        self.coordinator.requirements.resolve = Mock(return_value={
            'purpose': 'Convert and save correctly',
            'required_output': ['Saved result'],
            'params': {'base': 'CLP', 'amount': 100}, 'missing_questions': []})
        self.coordinator.revision_planner.target = Mock(return_value={
            'step_id': 'apply', 'problem': 'missing'})
        self.coordinator.advance(self.coordinator.store.get(task['id']))
        self.assertEqual(self.coordinator.store.get(task['id'])['state'],
                         'WAITING_EFFECT_DETAIL')
        self.proxy.submit.assert_not_called()
        reply = self.coordinator.accept_effect_detail(
            'owner:detail', 'No aparece en mis notas; esperaba verla en la carpeta de hoy')
        self.assertIn('actualizar el recurso registrado', reply)
        waiting = self.coordinator.store.get(task['id'])
        self.assertEqual(waiting['state'], 'WAITING_EFFECT_DECISION')
        self.assertEqual(waiting['effect_options']['1'], 'update')
        self.proxy.submit.assert_not_called()
        self.coordinator.accept_effect_decision('owner:update', '1')
        chosen = self.coordinator.store.get(task['id'])
        self.assertEqual(chosen['state'], 'RESOLVING_SCOPE')
        self.assertEqual(chosen['effect_constraints'][0]['resource_id'], 'record-1')

    def test_incorrect_recorded_write_asks_for_missing_value(self):
        task = self.coordinator.intake_recipe('owner:wrong-body', self.recipe,
                                             {'base': 'CLP', 'amount': 100})
        correction = {'service_id': 'records', 'operation': 'update',
                      'version': 'v1', 'identity_parameter': 'id',
                      'questions': {'body': {
                          'description': 'Contenido corregido de la nota',
                          'question': '¿Qué debería decir la nota?'}}}
        self.manager.voice_services = Mock()
        self.manager.voice_services.active_services.return_value = {
            'records': {'operations': {'update': {'version': 'v1', 'effect': 'write',
                                                   'parameters': ['id', 'body']}}}}
        receipts = [
            {'step_id': 'rate', 'version_id': 'rate-v1', 'output': {'rate': 2},
             'external_effects': [], 'services': []},
            {'step_id': 'apply', 'version_id': 'apply-v1', 'output': {'total': 200},
             'external_effects': [{'effect': 'write', 'status': 'completed',
                                   'resource_id': 'record-1',
                                   'effect_contract': {'correction': correction}}],
             'services': []}]
        self.coordinator.store.update(task['id'], state='RESOLVING_SCOPE',
            original_request='Convert and save', recipe_receipts=receipts,
            recipe_attempts=[{'recipe': self.recipe, 'receipts': receipts}],
            scope_answers=[{'questions': [], 'answer': 'La nota dice algo incorrecto',
                            'source': 'owner:correction'}])
        self.coordinator.requirements.resolve = Mock(return_value={
            'purpose': 'Correct the saved note', 'required_output': ['Correct note'],
            'params': {'base': 'CLP', 'amount': 100}, 'missing_questions': []})
        self.coordinator.revision_planner.target = Mock(return_value={
            'step_id': 'apply', 'problem': 'incorrect'})
        self.manager.llm.generate_content.return_value = (
            '{"required":["body"],"provided":{}}')
        self.coordinator.advance(self.coordinator.store.get(task['id']))
        waiting = self.coordinator.store.get(task['id'])
        self.assertEqual(waiting['state'], 'WAITING_EFFECT_DETAIL')
        self.assertIn('¿Qué debería decir la nota?', waiting['effect_question'])
        self.proxy.submit.assert_not_called()

    def test_correction_targets_only_challenged_write_and_keeps_other_receipt(self):
        task = self.coordinator.intake_recipe('owner:multi-write', self.recipe,
                                             {'base': 'CLP', 'amount': 100})
        correction = {'service_id': 'records', 'operation': 'update',
                      'version': 'v1', 'identity_parameter': 'id'}
        self.manager.voice_services = Mock()
        self.manager.voice_services.active_services.return_value = {
            'records': {'operations': {'update': {'version': 'v1', 'effect': 'write',
                                                   'parameters': ['id', 'body']}}}}
        receipts = [
            {'step_id': 'rate', 'external_effects': [{
                'effect': 'write', 'status': 'completed', 'resource_id': 'untouched-1',
                'effect_contract': {'correction': correction}}]},
            {'step_id': 'apply', 'external_effects': [{
                'effect': 'write', 'status': 'completed', 'resource_id': 'challenged-2',
                'effect_contract': {'correction': correction}}]},
        ]
        self.coordinator.store.update(task['id'], state='WAITING_EFFECT_DETAIL',
            effect_target_step='apply', recipe_attempts=[{
                'recipe': self.recipe, 'receipts': receipts}],
            scope_answers=[{'questions': [], 'answer': 'The save did not happen',
                            'source': 'owner:feedback'}])
        reply = self.coordinator.accept_effect_detail(
            'owner:detail', 'La tarea tiene fecha incorrecta; la nota está bien')
        self.assertIn('actualizar el recurso registrado', reply)
        waiting = self.coordinator.store.get(task['id'])
        self.assertEqual(waiting['pending_effect_constraints'], [
            {**correction, 'resource_id': 'challenged-2'}])
        self.coordinator.accept_effect_decision('owner:update', '1')
        self.coordinator.requirements.resolve = Mock(return_value={
            'purpose': 'Correct the challenged resource', 'required_output': [],
            'params': {}, 'missing_questions': []})
        self.coordinator.advance(self.coordinator.store.get(task['id']))
        revised = self.coordinator.store.get(task['id'])
        self.assertEqual(revised['state'], 'ROUTING')
        self.assertEqual(revised['effect_constraints'], [
            {**correction, 'resource_id': 'challenged-2'}])
        self.assertEqual(revised['recipe_attempts'][0]['receipts'], receipts)

    def test_effect_detail_asks_only_missing_correction_field(self):
        task = self.coordinator.intake_recipe('owner:folder-detail', self.recipe,
                                             {'base': 'CLP', 'amount': 100})
        correction = {'service_id': 'records', 'operation': 'update',
                      'version': 'v1', 'identity_parameter': 'id',
                      'questions': {
                          'folder': {'description': 'Carpeta de destino de la nota',
                                     'question': '¿En qué carpeta debería estar?'},
                          'body': {'description': 'Contenido corregido de la nota',
                                   'question': '¿Qué debería decir la nota?'}}}
        self.manager.voice_services = Mock()
        self.manager.voice_services.active_services.return_value = {
            'records': {'operations': {'update': {'version': 'v1', 'effect': 'write',
                                                   'parameters': ['id', 'folder', 'body']}}}}
        effect = {'effect': 'write', 'status': 'completed', 'resource_id': 'note-1',
                  'request_id': 'request-1', 'effect_contract': {'correction': correction}}
        self.coordinator.store.update(task['id'], state='WAITING_EFFECT_DETAIL',
            effect_target_step='apply', recipe_attempts=[{
                'recipe': self.recipe, 'receipts': [
                    {'step_id': 'apply', 'external_effects': [effect]}]}])
        self.manager.llm.generate_content.return_value = (
            '{"required":["folder"],"provided":{}}')
        question = self.coordinator.accept_effect_detail(
            'owner:folder-first', 'La nota está en otra carpeta')
        self.assertIn('¿En qué carpeta debería estar?', question)
        self.assertEqual(self.coordinator.store.get(task['id'])['state'],
                         'WAITING_EFFECT_DETAIL')
        self.manager.llm.generate_content.return_value = (
            '{"required":["folder"],"provided":{"folder":"X"}}')
        decision = self.coordinator.accept_effect_detail(
            'owner:folder-second', 'X')
        self.assertIn('actualizar el recurso registrado', decision)
        waiting = self.coordinator.store.get(task['id'])
        self.assertEqual(waiting['state'], 'WAITING_EFFECT_DECISION')
        self.assertEqual(waiting['effect_provided_fields'], {'folder': 'X'})
        self.assertIn('La nota está en otra carpeta', waiting['scope_answers'][-1]['answer'])
        self.assertIn(' X', waiting['scope_answers'][-1]['answer'])

    def test_multiple_writes_in_challenged_step_wait_for_identification(self):
        task = self.coordinator.intake_recipe('owner:ambiguous-write', self.recipe,
                                             {'base': 'CLP', 'amount': 100})
        correction = {'service_id': 'records', 'operation': 'update',
                      'version': 'v1', 'identity_parameter': 'id'}
        self.manager.voice_services = Mock()
        self.manager.voice_services.active_services.return_value = {
            'records': {'operations': {'update': {'version': 'v1', 'effect': 'write',
                                                   'parameters': ['id', 'body']}}}}
        effects = [{'effect': 'write', 'status': 'completed', 'resource_id': rid,
                    'request_id': f'request-{index}',
                    'effect_contract': {'correction': correction}}
                   for index, rid in enumerate(('item-1', 'item-2'), 1)]
        self.coordinator.store.update(task['id'], state='WAITING_EFFECT_DETAIL',
            effect_target_step='apply', recipe_attempts=[{
                'recipe': self.recipe, 'receipts': [
                    {'step_id': 'apply', 'external_effects': effects}]}])
        reply = self.coordinator.accept_effect_detail(
            'owner:detail', 'El segundo recurso tiene fecha incorrecta')
        self.assertIn('escritura 2 de 2', reply)
        self.assertEqual(self.coordinator.store.get(task['id'])['effect_target_request_id'],
                         'request-2')

    def test_numeric_reply_selects_write_without_extra_clarification(self):
        task = self.coordinator.intake_recipe('owner:numeric-write', self.recipe,
                                             {'base': 'CLP', 'amount': 100})
        correction = {'service_id': 'records', 'operation': 'update',
                      'version': 'v1', 'identity_parameter': 'id'}
        self.manager.voice_services = Mock()
        self.manager.voice_services.active_services.return_value = {
            'records': {'operations': {'update': {'version': 'v1', 'effect': 'write',
                                                   'parameters': ['id', 'body']}}}}
        effects = [{'effect': 'write', 'status': 'completed', 'resource_id': f'item-{index}',
                    'request_id': f'request-{index}',
                    'effect_contract': {'correction': correction}}
                   for index in (1, 2)]
        self.coordinator.store.update(task['id'], state='WAITING_EFFECT_DETAIL',
            effect_target_step='apply', recipe_attempts=[{
                'recipe': self.recipe, 'receipts': [
                    {'step_id': 'apply', 'external_effects': effects}]}])
        reply = self.coordinator.accept_effect_detail('owner:numeric-detail', '2')
        self.assertIn('escritura 2 de 2', reply)
        self.assertEqual(self.coordinator.store.get(task['id'])['effect_target_request_id'],
                         'request-2')

    def test_ambiguous_answer_closes_without_rejecting_existing_version(self):
        task = self.coordinator.intake_recipe('owner:ambiguous-answer', self.recipe,
                                             {'base': 'CLP', 'amount': 100})
        effects = [{'effect': 'write', 'status': 'completed', 'resource_id': rid,
                    'request_id': f'request-{index}',
                    'summary': {'name': 'Registro', 'context': f'Acción {index}'}}
                   for index, rid in enumerate(('item-1', 'item-2'), 1)]
        self.coordinator.store.update(task['id'], state='WAITING_EFFECT_DETAIL',
            effect_target_step='apply', recipe_attempts=[{
                'recipe': self.recipe, 'receipts': [
                    {'step_id': 'apply', 'version_id': 'apply-v1',
                     'external_effects': effects}]}])
        first = self.coordinator.accept_effect_detail(
            'owner:unclear-first', 'La tarea con fecha incorrecta')
        self.assertIn('¿Cuál de ellas', first)
        self.assertIn('Registro — Acción 1', first)
        self.assertIn('Registro — Acción 2', first)
        self.manager.llm.generate_content.return_value = '{"index":null,"evidence":null}'
        last = self.coordinator.accept_effect_detail(
            'owner:unclear-second', 'La que se ve en mi lista')
        closed = self.coordinator.store.get(task['id'])
        self.assertIn('no puedo distinguir', last)
        self.assertEqual(closed['state'], 'UNFULFILLABLE')
        self.assertEqual(closed['stop_reason'], 'owner_ambiguity')
        self.manager.hand.reject.assert_not_called()
        self.proxy.submit.assert_not_called()

    def test_unresolved_new_candidate_is_rejected_but_receipts_remain(self):
        task = self.coordinator.intake_recipe('owner:new-ambiguous', self.recipe,
                                             {'base': 'CLP', 'amount': 100})
        effects = [{'effect': 'write', 'status': 'completed', 'resource_id': rid}
                   for rid in ('item-1', 'item-2')]
        self.coordinator.store.update(task['id'], state='WAITING_EFFECT_DETAIL',
            effect_target_step='apply', recipe_attempts=[{
                'recipe': self.recipe, 'receipts': [
                    {'step_id': 'apply', 'version_id': 'new-v1',
                     'external_effects': effects}]}])
        original_version = self.manager.hand.version.side_effect
        self.manager.hand.version.side_effect = lambda vid: (
            {'version_id': vid, 'status': 'draft'} if vid == 'new-v1'
            else original_version(vid))
        self.coordinator.accept_effect_detail('owner:first', 'Uno de los dos está mal')
        self.manager.llm.generate_content.return_value = '{"index":null,"evidence":null}'
        self.coordinator.accept_effect_detail('owner:second', 'No sé cuál es')
        closed = self.coordinator.store.get(task['id'])
        self.assertEqual(closed['state'], 'UNFULFILLABLE')
        self.assertEqual(closed['stop_reason'], 'system_identity_gap')
        self.assertEqual(closed['recipe_attempts'][0]['receipts'][0]['external_effects'],
                         effects)
        self.manager.hand.reject.assert_called_once_with(
            'new-v1', 'Unresolved external effect')

    def test_pending_candidate_rejection_reconciles_after_failure(self):
        task = self.coordinator.intake_recipe('owner:rejection-recovery', self.recipe,
                                             {'base': 'CLP', 'amount': 100})
        effects = [{'effect': 'write', 'status': 'completed', 'resource_id': rid}
                   for rid in ('item-1', 'item-2')]
        self.coordinator.store.update(task['id'], state='WAITING_EFFECT_DETAIL',
            effect_target_step='apply', recipe_attempts=[{
                'recipe': self.recipe, 'receipts': [
                    {'step_id': 'apply', 'version_id': 'new-v1',
                     'external_effects': effects}]}])
        self.manager.hand.version.side_effect = lambda vid: {
            'version_id': vid, 'status': 'draft'} if vid == 'new-v1' else None
        self.manager.hand.reject.side_effect = [RuntimeError('temporary'), None]
        self.coordinator.accept_effect_detail('owner:first', 'Una está mal')
        self.manager.llm.generate_content.return_value = '{"index":null,"evidence":null}'
        self.coordinator.accept_effect_detail('owner:second', 'No sé cuál es')
        closed = self.coordinator.store.get(task['id'])
        self.assertEqual(closed['candidate_rejection_pending'], ['new-v1'])
        self.coordinator.tick()
        reconciled = self.coordinator.store.get(task['id'])
        self.assertEqual(reconciled['candidate_rejection_pending'], [])
        self.assertEqual(self.manager.hand.reject.call_count, 2)

    def test_llm_fallback_interprets_order_only_after_clarification(self):
        planner = self.coordinator.revision_planner
        self.assertIsNone(planner.effect_index('La del informe', 2, allow_llm=False))
        self.assertEqual(planner.effect_index('9', 9, allow_llm=False), 9)
        self.manager.llm.generate_content.return_value = '{"index":2,"evidence":"después"}'
        self.assertEqual(planner.effect_index('La que hiciste después', 2), 2)
        self.manager.llm.generate_content.assert_called_once()
        self.manager.llm.generate_content.return_value = '{"index":2,"evidence":"informe"}'
        self.assertIsNone(planner.effect_index('La del informe', 2))

    def test_normal_request_routes_to_existing_actions(self):
        self.manager.mind.process_message.return_value = {
            'action': 'ACTION_REQUIRED', 'extracted_goal': 'Convert currency'}
        self.manager.action_planner = Mock()
        self.manager.action_planner.propose.return_value = {
            'recipe': self.recipe, 'inputs': {'base': 'CLP', 'amount': 100},
            'missing_specs': {}}
        task = self.coordinator.intake('owner:5', 'Convert 100 CLP')
        self.coordinator.advance(task)
        routed = self.coordinator.store.get(task['id'])
        self.assertEqual(routed['state'], 'RECIPE_STEP')
        self.assertEqual(routed['id'], task['id'])
        self.assertEqual(routed['recipe']['steps'][0]['version_id'], 'rate-v1')
        self.manager.hand.generate.assert_not_called()

    def test_structured_action_executes_planned_actions(self):
        self.coordinator.notify = Mock()
        self.manager.mind.process_message.return_value = {
            'action': 'ACTION_REQUIRED', 'extracted_goal': 'Convierte 100 CLP con la tasa actual'}
        self.manager.action_planner = Mock()
        self.manager.action_planner.propose.return_value = {
            'recipe': self.recipe, 'inputs': {'base': 'CLP', 'amount': 100},
            'missing_specs': {}}
        task = self.coordinator.intake('owner:misclassified', 'Convierte 100 CLP con la tasa actual')
        self.coordinator.advance(task)
        routed = self.coordinator.store.get(task['id'])
        self.assertEqual(routed['state'], 'RECIPE_STEP')
        self.assertEqual(routed['routing_action'], 'ACTION_REQUIRED')
        self.manager.memory.append_long_term.assert_called_once_with('user_chat', task['request'])
        first = self.advance_to_running(task['id'])
        self.proxy.get.return_value = {'status': 'finished', 'returncode': 0,
                                      'stdout': '{"rate": 2}'}
        self.coordinator.advance(first)
        second = self.advance_to_running(task['id'])
        self.assertEqual(second['payload']['params'], {'amount': 100, 'rate': 2})
        self.proxy.get.return_value = {'status': 'finished', 'returncode': 0,
                                      'stdout': '{"total": 200}'}
        self.coordinator.advance(second)
        current = self.coordinator.store.get(task['id'])
        self.assertEqual(current['state'], 'UNVERIFIED')
        self.assertEqual(current['recipe_result'], 200)
        self.assertEqual(len(current['recipe_receipts']), 2)
        self.assertFalse(any('Voy a consultar' in call.args[0]
                             for call in self.coordinator.notify.call_args_list))

    def test_short_instruction_uses_history_and_durable_task_context(self):
        self.manager.memory.get_chat_history.return_value = 'Usuario: Convierte 100 CLP usando la tasa actual.'
        previous = self.coordinator.store.create('owner:previous', 'Convierte 100 CLP',
                                                 state='COMPLETED', executions=0)
        self.manager.mind.process_message.return_value = {
            'action': 'ACTION_REQUIRED', 'extracted_goal': 'Convierte 100 CLP con la tasa actual'}
        self.manager.action_planner = Mock()
        self.manager.action_planner.propose.return_value = {
            'recipe': self.recipe, 'inputs': {'base': 'CLP', 'amount': 100},
            'missing_specs': {}}
        task = self.coordinator.intake('owner:short', 'Hazlo')
        self.coordinator.advance(task)
        current = self.coordinator.store.get(task['id'])
        self.assertEqual(current['state'], 'RECIPE_STEP')
        context = self.manager.action_planner.propose.call_args.kwargs
        self.assertIn('100 CLP', context['conversation_context'])
        self.assertEqual(context['task_context'][0]['id'], previous['id'])
        self.assertEqual(context['task_context'][0]['executions'], 0)
        self.assertFalse(self.manager.mind.process_message.call_args.kwargs['persist'])

    def test_weather_followup_without_skill_waits_for_real_creation_approval(self):
        self.coordinator.notify = Mock()
        self.manager.memory.get_chat_history.return_value = (
            'Usuario: Quiero el clima de mañana para Santiago de Chile usando openweathermap.org.')
        self.manager.mind.process_message.return_value = {
            'action': 'ACTION_REQUIRED', 'extracted_goal': 'Consultar el clima de ma?ana para Santiago de Chile con openweathermap.org'}
        self.manager.hand.catalog.active.return_value = []
        self.manager.action_planner = ActionPlanner(self.manager.llm, self.manager.hand.catalog)
        self.manager.llm.generate_content.return_value = json.dumps({
            'steps': [{'id': 'weather', 'new_skill': {
                'skill_id': 'weather',
                'description': 'Consultar el pronóstico de mañana para Santiago de Chile.',
                'capabilities': ['Network'], 'inputs': {'city': 'string'},
                'outputs': {'forecast': 'string'}}}],
            'inputs': {'city': 'Santiago de Chile'}})
        self.coordinator._setup_service = Mock(return_value={
            'step_id': 'weather', 'hash': 'd' * 64, 'effect': 'read',
            'host': 'openweathermap.org', 'name': 'Consulta meteorológica',
            'ref': {'service_id': 'weather', 'operation': 'forecast', 'version': 'v1'}})
        task = self.coordinator.intake('owner:weather-followup', 'Hazlo')
        self.coordinator.advance(task)
        waiting = self.coordinator.store.get(task['id'])
        self.assertEqual(waiting['state'], 'WAITING_CREATION_APPROVAL')
        self.assertEqual(waiting['recipe_input'], {'city': 'Santiago de Chile'})
        message = self.coordinator.notify.call_args.args[0]
        self.assertIn('openweathermap.org', message)
        self.assertIn('Santiago de Chile', message)
        self.assertIn('Responde y/n', message)
        self.manager.hand.generate.assert_not_called()
        self.proxy.submit.assert_not_called()
        self.manager.memory.append_long_term.assert_called_once_with('user_chat', 'Hazlo')

    def test_greeting_does_not_select_a_skill(self):
        self.coordinator.notify = Mock()
        self.manager.mind.process_message.return_value = {'action': 'GENERAL_CHAT', 'reply': 'Hola.'}
        self.manager.action_planner = Mock()
        self.manager.action_planner.propose.return_value = None
        self.manager.skill_selector = Mock()
        task = self.coordinator.intake('owner:greeting', 'Hola')
        self.coordinator.advance(task)
        self.assertEqual(self.coordinator.store.get(task['id'])['state'], 'COMPLETED')
        self.manager.skill_selector.select.assert_not_called()
        self.coordinator.notify.assert_called_once_with('Hola.')

    def test_invalid_plan_does_not_become_completed_chat(self):
        self.coordinator.notify = Mock()
        self.manager.mind.process_message.return_value = {
            'action': 'ACTION_REQUIRED', 'extracted_goal': 'Consulta los datos actuales'}
        self.manager.action_planner = Mock()
        self.manager.action_planner.propose.side_effect = ValueError('Plan inválido')
        task = self.coordinator.intake('owner:bad-plan', 'Consulta los datos actuales')
        self.coordinator._run_task(task)
        self.assertEqual(self.coordinator.store.get(task['id'])['state'], 'FAILED')
        self.manager.memory.append_long_term.assert_not_called()
        self.assertFalse(any('Ya estoy ejecutando' in call.args[0]
                             for call in self.coordinator.notify.call_args_list))

    def test_missing_action_is_generated_inside_same_objective(self):
        self.manager.mind.process_message.return_value = {
            'action': 'ACTION_REQUIRED', 'extracted_goal': 'Convert currency'}
        self.manager.action_planner = Mock()
        missing_recipe = json.loads(json.dumps(self.recipe))
        missing_recipe['steps'][1]['version_id'] = 'missing-apply'
        action = {key: self.specs['apply-v1'][key] for key in (
            'skill_id', 'description', 'use_cases', 'input_schema', 'output_schema')}
        action['capabilities'] = []
        self.manager.action_planner.propose.return_value = {
            'recipe': missing_recipe, 'inputs': {'base': 'CLP', 'amount': 100},
            'missing_specs': {'apply': action}}
        task = self.coordinator.intake('owner:6', 'Convert 100 CLP')
        self.coordinator.advance(task)
        waiting = self.coordinator.store.get(task['id'])
        self.assertEqual(waiting['state'], 'WAITING_CREATION_APPROVAL')
        self.coordinator.store.update(task['id'], expected={'WAITING_CREATION_APPROVAL'},
            state='SCOPE_PLANNING', creation_consent={'source': 'owner:7'})
        self.coordinator.requirements.questions = Mock(return_value=[])
        self.coordinator.advance(self.coordinator.store.get(task['id']))
        first = self.advance_to_running(task['id'])
        self.proxy.get.return_value = {'status': 'finished', 'returncode': 0,
                                       'stdout': '{"rate": 2}'}
        self.coordinator.advance(first)
        self.manager.hand.generate.return_value = ('# MODE: SAFE\nprint("{}")', 'SAFE', [])
        draft = {'version_id': 'apply-v2', 'skill_name': 'apply', 'status': 'draft',
                 'artifact_hash': 'c' * 64,
                 'manifest': json.dumps({'purpose': action['description'], 'capabilities': []})}
        old_version = self.manager.hand.version.side_effect
        self.manager.hand.version.side_effect = lambda vid: draft if vid == 'apply-v2' else old_version(vid)
        self.manager.hand.create.return_value = draft
        self.manager.hand.describe.return_value = {
            **self.specs['apply-v1'], 'services': []}
        old_catalog = self.manager.hand.catalog.get.side_effect
        self.manager.hand.catalog.get.side_effect = lambda vid: (
            {'status': 'candidate', 'spec': self.specs['apply-v1'], 'spec_hash': 'b' * 64}
            if vid == 'apply-v2' else old_catalog(vid))
        self.manager.hand.catalog.stage.return_value = 'b' * 64
        second = self.advance_to_running(task['id'])
        self.assertEqual(second['payload']['params'], {'amount': 100, 'rate': 2})
        self.assertEqual(second['recipe']['steps'][1]['version_id'], 'apply-v2')
        self.proxy.get.return_value = {'status': 'finished', 'returncode': 0,
                                       'stdout': '{"total": 200}'}
        self.coordinator.advance(second)
        final = self.coordinator.store.get(task['id'])
        self.assertEqual(final['state'], 'UNVERIFIED')
        self.assertEqual(final['recipe_result'], 200)
        self.manager.hand.approve.return_value = {'artifact_hash': 'c' * 64}
        self.coordinator.verify(task['id'], final['execution_id'], True, 'owner:8')
        self.coordinator.advance(self.coordinator.store.get(task['id']))
        self.manager.hand.catalog.promote.assert_called_once_with(
            'apply-v2', task['id'], final['execution_id'], 'c' * 64)

    def test_planner_accepts_typed_missing_action_and_rejects_partial_binding(self):
        missing_recipe = json.loads(json.dumps(self.recipe))
        missing_recipe['steps'][1]['version_id'] = 'missing-apply'
        action = {key: self.specs['apply-v1'][key] for key in (
            'skill_id', 'description', 'use_cases', 'input_schema', 'output_schema')}
        action['capabilities'] = []
        llm = Mock()
        proposal = {'recipe': missing_recipe, 'inputs': {'base': 'CLP', 'amount': 100},
                    'missing_specs': {'apply': action}}
        llm.generate_content.return_value = json.dumps(proposal)
        catalog = Mock()
        catalog.active.return_value = [
            {'version_id': 'rate-v1', 'skill_id': 'rate', 'spec': self.specs['rate-v1']}]
        planner = ActionPlanner(llm, catalog)
        self.assertEqual(planner.propose('Convert 100 CLP', lambda _: True), proposal)
        proposal['recipe']['steps'][1]['bindings']['rate'] = 'input.base'
        llm.generate_content.return_value = json.dumps(proposal)
        with self.assertRaises(ValueError):
            planner.propose('Convert 100 CLP', lambda _: True)

    def test_planner_pins_prior_resource_in_single_correction_action(self):
        update = {'skill_id': 'update-record', 'description': 'Update one resource.',
                  'use_cases': ['Correct a resource'],
                  'input_schema': schema(id='string', body='string'),
                  'output_schema': schema(id='string'), 'runtime': 'SAFE',
                  'dependencies': [],
                  'services': [{'service_id': 'records', 'operation': 'update',
                                'version': 'v1'}],
                  'verifier': {'kind': 'owner_criterion'}}
        recipe = {'recipe_id': 'correct-record',
                  'input_schema': schema(id='string', body='string'),
                  'steps': [{'id': 'update', 'version_id': 'update-v1',
                             'bindings': {'id': 'input.id', 'body': 'input.body'}}],
                  'output': 'update.id'}
        llm = Mock()
        llm.generate_content.return_value = json.dumps({
            'recipe': recipe, 'inputs': {'body': 'corrected'}, 'missing_specs': {}})
        catalog = Mock()
        catalog.active.return_value = [{'version_id': 'update-v1',
                                        'skill_id': 'update-record', 'spec': update}]
        correction = {'service_id': 'records', 'operation': 'update',
                      'version': 'v1', 'identity_parameter': 'id',
                      'resource_id': 'record-1'}
        proposed = ActionPlanner(llm, catalog).propose(
            'Correct the same resource', lambda _: True, [correction])
        self.assertEqual(proposed['inputs'], {'id': 'record-1', 'body': 'corrected'})


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
            'credential_header': 'Authorization', 'credential_prefix': '',
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
                manager.hand.describe_service = Mock(side_effect=lambda t: (descriptor, manager.hand.propose_service(descriptor)))
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
            'effect': 'read', 'credential_ref': None, 'credential_header': 'Authorization',
            'credential_prefix': '', 'effect_contract': None,
        }
        mail_desc = {
            'service_id': 'mail', 'operation': 'send', 'version': 'v1',
            'url': 'https://mail.example.org/send', 'method': 'POST',
            'parameters': ['to', 'noticias'], 'content_types': ['application/json'],
            'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
            'effect': 'write', 'credential_ref': None, 'credential_header': 'Authorization',
            'credential_prefix': '',
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
            'effect': 'write', 'credential_ref': None, 'credential_header': 'Authorization',
            'credential_prefix': '',
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
                manager.hand.describe_service = Mock(side_effect=[
                    (news_desc, manager.hand.propose_service(news_desc)),
                    (mail_desc, manager.hand.propose_service(mail_desc)),
                ])
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
