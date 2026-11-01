"""隔离 Selenium：清空触发响应式输入框重建，回车只发送一次。"""
import sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from selenium.webdriver.common.keys import Keys
from gift_registry.banniu_page import BanniuPage
from gift_registry.browser import create_driver
from gift_registry.config import ROOT,Site

HTML='''<meta charset="utf-8"><div class="w-edit-task-dialog"><input id="synthetic" value="old"></div>
<script>window.clears=0;window.enters=0;
document.addEventListener('change',e=>{if(e.target.id==='synthetic'&&!e.target.value){window.clears++;e.target.replaceWith(e.target.cloneNode(true));document.querySelector('#synthetic').value='';}});
document.addEventListener('keydown',e=>{if(e.key==='Enter')window.enters++});</script>'''
with tempfile.TemporaryDirectory(prefix='input-rebuild-',dir=ROOT/'.tmp') as folder:
 path=Path(folder)/'input.html';path.write_text(HTML,encoding='utf-8')
 driver=create_driver(Site('synthetic','synthetic',path.as_uri(),()),headless=True,profile=Path(folder)/'profile')
 try:
  driver.get(path.as_uri());page=BanniuPage(driver)
  element=page._set_text('#synthetic','0001');page._send_keys(element,Keys.ENTER)
  assert driver.execute_script('return [window.clears,window.enters,document.querySelector("#synthetic").value]')==[1,1,'0001']
  print('PASS: input rebuilt after clear; exact string retained; Enter sent once; local synthetic page only.')
 finally:driver.quit()
