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

        store.update(tid, expected={'FAILED'}, state='GENERATING')
        self.recovery.advance(cid)
        self.assertEqual(self.recovery.get(cid)['state'], 'HAND_EXECUTING')

        store.update(tid, expected={'GENERATING'}, state='FAILED', error='Still broken')
        self.recovery.advance(cid)
        self.assertEqual(self.recovery.get(cid)['state'], 'FAILED')


class FullRecoveryCycleIntegrationTests(unittest.TestCase):
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

                pixy_reply = manager.pixy.direct_response('diagnóstico del sistema', source='telegram:101')
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

                pixy_reply = manager.pixy.direct_response('por que fallo la tarea?', source='telegram:201')
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

