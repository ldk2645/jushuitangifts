"""工作台内的持久化定时任务：北京时间 01:00 归档，失败后延迟只读复核。"""
import hashlib
import json
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from .daily_export import export_daily
from .registration import row_signature
from .sessions import atomic_write

CHINA = timezone(timedelta(hours=8))


def china_now():
    return datetime.now(CHINA)


class MaintenanceService:
    def __init__(self, jobs, rows, directory, *, clock=china_now):
        self.jobs, self.rows, self.directory, self.clock = jobs, rows, Path(directory), clock
        self.path = self.directory / "maintenance.json"
        self.exports = self.directory / "exports"
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.thread = None

    def _read(self):
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != 1:
                raise ValueError("后台定时任务配置版本不正确")
            return data
        current = self.clock().astimezone(CHINA)
        next_day = current.date() + timedelta(days=int(current.hour >= 1))
        return {"version": 1, "next_day": next_day.isoformat(), "last_error": "", "review_export_cursor": ""}

    def _save(self, state):
        atomic_write(self.path, json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"))

    def status(self):
        with self.lock:
            state = self._read()
            return {"next_run": state["next_day"] + "T01:00:00+08:00", "last_export_day": state.get("last_export_day"),
                    "last_removed_count": state.get("last_removed_count", 0), "last_error": state.get("last_error", ""),
                    "export_directory": str(self.exports)}

    def _export(self, day):
        day_text, records = self.jobs.daily_records(day)
        path = self.exports / f"gift-registration-{day_text}.xlsx"
        atomic_write(path, export_daily(day_text, records))
        return path

    def tick(self):
        with self.lock:
            state = self._read()
            if not self.path.exists():
                self._save(state)  # 首次启用从下一个 01:00 开始；后续重启按此日期补做。
            current = self.clock().astimezone(CHINA)
            self.jobs.enqueue_due_reviews(current)
            try:
                # 与普通开始登记保持同样的锁顺序；清理不会与快照创建交错。
                with self.rows.lock, self.jobs.lock:
                    if self.jobs.pending.unfinished_tasks:
                        return
                    # 延迟复核改变已导出的历史结果时，更新对应日期文件。
                    reviews = [json.loads(path.read_text(encoding="utf-8"))
                               for path in (self.jobs.directory / "auto_reviews").glob("*.json")]
                    changed = sorted((r for r in reviews if r.get("status") == "done"
                                      and r.get("completed_at", "") > state.get("review_export_cursor", "")),
                                     key=lambda r: r["completed_at"])
                    for review in changed:
                        day = datetime.fromisoformat(review["log_day"]).date()
                        if (self.exports / f"gift-registration-{day.isoformat()}.xlsx").exists():
                            self._export(day)
                        state["review_export_cursor"] = review["completed_at"]
                    cutoff = datetime.combine(datetime.fromisoformat(state["next_day"]).date(), time(1), CHINA)
                    if current >= cutoff:
                        day = cutoff.date() - timedelta(days=1)
                        self._export(day)  # 导出失败时不能进入表格清理。
                        from .host_management import HostManagement
                        states = HostManagement(self.rows, self.jobs).states()
                        document = self.rows.read()
                        kept, removed = [], []
                        for row in document["rows"]:
                            status = states.get(row["id"], {})
                            eligible = (status.get("state") == "success" and not status.get("manual_status")
                                        and status.get("signature") == row_signature(row)
                                        and datetime.fromisoformat(status["updated_at"]) <= cutoff)
                            (removed if eligible else kept).append(row)
                        if removed:
                            backup = self.directory / "draft_archive" / f"before-cleanup-{cutoff.date().isoformat()}.json"
                            payload = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
                            if backup.exists() and backup.read_bytes() != payload:
                                # 上次备份后清理可能失败；保留旧备份并为当前草稿保存独立快照。
                                digest = hashlib.sha256(payload).hexdigest()
                                backup = backup.with_name(f"{backup.stem}-r{document['revision']}-{digest}.json")
                            if not backup.exists():
                                atomic_write(backup, payload)
                            elif backup.read_bytes() != payload:
                                raise ValueError("清理前草稿备份不一致")
                            self.rows.write(document["revision"], kept)
                        state.update(next_day=(cutoff.date() + timedelta(days=1)).isoformat(),
                                     last_export_day=day.isoformat(), last_removed_count=len(removed))
                    state["last_error"] = ""
                    self._save(state)
            except Exception as exc:
                state["last_error"] = f"后台归档未完成（{type(exc).__name__}），保留表格并稍后重试"
                self._save(state)

    def start(self):
        if self.thread is not None:
            return
        with self.lock:
            if not self.path.exists():
                self._save(self._read())
        def work():
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception:
                    pass  # 下次定时检查继续；不触发登记重试，不记录敏感异常原文。
                self.stop.wait(30)
        self.thread = threading.Thread(target=work, name="gift-maintenance", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
