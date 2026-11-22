"""主窗口：置顶悬浮小窗 + 地图画布 + 功能面板 + 识图线程 + 全局热键。"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRect, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QCursor, QDesktopServices, QKeySequence, QPixmap,
                           QShortcut)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QPushButton, QSlider, QSpinBox, QToolButton, QVBoxLayout,
                               QWidget)

from .. import COMPLIANCE_STATEMENT, __app_name__, __version__
from ..core import paths
from ..core.auth import AuthManager
from ..core.config import Config
from ..core.hotkeys import HOTKEY_ACTIONS, HotkeyManager
from ..core.marker_store import MarkerStore
from ..core.guide import (ARRIVE_METERS, auto_route, floor_from_combo, floor_label,
                          generic_hint, meters_between, plan_walk, point_floor,
                          relative_bearing)
from ..core.models import Layer, MapPoint, Route, RouteStep
from ..core.provider import (ApiConfig, DataProvider, KuroApiProvider, LocalDataProvider,
                             ProviderError, discover_map_packs)
from ..core.sync import SyncEngine
from ..core.theme import palette, qss
from .map_canvas import MapCanvas
from .overlay import OverlayHUD
from .settings_dialog import SettingsDialog
from .side_panel import SidePanel
from .tray import TrayController

ACCURACY_NOTE = "识图定位存在误差，仅作探索参考，不保证识别结果完全准确。"


class VisionRunner(QObject):
    """把识图放到独立线程里跑，UI 线程零阻塞。"""

    result = Signal(object)     # VisionResult
    availability = Signal(bool, str)

    def __init__(self, map_image: Path | None, config: dict, parent: QObject | None = None):
        super().__init__(parent)
        self.map_image = map_image
        self.cfg = dict(config)
        self.thread: QThread | None = None
        self.worker = None
        self.enabled = False
        self.available = True
        self.error = ""
        self.last_result = None
        self.fail_streak = 0
        self.stats = {"ok": 0, "fail": 0}

    def _make_config(self):
        from ..core.recognizer import VisionConfig

        return VisionConfig(
            enabled=self.enabled,
            interval_ms=int(self.cfg.get("interval_ms", 1500)),
            minimap_rect=tuple(self.cfg.get("minimap_rect", (0.008, 0.012, 0.115, 0.115))),
            auto_detect=bool(self.cfg.get("auto_detect", True)),
            sensitivity=float(self.cfg.get("sensitivity", 0.55)),
            smooth=bool(self.cfg.get("smooth", True)),
        )

    def start(self) -> None:
        if self.thread is not None:
            if not self.stop():
                # 上一轮线程还没退干净：先别起第二个，否则两个识别线程抢同一张索引
                self.availability.emit(False, "识图线程正在退出，请稍后再开启")
                return
        from ..core.recognizer import VisionWorker

        if not self.map_image or not Path(self.map_image).exists():
            self.availability.emit(False, "未加载地图底图，识图不可用")
            self.available = False
            return
        self.available = True
        vcfg = self._make_config()
        try:
            probe = VisionWorker(str(self.map_image), vcfg)
        except Exception as exc:  # noqa: BLE001 - 依赖缺失等
            self.available = False
            self.availability.emit(False, f"识图初始化失败：{exc}")
            return
        self.worker = probe
        probe.result_ready.connect(self._on_result)
        self.thread = QThread()
        probe.moveToThread(self.thread)
        self.thread.started.connect(probe.run_loop)
        self.thread.finished.connect(probe.deleteLater)
        probe.stopped.connect(self._on_worker_stopped)
        self.enabled = True
        self.thread.start()
        self.availability.emit(True, "识图已开启")

    def _on_worker_stopped(self) -> None:
        # 只负责让 worker 线程退出事件循环。
        # 这里绝对不能把 self.thread 置空：线程可能还在跑，丢掉唯一的 Python 引用
        # 会析构仍在运行的 QThread，Qt 会直接 abort（表现为“点关闭识图就闪退”）。
        thread = self.thread
        if thread is not None and thread.isRunning():
            thread.quit()

    def stop(self) -> bool:
        """停止识图线程；返回 False 表示线程 3s 内没退出（引用保留，下次再收）。"""
        self.enabled = False
        worker, thread = self.worker, self.thread
        if worker is not None:
            worker.stop()
        if thread is not None:
            thread.quit()
            if not thread.wait(3000):
                return False
            thread.deleteLater()
        self.thread = None
        self.worker = None
        return True

    def apply(self, cfg: dict, enabled: bool | None = None) -> None:
        self.cfg = dict(cfg)
        if enabled is not None:
            if enabled and not self.enabled:
                self.start()
            elif not enabled and self.enabled:
                self.stop()
            else:
                self.enabled = bool(enabled)
        if self.enabled and self.worker:
            self.worker.set_config(self._make_config())

    def locate_once(self) -> None:
        if self.worker:
            self.worker.locate_once()

    def _on_result(self, payload: dict) -> None:
        from ..core.recognizer import VisionResult

        res = VisionResult(**payload) if isinstance(payload, dict) else payload
        self.last_result = res
        if res.ok:
            self.fail_streak = 0
            self.stats["ok"] += 1
        else:
            self.stats["fail"] += 1
            self.fail_streak += 1
        self.result.emit(res)


class MainWindow(QMainWindow):
    close_to_tray = Signal()

    def __init__(self, config: Config | None = None):
        super().__init__()
        self.cfg = config or Config()
        self.store = MarkerStore(on_change=self._refresh_progress)
        self.api = ApiConfig.load()
        self.auth = AuthManager(self.api, parent=self)
        self.local_provider = self._make_local_provider()
        self.provider: DataProvider = self.local_provider
        self.sync = SyncEngine(self.store, conflict=self.cfg.sync.get("conflict", "newer"))
        self.hotkeys = HotkeyManager(self)
        self.dataset = None
        self.points: list[MapPoint] = []
        self.layers: list[Layer] = []
        self.layer_map: dict[str, Layer] = {}
        self.routes: list[Route] = []
        self.route_index_by_id: dict[str, Route] = {}
        self.active_route: Route | None = None
        self.waypoint = -1
        self.auto_next = True
        self._route_only_fav = False
        self._filter_query = ""
        self._vision_fail_hint_at = 0.0
        self._quitting = False
        self.guide_mode = False
        self.guide_route_id: str | None = None
        self.guide_started_at = 0.0
        self.guide_completed: set[str] = set()
        self.guide_skipped: set[str] = set()
        self.guide_step_times: dict[str, float] = {}
        self._guide_arrived_step = -1
        self._guide_auto_plan = False
        self._guide_completing = False

        # 窗口本身：轻量悬浮
        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.ui = self.cfg.ui
        flags = Qt.Window
        if self.ui.get("always_on_top", True):
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.resize(*self._default_size())

        self.canvas = MapCanvas(self, theme=self.ui.get("theme", "dark"))
        self.canvas.set_state_callbacks(self.store.is_done, self.store.get_note)
        central = QWidget(self)
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.panel = SidePanel(central, theme=self.ui.get("theme", "dark"))
        lay.addWidget(self.canvas, 1)
        lay.addWidget(self.panel)
        self.setCentralWidget(central)
        self.overlay = OverlayHUD(self.ui.get("theme", "dark"),
                                  width=int(self.ui.get("overlay_width", 300)),
                                  height=int(self.ui.get("overlay_height", 300)))
        self.overlay.set_opacity(float(self.ui.get("opacity", 0.96)))
        self.overlay.set_clickable(bool(self.ui.get("overlay_clickable", False)))
        self.overlay.point_done.connect(self._on_overlay_point_done)

        self.status = QLabel("", self)
        self.status.setObjectName("StatusBar")
        sb = self.statusBar()
        sb.addPermanentWidget(self.status)
        sb.setSizeGripEnabled(not self.ui.get("lock_size", False))

        self.vision = VisionRunner(None, self.cfg.vision, parent=self)
        self.vision.result.connect(self._on_vision_result)

        self.tray = TrayController(self)
        self._build_toolbar()
        self._connect_signals()
        self._apply_window_options()
        self.hotkeys.set_bindings(dict(self.cfg.hotkeys))
        self.install_local_shortcuts()

        QTimer.singleShot(0, self.load_data)

    # ------------------------------------------------------------------ 顶栏
    def _build_toolbar(self) -> None:
        tb = QToolButton(self)
        tb.setText("⚙")
        tb.setToolTip("设置（主题 / 透明度 / 快捷键 / 数据）")
        tb.clicked.connect(self.open_settings)
        self._tb_settings = tb
        bar = QWidget(self)
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(8, 4, 8, 4)
        hl.setSpacing(6)
        title = QLabel(f"{__app_name__}", bar)
        title.setObjectName("TitleLabel")
        hl.addWidget(title)
        self.chip_account = QLabel("无账号", bar)
        self.chip_account.setObjectName("StatusChip")
        self.chip_vision = QLabel("识图关", bar)
        self.chip_vision.setObjectName("StatusChip")
        self.chip_pos = QLabel("未定位", bar)
        self.chip_pos.setObjectName("StatusChip")
        hl.addWidget(self.chip_account)
        hl.addWidget(self.chip_vision)
        hl.addWidget(self.chip_pos)
        hl.addStretch(1)

        def tool(text: str, tip: str, checkable: bool = False) -> QToolButton:
            b = QToolButton(bar)
            b.setText(text)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            b.setAutoRaise(True)
            return b

        self.btn_top = tool("📌", "窗口置顶", True)
        self.btn_top.setChecked(bool(self.ui.get("always_on_top", True)))
        self.btn_top.toggled.connect(self._set_topmost)
        self.btn_compact = tool("◱", "紧凑悬浮模式（隐藏面板、缩小窗口，尽量少遮挡画面）", True)
        self.btn_compact.setChecked(bool(self.ui.get("compact", False)))
        self.btn_compact.toggled.connect(self._set_compact)
        self.btn_hide_done = tool("👻", "隐藏 / 显示已完成点位", True)
        self.btn_hide_done.setChecked(bool(self.ui.get("hide_completed", False)))
        self.btn_hide_done.toggled.connect(self._set_hide_completed)
        self.btn_mark = tool("✚", "点击即标记模式", True)
        self.btn_mark.toggled.connect(self._set_mark_mode)
        self.btn_vision = tool("👁", "开启 / 关闭屏幕识图", True)
        self.btn_vision.toggled.connect(self._toggle_vision)
        self.btn_overlay = tool("➤", "未探索点位浮窗（贴左下角、置顶、可穿透）", True)
        self.btn_overlay.setChecked(bool(self.ui.get("overlay", False)))
        self.btn_overlay.toggled.connect(self._toggle_overlay)
        self.btn_theme = tool("◐", "深色 / 浅色主题")
        self.btn_theme.clicked.connect(self._toggle_theme)
        self.btn_opaque = tool("💧", "窗口透明度（点击循环）")
        self.btn_opaque.clicked.connect(self._cycle_opacity)
        self.btn_min = tool("—", "最小化到系统托盘")
        self.btn_min.clicked.connect(self._minimize_to_tray)
        self.btn_close = tool("✕", "退出程序")
        self.btn_close.clicked.connect(self._quit)
        for b in (self.btn_mark, self.btn_hide_done, self.btn_compact, self.btn_vision,
                  self.btn_overlay, self.btn_top, self.btn_theme, self.btn_opaque, tb,
                  self.btn_min, self.btn_close):
            hl.addWidget(b)
        hl.addWidget(self._tb_settings)
        self.setMenuWidget(bar)

    def _default_size(self) -> tuple[int, int]:
        geo = self.ui.get("window_geometry")
        if geo and len(geo) == 4:
            return int(geo[2]), int(geo[3])
        return 1080, 700

    # ------------------------------------------------------------------ 信号连线
    def _connect_signals(self) -> None:
        c, p = self.canvas, self.panel
        c.point_clicked.connect(self._on_point_clicked)
        c.point_hovered.connect(self._on_point_hovered)
        c.point_context.connect(self._on_point_context)
        c.background_context.connect(self._on_background_context)
        c.position_picked.connect(self._on_manual_position)
        p.layer_toggled.connect(self._on_layer_toggled)
        p.layers_all.connect(self._on_layers_all)
        p.hide_completed_toggled.connect(lambda on: (self.btn_hide_done.setChecked(on), self._set_hide_completed(on)))
        p.mark_mode_toggled.connect(lambda on: (self.btn_mark.setChecked(on), self._set_mark_mode(on)))
        p.search_changed.connect(self._on_search)
        p.result_selected.connect(lambda pt: (self.canvas.center_on(pt.x, pt.y, max(self.canvas._scale, 2.2)),
                                              self._select_point(pt)))
        p.focus_player.connect(lambda: self.canvas.center_player())
        p.reset_view.connect(self.canvas.fit_view)
        p.route_load.connect(self.load_route)
        p.route_favorite.connect(self._toggle_favorite)
        p.routes_only_fav.connect(self._on_only_fav)
        p.waypoint_prev.connect(lambda: self.step_waypoint(-1))
        p.waypoint_next.connect(lambda: self.step_waypoint(+1))
        p.waypoint_complete.connect(self._complete_current_waypoint)
        p.waypoint_auto_next.connect(lambda on: setattr(self, "auto_next", on))
        p.login_clicked.connect(self.begin_login)
        p.logout_clicked.connect(self.logout)
        p.sync_pull.connect(lambda: self.sync.sync(do_push=False) if self.sync.cloud_enabled() else self._notify_local_mode("pull"))
        p.sync_push.connect(lambda: self.sync.push())
        p.sync_both.connect(lambda: self.sync.sync(do_push=True))
        p.vision_toggled.connect(lambda on: (self.btn_vision.setChecked(on), self._toggle_vision(on)))
        p.vision_interval.connect(lambda ms: self._update_vision_cfg(interval_ms=ms))
        p.vision_sensitivity.connect(lambda s: self._update_vision_cfg(sensitivity=s))
        p.manual_pick.connect(self.enable_manual_pick)
        p.clear_position.connect(self.clear_position)
        self.auth.login_url_ready.connect(self._open_authorize_url)
        self.auth.login_succeeded.connect(lambda info: self._on_login_ok(info))
        self.auth.login_failed.connect(lambda m: self._notify(f"登录未完成：{m}", warn=True))
        self.auth.needs_configuration.connect(lambda m: self._notify(m, warn=True, details=True))
        self.auth.state_changed.connect(lambda _b: self._refresh_account_chip())
        p.map_pack_selected.connect(self.select_map_pack)
        p.open_official_map.connect(self._open_official_map)
        p.open_official_map_external.connect(self._open_official_map_external)
        p.open_official_map_only.connect(self.enter_map_only)
        p.start_guide.connect(self.start_guide_ui)
        self.sync.finished.connect(self._on_sync_finished)
        self.sync.progress.connect(lambda m: self.status.setText(m))
        self.vision.availability.connect(lambda ok, msg: self._on_vision_availability(ok, msg))
        self.hotkeys.triggered.connect(self._on_hotkey)

    def _make_local_provider(self) -> LocalDataProvider:
        imp = self.cfg.data_cfg.get("import_dir")
        points = self.cfg.data_cfg.get("points_file")
        image = self.cfg.data_cfg.get("map_image")
        if not imp and not points and not image:
            # 地面世界已拆成国家包时，默认加载第一个（瑝珑），不再落到拼合总图
            packs = discover_map_packs()
            first = next((p for p in packs if not p.is_root), None)
            if first is not None:
                imp = str(first.directory)
        return LocalDataProvider(
            imp_dir=Path(imp) if imp else None,
            override_points=points,
            override_map=image)

    # ------------------------------------------------------------------ 数据加载
    def load_data(self) -> None:
        try:
            self.dataset = self.provider.load_dataset()
        except ProviderError as exc:
            self._notify(str(exc), warn=True)
            self.dataset = LocalDataProvider().load_dataset()
        try:
            known_ids = {p.id for p in self.dataset.points}
            self.routes = self.provider.load_routes(valid_point_ids=known_ids)
        except ProviderError as exc:
            self._notify(f"路线加载失败：{exc}", warn=True)
            self.routes = []
        self.points = list(self.dataset.points) + self.store.private_points()
        self.layer_map = self.dataset.layer_map()
        self.layers = self.dataset.ordered_layers()
        for p in self.points:
            if p.layer not in self.layer_map:
                self.layer_map[p.layer] = Layer(key=p.layer, label=p.layer, color="#9AA4B2")

        img = self.provider.map_image_path(self.dataset)
        if img and Path(img).exists():
            pix = QPixmap(str(img))
            self.canvas.set_map_pixmap(pix if not pix.isNull() else None)
            self.overlay.set_map(self.canvas._pix)
            self.vision.map_image = Path(img) if self.canvas._pix else None
            if self.vision.enabled:
                self.vision.apply(self.cfg.vision, enabled=True)
        else:
            self.canvas.set_map_pixmap(None)
            self.overlay.set_map(None)
            self.vision.map_image = None

        visible = {l.key for l in self.layers if self.cfg.layer_visible(l.key)}
        self.canvas.set_points(self.points, self.layer_map, visible)
        self.canvas.hide_completed = bool(self.ui.get("hide_completed", False))
        self.btn_hide_done.setChecked(self.canvas.hide_completed)
        self.panel.set_layers(self.layers, self._layer_counts(), visible)
        # 数据包没有 routes_*.json 时，至少给一条能用的顺路路线，别让「探索路线」空着
        if not self.routes and self.points:
            auto = self._make_auto_route()
            if auto is not None:
                self.routes = [auto]
                self._notify(f"当前数据包没有路线文件，已按就近顺序自动生成「{auto.name}」")
        self._refresh_progress()
        self._refresh_routes()
        self._refresh_account_chip()
        self._update_vision_status()
        note = self.dataset.disclaimer or ""
        self.status.setText(f"数据源：{self.provider.display}｜{note[:60]}")
        self._refresh_map_packs()
        if self.guide_mode:
            self._refresh_guide_options()
        self._sync_overlay_route()
        self._update_overlay_status()
        self._guide_update_panel()
        if self.ui.get("overlay"):
            self.overlay.show()
            self.overlay.raise_()

    def _make_auto_route(self, layers=None, floor: str = "", limit: int = 120):
        """用当前点位自动排一条顺路路线（没有 routes 文件时的兜底）。"""
        pool = [p for p in self.points if getattr(p, "source", "") != "private"]
        if not pool:
            return None
        done = {p.id for p in pool if self.store.is_done(p.id)}
        wanted = layers or {"chest", "puzzle", "quest", "anchor"}
        pts = auto_route(pool, layers=wanted, floor=floor, limit=limit, exclude_ids=done)
        if not pts:
            pts = auto_route(pool, layers=None, floor=floor, limit=limit, exclude_ids=done)
        if not pts:
            return None
        steps = [RouteStep(seq=i + 1, point_id=p.id, label=p.name,
                           note=(p.note or generic_hint(p.layer)))
                 for i, p in enumerate(pts)]
        pack = self._current_pack()
        key = str(getattr(pack, "key", "root") or "root")
        return Route(id=f"auto-{key}", name=f"自动顺路 · {len(steps)} 站",
                     author="本机自动排序",
                     description="由当前地图包的点位按就近顺序自动生成（最近邻 + 2-opt），只含未完成点位。",
                     tags=["自动"], steps=steps)

    def _current_pack_key(self, packs) -> str:
        imp = self.cfg.data_cfg.get("import_dir")
        if imp:
            target = Path(imp)
            for pack in packs:
                try:
                    if pack.directory.resolve() == target.resolve() and not pack.is_root:
                        return pack.key
                except OSError:
                    pass
        else:
            first = next((p for p in packs if not p.is_root), None)
            if first is not None:
                return first.key
        return "root"

    def _current_pack(self):
        # 向导每帧都要拿 bbox 算距离；discover_map_packs() 是目录 glob，
        # 每帧扫一遍 import/maps 太浪费，按 import_dir 缓存结果。
        key = str(self.cfg.data_cfg.get("import_dir") or "")
        cached = getattr(self, "_pack_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        packs = discover_map_packs()
        pk = self._current_pack_key(packs)
        pack = next((p for p in packs if p.key == pk), None)
        self._pack_cache = (key, pack)
        return pack

    def _refresh_map_packs(self) -> None:
        try:
            packs = discover_map_packs()
        except Exception:
            packs = []
        info = ""
        if self.dataset is not None:
            info = (f"{len(self.points)} 个点位 · {len(self.dataset.regions)} 个区域 · "
                    f"{self.dataset.map_width}×{self.dataset.map_height}")
        key = self._current_pack_key(packs)
        items = [(p.key, p.name) for p in packs]
        self.panel.set_map_packs(items, key, info)
        dlg = getattr(self, "_official_dialog", None)
        if dlg is not None and hasattr(dlg, "set_map_packs"):
            dlg.set_map_packs(items, key)

    def select_map_pack(self, key: str) -> None:
        """切换 import 根目录（地面世界）或 maps/state_* 子地图。"""
        packs = discover_map_packs()
        pack = next((p for p in packs if p.key == key), None)
        if pack is None:
            return
        self.cfg.set("data", "import_dir", None if pack.is_root else str(pack.directory))
        self.cfg.set("data", "points_file", None)
        self.cfg.set("data", "map_image", None)
        self._pack_cache = None
        self.local_provider = self._make_local_provider()
        if isinstance(self.provider, KuroApiProvider):
            self.provider.fallback = self.local_provider
        else:
            self.provider = self.local_provider
        self.active_route = None
        self.waypoint = -1
        self.guide_route_id = None
        self._guide_arrived_step = -1
        self.routes = [r for r in self.routes if not r.id.startswith("guide-")]
        self.canvas.set_route([])
        self.canvas._selected = None
        self.clear_position()
        dlg = getattr(self, "_official_dialog", None)
        if dlg is not None:
            dlg.set_pack(self._current_pack())
        self.load_data()
        self._notify(f"已切换地图：{pack.name}")

    def _open_official_map(self, state: str = "") -> None:
        """应用内官方地图：可最小化，并把本机识图位置叠加到官方地面世界。"""
        try:
            from .official_map import OfficialMapDialog

            pack = self._current_pack()
            pack_state = str(getattr(pack, "state", "") or "") if pack is not None else ""
            initial = state or (pack_state if getattr(pack, "bbox", None) else "")
            dlg = getattr(self, "_official_dialog", None)
            if dlg is None:
                dlg = OfficialMapDialog(self, initial, pack)
                self._official_dialog = dlg
                dlg.finished.connect(self._on_official_closed)
                dlg.vision_toggled.connect(self._toggle_vision)
                dlg.vision_calibrate.connect(self._calibrate_minimap)
                dlg.vision_probe.connect(self._probe_vision)
                dlg.prev_waypoint.connect(lambda: self.step_waypoint(-1))
                dlg.next_waypoint.connect(lambda: self.step_waypoint(+1))
                dlg.complete_waypoint.connect(self._complete_current_waypoint)
                dlg.show_main_requested.connect(self.show_main_window)
                dlg.hide_main_requested.connect(lambda: self.enter_map_only())
                dlg.guide_requested.connect(self.start_guide_ui)
                dlg.overlay_toggled.connect(self._toggle_overlay)
                dlg.map_pack_selected.connect(self.select_map_pack)
                dlg.guide.plan_requested.connect(self._guide_plan)
                dlg.guide.complete_requested.connect(self._guide_complete)
                dlg.guide.skip_requested.connect(self._guide_skip)
                dlg.guide.finish_requested.connect(lambda: self._guide_finish(manual=True))
                dlg.show()
            else:
                dlg.set_pack(pack)
                if state:
                    dlg.load_state(state)
                dlg.showNormal()
                dlg.raise_()
                dlg.activateWindow()
            dlg.set_guide_mode(self.guide_mode)
            dlg.set_overlay_state(bool(self.ui.get("overlay", False)))
            self._refresh_map_packs()
            if self.guide_mode:
                self._refresh_guide_options()
            dlg.set_vision_state(bool(self.cfg.vision.get("enabled", False)))
            self._update_vision_status()
            self._sync_overlay_route()
            self._update_overlay_status()
            if self.canvas._player:
                dlg.set_player(*self.canvas._player,
                               rotation=self.canvas._player_rot)
        except Exception as exc:  # PySide6 未带 QtWebEngine / 初始化失败
            self._open_official_map_external(state)
            self._notify(f"应用内浏览器不可用（{exc}），已改用系统浏览器", warn=True)

    def _on_official_closed(self, _code=0) -> None:
        dlg = getattr(self, "_official_dialog", None)
        self._official_dialog = None
        self.guide_mode = False
        self._guide_auto_plan = False
        if dlg is not None:
            # 对话框以主窗口为 parent，光置 None 不会释放：反复打开会累积
            # QWebEngineProfile/View/Page（多进程 + 显存）。这里显式回收。
            dlg.deleteLater()
        if self._quitting:
            return
        if self.cfg.ui.get("map_only") and not self.isVisible():
            self.show_main_window()

    def enter_map_only(self, state: str = "") -> None:
        """只保留官方浏览器地图：隐藏主窗口，识图/路线操作移到官方窗口工具栏。"""
        self.ui["map_only"] = True
        self.cfg.set("ui", "map_only", True)
        self._open_official_map(state)
        dlg = getattr(self, "_official_dialog", None)
        if dlg is None:
            self.show_main_window()
            self._notify("内置浏览器不可用，已保留主窗口", warn=True)
            return
        self.hide()

    def show_main_window(self) -> None:
        # 仅切换当前视图，不改变“官方浏览器地图为主界面”的启动偏好
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------ 探索向导
    def start_guide_ui(self) -> None:
        """只开官方地图 + 向导面板 + 识图/悬浮指示。"""
        self.guide_mode = True
        self._guide_auto_plan = True
        self.enter_map_only()
        dlg = getattr(self, "_official_dialog", None)
        if dlg is None:
            self.guide_mode = False
            return
        dlg.set_guide_mode(True)
        self._refresh_guide_options()
        dlg.guide.set_status("等待定位；到位后自动规划，也可手动点「根据当前位置规划」")
        if not self.cfg.vision.get("enabled", False):
            self._toggle_vision(True)
        self._toggle_overlay(True)
        self._notify("探索向导已启动：官方地图 + 实时位置 + 顺路路线")

    def _refresh_guide_options(self) -> None:
        dlg = getattr(self, "_official_dialog", None)
        if dlg is None or not hasattr(dlg, "guide"):
            return
        layers = self.layers or []
        if not layers and self.dataset is not None:
            layers = self.dataset.ordered_layers()
        dlg.guide.set_layers(layers)
        floors: list[tuple[str, str]] = []
        seen = set()
        for p in self.points:
            floor = point_floor(p)
            if floor not in seen:
                seen.add(floor)
                floors.append((floor_label(floor), floor))
        if not floors:
            floors = [("地上", "")]
        dlg.guide.set_floors(floors)

    def _guide_active(self) -> bool:
        return bool(self.guide_route_id and self.active_route
                    and self.active_route.id == self.guide_route_id)

    def _guide_current_point(self):
        if not self._guide_active():
            return None
        steps = self.active_route.steps
        if 0 <= self.waypoint < len(steps):
            return next((p for p in self.points if p.id == steps[self.waypoint].point_id), None)
        return None

    def _guide_distance_m(self, point) -> float | None:
        if not self.canvas._player or point is None:
            return None
        pack = self._current_pack()
        bbox = getattr(pack, "bbox", None) if pack is not None else None
        return meters_between((point.x, point.y), self.canvas._player, bbox)

    def _guide_fallback_start(self, layers, floor: str):
        """还没定位时的起点：取候选点位的中位数坐标（抗离群点，比质心稳）。"""
        cand = [p for p in self.points
                if (not layers or p.layer in layers) and point_floor(p) == floor]
        if not cand:
            cand = list(self.points)
        if not cand:
            return (0.5, 0.5)
        xs = sorted(p.x for p in cand)
        ys = sorted(p.y for p in cand)
        return (xs[len(xs) // 2], ys[len(ys) // 2])

    def _guide_plan(self, layers=None, floor: str = "") -> None:
        dlg = getattr(self, "_official_dialog", None)
        if dlg is None:
            return
        dlg.guide.set_planning(True)
        # 没有定位时不再直接拒绝：向导本身仍然可用（路线 + 到站提示），
        # 只是从「当前活动区域中心」起算，并在面板上说清楚。
        start = self.canvas._player
        unlocated = not bool(start)
        if not layers:
            layers = dlg.guide.selected_layers()
        layers = set(layers or ())
        floor = str(floor if floor is not None else "")
        if not layers:
            dlg.guide.set_planning(False)
            dlg.guide.set_status("至少勾选一类点位")
            return
        if unlocated:
            start = self._guide_fallback_start(layers, floor)
        done = {p.id for p in self.points if self.store.is_done(p.id)}
        points = plan_walk(self.points, start, layers=layers, floor=floor,
                           limit=60, radius=0.2, exclude_ids=done)
        if not points:
            dlg.guide.set_planning(False)
            dlg.guide.set_status("附近没有符合条件的未完成点位；换个位置或放宽类型后重试")
            return
        steps = [RouteStep(seq=i + 1, point_id=p.id, label=p.name,
                           note=(p.note or generic_hint(p.layer))) for i, p in enumerate(points)]
        route = Route(id=f"guide-{int(time.time())}",
                      name=f"探索向导 · {floor_label(floor)} · {len(steps)} 站",
                      author="本机识图自动规划",
                      description="从当前位置按最近邻 + 2-opt 生成的连续路线，不回头；仅统计未完成点位。",
                      tags=["向导"], steps=steps)
        self.routes = [r for r in self.routes if not r.id.startswith("guide-")]
        self.routes.append(route)
        self.guide_route_id = route.id
        self.guide_started_at = time.time()
        self.guide_completed = set()
        self.guide_skipped = set()
        self.guide_step_times = {}
        self._guide_arrived_step = -1
        self._guide_auto_plan = False
        self._guide_completing = False
        dlg.cb_follow.setChecked(True)
        dlg.guide.set_planning(False)
        self.load_route(route.id)
        self._guide_update_panel()
        if unlocated:
            dlg.guide.set_status(f"已规划 {len(steps)} 站。当前还没有位置，路线按活动区域中心起算；"
                                 f"识图成功后点「根据当前位置规划」会按你脚下重排。")
        else:
            dlg.guide.set_status(f"已规划 {len(steps)} 站；按顺序前往，到达后在右侧点「完成当前」")
        self._notify(f"探索向导：已规划 {len(steps)} 站，下一站 {points[0].name}")
        self.tray.showMessage("探索向导", f"已规划 {len(steps)} 站；下一站：{points[0].name}")

    def _guide_update_panel(self) -> None:
        dlg = getattr(self, "_official_dialog", None)
        if dlg is None or not hasattr(dlg, "guide"):
            return
        if not self._guide_active():
            if self.guide_mode:
                dlg.guide.set_status("等待规划：定位后自动规划，或点「根据当前位置规划」")
            return
        point = self._guide_current_point()
        steps = self.active_route.steps
        total = len(steps)
        done = sum(1 for s in steps if self.store.is_done(s.point_id))
        if point is not None and self.store.is_done(point.id):
            # 用户从地图右键/侧栏手动标记了当前点：自动推进并播报下一站
            if not self._guide_completing:
                self._guide_complete()
            return
        if point is None:
            dlg.guide.clear_target(f"进度 {done}/{total}")
            return
        floor = floor_label(point_floor(point))
        layer = self.layer_map.get(point.layer)
        meta = " · ".join(x for x in ((layer.label if layer else point.layer),
                                      floor, point.region or "") if x)
        meters = self._guide_distance_m(point)
        arrived = meters is not None and meters <= ARRIVE_METERS
        dist_text = "已到达附近" if arrived else (
            f"距离约 {meters:.0f} m" if meters is not None else "等待定位")
        hint = ((point.note or generic_hint(point.layer))[:300] if arrived
                else "前往目标；到达后显示解谜/获取要点。")
        dlg.guide.set_target(self.waypoint + 1, total, point.name, meta, dist_text, hint, arrived)
        dlg.guide.set_progress(done, total)
        dlg.guide.set_status("向导进行中")
        if arrived and self._guide_arrived_step != self.waypoint:
            self._guide_arrived_step = self.waypoint
            short = (point.note or generic_hint(point.layer))[:80]
            self._notify(f"已到达「{point.name}」：{short}")
            self.tray.showMessage("探索向导 · 到达", f"{point.name}" + chr(10) + short)
            self.overlay.set_status(f"到达 {point.name}：{short}")
        elif not arrived and self._guide_arrived_step == self.waypoint:
            self._guide_arrived_step = -1

    def _guide_announce_next(self, prefix: str = "") -> None:
        point = self._guide_current_point()
        if point is None:
            return
        meters = self._guide_distance_m(point)
        dist = f"，距离约 {meters:.0f} m" if meters is not None else ""
        text = f"{prefix}下一站：{point.name}{dist}"
        self._notify(text)
        self.tray.showMessage("探索向导", text)
        dlg = getattr(self, "_official_dialog", None)
        if dlg is not None:
            dlg.guide.set_status(text[:80])

    def _guide_complete(self) -> None:
        if not self._guide_active() or self._guide_completing:
            return
        point = self._guide_current_point()
        if point is None:
            return
        self._guide_completing = True
        try:
            self.store.set_done(point.id, True)
            self.guide_completed.add(point.id)
            self.guide_step_times[point.id] = time.time()
            self._refresh_progress()
            total = len(self.active_route.steps)
            if self.waypoint >= total - 1:
                self._guide_finish(manual=False)
                return
            self.waypoint += 1
            self._update_waypoint_label()
            self._guide_update_panel()
            self._guide_announce_next(f"已完成「{point.name}」，")
        finally:
            self._guide_completing = False

    def _guide_skip(self) -> None:
        if not self._guide_active():
            return
        point = self._guide_current_point()
        if point is None:
            return
        self.guide_skipped.add(point.id)
        total = len(self.active_route.steps)
        if self.waypoint >= total - 1:
            self._guide_finish(manual=False)
            return
        self.waypoint += 1
        self._update_waypoint_label()
        self._guide_update_panel()
        self._guide_announce_next(f"已跳过「{point.name}」，")

    def _guide_finish(self, manual: bool = False) -> None:
        if not self._guide_active():
            dlg = getattr(self, "_official_dialog", None)
            if dlg is not None:
                dlg.guide.set_status("当前没有进行中的向导")
            return
        route = self.active_route
        steps = route.steps
        total = len(steps)
        done = sum(1 for s in steps if self.store.is_done(s.point_id))
        skipped = sum(1 for s in steps if s.point_id in self.guide_skipped
                      and not self.store.is_done(s.point_id))
        remaining = total - done - skipped
        elapsed = max(0.0, time.time() - self.guide_started_at)
        lines = [
            "# 探索向导完成清单", "",
            f"- 路线：{route.name}",
            f"- 用时：{int(elapsed // 60)} 分 {int(elapsed % 60)} 秒",
            f"- 总数：{total}，完成：{done}，跳过：{skipped}，未完成：{remaining}", "",
            "| # | 状态 | 点位 | 类型 | 分层 | 要点 |",
            "| ---: | --- | --- | --- | --- | --- |",
        ]
        for i, st in enumerate(steps, 1):
            point = next((p for p in self.points if p.id == st.point_id), None)
            status = "完成" if self.store.is_done(st.point_id) else (
                "跳过" if st.point_id in self.guide_skipped else "未完成")
            name = point.name if point else st.label or st.point_id
            layer = point.layer if point else ""
            floor = floor_label(point_floor(point)) if point else "地上"
            note = ((point.note if point else "") or st.note or "").replace("|", "/")[:100]
            lines.append(f"| {i} | {status} | {name} | {layer} | {floor} | {note} |")
        text = chr(10).join(lines)
        path = paths.data_root() / f"guide_summary_{time.strftime('%Y%m%d_%H%M%S')}.md"
        saved = True
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            saved = False
        if saved:
            self._notify(f"向导结束：完成 {done}/{total}，清单已保存到 {path}")
        else:
            self._notify(f"向导结束：完成 {done}/{total}（清单保存失败）", warn=True)
        QMessageBox.information(
            self, "探索向导 · 完成",
            f"完成 {done}/{total}，跳过 {skipped}，未完成 {remaining}。" + chr(10)
            + (f"清单已保存：{chr(10)}{path}" if saved else "清单保存失败，请检查数据目录权限。"))
        self.routes = [r for r in self.routes if not r.id.startswith("guide-")]
        self.active_route = None
        self.waypoint = -1
        self.guide_route_id = None
        self._guide_arrived_step = -1
        self.canvas.set_route([])
        self._refresh_routes()
        self._sync_overlay_route()
        self._update_waypoint_label()
        dlg = getattr(self, "_official_dialog", None)
        if dlg is not None:
            dlg.guide.clear_target(f"已结束 · 完成 {done}/{total}")
            dlg.guide.set_status("向导已结束；可重新点「根据当前位置规划」")
        self._guide_auto_plan = False

    def _open_official_map_external(self, state: str = "") -> None:
        from .official_map import official_map_url

        url = official_map_url(state)
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.information(self, "官方大地图", f"请在浏览器打开：{url}")

    def _layer_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.points:
            out[p.layer] = out.get(p.layer, 0) + 1
        return out

    # ------------------------------------------------------------------ 图层 / 搜索
    def _on_layer_toggled(self, key: str, on: bool) -> None:
        self.cfg.set_layer_visible(key, on)
        visible = {l.key for l in self.layers if self.cfg.layer_visible(l.key)}
        self.canvas.set_layer_visibility(visible)
        self.panel.sync_layer_checks(visible)
        self._update_overlay_status()

    def _on_layers_all(self, on: bool) -> None:
        for l in self.layers:
            self.cfg.set_layer_visible(l.key, on)
        visible = {l.key for l in self.layers if on}
        self.canvas.set_layer_visibility(visible)
        self.panel.sync_layer_checks(visible)
        self._update_overlay_status()

    def _on_search(self, text: str) -> None:
        self._filter_query = text.strip().lower()
        if not self._filter_query:
            self.canvas.set_filter("", None)
            self.panel.clear_search()
            return
        matched = [p for p in self.points if self._match(p)]
        self.canvas.set_filter(text, {p.id for p in matched})
        self.panel.set_search_results(matched, text)

    def _match(self, p: MapPoint) -> bool:
        q = self._filter_query
        layer = self.layer_map.get(p.layer)
        fields = [p.name, p.note, p.author or "", self.store.get_note(p.id),
                  (layer.label if layer else p.layer)] + list(p.drops)
        return any(q in str(f).lower() for f in fields)

    # ------------------------------------------------------------------ 点位交互
    def _select_point(self, point: MapPoint | None) -> None:
        self._selected = point

    def _on_point_clicked(self, point: MapPoint) -> None:
        self._selected = point
        if self.canvas.mark_mode:
            done = self.store.toggle(point.id)
            self._refresh_progress()
            label = "已完成" if done else "未完成"
            self.status.setText(f"{point.name} → {label}"
                                + ("" if not self.sync.cloud_enabled() else "（待回传）"))
        else:
            self.status.setText(f"已选中：{point.name}（{self.layer_map[point.layer].label}）｜"
                                f"再次点击或按 {self._hk('mark_done')} 标记完成")

    def _on_point_hovered(self, point: MapPoint | None) -> None:
        self._hovered = point

    def _on_point_context(self, point: MapPoint, global_pos) -> None:
        menu = QMenu(self)
        done = self.store.is_done(point.id)
        act = QAction("标记为已完成" if not done else "标记为未完成", menu)
        act.triggered.connect(lambda: (self.store.toggle(point.id), self._refresh_progress()))
        menu.addAction(act)
        act_note = QAction("编辑我的备注…", menu)
        act_note.triggered.connect(lambda: self._edit_note(point))
        menu.addAction(act_note)
        if self.active_route:
            act_go = QAction("设为下一站", menu)
            act_go.triggered.connect(lambda: self._set_waypoint_to_point(point))
            menu.addAction(act_go)
        if point.source == "private":
            act_del = QAction("删除私人点位", menu)
            act_del.triggered.connect(lambda: (self.store.remove_private(point.id), self.load_data()))
            menu.addAction(act_del)
        menu.addSeparator()
        act_copy = QAction("复制坐标", menu)
        act_copy.triggered.connect(lambda: QApplication.clipboard().setText(f"{point.x:.4f}, {point.y:.4f}"))
        menu.addAction(act_copy)
        menu.exec(global_pos)

    def _edit_note(self, point: MapPoint) -> None:
        from PySide6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(self, "备注", f"{point.name}", QLineEdit.Normal,
                                        self.store.get_note(point.id) or point.note)
        if ok:
            self.store.set_note(point.id, text.strip())
            self._on_search(self.panel.search.text())

    def _on_background_context(self, x: float, y: float) -> None:
        from PySide6.QtWidgets import QInputDialog

        menu = QMenu(self)
        a_add = QAction("在此新增私人点位…", menu)
        a_add.triggered.connect(lambda: self._add_private_point(x, y))
        menu.addAction(a_add)
        a_pos = QAction("把这里设为我的位置", menu)
        a_pos.triggered.connect(lambda: self._set_position(x, y, source="manual"))
        menu.addAction(a_pos)
        menu.exec(QCursor.pos())

    def _add_private_point(self, x: float, y: float) -> None:
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(self, "新增私人点位", "名称：", QLineEdit.Normal, "我的记号")
        if not ok:
            return
        layer_names = [f"{l.label}" for l in self.layers]
        keys = [l.key for l in self.layers]
        choice, ok2 = QInputDialog.getItem(self, "新增私人点位", "归类：", layer_names, keys.index("quest") if "quest" in keys else 0, False)
        layer = keys[layer_names.index(choice)] if ok2 and choice in layer_names else (keys[0] if keys else "quest")
        note, ok3 = QInputDialog.getText(self, "新增私人点位", "备注（可选）：")
        p = self.store.add_private(name.strip() or "私人点位", x, y, layer, note.strip() if ok3 else "")
        self.points.append(p)
        self.canvas.set_points(self.points, self.layer_map,
                               {l.key for l in self.layers if self.cfg.layer_visible(l.key)})
        self._refresh_progress()
        self._notify(f"已新增私人点位：{p.name}（仅保存在本机）")

    # ------------------------------------------------------------------ 标记 / 手动定位
    def _toggle_selected_done(self) -> None:
        point = getattr(self, "_hovered", None) or getattr(self, "_selected", None)
        if point is None:
            self._notify("先把鼠标移到某个点位上（或点选它），再按标记快捷键", warn=True)
            return
        done = self.store.toggle(point.id)
        self._refresh_progress()
        self.status.setText(f"{point.name} → {'已完成' if done else '未完成'}"
                            + ("（待回传）" if self.sync.cloud_enabled() else "（本机）"))
        if done and self.active_route and self.auto_next:
            idx = self._route_index_of(point.id)
            if idx is not None and idx == self.waypoint:
                self.step_waypoint(+1)

    def _refresh_progress(self) -> None:
        ids = [p.id for p in self.points]
        done, total = self.store.counts(ids)
        self.panel.set_progress(done, total)
        if self.active_route:
            self._update_waypoint_label()
        # 任意来源（画布 / 热键 / 悬浮窗）改了完成状态，都让浮窗清单跟着变
        if getattr(self, "overlay", None) is not None:
            self._refresh_overlay_list()

    def enable_manual_pick(self) -> None:
        self.canvas.set_pick_mode(True)
        self.panel.set_pick_mode(True)
        self._notify("已切换到手动定位：在大图上点击你当前所在位置（下一步就是备用方案）")

    def _on_manual_position(self, x: float, y: float) -> None:
        self.canvas.set_pick_mode(False)
        self.panel.set_pick_mode(False)
        self._set_position(x, y, source="manual")

    def clear_position(self) -> None:
        self.canvas.set_player(None, None)
        self.chip_pos.setText("未定位")
        self.overlay.set_position(None, None)
        dlg = getattr(self, "_official_dialog", None)
        if dlg is not None:
            dlg.clear_player()
        self._update_overlay_status()

    def _set_position(self, x: float, y: float, source: str = "manual",
                      rotation: float | None = None, confidence: float | None = None) -> None:
        self.canvas.set_player(x, y, rotation=rotation, confidence=confidence, source=source)
        tag = "识图" if source == "vision" else "手动"
        conf = "" if confidence is None else f" 置信 {confidence:.0%}"
        self.chip_pos.setText(f"{tag} {x:.3f},{y:.3f}{conf}")
        self.overlay.set_position(x, y, rotation=rotation, confidence=confidence, source=source)
        dlg = getattr(self, "_official_dialog", None)
        if dlg is not None:
            dlg.set_player(x, y, rotation)
        self._update_overlay_status()
        self._guide_update_panel()
        if self.guide_mode and self._guide_auto_plan and not self._guide_active():
            self._guide_auto_plan = False
            panel = getattr(dlg, "guide", None) if dlg is not None else None
            if panel is not None:
                self._guide_plan(panel.selected_layers(),
                                 str(panel.floor.currentData() or ""))
        if self.ui.get("compact") and self.ui.get("minimap_follow", True):
            self.canvas.center_on(x, y)

    # ------------------------------------------------------------------ 路线
    def _refresh_routes(self) -> None:
        routes = self.routes
        if self._route_only_fav:
            fav = set(self.store.favorites())
            routes = [r for r in routes if r.id in fav]
        current = self.active_route.id if self.active_route else None
        self.panel.set_routes(routes, set(self.store.favorites()), current)

    def _on_only_fav(self, on: bool) -> None:
        self._route_only_fav = on
        self._refresh_routes()

    def load_route(self, route_id: str) -> None:
        route = next((r for r in self.routes if r.id == route_id), None)
        if not route:
            return
        if self.guide_route_id and route.id != self.guide_route_id:
            self.guide_route_id = None
            self._guide_auto_plan = False
            dlg = getattr(self, "_official_dialog", None)
            if dlg is not None:
                dlg.guide.clear_target("已切换到普通路线")
        self.active_route = route
        self.waypoint = self._first_incomplete(route)
        pts = self._route_coords(route)
        done = {i for i, p in enumerate(route.steps) if self.store.is_done(p.point_id)}
        self.canvas.set_route(pts, self.waypoint, done)
        self._update_waypoint_label()
        self._refresh_routes()
        self.status.setText(f"已加载路线「{route.name}」·{len(route.steps)} 站（作者 {route.author or '匿名'}）")
        if 0 <= self.waypoint < len(pts):
            self.canvas.center_on(*pts[self.waypoint])

    def _route_coords(self, route: Route) -> list[tuple[float, float]]:
        index = {p.id: p for p in self.points}
        out: list[tuple[float, float]] = []
        for st in route.steps:
            p = index.get(st.point_id)
            if p is None:
                continue
            out.append((p.x, p.y))
        return out

    def _route_index_of(self, point_id: str) -> int | None:
        if not self.active_route:
            return None
        for i, st in enumerate(self.active_route.steps):
            if st.point_id == point_id:
                return i
        return None

    def _first_incomplete(self, route: Route) -> int:
        for i, st in enumerate(route.steps):
            if not self.store.is_done(st.point_id):
                return i
        return 0 if route.steps else -1

    def step_waypoint(self, delta: int) -> None:
        if not self.active_route:
            self._notify("尚未加载路线：在右侧「探索路线」里选一条并点「加载路线」", warn=True)
            return
        n = len(self.active_route.steps)
        if n == 0:
            return
        self.waypoint = (max(0, min(n - 1, self.waypoint + delta)))
        pts = self._route_coords(self.active_route)
        done = {i for i, s in enumerate(self.active_route.steps) if self.store.is_done(s.point_id)}
        self.canvas.set_route(pts, self.waypoint, done)
        self._update_waypoint_label()
        if 0 <= self.waypoint < len(pts):
            self.canvas.center_on(*pts[self.waypoint], scale=max(self.canvas._scale, 1.4))

    def _set_waypoint_to_point(self, point: MapPoint) -> None:
        idx = self._route_index_of(point.id)
        if idx is None:
            self._notify("该点位不在当前路线里", warn=True)
            return
        self.waypoint = idx
        self.step_waypoint(0)

    def _complete_current_waypoint(self) -> None:
        if self._guide_active():
            self._guide_complete()
            return
        if not self.active_route or not (0 <= self.waypoint < len(self.active_route.steps)):
            self._notify("当前没有目标点", warn=True)
            return
        st = self.active_route.steps[self.waypoint]
        if not self.store.is_done(st.point_id):
            self.store.set_done(st.point_id, True)
        self._refresh_progress()
        if self.auto_next:
            self.step_waypoint(+1)
        else:
            self.step_waypoint(0)

    def _update_waypoint_label(self) -> None:
        if not self.active_route:
            self.panel.set_waypoint("未加载路线", 0, 0)
            self._sync_overlay_route()
            self._update_overlay_status()
            return
        steps = self.active_route.steps
        done = sum(1 for s in steps if self.store.is_done(s.point_id))
        if 0 <= self.waypoint < len(steps):
            st = steps[self.waypoint]
            p = next((pp for pp in self.points if pp.id == st.point_id), None)
            name = p.name if p else st.point_id
            dist = ""
            player = self.canvas._player if hasattr(self.canvas, "_player") else None
            if p and player:
                dist = f"｜图上距离 {((p.x - player[0]) ** 2 + (p.y - player[1]) ** 2) ** 0.5:.3f}"
            text = (f"下一站 {self.waypoint + 1}/{len(steps)}：{name}"
                    + (f"｜{st.label}" if st.label else "") + dist)
            self.panel.set_waypoint(text, done, len(steps))
            coords = self._route_coords(self.active_route)
            self.canvas.set_route(coords, self.waypoint,
                                  {i for i, s in enumerate(steps) if self.store.is_done(s.point_id)})
        else:
            self.panel.set_waypoint(f"路线 {done}/{len(steps)} 站已全部标记完成 🎉", done, len(steps))
        self._sync_overlay_route()
        self._update_overlay_status()
        self._guide_update_panel()

    def _toggle_favorite(self, route_id: str) -> None:
        on = self.store.toggle_favorite(route_id)
        self._refresh_routes()
        self.status.setText(f"{'已收藏' if on else '已取消收藏'}该路线，收藏可在面板随时切换加载")

    def _cycle_routes(self) -> None:
        if not self.routes:
            return
        cur = self.active_route.id if self.active_route else None
        ids = [r.id for r in self.routes]
        nxt = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
        self.load_route(nxt)

    # ------------------------------------------------------------------ 账号 / 同步
    def begin_login(self) -> None:
        """登录入口：先讲清流程，再由用户主动打开官方授权页。"""
        from .login_dialog import LoginDialog

        if self.auth.logged_in():
            self._notify("当前已是登录状态；如需换账号请先点「退出登录」。")
            return
        LoginDialog(self.auth, self).exec()

    def _open_authorize_url(self, url: str) -> None:
        if not self.auth.open_browser(url):
            self._notify("无法自动打开浏览器，请复制授权链接手动打开：" + url, warn=True, details=True)
        else:
            self.status.setText("等待官方授权回调…")

    def _on_login_ok(self, info) -> None:
        self.provider = KuroApiProvider(self.api, self.auth.access_token, fallback=self.local_provider)
        self.sync.set_provider(self.provider)
        self.cfg.set("account", "mode", "oauth")
        self.cfg.set("account", "uid_masked", getattr(info, "uid_masked", ""))
        self.cfg.set("account", "nickname", getattr(info, "nickname", ""))
        self._refresh_account_chip()
        self._notify("库街区授权完成，正在拉取云端探索进度…")
        if self.cfg.sync.get("auto_pull_on_login", True):
            self.sync.sync(do_push=False)

    def logout(self) -> None:
        if QMessageBox.question(self, "退出登录",
                                "退出后本工具会立即删除本地保存的授权令牌；\n"
                                "本机标记进度仍会保留。确定退出吗？") != QMessageBox.Yes:
            return
        self.auth.logout()
        self.provider = self.local_provider
        self.sync.set_provider(self.local_provider)
        self.cfg.set("account", "mode", "local")
        self.cfg.set("account", "nickname", "")
        self._refresh_account_chip()
        self._notify("已退出登录，本地凭证已清除，继续以无账号模式使用")

    def _refresh_account_chip(self) -> None:
        logged = self.auth.logged_in()
        cloud = self.sync.cloud_enabled()
        if logged:
            name = self.cfg.account.get("nickname") or "库街区漂泊者"
            self.chip_account.setText(f"已登录 {name}")
            self.chip_account.setObjectName("StatusChip")
            pend = self.store.pending_count()
            self.panel.set_account(True, name, cloud, pend,
                                   "" if cloud else "提示：官方云端标记端点未配置，暂时只能本地保存进度")
        else:
            self.chip_account.setText("无账号模式")
            self.chip_account.setObjectName("StatusChip")
            self.panel.set_account(False, "", False, self.store.pending_count(),
                                   "未登录：全部进度仅保存在本机，地图 / 导航 / 标记功能不受影响")

    def _on_sync_finished(self, report) -> None:
        self._refresh_account_chip()
        self._refresh_progress()
        if report.skipped:
            self.status.setText(report.summary())
            return
        if not report.ok:
            self._notify(report.message or "同步失败", warn=True)
            return
        self.status.setText("同步完成：" + report.summary())

    def _notify_local_mode(self, kind: str) -> None:
        pend = self.store.pending_count()
        msg = ("未登录库街区账号：云端标记无法拉取。" if kind == "pull" else "未登录库街区账号：无法回传云端。")
        self.status.setText(f"{msg} 本机已有 {pend} 条待回传改动（保存在本机）。")

    # ------------------------------------------------------------------ 识图
    def _toggle_vision(self, on: bool) -> None:
        self.cfg.set("vision", "enabled", bool(on))
        self.vision.apply(self.cfg.vision, enabled=bool(on))
        self._update_vision_status()
        dlg = getattr(self, "_official_dialog", None)
        if dlg is not None:
            dlg.set_vision_state(bool(on))

    # ------------------------------------------------------------------ 游戏内指示
    def _toggle_overlay(self, on: bool) -> None:
        on = bool(on)
        self.ui["overlay"] = on
        self.cfg.set("ui", "overlay", on)
        self.btn_overlay.blockSignals(True)
        self.btn_overlay.setChecked(on)
        self.btn_overlay.blockSignals(False)
        dlg = getattr(self, "_official_dialog", None)
        if dlg is not None:
            dlg.set_overlay_state(on)
        if not on:
            self.overlay.hide()
            return
        self.overlay.set_theme(self.ui.get("theme", "dark"))
        self.overlay.set_size(int(self.ui.get("overlay_width", 300)),
                              int(self.ui.get("overlay_height", 300)))
        self.overlay.set_opacity(float(self.ui.get("opacity", 0.96)))
        self.overlay.set_clickable(bool(self.ui.get("overlay_clickable", False)))
        self.overlay.set_map(self.canvas._pix)
        self._sync_overlay_route()
        if self.canvas._player:
            self.overlay.set_position(*self.canvas._player,
                                      rotation=self.canvas._player_rot,
                                      confidence=self.canvas._player_conf,
                                      source=self.canvas._player_src)
        self._update_overlay_status()
        self.overlay.show()
        self.overlay.raise_()
        self._notify("未探索点位浮窗已开启：贴左下角、置顶；Ctrl+Alt+K 可切成可点击；"
                     "游戏需窗口化/无边框才能浮在上层")

    def _sync_overlay_route(self) -> None:
        dlg = getattr(self, "_official_dialog", None)
        if not self.active_route:
            self.overlay.set_route([], -1, set(), "", "")
            if dlg is not None:
                dlg.set_route([], -1)
            return
        steps = self.active_route.steps
        coords = self._route_coords(self.active_route)
        done = {i for i, s in enumerate(steps) if self.store.is_done(s.point_id)}
        label = ""
        if 0 <= self.waypoint < len(steps):
            st = steps[self.waypoint]
            point = next((p for p in self.points if p.id == st.point_id), None)
            label = st.label or (point.name if point else "")
        self.overlay.set_route(coords, self.waypoint, done, label, self.active_route.name)
        if dlg is not None:
            dlg.set_route(coords, self.waypoint, label)

    def _next_route_point(self):
        if not self.active_route or not (0 <= self.waypoint < len(self.active_route.steps)):
            return None
        st = self.active_route.steps[self.waypoint]
        return next((p for p in self.points if p.id == st.point_id), None)

    def _route_distance_m(self) -> float | None:
        point = self._next_route_point()
        if point is None or not self.canvas._player:
            return None
        pack = self._current_pack()
        bbox = getattr(pack, "bbox", None) if pack is not None else None
        return meters_between((point.x, point.y), self.canvas._player, bbox)

    def _update_overlay_status(self) -> None:
        # 官方地图窗口那行仍然显示「距下一站」的距离（它只服务向导）
        dlg_text = "未加载路线"
        dist = self._route_distance_m()
        if self.active_route and 0 <= self.waypoint < len(self.active_route.steps):
            if dist is None:
                dlg_text = "下一站：等待定位"
            elif dist < 20:
                dlg_text = "已到下一站附近"
            elif dist >= 1000:
                dlg_text = f"距下一站约 {dist / 1000:.1f} km"
            else:
                dlg_text = f"距下一站约 {dist:.0f} m"
        dlg = getattr(self, "_official_dialog", None)
        if dlg is not None:
            dlg.set_position_status(dlg_text)

        # 悬浮窗顶部只讲定位状态 + 列表；下一站带正确距离，避免旧像素比例误差
        if self.canvas._player:
            text = "已手动定位" if self.canvas._player_src == "manual" else "位置已获取"
            if self.canvas._player_rot is None:
                # 手动定位/识图没解出朝向：箭头只能按正北向上，必须写明白
                text += " · 北向基准（暂无朝向）"
        else:
            text = "未定位 · 按地图中心起算"
        if self.active_route and 0 <= self.waypoint < len(self.active_route.steps):
            text += f" · 下一站 {self.waypoint + 1}/{len(self.active_route.steps)}"
            if dist is not None:
                text += f"（约 {dist:.0f} m）"
        self.overlay.set_status(text)
        self._refresh_overlay_list()

    def _refresh_overlay_list(self) -> None:
        """把最近 N 个未探索点位算好交给悬浮窗（隐藏图层与已完成在这里过滤）。"""
        origin = self.canvas._player
        unlocated = origin is None
        if unlocated:
            origin = (0.5, 0.5)
        facing = None if unlocated else self.canvas._player_rot
        rows: list[dict] = []
        points = getattr(self, "points", []) or []
        if points:
            visible = {l.key for l in getattr(self, "layers", []) if self.cfg.layer_visible(l.key)}
            pack = self._current_pack()
            bbox = getattr(pack, "bbox", None)
            candidates = []
            for p in points:
                if p.layer not in visible or self.store.is_done(p.id):
                    continue
                candidates.append((meters_between(origin, (p.x, p.y), bbox), p))
            candidates.sort(key=lambda t: t[0])
            try:
                count = int(self.ui.get("overlay_count", 5))
            except (TypeError, ValueError):
                count = 5
            for dist, point in candidates[:min(12, max(3, count))]:
                layer = self.layer_map.get(point.layer)
                bear = (None if dist < 1.0 else
                        relative_bearing(origin, (point.x, point.y),
                                         bbox=bbox, facing=facing)[0])
                floor = floor_label(point_floor(point))
                layer_text = layer.label if layer is not None else point.layer
                rows.append({
                    "id": point.id,
                    "name": point.name,
                    "dist_m": dist,
                    "layer": f"{layer_text} · {floor}",
                    "color": getattr(layer, "color", None) if layer is not None else None,
                    # 未定位时不画箭头：按地图中心算出来的方向没有意义
                    "bearing": None if (unlocated or bear is None) else bear,
                })
        self.overlay.set_unlocated(unlocated)
        self.overlay.set_items(rows)

    def _on_overlay_point_done(self, point_id: str) -> None:
        point_id = str(point_id or "")
        if not point_id or self.store.is_done(point_id):
            return
        self.store.set_done(point_id, True, origin="overlay")
        point = next((p for p in self.points if p.id == point_id), None)
        self._notify(f"悬浮窗已标记完成：{point.name if point else point_id}")
        self._refresh_overlay_list()

    def _toggle_overlay_clickable(self, on: bool | None = None) -> None:
        current = bool(self.ui.get("overlay_clickable", False))
        on = (not current) if on is None else bool(on)
        self.ui["overlay_clickable"] = on
        self.cfg.set("ui", "overlay_clickable", on)
        self.overlay.set_clickable(on)
        if on:
            self._notify("悬浮窗已切到可点击：点条目即标记完成；Ctrl+Alt+K 恢复穿透")
        else:
            self._notify("悬浮窗已恢复鼠标穿透，不会挡住游戏操作")

    def _update_vision_cfg(self, **changes) -> None:
        for k, v in changes.items():
            self.cfg.set("vision", k, v)
        self.vision.apply(self.cfg.vision, enabled=self.cfg.vision.get("enabled", False))
        self._update_vision_status()

    def _on_vision_result(self, res) -> None:
        if res.ok:
            self._set_position(res.x, res.y, source="vision",
                               rotation=res.rotation, confidence=res.confidence)
            self._update_vision_status(res)
        else:
            self._update_vision_status(res)
            self.overlay.note_failure()
            now = time.time()
            if self.vision.fail_streak >= 3 and now - self._vision_fail_hint_at > 25:
                self._vision_fail_hint_at = now
                if not self.canvas.pick_mode:
                    self.enable_manual_pick()

    def _on_vision_availability(self, ok: bool, msg: str) -> None:
        self.vision.available = ok
        self.vision.error = "" if ok else msg
        if not ok:
            self.enable_manual_pick()
            self._notify(msg, warn=True)

    # ------------------------------------------------------------------ 识图校准
    def _calibrate_minimap(self) -> None:
        """框选一次游戏小地图区域，落盘并立刻试识别。

        自动检测只对圆形小地图靠谱；方形、被 HUD 遮挡、或者 UI 布局特殊的玩家
        会一直漏检，随后回退到默认矩形必然裁偏。所以把「框一下小地图」的入口
        直接放到官方地图窗口，一步到位。
        """
        host = getattr(self, "_official_dialog", None) or self
        host.showMinimized()
        QTimer.singleShot(350, lambda: self._do_calibrate_minimap(host))

    def _do_calibrate_minimap(self, host) -> None:
        from PySide6.QtWidgets import QApplication

        from .region_picker import pick_region

        rect = None
        try:
            rect = pick_region()
        finally:
            if host is not None:
                host.showNormal()
                host.raise_()
        if rect is None or rect.width() < 24 or rect.height() < 24:
            self._notify("已取消框选（区域太小或按了 Esc）", warn=True)
            return
        screen = QApplication.primaryScreen()
        size = screen.size() if screen is not None else None
        if size is None or size.width() <= 0 or size.height() <= 0:
            self._notify("拿不到主屏尺寸，无法换算框选区域", warn=True)
            return
        nx = min(max(0.0, rect.x() / size.width()), 1.0)
        ny = min(max(0.0, rect.y() / size.height()), 1.0)
        nw = min(max(0.02, rect.width() / size.width()), 1.0 - nx)
        nh = min(max(0.02, rect.height() / size.height()), 1.0 - ny)
        self.cfg.set("vision", "minimap_rect", [nx, ny, nw, nh])
        self.cfg.set("vision", "auto_detect", False)
        self._update_vision_cfg()
        self._notify(f"小地图区域已校准为 {rect.width()}×{rect.height()}（自动检测已关闭），正在试识别…")
        if not self.cfg.vision.get("enabled", False):
            self._toggle_vision(True)
        QTimer.singleShot(500, self.vision.locate_once)

    def _probe_vision(self) -> None:
        """立刻识别一次，结果与失败细节走正常信号链显示。"""
        if not self.cfg.vision.get("enabled", False):
            self._toggle_vision(True)
        self.vision.locate_once()
        self._notify("已触发一次识图自检，结果会显示在窗口状态里")

    def _update_vision_status(self, res=None) -> None:
        enabled = self.cfg.vision.get("enabled", False)
        if not enabled:
            text = "识图未开启（可选开启；也可一直用手动定位）"
        elif not self.vision.available:
            text = self.vision.error or "识图组件不可用，请用手动定位"
        elif res is None:
            text = f"识图运行中 · 每 {self.cfg.vision.get('interval_ms')}ms"
        elif res.ok:
            text = (f"识图成功 · 置信 {res.confidence:.0%} · {res.elapsed_ms:.0f}ms · "
                    f"{self.vision.stats['ok']}次成功/{self.vision.stats['fail']}次失败\n{ACCURACY_NOTE}")
        else:
            reason = {
                "minimap_not_found": "没在屏幕上识别到游戏小地图",
                "too_few_features": "小地图特征太少（可能被 HUD 遮挡）",
                "low_confidence": "匹配置信度不足",
                "capture_failed": "屏幕截取失败",
                "map_index_missing": "地图底图未加载",
                "dependency_missing": "缺少识图依赖",
            }.get(res.reason, res.reason)
            detail = (getattr(res, "detail", "") or "").strip()
            text = f"识图失败：{reason}（已连续 {self.vision.fail_streak} 次）→ 改用手动定位"
            if detail:
                text += f"\n原因细节：{detail[:180]}"
            text += f"\n{res.accuracy_note}"
        self.panel.set_vision(bool(enabled) and self.vision.available, text, self.vision.available)
        # 默认是「只开官方地图」模式：侧栏根本看不见，失败原因必须同步到官方地图窗口，
        # 否则用户只能看到「位置不动」，无从判断是小地图没找到还是底图不对。
        dlg = getattr(self, "_official_dialog", None)
        if dlg is not None:
            dlg.set_vision_status(text)
        self.chip_vision.setText("识图开" if enabled else "识图关")
        self.chip_vision.setObjectName("WarnChip" if enabled else "StatusChip")
        self.btn_vision.blockSignals(True)
        self.btn_vision.setChecked(bool(enabled))
        self.btn_vision.blockSignals(False)
        self.panel.interval.setValue(int(self.cfg.vision.get("interval_ms", 1500)))
        self.panel.sensitivity.setValue(int(self.cfg.vision.get("sensitivity", 0.55) * 100))

    # ------------------------------------------------------------------ 窗口选项
    def _apply_window_options(self) -> None:
        theme = self.ui.get("theme", "dark")
        QApplication.instance().setStyleSheet(qss(theme))
        self.overlay.set_theme(theme)
        self.canvas.set_theme(theme)
        self.panel.apply_theme(theme)
        self.setWindowOpacity(float(self.ui.get("opacity", 0.96)))
        self.overlay.set_size(int(self.ui.get("overlay_width", 300)),
                              int(self.ui.get("overlay_height", 300)))
        self.overlay.set_opacity(float(self.ui.get("opacity", 0.96)))
        self.overlay.set_clickable(bool(self.ui.get("overlay_clickable", False)))
        self._set_topmost(self.ui.get("always_on_top", True), save=False)
        self.canvas.set_hide_completed(bool(self.ui.get("hide_completed", False)))
        if self.ui.get("lock_size", False):
            self.setFixedSize(self.size())
        if self.ui.get("compact", False):
            self._set_compact(True, save=False)
        geo = self.ui.get("window_geometry")
        if geo and len(geo) == 4:
            self.move(int(geo[0]), int(geo[1]))

    def _set_topmost(self, on: bool, save: bool = True) -> None:
        flags = self.windowFlags() & ~Qt.WindowStaysOnTopHint
        was_visible = self.isVisible()
        if on:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        # setWindowFlags 会把窗口隐藏掉。只有原本可见时才恢复显示：
        # 构造函数里也会走到这里，无条件 show() 会让 --map-only / --guide
        # 启动时先闪一下主窗口，也会把托盘里隐藏的主窗口弹回来。
        if was_visible:
            self.show()
        if save:
            self.cfg.set("ui", "always_on_top", bool(on))
        self.btn_top.blockSignals(True)
        self.btn_top.setChecked(bool(on))
        self.btn_top.blockSignals(False)
        self.status.setText("窗口已置顶（游戏全屏时会被遮挡，建议改用窗口化 / 无边框窗口模式）"
                            if on else "已取消置顶")

    def _set_compact(self, on: bool, save: bool = True) -> None:
        self.panel.setVisible(not on)
        if on:
            self.resize(430, 300)
        elif save:
            geo = self.ui.get("window_geometry")
            if geo and len(geo) == 4:
                self.resize(int(geo[2]), int(geo[3]))
            else:
                self.resize(*self._default_size())
        if save:
            self.cfg.set("ui", "compact", bool(on))
        self.canvas.set_follow(bool(on) and bool(self.ui.get("minimap_follow", True)))
        self.btn_compact.blockSignals(True)
        self.btn_compact.setChecked(bool(on))
        self.btn_compact.blockSignals(False)

    def _set_hide_completed(self, on: bool, save: bool = True) -> None:
        self.canvas.set_hide_completed(on)
        if save:
            self.cfg.set("ui", "hide_completed", bool(on))
        self.panel.hide_done.blockSignals(True)
        self.panel.hide_done.setChecked(on)
        self.panel.hide_done.blockSignals(False)
        self.status.setText("已隐藏全部已完成点位" if on else "已完成点位重新显示（变暗打勾）")

    def _set_mark_mode(self, on: bool, save: bool = True) -> None:
        self.canvas.set_mark_mode(on)
        if save:
            self.cfg.set("ui", "mark_mode", bool(on))
        self.btn_mark.blockSignals(True)
        self.btn_mark.setChecked(on)
        self.btn_mark.blockSignals(False)

    def _cycle_opacity(self) -> None:
        vals = [1.0, 0.9, 0.78, 0.65, 0.5]
        cur = float(self.ui.get("opacity", 1.0))
        nxt = next((v for v in vals if v < cur - 0.02), 1.0)
        self.ui["opacity"] = nxt
        self.cfg.set("ui", "opacity", nxt)
        self.setWindowOpacity(nxt)
        self.overlay.set_opacity(nxt)
        self.status.setText(f"窗口透明度 {int(nxt * 100)}%（点击按钮循环切换，也可在设置里精调）")

    def _toggle_theme(self) -> None:
        theme = "light" if self.ui.get("theme") == "dark" else "dark"
        self.cfg.set("ui", "theme", theme)
        self._apply_window_options()
        self.status.setText(f"已切换到{'浅色' if theme == 'light' else '深色'}主题")

    # ------------------------------------------------------------------ 热键 / 快捷键
    def _hk(self, action: str) -> str:
        from ..core.hotkeys import normalize_combo

        try:
            return normalize_combo(self.cfg.hotkeys.get(action, ""))
        except Exception:
            return "未设置"

    def _on_hotkey(self, action: str) -> None:
        handlers = {
            "toggle_panel": lambda: self._set_panel_visible(not self.panel.isVisible()),
            "mark_done": self._toggle_selected_done,
            "next_waypoint": lambda: self.step_waypoint(+1),
            "prev_waypoint": lambda: self.step_waypoint(-1),
            "toggle_vision": lambda: self._toggle_vision(not self.cfg.vision.get("enabled", False)),
            "toggle_overlay": lambda: self._toggle_overlay(not self.ui.get("overlay", False)),
            "overlay_click": self._toggle_overlay_clickable,
            "next_route": self._cycle_routes,
            "toggle_topmost": lambda: self._set_topmost(not self.ui.get("always_on_top", True)),
        }
        fn = handlers.get(action)
        if fn:
            fn()

    def _set_panel_visible(self, on: bool) -> None:
        self.panel.setVisible(on)
        self.cfg.set("ui", "panel_visible", bool(on))
        if not on:
            self.resize(min(self.width(), 620), self.height())
        elif not self.ui.get("compact"):
            geo = self.ui.get("window_geometry")
            if geo and len(geo) == 4:
                self.resize(int(geo[2]), int(geo[3]))

    def install_local_shortcuts(self) -> None:
        """窗口内快捷键（非 Windows 平台 / 全局热键被占用时的兜底）。"""
        mapping = {
            "toggle_panel": lambda: self._set_panel_visible(not self.panel.isVisible()),
            "mark_done": self._toggle_selected_done,
            "next_waypoint": lambda: self.step_waypoint(+1),
            "prev_waypoint": lambda: self.step_waypoint(-1),
            "toggle_vision": lambda: self._toggle_vision(not self.cfg.vision.get("enabled", False)),
            "toggle_overlay": lambda: self._toggle_overlay(not self.ui.get("overlay", False)),
            "overlay_click": self._toggle_overlay_clickable,
            "next_route": self._cycle_routes,
            "toggle_topmost": lambda: self._set_topmost(not self.ui.get("always_on_top", True)),
        }
        self._local_shortcuts = []
        for action, fn in mapping.items():
            combo = self.cfg.hotkeys.get(action)
            if not combo:
                continue
            sc = QShortcut(QKeySequence(combo.replace("win", "meta")), self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(fn)
            self._local_shortcuts.append(sc)

    # ------------------------------------------------------------------ 设置 / 提示
    def open_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self.api, self, hotkeys=self.hotkeys)
        if dlg.exec():
            self._apply_settings(dlg.result_summary())

    def _apply_settings(self, changes: dict) -> None:
        self.cfg.load()
        self.ui = self.cfg.ui
        self._apply_window_options()
        self.sync.set_conflict(self.cfg.sync.get("conflict", "newer"))
        self.hotkeys.set_bindings(dict(self.cfg.hotkeys))
        if changes.get("data_changed"):
            self.local_provider = self._make_local_provider()
            if not self.sync.cloud_enabled():
                self.provider = self.local_provider
            else:
                self.provider.fallback = self.local_provider
            self.load_data()
        if changes.get("hotkey_failed"):
            self._notify("部分全局快捷键注册失败：\n" + "\n".join(
                f"· {k}：{v}" for k, v in changes["hotkey_failed"].items()), warn=True, details=True)
        self._update_vision_status()
        if getattr(self, "overlay", None) is not None:
            self._update_overlay_status()

    def _notify(self, text: str, warn: bool = False, details: bool = False) -> None:
        self.status.setText(text[:220])
        if details:
            box = QMessageBox(self)
            box.setWindowTitle("库街区探索助手")
            box.setText(text)
            box.setStandardButtons(QMessageBox.Ok)
            box.exec()
        elif warn:
            self.tray.showMessage("提示", text, 1)

    # ------------------------------------------------------------------ 托盘 / 退出
    def _minimize_to_tray(self) -> None:
        self.hide()
        self.tray.show_minimized_hint()

    def _quit(self) -> None:
        self.save_geometry()
        self.shutdown()
        QApplication.instance().quit()

    def save_geometry(self) -> None:
        g = self.geometry()
        self.cfg.set("ui", "window_geometry", [g.x(), g.y(), g.width(), g.height()])

    def shutdown(self) -> None:
        self._quitting = True
        dlg = getattr(self, "_official_dialog", None)
        if dlg is not None:
            dlg.close()
        self.overlay.close()
        self.vision.stop()
        self.sync.shutdown()
        self.hotkeys.release()
        self.tray.disable()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.tray.available and self.cfg.ui.get("close_to_tray", True):
            event.ignore()
            self.hide()
            self.tray.show_minimized_hint()
            return
        self.save_geometry()
        self.shutdown()
        event.accept()
