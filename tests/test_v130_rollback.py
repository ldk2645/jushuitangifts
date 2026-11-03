import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.prepare_v120_rollback import prepare
from gift_registry.registration import row_signature, legacy_row_signature


class RollbackTests(unittest.TestCase):
    def test_keeps_new_submission_history_and_source_bytes_while_adapting_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'latest-data'; source.mkdir()
            (source / 'registration_jobs').mkdir()
            row = {'id': 'SYN-R', 'order_no': 'SYN-ORDER', 'gift': '合成礼品', 'gift_ids': ['SYN-A'], 'change_address': False,
                   'gift_selection': {'gift_ids': ['SYN-A']}}
            doc = {'rows': [row], 'items': [{'execution_id': 'NEW-EXECUTION', 'state': 'failed', 'submit_attempted': True,
                                           'attempts': 1, 'submission_status': 'uncertain', 'evidence': {'submit_clicked': True}}]}
            (source / 'registration_jobs/new.json').write_text(json.dumps(doc), encoding='utf-8')
            (source / 'registrations.json').write_text(json.dumps({'revision': 9, 'rows': [row]}), encoding='utf-8')
            mark = {'row_id': row['id'], 'signature': hashlib.sha256(row_signature(row).encode()).hexdigest(), 'status': 'pending'}
            (source / 'manual_status.json').write_text(json.dumps({'events': [mark], 'marks': {row['id']: mark}}), encoding='utf-8')
            before = {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
            destination = Path(tmp) / 'rollback-copy'
            result = prepare(source, destination)
            self.assertFalse(result['deployment_performed'])
            self.assertEqual(before, {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()})
            copied = json.loads((destination / 'registration_jobs/new.json').read_bytes())
            self.assertEqual(copied['items'], doc['items'])
            self.assertEqual(copied['rows'][0]['recipient_name'], '')
            copied_mark = json.loads((destination / 'manual_status.json').read_bytes())['marks'][row['id']]
            self.assertEqual(copied_mark['status'], 'pending')
            self.assertEqual(copied_mark['signature'], hashlib.sha256(legacy_row_signature(copied['rows'][0]).encode()).hexdigest())
            self.assertEqual(copied_mark['v130_signature'], mark['signature'])
            with self.assertRaises(ValueError): prepare(source, destination)

    def test_running_marker_and_production_target_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'data'; source.mkdir(); (source / 'server.pid').write_text('synthetic')
            with self.assertRaises(ValueError): prepare(source, Path(tmp) / 'out')
            (source / 'server.pid').unlink()
            with self.assertRaises(ValueError): prepare(source, Path(r'E:\gifts\rollback-copy'))

    def test_manual_signature_uses_latest_display_binding_not_filename_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'data'
            jobs = source / 'registration_jobs'; jobs.mkdir(parents=True)
            row = {'id': 'SYN-R', 'order_no': 'SYN-O', 'gift': '合成旧名称', 'gift_ids': ['SYN-A']}
            newer = {**row, 'gift': '合成新名称'}
            for name, record, at in [('a-new', newer, '2026-11-03T10:00:00+08:00'),
                                     ('z-old', row, '2026-11-02T10:00:00+08:00')]:
                (jobs / (name + '.json')).write_text(json.dumps({'rows': [record], 'items': [
                    {'display_updated_at': at, 'updated_at': '2026-11-04T10:00:00+08:00'}]}), encoding='utf-8')
            mark = {'signature': hashlib.sha256(row_signature(row).encode()).hexdigest(), 'status': 'pending'}
            (source / 'manual_status.json').write_text(json.dumps({'marks': {'SYN-R': mark}, 'events': []}), encoding='utf-8')
            destination = Path(tmp) / 'out'; prepare(source, destination)
            converted = json.loads((destination / 'manual_status.json').read_bytes())['marks']['SYN-R']
            self.assertEqual(converted['signature'], hashlib.sha256(legacy_row_signature(newer).encode()).hexdigest())


if __name__ == '__main__': unittest.main()
