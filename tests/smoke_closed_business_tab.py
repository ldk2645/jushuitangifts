"""本地 Chrome 复现关闭当前标签但仍有存活标签的失效引用；无平台访问。"""
import sys,tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from selenium.common.exceptions import NoSuchWindowException
from gift_registry.browser import create_driver
from gift_registry.business_browser import BusinessBrowsers
from gift_registry.config import ROOT,Site
from gift_registry.workflow import PlatformOperationError
with tempfile.TemporaryDirectory(dir=ROOT/'.tmp',prefix='closed-tab-') as folder:
 driver=create_driver(Site('test','test','about:blank',()),headless=True,profile=Path(folder)/'profile')
 try:
  driver.get("about:blank")
  survivor=driver.current_window_handle
  driver.switch_to.new_window('tab');driver.close()
  assert driver.window_handles==[survivor]
  try:driver.current_url
  except NoSuchWindowException:pass
  else:raise AssertionError('closed active reference not reproduced')
  assert BusinessBrowsers._session_closed(SimpleNamespace(driver=driver)) is False
  assert driver.current_window_handle==survivor
  assert driver.current_url=='about:blank'
  driver.switch_to.new_window('tab');driver.switch_to.new_window('tab');driver.close()
  assert len(driver.window_handles)==2
  try:BusinessBrowsers._session_closed(SimpleNamespace(driver=driver))
  except PlatformOperationError:pass
  else:raise AssertionError('ambiguous remaining tabs accepted')
  assert len(driver.window_handles)==2
  print('PASS: closed-active-tab bug reproduced; unique surviving tab restored; ambiguous tabs preserved and blocked; no browser reopening or platform submission.')
 finally:driver.quit()
