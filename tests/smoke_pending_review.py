"""Real browser acceptance of failure presentation; synthetic platforms only."""
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.gift_groups import GiftGroupStore
from gift_registry.registration import RegistrationQueue
from test_pending_review import CATALOG, ROWS, MODES, SUBMITTED, TrackedOperations


def main():
    with tempfile.TemporaryDirectory(prefix='pending-review-', dir=ROOT/'.tmp') as directory:
        folder = Path(directory)
        path = folder/'gifts.json'; path.write_text(json.dumps(CATALOG), encoding='utf-8')
        catalog = GiftCatalogStore(path)
        groups = folder/'gift_groups.local.json'
        groups.write_text(json.dumps(dict(version=1, revision=1, groups=[], memberships={})), encoding='utf-8')
        store = RowStore(folder/'rows.json'); store.write(0, ROWS)
        ops = TrackedOperations(); jobs = RegistrationQueue(folder/'jobs', operations=ops)
        jobs.start(ROWS, CATALOG); jobs.pending.join()
        submission_count = len(ops.submissions)
        server = make_server(0, store=store, queue=jobs, catalog=catalog, groups=GiftGroupStore(groups, catalog))
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        driver = None
        try:
            url = f'http://127.0.0.1:{server.server_port}/'
            driver = create_driver(Site('pending-review-ui', '状态隔离验收', url, ('127.0.0.1',)),
                                   headless=True, profile=folder/'browser')
            driver.set_window_size(1440, 1000); driver.get(url)
            js = driver.execute_script
            WebDriverWait(driver, 15).until(lambda d: d.execute_script('return initialized'))
            driver.find_element(By.ID, 'overview-toggle').click()
            for mode in MODES:
                expected = 'success' if mode == 'success' else 'pending_review' if mode in SUBMITTED else 'failed'
                assert js('return status(rows.find(r=>r.id===arguments[0])).type', mode) == expected
                assert driver.find_element(By.CSS_SELECTOR, f'tr[data-id="{mode}"] .status-badge.{expected}')
                retry_buttons = driver.find_elements(By.CSS_SELECTOR, f'tr[data-id="{mode}"] .row-retry')
                assert len(retry_buttons) == (1 if expected == 'failed' else 0), mode
            assert js("""const row=rows.find(r=>r.id==='unknown_error'), original=rowStates[row.id].state;
                rowStates[row.id].state='manual_required';
                const manual=[status(row).type,Boolean(statusNode(row).querySelector('.row-retry'))];
                rowStates[row.id].state='verification_pending';const pending=status(row).type;
                rowStates[row.id].state=original;return [manual,pending];""") == [['pending_review', False], 'processing']
            assert driver.find_element(By.ID, 'metric-failed').text == '3', js("return {text:$('#metric-failed').textContent,metrics:getComputedStyle($('.overview-metrics')).display,states:rows.map(r=>[r.id,status(r).type])}")
            assert driver.find_element(By.ID, 'metric-pending-review').text == '4'
            assert driver.find_element(By.ID, 'metric-success').text == '1'
            def choose(value):
                Select(driver.find_element(By.ID, 'status-filter')).select_by_value(value)
            choose('pending_review')
            assert js('return displayedRows.map(r=>r.id)') == ['submit_timeout', 'no_new', 'verification_error', 'rejected']
            assert '待人工核查 4' in driver.find_element(By.ID, 'ready-summary').text
            choose('failed')
            assert js('return displayedRows.map(r=>r.id)') == ['query_error', 'form_error', 'unknown_error']
            choose('')
            js("rowStates.no_new.manual_status='pending';render()")
            assert js("return status(rows.find(r=>r.id==='no_new')).type") == 'manual_pending'
            js("delete rowStates.no_new.manual_status;render()")
            driver.find_element(By.ID, 'retry-failures').click()
            WebDriverWait(driver, 10).until(lambda d: '待人工核查 4' in d.find_element(By.ID, 'retry-summary').text)
            assert len(driver.find_elements(By.CSS_SELECTOR, '#retry-list button:not(:disabled)')) == 2
            assert js("return [...$('#retry-list').children].filter(card=>card.querySelector('p:nth-of-type(2)').textContent.startsWith('\u5f85\u4eba\u5de5\u6838\u67e5')).map(card=>!!card.querySelector('button'))") == [False] * 4
            driver.find_element(By.ID, 'retry-close').click()
            # A later successful automatic review leaves the pending filter normally.
            choose('pending_review')
            failure = next(f for f in jobs.retry_candidates()['failures'] if f['order_no'] == 'SYN-no_new')
            review_path = folder/'review.json'
            review_path.write_text(json.dumps({'execution_id': failure['failure_id']}), encoding='utf-8')
            ops.modes['SYN-no_new'] = 'success'
            jobs._review_once(review_path)
            try:
                WebDriverWait(driver, 10).until(lambda d: d.find_element(By.ID, 'metric-success').text == '2')
            except Exception:
                print(js("return {metric:$('#metric-success').textContent,open:$('#workspace-overview').open,visible:getComputedStyle($('.page-heading')).display,state:rowStates.no_new,status:status(rows.find(r=>r.id==='no_new'))}"), flush=True)
                print('Backend state:', jobs.states()['no_new'], flush=True)
                raise
            assert js('return displayedRows.map(r=>r.id)') == ['submit_timeout', 'verification_error', 'rejected']
            choose('')
            output = ROOT/'.tmp/ui-checks'; output.mkdir(exist_ok=True)
            driver.save_screenshot(str(output/'pending-review.png'))
            assert len(ops.submissions) == submission_count
            assert store.read()['revision'] == 1
            print('PASS: failure split, badges, counts, filters, manual overrides, unchanged retry eligibility, successful review refresh; no extra submissions or draft writes.', flush=True)
        finally:
            if driver:
                quit_owned_driver(driver)
            server.shutdown(); server.server_close(); worker.join(timeout=3)


if __name__ == '__main__':
    main()
