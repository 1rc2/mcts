"""配置读写：默认值 + 原子写入 + 范围钳制。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import paths

DEFAULTS: dict[str, Any] = {
    "ui": {
        "theme": "dark",              # dark / light
        "opacity": 0.96,              # 0.4 ~ 1.0
        "always_on_top": True,
        "lock_size": False,
        "compact": False,             # 紧凑悬浮模式（隐藏侧栏、小窗跟随）
        "panel_visible": True,
        "window_geometry": None,      # [x, y, w, h]
        "minimap_follow": True,       # 紧凑模式下自动跟随当前位置
        "hide_completed": False,      # 一键隐藏全部已完成点位
        "mark_mode": False,           # 单击点位即切换完成状态
        "close_to_tray": True,        # 关闭窗口时最小化到托盘
        "overlay": False,             # 游戏内悬浮指示窗
        "overlay_clickable": False,   # 悬浮窗可点击（关闭时鼠标穿透）
        "overlay_width": 300,         # 悬浮窗尺寸，可在设置里调
        "overlay_height": 300,        # 5 条点位刚好放满，不空一大截
        "overlay_count": 5,           # 列表里显示几条未探索点位
        "map_only": True,             # 默认以官方浏览器地图为主界面
    },
    "vision": {
        "enabled": False,
        "interval_ms": 1500,
        "minimap_rect": [0.008, 0.012, 0.115, 0.115],
        "auto_detect": True,
        "sensitivity": 0.55,
        "show_trail": True,           # 在放大图上画出识别轨迹
    },
    "hotkeys": {
        "toggle_panel": "ctrl+alt+h",
        "mark_done": "ctrl+alt+m",
        "next_waypoint": "ctrl+alt+n",
        "prev_waypoint": "ctrl+alt+b",
        "toggle_vision": "ctrl+alt+v",
        "toggle_overlay": "ctrl+alt+o",
        "overlay_click": "ctrl+alt+k",
        "next_route": "ctrl+alt+r",
        "toggle_topmost": "ctrl+alt+t",
    },
    "layers_visible": {},             # layer key -> bool
    "sync": {
        "conflict": "newer",          # newer / remote
        "auto_pull_on_login": True,
        "auto_push": False,           # 关闭时仅手动「一键回传」
    },
    "data": {
        "points_file": None,          # 覆盖内置示例数据（库街区导出/社区公开数据）
        "map_image": None,            # 覆盖内置底图（建议与点位数据同源）
        "import_dir": None,
    },
    "account": {
        "mode": "local",              # local / oauth
        "uid_masked": "",
        "nickname": "",
    },
}


def _merge(base: dict, over: dict) -> dict:
    if not isinstance(over, dict):
        return dict(base)
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif isinstance(out.get(k), dict) and not isinstance(v, dict):
            # 章节被手改成 null / 列表 / 字符串时保留默认章节：
            # 这类脏值以前会让 _clamp 直接抛异常，而 Config() 在 excepthook 之前构造，
            # 打包版没有控制台，表现就是「双击没反应」。
            continue
        else:
            out[k] = v
    return out


def _as_int(value, default: int, low: int, high: int) -> int:
    try:
        return int(min(high, max(low, int(value))))
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float, low: float, high: float) -> float:
    try:
        return float(min(high, max(low, float(value))))
    except (TypeError, ValueError):
        return default


def _clamp(cfg: dict) -> dict:
    for name in ("ui", "vision", "hotkeys", "layers_visible", "sync", "data", "account"):
        if not isinstance(cfg.get(name), dict):
            cfg[name] = dict(DEFAULTS[name])
    v = cfg["vision"]
    v["enabled"] = bool(v.get("enabled", False))
    v["auto_detect"] = bool(v.get("auto_detect", True))
    v["smooth"] = bool(v.get("smooth", True))
    v["interval_ms"] = _as_int(v.get("interval_ms"), 1500, 500, 10000)
    v["sensitivity"] = _as_float(v.get("sensitivity"), 0.55, 0.0, 1.0)
    r = v.get("minimap_rect")
    try:
        vals = [float(x) for x in list(r)[:4]]
        if len(vals) != 4:
            raise ValueError
        v["minimap_rect"] = vals
    except (TypeError, ValueError):
        v["minimap_rect"] = list(DEFAULTS["vision"]["minimap_rect"])
    u = cfg["ui"]
    u["opacity"] = _as_float(u.get("opacity"), 0.96, 0.4, 1.0)
    u["overlay_width"] = _as_int(u.get("overlay_width"), 300, 220, 560)
    u["overlay_height"] = _as_int(u.get("overlay_height"), 300, 220, 720)
    u["overlay_count"] = _as_int(u.get("overlay_count"), 5, 3, 12)
    u["overlay_clickable"] = bool(u.get("overlay_clickable", False))
    u["theme"] = "light" if u.get("theme") == "light" else "dark"
    return cfg


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or paths.config_path()
        self.data: dict[str, Any] = _merge(DEFAULTS, {})
        self.load()

    def load(self) -> None:
        raw = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                with_defaults = _merge(DEFAULTS, raw)
                self.data = _clamp(with_defaults)
                return
            except Exception:
                # 配置文件损坏/类型完全对不上时备份并回到默认，不阻塞启动
                try:
                    self.path.rename(self.path.with_suffix(".broken"))
                except Exception:
                    pass
                raw = {}
        self.data = _clamp(_merge(DEFAULTS, raw))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # 便捷访问
    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value: Any, save: bool = True) -> None:
        self.data.setdefault(section, {})[key] = value
        _clamp(self.data)
        if save:
            self.save()

    @property
    def ui(self) -> dict:
        return self.data["ui"]

    @property
    def vision(self) -> dict:
        return self.data["vision"]

    @property
    def hotkeys(self) -> dict:
        return self.data["hotkeys"]

    @property
    def sync(self) -> dict:
        return self.data["sync"]

    @property
    def data_cfg(self) -> dict:
        return self.data["data"]

    @property
    def account(self) -> dict:
        return self.data["account"]

    def layer_visible(self, key: str) -> bool:
        return bool(self.data["layers_visible"].get(key, True))

    def set_layer_visible(self, key: str, on: bool) -> None:
        self.data["layers_visible"][key] = bool(on)
        self.save()
