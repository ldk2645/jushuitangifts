"""关闭最后一个 Chrome 窗口后重建；仅使用本地文件和隔离配置。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gift_registry.browser import SiteBrowser, create_driver
from gift_registry.business_browser import BusinessBrowsers
from gift_registry.config import ROOT, Site
from gift_registry.platform_operations import SeleniumOperations


def main():
    with tempfile.TemporaryDirectory(prefix='reopen-', dir=ROOT / '.tmp') as folder:
        base = Path(folder)
        html = base / 'page.html'
        html.write_text('<html><title>isolated reopen test</title><body>synthetic</body></html>', encoding='utf-8')
        site = Site('jushuitan', 'isolated', html.as_uri(), ())
        opened = []

        class LocalBrowser(SiteBrowser):
            def __init__(self, site, store):
                super().__init__(site, store, authenticator=False)

            def open(self):
                self.driver = create_driver(self.site, headless=True, profile=base / 'profile')
                self.driver.get(html.as_uri())
                opened.append(self)

        class LocalPage:
            def __init__(self, driver):
                self.driver = driver

            def query_order(self, order):
                assert self.driver.title == 'isolated reopen test'
                return order

        manager = BusinessBrowsers(data_dir=base, sites=(site,), browser_factory=LocalBrowser)
        operations = SeleniumOperations(browsers=manager, query_factory=LocalPage)
        try:
            assert operations.query_order('0001') == '0001'
            first = operations._query_page
            first.driver.close()  # 模拟用户关闭最后一个窗口，保留原驱动引用。
            assert operations.query_order('0002') == '0002'
            assert operations._query_page is not first
            assert operations.query_order('0003') == '0003'
            assert len(opened) == 2
            assert manager._owns_profiles
        finally:
            operations.close()
    print('PASS: last-window close recreated Chrome once, rebuilt the page adapter and then reused it. Isolated local HTML; zero real submissions.')


if __name__ == '__main__':
    main()
