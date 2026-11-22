# -*- coding: utf-8 -*-
"""游戏内浮窗与官方地图直达地址。"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication

from kumap.core.guide import relative_bearing
from kumap.ui.official_map import official_map_url, to_full_coords
from kumap.ui.overlay import OverlayHUD


def _items(n: int = 4) -> list[dict]:
    return [{"id": f"p{i}", "name": f"宝箱 {i}", "dist_m": 120.0 * (i + 1),
             "layer": "宝箱", "color": "#E0B050"} for i in range(n)]


def test_overlay_renders_unexplored_list(qt_app):
    hud = OverlayHUD("dark", width=300, height=340)
    hud.set_items(_items(5))
    hud.set_position(0.5, 0.5, rotation=30, confidence=0.9, source="vision")
    hud.set_unlocated(False)
    hud.set_status("位置已获取")

    img = hud.grab().toImage()
    assert not img.isNull()
    assert (img.width(), img.height()) == (300, 340)
    assert len(hud.items()) == 5
    assert hud.is_clickable() is False          # 默认穿透
    hud.close()


def test_overlay_empty_list_still_renders(qt_app):
    hud = OverlayHUD("light")
    hud.set_items([])
    hud.set_unlocated(True)
    assert not hud.grab().toImage().isNull()
    hud.close()


def test_relative_bearing_rotates_with_facing():
    # 目标在正上方（正北）：朝北看是 0°，朝东（90°）看则落在左侧 270°
    assert relative_bearing((0.5, 0.5), (0.5, 0.4), facing=0)[0] == pytest.approx(0)
    assert relative_bearing((0.5, 0.5), (0.5, 0.4), facing=90)[0] == pytest.approx(270)
    # 目标在正右（正东）：朝北看是 90°
    assert relative_bearing((0.5, 0.5), (0.6, 0.5), facing=0)[0] == pytest.approx(90)
    # bbox 的纵横比只影响斜向角度，正东方向仍是 90°
    assert relative_bearing((0.5, 0.5), (0.6, 0.5), bbox=(0, 0, 0.5, 0.25),
                            facing=0)[0] == pytest.approx(90)


def test_relative_bearing_falls_back_to_north_without_facing():
    ang, used = relative_bearing((0.5, 0.5), (0.5, 0.4))
    assert used is False and ang == pytest.approx(0)
    ang, used = relative_bearing((0.5, 0.5), (0.4, 0.5))
    assert used is False and ang == pytest.approx(270)


def test_overlay_arrow_angles_follow_items(qt_app):
    hud = OverlayHUD("dark")
    rows = []
    for i, ang in enumerate((0, 90, 180, 270)):
        item = dict(_items(1)[0])
        item.update(id=f"p{i}", name=f"点 {i}", bearing=ang)
        rows.append(item)
    hud.set_items(rows)
    hud.set_unlocated(False)
    assert hud.arrow_angles() == [0, 90, 180, 270]
    assert not hud.grab().toImage().isNull()
    hud.close()


def test_overlay_hides_arrows_when_unlocated(qt_app):
    hud = OverlayHUD("dark")
    hud.set_items([{**_items(1)[0], "bearing": None}])
    hud.set_unlocated(True)
    hud.set_status("未定位 · 按地图中心起算")
    assert hud.arrow_angles() == [None]        # 未定位不给假方向
    assert not hud.grab().toImage().isNull()
    hud.close()


def test_next_distance_still_serves_guide_mode(qt_app):
    """列表不画路线了，但向导仍要靠它算「距下一站」。"""
    hud = OverlayHUD("dark")
    pm = QPixmap(400, 400)
    pm.fill(QColor(20, 40, 50))
    hud.set_map(pm)
    hud.set_position(0.5, 0.5)
    hud.set_route([(0.5, 0.5), (0.6, 0.6)], 1, set(), "测试点", "测试路线")
    dist = hud.next_distance_m()
    assert dist is not None and 0 < dist < 25500
    hud.close()


def test_overlay_clickable_toggle_and_row_click(qt_app):
    hud = OverlayHUD("dark")
    hud.set_items(_items(3))

    hud.set_clickable(True)
    assert hud.is_clickable() is True
    assert not (hud.windowFlags() & Qt.WindowTransparentForInput)   # 可点击时必须去掉穿透
    hud.set_opacity(0.62)
    hud.set_clickable(False)
    hud.set_clickable(True)
    # 切换穿透会重建原生窗口，透明度不能被重置（系统按 1/255 量化，给 0.01 容差）
    assert abs(hud.windowOpacity() - 0.62) < 0.01

    got: list[str] = []
    hud.point_done.connect(got.append)
    rect = hud._row_rect(1)
    ev = QMouseEvent(QEvent.MouseButtonPress,
                     QPointF(rect.center().x(), rect.center().y()),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    hud.mousePressEvent(ev)
    assert got == ["p1"]

    hud.set_clickable(False)
    assert hud.windowFlags() & Qt.WindowTransparentForInput
    hud.mousePressEvent(ev)
    assert got == ["p1"]                        # 穿透模式不再响应
    hud.close()


def test_overlay_anchors_bottom_left_and_clamps_size(qt_app):
    hud = OverlayHUD("dark", width=9999, height=1)
    assert hud.width() == OverlayHUD.MAX_W      # 越界尺寸被夹住
    assert hud.height() == OverlayHUD.MIN_H
    hud.set_opacity(5.0)
    assert abs(hud.windowOpacity() - 1.0) < 1e-6
    hud.show()
    geo = QApplication.primaryScreen().availableGeometry()
    assert hud.x() == geo.left() + hud.MARGIN
    assert hud.geometry().bottom() == geo.bottom() - hud.MARGIN
    hud.close()


def test_official_map_deep_links():
    assert official_map_url("") == "https://www.kurobbs.com/mc/map/"
    assert "state=906" in official_map_url("906")
    assert "country=4" in official_map_url("906")
    assert "state=902" in official_map_url("902")
    assert "country=3" in official_map_url("902")
    assert official_map_url("8", "1") == "https://www.kurobbs.com/mc/map/?state=8&country=1"


def test_nation_local_coords_to_full_world():
    bbox = (0.1, 0.2, 0.5, 0.6)
    assert to_full_coords(bbox, 0.0, 0.0) == (0.1, 0.2)
    assert to_full_coords(bbox, 1.0, 1.0) == (0.5, 0.6)
    x, y = to_full_coords(bbox, 0.5, 0.5)
    assert abs(x - 0.3) < 1e-9 and abs(y - 0.4) < 1e-9
    assert to_full_coords(None, 0.5, 0.5) is None
