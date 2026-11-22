"""屏幕框选：为识图设定「游戏小地图」所在区域。

只在桌面顶层覆盖一层半透明遮罩供你拖框，读取的是你自己的屏幕操作，
不涉及游戏窗口句柄、内存或输入注入。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QDialog, QLabel


class _Overlay(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        geo = QApplication.primaryScreen().virtualGeometry()
        self.setGeometry(geo)
        self.setCursor(Qt.CrossCursor)
        self._start: QPoint | None = None
        self._end: QPoint | None = None
        self.result_rect: QRect | None = None
        tip = QLabel("按住鼠标左键，把游戏里的小地图框起来；按 Esc 取消", self)
        tip.setStyleSheet("background:#20252d;color:#e6e9ef;padding:8px 12px;border-radius:8px;")
        tip.adjustSize()
        tip.move(24, 24)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._start = event.position().toPoint()
            self._end = self._start

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._start is not None:
            self._end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._start is None:
            return
        self._end = event.position().toPoint()
        rect = QRect(self._start, self._end).normalized()
        self._start = None
        if rect.width() >= 8 and rect.height() >= 8:
            self.result_rect = rect.translated(self.geometry().topLeft())
            self.accept()
        else:
            self.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.result_rect = None
            self.reject()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if self._start and self._end:
            rect = QRect(self._start, self._end).normalized()
            p.setCompositionMode(QPainter.CompositionMode_Clear)
            p.fillRect(rect, Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.setPen(QPen(QColor("#4fd1c5"), 2))
            p.drawRect(rect)
            p.setPen(QColor("#e6e9ef"))
            p.drawText(rect.adjusted(4, -18, 0, 0), f"{rect.width()} × {rect.height()}")
        p.end()


def pick_region() -> QRect | None:
    dlg = _Overlay()
    dlg.exec()
    return dlg.result_rect
