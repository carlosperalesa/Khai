"""Pixy organ tests: governance, permissions, secret capture, skill approval, and execution authority."""
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests

from tests.fixtures import Fixture
from app.kernel.control_plane import ControlPlane, PixyPort
from app.kernel.database import connection
from app.kernel.execution_authority import VoiceExecutionPort
from app.mind.conversation import ReplyResult
from app.pixy.skill_approvals import SkillApprovalStore
from app.voice.gateway.external_gateway import ExternalGateway
from app.voice.secrets import SecretCaptureService, Secrets
from app.voice.voice import command


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


class SecretCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "control.sqlite3"
        self.secrets = Secrets(self.root)
        self.plane = ControlPlane(self.db)
        self.capture = SecretCaptureService(self.secrets, self.plane.voice_secrets)

    def tearDown(self):
        self.tmp.cleanup()

    def test_capture_is_bound_to_private_identity_and_is_not_persisted_in_control_store(self):
        request = self.plane.ports.pixy.request_secret(
            kind="api_key", env_key="GEMINI_API_KEY", provider="gemini",
            purpose="Proveedor LLM", user_id="owner", chat_id="chat",
        )
        receipt = self.capture.capture("/api", "marker-value", "owner", "chat")
        self.assertTrue(receipt.startswith("secret_"))
        self.assertEqual(self.secrets.get("GEMINI_API_KEY"), "marker-value")
        with self.assertRaises(PermissionError):
            self.capture.capture("/api", "other-value", "owner", "chat")
        self.assertNotIn("marker-value", str(self.plane.voice_secrets.outbox()))
        self.assertNotIn("marker-value", request["request_id"])

    def test_ambiguous_or_wrong_identity_does_not_select_a_request(self):
        self.plane.ports.pixy.request_secret(
            kind="password", env_key="SERVICE_PASSWORD", provider="service",
            purpose="Servicio", user_id="owner", chat_id="chat",
        )
        self.plane.ports.pixy.request_secret(
            kind="password", env_key="OTHER_PASSWORD", provider="other",
            purpose="Otro servicio", user_id="owner", chat_id="chat",
        )
        with self.assertRaises(PermissionError):
            self.capture.capture("/pas", "value", "owner", "chat")
        with self.assertRaises(PermissionError):
            self.capture.capture("/pas", "value", "attacker", "chat")

    def test_rotation_uses_expected_file_version(self):
        first = self.plane.ports.pixy.request_secret(
            kind="api_key", env_key="OPENAI_API_KEY", provider="openai",
            purpose="Proveedor", user_id="owner", chat_id="chat",
        )
        self.capture.capture("/api", "first", "owner", "chat")
        second = self.plane.ports.pixy.request_secret(
            kind="api_key", env_key="OPENAI_API_KEY", provider="openai",
            purpose="Rotación", user_id="owner", chat_id="chat", expected_version=1,
        )
        self.capture.capture("/api", "second", "owner", "chat")
        self.assertEqual(self.secrets.get("OPENAI_API_KEY"), "second")
        self.assertEqual(self.plane.voice_secrets.outbox()[0]["request_id"], first["request_id"])
        self.assertEqual(len(self.plane.voice_secrets.outbox()), 2)

    def test_outbox_receipt_can_be_delivered_and_acknowledged_after_restart(self):
        self.plane.ports.pixy.request_secret(
            kind="username", env_key="SERVICE_USER", provider="service",
            purpose="Servicio", user_id="owner", chat_id="chat",
        )
        self.capture.capture("/us", "carlos", "owner", "chat")
        delivered = []
        self.capture.flush(delivered.append)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(self.plane.voice_secrets.outbox(), [])

    def test_restart_reconciles_write_before_receipt_without_resending_value(self):
        request = self.plane.ports.pixy.request_secret(
            kind="api_key", env_key="RECONCILE_API_KEY", provider="service",
            purpose="Servicio", user_id="owner", chat_id="chat",
        )
        self.plane.voice_secrets.capture(request["request_id"], "owner", "chat", "api_key")
        self.secrets.put("RECONCILE_API_KEY", "marker", 0)
        receipt = self.capture.capture("/api", "not-used", "owner", "chat")
        self.assertTrue(receipt.startswith("secret_"))
        self.assertEqual(len(self.plane.voice_secrets.outbox()), 1)


class ExecutionAuthorityTests(Fixture):
    def setUp(self):
        super().setUp()
        self.authority = VoiceExecutionPort(self.store.db_path)
        self.registry = Mock()
        self.registry.active_services.return_value = {'notes': {'operations': {
            'read': {'version': 'v1', 'effect': 'read'},
            'create': {'version': 'v1', 'effect': 'write', 'effect_contract': {
                'kind': 'create', 'identity': None, 'correction': None,
                'summary': {'name': 'Nota', 'context': 'Nota solicitada'}}}}}}
        self.gateway = ExternalGateway(self.root/'absent.yaml', Mock(),
            db=self.root/'voice.sqlite3', registry=self.registry, execution_authority=self.authority)
        self.gateway.request = Mock(return_value={'content_type': 'application/json', 'body': b'{"ok":true}'})

    def running(self, **changes):
        defaults = dict(state='RUNNING', mode='AGENT', runtime_status='started',
                        execution_id='run_'+'a'*32, goal_revision=2,
                        manifest={'capabilities': ['Network']},
                        services=[{'service_id': 'notes', 'operation': op, 'version': 'v1'}
                                  for op in ('read', 'create')])
        defaults.update(changes)
        return self.artifact(**defaults)

    def request(self, operation='read', **extra):
        return {'service': 'notes', 'operation': operation, 'params': {}, **extra}

    def write_request(self, task, name, **extra):
        workspace = self.c.root/'workspaces'/task['id']/task['artifact_hash']
        workspace.mkdir(parents=True, exist_ok=True)
        path = workspace/('.voice-'+name+'.request.json')
        path.write_text(json.dumps(self.request(execution_id=task['execution_id'],
            artifact_hash=task['artifact_hash'], goal_revision=task['goal_revision'], **extra)), encoding='utf-8')
        return path

    def facts(self, task):
        return self.gateway.technical_receipts(task['id'], task['execution_id'], task['services'])

    def test_cancel_between_mailbox_requests_preserves_first_and_denies_second(self):
        task = self.running()
        first = self.write_request(task, 'first')
        second = self.write_request(task, 'second')
        def send(*args, **kwargs):
            self.c.cancel(task['id'], 'cancel:in-flight')
            return {'content_type': 'application/json', 'body': b'{"ok":true}'}
        self.gateway.request.side_effect = send
        self.gateway.process_mailbox(task, first.parent)
        self.gateway.request.assert_called_once()
        self.assertEqual(sorted(item['status'] for item in self.facts(task)), ['completed', 'denied'])
        replies = [json.loads(p.with_name(p.name.replace('.request.', '.response.')).read_text())
                   for p in (first, second)]
        self.assertEqual(sum('result' in reply for reply in replies), 1)
        self.assertEqual(self.store.get(task['id'])['runtime_status'], 'cancelling')

    def test_live_authority_changes_reject_stale_snapshot(self):
        for changes in ({'state': 'CANCELLING'}, {'deadline': time.time()-1},
                        {'execution_id': 'run_'+'b'*32}, {'goal_revision': 3},
                        {'artifact_hash': 'f'*64}, {'authorized_hash': None},
                        {'services': []}, {'manifest': {'capabilities': []}},
                        {'runtime_status': 'finished'}):
            with self.subTest(changes=changes):
                task = self.running()
                self.store.update(task['id'], **changes)
                reply = self.gateway._mailbox_request(task, task['id'], self.request())
                self.assertEqual(reply, {'error': 'PermissionError'})
        self.gateway.request.assert_not_called()

    def test_missing_authority_fails_closed(self):
        self.gateway.execution_authority = None
        with self.assertRaises(PermissionError):
            self.gateway._mailbox_request(self.running(), 'first', self.request())
        self.gateway.request.assert_not_called()

    def test_old_worker_envelope_is_not_sent_in_new_execution(self):
        task = self.running()
        path = self.write_request(task, 'old')
        newer = self.store.update(task['id'], execution_id='run_'+'b'*32)
        self.gateway.process_mailbox(newer, path.parent)
        self.assertEqual(json.loads(path.with_name('.voice-old.response.json').read_text()),
                         {'error': 'PermissionError'})
        self.gateway.request.assert_not_called()

    def test_lost_write_response_is_uncertain_and_blocks_further_writes(self):
        task = self.running()
        self.gateway.request.side_effect = requests.ReadTimeout()
        self.gateway._mailbox_request(task, 'write1', self.request('create'))
        self.gateway.request.side_effect = None
        reply = self.gateway._mailbox_request(task, 'write2', self.request('create'))
        self.assertEqual(reply, {'error': 'UnconfirmedDispatch'})
        self.assertEqual([item['status'] for item in self.facts(task)], ['uncertain', 'uncertain'])
        self.assertEqual(self.gateway.request.call_count, 1)
        with connection(self.store.db_path) as conn:
            self.assertEqual(conn.execute('SELECT status FROM voice_dispatches').fetchone()[0], 'uncertain')
        self.gateway._mailbox_request(task, 'read3', self.request())
        self.assertEqual(self.gateway.request.call_count, 2)

    def test_committed_dispatch_without_voice_receipt_never_replays(self):
        task = self.running()
        request = self.request('create')
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        self.authority.authorize_dispatch(task, task['execution_id']+':lost', fingerprint,
                                         'notes', 'create', 'v1', 'write')
        reply = self.gateway._mailbox_request(task, 'lost', request)
        self.assertEqual(reply, {'error': 'UnconfirmedDispatch'})
        self.assertEqual(self.facts(task)[0]['status'], 'uncertain')
        self.gateway._mailbox_request(task, 'different-id', request)
        self.gateway.request.assert_not_called()

    def test_service_version_change_between_review_and_transport_is_denied(self):
        task = self.running()
        self.gateway.request = ExternalGateway.request.__get__(self.gateway)
        initial = self.registry.active_services.return_value
        self.registry.active_services.side_effect = [initial, {}]
        with patch('app.voice.gateway.external_gateway.socket.getaddrinfo') as dns:
            reply = self.gateway._mailbox_request(task, 'revoked', self.request())
        self.assertEqual(reply, {'error': 'DispatchRejected'})
        self.assertEqual(self.facts(task)[0]['status'], 'denied')
        dns.assert_not_called()

    def test_inflight_receipt_is_not_repeated_after_cancel(self):
        task = self.running()
        first = self.gateway._mailbox_request(task, 'once', self.request())
        self.c.cancel(task['id'], 'cancel:receipt')
        self.assertEqual(self.gateway._mailbox_request(task, 'once', self.request()), first)
        self.gateway.request.assert_called_once()

    def test_agent_authority_survives_pending_review_and_goal_acceptance(self):
        for state in ('UNVERIFIED', 'WAITING_FEEDBACK', 'VERIFYING', 'COMPLETED'):
            with self.subTest(state=state):
                task = self.running(state=state)
                self.assertIn('result', self.gateway._mailbox_request(task, task['id'], self.request()))
        self.assertEqual(self.gateway.request.call_count, 4)


class AgentSupervisionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.manager.gateway = Mock()
        self.manager.gateway.technical_receipts.return_value = []
        self.proxy.get.return_value = {'status': 'started', 'returncode': 0}
        self.proxy.cancel.return_value = {'status': 'cancelling'}

    def agent(self, **changes):
        defaults = dict(state='UNVERIFIED', mode='AGENT', execution_id='run_'+'a'*32,
                        result={'status': 'started'}, runtime_status='started')
        defaults.update(changes)
        return self.artifact(**defaults)

    def test_gateway_survives_owner_acceptance_and_stops_after_exit(self):
        task = self.agent(state='RUNNING')
        self.c.tick()
        self.assertEqual(self.store.get(task['id'])['state'], 'UNVERIFIED')
        self.c.verify(task['id'], task['execution_id'], True, 'owner:accept')
        self.c.tick()
        accepted = self.store.get(task['id'])
        self.assertEqual(accepted['state'], 'COMPLETED')
        self.assertTrue(accepted['verification']['success'])
        self.c.tick()
        self.assertEqual(self.manager.gateway.process_mailbox.call_count, 3)
        self.proxy.get.return_value = {'status': 'finished', 'returncode': 0}
        self.c.tick()
        self.c.tick()
        self.assertEqual(self.manager.gateway.process_mailbox.call_count, 3)
        self.assertEqual(self.store.get(task['id'])['runtime_status'], 'finished')
        self.assertEqual(self.store.get(task['id'])['state'], 'COMPLETED')

    def test_runtime_heartbeat_preserves_pending_question(self):
        task = self.agent()
        sid, qid = self.delivered_question(task, 'result')
        before = self.store.get(task['id'])['revision']
        self.c.tick()
        self.assertEqual(self.store.get(task['id'])['revision'], before)
        self.c.verify(task['id'], task['execution_id'], True, 'owner:after-poll')
        self.assertEqual(self.store.get(task['id'])['state'], 'VERIFYING')

    def test_accepted_service_cancellation_keeps_verification_and_waits_for_ack(self):
        verification = {'success': True, 'evidence': {'owner': 'owner'}}
        task = self.agent(state='COMPLETED', verification=verification)
        self.c.cancel(task['id'], 'owner:cancel')
        cancelling = self.store.get(task['id'])
        self.assertEqual(cancelling['runtime_status'], 'cancelling')
        self.assertEqual(cancelling['state'], 'COMPLETED')
        self.proxy.cancel.return_value = {'status': 'cancelled'}
        self.c.tick()
        stopped = self.store.get(task['id'])
        self.assertEqual(stopped['verification'], verification)
        self.assertEqual(stopped['runtime_status'], 'cancelled')
        self.manager.gateway.process_mailbox.assert_not_called()

    def test_cancel_pending_result_uses_runtime_stop_not_only_goal_transition(self):
        task = self.agent()
        reply = self.c.accept_review_reply('owner:cancel', 'cancelar')
        self.assertIn('solicitada', reply)
        self.assertEqual(self.store.get(task['id'])['state'], 'CANCELLING')
        self.proxy.cancel.assert_called_once_with(task['execution_id'])

    def test_deadline_stops_already_accepted_service(self):
        task = self.agent(state='COMPLETED', deadline=time.time()-1)
        self.c.tick()
        self.assertEqual(self.store.get(task['id'])['runtime_status'], 'cancelling')
        self.proxy.cancel.assert_called_once_with(task['execution_id'])
        self.manager.gateway.process_mailbox.assert_not_called()

    def test_transport_failure_does_not_dispatch_or_invent_stop(self):
        task = self.agent()
        self.proxy.get.side_effect = requests.Timeout()
        self.c.tick()
        self.assertEqual(self.store.get(task['id'])['state'], 'UNVERIFIED')
        self.assertEqual(self.store.get(task['id'])['runtime_status'], 'started')
        self.manager.gateway.process_mailbox.assert_not_called()

    def test_feedback_cannot_detach_a_live_agent(self):
        task = self.agent()
        self.assertIn('mismo objetivo', self.c._record_scope_refinement('owner:change', task, 'otro formato'))
        current = self.store.get(task['id'])
        self.assertEqual(current['execution_id'], task['execution_id'])
        self.assertEqual(current['state'], 'WAITING_FEEDBACK')
        self.assertEqual(current['runtime_status'], 'cancelling')
        self.proxy.cancel.return_value = {'status': 'cancelled'}
        self.c._poll_stop(current)
        revised = self.store.get(task['id'])
        self.assertEqual(revised['id'], task['id'])
        self.assertEqual(revised['state'], 'RESOLVING_SCOPE')
        self.assertIsNone(revised['execution_id'])
        self.assertEqual(revised['single_attempts'][0]['owner_feedback'], 'otro formato')

    def test_unconfirmed_stop_preserves_pending_feedback_and_execution(self):
        task = self.agent()
        self.proxy.cancel.return_value = {'status': 'unknown'}
        self.c._record_scope_refinement('owner:change', task, 'otro formato')
        current = self.store.get(task['id'])
        self.assertEqual(current['runtime_status'], 'unknown')
        self.assertIn('No pude confirmar', self.c._record_scope_refinement('owner:again', current, 'cambiar'))
        self.assertEqual(self.store.get(task['id'])['execution_id'], task['execution_id'])

    def test_stale_runtime_observation_cannot_revive_cancelled_service(self):
        task = self.agent()
        self.c.cancel(task['id'], 'owner:stop')
        self.store.observe_execution(task, {'status': 'started'})
        self.assertEqual(self.store.get(task['id'])['runtime_status'], 'cancelling')
        self.proxy.cancel.return_value = {'status': 'cancelled'}
        self.c.tick()
        self.store.observe_execution(task, {'status': 'started'})
        self.assertEqual(self.store.get(task['id'])['runtime_status'], 'cancelled')

    def test_new_unconfirmed_write_blocks_owner_acceptance(self):
        task = self.agent()
        self.manager.gateway.technical_receipts.return_value = [{'effect': 'write', 'status': 'uncertain'}]
        with self.assertRaises(PermissionError):
            self.c.verify(task['id'], task['execution_id'], True, 'owner:accept')
        self.assertEqual(self.store.get(task['id'])['state'], 'UNVERIFIED')

    def test_stop_without_id_includes_accepted_live_agents(self):
        task = self.agent(state='COMPLETED')
        reply = command(self.manager, 'owner:stop-all', '/stop')
        self.assertIn('Cancelación solicitada', reply)
        self.proxy.cancel.assert_called_once_with(task['execution_id'])


class PixyPortTests(Fixture):
    def setUp(self):
        super().setUp()
        self.plane = ControlPlane(self.state.db_path)
        self.manager.pixy.authorizations = self.plane.ports.pixy
        self.sid = self.c.conversations.session('owner', 'owner')

    def test_pixy_port_get_and_get_by_trace(self):
        """PixyPort must expose get and get_by_trace from underlying store."""
        pixy_port = self.plane.ports.pixy
        self.assertIsInstance(pixy_port, PixyPort)
        
        case_id = 'case-20261029' + 'a' * 8
        auth = pixy_port.issue(case_id, ['DockerRestart'], 'core', 'hash123', 'telegram:test')
        auth_id = auth['authorization_id']
        self.assertTrue(auth_id.startswith('auth_'))

        fetched = pixy_port.get(auth_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched['authorization_id'], auth_id)
        self.assertEqual(fetched['status'], 'pending')
        self.assertEqual(fetched['plan_hash'], 'hash123')

        by_trace = pixy_port.get_by_trace(auth.get('trace_id') or case_id)
        self.assertIsNotNone(by_trace)
        self.assertEqual(by_trace['authorization_id'], auth_id)

        self.assertIsNone(pixy_port.get('auth_nonexistent'))

    def test_answer_question_with_pixy_port(self):
        """Verify Coordinator.answer_question succeeds when manager.pixy.authorizations is PixyPort."""
        self.manager.pixy.direct_response = Mock(return_value='Operación aprobada por Pixy')

        case_id = 'case-20261029' + 'b' * 8
        auth = self.plane.ports.pixy.issue(case_id, ['DockerRestart'], 'core', 'hash_abc', 'telegram:123')
        qid = self.c.conversations.question(self.sid, 'authorization', auth['authorization_id'], 'hash_abc', 'pending')
        self.c.conversations.delivered(self.sid, 'deliv-test-01', '¿Apruebas?', question_id=qid)

        question = self.c.conversations.pending(self.sid)[0]
        res = self.c.answer_question('telegram:123', question, 'Y', self.sid)
        self.assertIsInstance(res, ReplyResult)
        self.assertTrue(res.handled)
        self.assertEqual(res.text, 'Operación aprobada por Pixy')


if __name__ == '__main__':
    unittest.main()

