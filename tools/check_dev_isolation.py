"""Resolve development storage before starting any queue or browser."""
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gift_registry.catalog_transaction import _no_links


def inspect(port=18765):
    protected = [Path(r'D:\UserData\Desktop\NL').resolve(), Path(r'E:\gifts').resolve()]
    root = _no_links(ROOT).resolve()
    paths = {name: root / relative for name, relative in {
        'python': '.venv/Scripts/python.exe', 'config': 'config', 'draft': 'data/registrations.json',
        'queue': 'data/registration_jobs', 'queue_lock': 'data/registration_jobs/.queue.lock',
        'browser': 'data/browser', 'sessions': 'data/sessions', 'driver_cache': 'data/selenium',
        'logs': 'data/frontend_access.log', 'exports': 'data/exports', 'maintenance': '.tmp/catalog-maintenance',
    }.items()}
    for name, path in paths.items():
        resolved = _no_links(path).resolve()
        if not resolved.is_relative_to(root) or any(resolved == p or resolved.is_relative_to(p) for p in protected):
            raise ValueError('Development path is not isolated: ' + name)
    for key in ('SE_CACHE_PATH', 'GIFT_CHROMEDRIVER', 'GIFT_EDGEDRIVER'):
        if os.environ.get(key) and not Path(os.environ[key]).resolve().is_relative_to(root):
            raise ValueError('External writable/tool path: ' + key)
    if port in (8765, 8766) or not 1024 <= port <= 65535:
        raise ValueError('Development port is invalid/protected')
    with socket.socket() as probe:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'): probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(('127.0.0.1', port))
    return {'root': str(root), 'port_checked_free': port, 'paths': {name: str(path.resolve()) for name, path in paths.items()},
            'business_data': 'synthetic only', 'production_and_usb': 'not opened by development queue'}


if __name__ == '__main__':
    result = inspect(int(sys.argv[1]) if len(sys.argv) > 1 else 18765)
    print(json.dumps(result, indent=2, ensure_ascii=False))
