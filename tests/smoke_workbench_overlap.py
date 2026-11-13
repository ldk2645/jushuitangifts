"""Synthetic browser check: overlapping starts, transient reads and a lost write reply."""
import json
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.registration import RegistrationQueue
from test_workbench_state import CATALOG, GatedOperations, row


def main():
    with tempfile.TemporaryDirectory(prefix='synthetic-overlap-') as tmp:
        folder = Path(tmp); ops = GatedOperations(); ops.modes.update({'SYN-c':'no_new', 'SYN-d':'query_error'})
        jobs = RegistrationQueue(folder/'jobs', operations=ops)
        store = RowStore(folder/'rows.json'); store.write(0, [row(), row('b')])
        catalog_path = folder/'gifts.json'; catalog_path.write_text(json.dumps(CATALOG), encoding='utf-8')
        server = make_server(0, store=store, queue=jobs, catalog=GiftCatalogStore(catalog_path))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        driver = None
        try:
            url = f'http://127.0.0.1:{server.server_port}/'
            driver = create_driver(Site('overlap-test','Synthetic overlap',url,('127.0.0.1',)),
                                   headless=True, profile=folder/'browser')
            driver.set_window_size(1440, 1000); driver.get(url)
            js = driver.execute_script; wait = WebDriverWait(driver, 15)
            wait.until(lambda d: js('return initialized'))
            js("""window.originalFetch=window.fetch; window.startPosts=0; window.statusReads=0;
                window.failReads=0; window.loseStart=false;
                window.fetch=async function(path,options) {
                  if(path==='/api/registrations/status') {
                    window.statusReads++;
                    if(window.failReads-->0) return new Response('{"error":"Synthetic read fault"}',{status:503});
                  }
                  if(path==='/api/registrations/start') {
                    window.startPosts++; const result=await window.originalFetch(path,options);
                    if(window.loseStart) {window.loseStart=false;throw new TypeError('Synthetic lost response');}
                    return result;
                  }
                  return window.originalFetch(path,options);
                };""")
            driver.find_element(By.ID, 'start-registration').click()
            assert ops.entered['SYN-a'].wait(5)
            wait.until(lambda d: not js('return starting'))
            # Immediately load the sheet and poll while the first batch is running.
            result = driver.execute_async_script("""const done=arguments[0]; Promise.all([
                api('/api/rows'),api('/api/registrations/status')]).then(done);""")
            assert all(r['row_states']['a']['state'] == 'querying' for r in result)
            js('for(const r of arguments[0]) {assignGifts(r,r.gift_ids); rows.push(r);} version++; render();', [row('c'), row('d')])
            driver.find_element(By.ID, 'start-registration').click()
            wait.until(lambda d: not js('return starting'))
            assert js("return rowStates.a.state") == 'querying', js("return {states:rowStates,posts:window.startPosts,toast:$('#toast').textContent}")
            ops.release['SYN-a'].set(); assert ops.entered['SYN-b'].wait(5)
            wait.until(lambda d: js("return rowStates.a.state==='success' && rowStates.b.state==='querying'"))
            assert driver.find_element(By.CSS_SELECTOR, 'tr[data-id="a"] .status-badge.success')
            # A failed read preserves both last known states and unsaved edits.
            js("window.failReads=2; rows[2].order_no='SYN-unsaved-edit'; version++; clearTimeout(saveTimer)")
            driver.execute_async_script('const done=arguments[0]; refreshExecutionStates(false).then(done)')
            assert js("return rowStates.a.state==='success' && rows[2].order_no==='SYN-unsaved-edit' && version!==savedVersion")
            js('window.failReads=0')
            driver.execute_async_script('const done=arguments[0]; refreshExecutionStates(false).then(done)')
            assert js("return rows[2].order_no==='SYN-unsaved-edit' && !conflict")
            js("rows[2].order_no='SYN-c';version++;window.loseStart=true;window.readsBeforeLoss=window.statusReads;render()")
            driver.find_element(By.ID, 'start-registration').click()
            wait.until(lambda d: js('return !starting && window.statusReads>window.readsBeforeLoss'))
            assert js('return window.startPosts') == 3
            ops.release_all(); jobs.pending.join()
            wait.until(lambda d: js("return status(rows[2]).type==='pending_review' && status(rows[3]).type==='failed'"))
            assert not driver.find_elements(By.CSS_SELECTOR, 'tr[data-id="c"] .row-retry')
            assert driver.find_elements(By.CSS_SELECTOR, 'tr[data-id="d"] .row-retry')
            assert js('return window.startPosts') == 3
            ops.modes.clear()
            assert jobs.enqueue_due_reviews(datetime.now().astimezone()+timedelta(minutes=5)) == 1
            jobs.pending.join()
            wait.until(lambda d: js("return status(rows[2]).type==='success'"))
            assert jobs.enqueue_due_reviews(datetime.now().astimezone()+timedelta(days=1)) == 0
            assert len(ops.submissions) == 3
            print('PASS: real start clicks, concurrent sheet/status reads, overlapping progress/success, pending vs ordinary failure, one read-only review, retained edits, and no replay after lost POST response.')
        finally:
            ops.release_all(); jobs.pending.join()
            if driver:
                quit_owned_driver(driver)
            server.shutdown(); server.server_close(); thread.join(3); jobs.close()


if __name__ == '__main__':
    main()
