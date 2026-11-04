"""订单与整组礼品身份的隔离验证，包含旧记录、人工标记、取消及班牛只读核查。"""
import hashlib
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from gift_registry.banniu_page import GIFT_COLUMNS
from gift_registry.banniu_receipt import BanniuReceiptReader
from gift_registry.frontend_server import RowStore
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.gift_groups import GiftGroupStore
from gift_registry.host_management import HostManagement
from gift_registry.platform_operations import SeleniumOperations
from gift_registry.registration import RegistrationQueue, now, row_signature, task_key
from gift_registry.workflow import OrderSnapshot, PlatformOperationError, RegistrationRequest
from test_host_management import RetryOperations

CATALOG = [{'id': code, 'name': '合成同名礼品'} for code in ('001-A', '001-B', '001-C')]


def row(identifier='r1', codes=('001-A',), order='SYNTHETIC-ORDER'):
    return {'id': identifier, 'order_no': order, 'gift': '、'.join('合成同名礼品' for _ in codes),
            'gift_ids': list(codes), 'change_address': False}


class TaskIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.ops = RetryOperations()
        self.jobs = RegistrationQueue(self.folder / 'jobs', operations=self.ops)
        self.store = RowStore(self.folder / 'rows.json')
        self.management = HostManagement(self.store, self.jobs)

    def tearDown(self):
        self.jobs.pending.join()
        self.jobs.close()
        self.temp.cleanup()

    def run_rows(self, rows):
        result = self.jobs.start(rows, CATALOG)
        self.jobs.pending.join()
        return result

    def test_whole_code_sets_ignore_names_order_and_hidden_address_but_allow_overlap(self):
        ab = row('ab', ('001-A', '001-B'))
        ba = row('ba', ('001-B', '001-A'), ' SYNTHETIC-ORDER ')
        ac = row('ac', ('001-A', '001-C'))
        self.assertEqual(task_key(ab), task_key({**ba, 'gift': '改名', 'phone': 'hidden-synthetic'}))
        self.assertNotEqual(task_key(ab), task_key(ac))
        self.run_rows([ab, ba, ac, row('a')])
        self.assertEqual(len(self.ops.submissions), 3)
        self.assertTrue(all(state['state'] == 'success' for state in self.jobs.states().values()))
        self.assertEqual(len(self.jobs.daily_records()[1]), 3)
        self.jobs.close()
        self.ops = RetryOperations()
        self.jobs = RegistrationQueue(self.folder / 'jobs', operations=self.ops)
        self.run_rows([row('copied', ('001-B', '001-A'))])
        self.assertEqual(self.ops.trace, [])

    def test_failure_candidates_and_legacy_manual_success_are_scoped_to_gift_set(self):
        a, b = row('a'), row('b', ('001-B',))
        self.store.write(0, [a, b])
        self.ops.modes[a['order_no']] = 'query_error'
        self.run_rows([a, b])
        self.assertEqual(len(self.management.retry_candidates()['failures']), 2)
        self.management.mark(['a'], 'success')
        document = json.loads(self.management.path.read_text(encoding='utf-8'))
        for mark in [*document['events'], *document['marks'].values()]:
            mark.pop('task_key')
            mark['order_key'] = self.management._digest(a['order_no'])
        self.management.path.write_text(json.dumps(document), encoding='utf-8')
        original = self.management.path.read_bytes()
        self.assertEqual(self.management.states()['a']['manual_status'], 'success')
        self.assertNotIn('manual_status', self.management.states()['b'])
        candidates = self.management.retry_candidates()['failures']
        self.assertEqual([entry['gift_ids'] for entry in candidates], [['001-B']])
        self.assertEqual(self.management.path.read_bytes(), original)
        self.ops.modes.clear()
        self.management.retry([candidates[0]['failure_id']])
        self.jobs.pending.join()
        self.assertEqual([r.gift_ids for r in self.ops.submissions], [('001-B',)])
        self.assertEqual(self.management.states()['a']['manual_status'], 'success')

    def test_other_gift_success_does_not_erase_failed_combination_or_its_review(self):
        a, b = row('a'), row('b', ('001-B',))
        self.ops.modes[a['order_no']] = 'no_new'
        self.run_rows([a])
        due = datetime.fromisoformat(self.jobs.states()['a']['review_due_at'])
        self.ops.modes.clear()
        self.run_rows([b])
        self.assertEqual(len(self.jobs.retry_candidates()['failures']), 1)
        self.assertFalse(self.jobs.retry_candidates()['failures'][0]['eligible'])
        self.assertEqual(self.jobs.enqueue_due_reviews(due), 1)
        self.jobs.pending.join()
        self.assertEqual(len(self.ops.submissions), 2)
        self.assertIsNotNone(self.jobs.states()['a']['review_result'])
        self.assertEqual(self.jobs.states()['b']['state'], 'success')

    def test_cancel_only_matching_combination_leaves_other_gifts_queued(self):
        entered, release = threading.Event(), threading.Event()
        original = self.ops.query_order
        def held(order):
            if order == 'SYNTHETIC-GATE':
                entered.set()
                release.wait(10)
            return original(order)
        self.ops.query_order = held
        records = [row('gate', order='SYNTHETIC-GATE'), row('a'), row('b', ('001-B',))]
        self.store.write(0, records)
        try:
            self.jobs.start(records, CATALOG)
            self.assertTrue(entered.wait(3))
            self.jobs.start([row('a-copy')], CATALOG)
            target = {'row_id': 'a', 'task_id': self.jobs.states()['a']['task_id']}
            with self.store.lock, self.jobs.lock:
                result = self.management.cancel([target])
            self.assertEqual(result['task_count'], 2)
            self.assertEqual(self.jobs.states()['b']['state'], 'queued')
        finally:
            release.set()
            self.jobs.pending.join()
        self.assertEqual([r.gift_ids for r in self.ops.submissions], [('001-A',), ('001-B',)])

    def test_old_order_only_skipped_snapshot_is_preserved_when_new_gifts_are_started(self):
        self.run_rows([row('a')])
        b = row('b', ('001-B',))
        fingerprint = hashlib.sha256(row_signature(b).encode()).hexdigest()
        path = self.jobs.directory / (fingerprint + '.json')
        item = self.jobs._item(b)
        item.update(state='skipped', reused_result=True, detail='旧版按订单跳过')
        self.jobs._save(path, {'version': 2, 'id': fingerprint[:16], 'created_at': now(),
                              'rows': [b], 'items': [item], 'gift_catalog': CATALOG})
        original = path.read_bytes()
        result = self.run_rows([b])
        self.assertNotEqual(result['task_id'], fingerprint[:16])
        self.assertEqual(self.jobs.states()['b']['state'], 'success')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.ops.submissions), 2)

    def test_missing_historical_codes_remain_conservative_blockers(self):
        old = row('unknown', ())
        item = self.jobs._item(old)
        item.update(state='failed', submission_status='uncertain', submit_attempted=True, attempts=1)
        self.jobs._save(self.jobs.directory / 'unknown.json', {'version': 2, 'id': 'unknown', 'created_at': now(),
                          'rows': [old], 'items': [item], 'gift_catalog': CATALOG})
        self.run_rows([row('new')])
        self.assertEqual(self.ops.trace, [])
        self.assertEqual(self.jobs.states()['new']['state'], 'skipped')


class GroupMembershipTests(unittest.TestCase):
    def test_add_remove_and_replace_preserve_other_memberships_and_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            catalog = folder / 'gifts.json'
            catalog.write_text(json.dumps(CATALOG), encoding='utf-8')
            original = catalog.read_bytes()
            (folder / 'groups.json').write_text(json.dumps({'version': 1, 'revision': 0,
                'groups': [{'id': 'synthetic-a', 'name': '合成甲组'}, {'id': 'synthetic-b', 'name': '合成乙组'}],
                'memberships': {}}), encoding='utf-8')
            groups = GiftGroupStore(folder / 'groups.json', GiftCatalogStore(catalog))
            ids = [group['id'] for group in groups.read()['groups']]
            for revision, group in enumerate(ids):
                groups.update({'action': 'assign', 'revision': revision, 'mode': 'add', 'gift_ids': ['001-A'], 'group_ids': [group]})
            self.assertEqual(groups.read()['memberships']['001-A'], ids)
            groups.update({'action': 'assign', 'revision': 2, 'mode': 'remove', 'gift_ids': ['001-A'], 'group_ids': [ids[0]]})
            self.assertEqual(groups.read()['memberships']['001-A'], [ids[1]])
            groups.update({'action': 'assign', 'revision': 3, 'mode': 'replace', 'gift_ids': ['001-A'], 'group_ids': []})
            self.assertEqual(groups.read()['memberships'], {})
            self.assertEqual(catalog.read_bytes(), original)


class RetryGiftBaselineTests(unittest.TestCase):
    def reader(self, codes):
        page = Mock()
        page._wait.side_effect = lambda predicate, message: predicate(None)
        snapshot = {'fields': {'工单编号': '001', '订单号': 'SYNTHETIC-ORDER'}, 'headers': list(GIFT_COLUMNS),
                    'rows': [['补发', code, '合成同名礼品', '0', '1'] for code in codes]}
        page.driver.execute_script.return_value = snapshot
        reader = BanniuReceiptReader(page)
        reader.open_task = Mock()
        return reader, snapshot

    def test_same_combo_blocks_reordered_codes_while_different_combo_is_allowed(self):
        request = RegistrationRequest('SYNTHETIC-ORDER', ('001-A', '001-B'), ('同名', '同名'))
        rows = [{'task_id': '001'}]
        reader, snapshot = self.reader(('001-C', '001-A'))
        self.assertTrue(reader.check_distinct_gifts(request, rows))
        snapshot['rows'][0][1] = '001-B'
        with self.assertRaisesRegex(PlatformOperationError, '已有该订单及礼品组合'):
            reader.check_distinct_gifts(request, rows)
        snapshot['fields']['订单号'] = 'SYNTHETIC-OTHER'
        with self.assertRaises(PlatformOperationError):
            reader.check_distinct_gifts(request, rows)
        snapshot['fields']['订单号'] = request.order_no
        snapshot['rows'] = []
        with self.assertRaises(PlatformOperationError):
            reader.check_distinct_gifts(request, rows)
        reader.page.submit.assert_not_called()

    def test_changed_task_list_during_detail_checks_blocks(self):
        reader, snapshot = self.reader(('001-C',))
        reader.query_tasks = Mock(return_value=[{'task_id': '003'}])
        request = RegistrationRequest('SYNTHETIC-ORDER', ('001-A',), ('同名',))
        with self.assertRaisesRegex(PlatformOperationError, '基线'):
            reader.check_distinct_gifts(request, [{'task_id': '001'}, {'task_id': '002'}])

    def test_retry_authorization_does_not_transfer_to_other_gift_set(self):
        operations = SeleniumOperations(browsers=Mock())
        operations.configured = True  # 仅此显式 mock 测试。
        page = Mock()
        operations._banniu = Mock(return_value=page)
        a = RegistrationRequest('SYNTHETIC-ORDER', ('001-A',), ('同名',))
        b = RegistrationRequest('SYNTHETIC-ORDER', ('001-B',), ('同名',))
        with patch('gift_registry.banniu_receipt.BanniuReceiptReader') as reader:
            reader.return_value.query_tasks.return_value = [{'task_id': '001'}]
            reader.return_value.check_distinct_gifts.return_value = True
            self.assertTrue(operations.check_retry_safety(a, OrderSnapshot(a.order_no, True), {}))
        operations.submit_registration(b, OrderSnapshot(b.order_no, True))
        page.submit.assert_called_once_with(b, OrderSnapshot(b.order_no, True))
        self.assertIsNone(operations._retry_authorized_task)


if __name__ == '__main__':
    unittest.main()
