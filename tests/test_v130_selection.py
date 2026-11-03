import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from fakes import FakeOperations
from gift_registry.frontend_server import RowStore, validate_rows
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.gift_groups import GiftGroupStore
from gift_registry.host_management import HostManagement
from gift_registry.registration import RegistrationQueue, row_signature, legacy_row_signature, registration_rows, task_key
from gift_registry.selection import SelectionPolicy, SelectionConflict, migrate_legacy_selections
from gift_registry.workflow import request_from_row, PlatformOperationError

GIFTS = [{'id': '001-A', 'name': '合成同名'}, {'id': '002-B', 'name': '合成同名'}, {'id': '003-C', 'name': '合成丙'}]
GROUPS = {'version': 1, 'revision': 3, 'groups': [{'id': 'a', 'name': '合成甲'}, {'id': 'b', 'name': '合成乙'}, {'id': 'empty', 'name': '合成空组'}],
          'memberships': {'001-A': ['a', 'b'], '002-B': ['b']}}
def row(identifier='r', codes=('001-A',)):
    return {'id': identifier, 'order_no': 'SYN-ORDER', 'gift': '、'.join(next(g['name'] for g in GIFTS if g['id'] == c) for c in codes),
            'gift_ids': list(codes), 'change_address': False}


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = GiftCatalogStore(self.root / 'gifts.json')
        self.catalog.path.write_text(json.dumps(GIFTS), encoding='utf-8')
        self.groups = GiftGroupStore(self.root / 'groups.local.json', self.catalog)
        self.groups.path.write_text(json.dumps(GROUPS), encoding='utf-8')
        self.policy = SelectionPolicy(self.catalog, self.groups)
        self.store = RowStore(self.root / 'rows.json'); self.store.selection_policy = self.policy

    def scope(self, group='a'):
        _, groups, version = self.policy.context()
        return {'group_id': group, 'group_revision': groups['revision'], 'catalog_version': version}

    def save(self, record):
        self.store.write(self.store.read()['revision'], [record])
        return self.store.read()['rows'][0]

    def test_exact_group_codes_empty_groups_and_unknown_scope_never_fall_back(self):
        for group, code in [('a', '002-B'), ('empty', '001-A'), ('missing', '001-A')]:
            with self.subTest(group=group), self.assertRaises(SelectionConflict):
                self.save({**row(codes=(code,)), 'selection_scope': self.scope(group)})
        self.assertEqual(self.store.read()['revision'], 0)

    def test_two_clients_choose_independently_and_group_file_does_not_change(self):
        before = self.groups.path.read_bytes()
        a = self.save({**row(), 'selection_scope': self.scope('a')})
        b = {**row('other', ('002-B',)), 'selection_scope': self.scope('b')}
        self.store.write(1, [a, b])
        self.assertEqual(self.groups.path.read_bytes(), before)
        saved = self.store.read()['rows']
        self.assertEqual([r['gift_ids'] for r in saved], [['001-A'], ['002-B']])
        self.assertEqual(saved[0]['gift_selection']['events'][0]['group_id'], 'a')
        self.assertEqual(saved[1]['gift_selection']['events'][0]['group_id'], 'b')

    def test_existing_codes_survive_scope_revision_rename_and_deletion(self):
        saved = self.save({**row(), 'selection_scope': self.scope()})
        modified = deepcopy(GROUPS); modified['revision'] += 1
        modified['groups'] = [g for g in modified['groups'] if g['id'] != 'a']
        modified['memberships']['001-A'] = ['b']
        self.groups.path.write_text(json.dumps(modified), encoding='utf-8')
        result = self.save({**saved, 'gift': '新展示名', 'selection_scope': self.scope('missing')})
        self.assertEqual(result['gift_ids'], ['001-A'])
        self.assertEqual(result['gift_selection'], saved['gift_selection'])
        self.assertEqual(row_signature(saved), row_signature(result))

    def test_new_copy_cannot_reuse_snapshot_or_outdated_generation(self):
        saved = self.save({**row(), 'selection_scope': self.scope()})
        for scope in ({**self.scope(), 'group_revision': 2}, {**self.scope(), 'catalog_version': 'stale'}, self.scope('empty')):
            with self.subTest(scope=scope), self.assertRaises(SelectionConflict):
                self.store.write(1, [saved, {**saved, 'id': 'copy', 'selection_scope': scope}])
        with self.assertRaises(SelectionConflict):
            self.store.write(1, [saved, {**row('copy'), 'gift_selection': saved['gift_selection']}])

    def test_reediting_can_keep_old_outside_codes_but_add_only_current_group(self):
        saved = self.save({**row(codes=('002-B',)), 'selection_scope': self.scope('b')})
        saved = self.save({**saved, 'gift_ids': ['002-B', '001-A'], 'selection_scope': self.scope('a')})
        self.assertEqual(saved['gift_ids'], ['002-B', '001-A'])
        with self.assertRaises(SelectionConflict):
            self.save({**saved, 'gift_ids': ['002-B', '001-A', '003-C'], 'selection_scope': self.scope('a')})
        self.assertEqual(self.save({**saved, 'gift_ids': ['001-A']})['gift_ids'], ['001-A'])

    def test_ungrouped_and_all_are_explicit_scopes_and_metadata_is_not_task_identity(self):
        saved = self.save({**row(codes=('003-C',)), 'selection_scope': self.scope('__ungrouped')})
        self.assertEqual(task_key(saved), task_key({**saved, 'selection_scope': self.scope('b')}))
        self.assertEqual(row_signature(saved), row_signature({**saved, 'gift_selection': {'anything': 'display'}}))

    def test_omitted_address_is_compatible_hidden_data_is_not_serialized_true_is_rejected(self):
        legacy = {**row(), 'recipient_name': 'SYN-NAME', 'phone': 'SYN-PHONE', 'address': 'SYN-ADDRESS'}
        legacy.pop('change_address')
        clean = validate_rows([legacy])[0]
        self.assertFalse(clean['change_address'])
        self.assertFalse(set(clean) & {'recipient_name', 'phone', 'address'})
        self.assertIsNone(request_from_row(clean, GIFTS).recipient)
        self.assertFalse(set(registration_rows([legacy], GIFTS)[0]) & {'recipient_name', 'phone', 'address'})
        with self.assertRaises(PlatformOperationError): request_from_row({**legacy, 'change_address': True}, GIFTS)
        with self.assertRaises(ValueError): registration_rows([{**legacy, 'change_address': True}], GIFTS)

    def test_legacy_rename_preserves_state_manual_marks_and_raw_history(self):
        ops = FakeOperations({'SYN-ORDER': 'query_error'})
        with RegistrationQueue(self.root / 'jobs', operations=ops) as jobs:
            original = row(); jobs.start([original], GIFTS); jobs.pending.join()
            history = {str(p): p.read_bytes() for p in jobs.directory.rglob('*.json')}
            self.store.selection_policy = None
            self.store.write(0, [original])
            management = HostManagement(self.store, jobs)
            for kind in ('success', 'pending', 'failed'):
                mark = {'row_id': 'r', 'signature': hashlib.sha256(legacy_row_signature(original).encode()).hexdigest(),
                        'status': kind, 'at': '2026-11-02T12:00:00+08:00'}
                management.path.write_text(json.dumps({'marks': {'r': mark}, 'events': [mark]}), encoding='utf-8')
                renamed = [{**g, 'name': '合成更名'} if g['id'] == '001-A' else g for g in GIFTS]
                migrate_legacy_selections(self.store.document['rows'], renamed, jobs)
                current = self.store.document['rows'][0]; current['gift'] = '合成更名'
                self.assertEqual(management.states()['r'].get('manual_status'), kind)
                self.assertEqual(management.states()['r']['signature'], row_signature(current))
            self.assertEqual(history, {str(p): p.read_bytes() for p in jobs.directory.rglob('*.json')})

    def test_legacy_unmatched_text_remains_blocked_instead_of_being_rematched(self):
        with RegistrationQueue(self.root / 'jobs', operations=FakeOperations()) as jobs:
            record = {**row(), 'gift': '合成同名、未知额外礼品'}
            migrate_legacy_selections([record], GIFTS, jobs)
            self.assertTrue(record['gift_unmatched'])
            with self.assertRaises(ValueError): registration_rows([record], GIFTS)


if __name__ == '__main__': unittest.main()
