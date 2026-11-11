"""Real local Chrome: large paste, cross-page selection/copy/fill/undo/filter; fake platform only."""
import argparse
import json
import sys
import tempfile
import threading
from pathlib import Path

parser=argparse.ArgumentParser(); parser.add_argument('--repo'); parser.add_argument('--baseline',action='store_true')
args=parser.parse_args(); root=Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parents[1]
sys.path[:0]=[str(root),str(root/'tests')]
from selenium.webdriver.support.ui import WebDriverWait
from gift_registry.browser import create_driver
from gift_registry.config import Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.frontend_server import RowStore,make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.registration import RegistrationQueue
from fakes import FakeOperations

def main():
    metrics=[]
    with tempfile.TemporaryDirectory(prefix='core-ui-') as folder:
        path=Path(folder); catalog=GiftCatalogStore(path/'gifts.json')
        catalog.path.write_text(json.dumps([{'id':'00001','name':'Synthetic cup','aliases':[]},
                                            {'id':'00002','name':'Synthetic bag','aliases':[]}]))
        ops=FakeOperations(); ops.configured=False
        jobs=RegistrationQueue(path/'jobs',operations=ops)
        server=make_server(0,store=RowStore(path/'rows.json'),queue=jobs,catalog=catalog)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start(); driver=None
        try:
            url=f'http://127.0.0.1:{server.server_port}'
            driver=create_driver(Site('core-ui','Synthetic local UI',url,('127.0.0.1',)),headless=True,profile=path/'profile')
            driver.set_window_size(1500,1000); driver.get(url)
            wait=WebDriverWait(driver,25); wait.until(lambda d:d.execute_script('return initialized'))
            js=driver.execute_script
            def saved(): wait.until(lambda d:d.execute_script('return version===savedVersion && !saving'))
            for size in [100,500,1000,2000]:
                saved()
                metric=js('''rows=[newRow()]; history.length=0; selectedStatus=''; selection=null; active=null; render();
                  focusCell(rows[0].id,'order_no'); const start=performance.now();
                  pasteText(Array.from({length:arguments[0]},(_,i)=>`SYNTHETIC-${i}\\t00001`).join('\\n'));
                  return {rows:rows.length,paste_ms:performance.now()-start,dom_nodes:document.querySelectorAll('*').length,
                          mounted_rows:document.querySelectorAll('#grid-body tr[data-id]').length,
                          valid:rows.every(r=>validGifts(r))};''',size)
                assert metric['rows']==size and metric['valid'],metric
                if not args.baseline: assert metric['mounted_rows']<=200,metric
                metrics.append(metric); saved()
            if not args.baseline:
                assert js('''selectRange({id:rows[0].id,field:'order_no'},{id:rows.at(-1).id,field:'gift'});
                  return clipboardContent().text.split('\\n').length''')==2000
                assert js("focusCell(rows[1200].id,'gift',true); return !!rowElement(rows[1200].id)")
                assert js('''selectRange({id:rows[199].id,field:'gift'},{id:rows[401].id,field:'gift'});
                  const match=matchGifts('00002'); assignGifts(rows[199],match.ids); fillSelectedDown();
                  return rows.slice(199,402).every(r=>r.gift_ids[0]==='00002');''')
                js('undo();'); assert js("return rows[200].gift_ids[0]")=='00001'
                js("selectedStatus='ready'; render();")
                assert js('return gridRows().length')==2000
                js("selectedStatus=''; render(); focusCell(rows[0].id,'gift'); pasteText('unknown typo');")
                assert js('return !validGifts(rows[0]) && rows[0].gift_unmatched[0] === "unknown typo"')
                js('undo();'); saved()
                # Unchanged status polls must keep mounted status nodes.
                js("window.coreNode=rowElement(rows[0].id)?.querySelector('[data-field=status]')?.firstChild")
                driver.execute_async_script('const done=arguments[0]; refreshExecutionStates().then(done)')
                assert js("return window.coreNode===rowElement(rows[0].id)?.querySelector('[data-field=status]')?.firstChild")
                assert ops.trace==[]
                destination=Path(__file__).resolve().parents[1]/'.tmp/core-ui.png'
                driver.save_screenshot(str(destination))
            print(json.dumps({'browser':driver.capabilities.get('browserVersion'),'metrics':metrics},indent=2))
            output=Path(__file__).resolve().parents[1]/f'docs/benchmark-dom-{"before" if args.baseline else "after"}.json'
            output.write_text(json.dumps(metrics,indent=2),encoding='utf-8')
        finally:
            if driver is not None: quit_owned_driver(driver)
            server.shutdown(); server.server_close(); thread.join()

if __name__=='__main__':main()
