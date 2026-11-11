"""Synthetic HTTP/storage measurements; never constructs SeleniumOperations."""
import argparse
import json
import platform
import sys
import tempfile
import threading
import time
import tracemalloc
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / 'tests')]
from fakes import FakeOperations
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.registration import RegistrationQueue
import gift_registry.registration as registration


def measure(n):
    with tempfile.TemporaryDirectory(prefix='core-bench-') as folder:
        root = Path(folder)
        ops = FakeOperations(); ops.configured = False
        jobs = RegistrationQueue(root / 'jobs', operations=ops)
        rows = RowStore(root / 'rows.json')
        catalog = GiftCatalogStore(root / 'gifts.json')
        catalog.path.write_text(json.dumps([{'id': '00001', 'name': 'Synthetic gift', 'aliases': []}]), encoding='utf-8')
        data = [{'id': f'r{i}', 'order_no': f'SYNTHETIC-{i:06}', 'gift': 'Synthetic gift',
                 'gift_ids': ['00001'], 'change_address': False} for i in range(n)]
        counts = dict(reads=0, read_bytes=0, writes=0, write_bytes=0)
        read = Path.read_text; write = registration.atomic_write
        def observed_read(path, *args, **kwargs):
            value = read(path, *args, **kwargs)
            if path.parent == jobs.directory:
                counts['reads'] += 1; counts['read_bytes'] += len(value.encode())
            return value
        def observed_write(path, value):
            counts['writes'] += 1; counts['write_bytes'] += len(value)
            return write(path, value)
        server = make_server(0, store=rows, queue=jobs, catalog=catalog)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        samples = []
        tracemalloc.start()
        started = time.perf_counter()
        try:
            with patch.object(Path, 'read_text', observed_read), patch.object(registration, 'atomic_write', observed_write):
                jobs.start(data, catalog.read())
                for _ in range(20):
                    before = time.perf_counter()
                    with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/registrations/status') as response:
                        assert response.status == 200; response.read()
                    samples.append((time.perf_counter() - before) * 1000)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop(); server.shutdown(); server.server_close(); thread.join()
        samples.sort()
        return dict(rows=n, scenario='unconfigured acceptance + 20 status HTTP requests',
                    http_ms=samples, p50=samples[9], p95=samples[18], p99=samples[19],
                    elapsed_seconds=time.perf_counter()-started, peak_python_bytes=peak, **counts)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = {'environment': {'platform': platform.platform(), 'python': sys.version}, 'results': []}
    for size in (100, 500, 1000, 2000):
        result['results'].append(measure(size))
        Path(args.output).write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(size, 'complete', flush=True)
