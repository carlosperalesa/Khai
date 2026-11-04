"""Regression coverage for audited production failures and the destructive reset boundary."""
import json
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.kernel.control_plane import ControlPlane
from app.kernel.database import connection
from app.kernel.database import backup
from app.mind.conversation import ConversationStore
from app.mind.action_planner import ActionPlanner
from app.mind.provider_resolution import explicit_provider
from app.heart.recovery import Recovery
from app.heart.catalog import validate_plan, task_binding
from app.voice.mailbox import VoiceMailbox
from app.voice.gateway.telegram_gateway import TelegramHandler
from app.voice.history_admin import delete_history
from scripts.reset_history import schema_reset, reset
from tests.fixtures import Fixture


class DeliveryAuthorityTests(unittest.TestCase):
    def test_ttl_begins_after_durable_delivery_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder)/'control.sqlite3'
            plane = ControlPlane(db)
            conversations = ConversationStore(db)
            mailbox = VoiceMailbox(db)
            sid = conversations.session('owner', 'owner')
            with patch('app.pixy.authorization.time.time', return_value=100):
                auth = plane.ports.pixy.issue('case-audit123', ['TaskRemediate'], 'task', 'a'*64, 'source', delivery_required=True)
            qid = conversations.question(sid, 'authorization', auth['authorization_id'], auth['plan_hash'], 'pending')
            delivery = mailbox.enqueue('Approve?', metadata={'conversation_id': sid, 'question_id': qid,
                'authorization_id': auth['authorization_id'], 'buttons': [[{'label': 'Yes', 'value': 'y '+qid}]]})
            conversations.attach(qid, delivery)
            with patch('app.pixy.authorization.time.time', return_value=1100):
                self.assertEqual(plane.ports.pixy.get(auth['authorization_id'])['status'], 'pending_delivery')
                with self.assertRaises(PermissionError):
                    plane.ports.pixy.decide(auth['authorization_id'], True, 'owner')
                mailbox.deliver_one(lambda text, meta: {'ok': True, 'result': {'message_id': 42}})
                restarted = ControlPlane(db)
                active = restarted.ports.pixy.get(auth['authorization_id'])
                self.assertEqual(active['status'], 'pending')
                self.assertEqual(active['expires_at'], 2000)
                decided = restarted.ports.pixy.decide(auth['authorization_id'], True, 'owner')
                self.assertEqual(decided['status'], 'approved')
                self.assertEqual(restarted.ports.pixy.get(auth['authorization_id'])['expires_at'], 2000)

    def test_invalid_buttons_never_enter_delivery_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            mailbox = VoiceMailbox(Path(folder)/'control.sqlite3')
            with self.assertRaises(ValueError):
                mailbox.enqueue('Approve', {'buttons': [{'text': 'Yes', 'callback_data': 'x'}]})
            self.assertEqual(mailbox.pending_counts()['outbox'], {})

    def test_batches_scope_age_and_rate_limit_without_claiming_individual_deletions(self):
        sender = Mock(side_effect=[SimpleNamespace(json=lambda: {'ok': False, 'error_code': 429,
                        'parameters': {'retry_after': 2}}), SimpleNamespace(json=lambda: {'ok': True}),
                        SimpleNamespace(json=lambda: {'ok': True})])
        sleep = Mock()
        items = {('owner', i): 100 for i in range(1, 102)}
        items[('owner', 300)] = -200000
        items[('stranger', 301)] = 100
        report = delete_history(SimpleNamespace(user_id='owner', base_url='unused'), items, 200, send=sender, sleep=sleep)
        self.assertEqual(report['requested'], 101)
        self.assertEqual(report['batches_accepted'], 2)
        self.assertFalse(report['individual_deletions_verified'])
        sleep.assert_called_once_with(2)
        for call in sender.call_args_list:
            self.assertEqual(call.kwargs['json']['chat_id'], 'owner')
            self.assertLessEqual(len(call.kwargs['json']['message_ids']), 100)


class AuditedBehaviorTests(Fixture):
    def test_unknown_assessment_cannot_complete_or_repeat_execution(self):
        self.llm.generate_content.side_effect = TimeoutError()
        task = self.artifact(state='RUNNING', execution_id='run_'+'b'*32)
        self.proxy.get.return_value = {'status': 'finished', 'returncode': 0, 'stdout': '42'}
        self.c.advance(task)
        current = self.store.get(task['id'])
        self.assertEqual(current['state'], 'UNVERIFIED')
        self.assertIsNone(current['goal_assessment']['success'])
        self.proxy.submit.assert_not_called()
        self.c.verify(task['id'], task['execution_id'], True, 'owner:answer')
        self.c.advance(self.store.get(task['id']))
        self.assertEqual(self.store.get(task['id'])['state'], 'UNVERIFIED')

    def test_primitive_names_cannot_be_modified_or_impersonated(self):
        for name in ('web-search', 'document-parser'):
            for operation in (lambda: self.manager.hand.create(name, '# MODE: SAFE\nprint(1)', {'purpose': name}),
                              lambda: self.manager.hand.delete(name), lambda: self.manager.hand.archive(name)):
                with self.assertRaises(PermissionError):
                    operation()
        generated = self.manager.hand.create('my-search', '# MODE: SAFE\nprint(1)', {'purpose': 'local example'})
        with connection(self.store.db_path) as conn:
            self.assertEqual(conn.execute('SELECT kind FROM skill_origins WHERE version_id=?',
                                         (generated['version_id'],)).fetchone()[0], 'generated')

    def test_primitive_evolution_becomes_a_separate_derivative(self):
        spec = {'skill_id': 'web-search', 'description': 'Search public documents', 'use_cases': ['search'],
                'input_schema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query'], 'additionalProperties': False},
                'output_schema': {'type': 'object', 'properties': {'result': {'type': 'string'}}, 'required': ['result'], 'additionalProperties': False},
                'runtime': 'SAFE', 'dependencies': [], 'services': [], 'verifier': {'kind': 'owner_criterion'}}
        planner = ActionPlanner(self.llm, SimpleNamespace(active=lambda: []))
        recipe, missing, specs = {'steps': [{'id': 'search', 'version_id': 'base'}]}, {}, {'base': spec}
        self.assertTrue(planner._evolve_step_for_coverage(recipe, specs, missing, ['dated results'], 'search dated', []))
        self.assertEqual(missing['search']['derived_from_version_id'], 'base')
        self.assertNotEqual(missing['search']['skill_id'], 'web-search')
        self.assertEqual(specs['base']['skill_id'], 'web-search')

    def test_routing_failure_is_not_diagnosed_as_skill_code(self):
        self.assertEqual(Recovery.classify_cause({'stage': 'routing', 'error': 'ModelUnavailable'})[0], 'model_unavailable')
        self.assertEqual(Recovery.classify_cause({'stage': 'routing', 'error': 'TypeError'})[0], 'core_fault')
        self.assertEqual(Recovery.classify_cause({'error': 'unrecognized failure'})[0], 'unknown')
        with self.assertRaisesRegex(ValueError, 'use_existing_skill'):
            ActionPlanner._convert_template_steps({'steps': [{'use_existing_skill': {'id': 'x'}}]}, [])

    def test_planner_rejection_preserves_original_private_evidence(self):
        self.llm.generate_content.return_value = '{"steps":[{"use_existing_skill":{"id":"x"}}]}'
        self.llm.record_contract_failure.return_value = {'id': 'rejection_audit'}
        planner = ActionPlanner(self.llm, SimpleNamespace(active=lambda: []))
        with self.assertRaises(ValueError) as failure:
            planner.propose('build example', lambda spec: True)
        self.assertEqual(failure.exception.diagnostic['id'], 'rejection_audit')
        self.assertIn('use_existing_skill', self.llm.record_contract_failure.call_args.args[1])

    def test_failed_local_and_nano_never_announce_nano_connected(self):
        from app.voice.gateway.llm_gateway import ModelUnavailable
        from tests.test_model_contract import ModelContractTests
        gateway = ModelContractTests().gateway()
        gateway._call_dolphin = Mock(side_effect=TimeoutError())
        gateway.probe_nano = Mock(side_effect=ModelUnavailable('Nano unavailable'))
        with self.assertRaises(ModelUnavailable) as failure:
            gateway.generate_content('hello')
        self.assertEqual(failure.exception.evidence['attempts'][0]['profile'], 'low')
        texts = ' '.join(call.args[0] for call in gateway._notify.call_args_list)
        self.assertIn('falló', texts)
        self.assertNotIn('[Modo nano conectado]', texts)

    def test_evolution_plan_contains_task_and_exact_skill_binding(self):
        task = self.task(state='DIAGNOSING', skill_name='weather', version_id='v1', artifact_hash='c'*64)
        diagnosis = {'observed_task': task, 'findings': [{'code': 'cognitive_discrepancy', 'severity': 'high',
            'detail': 'missing wind', 'remedy': 'evolve_skill:'+task['id'],
            'task_binding': task_binding(task), 'corrective_instruction': 'Include wind from approved service'}]}
        plan = Recovery.design('case-audit123', diagnosis)
        self.assertEqual(validate_plan(plan)['steps'], [{'action': 'evolve_skill', 'target': 'task'}])
        self.assertEqual(plan['evolution']['version_id'], 'v1')
        plan['evolution']['artifact_hash'] = 'invalid'
        with self.assertRaises(ValueError):
            validate_plan(plan)

    def test_provider_requires_actual_source_mention(self):
        self.assertIsNone(explicit_provider('made_up', 'create an image'))
        self.assertEqual(explicit_provider('made_up', 'use Made Up please', 'Made Up'), 'made_up')
        self.assertIsNone(explicit_provider('made_up', 'create an image', 'Made Up'))
        self.assertIsNone(explicit_provider('made_up', 'No uses Made Up', 'Made Up'))

    def test_recovery_cannot_execute_unchanged_code_and_inputs(self):
        from app.mind.recovery_attempt import assert_changed_attempt
        task = {'params': {'x': 1}, 'services': [], 'recovery_history': [
            {'artifact_hash': 'a'*64, 'params': {'x': 1}, 'services': []}]}
        with self.assertRaisesRegex(ValueError, 'unchanged'):
            assert_changed_attempt(task, 'a'*64)
        assert_changed_attempt(task, 'b'*64)
        assert_changed_attempt(dict(task, params={'x': 2}), 'a'*64)

    def test_schema_reset_keeps_knowledge_but_removes_tasks_and_receipts(self):
        self.task(state='FAILED')
        self.memory.set_preference('city', 'Santiago')
        self.memory.record_episode('old_task', 'old_run', 'private conversation', 'failure', 'routing', 'private transcript')
        target = self.root/'clean.sqlite3'
        schema_reset(Path(self.store.db_path), target)
        with closing(sqlite3.connect(target)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM control_tasks').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM episode_notes').fetchone()[0], 0)
            self.assertEqual(json.loads(conn.execute("SELECT value_json FROM preferences WHERE key='city'").fetchone()[0]), 'Santiago')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM retained_learning_stats').fetchone()[0], 1)
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND sql LIKE '%DROP%'").fetchone())

    def test_offline_reset_preserves_artifact_and_identity_and_removes_backups(self):
        data = self.root/'data'
        data.mkdir()
        version = self.manager.hand.create('retained-skill', '# MODE: SAFE\nprint(42)', {'purpose': 'retained skill'})
        self.manager.hand.approve(version['version_id'], user_approved=True)
        shutil.copytree(self.manager.hand.root, data/'hand')
        self.memory.set_preference('city', 'Santiago')
        self.store.create('private:old', 'historical private task', state='FAILED')
        backup(self.store.db_path, data/'control.sqlite3')
        (data/'voice').mkdir()
        (data/'voice/secrets.env').write_text('DUMMY=test')
        (data/'mind').mkdir()
        (data/'mind/identity.yaml').write_text('content: identity')
        (data/'logs').mkdir()
        (data/'logs/history.log').write_text('private transcript')
        (self.root/'backups').mkdir()
        (self.root/'backups/history.txt').write_text('private transcript')
        self.assertFalse(reset(data)['applied'])
        report = reset(data, apply=True)
        self.assertTrue(report['applied'])
        self.assertEqual((data/'voice/secrets.env').read_text(), 'DUMMY=test')
        self.assertTrue((data/'hand/retained-skill/app/main.py').is_file())
        self.assertFalse((self.root/'backups').exists())
        self.assertFalse((data/'logs').exists())
        with closing(sqlite3.connect(data/'control.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM control_tasks').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM versions').fetchone()[0], 1)
