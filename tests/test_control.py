import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.pixy.control import ControlStore, BudgetExceeded, InstanceLock
from app.pixy.budget import current_budget
from app.mind.memory import MemoryEngine
from app.mind.state import StateManager
from app.mind.coordinator import Coordinator
from app.voice.runtime.contract import digest, proxy_url, skill_metadata
from app.mind.verifier import verify_result
from app.hand.hand import Hand
from app.hand.base_skill import BaseSkill
from app.pixy.pixy import PixyMind
from app.pixy.rules import Rules
from app.dreams.dreams import Dreams
from app.voice.gateway.llm_gateway import LLMManager
from docker_proxy.engine import Engine
from app.voice.voice import app, authenticated_message, process_ordinary
import asyncio
import httpx


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'KHAI_DB_PATH': str(self.root/'openclaw.db')})
        self.env.start()
        self.memory = MemoryEngine(str(self.root/'memory'))
        self.state = StateManager(str(self.root/'memory'))
        self.store = ControlStore(self.state.db_path)
        self.llm = Mock()
        self.rules = Rules(self.state.db_path)
        self.events = Mock()
        self.dreams = Dreams(self.state.db_path, self.rules, self.events, memory=self.memory)
        self.manager = SimpleNamespace(state=self.state, memory=self.memory, telegram=Mock(), llm=self.llm,
            core_mind=Mock(), core_heart=Mock(), rules=self.rules, dreams=self.dreams, events=self.events)
        pixy_audit = Mock(audit_artifact=Mock(return_value={'safe': True, 'requires_approval': False}))
        self.manager.hand = Hand(self.root/'skills', pixy_audit, llm=self.llm, db_path=self.state.db_path)
        self.manager.pixy = PixyMind(self.manager)
        self.proxy = Mock()
        self.c = Coordinator(self.manager, self.proxy)
        self.manager.coordinator = self.c

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def task(self, **kwargs):
        return self.store.create('source:' + str(time.time_ns()), 'compute', **kwargs)

    def artifact(self, **kwargs):
        code = '# MODE: SAFE\nprint("result")'
        path, sha = self.manager.hand.snapshot(code)
        defaults = dict(state='READY', code=code, mode='SAFE', dependencies=[], artifact=path,
                        artifact_hash=sha, authorized_hash=sha, manifest={'purpose':'compute','capabilities':[]})
        defaults.update(kwargs)
        return self.task(**defaults)


class StoreTests(Fixture):
    def test_duplicate_message_is_one_objective(self):
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            tasks = list(pool.map(lambda _: self.store.create('same-update', 'do'), range(20)))
        self.assertEqual(len({t['id'] for t in tasks}), 1)
        self.assertEqual(len(self.store.all()), 1)

    def test_budget_survives_restart(self):
        task = self.task(max_llm_calls=1)
        self.store.consume(task['id'], 'llm_calls')
        with self.assertRaises(BudgetExceeded):
            ControlStore(self.state.db_path).consume(task['id'], 'llm_calls')

    def test_cancel_is_fenced(self):
        task = self.artifact()
        self.c.cancel(task['id'], 'cancel-1')
        with self.assertRaises(ValueError):
            self.store.update(task['id'], expected={'READY'}, state='SUBMITTED')
        self.c.advance(task)
        self.proxy.submit.assert_not_called()

    def test_second_owner_rejected(self):
        one, two = InstanceLock(self.root/'owner.lock'), InstanceLock(self.root/'owner.lock')
        try:
            one.acquire()
            with self.assertRaises(OSError):
                two.acquire()
        finally:
            one.release()
            two.release()

    def test_db_paths_unified(self):
        self.assertEqual(self.memory.db_path, self.state.db_path)
        self.assertEqual(StateManager(str(self.root)).db_path, self.state.db_path)


class CoordinatorTests(Fixture):
    def test_creation_consent_clarifies_scope_before_hand_generates(self):
        task = self.store.create('telegram:goal', 'quiero saber el clima',
            state='WAITING_CREATION_APPROVAL', original_request='quiero saber el clima',
            manifest={'purpose': 'quiero saber el clima', 'capabilities': ['Network']})
        self.manager.llm.generate_content.side_effect = [
            json.dumps({'questions': ['¿Para qué ubicación y qué datos del clima necesitas la respuesta?']}),
            json.dumps({'purpose': 'Clima en Santiago, Chile: temperatura en Celsius y lluvia',
                        'required_output': ['temperatura en Celsius', 'posibilidad de lluvia'],
                        'params': {'city': 'Santiago', 'country': 'Chile'},
                        'missing_questions': []})]
        self.manager.pixy.direct_response('y', source='telegram:consent')
        self.assertEqual(self.store.get(task['id'])['state'], 'SCOPE_PLANNING')
        self.c.advance(self.store.get(task['id']))
        self.assertEqual(self.store.get(task['id'])['state'], 'WAITING_SCOPE')
        self.assertEqual(self.store.get(task['id'])['generations'], 0)
        self.c.accept_scope_reply('telegram:answer', 'Santiago, Chile, Celsius y si va a llover')
        self.c.advance(self.store.get(task['id']))
        current = self.store.get(task['id'])
        self.assertEqual(current['state'], 'GENERATING')
        self.assertEqual(current['goal_revision'], 1)
        self.assertEqual(current['params']['city'], 'Santiago')
        self.assertIn('lluvia', current['scope_revisions'][0]['required_output'][1])

    def test_incomplete_owner_result_revises_same_goal_without_heart(self):
        task = self.store.create('telegram:weather', 'clima en Santiago',
            state='UNVERIFIED', original_request='quiero saber el clima',
            manifest={'purpose': 'clima en Santiago', 'capabilities': ['Network']},
            execution_id='run_' + 'a' * 32, goal_revision=1,
            scope_revisions=[{'number': 1, 'purpose': 'temperatura en Santiago',
                              'required_output': ['temperatura'], 'params': {'city': 'Santiago'}}])
        self.manager.llm.generate_content.return_value = json.dumps({
            'purpose': 'temperatura y lluvia en Santiago',
            'required_output': ['temperatura', 'posibilidad de lluvia'],
            'params': {'city': 'Santiago'}, 'missing_questions': []})
        self.c.accept_review_reply('telegram:feedback', 'no, falta saber si va a llover')
        self.assertEqual(self.store.get(task['id'])['state'], 'RESOLVING_SCOPE')
        self.c.advance(self.store.get(task['id']))
        current = self.store.get(task['id'])
        self.assertEqual(current['id'], task['id'])
        self.assertEqual(current['goal_revision'], 2)
        self.assertEqual(current['state'], 'GENERATING')
        self.assertEqual(current['owner_reviews'][0]['satisfied'], False)
        with self.store.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM control_feedback').fetchone()[0], 0)

    def test_new_candidate_waits_for_owner_after_machine_check(self):
        task = self.artifact(state='RUNNING', execution_id='run_' + 'b' * 32,
                             version_id='v_candidate', criterion={
                                 'kind': 'file_sha256', 'path': 'answer.txt',
                                 'sha256': digest(b'expected')})
        workspace = self.c.root/'workspaces'/task['id']/task['artifact_hash']
        workspace.mkdir(parents=True)
        (workspace/'answer.txt').write_bytes(b'expected')
        self.proxy.get.return_value = {'status': 'finished', 'returncode': 0, 'stdout': 'expected'}
        with patch.object(self.manager.hand, 'version', return_value={'status': 'draft'}):
            self.c.advance(task)
        current = self.store.get(task['id'])
        self.assertEqual(current['state'], 'UNVERIFIED')
        self.assertTrue(current['machine_verification']['success'])

    def test_owner_no_requests_detail_before_revising_scope(self):
        task = self.store.create('telegram:result', 'respuesta parcial',
            state='UNVERIFIED', execution_id='run_' + 'c' * 32,
            manifest={'purpose': 'respuesta parcial', 'capabilities': []})
        reply = self.c.accept_review_reply('telegram:no', 'n')
        self.assertIn('¿Qué falta', reply)
        self.assertEqual(self.store.get(task['id'])['state'], 'WAITING_FEEDBACK')
        self.c.accept_review_reply('telegram:detail', 'Quiero también la probabilidad')
        self.assertEqual(self.store.get(task['id'])['state'], 'RESOLVING_SCOPE')

    def test_direct_correction_has_same_negative_outcome_as_no(self):
        task = self.store.create('telegram:direct-feedback', 'respuesta parcial',
            state='UNVERIFIED', execution_id='run_' + 'd' * 32,
            manifest={'purpose': 'respuesta parcial', 'capabilities': []})
        process_ordinary(self.manager, 'telegram:correction', 'trace-correction',
                         'El resumen está incorrecto, corrige la fuente')
        current = self.store.get(task['id'])
        self.assertEqual(current['state'], 'RESOLVING_SCOPE')
        self.assertFalse(current['owner_reviews'][0]['satisfied'])
        self.assertIn('corrige la fuente', current['owner_reviews'][0]['feedback'])

    def test_multiple_pending_questions_require_id_for_natural_reply(self):
        scope = self.store.create('telegram:scope', 'consulta', state='WAITING_SCOPE')
        review = self.store.create('telegram:review', 'resultado', state='UNVERIFIED',
                                   execution_id='run_' + 'e' * 32)
        with patch.object(self.c, 'notify') as notify:
            process_ordinary(self.manager, 'telegram:ambiguous', 'trace-ambiguous',
                             'El resumen está incorrecto')
        self.assertEqual(self.store.get(scope['id'])['state'], 'WAITING_SCOPE')
        self.assertEqual(self.store.get(review['id'])['state'], 'UNVERIFIED')
        self.assertIn('Incluye el ID', notify.call_args.args[0])
        process_ordinary(self.manager, 'telegram:review-specific', 'trace-specific',
                         review['id'] + ' El resumen está incorrecto')
        self.assertEqual(self.store.get(review['id'])['state'], 'RESOLVING_SCOPE')
        self.assertEqual(self.store.get(scope['id'])['state'], 'WAITING_SCOPE')

    def test_zero_exit_error_text_is_not_success(self):
        task = self.artifact(state='RUNNING', execution_id='run_'+'a'*32)
        self.proxy.get.return_value = {'status':'finished', 'returncode':0, 'stdout':'ERROR: missing output'}
        self.c.tick()
        self.assertEqual(self.store.get(task['id'])['state'], 'UNVERIFIED')
        with self.store.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM control_feedback').fetchone()[0], 0)

    def test_started_daemon_is_not_verified(self):
        task = self.artifact(state='RUNNING', mode='AGENT', execution_id='run_'+'a'*32)
        self.proxy.get.return_value = {'status':'started', 'returncode':0}
        self.c.tick()
        self.assertEqual(self.store.get(task['id'])['state'], 'UNVERIFIED')

    def test_lost_submit_reply_reuses_execution_id(self):
        import requests
        task = self.artifact()
        self.c.tick()
        run = self.store.get(task['id'])['execution_id']
        self.proxy.submit.side_effect = requests.Timeout()
        self.c.tick()
        restarted = Coordinator(self.manager, self.proxy)
        restarted.tick()
        self.assertEqual(self.store.get(task['id'])['execution_id'], run)
        self.assertEqual(self.proxy.submit.call_count, 2)
        self.assertEqual(self.store.get(task['id'])['executions'], 1)

    def test_approval_bound_to_content(self):
        task = self.artifact(state='REVIEWING')
        self.c._wait_approval(task, 'review')
        pending = self.store.get(task['id'])
        Path(task['artifact']).write_text('changed')
        with self.assertRaises(ValueError):
            self.c.approval(task['id'], pending['approval']['token'], True, 'approve')

    def test_approval_wrong_token_and_denial(self):
        task = self.artifact(state='REVIEWING')
        self.c._wait_approval(task, 'review')
        with self.assertRaises(ValueError):
            self.c.approval(task['id'], 'no aprobar', True, 'bad')
        token = self.store.get(task['id'])['approval']['token']
        self.c.approval(task['id'], token, False, 'deny')
        self.c.tick()
        self.assertEqual(self.store.get(task['id'])['state'], 'CANCELLED')
        self.proxy.submit.assert_not_called()

    def test_approval_receipt_is_not_generic_failure(self):
        task = self.artifact(state='RUNNING', execution_id='run_'+'a'*32)
        operation = {'action':'publish','details':'item 1'}
        self.proxy.get.return_value = {'status':'approval_required','approval':operation,'returncode':75}
        self.c.tick()
        pending = self.store.get(task['id'])
        self.assertEqual(pending['state'], 'WAITING_APPROVAL')
        self.c.approval(task['id'], pending['approval']['token'], True, 'yes')
        self.assertEqual(self.store.get(task['id'])['grants'], [operation])

    def test_exhausted_hypotheses_stop_same_objective(self):
        task = self.artifact(state='DIAGNOSING', diagnosed=True, alternatives=[])
        self.c.tick()
        self.assertEqual(self.store.get(task['id'])['state'], 'FAILED')
        self.assertEqual(len(self.store.all()), 1)

    def test_rejection_generation_budget(self):
        task = self.artifact(state='REVIEWING', generations=4)
        self.manager.pixy.review = Mock(return_value={'safe':False,'reason':'invalid'})
        self.c.tick()
        self.c.tick()
        self.assertEqual(self.store.get(task['id'])['state'], 'EXHAUSTED')
        self.llm.generate_content.assert_not_called()

    def test_cancel_waits_for_proxy_confirmation(self):
        task = self.artifact(state='RUNNING', execution_id='run_'+'a'*32)
        self.proxy.cancel.return_value = {'status':'cancelling'}
        self.c.cancel(task['id'], 'cancel')
        self.c.tick()
        self.assertEqual(self.store.get(task['id'])['state'], 'CANCELLING')
        self.proxy.cancel.return_value = {'status':'cancelled'}
        self.c.tick()
        self.assertEqual(self.store.get(task['id'])['state'], 'CANCELLED')

    def test_unknown_result_never_replays(self):
        task = self.artifact(state='RUNNING', execution_id='run_'+'a'*32)
        self.proxy.get.return_value = {'status':'unknown'}
        self.c.tick(); self.c.tick()
        self.assertEqual(self.store.get(task['id'])['state'], 'UNKNOWN')
        self.proxy.submit.assert_not_called()

    def test_full_offline_lifecycle_preserves_objective(self):
        request = '/make compute [MANIFEST] ' + json.dumps({'purpose':'compute','capabilities':[]})
        task = self.c.intake('telegram:lifecycle', request)
        self.llm.generate_content.return_value = json.dumps({'questions': []})
        self.manager.hand.generate = Mock(return_value=('# MODE: SAFE\nprint(42)', 'SAFE', []))
        self.manager.pixy.review = Mock(return_value={'safe':True,'requires_approval':False,'reason':'offline'})
        self.proxy.submit.return_value = {'status':'queued'}
        self.proxy.get.return_value = {'status':'finished','returncode':0,'stdout':'42'}
        for _ in range(7):
            self.c.tick()
        current = self.store.get(task['id'])
        self.assertEqual(current['state'], 'UNVERIFIED')
        self.c.verify(task['id'], current['execution_id'], True, 'owner:lifecycle')
        self.c.tick()
        self.assertEqual(self.store.get(task['id'])['state'], 'COMPLETED')
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(self.proxy.submit.call_count, 1)

    def test_external_failure_is_not_replayed(self):
        task = self.artifact(state='RUNNING', execution_id='run_'+'a'*32, payload={'network':True})
        self.proxy.get.return_value = {'status':'failed','returncode':1,'evidence_kind':'process_exit'}
        self.c.tick(); self.c.tick()
        self.assertEqual(self.store.get(task['id'])['state'], 'UNKNOWN')
        self.manager.core_mind.process_message.assert_not_called()

    def test_protocol_mismatch_is_not_treated_as_success(self):
        task = self.artifact(state='RUNNING', execution_id='run_'+'a'*32)
        self.proxy.get.return_value = {'status':'surprise'}
        self.c.tick()
        self.assertEqual(self.store.get(task['id'])['state'], 'UNKNOWN')

    def test_cancel_verified_service_still_requests_stop(self):
        task = self.artifact(state='COMPLETED', mode='AGENT', execution_id='run_'+'a'*32,
                             result={'status':'started'})
        self.proxy.cancel.return_value = {'status':'cancelling'}
        self.c.cancel(task['id'], 'cancel:verified-service')
        self.assertEqual(self.store.get(task['id'])['state'], 'CANCELLING')


    def test_dependencies_require_approval_before_build(self):
        task = self.artifact(state='REVIEWING', dependencies=['requests==2.31.0'])
        self.manager.pixy.review = Mock(return_value={'safe':True,'requires_approval':False,'reason':'reviewed'})
        self.c.tick()
        self.assertEqual(self.store.get(task['id'])['state'], 'WAITING_APPROVAL')
        self.proxy.submit.assert_not_called()


class LearningTests(Fixture):
    def test_only_explicit_verification_records_and_deduplicates(self):
        hid = self.memory.add_hypothesis('compute','strategy')
        task = self.artifact(state='UNVERIFIED', execution_id='run_'+'a'*32, hypothesis_id=hid)
        self.c.verify(task['id'], task['execution_id'], True, 'owner-verified')
        self.c.tick()
        self.c.learning.record(self.store.get(task['id']), True, {'duplicate':True})
        self.assertEqual(self.memory.get_hypothesis(hid)['successes'], 1)
        self.assertEqual(self.store.get(task['id'])['state'], 'COMPLETED')

    def test_candidate_requires_three_different_objectives(self):
        hid = self.memory.add_hypothesis('compute','strategy')
        one = self.task(hypothesis_id=hid)
        for i in range(3):
            self.c.learning.record(dict(one, execution_id='repeat_'+str(i)), True, {'check':'test'})
        with self.store.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM rule_proposals').fetchone()[0], 0)
        for i in range(2):
            task = self.task(hypothesis_id=hid, execution_id='other_'+str(i))
            self.c.learning.record(task, True, {'check':'test'})
        with self.store.connection() as conn:
            rule = conn.execute("SELECT * FROM dynamic_guidelines WHERE status='candidate'").fetchone()
        self.assertIsNotNone(rule)
        self.c.learning.activate(rule['id'])
        self.c.learning.rollback(rule['id'])
        self.assertEqual(self.memory.get_dynamic_guidelines(), [])

    def test_repeated_verified_failures_retire_rule(self):
        rule = self.memory.add_guideline('compute','general','do this')
        for i in range(3):
            task = self.task(execution_id='failure_'+str(i), guidelines=[rule])
            self.c.learning.record(task, False, {'check':'failed'})
        self.assertEqual(self.memory.get_dynamic_guidelines(), [])

    def test_rule_commands_are_idempotent(self):
        with self.store.connection() as conn:
            rid = conn.execute("INSERT INTO dynamic_guidelines(topic,scope,rule_text,status) VALUES ('x','general','rule','candidate')").lastrowid
        self.c.learning.activate(rid, source='approve-rule')
        self.c.learning.activate(rid, source='approve-rule')
        self.c.learning.rollback(rid, source='rollback-rule')
        self.c.learning.rollback(rid, source='rollback-rule')
        self.assertEqual(self.memory.get_dynamic_guidelines(), [])

    def test_legacy_guidelines_migration_preserves_records(self):
        import sqlite3
        path = self.root/'legacy.db'
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE dynamic_guidelines(id INTEGER PRIMARY KEY,rule TEXT)')
            conn.execute("INSERT INTO dynamic_guidelines(rule) VALUES ('historic instruction')")
        conn.close()
        with patch.dict(os.environ, {'KHAI_DB_PATH':str(path)}):
            memory = MemoryEngine(str(self.root/'legacy-memory'))
            with memory._get_conn() as conn:
                row = conn.execute('SELECT rule FROM legacy_dynamic_guidelines').fetchone()
                self.assertEqual(row[0],'historic instruction')
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM dynamic_guidelines').fetchone()[0],0)


class ContractTests(Fixture):
    def test_owner_criterion_detects_bad_artifact(self):
        (self.root/'output').write_bytes(b'wrong')
        result = {'status':'finished','returncode':0}
        criterion = {'kind':'file_sha256','path':'output','sha256':digest(b'right')}
        self.assertFalse(verify_result(result, criterion, self.root)[0])
        criterion['path'] = '../outside'
        self.assertFalse(verify_result(result, criterion, self.root)[0])

    def test_legacy_proxy_url_normalized(self):
        with patch.dict(os.environ, {'PROXY_URL':'http://proxy:8001/run-skill'}):
            self.assertEqual(proxy_url('docker-logs'), 'http://proxy:8001/docker-logs')

    def test_dependency_injection_rejected(self):
        with self.assertRaises(ValueError):
            skill_metadata('# MODE: SAFE\n# PIP: requests;curl')

    def test_ast_alias_rejected_before_model(self):
        result = self.manager.pixy._analyze_code_ast('import os\nf=os.system\nf("x")', {'purpose':'x','capabilities':['FileSystem']})
        self.assertFalse(result['safe'])
        self.llm.generate_content.assert_not_called()

    def test_missing_constitution_fails_closed(self):
        with patch('app.pixy.pixy.Path.read_text', side_effect=FileNotFoundError()):
            self.assertFalse(self.manager.pixy._analyze_code_ast('print(1)', {'purpose':'x'})['safe'])

    def test_provider_fallback_charges_same_budget(self):
        llm = LLMManager('config', str(self.root/'llm'), self.memory)
        llm.api_keys_pool = [{'type':'gemini','key':'dummy','id':'one'}, {'type':'openai','key':'dummy','id':'two'}]
        llm._generate_gemini = Mock(side_effect=RuntimeError('down'))
        llm._generate_openai = Mock(return_value='ok')
        task = self.task()
        token = current_budget.set(lambda:self.store.consume(task['id'], 'llm_calls'))
        try:
            self.assertEqual(llm.generate_content('hello'), 'ok')
        finally:
            current_budget.reset(token)
        self.assertEqual(self.store.get(task['id'])['llm_calls'], 2)

    def test_gemini_none_response_format(self):
        llm = LLMManager('config', str(self.root/'llm'), self.memory)
        with patch('requests.post') as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                "candidates": [{"content": {"parts": [{"text": "respuesta directa"}]}}]
            }
            res = llm._generate_gemini("hola", "dummy_key", {"response_format": None})
            self.assertEqual(res, "respuesta directa")

    def test_callback_identity_checked(self):
        with self.assertRaises(PermissionError):
            authenticated_message({'update_id':1, 'callback_query':{'from':{'id':'attacker'},'data':'/approve x y'}}, 'owner')

    def test_webhook_secret_and_duplicate(self):
        app.state.manager = self.manager
        self.manager.telegram.user_id = 'owner'
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                with patch.dict(os.environ, {'TELEGRAM_WEBHOOK_SECRET':'s'*32}):
                    data = {'update_id':42,'message':{'from':{'id':'owner'},'text':'do something'}}
                    self.assertEqual((await client.post('/webhook',json=data)).status_code, 401)
                    headers={'X-Telegram-Bot-Api-Secret-Token':'s'*32}
                    self.assertEqual((await client.post('/webhook',json=data,headers=headers)).status_code, 200)
                    self.assertEqual((await client.post('/webhook',json=data,headers=headers)).status_code, 200)
        asyncio.run(check())
        self.assertEqual(len(self.store.all()), 1)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run = Mock(return_value=SimpleNamespace(returncode=0,stdout='',stderr=''))
        self.engine = Engine(self.root, '/host/data', '/host/docker_proxy', self.run)
        code=b'# MODE: SAFE\nprint(1)'
        sha=hashlib.sha256(code).hexdigest()
        (self.root/'artifacts').mkdir()
        (self.root/'artifacts'/f'{sha}.py').write_bytes(code)
        self.p=dict(execution_id='run_'+'a'*32,task_id='task_'+'b'*32,artifact_hash=sha,
                    mode='SAFE',dependencies=[],request='test',network=False,grants=[],deadline=time.time()+300)

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def test_duplicate_submission_and_payload_conflict(self):
        self.engine.submit(self.p); self.engine.submit(self.p)
        with self.assertRaises(ValueError):
            self.engine.submit(dict(self.p,request='other'))
        self.run.assert_not_called()

    def test_cancel_before_submit_is_tombstoned(self):
        self.assertEqual(self.engine.cancel(self.p['execution_id'])['status'], 'cancelled')
        self.assertEqual(self.engine.submit(self.p)['status'], 'cancelled')
        self.engine.tick()
        self.run.assert_not_called()

    def test_worker_has_no_core_mounts_or_secrets(self):
        args=self.engine.create_command(self.p,'python:3.10-slim')
        text=' '.join(args)
        for forbidden in ['/app/modules', '/app/.env', 'docker.sock', 'PROXY_TOKEN', 'openclaw.db', '--privileged']:
            self.assertNotIn(forbidden,text)
        self.assertIn('--read-only',args)
        self.assertIn('--pids-limit',args)
        self.assertIn('none',args)

    def test_restart_missing_container_not_replayed(self):
        self.engine.submit(self.p)
        self.engine.save(self.p['execution_id'],status='running',started_at=time.time())
        self.run.return_value=SimpleNamespace(returncode=1,stdout='',stderr='Error: No such object')
        self.engine.tick()
        self.assertEqual(self.engine.get(self.p['execution_id'])['status'],'unknown')
        self.assertFalse(any('create' in c.args[0] for c in self.run.call_args_list))

    def test_timeout_removes_container_before_receipt(self):
        self.engine.submit(self.p)
        self.engine.save(self.p['execution_id'],status='running',started_at=time.time()-121)
        item={'Config':{'Labels':{'khai.execution':self.p['execution_id'],'khai.artifact':self.p['artifact_hash']}},'State':{'Running':True}}
        self.run.side_effect=[SimpleNamespace(returncode=0,stdout=json.dumps([item]),stderr=''),SimpleNamespace(returncode=0,stdout='',stderr='')]
        self.engine.tick()
        self.assertEqual(self.engine.get(self.p['execution_id'])['status'],'timeout')
        self.assertIn('rm', self.run.call_args_list[1].args[0])

    def test_docker_outage_keeps_receipt_for_reconciliation(self):
        self.engine.submit(self.p)
        self.engine.save(self.p['execution_id'],status='running',started_at=time.time())
        self.run.return_value=SimpleNamespace(returncode=1,stdout='',stderr='Cannot connect to daemon')
        with self.assertLogs('docker_proxy.engine', level='ERROR'):
            self.engine.tick()
        self.assertEqual(self.engine.get(self.p['execution_id'])['status'],'running')


class WorkerTests(unittest.TestCase):
    def test_real_worker_approval_and_checkpoint_in_private_fixture(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root/'sample.py'
            artifact.write_text("""# MODE: GOD
from app.hand.base_skill import BaseSkill
class DynamicSkill(BaseSkill):
    def execute(self, request):
        value = self.run_step('prepare', lambda: {'message':'prepared'})
        self.require_approval('publish', 'example')
        return value['message']
""", encoding='utf-8')
            config = root/'input.json'
            value = {'task_id':'fixture','request':'test','mode':'GOD','grants':[]}
            config.write_text(json.dumps(value))
            program = ('import sys,json; from pathlib import Path; '
                       'from docker_proxy.worker_runtime import execute; '
                       'sys.exit(execute(json.loads(Path(sys.argv[1]).read_text()),sys.argv[2],sys.argv[3]))')
            env = {'PATH':os.defpath,'SYSTEMROOT':os.environ.get('SYSTEMROOT','C:\\Windows'),
                   'PYTHONIOENCODING':'utf-8'}
            args = [sys.executable,'-B','-c',program,str(config),str(artifact),str(root)]
            paused = subprocess.run(args,env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(paused.returncode,75,paused.stderr)
            self.assertIn('KHAI_APPROVAL:',paused.stdout)
            self.assertTrue((root/'.checkpoints.json').exists())
            value['grants'] = [{'action':'publish','details':'example'}]
            config.write_text(json.dumps(value))
            completed = subprocess.run(args,env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(completed.returncode,0,completed.stderr)
            self.assertIn('prepared',completed.stdout)


if __name__ == '__main__':
    unittest.main()
