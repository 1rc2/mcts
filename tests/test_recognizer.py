# -*- coding: utf-8 -*-
"""recognizer 单元测试：全程用内存合成画面，不依赖真实游戏、不依赖显示器。

运行：
    cd /data/kuro-map-helper && QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_recognizer.py -q
"""

from __future__ import annotations

import math
import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from kumap.core import recognizer as rec  # noqa: E402
from kumap.core.recognizer import (  # noqa: E402
    MapIndex,
    MinimapLocalizer,
    VisionConfig,
    VisionResult,
    VisionWorker,
)

WORLD_W, WORLD_H = 1200, 800  # 合成大地图尺寸
SCREEN_W, SCREEN_H = 1920, 1080  # 合成"屏幕"尺寸
FAIL_REASONS = {"minimap_not_found", "too_few_features", "low_confidence"}
MINI_RECT = VisionConfig().minimap_rect
MINI_D = int(min(MINI_RECT[2] * SCREEN_W, MINI_RECT[3] * SCREEN_H))  # 小地图外接正方形边长


# --------------------------------------------------------------------- 合成素材


def make_world(seed: int = 11, w: int = WORLD_W, h: int = WORLD_H) -> np.ndarray:
    """随机噪声底 + 圆形地标 + 道路线段，保证有充足且唯一的 ORB 特征。"""
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 255, size=(h // 6, w // 6, 3), dtype=np.uint8)
    img = cv2.resize(base, (w, h), interpolation=cv2.INTER_CUBIC)
    noise = rng.integers(0, 60, size=(h, w, 1), dtype=np.uint8)
    img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    for _ in range(70):
        c = (int(rng.integers(20, w - 20)), int(rng.integers(20, h - 20)))
        r = int(rng.integers(6, 22))
        col = tuple(int(v) for v in rng.integers(40, 250, 3))
        cv2.circle(img, c, r, col, 2, cv2.LINE_AA)
        cv2.circle(img, c, max(1, r // 3), col, -1, cv2.LINE_AA)
    for _ in range(30):
        p1 = (int(rng.integers(0, w)), int(rng.integers(0, h)))
        p2 = (int(rng.integers(0, w)), int(rng.integers(0, h)))
        col = tuple(int(v) for v in rng.integers(60, 250, 3))
        cv2.line(img, p1, p2, col, int(rng.integers(1, 4)), cv2.LINE_AA)
    return img


def make_minimap(world, center, heading, view, d):
    """从大地图 center 处取 view x view 区域，按朝向旋转后缩放到 d x d。

    约定：鸣潮小地图随朝向旋转，即世界里方向 heading（0=北，顺时针）在小地图里指向上方。
    """
    wx, wy = float(center[0]), float(center[1])
    s = d / float(view)
    A = np.array([[s, 0.0, -wx * s + d / 2.0], [0.0, s, -wy * s + d / 2.0]], dtype=np.float64)
    th = math.radians(-heading)
    R = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
    c0 = np.array([d / 2.0, d / 2.0])
    M = A.copy()
    M[:, :2] = R @ A[:, :2]
    M[:, 2] = R @ (A[:, 2] - c0) + c0
    return cv2.warpAffine(world, M, (d, d), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0))


def paste_circle(screen, mini, cx, cy, ring=6):
    """把方形小地图裁片贴成带暗色外圈的圆形小地图。"""
    d = mini.shape[0]
    r = d // 2
    yy, xx = np.mgrid[0:d, 0:d]
    mask = ((xx - d / 2.0) ** 2 + (yy - d / 2.0) ** 2) <= (r - 2) ** 2
    x0, y0 = int(cx - r), int(cy - r)
    roi = screen[y0 : y0 + d, x0 : x0 + d]
    roi[mask] = mini[mask]
    screen[y0 : y0 + d, x0 : x0 + d] = roi
    cv2.circle(screen, (int(cx), int(cy)), r + 2, (10, 10, 10), ring, cv2.LINE_AA)


def make_screen(world, center, heading, view, rect=MINI_RECT, pos=None) -> np.ndarray:
    """合成整屏：暗色底 + 一个小地图圆。pos 为 None 时按 minimap_rect 居中放置。"""
    rng = np.random.default_rng(3)
    screen = np.full((SCREEN_H, SCREEN_W, 3), 46, np.uint8)
    screen = np.clip(
        screen.astype(np.int16) + rng.integers(0, 12, size=(SCREEN_H, SCREEN_W, 1)),
        0, 255,
    ).astype(np.uint8)
    if pos is None:
        pos = (rect[0] * SCREEN_W + rect[2] * SCREEN_W / 2.0,
               rect[1] * SCREEN_H + rect[3] * SCREEN_H / 2.0)
    d = int(min(rect[2] * SCREEN_W, rect[3] * SCREEN_H))
    paste_circle(screen, make_minimap(world, center, heading, view, d), pos[0], pos[1])
    return screen


def ang_diff(a, b):
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def pos_err(res, center):
    return max(abs(res.x - center[0] / float(WORLD_W)), abs(res.y - center[1] / float(WORLD_H)))


@pytest.fixture(scope="session")
def world_img():
    return make_world(seed=11)


@pytest.fixture(scope="session")
def world_png(tmp_path_factory, world_img):
    path = tmp_path_factory.mktemp("maps") / "world.png"
    assert cv2.imwrite(str(path), world_img)
    return str(path)


@pytest.fixture(scope="session")
def big_world_img():
    return make_world(seed=21, w=3000, h=3000)


@pytest.fixture(scope="session")
def big_world_png(tmp_path_factory, big_world_img):
    path = tmp_path_factory.mktemp("maps") / "big_world.png"
    assert cv2.imwrite(str(path), big_world_img)
    return str(path)


@pytest.fixture()
def qapp():
    from PySide6.QtCore import QCoreApplication

    return QCoreApplication.instance() or QCoreApplication([])


# --------------------------------------------------------------------- 正向识别


@pytest.mark.parametrize(
    "center,heading,view",
    [
        ((300, 250), 0, 380),
        ((600, 400), 90, 380),
        ((900, 600), 200, 300),
        ((300, 300), 315, 520),
        ((480, 360), 45, 620),
    ],
)
def test_locate_recovers_position_rotation_and_scale(world_png, world_img, center, heading, view):
    cfg = VisionConfig(auto_detect=False)
    loc = MinimapLocalizer(world_png, cfg)
    screen = make_screen(world_img, center, heading, view)

    res = loc.locate(frame_bgr=screen)

    assert res.ok and res.reason == "ok", (res.reason, loc.last_detail)
    assert res.x is not None and res.y is not None
    assert pos_err(res, center) < 0.05
    assert ang_diff(res.rotation, heading) < 6.0, res.rotation
    assert 0.0 < res.confidence <= 0.99
    expect_scale = view / float(MINI_D)  # 比例尺 = 大地图像素 / 小地图像素
    assert abs(res.scale - expect_scale) / expect_scale < 0.25
    assert res.accuracy_note == "识图定位存在误差，仅作探索参考"
    assert res.elapsed_ms > 0 and res.timestamp > 0


def test_large_map_uses_sift_engine(big_world_png, big_world_img):
    """官方 6144px 级底图会自动切到 SIFT：位置/朝向仍要准，且不能误报。"""
    rect = (0.05, 0.08, 220 / SCREEN_W, 220 / SCREEN_H)
    center, heading, view = (1500, 1500), 37, 720
    loc = MinimapLocalizer(big_world_png, VisionConfig(auto_detect=False, minimap_rect=rect))
    screen = make_screen(big_world_img, center, heading, view, rect=rect)

    res = loc.locate(frame_bgr=screen)

    assert loc._sift_index is not None and loc._sift_index.ready
    assert res.ok and res.reason == "ok", (res.reason, loc.last_detail)
    err = max(abs(res.x - center[0] / 3000.0), abs(res.y - center[1] / 3000.0))
    assert err < 0.01, (err, loc.last_detail)
    assert ang_diff(res.rotation, heading) < 8.0, res.rotation


def test_auto_detect_finds_circle_outside_fixed_rect(world_png, world_img):
    """小地图被挪到右上角象限：固定区域里只有背景，只能靠自动检测命中。"""
    loc = MinimapLocalizer(world_png, VisionConfig(auto_detect=True))
    screen = make_screen(world_img, (500, 300), 130, 380, pos=(SCREEN_W - 160, 170))

    res = loc.locate(frame_bgr=screen)

    assert res.ok, (res.reason, loc.last_detail)
    assert pos_err(res, (500, 300)) < 0.05
    assert "auto_detect" in loc.last_detail


def test_auto_detect_falls_back_when_no_circle(world_png, world_img, monkeypatch):
    """自动检测找不到圆形边界时回退固定区域，并在细节里说明（reason 仍是纯原因码）。"""
    monkeypatch.setattr(rec.MinimapLocalizer, "_detect_circle", lambda self, frame: None)
    loc = rec.MinimapLocalizer(world_png, VisionConfig(auto_detect=True))
    screen = make_screen(world_img, (600, 400), 30, 380)

    res = loc.locate(frame_bgr=screen)

    assert res.ok, (res.reason, loc.last_detail)
    assert res.reason == "ok"
    assert "自动检测未找到圆形小地图，已回退固定区域" in loc.last_detail
    assert pos_err(res, (600, 400)) < 0.05


def test_update_world_map_switches_map(tmp_path, world_img):
    other = make_world(seed=77)
    path_a, path_b = tmp_path / "a.png", tmp_path / "b.png"
    assert cv2.imwrite(str(path_a), world_img) and cv2.imwrite(str(path_b), other)

    cfg = VisionConfig(auto_detect=False)
    loc = MinimapLocalizer(str(path_b), cfg)
    screen = make_screen(world_img, (400, 500), 70, 380)
    wrong = loc.locate(frame_bgr=screen)
    assert not wrong.ok and wrong.reason in FAIL_REASONS

    loc.update_world_map(str(path_a))
    right = loc.locate(frame_bgr=screen)
    assert right.ok and right.reason == "ok"
    assert pos_err(right, (400, 500)) < 0.05


def test_set_config_changes_strictness(world_png, world_img):
    loc = MinimapLocalizer(world_png, VisionConfig(auto_detect=False, sensitivity=0.2))
    screen = make_screen(world_img, (600, 400), 90, 380)
    assert loc.locate(frame_bgr=screen).ok

    loc.set_config(VisionConfig(auto_detect=False, min_inliers=4000))
    res = loc.locate(frame_bgr=screen)
    assert not res.ok and res.x is None and res.reason == "too_few_features"


# --------------------------------------------------------------------- 失败路径


@pytest.mark.parametrize("flat", [0, 137])
def test_uniform_minimap_fails(world_png, flat):
    loc = MinimapLocalizer(world_png, VisionConfig(auto_detect=False))
    res = loc.locate(frame_bgr=np.full((SCREEN_H, SCREEN_W, 3), flat, np.uint8))
    assert not res.ok
    assert res.reason in FAIL_REASONS
    assert res.x is None and res.y is None and res.rotation is None and res.scale is None
    assert res.confidence == 0.0
    assert res.accuracy_note == "识图定位存在误差，仅作探索参考"


def test_unrelated_minimap_content_fails(world_png):
    rng = np.random.default_rng(9)
    screen = np.full((SCREEN_H, SCREEN_W, 3), 40, np.uint8)
    paste_circle(screen, rng.integers(0, 255, size=(MINI_D, MINI_D, 3), dtype=np.uint8), 120, 80)
    res = MinimapLocalizer(world_png, VisionConfig(auto_detect=False)).locate(frame_bgr=screen)
    assert not res.ok and res.reason in FAIL_REASONS and res.x is None


def test_low_confidence_when_sensitivity_maximal(world_png, world_img):
    """同一画面：默认档通过，最严格档因内点/置信度不足被拒——sensitivity 确实在起作用。"""
    screen = make_screen(world_img, (600, 400), 90, 380)
    loose = MinimapLocalizer(world_png, VisionConfig(auto_detect=False, min_inliers=20,
                                                    sensitivity=0.55))
    assert loose.locate(frame_bgr=screen).ok

    strict = MinimapLocalizer(world_png, VisionConfig(auto_detect=False, min_inliers=20,
                                                      sensitivity=1.0))
    res = strict.locate(frame_bgr=screen)
    assert not res.ok and res.x is None and res.y is None
    assert res.reason in {"low_confidence", "too_few_features"}
    assert res.confidence == 0.0
    assert ("conf=" in strict.last_detail) or ("内点不足" in strict.last_detail)


def test_sensitivity_is_monotonic(world_png, world_img):
    """同一画面提高 sensitivity：门槛线性上升，先放行后拒绝，中间不回头。"""
    screen = make_screen(world_img, (600, 400), 90, 380)
    passed = []
    for s in (0.0, 0.2, 0.4, 0.55, 0.7, 0.85, 1.0):
        loc = MinimapLocalizer(world_png, VisionConfig(auto_detect=False, min_inliers=20,
                                                       sensitivity=s))
        res = loc.locate(frame_bgr=screen)
        passed.append(bool(res.ok))
        if res.ok:
            assert "conf=" in loc.last_detail  # 细节里可核对真实置信度
    assert passed[0] is True
    assert passed[-1] is False
    assert passed == sorted(passed, reverse=True)  # 只可能从通过变成拒绝


def test_missing_map_path(tmp_path, world_img):
    missing = str(tmp_path / "not_exist.png")
    loc = MinimapLocalizer(missing, VisionConfig(auto_detect=False))
    res = loc.locate(frame_bgr=make_screen(world_img, (600, 400), 0, 380))
    assert res.reason == "map_index_missing"
    assert not res.ok and res.x is None and res.y is None
    assert loc.index.ready is False
    assert "map_index_missing" in loc.index.error

    idx = MapIndex(missing)
    idx.build()
    assert idx.ready is False and idx.error


def test_bad_frame_input(world_png):
    loc = MinimapLocalizer(world_png, VisionConfig(auto_detect=False))
    assert loc.locate(frame_bgr=np.zeros((8, 8, 3), np.uint8)).reason == "capture_failed"


def test_capture_failed_without_display(world_png, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("no display")

    monkeypatch.setattr(rec, "mss", types.SimpleNamespace(MSS=boom))
    loc = MinimapLocalizer(world_png, VisionConfig())
    res = loc.locate()
    assert res.reason == "capture_failed"
    assert not res.ok and res.x is None
    assert "no display" in loc.last_detail  # 细节只写 last_detail，不污染 reason 码


def test_capture_success_path_uses_grabbed_frame(world_png, world_img, monkeypatch):
    """伪造 mss 截屏，验证截屏分支（BGRA->BGR、monitor 选取）也能走通。"""
    screen = make_screen(world_img, (600, 400), 90, 380)
    rgba = np.dstack([screen[:, :, ::-1], np.full(screen.shape[:2], 255, np.uint8)])

    class Shot:
        rgb = rgba.tobytes()
        width, height = SCREEN_W, SCREEN_H

    class Grabber:
        monitors = [
            {"left": 0, "top": 0, "width": SCREEN_W, "height": SCREEN_H},
            {"left": 0, "top": 0, "width": SCREEN_W, "height": SCREEN_H},
        ]

        def grab(self, monitor):
            return Shot()

    monkeypatch.setattr(rec, "mss", types.SimpleNamespace(MSS=lambda *a, **k: Grabber()))
    loc = MinimapLocalizer(world_png, VisionConfig(auto_detect=False))
    res = loc.locate()
    assert res.ok, (res.reason, loc.last_detail)
    assert pos_err(res, (600, 400)) < 0.05
    assert "captured" in loc.last_detail


def test_dependency_missing_is_not_fatal(world_png, monkeypatch):
    monkeypatch.setattr(rec, "cv2", None)
    loc = MinimapLocalizer(world_png, VisionConfig(auto_detect=False))
    res = loc.locate(frame_bgr=np.zeros((100, 100, 3), np.uint8))
    assert res.reason == "dependency_missing" and not res.ok and res.x is None
    assert loc.available is False
    assert loc.error.startswith("dependency_missing: cv2")

    monkeypatch.setattr(rec, "cv2", cv2)
    monkeypatch.setattr(rec, "mss", None)
    loc2 = MinimapLocalizer(world_png)
    assert loc2.available is False and loc2.error == "dependency_missing: mss"
    assert loc2.locate().reason == "dependency_missing"  # 缺 mss 时截屏链路明确失败
    # 传入画面时不依赖 mss，识别照常工作
    ok = MinimapLocalizer(world_png, VisionConfig(auto_detect=False)).locate(
        frame_bgr=make_screen(cv2.imread(world_png), (600, 400), 90, 380)
    )
    assert ok.reason == "ok", ok.reason


# --------------------------------------------------------------------- 数据结构


def test_vision_config_defaults_and_clamps():
    cfg = VisionConfig()
    assert (cfg.enabled, cfg.interval_ms, cfg.auto_detect) == (False, 1500, True)
    assert cfg.minimap_rect == (0.008, 0.012, 0.115, 0.115)
    assert cfg.sensitivity == 0.55 and cfg.min_inliers == 8
    assert cfg.monitor_index == rec.SCREEN_PRIMARY == 0

    assert VisionConfig(interval_ms=1).interval_ms == 500  # clamp 下限
    assert VisionConfig(interval_ms=500).interval_ms == 500
    assert VisionConfig(interval_ms=999999).interval_ms == 10000  # clamp 上限
    assert VisionConfig(interval_ms=2000).interval_ms == 2000
    assert VisionConfig(sensitivity=3.0).sensitivity == 1.0
    assert VisionConfig(sensitivity=-1.0).sensitivity == 0.0
    assert VisionConfig(min_inliers=0).min_inliers >= 3
    assert VisionConfig(monitor_index=-5).monitor_index == 0

    rect = VisionConfig(minimap_rect=(0.9, 0.9, 0.5, 0.5)).minimap_rect
    assert rect[2] <= 1.0 and rect[0] + rect[2] <= 1.0 and rect[1] + rect[3] <= 1.0
    assert VisionConfig(minimap_rect="bad").minimap_rect == (0.008, 0.012, 0.115, 0.115)
    assert VisionConfig(minimap_rect=[0.5, 0.5, 0.001, 0.001]).minimap_rect[2] >= 0.02


def test_vision_result_to_dict():
    res = VisionResult(ok=True, x=0.25, y=0.5, rotation=90.0, confidence=0.8,
                       scale=3.2, reason="ok")
    d = res.to_dict()
    assert isinstance(d, dict)
    assert set(d) == {"ok", "x", "y", "rotation", "confidence", "scale", "reason",
                      "accuracy_note", "timestamp", "elapsed_ms"}
    assert d["x"] == 0.25 and d["reason"] == "ok"
    assert d["accuracy_note"] == "识图定位存在误差，仅作探索参考"
    failed = VisionResult(False, None, None, None, 0.0, None, "low_confidence").to_dict()
    assert failed["x"] is None and failed["reason"] == "low_confidence"


# --------------------------------------------------------------------- Worker


def test_worker_locate_once_emits_dict(world_png, qapp):
    worker = VisionWorker(world_png, VisionConfig(auto_detect=False))
    got = []
    worker.result_ready.connect(lambda payload: got.append(payload))

    payload = worker.locate_once()  # 沙箱无显示器：截屏失败也要给出结构化结果

    assert isinstance(payload, dict) and payload["accuracy_note"]
    assert payload["reason"] in {"capture_failed", "map_index_missing", "minimap_not_found",
                                 "too_few_features", "low_confidence"}
    assert payload["ok"] is False and payload["x"] is None
    assert got == [payload]


def test_worker_throttles_after_three_failures(world_png, qapp, monkeypatch):
    cfg = VisionConfig(auto_detect=False, enabled=True, interval_ms=800)
    worker = VisionWorker(world_png, cfg)
    calls = []

    def fake_locate(frame_bgr=None):
        calls.append(1)
        return VisionResult(False, None, None, None, 0.0, None, "low_confidence")

    monkeypatch.setattr(worker._localizer, "locate", fake_locate)
    seen = []
    worker.result_ready.connect(lambda payload: seen.append(payload))

    for _ in range(2):
        worker._tick()
        assert worker.degraded is False
        assert worker.effective_interval_ms == 800
    worker._tick()  # 第 3 次连续失败
    assert worker.degraded is True
    assert worker.effective_interval_ms == max(800, 3000)
    assert len(calls) == 3

    worker._tick()  # 降频期间仍继续识别（只是间隔变长），否则画面恢复后无法自愈
    assert len(calls) == 4
    assert seen[-1]["reason"] == "low_confidence"
    assert worker.degraded is True
    assert worker.effective_interval_ms == max(800, 3000)

    monkeypatch.setattr(
        worker._localizer, "locate",
        lambda frame_bgr=None: VisionResult(True, 0.5, 0.5, 10.0, 0.9, 3.0, "ok"),
    )
    res = worker._tick()
    assert worker.degraded is False and worker.effective_interval_ms == 800
    assert res.ok


def test_worker_pause_and_config(world_png, qapp, monkeypatch):
    worker = VisionWorker(world_png, VisionConfig(auto_detect=False, enabled=True, interval_ms=1000))
    monkeypatch.setattr(
        worker._localizer, "locate",
        lambda frame_bgr=None: VisionResult(True, 0.1, 0.2, 5.0, 0.7, 2.0, "ok"),
    )
    worker.set_config(VisionConfig(auto_detect=False, enabled=True, interval_ms=300))
    assert worker.config.interval_ms == 500  # 钳制下限
    assert worker.effective_interval_ms == 500

    emitted = []
    worker.result_ready.connect(lambda payload: emitted.append(payload))
    worker.set_paused(True)
    assert worker.paused is True
    worker._tick()
    assert emitted == []  # 暂停期间不识别
    worker.set_paused(False)
    worker._tick()
    assert len(emitted) == 1
    assert worker.locate_once()["ok"] is True  # 手动单次识别忽略暂停


def test_worker_run_loop_and_stop(world_png, qapp, monkeypatch):
    worker = VisionWorker(world_png, VisionConfig(auto_detect=False, enabled=True))
    monkeypatch.setattr(
        worker._localizer, "locate",
        lambda frame_bgr=None: VisionResult(False, None, None, None, 0.0, None, "capture_failed"),
    )
    started, stopped = [], []
    worker.started_loop.connect(lambda: started.append(1))
    worker.stopped.connect(lambda: stopped.append(1))

    worker.run_loop()
    assert started == [1]
    assert worker._timer is not None and worker._timer.isActive()
    assert worker._timer.interval() == worker.effective_interval_ms == 1500

    worker.stop()
    assert worker._timer is None  # 定时器在 worker 线程内就地释放，不留跨线程析构隐患
    assert stopped == [1]


def test_worker_in_qthread(world_png, qapp):
    """周期识别跑在独立 QThread 里：不阻塞调用线程，且能持续产出结果。"""
    from PySide6.QtCore import QThread

    thread = QThread()
    worker = VisionWorker(world_png, VisionConfig(auto_detect=False, enabled=True, interval_ms=500))
    worker.moveToThread(thread)
    payloads = []
    worker.result_ready.connect(lambda p: payloads.append(p))
    thread.started.connect(worker.run_loop)
    thread.start()
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and len(payloads) < 2:
        qapp.processEvents()
        time.sleep(0.02)
    worker.stop()
    assert thread.wait(5000)
    qapp.processEvents()

    assert thread.isFinished()
    assert len(payloads) >= 2
    for p in payloads:
        assert {"ok", "reason", "x", "y", "accuracy_note"} <= set(p)
    assert payloads[0]["reason"] in {"capture_failed", "too_few_features", "low_confidence",
                                     "minimap_not_found", "map_index_missing"}


def test_worker_control_from_other_thread_is_safe(world_png, qapp):
    """GUI 线程去改/停一个活在 QThread 里的 worker：不得跨线程碰 QTimer。

    这是真实使用路径（关窗口、切设置、点「识别一次」都发生在 GUI 线程）。
    曾经的缺陷：直接调用导致 Qt 打印 "Timers cannot be stopped from another
    thread"，并在对象析构时段错误。
    """
    from PySide6.QtCore import QThread

    thread = QThread()
    worker = VisionWorker(world_png, VisionConfig(auto_detect=False, enabled=True, interval_ms=300))
    worker.moveToThread(thread)
    payloads = []
    worker.result_ready.connect(lambda p: payloads.append(p))
    thread.started.connect(worker.run_loop)
    thread.start()

    def wait_until(predicate, seconds=8.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not predicate():
            qapp.processEvents()
            time.sleep(0.02)
        return predicate()

    assert wait_until(lambda: len(payloads) >= 1)

    worker.set_config(VisionConfig(auto_detect=False, enabled=True, interval_ms=600))
    assert worker.locate_once() is None  # 跨线程只能排队，结果走 result_ready
    assert wait_until(lambda: len(payloads) >= 3)
    assert worker.config.interval_ms == 600  # 排队后的 set_config 已在 worker 线程生效

    worker.stop()
    assert thread.wait(5000)
    qapp.processEvents()
    assert thread.isFinished()
    assert worker._timer is None
    del worker
    qapp.processEvents()


# --------------------------------------------------------------------- 索引与并发


def test_map_index_build_is_reusable(world_png, world_img):
    idx = MapIndex(world_png)
    assert idx.ready is False
    idx.build()
    assert idx.ready is True and idx.error == ""
    assert idx.size == (WORLD_W, WORLD_H)
    levels = idx.levels
    assert 2 <= len(levels) <= 3  # 2~3 级降采样
    assert all(levels[i]["eff"] < levels[i + 1]["eff"] for i in range(len(levels) - 1))
    for level in levels:
        assert level["des"].dtype == np.uint8 and level["des"].shape[1] == 32  # ORB 描述子
        assert level["pts"].shape[0] == level["des"].shape[0] >= 8
        assert level["w"] <= WORLD_W and level["h"] <= WORLD_H

    # 同一份索引可被多次识别复用（不重复检测底图特征）
    loc = MinimapLocalizer(world_png, VisionConfig(auto_detect=False))
    loc.update_world_map(world_png)
    built = loc.index.levels[0]["des"]
    for i in range(2):
        assert loc.locate(frame_bgr=make_screen(world_img, (600, 400), 90, 380)).ok
        assert loc.index.levels[0]["des"] is built  # 描述子数组未被重建


def test_concurrent_locate_and_rebuild(world_png, world_img):
    """索引重建与识别并发时不应崩溃或返回脏结果。"""
    cfg = VisionConfig(auto_detect=False)
    loc = MinimapLocalizer(world_png, cfg)
    screens = [make_screen(world_img, (300 + 80 * i, 250 + 60 * i), 30 * i, 380) for i in range(4)]
    results, errors = [], []

    def recognize():
        try:
            for s in screens:
                results.append(loc.locate(frame_bgr=s))
        except Exception as exc:
            errors.append(exc)

    def rebuild():
        try:
            for _ in range(3):
                loc.update_world_map(world_png)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=recognize), threading.Thread(target=rebuild)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    assert not any(t.is_alive() for t in threads)
    assert len(results) == len(screens)
    for res in results:
        assert res.ok or res.reason in FAIL_REASONS | {"map_index_missing"}
