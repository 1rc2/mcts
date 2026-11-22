"""游戏内的悬浮清单窗（未探索点位）。

- 无边框、半透明、置顶、不占任务栏；默认鼠标穿透，可切换成可点击模式；
- 固定贴在主屏左下角，主屏分辨率变化后自动贴回去；
- 内容：顶部一行定位状态，下面按距离由近到远列出最近若干条未探索点位
  （名称、距离、所属图层）。已完成的点位和隐藏图层在数据源侧就过滤掉了；
- 只读屏幕像素与本地数据，不接触游戏进程，也不往游戏里注入任何东西。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QApplication, QWidget

from ..core.theme import palette

# 官方地面世界：东西约 25500 米 / 6144 像素
METERS_PER_MAP_PX = 25500.0 / 6144.0

ROW_H = 40
TOP_H = 34
FOOT_H = 24
PAD = 8


class OverlayHUD(QWidget):
    """常驻游戏旁的未探索点位清单。"""

    MARGIN = 16
    MIN_W, MAX_W = 220, 560
    MIN_H, MAX_H = 220, 720

    point_done = Signal(str)  # 用户在可点击模式下点了某一条

    def __init__(self, theme: str = "dark", width: int = 300, height: int = 300):
        super().__init__(None)
        self._clickable = False
        self._c = palette(theme)
        self._items: list[dict] = []
        self._has_position = False
        self._unlocated = True
        self._status = "等待定位（Ctrl+Alt+V 开启识图）"
        self._hover = -1
        self._opacity = 1.0
        # 只为兼容旧接口：路线不再画，但下一站距离提示还用它
        self._pix = None
        self._player: tuple[float, float] | None = None
        self._rot: float | None = None
        self._conf: float | None = None
        self._source = ""
        self._route_next: tuple[float, float] | None = None
        self._screen = None

        self.setWindowTitle("库街区探索助手 · 未探索点位")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self._apply_flags()
        self.set_size(width, height)
        self.hide()

    # ------------------------------------------------------------------ 窗口
    def _apply_flags(self) -> None:
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                 Qt.Tool | Qt.NoDropShadowWindowHint)
        if not self._clickable:
            # 穿透模式下鼠标事件根本到不了这里；要能点条目就必须去掉这个标志
            flags |= Qt.WindowTransparentForInput
        self.setWindowFlags(flags)

    def set_clickable(self, on: bool) -> None:
        on = bool(on)
        if on == self._clickable:
            return
        was_visible = self.isVisible()
        self._clickable = on
        self._hover = -1
        self._apply_flags()
        # setWindowFlags 会重建原生窗口，透明度会被重置回 1.0，必须重新贴上去
        self.setWindowOpacity(self._opacity)
        if was_visible:
            self.show()
            self.raise_()
        self.update()

    def is_clickable(self) -> bool:
        return self._clickable

    def set_size(self, width: int, height: int) -> None:
        try:
            w = int(width)
            h = int(height)
        except (TypeError, ValueError):
            w, h = 300, 300
        self.setFixedSize(min(self.MAX_W, max(self.MIN_W, w)),
                          min(self.MAX_H, max(self.MIN_H, h)))
        if self.isVisible():
            self._place_default()
        self.update()

    def set_opacity(self, value: float) -> None:
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = 0.96
        self._opacity = min(1.0, max(0.4, v))
        self.setWindowOpacity(self._opacity)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._watch_screen()
        self._place_default()
        self.raise_()

    def _watch_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None or screen is self._screen:
            return
        if self._screen is not None:
            try:
                self._screen.availableGeometryChanged.disconnect(self._on_screen_changed)
            except (RuntimeError, TypeError):
                pass
        self._screen = screen
        # 分辨率/任务栏变化后重新贴回左下角，避免飘到屏幕外
        screen.availableGeometryChanged.connect(self._on_screen_changed)

    def _on_screen_changed(self, *_args) -> None:
        self._place_default()

    def _place_default(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.left() + self.MARGIN,
                  geo.bottom() - self.height() - self.MARGIN + 1)

    # ------------------------------------------------------------------ 数据
    def set_theme(self, theme: str) -> None:
        self._c = palette(theme)
        self.update()

    def set_map(self, pixmap) -> None:
        """兼容旧接口：只保留底图尺寸用于算距离，不再渲染雷达。"""
        self._pix = pixmap if pixmap is not None and not pixmap.isNull() else None

    def set_position(self, x, y, rotation=None, confidence=None, source: str = "vision") -> None:
        if x is None or y is None:
            self._player = None
            self._rot = None
            self._conf = None
            self._source = ""
            self._has_position = False
        else:
            self._player = (float(x), float(y))
            self._rot = rotation
            self._conf = confidence
            self._source = source
            self._has_position = True
        self.update()

    def set_route(self, points, index: int, done=None, label: str = "",
                  route_name: str = "") -> None:
        """兼容旧接口：路线不再绘制，仅记下下一站坐标供距离提示使用。"""
        if points and 0 <= index < len(points):
            self._route_next = (float(points[index][0]), float(points[index][1]))
        else:
            self._route_next = None

    def set_status(self, text: str) -> None:
        self._status = text or ""
        self.update()

    def set_unlocated(self, unlocated: bool) -> None:
        self._unlocated = bool(unlocated)
        self.update()

    def set_items(self, items) -> None:
        """items: [{id, name, dist_m, layer, color?}, ...]，由主窗口排好序。"""
        self._items = list(items or [])
        self._hover = -1
        self.update()

    def items(self) -> list[dict]:
        return list(self._items)

    def arrow_angles(self) -> list[float | None]:
        """每行箭头的旋转角（0=屏幕上方，顺时针）。None 表示这一行不画箭头。"""
        return [item.get("bearing") for item in self._items]

    def note_failure(self, text: str = "识图失败，保持上次位置") -> None:
        self._conf = None
        self.set_status(text)

    def next_distance_m(self) -> float | None:
        if self._pix is None or self._player is None or self._route_next is None:
            return None
        tx, ty = self._route_next
        dx = (tx - self._player[0]) * float(self._pix.width())
        dy = (ty - self._player[1]) * float(self._pix.height())
        return math.hypot(dx, dy) * METERS_PER_MAP_PX

    # ------------------------------------------------------------------ 交互
    def _visible_rows(self) -> int:
        usable = self.height() - TOP_H - FOOT_H - PAD
        return max(0, usable // ROW_H)

    def _row_rect(self, index: int) -> QRectF:
        return QRectF(PAD, TOP_H + index * ROW_H, self.width() - PAD * 2, ROW_H - 4)

    def _row_at(self, pos: QPoint) -> int:
        for i in range(min(len(self._items), self._visible_rows())):
            if self._row_rect(i).contains(float(pos.x()), float(pos.y())):
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._clickable:
            return
        idx = self._row_at(event.position().toPoint())
        if idx != self._hover:
            self._hover = idx
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self._clickable or event.button() != Qt.LeftButton:
            return
        idx = self._row_at(event.position().toPoint())
        if 0 <= idx < len(self._items):
            point_id = str(self._items[idx].get("id") or "")
            if point_id:
                self.point_done.emit(point_id)

    # ------------------------------------------------------------------ 绘制
    def _fmt_distance(self, meters: float) -> str:
        try:
            m = float(meters)
        except (TypeError, ValueError):
            return "—"
        if m >= 1000:
            return f"{m / 1000:.1f} km"
        return f"{m:.0f} m"

    def paintEvent(self, event) -> None:  # noqa: N802
        c = self._c
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        # 底板
        bg = QColor(c["panel"])
        bg.setAlpha(230)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 10, 10)

        # 顶部状态
        p.setBrush(QColor(c["accent"]))
        p.drawRoundedRect(QRectF(PAD, 6, 3, 20), 1.5, 1.5)
        status = self._status
        if self._unlocated:
            status = "未定位 · 按地图中心起算"
            if self._status and "未定位" not in self._status and "等待定位" not in self._status:
                status += f" · {self._status}"
        p.setPen(QColor(c["text"]))
        f = QFont(self.font())
        f.setPointSizeF(9.0)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(PAD + 10, 4, self.width() - PAD * 2 - 10, 24),
                   Qt.AlignVCenter | Qt.AlignLeft,
                   QFontMetrics(f).elidedText(status, Qt.ElideRight, self.width() - PAD * 2 - 24))

        # 列表
        rows = self._visible_rows()
        if not self._items:
            p.setPen(QColor(c["text2"]))
            f2 = QFont(self.font())
            f2.setPointSizeF(8.8)
            p.setFont(f2)
            p.drawText(QRectF(PAD, TOP_H, self.width() - PAD * 2, ROW_H),
                       Qt.AlignCenter, "附近没有未探索点位")
        for i, item in enumerate(self._items[:rows]):
            rect = self._row_rect(i)
            row_bg = QColor(c["map_bg"])
            row_bg.setAlpha(150)
            p.setPen(Qt.NoPen)
            p.setBrush(row_bg)
            p.drawRoundedRect(rect, 6, 6)
            if self._clickable and i == self._hover:
                p.setPen(QPen(QColor(c["accent"]), 1.2))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(rect, 6, 6)

            color = item.get("color")
            dot = QColor(color) if color else QColor(c["accent"])
            p.setPen(Qt.NoPen)
            p.setBrush(dot)
            p.drawEllipse(QRectF(rect.x() + 7, rect.y() + 9, 7, 7))

            dist_text = self._fmt_distance(item.get("dist_m"))
            fd = QFont(self.font())
            fd.setPointSizeF(8.6)
            p.setFont(fd)
            dist_w = QFontMetrics(fd).horizontalAdvance(dist_text) + 4

            # 方向箭头：与距离同列、在距离左侧。没有 bearing（未定位）就不画，
            # 免得给出一个假方向；形状本身是三角，不靠颜色区分。
            bearing = item.get("bearing")
            if bearing is not None:
                acx = rect.right() - dist_w - 20
                acy = rect.y() + rect.height() / 2 - 2
                p.save()
                p.translate(acx, acy)
                p.rotate(float(bearing))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(c["text"]))
                p.drawPolygon(QPolygonF([
                    QPointF(0, -6.6), QPointF(5.0, 5.0),
                    QPointF(0, 2.4), QPointF(-5.0, 5.0),
                ]))
                p.restore()

            p.setPen(QColor(c["accent"]))
            p.drawText(QRectF(rect.right() - dist_w - 7, rect.y() + 3, dist_w, 16),
                       Qt.AlignVCenter | Qt.AlignRight, dist_text)

            fn = QFont(self.font())
            fn.setPointSizeF(9.2)
            p.setFont(fn)
            name_w = rect.width() - dist_w - 34
            p.setPen(QColor(c["text"]))
            p.drawText(QRectF(rect.x() + 20, rect.y() + 2, name_w, 18),
                       Qt.AlignVCenter | Qt.AlignLeft,
                       QFontMetrics(fn).elidedText(str(item.get("name") or ""),
                                                   Qt.ElideRight, max(20, int(name_w))))

            fl = QFont(self.font())
            fl.setPointSizeF(7.8)
            p.setFont(fl)
            p.setPen(QColor(c["text2"]))
            p.drawText(QRectF(rect.x() + 20, rect.y() + 19, rect.width() - 28, 15),
                       Qt.AlignVCenter | Qt.AlignLeft,
                       QFontMetrics(fl).elidedText(str(item.get("layer") or "未分类"),
                                                   Qt.ElideRight, rect.width() - 28))

        # 底部提示
        hint = ("点击条目标记完成 · Ctrl+Alt+K 恢复穿透" if self._clickable
                else "鼠标穿透中 · Ctrl+Alt+K 切到可点击")
        ff = QFont(self.font())
        ff.setPointSizeF(7.8)
        p.setFont(ff)
        p.setPen(QColor(c["text2"]))
        p.drawText(QRectF(PAD, self.height() - FOOT_H, self.width() - PAD * 2, FOOT_H - 4),
                   Qt.AlignVCenter | Qt.AlignLeft,
                   QFontMetrics(ff).elidedText(hint, Qt.ElideRight, self.width() - PAD * 2))
        p.end()
