"""数据模型：点位 / 图层 / 路线 / 标记状态。

所有坐标统一使用归一化值（0~1），这样换分辨率、换底图都不会错位。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

LAYER_FALLBACK_ORDER = ["chest", "anchor", "material", "echo", "puzzle", "quest"]

LAYER_FALLBACK: dict[str, dict[str, str]] = {
    "chest": {"label": "宝箱", "color": "#F2B134", "glyph": "diamond"},
    "anchor": {"label": "锚点", "color": "#3EC6C0", "glyph": "beacon"},
    "material": {"label": "材料", "color": "#66C46A", "glyph": "hex"},
    "echo": {"label": "声骸", "color": "#B07DE0", "glyph": "star"},
    "puzzle": {"label": "解谜", "color": "#5B9DF9", "glyph": "ring"},
    "quest": {"label": "任务", "color": "#F2705B", "glyph": "flag"},
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


@dataclass(frozen=True)
class Layer:
    key: str
    label: str
    color: str
    glyph: str = "dot"

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Layer":
        key = str(d.get("key", "other"))
        fb = LAYER_FALLBACK.get(key, {})
        return Layer(
            key=key,
            label=str(d.get("label") or fb.get("label") or key),
            color=str(d.get("color") or fb.get("color") or "#9AA4B2"),
            glyph=str(d.get("glyph") or fb.get("glyph") or "dot"),
        )


@dataclass
class MapPoint:
    id: str
    layer: str
    name: str
    x: float
    y: float
    region: str | None = None
    floor: str = ""          # 官方分层 id，如 "-1/49"；空=地上
    level: int | None = None
    drops: list[str] = field(default_factory=list)
    note: str = ""
    author: str = ""
    source: str = "official"

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MapPoint":
        return MapPoint(
            id=str(d["id"]),
            layer=str(d.get("layer", "other")),
            name=str(d.get("name") or d["id"]),
            x=_clamp01(d.get("x", 0.5)),
            y=_clamp01(d.get("y", 0.5)),
            region=d.get("region"),
            floor=str(d.get("floor") or ""),
            level=d.get("level") if isinstance(d.get("level"), int) else None,
            drops=[str(s) for s in (d.get("drops") or [])],
            note=str(d.get("note") or ""),
            author=str(d.get("author") or ""),
            source=str(d.get("source") or "official"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "layer": self.layer,
            "name": self.name,
            "x": round(self.x, 5),
            "y": round(self.y, 5),
            "region": self.region,
            "floor": self.floor,
            "level": self.level,
            "drops": self.drops,
            "note": self.note,
            "author": self.author,
            "source": self.source,
        }


@dataclass
class RouteStep:
    seq: int
    point_id: str
    label: str = ""
    note: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RouteStep":
        return RouteStep(
            seq=int(d.get("seq", 0)),
            point_id=str(d.get("point_id", "")),
            label=str(d.get("label") or ""),
            note=str(d.get("note") or ""),
        )


@dataclass
class Route:
    id: str
    name: str
    author: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    est_minutes: int | None = None
    region_ids: list[str] = field(default_factory=list)
    steps: list[RouteStep] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Route":
        steps = [RouteStep.from_dict(s) for s in (d.get("steps") or [])]
        steps.sort(key=lambda s: s.seq)
        return Route(
            id=str(d["id"]),
            name=str(d.get("name") or d["id"]),
            author=str(d.get("author") or ""),
            description=str(d.get("description") or ""),
            tags=[str(t) for t in (d.get("tags") or [])],
            est_minutes=d.get("est_minutes") if isinstance(d.get("est_minutes"), int) else None,
            region_ids=[str(r) for r in (d.get("region_ids") or [])],
            steps=steps,
        )


@dataclass
class MarkerState:
    """单个点位的完成状态（本地 + 云端合并后的视图）。"""

    done: bool = False
    updated_at: str = ""
    origin: str = "local"  # local / remote / private
    note: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MarkerState":
        return MarkerState(
            done=bool(d.get("done", False)),
            updated_at=str(d.get("updated_at") or ""),
            origin=str(d.get("origin") or "local"),
            note=str(d.get("note") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "done": self.done,
            "updated_at": self.updated_at or _now(),
            "origin": self.origin,
            "note": self.note,
        }


@dataclass
class Region:
    id: str
    name: str
    bounds: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)  # x0,y0,x1,y1

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Region":
        b = d.get("bounds") or d.get("box") or d.get("bbox") or {}
        return Region(
            id=str(d.get("id", "")),
            name=str(d.get("name") or ""),
            bounds=(
                _clamp01(b.get("x0", 0.0)),
                _clamp01(b.get("y0", 0.0)),
                _clamp01(b.get("x1", 1.0)),
                _clamp01(b.get("y1", 1.0)),
            ),
        )


@dataclass
class MapDataset:
    """一整套点位数据（含图层定义、区域、免责声明、底图信息）。"""

    points: list[MapPoint] = field(default_factory=list)
    layers: list[Layer] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    map_image: str | None = None
    map_width: int = 0
    map_height: int = 0
    source: str = "sample"
    disclaimer: str = ""

    def layer_map(self) -> dict[str, Layer]:
        """与 ordered_layers() 保持一致：包含内置图层、数据声明的图层，以及点位里出现的未知图层。"""
        return {l.key: l for l in self.ordered_layers()}

    def ordered_layers(self) -> list[Layer]:
        known = {l.key for l in self.layers}
        out = list(self.layers)
        for key in LAYER_FALLBACK_ORDER:
            if key not in known:
                out.append(Layer(key=key, **LAYER_FALLBACK[key]))
        for p in self.points:
            if p.layer not in known and p.layer not in LAYER_FALLBACK:
                out.append(Layer(key=p.layer, label=p.layer, color="#9AA4B2", glyph="dot"))
                known.add(p.layer)
        return out

    @staticmethod
    def from_payload(d: dict[str, Any]) -> "MapDataset":
        mp = d.get("map") or {}
        return MapDataset(
            points=[MapPoint.from_dict(p) for p in (d.get("points") or []) if p.get("id")],
            layers=[Layer.from_dict(l) for l in (d.get("layers") or [])],
            regions=[Region.from_dict(r) for r in (d.get("regions") or [])],
            map_image=mp.get("image"),
            map_width=int(mp.get("width") or 0),
            map_height=int(mp.get("height") or 0),
            source=str(d.get("source") or "unknown"),
            disclaimer=str(d.get("disclaimer") or ""),
        )
