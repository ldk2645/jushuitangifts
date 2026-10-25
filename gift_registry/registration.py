"""持久化逐行任务、串行执行、结果回传与不自动重试策略。"""

import hashlib
import json
import os
import queue
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from .config import ROOT
from .platform_operations import SeleniumOperations
from .sessions import atomic_write
from .workflow import PlatformOperationError, Workflow, request_from_row, validate_snapshot, verify_new_records

TERMINAL = {"success", "failed"}
ACTIVE = {"queued", "querying", "submitting", "verifying"}


class RegistrationStartError(ValueError):
    """环境尚不能接收新任务；此次请求没有开始业务执行。"""


class _QueueDirectoryLock:
    """持有内核文件锁，防止其他进程在同一目录恢复或执行任务。"""

    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.stream = (directory / ".queue.lock").open("a+b")
        try:
            self.stream.seek(0)
            if not self.stream.read(1):
                self.stream.write(b"\0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise ValueError("任务目录已被其他进程占用或无法独占；请关闭其他工作台后再启动") from None

    def close(self):
        # 关闭文件描述符即释放内核锁。保留锁文件，避免其他进程锁住不同文件。
        self.stream.close()


def now():
    return datetime.now().astimezone().isoformat()


def _item_updated_time(item, job):
    try:
        return datetime.fromisoformat(item.get("updated_at", job["created_at"])).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def _verified_success(item):
    evidence = item.get("evidence")
    identifiers = evidence.get("verified_record_ids") if isinstance(evidence, dict) else None
    return (item["state"] == "success" and isinstance(identifiers, (list, tuple)) and bool(identifiers)
            and all(isinstance(identifier, str) and identifier.strip() for identifier in identifiers))


def load_gifts():
    catalog = json.loads((ROOT / "config" / "gifts.json").read_text(encoding="utf-8"))
    seen = set()
    for gift in catalog:
        if not isinstance(gift.get("id"), str) or gift["id"] in seen or not gift.get("name"):
            raise ValueError("礼品配置中的标识或名称不正确")
        seen.add(gift["id"])
    return catalog


def row_signature(row):
    return json.dumps([row["id"], row["order_no"], row["gift"], row.get("gift_ids", []),
                       row["change_address"], row["recipient_name"], row["phone"], row["address"]],
                      ensure_ascii=False, separators=(",", ":"))


def business_key(row):
    content = [row["order_no"].strip(), sorted(set(row.get("gift_ids", []))), row["change_address"]]
    if row["change_address"]:
        content += [row["recipient_name"].strip(), row["phone"].strip(), row["address"].strip()]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode("utf-8")).hexdigest()


def registration_rows(rows, gifts):
    names = {gift["id"]: gift["name"] for gift in gifts}
    records, problems = [], []
    for index, row in enumerate(rows, 2):
        if not (row["order_no"].strip() or row["gift"].strip() or row["change_address"]):
            continue
        missing = []
        if not row["order_no"].strip():
            missing.append("订单号")
        ids = row.get("gift_ids", [])
        if not ids or any(identifier not in names for identifier in ids):
            missing.append("有效礼品选项")
        elif row["gift"] != "、".join(names[identifier] for identifier in ids):
            missing.append("未匹配的礼品需重新选择")
        if row["change_address"]:
            missing.extend(label for field, label in (("recipient_name", "姓名"), ("phone", "电话"), ("address", "地址"))
                           if not row[field].strip())
        if missing:
            problems.append(f"第 {index} 行：{'、'.join(missing)}")
        records.append(deepcopy(row))
    if problems:
        raise ValueError("请先完善草稿；" + "；".join(problems[:5]))
    if not records:
        raise ValueError("当前没有可登记的草稿，请先填写订单号和礼品")
    return records


class RegistrationQueue:
    def __init__(self, directory, operations=None):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.pending = queue.Queue()
        self.worker = None
        self.scheduled = set()
        self._closed = False
        self._stopping = False
        self._directory_lock = _QueueDirectoryLock(self.directory)
        try:
            self.operations = operations if operations is not None else SeleniumOperations()
            self._recover_interrupted()
        except BaseException:
            self._directory_lock.close()
            raise

    def stop_accepting(self):
        """先冻结新任务，再由调用方等待已有工作完成；此时继续保留目录锁。"""
        with self.lock:
            self._stopping = True

    def close(self):
        """仅在队列空闲时释放所有权；执行中的提交仍由原进程独占。"""
        with self.lock:
            if self._closed:
                return
            if self.pending.unfinished_tasks:
                raise RuntimeError("礼赠任务仍在执行，暂不能释放任务目录；请等待任务结束")
            self._closed = True
            if self.worker is not None and self.worker.is_alive():
                self.pending.put(None)
            self._directory_lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @property
    def configured(self):
        return self.operations.configured is True

    def _paths(self):
        return sorted(self.directory.glob("*.json"))

    def _load(self, path):
        job = json.loads(path.read_text(encoding="utf-8"))
        if "items" not in job:
            job["items"] = [self._item(row) for row in job["rows"]]
            legacy_version = ("version" not in job
                              or (type(job["version"]) is int and job["version"] == 1))
            safe_waiting = legacy_version and job.get("state") == "waiting_for_workflow"
            for item in job["items"]:
                # 沿用原快照时间，不能在每次只读加载时把旧状态刷新成最新状态。
                item["updated_at"] = job.get("updated_at", job["created_at"])
                if not safe_waiting:
                    item.update(state="failed", detail="任务记录缺少执行明细，历史提交状态无法确认；禁止自动重复登记")
            if not safe_waiting:
                # 只在内存中形成保守结果；不删除、改写原文件或猜测提交成功证据。
                job["state"] = "failed"
        for item in job["items"]:
            if item.get("state") == "success" and not _verified_success(item):
                # 缺少本次新增记录核验依据的“成功”不能作为成功沿用；
                # 保留失败终态阻止任何自动重复提交，要求人工核查。
                item["state"] = "failed"
                item["detail"] = "已有成功记录缺少本次新增核验依据，请人工核查；禁止自动重复登记"
        return job

    def _item(self, row):
        return {"row_id": row["id"], "business_key": business_key(row), "state": "waiting_for_workflow",
                "detail": getattr(self.operations, "configuration_detail", "具体业务操作尚未接入"), "submit_attempted": False, "attempts": 0,
                "updated_at": now(), "events": []}

    def _save(self, path, job):
        states = {item["state"] for item in job["items"]}
        if states <= TERMINAL:
            job["state"] = "success" if states == {"success"} else "failed" if states == {"failed"} else "partial_failure"
        elif states & {"querying", "submitting", "verifying"}:
            job["state"] = "running"
        elif "queued" in states:
            job["state"] = "queued"
        else:
            job["state"] = "waiting_for_workflow"
        job["updated_at"] = now()
        atomic_write(path, json.dumps(job, ensure_ascii=False, indent=2).encode("utf-8"))

    def _release_unused_start(self):
        release = getattr(self.operations, "release_unused_start", None)
        if callable(release):
            try:
                release()
            except Exception:
                # 只做资源清理，不能覆盖任务结果或暴露底层异常中的登录信息。
                pass

    def _recover_interrupted(self):
        for path in self._paths():
            job = self._load(path)
            interrupted = False
            for item in job["items"]:
                if item["state"] in ACTIVE:
                    item.update(state="failed", detail="上次任务中断，结果需人工核查；未自动重试登记", updated_at=now())
                    item.setdefault("events", []).append({"state": "failed", "at": now(), "detail": item["detail"]})
                    interrupted = True
            if interrupted:
                self._save(path, job)

    def states(self):
        result, latest = {}, {}
        with self.lock:
            for path in self._paths():
                job = self._load(path)
                for row, item in zip(job["rows"], job["items"]):
                    updated = (_item_updated_time(item, job), path.name)
                    if row["id"] in latest and latest[row["id"]] > updated:
                        continue
                    latest[row["id"]] = updated
                    result[row["id"]] = {"signature": row_signature(row), "state": item["state"], "task_id": job["id"],
                                         "detail": item["detail"], "attempts": item["attempts"], "updated_at": item["updated_at"]}
        return result

    def _existing_result(self, key, exclude=None):
        candidates = []
        for path in self._paths():
            job = self._load(path)
            for item in job["items"]:
                if item["business_key"] == key and (item["state"] in TERMINAL or item["submit_attempted"]):
                    if exclude != (path.name, item["row_id"]):
                        result = deepcopy(item)
                        if result["state"] not in TERMINAL:
                            result.update(state="failed", detail="已有提交尝试但结果未确认，请人工核查；禁止自动重复登记")
                        # 已核对过新增记录的成功不会被较晚的中断恢复失败覆盖。
                        candidates.append((_verified_success(result), _item_updated_time(item, job), path.name, result))
        return max(candidates, key=lambda candidate: candidate[:3])[3] if candidates else None

    def record_external_submission(self, row, catalog, before, after=None, receipt=None, source_reference=""):
        """记录用户已人工提交的业务；只写防重结果，不派发或重试平台操作。

        未完成新增信息核对时先保存失败终态；随后可凭完整基线、复查和回执，
        只把同一外部记录升级为已核验成功。原任务与用户草稿均不改写。
        """
        records = registration_rows([row], catalog)
        record = records[0]
        if not isinstance(source_reference, str) or len(source_reference) > 200:
            raise ValueError("人工提交来源标识必须为文本且不超过 200 字符")
        request = request_from_row(record, catalog)
        if before is not None:
            validate_snapshot(before, request.order_no)
        success = False
        identifiers = []
        detail = "已人工提交，结果待核查；禁止重复登记"
        if after is not None:
            if before is None or receipt is None:
                raise ValueError("人工提交核验需要原始基线、复查结果及提交回执")
            success, reason, identifiers = verify_new_records(request, before, after, receipt)
            detail = ("已人工提交，聚水潭已确认本次新增礼赠信息正确" if success
                      else f"已人工提交，{reason}；禁止重复登记")
        key = business_key(record)
        path = self.directory / f"external-{key}.json"
        with self.lock:
            if self._closed:
                raise RuntimeError("礼赠任务队列已关闭，不能记录人工提交")
            if self._stopping:
                raise ValueError("工作台正在关闭，不能记录人工提交")
            if self.pending.unfinished_tasks:
                raise RuntimeError("礼赠任务仍在执行，不能同时记录人工提交")
            if path.exists():
                job = self._load(path)
                item = job["items"][0]
                if (item.get("source") != "manual" or item["business_key"] != key
                        or len(job["items"]) != 1 or len(job["rows"]) != 1):
                    raise ValueError("人工提交记录格式不正确，未覆盖已有记录")
                # 即使本次核验未通过，也不能用不同基线覆盖已有记录或证据。
                saved_baseline = item.get("evidence", {}).get("before_record_ids", [])
                incoming_baseline = ({entry.record_id for entry in before.records}
                                     if before is not None else None)
                if (saved_baseline and incoming_baseline is not None
                        and set(saved_baseline) != incoming_baseline):
                    raise ValueError("人工提交核验基线与首次记录不一致，未覆盖已有记录")
                # 重复记录不会降级、追加尝试次数或抹去既有核验；只允许待核查升级。
                if item["state"] == "success" or not success:
                    return deepcopy(item)
            else:
                # 隐藏的旧地址草稿不属于本次业务，也不保存到新增人工记录中。
                if not record["change_address"]:
                    record.update(recipient_name="", phone="", address="")
                job = {"version": 2, "id": "external-" + key[:16], "created_at": now(),
                       "rows": [record], "gift_catalog": deepcopy(catalog), "items": [self._item(record)]}
                item = job["items"][0]
            item.update(state="success" if success else "failed", detail=detail,
                        submit_attempted=True, attempts=1, source="manual", updated_at=now())
            evidence = item.setdefault("evidence", {})
            evidence.update(before_record_ids=[entry.record_id for entry in before.records] if before else [],
                            verified_record_ids=identifiers)
            if source_reference:
                evidence["source_reference"] = source_reference
            item.setdefault("events", []).append({"state": item["state"], "at": now(), "detail": detail,
                                                   "source": "manual"})
            self._save(path, job)
            return deepcopy(item)

    def start(self, rows, gifts):
        records = registration_rows(rows, gifts)
        identifiers = [row.get("id") for row in records]
        if (any(not isinstance(identifier, str) or not identifier.strip() for identifier in identifiers)
                or len(set(identifiers)) != len(identifiers)):
            # 同一批次重复行标识会让按行标识排除自身结果的查重失效，必须在派发前拒绝。
            raise ValueError("草稿行标识缺失或重复，请刷新页面后重试")
        fingerprint = hashlib.sha256("\n".join(row_signature(row) for row in records).encode("utf-8")).hexdigest()
        path = self.directory / f"{fingerprint}.json"
        with self.lock:
            if self._closed:
                raise RuntimeError("礼赠任务队列已关闭，不能再次开始登记")
            if self._stopping:
                raise ValueError("工作台正在关闭，不能开始新的礼赠任务")
            reused = path.exists()
            if reused:
                job = self._load(path)
                for item in job["items"]:
                    if item["state"] in TERMINAL:
                        prior = self._existing_result(item["business_key"], exclude=(path.name, item["row_id"]))
                        if prior and _verified_success(prior):
                            identifier = item["row_id"]
                            item.update(prior, row_id=identifier, reused_result=True)
                        # 显式选回旧快照时，让前端显示该快照的终态；终态不会重新派发。
                        item["updated_at"] = now()
            else:
                job = {"version": 2, "id": fingerprint[:16], "created_at": now(), "rows": records,
                       "gift_catalog": deepcopy(gifts), "items": [self._item(row) for row in records]}
                for item in job["items"]:
                    prior = self._existing_result(item["business_key"])
                    if prior:
                        identifier = item["row_id"]
                        item.update(prior, row_id=identifier, reused_result=True, updated_at=now())
            # 只由明确的开始请求派发，失败终态永不重置。
            dispatch = self.configured and any(item["state"] == "waiting_for_workflow" for item in job["items"])
            # 在写入任务和 queued 状态之前预留登录配置，避免环境冲突消耗订单。
            # 全部终态复用、待接入或只含不支持的改地址请求都不需要平台浏览器。
            needs_browser = dispatch and any(
                item["state"] == "waiting_for_workflow" and not row["change_address"]
                for row, item in zip(job["rows"], job["items"]))
            prepare = getattr(self.operations, "prepare_start", None)
            prepared = False
            if needs_browser and callable(prepare):
                try:
                    prepare()
                    prepared = True
                except Exception as exc:
                    if not self.pending.unfinished_tasks:
                        self._release_unused_start()
                    reason = str(exc) if isinstance(exc, PlatformOperationError) else "登记环境检查失败，请检查工作台及登录助手"
                    raise RegistrationStartError(reason + "；本次未启动登记，未写入新的失败记录") from None
            if dispatch:
                for item in job["items"]:
                    if item["state"] == "waiting_for_workflow":
                        item.update(state="queued", detail="等待执行", updated_at=now())
            try:
                self._save(path, job)
            except Exception:
                if prepared and not self.pending.unfinished_tasks:
                    self._release_unused_start()
                raise
            if dispatch and path.name not in self.scheduled:
                self.scheduled.add(path.name)
                self.pending.put(path)
                if self.worker is None or not self.worker.is_alive():
                    self.worker = threading.Thread(target=self._work, name="registration-worker", daemon=True)
                    self.worker.start()
            return {"task_id": job["id"], "count": len(records), "state": job["state"], "reused": reused,
                    "workflow_configured": self.configured, "row_states": self.states()}

    def _work(self):
        while True:
            path = self.pending.get()
            if path is None:
                self.pending.task_done()
                return
            try:
                self._process(path)
            except Exception:
                # 编排异常也不重新入队；尽力记失败后继续处理其他独立任务。
                try:
                    with self.lock:
                        job = self._load(path)
                        for item in job["items"]:
                            if item["state"] not in TERMINAL:
                                item.update(state="failed", detail="任务处理异常，请人工核查；未自动重试登记", updated_at=now())
                        self._save(path, job)
                except (OSError, ValueError):
                    pass
            finally:
                with self.lock:
                    self.scheduled.discard(path.name)
                    # 最后一个已接收任务结束后，释放未曾打开浏览器的空预留。
                    if self.pending.unfinished_tasks == 1:
                        self._release_unused_start()
                    self.pending.task_done()

    def _process(self, path):
        with self.lock:
            job = self._load(path)
        for index, row in enumerate(job["rows"]):
            with self.lock:
                current = self._load(path)
                item = current["items"][index]
                if item["state"] in TERMINAL:
                    continue
                previous = self._existing_result(item["business_key"], exclude=(path.name, item["row_id"]))
                if previous:
                    identifier = item["row_id"]
                    item.update(previous, row_id=identifier, reused_result=True, updated_at=now())
                    self._save(path, current)
                    continue
                if item["submit_attempted"]:
                    item.update(state="failed", detail="该任务已尝试提交，禁止自动重复登记")
                    self._save(path, current)
                    continue

            def report(state, **updates):
                with self.lock:
                    document = self._load(path)
                    target = document["items"][index]
                    evidence = updates.pop("evidence", None)
                    target.update(state=state, updated_at=now(), **updates)
                    if evidence:
                        target.setdefault("evidence", {}).update(evidence)
                    target["events"].append({"state": state, "at": now(), "detail": target["detail"]})
                    self._save(path, document)

            Workflow(self.operations).execute(row, job["gift_catalog"], report)
