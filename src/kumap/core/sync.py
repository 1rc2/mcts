"""进度双向同步引擎。

pull：库街区账号 → 本地（已标记完成的点位在本工具里显示为已完成）
push：本地手动标记 → 库街区账号（一键回传待发送队列）

网络操作全部在子线程执行，结果通过信号回主线程落盘；
云端不可用（未登录 / 未配置端点 / 断网）时静默降级为本地模式，
所有本地功能照常，不阻塞、不弹窗骚扰。
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QThread, Signal

from .marker_store import MarkerStore
from .provider import DataProvider, KuroApiProvider, ProviderError


@dataclass
class SyncReport:
    ok: bool = True
    pulled: int = 0
    conflicts: int = 0
    pushed: int = 0
    message: str = ""
    skipped: bool = False

    def summary(self) -> str:
        if self.skipped:
            return self.message or "本地模式：未启用云端同步"
        parts = []
        if self.pulled:
            parts.append(f"拉取 {self.pulled} 条")
        if self.pushed:
            parts.append(f"回传 {self.pushed} 条")
        if self.conflicts:
            parts.append(f"{self.conflicts} 条以本地较新为准")
        if not parts:
            parts.append("两端进度已一致")
        return " · ".join(parts)


class _NetWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent: QObject | None = None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # pragma: no cover - 依赖网络
        try:
            self.result.emit(self._fn())
        except ProviderError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"同步请求异常：{exc}")


class SyncEngine(QObject):
    progress = Signal(str)
    finished = Signal(object)      # SyncReport
    busy_changed = Signal(bool)

    def __init__(self, store: MarkerStore, conflict: str = "newer",
                 parent: QObject | None = None):
        super().__init__(parent)
        self.store = store
        self.conflict = conflict
        self.provider: DataProvider | None = None
        self._workers: list[_NetWorker] = []

    def set_provider(self, provider: DataProvider | None) -> None:
        self.provider = provider

    def set_conflict(self, conflict: str) -> None:
        self.conflict = conflict if conflict in ("newer", "remote") else "newer"

    def cloud_enabled(self) -> bool:
        return bool(self.provider and isinstance(self.provider, KuroApiProvider)
                    and getattr(self.provider, "supports_cloud", False))

    # ---------------------------------------------------------- 纯逻辑（可测试）
    def merge_pull(self, remote: dict[str, bool]) -> SyncReport:
        applied, conflicts = self.store.apply_remote(remote, conflict=self.conflict)
        return SyncReport(pulled=applied, conflicts=conflicts, message="已同步云端标记")

    def build_push_payload(self) -> dict[str, bool]:
        return self.store.pending()

    def confirm_push(self, pushed: dict[str, bool]) -> SyncReport:
        self.store.mark_pushed(pushed.keys())
        return SyncReport(pushed=len(pushed), message="回传完成")

    # ---------------------------------------------------------- 异步入口
    def pull(self) -> bool:
        if not self.cloud_enabled():
            self.finished.emit(SyncReport(skipped=True, message="本地模式：未登录库街区，仅保留本机进度"))
            return False
        provider = self.provider
        self._start(lambda: provider.fetch_markers(), self._on_pull_done, "正在拉取库街区云端标记…")
        return True

    def push(self) -> bool:
        if not self.cloud_enabled():
            pend = self.store.pending_count()
            self.finished.emit(SyncReport(
                skipped=True,
                message=f"本地模式：{pend} 条标记已保存在本机（未登录库街区，不回传云端）"))
            return False
        delta = self.store.pending()
        if not delta:
            self.finished.emit(SyncReport(message="没有待回传的改动"))
            return False
        provider = self.provider
        self._start(lambda: provider.push_markers(delta),
                    lambda _r: self._on_push_done(delta), "正在回传本工具标记到库街区…")
        return True

    def sync(self, do_push: bool = True) -> None:
        """拉取 + （可选）回传：一键双向。"""
        if not self.cloud_enabled():
            self.finished.emit(SyncReport(skipped=True, message="本地模式：进度仅保存在本机"))
            return
        self._pending_push = do_push
        self.pull()

    def _start(self, fn, on_done, label: str) -> None:
        if any(w.isRunning() for w in self._workers):
            self.progress.emit("已有同步任务在进行中，请稍候…")
            return
        self.progress.emit(label)
        self.busy_changed.emit(True)
        worker = _NetWorker(fn, self)
        worker.result.connect(lambda r: self._finish(worker, on_done, r))
        worker.failed.connect(lambda msg: self._fail(worker, msg))
        worker.finished.connect(lambda: self._reap(worker))
        self._workers.append(worker)
        worker.start()

    def _finish(self, worker: _NetWorker, on_done, result) -> None:
        try:
            on_done(result)
        finally:
            self.busy_changed.emit(any(w.isRunning() for w in self._workers))

    def _fail(self, worker: _NetWorker, msg: str) -> None:
        self.progress.emit(msg)
        self.finished.emit(SyncReport(ok=False, message=msg))
        self.busy_changed.emit(any(w.isRunning() for w in self._workers))

    def _reap(self, worker: _NetWorker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        worker.deleteLater()
        self.busy_changed.emit(any(w.isRunning() for w in self._workers))

    def _on_pull_done(self, remote: dict[str, bool]) -> None:
        report = self.merge_pull(remote or {})
        if getattr(self, "_pending_push", False):
            self._pending_push = False
            if self.store.pending_count():
                self._last_pull_report = report
                self.push()
                return
        self._emit(report)

    def _on_push_done(self, delta: dict[str, bool]) -> None:
        report = self.confirm_push(delta)
        prev = getattr(self, "_last_pull_report", None)
        self._last_pull_report = None
        if prev:
            report.pulled = prev.pulled
            report.conflicts = prev.conflicts
        self._emit(report)

    def _emit(self, report: SyncReport) -> None:
        self.progress.emit(report.summary())
        self.finished.emit(report)

    def shutdown(self) -> None:
        for worker in list(self._workers):
            if worker.isRunning():
                worker.wait(3000)
