"""A reviewed active skill may reuse only the owner's approved scope."""
import unittest
from unittest.mock import Mock

from tests.test_control import Fixture
from app.pixy.skill_approvals import SkillApprovalStore


class ReusableSkillApprovalTests(Fixture):
    def test_approved_version_reuses_subset_after_restart_and_rejects_wider_scope(self):
        code = '# MODE: SAFE\nprint("result")'
        manifest = {'purpose': 'guardar nota', 'capabilities': ['Network']}
        version = self.manager.hand.create('notes', code, manifest)
        artifact, code_hash = self.manager.hand.snapshot(code)
        broad = [{'service_id': 'notes', 'operation': 'create', 'version': 'v1'},
                 {'service_id': 'notes', 'operation': 'update', 'version': 'v1'}]
        original = self.task(state='REVIEWING', code=code, artifact=artifact,
                             artifact_hash=code_hash, manifest=manifest,
                             skill_name='notes', version_id=version['version_id'],
                             selected_version_id=version['version_id'], mode='SAFE',
                             dependencies=[], services=broad)
        self.c._wait_approval(original, 'Código revisado')
        token = self.store.get(original['id'])['approval']['token']
        self.c.approval(original['id'], token, True, 'telegram:owner')
        self.manager.hand.pixy.audit_artifact.return_value = {
            'safe': True, 'requires_approval': True, 'audit_id': 'test-audit'}
        self.manager.hand.approve(version['version_id'], user_approved=True)
        self.manager.pixy.skill_approvals = SkillApprovalStore(self.manager.pixy.skill_approvals.path)
        self.manager.pixy.review = Mock(return_value={'safe': True, 'requires_approval': True})

        def task_with(services):
            return self.task(state='REVIEWING', code=code, artifact=artifact,
                             artifact_hash=code_hash, manifest=manifest,
                             skill_name='notes', version_id=version['version_id'],
                             selected_version_id=version['version_id'], mode='SAFE',
                             dependencies=[], services=services)

        narrow = task_with(broad[:1])
        self.c.advance(narrow)
        reused = self.store.get(narrow['id'])
        self.assertEqual(reused['state'], 'READY')
        self.assertEqual(reused['approval']['reused_from_task'], original['id'])
        wider = task_with(broad + [{'service_id': 'notes', 'operation': 'delete', 'version': 'v1'}])
        self.c.advance(wider)
        self.assertEqual(self.store.get(wider['id'])['state'], 'WAITING_APPROVAL')
        changed_version = dict(self.manager.hand.version(version['version_id']), artifact_hash='different')
        self.assertIsNone(self.manager.pixy.skill_approvals.matching(narrow, changed_version))
        self.manager.pixy.review.assert_called()

    def test_approval_rejects_scope_change_after_prompt(self):
        code = '# MODE: SAFE\nprint("result")'
        artifact, code_hash = self.manager.hand.snapshot(code)
        task = self.task(state='REVIEWING', code=code, artifact=artifact,
                         artifact_hash=code_hash, manifest={'purpose': 'notes', 'capabilities': ['Network']},
                         mode='SAFE', dependencies=[], services=[])
        self.c._wait_approval(task, 'Código revisado')
        pending = self.store.get(task['id'])
        self.store.update(task['id'], services=[{'service_id': 'notes', 'operation': 'create',
                                                 'version': 'v1'}])
        with self.assertRaisesRegex(ValueError, 'alcance cambi'):
            self.c.approval(task['id'], pending['approval']['token'], True, 'telegram:owner')


if __name__ == '__main__':
    unittest.main()
