"""运行期路径解析：程序资源目录 vs 用户数据目录。

用户数据（配置、令牌、本地标记）一律写在用户目录，保证程序目录只读、
便携模式（把数据目录搬进程序旁的 data/ 文件夹）也可用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ENV_DATA_DIR = "KUMAPH_DATA_DIR"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """打包资源根目录（地图底图、示例数据）。"""
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        cand = base / "kumap" / "resources"
        if cand.exists():
            return cand
        return base / "resources"
    return Path(__file__).resolve().parent.parent / "resources"


def data_root() -> Path:
    """可写数据目录。"""
    override = os.environ.get(_ENV_DATA_DIR)
    if override:
        base = Path(override)
    elif is_frozen():
        portable = Path(sys.executable).parent / "data"
        if (portable / ".portable").exists():
            base = portable
        else:
            base = _appdata() / "KuroMapHelper"
    else:
        base = _appdata() / "KuroMapHelper"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _appdata() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata)
    return Path.home() / ".config"


def ensure_sub(name: str) -> Path:
    p = data_root() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


# 约定好的具体路径
def config_path() -> Path:
    return data_root() / "config.json"


def markers_path() -> Path:
    return data_root() / "markers.json"


def private_points_path() -> Path:
    return data_root() / "private_points.json"


def favorites_path() -> Path:
    return data_root() / "favorites.json"


def sync_queue_path() -> Path:
    return data_root() / "sync_queue.json"


def secure_store_path() -> Path:
    return data_root() / "credentials.bin"


def import_dir() -> Path:
    """用户放入的库街区导出 / 社区公开数据文件（JSON）。"""
    return ensure_sub("import")


def api_config_path() -> Path:
    """可选：库街区官方开放接口配置（授权端点等）。"""
    return data_root() / "kuro_api.json"


def log_path() -> Path:
    return data_root() / "app.log"
