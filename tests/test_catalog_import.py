import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gift_registry.catalog_import import read_code_file, plan_txt_import, apply_txt_plan
from gift_registry.catalog_maintenance import BoundCatalog
from gift_registry.product_reader import complete_rows, observation, banniu_first_row_state


class Reader:
    def __init__(self, names=None):
        self.names = names or {}

    def query(self, codes, folder):
        rows = []
        for platform in ('banniu', 'jushuitan'):
            for code in codes:
                name = self.names.get(code, {}).get(platform, '核实后的商品')
                rows.append(observation(platform, code, [] if name is None else
                                        [{'code': code, 'name': name}], 'synthetic-test'))
        return rows


class CatalogImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config'
        self.config.mkdir()
        self.catalog = [{'id': '00-Ab/1', 'name': '旧名称', 'aliases': ['旧别名'], 'custom': '保留'},
                        {'id': '00-Ab/2', 'name': '未列出的礼品', 'aliases': []}]
        self.groups = {'version': 1, 'revision': 3, 'groups': [{'id': 'a', 'name': '已有组'}],
                       'memberships': {'00-Ab/1': ['a'], '00-Ab/2': ['a']}}
        (self.config / 'gifts.json').write_text(json.dumps(self.catalog), encoding='utf-8')
        (self.config / 'gift_groups.local.json').write_text(json.dumps(self.groups), encoding='utf-8')
        self.tool = BoundCatalog(self.root, reader=Reader())

    def source(self, content='00-Ab/1\n', name='荣耀'):
        path = self.config / f'gifts-{name}.txt'
        path.write_bytes(content.encode('utf-8-sig'))
        return path

    def raw(self):
        return [(self.config / name).read_bytes() for name in ('gifts.json', 'gift_groups.local.json')]

    def test_bom_crlf_blanks_dedup_and_exact_identity(self):
        parsed = read_code_file(self.source(' 00-Ab/1 \r\n\r\n00-Ab/1\r\n00-ab/1\n0001\n'))
        self.assertEqual(parsed['codes'], ['00-Ab/1', '00-ab/1', '0001'])
        self.assertEqual(parsed['group'], '荣耀')
        self.assertEqual(parsed['duplicates'], [{'line': 3, 'code': '00-Ab/1'}])

    def test_invalid_input_does_not_change_config(self):
        before = self.raw()
        for content in ('', '00-Ab/1 商品名称', '00-Ab/1\t00-Ab/2', 'A' * 81, 'SKU\x00'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                plan_txt_import(self.tool, [self.source(content)])
        self.assertEqual(self.raw(), before)
        path = self.source().with_name('gifts.txt')
        path.write_text('00-Ab/1', encoding='utf-8')
        with self.assertRaises(ValueError): read_code_file(path)

    def test_merge_add_rename_multigroup_and_keep_unlisted_data(self):
        paths = [self.source('00-Ab/1\nNEW/001\n'), self.source('00-Ab/1\n', '另一组')]
        before = self.raw()
        result = plan_txt_import(self.tool, paths)
        self.assertTrue(result['applicable'])
        self.assertEqual(self.raw(), before)
        apply_txt_plan(self.tool, result)
        catalog, groups = [json.loads(raw) for raw in self.raw()]
        self.assertEqual(len(catalog), 3)
        self.assertEqual(catalog[1], self.catalog[1])
        self.assertEqual(catalog[0]['custom'], '保留')
        self.assertEqual(catalog[0]['aliases'], ['旧别名', '旧名称'])
        self.assertEqual(catalog[0]['name'], catalog[2]['name'])
        self.assertEqual(groups['memberships']['00-Ab/2'], ['a'])
        self.assertEqual(len(groups['memberships']['00-Ab/1']), 3)
        self.assertEqual(groups['revision'], 4)
        after = self.raw()
        second = plan_txt_import(self.tool, paths)
        self.assertEqual(second['diff'], [])
        apply_txt_plan(self.tool, second)
        self.assertEqual(self.raw(), after)
        backup = json.loads(self.tool._work_file('backups', result['plan_id']).read_bytes())
        self.assertEqual([base64.b64decode(backup['backups'][name]) for name in
                          ('gifts.json', 'gift_groups.local.json')], before)

    def test_offline_only_adds_groups_and_rejects_unknown_codes(self):
        with patch.object(self.tool, 'query', side_effect=AssertionError('离线不能查询')):
            result = plan_txt_import(self.tool, [self.source()], offline=True)
            apply_txt_plan(self.tool, result)
            self.assertEqual(json.loads(self.raw()[0]), self.catalog)
            before = self.raw()
            result = plan_txt_import(self.tool, [self.source('00-Ab/1\nUNKNOWN\n')], offline=True)
            self.assertFalse(result['applicable'])
            with self.assertRaises(ValueError): apply_txt_plan(self.tool, result)
            self.assertEqual(self.raw(), before)

    def test_missing_blocked_wrong_code_and_duplicate_name_block_entire_batch(self):
        before = self.raw()
        for kind in ('blocked', 'missing', 'wrong-code', 'duplicate'):
            observations = Reader().query(['00-Ab/1', 'NEW/001'], None)
            target = next(row for row in observations
                          if row['platform'] == 'banniu' and row['queried_code'] == 'NEW/001')
            if kind == 'blocked': target['status'] = 'blocked'
            if kind == 'missing': target['status'] = 'missing'; target['matches'] = []
            if kind == 'wrong-code': target['matches'][0]['code'] = 'NEAR-CODE'
            if kind == 'duplicate': target['matches'].append({'code': 'NEW/001', 'name': '另一名称'})
            with self.subTest(kind=kind), patch.object(self.tool.reader, 'query', return_value=observations):
                result = plan_txt_import(self.tool, [self.source('00-Ab/1\nNEW/001\n')])
                self.assertFalse(result['applicable'])
                with self.assertRaises(ValueError): apply_txt_plan(self.tool, result)
                self.assertEqual(self.raw(), before)

    def test_banniu_names_differing_only_in_whitespace_keep_existing_name(self):
        self.catalog[0]['name'] = '自拍杆'
        (self.config / 'gifts.json').write_text(json.dumps(self.catalog), encoding='utf-8')
        self.tool = BoundCatalog(self.root, reader=Reader(
            {'00-Ab/1': {'banniu': '自 拍杆', 'jushuitan': '聚水潭名称'}}))
        result = plan_txt_import(self.tool, [self.source()])
        self.assertTrue(result['applicable'])
        self.assertEqual(result['pending'], [])
        change = next(entry for entry in result['diff'] if entry['code'] == '00-Ab/1')
        self.assertEqual(change['after']['name'], '自拍杆')

    def test_banniu_first_row_name_is_written_into_catalog(self):
        self.tool = BoundCatalog(self.root, reader=Reader(
            {'00-Ab/1': {'banniu': '班牛名称', 'jushuitan': '聚水潭名称'}}))
        result = plan_txt_import(self.tool, [self.source()])
        self.assertTrue(result['applicable'])
        apply_txt_plan(self.tool, result)
        catalog = json.loads(self.raw()[0])
        self.assertEqual(catalog[0]['name'], '班牛名称')
        self.assertIn('旧名称', catalog[0]['aliases'])

    def test_banniu_missing_name_keeps_existing_name_but_updates_group(self):
        before = self.raw()
        self.tool = BoundCatalog(self.root, reader=Reader(
            {'00-Ab/1': {'banniu': None, 'jushuitan': '聚水潭名称'}}))
        result = plan_txt_import(self.tool, [self.source()])
        self.assertTrue(result['applicable'])
        self.assertEqual([entry['code'] for entry in result['name_pending']], ['00-Ab/1'])
        self.assertEqual(result['pending'], [])
        apply_txt_plan(self.tool, result)
        catalog = json.loads(self.raw()[0])
        self.assertEqual(catalog[0]['name'], '旧名称')
        self.assertNotEqual(self.raw(), before)

    def test_partial_banniu_observation_keeps_name_and_updates_group(self):
        class PartialReader(Reader):
            def query(self, codes, folder):
                rows = super().query(codes, folder)
                for row in rows:
                    if row['platform'] == 'banniu' and row['queried_code'] == '00-Ab/1':
                        row['status'] = 'partial'
                return rows

        self.tool = BoundCatalog(self.root, reader=PartialReader())
        result = plan_txt_import(self.tool, [self.source()])
        self.assertTrue(result['applicable'])
        self.assertEqual([entry['code'] for entry in result['name_pending']], ['00-Ab/1'])

    def test_new_code_without_banniu_name_blocks_entire_batch(self):
        before = self.raw()
        self.tool = BoundCatalog(self.root, reader=Reader(
            {'NEW/001': {'banniu': None, 'jushuitan': '聚水潭名称'}}))
        result = plan_txt_import(self.tool, [self.source('NEW/001\n')])
        self.assertFalse(result['applicable'])
        self.assertEqual([entry['code'] for entry in result['pending']], ['NEW/001'])
        with self.assertRaises(ValueError):
            apply_txt_plan(self.tool, result)
        self.assertEqual(self.raw(), before)

    def test_edit_during_query_is_not_overwritten(self):
        original = self.tool.reader.query
        def edit(codes, folder):
            (self.config / 'gifts.json').write_text(json.dumps(self.catalog) + '\n', encoding='utf-8')
            return original(codes, folder)
        with patch.object(self.tool.reader, 'query', side_effect=edit), self.assertRaisesRegex(ValueError, '查询期间'):
            plan_txt_import(self.tool, [self.source()])
        self.assertTrue(self.raw()[0].endswith(b'\n'))

    def test_edit_after_preview_is_not_overwritten(self):
        result = plan_txt_import(self.tool, [self.source()])
        (self.config / 'gifts.json').write_text(json.dumps(self.catalog) + '\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '预览后'):
            apply_txt_plan(self.tool, result)
        self.assertTrue(self.raw()[0].endswith(b'\n'))

    def test_second_file_write_failure_restores_both_configs(self):
        result = plan_txt_import(self.tool, [self.source()])
        before = self.raw()
        replace = self.tool._replace_target
        calls = 0
        def fail(path, content):
            nonlocal calls
            calls += 1
            if calls == 2: raise OSError('synthetic disk failure')
            return replace(path, content)
        with patch.object(self.tool, '_replace_target', side_effect=fail), self.assertRaisesRegex(ValueError, '恢复'):
            apply_txt_plan(self.tool, result)
        self.assertEqual(self.raw(), before)


class ProductEvidenceTests(unittest.TestCase):
    def test_partial_virtual_rows_pagination_loading_and_missing_columns_are_rejected(self):
        valid = {'valid': True, 'loading': False, 'page': '1', 'start': True, 'end': True,
                 'total': '共 1 条', 'rows': [{'code': '00-Ab/1', 'name': '商品'}]}
        self.assertEqual(complete_rows(valid), valid['rows'])
        for update in ({'total': '共 2 条'}, {'end': False}, {'page': '2'}, {'loading': True},
                       {'valid': False}, {'rows': [{'code': '00-Ab/1'}]}):
            self.assertIsNone(complete_rows({**valid, **update}))

    def test_names_are_taken_only_from_exact_codes_and_conflicting_duplicates_block(self):
        rows = [{'code': '00-Ab/1-extra', 'name': '不能采用'}, {'code': '00-Ab/1', 'name': '商品'},
                {'code': '00-ab/1', 'name': '大小写不同'}]
        evidence = observation('banniu', '00-Ab/1', rows, 'synthetic-test')
        self.assertEqual(evidence['matches'], [rows[1]])
        self.assertEqual(evidence['status'], 'exact')
        rows.append({'code': '00-Ab/1', 'name': '同编码冲突名称'})
        self.assertEqual(observation('banniu', '00-Ab/1', rows, 'synthetic-test')['status'], 'conflict')
        self.assertEqual(observation('banniu', 'missing', rows, 'synthetic-test')['status'], 'missing')

    def test_whitespace_variants_are_one_verified_name_but_real_differences_conflict(self):
        rows = [{'code': '00-Ab/1', 'name': '自 拍杆'}, {'code': '00-Ab/1', 'name': '自拍杆'}]
        self.assertEqual(observation('banniu', '00-Ab/1', rows, 'synthetic-test')['status'], 'exact')
        rows.append({'code': '00-Ab/1', 'name': '自拍棒'})
        self.assertEqual(observation('banniu', '00-Ab/1', rows, 'synthetic-test')['status'], 'conflict')

    def test_duplicate_rows_are_deduplicated_in_evidence(self):
        rows = [{'code': '00-Ab/1', 'name': '商品'}] * 5 + [{'code': '00-other', 'name': '其它'}]
        evidence = observation('banniu', '00-Ab/1', rows, 'synthetic-test')
        self.assertEqual(evidence['matches'], [{'code': '00-Ab/1', 'name': '商品'}])

    def test_banniu_only_first_row_name_is_used(self):
        rows = [{'code': 'c', 'name': '第一行名称'}, {'code': 'c', 'name': '其它名称'}]
        page = {'total': '共 1216 条', 'page': '1', 'start': True, 'end': False, 'loading': False, 'rows': rows}
        self.assertEqual(banniu_first_row_state(page), (1216, [rows[0]]))
        small = {'total': '共 1 条', 'page': '1', 'start': True, 'end': True, 'loading': False,
                 'rows': [{'code': 'c', 'name': '唯一名称'}]}
        self.assertEqual(banniu_first_row_state(small), (1, small['rows']))
        empty = {'total': '共 0 条', 'page': '1', 'start': True, 'end': True, 'loading': False, 'rows': []}
        self.assertEqual(banniu_first_row_state(empty), (0, []))
        for update in ({'page': '2'}, {'loading': True}, {'total': '加载中'}, {'start': False},
                       {'total': '共 2 条', 'rows': [{'code': 'c'}]},
                       {'total': '共 2 条', 'rows': []}):
            with self.subTest(update=update):
                self.assertIsNone(banniu_first_row_state({**page, **update}))


if __name__ == '__main__':
    unittest.main()
