"""A recipe keeps typed step evidence under one durable objective."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.mind.coordinator import Coordinator
from app.mind.action_planner import ActionPlanner


def schema(**fields):
    return {'type': 'object', 'properties': {name: {'type': kind} for name, kind in fields.items()},
            'required': list(fields), 'additionalProperties': False}


class RecipeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        hand = Mock()
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


if __name__ == '__main__':
    unittest.main()
