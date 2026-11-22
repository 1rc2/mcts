"""右侧功能面板：进度、图层、搜索、路线、账号、识图。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QProgressBar,
                               QPushButton, QRadioButton, QScrollArea, QSizePolicy,
                               QSpinBox, QVBoxLayout, QWidget)

from ..core.models import Layer, MapPoint, Route
from ..core.theme import palette
from .official_map import OFFICIAL_MAPS


class CollapsibleGroup(QWidget):
    """点击标题折叠的分组，保持面板信息密度可控。"""

    def __init__(self, title: str, parent: QWidget | None = None, open_: bool = True):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._toggle = QPushButton("▾  " + title, self)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(open_)
        self._toggle.setStyleSheet(
            "QPushButton{background:transparent;border:none;text-align:left;"
            "font-weight:600;padding:2px 4px;}")
        self._body = QWidget(self)
        self.body_layout = QVBoxLayout(self._body)
        self.body_layout.setContentsMargins(2, 0, 2, 2)
        self.body_layout.setSpacing(5)
        self._toggle.toggled.connect(self._body.setVisible)
        self._title = title
        lay.addWidget(self._toggle)
        lay.addWidget(self._body)
        self._body.setVisible(open_)

    def set_title(self, text: str) -> None:
        self._title = text
        prefix = "▾  " if self._toggle.isChecked() else "▸  "
        self._toggle.setText(prefix + text)

    def refresh_prefix(self) -> None:
        self.set_title(self._title)


class SidePanel(QWidget):
    # 地图数据
    map_pack_selected = Signal(str)
    open_official_map = Signal(str)           # 应用内官方原始地图
    open_official_map_external = Signal(str)  # 系统浏览器
    open_official_map_only = Signal(str)      # 只开官方地图（隐藏主窗口）
    start_guide = Signal()                    # 探索向导
    # 图层 / 视图
    layer_toggled = Signal(str, bool)
    layers_all = Signal(bool)
    hide_completed_toggled = Signal(bool)
    mark_mode_toggled = Signal(bool)
    search_changed = Signal(str)
    result_selected = Signal(object)      # MapPoint
    focus_player = Signal()
    reset_view = Signal()
    # 路线
    route_selected = Signal(str)          # 选中（未加载）
    route_load = Signal(str)
    route_favorite = Signal(str)
    routes_only_fav = Signal(bool)
    waypoint_prev = Signal()
    waypoint_next = Signal()
    waypoint_complete = Signal()
    waypoint_auto_next = Signal(bool)
    # 账号
    login_clicked = Signal()
    logout_clicked = Signal()
    sync_pull = Signal()
    sync_push = Signal()
    sync_both = Signal()
    # 识图
    vision_toggled = Signal(bool)
    vision_interval = Signal(int)
    vision_sensitivity = Signal(float)
    manual_pick = Signal()
    clear_position = Signal()

    def __init__(self, parent: QWidget | None = None, theme: str = "dark"):
        super().__init__(parent)
        self.setObjectName("Panel")
        self.setFixedWidth(276)
        self._theme = theme
        self._c = palette(theme)
        self._layers: list[Layer] = []
        self._routes: list[Route] = []
        self._layer_checks: dict[str, QCheckBox] = {}
        self._build()
        self.apply_theme(theme)

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        # 搜索
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索点位 / 材料 / 声骸 / 备注…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        root.addWidget(self.search)
        self.search_info = QLabel("", self)
        self.search_info.setObjectName("Disclaimer")
        root.addWidget(self.search_info)
        self.results = QListWidget(self)
        self.results.setMaximumHeight(148)
        self.results.setVisible(False)
        self.results.itemClicked.connect(lambda it: self.result_selected.emit(it.data(Qt.UserRole)))
        root.addWidget(self.results)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget(scroll)
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # 地图数据（离线包：默认只有地面世界；可放入自定义包）
        g_map = CollapsibleGroup("地图数据", body, True)
        self.map_pack = QComboBox(g_map._body)
        self.map_pack.setToolTip("离线数据包；默认是地面世界（含四大区域）")
        self.map_pack.currentIndexChanged.connect(self._on_map_pack_changed)
        g_map.body_layout.addWidget(self.map_pack)
        self.map_pack_info = QLabel("", g_map._body)
        self.map_pack_info.setObjectName("Disclaimer")
        g_map.body_layout.addWidget(self.map_pack_info)
        bl.addWidget(g_map)

        # 官方原始地图：全部地区/楼层都在这里，避免拼接带来的错位与空白
        g_web = CollapsibleGroup("官方原始地图", body, True)
        tip = QLabel("库街区官方 H5 地图，含全部地区与楼层；不带本机进度。", g_web._body)
        tip.setWordWrap(True)
        tip.setObjectName("Disclaimer")
        g_web.body_layout.addWidget(tip)
        self.official_state = QComboBox(g_web._body)
        for state, name in OFFICIAL_MAPS:
            self.official_state.addItem(name, state)
        g_web.body_layout.addWidget(self.official_state)
        row = QHBoxLayout()
        btn_app = QPushButton("应用内打开", g_web._body)
        btn_app.setToolTip("用内置浏览器加载官方原始页面；失败会提示改用系统浏览器")
        btn_app.clicked.connect(lambda: self.open_official_map.emit(
            str(self.official_state.currentData() or "")))
        btn_web = QPushButton("浏览器打开", g_web._body)
        btn_web.clicked.connect(lambda: self.open_official_map_external.emit(
            str(self.official_state.currentData() or "")))
        row.addWidget(btn_app)
        row.addWidget(btn_web)
        g_web.body_layout.addLayout(row)
        btn_only = QPushButton("只开官方地图（隐藏主窗口）", g_web._body)
        btn_only.setToolTip("把官方地图当成主界面；识图开关、上一站/下一站/完成都在官方窗口工具栏")
        btn_only.clicked.connect(lambda: self.open_official_map_only.emit(
            str(self.official_state.currentData() or "")))
        g_web.body_layout.addWidget(btn_only)
        btn_guide = QPushButton("探索向导（自动规划顺路路线）", g_web._body)
        btn_guide.setToolTip("只开官方地图并开启识图；根据当前位置规划连续路线，到点提示要点")
        btn_guide.clicked.connect(self.start_guide.emit)
        g_web.body_layout.addWidget(btn_guide)
        bl.addWidget(g_web)

        # 进度
        g_progress = CollapsibleGroup("探索进度", body, True)
        self.progress = QProgressBar(g_progress._body)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        g_progress.body_layout.addWidget(self.progress)
        self.progress_label = QLabel("已完成 0 / 0", g_progress._body)
        self.progress_label.setObjectName("ProgressValue")
        g_progress.body_layout.addWidget(self.progress_label)
        row = QHBoxLayout()
        self.hide_done = QCheckBox("隐藏已完成点位", g_progress._body)
        self.hide_done.toggled.connect(self.hide_completed_toggled.emit)
        g_progress.body_layout.addWidget(self.hide_done)
        row2 = QHBoxLayout()
        self.mark_mode = QCheckBox("点击即标记模式", g_progress._body)
        self.mark_mode.setToolTip("开启后单击地图点位直接切换完成 / 未完成")
        self.mark_mode.toggled.connect(self.mark_mode_toggled.emit)
        row2.addWidget(self.mark_mode)
        btn_focus = QPushButton("定位到我", g_progress._body)
        btn_focus.clicked.connect(self.focus_player.emit)
        btn_reset = QPushButton("复位视图", g_progress._body)
        btn_reset.clicked.connect(self.reset_view.emit)
        row2.addWidget(btn_focus)
        row2.addWidget(btn_reset)
        g_progress.body_layout.addLayout(row2)
        bl.addWidget(g_progress)

        # 图层
        g_layers = CollapsibleGroup("点位图层", body, True)
        top = QHBoxLayout()
        b_all = QPushButton("一键全显", g_layers._body)
        b_all.clicked.connect(lambda: self._all_layers(True))
        b_none = QPushButton("一键隐藏", g_layers._body)
        b_none.clicked.connect(lambda: self._all_layers(False))
        top.addWidget(b_all)
        top.addWidget(b_none)
        g_layers.body_layout.addLayout(top)
        self.layer_host = QWidget(g_layers._body)
        self.layer_layout = QVBoxLayout(self.layer_host)
        self.layer_layout.setContentsMargins(0, 0, 0, 0)
        self.layer_layout.setSpacing(3)
        g_layers.body_layout.addWidget(self.layer_host)
        self.layer_total_label = QLabel("", g_layers._body)
        self.layer_total_label.setObjectName("Disclaimer")
        g_layers.body_layout.addWidget(self.layer_total_label)
        bl.addWidget(g_layers)

        # 路线
        g_routes = CollapsibleGroup("探索路线 · 顺序导航", body, True)
        self.only_fav = QCheckBox("只看收藏", g_routes._body)
        self.only_fav.toggled.connect(self.routes_only_fav.emit)
        g_routes.body_layout.addWidget(self.only_fav)
        self.route_list = QListWidget(g_routes._body)
        self.route_list.setMaximumHeight(132)
        self.route_list.itemClicked.connect(self._on_route_clicked)
        g_routes.body_layout.addWidget(self.route_list)
        lb = QHBoxLayout()
        b_load = QPushButton("加载路线", g_routes._body)
        b_load.clicked.connect(lambda: self._load_clicked())
        b_fav = QPushButton("☆ 收藏", g_routes._body)
        b_fav.clicked.connect(self._on_fav_clicked)
        lb.addWidget(b_load)
        lb.addWidget(b_fav)
        g_routes.body_layout.addLayout(lb)
        self.route_info = QLabel("未加载路线", g_routes._body)
        self.route_info.setWordWrap(True)
        self.route_info.setObjectName("Disclaimer")
        g_routes.body_layout.addWidget(self.route_info)
        nav = QHBoxLayout()
        b_prev = QPushButton("◀ 上一个", g_routes._body)
        b_prev.clicked.connect(self.waypoint_prev.emit)
        b_next = QPushButton("下一个 ▶", g_routes._body)
        b_next.clicked.connect(self.waypoint_next.emit)
        nav.addWidget(b_prev)
        nav.addWidget(b_next)
        g_routes.body_layout.addLayout(nav)
        self.waypoint_label = QLabel("—", g_routes._body)
        self.waypoint_label.setWordWrap(True)
        g_routes.body_layout.addWidget(self.waypoint_label)
        b_done = QPushButton("✓ 完成当前目标点", g_routes._body)
        b_done.clicked.connect(self.waypoint_complete.emit)
        g_routes.body_layout.addWidget(b_done)
        self.auto_next = QCheckBox("标记完成后自动跳到下一站", g_routes._body)
        self.auto_next.setChecked(True)
        self.auto_next.toggled.connect(self.waypoint_auto_next.emit)
        g_routes.body_layout.addWidget(self.auto_next)
        bl.addWidget(g_routes)

        # 账号
        g_account = CollapsibleGroup("库街区账号", body, True)
        g_accounts = g_account._body
        self.account_label = QLabel("未登录 · 无账号模式", g_accounts)
        self.account_label.setWordWrap(True)
        g_account.body_layout.addWidget(self.account_label)
        row_a = QHBoxLayout()
        self.btn_login = QPushButton("官方授权登录", g_accounts)
        self.btn_login.clicked.connect(self.login_clicked.emit)
        self.btn_logout = QPushButton("退出登录", g_accounts)
        self.btn_logout.clicked.connect(self.logout_clicked.emit)
        self.btn_logout.setEnabled(False)
        row_a.addWidget(self.btn_login)
        row_a.addWidget(self.btn_logout)
        g_account.body_layout.addLayout(row_a)
        row_s = QHBoxLayout()
        b_pull = QPushButton("拉取云端", g_accounts)
        b_pull.clicked.connect(self.sync_pull.emit)
        b_push = QPushButton("回传云端", g_accounts)
        b_push.clicked.connect(self.sync_push.emit)
        b_both = QPushButton("双向同步", g_accounts)
        b_both.clicked.connect(self.sync_both.emit)
        row_s.addWidget(b_pull)
        row_s.addWidget(b_push)
        row_s.addWidget(b_both)
        g_account.body_layout.addLayout(row_s)
        self.sync_label = QLabel("本地进度不会离开本机", g_accounts)
        self.sync_label.setWordWrap(True)
        self.sync_label.setObjectName("Disclaimer")
        g_account.body_layout.addWidget(self.sync_label)
        bl.addWidget(g_account)

        # 识图
        g_vision = CollapsibleGroup("识图定位", body, True)
        self.vision_enabled = QCheckBox("开启屏幕识图", g_vision._body)
        self.vision_enabled.toggled.connect(self.vision_toggled.emit)
        g_vision.body_layout.addWidget(self.vision_enabled)
        fr = QHBoxLayout()
        fr.addWidget(QLabel("识别间隔", g_vision._body))
        self.interval = QSpinBox(g_vision._body)
        self.interval.setRange(500, 10000)
        self.interval.setSingleStep(250)
        self.interval.setSuffix(" ms")
        self.interval.setValue(1500)
        self.interval.valueChanged.connect(self.vision_interval.emit)
        fr.addWidget(self.interval, 1)
        g_vision.body_layout.addLayout(fr)
        sr = QHBoxLayout()
        sr.addWidget(QLabel("严格度", g_vision._body))
        self.sensitivity = QSpinBox(g_vision._body)
        self.sensitivity.setRange(10, 95)
        self.sensitivity.setSuffix("")
        self.sensitivity.setValue(55)
        self.sensitivity.valueChanged.connect(lambda v: self.vision_sensitivity.emit(v / 100.0))
        sr.addWidget(self.sensitivity, 1)
        g_vision.body_layout.addLayout(sr)
        self.vision_status = QLabel("识图未开启", g_vision._body)
        self.vision_status.setWordWrap(True)
        g_vision.body_layout.addWidget(self.vision_status)
        row_m = QHBoxLayout()
        b_manual = QPushButton("手动标记我的位置", g_vision._body)
        b_manual.setToolTip("识别失败时的备用方案：点击后在大图上点一下自己的位置")
        b_manual.clicked.connect(self.manual_pick.emit)
        b_clear = QPushButton("清除位置", g_vision._body)
        b_clear.clicked.connect(self.clear_position.emit)
        row_m.addWidget(b_manual)
        row_m.addWidget(b_clear)
        g_vision.body_layout.addLayout(row_m)
        note = QLabel("识图仅读取屏幕像素，存在误差，仅作探索参考。", g_vision._body)
        note.setWordWrap(True)
        note.setObjectName("Disclaimer")
        g_vision.body_layout.addWidget(note)
        bl.addWidget(g_vision)
        bl.addStretch(1)

        for grp in (g_progress, g_layers, g_routes, g_account, g_vision):
            grp._toggle.toggled.connect(grp.refresh_prefix)

    # ------------------------------------------------------------------ 交互
    def _on_search(self, text: str) -> None:
        self.search_changed.emit(text)

    def _all_layers(self, on: bool) -> None:
        for key, chk in self._layer_checks.items():
            chk.blockSignals(True)
            chk.setChecked(on)
            chk.blockSignals(False)
        self.layers_all.emit(on)

    def _on_route_clicked(self, item: QListWidgetItem) -> None:
        rid = item.data(Qt.UserRole)
        if rid:
            self.route_selected.emit(rid)

    def _load_clicked(self) -> None:
        item = self.route_list.currentItem()
        if item and item.data(Qt.UserRole):
            self.route_load.emit(item.data(Qt.UserRole))

    def _on_fav_clicked(self) -> None:
        item = self.route_list.currentItem()
        if item and item.data(Qt.UserRole):
            self.route_favorite.emit(item.data(Qt.UserRole))

    # ------------------------------------------------------------------ 更新
    def set_map_packs(self, packs: list[tuple[str, str]], current_key: str, info: str = "") -> None:
        """packs = [(key, 展示名)]；current_key 用于选中当前地图。"""
        self.map_pack.blockSignals(True)
        self.map_pack.clear()
        for key, name in packs:
            self.map_pack.addItem(name, key)
        if packs:
            idx = self.map_pack.findData(current_key)
            self.map_pack.setCurrentIndex(idx if idx >= 0 else 0)
            self.map_pack.setEnabled(True)
        else:
            self.map_pack.addItem("内置示例数据")
            self.map_pack.setEnabled(False)
        self.map_pack.blockSignals(False)
        self.map_pack.setVisible(len(packs) > 1)
        self.map_pack_info.setText(info)

    def _on_map_pack_changed(self, index: int) -> None:
        key = self.map_pack.itemData(index)
        if key:
            self.map_pack_selected.emit(str(key))

    def set_layers(self, layers: list[Layer], counts: dict[str, int],
                   visible: set[str]) -> None:
        self._layers = layers
        host = self.layer_host
        while self.layer_layout.count():
            item = self.layer_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self._layer_checks = {}
        for layer in layers:
            row_widget = QWidget(host)
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(5)
            chk = QCheckBox(layer.label, row_widget)
            chk.setChecked(layer.key in visible)
            chk.toggled.connect(lambda on, k=layer.key: self.layer_toggled.emit(k, on))
            dot = QLabel(row_widget)
            dot.setFixedSize(12, 12)
            dot.setStyleSheet(
                f"background:{layer.color};border-radius:5px;border:1px solid rgba(0,0,0,0.45);")
            cnt = QLabel(str(counts.get(layer.key, 0)), row_widget)
            cnt.setObjectName("Disclaimer")
            row.addWidget(dot)
            row.addWidget(chk, 1)
            row.addWidget(cnt)
            self.layer_layout.addWidget(row_widget)
            self._layer_checks[layer.key] = chk
        self.layer_total_label.setText(f"共 {sum(counts.values())} 个点位")

    def sync_layer_checks(self, visible: set[str]) -> None:
        for key, chk in self._layer_checks.items():
            chk.blockSignals(True)
            chk.setChecked(key in visible)
            chk.blockSignals(False)

    def set_progress(self, done: int, total: int) -> None:
        pct = int(done * 100 / total) if total else 0
        self.progress.setValue(pct)
        self.progress_label.setText(f"已完成 {done} / {total}")

    def set_search_results(self, points: list[MapPoint], text: str) -> None:
        self.results.clear()
        if not text.strip():
            self.results.setVisible(False)
            self.search_info.setText("")
            return
        shown = points[:120]
        for p in shown:
            layer = next((l for l in self._layers if l.key == p.layer), None)
            label = layer.label if layer else p.layer
            it = QListWidgetItem(f"{label} · {p.name}")
            it.setData(Qt.UserRole, p)
            tip = p.note or ""
            it.setToolTip(f"{p.name}\n{', '.join(p.drops[:4])}\n{tip}")
            self.results.addItem(it)
        self.results.setVisible(bool(shown))
        self.search_info.setText(f"匹配 {len(points)} 个点位，显示前 {len(shown)} 个")

    def clear_search(self) -> None:
        self.results.clear()
        self.results.setVisible(False)
        self.search_info.setText("")

    def set_routes(self, routes: list[Route], favorites: set[str],
                   current_id: str | None) -> None:
        self._routes = routes
        self.route_list.clear()
        for r in routes:
            star = "★" if r.id in favorites else "☆"
            it = QListWidgetItem(f"{star} {r.name}   ·{len(r.steps)}站")
            it.setData(Qt.UserRole, r.id)
            it.setToolTip(f"{r.description}\n作者：{r.author or '匿名'}"
                          + (f"\n预计 {r.est_minutes} 分钟" if r.est_minutes else ""))
            self.route_list.addItem(it)
            if r.id == current_id:
                self.route_list.setCurrentItem(it)
                it.setText(f"{star} {r.name}   ·{len(r.steps)}站  ✔已加载")
        self.route_info.setText(
            f"共 {len(routes)} 条分享路线，收藏 {len(favorites)} 条" if routes else "暂无路线数据")

    def set_waypoint(self, text: str, done_steps: int, total_steps: int) -> None:
        self.waypoint_label.setText(text)
        if total_steps:
            self.route_info.setText(f"当前路线进度 {done_steps} / {total_steps} 站")

    def set_account(self, logged_in: bool, nickname: str, cloud_ready: bool,
                    pending: int, mode_text: str) -> None:
        self.btn_logout.setEnabled(logged_in)
        self.btn_login.setEnabled(not logged_in)
        for b in (self.findChild(QPushButton, None) or [],):
            pass
        if logged_in:
            self.account_label.setText(f"已登录：{nickname}" + ("（云端已就绪）" if cloud_ready else "（云端端点未配置）"))
        else:
            self.account_label.setText("未登录 · 无账号模式")
        self.sync_label.setText(
            f"本机待回传 {pending} 条改动" if pending else "本机没有待回传的改动")
        if mode_text:
            self.sync_label.setText(self.sync_label.text() + f"\n{mode_text}")

    def set_vision(self, enabled: bool, status_text: str, available: bool = True) -> None:
        self.vision_enabled.blockSignals(True)
        self.vision_enabled.setChecked(enabled)
        self.vision_enabled.blockSignals(False)
        self.vision_status.setText(status_text if available else "识图组件不可用（缺少 OpenCV / 截屏依赖），请用手动定位")
        self.interval.setEnabled(enabled)
        self.sensitivity.setEnabled(enabled)

    def set_pick_mode(self, on: bool) -> None:
        self.vision_status.setText("请在地图上点击你当前所在的位置…" if on else self.vision_status.text())

    def set_hide_completed(self, on: bool) -> None:
        self.hide_done.blockSignals(True)
        self.hide_done.setChecked(on)
        self.hide_done.blockSignals(False)

    def set_filter_text(self, text: str) -> None:
        self.search.blockSignals(True)
        self.search.setText(text)
        self.search.blockSignals(False)

    def apply_theme(self, theme: str) -> None:
        self._theme = theme
        self._c = palette(theme)
        for grp in self.findChildren(CollapsibleGroup):
            grp.refresh_prefix()
