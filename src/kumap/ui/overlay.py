"""游戏内悬浮指示窗。

- 无边框、半透明、置顶、鼠标可穿透（不会挡住游戏操作）；
- 以识图/手动定位的位置为中心，画出官方底图、当前路线和下一站方向；
- 只读屏幕像素与本地路线，不接触游戏进程，也不往游戏里注入任何东西。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen,
                           QPixmap, QPolygonF)
from PySide6.QtWidgets import QApplication, QWidget

from ..core.theme import palette

# 官方地面世界：东西约 25500 米 / 6144 像素
METERS_PER_MAP_PX = 25500.0 / 6144.0


class OverlayHUD(QWidget):
    WIDTH = 300
    HEIGHT = 330
    RADIUS = 116
    VIEW_SPAN = 900.0          # 圆圈内覆盖的地图像素宽度（约 3.7km）
    MARGIN = 20

    def __init__(self, theme: str = "dark"):
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                            Qt.Tool | Qt.WindowTransparentForInput |
                            Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setWindowTitle("库街区探索助手 · 游戏内指示")

        self._c = palette(theme)
        self._pix: QPixmap | None = None
        self._player: tuple[float, float] | None = None
        self._rot: float | None = None
        self._conf: float | None = None
        self._source = ""
        self._route: list[tuple[float, float]] = []
        self._index = -1
        self._done: set[int] = set()
        self._step_label = ""
        self._route_name = ""
        self._status = "等待定位（Ctrl+Alt+V 开启识图）"
        self._placed = False

        self._timer = QTimer(self)
        self._timer.setInterval(160)
        self._timer.timeout.connect(self.update)
        self.hide()

    # ------------------------------------------------------------------ 数据
    def set_theme(self, theme: str) -> None:
        self._c = palette(theme)
        self.update()

    def set_map(self, pix: QPixmap | None) -> None:
        self._pix = pix if pix and not pix.isNull() else None
        self.update()

    def set_position(self, x: float | None, y: float | None,
                     rotation: float | None = None, confidence: float | None = None,
                     source: str = "vision") -> None:
        if x is None or y is None:
            self._player = None
            self._rot = None
            self._conf = None
            self._source = ""
        else:
            self._player = (float(x), float(y))
            self._rot = rotation
            self._conf = confidence
            self._source = source
        self.update()

    def set_route(self, points: list[tuple[float, float]], index: int,
                  done: set[int] | None, label: str = "", route_name: str = "") -> None:
        self._route = list(points)
        self._index = index
        self._done = set(done or ())
        self._step_label = label
        self._route_name = route_name
        self.update()

    def set_status(self, text: str) -> None:
        self._status = text or ""
        self.update()

    def note_failure(self, text: str = "识图失败，保持上次位置") -> None:
        self._conf = None
        self.set_status(text)

    # ------------------------------------------------------------------ 窗口
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._placed:
            self._place_default()
            self._placed = True
        self._timer.start()
        self.raise_()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    def _place_default(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.right() - self.width() - self.MARGIN,
                  geo.bottom() - self.height() - self.MARGIN)

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        c = self._c
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        cx, cy = self.WIDTH / 2.0, 168.0
        circle = QRectF(cx - self.RADIUS, cy - self.RADIUS,
                        self.RADIUS * 2, self.RADIUS * 2)

        # 顶部：路线 / 下一站
        title = self._route_name or "游戏内指示"
        sub = ""
        if self._route and 0 <= self._index < len(self._route):
            sub = f"下一站 {self._index + 1}/{len(self._route)}"
            if self._step_label:
                sub += f" · {self._step_label}"
        self._pill(p, QRectF(8, 6, self.WIDTH - 16, 22), title)
        if sub:
            self._pill(p, QRectF(8, 30, self.WIDTH - 16, 22), sub)

        # 底图
        p.save()
        path = QPainterPath()
        path.addEllipse(circle)
        p.setClipPath(path)
        p.fillRect(circle, QColor(c["map_bg"]))
        if self._pix is not None and self._player is not None:
            self._draw_map(p, circle)
        p.restore()

        # 圆形边框
        p.setPen(QPen(QColor(c["text"]), 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(circle)

        if self._player is None:
            p.setPen(QColor(c["text2"]))
            f = QFont(self.font())
            f.setPointSizeF(10)
            p.setFont(f)
            p.drawText(circle, Qt.AlignCenter, "等待定位…")
        else:
            self._draw_route(p, cx, cy)
            self._draw_player(p, cx, cy)

        # 底部状态
        conf = ""
        if self._player is not None and self._source == "vision" and self._conf is not None:
            conf = f" · 识图 {self._conf:.0%}"
        self._pill(p, QRectF(8, self.HEIGHT - 30, self.WIDTH - 16, 22),
                   (self._status + conf)[:60])
        p.end()

    def _pill(self, p: QPainter, rect: QRectF, text: str) -> None:
        p.setPen(Qt.NoPen)
        bg = QColor(self._c["panel"])
        bg.setAlpha(205)
        p.setBrush(bg)
        p.drawRoundedRect(rect, 6, 6)
        p.setPen(QColor(self._c["text"]))
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        elided = QFontMetrics(f).elidedText(text, Qt.ElideRight, int(rect.width() - 12))
        p.drawText(rect.adjusted(6, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft, elided)

    # -- 坐标换算
    def _to_widget(self, x: float, y: float, cx: float, cy: float) -> QPointF:
        if self._pix is None or self._player is None:
            return QPointF(cx, cy)
        mw, mh = float(self._pix.width()), float(self._pix.height())
        scale = (self.RADIUS * 2.0) / self.VIEW_SPAN
        return QPointF(cx + (x * mw - self._player[0] * mw) * scale,
                       cy + (y * mh - self._player[1] * mh) * scale)

    def _draw_map(self, p: QPainter, circle: QRectF) -> None:
        assert self._pix is not None and self._player is not None
        mw, mh = float(self._pix.width()), float(self._pix.height())
        px, py = self._player[0] * mw, self._player[1] * mh
        half = self.VIEW_SPAN / 2.0
        sx0, sy0 = max(0.0, px - half), max(0.0, py - half)
        sx1, sy1 = min(mw, px + half), min(mh, py + half)
        if sx1 <= sx0 or sy1 <= sy0:
            return
        scale = (self.RADIUS * 2.0) / self.VIEW_SPAN
        cx, cy = circle.center().x(), circle.center().y()
        target = QRectF(cx + (sx0 - px) * scale, cy + (sy0 - py) * scale,
                        (sx1 - sx0) * scale, (sy1 - sy0) * scale)
        p.setOpacity(0.92)
        p.drawPixmap(target, self._pix, QRectF(sx0, sy0, sx1 - sx0, sy1 - sy0))
        p.setOpacity(1.0)

    def _draw_route(self, p: QPainter, cx: float, cy: float) -> None:
        if not self._route:
            return
        accent = QColor(self._c["accent"])
        pen = QPen(accent, 2.0, Qt.DashLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        prev = None
        for i, (x, y) in enumerate(self._route):
            sp = self._to_widget(x, y, cx, cy)
            if prev is not None:
                p.drawLine(prev, sp)
            prev = sp
        # 当前目标
        if 0 <= self._index < len(self._route):
            tx, ty = self._route[self._index]
            tip = self._to_widget(tx, ty, cx, cy)
            dx, dy = tip.x() - cx, tip.y() - cy
            dist = math.hypot(dx, dy)
            if dist > self.RADIUS - 10:
                ux, uy = dx / max(1e-6, dist), dy / max(1e-6, dist)
                tip = QPointF(cx + ux * (self.RADIUS - 9), cy + uy * (self.RADIUS - 9))
                p.setPen(Qt.NoPen)
                p.setBrush(accent)
                size = 9.0
                left = QPointF(tip.x() - ux * size - uy * size * 0.6,
                               tip.y() - uy * size + ux * size * 0.6)
                right = QPointF(tip.x() - ux * size + uy * size * 0.6,
                                tip.y() - uy * size - ux * size * 0.6)
                p.drawPolygon(QPolygonF([tip, left, right]))
            else:
                p.setPen(QPen(QColor("#ffffff"), 1.8))
                p.setBrush(accent)
                p.drawEllipse(tip, 6.5, 6.5)
                p.setPen(QColor("#0c0f13"))
                f = QFont(self.font())
                f.setPointSizeF(7.5)
                f.setBold(True)
                p.setFont(f)
                p.drawText(QRectF(tip.x() - 8, tip.y() - 8, 16, 16),
                           Qt.AlignCenter, str(self._index + 1))

    def _draw_player(self, p: QPainter, cx: float, cy: float) -> None:
        center = QPointF(cx, cy)
        p.setPen(Qt.NoPen)
        halo = QColor(self._c["accent"])
        halo.setAlpha(80)
        p.setBrush(halo)
        p.drawEllipse(center, 13, 13)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(center, 5.2, 5.2)
        if self._rot is not None:
            rad = math.radians(float(self._rot) - 90.0)
            tip = QPointF(cx + math.cos(rad) * 13, cy + math.sin(rad) * 13)
            p.setPen(QPen(QColor(self._c["accent"]), 2.4, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(center, tip)

    # -- 距离提示（供主窗口拼状态文字）
    def next_distance_m(self) -> float | None:
        if self._pix is None or self._player is None or not (0 <= self._index < len(self._route)):
            return None
        tx, ty = self._route[self._index]
        mw = float(self._pix.width())
        dx = (tx - self._player[0]) * mw
        dy = (ty - self._player[1]) * float(self._pix.height())
        return math.hypot(dx, dy) * METERS_PER_MAP_PX
