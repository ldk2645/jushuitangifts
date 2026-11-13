"""Numeric picker search and per-group frequency, isolated HTTP and fake jobs."""
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
from fakes import FakeOperations


def main():
    with tempfile.TemporaryDirectory(prefix='gift-search-', dir=ROOT/'.tmp') as directory:
        folder = Path(directory)
        catalog = [
            {'id': '001-Ab/7', 'name': '小米平板7钢化膜', 'aliases': ['7代贴膜']},
            {'id': '002-Bb/8', 'name': '小米平板8保护壳', 'aliases': ['80瓦套装']},
            {'id': '003-Cc/9', 'name': '小米手环9'},
            {'id': '004-Dd/0', 'name': '共享礼品'},
            {'id': '005-Uu/0', 'name': '未分组礼品'},
        ]
        groups = {'version': 1, 'revision': 1,
                  'groups': [{'id': 'a', 'name': '甲组'}, {'id': 'b', 'name': '乙组'}, {'id': 'empty', 'name': '空组'}],
                  'memberships': {'001-Ab/7': ['a'], '002-Bb/8': ['a'], '003-Cc/9': ['b'], '004-Dd/0': ['a', 'b']}}
        path = folder/'gifts.json'; path.write_text(json.dumps(catalog), encoding='utf-8')
        group_path = folder/'gift_groups.local.json'; group_path.write_text(json.dumps(groups), encoding='utf-8')
        store = RowStore(folder/'rows.json')
        def row(identifier, order, codes):
            return dict(id=identifier, order_no=order, gift_ids=codes,
                        gift='、'.join(g['name'] for g in catalog if g['id'] in codes), change_address=False)
        records = [row('edit', '', []), row('a-old', 'order-a-old', ['002-Bb/8']),
                   row('a-new', 'order-a-new', ['002-Bb/8']),
                   row('a-duplicate', 'order-a-new', ['002-Bb/8']),
                   row('a-first', 'order-a-first', ['001-Ab/7']),
                   row('shared', 'order-shared', ['004-Dd/0'])]
        # More than 100 unrelated orders must not evict group A's relevant history.
        records += [row('b-'+str(i), 'order-b-'+str(i), ['003-Cc/9']) for i in range(105)]
        records += [row('ungrouped', 'order-ungrouped', ['005-Uu/0'])]
        store.write(0, records)
        catalog_store = GiftCatalogStore(path)
        jobs = RegistrationQueue(folder/'jobs', operations=FakeOperations())
        server = make_server(0, store=store, queue=jobs, catalog=catalog_store,
                             groups=GiftGroupStore(group_path, catalog_store))
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        driver = None
        try:
            url = f'http://127.0.0.1:{server.server_port}/'
            driver = create_driver(Site('gift-search', '礼品搜索隔离验收', url, ('127.0.0.1',)),
                                   headless=True, profile=folder/'browser')
            driver.set_window_size(1500, 1000); driver.get(url)
            js = driver.execute_script
            WebDriverWait(driver, 15).until(lambda d: d.execute_script('return initialized'))
            def choose(group, picker=False):
                Select(driver.find_element(By.ID, 'gift-group-filter' if picker else 'toolbar-gift-group')).select_by_value(group)
            def options():
                return [e.get_attribute('value') for e in driver.find_elements(By.CSS_SELECTOR, '#gift-options input')]
            def search(value):
                field = driver.find_element(By.ID, 'gift-search'); field.clear(); field.send_keys(value)
            choose('a')
            assert js('return recentGiftUsage().orderCount') == 4
            assert js('return [...recentGiftUsage().counts]') == [['004-Dd/0', 1], ['001-Ab/7', 1], ['002-Bb/8', 2]]
            driver.find_element(By.CSS_SELECTOR, 'tr[data-id="edit"] .gift-trigger').click()
            assert options() == ['002-Bb/8', '001-Ab/7', '004-Dd/0']
            assert '甲组' in driver.find_element(By.ID, 'gift-sort-hint').text, js("return {hint:$('#gift-sort-hint').textContent,hidden:$('#gift-picker').hidden,pickerRowId,selectedGiftGroup}")
            search('7'); assert options() == ['001-Ab/7']
            search('80'); assert options() == ['002-Bb/8']
            search('001'); assert options() == ['001-Ab/7']
            search('Ab/7'); assert options() == ['001-Ab/7']
            search('001-Ab/7'); assert options() == ['001-Ab/7']
            search('003-Cc/9'); assert options() == []  # Exact out-of-group code cannot fall back.
            search('001-Ab/6'); assert options() == []  # No edit-distance correction of codes.
            assert js("return rows.find(r=>r.id==='edit').gift_ids") == []
            for query in ['7', '001', '001-Ab', '001-ab/7']:
                assert js('return matchGifts(arguments[0]).ids', query) == [], query
            assert js("return matchGifts('001-Ab/7').ids") == ['001-Ab/7']
            print('PASS: numeric model/alias/code-fragment search; exact scope; no implicit selection or partial-code paste.', flush=True)
            search('7')
            driver.find_element(By.CSS_SELECTOR, '#gift-options input').click()
            WebDriverWait(driver, 12).until(lambda d: d.execute_script('return version===savedVersion && !saving'))
            assert js("return rows.find(r=>r.id==='edit').gift_ids") == ['001-Ab/7']
            search(' ')
            choose('b', picker=True)
            assert options() == ['003-Cc/9', '004-Dd/0']
            assert js('return pickerGiftUsage.orderCount') == 100
            assert js('return [...pickerGiftUsage.counts]') == [['003-Cc/9', 100]]
            assert js("return rows.find(r=>r.id==='edit').gift_ids") == ['001-Ab/7']
            choose('a', picker=True)
            assert options() == ['002-Bb/8', '001-Ab/7', '004-Dd/0']
            assert js('return pickerGiftUsage.orderCount') == 4
            choose('__ungrouped', picker=True)
            assert options() == ['005-Uu/0'] and js('return pickerGiftUsage.orderCount') == 1
            choose('empty', picker=True)
            assert options() == [] and js('return pickerGiftUsage.orderCount') == 0
            choose('', picker=True)
            assert set(options()) == {g['id'] for g in catalog}
            assert js('return pickerGiftUsage.orderCount') == 100
            js("acceptCatalogContext({catalog_version:'changed',gift_groups:giftGroups});renderGiftOptions()")
            assert options() == [] and js('return pickerGiftUsage') is None
            print('PASS: group-only recent 100 orders; duplicate deduplication; shared/ungrouped/empty/all groups; open-picker switching and stale-scope invalidation.', flush=True)
            assert not list((folder/'jobs').glob('*.json')), 'No registration should run'
        finally:
            if driver:
                quit_owned_driver(driver)
            server.shutdown(); server.server_close(); worker.join(timeout=3)


if __name__ == '__main__':
    main()
