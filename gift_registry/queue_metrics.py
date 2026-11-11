"""Anonymous process-local queue/lock timings, seconds, no order identifiers."""
import queue
import threading
import time


class MeasuredRLock:
    def __init__(self):
        self.lock = threading.RLock(); self.local = threading.local()
        self.max_wait_seconds = self.max_hold_seconds = 0.0

    def acquire(self, *args, **kwargs):
        started = time.perf_counter(); acquired = self.lock.acquire(*args, **kwargs)
        if acquired:
            self.max_wait_seconds = max(self.max_wait_seconds, time.perf_counter()-started)
            if not getattr(self.local, 'depth', 0): self.local.started = time.perf_counter()
            self.local.depth = getattr(self.local, 'depth', 0)+1
        return acquired

    def release(self):
        self.local.depth -= 1
        if not self.local.depth:
            self.max_hold_seconds = max(self.max_hold_seconds, time.perf_counter()-self.local.started)
        self.lock.release()

    def __enter__(self): self.acquire(); return self
    def __exit__(self, *_): self.release()


class MeasuredQueue(queue.Queue):
    def __init__(self):
        super().__init__(); self.enqueued = {}; self.max_wait_seconds = 0.0

    def _put(self, item):
        self.enqueued[id(item)] = time.perf_counter(); super()._put(item)

    def _get(self):
        item = super()._get()
        self.max_wait_seconds = max(self.max_wait_seconds, time.perf_counter()-self.enqueued.pop(id(item), time.perf_counter()))
        return item
