"""Browser startup with a slow config endpoint and synthetic drafts; no platform."""
import json
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selenium.webdriver.support.ui import WebDriverWait
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.frontend_server import LocalHTTPServer


def main():
    rows_requested = threading.Event()
    concurrent = []
    row_count = 0
    config = dict(token='synthetic', gifts=[dict(id='SKU', name='Synthetic gift')],
                  frontend_version=11, catalog_version='synthetic', host_actions_available=False,
                  gift_groups=dict(revision=0, groups=[], memberships={}), platform_login_available=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            content_type = 'application/json'
            if self.path == '/api/config':
                # Old sequential loading cannot request rows until this completes.
                concurrent.append(rows_requested.wait(3))
                data = json.dumps(config).encode()
            elif self.path == '/api/rows':
                rows_requested.set()
                rows = [dict(id=f'row-{i}', order_no=f'SYN-{i}', gift='Synthetic gift',
                             gift_ids=['SKU'], gift_unmatched=[], change_address=False) for i in range(row_count)]
                data = json.dumps(dict(revision=1, rows=rows, row_states={})).encode()
            elif self.path == '/api/registrations/status':
                data = json.dumps(dict(row_states={}, draft_revision=1, workflow_configured=False)).encode()
            elif self.path in ('/', '/app.js', '/styles.css', '/favicon.svg'):
                filename = 'index.html' if self.path == '/' else self.path[1:]
                data = (ROOT/'frontend'/filename).read_bytes()
                content_type = {'index.html':'text/html', 'app.js':'text/javascript',
                                'styles.css':'text/css', 'favicon.svg':'image/svg+xml'}[filename]
            else:
                self.send_error(404); return
            self.send_response(200)
            self.send_header('Content-Type',content_type+'; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data)

    server = LocalHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='sheet-startup-') as folder, ExitStack() as cleanup:
            url=f'http://127.0.0.1:{server.server_port}/'
            driver=create_driver(Site('startup-test','Local synthetic sheet',url,('127.0.0.1',)),
                                 headless=True,profile=Path(folder)/'profile')
            cleanup.callback(quit_owned_driver, driver)
            metrics=[]
            for count in (0,1000):
                row_count=count; rows_requested.clear()
                started=time.perf_counter(); driver.get(url)
                WebDriverWait(driver,20).until(lambda d:d.execute_script('return initialized'))
                assert concurrent[-1], 'Config and draft must load concurrently'
                assert driver.execute_script('return rows.length') == (count or 18)
                assert driver.execute_script('return document.querySelector("#load-error").hidden')
                if count:
                    assert driver.execute_script('return rows.every(r=>validGifts(r))')
                metrics.append(dict(rows=count,ready_ms=round((time.perf_counter()-started)*1000,1)))
            print(json.dumps(dict(concurrent_requests=True,metrics=metrics)))
    finally:
        server.shutdown(); server.server_close(); thread.join(3)


if __name__ == '__main__': main()
