# -*- coding: utf-8 -*-
"""MapCanvas 的点位绘制与命中检测。

重点锁两件事：官方点位包（约 1.9 万点）下重绘不能退化成逐点画图标，
以及降级聚合之后鼠标悬浮/点击命中的仍然是真实点位。
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

from kumap.core.models import Layer, MapPoint
from kumap.ui.map_canvas import LOD_BUDGET, MapCanvas


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def make_canvas(qapp, n: int, size: int = 400) -> MapCanvas:
    cv = MapCanvas()
    cv.resize(800, 500)
    pm = QPixmap(size, size)
    pm.fill(QColor(30, 40, 55))
    cv.set_map_pixmap(pm)
    layers = {"chest": Layer(key="chest", label="宝箱", color="#F2B134", glyph="diamond"),
              "anchor": Layer(key="anchor", label="锚点", color="#3EC6C0", glyph="beacon")}
    pts = []
    side = int(n ** 0.5) + 1
    for i in range(n):
        pts.append(MapPoint(id=f"p{i}", layer="chest" if i % 3 else "anchor",
                            name=f"点{i}", x=(i % side) / side, y=(i // side) / side))
    cv.set_points(pts, layers, visible=set(layers))
    return cv


def test_screen_scan_culls_by_viewport_and_layer(qapp):
    cv = make_canvas(qapp, 900)
    cv.grab()
    assert len(cv._drawn) == 900                       # 全览：屏内就是全部
    cv.zoom_step(6.0, QPoint(400, 250))
    cv.grab()
    inside = len(cv._drawn)
    assert 0 < inside < 900                            # 放大后只留屏内的
    cv.set_layer_visibility({"anchor"})
    cv.grab()
    assert all(p.layer == "anchor" for p, _, _ in cv._drawn)
    assert len(cv._drawn) < inside                     # 图层开关立刻生效
    keep = cv._drawn[0][0].id                 # 取一个当前屏内的点做搜索过滤
    cv.set_filter("宝箱", {keep})
    cv.grab()
    assert [p.id for p, _, _ in cv._drawn] == [keep]


def test_hit_test_uses_recorded_positions(qapp):
    cv = make_canvas(qapp, 900)
    cv.grab()
    p, sx, sy = cv._drawn[10]
    assert cv._hit(QPoint(int(sx), int(sy))) is p
    assert cv._hit(QPoint(-500, -500)) is None


def test_dense_view_switches_to_clusters(qapp):
    """超过预算才降级；降级后 _drawn 仍是真实点位，命中/详情不失真。"""
    cv = make_canvas(qapp, LOD_BUDGET + 900)
    calls: list[int] = []
    origin = cv._draw_clusters

    def spy(painter, vis, r):
        calls.append(len(vis))
        return origin(painter, vis, r)

    cv._draw_clusters = spy
    cv.grab()
    assert calls == [LOD_BUDGET + 900]
    assert len(cv._drawn) == LOD_BUDGET + 900
    p, sx, sy = cv._drawn[500]
    assert cv._hit(QPoint(int(sx), int(sy))) is p      # 聚合图上也能点到真点
    cv.set_points(cv.points[:200], cv.layers, visible={"chest", "anchor"})
    calls.clear()
    cv.grab()
    assert calls == []                                 # 预算内照旧逐个画


def test_official_scale_paint_stays_fast(qapp):
    """官方包量级（1.9 万点）全览一帧必须远低于肉眼可感的重绘延迟。"""
    cv = make_canvas(qapp, 19249, size=1792)
    cv.grab()                                            # 先预热一次
    t0 = time.perf_counter()
    cv.grab()
    dt = time.perf_counter() - t0
    assert len(cv._drawn) == 19249
    assert dt < 0.35, f"全览重绘 {dt * 1000:.0f} ms，点位绘制性能退化了"


def test_hit_grid_matches_linear_scan(qapp):
    """空间索引必须与线性扫描结果一致，且重绘后失效重建。"""
    cv = make_canvas(qapp, 6000)
    cv.grab()
    assert cv._hit_grid is None                        # 惰性：绘制后还没建索引
    probes = [QPoint(400, 250), QPoint(0, 0), QPoint(799, 499), QPoint(-50, -50)]
    probes += [QPoint(int(sx), int(sy)) for _, sx, sy in cv._drawn[::500]]
    for pos in probes:
        assert cv._hit(pos) is cv._hit_linear(pos.x(), pos.y())
    assert cv._hit_grid is not None

    cv.zoom_step(3.0, QPoint(400, 250))
    cv.grab()
    assert cv._hit_grid is None                        # 视图变了，旧索引不能复用
    for pos in probes:
        assert cv._hit(pos) is cv._hit_linear(pos.x(), pos.y())


def test_hit_grid_is_much_faster_at_official_scale(qapp):
    """1.9 万点量级：索引命中应远快于逐点扫描（不设严苛阈值，防环境抖动）。"""
    cv = make_canvas(qapp, 19249, size=1792)
    cv.grab()
    t0 = time.perf_counter()
    for i in range(1000):
        cv._hit(QPoint(400 + i % 200, 250 + i % 120))
    dt = time.perf_counter() - t0
    assert cv._hit_grid is not None
    assert dt < 0.5, f"1000 次命中耗时 {dt * 1000:.0f} ms，空间索引可能失效"


def test_map_to_widget_roundtrip_still_matches_scan(qapp):
    cv = make_canvas(qapp, 30)
    cv.zoom_step(3.0, QPoint(100, 100))
    cv.grab()
    for p, sx, sy in cv._drawn[:5]:
        sp = cv.map_to_widget(p.x, p.y)
        assert (sx, sy) == pytest.approx((sp.x(), sp.y()), abs=1e-6)
