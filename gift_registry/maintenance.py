"""工作台内的持久化定时任务：北京时间 01:00 归档，失败后延迟只读复核。"""
import hashlib
import json
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from .daily_export import export_daily
from .registration import row_signature
from .sessions import atomic_write
from .diagnostics import diagnose

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
        self.last_fault = None
        from .registration import RegistrationQueue
        from .task_day_export import TaskDayExports
        self.task_exports = TaskDayExports(jobs, rows, directory, clock=clock) if isinstance(jobs, RegistrationQueue) else None

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("后台定时任务配置版本不正确")
            datetime.strptime(data['next_day'], '%Y-%m-%d')
            return data
        except FileNotFoundError:
            pass
        current = self.clock().astimezone(CHINA)
        next_day = current.date() + timedelta(days=int(current.hour >= 1))
        return {"version": 1, "next_day": next_day.isoformat(), "last_error": "", "review_export_cursor": ""}

    def _save(self, state):
        atomic_write(self.path, json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"))

    def repair_cursor(self, *, expected_sha256, next_day, review_export_cursor=''):
        """Explicit maintenance action on an offline copy; no inferred cleanup cursor."""
        datetime.strptime(next_day, '%Y-%m-%d')
        if review_export_cursor:
            datetime.fromisoformat(review_export_cursor)
        with self.lock:
            raw = self.path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != expected_sha256:
                raise ValueError('维护文件已变化；未修复')
            backup = self.directory / 'maintenance_backups' / (digest + '.json')
            if backup.exists() and backup.read_bytes() != raw:
                raise ValueError('维护备份不一致；未修复')
            if not backup.exists():
                atomic_write(backup, raw)
            self._save({'version':1, 'next_day':next_day, 'review_export_cursor':review_export_cursor,
                        'last_error':'', 'repaired_from_sha256':digest})
            self.last_fault = None
            return {'backup_sha256':digest, 'next_day':next_day}

    def status(self):
        with self.lock:
            try:
                state = self._read()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.last_fault = diagnose(self.directory, 'maintenance', 'read_cursor', exc)
                state = {}
            return {"next_run": state.get("next_day", "") + "T01:00:00+08:00" if state else None,
                    "health": "degraded" if self.last_fault else "ok", "needs_attention": bool(self.last_fault),
                    "diagnostic": self.last_fault, "archive_enabled": bool(state) and not self.last_fault,
                    "last_export_day": state.get("last_export_day"),
                    "last_removed_count": state.get("last_removed_count", 0), "last_error": state.get("last_error", ""),
                    "export_directory": str(self.exports)}

    def _export(self, day):
        day_text, records = self.jobs.daily_records(day)
        path = self.exports / f"gift-registration-{day_text}.xlsx"
        atomic_write(path, export_daily(day_text, records))
        return path

    def tick(self):
        # Read-only reviews do not depend on the archive cursor or archive lock.
        current = self.clock().astimezone(CHINA)
        try:
            self.jobs.enqueue_due_reviews(current)
        except Exception as exc:
            diagnose(self.directory, 'maintenance', 'schedule_reviews', exc)
        if self.task_exports is not None:
            try:
                self.task_exports.tick()
            except (OSError,ValueError) as exc:
                diagnose(self.directory,'task_export','reconcile',exc)
        self._archive_tick()

    def reconcile_archive(self, expected_revision):
        """Explicit operator action; never called by the timer or HTTP status.

        Review the cursor and draft revision before calling on a maintenance copy.
        A corrupt cursor remains blocked; it must be repaired separately.
        """
        self.jobs.enqueue_due_reviews(self.clock().astimezone(CHINA))
        self._cleanup_executions = set()
        self._cleanup_tokens = {}
        # Legacy explicit cleanup requires both a trustworthy cursor and a V4
        # final for the original batch. Timer ticks still never delete drafts.
        try:
            state=self._read()
            cutoff=datetime.combine(datetime.fromisoformat(state['next_day']).date(),time(1),CHINA)
            if self.task_exports is not None and self.clock().astimezone(CHINA)>=cutoff and not self.jobs.pending.unfinished_tasks:
                self.task_exports.tick()
                jobs,marks,_=self.task_exports.snapshot()
                from .business_day import job_day
                from .task_day_export import digest
                for day in {job_day(j) for j in jobs} - {None}:
                    batches=[j for j in jobs if job_day(j)==day]
                    manifest=self.task_exports.read_manifest(day)
                    if manifest.get('export_state')=='final' and manifest.get('source_version')==digest([batches,marks]):
                        file=self.task_exports.exports/manifest['filename']
                        if file.exists() and hashlib.sha256(file.read_bytes()).hexdigest()==manifest.get('sha256'):
                            self._cleanup_executions.update(i.get('execution_id') for j in batches for i in j['items'])
                            self._cleanup_tokens[day]=self.task_exports.token(day)
        except (OSError,ValueError,KeyError,TypeError):
            return  # No destructive maintenance can proceed without proof.
        self._archive_tick(expected_revision=expected_revision)

    def _archive_tick(self, expected_revision=None):
        current = self.clock().astimezone(CHINA)
        with self.lock:
            try:
                state = self._read()
                if not self.path.exists():
                    self._save(state)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.last_fault = diagnose(self.directory, 'maintenance', 'read_cursor', exc)
                return
            if expected_revision is None and self.task_exports is not None:
                # V4 scheduling has its own manifest and never consumes the old
                # completion-day cursor. Keep the latter intact for rollback.
                self.last_fault = None
                return
            try:
                # 与普通开始登记保持同样的锁顺序；清理不会与快照创建交错。
                with self.rows.lock, self.jobs.lock:
                    if expected_revision is not None and self.rows.read()['revision'] != expected_revision:
                        raise ValueError('草稿版本已变化，未执行维护清理')
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
                                        and status.get('execution_id') in getattr(self,'_cleanup_executions',set())
                                        and status.get("signature") == row_signature(row)
                                        and datetime.fromisoformat(status["updated_at"]) <= cutoff)
                            (removed if eligible else kept).append(row)
                        # A clock tick may export, but never authorizes deleting a draft.
                        # Explicit archive reconciliation is a separate maintenance action.
                        if removed and expected_revision is not None:
                            if any(self.task_exports.token(day)!=token for day,token in self._cleanup_tokens.items()):
                                raise ValueError('报表源版本已变化，未清理草稿')
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
                                     last_export_day=day.isoformat(), last_removed_count=len(removed) if expected_revision is not None else 0,
                                     cleanup_candidates=len(removed))
                    state["last_error"] = ""
                    self._save(state)
                    self.last_fault = None
            except Exception as exc:
                self.last_fault = diagnose(self.directory, 'maintenance', 'archive', exc)
                state["last_error"] = f"后台归档未完成（{type(exc).__name__}），保留表格并稍后重试"
                try:
                    self._save(state)
                except OSError as save_exc:
                    diagnose(self.directory, 'maintenance', 'save_error', save_exc)

    def start(self):
        if self.thread is not None:
            return
        def work():
            while not self.stop.is_set():
                self.jobs.changed.clear()
                try:
                    self.tick()
                except Exception as exc:
                    self.last_fault = diagnose(self.directory, 'maintenance', 'loop', exc)
                # Event-triggered completion with a bounded debounce and timer
                # reconciliation; not gated on registration queue emptiness.
                if self.stop.wait(.25): break
                self.jobs.changed.wait(30)
        self.thread = threading.Thread(target=work, name="gift-maintenance", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.jobs.changed.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
