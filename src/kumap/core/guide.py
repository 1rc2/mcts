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
        # 国家包的 bbox 通常不是正方形，x/y 的归一化单位对应的米数并不相同。
        # 只拿 x 跨度去乘会把纵向上千公里的距离压扁（实测 bbox 纵横比可差一倍），
        # 35m 到站判定也会跟着偏。
        dx = (float(b[0]) - float(a[0])) * max(1e-9, (x1 - x0)) * 25500.0
        dy = (float(b[1]) - float(a[1])) * max(1e-9, (y1 - y0)) * 25500.0
        return math.hypot(dx, dy)
    return norm * 25500.0


def relative_bearing(origin: tuple[float, float], target: tuple[float, float],
                     bbox: tuple | None = None,
                     facing: float | None = None) -> tuple[float, bool]:
    """点位相对「屏幕上方」的方位角（0~360，顺时针）。

    facing 是角色朝向（与地图画布一致：0=正北，顺时针度数）。给了朝向就以朝向为
    屏幕上方回算，怎么转视角箭头都指着目标；没给就退化为正北向上。
    返回 (角度, 是否用了朝向)，第二个值让 UI 能明确标注「北向基准」。
    """
    if bbox:
        x0, y0, x1, y1 = bbox
        dx = (float(target[0]) - float(origin[0])) * max(1e-9, x1 - x0)
        dy = (float(target[1]) - float(origin[1])) * max(1e-9, y1 - y0)
    else:
        dx = float(target[0]) - float(origin[0])
        dy = float(target[1]) - float(origin[1])
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return 0.0, facing is not None
    # 屏幕 y 向下，正北是 -y：atan2(dx, -dy) 正好是 0=上、顺时针
    north = math.degrees(math.atan2(dx, -dy)) % 360.0
    if facing is None:
        return north, False
    return (north - float(facing)) % 360.0, True


def auto_route(points: list[MapPoint], start: tuple[float, float] | None = None,
               layers: set[str] | None = None, floor: str = "",
               limit: int = 120, exclude_ids: set[str] | None = None) -> list[MapPoint]:
    """没有 routes 文件（或向导没有位置）时的兜底路线：把点位就近排成一条顺路顺序。

    与 plan_walk 同一套排序（最近邻 + 2-opt），区别只是不限半径：
    起点没给就取候选点位的中位数坐标，比取均值更抗离群点。
    不做全局 TSP 求解——10k 级点位下没有必要，先保证「有路线可用」。
    """
    exclude = exclude_ids or set()
    cand = [p for p in points
            if p.id not in exclude
            and (not layers or p.layer in layers)
            and point_floor(p) == str(floor or "")]
    if not cand:
        return []
    if start is None:
        xs = sorted(p.x for p in cand)
        ys = sorted(p.y for p in cand)
        start = (xs[len(xs) // 2], ys[len(ys) // 2])
    return plan_walk(points, start, layers=layers, floor=floor,
                     limit=limit, radius=None, exclude_ids=exclude)
