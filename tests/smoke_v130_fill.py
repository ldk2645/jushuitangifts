"""Exact SKU matching and real pointer fill gestures; synthetic local HTTP only."""
import json
import sys
import tempfile
import threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.frontend_server import make_server, RowStore
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.gift_groups import GiftGroupStore
from gift_registry.registration import RegistrationQueue
from fakes import FakeOperations

def main():
    with tempfile.TemporaryDirectory(prefix='fill-ui-', dir=ROOT / '.tmp') as folder:
        root = Path(folder)
        catalog = [{'id': '001-Ab/7', 'name': '合成蓝色背包', 'aliases': ['合成蓝包']},
                   {'id': '002-Bb/8', 'name': '合成绿色背包'}, {'id': '003+C', 'name': '合成水杯'}]
        groups = {'version': 1, 'revision': 1, 'groups': [{'id': 'a', 'name': '甲组'}],
                  'memberships': {'001-Ab/7': ['a']}}
        path = root / 'gifts.json'; path.write_text(json.dumps(catalog), encoding='utf-8')
        group_path = root / 'gift_groups.local.json'; group_path.write_text(json.dumps(groups), encoding='utf-8')
        store = RowStore(root / 'rows.json'); catalog_store = GiftCatalogStore(path)
        ops = FakeOperations(); jobs = RegistrationQueue(root / 'jobs', operations=ops)
        server = make_server(18766, store=store, queue=jobs, catalog=catalog_store,
                             groups=GiftGroupStore(group_path, catalog_store))
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        driver = None
        try:
            driver = create_driver(Site('fill-ui', '填充离线验收', 'http://127.0.0.1:18766', ('127.0.0.1',)),
                                   headless=True, profile=root / 'browser')
            driver.set_window_size(1550, 1100); driver.get('http://127.0.0.1:18766')
            WebDriverWait(driver, 15).until(lambda d: d.execute_script('return initialized'))
            js = driver.execute_script
            def saved():
                WebDriverWait(driver, 12).until(lambda d: d.execute_script('return version === savedVersion && !saving'))
            def reset():
                saved()
                js("""rows=Array.from({length:8},(_,i)=>({...newRow(),order_no:'0000000000000000000'+i}));
                assignGifts(rows[0],['001-Ab/7']);assignGifts(rows[1],['002-Bb/8']);changed();render();
                selectRange({id:rows[0].id,field:'gift'});""")
                saved()
            def drag_to(index):
                handle = driver.find_element(By.CSS_SELECTOR, '.fill-handle')
                target = js("return fieldElement(rows[arguments[0]].id,'gift')", index)
                ActionChains(driver).move_to_element(handle).click_and_hold().move_to_element(target).pause(.25).release().perform()
            for value in ['001-ab/7', '01-Ab/7', '001-Ab/8', '001Ab7', '001-Ab', '003+c', '003C']:
                assert js('return matchGifts(arguments[0]).ids', value) == [], value
                assert js('return giftCatalog.filter(g=>giftScore(arguments[0],g)>=.45).length', value) == 0, value
            assert js("return matchGifts('001-Ab/7').ids") == ['001-Ab/7']
            assert js("return matchGifts('003+C').ids") == ['003+C']
            assert js("return matchGifts('001-Ab/7 002-Bb/8').ids") == ['001-Ab/7', '002-Bb/8']
            assert js("return matchGifts('合成蓝包').ids") == ['001-Ab/7']
            assert js("return matchGifts('合成蓝色背').ids") == []  # Incomplete names require explicit selection.
            print('PASS: full SKU strings only; case, leading zeros, punctuation and typos never fuzzy-match; names/aliases still work.', flush=True)
            reset()
            original_ids = js('return rows.map(r=>r.id)')
            orders = js('return rows.map(r=>r.order_no)')
            drag_to(4); saved()
            assert js('return rows.slice(0,5).map(r=>r.gift_ids)') == [['001-Ab/7']]*5
            assert js('return rows.map(r=>r.id)') == original_ids
            assert js('return rows.map(r=>r.order_no)') == orders
            js('undo()'); saved(); assert js('return rows[2].gift_ids') == []
            print('PASS: native fill-handle drag copies confirmed codes and preserves IDs/orders; one undo restores all targets.', flush=True)
            js("selectRange({id:rows[0].id,field:'gift'},{id:rows[1].id,field:'gift'})")
            drag_to(6); saved()
            assert js('return rows.slice(2,7).map(r=>r.gift_ids[0])') == ['001-Ab/7','002-Bb/8','001-Ab/7','002-Bb/8','001-Ab/7']
            reset()
            js("selectRange({id:rows[0].id,field:'order_no'},{id:rows[4].id,field:'status'});focusCell(rows[0].id,'order_no',true)")
            driver.switch_to.active_element.send_keys(Keys.CONTROL, 'd'); saved()
            assert js('return rows.slice(0,5).map(r=>r.order_no)') == [orders[0]]*5
            assert js('return rows.slice(0,5).map(r=>r.gift_ids)') == [['001-Ab/7']]*5
            assert js('return rows.every(r=>!r.state && !r.recipient_name && !r.phone && !r.address)')
            reset()
            ActionChains(driver).double_click(driver.find_element(By.CSS_SELECTOR,'.fill-handle')).perform(); saved()
            assert js('return rows.map(r=>r.gift_ids)') == [['001-Ab/7']]*8
            print('PASS: multi-row repeat, Ctrl+D, adjacent-column double-click fill; no status/address copying or numeric increments.', flush=True)
            reset()
            before = js('return JSON.stringify(rows)')
            handle = driver.find_element(By.CSS_SELECTOR,'.fill-handle')
            target = js("return fieldElement(rows[5].id,'gift')")
            ActionChains(driver).move_to_element(handle).click_and_hold().move_to_element(target).pause(.15).send_keys(Keys.ESCAPE).release().perform()
            assert js('return JSON.stringify(rows)') == before
            js("selectedGiftGroup='a';scopeInvalid=false;giftMatcher=null;renderGroupFilter();selectRange({id:rows[1].id,field:'gift'})")
            before = js('return JSON.stringify(rows)')
            drag_to(5); assert js('return JSON.stringify(rows)') == before
            js("selectedGiftGroup='';scopeInvalid=true;giftMatcher=null;selectRange({id:rows[0].id,field:'gift'})")
            drag_to(5); assert js('return JSON.stringify(rows)') == before
            js("scopeInvalid=false;selectedGiftGroup='';giftMatcher=null;renderGroupFilter();selectedStatus='ready';render();")
            hidden = js('return JSON.stringify(rows.slice(2))')
            js("selectRange({id:gridRows()[0].id,field:'gift'});applyFill(fillPlan(rangeBounds()),1)"); saved()
            assert js('return rows[1].gift_ids') == ['001-Ab/7']
            assert js('return JSON.stringify(rows.slice(2))') == hidden
            js("selectedStatus='';render();rows[2].gift_unmatched=['SYN-UNKNOWN'];rows[2].gift='SYN-UNKNOWN';changed();render();")
            saved(); js("selectRange({id:rows[2].id,field:'gift'});applyFill(fillPlan(rangeBounds()),4)"); saved()
            assert js('return rows[4].gift_unmatched') == ['SYN-UNKNOWN']
            print('PASS: Esc cancels; out-of-group and invalid scopes reject the whole fill; unresolved input remains unresolved.', flush=True)
            js("selectRange({id:rows[0].id,field:'status'});focusCell(rows[0].id,'status',true)")
            driver.switch_to.active_element.send_keys(Keys.TAB)
            assert js("return active.id===rows[1].id && active.field==='order_no'")
            assert not js('return columns().includes(undefined)')
            js("selectRange({id:rows[0].id,field:'gift'});applyFill(fillPlan(rangeBounds()),10)"); saved()
            assert js('return rows.length') == 11
            assert js('return new Set(rows.map(r=>r.id)).size') == 11
            assert js("return rows.slice(8).every(r=>r.order_no==='' && r.gift_ids[0]==='001-Ab/7')")
            # Scroll during a drag previews only; cancelling leaves the draft byte-for-byte intact.
            js("addRows(35);selectRange({id:rows[0].id,field:'gift'});scrollCellIntoView(rows[0].id,'gift')")
            saved(); before = js('return JSON.stringify(rows)')
            handle = driver.find_element(By.CSS_SELECTOR,'.fill-handle')
            box = js("const r=document.querySelector('#grid-scroll').getBoundingClientRect();return {x:r.left+300,y:r.bottom-3}")
            center = js("const r=arguments[0].getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}", handle)
            ActionChains(driver).move_to_element(handle).click_and_hold().move_by_offset(int(box['x']-center['x']),int(box['y']-center['y'])).pause(1).perform()
            assert js("return document.querySelector('#grid-scroll').scrollTop") > 0
            ActionChains(driver).send_keys(Keys.ESCAPE).release().perform()
            assert js('return JSON.stringify(rows)') == before
            js("scrollCellIntoView(rows[0].id,'gift')")
            saved(); assert ops.trace == []
            js("selectedGiftGroup='';scopeInvalid=false;renderGroupFilter();selectRange({id:rows[0].id,field:'gift'})")
            (ROOT / '.tmp/ui-checks').mkdir(exist_ok=True)
            driver.save_screenshot(str(ROOT / '.tmp/ui-checks/v130-fill.png'))
            print('PASS: three-column keyboard wrapping, persisted selections and zero platform calls.', flush=True)
        finally:
            if driver: quit_owned_driver(driver)
            server.shutdown(); server.server_close(); jobs.close(); worker.join(timeout=3)

if __name__ == '__main__': main()
