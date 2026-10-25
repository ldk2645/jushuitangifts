"""用户授权的本机账号配置；密码使用当前 Windows 用户的 DPAPI 加密。"""

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

from .config import ROOT, SITES
from .sessions import _crypt, atomic_write


DEFAULT_PATH = ROOT / "config" / "accounts.local.json"
_SITES = frozenset(site.key for site in SITES)
_MAGIC = b"GIFT-ACCOUNT-DPAPI-1\n"


class CredentialStoreError(RuntimeError):
    """固定且可展示的错误信息，不包含账号、密码、密文或底层异常正文。"""


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str = field(repr=False)


def _protect(payload):
    return _crypt(payload)


def _unprotect(payload):
    return _crypt(payload, decrypt=True)


def _validate_site(site_key):
    if not isinstance(site_key, str) or site_key not in _SITES:
        raise CredentialStoreError("账号配置站点不受支持")


def _validate_values(username, password):
    if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
        raise CredentialStoreError("账号和密码必须是非空文本")


class CredentialStore:
    """两平台独立存放于一个本地 JSON 文件；路径和加密器可注入隔离测试。

    load() 仅在登录时返回内存中的明文密码；调用方不得打印、记录或序列化它。
    文件及其临时文件只能包含用户名和密码密文，不存放明文密码。
    """

    def __init__(self, path=DEFAULT_PATH, *, protect=_protect, unprotect=_unprotect):
        self.path = Path(path)
        self._protect = protect
        self._unprotect = unprotect
        self._lock = RLock()

    def _read(self):
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "accounts": {}}
        if (not isinstance(document, dict) or set(document) != {"version", "accounts"}
                or type(document["version"]) is not int or document["version"] != 1
                or not isinstance(document["accounts"], dict)):
            raise ValueError("invalid format")
        for site_key, account in document["accounts"].items():
            _validate_site(site_key)
            if (not isinstance(account, dict) or set(account) != {"username", "password_dpapi"}
                    or not isinstance(account["username"], str) or not account["username"].strip()
                    or not isinstance(account["password_dpapi"], str) or not account["password_dpapi"]):
                raise ValueError("invalid account")
            if not base64.b64decode(account["password_dpapi"], validate=True):
                raise ValueError("invalid encrypted password")
        return document

    def save(self, site_key, username, password):
        _validate_site(site_key)
        _validate_values(username, password)
        with self._lock:
            try:
                document = self._read()
                # 同时绑定站点和用户名，防止密文在两平台或账号之间误复制。
                payload = _MAGIC + json.dumps({"site": site_key, "username": username, "password": password},
                                              ensure_ascii=False).encode("utf-8")
                encrypted = self._protect(payload)
                if not isinstance(encrypted, bytes) or not encrypted:
                    raise ValueError("invalid encrypted result")
                document["accounts"][site_key] = {
                    "username": username,
                    "password_dpapi": base64.b64encode(encrypted).decode("ascii"),
                }
                atomic_write(self.path, json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))
            except Exception:
                raise CredentialStoreError("账号配置保存失败，请检查本机文件权限及 Windows 加密服务") from None

    def load(self, site_key):
        _validate_site(site_key)
        with self._lock:
            try:
                account = self._read()["accounts"].get(site_key)
                if account is None:
                    return None
                encrypted = base64.b64decode(account["password_dpapi"], validate=True)
                payload = self._unprotect(encrypted)
                if not isinstance(payload, bytes) or not payload.startswith(_MAGIC):
                    raise ValueError("invalid decrypted format")
                values = json.loads(payload[len(_MAGIC):].decode("utf-8"))
                if (not isinstance(values, dict) or set(values) != {"site", "username", "password"}
                        or values["site"] != site_key or values["username"] != account["username"]):
                    raise ValueError("account identity mismatch")
                _validate_values(values["username"], values["password"])
                return Credentials(values["username"], values["password"])
            except Exception:
                raise CredentialStoreError("账号配置读取或解密失败，请使用保存配置的 Windows 用户重新配置账号") from None
