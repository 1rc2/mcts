"""系统托盘：最小化常驻、右键快捷操作。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .. import __app_name__

INFO_ICON = QSystemTrayIcon.MessageIcon.Information


def make_icon(theme: str = "dark") -> QIcon:
    """程序内自绘图标，不引入图片资源，体积小。"""
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setBrush(QColor("#4fd1c5"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setBrush(QColor("#0d1117"))
    p.drawEllipse(20, 16, 16, 16)
    p.setPen(QPen(QColor("#0d1117"), 6, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(28, 30, 28, 50)
    p.drawLine(28, 38, 44, 30)
    p.end()
    return QIcon(pix)


class TrayController:
    def __init__(self, window):
        self.window = window
        self.icon = QSystemTrayIcon(make_icon(), window)
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self.menu = QMenu(window)
        act_show = QAction("显示主窗口", self.menu)
        act_show.triggered.connect(self._show)
        act_map = QAction("官方地图模式（隐藏主窗口）", self.menu)
        act_map.triggered.connect(lambda: window.enter_map_only())
        act_guide = QAction("探索向导（自动规划路线）", self.menu)
        act_guide.triggered.connect(lambda: window.start_guide_ui())
        act_compact = QAction("紧凑悬浮模式", self.menu)
        act_compact.triggered.connect(lambda: window._set_compact(not window.ui.get("compact", False)))
        act_vision = QAction("开启 / 关闭识图", self.menu)
        act_vision.triggered.connect(lambda: window._toggle_vision(not window.cfg.vision.get("enabled", False)))
        act_hide_done = QAction("隐藏已完成点位", self.menu)
        act_hide_done.triggered.connect(lambda: window._set_hide_completed(not window.canvas.hide_completed))
        act_top = QAction("窗口置顶", self.menu)
        act_top.triggered.connect(lambda: window._set_topmost(not window.ui.get("always_on_top", True)))
        act_quit = QAction("退出程序", self.menu)
        act_quit.triggered.connect(window._quit)
        for a in (act_show, act_map, act_guide, act_compact, act_vision, act_hide_done, act_top):
            self.menu.addAction(a)
        self.menu.addSeparator()
        self.menu.addAction(act_quit)
        self.icon.setContextMenu(self.menu)
        self.icon.setToolTip(f"{__app_name__}（在托盘常驻，低占用）")
        self.icon.activated.connect(self._activated)
        if self.available:
            self.icon.show()

    def _show(self) -> None:
        self.window.show_main_window()

    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.window.isVisible():
                self.window.hide()
            else:
                self._show()

    def show_minimized_hint(self) -> None:
        if self.available:
            self.icon.showMessage(__app_name__, "已最小化到托盘，双击图标恢复。全局快捷键仍然可用。",
                                  INFO_ICON, 4000)

    def showMessage(self, title: str, text: str, reason: int = 1) -> None:
        if self.available:
            self.icon.showMessage(title, text[:180], INFO_ICON, 4500)

    def disable(self) -> None:
        if self.available:
            self.icon.hide()
