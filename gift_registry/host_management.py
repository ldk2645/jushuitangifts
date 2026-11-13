"""主机人工标记及草稿删除；不修改平台执行历史和防重证据。"""

import hashlib
import json
import uuid
from copy import deepcopy

from .registration import now, row_signature, legacy_row_signature, task_key
from .sessions import atomic_write


class StatusConflict(ValueError):
    """A stale per-execution request; HTTP maps this to 409."""


class HostManagement:
    def __init__(self, rows, jobs):
        self.rows, self.jobs = rows, jobs
        self.path = rows.path.with_name("manual_status.json")

    def _read(self):
        if not self.path.exists():
            return {"events": [], "marks": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    @staticmethod
    def _digest(signature):
        return hashlib.sha256(signature.encode("utf-8")).hexdigest()

    def _task_marks(self, states):
        """旧人工标记从原签名/快照解析礼品组合，保留审计文件且不扩散到其它礼品。"""
        document = self._read()
        marks = [*document.get("events", []), *document["marks"].values()]
        signatures = {self._digest(state["signature"]): state["task_key"]
                      for state in states.values()}
        unresolved = [m for m in marks if not m.get("task_key") and m["signature"] not in signatures]
        if unresolved:
            for path in self.jobs._paths():
                for row in self.jobs._load(path)["rows"]:
                    signatures[self._digest(row_signature(row))] = task_key(row)
                    signatures[self._digest(legacy_row_signature(row))] = task_key(row)
        result = {}
        for mark in marks:
            key = mark.get("task_key") or signatures.get(mark["signature"])
            if key and (key not in result or mark["at"] >= result[key]["at"]):
                result[key] = mark
        return result

    def states(self, identifiers=None):
        with self.rows.lock, self.jobs.lock:
            states = self.jobs.states(identifiers)
            marks = self._read()["marks"]
            tasks = self._task_marks(states)
            blocked = self._blocked_executions()
            for identifier, state in states.items():
                state['manual_editable'] = self._editable(state, blocked)
                task_mark = tasks.get(state["task_key"], {})
                mark = marks.get(identifier, {})
                if task_mark.get("status") == "success":
                    mark = task_mark
                elif (mark.get("status") == "success" or
                      (mark.get("signature") != self._digest(state["signature"]) and
                       not (task_mark.get('row_id') == identifier and task_mark == mark))):
                    continue
                if mark.get("status") and state["state"] in {"success", "failed", "skipped", "manual_required"}:
                    state["manual_status"] = mark["status"]
                    state["manual_updated_at"] = mark["at"]
            return states

    def retry_candidates(self):
        with self.rows.lock, self.jobs.lock:
            states = self.states()
            successes = {key for key, mark in self._task_marks(states).items() if mark.get("status") == "success"}
            tasks = {task_key(row) for row in self.rows.document["rows"]
                      if (state := states.get(row["id"], {})).get("signature") == row_signature(row)
                      and (state.get("state") == "failed" or state.get("retry_cancelled"))
                      and state.get("manual_status") not in {"success", "pending"}
                      and task_key(row) not in successes}
            result = self.jobs.retry_candidates()
            result["failures"] = [f for f in result["failures"] if f["task_key"] in tasks]
            return result

    def retry(self, identifiers, *, batch=False):
        with self.rows.lock, self.jobs.lock:
            if not isinstance(identifiers, list) or not identifiers or any(not isinstance(i, str) for i in identifiers):
                raise ValueError("请选择当前表格中的失败订单")
            if batch:
                existing = self.jobs.existing_retry_batch(identifiers)
                if existing is not None:
                    return existing
            candidates = self.retry_candidates()['failures']
            allowed = {f['failure_id'] for f in candidates if not batch or f['eligible']}
            if batch:
                return self.jobs.accept_retry_batch(identifiers, allowed)
            if any(identifier not in allowed for identifier in identifiers):
                raise ValueError("失败列表已变化：仅可重登当前表格中的失败订单，人工成功订单已排除，请刷新")
            result = (self.jobs.retry_all_failed(identifiers) if batch else self.jobs.retry_failed(identifiers[0]))
            result["row_states"] = self.states()
            return result

    def _selected(self, identifiers):
        if (not isinstance(identifiers, list) or not identifiers or len(identifiers) > 10000
                or any(not isinstance(value, str) for value in identifiers)
                or len(set(identifiers)) != len(identifiers)):
            raise ValueError("请选择有效且不重复的草稿行")
        records = {row["id"]: row for row in self.rows.document["rows"]}
        if any(identifier not in records for identifier in identifiers):
            raise ValueError("所选草稿行已变化，请刷新")
        return [records[identifier] for identifier in identifiers]

    def _blocked_executions(self):
        # Queued/running delayed claims exclude this execution. Other orders
        # and scheduled future reviews do not block an already terminal row.
        blocked = set()
        for path in (self.jobs.directory / 'auto_reviews').glob('*.json'):
            review = json.loads(path.read_text(encoding='utf-8'))
            if review.get('status') in {'queued', 'running'}:
                blocked.add(review.get('execution_id'))
        return blocked

    def _editable(self, state, blocked=None):
        if state.get('state') not in {'success', 'failed', 'skipped', 'manual_required'}:
            return False
        if state.get('execution_id') in (self._blocked_executions() if blocked is None else blocked):
            return False
        order = json.loads(state['signature'])[1] if state.get('signature') else ''
        return self.jobs.verification.active_order != order

    def mark(self, identifiers, status, *, targets=None, draft_revision=None, actor='host'):
        with self.rows.lock, self.jobs.lock:
            return self._mark(identifiers, status, targets=targets, draft_revision=draft_revision, actor=actor)

    def _mark(self, identifiers, status, *, targets, draft_revision, actor):
        if not isinstance(status, str) or status not in {"", "success", "failed", "pending"}:
            raise ValueError("人工状态不受支持")
        if draft_revision is not None and draft_revision != self.rows.document['revision']:
            raise StatusConflict('草稿已变化，请刷新后重试')
        selected, states = self._selected(identifiers), self.jobs.states()
        blocked = self._blocked_executions()
        if targets is not None and (not isinstance(targets, list) or len(targets) != len(selected)
                                    or any(not isinstance(t, dict) for t in targets)):
            raise StatusConflict('缺少逐单执行版本，请刷新后重试')
        for row in selected:
            state = states.get(row["id"], {})
            if state.get("signature") != row_signature(row) or not self._editable(state, blocked):
                raise StatusConflict("仅可标记内容未修改且本单核验已结束的订单，请刷新")
            if targets is not None:
                target = next((t for t in targets if t.get('row_id') == row['id']), {})
                expected = dict(row_id=row['id'], task_id=state['task_id'], execution_id=state.get('execution_id'),
                                row_signature=state['signature'], state_version=state.get('state_version', 0))
                if any(target.get(k) != v for k, v in expected.items()):
                    raise StatusConflict('订单执行身份或状态版本已变化；本次整组选中行均未修改')
            if status == 'success' and (state.get('submission_status') == 'uncertain' or state['state'] == 'manual_required'):
                raise StatusConflict('提交结果不确定，只能标记待核查；人工显示不能替代平台核验')
        document = deepcopy(self._read())
        for row in selected:
            mark = {"row_id": row["id"], "signature": self._digest(row_signature(row)), "status": status,
                    "at": now(), "original_state": states[row["id"]]["state"],
                    "execution_updated_at": states[row["id"]]["updated_at"],
                    "task_key": task_key(row), 'actor': actor,
                    'old_status': document['marks'].get(row['id'], {}).get('status', ''),
                    'new_status': status, 'execution_id': states[row['id']].get('execution_id'),
                    'state_version': states[row['id']].get('state_version', 0)}
            document["events"].append(mark)
            document["marks"][row["id"]] = mark
        atomic_write(self.path, json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))
        self.jobs.changed.set()
        return {"row_states": self.states(), "count": len(selected)}

    def cancel(self, targets):
        # 调用方持有草稿与队列锁；拒绝旧页面误取消后来新建的任务。
        if (not isinstance(targets, list) or not targets or len(targets) > 10000
                or any(not isinstance(target, dict) or not isinstance(target.get("row_id"), str)
                       or not isinstance(target.get("task_id"), str) for target in targets)):
            raise ValueError("请选择有效的排队任务")
        selected = self._selected([target["row_id"] for target in targets])
        states, keys, results = self.jobs.states(), set(), []
        for row, target in zip(selected, targets):
            state = states.get(row["id"], {})
            eligible = (state.get("signature") == row_signature(row) and state.get("task_id") == target["task_id"]
                        and state.get("state") == "queued")
            if eligible:
                keys.add(task_key(row))
            results.append({"row_id": row["id"], "cancelled": False,
                            "reason": "订单已开始处理、已结束或任务已变化，未取消"})
        cancelled, count = self.jobs.cancel_queued(keys)
        after = self.states()
        for row, target, result in zip(selected, targets, results):
            state = after.get(row["id"], {})
            if (task_key(row) in cancelled and state.get("state") == "cancelled"
                    and state.get("task_id") == target["task_id"] and state.get("signature") == row_signature(row)):
                result.update(cancelled=True, reason="已取消同订单及同礼品组合的未开始排队任务")
        return {"results": results, "count": len(cancelled), "task_count": count, "row_states": after}

    def delete(self, identifiers, all_rows=False):
        if type(all_rows) is not bool:
            raise ValueError("删除范围格式不正确")
        if self.jobs.pending.unfinished_tasks:
            raise ValueError("请等待登记及复核结束后删除草稿订单")
        document = self.rows.read()
        selected = document["rows"] if all_rows else self._selected(identifiers)
        if not selected:
            return {**document, "removed": 0, "row_states": self.states()}
        # 完整草稿仅保存在受本机数据策略保护的备份目录，不进入普通日志或 Git。
        backup = self.rows.path.parent / "draft_archive" / f"before-host-delete-{uuid.uuid4().hex}.json"
        atomic_write(backup, json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))
        removed = {row["id"] for row in selected}
        kept = [row for row in document["rows"] if row["id"] not in removed]
        self.rows.write(document["revision"], kept)
        return {**self.rows.read(), "removed": len(removed), "row_states": self.states()}
