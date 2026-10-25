"""Unit and integration tests for the coordinated recovery cycle (Pixy, Heart, Senses, Hand)."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.common.database import connection, migrate
from app.common.events import Events
from app.common.recovery import validate_plan, plan_hash, requires_approval, ACTIONS, TARGETS
from app.pixy.authorization import AuthorizationStore
from app.pixy.pixy import PixyMind
from app.senses.senses import Senses, OBSERVERS
from app.heart.recovery import Recovery
from app.heart.beat import Beat
from app.hand.hand import Hand
from app.voice.runtime.recovery_runtime import ProxyRecoveryExecutor, RecoveryRejected
from app.voice.runtime.recovery_engine import RecoveryEngine


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

        # Cannot verify before user approval
        with self.assertRaises(PermissionError):
            self.store.verify(auth['authorization_id'], auth['nonce'], case_id, ['DockerRestart'], 'core', h)

        # Decide approve
        decided = self.store.decide(auth['authorization_id'], True, 'telegram:1')
        self.assertEqual(decided['status'], 'approved')

        # Now verify succeeds
        verified = self.store.verify(auth['authorization_id'], auth['nonce'], case_id, ['DockerRestart'], 'core', h)
        self.assertEqual(verified['status'], 'approved')

        # Consume once by Hand
        consumed = self.store.consume(auth['authorization_id'], auth['nonce'], case_id, ['DockerRestart'], 'core', h,
                                      consumer='hand:' + case_id)
        self.assertEqual(consumed['status'], 'consumed')

        # Same consumer retry succeeds (idempotent for crash-recovery)
        retry = self.store.consume(auth['authorization_id'], auth['nonce'], case_id, ['DockerRestart'], 'core', h,
                                   consumer='hand:' + case_id)
        self.assertEqual(retry['status'], 'consumed')

        # Different consumer fails
        with self.assertRaises(PermissionError):
            self.store.consume(auth['authorization_id'], auth['nonce'], case_id, ['DockerRestart'], 'core', h,
                               consumer='hand:other_case')

    def test_tampered_terms_rejected(self):
        case_id = 'case-20261025abcd1234'
        h = plan_hash(case_id, {'target': 'core', 'steps': [{'action': 'health_check', 'target': 'core'}],
                                'capabilities': ['HealthCheck']})
        auth = self.store.issue(case_id, ['HealthCheck'], 'core', h, 'user')
        self.store.decide(auth['authorization_id'], True, 'user')

        # Altered capabilities
        with self.assertRaises(PermissionError):
            self.store.verify(auth['authorization_id'], auth['nonce'], case_id, ['DockerRestart'], 'core', h)

        # Altered target
        with self.assertRaises(PermissionError):
            self.store.verify(auth['authorization_id'], auth['nonce'], case_id, ['HealthCheck'], 'proxy', h)

        # Altered hash
        with self.assertRaises(PermissionError):
            self.store.verify(auth['authorization_id'], auth['nonce'], case_id, ['HealthCheck'], 'core', '0' * 64)

    def test_expiration_and_supersede(self):
        case_id1 = 'case-20261025abcd0001'
        h1 = '1' * 64
        short_store = AuthorizationStore(self.db, ttl=0.01)
        auth1 = short_store.issue(case_id1, ['DockerRestart'], 'core', h1, 'user')
        time.sleep(0.02)
        with self.assertRaises(PermissionError):
            short_store.decide(auth1['authorization_id'], True, 'user')

        # Superseded pending
        auth2 = self.store.issue('case-20261025abcd0002', ['DockerRestart'], 'core', '2' * 64, 'user')
        auth3 = self.store.issue('case-20261025abcd0003', ['DockerRestart'], 'core', '3' * 64, 'user')
        self.assertEqual(self.store.get(auth2['authorization_id'])['status'], 'superseded')
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

            # Heart specifically requests only storage and container_state_core
            samples = senses.sample(['storage', 'container_state_core'], 'diagnostic test')
            self.assertEqual(samples['storage']['vitals']['ram_percent'], 50)
            self.assertTrue(samples['container_state_core']['running'])
            self.assertNotIn('core_logs', samples)  # Not requested by Heart, therefore not sampled

            # Check persisted audit in observations table
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

        # Issue authorization and await approval
        auth = self.auth_store.issue(case['case_id'], case['plan']['capabilities'], case['plan']['target'],
                                      case['plan_hash'], 'user:1')
        waiting = self.recovery.await_approval(case['case_id'], auth)
        self.assertEqual(waiting['state'], 'WAITING_USER_APPROVAL')

        # User denies
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
        # Attempting advance while not authorized does nothing
        self.recovery.advance(case['case_id'])
        self.assertEqual(self.recovery.get(case['case_id'])['state'], 'SOLUTION_PROPOSED')
        self.hand.create_recovery.assert_not_called()

        # If somehow moved to AUTHORIZED without valid Pixy authorization terms, advance fails closed
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

        # 1. Hand creates skill under data/hand/heart/<case_id>
        created = self.hand.create_recovery(order)
        self.assertEqual(created['status'], 'created')
        skill_dir = self.root / 'hand' / 'heart' / case_id
        self.assertTrue(skill_dir.exists())
        self.assertTrue((skill_dir / 'app' / 'recovery.json').exists())

        # 2. Hand executes steps
        self.mock_executor.submit.side_effect = [
            {'status': 'completed', 'requested_at': time.time(), 'action_id': 'rec_1'},
            {'status': 'completed', 'state': {'running': True}, 'action_id': 'rec_2'}
        ]
        result = self.hand.advance_recovery(order)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['steps']), 2)

        # Pixy authorization must now be consumed
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

                # Simulate degraded proxy scheduler
                manager.senses.evidence = Mock(side_effect=[
                    # Before evidence
                    {'storage': {'vitals': {'ram_percent': 50, 'disk_free': 10 * 1024**3}, 'integrity': {'status': 'ok'}},
                     'container_core': {'running': True, 'started_at': 100},
                     'container_proxy': {'running': True, 'status': 'running', 'scheduler_age': 120},
                     'core_logs': {'text': 'OK'}},
                    # After evidence
                    {'storage': {'vitals': {'ram_percent': 50, 'disk_free': 10 * 1024**3}, 'integrity': {'status': 'ok'}},
                     'container_core': {'running': True, 'started_at': 100},
                     'container_proxy': {'running': True, 'status': 'running', 'scheduler_age': 2, 'started_at': time.time() + 10},
                     'core_logs': {'text': 'OK'}}
                ])

                # Mock executor for Hand
                manager.hand.recovery_executor = Mock()
                manager.hand.recovery_executor.submit.return_value = {'status': 'completed', 'requested_at': time.time()}
                manager.hand.recovery_executor.status.return_value = {'status': 'completed'}

                # 1. User asks Pixy for diagnostic
                pixy_reply = manager.pixy.direct_response('diagnóstico del sistema', source='telegram:101')
                self.assertIn('apruebas: y/n', pixy_reply)
                pending_auth = manager.pixy.authorizations.pending()
                self.assertIsNotNone(pending_auth)
                case_id = pending_auth['case_id']

                # 2. User approves with 'y'
                approval_reply = manager.pixy.direct_response('y', source='telegram:102')
                self.assertIn('Autorización', approval_reply)

                # 3. Heart advances the case
                case = manager.heart.recovery.get(case_id)
                self.assertEqual(case['state'], 'AUTHORIZED')

                # Coordinator or Heart tick advances through HAND_CREATING, HAND_EXECUTING, VERIFYING, RECOVERED
                manager.heart.recovery.tick()
                case_final = manager.heart.recovery.get(case_id)
                self.assertEqual(case_final['state'], 'RECOVERED')
                self.assertIsNone(case_final['error'])

                # Verify skill created under data/hand/heart/<case_id>
                self.assertTrue((root / 'hand' / 'heart' / case_id / 'app' / 'recovery.json').exists())


if __name__ == '__main__':
    unittest.main()
