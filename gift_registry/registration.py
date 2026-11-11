"""持久化逐行任务、串行执行、结果回传与不自动重试策略。"""

import hashlib
import json
import os
import queue
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from .config import ROOT
from .platform_operations import SeleniumOperations
from .sessions import atomic_write
from .workflow import (PlatformOperationError, Workflow, GiftRecord, OrderSnapshot, SubmissionReceipt,
                       request_from_row, validate_snapshot, validate_receipt, verify_new_records, verify_added_records)

TERMINAL = {"success", "failed", "skipped", "cancelled"}
ACTIVE = {"queued", "querying", "submitting", "verifying"}
REVIEW_DELAY_SECONDS = 180


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


_time_lock = threading.Lock()
_last_now = None


def now():
    # Windows 快速连续操作可能得到相同墙钟值；确保本进程写入顺序可区分。
    global _last_now
    with _time_lock:
        current = datetime.now().astimezone()
        if _last_now is not None and current <= _last_now:
            current = _last_now + timedelta(microseconds=1)
        _last_now = current
        return current.isoformat()


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
    from .gift_catalog import GiftCatalogStore
    return GiftCatalogStore(ROOT / 'config/gifts.json').read()


def legacy_row_signature(row):
    return json.dumps([row["id"], row["order_no"], row.get("gift", ""), row.get("gift_ids", []),
                       row.get("change_address", False), row.get("recipient_name", ""), row.get("phone", ""), row.get("address", "")],
                      ensure_ascii=False, separators=(",", ":"))


def row_signature(row):
    return json.dumps([row["id"], row["order_no"].strip(), sorted(set(row.get("gift_ids", []))),
                       row.get("change_address", False), row.get("gift_unmatched", [])],
                      ensure_ascii=False, separators=(",", ":"))


def business_key(row):
    content = [row["order_no"].strip(), sorted(set(row.get("gift_ids", []))), row.get("change_address", False)]
    if row.get("change_address", False):
        content += [row.get("recipient_name", "").strip(), row.get("phone", "").strip(), row.get("address", "").strip()]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode("utf-8")).hexdigest()


def task_key(row):
    """登记身份只取订单号和整组编码；不改写旧 business_key 或执行历史。"""
    ids = row.get("gift_ids", [])
    if not isinstance(ids, (list, tuple)) or any(not isinstance(value, str) or not value for value in ids):
        ids = []  # 旧记录编码不完整时保留未知组合，不能从同名礼品猜编码。
    content = [row["order_no"].strip(), sorted(set(ids))]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode("utf-8")).hexdigest()


def registration_rows(rows, gifts):
    names = {gift["id"]: gift["name"] for gift in gifts}
    records, problems = [], []
    for index, row in enumerate(rows, 2):
        if not (row["order_no"].strip() or row["gift"].strip() or row.get("change_address", False)):
            continue
        missing = []
        if not row["order_no"].strip():
            missing.append("订单号")
        ids = row.get("gift_ids", [])
        if not ids or any(identifier not in names for identifier in ids):
            missing.append("有效礼品选项")
        elif (row.get("gift_unmatched") or row.get("gift_selection", {}).get("confirmed") is False
              or row.get("gift_selection", {}).get("source") == "unresolved"
              or (not row.get("gift_selection") and row["gift"] != "、".join(names[identifier] for identifier in ids))):
            missing.append("未匹配的礼品需重新选择")
        if row.get("change_address", False):
            missing.append("旧草稿请求改地址，本版本不支持；请核对原意后删除该行并新建原地址订单")
        if missing:
            problems.append(f"第 {index} 行：{'、'.join(missing)}")
        record = deepcopy(row)
        record.setdefault('change_address', False)
        for field in ('recipient_name', 'phone', 'address'):
            record.pop(field, None)
        records.append(record)
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
            # 复核只执行一次；中断后即使不能确认是否已查询，也不重新派发。
            for path in (self.directory / "auto_reviews").glob("*.json"):
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("status") in {"queued", "running"}:
                    record.update(status="interrupted", detail="自动复核中断，未重复查询或提交")
                    atomic_write(path, json.dumps(record, ensure_ascii=False).encode("utf-8"))
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
                item.pop("execution_id", None)  # 旧记录只读加载不能每次生成新的执行身份。
                item.pop("display_updated_at", None)
                # 沿用原快照时间，不能在每次只读加载时把旧状态刷新成最新状态。
                item["updated_at"] = job.get("updated_at", job["created_at"])
                if not safe_waiting:
                    item.update(state="failed", detail="任务记录缺少执行明细，历史提交状态无法确认；禁止自动重复登记")
            if not safe_waiting:
                # 只在内存中形成保守结果；不删除、改写原文件或猜测提交成功证据。
                job["state"] = "failed"
        for item in job["items"]:
            if item.get("state") == "cancelled" and not (
                    item.get("submit_attempted") is False and item.get("attempts") == 0
                    and item.get("submission_status") == "not_submitted" and item.get("cancelled_by") == "host"
                    and not item.get("evidence", {}).get("submit_clicked")):
                item.update(state="failed", submission_status="uncertain",
                            detail="取消记录缺少明确未提交依据，请人工核查；禁止自动重复登记")
            if item.get("state") == "success" and not _verified_success(item):
                # 缺少本次新增记录核验依据的“成功”不能作为成功沿用；
                # 保留失败终态阻止任何自动重复提交，要求人工核查。
                item["state"] = "failed"
                item["detail"] = "已有成功记录缺少本次新增核验依据，请人工核查；禁止自动重复登记"
                item["submission_status"] = "uncertain"
        return job

    def _item(self, row):
        timestamp = now()
        return {"row_id": row["id"], "business_key": business_key(row), "state": "waiting_for_workflow",
                "execution_id": uuid.uuid4().hex,
                "detail": getattr(self.operations, "configuration_detail", "具体业务操作尚未接入"), "submit_attempted": False, "attempts": 0,
                "updated_at": timestamp, "display_updated_at": timestamp, "events": []}

    def _save(self, path, job):
        # 在结果复用更新快照前保留实际终态，避免重登成功抹去当天的原失败原因。
        documents = [self._load(path)] if path.exists() else []
        documents.append(job)
        for document in documents:
            for record in self._job_attempts(document):
                if record["item"]["state"] not in {"success", "failed", "cancelled"}:
                    continue
                destination = self.directory / "execution_history" / (hashlib.sha256(record["failure_id"].encode()).hexdigest() + ".json")
                if not destination.exists():
                    atomic_write(destination, json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))
        states = {item["state"] for item in job["items"]}
        if states <= TERMINAL:
            completed = states - {"cancelled"}
            job["state"] = ("cancelled" if not completed else "partial_cancelled" if "cancelled" in states
                            else "skipped" if states == {"skipped"} else "success" if states <= {"success", "skipped"}
                            else "failed" if states <= {"failed", "skipped"} else "partial_failure")
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
            for row, item in zip(job["rows"], job["items"]):
                if item["state"] in ACTIVE:
                    item.setdefault("display_updated_at", item.get("updated_at", job["created_at"]))
                    item.update(state="failed", detail="上次任务中断，结果需人工核查；未自动重试登记", updated_at=now())
                    self._schedule_review(item, row)
                    item.setdefault("events", []).append({"state": "failed", "at": now(), "detail": item["detail"]})
                    interrupted = True
            if interrupted:
                self._save(path, job)

    @staticmethod
    def _schedule_review(item, row):
        if not row["change_address"] and item.get("source") != "manual" and not item.get("review_due_at"):
            item["review_delay_seconds"] = REVIEW_DELAY_SECONDS
            item["review_due_at"] = (datetime.fromisoformat(now()) + timedelta(seconds=REVIEW_DELAY_SECONDS)).isoformat()

    @staticmethod
    def _review_due_at(item):
        due = item.get("review_due_at")
        if due and not item.get("review_result") and item.get("review_delay_seconds") is None:
            # 旧版已排期任务使用 10 分钟；只换算有效到期时间，不改写历史。
            return (datetime.fromisoformat(due) - timedelta(minutes=7)).isoformat()
        return due

    def states(self):
        result, latest = {}, {}
        with self.lock:
            for path in self._paths():
                job = self._load(path)
                for row, item in zip(job["rows"], job["items"]):
                    # 行归属只由明确创建／选回任务更新；后台复核不能抢占后来使用此行的订单。
                    display = {"updated_at": item.get("display_updated_at", item.get("updated_at", job["created_at"]))}
                    updated = (_item_updated_time(display, job), path.name)
                    if row["id"] in latest and latest[row["id"]] > updated:
                        continue
                    latest[row["id"]] = updated
                    result[row["id"]] = {"signature": row_signature(row), "state": item["state"], "task_id": job["id"],
                                         "task_key": task_key(row),
                                         "detail": item["detail"], "attempts": item["attempts"], "updated_at": item["updated_at"],
                                         "review_due_at": self._review_due_at(item), "review_result": item.get("review_result"),
                                         "retry_cancelled": item["state"] == "cancelled" and bool(item.get("retry_of"))}
        return result

    def _existing_result(self, key, exclude=None, order_no=None):
        candidates = []
        for path in self._paths():
            job = self._load(path)
            for row, item in zip(job["rows"], job["items"]):
                ids = row.get("gift_ids")
                unknown_gifts = not isinstance(ids, list) or not ids or any(not isinstance(value, str) or not value for value in ids)
                matches = task_key(row) == key or (unknown_gifts and row["order_no"].strip() == order_no)
                if matches and item["state"] not in {"skipped", "cancelled"} and (item["state"] in TERMINAL or item["submit_attempted"]):
                    if exclude != (path.name, item["row_id"]):
                        result = deepcopy(item)
                        if result["state"] not in TERMINAL:
                            result.update(state="failed", detail="已有提交尝试但结果未确认，请人工核查；禁止自动重复登记")
                        # 已核对过新增记录的成功不会被较晚的中断恢复失败覆盖。
                        candidates.append((_verified_success(result), _item_updated_time(item, job), path.name, result))
        return max(candidates, key=lambda candidate: candidate[:3])[3] if candidates else None

    @staticmethod
    def _reuse(item, prior):
        identifier, key = item["row_id"], item["business_key"]
        timestamp = now()
        if key == prior["business_key"]:
            item.update(deepcopy(prior), row_id=identifier, reused_result=True, updated_at=timestamp,
                        display_updated_at=timestamp)
        else:
            item.update(state="skipped", reused_result=True, updated_at=timestamp, display_updated_at=timestamp,
                        detail=f"已跳过：该订单及礼品组合已有{'成功' if _verified_success(prior) else '失败或提交'}记录，不重复登记")

    @staticmethod
    def _job_attempts(job):
        for row, item in zip(job["rows"], job["items"]):
            if item["state"] == "skipped":
                continue
            events = item.get("events", [])
            identity = item.get("execution_id") or hashlib.sha256(json.dumps(
                [item["business_key"], events, item.get("source"), item.get("submit_attempted")],
                ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            terminal = [event for event in events if event.get("state") in {"success", "failed", "cancelled"}]
            completed_at = terminal[-1]["at"] if terminal else job["created_at"]
            completed_at = item.get("review_original_completed_at", completed_at)
            yield {"failure_id": identity, "row": row, "item": item, "catalog": job.get("gift_catalog", []),
                   "completed_at": completed_at, "created_at": job["created_at"]}

    def _attempt_records(self):
        """按实际执行去重：换行、重复开始产生的结果副本不增加日志或重登次数。"""
        unique = {}
        records = [json.loads(path.read_text(encoding="utf-8"))
                   for path in sorted((self.directory / "execution_history").glob("*.json"))]
        for path in self._paths():
            records.extend(self._job_attempts(self._load(path)))
        for record in records:
            identity, item = record["failure_id"], record["item"]
            existing = unique.get(identity)
            if (existing is None or _verified_success(item) and not _verified_success(existing["item"])
                    or existing["item"].get("reused_result") and not item.get("reused_result")
                    or (not item.get("reused_result") and item.get("updated_at", "") > existing["item"].get("updated_at", "")
                        and not _verified_success(existing["item"]))):
                unique[identity] = record
        return list(unique.values())

    @staticmethod
    def _retry_reason(item):
        if item["state"] != "failed" or item.get("source") == "manual":
            return "该记录不属于可重登的自动登记失败任务"
        if item.get("submission_status") == "not_submitted":
            return ""
        if item.get("submission_status") is not None:
            return "已有提交尝试或历史证据不足，不能重新登记，请人工核查"
        events = item.get("events", [])
        if (item.get("submit_attempted") is False and item.get("attempts") == 0 and events
                and all(event.get("state") in {"querying", "failed"} for event in events)):
            return ""
        return "已有提交尝试或历史证据不足，不能重新登记，请人工核查"

    def _latest_attempts(self):
        result = {}
        for record in self._attempt_records():
            if record["item"]["state"] == "cancelled":
                continue  # 未执行的取消记录不能取代此前真实失败或成功的防重依据。
            key = task_key(record["row"])
            previous = result.get(key)
            def timestamp(value):
                try:
                    return datetime.fromisoformat(value).timestamp()
                except (TypeError, ValueError):
                    return float("-inf")
            rank = (_verified_success(record["item"]), timestamp(record["completed_at"]), timestamp(record["created_at"]))
            if previous is None or rank > previous[0]:
                result[key] = (rank, record)
        return [entry[1] for entry in result.values()]

    def retry_candidates(self):
        with self.lock:
            candidates = []
            for record in self._latest_attempts():
                item, row = record["item"], record["row"]
                if item["state"] != "failed":
                    continue
                reason = self._retry_reason(item)
                if row["change_address"]:
                    reason = "本版本不支持改地址登记"
                candidates.append({"failure_id": record["failure_id"], "order_no": row["order_no"].strip(),
                                   "task_key": task_key(row), "gift_ids": sorted(set(row.get("gift_ids", []))),
                                   "gift": row["gift"], "detail": item["detail"], "completed_at": record["completed_at"],
                                   "eligible": not reason, "blocked_reason": reason})
            return {"failures": candidates, "busy": bool(self.pending.unfinished_tasks)}

    def cancel_queued(self, keys):
        """持锁取消同订单及同礼品组合的未领取快照，不影响其它组合。"""
        with self.lock:
            if self._closed:
                raise ValueError("工作台已关闭，请重新打开")
            cancelled, count = set(), 0
            for path in self._paths():
                job, changed = self._load(path), False
                for row, item in zip(job["rows"], job["items"]):
                    key = task_key(row)
                    if (key not in keys or item["state"] != "queued" or item.get("submit_attempted") is not False
                            or item.get("attempts") != 0 or item.get("evidence", {}).get("submit_clicked")
                            or item.get("submission_status") not in {None, "not_submitted"}):
                        continue
                    stamp = now()
                    item.update(state="cancelled", detail="主机取消排队，尚未开始平台处理；未提交",
                                updated_at=stamp, submission_status="not_submitted", cancelled_by="host")
                    item.setdefault("events", []).append({"state": "cancelled", "at": stamp,
                                                           "detail": item["detail"], "source": "host"})
                    cancelled.add(key)
                    count += 1
                    changed = True
                if changed:
                    self._save(path, job)
            return cancelled, count

    def _request_path(self, base):
        """取消后再次明确开始创建新快照；后续重复请求沿用该快照。"""
        paths = [base] if base.exists() else []
        paths.extend(self.directory.glob(base.stem + "-next-*.json"))
        return max(paths, key=lambda path: (self._load(path)["created_at"], path.name)) if paths else base

    def retry_all_failed(self, failure_ids):
        """一次明确请求的固定失败集合；重复请求复用任务，不追逐本批次新失败。"""
        if (not isinstance(failure_ids, list) or not failure_ids or len(failure_ids) > 10000
                or any(not isinstance(value, str) or not value or len(value) > 128 for value in failure_ids)
                or len(set(failure_ids)) != len(failure_ids)):
            raise ValueError("请选择有效且不重复的失败记录")
        with self.lock:
            def existing(value):
                path = self._request_path(self.directory / f"retry-{hashlib.sha256(value.encode()).hexdigest()}.json")
                return path.exists() and all(item["state"] != "cancelled" for item in self._load(path)["items"])
            if self.pending.unfinished_tasks and not all(existing(value) for value in failure_ids):
                raise ValueError("请等待当前登记任务结束，再操作批量重登")
            results = []
            for failure_id in failure_ids:
                try:
                    result = self.retry_failed(failure_id, _batch=True)
                    results.append({"failure_id": failure_id, "accepted": True, "reused": result["reused"]})
                except ValueError as exc:
                    results.append({"failure_id": failure_id, "accepted": False, "reason": str(exc)})
            return {"results": results, "row_states": self.states()}

    def retry_failed(self, failure_id, *, _batch=False):
        if not isinstance(failure_id, str) or not failure_id or len(failure_id) > 128:
            raise ValueError("请选择有效的失败记录")
        fingerprint = hashlib.sha256(failure_id.encode()).hexdigest()
        path = self.directory / f"retry-{fingerprint}.json"
        with self.lock:
            if self._closed or self._stopping:
                raise ValueError("工作台正在关闭，不能重新登记")
            path = self._request_path(path)
            if path.exists():
                job = self._load(path)
                if all(item["state"] != "cancelled" for item in job["items"]):
                    return {"task_id": job["id"], "reused": True, "row_states": self.states()}
                path = self.directory / f"retry-{fingerprint}-next-{uuid.uuid4().hex}.json"
            # 独立订单可以继续入同一串行队列；执行时仍逐单核查。
            record = next((entry for entry in self._latest_attempts() if entry["failure_id"] == failure_id), None)
            if record is None:
                raise ValueError("失败记录已更新或该订单已有成功记录，请刷新失败列表")
            reason = self._retry_reason(record["item"])
            if reason:
                raise ValueError(reason)
            row = deepcopy(record["row"])
            if row["change_address"]:
                raise ValueError("本版本不支持改地址登记")
            if not self.configured or not callable(getattr(self.operations, "check_retry_safety", None)):
                raise ValueError("当前平台尚未接入重登核查，不能重新登记")
            registration_rows([row], record["catalog"])
            prepare = getattr(self.operations, "prepare_start", None)
            try:
                if callable(prepare):
                    prepare()
                item = self._item(row)
                item.update(state="queued", detail="主机已请求重登，等待核查历史提交及平台记录",
                            retry_of=failure_id, retry_evidence=deepcopy(record["item"].get("evidence", {})),
                            evidence=deepcopy(record["item"].get("evidence", {})))
                job = {"version": 3, "id": path.stem, "created_at": now(),
                       "rows": [row], "gift_catalog": deepcopy(record["catalog"]), "items": [item]}
                self._save(path, job)
            except Exception as exc:
                if not self.pending.unfinished_tasks:
                    self._release_unused_start()
                reason = str(exc) if isinstance(exc, PlatformOperationError) else "重登环境准备或存档失败"
                raise RegistrationStartError(reason + "；未启动重登") from None
            self.scheduled.add(path.name)
            self.pending.put(path)
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._work, name="registration-worker", daemon=True)
                self.worker.start()
            return {"task_id": job["id"], "reused": False, "row_states": self.states()}

    def daily_records(self, day=None):
        day = day or datetime.now().astimezone().date()
        with self.lock:
            records = []
            for record in self._attempt_records():
                item = record["item"]
                if item["state"] not in {"success", "failed"}:
                    continue
                try:
                    completed = datetime.fromisoformat(record["completed_at"]).astimezone()
                except (TypeError, ValueError):
                    continue
                if completed.date() == day:
                    records.append({"order_no": record["row"]["order_no"].strip(), "state": item["state"],
                                    "internal_order_ids": item.get("evidence", {}).get("verified_record_ids", []) if _verified_success(item) else [],
                                    "detail": item["detail"], "completed_at": completed.isoformat(),
                                    "gift": record["row"]["gift"], "execution_id": record["failure_id"]})
            return day.isoformat(), sorted(records, key=lambda record: (record["completed_at"], record["order_no"]))

    def enqueue_due_reviews(self, current_time):
        """后台调度只把到期的只读复核放进原串行队列；绝不派发登记。"""
        with self.lock:
            if self._closed or self._stopping or not self.configured or self.pending.unfinished_tasks:
                return 0
            scheduled = 0
            for record in self._latest_attempts():
                item = record["item"]
                due = self._review_due_at(item)
                if item["state"] != "failed" or not due or item.get("source") == "manual" or item.get("review_result"):
                    # 已有复核结果亦是完成依据，即使旧备份缺少领取文件也不能重跑。
                    continue
                if datetime.fromisoformat(due) > current_time:
                    continue
                identity = record["failure_id"]
                path = self.directory / "auto_reviews" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
                if path.exists():
                    continue
                prepare = getattr(self.operations, "prepare_start", None)
                try:
                    if callable(prepare):
                        prepare()
                    atomic_write(path, json.dumps({"execution_id": identity, "status": "queued", "due_at": due},
                                                 ensure_ascii=False).encode("utf-8"))
                except Exception:
                    if not self.pending.unfinished_tasks:
                        self._release_unused_start()
                    break
                key = "review:" + identity
                self.scheduled.add(key)
                self.pending.put((key, path))
                scheduled += 1
            if scheduled and (self.worker is None or not self.worker.is_alive()):
                self.worker = threading.Thread(target=self._work, name="registration-worker", daemon=True)
                self.worker.start()
            return scheduled

    def _review_once(self, path):
        review = json.loads(path.read_text(encoding="utf-8"))
        identity = review["execution_id"]
        with self.lock:
            record = next((entry for entry in self._latest_attempts() if entry["failure_id"] == identity), None)
            if record is None or record["item"]["state"] != "failed":
                review.update(status="skipped", detail="已有更新的登记结果，未查询或提交")
                atomic_write(path, json.dumps(review, ensure_ascii=False).encode("utf-8"))
                return
            review.update(status="running", started_at=now())
            atomic_write(path, json.dumps(review, ensure_ascii=False).encode("utf-8"))
        identifiers, success = [], False
        try:
            request = request_from_row(record["row"], record["catalog"])
            after = self.operations.query_order(request.order_no)  # 唯一复核查询入口；没有班牛提交调用。
            validate_snapshot(after, request.order_no)
            evidence = record["item"].get("evidence", {})
            raw = evidence.get("before_snapshot")
            if not raw or raw.get("recipient") is not None or request.recipient is not None:
                raise PlatformOperationError("缺少本次登记的完整原始基线，不能自动确认成功")
            before = OrderSnapshot(raw["order_no"], raw["found"], tuple(GiftRecord(**entry) for entry in raw["records"]))
            validate_snapshot(before, request.order_no)
            receipt = evidence.get("receipt")
            if receipt:
                receipt = SubmissionReceipt(**receipt)
                validate_receipt(receipt)
                success, detail, identifiers = verify_new_records(request, before, after, receipt)
            elif evidence.get("submit_clicked") is True:
                raise PlatformOperationError("缺少有效回执，新增记录无法唯一关联本次任务；保留待人工核查，禁止重提")
            else:
                raise PlatformOperationError("缺少本次提交依据或此前明确未提交，不能自动确认成功")
        except Exception as exc:
            detail = str(exc) if isinstance(exc, PlatformOperationError) else f"自动复核未完成（{type(exc).__name__}）"
        with self.lock:
            # 在同一串行队列内仍重新核对最新执行身份，防止旧失败覆盖新任务。
            latest = next((entry for entry in self._latest_attempts() if task_key(entry["row"]) == task_key(record["row"])), None)
            if latest is None or latest["failure_id"] != identity:
                success, detail, identifiers = False, "已有更新的登记结果，保留原状态", []
            for job_path in self._paths():
                job = self._load(job_path)
                changed = False
                for attempt, item in zip(self._job_attempts(job), [i for i in job["items"] if i["state"] != "skipped"]):
                    if attempt["failure_id"] != identity or item["state"] != "failed":
                        continue
                    item.setdefault("display_updated_at", item.get("updated_at", job["created_at"]))
                    item["review_result"] = {"at": now(), "success": success, "detail": detail}
                    if success:
                        item.update(state="success", detail="自动复核成功：聚水潭新增记录与本次礼赠一致", updated_at=now(),
                                    review_original_completed_at=record["completed_at"])
                        item.setdefault("evidence", {})["verified_record_ids"] = identifiers
                        item["events"].append({"state": "success", "at": now(), "detail": item["detail"], "source": "auto_review"})
                    else:
                        item.update(detail=item["detail"] + "；自动复核：" + detail, updated_at=now())
                    changed = True
                if changed:
                    self._save(job_path, job)
            review.update(status="done", success=success, completed_at=now(), detail=detail,
                          log_day=datetime.fromisoformat(record["completed_at"]).astimezone().date().isoformat())
            atomic_write(path, json.dumps(review, ensure_ascii=False).encode("utf-8"))

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
                for field in ('recipient_name', 'phone', 'address'):
                    record.pop(field, None)
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
            path = self._request_path(path)
            if path.exists():
                previous_job = self._load(path)
                # 旧版仅因同订单的其它礼品而跳过的行，明确开始时创建新任务；旧快照不重置。
                restartable = any(item["state"] == "cancelled" or (
                    item["state"] == "skipped" and item.get("submit_attempted") is False and item.get("attempts") == 0
                    and not item.get("evidence", {}).get("submit_clicked")
                    and not self._existing_result(task_key(row), order_no=row["order_no"].strip()))
                    for row, item in zip(previous_job["rows"], previous_job["items"]))
                if restartable:
                    path = self.directory / f"{fingerprint}-next-{uuid.uuid4().hex}.json"
            reused = path.exists()
            if reused:
                job = self._load(path)
                for row, item in zip(job["rows"], job["items"]):
                    if item["state"] in TERMINAL:
                        prior = self._existing_result(task_key(row), exclude=(path.name, item["row_id"]), order_no=row["order_no"].strip())
                        if prior and _verified_success(prior) and not _verified_success(item):
                            self._reuse(item, prior)
                        # 显式选回旧快照时，让前端显示该快照的终态；终态不会重新派发。
                        item["updated_at"] = now()
                        item["display_updated_at"] = item["updated_at"]
            else:
                job = {"version": 2, "id": path.stem, "created_at": now(), "rows": records,
                       "gift_catalog": deepcopy(gifts), "items": [self._item(row) for row in records]}
                for row, item in zip(job["rows"], job["items"]):
                    prior = self._existing_result(task_key(row), order_no=row["order_no"].strip())
                    if prior:
                        self._reuse(item, prior)
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
                if isinstance(path, tuple):
                    self._review_once(path[1])
                else:
                    self._process(path)
            except Exception:
                # 编排异常也不重新入队；尽力记失败后继续处理其他独立任务。
                try:
                    with self.lock:
                        if isinstance(path, tuple):
                            review = json.loads(path[1].read_text(encoding="utf-8"))
                            review.update(status="interrupted", detail="自动复核发生异常，未重复查询或提交")
                            atomic_write(path[1], json.dumps(review, ensure_ascii=False).encode("utf-8"))
                            continue
                        job = self._load(path)
                        for row, item in zip(job["rows"], job["items"]):
                            if item["state"] not in TERMINAL:
                                item.update(state="failed", detail="任务处理异常，请人工核查；未自动重试登记", updated_at=now())
                                self._schedule_review(item, row)
                        self._save(path, job)
                except (OSError, ValueError):
                    pass
            finally:
                with self.lock:
                    self.scheduled.discard(path[0] if isinstance(path, tuple) else path.name)
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
                previous = self._existing_result(task_key(row), exclude=(path.name, item["row_id"]), order_no=row["order_no"].strip())
                if previous and (not item.get("retry_of") or _verified_success(previous)):
                    self._reuse(item, previous)
                    self._save(path, current)
                    continue
                if item["submit_attempted"]:
                    item.update(state="failed", detail="该任务已尝试提交，禁止自动重复登记")
                    self._schedule_review(item, row)
                    self._save(path, current)
                    continue
                # 领取与取消使用同一把锁，释放锁前落盘，关闭“仍显示排队但已执行”的竞态窗口。
                stamp = now()
                item.update(state="querying", detail="已领取，正在开始订单核查", updated_at=stamp)
                item.setdefault("events", []).append({"state": "querying", "at": stamp, "detail": item["detail"]})
                self._save(path, current)

            def report(state, **updates):
                with self.lock:
                    document = self._load(path)
                    target = document["items"][index]
                    target.setdefault("execution_id", uuid.uuid4().hex)
                    evidence = updates.pop("evidence", None)
                    target.update(state=state, updated_at=now(), **updates)
                    if state == "failed":
                        self._schedule_review(target, row)
                    if evidence:
                        target.setdefault("evidence", {}).update(evidence)
                    target["events"].append({"state": state, "at": now(), "detail": target["detail"]})
                    self._save(path, document)

            guard = None
            if item.get("retry_of"):
                def guard(request, before):
                    check = getattr(self.operations, "check_retry_safety", None)
                    if not callable(check) or check(request, before, item.get("retry_evidence", {})) is not True:
                        raise PlatformOperationError("重登核查未通过；未进行班牛登记")
                    report("querying", detail="主机重登核查通过，班牛暂无该订单任务",
                           evidence={"retry_checked": True})
            Workflow(self.operations).execute(row, job["gift_catalog"], report, before_submit=guard)
