"""Overlapping synthetic batches and HTTP response-boundary regressions."""
import io
import json
import tempfile
import threading
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

from fakes import FakeOperations
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.host_management import HostManagement
from gift_registry.registration import RegistrationQueue, row_signature, task_key

CATALOG = [{'id': 'SYN-A', 'name': 'Synthetic A'}, {'id': 'SYN-B', 'name': 'Synthetic B'}]


def row(identifier='a', order=None, gift='SYN-A'):
    return dict(id=identifier, order_no=order or 'SYN-'+identifier,
                gift=next(g['name'] for g in CATALOG if g['id'] == gift), gift_ids=[gift], change_address=False)


class GatedOperations(FakeOperations):
    def __init__(self):
        super().__init__()
        self.entered = {key: threading.Event() for key in ('SYN-a', 'SYN-b')}
        self.release = {key: threading.Event() for key in self.entered}

    def query_order(self, order):
        if order in self.entered and not self.queries.get(order):
            self.entered[order].set()
            if not self.release[order].wait(15):
                raise TimeoutError('Synthetic gate timed out')
        return super().query_order(order)

    def release_all(self):
        for gate in self.release.values():
            gate.set()


class WorkbenchStateTests(unittest.TestCase):
    def test_overlap_progress_success_cancel_and_changed_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = GatedOperations()
            with RegistrationQueue(Path(tmp)/'jobs', operations=ops) as jobs:
                store = RowStore(Path(tmp)/'rows.json'); store.write(0, [row(), row('b'), row('c')])
                host = HostManagement(store, jobs)
                try:
                    first = jobs.start([row(), row('b')], CATALOG)
                    self.assertTrue(ops.entered['SYN-a'].wait(5))
                    jobs.start([row(), row('b'), row('c')], CATALOG)
                    self.assertEqual(jobs.states()['a']['state'], 'querying')
                    self.assertEqual(jobs.states()['a']['task_id'], first['task_id'])
                    ops.release['SYN-a'].set()
                    self.assertTrue(ops.entered['SYN-b'].wait(5))
                    states = host.states()
                    self.assertEqual(states['a']['state'], 'success')
                    self.assertEqual(states['b']['state'], 'querying')
                    self.assertEqual(states['b']['task_id'], first['task_id'])
                    cancelled = host.cancel([dict(row_id='c', task_id=states['c']['task_id'])])
                    self.assertTrue(cancelled['results'][0]['cancelled'])
                    self.assertEqual(jobs.states()['c']['state'], 'cancelled')
                    # Both order and the complete gift set are display boundaries.
                    for changed in [row(order='SYN-edited'), row(gift='SYN-B')]:
                        jobs.start([changed], CATALOG)
                        self.assertEqual(jobs.states()['a']['signature'], row_signature(changed))
                        self.assertEqual(jobs.states()['a']['state'], 'queued')
                finally:
                    ops.release_all(); jobs.pending.join()
                self.assertEqual([r.order_no for r in ops.submissions].count('SYN-b'), 1)
                self.assertEqual(sum(task_key(dict(order_no=r.order_no, gift_ids=list(r.gift_ids))) == task_key(row())
                                     for r in ops.submissions), 1)
                restarted = jobs.start([row('c')], CATALOG); jobs.pending.join()
                self.assertFalse(restarted['reused'])
                self.assertEqual(jobs.states()['c']['state'], 'success')

    def test_reused_failure_refreshes_from_same_execution_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = FakeOperations({'SYN-a':'no_new'})
            with RegistrationQueue(tmp, operations=ops) as jobs:
                first = jobs.start([row()], CATALOG); jobs.pending.join()
                jobs.start([row(), row('b')], CATALOG); jobs.pending.join()
                path = jobs.directory/(first['task_id']+'.json')
                job = jobs._load(path); item = job['items'][0]
                item.update(state='success', detail='Synthetic verified',
                            review_result={'success':True}, evidence={'verified_record_ids':['SYN-new']})
                with jobs.lock:
                    jobs._save_item(path, job, 0, item)
                state = jobs.states()['a']
                self.assertEqual(state['state'], 'success')
                self.assertEqual(state['execution_id'], item['execution_id'])
                self.assertTrue(state['review_result']['success'])
                # External overlay edits and deleted snapshots invalidate cached reads/indexes.
                overlay = jobs._overlay_dir(path)/'0.json'
                update = json.loads(overlay.read_text(encoding='utf-8'))
                update['item']['detail'] = 'Synthetic external verified update'
                overlay.write_text(json.dumps(update), encoding='utf-8')
                self.assertEqual(jobs.states()['a']['detail'], update['item']['detail'])
                jobs._refresh_result_index()
                self.assertEqual(jobs._existing_result(task_key(row()))['detail'], update['item']['detail'])
                path.unlink(); jobs._refresh_result_index()
                self.assertEqual(jobs._existing_result(task_key(row()))['state'], 'failed')


class HTTPBoundaryTests(unittest.TestCase):
    def test_response_oserrors_end_once_and_storage_faults_still_respond(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp); catalog_path = folder/'gifts.json'
            catalog_path.write_text(json.dumps(CATALOG), encoding='utf-8')
            store = RowStore(folder/'rows.json')
            with RegistrationQueue(folder/'jobs', operations=FakeOperations()) as jobs:
                server = make_server(0, store=store, queue=jobs, catalog=GiftCatalogStore(catalog_path))
                try:
                    handler = object.__new__(server.RequestHandlerClass)
                    handler.server = server
                    handler.path = '/api/rows?secret=SYN-private'
                    handler.command = 'GET'; handler.request_version = 'HTTP/1.1'
                    handler.send_response = Mock(); handler.send_header = Mock()
                    handler.end_headers = Mock(); handler.wfile = Mock()
                    handler.permitted = Mock(return_value=True)
                    handler.read_request = lambda: handler.send(200, {'rows':[]})
                    for exc in (BrokenPipeError('SYN-private'), OSError('SYN-private'), TimeoutError('SYN-private')):
                        for stage in ('end_headers', 'wfile'):
                            handler.send_response.reset_mock()
                            handler.end_headers.side_effect = exc if stage == 'end_headers' else None
                            handler.wfile.write.side_effect = exc if stage == 'wfile' else None
                            handler.do_GET()
                            handler.send_response.assert_called_once_with(200)
                            self.assertTrue(handler.close_connection)
                    handler.end_headers.side_effect = None; handler.wfile = io.BytesIO()
                    handler.send_response.reset_mock()
                    handler.read_request = Mock(side_effect=OSError('SYN-private'))
                    handler.do_GET(); handler.send_response.assert_called_once_with(503)
                    self.assertIn('error', json.loads(handler.wfile.getvalue()))
                    records = json.loads((jobs.directory/'diagnostics/http.json').read_text(encoding='utf-8'))
                    self.assertEqual(records[-1]['phase'], 'read:/api/rows')
                    self.assertTrue(any(r['phase'] == 'response_write:/api/rows' and r['error_type'] == 'OSError' for r in records))
                    self.assertNotIn('SYN-private', json.dumps(records))
                    handler.path = '/api/config'; handler.is_local_access = Mock(return_value=True)
                    handler.send = Mock()
                    server.RequestHandlerClass.read_request(handler)
                    token = handler.send.call_args.args[1]['token']
                    del handler.send
                    payload = json.dumps({'revision':0, 'rows':[]}).encode()
                    handler.path = '/api/rows'; handler.command = 'PUT'; handler.headers = Message()
                    handler.headers['X-CSRF-Token'] = token
                    handler.headers['Content-Length'] = str(len(payload))
                    handler.rfile = io.BytesIO(payload); handler.wfile = io.BytesIO()
                    handler.send_response.reset_mock()
                    with patch.object(store, 'write', side_effect=PermissionError('SYN-private')):
                        handler.mutate()
                    handler.send_response.assert_called_once_with(500)
                    records = json.loads((jobs.directory/'diagnostics/http.json').read_text(encoding='utf-8'))
                    self.assertEqual(records[-1]['phase'], 'mutation:/api/rows')
                    self.assertEqual(records[-1]['error_type'], 'PermissionError')
                    self.assertNotIn('SYN-private', json.dumps(records))
                    handler.rfile = io.BytesIO(payload); handler.wfile = Mock()
                    handler.wfile.write.side_effect = OSError('SYN-private')
                    handler.send_response.reset_mock()
                    handler.mutate()
                    handler.send_response.assert_called_once_with(200)
                    self.assertEqual(store.document['revision'], 1)
                finally:
                    server.server_close()
