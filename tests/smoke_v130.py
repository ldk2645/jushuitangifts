"""v1.3 isolated Selenium acceptance: two insecure HTTP clients, synthetic data only."""
import base64
import json
import sys
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.gift_groups import GiftGroupStore
from gift_registry.network import NetworkSettings
from gift_registry.registration import RegistrationQueue
from fakes import FakeOperations


def main():
    output = ROOT / '.tmp/ui-checks'; output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='v130-ui-', dir=ROOT / '.tmp') as directory:
        folder = Path(directory)
        catalog = [{'id': 'SYN-A', 'name': '合成同名'}, {'id': 'SYN-B', 'name': '合成同名'},
                   {'id': 'SYN-C', 'name': '合成共享礼品'}]
        groups = {'version': 1, 'revision': 1, 'groups': [{'id': 'a', 'name': '合成甲组'}, {'id': 'b', 'name': '合成乙组'},
                 {'id': 'empty', 'name': '合成空组'}], 'memberships': {'SYN-A': ['a'], 'SYN-B': ['b'], 'SYN-C': ['a', 'b']}}
        path = folder / 'gifts.json'; path.write_text(json.dumps(catalog), encoding='utf-8')
        group_path = folder / 'gift_groups.local.json'; group_path.write_text(json.dumps(groups), encoding='utf-8')
        catalog_store = GiftCatalogStore(path); group_store = GiftGroupStore(group_path, catalog_store)
        store = RowStore(folder / 'rows.json'); ops = FakeOperations()
        jobs = RegistrationQueue(folder / 'jobs', operations=ops)
        original = {'id': 'old-success', 'order_no': 'SYN-OLD', 'gift': '合成同名', 'gift_ids': ['SYN-A'], 'change_address': False}
        jobs.start([original], catalog); jobs.pending.join()
        unsupported = {**original, 'id': 'old-unsupported', 'order_no': 'SYN-ADDRESS', 'change_address': True,
                       'recipient_name': 'SYN-PRIVATE-NAME', 'phone': 'SYN-PRIVATE-PHONE', 'address': 'SYN-PRIVATE-ADDRESS'}
        store.write(0, [original, unsupported])
        network = NetworkSettings(True, '192.168.1.91', '192.168.1.0/24', 'v130.example.test')
        server = make_server(18765, store=store, queue=jobs, catalog=catalog_store, groups=group_store,
                             network=network, access_password='000042')
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        drivers = []
        try:
            site = Site('v130-ui', '隔离普通 HTTP 验收', f'http://{network.domain}:{server.server_port}/', (network.domain,))
            for index in (1, 2):
                options = webdriver.ChromeOptions()
                options.add_argument('--host-resolver-rules=MAP v130.example.test 127.0.0.1')
                options.add_argument('--no-proxy-server')
                with patch('gift_registry.browser.webdriver.ChromeOptions', return_value=options):
                    driver = create_driver(site, headless=True, profile=folder / f'client-{index}')
                drivers.append(driver); driver.set_window_size(1600, 1000)
                driver.execute_cdp_cmd('Network.enable', {})
                driver.execute_cdp_cmd('Network.setExtraHTTPHeaders', {'headers': {
                    'Authorization': 'Basic ' + base64.b64encode(b'gift:000042').decode()}})
                driver.get(site.url)
                WebDriverWait(driver, 15).until(lambda d: d.execute_script('return initialized'))
                assert driver.execute_script('return isSecureContext') is False
            a, b = drivers
            def js(script, *args): return a.execute_script(script, *args)
            def wait():
                WebDriverWait(a, 12).until(lambda d: d.execute_script('return version === savedVersion && !saving'))
            def choose(driver, group): Select(driver.find_element(By.ID, 'toolbar-gift-group')).select_by_value(group)
            def call(driver, expr): return driver.execute_async_script('const done=arguments[0]; (' + expr + ').then(done).catch(e=>done({error:e.message}));')
            def refresh(driver):
                driver.refresh(); WebDriverWait(driver, 12).until(lambda d: d.execute_script('return initialized'))

            assert js('return columns()') == ['order_no', 'gift', 'status']
            assert js('return document.querySelectorAll(".address-toggle,.address-details,[data-field=recipient_name],[data-field=phone],[data-field=address]").length') == 0
            assert js('return status(rows[0]).type') == 'success'
            assert js('return status(rows[1]).label') == '旧草稿不支持'
            assert 'SYN-PRIVATE' not in a.page_source
            assert not a.find_element(By.ID, 'retry-failures').is_displayed()
            assert call(a, "api('/api/gift-groups',{method:'PUT',body:JSON.stringify({revision:1,action:'create',name:'unauthorized'})})")['error']
            print('PASS: three columns; old true draft blocked; hidden recipients absent; remote host controls denied.')

            choose(a, 'a'); choose(b, 'b')
            before = group_path.read_bytes()
            assert js("return matchGifts('SYN-B').ids") == []
            assert js("return matchGifts('合成同名').ids") == ['SYN-A']
            assert b.execute_script("return matchGifts('合成同名').ids") == ['SYN-B']
            assert js("return matchGifts('SYN-C').ids") == ['SYN-C']
            choose(a, 'empty'); assert js("return matchGifts('SYN-A').ids") == []
            choose(a, ''); assert js("return matchGifts('合成同名').ids") == []
            choose(a, 'a')
            js("addRows(1); focusCell(rows.at(-1).id,'order_no'); pasteText('SYN-NEW\\tSYN-A');")
            wait(); selected_id = js('return rows.at(-1).id')
            snapshot = store.read()['rows'][-1]['gift_selection']
            assert snapshot['gift_ids'] == ['SYN-A'] and snapshot['events'][0]['group_id'] == 'a'
            choose(a, 'b')
            assert js('return rows.at(-1).gift_ids') == ['SYN-A']
            assert js('return status(rows.at(-1)).type') == 'ready'
            assert group_path.read_bytes() == before
            assert b.execute_script('return selectedGiftGroup') == 'b'
            refresh(a); assert js('return selectedGiftGroup') == 'b'
            assert js('return rows.at(-1).gift_ids') == ['SYN-A']
            print('PASS: independent preferences, exact scope, same-name ambiguity, multi-membership, empty groups, refresh; existing selections unchanged.')

            # Existing out-of-scope selections remain visible with explicit removal.
            js("openGiftPicker(rows.at(-1),document.querySelector('tr[data-id=\"'+rows.at(-1).id+'\"] .gift-trigger'))")
            assert 'SYN-A' in a.find_element(By.ID, 'gift-options').text
            a.find_element(By.CSS_SELECTOR, '#gift-options input[value="SYN-B"]').click()
            a.find_element(By.ID, 'gift-done').click(); wait()
            assert js('return rows.at(-1).gift_ids') == ['SYN-A', 'SYN-B']
            # Structured copy cannot carry group A into a new row while scope B is selected.
            js("focusCell(rows[0].id,'gift'); window.copied=clipboardContent(); addRows(1); focusCell(rows.at(-1).id,'gift');")
            wait(); count = len(store.read()['rows'])
            js("const data=new DataTransfer();data.setData('text/plain',copied.text);data.setData('text/html',copied.html);document.querySelector('#registry-grid').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));")
            assert js('return rows.at(-1).gift_ids') == []
            assert '当前分组未找到' in a.find_element(By.ID, 'toast').text
            # Formula-bar replacement uses the same scoped matcher; no partial registration.
            js("activate(rows.at(-1).id,'gift');const input=document.querySelector('#formula-input');input.value='SYN-A';input.dispatchEvent(new Event('change')); ")
            wait(); assert js('return rows.at(-1).gift_ids') == []
            assert js('return validGifts(rows.at(-1))') is False
            choose(a, 'a')
            js("focusCell(rows.at(-1).id,'order_no');pasteText('SYN-PASTE-1\\tSYN-A\\nSYN-PASTE-2\\tSYN-C');")
            wait(); assert len(store.read()['rows']) == count + 1
            assert js('return rows.at(-1).gift_ids') == ['SYN-C']
            print('PASS: scoped picker keeps original codes; structured copy, formula input and expanding regional paste enforce scope.')

            # Legacy seven/four-column formats: reject true, discard false hidden fields.
            before_rows = js('return JSON.stringify(rows)')
            js("focusCell(rows.at(-1).id,'order_no');pasteText('SYN-LEGACY\\tSYN-A\\t是\\tSYN-PRIVATE-NAME\\tSYN-PRIVATE-PHONE\\tSYN-PRIVATE-ADDRESS\\t成功');")
            assert js('return JSON.stringify(rows)') == before_rows
            js("focusCell(rows.at(-1).id,'order_no');pasteText('SYN-LEGACY\\tSYN-A\\t否\\tSYN-PRIVATE-NAME\\tSYN-PRIVATE-PHONE\\tSYN-PRIVATE-ADDRESS\\t成功');")
            wait(); assert 'SYN-PRIVATE' not in store.path.read_text(encoding='utf-8')
            assert not set(store.read()['rows'][-1]) & {'recipient_name', 'phone', 'address'}
            js("focusCell(rows.at(-1).id,'gift');handleNavigation({key:'ArrowRight',preventDefault(){},shiftKey:false},rows.at(-1).id,'gift');")
            # Direct selection checks C is the status column, without obsolete fixed index 3.
            js("focusCell(rows.at(-1).id,'status')")
            assert a.find_element(By.ID, 'cell-reference').text.startswith('C')
            print('PASS: old clipboard true/false compatibility, privacy, formula/navigation status column C.')

            # Config changes invalidate ONLY subsequent selection, even when a name changes.
            catalog[0]['name'] = '合成更名后礼品'; path.write_text(json.dumps(catalog), encoding='utf-8')
            call(a, 'refreshGiftCatalog()')
            assert js('return scopeInvalid') is True
            assert js('return rows[0].gift_ids') == ['SYN-A']
            assert js('return status(rows[0]).type') == 'success'
            assert js("return matchGifts('SYN-A').ids") == []
            choose(a, 'a')
            groups['revision'] += 1
            groups['groups'] = [g for g in groups['groups'] if g['id'] != 'a']
            groups['memberships'] = {code: [g for g in members if g != 'a'] for code, members in groups['memberships'].items()}
            group_path.write_text(json.dumps(groups), encoding='utf-8')
            call(a, 'refreshGiftCatalog()')
            assert js('return scopeInvalid') is True
            assert js('return selectedGiftGroup') == 'a'
            assert js('return rows[0].gift_ids') == ['SYN-A']
            refresh(a); assert js('return scopeInvalid') is True
            choose(a, 'b')
            print('PASS: catalog rename retains success; changed/deleted group pauses new matching without fallback or clearing existing selections.')

            # Force responses to arrive in reverse order, independently of backend mutations.
            js("""
                window.originalFetch=fetch; window.originalSetTimeout=setTimeout; window.pendingPolls=[];
                window.setTimeout=(fn,ms,...args)=>fn===refreshExecutionStates?0:originalSetTimeout(fn,ms,...args);
                window.fetch=(url,opts)=>url==='/api/registrations/status'?new Promise(resolve=>pendingPolls.push(resolve)):originalFetch(url,opts);
                window.oldPoll=refreshExecutionStates();window.newPoll=refreshExecutionStates();
            """)
            js("pendingPolls[1](new Response(JSON.stringify({row_states:{[rows[0].id]:{signature:rowSignature(rows[0]),state:'success',detail:'NEW-SYNTHETIC'}},draft_revision:revision}),{status:200,headers:{'Content-Type':'application/json'}}))")
            call(a, 'newPoll')
            js("pendingPolls[0](new Response(JSON.stringify({row_states:{[rows[0].id]:{signature:rowSignature(rows[0]),state:'failed',detail:'OLD-SYNTHETIC'}},draft_revision:revision}),{status:200,headers:{'Content-Type':'application/json'}}))")
            call(a, 'oldPoll')
            assert js('return rowStates[rows[0].id].detail') == 'NEW-SYNTHETIC'
            js('window.fetch=originalFetch;window.setTimeout=originalSetTimeout;')
            call(a, 'refreshExecutionStates()')
            a.save_screenshot(str(output / 'v130-workbench.png'))
            assert len(ops.submissions) == 1  # Only explicit synthetic baseline fixture above.
            print('PASS: older poll cannot overwrite newer state; no browser-triggered business submission; screenshot saved.')
        finally:
            for driver in drivers: quit_owned_driver(driver)
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__': main()
