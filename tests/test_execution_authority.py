"""Cancellation fences real SQLite admission; runtime facts preserve goal evidence."""
import hashlib
import json
import time
from unittest.mock import Mock, patch

import requests

from tests.test_control import Fixture
from app.kernel.database import connection
from app.kernel.execution_authority import VoiceExecutionPort
from app.voice.gateway.external_gateway import ExternalGateway
from app.voice.voice import command


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
