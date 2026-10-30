"""Kernel tests: control store, architecture invariants, database migrations, tracing, and proxy isolation."""
import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import requests

from tests.fixtures import Fixture
from app.bootstrap import ModuleManager
from app.dreams.dreams import Dreams
from app.hand.base_skill import BaseSkill
from app.hand.hand import Hand
from app.kernel.database import backup, connection, migrate
from app.kernel.events import Events
from app.kernel.migration import migrate_data
from app.kernel.tracing import (
    TracingFilter,
    get_trace_id,
    new_trace_id,
    parse_log_errors,
    set_trace_id,
    trace_scope,
)
from app.mind.coordinator import Coordinator, normalize_manifest
from app.mind.memory import MemoryEngine
from app.mind.state import StateManager
from app.mind.verifier import verify_result
from app.pixy.authorization import AuthorizationStore
from app.pixy.budget import current_budget
from app.pixy.control import BudgetExceeded, ControlStore, InstanceLock
from app.pixy.pixy import PixyMind
from app.pixy.rules import Rules
from app.heart.catalog import plan_hash
from app.senses.senses import OBSERVERS, Senses
from app.voice.gateway.asset_gateway import AssetGateway
from app.voice.gateway.external_gateway import ExternalGateway
from app.voice.gateway.llm_gateway import LLMManager
from app.voice.gateway.telegram_gateway import TelegramHandler
from app.voice.mailbox import VoiceMailbox
from app.voice.runtime.contract import digest, proxy_url, skill_metadata
from app.voice.runtime.docker_runtime import DockerRuntime
from app.voice.runtime.local_runtime import LocalRuntime
from app.voice.voice import app, authenticated_message, process_ordinary
from docker_proxy.engine import Engine

CODE = '# MODE: SAFE\nprint(42)'
MANIFEST = {'purpose': 'compute', 'capabilities': []}


class ArchitectureFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.events = Events(self.root/'logs.sqlite3')
        self.pixy = PixyMind(SimpleNamespace(llm=Mock(),telegram=Mock(),audit_db=self.root/'pixy'/'logs.sqlite3'))
        self.hand = Hand(self.root/'hand',self.pixy,self.events)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self,name='calculate',code=CODE):
        return self.hand.create(name,code,MANIFEST)


class HandTests(ArchitectureFixture):
    def test_draft_reject_does_not_publish(self):
        v = self.create()
        self.assertEqual(v['status'],'draft')
        self.assertFalse((self.root/'hand'/'calculate').exists())
        self.hand.reject(v['version_id'])
        self.assertEqual(self.hand.version(v['version_id'])['status'],'rejected')
        self.assertIsNone(self.hand.active('calculate'))

    def test_approve_renames_whole_directory(self):
        v = self.hand.approve(self.create()['version_id'])
        self.assertEqual(v['status'],'active')
        self.assertFalse((self.root/'hand'/'.calculate').exists())
        self.assertEqual({p.name for p in (self.root/'hand'/'calculate').iterdir()},{'SKILL.md','app','data','other'})

    def test_update_archives_before_swap_and_rollback_is_new_identity(self):
        one = self.hand.approve(self.create()['version_id'])
        two = self.hand.approve(self.create(code=CODE+'\nprint(43)')['version_id'])
        archived = self.hand.version(one['version_id'])
        self.assertEqual(archived['archive_name'],'calculate001.zip')
        self.assertEqual(archived['status'],'archived')
        restored = self.hand.rollback(one['version_id'])
        self.assertEqual(restored['version_number'],'v2.1')
        self.assertEqual(restored['previous_version_id'],two['version_id'])
        self.assertEqual(restored['restored_from_version_id'],one['version_id'])
        self.assertEqual(restored['artifact_hash'],one['artifact_hash'])
        self.assertNotEqual(restored['version_id'],one['version_id'])

    def test_corrupt_zip_stops_update_without_removing_active(self):
        one = self.hand.approve(self.create()['version_id'])
        archived = self.hand.archive('calculate')
        (self.root/'hand'/archived['archive_name']).write_bytes(b'bad zip')
        draft = self.create(code=CODE+'\nprint(43)')
        with self.assertRaises(Exception):
            self.hand.approve(draft['version_id'])
        self.assertEqual(self.hand.active('calculate')['version_id'],one['version_id'])
        self.assertTrue((self.root/'hand'/'calculate'/'app'/'main.py').exists())

    def test_delete_retains_archive_and_restore(self):
        one = self.hand.approve(self.create()['version_id'])
        self.hand.delete('calculate')
        self.assertTrue((self.root/'hand'/'calculate001.zip').exists())
        self.assertFalse((self.root/'hand'/'calculate').exists())
        restored = self.hand.restore(one['version_id'])
        self.assertEqual(restored['status'],'active')

    def test_modified_draft_rejected(self):
        v = self.create()
        (self.root/'hand'/'.calculate'/'app'/'main.py').write_text('print("tampered")')
        with self.assertRaises(ValueError):
            self.hand.approve(v['version_id'])

    def test_secret_excluded_from_archive(self):
        self.hand.approve(self.create()['version_id'])
        (self.root/'hand'/'calculate'/'data'/'secrets.env').write_text('SECRET=x')
        with self.assertRaises(ValueError):
            self.hand.archive('calculate')

    def test_recovery_after_rename_before_db_commit(self):
        one = self.hand.approve(self.create()['version_id'])
        two = self.create(code=CODE+'\nprint(43)')
        self.hand.archive('calculate')
        root = self.root/'hand'
        (root/'calculate').rename(root/('.previous-'+two['version_id']))
        (root/'.calculate').rename(root/'calculate')
        with connection(self.hand.db) as conn:
            conn.execute('UPDATE versions SET status="approved" WHERE version_id=?',(two['version_id'],))
        recovered = Hand(root,self.pixy,self.events)
        self.assertEqual(recovered.active('calculate')['version_id'],two['version_id'])
        self.assertEqual(recovered.version(one['version_id'])['status'],'archived')
        self.assertFalse((root/('.previous-'+two['version_id'])).exists())

    def test_operation_history_cannot_be_rewritten(self):
        self.create()
        with self.assertRaises(sqlite3.IntegrityError), connection(self.hand.db) as conn:
            conn.execute('UPDATE operations SET reason="rewrite"')

    def test_rename_failure_restores_previous_active_skill(self):
        one = self.hand.approve(self.create()['version_id'])
        two = self.create(code=CODE+'\nprint(43)')
        original = Path.rename
        def rename(path,target):
            if path.name=='.calculate':
                raise OSError('simulated rename failure')
            return original(path,target)
        with patch.object(Path,'rename',rename), self.assertRaises(OSError):
            self.hand.approve(two['version_id'])
        self.assertEqual(self.hand.active('calculate')['version_id'],one['version_id'])
        self.assertEqual((self.root/'hand'/'calculate'/'app'/'main.py').read_text(),CODE)

    def test_invalid_skill_names(self):
        for name in ('../escape','C:\\x','con','a/b',''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.create(name=name)

    def test_execution_bundle_has_full_skill_and_is_bound_to_reviewed_code(self):
        v = self.hand.approve(self.create()['version_id'])
        code_hash = hashlib.sha256(CODE.encode()).hexdigest()
        folder = Path(self.hand.prepare_execution(v['version_id'],'run_'+'a'*32,code_hash))
        self.assertEqual({p.name for p in folder.iterdir()},{'SKILL.md','app','data','other'})
        self.assertEqual((folder/'app'/'main.py').read_text(),CODE)
        with self.assertRaises(ValueError):
            self.hand.prepare_execution(v['version_id'],'run_'+'b'*32,'0'*64)


class PolicyTests(ArchitectureFixture):
    def test_network_library_use_is_reviewed_in_task_context(self):
        manifest = {'purpose': 'fetch data', 'capabilities': ['Network']}
        context = {'request': 'consultar tasa', 'services': [
            {'service_id': 'fx', 'operation': 'rate', 'version': 'v1'}]}
        self.pixy.llm.generate_content.return_value = json.dumps({
            'safe': False, 'requires_approval': False,
            'reason': 'Intenta comunicar directamente fuera de Voice'})
        direct = self.pixy.review('# MODE: SAFE\nimport requests\nrequests.get("https://example.org")',
                                  manifest, context)
        self.assertFalse(direct['safe'])
        prompt = self.pixy.llm.generate_content.call_args.args[0]
        self.assertIn('consultar tasa', prompt)
        self.assertIn('fx', prompt)
        self.pixy.llm.generate_content.return_value = json.dumps({
            'safe': True, 'requires_approval': False, 'reason': 'Solo analiza una URL'})
        parsed = self.pixy.review('# MODE: SAFE\nfrom urllib.parse import urlsplit\nprint(urlsplit("https://example.org").hostname)',
                                  {'purpose': 'analizar URL', 'capabilities': []},
                                  {'request': 'analizar URL', 'services': []})
        self.assertTrue(parsed['safe'])
        self.assertTrue(self.pixy._analyze_code_ast(
            '# MODE: SAFE\nfrom khai_gateway import request\nrequest("fx", "rate", {})',
            manifest)['safe'])

    def test_process_escapes_always_denied(self):
        for module in ('ctypes', 'subprocess', 'multiprocessing', 'pty'):
            result = self.pixy._analyze_code_ast('# MODE: SAFE\nimport ' + module,
                {'purpose': 'escape', 'capabilities': ['Network', 'FileSystem']})
            self.assertFalse(result['safe'])

    def test_rules_need_explicit_owner_and_keep_history(self):
        rules = Rules(self.root/'rules.sqlite3')
        a = rules.propose('Prefer concise output','user')
        with self.assertRaises(PermissionError): rules.approve(a,None)
        rules.approve(a,'owner:1')
        b = rules.propose('Prefer detailed output','user')
        rules.approve(b,'owner:2',a)
        c = rules.rollback(b,'owner:3')
        self.assertEqual(rules.active()[0]['rule_id'],c)
        self.assertEqual(rules.active()[0]['rollback_of'],b)
        with self.assertRaises(ValueError): rules.propose('permit direct internet','user','security')

    def test_metrics_do_not_mix_versions_or_duplicate_executions(self):
        dreams = Dreams(self.root/'dreams.sqlite3',Rules(self.root/'rules.sqlite3'),self.events)
        task = dict(id='task',execution_id='run1',skill_name='calculate',version_id='v1',artifact_hash='a'*64)
        self.assertTrue(dreams.record(task,True,{'owner':'user'}))
        self.assertFalse(dreams.record(task,True,{'owner':'user'}))
        with self.assertRaises(ValueError):
            dreams.record(task,False,{'owner':'user'})
        dreams.record(dict(task,execution_id='run2',version_id='v2'),False,{})
        self.assertEqual(dreams.metrics('calculate','v1')['success_rate'],1)
        self.assertEqual(dreams.metrics('calculate','v2')['success_rate'],0)


class AssetTests(ArchitectureFixture):
    def setUp(self):
        super().setUp()
        self.assets = AssetGateway(self.root/'other',self.root/'assets.sqlite3',self.pixy,self.events)

    def test_quarantine_publish_and_replaceable_channel(self):
        aid = self.assets.quarantine(b'report','report.txt','owner')
        channel = Mock()
        with self.assertRaises(PermissionError): self.assets.deliver(aid,'owner',channel)
        with self.assertRaises(PermissionError): self.assets.publish(aid,'owner')
        self.assets.publish(aid,'owner',True)
        self.assets.deliver(aid,'owner',channel)
        channel.send_document.assert_called_once()
        self.assets.unpublish(aid,'owner')
        with self.assertRaises(PermissionError): self.assets.deliver(aid,'owner',channel)

    def test_paths_credentials_owner_and_expiration_denied(self):
        for filename in ('../secret.txt','secrets.env','memory.sqlite3'):
            with self.assertRaises(ValueError): self.assets.quarantine(b'x',filename,'owner')
        aid = self.assets.quarantine(b'report','report.txt','owner',ttl=-1)
        with self.assertRaises(PermissionError): self.assets.publish(aid,'owner',True)
        aid = self.assets.quarantine(b'report','report.txt','owner')
        with self.assertRaises(PermissionError): self.assets.publish(aid,'attacker',True)
        with self.assertRaises(ValueError): self.assets.publish('../../data','owner',True)

    def test_mime_and_integrity_verified(self):
        with self.assertRaises(ValueError): self.assets.quarantine(b'fake','photo.png','owner')
        aid = self.assets.quarantine(b'report','report.txt','owner')
        (self.root/'other'/'quarantine'/aid/'report.txt').write_bytes(b'changed')
        with self.assertRaises(ValueError): self.assets.publish(aid,'owner',True)


class DatabaseTests(ArchitectureFixture):
    def test_connection_pragmas_and_failed_migration_is_atomic(self):
        path = self.root/'sample.sqlite3'
        with connection(path) as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0],1)
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0],'wal')
            self.assertEqual(conn.execute('PRAGMA busy_timeout').fetchone()[0],5000)
        with self.assertRaises(sqlite3.OperationalError): migrate(path,[['CREATE TABLE example(id)','INVALID SQL']])
        with connection(path) as conn:
            self.assertIsNone(conn.execute('SELECT name FROM sqlite_master WHERE name="example"').fetchone())

    def test_split_is_repeatable_and_legacy_pending_task_not_replayed(self):
        root = self.root/'legacy'
        root.mkdir()
        db = root/'openclaw.db'
        with connection(db) as conn:
            conn.execute('CREATE TABLE preferences(key TEXT PRIMARY KEY,value_json TEXT)')
            conn.execute('INSERT INTO preferences VALUES ("language",?)',(json.dumps('es'),))
            conn.execute('CREATE TABLE control_tasks(id TEXT PRIMARY KEY,source TEXT,document TEXT)')
            conn.execute('INSERT INTO control_tasks VALUES (?,?,?)',('task','source',json.dumps({'state':'READY'})))
        with patch.dict(os.environ,{'KHAI_DB_PATH':str(db)}):
            result = migrate_data(root)
            self.assertEqual(result,migrate_data(root))
        with connection(root/'mind'/'memory.sqlite3') as conn:
            self.assertEqual(conn.execute('SELECT value_json FROM preferences').fetchone()[0],json.dumps('es'))
        with connection(root/'pixy'/'logs.sqlite3') as conn:
            self.assertEqual(json.loads(conn.execute('SELECT document FROM control_tasks').fetchone()[0])['state'],'UNKNOWN')
        with connection(db) as conn:
            self.assertEqual(json.loads(conn.execute('SELECT document FROM control_tasks').fetchone()[0])['state'],'READY')
        restored = self.root/'restored.sqlite3'
        backup(root/'backups'/'before-domain-split.sqlite3',restored)
        with connection(restored) as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_backup_and_restore_all_domains_without_overwrite(self):
        from scripts.backup_data import snapshot, restore
        source = self.root/'source'
        source.mkdir()
        with connection(source/'mind'/'memory.sqlite3') as conn:
            conn.execute('CREATE TABLE evidence(value TEXT)')
            conn.execute('INSERT INTO evidence VALUES ("preserved")')
        saved = snapshot(source,self.root/'backup')
        recovered = restore(saved,self.root/'recovered')
        with connection(recovered/'mind'/'memory.sqlite3') as conn:
            self.assertEqual(conn.execute('SELECT value FROM evidence').fetchone()[0],'preserved')
        with self.assertRaises(ValueError): restore(saved,recovered)


class RuntimeAndGatewayTests(ArchitectureFixture):
    def test_docker_refuses_direct_network(self):
        with self.assertRaises(PermissionError): DockerRuntime().prepare({'network':True})

    def test_local_has_no_unsafe_fallback(self):
        runtime = LocalRuntime(self.root)
        with patch('app.voice.runtime.local_runtime.shutil.which',return_value=None):
            with self.assertRaises(RuntimeError): runtime.command({},self.root,self.root/'input.json')

    def test_local_execution_identity_cancel_and_receipts(self):
        runtime = LocalRuntime(self.root)
        raw = CODE.encode()
        h = hashlib.sha256(raw).hexdigest()
        (self.root/'artifacts').mkdir()
        (self.root/'artifacts'/(h+'.py')).write_bytes(raw)
        payload = dict(execution_id='run_'+'a'*32,task_id='task_'+'b'*32,
                       artifact_hash=h,network=False,mode='SAFE',dependencies=[],deadline=time.time()+60,request='compute')
        runtime.cancel(payload['execution_id'])
        self.assertEqual(runtime.submit(payload)['status'],'cancelled')
        runtime.command = Mock(return_value=['sandbox'])
        process = Mock()
        process.poll.return_value=0
        process.returncode=0
        runtime.launcher = Mock(return_value=process)
        payload['execution_id']='run_'+'c'*32
        runtime.submit(payload)
        self.assertEqual(runtime.collect_result(payload['execution_id'])['status'],'finished')
        self.assertEqual(runtime.submit(payload)['status'],'finished')
        runtime.launcher.assert_called_once()

    def test_telegram_transport_error_does_not_expose_token(self):
        from app.voice.gateway.telegram_gateway import TelegramHandler
        import requests
        token = 'private-telegram-token'
        with patch('app.voice.gateway.telegram_gateway.requests.post',side_effect=requests.ConnectionError(token)), \
             patch('app.voice.gateway.telegram_gateway.time.sleep'), self.assertLogs('Utils') as logs:
            with self.assertRaises(RuntimeError) as failure:
                TelegramHandler(token,'owner').send_message('hello')
        self.assertNotIn(token,str(failure.exception))
        self.assertNotIn(token,' '.join(logs.output))

    def test_gateway_rejects_unconfigured_operation_and_private_dns(self):
        policy = self.root/'gateways.yaml'
        policy.write_text('services:\n  test:\n    operations:\n      get:\n        url: https://example.test/data\n',encoding='utf-8')
        gateway = ExternalGateway(policy,Mock())
        with self.assertRaises(PermissionError): gateway.request('unknown','get')
        with patch('app.voice.gateway.external_gateway.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(PermissionError): gateway.request('test','get')

    def test_observers_only_call_registered_read_adapters(self):
        adapters = {name:Mock(return_value={'status':'ok'}) for name in OBSERVERS}
        senses = Senses(self.root/'senses.sqlite3',adapters)
        for name in OBSERVERS:
            self.assertEqual(getattr(senses,'listen_'+name)()['status'],'ok')
            adapters[name].assert_called_once()
        with self.assertRaises(ValueError): senses.listen('shell')

    def test_gateway_receipt_prevents_replaying_uncertain_side_effect(self):
        gateway = ExternalGateway(self.root/'missing.yaml',Mock(),db=self.root/'voice.sqlite3',
                                  execution_authority=Mock())
        gateway.request = Mock(return_value={'content_type':'application/json','body':b'{"ok":true}'})
        task = {'id':'task1','execution_id':'run1','deadline':time.time()+60}
        request = {'service':'test','operation':'post','params':{}}
        first = gateway._mailbox_request(task,'request1',request)
        self.assertEqual(gateway._mailbox_request(task,'request1',request),first)
        gateway.request.assert_called_once()
        with connection(gateway.db) as conn:
            conn.execute('UPDATE gateway_requests SET status="sending",reply=NULL')
        self.assertEqual(gateway._mailbox_request(task,'request1',request)['error'],'UnconfirmedPreviousRequest')
        gateway.request.assert_called_once()


class IntegrationTests(unittest.TestCase):
    def test_real_composition_and_verified_task_flow(self):
        from app.bootstrap import ModuleManager
        from app.mind.coordinator import Coordinator
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'data'
            (root/'memory').mkdir(parents=True)
            identity = 'Existing personality preserved verbatim.'
            (root/'memory'/'agent.md').write_text(identity,encoding='utf-8')
            with patch.dict(os.environ,{'KHAI_DB_PATH':str(root/'openclaw.db'),'KHAI_RUNTIME':'local',
                    'TELEGRAM_BOT_TOKEN':'test-token','TELEGRAM_USER_ID':'owner'}):
                manager = ModuleManager(data_dir=str(root))
                self.assertEqual(manager.identity.read(),identity)
                self.assertEqual(manager.memory.db_path,manager.state.db_path)
                self.assertEqual(Path(manager.memory.db_path),root/'control.sqlite3')
                manager.telegram = Mock()
                manager.hand.generate = Mock(return_value=(CODE,'SAFE',[]))
                manager.pixy.review = Mock(return_value={'safe':True,'requires_approval':False})
                proxy = Mock()
                proxy.submit.return_value = {'status':'queued'}
                proxy.get.return_value = {'status':'finished','returncode':0,'stdout':'42'}
                c = Coordinator(manager,proxy)
                try:
                    c.requirements.questions = Mock(return_value=[])
                    manager.coordinator = c
                    task = c.intake('telegram:1','/make compute [MANIFEST] '+json.dumps(MANIFEST))
                    for _ in range(8): c.tick()
                    task = c.store.get(task['id'])
                    self.assertEqual(task['state'],'UNVERIFIED')
                    self.assertEqual(manager.hand.version(task['version_id'])['status'],'draft')
                    self.assertFalse(task['payload']['network'])
                    c.verify(task['id'],task['execution_id'],True,'telegram:2')
                    c.tick()
                    self.assertEqual(c.store.get(task['id'])['state'],'COMPLETED')
                    self.assertEqual(manager.hand.version(task['version_id'])['status'],'one_off')
                    self.assertEqual(manager.dreams.metrics(task['skill_name'],task['version_id'])['success_rate'],1)
                    with connection(manager.events.path) as conn:
                        self.assertGreater(conn.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)
                finally:
                    c._llm_executor.shutdown(wait=True, cancel_futures=True)
                    c._mailbox_executor.shutdown(wait=True, cancel_futures=True)


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
        sid, _ = self.delivered_question(task, 'review')
        self.c.conversations.begin('telegram:correction', 'trace-correction',
            'El resumen está incorrecto, corrige la fuente', 'owner', 'owner', reply_to=800)
        process_ordinary(self.manager, 'telegram:correction', 'trace-correction',
                         'El resumen está incorrecto, corrige la fuente')
        current = self.store.get(task['id'])
        self.assertEqual(current['state'], 'RESOLVING_SCOPE')
        self.assertFalse(current['owner_reviews'][0]['satisfied'])
        self.assertIn('corrige la fuente', current['owner_reviews'][0]['feedback'])

    def test_multiple_pending_questions_default_to_most_recent_without_id(self):
        scope = self.store.create('telegram:scope', 'consulta', state='WAITING_SCOPE')
        review = self.store.create('telegram:review', 'resultado', state='UNVERIFIED',
                                   execution_id='run_' + 'e' * 32)
        self.delivered_question(scope, 'scope', 801)
        self.delivered_question(review, 'review', 802)
        with patch.object(self.c, 'notify'):
            process_ordinary(self.manager, 'telegram:natural', 'trace-natural',
                             'n')
        self.assertEqual(self.store.get(review['id'])['state'], 'WAITING_FEEDBACK')
        self.assertEqual(self.store.get(scope['id'])['state'], 'WAITING_SCOPE')
        with patch.object(self.c, 'notify'):
            process_ordinary(self.manager, 'telegram:scope-specific', 'trace-specific',
                             scope['id'] + ' Santiago')
        self.assertEqual(self.store.get(scope['id'])['state'], 'RESOLVING_SCOPE')

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
        self.assertEqual(self.store.get(task['id'])['state'], 'COMPLETED')
        self.assertEqual(self.store.get(task['id'])['runtime_status'], 'cancelling')


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

    def test_mode_button_is_authenticated_persisted_and_executed_once(self):
        from app.voice.mailbox import VoiceMailbox
        app.state.manager = self.manager
        self.manager.telegram.user_id = 'owner'
        self.manager.voice_mailbox = VoiceMailbox(self.state.db_path)
        self.manager.dolphin = Mock()
        self.manager.dolphin.set_mode.return_value = '[Mode = mid]'
        self.c.notify = Mock()
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                with patch.dict(os.environ, {'TELEGRAM_WEBHOOK_SECRET': 's' * 32}):
                    headers = {'X-Telegram-Bot-Api-Secret-Token': 's' * 32}
                    data = {'update_id': 43, 'callback_query': {
                        'id': 'callback-mode', 'from': {'id': 'attacker'}, 'data': '/mode mid'}}
                    self.assertEqual((await client.post('/webhook', json=data, headers=headers)).status_code, 403)
                    self.manager.telegram.answer_callback_query.assert_not_called()
                    data['callback_query']['from']['id'] = 'owner'
                    for _ in range(2):
                        self.assertEqual((await client.post('/webhook', json=data, headers=headers)).status_code, 200)
        asyncio.run(check())
        mailbox = self.manager.voice_mailbox
        self.assertTrue(mailbox.process_one(
            lambda source, trace, text: process_ordinary(self.manager, source, trace, text), urgent=True))
        self.assertFalse(mailbox.process_one(lambda *_: self.fail('Botón repetido'), urgent=True))
        self.assertEqual(self.manager.dolphin.set_mode.call_args.args[0], 'mid')
        self.assertIn('[Mode = mid]', [call.args[0] for call in self.c.notify.call_args_list])

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

    def test_webhook_acknowledges_service_update_and_accepts_group_topic_text(self):
        from app.voice.mailbox import VoiceMailbox
        app.state.manager = self.manager
        self.manager.telegram.user_id = 'owner'
        self.manager.voice_mailbox = VoiceMailbox(self.state.db_path)

        async def check():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                with patch.dict(os.environ, {'TELEGRAM_WEBHOOK_SECRET': 's' * 32}):
                    headers = {'X-Telegram-Bot-Api-Secret-Token': 's' * 32}
                    service = {
                        'update_id': 50,
                        'my_chat_member': {'chat': {'id': -100123, 'type': 'supergroup'}},
                    }
                    response = await client.post('/webhook', json=service, headers=headers)
                    self.assertEqual(response.status_code, 200)

                    message = {
                        'update_id': 51,
                        'message': {
                            'message_id': 70,
                            'message_thread_id': 100,
                            'from': {'id': 'owner'},
                            'chat': {'id': -100123, 'type': 'supergroup'},
                            'text': 'hola desde el grupo',
                        },
                    }
                    response = await client.post('/webhook', json=message, headers=headers)
                    self.assertEqual(response.status_code, 200)

        asyncio.run(check())
        self.assertIsNone(self.c.conversations.turn('telegram:50'))
        turn = self.c.conversations.turn('telegram:51')
        self.assertIsNotNone(turn)
        self.assertEqual(turn['chat_id'], '-100123')
        self.assertEqual(turn['thread_id'], 100)


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


class TracingAndManifestTests(unittest.TestCase):
    def test_normalize_manifest_markdown(self):
        raw = "```json\n{\"purpose\": \"Consultar el clima de mañana\", \"capabilities\": [\"Network\"]}\n```"
        norm = normalize_manifest(raw, default_purpose="clima")
        self.assertEqual(norm["purpose"], "Consultar el clima de mañana")
        self.assertEqual(norm["capabilities"], ["Network"])

    def test_normalize_manifest_nested_and_string_capabilities(self):
        raw = "{\"manifest\": {\"purpose\": \"Buscar datos\", \"capabilities\": \"Network, FileSystem\"}}"
        norm = normalize_manifest(raw, default_purpose="buscar")
        self.assertEqual(norm["purpose"], "Buscar datos")
        self.assertIn("Network", norm["capabilities"])
        self.assertIn("FileSystem", norm["capabilities"])

    def test_normalize_manifest_fallback_on_empty(self):
        raw = "texto que no es json"
        norm = normalize_manifest(raw, default_purpose="revisar el clima")
        self.assertEqual(norm["purpose"], "revisar el clima")
        self.assertEqual(norm["capabilities"], ["Network"])  # Inferido por 'clima'

    def test_trace_scope_and_context(self):
        tid1 = new_trace_id("req_1")
        self.assertTrue(tid1.startswith("req_1-"))
        with trace_scope(tid1):
            self.assertEqual(get_trace_id(), tid1)
            tid2 = new_trace_id("sub")
            with trace_scope(tid2):
                self.assertEqual(get_trace_id(), tid2)
            self.assertEqual(get_trace_id(), tid1)
        self.assertEqual(get_trace_id(), "")

    def test_tracing_filter(self):
        record = logging.LogRecord("test", logging.INFO, "test.py", 10, "Hello", (), None)
        f = TracingFilter()
        f.filter(record)
        self.assertEqual(record.trace_id, "[-]")

        tid = "req-test-1234"
        with trace_scope(tid):
            f.filter(record)
            self.assertEqual(record.trace_id, f"[{tid}]")

    def test_parse_log_errors_with_boot_epoch(self):
        log_sample = """2026-10-25 01:00:00,100 ERROR [req-01] app.test Error antiguo
Traceback (most recent call last):
  File "test.py", line 1, in <module>
    raise ValueError("antiguo")
ValueError: antiguo
2026-10-25 01:30:00,200 ERROR [req-02] app.test Error reciente
ValueError: reciente
"""
        boot_time = time.mktime(time.strptime("2026-10-25 01:20:00", "%Y-%m-%d %H:%M:%S"))
        incidents = parse_log_errors(log_sample, boot_time=boot_time)
        self.assertEqual(len(incidents), 2)
        self.assertFalse(incidents[0]["is_active"])  # Antiguo al boot
        self.assertTrue(incidents[1]["is_active"])   # Posterior al boot
        self.assertEqual(incidents[1]["trace_id"], "req-02")


class UnifiedPixyAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "auth.sqlite3"
        self.store = AuthorizationStore(self.db, ttl=10)

    def tearDown(self):
        self.tmp.cleanup()

    def test_trace_never_grants_or_consumes_authority(self):
        trace_id = "req-20261025-a1b2c3"
        plan = {'target': 'core', 'steps': [{'action': 'health_check', 'target': 'core'}],
                'capabilities': ['HealthCheck']}
        h = plan_hash(trace_id, plan)

        # Emitir autorización ligada al trace_id
        auth = self.store.issue(trace_id, ['HealthCheck'], 'core', h, 'user', trace_id=trace_id)
        self.assertEqual(auth['status'], 'pending')
        self.assertEqual(auth['trace_id'], trace_id)

        with self.assertRaises(PermissionError):
            self.store.verify_by_trace(trace_id, 'HealthCheck')

        # Usuario aprueba
        decided = self.store.decide(auth['authorization_id'], True, 'user')
        self.assertEqual(decided['status'], 'approved')

        with self.assertRaises(PermissionError):
            self.store.verify_by_trace(trace_id, 'HealthCheck')
        with self.assertRaises(PermissionError):
            self.store.consume_by_trace(trace_id, 'HealthCheck', consumer='hand:test')
        self.assertEqual(self.store.get(auth['authorization_id'])['status'], 'approved')


if __name__ == '__main__':
    unittest.main()
