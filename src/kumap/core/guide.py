"""探索向导的纯逻辑：分层识别、最近邻 + 2-opt 连续路线。

设计目标：
- 从当前位置出发，只向前走，不重复访问已选点位（自然不走回头路）；
- 先取近处候选（局部探索），再用 2-opt 消除明显交叉；
- 严格按 floor 分组，地上和地下不混在一条路线里。
"""

from __future__ import annotations

import math
import re

from .models import MapPoint

_FLOOR_RE = re.compile(r"\[分层\s*([^\]]+)\]")
ARRIVE_METERS = 35.0


def point_floor(point: MapPoint) -> str:
    """官方分层 id；空字符串表示地上。"""
    floor = str(getattr(point, "floor", "") or "").strip()
    if floor:
        return floor
    m = _FLOOR_RE.search(str(getattr(point, "note", "") or ""))
    return m.group(1).strip() if m else ""


def floor_label(floor: str) -> str:
    floor = str(floor or "").strip()
    if not floor:
        return "地上"
    if floor.startswith("-"):
        return "地下 " + floor
    return floor


def floor_from_combo(text: str) -> str:
    text = str(text or "").strip()
    if text in ("", "地上"):
        return ""
    if text.startswith("地下 "):
        return text[3:].strip()
    return text


def generic_hint(layer: str) -> str:
    return {
        "chest": "打开宝箱 / 收集物，注意周围敌人。",
        "puzzle": "完成解谜或挑战，留意机关与引导。",
        "quest": "接取/推进任务，注意对话与目标区域。",
        "anchor": "激活信标/传送点，方便之后回访。",
        "material": "采集材料，顺路带走即可。",
        "enemy": "击败敌人，注意站位与仇恨。",
        "npc": "与 NPC 交互，查看任务/商店。",
    }.get(str(layer), "到达点位，按提示完成。")


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def two_opt_order(points: list[MapPoint], start: tuple[float, float]) -> list[MapPoint]:
    """返回 2-opt 后的点位顺序（与输入对象一一对应）。"""
    if len(points) < 3:
        return list(points)
    indexed = list(enumerate(points))
    path: list[int] = [-1] + [i for i, _ in indexed]
    coords = {i: (p.x, p.y) for i, p in indexed}

    def at(idx: int):
        return start if idx < 0 else coords[idx]

    improved = True
    passes = 0
    while improved and passes < 8:
        improved = False
        passes += 1
        n = len(path)
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                a, b, c = path[i - 1], path[i], path[j]
                d = path[j + 1] if j + 1 < n else None
                old = _dist(at(a), at(b)) + (_dist(at(c), at(d)) if d is not None else 0.0)
                new = _dist(at(a), at(c)) + (_dist(at(b), at(d)) if d is not None else 0.0)
                if new + 1e-12 < old:
                    path[i:j + 1] = reversed(path[i:j + 1])
                    improved = True
    return [points[i] for i in path[1:]]


def plan_walk(points: list[MapPoint], start: tuple[float, float],
              layers: set[str] | None = None, floor: str = "",
              limit: int = 60, radius: float | None = 0.2,
              exclude_ids: set[str] | None = None) -> list[MapPoint]:
    """从 start 出发，按最近邻 + 2-opt 生成一条连续、不重复的路线。

    floor 空字符串表示地上；limit 限制局部候选数量，避免一次规划上千点。
    """
    want_floor = str(floor or "")
    exclude = exclude_ids or set()
    cand: list[tuple[float, MapPoint]] = []
    for p in points:
        if p.id in exclude or (layers and p.layer not in layers):
            continue
        if point_floor(p) != want_floor:
            continue
        d = _dist(start, (p.x, p.y))
        if radius is not None and d > radius:
            continue
        cand.append((d, p))
    if not cand:
        return []
    cand.sort(key=lambda item: item[0])
    near = [p for _d, p in cand[:max(1, int(limit))]]

    remaining = list(near)
    route: list[MapPoint] = []
    cur = start
    while remaining:
        remaining.sort(key=lambda p: (p.x - cur[0]) ** 2 + (p.y - cur[1]) ** 2)
        nxt = remaining.pop(0)
        route.append(nxt)
        cur = (nxt.x, nxt.y)
    return two_opt_order(route, start)


def meters_between(a: tuple[float, float], b: tuple[float, float],
                   bbox: tuple | None) -> float:
    norm = _dist(a, b)
    if bbox:
        x0, y0, x1, y1 = bbox
        return norm * max(1e-9, (x1 - x0)) * 25500.0
    return norm * 6144.0 * (25500.0 / 6144.0)
