# -*- coding: utf-8 -*-
"""把官方 state=8 地面世界拆成四个国家/区域的独立地图包。

鸣潮的地面世界是箱庭式分区：瑝珑、黎那汐塔、罗伊冰原、黑海岸彼此独立。
不再把四国拼在一张总图上，而是分别生成：

    import/maps/surface_huanglong/   瑝珑
    import/maps/surface_rinascita/   黎那汐塔
    import/maps/surface_roy/         罗伊冰原
    import/maps/surface_blackshores/ 黑海岸

实现：
  * 区域归属来自官方 country.json（区域名 -> 顶层国家）；
  * 点位与说明来自 Electron 版 data/markers.json（u/v 已与官方底图同源）；
  * 底图按 bbox 从官方瓦片重新抓取：大区域用 256px/片，小区域自动提高片尺寸，
    避免 6144 总图里黑海岸只有一百多像素的问题；
  * 点位重新归一化到各自裁剪图；每个国家重新生成最近邻示例路线。

用法（项目根目录）：
    set PYTHONPATH=src
    python tools\\split_surface_nations.py --source D:\\mcts --dry-run
    python tools\\split_surface_nations.py --source D:\\mcts

需要 Pillow（requirements-dev.txt 已包含）。数据版权归库洛游戏/鸣潮官方，
仅供个人学习研究，不得二次分发或商用。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kumap.core import paths  # noqa: E402

API_HOST = "https://api.kurobbs.com"
OSS = "https://web-static.kurobbs.com/"
STATE = 8
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://www.kurobbs.com/mc/map/",
    "Content-Type": "application/json",
}
BG = (10, 13, 16)

NATIONS = [
    ("瑝珑", "surface_huanglong", 1, "1"),
    ("黎那汐塔", "surface_rinascita", 2, "3"),
    ("罗伊冰原", "surface_roy", 3, "4"),
    ("黑海岸", "surface_blackshores", 4, "900"),
]

LAYERS = [
    {"key": "chest", "label": "宝箱", "color": "#F2B134", "glyph": "diamond"},
    {"key": "anchor", "label": "锚点", "color": "#3EC6C0", "glyph": "beacon"},
    {"key": "material", "label": "材料", "color": "#66C46A", "glyph": "hex"},
    {"key": "enemy", "label": "敌人 / 声骸", "color": "#E05D5D", "glyph": "star"},
    {"key": "puzzle", "label": "解谜", "color": "#5B9DF9", "glyph": "ring"},
    {"key": "quest", "label": "任务", "color": "#F2705B", "glyph": "flag"},
    {"key": "npc", "label": "NPC / 服务点", "color": "#C9D1D9", "glyph": "dot"},
]
DISCLAIMER = ("底图与点位来自库街区大地图公开静态资源，由本项目离线拆分；"
              "版权归库洛游戏/鸣潮官方所有，仅供个人学习研究，不得二次分发或商用。")


def _post(url: str) -> dict:
    req = urllib.request.Request(url, data=b"{}", headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"],
                                               "Referer": HEADERS["Referer"]})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", "ignore"))


def resolve_ee(override: str | None) -> str:
    if override:
        return override
    payload = _post(f"{API_HOST}/map/core/config/getMapResource")
    raw = payload.get("data")
    if isinstance(raw, dict):
        raw = raw.get("data")
    if not raw:
        raise RuntimeError(f"取不到地图资源 id：{payload}")
    return str(raw)


def fetch_grid(ee: str = "") -> tuple[int, int, int, int]:
    """返回 (cols, rows, tx_min, ty_max)，与官方瓦片索引一致。"""
    payload = _post(f"{API_HOST}/map/core/config/getMapIdList")
    keys = (payload.get("data") or {}).get(str(STATE)) or []
    coords = []
    for key in keys:
        parts = str(key).split("_")
        if len(parts) == 3:
            coords.append((int(parts[1]), int(parts[2])))
    if not coords:
        raise RuntimeError("官方瓦片索引里没有 state=8 的数据")
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1, min(xs), max(ys))


def fetch_country(ee: str, override: str | None) -> list:
    if override:
        return json.loads(Path(override).read_text(encoding="utf-8"))
    return _get_json(f"{OSS}mcmap/country/{ee}/country.json")


def build_name2nation(tree: list) -> dict[str, str]:
    out: dict[str, str] = {}

    def walk(node, nation):
        if isinstance(node, list):
            for x in node:
                walk(x, nation)
            return
        if not isinstance(node, dict):
            return
        if node.get("name") is not None:
            out.setdefault(str(node["name"]), nation)
        for key in ("countrys", "children"):
            if node.get(key):
                walk(node[key], nation)

    for top in tree:
        walk(top, str(top.get("name") or ""))
    return out


def _tile_cache_path(cache: Path, tx: int, ty: int, size: int) -> Path:
    return cache / f"8_{tx}_{ty}_{size}.png"


def fetch_tile(cache: Path, ee: str, tx: int, ty: int, size: int, delay: float):
    path = _tile_cache_path(cache, tx, ty, size)
    if path.exists() and path.stat().st_size > 0:
        return path
    url = (f"{OSS}mcmap/tiles/{ee}/{STATE}/{STATE}_{tx}_{ty}.png"
           f"?x-oss-process=image/format,png/resize,w_{size},h_{size}")
    req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"],
                                               "Referer": HEADERS["Referer"]})
    time.sleep(delay)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:  # noqa: S310
            blob = resp.read()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        return path
    except Exception:
        return None


def choose_tile_size(span_w: float, span_h: float) -> int:
    for size in (256, 512, 1024):
        if max(span_w * size, span_h * size) >= 2400:
            return size
    return 1024


def build_routes(points: list[dict], nation: str, key: str) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for p in points:
        if p.get("region") and p["layer"] in ("chest", "material", "puzzle", "anchor", "enemy"):
            groups[(p["layer"], str(p["region"]))].append(p)
    specs = [("chest", "奇藏箱", 2), ("material", "采集", 1),
             ("puzzle", "解谜探索", 1), ("anchor", "锚点", 1), ("enemy", "敌人", 1)]
    routes: list[dict] = []
    for layer, label, limit in specs:
        clusters = sorted(((k, v) for k, v in groups.items() if k[0] == layer),
                          key=lambda kv: -len(kv[1]))[:limit]
        for (_l, region), pts in clusters:
            if len(pts) < 4:
                continue
            rest = sorted(pts, key=lambda q: (q["x"], q["y"]))
            chain = [rest.pop(0)]
            while rest and len(chain) < 18:
                last = chain[-1]
                rest.sort(key=lambda q: (q["x"] - last["x"]) ** 2 + (q["y"] - last["y"]) ** 2)
                chain.append(rest.pop(0))
            routes.append({
                "id": f"{key}-{layer}-{len(routes) + 1:02d}",
                "name": f"{nation} · {region} · {label}路线",
                "author": "官方公开点位自动串联",
                "description": "按官方公开点位的最近邻顺序生成，仅作路线导航演示，非最优路径。",
                "tags": [label, "自动生成"],
                "est_minutes": max(5, len(chain) * 2),
                "region_ids": [],
                "steps": [{"seq": i + 1, "point_id": p["id"], "label": p["name"], "note": ""}
                          for i, p in enumerate(chain)],
            })
    return routes


def build_pack(nation: str, key: str, order: int, country: str, markers: list[dict], grid,
               ee: str, imp: Path, margin: float, delay: float, dry_run: bool) -> dict:
    from PIL import Image

    cols, rows, tx_min, ty_max = grid
    xs = [p["u"] for p in markers]
    ys = [p["v"] for p in markers]
    x0 = max(0.0, min(xs) - margin)
    x1 = min(1.0, max(xs) + margin)
    y0 = max(0.0, min(ys) - margin)
    y1 = min(1.0, max(ys) + margin)
    span_w, span_h = x1 - x0, y1 - y0
    size = choose_tile_size((x1 - x0) * cols, (y1 - y0) * rows)

    c0 = min(cols - 1, int(x0 * cols))
    c1 = min(cols - 1, int(max(0.0, x1 - 1e-9) * cols))
    r0 = min(rows - 1, int(y0 * rows))
    r1 = min(rows - 1, int(max(0.0, y1 - 1e-9) * rows))
    tx0, tx1 = tx_min + c0, tx_min + c1
    ty_top, ty_bottom = ty_max - r0, ty_max - r1
    ninfo = {"nation": nation, "key": key, "points": len(markers),
             "bbox": [x0, y0, x1, y1], "country": country,
             "tiles": (tx1 - tx0 + 1) * (ty_top - ty_bottom + 1),
             "tile_size": size, "grid": grid}
    print(f"[{nation}] {len(markers)} 点 bbox=({x0:.3f},{y0:.3f})-({x1:.3f},{y1:.3f}) "
          f"瓦片 {tx1-tx0+1}x{ty_top-ty_bottom+1} @ {size}px")
    if dry_run:
        return ninfo

    dest = imp / "maps" / key
    dest.mkdir(parents=True, exist_ok=True)
    cache = imp / "maps" / ".tilecache"
    cache.mkdir(parents=True, exist_ok=True)

    wpx, hpx = (c1 - c0 + 1) * size, (r1 - r0 + 1) * size
    canvas = Image.new("RGB", (wpx, hpx), BG)
    total = (tx1 - tx0 + 1) * (ty_top - ty_bottom + 1)
    done = ok = 0
    for col, tx in enumerate(range(tx0, tx1 + 1)):
        for row, ty in enumerate(range(ty_top, ty_bottom - 1, -1)):
            done += 1
            path = fetch_tile(cache, ee, tx, ty, size, delay)
            if path:
                tile = Image.open(path).convert("RGBA")
                if tile.size != (size, size):
                    tile = tile.resize((size, size), Image.LANCZOS)
                canvas.paste(tile, (col * size, row * size), tile)
                ok += 1
            if done % 30 == 0 or done == total:
                print(f"  瓦片 {done}/{total}（有效 {ok}）")

    xA, yA = c0 / cols, r0 / rows
    box = (int((x0 - xA) * cols * size), int((y0 - yA) * rows * size),
           int((x1 - xA) * cols * size), int((y1 - yA) * rows * size))
    box = (max(0, box[0]), max(0, box[1]),
           min(wpx, box[2]), min(hpx, box[3]))
    crop = canvas.crop(box)
    image_out = dest / "world_map.jpg"
    crop.save(image_out, "JPEG", quality=90, optimize=True, progressive=True)
    print(f"  底图 {crop.width}x{crop.height} -> {image_out}")

    points = []
    for m in markers:
        nx = (float(m["u"]) - x0) / span_w
        ny = (float(m["v"]) - y0) / span_h
        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))
        points.append({
            "id": str(m["id"]),
            "layer": str(m.get("type") or "other"),
            "name": str(m.get("name") or m["id"]),
            "x": round(nx, 6),
            "y": round(ny, 6),
            "region": str(m.get("region") or "") or None,
            "level": None,
            "drops": [],
            "note": str(m.get("note") or m.get("drops") or "").strip(),
            "author": "库街区官方大地图",
            "source": "kurobbs",
            "official_type": str(m.get("type") or ""),
        })
    regions = []
    seen = set()
    for p in points:
        name = p.get("region")
        if name and name not in seen:
            seen.add(name)
            regions.append({"id": name, "name": name})

    payload = {
        "schema_version": 1,
        "source": "kurobbs",
        "disclaimer": DISCLAIMER,
        "map": {"id": key, "name": f"鸣潮 · {nation}（官方公开数据）",
                "width": crop.width, "height": crop.height},
        "layers": LAYERS,
        "regions": regions,
        "points": points,
    }
    routes = build_routes(points, nation, key)
    routes_payload = {"schema_version": 1, "source": "kurobbs",
                      "disclaimer": DISCLAIMER, "routes": routes}
    (dest / f"points_{key}.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (dest / f"routes_{key}.json").write_text(
        json.dumps(routes_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (dest / "pack.json").write_text(json.dumps({
        "key": key, "state": STATE, "country": country, "name": f"{nation}（地面）",
        "order": order, "points": f"points_{key}.json",
        "image": "world_map.jpg", "source": "kurobbs", "disclaimer": DISCLAIMER,
        "bbox": [round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  点位 {len(points)} / 区域 {len(regions)} / 路线 {len(routes)} -> {dest}")
    ninfo.update({"routes": len(routes), "image": [crop.width, crop.height],
                  "tiles_ok": ok, "output": str(dest)})
    return ninfo


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把官方地面世界拆成四个国家/区域地图包")
    ap.add_argument("--source", default=str(PROJECT_ROOT.parent),
                    help="Electron 版仓库目录（含 data/markers.json）")
    ap.add_argument("--data-dir", default=None, help="目标数据目录，默认 %%APPDATA%%\\KuroMapHelper")
    ap.add_argument("--country-json", default=None, help="离线 country.json（默认在线获取）")
    ap.add_argument("--ee", default=None, help="地图资源 id（默认自动查询）")
    ap.add_argument("--margin", type=float, default=0.008, help="裁剪边距（相对整张 6144 图）")
    ap.add_argument("--delay", type=float, default=0.12, help="瓦片请求间隔秒")
    ap.add_argument("--only", nargs="*", default=None, help="只处理指定国家名")
    ap.add_argument("--keep-combined", action="store_true",
                    help="保留 import 根目录的四国总图（默认移到 _combined_backup）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不下载瓦片")
    args = ap.parse_args(argv)

    source = Path(args.source).expanduser().resolve()
    markers_path = source / "data" / "markers.json"
    if not markers_path.exists():
        print(f"[ERROR] 没找到 {markers_path}", file=sys.stderr)
        return 2
    markers = json.loads(markers_path.read_text(encoding="utf-8")).get("markers") or []
    ee = resolve_ee(args.ee)
    tree = fetch_country(ee, args.country_json)
    name2nation = build_name2nation(tree)
    grid = fetch_grid(ee)
    print(f"resource={ee} grid={grid}")
    imp = Path(args.data_dir).expanduser().resolve() if args.data_dir else paths.data_root() / "import"

    groups: dict[str, list[dict]] = defaultdict(list)
    for m in markers:
        nation = name2nation.get(str(m.get("region") or ""))
        if nation:
            groups[nation].append(m)
    wanted = set(args.only) if args.only else None
    infos = []
    for nation, key, order, country in NATIONS:
        if not groups.get(nation):
            continue
        if wanted and nation not in wanted:
            continue
        infos.append(build_pack(nation, key, order, country, groups[nation], grid, ee, imp,
                                args.margin, args.delay, args.dry_run))
    if args.dry_run:
        print(json.dumps(infos, ensure_ascii=False, indent=1))
        return 0
    if not infos:
        print("[ERROR] 没有生成任何地图包", file=sys.stderr)
        return 3

    if not args.keep_combined:
        backup = imp / "_combined_backup"
        backup.mkdir(parents=True, exist_ok=True)
        moved = []
        for name in ("points_kurobbs_official.json", "routes_kurobbs_official.json",
                     "world_map.jpg", "kurobbs_meta.json"):
            src = imp / name
            if src.exists():
                shutil.move(str(src), str(backup / name))
                moved.append(name)
        if moved:
            print(f"已把拼合总图移到 {backup}：{', '.join(moved)}")

    print("完成。重启程序：侧栏「地图数据」会出现四个独立地面地图，默认加载瑝珑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
