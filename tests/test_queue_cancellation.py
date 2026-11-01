"""临时任务目录验证取消边界、并发领取、防重及主机接口。"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.host_management import HostManagement
from gift_registry.network import NetworkSettings
from gift_registry.registration import RegistrationQueue
from gift_registry.workflow import Workflow
from test_host_management import CATALOG, RetryOperations, row


class CancellationFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.ops = RetryOperations()
        self.jobs = RegistrationQueue(self.folder / 'jobs', operations=self.ops)
        self.rows = RowStore(self.folder / 'rows.json')
        self.records = [row('SYNTHETIC-A', 'a'), row('SYNTHETIC-B', 'b'), row('SYNTHETIC-C', 'c')]
        self.rows.write(0, self.records)
        self.management = HostManagement(self.rows, self.jobs)
        self.entered, self.release = threading.Event(), threading.Event()
        self.original_query = self.ops.query_order
        self.ops.query_order = self.block_query

    def block_query(self, order):
        if order == 'SYNTHETIC-A':
            self.entered.set()
            if not self.release.wait(30):
                raise TimeoutError('synthetic gate')
        return self.original_query(order)

    def tearDown(self):
        self.release.set()
        self.jobs.pending.join()
        self.jobs.close()
        self.temp.cleanup()

    def begin(self):
        result = self.jobs.start(self.records, CATALOG)
        self.assertTrue(self.entered.wait(5))
        return result

    def targets(self, *identifiers):
        states = self.jobs.states()
        return [{'row_id': key, 'task_id': states[key]['task_id']} for key in identifiers]

    def cancel(self, targets):
        with self.rows.lock, self.jobs.lock:
            return self.management.cancel(targets)

    def finish(self):
        self.release.set()
        self.jobs.pending.join()


class CancellationTests(CancellationFixture, unittest.TestCase):
    def test_cancel_same_order_in_all_queued_batches_without_interrupting_active_order(self):
        self.begin()
        self.jobs.start([row('SYNTHETIC-B', 'copy-b')], CATALOG)
        response = self.cancel(self.targets('a', 'b'))
        self.assertEqual((response['count'], response['task_count']), (1, 2))
        self.assertFalse(response['results'][0]['cancelled'])
        self.assertTrue(response['results'][1]['cancelled'])
        self.assertEqual(response['row_states']['a']['state'], 'querying')
        self.assertEqual(response['row_states']['b']['state'], 'cancelled')
        self.assertEqual(response['row_states']['copy-b']['state'], 'cancelled')
        self.assertEqual(self.cancel(self.targets('b'))['count'], 0)
        self.finish()
        self.assertEqual([r.order_no for r in self.ops.submissions], ['SYNTHETIC-A', 'SYNTHETIC-C'])
        self.assertNotIn(('query', 'SYNTHETIC-B'), self.ops.trace)
        self.assertIsNone(self.jobs.states()['b']['review_due_at'])
        history = self.jobs._attempt_records()
        cancelled = [r['item'] for r in history if r['item']['state'] == 'cancelled']
        self.assertEqual(len(cancelled), 2)
        self.assertTrue(all(i['cancelled_by'] == 'host' and i['attempts'] == 0 for i in cancelled))
        self.assertEqual(len(self.jobs.daily_records()[1]), 2)

    def test_explicit_start_after_cancellation_preserves_history_and_runs_once(self):
        original = self.begin()
        self.cancel(self.targets('b'))
        self.finish()
        before = {p: p.read_bytes() for p in self.jobs._paths()}
        resumed = self.jobs.start(self.records, CATALOG)
        self.jobs.pending.join()
        again = self.jobs.start(self.records, CATALOG)
        self.jobs.pending.join()
        self.assertNotEqual(original['task_id'], resumed['task_id'])
        self.assertEqual(resumed['task_id'], again['task_id'])
        self.assertTrue(again['reused'])
        self.assertEqual(len(self.ops.submissions), 3)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_stale_task_or_edited_row_cannot_cancel(self):
        self.begin()
        stale = self.targets('b')
        stale[0]['task_id'] = 'old-task'
        self.assertEqual(self.cancel(stale)['count'], 0)
        modified = [dict(r) for r in self.records]
        modified[1]['order_no'] = 'SYNTHETIC-EDITED'
        self.rows.write(1, modified)
        self.assertEqual(self.cancel(self.targets('b'))['count'], 0)
        self.assertEqual(self.jobs.states()['b']['state'], 'queued')
        with self.assertRaises(ValueError):
            self.cancel(self.targets('b') * 2)

    def test_claim_is_persisted_before_workflow_first_callback(self):
        entered, release = threading.Event(), threading.Event()
        original = Workflow.execute
        def held(workflow, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('synthetic gate')
            return original(workflow, *args, **kwargs)
        self.release.set()
        with patch.object(Workflow, 'execute', held):
            try:
                self.jobs.start([self.records[0]], CATALOG)
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.jobs.states()['a']['state'], 'querying')
                self.assertEqual(self.cancel(self.targets('a'))['count'], 0)
                self.assertEqual(self.ops.trace, [])
            finally:
                release.set()
                self.jobs.pending.join()
        self.assertEqual(len(self.ops.submissions), 1)

    def test_restart_preserves_cancelled_and_does_not_dispatch(self):
        self.begin()
        self.cancel(self.targets('b'))
        self.finish()
        self.jobs.close()
        self.ops = RetryOperations()
        self.jobs = RegistrationQueue(self.folder / 'jobs', operations=self.ops)
        self.assertEqual(self.jobs.states()['b']['state'], 'cancelled')
        self.assertEqual(self.jobs.retry_candidates()['failures'], [])
        self.assertEqual(self.ops.trace, [])
        self.jobs.start(self.records, CATALOG)
        self.jobs.pending.join()
        self.assertEqual([r.order_no for r in self.ops.submissions], ['SYNTHETIC-B'])

    def test_cancelled_retry_retains_original_failure_and_requires_explicit_safe_retry(self):
        self.ops.modes['SYNTHETIC-B'] = 'query_error'
        self.jobs.start([self.records[1]], CATALOG)
        self.jobs.pending.join()
        failure = self.jobs.retry_candidates()['failures'][0]
        original = self.jobs._paths()[0]
        original_content = original.read_bytes()
        self.ops.modes.clear()
        self.jobs.start([self.records[0]], CATALOG)
        self.assertTrue(self.entered.wait(3))
        first = self.management.retry([failure['failure_id']])
        self.assertEqual(self.cancel(self.targets('b'))['count'], 1)
        self.assertEqual(self.management.retry_candidates()['failures'][0]['failure_id'], failure['failure_id'])
        self.jobs.start([self.records[1]], CATALOG)
        self.assertEqual(self.jobs.states()['b']['state'], 'failed')
        self.assertEqual(self.ops.submissions, [])
        original_content = original.read_bytes()  # 普通开始仅刷新显示时间，不再次派发失败订单。
        second = self.management.retry([failure['failure_id']])
        self.assertNotEqual(first['task_id'], second['task_id'])
        self.assertTrue(self.jobs.retry_failed(failure['failure_id'])['reused'])
        self.finish()
        self.assertEqual(original.read_bytes(), original_content)
        self.assertEqual(len(self.ops.checks), 1)
        self.assertEqual([r.order_no for r in self.ops.submissions], ['SYNTHETIC-A', 'SYNTHETIC-B'])

    def test_uncertain_submission_cannot_be_cancelled_or_resubmitted(self):
        self.ops.modes['SYNTHETIC-B'] = 'submit_timeout'
        self.jobs.start([self.records[1]], CATALOG)
        self.jobs.pending.join()
        self.begin()
        self.assertEqual(self.cancel(self.targets('b'))['count'], 0)
        self.finish()
        self.jobs.start([row('SYNTHETIC-B', 'new-b')], CATALOG)
        self.jobs.pending.join()
        self.assertEqual(sum(r.order_no == 'SYNTHETIC-B' for r in self.ops.submissions), 1)
        self.assertFalse(self.jobs.retry_candidates()['failures'][0]['eligible'])


class CancellationHttpTests(CancellationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        catalog = self.folder / 'gifts.json'
        catalog.write_text(json.dumps(CATALOG), encoding='utf-8')
        self.server = make_server(0, store=self.rows, queue=self.jobs, catalog=GiftCatalogStore(catalog), network=NetworkSettings())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.opener = build_opener(ProxyHandler({}))
        with self.request('/api/config') as response:
            self.token = json.load(response)['token']

    def tearDown(self):
        self.release.set()
        self.jobs.pending.join()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        super().tearDown()

    def request(self, path, body=None, headers=None):
        return self.opener.open(Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                                       headers=headers or {}), timeout=5)

    def test_host_csrf_and_revision_required(self):
        self.begin()
        payload = {'revision': 1, 'targets': self.targets('b')}
        headers = {'X-CSRF-Token': self.token}
        with self.assertRaises(HTTPError) as denied:
            self.request('/api/registrations/cancel', payload)
        self.assertEqual(denied.exception.code, 403)
        with patch.object(self.server.RequestHandlerClass, 'is_local_access', return_value=False):
            with self.assertRaises(HTTPError) as denied:
                self.request('/api/registrations/cancel', payload, {**headers, 'X-Forwarded-For': '127.0.0.1'})
            self.assertEqual(denied.exception.code, 403)
        with self.assertRaises(HTTPError) as stale:
            self.request('/api/registrations/cancel', {**payload, 'revision': 0}, headers)
        self.assertEqual(stale.exception.code, 409)
        self.assertEqual(self.jobs.states()['b']['state'], 'queued')
        with self.request('/api/registrations/cancel', payload, headers) as response:
            self.assertEqual(json.load(response)['count'], 1)

    def test_cancel_remains_available_during_graceful_shutdown(self):
        self.begin()
        headers = {'X-CSRF-Token': self.token}
        with self.request('/api/shutdown', {}, headers) as response:
            self.assertEqual(response.status, 200)
        with self.request('/api/registrations/cancel', {'revision': 1, 'targets': self.targets('b', 'c')}, headers) as response:
            self.assertEqual(json.load(response)['count'], 2)
        with self.request('/api/health') as response:
            self.assertTrue(json.load(response)['stopping'])
        self.finish()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual([r.order_no for r in self.ops.submissions], ['SYNTHETIC-A'])


if __name__ == '__main__':
    unittest.main()
