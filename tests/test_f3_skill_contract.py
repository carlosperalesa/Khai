"""Compatibility and authority checks for reusable skill contracts."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.hand.hand import Hand
from app.hand.skill_spec import extract_params, spec_hash, validate_recipe, validate_skill_spec
from app.pixy.control import ControlStore
from app.mind.skill_selector import SkillSelector
from app.voice.gateway.service_registry import ServiceRegistry, HandServicePort, PixyServicePort, VoiceServicePort
from app.voice.gateway.external_gateway import ExternalGateway


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
            'effect': 'read', 'credential_ref': None, 'effect_contract': None}
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data'
            (data / 'memory').mkdir(parents=True)
            (data / 'memory' / 'agent.md').write_text('Test identity', encoding='utf-8')
            with patch.dict('os.environ', {'KHAI_RUNTIME': 'local', 'TELEGRAM_BOT_TOKEN': 'token',
                                           'TELEGRAM_USER_ID': 'owner'}):
                manager = ModuleManager(data_dir=str(data))
                manager.telegram = Mock()
                manager.llm.generate_content = Mock(side_effect=[
                    json.dumps(descriptor),
                    '# MODE: SAFE\nprint("ok")'
                ])
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
        llm.generate_content.return_value = '# MODE: SAFE\nprint("ok")'
        with tempfile.TemporaryDirectory() as folder:
            hand = Hand(Path(folder) / 'hand', Mock(), llm=llm, capabilities=caps_local)
            code, mode, deps = hand.generate({'request': 'test'}, [])
            self.assertEqual(mode, 'SAFE')
            prompt = llm.generate_content.call_args.args[0]
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
            'effect': 'read', 'credential_ref': 'FX_API_KEY', 'effect_contract': None}
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
            'effect': 'read', 'credential_ref': 'FX_API_KEY', 'effect_contract': None}
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
            request.write_text('{"service":"fx","operation":"rate","params":{}}', encoding='utf-8')
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
        llm.generate_content.return_value = '# MODE: SAFE\nprint("ok")'
        with tempfile.TemporaryDirectory() as folder:
            hand = Hand(Path(folder) / 'hand', Mock(), llm=llm)
            hand.generate({'request': 'consultar tasa', 'manifest': {'capabilities': ['Network']},
                           'services': [{'service_id': 'fx', 'operation': 'get-rate', 'version': 'v1'}]}, [])
        prompt = llm.generate_content.call_args.args[0]
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


if __name__ == '__main__':
    unittest.main()
