"""Unit tests for Phase 4: Unique Doctor (Heart), deterministic taxonomy and incident deduplication."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.heart.catalog import CAUSE_TAXONOMY, cause_fingerprint, ACTIONS, TARGETS
from app.heart.recovery import Recovery
from app.kernel.database import connection


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
        # 1. Budget exhausted
        c1, _ = Recovery.classify_cause({'error': 'Presupuesto agotado: generations'})
        self.assertEqual(c1, 'budget_exhausted')

        # 2. Missing credential
        c2, _ = Recovery.classify_cause({'error': 'HTTP 401 Unauthorized: API key missing'})
        self.assertEqual(c2, 'missing_credential')

        # 3. Uncertain effect
        c3, _ = Recovery.classify_cause({'status': 'unknown', 'stderr': 'Timeout sin confirmación'})
        self.assertEqual(c3, 'uncertain_effect')

        # 4. Missing capability
        c4, _ = Recovery.classify_cause({'error': 'CapabilityRejected: service not registered'})
        self.assertEqual(c4, 'missing_capability')

        # 5. Dependency error
        c5, _ = Recovery.classify_cause({'stderr': 'ModuleNotFoundError: No module named pandas'})
        self.assertEqual(c5, 'dependency_error')

        # 6. Syntax or code error
        c6, _ = Recovery.classify_cause({'stderr': 'SyntaxError: invalid syntax', 'returncode': 1})
        self.assertEqual(c6, 'syntax_or_code')

        # 7. Temporary provider
        c7, _ = Recovery.classify_cause({'error': '503 Service Unavailable: connection reset'})
        self.assertEqual(c7, 'temporary_provider')

        # 8. Invalid result
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

        # First incident opening creates a new case in SOLUTION_PROPOSED
        case1 = self.recovery.open_incident_case(task, failure)
        self.assertIsNotNone(case1)
        self.assertEqual(case1['state'], 'SOLUTION_PROPOSED')
        self.assertEqual(case1['task_id'], task['id'])
        self.assertEqual(case1['cause_code'], 'syntax_or_code')

        # Second incident opening with exact same task, run and failure deduplicates to the same case
        case2 = self.recovery.open_incident_case(task, failure)
        self.assertEqual(case1['case_id'], case2['case_id'])

        # Total rows in recovery_cases must remain 1
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

        # Create a task in RUNNING state
        task = store.create('test_source', 'consultar clima', state='RUNNING',
                            execution_id='run_fail_1',
                            artifact_hash='hash_123', mode='SAFE',
                            manifest={'purpose': 'clima', 'capabilities': []})
        tid = task['id']

        # Simulate execution failure in proxy
        proxy.get.return_value = {
            'status': 'failed',
            'returncode': 1,
            'stderr': 'ZeroDivisionError: division by zero',
            'evidence_kind': 'process_exit'
        }

        # Coordinator advances RUNNING -> DIAGNOSING
        coordinator.advance(task)
        t_diagnosing = store.get(tid)
        self.assertEqual(t_diagnosing['state'], 'DIAGNOSING')
        self.assertIsNotNone(t_diagnosing.get('recovery_case_id'))

        # Verify Heart opened an incident case
        heart_case = self.recovery.get(t_diagnosing['recovery_case_id'])
        self.assertEqual(heart_case['cause_code'], 'syntax_or_code')
        self.assertEqual(heart_case['task_id'], tid)
        self.assertIn('ZeroDivisionError', heart_case['diagnosis']['findings'][0]['detail'])

        manager.pixy.request_recovery_approval.return_value = {'authorization_id': 'auth_diag_123', 'status': 'pending'}
        coordinator.advance(t_diagnosing)
        t_after = store.get(tid)
        self.assertEqual(t_after['state'], 'DIAGNOSING')
        self.assertEqual(t_after.get('recovery_authorization_id'), 'auth_diag_123')
        # Mind process_message was NEVER called (zero parallel diagnosis)
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

        # Open incident case
        case = self.recovery.open_incident_case(task, {'stderr': 'ZeroDivisionError', 'returncode': 1})
        cid = case['case_id']

        # Verify state machine rejects direct transition AUTHORIZED -> RECOVERED
        with self.assertRaises(ValueError):
            self.recovery._move(cid, 'RECOVERED', expected={'AUTHORIZED'})

        # Setup coordinator mock
        coord_mock = SimpleNamespace(
            store=store,
            retry_task=Mock()
        )
        self.recovery.coordinator = coord_mock

        # Setup authorization
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

        # Pixy hands over decision -> AUTHORIZED
        self.recovery.receive_decision(cid, auth)
        case_auth = self.recovery.get(cid)
        self.assertEqual(case_auth['state'], 'AUTHORIZED')

        # advance() advances AUTHORIZED -> HAND_EXECUTING and calls coord.retry_task
        self.recovery.advance(cid)
        case_exec = self.recovery.get(cid)
        self.assertEqual(case_exec['state'], 'HAND_EXECUTING')
        coord_mock.retry_task.assert_called_once()
        self.assertEqual(coord_mock.retry_task.call_args[0][0], tid)
        self.assertEqual(coord_mock.retry_task.call_args[1]['authorization_id'], 'auth_123')
        self.assertEqual(coord_mock.retry_task.call_args[1]['order']['plan']['task_id'], tid)

        # While task is in GENERATING, advance() remains in HAND_EXECUTING
        store.update(tid, expected={'FAILED'}, state='GENERATING')
        self.recovery.advance(cid)
        self.assertEqual(self.recovery.get(cid)['state'], 'HAND_EXECUTING')

        # When task fails, advance() transitions to FAILED via VERIFYING
        store.update(tid, expected={'GENERATING'}, state='FAILED', error='Still broken')
        self.recovery.advance(cid)
        self.assertEqual(self.recovery.get(cid)['state'], 'FAILED')


if __name__ == '__main__':
    unittest.main()


