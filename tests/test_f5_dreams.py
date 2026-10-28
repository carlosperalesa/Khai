"""Unit tests for Phase 5: Dreams consolidated learning, distinct objectives, and rule lifecycle."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.dreams.dreams import Dreams
from app.mind.memory import MemoryEngine
from app.pixy.rules import Rules
from app.kernel.database import connection


class Phase5DreamsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / 'control.sqlite3'
        self.memory = MemoryEngine(str(self.root / 'memory'))
        self.rules = Rules(self.db)
        self.events = Mock()
        self.dreams = Dreams(self.db, self.rules, self.events, memory=self.memory)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_requires_valid_execution_and_boolean_outcome(self):
        with self.assertRaises(ValueError):
            self.dreams.record({'id': 'task_1'}, True, {})
        with self.assertRaises(ValueError):
            self.dreams.record({'execution_id': 'run_1'}, 'not_a_bool', {})

    def test_distinct_task_objectives_required_for_skill_consolidation(self):
        # 3 executions of the SAME task ID must NOT trigger skill proposal
        skill_name = 'weather_forecast'
        version_id = 'v1_0'
        artifact_hash = 'sha_' + '0' * 60

        same_task = {
            'id': 'task_unique_1',
            'execution_id': 'run_101',
            'skill_name': skill_name,
            'version_id': version_id,
            'artifact_hash': artifact_hash,
            'context': 'clima'
        }
        self.assertTrue(self.dreams.record(same_task, True, {'ok': True}))

        # Run 2 more times with the exact same task_id
        self.assertTrue(self.dreams.record(dict(same_task, execution_id='run_102'), True, {'ok': True}))
        self.assertTrue(self.dreams.record(dict(same_task, execution_id='run_103'), True, {'ok': True}))

        # Verify: samples = 3, but distinct_tasks = 1 -> No proposal generated
        metrics = self.dreams.metrics(skill_name, version_id)
        self.assertEqual(metrics['samples'], 3)
        self.assertEqual(metrics['distinct_tasks'], 1)
        self.assertEqual(metrics['success_rate'], 1.0)

        with connection(self.db) as conn:
            proposals = conn.execute('SELECT COUNT(*) FROM proposals').fetchone()[0]
        self.assertEqual(proposals, 0)

        # Now execute 2 DIFFERENT tasks
        task_2 = dict(same_task, id='task_unique_2', execution_id='run_104')
        task_3 = dict(same_task, id='task_unique_3', execution_id='run_105')
        self.assertTrue(self.dreams.record(task_2, True, {'ok': True}))
        self.assertTrue(self.dreams.record(task_3, True, {'ok': True}))

        # Now distinct_tasks = 3 -> Proposal generated in proposals and Rules
        metrics_after = self.dreams.metrics(skill_name, version_id)
        self.assertEqual(metrics_after['distinct_tasks'], 3)

        with connection(self.db) as conn:
            proposals = conn.execute('SELECT COUNT(*) FROM proposals').fetchone()[0]
        self.assertEqual(proposals, 1)

    def test_degraded_rule_gets_deprecated_after_three_failures(self):
        # Propose and approve a rule
        rid = self.rules.propose('Siempre usa requests con timeout', 'tester')
        self.rules.approve(rid, 'admin')
        self.assertEqual(len(self.rules.active()), 1)

        # 3 failures using this rule
        for i in range(3):
            task = {
                'id': f'task_fail_{i}',
                'execution_id': f'run_fail_{i}',
                'guidelines': [rid]
            }
            self.dreams.record(task, False, {'error': 'timed out'})

        # The rule must now be deprecated and excluded from active()
        self.assertEqual(len(self.rules.active()), 0)
        rule_record = self.rules.get(rid)
        self.assertEqual(rule_record['status'], 'deprecated')
        self.events.emit.assert_called_with(
            'dreams', 'rule_deprecated', correlation_id='task_fail_2', task_id='task_fail_2',
            metadata={'rule_id': rid, 'success_rate': 0.0, 'uses': 3}
        )

    def test_hypothesis_tracking_updates_memory_engine(self):
        hid = self.memory.add_hypothesis('parsing', 'use ast parser', 0.5)
        task = {
            'id': 'task_hyp_1',
            'execution_id': 'run_hyp_1',
            'hypothesis_id': hid,
            'context': 'coding'
        }
        self.dreams.record(task, True, {'test': True})

        hyp = self.memory.get_hypothesis(hid)
        self.assertEqual(hyp['activations'], 1)
        self.assertEqual(hyp['successes'], 1)
        self.assertGreater(hyp['confidence'], 0.5)


if __name__ == '__main__':
    unittest.main()
