import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from fakes import FakeOperations
from gift_registry.banniu_page import BanniuPage
from gift_registry.frontend_server import RowStore
from gift_registry.maintenance import MaintenanceService, CHINA
from gift_registry.registration import RegistrationQueue, registration_rows
from gift_registry.sessions import atomic_write
from gift_registry.workflow import PlatformOperationError

CATALOG = [{'id': '00001', 'name': 'Synthetic', 'aliases': []}]
def row(i=0):
    return {'id': f'r{i}', 'order_no': f'SYNTHETIC-{i}', 'gift': 'Synthetic', 'gift_ids': ['00001'], 'change_address': False}


class CoreSafetyTests(unittest.TestCase):
    def test_bad_cursor_does_not_block_review_or_change_bytes(self):
        for payload in (b'\0'*191, b'{"version":1,', b'[]', b'{"version":1}'):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); jobs = Mock(); rows = Mock()
                path = root/'maintenance.json'; path.write_bytes(payload)
                service = MaintenanceService(jobs, rows, root)
                service.tick()
                jobs.enqueue_due_reviews.assert_called_once()
                rows.write.assert_not_called()
                self.assertEqual(path.read_bytes(), payload)
                self.assertEqual(service.status()['health'], 'degraded')
                self.assertIsNone(service.status()['next_run'])

    def test_cursor_permission_and_write_failure_still_schedule(self):
        for error in (PermissionError('sensitive data'), OSError('disk full')):
            with tempfile.TemporaryDirectory() as folder:
                jobs = Mock(); service = MaintenanceService(jobs, Mock(), Path(folder))
                with patch.object(service, '_read', side_effect=error):
                    service.tick(); self.assertTrue(service.status()['needs_attention'])
                jobs.enqueue_due_reviews.assert_called_once()
                self.assertNotIn('sensitive data', (Path(folder)/'diagnostics/maintenance.json').read_text())
        with tempfile.TemporaryDirectory() as folder:
            jobs = Mock(); service = MaintenanceService(jobs, Mock(), Path(folder))
            with patch.object(service, '_save', side_effect=OSError('full')):
                service.tick()
            jobs.enqueue_due_reviews.assert_called_once()
            self.assertTrue(service.status()['needs_attention'])

    def test_missing_cursor_initializes_without_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs = Mock(); jobs.pending.unfinished_tasks = 1
            jobs.lock = threading.RLock(); rows = Mock(); rows.lock = threading.RLock()
            service = MaintenanceService(jobs, rows, Path(folder))
            service.tick()
            self.assertTrue(service.path.exists()); self.assertFalse(service.status()['needs_attention'])

    def test_atomic_concurrent_writers_readers_and_replace_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'; atomic_write(path, b'{"i":-1}')
            errors = []
            def writer(n):
                try:
                    for i in range(15): atomic_write(path, json.dumps({'i': n*100+i}).encode())
                except Exception as exc: errors.append(exc)
            threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
            for t in threads: t.start()
            while any(t.is_alive() for t in threads):
                try: json.loads(path.read_bytes())
                except PermissionError: pass  # Windows sharing violation is an explicit read failure, never partial JSON.
            for t in threads: t.join()
            self.assertEqual(errors, [])
            original = path.read_bytes()
            with patch('gift_registry.sessions.os.replace', side_effect=PermissionError):
                with self.assertRaises(PermissionError): atomic_write(path, b'new')
            self.assertEqual(original, path.read_bytes())
            self.assertFalse(list(Path(folder).glob('*.tmp')))
            with patch('gift_registry.sessions.os.fsync', side_effect=OSError):
                with self.assertRaises(OSError): atomic_write(path, b'new')
            self.assertEqual(original, path.read_bytes())

    def test_click_wait_failure_vs_native_call(self):
        for phase in ('wait', 'native', 'ok'):
            page = object.__new__(BanniuPage); page.driver = Mock(); button = Mock()
            page._form_action = lambda: nullcontext()
            page._wait = Mock(side_effect=PlatformOperationError('blocked') if phase=='wait' else None)
            button.click.side_effect = TimeoutError('unknown') if phase=='native' else None
            markers = []
            try: page._click_once(button, on_click_started=lambda: markers.append('started'))
            except (PlatformOperationError, TimeoutError): pass
            self.assertEqual(markers, [] if phase=='wait' else ['started'])
            self.assertEqual(button.click.call_count, 0 if phase=='wait' else 1)

    def test_recovery_distinguishes_unclaimed_from_uncertain(self):
        for state in ('queued', 'querying', 'submitting', 'verifying'):
            with tempfile.TemporaryDirectory() as folder:
                ops = FakeOperations(); ops.configured = False
                with RegistrationQueue(folder, operations=ops) as jobs:
                    jobs.start([row()], CATALOG)
                path = next(Path(folder).glob('*.json')); data = json.loads(path.read_text())
                data['items'][0].update(state=state, submit_attempted=state in ('submitting','verifying'),
                                        attempts=int(state in ('submitting','verifying')))
                path.write_text(json.dumps(data))
                with RegistrationQueue(folder, operations=ops) as jobs:
                    item = jobs._load(path)['items'][0]
                    self.assertEqual(item['state'], 'waiting_for_workflow' if state=='queued' else 'failed')
                    self.assertEqual(item['submission_status'], 'not_submitted' if state in ('queued','querying') else 'uncertain')
                    self.assertEqual(ops.trace, [])

    def test_corrupt_job_and_mismatched_rows_fail_closed(self):
        for payload in ('{', json.dumps({'rows':[row()], 'items': []})):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder)/'bad.json'; path.write_text(payload)
                ops = FakeOperations()
                with self.assertRaises(ValueError): RegistrationQueue(folder, operations=ops)
                self.assertEqual(path.read_text(), payload); self.assertEqual(ops.trace, [])

    def test_unresolved_snapshot_cannot_register(self):
        data = row(); data['gift_selection'] = {'source':'unresolved'}
        with self.assertRaises(ValueError): registration_rows([data], CATALOG)

    def test_no_evidence_review_never_opens_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            ops = FakeOperations({'SYNTHETIC-0':'query_error'}); ops.prepare_start = Mock()
            with RegistrationQueue(folder, operations=ops) as jobs:
                jobs.start([row()], CATALOG); jobs.pending.join()
                count = len(ops.trace); prepared = ops.prepare_start.call_count
                jobs.enqueue_due_reviews(datetime.now().astimezone()+timedelta(minutes=5)); jobs.pending.join()
                claim = json.loads(next((Path(folder)/'auto_reviews').glob('*.json')).read_text(encoding='utf-8'))
                self.assertEqual(claim['status'], 'manual_required')
                self.assertEqual(len(ops.trace), count); self.assertEqual(ops.prepare_start.call_count, prepared)


if __name__ == '__main__': unittest.main()
