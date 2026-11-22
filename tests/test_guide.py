# -*- coding: utf-8 -*-
"""探索向导规划：分层、过滤、连续不回头、2-opt。"""

from __future__ import annotations

import math

from kumap.core.guide import (floor_from_combo, floor_label, generic_hint,
                              meters_between, plan_walk, point_floor)
from kumap.core.models import MapPoint


def _p(pid, x, y, layer="chest", note="", floor=""):
    return MapPoint(id=pid, layer=layer, name=pid, x=x, y=y, note=note, floor=floor)


def _length(route, start):
    cur = start
    total = 0.0
    for p in route:
        total += math.hypot(p.x - cur[0], p.y - cur[1])
        cur = (p.x, p.y)
    return total


def test_floor_helpers():
    assert point_floor(_p("a", 0, 0)) == ""
    assert point_floor(_p("b", 0, 0, note="[分层 -1/49] 宝箱")) == "-1/49"
    assert floor_label("") == "地上"
    assert floor_label("-1/49") == "地下 -1/49"
    assert floor_from_combo("地上") == ""
    assert floor_from_combo("地下 -1/49") == "-1/49"
    assert "宝箱" in generic_hint("chest")


def test_plan_walk_filters_layers_floor_and_done():
    pts = [
        _p("chest1", 0.10, 0.10),
        _p("chest2", 0.12, 0.10),
        _p("material1", 0.11, 0.11, layer="material"),
        _p("underground", 0.10, 0.10, floor="-1/49"),
        _p("done", 0.09, 0.09),
    ]
    route = plan_walk(pts, (0.0, 0.0), layers={"chest"}, floor="",
                      exclude_ids={"done"}, limit=10, radius=1.0)
    assert [p.id for p in route] == ["chest1", "chest2"]


def test_plan_walk_is_continuous_without_duplicates():
    pts = [_p(f"p{i}", 0.1 + i * 0.01, 0.1 + (i % 3) * 0.01) for i in range(8)]
    route = plan_walk(pts, (0.0, 0.0), layers={"chest"}, floor="", limit=8, radius=1.0)
    ids = [p.id for p in route]
    assert len(ids) == len(set(ids)) == 8
    assert ids[0] == "p0"          # 最近点优先


def test_two_opt_does_not_make_route_longer():
    # 交叉排列：贪心路径会来回穿插，2-opt 应缩短
    pts = [
        _p("a", 0.10, 0.00), _p("b", 0.10, 0.10),
        _p("c", 0.20, 0.00), _p("d", 0.20, 0.10),
        _p("e", 0.30, 0.00), _p("f", 0.30, 0.10),
    ]
    start = (0.0, 0.05)
    route = plan_walk(pts, start, layers={"chest"}, floor="", limit=10, radius=1.0)
    # 手工贪心（无 2-opt）长度作为上界
    remaining = list(pts)
    cur = start
    greedy = []
    while remaining:
        remaining.sort(key=lambda p: (p.x - cur[0]) ** 2 + (p.y - cur[1]) ** 2)
        nxt = remaining.pop(0)
        greedy.append(nxt)
        cur = (nxt.x, nxt.y)
    assert _length(route, start) <= _length(greedy, start) + 1e-9


def test_meters_between_uses_pack_bbox():
    m = meters_between((0.0, 0.0), (0.01, 0.0), (0.0, 0.0, 0.5, 0.5))
    assert abs(m - 0.01 * 0.5 * 25500) < 1e-6
