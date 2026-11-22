# -*- coding: utf-8 -*-
"""本地屏幕识图定位引擎（纯像素方案）。

合规约束（硬性，本模块任何代码都不得越过）：
    1. 只读取屏幕画面像素：使用 mss 截屏 + OpenCV 特征匹配估算位置。
    2. 不读取游戏进程内存：不调用 ReadProcessMemory / VirtualAllocEx / OpenProcess，
       不持有任何进程句柄，不做 DLL 注入。
    3. 不 Hook、不拦截游戏消息：不调用 SetWindowsHookEx / SetWinEventHook 等。
    4. 不模拟任何键盘鼠标输入：不依赖 pyautogui / pynput / SendInput / mouse / keyboard，
       不自动点击、不自动寻路、不自动战斗。
    5. 本模块不含任何 UI，也不实现失败回退策略：识别失败只返回原因码，
       由上层界面决定是否需要切换「手动点击大图定位」。

算法规格摘要：
    截屏 -> 小地图区域（固定归一化矩形 或 圆形自动检测）-> 灰度 / 去黑圈边框 / CLAHE
    -> ORB 特征 -> 与大地图多尺度索引（预生成的降采样层，缓存复用）做
      BFMatcher(Hamming) knn + Lowe ratio test + estimateAffinePartial2D(RANSAC)
    -> 由仿射阵解出大地图归一化坐标 (x, y)、朝向角、比例尺与内点置信度。

运行策略（VisionWorker）：
    连续失败 >= DEGRADED_FAIL_STREAK 次后「降频不降智」：只把定时器间隔拉长到
    max(interval_ms, DEGRADED_INTERVAL_MS)（省电/省 CPU），每一轮仍然真识别一次，
    失败期间上报的 reason 保持最后一次失败原因；一旦成功，立即解除降频并自动
    恢复原来的 interval_ms。之所以不在降频期间干脆不识别：过场动画 / 黑屏 /
    loading 造成连续失败是常态，若停止重试位置会永久冻结且无法自愈。

已知精度局限（上层写免责声明时可直接引用）：
    - 索引把大地图最长边压到 INDEX_MAX_DIM 像素后再提特征，定位分辨率受该层限制：
      内容丰富的常规地图，误差量级约为大地图边长的 0.1%~0.5%（合成用例 0.08%~0.34%）。
    - 只匹配「降采样后的底图」，本质是探索参考级定位，不是导航级坐标；不能用来判定
      精确格子、传送点命中或碰撞体积。
    - 需要小地图视野与大地图内容在同一尺度可对齐：视野过大/过小使重采样倍率落到
      MINI_SCALES_LIMIT 之外，或底图在当前尺度特征稀疏时，会以 too_few_features 失败
      （常见于超大地图的极远视野、纯色/低纹理区域、小地图被 HUD 大面积遮挡）。
    - 小地图上的玩家箭头、任务标记、怪物/队友头像等会随帧移动的图层是干扰源，
      单帧结果会有轻微抖动；本模块不做跨帧滤波（无历史、无平滑），抖动由上层决定是否平滑。
    - 圆形自动检测只保证「找到圆」，半径估计会带来若干像素偏差，位置误差通常比
      固定区域略大；建议用户校准 minimap_rect。
    - 朝向角来自相似变换的旋转分量，本身不含「地图北偏」概念，取到的是小地图旋转角。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, replace
from typing import Optional, Sequence

try:  # 可选依赖：缺失时降级为 dependency_missing，不抛异常
    import numpy as np
except Exception:  # pragma: no cover - 取决于运行环境
    np = None  # type: ignore[assignment]

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

try:
    import mss
except Exception:  # pragma: no cover
    mss = None  # type: ignore[assignment]

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Qt

logger = logging.getLogger("kumap.recognizer")

SCREEN_PRIMARY = 0

# 原因码：调用方按 reason 分支，因此 reason 只放纯代码，细节写日志与 last_detail
REASON_OK = "ok"
REASON_DEPENDENCY = "dependency_missing"
REASON_CAPTURE_FAILED = "capture_failed"
REASON_MINIMAP_NOT_FOUND = "minimap_not_found"
REASON_TOO_FEW_FEATURES = "too_few_features"
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_MAP_INDEX_MISSING = "map_index_missing"

INTERVAL_MIN_MS = 500
INTERVAL_MAX_MS = 10000
DEGRADED_INTERVAL_MS = 3000  # 连续失败后的降频下限（省电/省资源）
DEGRADED_FAIL_STREAK = 3

ACCURACY_NOTE = "识图定位存在误差，仅作探索参考"

# 索引规模与算力上限：大地图先按参考尺寸整体降采样，再生成若干级特征层
INDEX_MAX_DIM = 1400  # 参考层最长边像素上限
LEVEL_FACTORS = (0.25, 0.5, 1.0)  # 相对参考层的降采样比例（由粗到细）
WORLD_ORB_FEATURES = 2200
MINI_ORB_FEATURES = 420
MINI_MAX_DIM = 360  # 小地图裁片预处理后的边长上限
# 尺度搜索只放大不缩小：缩小会毁掉 FAST 角点。对齐条件 r ≈ eff * scale
MINI_SCALE_CANDIDATES = (1.00, 1.60, 2.40, 3.40)
MINI_SCALES_LIMIT = (0.55, 4.00)  # 小地图重采样倍率安全边界
# 大尺寸官方底图（6144px 级）改用 SIFT：ORB 在该尺度下特征对齐不稳定，
# SIFT 自带尺度/旋转不变性，实测合成小地图定位误差约 0.2‰、无误报。
SIFT_ENGINE_MIN_DIM = 1800   # 底图最长边 >= 该值自动启用 SIFT（含 2k~3k 子地图）
SIFT_MAX_DIM = 2560          # SIFT 索引的底图最长边（兼顾特征量与内存）
SIFT_CONTRAST = 0.01
SIFT_EDGE = 12
SIFT_RATIO = 0.80            # Lowe ratio test
SIFT_MIN_INLIERS = 10        # SIFT 内点下限（低于此值宁可失败也不报错位置）
REFINE_STEPS = 2  # 最细层 tile 细化次数
SANITY_MATCHES = 5  # 单次匹配的最小可用内点数（真正的严格程度由 min_inliers / sensitivity 决定）
DETECT_REGION_MAX_DIM = 320  # 自动检测只在缩小的候选区上跑，控制 CPU
PRECISE_TIMER = Qt.TimerType.PreciseTimer

# 置信度由三部分构成，任何一项打满都不算「稳」，这样 sensitivity 才有实质约束力
CONF_INLIER_REF = 80.0  # 内点绝对数参考值（与 min_inliers 无关，防止调低门槛就把分数刷满）
CONF_COVERAGE_REF = 6.0  # 内点在小地图 3x3 网格上覆盖的格子数参考值
CONF_WEIGHTS = (0.40, 0.30, 0.30)  # (内点比例, 内点数量, 空间覆盖) 权重
CONF_RATIO_REF = 0.6  # 内点/全匹配比例达到该值视为干净
CONF_MIN = 0.35  # sensitivity=0 时的置信度门槛
CONF_SPAN = 0.55  # sensitivity=1 时门槛抬到 CONF_MIN + CONF_SPAN = 0.90


def _coverage(pts, size) -> int:
    """内点落在小地图 3x3 网格中的格子数（0~9）。

    只在某一小块上碰巧对齐也会得到不少内点，但空间覆盖很低，因此用它惩罚「局部假对齐」。
    """
    w, h = float(size[0]), float(size[1])
    if pts is None or len(pts) == 0 or w <= 0.0 or h <= 0.0:
        return 0
    gx = np.clip((pts[:, 0] / w * 3.0).astype(np.int32), 0, 2)
    gy = np.clip((pts[:, 1] / h * 3.0).astype(np.int32), 0, 2)
    return int(len(np.unique(gy * 3 + gx)))


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


def _quality(hyp: dict) -> float:
    """假设排序分数：内点数与置信度的乘积，兼顾"匹配多"和"匹配干净"。"""
    return float(hyp["inliers"]) * float(hyp["conf"])


@dataclass
class VisionConfig:
    """识图参数。__post_init__ 统一做范围钳制，避免脏配置流入引擎。"""

    enabled: bool = False
    interval_ms: int = 1500  # 识别频率，允许 500~10000
    minimap_rect: tuple = (0.008, 0.012, 0.115, 0.115)  # 归一化 (x, y, w, h)，相对主屏
    auto_detect: bool = True
    sensitivity: float = 0.55  # 0~1，越高越严格
    min_inliers: int = 8
    monitor_index: int = SCREEN_PRIMARY

    def __post_init__(self) -> None:
        self.enabled = bool(self.enabled)
        self.auto_detect = bool(self.auto_detect)
        self.interval_ms = int(_clamp(float(self.interval_ms), INTERVAL_MIN_MS, INTERVAL_MAX_MS))
        self.sensitivity = float(_clamp(self.sensitivity, 0.0, 1.0))
        self.min_inliers = int(max(3, int(self.min_inliers)))
        self.monitor_index = int(max(0, int(self.monitor_index)))
        self.minimap_rect = _normalize_rect(self.minimap_rect)

    def cloned(self) -> "VisionConfig":
        """返回钳制过的副本，避免外部后续修改影响引擎。"""
        return replace(self)


def _normalize_rect(rect: Sequence[float]) -> tuple:
    try:
        x, y, w, h = (float(v) for v in list(rect)[:4])
    except Exception:
        return (0.008, 0.012, 0.115, 0.115)
    w = float(_clamp(w, 0.02, 1.0))
    h = float(_clamp(h, 0.02, 1.0))
    x = float(_clamp(x, 0.0, max(0.0, 1.0 - w)))
    y = float(_clamp(y, 0.0, max(0.0, 1.0 - h)))
    return (x, y, w, h)


@dataclass
class VisionResult:
    """单次识别结果。x/y 为归一化大地图坐标，rotation 为朝向角（0=正北）。"""

    ok: bool
    x: Optional[float]
    y: Optional[float]
    rotation: Optional[float]  # 度，0=正北，可为 None
    confidence: float  # 0~1
    scale: Optional[float]  # 大地图原始像素 / 小地图像素
    reason: str  # 成功时为 "ok"
    accuracy_note: str = ACCURACY_NOTE
    timestamp: float = 0.0
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


# --------------------------------------------------------------------------- 索引


def _imread_unicode(path: str):
    """兼容中文/空格路径的图片读取（cv2.imread 在 Windows 上对非 ASCII 路径不可靠）。"""
    if np is None or cv2 is None or not path:
        return None
    try:
        buf = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if buf is None or buf.size == 0:
        return None
    try:
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        return None


class MapIndex:
    """大地图多尺度 ORB 索引：载入 + 预生成降采样层，可复用、可重建。"""

    def __init__(self, world_map_path: str):
        self.path = str(world_map_path or "")
        self._lock = threading.RLock()
        self._levels: list[dict] = []
        self._error = ""
        self._size = (0, 0)  # 原始 (w, h)

    # 状态
    @property
    def ready(self) -> bool:
        return bool(self._levels)

    @property
    def error(self) -> str:
        return self._error

    @property
    def size(self) -> tuple:
        return self._size

    @property
    def levels(self) -> list:
        """由粗到细的特征层（只读快照，重建后旧引用仍自洽）。"""
        with self._lock:
            return list(self._levels)

    def build(self) -> None:
        """载入底图并重建各降采样层的 ORB 特征，失败时清空并保持 ready=False。"""
        with self._lock:
            self._levels = []
            self._error = ""
            if cv2 is None or np is None:
                self._error = "dependency_missing: cv2/numpy"
                return
            if not self.path:
                self._error = "map_index_missing: 未设置底图路径"
                return
            img = _imread_unicode(self.path)
            if img is None or getattr(img, "ndim", 0) != 3:
                self._error = "map_index_missing: 底图无法读取 %s" % self.path
                return
            h, w = img.shape[:2]
            if w < 64 or h < 64:
                self._error = "map_index_missing: 底图尺寸过小 %dx%d" % (w, h)
                return
            self._size = (int(w), int(h))
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            ref = float(_clamp(float(INDEX_MAX_DIM) / float(max(w, h)), 0.02, 1.0))
            orb = cv2.ORB_create(nfeatures=WORLD_ORB_FEATURES)
            levels: list[dict] = []
            for factor in LEVEL_FACTORS:
                eff = ref * factor  # 该层像素 / 原始像素
                lw = max(32, int(round(w * eff)))
                lh = max(32, int(round(h * eff)))
                if factor >= 1.0 and (lw, lh) == (w, h):
                    layer_gray = gray
                else:
                    layer_gray = cv2.resize(gray, (lw, lh), interpolation=cv2.INTER_AREA)
                kps, des = orb.detectAndCompute(layer_gray, None)
                if des is None or kps is None or len(kps) < 8:
                    logger.debug("skip level eff=%.4f: no features", eff)
                    continue
                pts = np.float32([[kp.pt[0], kp.pt[1]] for kp in kps]).reshape(-1, 2)
                levels.append(
                    {
                        "eff": float(eff),
                        "factor": float(factor),
                        "w": int(lw),
                        "h": int(lh),
                        "pts": pts,
                        "des": np.ascontiguousarray(des),
                    }
                )
            if not levels:
                self._error = "map_index_missing: 底图特征不足"
                return
            levels.sort(key=lambda d: d["eff"])  # 由粗到细
            self._levels = levels
            logger.debug("index built: %dx%d levels=%s", w, h, [d["eff"] for d in levels])


class SiftMapIndex:
    """大尺寸底图的 SIFT 索引（尺度/旋转不变），按需构建一次后复用。

    官方 6144px 底图 + 小地图视野只有约 700px 时，ORB 的固定尺度候选很难对齐；
    SIFT 能直接从匹配点估出相似变换，实测更稳。仅在底图最长边 >= SIFT_ENGINE_MIN_DIM
    时启用，小图仍走原有 ORB 路径。
    """

    def __init__(self, world_map_path: str):
        self.path = str(world_map_path or "")
        self._error = ""
        self._size = (0, 0)          # 索引层尺寸（缩放后）
        self._orig = (0, 0)          # 原图尺寸
        self._sift = None
        self._pts = None
        self._des = None

    @property
    def ready(self) -> bool:
        return self._des is not None and len(self._pts) >= 16

    @property
    def error(self) -> str:
        return self._error

    @property
    def size(self) -> tuple:
        return self._size

    @property
    def original_size(self) -> tuple:
        return self._orig

    @property
    def descriptors(self):
        return self._des

    @property
    def points(self):
        return self._pts

    @property
    def detector(self):
        return self._sift

    def build(self) -> None:
        self._error = ""
        self._sift = self._pts = self._des = None
        if cv2 is None or np is None:
            self._error = "dependency_missing: cv2/numpy"
            return
        if not hasattr(cv2, "SIFT_create"):
            self._error = "dependency_missing: cv2.SIFT_create"
            return
        img = _imread_unicode(self.path)
        if img is None or getattr(img, "ndim", 0) != 3:
            self._error = "map_index_missing: 底图无法读取 %s" % self.path
            return
        h, w = img.shape[:2]
        self._orig = (int(w), int(h))
        scale = min(1.0, float(SIFT_MAX_DIM) / float(max(w, h)))
        if scale < 0.999:
            img = cv2.resize(img, (max(64, int(round(w * scale))), max(64, int(round(h * scale)))),
                             interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        try:
            sift = cv2.SIFT_create(nfeatures=0, contrastThreshold=SIFT_CONTRAST,
                                   edgeThreshold=SIFT_EDGE)
            kps, des = sift.detectAndCompute(gray, None)
        except Exception as exc:  # pragma: no cover - 取决于 OpenCV 构建
            self._error = "map_index_missing: SIFT 不可用（%s）" % exc
            return
        if des is None or kps is None or len(kps) < 32:
            self._error = "map_index_missing: 底图 SIFT 特征不足"
            return
        self._size = (int(gray.shape[1]), int(gray.shape[0]))
        self._sift = sift
        self._pts = np.float32([[kp.pt[0], kp.pt[1]] for kp in kps]).reshape(-1, 2)
        self._des = np.ascontiguousarray(des)
        logger.debug("sift index built: %dx%d kp=%d", w, h, len(kps))


# --------------------------------------------------------------------------- 定位


class MinimapLocalizer:
    """小地图 -> 大地图 的像素定位引擎（无 UI、无回退逻辑）。"""

    def __init__(self, world_map_path: str, config: Optional[VisionConfig] = None):
        self._cfg = (config or VisionConfig()).cloned()
        self._world_path = str(world_map_path or "")
        self._index = MapIndex(world_map_path)
        self._sift_index: Optional[SiftMapIndex] = None
        self._lock = threading.RLock()  # 保护索引重建与识别
        self._capture_lock = threading.Lock()
        self._grabber = None
        self._matcher = None
        self._sift_matcher = None
        self._orb_mini = None
        self._last_detail = ""
        self._sync_engine()

    # -- 对外只读信息
    @property
    def available(self) -> bool:
        """依赖是否齐全（截屏链路需要 numpy + cv2 + mss）。"""
        return not (np is None or cv2 is None or mss is None)

    @property
    def error(self) -> str:
        """不可用原因，例如 "dependency_missing: cv2"；否则返回底图载入错误。"""
        if np is None:
            return "dependency_missing: numpy"
        if cv2 is None:
            return "dependency_missing: cv2"
        if mss is None:
            return "dependency_missing: mss"
        return self._index.error

    @property
    def config(self) -> VisionConfig:
        return self._cfg

    @property
    def last_detail(self) -> str:
        """最近一次识别的调试细节（reason 只放纯原因码，细节在此与日志里）。"""
        return self._last_detail

    @property
    def index(self) -> MapIndex:
        return self._index

    def set_config(self, config: VisionConfig) -> None:
        with self._lock:
            self._cfg = (config or VisionConfig()).cloned()

    def update_world_map(self, path: str) -> None:
        """更换底图并重建索引（与 locate 互斥）。"""
        with self._lock:
            self._world_path = str(path or "")
            self._index = MapIndex(path)
            self._index.build()
            self._sift_index = None
            self._sync_engine()

    # -- 主流程
    def locate(self, frame_bgr=None) -> VisionResult:
        """单次识别；传入 frame_bgr（整屏 BGR）可跳过截屏，便于测试与复用。

        全程持有实例锁：与 update_world_map() 的索引重建互斥。
        reason 只写纯原因码，细节进 last_detail 与日志。
        """
        t0 = time.perf_counter()
        if np is None or cv2 is None:
            return self._fail(REASON_DEPENDENCY, t0, "dependency_missing: cv2/numpy")
        if frame_bgr is None and mss is None:
            return self._fail(REASON_DEPENDENCY, t0, "dependency_missing: mss")
        with self._lock:
            return self._locate_locked(frame_bgr, t0)

    def _locate_locked(self, frame_bgr, t0: float) -> VisionResult:
        cfg = self._cfg
        if frame_bgr is None:
            frame, frame_detail = self._capture()
            if frame is None:
                return self._fail(REASON_CAPTURE_FAILED, t0, frame_detail or "截屏失败")
        else:
            frame = self._as_bgr(frame_bgr)
            if frame is None:
                return self._fail(REASON_CAPTURE_FAILED, t0, "传入画面无效")
            frame_detail = "frame %dx%d" % (frame.shape[1], frame.shape[0])

        if not self._index.ready:
            self._index.build()
        levels = self._index.levels
        use_sift = max(self._index.size) >= SIFT_ENGINE_MIN_DIM
        if not levels and not use_sift:
            return self._fail(REASON_MAP_INDEX_MISSING, t0,
                              "%s；%s" % (frame_detail, self._index.error or "底图索引未就绪"))

        prep, region_detail, code = self._extract_minimap(frame)
        trace = "%s；%s" % (frame_detail, region_detail)
        if prep is None:
            return self._fail(code or REASON_MINIMAP_NOT_FOUND, t0, trace)

        # 官方大图以 SIFT 为准：它在实测中没有误报；ORB 在该尺度下可能给出错误位置，
        # 所以大图上 SIFT 失败就直接上报失败（上层保留上次位置），不再退回 ORB。
        # 只有 SIFT 本身不可用（未装 OpenCV/SIFT）时才走 ORB。
        if use_sift:
            sift_res = self._sift_locate(prep, t0, trace)
            if sift_res is not None:
                return sift_res
        if not levels:
            return self._fail(REASON_MAP_INDEX_MISSING, t0,
                              "%s；%s" % (trace, self._index.error or "底图索引未就绪"))

        best, fail = self._search(prep, levels, cfg)
        if best is None:
            return self._fail(fail[0], t0, "%s；%s" % (trace, fail[1]))
        self._last_detail = "%s；inliers=%d good=%d cov=%d level=%.3f r=%.2f conf=%.3f" % (
            trace, best["inliers"], best["good"], best["cov"], best["eff"], best["r"], best["conf"])
        return self._build_result(best, t0)

    def _sift_locate(self, prep, t0: float, trace: str) -> Optional[VisionResult]:
        """大图定位：SIFT 匹配 + 相似变换。不可用时返回 None（交回 ORB）。"""
        if self._sift_index is None:
            self._sift_index = SiftMapIndex(self._world_path)
        idx = self._sift_index
        if not idx.ready and not idx.error:
            idx.build()
        if not idx.ready or self._sift_matcher is None:
            logger.debug("sift unavailable: %s", idx.error or "not ready")
            return None

        gray = prep["gray"]
        try:
            kps, des = idx.detector.detectAndCompute(gray, None)
        except Exception:
            kps, des = None, None
        if des is None or kps is None or len(kps) < 8:
            return self._fail(REASON_TOO_FEW_FEATURES, t0, "%s；SIFT 小地图特征不足" % trace)

        try:
            pairs = self._sift_matcher.knnMatch(des, idx.descriptors, k=2)
        except Exception:
            return self._fail(REASON_TOO_FEW_FEATURES, t0, "%s；SIFT 匹配异常" % trace)
        good = []
        for pair in pairs:
            if len(pair) >= 2 and pair[0].distance < SIFT_RATIO * pair[1].distance:
                good.append(pair[0])
        min_in = max(int(self._cfg.min_inliers), SIFT_MIN_INLIERS)
        if len(good) < min_in:
            return self._fail(REASON_TOO_FEW_FEATURES, t0,
                              "%s；SIFT good=%d < %d" % (trace, len(good), min_in))

        src = np.float32([[kps[m.queryIdx].pt[0], kps[m.queryIdx].pt[1]] for m in good]).reshape(-1, 1, 2)
        dst = np.float32([idx.points[m.trainIdx] for m in good]).reshape(-1, 1, 2)
        try:
            M, inliers = cv2.estimateAffinePartial2D(
                src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0, confidence=0.995
            )
        except Exception:
            M, inliers = None, None
        if M is None or inliers is None:
            return self._fail(REASON_TOO_FEW_FEATURES, t0, "%s；SIFT 无法解出相似变换" % trace)
        n_in = int(np.count_nonzero(inliers))
        if n_in < min_in:
            return self._fail(REASON_TOO_FEW_FEATURES, t0,
                              "%s；SIFT 内点不足 inliers=%d < %d" % (trace, n_in, min_in))

        sx = math.hypot(float(M[0, 0]), float(M[1, 0]))
        if sx <= 1e-6:
            return self._fail(REASON_TOO_FEW_FEATURES, t0, "%s；SIFT 变换退化" % trace)
        cx, cy = prep["cx"], prep["cy"]
        wx = float(M[0, 0]) * cx + float(M[0, 1]) * cy + float(M[0, 2])
        wy = float(M[1, 0]) * cx + float(M[1, 1]) * cy + float(M[1, 2])
        nx, ny = wx / float(idx.size[0]), wy / float(idx.size[1])
        if not (-0.1 <= nx <= 1.1 and -0.1 <= ny <= 1.1):
            return self._fail(REASON_LOW_CONFIDENCE, t0,
                              "%s；SIFT 结果越界 (%.3f, %.3f)" % (trace, nx, ny))

        keep = np.asarray(inliers).ravel().astype(bool)
        cov = _coverage(np.asarray(src).reshape(-1, 2)[keep], (gray.shape[1], gray.shape[0]))
        ratio = n_in / float(max(1, len(good)))
        conf = float(_clamp(0.45 * min(1.0, n_in / 30.0)
                            + 0.30 * min(1.0, ratio / 0.6)
                            + 0.25 * min(1.0, cov / 6.0), 0.0, 0.99))
        need = 0.30 + 0.30 * self._cfg.sensitivity
        if conf < need:
            return self._fail(REASON_LOW_CONFIDENCE, t0,
                              "%s；SIFT conf=%.3f < %.3f" % (trace, conf, need))

        heading = math.degrees(math.atan2(float(M[1, 0]), float(M[0, 0]))) % 360.0
        hyp = {
            "nx": nx,
            "ny": ny,
            "heading": heading,
            "scale": sx,
            "conf": conf,
            "foot": math.hypot(gray.shape[1], gray.shape[0]) * sx,
        }
        self._last_detail = "%s；sift inliers=%d good=%d cov=%d conf=%.3f" % (
            trace, n_in, len(good), cov, conf)
        return self._build_result(hyp, t0)

    # -- 内部
    def _sync_engine(self) -> None:
        if cv2 is None:
            return
        self._matcher = cv2.BFMatcher(cv2.NORM_HAMMING, False)
        self._sift_matcher = cv2.BFMatcher(cv2.NORM_L2, False)
        self._orb_mini = cv2.ORB_create(nfeatures=MINI_ORB_FEATURES)

    def _fail(self, reason: str, t0: float, detail: str = "") -> VisionResult:
        self._last_detail = detail or reason
        if detail:
            logger.debug("locate failed: %s (%s)", reason, detail)
        return VisionResult(
            ok=False,
            x=None,
            y=None,
            rotation=None,
            confidence=0.0,
            scale=None,
            reason=reason,
            timestamp=time.time(),
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def _as_bgr(self, frame):
        try:
            arr = np.asarray(frame)
        except Exception:
            return None
        if arr.ndim == 2:
            arr = cv2.cvtColor(arr.astype(np.uint8), cv2.COLOR_GRAY2BGR)
        elif arr.ndim == 3:
            if arr.shape[2] == 4:
                arr = arr[:, :, [2, 1, 0]]
            elif arr.shape[2] != 3:
                return None
        else:
            return None
        if arr.shape[0] < 32 or arr.shape[1] < 32:
            return None
        return np.ascontiguousarray(arr.astype(np.uint8))

    def _capture(self):
        """mss 截屏，返回 (BGR ndarray | None, detail)。"""
        with self._capture_lock:
            try:
                if self._grabber is None:
                    self._grabber = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
                monitors = list(self._grabber.monitors or [])
                if not monitors:
                    return None, "capture_failed: 无可用显示器"
                idx = self._cfg.monitor_index
                if idx == SCREEN_PRIMARY:
                    mon = monitors[1] if len(monitors) > 1 else monitors[0]
                else:
                    mon = monitors[idx] if idx < len(monitors) else monitors[0]
                shot = self._grabber.grab(mon)
                arr = None
                try:
                    buf = bytes(shot.rgb)
                    arr = np.frombuffer(buf, dtype=np.uint8).reshape(shot.height, shot.width, 4)
                    arr = np.ascontiguousarray(arr[:, :, [2, 1, 0]])
                except Exception:
                    arr = np.asarray(shot, dtype=np.uint8)[:, :, :3].copy()
                return arr, "captured %dx%d" % (arr.shape[1], arr.shape[0])
            except Exception as exc:  # 无显示器 / 权限 / 平台异常
                self._grabber = None
                return None, "capture_failed: %s" % exc

    # 小地图区域 ---------------------------------------------------------------

    def _extract_minimap(self, frame):
        """返回 (prepared dict | None, detail, reason_code)。"""
        h, w = frame.shape[:2]
        circle = None
        if self._cfg.auto_detect:
            circle = self._detect_circle(frame)
        if circle is not None:
            cx, cy, r = circle
            box = (int(cx - r), int(cy - r), int(cx + r), int(cy + r))
            detail = "auto_detect circle=(%.0f,%.0f,%.0f)" % (cx, cy, r)
        else:
            nx, ny, nw, nh = self._cfg.minimap_rect
            x0, y0 = int(round(nx * w)), int(round(ny * h))
            x1, y1 = int(round((nx + nw) * w)), int(round((ny + nh) * h))
            side = min(x1 - x0, y1 - y0)
            if side < 24:
                return None, "minimap_rect 过小: %dx%d" % (x1 - x0, y1 - y0), REASON_MINIMAP_NOT_FOUND
            # 归一化矩形可能非正方形：取居中最大正方形作为圆形小地图外接盒
            cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
            box = (cx - side // 2, cy - side // 2, cx + side // 2, cy + side // 2)
            detail = "fallback fixed rect (%d,%d,%d,%d)" % (x0, y0, x1, y1)
            if self._cfg.auto_detect and circle is None:
                detail += "；自动检测未找到圆形小地图，已回退固定区域"

        prep = self._prepare(frame, box)
        if prep is None:
            return None, detail, REASON_MINIMAP_NOT_FOUND
        return prep, detail, ""

    def _detect_circle(self, frame):
        """在左上/右上候选象限里找圆形小地图，返回 (cx, cy, r)（整帧坐标）或 None。"""
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        best = None
        for name, sub, ox, oy in (
            ("tl", gray[: h // 2, : w // 2], 0, 0),
            ("tr", gray[: h // 2, w // 2 :], w // 2, 0),
        ):
            found = self._detect_circle_in(sub, ox, oy)
            if found and (best is None or found[2] > best[2]):
                best = found
                logger.debug("minimap circle in %s: %s", name, found)
        return best

    def _detect_circle_in(self, region, ox, oy):
        rh, rw = region.shape[:2]
        if rw < 48 or rh < 48:
            return None
        scale = float(_clamp(float(DETECT_REGION_MAX_DIM) / max(rw, rh), 0.05, 1.0))
        small = cv2.resize(region, (max(24, int(rw * scale)), max(24, int(rh * scale))),
                           interpolation=cv2.INTER_AREA)
        sh, sw = small.shape[:2]
        mdim = min(sw, sh)
        blur = cv2.GaussianBlur(small, (5, 5), 0)
        out = None
        try:
            circles = cv2.HoughCircles(
                blur,
                cv2.HOUGH_GRADIENT,
                dp=1.2,
                minDist=int(mdim * 0.6),
                param1=110,
                param2=26,
                minRadius=int(mdim * 0.10),
                maxRadius=int(mdim * 0.48),
            )
        except Exception:
            circles = None
        if circles is not None:
            c = circles[0]
            # 取半径最大且内容有明显纹理的圆
            order = sorted(c.tolist(), key=lambda v: -v[2])
            for cx, cy, r in order:
                if self._texture_score(small, cx, cy, r) >= 4.0:
                    out = (cx, cy, r)
                    break
        if out is None:
            out = self._detect_by_contour(blur, scale)
        if out is None:
            return None
        cx, cy, r = out
        return (cx / scale + ox, cy / scale + oy, r / scale)

    def _detect_by_contour(self, blur, scale):
        """备选：边缘 + 轮廓的圆度筛选（小地图常有高对比外圈）。"""
        sh, sw = blur.shape[:2]
        edges = cv2.Canny(blur, 40, 120)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        for c in contours:
            area = cv2.contourArea(c)
            peri = cv2.arcLength(c, True)
            if area < 60 or peri <= 0:
                continue
            circular = 4.0 * math.pi * area / (peri * peri)
            if circular < 0.55:
                continue
            (cx, cy), r = cv2.minEnclosingCircle(c)
            if r < min(sw, sh) * 0.10 or r > min(sw, sh) * 0.50:
                continue
            if self._texture_score(blur, cx, cy, r) < 4.0:
                continue
            if best is None or r > best[2]:
                best = (cx, cy, r)
        return best

    def _texture_score(self, gray, cx, cy, r):
        """圆内纹理强度：标准差足够大才认为是一张地图而不是一色圆。"""
        h, w = gray.shape[:2]
        x0 = int(max(0, cx - r * 0.85))
        x1 = int(min(w, cx + r * 0.85))
        y0 = int(max(0, cy - r * 0.85))
        y1 = int(min(h, cy + r * 0.85))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return 0.0
        roi = gray[y0:y1, x0:x1]
        return float(roi.std())

    def _prepare(self, frame, box):
        """裁片预处理：灰度 -> 估算内容半径去掉黑圈边框 -> 圆形掩膜 -> CLAHE。

        返回 {"gray", "cx", "cy"}，其中 cx/cy 是内容中心在处理后灰度图中的坐标。
        """
        h, w = frame.shape[:2]
        x0, y0, x1, y1 = box
        x0 = int(max(0, min(w - 1, x0)))
        y0 = int(max(0, min(h - 1, y0)))
        x1 = int(max(x0 + 1, min(w, x1)))
        y1 = int(max(y0 + 1, min(h, y1)))
        crop = frame[y0:y1, x0:x1]
        side = min(crop.shape[0], crop.shape[1])
        if side < 24:
            return None
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        cx = crop.shape[1] / 2.0
        cy = crop.shape[0] / 2.0
        r_outer = side / 2.0
        r_inner = self._content_radius(gray, cx, cy, r_outer)
        if r_inner < r_outer * 0.30:
            return None
        if r_inner > MINI_MAX_DIM / 2.0:  # 控制匹配开销：小地图裁片不超过上限
            k = (MINI_MAX_DIM / 2.0) / r_inner
            gray = cv2.resize(
                gray,
                (max(8, int(gray.shape[1] * k)), max(8, int(gray.shape[0] * k))),
                interpolation=cv2.INTER_AREA,
            )
            cx *= k
            cy *= k
            r_inner *= k
        d = int(math.ceil(r_inner))
        bx0, bx1 = int(max(0, cx - d)), int(min(gray.shape[1], cx + d))
        by0, by1 = int(max(0, cy - d)), int(min(gray.shape[0], cy + d))
        inner = gray[by0:by1, bx0:bx1]
        if inner.size == 0 or min(inner.shape) < 16:
            return None
        ih, iw = inner.shape[:2]
        flat = int(np.median(inner))
        yy, xx = np.mgrid[0:ih, 0:iw]
        ccx, ccy = cx - bx0, cy - by0
        rr = min(ccx, iw - ccx, ccy, ih - ccy)
        mask = ((xx - ccx) ** 2 + (yy - ccy) ** 2) <= (rr * 0.995) ** 2
        out = np.where(mask, inner, flat).astype(np.uint8)
        out = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(out)
        if float(out[mask].std()) < 6.0:
            return None  # 全黑/纯色，没有可识别内容
        return {"gray": out, "cx": float(ccx), "cy": float(ccy), "r": float(rr)}

    @staticmethod
    def _content_radius(gray, cx, cy, r_outer):
        """由外向内采样圆周亮度，找到被暗色边框包住的内容半径（去掉黑圈/外框）。"""
        h, w = gray.shape[:2]
        x0, x1 = int(max(0, cx - 0.4 * r_outer)), int(min(w, cx + 0.4 * r_outer))
        y0, y1 = int(max(0, cy - 0.4 * r_outer)), int(min(h, cy + 0.4 * r_outer))
        if x1 <= x0 or y1 <= y0:
            return r_outer * 0.9
        core = float(gray[y0:y1, x0:x1].mean())
        ang = np.linspace(0.0, 2.0 * math.pi, 72, endpoint=False)
        ux, uy = np.cos(ang), np.sin(ang)
        step = max(1.0, r_outer / 24.0)
        limit = r_outer * 0.45
        r = r_outer - step
        while r > limit:
            xs = np.clip(np.round(cx + ux * r).astype(np.int32), 0, w - 1)
            ys = np.clip(np.round(cy + uy * r).astype(np.int32), 0, h - 1)
            if float(gray[ys, xs].mean()) >= 0.55 * core:
                return float(max(limit, r - step))  # 再收一格，避开抗锯齿边缘
            r -= step
        return float(limit)

    # 特征匹配 ---------------------------------------------------------------

    def _search(self, prep, levels, cfg):
        """两阶段匹配：粗到细取最佳假设 -> 最细层 tile 迭代细化 -> 统一阈值判定。

        对齐条件：把小地图放大 r 倍后，其「每原始像素采样数」应等于该层的 eff，
        即 r ≈ eff * scale，因此放大倍率集合只在粗层搜索，细化时可直接解出 r。
        阈值判定全部放在末尾，以便区分 too_few_features 与 low_confidence。
        """
        cache: dict = {}

        def features(r):
            """小地图在给定放大倍率下的特征（同一倍率只检测一次，跨层复用）。"""
            key = round(float(r), 3)
            if key not in cache:
                img = self._scaled(prep["gray"], key)
                try:
                    kps, des = self._orb_mini.detectAndCompute(img, None)
                except Exception:
                    kps, des = None, None
                need = max(cfg.min_inliers, 12)
                cache[key] = None if (des is None or kps is None or len(kps) < need) else (
                    kps, des, (img.shape[1], img.shape[0]))
            return cache[key]

        # -- 阶段 1：全层搜索（由粗到细，粗层最便宜）
        best = None
        for level in levels:
            level_best = None
            for r in MINI_SCALE_CANDIDATES:
                feat = features(r)
                if feat is None:
                    continue
                hyp = self._match_hypothesis(feat, r, level, None, prep, cfg)
                if hyp is not None and (level_best is None or _quality(hyp) > _quality(level_best)):
                    level_best = hyp
            if level_best is None:
                continue
            if best is None or _quality(level_best) > _quality(best):
                best = level_best
            if best["inliers"] >= max(cfg.min_inliers * 2, 12):
                break  # 粗层已经够用，直接进入细化，省算力
        if best is None:
            return None, (REASON_TOO_FEW_FEATURES, "小地图/底图特征不足或无法对齐尺度")

        # -- 阶段 2：最细层局部 tile 细化（提高坐标与朝向精度，同时压缩搜索量）
        fine = levels[-1]
        for _ in range(REFINE_STEPS):
            r = float(_clamp(fine["eff"] * best["scale"], *MINI_SCALES_LIMIT))
            feat = features(r)
            if feat is None:
                break
            hyp = self._match_hypothesis(feat, r, fine, self._window(best, fine), prep, cfg)
            if hyp is None or _quality(hyp) < 0.9 * _quality(best):
                break  # 细化没有更好就保留粗结果
            moved = math.hypot(hyp["nx"] - best["nx"], hyp["ny"] - best["ny"])
            best = hyp
            if moved < 0.002:
                break

        if best["inliers"] < cfg.min_inliers:
            return None, (REASON_TOO_FEW_FEATURES,
                          "内点不足 inliers=%d < min_inliers=%d" % (best["inliers"], cfg.min_inliers))
        need_conf = CONF_MIN + CONF_SPAN * cfg.sensitivity
        if best["conf"] < need_conf:
            return None, (REASON_LOW_CONFIDENCE, "conf=%.3f < %.3f" % (best["conf"], need_conf))
        return best, None

    def _match_hypothesis(self, feat, r, level, window, prep, cfg):
        res = self._match(feat, level, window, cfg)
        if res is None:
            return None
        return self._hypothesis(res, r, level, prep)

    @staticmethod
    def _scaled(mini, r):
        """按倍率重采样小地图（>1 用线性放大，<1 用区域缩小保纹理）。"""
        key = float(r)
        h, w = mini.shape[:2]
        return cv2.resize(mini, (max(16, int(w * key)), max(16, int(h * key))),
                          interpolation=cv2.INTER_LINEAR if key > 1.0 else cv2.INTER_AREA)

    def _window(self, hyp, level):
        """细化阶段的候选 tile：以已有结果为中心，按预期视野框定局部范围。"""
        half = hyp["foot"] * level["eff"] * 0.75 + 32.0
        half = max(48.0, half)
        cx = hyp["nx"] * level["w"]
        cy = hyp["ny"] * level["h"]
        return (cx - half, cy - half, cx + half, cy + half)

    def _match(self, feat, level, window, cfg):
        kps, des, size = feat
        pts = level["pts"]
        ldes = level["des"]
        if window is not None:
            x0, y0, x1, y1 = window
            keep = (pts[:, 0] >= x0) & (pts[:, 0] <= x1) & (pts[:, 1] >= y0) & (pts[:, 1] <= y1)
            if int(keep.sum()) < max(cfg.min_inliers * 3, 16):
                return None
            pts = pts[keep]
            ldes = ldes[keep]
        try:
            pairs = self._matcher.knnMatch(des, ldes, k=2)
        except Exception:
            return None
        # Lowe ratio test：sensitivity 越高阈值越严（0.88 -> 0.62，仍在可用区间内）
        ratio = float(_clamp(0.88 - 0.26 * cfg.sensitivity, 0.5, 0.92))
        good = []
        for pair in pairs:
            if len(pair) < 2:
                continue
            a, b = pair[0], pair[1]
            if a.distance < ratio * b.distance:
                good.append(a)
        if len(good) < SANITY_MATCHES:
            return None
        src = np.float32([[kps[m.queryIdx].pt[0], kps[m.queryIdx].pt[1]] for m in good]).reshape(-1, 1, 2)
        dst = np.float32([pts[m.trainIdx] for m in good]).reshape(-1, 1, 2)
        try:
            M, inliers = cv2.estimateAffinePartial2D(
                src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0, confidence=0.995
            )
        except Exception:
            return None
        if M is None or inliers is None:
            return None
        n_in = int(np.count_nonzero(inliers))
        if n_in < SANITY_MATCHES:
            return None
        keep = np.asarray(inliers).ravel().astype(bool)
        return {"M": M, "inliers": n_in, "good": len(good),
                "cov": _coverage(np.asarray(src).reshape(-1, 2)[keep], size)}

    def _hypothesis(self, res, r, level, prep):
        """由仿射阵解出归一化坐标 / 朝向 / 比例尺 / 置信度。"""
        M = res["M"]
        sx = math.hypot(float(M[0, 0]), float(M[1, 0]))
        if sx <= 1e-6:
            return None
        cx, cy = prep["cx"] * r, prep["cy"] * r
        wx = float(M[0, 0]) * cx + float(M[0, 1]) * cy + float(M[0, 2])
        wy = float(M[1, 0]) * cx + float(M[1, 1]) * cy + float(M[1, 2])
        nx, ny = wx / float(level["w"]), wy / float(level["h"])
        if not (-0.25 <= nx <= 1.25 and -0.25 <= ny <= 1.25):
            return None
        eff = level["eff"]
        scale = sx * r / eff  # 大地图原始像素 / 小地图像素
        theta = math.degrees(math.atan2(float(M[1, 0]), float(M[0, 0]))) % 360.0
        inlier_ratio = res["inliers"] / float(max(1, res["good"]))
        ratio_score = min(1.0, inlier_ratio / CONF_RATIO_REF)
        count_score = min(1.0, res["inliers"] / CONF_INLIER_REF)
        cover_score = min(1.0, res["cov"] / CONF_COVERAGE_REF)
        w_ratio, w_count, w_cover = CONF_WEIGHTS
        conf = float(_clamp(w_ratio * ratio_score + w_count * count_score
                            + w_cover * cover_score, 0.0, 0.99))
        foot = math.hypot(prep["gray"].shape[1], prep["gray"].shape[0]) * scale
        return {
            "nx": nx,
            "ny": ny,
            "heading": theta,
            "scale": scale,
            "conf": conf,
            "inliers": res["inliers"],
            "good": res["good"],
            "cov": res["cov"],
            "r": float(r),
            "eff": eff,
            "foot": foot,
        }

    def _build_result(self, hyp, t0) -> VisionResult:
        x = float(_clamp(hyp["nx"], 0.0, 1.0))
        y = float(_clamp(hyp["ny"], 0.0, 1.0))
        return VisionResult(
            ok=True,
            x=round(x, 6),
            y=round(y, 6),
            rotation=round(float(hyp["heading"]), 2),
            confidence=round(float(hyp["conf"]), 4),
            scale=round(float(hyp["scale"]), 4),
            reason=REASON_OK,
            timestamp=time.time(),
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )


# --------------------------------------------------------------------------- 线程


class VisionWorker(QObject):
    """周期识图 worker：放进入 QThread 后由 started->run_loop 驱动 QTimer，不阻塞 UI。

    用法（上层）：
        thread = QThread(); worker = VisionWorker(path, cfg); worker.moveToThread(thread)
        thread.started.connect(worker.run_loop)
        worker.result_ready.connect(on_result)
        worker.stopped.connect(thread.quit)
        thread.start()
    """

    result_ready = Signal(dict)  # VisionResult.to_dict()
    started_loop = Signal()
    stopped = Signal()

    # 跨线程控制：QTimer 只能被它所属线程启停，否则 Qt 会打印
    # "Timers cannot be stopped from another thread" 并可能在对象析构时崩溃。
    # 这三个信号在 __init__ 里连到本类的 _do_* 槽，AutoConnection 的语义正好是：
    # 同线程直连、立即执行；从 GUI 线程发出则排队到 worker 线程的事件循环里执行。
    stop_requested = Signal()
    config_requested = Signal(object)
    locate_requested = Signal()

    def __init__(self, world_map_path: str, config: VisionConfig, parent=None):
        super().__init__(parent)
        self._cfg = (config or VisionConfig()).cloned()
        self._localizer = MinimapLocalizer(world_map_path, self._cfg)
        self._timer: Optional[QTimer] = None
        self._finished_hooked: Optional[QThread] = None
        self._running = False
        self._paused = False
        self._fails = 0
        self._degraded = False
        self.stop_requested.connect(self._do_stop)
        self.config_requested.connect(self._do_set_config)
        self.locate_requested.connect(self._do_locate)

    # -- 属性
    @property
    def localizer(self) -> MinimapLocalizer:
        return self._localizer

    @property
    def config(self) -> VisionConfig:
        return self._cfg

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def degraded(self) -> bool:
        """连续失败 >=3 次后进入降频省电状态。"""
        return self._degraded

    @property
    def effective_interval_ms(self) -> int:
        if self._degraded:
            return max(int(self._cfg.interval_ms), DEGRADED_INTERVAL_MS)
        return int(self._cfg.interval_ms)

    # -- slots（对外：线程安全，可在 GUI 线程直接调用）
    def _same_thread(self) -> bool:
        return self.thread() is QThread.currentThread()

    def set_config(self, config: VisionConfig) -> None:
        """更新配置。跨线程调用时排队到 worker 线程执行，返回即表示「已受理」。"""
        if self._same_thread():
            self._do_set_config(config)
        else:
            self.config_requested.emit(config)

    def set_paused(self, paused: bool) -> None:
        """暂停只是不再自动识别，写一个 bool 标志，不涉及 Qt 定时器，可跨线程。"""
        self._paused = bool(paused)

    def run_loop(self) -> None:
        """在 worker 所在线程的事件循环里启动周期识别。"""
        self._running = True
        self._fails = 0
        self._degraded = False
        if self._timer is None:
            # 故意不给 QTimer 设 parent：这样它的生死完全由 Python 引用决定，
            # _release_timer 里置 None 就能在 worker 线程当场析构，
            # 避免上层在 GUI 线程回收本对象时跨线程销毁定时器（Qt 会崩溃）。
            self._timer = QTimer()
            self._timer.setTimerType(PRECISE_TIMER)
            self._timer.timeout.connect(self._tick)
        self._timer.setInterval(self.effective_interval_ms)
        self._timer.start()
        owner = self.thread()
        if owner is not None and self._finished_hooked is not owner:
            # 兜底：不管上层怎么退出线程（quit 抢在停止请求之前也无所谓），
            # 线程结束时一定在 worker 线程内把定时器收掉。
            # 注意这里必须在 worker 线程内连接：finished 从本线程发出，接收者同线程 → 直连，
            # 才能保证事件循环退出前执行；若只在「当前不在 worker 线程」时才连接，
            # 正常 started→run_loop 路径反而永远连不上，定时器会泄漏并可能跨线程析构。
            owner.finished.connect(self._release_timer)
            self._finished_hooked = owner
        self.started_loop.emit()

    def stop(self) -> None:
        """停止周期识别。

        定时器的启停一律在 worker 线程完成；QThread.quit 本身线程安全，
        这里额外请求退出所属线程的事件循环，方便「stop() 后直接 wait()」的调用方。
        """
        if self._same_thread():
            self._do_stop()
        else:
            self.stop_requested.emit()
        owner = self.thread()
        if owner is not None and owner is not QThread.currentThread():
            owner.quit()  # 只退出 worker 线程，不碰 GUI 线程

    def locate_once(self) -> Optional[dict]:
        """立即识别一次（忽略暂停与 enabled，用于手动触发）。

        同线程调用返回本次结果字典；从 GUI 线程调用时只能排队执行，返回 None，
        结果通过 result_ready 信号送达。
        """
        if self._same_thread():
            return self._do_locate()
        self.locate_requested.emit()
        return None

    # -- 内部（只在 worker 线程执行）
    def _do_set_config(self, config: VisionConfig) -> None:
        self._cfg = (config or VisionConfig()).cloned()
        self._localizer.set_config(self._cfg)
        if self._timer is not None:
            self._timer.setInterval(self.effective_interval_ms)

    def _do_stop(self) -> None:
        self._running = False
        self._release_timer()
        self.stopped.emit()

    def _release_timer(self) -> None:
        """停掉并销毁周期定时器。只能在定时器所属线程调用。"""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None  # 无 parent，引用一丢就在本线程当场析构

    def _do_locate(self) -> dict:
        return self._step().to_dict()

    def _tick(self) -> Optional[VisionResult]:
        """定时器回调：暂停 / 未开启时什么都不做，否则走一次步进并返回结果。"""
        if self._paused or not self._cfg.enabled:
            return None
        return self._step()

    def _step(self) -> VisionResult:
        """一次循环步进，含失败降频状态机。

        降频只拉长定时器间隔（省 CPU），每一轮仍然真的识别一次：
        若在降频期间干脆不识别，过场动画/黑屏导致的连续失败会让位置永久冻结，
        画面恢复正常后也无法自愈。
        """
        res = self._localizer.locate()
        if res.ok:
            self._fails = 0
            if self._degraded:
                self._degraded = False
                if self._timer is not None:
                    self._timer.setInterval(self.effective_interval_ms)
        else:
            self._fails += 1
            if self._fails >= DEGRADED_FAIL_STREAK and not self._degraded:
                self._degraded = True
                if self._timer is not None:
                    self._timer.setInterval(self.effective_interval_ms)
                    logger.info("vision degraded: interval -> %dms", self.effective_interval_ms)
        self.result_ready.emit(res.to_dict())
        return res
