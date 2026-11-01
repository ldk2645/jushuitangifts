"""本机礼赠清单：编码唯一，名称/别名允许重复，追加原子保存。"""

import json
import threading
from copy import deepcopy
from pathlib import Path

from .config import ROOT
from .sessions import atomic_write


class GiftCatalogConflict(ValueError):
    pass


def matching_key(value):
    # 仅用于名称搜索与本项别名去重，不能作为商品编码身份。
    return "".join(char for char in value.lower() if char.isalnum())


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{label}不能为空，且不能超过 {limit} 个字符")
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or not matching_key(value):
        raise ValueError(f"{label}格式不正确，请勿包含换行或控制字符")
    return value


def validate_gift(value):
    if not isinstance(value, dict):
        raise ValueError("礼赠数据格式不正确")
    identifier = _text(value.get("id"), "商品编码", 80)
    name = _text(value.get("name"), "礼赠名称", 200)
    aliases = value.get("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > 30:
        raise ValueError("别名应为列表，最多填写 30 项")
    result, seen = [], {matching_key(identifier), matching_key(name)}
    for alias in aliases:
        alias = _text(alias, "别名", 100)
        key = matching_key(alias)
        if key not in seen:
            result.append(alias)
            seen.add(key)
    return {"id": identifier, "name": name, "aliases": result}


class GiftCatalogStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else ROOT / "config" / "gifts.json"
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            gifts = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(gifts, list) or len(gifts) > 10000:
                raise ValueError("礼赠清单格式不正确或超过 10000 项")
            seen = set()
            for gift in gifts:
                validated = validate_gift(gift)
                if validated["id"] in seen:
                    raise ValueError("礼赠清单存在重复商品编码")
                seen.add(validated["id"])
            return gifts

    def add(self, value):
        gift = validate_gift(value)
        with self.lock:
            gifts = self.read()
            if len(gifts) >= 10000:
                raise ValueError("礼赠清单最多支持 10000 项")
            if any(existing["id"].strip() == gift["id"] for existing in gifts):
                raise GiftCatalogConflict("商品编码已存在，请使用不同编码；名称可以相同")
            gifts.append(gift)
            atomic_write(self.path, json.dumps(gifts, ensure_ascii=False, indent=2).encode("utf-8"))
            return {"gift": deepcopy(gift), "gifts": gifts}
