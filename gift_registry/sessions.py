"""Windows DPAPI 加密快照；不读取或记录密码输入框。"""

import ctypes
import json
import os
import time
from ctypes import wintypes
from pathlib import Path

MAGIC = b"GIFT-SESSION-DPAPI-1\n"


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(payload: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("此版本使用 Windows DPAPI，请在 Windows 上运行。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(payload)
    source = _Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN；默认仅限当前 Windows 用户。
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(target.data)


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


class SessionStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def path(self, key: str) -> Path:
        return self.directory / f"{key}.dpapi"

    def save(self, key: str, snapshot: dict):
        payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        atomic_write(self.path(key), MAGIC + _crypt(payload))

    def load(self, key: str) -> dict | None:
        path = self.path(key)
        if not path.exists():
            return None
        data = path.read_bytes()
        if not data.startswith(MAGIC):
            raise ValueError("会话文件格式不正确")
        result = json.loads(_crypt(data[len(MAGIC):], decrypt=True))
        if result.get("version") != 1 or result.get("site") != key:
            raise ValueError("会话文件版本或站点不匹配")
        return result


def cookie_for_restore(cookie: dict, site, now: float | None = None) -> dict | None:
    """保留 CDP 支持的属性；拒绝其他站点和已过期的 Cookie。"""
    if not site.allows_host(cookie.get("domain", "")):
        return None
    expires = cookie.get("expires", cookie.get("expiry", -1))
    if expires > 0 and expires <= (time.time() if now is None else now):
        return None
    allowed = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite",
               "priority", "sourceScheme", "sourcePort", "partitionKey")
    result = {key: cookie[key] for key in allowed if key in cookie}
    if expires > 0:
        result["expires"] = expires
    return result
