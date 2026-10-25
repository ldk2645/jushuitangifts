"""任务目录所有权测试；全部使用临时目录及模拟平台。"""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from fakes import FakeOperations
from gift_registry.registration import RegistrationQueue, load_gifts


def sample():
    return {"id": "lock-test", "order_no": "00009", "gift": "米家保温杯口袋版、小米小背包",
            "gift_ids": ["N01B0L1LE91VZS8S01", "N01B0J1JDK1V007H08"], "change_address": False,
            "recipient_name": "测试姓名", "phone": "0013800000000", "address": "测试地址"}


class QueueDirectoryLockTests(unittest.TestCase):
    def test_second_instance_is_blocked_before_recovering_live_tasks(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with RegistrationQueue(folder, operations=SimpleNamespace(configured=False)) as owner:
                owner.start([sample()], load_gifts())
                path = next(Path(folder).glob("*.json"))
                document = json.loads(path.read_text(encoding="utf-8"))
                document["items"][0].update(state="submitting", submit_attempted=True, attempts=1)
                path.write_text(json.dumps(document), encoding="utf-8")
                saved = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "任务目录"):
                    RegistrationQueue(folder)
                self.assertEqual(path.read_bytes(), saved)

    def test_other_process_cannot_own_same_directory(self):
        script = """
import sys
from gift_registry.registration import RegistrationQueue
try:
    jobs = RegistrationQueue(sys.argv[1])
except ValueError:
    sys.exit(17)
else:
    jobs.close()
    sys.exit(0)
"""
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with RegistrationQueue(folder):
                result = subprocess.run([sys.executable, "-c", script, folder], capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 17, result.stderr.decode(errors="replace"))
            result = subprocess.run([sys.executable, "-c", script, folder], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_process_crash_releases_kernel_lock_and_interrupted_task_stays_failed(self):
        script = """
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from gift_registry.registration import RegistrationQueue, load_gifts
jobs = RegistrationQueue(sys.argv[1], operations=SimpleNamespace(configured=False))
jobs.start([json.loads(sys.argv[2])], load_gifts())
path = next(Path(sys.argv[1]).glob('*.json'))
document = json.loads(path.read_text(encoding='utf-8'))
document['items'][0].update(state='verifying', submit_attempted=True, attempts=1)
path.write_text(json.dumps(document), encoding='utf-8')
os._exit(0)
"""
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            result = subprocess.run([sys.executable, "-c", script, folder, json.dumps(sample())],
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            operations = FakeOperations()
            with RegistrationQueue(folder, operations=operations) as recovered:
                self.assertEqual(recovered.states()["lock-test"]["state"], "failed")
                recovered.start([sample()], load_gifts())
                self.assertEqual(operations.trace, [])

    def test_active_queue_cannot_release_ownership(self):
        started, release = threading.Event(), threading.Event()

        def pause_submission(_):
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test-only synchronization timeout")

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = FakeOperations(on_submit=pause_submission)
            jobs = RegistrationQueue(folder, operations=operations)
            try:
                jobs.start([sample()], load_gifts())
                self.assertTrue(started.wait(timeout=5))
                with self.assertRaisesRegex(RuntimeError, "仍在执行"):
                    jobs.close()
                with self.assertRaises(ValueError):
                    RegistrationQueue(folder)
            finally:
                release.set()
                jobs.pending.join()
                jobs.close()
            self.assertEqual(len(operations.submissions), 1)
            with RegistrationQueue(folder):
                pass

    def test_constructor_failure_releases_ownership(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            broken = Path(folder) / "broken.json"
            broken.write_text("not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                RegistrationQueue(folder)
            broken.unlink()
            with RegistrationQueue(folder):
                pass

    def test_different_directories_and_repeated_close_are_safe(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with RegistrationQueue(Path(folder) / "first") as first, RegistrationQueue(Path(folder) / "second"):
                first.close()
                first.close()
                with self.assertRaisesRegex(RuntimeError, "已关闭"):
                    first.start([sample()], load_gifts())

    def test_stopping_blocks_late_enqueue_but_preserves_existing_work(self):
        started, release = threading.Event(), threading.Event()

        def pause_submission(_):
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test-only synchronization timeout")

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = FakeOperations(on_submit=pause_submission)
            jobs = RegistrationQueue(folder, operations=operations)
            try:
                jobs.start([sample()], load_gifts())
                self.assertTrue(started.wait(timeout=5))
                jobs.stop_accepting()
                with self.assertRaisesRegex(ValueError, "正在关闭"):
                    jobs.start([{**sample(), "id": "late-row", "order_no": "00010"}], load_gifts())
                with self.assertRaises(ValueError):
                    RegistrationQueue(folder)
                self.assertEqual(len(list(Path(folder).glob("*.json"))), 1)
            finally:
                release.set()
                jobs.pending.join()
                jobs.close()
            self.assertEqual([request.order_no for request in operations.submissions], ["00009"])
            self.assertEqual(jobs.states()["lock-test"]["state"], "success")


if __name__ == "__main__":
    unittest.main()
