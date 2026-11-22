# -*- coding: utf-8 -*-
"""tools/fetch_official_map.py 的离线单元测试（不联网）。

覆盖官方瓦片网格换算、点位投影、图层映射、抓取计划，以及一个合规红线：
抓取脚本只允许访问公开静态资源域名与那一个匿名资源 id 查询。
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MOD_PATH = ROOT / "tools" / "fetch_official_map.py"

spec = importlib.util.spec_from_file_location("fetch_official_map", MOD_PATH)
assert spec and spec.loader
fom = importlib.util.module_from_spec(spec)
sys.modules["fetch_official_map"] = fom
spec.loader.exec_module(fom)

S = fom.WORLD_PER_TILE


def fake_position():
    """构造一份与官方同构的最小点位数据：4 个点，其中 1 个是离群坏坐标、1 个是别的版本。"""
    return [
        {"id": "chest_a", "name": "朴素奇藏箱", "icon": "adminConfig/52/a.png",
         "location": [
             {"id": "1001", "typeId": "chest_a", "x": 0.0, "y": 0.0,
              "countryId": 1, "stateId": 8, "level": "0", "floorId": "", "description": "起点"},
             {"id": "1002", "typeId": "chest_a", "x": 936586.0, "y": 492151.0,
              "countryId": 3, "stateId": 8, "level": "0", "floorId": "-1/1",
              "description": "残息海岸地区危行任务"},
             {"id": "1003", "typeId": "chest_a", "x": 10_000_000.0, "y": -9_000_000.0,
              "countryId": 1, "stateId": 8, "level": "0", "floorId": "", "description": "坏坐标"},
             {"id": "1004", "typeId": "chest_a", "x": 10.0, "y": 10.0,
              "countryId": 1, "stateId": 9, "level": "0", "floorId": "", "description": "别的版本"},
         ]},
        {"id": "herb", "name": "珍珠草", "icon": "adminConfig/52/b.png",
         "location": [{"id": "2001", "typeId": "herb", "x": -80000.0, "y": -80000.0,
                       "countryId": 1, "stateId": 8, "level": "3", "floorId": "", "description": ""}]},
    ]


FAKE_CATALOG = [
    {"id": "3", "name": "收集物", "children": [{"id": "chest_a", "name": "朴素奇藏箱"}]},
    {"id": "5", "name": "采集物", "children": [{"id": "herb", "name": "珍珠草"}]},
    {"id": "99", "name": "未收录大类", "children": [{"id": "zzz", "name": "别的"}]},
]
FAKE_COUNTRIES = {"c1": "瑝珑", "c3": "黎那汐塔"}
# 包络要盖住上面 3 个有效点的瓦片：(1,0)、(12,-5)、(-1,0)
GRID = (-1, 12, -6, 1)


# ------------------------------------------------------------------- 瓦片网格

def test_tile_of_matches_official_examples():
    assert fom.tile_of(0.0, 0.0) == (1, 0)                 # 原点落在 (1,0) 的左上角
    assert fom.tile_of(936586.0, 492151.0) == (12, -5)     # 官方数据实测点（残息海岸）
    assert fom.tile_of(-1.0, 0.0) == (0, 0)                # X 刚过界就换列
    assert fom.tile_of(0.0, S) == (1, -1)                  # Y 越大越靠下 → tileY 变小


@pytest.mark.parametrize("x,y", [(0, 0), (12345, -6789), (936586, 492151), (-892900, -915523)])
def test_tile_rect_contains_its_own_point(x, y):
    tx, ty = fom.tile_of(x, y)
    x0, y0, x1, y1 = fom.tile_world_rect(tx, ty)
    assert x0 <= x < x1 and y0 <= y < y1


def test_tile_url_shape():
    url = fom.tile_url("EE", 8, 12, -5, 128)
    assert url.startswith("https://web-static.kurobbs.com/mcmap/tiles/EE/8/8_12_-5.png")
    assert "resize,w_128,h_128" in url and "format,png" in url


def test_envelope_drops_only_isolated_fly_tiles():
    """包络只剔「孤立飞格」：坏坐标是 1 个点且远离一切内容；边缘稀疏的真内容必须保留。"""
    tiles = [(x, y) for y in range(0, 4) for x in range(-3, 4) for _ in range(4)]
    tiles += [(-10, 1)] * 65 + [(-10, 0)] * 29      # 西端整列只有 2 格（黑海岸）
    tiles += [(16, -13)] * 30 + [(17, -12)] * 2 + [(16, -12)] * 2   # 东端稀疏但有邻居
    tiles += [(11, -62)]                            # 官方坏坐标：离主体 40 多格
    box, outliers = fom.envelope(tiles)
    assert outliers == [(11, -62)]
    assert box == (-10, 17, -13, 3)                 # 不会被百分位裁成缺边的矩形
    # 密集样本里的飞点照样只剔那一个
    dense = [(x, y) for y in range(6) for x in range(6) for _ in range(9)] + [(200, -300)]
    assert fom.envelope(dense) == ((0, 5, 0, 5), [(200, -300)])
    with pytest.raises(ValueError):
        fom.envelope([])


# --------------------------------------------------------------------- 图层映射

def test_build_layers_maps_typeid_to_layer_key():
    by_type, names = fom.build_layers(FAKE_CATALOG)
    assert by_type["chest_a"][0] == "chest"            # 官方「收集物」并入内置宝箱图层
    assert by_type["herb"][0] == "material"
    assert by_type["zzz"][0] == "k99"                  # 语义不同的官方大类保留独立图层
    assert names["k99"] == "未收录大类"
    assert all(re.fullmatch(r"#[0-9a-fA-F]{6}", v[2]) for v in by_type.values())


# ------------------------------------------------------------------- 点位转换

def test_convert_points_projection_and_filters():
    by_type, _ = fom.build_layers(FAKE_CATALOG)
    payload, dropped = fom.convert_points(fake_position(), by_type, FAKE_COUNTRIES,
                                          GRID, 3 * 128, 8 * 128, state=8)
    ids = {p["id"] for p in payload["points"]}
    assert ids == {f"kurobbs_{i}" for i in ("1001", "1002", "2001")}   # 坏坐标与别的版本都被剔除
    assert [d["id"] for d in dropped] == ["1003"]

    x0 = (GRID[0] - 1) * S
    span_x = (GRID[1] - GRID[0] + 1) * S
    y_top = -GRID[3] * S
    span_y = (GRID[3] - GRID[2] + 1) * S
    for p in payload["points"]:
        assert 0.0 <= p["x"] <= 1.0 and 0.0 <= p["y"] <= 1.0
        wx, wy = p["world"]
        assert math.isclose(p["x"], (wx - x0) / span_x, abs_tol=1e-6)
        assert math.isclose(p["y"], (wy - y_top) / span_y, abs_tol=1e-6)

    one = next(p for p in payload["points"] if p["id"] == "kurobbs_1002")
    # 世界坐标原样留档（改投影或日后与游戏内「位置同步」对齐时可重算，不必再联网）
    assert one["world"] == [936586.0, 492151.0] and one["official_type"] == "chest_a"
    assert one["layer"] == "chest" and one["region"] == "c3"
    assert one["drops"] == ["朴素奇藏箱"]
    assert "[分层 -1/1]" in one["note"] and "危行任务" in one["note"]
    assert one["source"] == "kurobbs"
    herb = next(p for p in payload["points"] if p["id"] == "kurobbs_2001")
    assert herb["level"] == 3 and herb["layer"] == "material"


def test_convert_points_declares_map_size_and_layers():
    by_type, _ = fom.build_layers(FAKE_CATALOG)
    payload, _ = fom.convert_points(fake_position(), by_type, FAKE_COUNTRIES,
                                    GRID, 384, 1024, state=8)
    assert payload["map"] == {"id": "kurobbs_state8",
                              "name": "库街区官方大地图（state 8）", "width": 384, "height": 1024}
    # 不写 map.image：让程序按「与点位文件同目录的 world_map.png」自动取同源底图
    assert "image" not in payload["map"]
    assert {l["key"] for l in payload["layers"]} == {"chest", "material"}
    assert {r["id"] for r in payload["regions"]} == {"c1", "c3"}
    assert "识图定位存在误差" in payload["disclaimer"]


def test_converted_payload_is_loadable_by_the_app():
    """转出来的载荷必须能被本工具自己的解析器吃下（字段口径不能跑偏）。"""
    from kumap.core.models import MapDataset

    by_type, _ = fom.build_layers(FAKE_CATALOG)
    payload, _ = fom.convert_points(fake_position(), by_type, FAKE_COUNTRIES,
                                    GRID, 384, 1024, state=8)
    ds = MapDataset.from_payload(json.loads(json.dumps(payload, ensure_ascii=False)))
    assert len(ds.points) == 3
    assert ds.layer_map()["material"].label == "采集物"
    assert {p.layer for p in ds.points} <= set(ds.layer_map())
    by_id = {p.id: p for p in ds.points}
    span_x = (GRID[1] - GRID[0] + 1) * S
    assert by_id["kurobbs_1001"].x == pytest.approx((0.0 - (GRID[0] - 1) * S) / span_x, abs=1e-6)


# ------------------------------------------------------------------- 抓取计划

def test_tile_plan_scopes():
    grid = (0, 2, 0, 2)
    pts = {(1, 1)}
    assert fom.tile_plan(grid, pts, "all", 0) == [(x, y) for y in range(2, -1, -1)
                                                  for x in range(0, 3)]
    assert len(fom.tile_plan(grid, pts, "near", 0)) == 1
    near1 = set(fom.tile_plan(grid, pts, "near", 1))
    assert (0, 0) in near1 and (2, 2) in near1 and len(near1) == 9


def test_fetcher_honours_cache_and_404_marker(tmp_path, monkeypatch):
    """缓存命中不发请求；确认过的 404 记标记，重跑不再敲 CDN。"""
    calls: list[str] = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"payload"

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return Resp()

    monkeypatch.setattr(fom.urllib.request, "urlopen", fake_urlopen)
    f = fom.Fetcher(cache=tmp_path, delay=0.0)
    dest = tmp_path / "t" / "1_2_64.png"
    assert f.fetch("https://web-static.kurobbs.com/a", dest=dest) == b"payload"
    assert f.fetch("https://web-static.kurobbs.com/a", dest=dest) == b"payload"
    assert len(calls) == 1 and f.stats["hit"] == 1 and f.stats["ok"] == 1
    assert f.throttle() is None                     # 只限速，不产生请求

    dest404 = tmp_path / "t" / "9_9_64.png"
    dest404.with_suffix(".png.404").write_bytes(b"")
    assert f.fetch("https://web-static.kurobbs.com/b", dest=dest404, required=False) is None
    assert len(calls) == 1


def test_resolve_ee_accepts_both_response_shapes(tmp_path, monkeypatch):
    """官方 getMapResource 的 data 既可能是字符串也可能是对象，两种都得能吃下。"""
    seen: dict[str, int] = {}
    bodies = [{"code": 200, "msg": "操作成功", "data": "ABC123"},
              {"code": 200, "data": {"data": "DEF456"}}]

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(bodies[seen["n"]]).encode()

    def fake_urlopen(req, timeout=None):
        seen["n"] = seen.get("n", -1) + 1
        assert req.data == b"{}"                      # 匿名请求：不带任何凭据
        return Resp()

    monkeypatch.setattr(fom.urllib.request, "urlopen", fake_urlopen)
    cache = tmp_path / "cache"
    f = fom.Fetcher(cache=cache, delay=0.0)
    assert fom.resolve_ee(f, 8, None, cache) == "ABC123"
    assert fom.resolve_ee(f, 8, None, tmp_path / "other") == "DEF456"
    assert (cache / "resource_id.txt").read_text(encoding="utf-8") == "ABC123"
    assert fom.resolve_ee(f, 8, None, cache) == "ABC123"   # 命中缓存，不再请求
    assert seen["n"] == 1


def test_resolve_ee_prefers_cli_override(tmp_path):
    f = fom.Fetcher(cache=tmp_path, delay=0.0)
    assert fom.resolve_ee(f, 8, "  FEEDFACE  ", tmp_path) == "FEEDFACE"


# --------------------------------------------------------------------- 合规红线

def test_fetcher_only_touches_public_official_hosts():
    """抓取脚本不许碰登录/验证码/写接口，也不许出现第三方域名。"""
    text = MOD_PATH.read_text(encoding="utf-8")
    hosts = set(re.findall(r"https?://([A-Za-z0-9.\-]+)", text))
    assert hosts <= {"web-static.kurobbs.com", "api.kurobbs.com", "www.kurobbs.com"}

    urls = re.findall(r"f?[\"'](https?://[^\"']+)[\"']", text)
    joined = "\n".join(urls) + text
    for banned in ("login", "captcha", "gt4", "sendCode", "sms", "password", "/admin/"):
        assert banned not in joined, f"抓取脚本出现了不该有的关键字：{banned}"

    # 唯一一个非静态请求：匿名查地图资源 id，且不带任何凭据
    assert text.count("API_HOST") >= 1
    assert "/map/core/config/getMapResource" in text
    assert "token" not in text.lower().replace("资源 id", "")

    mods = re.findall(r"^(?:import|from)\s+([a-zA-Z_][\w.]*)", text, re.M)
    allowed = {"argparse", "json", "math", "sys", "time", "urllib", "urllib.error",
               "urllib.request", "dataclasses", "datetime", "pathlib", "__future__",
               "os", "PySide6.QtGui"}
    assert set(mods) <= allowed, sorted(set(mods) - allowed)
