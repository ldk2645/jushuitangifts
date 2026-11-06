"""Shared read/write exclusion and fail-closed cross-file maintenance journal.

Two replaces are NOT a filesystem transaction. A durable pending journal makes
all cooperating readers refuse the pair until commit/recovery has completed.
"""
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

_locks = {}
_guard = threading.Lock()
_local = threading.local()


def state_directory(config_dir):
    config_dir = Path(config_dir).resolve()
    return (config_dir.parent / '.tmp/catalog-maintenance' if config_dir.name == 'config'
            else config_dir / '.catalog-maintenance')


def _no_links(path):
    path = Path(path).absolute()
    for parent in [path, *path.parents]:
        if parent.is_symlink() or parent.is_junction():
            raise ValueError('目录维护路径禁止符号链接或 junction')
    return path


@contextmanager
def catalog_access(config_dir, *, recovery=False):
    folder = _no_links(state_directory(config_dir))
    key = str(folder).casefold()
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        active = getattr(_local, 'active', set())
        if key in active:
            yield folder
            return
        folder.mkdir(parents=True, exist_ok=True)
        path = _no_links(folder / 'lock')
        stream = path.open('a+b')
        try:
            if stream.tell() == 0:
                stream.write(b'\0'); stream.flush()
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError('目录维护正被其它进程占用，请稍后重试') from None
            active.add(key); _local.active = active
            journal = folder / 'transaction.json'
            if journal.exists() and not recovery:
                if json.loads(journal.read_text(encoding='utf-8')).get('state') == 'pending':
                    raise ValueError('目录维护事务未完成，暂不读取配置；请运行受限维护工具 recover')
            yield folder
        finally:
            active.discard(key)
            stream.close()


def sha256(content):
    return hashlib.sha256(content).hexdigest()
