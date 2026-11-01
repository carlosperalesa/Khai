"""Heart organ tests: Doctor recovery cycle, diagnosis taxonomy, incident deduplication, and Senses evidence."""
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.hand.hand import Hand
from app.heart.beat import Beat
from app.heart.catalog import (
    ACTIONS,
    CAUSE_TAXONOMY,
    TARGETS,
    cause_fingerprint,
    plan_hash,
    requires_approval,
    validate_plan,
)
from app.heart.recovery import Recovery
from app.kernel.database import connection, migrate
from app.kernel.events import Events
from app.kernel.journal import Journal
from app.pixy.authorization import AuthorizationStore
from app.pixy.pixy import PixyMind
from app.senses.senses import OBSERVERS, Senses
from app.voice.runtime.recovery_engine import RecoveryEngine
from app.voice.runtime.recovery_runtime import ProxyRecoveryExecutor, RecoveryRejected


class RecoveryCatalogTests(unittest.TestCase):
    def test_catalog_validation(self):
        plan = {'target': 'core', 'steps': [{'action': 'restart_container', 'target': 'core'},
                                            {'action': 'health_check', 'target': 'core'}],
                'capabilities': ['DockerRestart', 'HealthCheck']}
        valid = validate_plan(plan)
        self.assertEqual(valid['target'], 'core')
        self.assertEqual(valid['capabilities'], ['DockerRestart', 'HealthCheck'])
        self.assertTrue(requires_approval(plan))

    def test_invalid_target_or_action_rejected(self):
        with self.assertRaises(ValueError):
            validate_plan({'target': 'evil', 'steps': [{'action': 'restart_container', 'target': 'evil'}],
                          'capabilities': ['DockerRestart']})
        with self.assertRaises(ValueError):
            validate_plan({'target': 'core', 'steps': [{'action': 'shell_exec', 'target': 'core'}],
                          'capabilities': ['DockerRestart']})
        with self.assertRaises(ValueError):
            validate_plan({'target': 'core', 'steps': [{'action': 'retry_task', 'target': 'core'}],
                          'capabilities': ['TaskRemediate']})

    def test_task_subject_is_required_and_bound_into_hash(self):
        base = {'target': 'task', 'steps': [{'action': 'retry_task', 'target': 'task'}],
                'capabilities': ['TaskRemediate']}
        with self.assertRaises(ValueError):
            validate_plan(base)
        binding = {'exists': True, 'goal_revision': 0, 'execution_id': None, 'artifact_hash': None, 'attempt_number': 1}
        one = dict(base, task_id='task_' + '1' * 32, task_binding=binding)
        two = dict(base, task_id='task_' + '2' * 32, task_binding=binding)
        self.assertNotEqual(plan_hash('case-20261026aaaabbbb', one),
                            plan_hash('case-20261026aaaabbbb', two))

    def test_capabilities_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            validate_plan({'target': 'core', 'steps': [{'action': 'restart_container', 'target': 'core'}],
                          'capabilities': ['DockerRestart', 'ExtraCap']})


class AuthorizationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / 'auth.sqlite3'
        self.store = AuthorizationStore(self.db, ttl=10)

    def tearDown(self):
        self.tmp.cleanup()

    def test_issue_verify_decide_and_consume(self):
        case_id = 'case-20261025abcd1234'
        plan = {'target': 'core', 'steps': [{'action': 'restart_container', 'target': 'core'}],
                'capabilities': ['DockerRestart']}
        h = plan_hash(case_id, plan)
        auth = self.store.issue(case_id, ['DockerRestart'], 'core', h, 'telegram:1')
        self.assertEqual(auth['status'], 'pending')
        self.assertEqual(self.store.pending()['authorization_id'], auth['authorization_id'])

        with self.assertRaises(PermissionError):
            self.store.verify(auth['authorization_id'], auth['nonce'], case_id, ['DockerRestart'], 'core', h)

        decided = self.store.decide(auth['authorization_id'], True, 'telegram:1')
        self.assertEqual(decided['status'], 'approved')

        verified = self.store.verify(auth['authorization_id'], auth['nonce'], case_id, ['DockerRestart'], 'core', h)
        self.assertEqual(verified['status'], 'approved')

        consumed = self.store.consume(auth['authorization_id'], auth['nonce'], case_id, ['DockerRestart'], 'core', h,
                                      consumer='hand:' + case_id)
        self.assertEqual(consumed['status'], 'consumed')

        retry = self.store.consume(auth['authorization_id'], auth['nonce'], case_id, ['DockerRestart'], 'core', h,
                                   consumer='hand:' + case_id)
        self.assertEqual(retry['status'], 'consumed')

        with self.assertRaises(PermissionError):
            self.store.consume(auth['authorization_id'], auth['nonce'], case_id, ['DockerRestart'], 'core', h,
                               consumer='hand:other_case')

    def test_tampered_terms_rejected(self):
        case_id = 'case-20261025abcd1234'
        h = plan_hash(case_id, {'target': 'core', 'steps': [{'action': 'health_check', 'target': 'core'}],
                                'capabilities': ['HealthCheck']})
        auth = self.store.issue(case_id, ['HealthCheck'], 'core', h, 'user')
        self.store.decide(auth['authorization_id'], True, 'user')

        with self.assertRaises(PermissionError):
            self.store.verify(auth['authorization_id'], auth['nonce'], case_id, ['DockerRestart'], 'core', h)

        with self.assertRaises(PermissionError):
            self.store.verify(auth['authorization_id'], auth['nonce'], case_id, ['HealthCheck'], 'proxy', h)

        with self.assertRaises(PermissionError):
            self.store.verify(auth['authorization_id'], auth['nonce'], case_id, ['HealthCheck'], 'core', '0' * 64)

    def test_expiration_and_multiple_pending_decisions(self):
        case_id1 = 'case-20261025abcd0001'
        h1 = '1' * 64
        short_store = AuthorizationStore(self.db, ttl=0.01)
        auth1 = short_store.issue(case_id1, ['DockerRestart'], 'core', h1, 'user')
        time.sleep(0.02)
        with self.assertRaises(PermissionError):
            short_store.decide(auth1['authorization_id'], True, 'user')

        auth2 = self.store.issue('case-20261025abcd0002', ['DockerRestart'], 'core', '2' * 64, 'user')
        auth3 = self.store.issue('case-20261025abcd0003', ['DockerRestart'], 'core', '3' * 64, 'user')
        self.assertEqual(self.store.get(auth2['authorization_id'])['status'], 'pending')
        self.assertEqual([a['authorization_id'] for a in self.store.pending_all()],
                         [auth2['authorization_id'], auth3['authorization_id']])
        self.assertEqual(self.store.pending()['authorization_id'], auth3['authorization_id'])

    def test_expired_decision_and_rejected_check_have_durable_separate_cycles(self):
        auth = self.store.issue('case-20261025abcd0020', ['HealthCheck'], 'core', 'a'*64, 'owner')
        with patch('app.pixy.authorization.time.time', return_value=auth['expires_at']+1):
            with self.assertRaises(PermissionError):
                self.store.decide(auth['authorization_id'], True, 'owner')
        self.assertEqual(self.store.get(auth['authorization_id'])['status'], 'expired')
        cycle = self.store.journal.get(Journal.cycle_id('authorization', auth['authorization_id']))
        self.assertEqual(cycle['status'], 'cancelled')
        with connection(self.db) as db:
            failures = db.execute("SELECT data FROM journal_events WHERE phase='finished' AND status='rejected'").fetchall()
        self.assertEqual(json.loads(failures[-1][0])['error_type'], 'PermissionError')
        self.assertNotIn(auth['nonce'], str(failures))

    def test_authority_and_journal_decision_roll_back_together(self):
        auth = self.store.issue('case-20261025abcd0021', ['HealthCheck'], 'core', 'b'*64, 'owner')
        original = self.store.journal.finish
        def fail_transaction(cycle, *args, **kwargs):
            if kwargs.get('conn') is not None:
                raise RuntimeError('injected journal failure')
            return original(cycle, *args, **kwargs)
        with patch.object(self.store.journal, 'finish', side_effect=fail_transaction):
            with self.assertRaises(RuntimeError):
                self.store.decide(auth['authorization_id'], False, 'owner')
        self.assertEqual(self.store.get(auth['authorization_id'])['status'], 'pending')
        self.assertEqual(self.store.journal.get(Journal.cycle_id('authorization', auth['authorization_id']))['status'], 'waiting')


class SensesEvidenceTests(unittest.TestCase):
    def test_evidence_collection_is_readonly_and_audited(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'senses.sqlite3'
            adapters = {'storage': lambda: {'vitals': {'ram_percent': 50, 'disk_free': 10 * 1024**3},
                                            'integrity': {'status': 'ok'}}}
            mock_gateway = Mock()
            mock_gateway.observe.side_effect = lambda rid, reason: json.dumps({'running': True, 'started_at': 100})
            senses = Senses(path, adapters, mock_gateway)

            samples = senses.sample(['storage', 'container_state_core'], 'diagnostic test')
            self.assertEqual(samples['storage']['vitals']['ram_percent'], 50)
            self.assertTrue(samples['container_state_core']['running'])
            self.assertNotIn('core_logs', samples)

            with connection(path) as conn:
                rows = conn.execute('SELECT * FROM observations WHERE observer="sample"').fetchall()
                self.assertEqual(len(rows), 1)


class HeartRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.events = Events(self.root / 'logs.sqlite3')
        self.auth_store = AuthorizationStore(self.root / 'auth.sqlite3')
        self.senses = Mock()
        self.hand = Mock()
        self.recovery = Recovery(self.root / 'heart.sqlite3', self.senses, self.hand, self.auth_store, self.events)

    def tearDown(self):
        self.tmp.cleanup()

    def test_evidence_survives_slow_diagnosis_exception_and_restart(self):
        self.senses.evidence.return_value = {'storage': {'marker': 'real evidence'}}
        def interrupted(evidence):
            persisted = self.recovery.recent(1)[0]
            self.assertEqual(persisted['state'], 'COLLECTING_EVIDENCE')
            self.assertEqual(persisted['evidence'], evidence)
            raise TimeoutError('provider private body')
        with patch.object(self.recovery, 'diagnose', side_effect=interrupted):
            case = self.recovery.open_case('repara memoria', 'owner:interrupted')
        restarted = Recovery(self.root/'heart.sqlite3', self.senses, self.hand, self.auth_store)
        self.assertEqual(restarted.get(case['case_id'])['evidence']['storage']['marker'], 'real evidence')
        branch = restarted.journal.tree(Journal.cycle_id('repair', case['case_id']))
        self.assertEqual(branch['cycles'][0]['status'], 'failed')
        self.assertEqual(branch['events'][-1]['data']['error_type'], 'TimeoutError')
        self.assertNotIn('provider private body', json.dumps(branch))

    def test_process_interrupted_diagnosis_is_reconciled_without_replay(self):
        self.senses.evidence.return_value = {'storage': {'marker': 'preserved'}}
        with patch.object(self.recovery, 'diagnose', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.recovery.open_case('repara memoria', 'owner:process')
        case = self.recovery.recent(1)[0]
        self.recovery.journal.interrupt_open()
        self.assertEqual(self.recovery.reconcile_interrupted(), 1)
        closed = self.recovery.get(case['case_id'])
        self.assertEqual(closed['state'], 'FAILED')
        self.assertEqual(closed['evidence']['storage']['marker'], 'preserved')
        self.assertEqual(self.recovery.reconcile_interrupted(), 0)
        self.hand.create_recovery.assert_not_called()

    def test_contract_rejection_uses_exact_evidence_without_network_repair(self):
        task = {'id': 'task_'+'1'*32, 'goal_revision': 0, 'attempt_number': 1,
                'result': {'stage': 'routing', 'diagnostic': {'id': 'rejection_target'}}}
        rejection = {'id': 'rejection_target', 'task_id': task['id'], 'goal_revision': 0,
                     'contract': 'AssistantTurn', 'errors': [{'loc': ['tool_calls'], 'type': 'missing'}]}
        self.senses.evidence.return_value = {'brain': {'model_rejections': [rejection]},
            'core_logs': {'text': 'ERROR old network timeout'},
            'storage': {'vitals': {'ram_percent': 99}, 'integrity': {'status': 'ok'}}}
        with patch.object(self.recovery, '_llm_analyze_incident') as analyze:
            case = self.recovery.open_incident_case(task, task['result'])
        self.assertEqual(case['state'], 'DIAGNOSED')
        self.assertIsNone(case['plan'])
        self.assertEqual(case['diagnosis']['findings'][0]['evidence_id'], rejection['id'])
        analyze.assert_not_called()
        self.assertEqual(self.recovery.journal.get(Journal.cycle_id('repair', case['case_id']))['status'], 'failed')

    def test_report_reference_selects_case_instead_of_other_recent_errors(self):
        self.senses.evidence.return_value = {'storage': {'vitals': {'ram_percent': 95}, 'integrity': {'status': 'ok'}}}
        original = self.recovery.open_case('repara memoria', 'owner:original')
        report = self.recovery.open_case('informa ' + original['case_id'], 'owner:report',
                                         propose=False, kind='pixy_report')
        self.assertEqual(report['diagnosis'], original['diagnosis'])
        self.assertIsNone(report['plan'])
        self.assertIsNone(report['authorization_id'])
        self.assertEqual(self.recovery.journal.get(Journal.cycle_id('pixy_report', report['case_id']))['status'], 'completed')

    def test_incident_journal_evidence_excludes_previous_attempt_and_other_tasks(self):
        from app.pixy.control import ControlStore
        db = self.root/'shared-scoped.sqlite3'
        store = ControlStore(db)
        task = store.create('owner:scoped', 'fixture', state='FAILED', attempt_number=2, goal_revision=1)
        old = store.journal.start('task_attempt', f"{task['id']}:1:0",
                                  parent_id=Journal.cycle_id('task', task['id']),
                                  refs={'task_id': task['id'], 'attempt_number': 1, 'goal_revision': 0})
        store.journal.finish(old, 'failed', data={'error_type': 'OldNetworkTimeout'})
        other = store.create('owner:other', 'other fixture', state='FAILED')
        recovery = Recovery(db, self.senses, self.hand, self.auth_store)
        self.senses.evidence.return_value = {'storage': {'vitals': {}, 'integrity': {'status': 'ok'}}}
        case = recovery.open_incident_case(task, {'error': 'ZeroDivisionError'})
        evidence = case['evidence']
        self.assertEqual(evidence['journal_scope']['kind'], 'current_attempt')
        self.assertEqual(evidence['journal_scope']['attempt_number'], 2)
        snapshot = json.dumps(evidence['journal'])
        self.assertNotIn('OldNetworkTimeout', snapshot)
        self.assertNotIn(other['id'], snapshot)

    def test_repair_branches_survive_restart_with_separate_immutable_histories(self):
        self.senses.evidence.return_value = {
            'storage': {'vitals': {'ram_percent': 95, 'disk_free': 5 * 1024**3},
                        'integrity': {'status': 'ok'}},
            'container_core': {'running': True},
            'container_proxy': {'running': True, 'scheduler_age': 5}}
        first = self.recovery.open_case('diagnostico memoria A', 'telegram:a')
        second = self.recovery.open_case('diagnostico memoria B', 'telegram:b')
        self.assertNotEqual(first['case_id'], second['case_id'])
        auth = self.auth_store.issue(first['case_id'], first['plan']['capabilities'],
            first['plan']['target'], first['plan_hash'], 'telegram:a')
        self.recovery.await_approval(first['case_id'], auth)
        denied = self.auth_store.decide(auth['authorization_id'], False, 'telegram:decision-a')
        self.recovery.receive_decision(first['case_id'], denied)

        reopened = Recovery(self.recovery.path, self.senses, self.hand, self.auth_store, self.events)
        self.assertEqual(reopened.get(first['case_id'])['state'], 'DENIED')
        self.assertEqual(reopened.get(second['case_id'])['state'], 'SOLUTION_PROPOSED')
        first_states = [r['to_state'] for r in reopened.transitions(first['case_id'])]
        second_states = [r['to_state'] for r in reopened.transitions(second['case_id'])]
        self.assertEqual(first_states, ['RECEIVED', 'COLLECTING_EVIDENCE', 'DIAGNOSED',
                                      'SOLUTION_PROPOSED', 'WAITING_USER_APPROVAL', 'DENIED'])
        self.assertEqual(second_states, first_states[:4])
        self.hand.create_recovery.assert_not_called()
        with connection(reopened.path) as db:
            for statement in ('DELETE FROM recovery_transitions WHERE case_id=?',
                              'UPDATE recovery_transitions SET to_state="FAILED" WHERE case_id=?'):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(statement, (first['case_id'],))
        with connection(self.events.path) as db:
            events = db.execute("SELECT correlation_id,metadata FROM events WHERE module='heart' "
                                "AND event_type='recovery_state'").fetchall()
        self.assertEqual({r['correlation_id'] for r in events}, {first['case_id'], second['case_id']})
        self.assertEqual([json.loads(r['metadata'])['to'] for r in events
                          if r['correlation_id'] == second['case_id']], second_states[1:])

    def test_task_verification_distinguishes_failure_and_missing_evidence(self):
        case = {'plan': {'target': 'task', 'task_id': 'task_x',
                         'task_binding': {'attempt_number': 1}},
                'execution': {}, 'created_at': 0}
        self.recovery.get = Mock(return_value=case)
        self.recovery._move = Mock(side_effect=lambda cid, state, **kw: (state, kw))
        self.recovery.coordinator = SimpleNamespace(store=Mock())
        task = {'state': 'COMPLETED', 'attempt_number': 2}
        self.recovery.coordinator.store.get.return_value = task
        self.assertEqual(self.recovery.verify('case')[0], 'UNKNOWN')
        task['verification'] = {'success': False}
        self.assertEqual(self.recovery.verify('case')[0], 'FAILED')
        task['verification'] = {'success': True}
        result = self.recovery.verify('case')
        self.assertEqual(result[0], 'RECOVERED')
        self.assertEqual(result[1]['verification']['verification_scope'], 'postcondition')

    def test_chat_verification_requires_matching_delivery_receipt(self):
        from app.mind.conversation import ConversationStore
        from app.voice.mailbox import VoiceMailbox
        conversation = ConversationStore(self.recovery.path)
        mailbox = VoiceMailbox(self.recovery.path)
        sid = conversation.session('owner', 'chat')
        task = {'state': 'COMPLETED', 'attempt_number': 2, 'routing_action': 'GENERAL_CHAT',
                'routing_intent': {'reply': 'respuesta'}, 'conversation_id': sid}
        self.recovery.coordinator = SimpleNamespace(store=Mock())
        self.recovery.coordinator.store.get.return_value = task
        self.recovery.get = Mock(return_value={'plan': {'target': 'task', 'task_id': 'task_x',
            'task_binding': {'attempt_number': 1}}, 'execution': {}, 'created_at': 0})
        self.recovery._move = Mock(side_effect=lambda cid, state, **kw: (state, kw))
        delivery = mailbox.enqueue('respuesta')
        self.assertEqual(self.recovery.verify('case')[0], 'UNKNOWN')
        mailbox.deliver_one(lambda text, meta: {'result': {'message_id': 42}})
        conversation.delivered(sid, delivery, 'respuesta', task_id='task_x', message_ids=[42])
        result = self.recovery.verify('case')
        self.assertEqual(result[0], 'RECOVERED')
        self.assertEqual(result[1]['verification']['verification_scope'], 'delivery_only')

    def test_diagnosis_and_plan_generation(self):
        evidence = {
            'storage': {'vitals': {'ram_percent': 95, 'disk_free': 5 * 1024**3}, 'integrity': {'status': 'ok'}},
            'container_core': {'running': True, 'started_at': time.time() - 1000},
            'container_proxy': {'running': True, 'status': 'running', 'scheduler_age': 5},
            'core_logs': {'text': 'INFO all good'}
        }
        diagnosis = self.recovery.diagnose(evidence)
        self.assertEqual(diagnosis['status'], 'degraded')
        self.assertEqual(diagnosis['findings'][0]['code'], 'memory_pressure')

        plan = self.recovery.design('case-2026102511223344', diagnosis)
        self.assertIsNotNone(plan)
        self.assertEqual(plan['target'], 'core')
        self.assertEqual(plan['capabilities'], ['DockerRestart', 'HealthCheck'])

    def test_no_plan_when_unrecoverable_corruption(self):
        evidence = {
            'storage': {'vitals': {'ram_percent': 50, 'disk_free': 5 * 1024**3},
                        'integrity': {'status': 'error', 'databases': {'db': 'corrupt'}}},
            'container_core': {'running': True},
            'container_proxy': {'running': True}
        }
        diagnosis = self.recovery.diagnose(evidence)
        self.assertEqual(diagnosis['status'], 'critical')
        plan = self.recovery.design('case-2026102511223344', diagnosis)
        self.assertIsNone(plan)

    def test_open_case_and_denial_flow(self):
        self.senses.evidence.return_value = {
            'storage': {'vitals': {'ram_percent': 95, 'disk_free': 5 * 1024**3}, 'integrity': {'status': 'ok'}},
            'container_core': {'running': True}, 'container_proxy': {'running': True, 'scheduler_age': 5}
        }
        case = self.recovery.open_case('diagnóstico de memoria', 'user:1')
        self.assertEqual(case['state'], 'SOLUTION_PROPOSED')

        auth = self.auth_store.issue(case['case_id'], case['plan']['capabilities'], case['plan']['target'],
                                      case['plan_hash'], 'user:1')
        waiting = self.recovery.await_approval(case['case_id'], auth)
        self.assertEqual(waiting['state'], 'WAITING_USER_APPROVAL')

        denied_auth = self.auth_store.decide(auth['authorization_id'], False, 'user:1')
        denied_case = self.recovery.receive_decision(case['case_id'], denied_auth)
        self.assertEqual(denied_case['state'], 'DENIED')
        self.hand.create_recovery.assert_not_called()

    def test_heart_refuses_hand_off_without_valid_pixy_authorization(self):
        self.senses.evidence.return_value = {
            'storage': {'vitals': {'ram_percent': 95, 'disk_free': 5 * 1024**3}, 'integrity': {'status': 'ok'}},
            'container_core': {'running': True}, 'container_proxy': {'running': True, 'scheduler_age': 5}
        }
        case = self.recovery.open_case('diagnóstico de memoria', 'user:1')
        self.recovery.advance(case['case_id'])
        self.assertEqual(self.recovery.get(case['case_id'])['state'], 'SOLUTION_PROPOSED')
        self.hand.create_recovery.assert_not_called()

        with connection(self.recovery.path) as conn:
            conn.execute('UPDATE recovery_cases SET state="AUTHORIZED",authorization_id=NULL WHERE case_id=?',
                         (case['case_id'],))
        adv = self.recovery.advance(case['case_id'])
        self.assertEqual(adv['state'], 'FAILED')
        self.assertIn('falta la autorización emitida por Pixy', adv['error'])
        self.hand.create_recovery.assert_not_called()


class RecipeBindingDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name)/'heart.sqlite3'
        self.senses, self.hand = Mock(), Mock()
        self.recovery = Recovery(self.db, self.senses, self.hand, Mock())
        self.senses.evidence.return_value = {
            'storage': {'vitals': {'ram_percent': 30}, 'integrity': {'status': 'ok'}},
            'core_logs': {'text': 'ERROR historical network timeout'},
            'container_core': {'running': True}, 'container_proxy': {'running': True}}

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def task():
        # Reproduce the production structure without claiming a catalog lookup.
        inputs = {'query': 'Brave Search API web search endpoint and authentication documentation'}
        return {'id': 'task_04735224fdd64cfab3dbadcce5b3c8bd', 'state': 'FAILED',
                'attempt_number': 1, 'goal_revision': 0, 'execution_id': None,
                'error': "'count'", 'result': None, 'recipe_step_index': 0,
                'recipe_receipts': [], 'recipe_input': dict(inputs),
                'recipe_outputs': {'input': dict(inputs)},
                'recipe': {'recipe_id': 'plan-compuesto', 'input_schema': {
                    'type': 'object', 'properties': {'query': {'type': 'string'},
                                                       'count': {'type': 'integer'}},
                    'required': ['query'], 'additionalProperties': False},
                    'steps': [{'id': 'paso_1', 'version_id': '79a56e394b684224b1d84b990ed5f998',
                               'bindings': {'query': 'input.query', 'count': 'input.count'}}],
                    'output': 'paso_1.provider'}}

    def test_typed_binding_failure_persists_exact_finding_without_invented_plan(self):
        task = self.task()
        failure = {'status': 'failed', 'stage': 'routing_recipe', 'dispatched': False,
                   'error_type': 'KeyError', 'error': 'KeyError'}
        task['result'] = failure
        with patch.object(self.recovery, '_llm_analyze_incident') as analyze:
            case = self.recovery.open_incident_case(task, failure)
        self.assertEqual(case['cause_code'], 'invalid_result')
        self.assertEqual(case['state'], 'DIAGNOSED')
        self.assertIsNone(case['plan'])
        finding = case['diagnosis']['findings'][0]
        self.assertEqual(finding['code'], 'recipe_binding_unavailable')
        self.assertIsNone(finding['remedy'])
        self.assertIn('contrato de resolución del Core', finding['detail'])
        observed = case['evidence']['recipe_binding_failure']
        self.assertEqual(observed['version_id'], task['recipe']['steps'][0]['version_id'])
        self.assertEqual(observed['catalog_status'], 'unobserved')
        self.assertEqual(observed['missing_optional_bindings'], [{'field': 'count', 'binding': 'input.count'}])
        analyze.assert_not_called()
        self.hand.create_recovery.assert_not_called()
        restarted = Recovery(self.db, self.senses, self.hand, Mock())
        self.assertEqual(restarted.get(case['case_id'])['diagnosis'], case['diagnosis'])

    def test_historical_quoted_key_is_only_possible_binding_failure(self):
        task = self.task()
        self.senses.evidence.return_value['pixy'] = {'recent_tasks': [task]}
        with patch.object(self.recovery, '_llm_analyze_incident') as analyze:
            case = self.recovery.open_case('repara ' + task['id'], 'owner:historical-binding')
        self.assertEqual(case['diagnosis']['status'], 'unknown')
        self.assertEqual(case['diagnosis']['findings'][0]['code'], 'possible_binding_unavailable')
        self.assertIsNone(case['plan'])
        self.assertIsNone(case['evidence']['recipe_binding_failure']['error_type'])
        self.assertIsNone(case['evidence']['recipe_binding_failure']['dispatched'])
        analyze.assert_not_called()

    def test_other_recipe_failures_keep_deep_diagnosis(self):
        scenarios = ('authorization', 'inactive_version', 'required_input', 'present_optional', 'dispatched')
        for revision, scenario in enumerate(scenarios):
            with self.subTest(scenario=scenario):
                task = self.task()
                task['goal_revision'] = revision
                failure = {'status': 'failed', 'stage': 'routing_recipe', 'dispatched': False,
                           'error_type': 'KeyError', 'error': 'KeyError'}
                if scenario == 'authorization':
                    failure.update(error_type='PermissionError', error='PermissionError')
                elif scenario == 'inactive_version':
                    failure.update(error_type='ValueError', error='Pinned recipe version is no longer active')
                elif scenario == 'required_input':
                    task['recipe']['input_schema']['required'].append('count')
                elif scenario == 'present_optional':
                    task['recipe_outputs']['input']['count'] = 3
                elif scenario == 'dispatched':
                    failure['dispatched'] = True
                    task['execution_id'] = 'run_'+'a'*32
                task['result'] = failure
                with patch.object(self.recovery, '_llm_analyze_incident', return_value={
                    'root_cause': 'Inspect the specific observed failure',
                    'corrective_instruction': 'Validate the actual failing contract', 'steps': []}) as analyze:
                    case = self.recovery.open_incident_case(task, failure)
                analyze.assert_called_once()
                self.assertNotIn('recipe_binding_failure', case['diagnosis'])


class HandRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.events = Events(self.root / 'logs.sqlite3')
        self.auth_store = AuthorizationStore(self.root / 'auth.sqlite3')
        self.pixy = SimpleNamespace(authorizations=self.auth_store, audit_artifact=Mock(return_value={'safe': True}))
        self.mock_executor = Mock()
        self.hand = Hand(self.root / 'hand', self.pixy, self.events, recovery_executor=self.mock_executor)

    def tearDown(self):
        self.tmp.cleanup()

    def test_hand_creates_and_executes_recovery_skill(self):
        case_id = 'case-20261025a1b2c3d4'
        plan = {'target': 'core', 'steps': [{'action': 'restart_container', 'target': 'core'},
                                            {'action': 'health_check', 'target': 'core'}],
                'capabilities': ['DockerRestart', 'HealthCheck']}
        h = plan_hash(case_id, plan)
        auth = self.auth_store.issue(case_id, plan['capabilities'], plan['target'], h, 'owner')
        self.auth_store.decide(auth['authorization_id'], True, 'owner')

        order = {
            'header': {'case_id': case_id, 'issued_by': 'heart', 'authorization_id': auth['authorization_id'],
                       'nonce': auth['nonce'], 'plan_hash': h},
            'plan': plan
        }

        created = self.hand.create_recovery(order)
        self.assertEqual(created['status'], 'created')
        skill_dir = self.root / 'hand' / 'heart' / case_id
        self.assertTrue(skill_dir.exists())
        self.assertTrue((skill_dir / 'app' / 'recovery.json').exists())
        self.assertTrue((skill_dir / 'SKILL.md').exists())

        self.mock_executor.submit.side_effect = [
            {'status': 'completed', 'requested_at': time.time(), 'action_id': 'rec_1'},
            {'status': 'completed', 'state': {'running': True}, 'action_id': 'rec_2'}
        ]
        result = self.hand.advance_recovery(order)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['steps']), 2)

        consumed_auth = self.auth_store.get(auth['authorization_id'])
        self.assertEqual(consumed_auth['status'], 'consumed')


class RecoveryEngineProxyTests(unittest.TestCase):
    def test_proxy_recovery_reconciliation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            mock_engine = Mock()
            mock_engine.connect = lambda: connection(root / 'proxy_runtime.sqlite3')
            mock_engine.inspect.return_value = {
                'State': {'Status': 'running', 'Running': True, 'StartedAt': '2026-10-25T01:00:00Z', 'ExitCode': 0}
            }
            rec_engine = RecoveryEngine(mock_engine, delay=0.01, spawn=lambda fn, *a: fn(*a))
            action_id = 'rec_' + 'e' * 32
            res = rec_engine.submit(action_id, 'health_check', 'core')
            self.assertEqual(res['status'], 'completed')
            self.assertTrue(res['state']['running'])


class Phase4TaxonomyAndDeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / 'recovery.sqlite3'
        self.senses = Mock()
        self.senses.evidence.return_value = {
            'storage': {'vitals': {'ram_percent': 20, 'disk_free': 10 * 1024**3}, 'integrity': {'status': 'ok'}},
            'container_core': {'running': True},
            'container_proxy': {'running': True, 'status': 'running'},
            'core_logs': {'text': 'All good'},
            'pixy': {'recent_tasks': []}
        }
        self.hand = Mock()
        self.authorizations = Mock()
        self.recovery = Recovery(self.db, self.senses, self.hand, self.authorizations)

    def tearDown(self):
        self.tmp.cleanup()

    def test_deterministic_cause_classification(self):
        c1, _ = Recovery.classify_cause({'error': 'Presupuesto agotado: generations'})
        self.assertEqual(c1, 'budget_exhausted')

        c2, _ = Recovery.classify_cause({'error': 'HTTP 401 Unauthorized: API key missing'})
        self.assertEqual(c2, 'missing_credential')

        c3, _ = Recovery.classify_cause({'status': 'unknown', 'stderr': 'Timeout sin confirmación'})
        self.assertEqual(c3, 'uncertain_effect')

        c4, _ = Recovery.classify_cause({'error': 'CapabilityRejected: service not registered'})
        self.assertEqual(c4, 'missing_capability')

        c5, _ = Recovery.classify_cause({'stderr': 'ModuleNotFoundError: No module named pandas'})
        self.assertEqual(c5, 'dependency_error')

        c6, _ = Recovery.classify_cause({'stderr': 'SyntaxError: invalid syntax', 'returncode': 1})
        self.assertEqual(c6, 'syntax_or_code')

        c7, _ = Recovery.classify_cause({'error': '503 Service Unavailable: connection reset'})
        self.assertEqual(c7, 'temporary_provider')

        c8, _ = Recovery.classify_cause({'verification': {'success': False}, 'error': 'Oracle mismatch'})
        self.assertEqual(c8, 'invalid_result')

    def test_status_query_does_not_retry_an_older_failed_task(self):
        self.senses.evidence.return_value['pixy']['recent_tasks'] = [
            {'id': 'task_old', 'request': 'hola', 'state': 'FAILED',
             'created_at': 1, 'updated_at': 100, 'error': 'LLM unavailable'},
            {'id': 'task_current', 'request': 'Hazlo', 'state': 'COMPLETED',
             'created_at': 2, 'updated_at': 2, 'executions': 0, 'trace_id': 'current'}]
        self.senses.evidence.return_value['core_logs']['text'] = '\n'.join(
            'ERROR [old] LLM unavailable' for _ in range(6))
        case = self.recovery.open_case('cual es el estado de la tarea?', 'owner:status')
        self.assertIsNone(case['plan'])
        self.assertEqual(case['diagnosis']['task']['id'], 'task_current')
        self.assertIn('No hay ejecución', case['diagnosis']['task']['note'])
        self.assertFalse(any(f['code'] in {'task_failure', 'core_error_burst'}
                             for f in case['diagnosis']['findings']))

    def test_explicit_last_error_query_selects_failed_task(self):
        failed_id = 'task_' + '1' * 32
        self.senses.evidence.return_value['pixy']['recent_tasks'] = [
            {'id': failed_id, 'request': 'hola', 'state': 'FAILED',
             'created_at': 1, 'error': 'LLM unavailable'},
            {'id': 'task_chat', 'request': 'Hola', 'state': 'COMPLETED', 'created_at': 2}]
        self.recovery._llm_analyze_incident = Mock(return_value=None)
        case = self.recovery.open_case('revisa el último error', 'owner:error')
        self.assertEqual(case['diagnosis']['task']['id'], failed_id)
        self.assertEqual(case['plan']['task_id'], failed_id)

    def test_model_incident_does_not_repair_unrelated_failed_task(self):
        self.senses.evidence.return_value['pixy']['recent_tasks'] = [
            {'id': 'task_' + '1' * 32, 'request': 'roleplay', 'state': 'FAILED',
             'created_at': 1, 'error': 'Invalid composition inputs'}]
        for question in ('revisa los errores de conexión anteriores',
                         'revisa el último error de cambio de modelo'):
            case = self.recovery.open_case(question, 'owner:error')
            self.assertIsNone(case['plan'])
            self.assertEqual(case['diagnosis']['status'], 'unknown')
            self.assertEqual(case['diagnosis']['findings'][0]['code'], 'model_transition_unobserved')
            self.assertNotIn('task', case['diagnosis'])

    def test_model_failure_uses_correlated_brain_events_without_guessing_disk_cause(self):
        self.senses.evidence.return_value['brain'] = {'recent_transitions': [
            {'attempt_id': 'attempt-1', 'profile': 'high', 'phase': 'activation_failed',
             'created_at': time.time(), 'error_type': 'ReadTimeout', 'disk': 'balanced', 'trace_id': 'trace-1'},
            {'attempt_id': 'attempt-1', 'profile': 'high', 'phase': 'warmup_started',
             'created_at': time.time()-90}]}
        case = self.recovery.open_case('revisa el fallo del modo high', 'owner:error')
        self.assertIsNone(case['plan'])
        finding = case['diagnosis']['findings'][0]
        self.assertEqual(finding['code'], 'model_activation_failed')
        self.assertIn('warmup_started', finding['detail'])
        self.assertIn('trace-1', finding['detail'])
        self.assertIn('no prueba la causa', finding['detail'])
        case = self.recovery.open_case('revisa el fallo del modo mid', 'owner:error')
        self.assertEqual(case['diagnosis']['status'], 'unknown')

    def test_incident_key_deduplication(self):
        task = {'id': 'task_00000000000000000000000000000001', 'execution_id': 'run_111'}
        failure = {'stderr': 'SyntaxError: unexpected EOF while parsing', 'returncode': 1}

        case1 = self.recovery.open_incident_case(task, failure)
        self.assertIsNotNone(case1)
        self.assertEqual(case1['state'], 'SOLUTION_PROPOSED')
        self.assertEqual(case1['task_id'], task['id'])
        self.assertEqual(case1['cause_code'], 'syntax_or_code')

        case2 = self.recovery.open_incident_case(task, failure)
        self.assertEqual(case1['case_id'], case2['case_id'])

        with connection(self.db) as conn:
            count = conn.execute('SELECT COUNT(*) FROM recovery_cases').fetchone()[0]
        self.assertEqual(count, 1)

    def test_different_failure_causes_create_distinct_incident_cases(self):
        task = {'id': 'task_00000000000000000000000000000002', 'execution_id': 'run_222'}
        f1 = {'stderr': 'SyntaxError: unexpected EOF', 'returncode': 1}
        f2 = {'stderr': 'ModuleNotFoundError: No module named requests', 'returncode': 1}

        case1 = self.recovery.open_incident_case(task, f1)
        case2 = self.recovery.open_incident_case(task, f2)

        self.assertNotEqual(case1['case_id'], case2['case_id'])
        self.assertEqual(case1['cause_code'], 'syntax_or_code')
        self.assertEqual(case2['cause_code'], 'dependency_error')

    def test_coordinator_failure_delegates_to_heart_incident(self):
        from types import SimpleNamespace
        from app.mind.coordinator import Coordinator
        from app.pixy.control import ControlStore

        c_db = self.root / 'coordinator.sqlite3'
        store = ControlStore(c_db)
        manager = SimpleNamespace(
            coordinator_control=store,
            data_dir=self.root,
            heart=SimpleNamespace(recovery=self.recovery),
            pixy=Mock(),
            hand=Mock(),
            mind=Mock(),
            memory=Mock(),
            events=Mock(),
        )
        proxy = Mock()
        coordinator = Coordinator(manager, proxy=proxy)
        manager.coordinator = coordinator

        task = store.create('test_source', 'consultar clima', state='RUNNING',
                            execution_id='run_fail_1',
                            artifact_hash='hash_123', mode='SAFE',
                            manifest={'purpose': 'clima', 'capabilities': []})
        tid = task['id']

        proxy.get.return_value = {
            'status': 'failed',
            'returncode': 1,
            'stderr': 'ZeroDivisionError: division by zero',
            'evidence_kind': 'process_exit'
        }

        coordinator.advance(task)
        t_diagnosing = store.get(tid)
        self.assertEqual(t_diagnosing['state'], 'DIAGNOSING')
        self.assertIsNotNone(t_diagnosing.get('recovery_case_id'))

        heart_case = self.recovery.get(t_diagnosing['recovery_case_id'])
        self.assertEqual(heart_case['cause_code'], 'syntax_or_code')
        self.assertEqual(heart_case['task_id'], tid)
        self.assertIn('ZeroDivisionError', heart_case['diagnosis']['findings'][0]['detail'])

        manager.pixy.request_recovery_approval.return_value = {'authorization_id': 'auth_diag_123', 'status': 'pending'}
        coordinator.advance(t_diagnosing)
        t_after = store.get(tid)
        self.assertEqual(t_after['state'], 'DIAGNOSING')
        self.assertEqual(t_after.get('recovery_authorization_id'), 'auth_diag_123')
        manager.mind.process_message.assert_not_called()

    def test_causal_cycle_transitions_and_verification(self):
        from types import SimpleNamespace
        from app.pixy.control import ControlStore

        c_db = self.root / 'coord_causal.sqlite3'
        store = ControlStore(c_db)
        task = store.create('test_source', 'consultar clima', state='FAILED',
                            execution_id='run_fail_2',
                            manifest={'purpose': 'clima', 'capabilities': []})
        tid = task['id']

        case = self.recovery.open_incident_case(task, {'stderr': 'ZeroDivisionError', 'returncode': 1})
        cid = case['case_id']

        with self.assertRaises(ValueError):
            self.recovery._move(cid, 'RECOVERED', expected={'AUTHORIZED'})

        coord_mock = SimpleNamespace(
            store=store,
            retry_task=Mock()
        )
        self.recovery.coordinator = coord_mock

        auth = {
            'authorization_id': 'auth_123',
            'nonce': 'nonce_abc',
            'case_id': cid,
            'target': 'task',
            'capabilities': ['TaskRemediate'],
            'plan_hash': case['plan_hash'],
            'status': 'approved',
            'decided_by': 'user'
        }
        self.authorizations.get.return_value = auth
        self.authorizations.verify.return_value = True

        self.recovery.receive_decision(cid, auth)
        case_auth = self.recovery.get(cid)
        self.assertEqual(case_auth['state'], 'AUTHORIZED')

        self.recovery.advance(cid)
        case_exec = self.recovery.get(cid)
        self.assertEqual(case_exec['state'], 'HAND_EXECUTING')
        coord_mock.retry_task.assert_called_once()
        self.assertEqual(coord_mock.retry_task.call_args[0][0], tid)
        self.assertEqual(coord_mock.retry_task.call_args[1]['authorization_id'], 'auth_123')
        self.assertEqual(coord_mock.retry_task.call_args[1]['order']['plan']['task_id'], tid)

        store.update(tid, expected={'FAILED'}, state='GENERATING', attempt_number=2,
                     goal_revision=1, execution_id=None)
        self.recovery.advance(cid)
        self.assertEqual(self.recovery.get(cid)['state'], 'HAND_EXECUTING')

        store.update(tid, expected={'GENERATING'}, state='FAILED', error='Still broken')
        self.recovery.advance(cid)
        self.assertEqual(self.recovery.get(cid)['state'], 'FAILED')


class FullRecoveryCycleIntegrationTests(unittest.TestCase):
    def test_invalid_model_json_reaches_heart_pixy_and_dreams_with_persisted_evidence(self):
        from app.bootstrap import ModuleManager
        from app.mind.coordinator import Coordinator
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'data';(root/'memory').mkdir(parents=True)
            (root/'memory'/'agent.md').write_text('Fixture identity',encoding='utf-8')
            with patch.dict(os.environ,{'KHAI_RUNTIME':'local','TELEGRAM_BOT_TOKEN':'token','TELEGRAM_USER_ID':'owner'}):
                manager=ModuleManager(data_dir=root);c=Coordinator(manager)
                manager.coordinator=c;manager.heart.recovery.coordinator=c;manager.telegram=Mock()
                manager.senses.adapters['storage']=lambda: {'vitals':{'ram_percent':30,'disk_free':20*1024**3},'integrity':{'status':'ok'}}
                manager.senses.legacy_gateway=Mock()
                manager.senses.legacy_gateway.observe.return_value=json.dumps({'status':'running','running':True,'text':'Fixture healthy'})
                # Exact lookup must work after evidence leaves the recent-events snapshot.
                manager.senses.adapters['brain'] = lambda: {'model_rejections': [
                    {'id': 'unrelated', 'task_id': 'task_other', 'response_excerpt': 'Unrelated old timeout'}]}
                bad='{"tool_calls":[{"name":"set_default_provider","arguments":{"provider":"brave"}}]}'
                manager.brain._gateway.generate_content=Mock(return_value=bad)
                task=c.store.create('fixture:rejection','Set the search provider',state='ROUTING',kind='turn')
                try:
                    c.advance(task);failed=c.store.get(task['id'])
                    self.assertEqual(failed['state'],'DIAGNOSING')
                    case=manager.heart.recovery.get(failed['recovery_case_id'])
                    self.assertEqual(case['cause_code'],'invalid_result')
                    evidence=case['evidence']['model_rejection']
                    self.assertEqual([r['id'] for r in case['evidence']['brain']['model_rejections']], [evidence['id']])
                    self.assertEqual(evidence['response_excerpt'],bad)
                    self.assertEqual(evidence['task_id'],task['id'])
                    self.assertEqual(evidence['contract'],'AssistantTurn')
                    self.assertTrue(any('capability' in e['loc'] for e in evidence['errors']))
                    with connection(root/'control.sqlite3') as db:
                        episode=db.execute('SELECT outcome,stage,run_id FROM episode_notes WHERE task_id=?',(task['id'],)).fetchone()
                    self.assertEqual(tuple(episode),('failure','routing',None))
                    c.advance(failed)
                    self.assertEqual(manager.heart.recovery.get(case['case_id'])['state'],'DIAGNOSED')
                    self.assertIsNone(manager.pixy.authorizations.pending())
                    self.assertIsNone(case['plan'])
                    original_id=case['case_id']
                    task2=dict(failed,attempt_number=2,goal_revision=1)
                    case2=manager.heart.recovery.open_incident_case(task2,failed['result'])
                    self.assertNotEqual(original_id,case2['case_id'])
                finally:
                    c._llm_executor.shutdown(wait=True);c._mailbox_executor.shutdown(wait=True)

    def test_pre_dispatch_dependency_failure_resumes_exact_approved_artifact(self):
        from app.bootstrap import ModuleManager
        from app.mind.coordinator import Coordinator
        from app.mind.recovery_attempt import build_attempt
        from app.heart.catalog import validate_plan
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'data'
            (root/'memory').mkdir(parents=True)
            (root/'memory'/'agent.md').write_text('Fixture identity', encoding='utf-8')
            with patch.dict(os.environ, {'KHAI_RUNTIME':'local','TELEGRAM_BOT_TOKEN':'token','TELEGRAM_USER_ID':'owner'}):
                manager = ModuleManager(data_dir=root)
                coordinator = Coordinator(manager)
                manager.coordinator = coordinator; manager.heart.recovery.coordinator = coordinator
                manager.telegram = Mock()
                manager.senses.evidence = Mock(return_value={
                    'storage': {'vitals': {'ram_percent':30,'disk_free':20*1024**3}, 'integrity':{'status':'ok'}},
                    'container_core':{'running':True},'container_proxy':{'running':True},'core_logs':{'text':'OK'}})
                failure = {'status':'failed','stage':'dependency_preparation','dispatched':False,'error':'ReadTimeout'}
                path, digest = manager.hand.snapshot('# MODE: SAFE\nprint(1)')
                task = coordinator.store.create('fixture','read fixture',state='FAILED',result=failure,
                    manifest={'purpose':'read fixture','capabilities':[]}, mode='SAFE',dependencies=['demo==1'],
                    artifact=path,artifact_hash=digest,authorized_hash=digest,approval={'artifact_hash':digest},
                    version_id='approved-version',params={'path':'fixture.csv'})
                try:
                    case = manager.heart.recovery.open_incident_case(task,failure)
                    self.assertEqual(case['plan']['steps'],[{'action':'resume_prepared_task','target':'task'}])
                    auth = manager.pixy.request_recovery_approval(case,'owner:resume')
                    decision = manager.pixy.authorizations.decide(auth['authorization_id'],True,'owner:resume')
                    manager.heart.recovery.receive_decision(case['case_id'],decision)
                    manager.heart.recovery.advance(case['case_id'])
                    resumed = coordinator.store.get(task['id'])
                    self.assertEqual(resumed['state'],'READY')
                    self.assertEqual(resumed['version_id'],'approved-version')
                    self.assertEqual(resumed['authorized_hash'],digest)
                    self.assertEqual(resumed['params'],{'path':'fixture.csv'})
                    self.assertEqual(resumed['attempt_number'],2)
                    root_cycle = manager.journal.cycle_id('task', task['id'])
                    root_tree = manager.journal.tree(root_cycle)
                    self.assertEqual(manager.journal.get(root_cycle)['status'], 'active')
                    attempts = [row for row in root_tree['cycles'] if row['kind'] == 'task_attempt']
                    self.assertEqual(len(attempts), 2)
                    self.assertEqual({row['status'] for row in attempts}, {'failed', 'active'})
                    self.assertEqual(resumed['generations'],0)
                    uncertain = dict(task,result=dict(failure,dispatched=True))
                    order={'plan':validate_plan(case['plan']),'header':{'authorization_id':'fixture','case_id':case['case_id'],'order_id':'fixture'}}
                    with self.assertRaises(PermissionError):
                        build_attempt(uncertain,order)
                finally:
                    coordinator._llm_executor.shutdown(wait=True);coordinator._mailbox_executor.shutdown(wait=True)

    def test_full_cycle_pixy_heart_senses_hand(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'data'
            (root / 'memory').mkdir(parents=True)
            (root / 'memory' / 'agent.md').write_text('Identity', encoding='utf-8')
            with patch.dict(os.environ, {'KHAI_DB_PATH': str(root / 'openclaw.db'), 'KHAI_RUNTIME': 'local',
                                         'TELEGRAM_BOT_TOKEN': 'token', 'TELEGRAM_USER_ID': 'owner'}):
                from app.bootstrap import ModuleManager
                manager = ModuleManager(data_dir=str(root))
                manager.llm.generate_content = Mock(return_value='Explico el diagnóstico. apruebas: y/n')
                manager.telegram = Mock()

                manager.senses.evidence = Mock(side_effect=[
                    {'storage': {'vitals': {'ram_percent': 50, 'disk_free': 10 * 1024**3}, 'integrity': {'status': 'ok'}},
                     'container_core': {'running': True, 'started_at': 100},
                     'container_proxy': {'running': True, 'status': 'running', 'scheduler_age': 120},
                     'core_logs': {'text': 'OK'}},
                    {'storage': {'vitals': {'ram_percent': 50, 'disk_free': 10 * 1024**3}, 'integrity': {'status': 'ok'}},
                     'container_core': {'running': True, 'started_at': 100},
                     'container_proxy': {'running': True, 'status': 'running', 'scheduler_age': 2, 'started_at': time.time() + 10},
                     'core_logs': {'text': 'OK'}}
                ])

                manager.hand.recovery_executor = Mock()
                manager.hand.recovery_executor.submit.return_value = {'status': 'completed', 'requested_at': time.time()}
                manager.hand.recovery_executor.status.return_value = {'status': 'completed'}

                pixy_reply = manager.pixy.direct_response('repara el sistema', source='telegram:101')
                self.assertIn('apruebas: y/n', pixy_reply)
                pending_auth = manager.pixy.authorizations.pending()
                self.assertIsNotNone(pending_auth)
                case_id = pending_auth['case_id']

                approval_reply = manager.pixy.direct_response('y', source='telegram:102')
                self.assertIn('Autorización', approval_reply)

                case = manager.heart.recovery.get(case_id)
                self.assertEqual(case['state'], 'AUTHORIZED')

                manager.heart.recovery.tick()
                case_final = manager.heart.recovery.get(case_id)
                self.assertEqual(case_final['state'], 'RECOVERED')
                self.assertIsNone(case_final['error'])

                self.assertTrue((root / 'hand' / 'heart' / case_id / 'app' / 'recovery.json').exists())
                self.assertTrue((root / 'hand' / 'heart' / case_id / 'SKILL.md').exists())

    def test_task_failure_remediation_cycle(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'data'
            (root / 'memory').mkdir(parents=True)
            (root / 'memory' / 'agent.md').write_text('Identity', encoding='utf-8')
            with patch.dict(os.environ, {'KHAI_DB_PATH': str(root / 'openclaw.db'), 'KHAI_RUNTIME': 'local',
                                         'TELEGRAM_BOT_TOKEN': 'token', 'TELEGRAM_USER_ID': 'owner'}):
                from app.bootstrap import ModuleManager
                from app.mind.coordinator import Coordinator
                manager = ModuleManager(data_dir=str(root))
                coordinator = Coordinator(manager)
                manager.coordinator = coordinator
                manager.heart.recovery.coordinator = coordinator
                manager.llm.generate_content = Mock(return_value='Diagnóstico: fallo en tarea. apruebas: y/n')
                manager.telegram = Mock()

                task = coordinator.store.create('test_source', 'consultar clima manana',
                                                state='EXHAUSTED', error='Presupuesto agotado: generations',
                                                generations=4, manifest={'purpose': 'clima', 'capabilities': ['Network']})
                tid = task['id']

                manager.senses.evidence = Mock(return_value={
                    'storage': {'vitals': {'ram_percent': 30, 'disk_free': 20 * 1024**3}, 'integrity': {'status': 'ok'}},
                    'container_core': {'running': True, 'started_at': 100},
                    'container_proxy': {'running': True, 'status': 'running'},
                    'core_logs': {'text': 'OK'},
                    'pixy': {'recent_tasks': [coordinator.store.get(tid)]}
                })

                pixy_reply = manager.pixy.direct_response('repara la tarea que fallo', source='telegram:201')
                self.assertIn('apruebas: y/n', pixy_reply)

                pending_auth = manager.pixy.authorizations.pending()
                self.assertIsNotNone(pending_auth)
                case_id = pending_auth['case_id']

                approval_reply = manager.pixy.direct_response('y', source='telegram:202')
                self.assertIn('Autorización', approval_reply)

                manager.heart.recovery.tick()
                case = manager.heart.recovery.get(case_id)
                self.assertEqual(case['state'], 'HAND_EXECUTING')

                retried_task = coordinator.store.get(tid)
                self.assertEqual(retried_task['state'], 'GENERATING')
                self.assertEqual(retried_task['generations'], 4)
                self.assertEqual(retried_task['max_generations'], 8)
                self.assertEqual(retried_task['attempt_number'], 2)
                self.assertEqual(retried_task['budget_extensions'][0]['authorization_id'], pending_auth['authorization_id'])
                self.assertIsNone(retried_task['error'])
                self.assertEqual(retried_task['recovery_history'][0]['error'], 'Presupuesto agotado: generations')

                coordinator.store.update(tid, expected={'GENERATING'}, state='COMPLETED',
                                         verification={'success': True, 'evidence': {'test': True}})

                manager.heart.recovery.tick()
                case = manager.heart.recovery.get(case_id)
                self.assertEqual(case['state'], 'RECOVERED')
                self.assertTrue(case['verification']['verified_success'])

    def test_skill_creation_approval_flow(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'data'
            (root / 'memory').mkdir(parents=True)
            (root / 'memory' / 'agent.md').write_text('Identity', encoding='utf-8')
            with patch.dict(os.environ, {'KHAI_DB_PATH': str(root / 'openclaw.db'), 'KHAI_RUNTIME': 'local',
                                         'TELEGRAM_BOT_TOKEN': 'token', 'TELEGRAM_USER_ID': 'owner'}):
                from app.bootstrap import ModuleManager
                from app.mind.coordinator import Coordinator
                manager = ModuleManager(data_dir=str(root))
                coordinator = Coordinator(manager)
                manager.coordinator = coordinator
                manager.telegram = Mock()

                manager.action_planner = None
                manager.skill_selector = None
                manager.mind.process_message = Mock(return_value={
                    'action': 'ACTION_REQUIRED',
                    'extracted_goal': 'Consultar pronóstico del clima mañana'
                })
                manager.llm.generate_content = Mock(return_value=json.dumps({
                    'purpose': 'Consultar el clima de mañana', 'capabilities': ['Network']
                }))
                spec = {
                    'service_id': 'weather', 'operation': 'forecast', 'version': 'v1',
                    'url': 'https://weather.example.org/forecast', 'method': 'GET',
                    'parameters': ['city'], 'content_types': ['application/json'],
                    'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
                    'effect': 'read', 'credential_ref': None, 'credential_header': 'Authorization',
                    'credential_prefix': '', 'effect_contract': None,
                }
                spec_hash = manager.hand.propose_service(spec)
                manager.hand.describe_service = Mock(return_value=(spec, spec_hash))

                task = coordinator.intake('tg:1', 'Khai que clima voy a tener?')
                tid = task['id']

                coordinator.tick()
                if coordinator._mailbox_job is not None:
                    coordinator._mailbox_job.result(timeout=5)
                current = coordinator.store.get(tid)
                self.assertEqual(current['state'], 'WAITING_CREATION_APPROVAL')

                notifications = [call.args[0] for call in manager.telegram.send_message.call_args_list]
                self.assertTrue(any('necesito preparar una capacidad nueva' in n and 'Responde y/n' in n for n in notifications))

                manager.pixy.direct_response('y', source='tg:2')
                current = coordinator.store.get(tid)
                self.assertEqual(current['state'], 'SCOPE_PLANNING')
                coordinator._llm_executor.shutdown(wait=True, cancel_futures=True)
                coordinator._mailbox_executor.shutdown(wait=True, cancel_futures=True)


if __name__ == '__main__':
    unittest.main()

