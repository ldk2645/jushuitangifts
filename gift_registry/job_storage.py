"""Incremental item persistence; full snapshots remain compatible after compaction.

Each overlay is bound to a snapshot generation and row/execution identity.
Old generations are explicitly retired in the atomic compacted snapshot, making
crashes between replacement and overlay cleanup safe to read.
"""
import hashlib
import json
import uuid
import os
from copy import deepcopy
from .sessions import atomic_write


class JobStorage:
    def _overlay_dir(self, path):
        return self.directory / 'item_updates' / path.stem

    def _read_snapshot(self, path):
        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        cached = self._snapshot_cache.get(path.name)
        if cached is None or cached[0] != stamp:
            raw = path.read_text(encoding='utf-8')
            cached = (stamp, json.loads(raw), raw)
            self._snapshot_cache[path.name] = cached
        # C JSON decoding is faster than recursive Python deepcopy for polling.
        return json.loads(cached[2])

    def _apply_overlay(self, job, index, update):
        generation = job.get('storage_generation', 'legacy')
        if update.get('generation') in job.get('retired_generations', []):
            return
        if (update.get('generation') != generation or update.get('index') != index
                or not 0 <= index < len(job['items']) or update['item'].get('row_id') != job['rows'][index]['id']
                or update.get('base_execution_id') != job['items'][index].get('execution_id')):
            raise ValueError('增量任务记录不一致；禁止执行')
        job['items'][index] = update['item']

    def _read_overlays(self, path, job):
        for update_path in sorted(self._overlay_dir(path).glob('*.json')):
            update = json.loads(update_path.read_text(encoding='utf-8'))
            self._apply_overlay(job, int(update_path.stem), update)
        return job

    def _current_item(self, path, index):
        # Reading a single item never serializes or copies the complete batch.
        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        cached = self._snapshot_cache.get(path.name)
        if cached is None or cached[0] != stamp:
            self._load(path)
        base = self._snapshot_cache[path.name][1]
        update_path = self._overlay_dir(path) / f'{index}.json'
        if update_path.exists():
            update = json.loads(update_path.read_text(encoding='utf-8'))
            if update['generation'] not in base.get('retired_generations', []):
                # Validate without copying every row.
                view = dict(base, items=list(base['items']))
                self._apply_overlay(view, index, update)
                return deepcopy(view['items'][index])
        return deepcopy(base['items'][index])

    def _save_item(self, path, job, index, item):
        from .registration import now
        previous = self._current_item(path, index)
        item['state_version'] = previous.get('state_version', 0) + 1
        base = self._snapshot_cache[path.name][1]
        update = {'generation': base.get('storage_generation', 'legacy'), 'index': index, 'item': item,
                  'base_execution_id': base['items'][index].get('execution_id')}
        self._archive_item(job, index, item)
        atomic_write(self._overlay_dir(path) / f'{index}.json', json.dumps(update, ensure_ascii=False).encode())
        self._storage_epochs[path.name] = self._storage_epochs.get(path.name, 0) + 1
        job['items'][index] = deepcopy(item)
        job['updated_at'] = now()
        self._index_item(path, job, index)
        self._update_order_hold(path, job['rows'][index], item)
        self.changed.set()

    def _compact_completed(self, path):
        """Prepare outside the queue lock; compare-and-swap before publishing.

        Retire the entire overlay directory under the lock so cleanup can never
        delete updates written by a later request using the original directory.
        """
        from .registration import now
        from .diagnostics import diagnose
        with self.lock:
            epoch = self._storage_epochs.get(path.name, 0)
        try:
            job = self._load(path)
            self._summarize_job(job)
            job['updated_at'] = now()
            job['retired_generations'] = list(dict.fromkeys([*job.get('retired_generations', []),
                                                           job.get('storage_generation', 'legacy')]))
            job['storage_generation'] = uuid.uuid4().hex
            payload = json.dumps(job, ensure_ascii=False, indent=2)
        except (OSError, ValueError) as exc:
            diagnose(self.directory, 'storage', 'prepare_compaction', exc, path.name)
            return  # Authoritative overlays remain intact and readable.
        retired = self.directory / 'retired_item_updates' / uuid.uuid4().hex
        with self.lock:
            if self._storage_epochs.get(path.name, 0) != epoch:
                return  # A cancellation/reselection/review superseded this copy.
            atomic_write(path, payload.encode('utf-8'))
            self._snapshot_cache[path.name] = ((path.stat().st_mtime_ns, path.stat().st_size), job, payload)
            self._storage_epochs[path.name] = epoch + 1
            source = self._overlay_dir(path)
            if source.exists():
                retired.parent.mkdir(exist_ok=True)
                try:
                    os.replace(source, retired)
                except OSError as exc:
                    diagnose(self.directory, 'storage', 'retire_compaction', exc, path.name)
                    return
        # Only this newly generated retired directory is cleaned; no historical
        # snapshots, execution_history, review claims or production paths are removed.
        if retired.exists() and retired.resolve().is_relative_to(self.directory.resolve()):
            try:
                for child in retired.iterdir():
                    child.unlink()
                retired.rmdir()
            except OSError as exc:
                diagnose(self.directory, 'storage', 'cleanup_compaction', exc, path.name)

    def _archive_item(self, job, index, item):
        if item['state'] not in {'success', 'failed', 'cancelled'}:
            return
        singleton = dict(job, rows=[job['rows'][index]], items=[item])
        record = next(self._job_attempts(singleton))
        path = self.directory / 'execution_history' / (hashlib.sha256(record['failure_id'].encode()).hexdigest()+'.json')
        if not path.exists():
            atomic_write(path, json.dumps(record, ensure_ascii=False).encode())

    def _refresh_result_index(self):
        self._result_index = {}; self._unknown_index = {}
        for path in self._paths():
            job = self._load(path)
            for index in range(len(job['items'])):
                self._index_item(path, job, index)
        self._index_ready = True

    def _index_item(self, path, job, index):
        from .registration import task_key
        row, item = job['rows'][index], job['items'][index]
        entry = (path.name, row, deepcopy(item), job['created_at'])
        identity = (path.name, item['row_id'])
        self._result_index.setdefault(task_key(row), {})[identity] = entry
        ids = row.get('gift_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(v, str) or not v for v in ids):
            self._unknown_index.setdefault(row['order_no'].strip(), {})[identity] = entry
