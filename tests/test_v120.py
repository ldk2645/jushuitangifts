"""v1.2.0 主机管理隔离测试，不读取真实任务或登录配置。"""
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.host_management import HostManagement
from gift_registry.maintenance import MaintenanceService, CHINA
from gift_registry.registration import RegistrationQueue
from test_host_management import CATALOG, RetryOperations, row
from test_network import SETTINGS, PASSWORD, authorization


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.operations = RetryOperations()
        self.jobs = RegistrationQueue(self.folder / 'jobs', operations=self.operations)
        self.addCleanup(self.jobs.close)
        self.rows = RowStore(self.folder / 'rows.json')
        self.records = [row('00001', 'r1'), row('00002', 'r2'), row('00003', 'r3')]
        self.rows.write(0, self.records)
        self.management = HostManagement(self.rows, self.jobs)

    def register(self):
        self.jobs.start(self.records, CATALOG)
        self.jobs.pending.join()

    def files(self):
        return {str(p.relative_to(self.jobs.directory)): p.read_bytes()
                for p in self.jobs.directory.rglob('*.json')}

    def test_manual_marks_preserve_actual_results_and_history(self):
        self.operations.modes['00001'] = 'query_error'
        self.register()
        before = self.files()
        self.management.mark(['r1'], 'success')
        self.assertEqual(self.management.states()['r1']['manual_status'], 'success')
        self.assertEqual(self.jobs.states()['r1']['state'], 'failed')
        self.assertEqual(self.files(), before)
        self.assertEqual(self.jobs.daily_records()[1][0]['state'], 'failed')
        self.assertTrue(self.jobs.retry_candidates()['failures'][0]['eligible'])
        restored = HostManagement(RowStore(self.rows.path), self.jobs)
        self.assertEqual(restored.states()['r1']['manual_status'], 'success')
        restored.mark(['r1'], '')
        self.assertNotIn('manual_status', restored.states()['r1'])
        self.assertEqual(len(json.loads(self.management.path.read_text())['events']), 2)

    def test_manual_failure_cannot_enable_retry_of_success(self):
        self.register()
        self.management.mark(['r1'], 'failed')
        self.assertEqual(self.jobs.retry_candidates()['failures'], [])
        copies = [dict(self.records[0], id='copy')]
        self.jobs.start(copies, CATALOG)
        self.jobs.pending.join()
        self.assertEqual(len(self.operations.submissions), 3)

    def test_manual_mark_survives_reuse_timestamp_update(self):
        self.register()
        self.management.mark(['r1'], 'pending')
        self.jobs.start(self.records, CATALOG)
        self.jobs.pending.join()
        self.assertEqual(self.management.states()['r1']['manual_status'], 'pending')

    def test_manual_success_survives_review_and_legacy_timestamp_mismatch(self):
        self.operations.modes['00001'] = 'query_error'
        self.register()
        self.management.mark(['r1'], 'success')
        document = json.loads(self.management.path.read_text(encoding='utf-8'))
        document['marks']['r1'].pop('task_key')
        document['marks']['r1']['order_key'] = self.management._digest('00001')
        for event in document['events']:
            event.pop('task_key', None)
        document['marks']['r1']['execution_updated_at'] = 'old timestamp'
        self.management.path.write_text(json.dumps(document), encoding='utf-8')
        self.assertEqual(self.management.states()['r1']['manual_status'], 'success')
        self.assertEqual(self.management.retry_candidates()['failures'], [])
        failure = self.jobs.retry_candidates()['failures'][0]
        with self.assertRaisesRegex(ValueError, '人工成功'):
            self.management.retry([failure['failure_id']])
        with self.assertRaises(ValueError):
            self.management.retry([failure['failure_id']], batch=True)
        self.assertEqual(self.operations.checks, [])

    def test_current_sheet_only_and_stale_candidate_is_rejected(self):
        self.operations.modes.update({'00001':'query_error', '00002':'query_error'})
        self.register()
        failures = self.management.retry_candidates()['failures']
        self.assertEqual(len(failures), 2)
        removed = next(f for f in failures if f['order_no'] == '00001')
        self.rows.write(1, self.records[1:])
        self.assertEqual([f['order_no'] for f in self.management.retry_candidates()['failures']], ['00002'])
        with self.assertRaises(ValueError):
            self.management.retry([removed['failure_id']])
        # 未修改历史失败记录；只改变主机操作范围。
        self.assertEqual(len(self.jobs.retry_candidates()['failures']), 2)

    def test_manual_success_excludes_copied_order_until_explicit_restore(self):
        self.operations.modes['00001'] = 'query_error'
        self.register()
        self.management.mark(['r1'], 'success')
        copied = dict(self.records[0], id='copy')
        self.rows.write(1, [copied])
        self.jobs.start([copied], CATALOG)
        self.jobs.pending.join()
        self.assertEqual(self.management.states()['copy']['manual_status'], 'success')
        self.assertEqual(self.management.retry_candidates()['failures'], [])
        self.management.mark(['copy'], '')
        self.assertNotIn('manual_status', self.management.states()['copy'])
        self.assertEqual(len(self.management.retry_candidates()['failures']), 1)

    def test_manual_success_remains_in_audit_when_row_id_is_reused(self):
        self.operations.modes['00001'] = 'query_error'
        self.register()
        self.management.mark(['r1'], 'success')
        other = dict(self.records[1], id='r1')
        self.rows.write(1, [other])
        self.jobs.start([other], CATALOG)
        self.jobs.pending.join()
        self.management.mark(['r1'], 'pending')
        original = dict(self.records[0], id='returned')
        self.rows.write(2, [original])
        self.jobs.start([original], CATALOG)
        self.jobs.pending.join()
        self.assertEqual(self.management.states()['returned']['manual_status'], 'success')
        self.assertEqual(self.management.retry_candidates()['failures'], [])

    def test_changed_draft_and_manual_pending_do_not_enter_retry_list(self):
        self.operations.modes.update({'00001':'query_error', '00002':'query_error'})
        self.register()
        self.management.mark(['r1'], 'pending')
        changed = deepcopy(self.records)
        changed[1]['gift'] = 'edited'
        self.rows.write(1, changed)
        self.assertEqual(self.management.retry_candidates()['failures'], [])

    def test_invalid_or_edited_rows_reject_entire_mark(self):
        with self.assertRaises(ValueError):
            self.management.mark(['r1'], 'success')
        self.register()
        modified = deepcopy(self.records)
        modified[1]['order_no'] = 'changed'
        self.rows.write(1, modified)
        for ids, status in [(['r1', 'r2'], 'success'), (['missing'], 'failed'), (['r1', 'r1'], 'success'), (['r1'], []), ([], 'pending')]:
            with self.assertRaises(ValueError):
                self.management.mark(ids, status)
        self.assertFalse(self.management.path.exists())

    def test_delete_backup_and_history_preserved_then_duplicate_still_blocked(self):
        self.register()
        before = self.files()
        result = self.management.delete(['r1', 'r3'])
        self.assertEqual([r['id'] for r in result['rows']], ['r2'])
        self.assertEqual(self.files(), before)
        backups = list((self.folder / 'draft_archive').glob('*.json'))
        self.assertEqual(json.loads(backups[0].read_text(encoding='utf-8'))['rows'], self.records)
        self.assertEqual(self.management.delete(None, True)['rows'], [])
        self.jobs.start([dict(self.records[0], id='restored')], CATALOG)
        self.jobs.pending.join()
        self.assertEqual(len(self.operations.submissions), 3)

    def test_backup_failure_prevents_deletion(self):
        with patch('gift_registry.host_management.atomic_write', side_effect=OSError):
            with self.assertRaises(OSError):
                self.management.delete(None, True)
        self.assertEqual(self.rows.read()['rows'], self.records)

    def test_busy_management_rejected(self):
        self.register()
        self.jobs.pending.put('synthetic-reservation')
        try:
            with self.assertRaises(ValueError):
                self.management.mark(['r1'], 'failed')
            with self.assertRaises(ValueError):
                self.management.delete(None, True)
        finally:
            self.jobs.pending.get_nowait()
            self.jobs.pending.task_done()

    def test_marked_verified_success_kept_during_archive(self):
        clock = datetime(2026, 10, 28, 0, 59, tzinfo=CHINA)
        with patch('gift_registry.registration.now', return_value=(clock - timedelta(hours=2)).isoformat()):
            self.register()
        self.management.mark(['r1'], 'failed')
        service = MaintenanceService(self.jobs, self.rows, self.folder, clock=lambda: clock)
        service.tick()
        clock += timedelta(minutes=1)
        service.tick()
        self.assertEqual([r['id'] for r in self.rows.read()['rows']], ['r1'])

    def test_bulk_retry_eligible_once_blocks_uncertain_and_retains_history(self):
        self.operations.modes.update({'00001': 'query_error', '00002': 'query_error', '00003': 'submit_timeout'})
        self.register()
        failures = self.jobs.retry_candidates()['failures']
        ids = [f['failure_id'] for f in failures]
        self.operations.modes.clear()
        result = self.jobs.retry_all_failed(ids)
        self.jobs.pending.join()
        self.assertEqual(sorted(r['accepted'] for r in result['results']), [False, True, True])
        self.assertEqual(len(self.operations.checks), 2)
        count = len(self.operations.submissions)
        repeated = self.jobs.retry_all_failed(ids)
        self.jobs.pending.join()
        self.assertEqual(len(self.operations.submissions), count)
        self.assertTrue(all(r['reused'] for r in repeated['results'] if r['accepted']))
        self.assertEqual(len(self.jobs.daily_records()[1]), 5)

    def test_bulk_checks_each_and_does_not_chase_new_failures(self):
        self.operations.modes = {r['order_no']: 'query_error' for r in self.records}
        self.register()
        ids = [f['failure_id'] for f in self.jobs.retry_candidates()['failures']]
        self.operations.modes.clear()
        self.operations.safe = False
        self.jobs.retry_all_failed(ids)
        self.jobs.pending.join()
        self.assertEqual(len(self.operations.checks), 3)
        self.assertEqual(self.operations.submissions, [])
        self.assertEqual(len(self.jobs.daily_records()[1]), 6)

    def test_invalid_batch_is_rejected_before_enqueue(self):
        for ids in (None, [], ['x', 'x'], [None], ['x' * 129]):
            with self.assertRaises(ValueError):
                self.jobs.retry_all_failed(ids)
        self.assertEqual(self.files(), {})


class ManagementHTTPTests(unittest.TestCase):
    setUp = ManagementTests.setUp
    register = ManagementTests.register
    def test_http_permissions_csrf_revision_and_independent_status(self):
        self.register()
        catalog_path = self.folder / 'gifts.json'
        catalog_path.write_text(json.dumps(CATALOG), encoding='utf-8')
        server = make_server(0, store=self.rows, queue=self.jobs, catalog=GiftCatalogStore(catalog_path),
                             network=SETTINGS, access_password=PASSWORD)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        opener = build_opener(ProxyHandler({}))
        def request(path, body=None, headers=None):
            return opener.open(Request(url + path, data=None if body is None else json.dumps(body).encode(), headers=headers or {}), timeout=5)
        try:
            with request('/api/config') as response:
                token = json.load(response)['token']
            headers = {'X-CSRF-Token': token, 'Origin': url}
            bodies = [('/api/rows/status', {'revision': 1, 'row_ids': ['r1'], 'status': 'failed'}),
                      ('/api/rows/delete', {'revision': 1, 'all': True}),
                      ('/api/registrations/retry-all', {'failure_ids': ['fake']})]
            for path, body in bodies:
                for bad_headers in ({}, {**headers, 'Host': f'{SETTINGS.domain}:{server.server_port}',
                                        'Origin': f'http://{SETTINGS.domain}:{server.server_port}', 'Authorization': authorization(),
                                        'X-Forwarded-For': '127.0.0.1'}):
                    with self.assertRaises(HTTPError) as error:
                        request(path, body, bad_headers)
                    self.assertEqual(error.exception.code, 403)
            with self.assertRaises(HTTPError) as error:
                request('/api/rows/delete', {'revision': 0, 'all': True}, headers)
            self.assertEqual(error.exception.code, 409)
            with request(*bodies[0], headers) as response:
                self.assertEqual(json.load(response)['row_states']['r1']['manual_status'], 'failed')
            with request('/api/registrations/status') as response:
                self.assertEqual(json.load(response)['row_states']['r1']['state'], 'success')
            with request('/api/rows/delete', {'revision': 1, 'row_ids': ['r2']}, headers) as response:
                self.assertEqual(json.load(response)['removed'], 1)
            self.assertEqual(len(self.jobs.daily_records()[1]), 3)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
