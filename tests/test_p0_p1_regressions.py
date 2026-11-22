"""P0/P1 回归：把上一轮修复固化成可重复执行的检查。

这里专门覆盖「看起来修了、其实没测到」的地方：
识别线程生命周期、脏配置、相对底图路径、云端 provider 签名、非正方形 bbox 距离。
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kumap.core import paths
from kumap.core.config import Config
from kumap.core.guide import meters_between
from kumap.core.provider import ApiConfig, KuroApiProvider, LocalDataProvider

REPO = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------ P0 线程生命周期

# 子进程里跑，是因为崩溃表现为 Qt abort（进程直接死），在 pytest 进程内测不出来。
_RUNNER_SCRIPT = r"""
import os, sys, tempfile, time
from pathlib import Path

import numpy as np
import cv2
from PySide6.QtWidgets import QApplication

app = QApplication([])
from kumap.ui.main_window import VisionRunner

img = Path(tempfile.mkdtemp()) / "m.png"
cv2.imwrite(str(img), np.random.default_rng(0).integers(0, 255, (600, 600, 3), dtype=np.uint8))

runner = VisionRunner(img, {"interval_ms": 500, "auto_detect": True, "smooth": True})
for round_ in range(3):
    runner.start()
    for _ in range(8):
        app.processEvents()
        time.sleep(0.05)
    assert runner.thread is not None, "thread not started"
    assert runner.stop() is True, "stop failed at round %d" % round_
    assert runner.thread is None, "thread ref kept after stop"
# 空闲状态再停一次也必须安全
assert runner.stop() is True
print("RUNNER_LIFECYCLE_OK")
"""


def test_vision_runner_start_stop_does_not_abort():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.run([sys.executable, "-c", _RUNNER_SCRIPT],
                          capture_output=True, text=True, env=env, timeout=180)
    # 旧写法（丢弃仍在运行的 QThread）在这里是 -1073740791 / 0xC0000409
    assert proc.returncode == 0, f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr[-1500:]}"
    assert "RUNNER_LIFECYCLE_OK" in proc.stdout


_EXIT_RUNNING_SCRIPT = _RUNNER_SCRIPT.split("runner = VisionRunner(")[0] + r"""
runner = VisionRunner(img, {"interval_ms": 500, "auto_detect": True, "smooth": True})
runner.start()
for _ in range(8):
    app.processEvents()
    time.sleep(0.05)
assert runner.thread is not None
# 不调用 stop：模拟进程退出时仍持有运行中的线程引用
print("EXIT_WHILE_RUNNING")
"""


def test_vision_runner_exit_while_running_does_not_abort():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.run([sys.executable, "-c", _EXIT_RUNNING_SCRIPT],
                          capture_output=True, text=True, env=env, timeout=180)
    assert proc.returncode == 0, f"exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr[-1500:]}"
    assert "EXIT_WHILE_RUNNING" in proc.stdout


# ------------------------------------------------------------ P1 脏配置

@pytest.mark.parametrize("raw", [
    {"vision": None},
    {"ui": None},
    {"hotkeys": None},
    {"layers_visible": None},
    {"vision": {"interval_ms": "abc"}},
    {"vision": {"sensitivity": None}},
    {"vision": {"minimap_rect": "not-a-list"}},
    {"ui": {"opacity": None}},
    {"ui": {"overlay_count": "many", "overlay_width": None, "overlay_height": [1]}},
    [1, 2, 3],
])
def test_config_survives_dirty_types(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("KUMAPH_DATA_DIR", str(tmp_path))
    p = paths.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(raw), encoding="utf-8")

    cfg = Config(p)  # 旧代码会在这里抛 AttributeError/TypeError（早于 excepthook）

    assert isinstance(cfg.vision, dict) and isinstance(cfg.ui, dict)
    assert isinstance(cfg.hotkeys, dict)
    assert isinstance(cfg.data["layers_visible"], dict)
    assert 500 <= cfg.vision["interval_ms"] <= 10000
    assert 0.4 <= cfg.ui["opacity"] <= 1.0
    assert len(cfg.vision["minimap_rect"]) == 4
    # 浮窗设置也要能容错（新增配置项走同一套降级逻辑）
    assert 220 <= cfg.ui["overlay_width"] <= 560
    assert 220 <= cfg.ui["overlay_height"] <= 720
    assert 3 <= cfg.ui["overlay_count"] <= 12


# ------------------------------------------------------------ P1 相对底图路径

def test_map_image_relative_path_resolves_next_to_points(tmp_path):
    png = tmp_path / "world_map.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "points_x.json").write_text(json.dumps({
        "points": [{"id": "a", "layer": "chest", "x": 0.1, "y": 0.1}],
        "map": {"image": "world_map.png", "width": 100, "height": 100},
    }, ensure_ascii=False), encoding="utf-8")

    prov = LocalDataProvider(imp_dir=tmp_path)
    ds = prov.load_dataset()
    resolved = prov.map_image_path(ds)

    assert resolved == png
    assert "resources" not in resolved.parts  # 旧代码会在这里落到内置占位图


def test_map_image_relative_missing_falls_back_to_bundled(tmp_path):
    (tmp_path / "points_x.json").write_text(json.dumps({
        "points": [{"id": "a", "layer": "chest", "x": 0.1, "y": 0.1}],
        "map": {"image": "missing.png", "width": 100, "height": 100},
    }, ensure_ascii=False), encoding="utf-8")
    prov = LocalDataProvider(imp_dir=tmp_path)
    ds = prov.load_dataset()
    resolved = prov.map_image_path(ds)
    assert resolved is None or resolved.exists()


# ------------------------------------------------------------ P1 provider 签名

def test_kuro_provider_load_routes_accepts_valid_point_ids(tmp_path):
    prov = KuroApiProvider(ApiConfig(), lambda: None,
                           fallback=LocalDataProvider(imp_dir=tmp_path))
    # 旧代码没有这个形参，这里会 TypeError
    routes = prov.load_routes(valid_point_ids={"definitely-missing"})
    assert isinstance(routes, list)


def test_kuro_provider_filters_routes_by_point_ids(tmp_path, monkeypatch):
    payload = {"routes": [
        {"id": "r1", "name": "好路线", "steps": [{"seq": 1, "point_id": "p1"}]},
        {"id": "r2", "name": "坏路线", "steps": [{"seq": 1, "point_id": "p2"}]},
    ]}
    monkeypatch.setattr("kumap.core.provider.http_json", lambda *a, **k: payload)
    api = ApiConfig(routes_url="https://example.invalid/routes.json")
    prov = KuroApiProvider(api, lambda: "token",
                           fallback=LocalDataProvider(imp_dir=tmp_path))

    assert [r.id for r in prov.load_routes(valid_point_ids={"p1"})] == ["r1"]
    assert prov.load_routes(valid_point_ids={"p2"})[0].id == "r2"
    assert prov.load_routes(valid_point_ids={"other"}) == []


# ------------------------------------------------------------ P1 距离换算

def test_meters_between_uses_both_bbox_spans():
    bbox = (0.0, 0.0, 0.5, 0.25)  # 纵横比 2:1 的国家包
    assert meters_between((0.0, 0.0), (0.01, 0.0), bbox) == pytest.approx(0.01 * 0.5 * 25500)
    # 旧实现用 x 跨度算纵向距离，这里会得到 2 倍值
    assert meters_between((0.0, 0.0), (0.0, 0.01), bbox) == pytest.approx(0.01 * 0.25 * 25500)
    diag = meters_between((0.0, 0.0), (0.01, 0.01), bbox)
    assert diag == pytest.approx(math.hypot(0.01 * 0.5 * 25500, 0.01 * 0.25 * 25500))
