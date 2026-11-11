"""Bounded, redacted persistent diagnostics; never includes exception messages."""
import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from .sessions import atomic_write

_lock = threading.Lock()


def diagnose(directory, component, phase, exc, correlation=''):
    record = {'at': datetime.now(timezone.utc).isoformat(), 'component': component,
              'phase': phase, 'error_type': type(exc).__name__,
              'anonymous_id': hashlib.sha256(str(correlation).encode()).hexdigest()[:16]}
    try:
        with _lock:
            path = directory / 'diagnostics' / (component + '.json')
            try:
                records = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                records = []
            atomic_write(path, json.dumps([*records[-99:], record], ensure_ascii=False).encode())
    except (OSError, ValueError):
        logging.getLogger(__name__).error('diagnostic_unavailable component=%s phase=%s error_type=%s',
                                          component, phase, type(exc).__name__)
    return record
