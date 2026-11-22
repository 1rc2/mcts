# -*- coding: utf-8 -*-
"""游戏内指示窗与官方地图直达地址。"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPixmap

from kumap.ui.official_map import official_map_url, to_full_coords
from kumap.ui.overlay import OverlayHUD


def test_overlay_renders_position_and_route(qt_app):
    hud = OverlayHUD("dark")
    pm = QPixmap(400, 400)
    pm.fill(QColor(20, 40, 50))
    hud.set_map(pm)
    hud.resize(300, 330)
    hud.set_position(0.5, 0.5, rotation=30, confidence=0.9, source="vision")
    hud.set_route([(0.5, 0.5), (0.6, 0.6)], 1, set(), "测试点", "测试路线")

    img = hud.grab().toImage()
    assert not img.isNull()
    dist = hud.next_distance_m()
    assert dist is not None and 0 < dist < 25500
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
