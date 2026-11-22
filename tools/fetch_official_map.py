#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「库街区《鸣潮》官方 H5 大地图」的公开静态资源本地化成本工具可用的数据包。

产出（写进本机数据目录的 import/ 子目录，程序加载优先级已支持，无需改配置）：

    import/world_map.png                 由官方瓦片拼成的整张同源底图
    import/points_kurobbs_<state>.json   官方点位（含分类图层、区域、说明），坐标已归一化
    import/kurobbs_meta.json             出处、抓取时间、瓦片网格与坐标换算参数
    import/kurobbs_cache/                瓦片与原始 JSON 缓存（可重复执行、断点续传）

合规口径（务必遵守，工具本身也按此设计）：

* 只读取**匿名可访问的公开静态文件**（图片瓦片与 JSON），与任何人打开
  https://www.kurobbs.com/mc/map/ 时浏览器发的请求同源同类；
* 不登录、不调用任何需要凭据的接口、不碰极验滑块与任何验证逻辑；
* 串行请求 + 固定间隔 + 本地缓存优先，默认一次跑完就长期离线使用，不要频繁重跑；
* 数据版权归库洛游戏/鸣潮官方所有，仅供个人学习研究，不得二次分发或商用。

坐标换算（由官方前端逆向并实测闭环，state=8）：

    S      = 85000                       # 世界单位 / 瓦片，与瓦片像素边长无关
    tileX  = floor(X / S) + 1            # 向 -inf 取整
    tileY  = -floor(Y / S)               # Y 越大越靠画面下方
    瓦片覆盖：X∈[(tileX-1)S, tileX·S)   Y∈[(-tileY)S, (1-tileY)S)

用法示例（Windows）：

    set PYTHONPATH=src
    python tools\\fetch_official_map.py --dry-run
    python tools\\fetch_official_map.py --yes
    python tools\\fetch_official_map.py --tile-width 64 --scope near --near 1
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

WORLD_PER_TILE = 85000.0          # S：一个瓦片代表的游戏世界单位
OSS = "https://web-static.kurobbs.com/"
API_HOST = "https://api.kurobbs.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
SOURCE_TAG = "kurobbs"
DEFAULT_STATE = 8
# 官方底图源瓦片原生 1024px，只有降采样有效
TILE_WIDTHS = (32, 64, 128, 256, 512, 1024)
DISCLAIMER = ("数据来自库街区《鸣潮》官方大地图的公开静态资源，坐标与名称版权归库洛游戏；"
              "本文件仅供个人学习研究使用，不得二次分发或商用。识图定位存在误差，"
              "本工具仅为探索参考辅助，不保证识别结果完全准确。")

# 官方 10 个大类 → 本工具图层。key 用官方目录 id 保证重抓不漂移；
# 与内置 6 图层语义一致的沿用内置 key，其余用 k<id> 独立图层。
GROUP_LAYERS = {
    "收集物": ("chest", "宝箱·收集物", "#f2b134"),
    "NPC及服务点": ("anchor", "信标·服务点", "#3ec6c0"),
    "采集物": ("material", "采集物", "#66c46a"),
    "BOSS": ("echo", "BOSS·声骸", "#b07de0"),
    "强敌": ("echo", "BOSS·声骸", "#b07de0"),
    "挑战": ("puzzle", "挑战·解谜", "#5b9df9"),
    "探索": ("puzzle", "挑战·解谜", "#5b9df9"),
    "敌人": ("k6", "敌人", "#8f9bb3"),
    "角色": ("k1", "角色相关", "#f2705b"),
    "武器": ("k2", "武器相关", "#c9a227"),
}
FALLBACK_LAYER = ("kx", "其它", "#9aa0a6")


# --------------------------------------------------------------------- 坐标换算

def tile_of(x: float, y: float, s: float = WORLD_PER_TILE) -> tuple[int, int]:
    """世界坐标 → 官方瓦片号（与官方前端 gameLocationConverter 一致）。"""
    return int(math.floor(x / s)) + 1, -int(math.floor(y / s))


def tile_world_rect(tx: int, ty: int, s: float = WORLD_PER_TILE) -> tuple[float, float, float, float]:
    """瓦片覆盖的世界矩形 (X0, Y0, X1, Y1)，左闭右开。"""
    return (tx - 1) * s, -ty * s, tx * s, (1 - ty) * s


def tile_url(ee: str, state: int, tx: int, ty: int, width: int, fmt: str = "png") -> str:
    return (f"{OSS}mcmap/tiles/{ee}/{state}/{state}_{tx}_{ty}.png"
            f"?x-oss-process=image/format,{fmt}/resize,w_{width},h_{width}")


def static_url(ee: str, state: int, name: str) -> str:
    """公开 JSON 清单。name 取 POSITION/COUNTRY/... 之一（见 RES_FILES）。"""
    path = RES_FILES[name].format(ee=ee, state=state)
    return f"{OSS}{path}?_t={int(time.time())}"


RES_FILES = {
    "POSITION": "mcmap/position/{state}/position.json",
    "COUNTRY": "mcmap/country/{ee}/country.json",
    "CATALOG": "mcmap/catalog/{ee}/{state}/catalog.json",
    "LAYER": "mcmap/layer/{ee}/{state}/layer.json",
    "AREA": "mcmap/area/{ee}/area.json",
    "CATALOG_RELATION": "mcmap/catalog/{ee}/catalogRelation.json",
}


def envelope(tiles: list[tuple[int, int]], radius: int = 5,
             core_min: int = 3) -> tuple[tuple[int, int, int, int], list[tuple[int, int]]]:
    """由点位所在瓦片求内容包络，并剔掉「孤立的飞格」（官方数据里存在个别坏坐标）。

    判定口径：点数 ≥ core_min 的格子算「有内容」的核心格；某格点数不足且切比雪夫距离
    radius 内没有任何核心格，就当作飞点剔除。刻意不用百分位裁剪——按官方 state=8 实测，
    西端 黑海岸（-10,*）与东端 黎那汐塔（16,*、17,*）每格只有几十甚至 2 个点，
    百分位会把这些真内容整列裁掉（曾误删 140 个点），而真正的坏坐标只有 1 个点。

    返回 ((txMin, txMax, tyMin, tyMax), 被剔除的格子列表)。
    """
    if not tiles:
        raise ValueError("没有任何点位瓦片，无法确定底图范围")
    counts: dict[tuple[int, int], int] = {}
    for t in tiles:
        counts[t] = counts.get(t, 0) + 1
    core = {t for t, n in counts.items() if n >= core_min}
    if not core:                      # 极端情况：所有格都只有零星点，那就谁都不算飞点
        core = set(counts)

    def isolated(t: tuple[int, int]) -> bool:
        if t in core:
            return False
        tx, ty = t
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if (tx + dx, ty + dy) in core:
                    return False
        return True

    out = sorted(t for t in counts if isolated(t))
    kept = [t for t in counts if t not in set(out)]
    xs = [t[0] for t in kept]
    ys = [t[1] for t in kept]
    return (min(xs), max(xs), min(ys), max(ys)), out


# ------------------------------------------------------------------ 数据转换

def build_layers(catalog: list[dict]) -> tuple[dict[str, tuple[str, str, str]], dict[str, str]]:
    """官方目录树 → (typeId→图层三元组, 图层 key→官方大类名)。"""
    by_type: dict[str, tuple[str, str, str]] = {}
    names: dict[str, str] = {}
    for group in catalog or []:
        gname = str(group.get("name") or "")
        key, label, color = GROUP_LAYERS.get(gname, (f"k{group.get('id')}", gname or "其它", "#9aa0a6"))
        names[key] = gname or label
        for child in group.get("children") or []:
            tid = str(child.get("id") or "")
            if tid:
                by_type[tid] = (key, label, color)
    return by_type, names


def convert_points(position: list[dict], by_type: dict[str, tuple[str, str, str]],
                   countries: dict[str, str], grid: tuple[int, int, int, int],
                   width: int, height: int, state: int) -> tuple[dict, list[dict]]:
    """官方点位 JSON → 本工具 points 载荷；返回 (载荷, 被丢弃条目的说明)。"""
    tx_min, tx_max, ty_min, ty_max = grid
    s = WORLD_PER_TILE
    world_x0 = (tx_min - 1) * s        # 图像左上角对应的世界 X
    span_x = (tx_max - tx_min + 1) * s
    span_y = (ty_max - ty_min + 1) * s

    used: dict[str, tuple[str, str, str]] = {}
    points: list[dict] = []
    dropped: list[dict] = []
    for entry in position:
        tid = str(entry.get("id") or "")
        type_name = str(entry.get("name") or tid)
        layer = by_type.get(tid, FALLBACK_LAYER)
        for loc in entry.get("location") or []:
            if int(loc.get("stateId") or 0) != int(state):
                continue                      # 其它版本地图的点
            x, y = float(loc.get("x") or 0.0), float(loc.get("y") or 0.0)
            tx, ty = tile_of(x, y)
            if not (tx_min <= tx <= tx_max and ty_min <= ty <= ty_max):
                dropped.append({"id": str(loc.get("id")), "x": x, "y": y, "reason": "超出包络"})
                continue
            nx = (x - world_x0) / span_x
            ny = (y - (-ty_max * s)) / span_y  # Y 越大越靠下，与图像行方向一致
            key, label, color = layer
            used[key] = (label, color, "")
            pid = f"{SOURCE_TAG}_{loc.get('id')}"
            region = f"c{loc.get('countryId')}"
            note = str(loc.get("description") or "").strip()
            floor = str(loc.get("floorId") or "")
            if floor:
                note = (note + " " if note else "") + f"[分层 {floor}]"
            level = loc.get("level")
            points.append({
                "id": pid,
                "layer": key,
                "name": type_name,
                "x": round(min(1.0, max(0.0, nx)), 6),
                "y": round(min(1.0, max(0.0, ny)), 6),
                "region": region,
                "level": int(level) if isinstance(level, int) or (isinstance(level, str) and level.isdigit()) else None,
                "drops": [type_name],
                "note": note,
                "author": "库街区官方大地图",
                "source": SOURCE_TAG,
                # 原始世界坐标留档，便于日后与游戏内「位置同步」对齐或改投影时重算
                "world": [x, y],
                "official_type": tid,
            })
    payload = {
        "schema_version": 1,
        "source": SOURCE_TAG,
        "disclaimer": DISCLAIMER,
        "map": {"id": f"kurobbs_state{state}", "name": f"库街区官方大地图（state {state}）",
                "width": width, "height": height},
        "layers": [{"key": k, "label": v[0], "color": v[1]} for k, v in sorted(used.items())],
        "regions": [{"id": rid, "name": name} for rid, name in sorted(countries.items())],
        "points": points,
    }
    return payload, dropped


# ------------------------------------------------------------------ 抓取实现

@dataclass
class Fetcher:
    """串行 + 限速 + 缓存的公开静态资源下载器。404 视为「该瓦片不存在」，不算错误。"""

    cache: Path
    delay: float = 0.25
    retries: int = 2
    timeout: float = 20.0
    ua: str = UA
    last: float = 0.0
    stats: dict[str, int] = field(default_factory=lambda: {"hit": 0, "ok": 0, "missing": 0, "error": 0, "bytes": 0})

    def throttle(self) -> None:
        wait = self.last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()

    def fetch(self, url: str, dest: Path | None = None, *, binary: bool = True,
              required: bool = True) -> bytes | str | None:
        if dest and dest.exists() and dest.stat().st_size > 0:
            self.stats["hit"] += 1
            data = dest.read_bytes()
            return data if binary else data.decode("utf-8", "ignore")
        if dest is not None and dest.with_suffix(dest.suffix + ".404").exists():
            self.stats["missing"] += 1
            return None          # 上次已确认官方没有这一片，不再重复敲 CDN
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            self.throttle()
            req = urllib.request.Request(url, headers={"User-Agent": self.ua,
                                                       "Accept": "*/*",
                                                       "Referer": "https://www.kurobbs.com/mc/map/"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - 固定官方域名
                    data = resp.read()
                self.stats["ok"] += 1
                self.stats["bytes"] += len(data)
                if dest:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    tmp.write_bytes(data)
                    tmp.replace(dest)
                return data if binary else data.decode("utf-8", "ignore")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    self.stats["missing"] += 1
                    if required:
                        raise RuntimeError(f"官方资源不存在（404）：{url}") from exc
                    if dest is not None:      # 记下「确实没有这一片」，重跑时直接跳过
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.with_suffix(dest.suffix + ".404").write_bytes(b"")
                    return None
                last_err = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = exc
            if attempt < self.retries:
                time.sleep(self.delay * (2 ** attempt) + 0.4)
        self.stats["error"] += 1
        if required:
            raise RuntimeError(f"下载失败：{url}（{last_err}）") from last_err
        return None


def resolve_ee(fetcher: Fetcher, state: int, override: str | None, cache: Path) -> str:
    """地图资源 id：优先命令行覆盖 → 缓存 → 匿名接口查一次。"""
    if override:
        return override.strip()
    hint = cache / "resource_id.txt"
    if hint.exists():
        return hint.read_text(encoding="utf-8").strip()
    body = json.dumps({}).encode()
    req = urllib.request.Request(f"{API_HOST}/map/core/config/getMapResource", data=body,
                                 headers={"User-Agent": fetcher.ua,
                                          "Content-Type": "application/json",
                                          "Referer": "https://www.kurobbs.com/mc/map/"})
    fetcher.throttle()
    with urllib.request.urlopen(req, timeout=fetcher.timeout) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode("utf-8", "ignore"))
    # 官方两种回包形状都见过：{"data":"<id>"} 与 {"data":{"data":"<id>"}}
    raw = payload.get("data")
    if isinstance(raw, dict):
        raw = raw.get("data")
    ee = str(raw or "").strip()
    if not ee:
        raise RuntimeError(f"未能从 getMapResource 取得资源 id：{payload}")
    cache.mkdir(parents=True, exist_ok=True)
    hint.write_text(ee, encoding="utf-8")
    return ee


def fetch_tile_keys(fetcher: Fetcher, state: int) -> set[tuple[int, int]]:
    """官方 getMapIdList：该 state 真实存在的瓦片集合（避免用点位包络撑大画布）。"""
    body = json.dumps({}).encode()
    req = urllib.request.Request(f"{API_HOST}/map/core/config/getMapIdList", data=body,
                                 headers={"User-Agent": fetcher.ua,
                                          "Content-Type": "application/json",
                                          "Referer": "https://www.kurobbs.com/mc/map/"})
    fetcher.throttle()
    with urllib.request.urlopen(req, timeout=fetcher.timeout) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode("utf-8", "ignore"))
    data = payload.get("data") or {}
    keys = data.get(str(state)) or data.get(state) or []
    out: set[tuple[int, int]] = set()
    for key in keys:
        parts = str(key).split("_")
        if len(parts) == 3:
            try:
                out.add((int(parts[1]), int(parts[2])))
            except ValueError:
                continue
    return out


def load_json(fetcher: Fetcher, cache: Path, ee: str, state: int, name: str, required: bool = True):
    dest = cache / "json" / f"{name.lower()}.json"
    raw = fetcher.fetch(static_url(ee, state, name), dest=dest, binary=False, required=required)
    if raw is None:
        return None
    return json.loads(raw)


def tile_plan(grid: tuple[int, int, int, int], point_tiles: set[tuple[int, int]],
              scope: str, near: int) -> list[tuple[int, int]]:
    tx_min, tx_max, ty_min, ty_max = grid
    if scope == "near":
        wanted: set[tuple[int, int]] = set()
        for (px, py) in point_tiles:
            for dx in range(-near, near + 1):
                for dy in range(-near, near + 1):
                    c = (px + dx, py + dy)
                    if tx_min <= c[0] <= tx_max and ty_min <= c[1] <= ty_max:
                        wanted.add(c)
        return sorted(wanted)
    return [(x, y) for y in range(ty_max, ty_min - 1, -1) for x in range(tx_min, tx_max + 1)]


def stitch(tiles: dict[tuple[int, int], Path], grid: tuple[int, int, int, int],
           width: int, out: Path) -> tuple[int, int]:
    """按 col=tx-txMin / row=tyMax-ty 把瓦片拼成一张图；官方对象集合不是矩形，缺片以深色填充。"""
    from PySide6.QtGui import QColor, QImage, QPainter

    tx_min, tx_max, ty_min, ty_max = grid
    w = (tx_max - tx_min + 1) * width
    h = (ty_max - ty_min + 1) * width
    canvas = QImage(w, h, QImage.Format.Format_RGBA8888)
    canvas.fill(QColor(10, 12, 16))
    painter = QPainter(canvas)
    try:
        for (tx, ty), path in tiles.items():
            img = QImage(str(path))
            if img.isNull():
                continue
            if img.width() != width or img.height() != width:
                img = img.scaled(width, width)
            painter.drawImage((tx - tx_min) * width, (ty_max - ty) * width, img)
    finally:
        painter.end()
    out.parent.mkdir(parents=True, exist_ok=True)
    if not canvas.save(str(out), "PNG"):
        raise RuntimeError(f"底图写出失败：{out}")
    return w, h


# ------------------------------------------------------------------------- CLI

def default_data_dir() -> Path:
    import os
    env = os.environ.get("KUMAPH_DATA_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "KuroMapHelper"
    return Path.home() / ".config" / "KuroMapHelper"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="下载库街区官方大地图的公开瓦片与点位数据，生成本工具可导入的数据包")
    ap.add_argument("--data-dir", default=None, help=f"本机数据目录（默认 {default_data_dir()}）")
    ap.add_argument("--state", type=int, default=DEFAULT_STATE, help="地图状态号（当前官方为 8）")
    ap.add_argument("--ee", default=None, help="手工指定地图资源 id（默认自动查一次并缓存）")
    ap.add_argument("--tile-width", type=int, default=128, choices=TILE_WIDTHS,
                    help="每片瓦片下载边长（官方源片 1024，只支持降采样）")
    ap.add_argument("--scope", choices=("all", "near"), default="all",
                    help="all=包络内整格网；near=只下有点位的格子及其邻域（省流量）")
    ap.add_argument("--tiles-from-index", action="store_true",
                    help="用官方 getMapIdList 的真实瓦片集合确定范围（子地图/多楼层推荐，忽略 --scope）")
    ap.add_argument("--near", type=int, default=1, help="near 模式下向四周扩展的格数")
    ap.add_argument("--format", dest="fmt", choices=("png", "webp"), default="png",
                    help="瓦片编码；webp 更省流量但需要 Qt 带 webp 解码")
    ap.add_argument("--delay", type=float, default=0.25, help="两次请求之间的最小间隔秒数")
    ap.add_argument("--max-tiles", type=int, default=900, help="安全阀：超过该瓦片数就拒绝执行")
    ap.add_argument("--limit", type=int, default=0, help="只下载计划中的前 N 片（连通性自检用，0=不限）")
    ap.add_argument("--skip-tiles", action="store_true", help="只更新点位 JSON，不重下底图")
    ap.add_argument("--skip-points", action="store_true", help="只拼底图")
    ap.add_argument("--keep-points-json", action="store_true",
                    help="不把官方点位写进 import/（避免覆盖你自己整理的点位包）")
    ap.add_argument("--dry-run", action="store_true", help="只读 JSON 并统计，不下载瓦片、不写文件")
    ap.add_argument("--yes", action="store_true", help="非交互，直接执行")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data = Path(args.data_dir).expanduser() if args.data_dir else default_data_dir()
    imp = data / "import"
    cache = imp / "kurobbs_cache"
    fetcher = Fetcher(cache=cache, delay=max(0.05, args.delay))

    print(f"数据目录：{data}")
    print("只读公开静态资源；请低频使用，数据版权归库洛游戏，仅限个人学习研究。")
    ee = resolve_ee(fetcher, args.state, args.ee, cache)
    print(f"地图资源 id：{ee}  state={args.state}")

    position = load_json(fetcher, cache, ee, args.state, "POSITION")
    catalog = load_json(fetcher, cache, ee, args.state, "CATALOG", required=False) or []
    country = load_json(fetcher, cache, ee, args.state, "COUNTRY", required=False) or []
    by_type, _ = build_layers(catalog)
    countries = {f"c{c.get('countryId')}": str(c.get("name") or c.get("countryId")) for c in country}

    tiles_of_points = [tile_of(float(l.get("x") or 0.0), float(l.get("y") or 0.0))
                       for e in position for l in (e.get("location") or [])
                       if int(l.get("stateId") or 0) == args.state]
    if args.tiles_from_index:
        actual = fetch_tile_keys(fetcher, args.state)
        if not actual:
            print(f"state={args.state} 没有官方瓦片索引，无法按索引取范围。")
            return 2
        grid = (min(t[0] for t in actual), max(t[0] for t in actual),
                min(t[1] for t in actual), max(t[1] for t in actual))
        point_tiles = {t for t in tiles_of_points if t in actual}
        outliers = [t for t in tiles_of_points if t not in actual]
        plan = sorted(actual, key=lambda t: (t[1], t[0]))
        print(f"官方瓦片索引：X {grid[0]}..{grid[1]}  Y {grid[2]}..{grid[3]}"
              f"（真实瓦片 {len(actual)} 格，点位命中 {len(point_tiles)} 格，计划下载 {len(plan)} 格）")
    else:
        grid, outliers = envelope(tiles_of_points)
        point_tiles = set(tiles_of_points) - set(outliers)
        plan = tile_plan(grid, point_tiles, args.scope, args.near)
        print(f"包络瓦片：X {grid[0]}..{grid[1]}  Y {grid[2]}..{grid[3]}"
              f"（点位实占 {len(point_tiles)} 格，计划下载 {len(plan)} 格；剔除飞点格 {len(outliers)} 格）")
    if args.limit > 0:
        plan = plan[:args.limit]
    if args.dry_run:
        payload, dropped = convert_points(position, by_type, countries, grid, 1, 1, args.state)
        print(f"dry-run：点位数 {len(payload['points'])}，图层 {[l['label'] for l in payload['layers']]}，"
              f"估算底图 {len(plan)} 格 × {args.tile_width}px（仅估算，未下载、未写文件）")
        for d in dropped[:5]:
            print(f"  剔除点示例：{d}")
        return 0
    if len(plan) > args.max_tiles:
        print(f"计划瓦片数 {len(plan)} 超过 --max-tiles {args.max_tiles}，已中止。"
              f"可用 --scope near / --tile-width 调小后重试。")
        return 2
    if not args.yes and not args.skip_tiles:
        ans = input(f"将向官方 CDN 发起约 {len(plan)} 次请求（每次间隔 {args.delay}s，"
                    f"约 {len(plan) * (args.delay + 0.3):.0f}s），继续？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            return 1

    grid_w = (grid[1] - grid[0] + 1) * args.tile_width
    grid_h = (grid[3] - grid[2] + 1) * args.tile_width
    payload, dropped = convert_points(position, by_type, countries, grid, grid_w, grid_h, args.state)

    imp.mkdir(parents=True, exist_ok=True)
    if not args.skip_points and not args.keep_points_json:
        out_points = imp / f"points_{SOURCE_TAG}_{args.state}.json"
        tmp = out_points.with_suffix(".json.part")
        tmp.write_text(json.dumps(payload, ensure_ascii=False,
                             separators=(",", ":")), encoding="utf-8")
        tmp.replace(out_points)
        print(f"点位已写入：{out_points}（{len(payload['points'])} 个，图层 {len(payload['layers'])} 类）")

    meta = {"source": "库街区官方大地图公开静态资源", "fetched_at": datetime.now(timezone.utc).isoformat(),
            "resource_id": ee, "state": args.state, "world_per_tile": WORLD_PER_TILE,
            "tile_grid": {"tx_min": grid[0], "tx_max": grid[1], "ty_min": grid[2], "ty_max": grid[3]},
            "tile_width": args.tile_width, "scope": args.scope,
            "tiles_from_index": bool(args.tiles_from_index),
            "image": {"width": grid_w, "height": grid_h},
            "point_count": len(payload["points"]), "outliers": dropped[:50],
            "world_origin_x0": (grid[0] - 1) * WORLD_PER_TILE, "world_top_y0": -grid[3] * WORLD_PER_TILE,
            "disclaimer": DISCLAIMER}

    if not args.skip_tiles:
        got: dict[tuple[int, int], Path] = {}
        for i, (tx, ty) in enumerate(plan, 1):
            dest = cache / "tiles" / f"{tx}_{ty}_{args.tile_width}.{args.fmt}"
            data_ = fetcher.fetch(tile_url(ee, args.state, tx, ty, args.tile_width, args.fmt),
                                  dest=dest, required=False)
            if data_:
                got[(tx, ty)] = dest
            if i % 25 == 0 or i == len(plan):
                print(f"  瓦片 {i}/{len(plan)}（已有内容 {len(got)}）")
        if not got:
            print("没有拿到任何瓦片，检查网络或稍后重试（缓存目录里已有的瓦片不会重复下载）。")
            return 3
        w, h = stitch(got, grid, args.tile_width, imp / "world_map.png")
        meta["image"] = {"width": w, "height": h, "tiles_ok": len(got), "tiles_missing": len(plan) - len(got)}
        meta["tile_grid"]["tileY_top_row"] = grid[3]
        print(f"底图已写出：{imp / 'world_map.png'}（{w}×{h}，{len(got)}/{len(plan)} 片）")
        if max(w, h) > 1400:
            print("  提示：识图定位的世界索引上限是 1400px，更大的底图会被进一步降采样、识别成功率下降。"
                  "若主要靠识图：用 --tile-width 32（全图 28×28 格 → 896px）或 --scope near 缩小范围重抓。")

    (imp / "kurobbs_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"抓取统计：{fetcher.stats}")
    print(f"元数据：{imp / 'kurobbs_meta.json'}")
    print("完成。重启本工具即可加载（设置 → 数据与地图 里可确认当前数据文件）。")
    print("免责声明：识图定位存在误差，本工具仅为探索参考辅助，不保证识别结果完全准确。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
