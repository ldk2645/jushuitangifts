import tempfile
import threading
import unittest
from pathlib import Path
from gift_registry.frontend_server import RowStore
from gift_registry.host_management import HostManagement, StatusConflict
from gift_registry.registration import RegistrationQueue
from test_verification_lane import Main, CATALOG, row

def target(state, identifier='0'):
    return dict(row_id=identifier, task_id=state['task_id'], execution_id=state['execution_id'],
                row_signature=state['signature'], state_version=state['state_version'])

class PerOrderStatusTests(unittest.TestCase):
    def test_hundred_rows_a_terminal_b_submitting_c_queued(self):
        with tempfile.TemporaryDirectory() as folder:
            main=Main(); gate=threading.Event(); entered=threading.Event(); original=main.submit_registration_tracked
            def submit(request, before, callback):
                if request.order_no=='SYNTHETIC-1':
                    entered.set(); gate.wait(10)
                return original(request,before,callback)
            main.submit_registration_tracked=submit
            jobs=RegistrationQueue(Path(folder)/'jobs',main)
            rows=RowStore(Path(folder)/'rows.json'); records=[row(i) for i in range(100)]; rows.write(0,records)
            manager=HostManagement(rows,jobs)
            try:
                jobs.start(records,CATALOG); self.assertTrue(entered.wait(5))
                states=manager.states(); self.assertTrue(states['0']['manual_editable'])
                self.assertFalse(states['1']['manual_editable']); self.assertFalse(states['2']['manual_editable'])
                before=jobs.states()['0']
                manager.mark(['0'],'failed',targets=[target(before)],draft_revision=1)
                self.assertEqual(jobs.states()['0'],before)
                self.assertEqual(manager.states()['0']['manual_status'],'failed')
                for i in ('1','2'):
                    with self.assertRaises(StatusConflict): manager.mark([i],'success',targets=[target(states[i],i)],draft_revision=1)
                stale=target(before); stale['state_version']-=1
                with self.assertRaises(StatusConflict): manager.mark(['0'],'pending',targets=[stale],draft_revision=1)
                with self.assertRaises(StatusConflict): manager.mark(['0','2'],'pending')
                self.assertEqual(manager.states()['0']['manual_status'],'failed')
            finally:
                gate.set(); jobs.pending.join(); jobs.close()
            self.assertEqual(len(main.submitted),100)

if __name__=='__main__': unittest.main()
