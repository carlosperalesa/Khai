"""Voice supplies transport facts; Mind evaluates the fixed result schema."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.mind.verifier import verify_result
from app.voice.gateway.external_gateway import ExternalGateway
from app.voice.gateway.service_registry import validate_descriptor


class HttpReceiptTests(unittest.TestCase):
    def test_failed_write_is_not_recorded_as_completed(self):
        contract = {'kind': 'create', 'identity': None, 'correction': None,
                    'summary': {'name': 'Envío de correo', 'context': 'Correo solicitado'}}
        with tempfile.TemporaryDirectory() as folder:
            registry = Mock()
            registry.active_services.return_value = {'mail': {'operations': {
                'send': {'version': 'v1', 'effect': 'write', 'effect_contract': contract}}}}
            gateway = ExternalGateway(Path(folder) / 'absent.yaml', Mock(),
                                      db=Path(folder) / 'voice.sqlite3', registry=registry, execution_authority=Mock())
            gateway.request = Mock(side_effect=RuntimeError('provider unavailable'))
            task = {'id': 'task_' + 'a' * 32, 'execution_id': 'run_' + 'b' * 32,
                    'deadline': time.time() + 60}
            reply = gateway._mailbox_request(task, 'send-1', {
                'service': 'mail', 'operation': 'send', 'params': {'to': 'test@example.org'}})
            receipt = gateway.technical_receipts(task['id'], task['execution_id'], [
                {'service_id': 'mail', 'operation': 'send', 'version': 'v1'}])[0]
            self.assertEqual(reply, {'error': 'RuntimeError'})
            self.assertEqual(receipt['status'], 'uncertain')

    def test_declared_write_identity_and_correction_guard(self):
        contract = {'kind': 'create', 'identity': {'source': 'response', 'field': 'id'},
                    'summary': {'name': 'Nota', 'context': 'Nota creada en Notes'},
                    'correction': {'service_id': 'notes', 'operation': 'update',
                                   'version': 'v1', 'identity_parameter': 'id',
                                   'questions': {'body': {
                                       'description': 'Contenido corregido de la nota',
                                       'question': '¿Qué debería decir la nota?'}}}}
        descriptor = {'service_id': 'notes', 'operation': 'create', 'version': 'v1',
                      'url': 'https://example.org/notes', 'method': 'POST',
                      'parameters': ['body'], 'content_types': ['application/json'],
                      'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
                      'effect': 'write', 'credential_ref': None,
                      'effect_contract': contract}
        self.assertEqual(validate_descriptor(descriptor), descriptor)
        with self.assertRaises(ValueError):
            validate_descriptor({**descriptor, 'effect_contract': None})
        with self.assertRaises(ValueError):
            validate_descriptor({**descriptor, 'effect_contract': {
                **contract, 'summary': {'name': 'Nota', 'context': 'token=secreto'}}})
        with self.assertRaises(ValueError):
            validate_descriptor({**descriptor, 'effect_contract': {
                **contract, 'correction': {
                    'service_id': 'notes', 'operation': 'update',
                    'version': 'v1', 'identity_parameter': 'id'}}})
        with tempfile.TemporaryDirectory() as folder:
            registry = Mock()
            registry.active_services.return_value = {'notes': {'operations': {
                'create': {'version': 'v1', 'effect': 'write', 'effect_contract': contract},
                'update': {'version': 'v1', 'effect': 'write',
                           'effect_contract': {'kind': 'update',
                                               'identity': {'source': 'request', 'field': 'id'},
                                               'summary': {'name': 'Nota', 'context': 'Nota actualizada en Notes'},
                                               'correction': contract['correction']}}}}}
            gateway = ExternalGateway(Path(folder) / 'absent.yaml', Mock(),
                                      db=Path(folder) / 'voice.sqlite3', registry=registry, execution_authority=Mock())
            gateway.request = Mock(return_value={
                'content_type': 'application/json', 'body': b'{"id":"note-1"}'})
            task = {'id': 'task_' + 'a' * 32, 'execution_id': 'run_' + 'b' * 32,
                    'deadline': time.time() + 60}
            gateway._mailbox_request(task, 'create', {
                'service': 'notes', 'operation': 'create', 'params': {'body': 'draft'}})
            refs = [{'service_id': 'notes', 'operation': 'create', 'version': 'v1'}]
            receipt = gateway.technical_receipts(task['id'], task['execution_id'], refs)[0]
            self.assertEqual(receipt['resource_id'], 'note-1')
            self.assertEqual(receipt['effect_contract'], contract)
            self.assertEqual(receipt['summary'], contract['summary'])
            corrected = {**task, 'execution_id': 'run_' + 'c' * 32,
                         'effect_constraints': [{**contract['correction'], 'resource_id': 'note-1'}]}
            with self.assertRaises(PermissionError):
                gateway._mailbox_request(corrected, 'duplicate', {
                    'service': 'notes', 'operation': 'create', 'params': {'body': 'again'}})
            with self.assertRaises(PermissionError):
                gateway._mailbox_request(corrected, 'wrong-id', {
                    'service': 'notes', 'operation': 'update',
                    'params': {'id': 'note-2', 'body': 'fixed'}})
            gateway._mailbox_request(corrected, 'update', {
                'service': 'notes', 'operation': 'update',
                'params': {'id': 'note-1', 'body': 'fixed'}})
            gateway.request.assert_called_with('notes', 'update',
                                               {'id': 'note-1', 'body': 'fixed'}, expected_version='v1')

    def test_receipt_is_bound_to_execution_operation_and_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = Mock()
            registry.active_services.return_value = {'weather': {'operations': {
                'forecast': {'version': 'v1', 'effect': 'read'}}}}
            gateway = ExternalGateway(Path(folder) / 'absent.yaml', Mock(),
                                      db=Path(folder) / 'voice.sqlite3', registry=registry, execution_authority=Mock())
            gateway.request = Mock(return_value={
                'content_type': 'application/json',
                'body': b'{"temperature": 20, "rain": true}'})
            task = {'id': 'task_' + 'a' * 32, 'execution_id': 'run_' + 'b' * 32,
                    'deadline': time.time() + 60}
            gateway._mailbox_request(task, '.voice-1.request.json', {
                'service': 'weather', 'operation': 'forecast', 'params': {'city': 'Santiago'}})
            refs = [{'service_id': 'weather', 'operation': 'forecast', 'version': 'v1'}]
            facts = gateway.technical_receipts(task['id'], task['execution_id'], refs)
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0]['effect'], 'read')
            criterion = {'kind': 'http_schema', 'service_id': 'weather',
                         'operation': 'forecast', 'version': 'v1', 'schema': {
                             'type': 'object', 'properties': {
                                 'temperature': {'type': 'number'},
                                 'rain': {'type': 'boolean'}},
                             'required': ['temperature', 'rain'],
                             'additionalProperties': False}}
            result = {'status': 'finished', 'returncode': 0}
            success, evidence = verify_result(result, criterion, folder, facts,
                                              task['execution_id'])
            self.assertTrue(success)
            self.assertEqual(evidence['receipts'][0]['schema_valid'], True)
            self.assertIsNone(verify_result(result, criterion, folder, facts,
                                             'run_' + 'c' * 32)[0])
            wrong = dict(criterion, schema={**criterion['schema'],
                         'required': ['temperature', 'rain', 'missing']})
            self.assertFalse(verify_result(result, wrong, folder, facts,
                                           task['execution_id'])[0])

    def test_owner_authorized_duplicate_risk_allows_one_pinned_write(self):
        contract = {'kind': 'create', 'identity': None, 'correction': None,
                    'summary': {'name': 'Nota', 'context': 'Creación en Notes'}}
        with tempfile.TemporaryDirectory() as folder:
            registry = Mock()
            registry.active_services.return_value = {'notes': {'operations': {
                'create': {'version': 'v1', 'effect': 'write', 'effect_contract': contract},
                'other': {'version': 'v1', 'effect': 'write', 'effect_contract': contract}}}}
            gateway = ExternalGateway(Path(folder) / 'absent.yaml', Mock(),
                                      db=Path(folder) / 'voice.sqlite3', registry=registry, execution_authority=Mock())
            gateway.request = Mock(return_value={
                'content_type': 'application/json', 'body': b'{"id":"note-2"}'})
            task = {'id': 'task_' + 'a' * 32, 'execution_id': 'run_' + 'b' * 32,
                    'deadline': time.time() + 60, 'state': 'RUNNING',
                    'new_effect_authorization': {
                        'id': 'choice-1', 'task_id': 'task_' + 'a' * 32,
                        'service_id': 'notes', 'operation': 'create', 'version': 'v1'}}
            with self.assertRaises(PermissionError):
                gateway._mailbox_request(task, 'wrong-operation', {
                    'service': 'notes', 'operation': 'other', 'params': {}})
            gateway._mailbox_request(task, 'first', {
                'service': 'notes', 'operation': 'create', 'params': {}})
            self.assertEqual(gateway.technical_receipts(task['id'], task['execution_id'],
                [{'service_id': 'notes', 'operation': 'create', 'version': 'v1'}]
                )[0]['new_effect_authorization_id'], 'choice-1')
            with self.assertRaises(PermissionError):
                gateway._mailbox_request(task, 'second', {
                    'service': 'notes', 'operation': 'create', 'params': {}})
            self.assertEqual(gateway.request.call_count, 1)

    def test_batch_write_has_one_receipt_per_explicit_item_identity(self):
        batch = {'request_field': 'items', 'response_field': 'results',
                 'index_field': 'index', 'identity_field': 'id',
                 'status_field': 'status', 'success_value': 'created',
                 'failure_value': 'failed'}
        contract = {'kind': 'create', 'identity': None, 'batch': batch,
                    'summary': {'name': 'Nota', 'context': 'Lote de Notes'},
                    'correction': {'service_id': 'notes', 'operation': 'update',
                                   'version': 'v1', 'identity_parameter': 'id',
                                   'questions': {'body': {
                                       'description': 'Contenido corregido de la nota',
                                       'question': '¿Qué debería decir la nota?'}}}}
        descriptor = {'service_id': 'notes', 'operation': 'create_batch', 'version': 'v1',
                      'url': 'https://example.org/batch', 'method': 'POST',
                      'parameters': ['items'], 'content_types': ['application/json'],
                      'max_bytes': 10000, 'timeout': 5, 'per_minute': 10,
                      'effect': 'write', 'credential_ref': None,
                      'effect_contract': contract}
        self.assertEqual(validate_descriptor(descriptor), descriptor)
        with self.assertRaises(ValueError):
            validate_descriptor({**descriptor, 'effect_contract': {
                **contract, 'batch': {**batch, 'request_field': 'undeclared'}}})
        with tempfile.TemporaryDirectory() as folder:
            registry = Mock()
            registry.active_services.return_value = {'notes': {'operations': {
                'create_batch': {'version': 'v1', 'effect': 'write',
                                 'effect_contract': contract}}}}
            gateway = ExternalGateway(Path(folder) / 'absent.yaml', Mock(),
                                      db=Path(folder) / 'voice.sqlite3', registry=registry, execution_authority=Mock())
            task = {'id': 'task_' + 'a' * 32, 'execution_id': 'run_' + 'b' * 32,
                    'deadline': time.time() + 60, 'state': 'RUNNING'}
            request = {'service': 'notes', 'operation': 'create_batch',
                       'params': {'items': [{'body': 'first'}, {'body': 'second'}]}}
            refs = [{'service_id': 'notes', 'operation': 'create_batch', 'version': 'v1'}]
            gateway.request = Mock(return_value={'content_type': 'application/json',
                'body': b'{"results":[{"index":1,"status":"created","id":"note-2"},'
                        b'{"index":0,"status":"created","id":"note-1"}]}'})
            gateway._mailbox_request(task, 'batch-1', request)
            receipts = gateway.technical_receipts(task['id'], task['execution_id'], refs)
            self.assertEqual([item['resource_id'] for item in receipts], ['note-1', 'note-2'])
            self.assertEqual([item['item_index'] for item in receipts], [0, 1])
            self.assertEqual(len({item['request_id'] for item in receipts}), 2)
            self.assertTrue(all(item['parent_request_id'] == task['execution_id'] + ':batch-1'
                                for item in receipts))
            criterion = {'kind': 'http_schema', 'service_id': 'notes',
                         'operation': 'create_batch', 'version': 'v1',
                         'schema': {'type': 'object', 'properties': {
                             'results': {'type': 'array'}}, 'required': ['results'],
                             'additionalProperties': False}}
            verified, _ = verify_result({'status': 'finished', 'returncode': 0},
                                        criterion, folder, receipts, task['execution_id'])
            self.assertTrue(verified)
            gateway.request = Mock(return_value={'content_type': 'application/json',
                'body': b'{"results":[{"index":0,"status":"created","id":"note-3"},'
                        b'{"index":1,"status":"failed"}]}'})
            gateway._mailbox_request(task, 'batch-2', request)
            partial = gateway.technical_receipts(task['id'], task['execution_id'], refs)[2:]
            self.assertEqual([(item['status'], item['resource_id']) for item in partial],
                             [('completed', 'note-3'), ('failed', None)])
            verified, _ = verify_result({'status': 'finished', 'returncode': 0},
                                        criterion, folder, partial, task['execution_id'])
            self.assertIsNone(verified)
            gateway.request = Mock(return_value={'content_type': 'application/json',
                'body': b'{"results":[{"index":0,"status":"created","id":"same"},'
                        b'{"index":0,"status":"created","id":"other"}]}'})
            gateway._mailbox_request(task, 'batch-3', request)
            uncertain = gateway.technical_receipts(task['id'], task['execution_id'], refs)[4:]
            self.assertEqual([item['status'] for item in uncertain], ['unknown', 'unknown'])
            self.assertTrue(all(item['resource_id'] is None for item in uncertain))


if __name__ == '__main__':
    unittest.main()
