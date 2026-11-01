"""礼品分组元数据；不改商品编码、草稿或历史任务。"""
import json
import threading
import uuid
from copy import deepcopy
from pathlib import Path

from .gift_catalog import _text
from .sessions import atomic_write


class GroupConflict(ValueError):
    pass


class GiftGroupStore:
    def __init__(self, path, catalog):
        self.path, self.catalog = Path(path), catalog
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            if not self.path.exists():
                return {"version": 1, "revision": 0, "groups": [
                    {"id": "xiaomi-ecosystem", "name": "小米生态链"},
                    {"id": "xiaomi-phone", "name": "小米手机"}], "memberships": {}}
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (data.get("version") != 1 or type(data.get("revision")) is not int
                    or not isinstance(data.get("groups"), list) or not isinstance(data.get("memberships"), dict)):
                raise ValueError("礼品分组配置格式不正确")
            ids = [group["id"] for group in data["groups"]]
            if len(ids) != len(set(ids)) or any(not isinstance(value, str) or not value for value in ids):
                raise ValueError("礼品分组标识重复或无效")
            for group in data["groups"]:
                _text(group["name"], "分组名称", 40)
            for sku, memberships in data["memberships"].items():
                if not isinstance(sku, str) or not isinstance(memberships, list) or any(value not in ids for value in memberships):
                    raise ValueError("礼品分组归属配置无效")
            return data

    def update(self, value):
        with self.lock:
            data = self.read()
            if type(value.get("revision")) is not int or value["revision"] != data["revision"]:
                raise GroupConflict("分组已在其他窗口更新，请重新打开分组管理")
            action = value.get("action")
            if action == "create":
                name = _text(value.get("name"), "分组名称", 40)
                if name in {"全部礼品", "未分组"} or any(group["name"] == name for group in data["groups"]):
                    raise ValueError("分组名称已存在或为保留名称")
                if len(data["groups"]) >= 100:
                    raise ValueError("最多支持 100 个分组")
                data["groups"].append({"id": uuid.uuid4().hex, "name": name})
            elif action == "assign":
                skus, groups = value.get("gift_ids"), value.get("group_ids")
                mode = value.get("mode", "replace")
                if not isinstance(mode, str) or mode not in {"replace", "add", "remove"}:
                    raise ValueError("分组设置方式无效")
                if mode != "replace" and not groups:
                    raise ValueError("请至少选择一个分组")
                known_skus = {gift["id"] for gift in self.catalog.read()}
                known_groups = {group["id"] for group in data["groups"]}
                if (not isinstance(skus, list) or not skus or len(skus) > 10000
                        or any(not isinstance(sku, str) or sku not in known_skus for sku in skus)
                        or not isinstance(groups, list) or len(groups) > 100
                        or any(not isinstance(group, str) or group not in known_groups for group in groups)):
                    raise ValueError("请选择有效的礼品及分组")
                for sku in skus:
                    previous = data["memberships"].get(sku, [])
                    updated = ([*previous, *groups] if mode == "add" else
                               [g for g in previous if g not in groups] if mode == "remove" else groups)
                    if updated:
                        data["memberships"][sku] = list(dict.fromkeys(updated))
                    else:
                        data["memberships"].pop(sku, None)
            else:
                raise ValueError("分组操作无效")
            data["revision"] += 1
            atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
            return deepcopy(data)
