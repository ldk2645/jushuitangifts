import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import build_opener,ProxyHandler,Request
from urllib.error import HTTPError
from gift_registry.frontend_server import RowStore,make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.registration import RegistrationQueue
from test_task_day_export import fixture,Offline,cells
from test_verification_lane import CATALOG

class TaskLogHTTPTests(unittest.TestCase):
    def test_host_csrf_partial_final_legacy_and_stale_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);jobs=RegistrationQueue(root/'jobs',Offline());rows=RowStore(root/'rows.json')
            path=fixture(jobs,'api',3,'2026-11-09T20:00:00+08:00','2026-11-10T04:00:00+08:00',2)
            rows.write(0,jobs._load(path)['rows']);catalog=GiftCatalogStore(root/'gifts.json');catalog.path.write_text(json.dumps(CATALOG))
            server=make_server(0,store=rows,queue=jobs,catalog=catalog);threading.Thread(target=server.serve_forever,daemon=True).start()
            url=f'http://127.0.0.1:{server.server_port}';opener=build_opener(ProxyHandler({}))
            def request(path,body=None,token=None,headers=None):
                return opener.open(Request(url+path,data=None if body is None else json.dumps(body).encode(),
                    headers={**({'X-CSRF-Token':token} if token else {}),**(headers or {})}),timeout=10)
            try:
                with request('/api/config') as response:token=json.load(response)['token']
                with request('/api/registrations/task-logs') as response:self.assertEqual(json.load(response)['days'][0]['remaining'],1)
                body=dict(day='2026-11-09')
                with self.assertRaises(HTTPError) as error:request('/api/registrations/task-logs/export',body)
                self.assertEqual(error.exception.code,403)
                with self.assertRaises(HTTPError) as error:request('/api/registrations/task-logs/export',body,token)
                self.assertEqual(error.exception.code,409)
                with request('/api/registrations/task-logs/export',dict(body,partial=True),token) as response:result=json.load(response)
                with request(result['download_url']) as response:self.assertEqual(len(cells(response.read()))-1,3)
                state=jobs.states()['api-0'];target=dict(row_id='api-0',task_id=state['task_id'],execution_id=state['execution_id'],row_signature=state['signature'],state_version=-1)
                with self.assertRaises(HTTPError) as error:request('/api/rows/status',dict(revision=1,draft_revision=1,row_ids=['api-0'],targets=[target],status='failed'),token)
                self.assertEqual(error.exception.code,409);self.assertFalse(rows.path.with_name('manual_status.json').exists())
                with request('/api/registrations/export-today') as response:self.assertIn('spreadsheetml',response.headers['Content-Type'])
                with self.assertRaises(HTTPError) as error:request('/api/registrations/task-logs',headers={'Host':'evil.example'})
                self.assertEqual(error.exception.code,403)
                job=jobs._load(path);job['items'][0]['detail']='changed'
                with jobs.lock:jobs._save(path,job)
                with self.assertRaises(HTTPError) as error:request(result['download_url'])
                self.assertEqual(error.exception.code,409)
            finally:server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()
