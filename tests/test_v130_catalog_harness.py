import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from gift_registry.catalog_maintenance import BoundCatalog, validate_pair
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.gift_groups import GiftGroupStore

CATALOG = [{'id': '00-Ab/1', 'name': '合成旧名称', 'aliases': ['合成旧别名'], 'custom': 'preserved'},
           {'id': '00-Ab/2', 'name': '合成旧名称', 'aliases': []}]
GROUPS = {'version': 1, 'revision': 7, 'groups': [{'id': 'a', 'name': '合成甲组'}, {'id': 'b', 'name': '合成乙组'}],
          'memberships': {'00-Ab/1': ['a', 'b'], '00-Ab/2': ['a']}}


class SyntheticReader:
    def query(self, codes, profile_directory):
        return [{'platform': platform, 'queried_code': code, 'observed_at': 'SYNTHETIC-TEST',
                 'status': 'exact', 'matches': [{'code': code, 'name': '合成核实名称'}]}
                for code in codes for platform in ('banniu', 'jushuitan')]


class CatalogHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/gifts.json').write_text(json.dumps(CATALOG, ensure_ascii=False), encoding='utf-8')
        (self.root / 'config/gift_groups.local.json').write_text(json.dumps(GROUPS, ensure_ascii=False), encoding='utf-8')
        self.tool = BoundCatalog(self.root, reader=SyntheticReader())
        self.evidence = self.tool.query(['00-Ab/1'])['evidence_id']
        self.before = self.bytes()

    def bytes(self):
        return [(self.root / 'config' / name).read_bytes() for name in ('gifts.json', 'gift_groups.local.json')]

    def request(self, groups=False):
        change = {'code': '00-Ab/1', 'name': '合成核实名称'}
        if groups: change['groups'] = {'mode': 'remove', 'ids': ['a']}
        return {'op': 'plan', 'changes': [change], 'evidence_ids': [self.evidence]}

    def approved(self, groups=False):
        result = self.tool.handle(self.request(groups))
        self.tool.approve(result['plan_id'])
        return result['plan_id']

    def test_default_dry_run_full_diff_same_name_distinct_codes_preserved(self):
        result = self.tool.handle(self.request())
        self.assertTrue(result['dry_run'])
        self.assertEqual(self.bytes(), self.before)
        self.assertEqual(result['diff'][0]['code'], '00-Ab/1')
        self.assertEqual(result['diff'][0]['after']['aliases'], ['合成旧别名', '合成旧名称'])
        self.assertEqual(result['diff'][0]['groups_after'], ['a', 'b'])

    def test_approval_is_host_only_and_required(self):
        plan = self.tool.handle(self.request())['plan_id']
        with self.assertRaisesRegex(ValueError, '批准'): self.tool.apply(plan)
        with self.assertRaises(ValueError): self.tool.handle({'op': 'approve', 'plan_id': plan})
        self.assertEqual(self.bytes(), self.before)

    def test_apply_preserves_aliases_unknown_fields_other_codes_and_group_file_bytes(self):
        result = self.tool.apply(self.approved())
        self.assertTrue(result['applied'])
        catalog = json.loads(self.bytes()[0])
        self.assertEqual(catalog[0]['custom'], 'preserved')
        self.assertEqual(catalog[1], CATALOG[1])
        self.assertEqual(self.bytes()[1], self.before[1])

    def test_group_change_preserves_unrelated_memberships_revision_once_and_idempotence(self):
        identifier = self.approved(True)
        self.tool.apply(identifier)
        groups = json.loads(self.bytes()[1])
        self.assertEqual(groups['revision'], 8)
        self.assertEqual(groups['memberships'], {'00-Ab/1': ['b'], '00-Ab/2': ['a']})
        after = self.bytes()
        self.assertTrue(self.tool.apply(identifier)['idempotent'])
        self.assertEqual(self.bytes(), after)

    def test_noop_group_change_does_not_increment_revision(self):
        plan = self.tool.handle({'changes': [{'code': '00-Ab/1', 'groups': {'mode': 'add', 'ids': ['a']}}]})
        self.tool.approve(plan['plan_id']); self.tool.apply(plan['plan_id'])
        self.assertEqual(self.bytes(), self.before)

    def test_missing_platform_and_conflicts_keep_everything_unchanged(self):
        for mutate in ('missing', 'conflict', 'wrong-code', 'duplicate-conflict'):
            with self.subTest(mutate=mutate):
                class Reader(SyntheticReader):
                    def query(self, codes, folder):
                        result = super().query(codes, folder)
                        if mutate == 'missing': result[0]['status'] = 'blocked'
                        if mutate == 'conflict': result[0]['matches'][0]['name'] = '另一名称'
                        if mutate == 'wrong-code': result[0]['matches'][0]['code'] = 'OTHER'
                        if mutate == 'duplicate-conflict': result[0]['matches'].append({'code': codes[0], 'name': '另一名称'})
                        return result
                tool = BoundCatalog(self.root, reader=Reader())
                evidence = tool.query(['00-Ab/1'])['evidence_id']
                result = tool.handle({**self.request(), 'evidence_ids': [evidence]})
                self.assertFalse(result['applicable'])
                with self.assertRaises(ValueError): tool.approve(result['plan_id'])
                self.assertEqual(self.bytes(), self.before)

    def test_unconfigured_query_is_honest_about_missing_real_adapter(self):
        result = BoundCatalog(self.root).query(['00-Ab/1'])
        self.assertTrue(all(e['status'] == 'blocked' and not e['matches'] for e in result['observations']))

    def test_invalid_inputs_no_shell_path_or_permission_route(self):
        for request in ({'op': 'shell', 'cmd': 'echo unsafe'}, {'root': '..'}, {'op': 'apply', 'plan_id': '../gifts.json'},
                        {'op': 'query', 'codes': ['00-Ab/1', '00-Ab/1']}, {'op': 'query', 'codes': [42]},
                        {'changes': [{'code': '00-Ab/1', 'id': 'replacement'}]},
                        {'changes': [{'code': '00-Ab/1', 'aliases_add': ['模型推测']}]},
                        {'add_items': [{'id': 'new', 'name': 'new'}]}):
            with self.subTest(request=request):
                try:
                    result = self.tool.handle(request)
                except (ValueError, TypeError):
                    pass
                else:
                    self.assertFalse(result['applicable'])
                self.assertEqual(self.bytes(), self.before)

    def test_stale_preview_rejects_and_does_not_overwrite_external_change(self):
        identifier = self.approved()
        path = self.root / 'config/gifts.json'
        path.write_bytes(path.read_bytes() + b'\n')
        expected = self.bytes()
        with self.assertRaisesRegex(ValueError, '摘要'): self.tool.apply(identifier)
        self.assertEqual(self.bytes(), expected)

    def test_schema_cross_reference_and_alias_limits_never_truncate(self):
        for change in ({'code': '00-Ab/1', 'groups': {'mode': 'add', 'ids': ['missing']}},):
            with self.assertRaises(ValueError): self.tool.handle({'changes': [change]})
        catalog = deepcopy(CATALOG); catalog[0]['aliases'] = ['x' * 101]
        with self.assertRaises(ValueError): validate_pair(catalog, GROUPS)
        self.assertEqual(self.bytes(), self.before)

    def test_failure_between_replaces_restores_both(self):
        identifier = self.approved(True)
        real = self.tool._replace_target
        calls = []
        def fail_once(path, content):
            calls.append(path)
            if len(calls) == 2: raise OSError('synthetic failure')
            return real(path, content)
        with patch.object(self.tool, '_replace_target', side_effect=fail_once), self.assertRaises(ValueError):
            self.tool.apply(identifier)
        self.assertEqual(self.bytes(), self.before)
        self.assertEqual(GiftCatalogStore(self.root / 'config/gifts.json').read(), CATALOG)

    def test_crash_blocks_readers_then_explicit_recovery_is_idempotent(self):
        identifier = self.approved(True)
        real = self.tool._replace_target
        calls = []
        def crash(path, content):
            calls.append(path)
            if len(calls) == 2: raise KeyboardInterrupt('synthetic process interruption')
            return real(path, content)
        with patch.object(self.tool, '_replace_target', side_effect=crash), self.assertRaises(KeyboardInterrupt):
            self.tool.apply(identifier)
        catalog = GiftCatalogStore(self.root / 'config/gifts.json')
        groups = GiftGroupStore(self.root / 'config/gift_groups.local.json', catalog)
        for store in (catalog, groups):
            with self.assertRaisesRegex(ValueError, '事务未完成'): store.read()
        self.assertTrue(self.tool.recover()['recovered'])
        self.assertFalse(self.tool.recover()['recovered'])
        self.assertEqual(self.bytes(), self.before)

    def test_production_and_usb_targets_rejected_before_writes(self):
        for root in (r'D:\UserData\Desktop\NL', r'D:\UserData\Desktop\NL\.tmp\escape', r'E:\gifts'):
            with self.subTest(root=root), self.assertRaises(ValueError): BoundCatalog(root)

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            link = self.root / 'config/gifts.json'
            link.unlink()
            try: link.symlink_to(Path(outside) / 'gifts.json')
            except OSError:
                self.skipTest('Windows symlink privilege unavailable; junction tested separately')
            with self.assertRaises(ValueError): BoundCatalog(self.root)

    def test_plan_tampering_is_rejected(self):
        identifier = self.approved()
        path = self.tool._work_file('plans', identifier)
        document = json.loads(path.read_bytes()); document['root'] = '..'
        path.write_text(json.dumps(document), encoding='utf-8')
        with self.assertRaises(ValueError): self.tool.apply(identifier)


if __name__ == '__main__': unittest.main()
