"""Durable request envelopes. Acceptance never means platform success."""
import hashlib
import json
from .sessions import atomic_write


def batch_id(identifiers):
    if (not isinstance(identifiers, list) or not 0 < len(identifiers) <= 10000
            or any(not isinstance(i, str) or not 0 < len(i) <= 128 for i in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise ValueError('请选择有效且不重复的失败记录')
    return hashlib.sha256(json.dumps(sorted(identifiers)).encode()).hexdigest()


class RetryBatches:
    def _batch_path(self, identity):
        if len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
            raise ValueError('批次编号无效')
        return self.directory / 'retry_batches' / (identity + '.json')

    def retry_batch_status(self, identity):
        with self.lock:
            record = json.loads(self._batch_path(identity).read_text(encoding='utf-8'))
            for result in record['results']:
                if result.get('task_id'):
                    job = self._load(self.directory / (result['task_id'] + '.json'))
                    result['execution_state'] = job['items'][0]['state']
            return record

    def existing_retry_batch(self, identifiers):
        identity = batch_id(identifiers)
        return self.retry_batch_status(identity) if self._batch_path(identity).exists() else None

    def accept_retry_batch(self, identifiers, allowed):
        identity = batch_id(identifiers)
        with self.lock:
            if self._closed or self._stopping:
                raise ValueError('工作台正在关闭，不能受理重登')
            path = self._batch_path(identity)
            if path.exists():
                return self.retry_batch_status(identity)
            results = [{'failure_id': i, 'accepted': i in allowed,
                        'state': 'pending' if i in allowed else 'rejected',
                        'reason': '' if i in allowed else '失败记录已更新、非当前表格或已人工标记，未受理'} for i in identifiers]
            record = {'version': 1, 'batch_id': identity, 'accepted': True, 'status': 'accepted',
                      'accepted_count': sum(r['accepted'] for r in results),
                      'rejected_count': sum(not r['accepted'] for r in results), 'results': results}
            atomic_write(path, json.dumps(record, ensure_ascii=False).encode())
            self._dispatch_batch(path)
            return record

    def _dispatch_batch(self, path):
        import threading
        key = 'batch:' + path.stem
        if key in self.scheduled:
            return
        self.scheduled.add(key); self.pending.put((key, path))
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._work, name='registration-worker', daemon=True)
            self.worker.start()

    def _restore_retry_batches(self):
        with self.lock:
            for path in sorted((self.directory / 'retry_batches').glob('*.json')):
                record = json.loads(path.read_text(encoding='utf-8'))
                waiting = any(r.get('task_id') and self._load(self.directory/(r['task_id']+'.json'))['items'][0]['state'] == 'waiting_for_workflow'
                              for r in record['results'])
                if record['status'] in {'accepted', 'preparing'} or (record['status'] == 'dispatched' and waiting):
                    self._dispatch_batch(path)

    def _run_retry_batch(self, path):
        from .registration import RegistrationStartError
        with self.lock:
            record = json.loads(path.read_text(encoding='utf-8'))
            record['status'] = 'preparing'
            atomic_write(path, json.dumps(record, ensure_ascii=False).encode())
            latest = {r['failure_id']: r for r in self._latest_attempts()}
        for result in record['results']:
            if not result['accepted'] or result['state'] == 'rejected':
                continue
            # Replaying a durable envelope resolves the deterministic retry path.
            # It cannot reset a failed/submitted execution or create another attempt.
            try:
                response = self.retry_failed(result['failure_id'], _batch=True, _records=latest)
                result.update(state='queued', task_id=response['task_id'], reused=response['reused'])
            except (ValueError, RegistrationStartError) as exc:
                result.update(state='rejected', reason=str(exc))
            atomic_write(path, json.dumps(record, ensure_ascii=False).encode())
        record['status'] = 'dispatched'
        atomic_write(path, json.dumps(record, ensure_ascii=False).encode())
