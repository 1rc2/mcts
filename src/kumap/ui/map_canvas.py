"""大地图画布：缩放平移、分层点位、路线轨迹、悬浮详情、手动定位。

纯本地渲染，无网络、无外部输入模拟。点位绘制只画可视范围内的，并且：
屏内点位用浮点算式一次线性扫描得到（不逐点建对象），过密时按像素格聚合降级，
因此官方点位包（约 1.9 万个点）全图层全览时单帧也只有几毫秒到几十毫秒。
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ..core.models import Layer, MapPoint
from ..core.theme import palette

MIN_SCALE_RATIO = 1.0   # 最小 = 刚好铺满窗口
MAX_SCALE = 24.0
HIT_RADIUS = 11.0
HIT_GRID_MIN = 512      # 超过该点数才建空间索引；点数少时线性扫描更快
LOD_BUDGET = 2400       # 单帧图标上限：超过就按像素格聚合（详见 _draw_clusters）


class MapCanvas(QWidget):
    point_hovered = Signal(object)          # MapPoint | None
    point_clicked = Signal(object)          # MapPoint
    point_context = Signal(object, QPoint)  # MapPoint, globalPos
    background_context = Signal(float, float)   # 归一化坐标（新增私人点位）
    position_picked = Signal(float, float)      # 手动定位结果
    view_changed = Signal(float, float, float)  # scale, x, y（中心）

    def __init__(self, parent: QWidget | None = None, theme: str = "dark"):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMinimumSize(240, 170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._pix: QPixmap | None = None
        self._img_w = 0
        self._img_h = 0
        self._scale = 1.0
        self._offset = QPointF(0.0, 0.0)

        self.points: list[MapPoint] = []
        self.layers: dict[str, Layer] = {}
        self.visible_layers: set[str] = set()
        self.hide_completed = False
        self.mark_mode = False
        self.pick_mode = False
        self.show_regions = True
        self.show_grid = True
        self.show_trail = True

        self._is_done = lambda pid: False
        self._note_of = lambda pid: ""
        self._filter_text = ""
        self._filter_ids: set[str] | None = None

        self._selected: MapPoint | None = None
        self._hovered: MapPoint | None = None
        self._drawn: list[tuple[MapPoint, float, float]] = []
        # 屏内点位的空间索引（惰性构建，每次重绘失效）：
        # 1.9 万点时线性命中一次约 4ms，鼠标移动会明显掉帧。
        self._hit_grid: dict[tuple[int, int], list[tuple[int, MapPoint, float, float]]] | None = None

        self._route_pts: list[tuple[float, float]] = []
        self._route_index = -1
        self._route_done: set[int] = set()

        self._player: tuple[float, float] | None = None
        self._player_rot: float | None = None
        self._player_conf: float | None = None
        self._player_src = "manual"
        self._trail: list[tuple[float, float, float]] = []
        self._follow = False

        self._pulse = QTimer(self)
        self._pulse.setInterval(90)
        self._pulse.timeout.connect(self._on_pulse)
        self._phase = 0.0

        self._dragging = False
        self._drag_start = QPoint()
        self._drag_offset_start = QPointF()
        self._press_pos = QPoint()
        self._moved = False
        self.set_theme(theme)

    # ------------------------------------------------------------------ 配置
    def set_theme(self, theme: str) -> None:
        self._c = palette(theme)
        if self._pix is None:
            self.update()

    def set_map_pixmap(self, pix: QPixmap | None) -> None:
        self._pix = pix
        if pix:
            self._img_w, self._img_h = pix.width(), pix.height()
        if self.width() < 4 or self.height() < 4:
            QTimer.singleShot(0, self.fit_view)
        else:
            self.fit_view()

    def set_points(self, points: list[MapPoint], layers: dict[str, Layer],
                   visible: set[str] | None = None) -> None:
        self.points = list(points)
        self.layers = dict(layers)
        self.visible_layers = set(visible) if visible is not None else set(layers)
        self.update()

    def set_state_callbacks(self, is_done, note_of) -> None:
        self._is_done = is_done
        self._note_of = note_of

    def set_layer_visibility(self, visible: set[str]) -> None:
        self.visible_layers = set(visible)
        self.update()

    def set_hide_completed(self, hide: bool) -> None:
        self.hide_completed = bool(hide)
        self.update()

    def set_mark_mode(self, on: bool) -> None:
        self.mark_mode = bool(on)
        self.setCursor(Qt.PointingHandCursor if on else Qt.ArrowCursor)

    def set_pick_mode(self, on: bool) -> None:
        self.pick_mode = bool(on)
        self.setCursor(Qt.CrossCursor if on else (Qt.PointingHandCursor if self.mark_mode else Qt.ArrowCursor))

    def set_filter(self, text: str, matched_ids: set[str] | None) -> None:
        """matched_ids=None 表示不过滤；否则仅显示集合内的点位。"""
        self._filter_text = text
        self._filter_ids = matched_ids
        self.update()

    def set_follow(self, on: bool) -> None:
        self._follow = bool(on)
        if on:
            self.center_player()

    # ------------------------------------------------------------------ 视图
    def fit_view(self) -> None:
        if not self._pix:
            self.update()
            return
        r = self.rect()
        sx = r.width() / self._img_w
        sy = r.height() / self._img_h
        self._scale = min(sx, sy) * MIN_SCALE_RATIO
        self._offset = QPointF((r.width() - self._img_w * self._scale) / 2.0,
                               (r.height() - self._img_h * self._scale) / 2.0)
        self._emit_view()
        self.update()

    def zoom_step(self, factor: float, anchor: QPoint | None = None) -> None:
        if not self._pix:
            return
        anchor = anchor or QPoint(self.rect().center())
        min_scale = min(self.rect().width() / max(1, self._img_w),
                        self.rect().height() / max(1, self._img_h))
        new_scale = max(min_scale, min(MAX_SCALE, self._scale * factor))
        if abs(new_scale - self._scale) < 1e-6:
            return
        mp = self.widget_to_map(QPointF(anchor))
        self._scale = new_scale
        self._offset = QPointF(anchor.x() - mp.x() * self._img_w * new_scale,
                               anchor.y() - mp.y() * self._img_h * new_scale)
        self._clamp_offset()
        self._emit_view()
        self.update()

    def _clamp_offset(self) -> None:
        """不让地图完全拖出窗口。"""
        r = self.rect()
        w = self._img_w * self._scale
        h = self._img_h * self._scale
        self._offset.setX(max(min(40, r.width() * 0.25 - w), min(r.width() * 0.75, self._offset.x())))
        self._offset.setY(max(min(40, r.height() * 0.25 - h), min(r.height() * 0.75, self._offset.y())))

    def map_to_widget(self, x: float, y: float) -> QPointF:
        return QPointF(self._offset.x() + x * self._img_w * self._scale,
                       self._offset.y() + y * self._img_h * self._scale)

    def widget_to_map(self, p: QPointF) -> QPointF:
        if not self._img_w or self._scale <= 0:
            return QPointF(0.5, 0.5)
        return QPointF((p.x() - self._offset.x()) / (self._img_w * self._scale),
                       (p.y() - self._offset.y()) / (self._img_h * self._scale))

    def center_on(self, x: float, y: float, scale: float | None = None) -> None:
        if not self._pix:
            return
        r = self.rect()
        if scale:
            self._scale = max(r.width() / self._img_w * MIN_SCALE_RATIO, min(MAX_SCALE, scale))
        self._offset = QPointF(r.center().x() - x * self._img_w * self._scale,
                               r.center().y() - y * self._img_h * self._scale)
        self._clamp_offset()
        self._emit_view()
        self.update()

    def center_player(self) -> None:
        if self._player:
            self.center_on(self._player[0], self._player[1])

    def _emit_view(self) -> None:
        c = self.widget_to_map(QPointF(self.rect().center()))
        self.view_changed.emit(self._scale, c.x(), c.y())

    # ------------------------------------------------------------------ 数据设置
    def set_route(self, pts: list[tuple[float, float]], index: int = -1,
                  done: set[int] | None = None) -> None:
        self._route_pts = list(pts)
        self._route_index = index
        self._route_done = set(done or ())
        if self._route_index >= 0 and not self._pulse.isActive():
            self._pulse.start()
        elif self._route_index < 0:
            self._pulse.stop()
        self.update()

    def set_player(self, x: float | None, y: float | None, rotation: float | None = None,
                   confidence: float | None = None, source: str = "vision",
                   keep_trail: bool = True) -> None:
        if x is None or y is None:
            self._player = None
            self._pulse.stop() if self._route_index < 0 else None
            self.update()
            return
        first = self._player is None
        self._player = (float(x), float(y))
        self._player_rot = rotation
        self._player_conf = confidence
        self._player_src = source
        if keep_trail and (first or self._dist(self._player, self._trail[-1][:2] if self._trail else (9, 9)) > 0.004):
            self._trail.append((self._player[0], self._player[1], time.time()))
            self._trail = self._trail[-80:]
        if self._follow and not self._dragging:
            self.center_on(self._player[0], self._player[1])
        if not self._pulse.isActive() and (self._route_index >= 0 or self._player):
            self._pulse.start()
        self.update()

    @staticmethod
    def _dist(a, b) -> float:
        return math.hypot(a[0] - b[0], a[1] - b[1])

    def _on_pulse(self) -> None:
        self._phase = (self._phase + 0.09) % (math.pi * 2)
        if not (self._route_index >= 0 or self._player) and not self._follow:
            self._pulse.stop()
        self.update()

    # ------------------------------------------------------------------ 事件
    def wheelEvent(self, event) -> None:  # noqa: N802
        factor = 1.0015 ** event.angleDelta().y()
        self.zoom_step(factor, event.position().toPoint())

    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if event.button() == Qt.MiddleButton or (event.button() == Qt.LeftButton):
            self._dragging = True
            self._drag_start = pos
            self._press_pos = pos
            self._drag_offset_start = QPointF(self._offset)
            self._moved = False
        if event.button() == Qt.RightButton:
            hit = self._hit(pos)
            if hit:
                self.point_context.emit(hit, event.globalPosition().toPoint())
            else:
                mp = self.widget_to_map(event.position())
                self.background_context.emit(max(0.0, min(1.0, mp.x())), max(0.0, min(1.0, mp.y())))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if self._dragging:
            if not self._moved and (pos - self._press_pos).manhattanLength() > 3:
                self._moved = True
            if self._moved:
                delta = pos - self._drag_start
                self._offset = QPointF(self._drag_offset_start.x() + delta.x(),
                                       self._drag_offset_start.y() + delta.y())
                self._clamp_offset()
                self._emit_view()
                self.update()
                return
        hit = self._hit(pos)
        if hit is not self._hovered:
            self._hovered = hit
            self.point_hovered.emit(hit)
            self.update()
        if hit:
            QToolTip.showText(event.globalPosition().toPoint(), self._tooltip_for(hit), self)
        else:
            QToolTip.hideText()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        was_drag = self._moved
        self._dragging = False
        self._moved = False
        if event.button() != Qt.LeftButton or was_drag:
            return
        if self.pick_mode:
            mp = self.widget_to_map(event.position())
            self.position_picked.emit(max(0.0, min(1.0, mp.x())), max(0.0, min(1.0, mp.y())))
            return
        hit = self._hit(pos)
        if hit:
            self._selected = hit
            self.point_clicked.emit(hit)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hovered:
            self._hovered = None
            self.point_hovered.emit(None)
        QToolTip.hideText()
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._pix and self._img_w:
            min_scale = min(self.width() / self._img_w, self.height() / self._img_h)
            if self._scale <= min_scale + 1e-6:
                self.fit_view()

    def _hit(self, widget_pos: QPoint) -> MapPoint | None:
        px, py = float(widget_pos.x()), float(widget_pos.y())
        if len(self._drawn) <= HIT_GRID_MIN:
            return self._hit_linear(px, py)
        grid = self._hit_grid
        if grid is None:
            grid = self._build_hit_grid()
        cell = HIT_RADIUS
        cx, cy = int(px // cell), int(py // cell)
        best: MapPoint | None = None
        best_d = HIT_RADIUS * HIT_RADIUS
        best_idx = -1
        # 命中半径不超过一格，只需查 3×3 邻域。
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for idx, p, sx, sy in grid.get((gx, gy), ()):
                    d = (sx - px) ** 2 + (sy - py) ** 2
                    # 与线性扫描的 `d <= best_d` 语义一致：等距时后出现的胜出
                    if d < best_d or (d == best_d and idx > best_idx):
                        best_d = d
                        best_idx = idx
                        best = p
        return best

    def _hit_linear(self, px: float, py: float) -> MapPoint | None:
        best: MapPoint | None = None
        best_d = HIT_RADIUS * HIT_RADIUS
        for p, sx, sy in self._drawn:
            d = (sx - px) ** 2 + (sy - py) ** 2
            if d <= best_d:
                best_d = d
                best = p
        return best

    def _build_hit_grid(self) -> dict[tuple[int, int], list[tuple[int, MapPoint, float, float]]]:
        cell = HIT_RADIUS
        grid: dict[tuple[int, int], list[tuple[int, MapPoint, float, float]]] = {}
        for idx, (p, sx, sy) in enumerate(self._drawn):
            key = (int(sx // cell), int(sy // cell))
            bucket = grid.get(key)
            if bucket is None:
                grid[key] = [(idx, p, sx, sy)]
            else:
                bucket.append((idx, p, sx, sy))
        self._hit_grid = grid
        return grid

    def _tooltip_for(self, p: MapPoint) -> str:
        c = self._c
        layer = self.layers.get(p.layer)
        label = layer.label if layer else p.layer
        done = self._is_done(p.id)
        rows = [
            f"<b>{p.name}</b>",
            f"<span style='color:{c['text2']}'>{label}"
            + (f" · 等级 {p.level}" if p.level else "")
            + (" · <b style='color:%s'>已完成</b>" % c["ok"] if done else "") + "</span>",
        ]
        if p.drops:
            rows.append(f"掉落 / 获取：{', '.join(p.drops[:6])}")
        note = self._note_of(p.id) or p.note
        if note:
            rows.append(f"备注：{note}")
        if p.author:
            rows.append(f"<span style='color:{c['text2']}'>来源：{p.author}</span>")
        rows.append(f"<span style='color:{c['text2']}'>坐标 {p.x:.4f}, {p.y:.4f}</span>")
        if p.source == "private":
            rows.append("<span style='color:%s'>私人点位（仅本机）</span>" % c["warn"])
        return "<div style='line-height:150%'>" + "<br>".join(rows) + "</div>"

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        c = self._c
        painter.fillRect(self.rect(), QColor(c["map_bg"]))
        if not self._pix:
            painter.setPen(QColor(c["text2"]))
            painter.drawText(self.rect(), Qt.AlignCenter, "未加载地图底图\n（设置 · 数据 中导入公开地图包）")
            return
        self._draw_image(painter)
        self._draw_grid(painter)
        self._draw_route(painter)
        self._draw_trail(painter)
        self._draw_points(painter)
        self._draw_player(painter)
        painter.end()

    def _draw_image(self, painter: QPainter) -> None:
        w, h = self._img_w, self._img_h
        vw, vh = self.width(), self.height()
        sx0 = max(0.0, min(1.0, (0 - self._offset.x()) / (w * self._scale)))
        sx1 = max(0.0, min(1.0, (vw - self._offset.x()) / (w * self._scale)))
        sy0 = max(0.0, min(1.0, (0 - self._offset.y()) / (h * self._scale)))
        sy1 = max(0.0, min(1.0, (vh - self._offset.y()) / (h * self._scale)))
        src = QRectF(sx0 * w, sy0 * h, max(1.0, (sx1 - sx0) * w), max(1.0, (sy1 - sy0) * h))
        tgt = QRectF(self._offset.x() + sx0 * w * self._scale, self._offset.y() + sy0 * h * self._scale,
                     src.width() * self._scale, src.height() * self._scale)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, self._scale < 1.15)
        painter.drawPixmap(tgt, self._pix, src)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, False)

    def _draw_grid(self, painter: QPainter) -> None:
        if not self.show_grid:
            return
        c = self._c
        top_left = self.widget_to_map(QPointF(0, 0))
        bottom_right = self.widget_to_map(QPointF(self.width(), self.height()))
        step = 0.05 if self._scale > 1.2 else 0.1
        line_col = QColor(c["text"])
        line_col.setAlpha(22)
        painter.save()
        painter.setPen(QPen(line_col, 1, Qt.DotLine))
        x = math.floor(top_left.x() / step) * step
        while x <= bottom_right.x():
            a = self.map_to_widget(x, top_left.y())
            b = self.map_to_widget(x, bottom_right.y())
            painter.drawLine(a, b)
            x += step
        y = math.floor(top_left.y() / step) * step
        while y <= bottom_right.y():
            a = self.map_to_widget(top_left.x(), y)
            b = self.map_to_widget(bottom_right.x(), y)
            painter.drawLine(a, b)
            y += step
        painter.restore()

    def _visible_on_screen(self) -> tuple[list[tuple[MapPoint, float, float]], float, float, float]:
        """屏内可见点位 + 本次绘制用到的像素比例，一次线性扫描完成。

        刻意不走 map_to_widget()/QPointF：官方点位包一个图层就上千点，逐点对象分配
        会让单次重绘多出几十毫秒，这里只用浮点算式。
        """
        w_px = self._img_w * self._scale
        h_px = self._img_h * self._scale
        ox, oy = self._offset.x(), self._offset.y()
        r = max(5.0, min(8.5, 5.4 + math.log(max(0.2, self._scale), 2) * 0.9))
        pad = r + 12.0
        vw, vh = float(self.width()), float(self.height())
        layers, flt = self.visible_layers, self._filter_ids
        hide, is_done = self.hide_completed, self._is_done
        out: list[tuple[MapPoint, float, float]] = []
        for p in self.points:
            if p.layer not in layers or (flt is not None and p.id not in flt):
                continue
            if hide and is_done(p.id):
                continue
            sx = ox + p.x * w_px
            if sx < -pad or sx > vw + pad:
                continue
            sy = oy + p.y * h_px
            if sy < -pad or sy > vh + pad:
                continue
            out.append((p, sx, sy))
        return out, ox, oy, r

    def _draw_points(self, painter: QPainter) -> None:
        c = self._c
        vis, ox, oy, r = self._visible_on_screen()
        self._drawn = vis
        self._hit_grid = None       # 屏内点位变了，惰性重建命中索引
        if not vis:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        if len(vis) > LOD_BUDGET:
            self._draw_clusters(painter, vis, r)
            painter.restore()
            return
        show_labels = self._scale > 2.4
        font = QFont(self.font())
        font.setPointSizeF(max(7.5, r * 0.95))
        painter.setFont(font)
        label_pen = QPen(QColor(c["text"]))
        for p, sx, sy in vis:
            sp = QPointF(sx, sy)
            layer = self.layers.get(p.layer) or Layer(key=p.layer, label=p.layer, color="#9AA4B2")
            done = self._is_done(p.id)
            _draw_glyph(painter, sp, r, QColor(layer.color), layer.glyph, done,
                        selected=(p is self._selected), hovered=(p is self._hovered),
                        color_text=c["text"])
            if show_labels and not done and r > 6.2 and layer.key in ("quest", "anchor"):
                painter.setPen(label_pen)
                painter.drawText(QPointF(sx + r + 3, sy + 3), p.name[:12])
        painter.restore()

    def _draw_clusters(self, painter: QPainter, vis: list, r: float) -> None:
        """点位过密时的降级绘制：按像素格聚合成一个图标（同色批量提交）。

        缩到整个世界时 1.9 万个图标本来就糊成一团，逐个画既看不清也拖慢刷新；
        聚合后一帧最多约 LOD_BUDGET 个图标，鼠标命中仍走 vis 里的真实点位。
        """
        cell = max(8.0, math.sqrt(float(self.width()) * float(self.height()) / LOD_BUDGET))
        acc: dict[tuple[int, int], list] = {}
        for p, sx, sy in vis:
            key = (int(sx // cell), int(sy // cell))
            a = acc.get(key)
            if a is None:
                acc[key] = [1, sx, sy, p.layer, {}]
            else:
                a[0] += 1
                a[1] += sx
                a[2] += sy
                t = a[4]
                t[p.layer] = t.get(p.layer, 0) + 1
        by_layer: dict[str, list] = {}
        for n, sumx, sumy, _, counts in acc.values():
            top = max(counts, key=counts.get) if counts else None
            by_layer.setdefault(top, []).append((sumx / n, sumy / n, n))
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, r * 0.8))
        for key, items in by_layer.items():
            layer = self.layers.get(key) or Layer(key=key or "", label=key or "", color="#9AA4B2")
            col = QColor(layer.color)
            col.setAlpha(215)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(col))
            rad = max(3.0, r * 0.62)
            # 实测逐次 drawEllipse 比合并成 QPainterPath 更快（后者构造/光栅化开销更大）
            for x, y, n in items:
                painter.drawEllipse(QPointF(x, y), rad, rad)
            if len(items) > 40:          # 数量标注只在聚合格少时才有意义
                continue
            painter.setPen(QPen(QColor(self._c["text"])))
            painter.setBrush(Qt.NoBrush)
            painter.setFont(font)
            for x, y, n in items:
                if n > 1:
                    painter.drawText(QPointF(x + rad + 1.5, y + 2.5), str(n))

    def _draw_route(self, painter: QPainter) -> None:
        if len(self._route_pts) < 2:
            return
        c = self._c
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(c["accent"]), 2.4, Qt.DashLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        path = QPainterPath()
        first = self.map_to_widget(*self._route_pts[0])
        path.moveTo(first)
        for x, y in self._route_pts[1:]:
            path.lineTo(self.map_to_widget(x, y))
        painter.drawPath(path)
        # 序号
        if self._scale > 0.55:
            small = QFont(self.font())
            small.setPointSizeF(8)
            painter.setFont(small)
            for i, (x, y) in enumerate(self._route_pts):
                if i >= 300:
                    break
                sp = self.map_to_widget(x, y)
                if not (-10 <= sp.x() <= self.width() + 10 and -10 <= sp.y() <= self.height() + 10):
                    continue
                done = i in self._route_done
                color = QColor(c["done"] if done else c["accent"])
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(color))
                painter.drawEllipse(sp, 6.5, 6.5)
                painter.setPen(QColor(c["accent_text"] if not done else c["map_bg"]))
                painter.drawText(QRectF(sp.x() - 7, sp.y() - 7, 14, 14), Qt.AlignCenter, str(i + 1))
        # 当前目标
        if 0 <= self._route_index < len(self._route_pts):
            tx, ty = self._route_pts[self._route_index]
            sp = self.map_to_widget(tx, ty)
            pulse = 9 + math.sin(self._phase) * 3
            painter.setPen(QPen(QColor(c["accent"]), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(sp, pulse, pulse)
            painter.drawEllipse(sp, 3.0, 3.0)
            painter.setPen(QColor(c["accent"]))
            f = QFont(self.font())
            f.setBold(True)
            f.setPointSizeF(9)
            painter.setFont(f)
            painter.drawText(QPointF(sp.x() + 12, sp.y() - 10), f"下一站 {self._route_index + 1}")
            if self._player:
                self._draw_direction(painter, self.map_to_widget(*self._player), sp)
        painter.restore()

    def _draw_direction(self, painter: QPainter, a: QPointF, b: QPointF) -> None:
        c = self._c
        dx, dy = b.x() - a.x(), b.y() - a.y()
        dist = math.hypot(dx, dy)
        if dist < 46:
            return
        ux, uy = dx / dist, dy / dist
        tip = QPointF(a.x() + ux * (dist - 18), a.y() + uy * (dist - 18))
        size = 8.0
        left = QPointF(tip.x() - ux * size - uy * size * 0.62, tip.y() - uy * size + ux * size * 0.62)
        right = QPointF(tip.x() - ux * size + uy * size * 0.62, tip.y() - uy * size - ux * size * 0.62)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(c["accent"])))
        painter.drawPolygon(QPolygonF([tip, left, right]))

    def _draw_trail(self, painter: QPainter) -> None:
        if not self.show_trail or len(self._trail) < 2:
            return
        c = self._c
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        now = time.time()
        prev = None
        for x, y, t in self._trail:
            sp = self.map_to_widget(x, y)
            age = max(0.0, min(1.0, (now - t) / 240.0))
            col = QColor(c["accent"])
            col.setAlpha(int(150 * (1 - age)))
            if prev:
                painter.setPen(QPen(col, 1.6))
                painter.drawLine(prev, sp)
            prev = sp
        painter.restore()

    def _draw_player(self, painter: QPainter) -> None:
        if not self._player:
            return
        c = self._c
        sp = self.map_to_widget(self._player[0], self._player[1])
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        # 识别误差圈
        conf = self._player_conf
        if conf is not None and self._player_src == "vision":
            radius = 14 + (1.0 - max(0.0, min(1.0, conf))) * 26
            ring = QColor(c["warn"])
            ring.setAlpha(150)
            painter.setPen(QPen(ring, 1.4, Qt.DotLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(sp, radius, radius)
        pulse = 11 + math.sin(self._phase) * 2.6
        halo = QColor(c["accent"])
        halo.setAlpha(90)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(halo))
        painter.drawEllipse(sp, pulse, pulse)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawEllipse(sp, 5.2, 5.2)
        painter.setPen(QPen(QColor(c["map_bg"]), 1.6))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(sp, 5.2, 5.2)
        if self._player_rot is not None:
            rad = math.radians(self._player_rot - 90)
            tip = QPointF(sp.x() + math.cos(rad) * 12, sp.y() + math.sin(rad) * 12)
            painter.setPen(QPen(QColor(c["accent"]), 2.2, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(sp, tip)
        tag = "识图" if self._player_src == "vision" else "手动"
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(QColor(c["text"]))
        painter.drawText(QPointF(sp.x() + 12, sp.y() - 12), f"我（{tag}）")
        painter.restore()


def _draw_glyph(painter: QPainter, pos: QPointF, r: float, color: QColor, glyph: str,
                done: bool, selected: bool, hovered: bool, color_text: str = "#ffffff") -> None:
    """按图层画差异化图标；已完成 = 变暗 + 对勾。"""
    col = QColor(color)
    if done:
        col.setAlpha(110)
    fill = QBrush(col)
    outline = QColor("#0c0f13")
    outline.setAlpha(200 if not done else 90)
    painter.setBrush(fill)
    painter.setPen(QPen(outline, 1.1))
    x, y = pos.x(), pos.y()
    if glyph == "diamond":
        poly = QPolygonF([QPointF(x, y - r), QPointF(x + r, y), QPointF(x, y + r), QPointF(x - r, y)])
        painter.drawPolygon(poly)
    elif glyph == "beacon":
        poly = QPolygonF([QPointF(x, y - r * 1.1), QPointF(x + r * 0.9, y + r * 0.7),
                          QPointF(x - r * 0.9, y + r * 0.7)])
        painter.drawPolygon(poly)
    elif glyph == "hex":
        pts = []
        for i in range(6):
            a = math.pi / 6 + i * math.pi / 3
            pts.append(QPointF(x + math.cos(a) * r * 1.05, y + math.sin(a) * r * 1.05))
        painter.drawPolygon(QPolygonF(pts))
    elif glyph == "star":
        pts = []
        for i in range(8):
            rr = r * 1.15 if i % 2 == 0 else r * 0.5
            a = -math.pi / 2 + i * math.pi / 4
            pts.append(QPointF(x + math.cos(a) * rr, y + math.sin(a) * rr))
        painter.drawPolygon(QPolygonF(pts))
    elif glyph == "ring":
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(pos, r, r)
        painter.setBrush(fill)
        painter.drawEllipse(QPointF(x, y - r * 0.35), r * 0.38, r * 0.38)
        painter.setBrush(fill)
        painter.setPen(QPen(outline, 1.1))
        painter.drawEllipse(pos, r * 0.62, r * 0.62)
    elif glyph == "flag":
        path = QPainterPath()
        path.moveTo(x - r * 0.7, y - r)
        path.lineTo(x - r * 0.7, y + r)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        poly = QPolygonF([QPointF(x - r * 0.7, y - r), QPointF(x + r, y - r * 0.35),
                          QPointF(x - r * 0.7, y + r * 0.25)])
        painter.setBrush(fill)
        painter.drawPolygon(poly)
    else:  # dot
        painter.drawEllipse(pos, r * 0.85, r * 0.85)

    if done:
        pen = QPen(QColor("#eaf6ef"), max(1.4, r * 0.28), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(QPointF(x - r * 0.45, y + r * 0.05), QPointF(x - r * 0.1, y + r * 0.42))
        painter.drawLine(QPointF(x - r * 0.1, y + r * 0.42), QPointF(x + r * 0.5, y - r * 0.42))
    if selected or hovered:
        ring = QColor("#ffffff")
        ring.setAlpha(230 if selected else 150)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(pos, r + 3.5, r + 3.5)
