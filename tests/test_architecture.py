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

from app.kernel.database import connection, migrate, backup
from app.kernel.events import Events
from app.kernel.migration import migrate_data
from app.hand.hand import Hand
from app.pixy.pixy import PixyMind
from app.pixy.rules import Rules
from app.dreams.dreams import Dreams
from app.voice.gateway.asset_gateway import AssetGateway
from app.voice.gateway.external_gateway import ExternalGateway
from app.voice.runtime.local_runtime import LocalRuntime
from app.voice.runtime.docker_runtime import DockerRuntime
from app.senses.senses import Senses, OBSERVERS

CODE = '# MODE: SAFE\nprint(42)'
MANIFEST = {'purpose':'compute','capabilities':[]}


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
        self.assertEqual({p.name for p in (self.root/'hand'/'calculate').iterdir()},{'app','data','other'})

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
        self.assertEqual({p.name for p in folder.iterdir()},{'app','data','other'})
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
        self.assertFalse(dreams.record(task,False,{'owner':'user'}))
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
        gateway = ExternalGateway(self.root/'missing.yaml',Mock(),db=self.root/'voice.sqlite3')
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


if __name__=='__main__':
    unittest.main()
