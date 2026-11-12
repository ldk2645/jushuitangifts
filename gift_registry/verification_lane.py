"""Durable, bounded, fair read-only work lane. Disk is the queue of record."""
import hashlib
import json
import threading
import time
from copy import deepcopy
from dataclasses import asdict

from .diagnostics import diagnose
from .sessions import atomic_write
from .workflow import (GiftRecord, OrderSnapshot, SubmissionReceipt, request_from_row,
                       verify_new_records)


class VerificationLane:
    LIMIT = 128

    def __init__(self, jobs, operations):
        self.jobs, self.operations = jobs, operations
        self.directory = jobs.directory / 'verification_tasks'
        self.condition = threading.Condition()
        self.thread = None
        self.stopping = False
        self.active_order = None
        self.completed = 0
        self.elapsed = self.max_wait = 0.0
        self.burst = 0
        self.close_error = None
        self.busy = False
        self.active = {}  # Only unfinished initial tasks, bounded by LIMIT.

    def records(self):
        result=[]
        for path in sorted(self.directory.glob('*.json')):
            record=json.loads(path.read_text(encoding='utf-8'))
            name=record.get('path')
            if (not isinstance(name,str) or '/' in name or '\\' in name or not name.endswith('.json')
                    or name in {'.json','..json'} or type(record.get('index')) is not int or record['index']<0
                    or type(record.get('state_version')) is not int):
                raise ValueError('核验交接记录结构不一致，保留原文件')
            result.append((path,record))
        return result

    def backlog(self):
        with self.condition:
            return len(self.active)

    def wait_capacity(self):
        # Called before taking a baseline, without jobs.lock. Disk is bounded by
        # backpressure; the process holds no unbounded in-memory work queue.
        while self.backlog() >= self.LIMIT:
            if self.close_error:
                raise RuntimeError('核验通道不可用；未领取新登记')
            with self.condition:
                self.condition.wait(.1)

    def handoff(self, path, job, index, row, request, before, receipt):
        from .registration import now, row_signature, task_key
        with self.jobs.lock:
            item = self.jobs._current_item(path, index)
            stamp = now()
            item.update(state='verification_pending', detail='已提交，等待独立只读核验',
                        submission_status='submitted', submitted_at=stamp, updated_at=stamp)
            item.setdefault('evidence', {}).update(before_snapshot=asdict(before), receipt=asdict(receipt))
            record = dict(batch_id=job['id'], task_id=job['id'], path=path.name, index=index,
                          row_id=row['id'], row_signature=row_signature(row), task_key=task_key(row),
                          execution_id=item['execution_id'], order_no=request.order_no,
                          gift_ids=list(request.gift_ids), quantities=[1]*len(request.gift_ids),
                          before_snapshot=asdict(before), receipt=asdict(receipt),
                          submit_attempted=item['submit_attempted'], submit_clicked=item['evidence'].get('submit_clicked', False),
                          created_at=stamp, due_at=stamp, status='ready', kind='initial',
                          state_version=item.get('state_version', 0)+1, item=deepcopy(item))
            destination = self.directory / (hashlib.sha256(item['execution_id'].encode()).hexdigest()+'.json')
            # This atomic record is sufficient to repair a crash before the item
            # projection is written. No network call precedes durable evidence.
            self.write(destination, record)
            self.jobs._save_item(path, job, index, item)
        self.wake()

    def write(self, path, record):
        atomic_write(path, json.dumps(record, ensure_ascii=False).encode('utf-8'))
        with self.condition:
            if record['status'] in {'ready','running'}:
                self.active[path] = deepcopy(record)
            else:
                self.active.pop(path,None)

    def recover(self):
        from .registration import now
        for path, record in self.records():
            job_path = self.jobs.directory / record['path']
            job = self.jobs._load(job_path)
            item = job['items'][record['index']]
            if item.get('execution_id') != record['execution_id']:
                record.update(status='interrupted', detail='执行身份已变化')
            elif item.get('initial_verification_result'):
                record.update(status='done')
            elif record['status'] == 'running':
                item.update(state='manual_required', detail='首次核验中断；未重复领取或提交', updated_at=now(),
                            first_terminal_at=now())
                self.jobs._save_item(job_path, job, record['index'], item)
                record.update(status='interrupted')
            elif record['status'] == 'ready' and item.get('state_version', 0) == record['state_version']-1:
                self.jobs._save_item(job_path, job, record['index'], deepcopy(record['item']))
            self.write(path, record)

    def wake(self):
        with self.condition:
            if self.thread is None:
                self.thread = threading.Thread(target=self.run, name='verification-worker', daemon=True)
                self.thread.start()
            self.condition.notify_all()

    def choose(self):
        initial = next(((p,r) for p,r in sorted(self.active.items(), key=lambda pair:pair[1]['created_at'])
                        if r['status']=='ready'),None)
        delayed = next((p for p in sorted((self.jobs.directory / 'auto_reviews').glob('*.json'))
                        if json.loads(p.read_text(encoding='utf-8')).get('status') == 'queued'), None)
        if delayed and (not initial or self.burst >= 3):
            self.burst = 0
            return 'delayed', delayed
        if initial:
            self.burst += 1
            return 'initial', initial
        return None

    def run(self):
        try:
            while True:
                with self.condition:
                    work = self.choose()
                    if work is None:
                        if self.stopping:
                            return
                        self.condition.wait(.2)
                        continue
                    self.busy = True
                started = time.monotonic()
                try:
                    if work[0] == 'initial':
                        self.verify(*work[1])
                    else:
                        self.jobs._review_once(work[1], operations=self.operations)
                except Exception as exc:
                    diagnose(self.jobs.directory, 'verification', 'worker', exc)
                    # A failed disk write must not turn into a hot retry loop.
                    # Leave the durable running claim for restart/manual repair.
                    self.close_error = type(exc).__name__
                    return
                finally:
                    self.active_order = None
                    self.elapsed += time.monotonic()-started
                    self.completed += 1
                    with self.condition:
                        self.busy = False
                        self.condition.notify_all()
        finally:
            close = getattr(self.operations, 'close', None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    self.close_error = type(exc).__name__
                    diagnose(self.jobs.directory, 'verification', 'close', exc)

    def verify(self, path, record):
        from .registration import now, row_signature, task_key
        job_path, index = self.jobs.directory / record['path'], record['index']
        with self.jobs.lock:
            # Read just this overlay. Re-reading all 1000 overlays for each
            # completion would make handoff quadratic and hold the shared lock.
            base = self.jobs._snapshot_cache.get(job_path.name)
            if base is None:
                self.jobs._read_snapshot(job_path)
                base = self.jobs._snapshot_cache[job_path.name]
            job = dict(base[1], items=list(base[1]['items']))
            row, item = job['rows'][index], self.jobs._current_item(job_path,index)
            identity = (item.get('execution_id'), row_signature(row), task_key(row), item.get('state_version', 0))
            expected = (record['execution_id'], record['row_signature'], record['task_key'], record['state_version'])
            if identity != expected or item['state'] != 'verification_pending':
                record.update(status='interrupted', detail='任务版本已变化，未核验')
                self.write(path, record)
                return
            record.update(status='running', started_at=now())
            self.write(path, record)
            self.active_order = record['order_no']
            item.update(state='verifying', detail='独立浏览器正在核验', updated_at=now())
            self.jobs._save_item(job_path, job, index, item)
            expected_version = item['state_version']
        from datetime import datetime
        self.max_wait = max(self.max_wait, (datetime.fromisoformat(now())-datetime.fromisoformat(record['created_at'])).total_seconds())
        identifiers = []
        try:
            request = request_from_row(row, job['gift_catalog'])
            raw = record['before_snapshot']
            before = OrderSnapshot(raw['order_no'], raw['found'], tuple(GiftRecord(**r) for r in raw['records']))
            receipt = SubmissionReceipt(**record['receipt'])
            if not receipt.accepted or not (receipt.reference.strip() or receipt.created_record_ids):
                raise ValueError('missing_receipt_binding')
            wait = getattr(self.operations, 'wait_for_verification', None)
            after = wait(request, before, receipt) if callable(wait) else self.operations.query_order(request.order_no)
            success, detail, identifiers = verify_new_records(request, before, after, receipt)
            state = 'success' if success else 'failed'
        except Exception as exc:
            state, detail = 'manual_required', '独立只读核验未完成，需人工核查；未重新提交'
            diagnose(self.jobs.directory, 'verification', 'query', exc, record['execution_id'])
        with self.jobs.lock:
            current = self.jobs._current_item(job_path, index)
            if (current.get('execution_id') != record['execution_id'] or current.get('state_version') != expected_version
                    or row_signature(self.jobs._snapshot_cache[job_path.name][1]['rows'][index]) != record['row_signature']):
                record.update(status='interrupted', detail='结果已过期，未覆盖当前状态')
            else:
                stamp = now()
                current.update(state=state, detail=detail, updated_at=stamp, verified_at=stamp,
                               completed_at=stamp, first_terminal_at=stamp,
                               initial_verification_result={'state':state, 'at':stamp})
                current.setdefault('evidence', {})['verified_record_ids'] = identifiers
                current['events'].append({'state':state, 'at':stamp, 'detail':detail})
                if state == 'failed':
                    self.jobs._schedule_review(current, row)
                self.jobs._save_item(job_path, job, index, current)
                record.update(status='done', completed_at=stamp, result=state)
            self.write(path, record)

    def join(self):
        while True:
            with self.condition:
                if self.close_error:
                    raise RuntimeError('核验通道已停止，请保留记录并重新启动核对')
                if self.choose() is None and not self.busy:
                    return
                self.condition.wait(.1)

    def close(self):
        self.join()
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        if self.thread:
            self.thread.join(60)
            if self.thread.is_alive() or self.close_error:
                raise RuntimeError('核验通道关闭未完成，保留目录锁')
