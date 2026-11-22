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
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _clamp(cfg: dict) -> dict:
    v = cfg["vision"]
    v["interval_ms"] = int(min(10000, max(500, int(v.get("interval_ms", 1500)))))
    v["sensitivity"] = float(min(1.0, max(0.0, float(v.get("sensitivity", 0.55)))))
    r = v.get("minimap_rect") or [0.008, 0.012, 0.115, 0.115]
    v["minimap_rect"] = [float(x) for x in r][:4]
    u = cfg["ui"]
    u["opacity"] = float(min(1.0, max(0.4, float(u.get("opacity", 0.96)))))
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
            except Exception:
                # 配置文件损坏时备份并回到默认，不阻塞启动
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
