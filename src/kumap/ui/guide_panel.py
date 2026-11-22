"""探索向导面板：规划、当前目标、要点提示、完成/跳过、结束汇总。"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from ..core.models import Layer


class GuidePanel(QWidget):
    plan_requested = Signal(object, str)   # layers(set), floor(str)
    complete_requested = Signal()
    skip_requested = Signal()
    finish_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(300)
        self._layer_checks: dict[str, QCheckBox] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(7)

        title = QLabel("探索向导", self)
        title.setObjectName("TitleLabel")
        lay.addWidget(title)
        self.status = QLabel("等待识图/手动定位后规划路线", self)
        self.status.setObjectName("Disclaimer")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        lay.addWidget(QLabel("包含点位", self))
        self.layer_host = QWidget(self)
        self.layer_layout = QVBoxLayout(self.layer_host)
        self.layer_layout.setContentsMargins(0, 0, 0, 0)
        self.layer_layout.setSpacing(2)
        lay.addWidget(self.layer_host)

        row = QHBoxLayout()
        row.addWidget(QLabel("分层", self))
        self.floor = QComboBox(self)
        self.floor.addItem("地上", "")
        row.addWidget(self.floor, 1)
        lay.addLayout(row)

        self.btn_plan = QPushButton("根据当前位置规划", self)
        self.btn_plan.clicked.connect(lambda: self.plan_requested.emit(
            self.selected_layers(), str(self.floor.currentData() or "")))
        lay.addWidget(self.btn_plan)

        self.progress = QLabel("尚未开始", self)
        self.progress.setObjectName("ProgressValue")
        lay.addWidget(self.progress)

        self.target_name = QLabel("—", self)
        f = self.target_name.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() + 1)
        self.target_name.setFont(f)
        self.target_name.setWordWrap(True)
        lay.addWidget(self.target_name)
        self.target_meta = QLabel("", self)
        self.target_meta.setObjectName("Disclaimer")
        self.target_meta.setWordWrap(True)
        lay.addWidget(self.target_meta)
        self.target_dist = QLabel("", self)
        lay.addWidget(self.target_dist)
        self.hint = QLabel("", self)
        self.hint.setWordWrap(True)
        self.hint.setObjectName("Disclaimer")
        lay.addWidget(self.hint)

        row2 = QHBoxLayout()
        self.btn_done = QPushButton("✓ 完成当前", self)
        self.btn_done.clicked.connect(self.complete_requested.emit)
        self.btn_skip = QPushButton("跳过", self)
        self.btn_skip.clicked.connect(self.skip_requested.emit)
        row2.addWidget(self.btn_done)
        row2.addWidget(self.btn_skip)
        lay.addLayout(row2)

        self.btn_finish = QPushButton("结束并汇总", self)
        self.btn_finish.clicked.connect(self.finish_requested.emit)
        lay.addWidget(self.btn_finish)
        lay.addStretch(1)
        self._set_active(False)

    # ------------------------------------------------------------------ 数据
    def set_layers(self, layers: list[Layer], checked: set[str] | None = None) -> None:
        while self.layer_layout.count():
            item = self.layer_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._layer_checks.clear()
        default = checked or {"chest", "puzzle", "quest", "anchor"}
        for layer in layers:
            cb = QCheckBox(layer.label, self.layer_host)
            cb.setChecked(layer.key in default)
            cb.setProperty("layer_key", layer.key)
            self.layer_layout.addWidget(cb)
            self._layer_checks[layer.key] = cb

    def set_floors(self, floors: list[tuple[str, str]], current: str = "") -> None:
        self.floor.blockSignals(True)
        self.floor.clear()
        for label, value in floors or [("地上", "")]:
            self.floor.addItem(label, value)
        idx = self.floor.findData(current)
        self.floor.setCurrentIndex(idx if idx >= 0 else 0)
        self.floor.blockSignals(False)

    def selected_layers(self) -> set[str]:
        return {k for k, cb in self._layer_checks.items() if cb.isChecked()}

    # ------------------------------------------------------------------ 状态
    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def set_progress(self, done: int, total: int) -> None:
        self.progress.setText(f"进度 {done}/{total}" if total else "尚未开始")

    def set_target(self, index: int, total: int, name: str, meta: str,
                   distance: str, hint: str, arrived: bool = False) -> None:
        self.target_name.setText(f"{index}/{total} · {name}" if total else name)
        self.target_meta.setText(meta)
        self.target_dist.setText(distance)
        self.hint.setText(hint)
        self.hint.setStyleSheet("color:#4fd1c5;" if arrived else "")
        self._set_active(True)

    def clear_target(self, text: str = "尚未开始") -> None:
        self.target_name.setText("—")
        self.target_meta.setText("")
        self.target_dist.setText("")
        self.hint.setText("")
        self.hint.setStyleSheet("")
        self.progress.setText(text)
        self._set_active(False)

    def set_planning(self, on: bool) -> None:
        self.btn_plan.setEnabled(not on)
        self.btn_plan.setText("规划中…" if on else "根据当前位置规划")

    def _set_active(self, on: bool) -> None:
        self.btn_done.setEnabled(on)
        self.btn_skip.setEnabled(on)
        self.btn_finish.setEnabled(True)
