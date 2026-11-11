import hashlib
import json
import tempfile
import threading
import http.client
import time
import urllib.request
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from fakes import FakeOperations
from gift_registry.registration import RegistrationQueue
from test_core_safety import CATALOG, row
from gift_registry.frontend_server import make_server, RowStore
from gift_registry.gift_catalog import GiftCatalogStore


class RetryOps(FakeOperations):
    def check_retry_safety(self, request, before, evidence): return True


class BatchSafetyTests(unittest.TestCase):
    def test_compaction_cannot_overwrite_concurrent_update(self):
        with tempfile.TemporaryDirectory() as folder:
            ops=RetryOps(); ops.configured=False
            with RegistrationQueue(folder,operations=ops) as jobs:
                jobs.start([row()],CATALOG); path=jobs._paths()[0]; job=jobs._load(path)
                original=jobs._summarize_job
                def concurrent(document):
                    item=jobs._current_item(path,0); item['detail']='concurrent update'
                    with jobs.lock: jobs._save_item(path,job,0,item)
                    original(document)
                with patch.object(jobs,'_summarize_job',concurrent): jobs._compact_completed(path)
                self.assertEqual(jobs._load(path)['items'][0]['detail'],'concurrent update')
                self.assertTrue((jobs._overlay_dir(path)/'0.json').exists())

    def test_orchestration_failure_affects_only_claimed_order(self):
        with tempfile.TemporaryDirectory() as folder:
            ops=RetryOps()
            with RegistrationQueue(folder,operations=ops) as jobs:
                with patch('gift_registry.registration.Workflow.execute',side_effect=RuntimeError('injected')):
                    jobs.start([row(0),row(1),row(2)], CATALOG); jobs.pending.join()
                states=jobs.states()
                self.assertEqual(states['r0']['state'],'failed')
                self.assertEqual([states['r1']['state'],states['r2']['state']],['queued','queued'])
                self.assertEqual(ops.trace,[])
                raw=json.loads(jobs._paths()[0].read_text(encoding='utf-8'))
                self.assertEqual(raw['items'][1]['submission_status'],'snapshot_guard')
                self.assertTrue(jobs._retry_reason(dict(raw['items'][1],state='failed')))
            with RegistrationQueue(folder,operations=ops) as jobs:
                self.assertEqual(jobs.states()['r1']['state'],'waiting_for_workflow')
                self.assertEqual(ops.trace,[])

    def test_http_lost_response_repeated_request_is_same_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); ops=RetryOps({'SYNTHETIC-0':'query_error'})
            jobs=RegistrationQueue(root/'jobs',operations=ops)
            rows=RowStore(root/'rows.json'); rows.write(0,[row()])
            catalog=GiftCatalogStore(root/'gifts.json'); catalog.path.write_text(json.dumps(CATALOG))
            jobs.start([row()], CATALOG); jobs.pending.join(); ops.modes.clear()
            identity=jobs.retry_candidates()['failures'][0]['failure_id']
            server=make_server(0,store=rows,queue=jobs,catalog=catalog)
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            try:
                base=f'http://127.0.0.1:{server.server_port}'
                token=json.load(urllib.request.urlopen(base+'/api/config'))['token']
                payload=json.dumps({'failure_ids':[identity]}).encode()
                headers={'X-CSRF-Token':token,'Content-Type':'application/json'}
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
                connection.request('POST','/api/registrations/retry-all',payload,headers)
                connection.close()  # Response is deliberately lost; backend may continue.
                deadline=time.monotonic()+5
                while not list((root/'jobs/retry_batches').glob('*.json')) and time.monotonic()<deadline:
                    time.sleep(.005)
                request=urllib.request.Request(base+'/api/registrations/retry-all',payload,headers)
                result=json.load(urllib.request.urlopen(request))
                jobs.pending.join()
                status=json.load(urllib.request.urlopen(base+'/api/registrations/batches/'+result['batch_id']))
                self.assertEqual(status['results'][0]['execution_state'],'success')
                self.assertEqual(len(ops.submissions),1)
                self.assertEqual(len(list((root/'jobs/retry_batches').glob('*.json'))),1)
            finally:
                jobs.pending.join(); server.shutdown(); server.server_close(); thread.join()

    def test_durable_acceptance_partial_rejection_and_response_loss(self):
        with tempfile.TemporaryDirectory() as folder:
            ops = RetryOps({'SYNTHETIC-0':'query_error'})
            with RegistrationQueue(folder, operations=ops) as jobs:
                jobs.start([row()], CATALOG); jobs.pending.join()
                identity = jobs.retry_candidates()['failures'][0]['failure_id']
                ops.modes.clear()
                with patch.object(jobs, '_dispatch_batch'):
                    receipt = jobs.accept_retry_batch([identity, 'unknown'], {identity})
                self.assertEqual(receipt['accepted_count'], 1)
                self.assertEqual(receipt['rejected_count'], 1)
                self.assertEqual(ops.submissions, [])
                self.assertEqual(jobs.accept_retry_batch(['unknown', identity], set())['batch_id'], receipt['batch_id'])
            # Restart resumes only the persisted, explicitly authorized request.
            with RegistrationQueue(folder, operations=ops) as jobs:
                jobs.pending.join()
                status = jobs.retry_batch_status(receipt['batch_id'])
                self.assertEqual(status['status'], 'dispatched')
                self.assertEqual(status['results'][0]['execution_state'], 'success')
                self.assertEqual(len(ops.submissions), 1)
                jobs.accept_retry_batch([identity, 'unknown'], {identity}); jobs.pending.join()
                self.assertEqual(len(ops.submissions), 1)

    def test_unknown_submission_batch_rejects_without_resubmit(self):
        with tempfile.TemporaryDirectory() as folder:
            ops = RetryOps({'SYNTHETIC-0':'submit_timeout'})
            with RegistrationQueue(folder, operations=ops) as jobs:
                jobs.start([row()], CATALOG); jobs.pending.join()
                identity = jobs.retry_candidates()['failures'][0]['failure_id']
                receipt = jobs.accept_retry_batch([identity], {identity}); jobs.pending.join()
                self.assertEqual(jobs.retry_batch_status(receipt['batch_id'])['results'][0]['state'], 'rejected')
                self.assertEqual(len(ops.submissions), 1)

    def test_due_review_runs_between_orders_on_same_thread(self):
        with tempfile.TemporaryDirectory() as folder:
            ops = RetryOps({'SYNTHETIC-0':'no_new'})
            with RegistrationQueue(folder, operations=ops) as jobs:
                jobs.start([row()], CATALOG); jobs.pending.join(); ops.modes.clear()
                entered, release = threading.Event(), threading.Event()
                query = ops.query_order; threads = []
                def blocking(order):
                    threads.append(threading.get_ident())
                    if order == 'SYNTHETIC-1' and not entered.is_set():
                        entered.set(); self.assertTrue(release.wait(5))
                    return query(order)
                ops.query_order = blocking
                jobs.start([row(1), row(2)], CATALOG); self.assertTrue(entered.wait(5))
                self.assertEqual(jobs.enqueue_due_reviews(datetime.now().astimezone()+timedelta(minutes=5)), 1)
                release.set(); jobs.pending.join()
                self.assertEqual(len(set(threads)), 1)
                trace = ops.trace
                last_review = max(i for i, value in enumerate(trace) if value == ('query','SYNTHETIC-0'))
                second = next(i for i, value in enumerate(trace) if value == ('query','SYNTHETIC-2'))
                self.assertLess(last_review, second)
                self.assertEqual(jobs.states()['r0']['state'], 'success')

    def test_result_committed_before_claim_done_reconciles_without_query(self):
        with tempfile.TemporaryDirectory() as folder:
            ops = RetryOps({'SYNTHETIC-0':'no_new'})
            with RegistrationQueue(folder, operations=ops) as jobs:
                jobs.start([row()], CATALOG); jobs.pending.join(); ops.modes.clear()
                identity = jobs._latest_attempts()[0]['failure_id']
                path = Path(folder)/'auto_reviews'/(hashlib.sha256(identity.encode()).hexdigest()+'.json')
                path.parent.mkdir(); path.write_text(json.dumps({'execution_id':identity,'status':'queued'}))
                import gift_registry.registration as module
                write = module.atomic_write
                def crash(destination, content):
                    if destination == path and json.loads(content)['status'] == 'done': raise OSError('injected')
                    return write(destination, content)
                with patch.object(module, 'atomic_write', crash):
                    with self.assertRaises(OSError): jobs._review_once(path)
                self.assertEqual(jobs.states()['r0']['state'], 'success')
            count = len(ops.trace)
            with RegistrationQueue(folder, operations=ops) as jobs:
                self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['status'], 'done')
                self.assertEqual(len(ops.trace), count)

    def test_overlay_mismatch_and_compaction_crash_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            ops = RetryOps(); ops.configured = False
            with RegistrationQueue(folder, operations=ops) as jobs:
                jobs.start([row()], CATALOG); path = jobs._paths()[0]; job = jobs._load(path)
                item = job['items'][0]; item.update(state='submitting', submit_attempted=True, attempts=1)
                jobs._save_item(path, job, 0, item)
                jobs._snapshot_cache.clear()
                self.assertTrue(jobs._load(path)['items'][0]['submit_attempted'])
                update_path = jobs._overlay_dir(path)/'0.json'
                saved = update_path.read_bytes()
                # Crash after compacted snapshot replace but before old overlay cleanup.
                with patch.object(Path, 'unlink', side_effect=OSError('injected')):
                    with self.assertRaises(OSError): jobs._save(path, jobs._load(path))
                jobs._snapshot_cache.clear()
                self.assertTrue(jobs._load(path)['items'][0]['submit_attempted'])
                update = json.loads(saved); update['generation'] = 'unrelated'
                update_path.write_text(json.dumps(update), encoding='utf-8')
                with self.assertRaises(ValueError): jobs._load(path)
                self.assertEqual(ops.trace, [])


if __name__ == '__main__': unittest.main()
