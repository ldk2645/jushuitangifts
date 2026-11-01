"""仅在本机桌面配置账号与业务浏览器显示模式，不经局域网 API。"""
import tkinter as tk
from tkinter import ttk, messagebox

from .credentials import CredentialStore, CredentialStoreError
from .config import SITES


def run():
    root = tk.Tk()
    root.title('平台账号与浏览器设置')
    root.geometry('650x510')
    frame = ttk.Frame(root, padding=20)
    frame.pack(fill='both', expand=True)
    store = CredentialStore()
    try:
        current = store.settings()
    except CredentialStoreError as exc:
        messagebox.showerror('无法读取配置', str(exc), parent=root)
        root.destroy()
        return
    ttk.Label(frame, text='密码留空保留已存密码；修改账号时必须填写新密码。', wraplength=600).pack(anchor='w')
    entries = {}
    for site in SITES:
        box = ttk.LabelFrame(frame, text=site.name, padding=12)
        box.pack(fill='x', pady=10)
        username = tk.StringVar(value=current[site.key]['username'])
        password = tk.StringVar()
        headless = tk.BooleanVar(value=current[site.key]['headless'])
        ttk.Label(box, text='账号').grid(row=0,column=0,sticky='w')
        ttk.Entry(box,textvariable=username,width=44).grid(row=0,column=1,padx=12,pady=4)
        ttk.Label(box, text='密码').grid(row=1,column=0,sticky='w')
        ttk.Entry(box,textvariable=password,show='●',width=44).grid(row=1,column=1,padx=12,pady=4)
        ttk.Checkbutton(box,text='业务浏览器无头运行（不显示窗口）',variable=headless).grid(row=2,column=0,columnspan=2,sticky='w',pady=6)
        entries[site.key] = (username,password,headless)
    ttk.Label(frame,text='保存后正常关闭并重启工作台生效。登录助手始终显示窗口。\n无头模式遇到验证码时，请关闭工作台，用 login.bat 完成登录后再启动。\n密码按当前 Windows 用户加密；更换电脑后在目标电脑重新填写。',wraplength=600).pack(anchor='w',pady=8)

    def save():
        values = {site:{'username':u.get(),'password':p.get(),'headless':h.get()}
                  for site,(u,p,h) in entries.items()}
        try:
            store.save_settings(values)
        except CredentialStoreError as exc:
            messagebox.showerror('未保存',str(exc),parent=root)
            return
        for _, password, _ in entries.values():
            password.set('')
        messagebox.showinfo('已保存','配置已保存，请正常关闭并重启工作台生效。',parent=root)
    ttk.Button(frame,text='保存配置',command=save).pack(anchor='e',pady=8)
    root.mainloop()


def run_browser_modes():
    root = tk.Tk()
    root.title('业务浏览器显示设置')
    root.geometry('510x320')
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill='both', expand=True)
    store = CredentialStore()
    try:
        current = store.settings()
    except CredentialStoreError as exc:
        messagebox.showerror('无法读取配置', str(exc), parent=root)
        root.destroy()
        return
    ttk.Label(frame, text='分别设置两个业务浏览器', font=('Microsoft YaHei UI', 14)).pack(anchor='w', pady=(0, 14))
    flags = {}
    for site in SITES:
        flags[site.key] = tk.BooleanVar(value=current[site.key]['headless'])
        ttk.Checkbutton(frame, text=f'{site.name}：无头运行（隐藏窗口）', variable=flags[site.key]).pack(anchor='w', pady=10)
    ttk.Label(frame, text='不勾选时显示浏览器窗口。保存后正常重启工作台生效。\n登录助手始终显示窗口；遇验证码请通过登录助手处理。\n本设置不会更改账号、密码或会话。', wraplength=455).pack(anchor='w', pady=14)
    def save():
        try:
            store.save_browser_modes({key: value.get() for key, value in flags.items()})
        except CredentialStoreError as exc:
            messagebox.showerror('未保存', str(exc), parent=root)
            return
        messagebox.showinfo('已保存', '请等待在途任务结束，正常退出并重启工作台。', parent=root)
    ttk.Button(frame, text='保存设置', command=save).pack(anchor='e')
    root.mainloop()
