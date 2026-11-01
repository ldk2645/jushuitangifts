"""临时副本执行 start.bat，并验证启动窗口；不启动真实工作台或平台浏览器。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gift_registry import launcher
from gift_registry.config import ROOT


def main():
    with tempfile.TemporaryDirectory(prefix='launch-smoke-',dir=ROOT/'.tmp') as directory:
        folder=Path(directory)
        scripts=folder/'.venv'/'Scripts'
        scripts.mkdir(parents=True)
        shutil.copy2(Path(sys.executable).with_name('pythonw.exe'),scripts/'pythonw.exe')
        (scripts.parent/'pyvenv.cfg').write_text(f'home = {sys.base_prefix}\ninclude-system-site-packages = false\n',encoding='utf-8')
        shutil.copy2(ROOT/'start.bat',folder/'start.bat')
        (folder/'main.py').write_text('import json,sys\nfrom pathlib import Path\nPath("args.json").write_text(json.dumps(sys.argv[1:]))\n',encoding='utf-8')
        subprocess.run(['cmd.exe','/d','/c','start.bat'],cwd=folder,check=True,timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        deadline=time.monotonic()+8
        while not (folder/'args.json').exists() and time.monotonic()<deadline:
            time.sleep(.05)
        assert json.loads((folder/'args.json').read_text())==['--launch']
        time.sleep(.2)  # 仅等待临时 Python 退出、释放其可执行文件。

    # 实际 Tk 控件及主循环；替身只提供状态，不访问任务目录或启动进程。
    for kind,value in [('waiting','上一工作台仍在完成已接收的订单，完成后自动打开。'),
                       ('error','工作台启动失败（RuntimeError）。'),
                       ('ready','http://127.0.0.1:8765/')]:
        window=tk.Tk()
        window.withdraw()
        observed=[]
        def inspect():
            try:
                labels=[]
                def walk(widget):
                    for child in widget.winfo_children():
                        if child.winfo_class()=='TLabel':
                            variable=child.cget('textvariable')
                            labels.append(child.getvar(variable) if variable else child.cget('text'))
                        walk(child)
                walk(window)
                observed.append(any(value in text for text in labels))
            finally:
                window.tk.call(window.protocol('WM_DELETE_WINDOW'))
        window.after(500,inspect)
        with patch.object(tk,'Tk',return_value=window), \
             patch.object(launcher,'supervise',side_effect=lambda port,notify,cancelled:notify(kind,value)), \
             patch.object(launcher.webbrowser,'open',return_value=False):
            launcher.run()
        assert observed==[True], (kind,observed)
    print('PASS: actual BAT invokes --launch; Tk displays waiting/error/ready fallback. Temporary project and synthetic startup only.')


if __name__=='__main__':
    main()
