"""官方 H5 大地图窗口：加载库街区原始页面，并把本机识图位置画到官方地图上。

说明：
- 页面本身用 Leaflet；脚本在页面加载后读取其 mapInstance，添加本机标记、路线和下一站；
- 坐标换算基于官方 state=8 的 30×31 瓦片网格：lat=(16-ny*31)*T，lng=(nx*30-12)*T；
- nx/ny 是相对 6144 总图的归一化坐标，由当前国家包的 bbox 把本机坐标还原；
- 只读屏幕像素/本地路线，不读取账号数据，不调用官方私有接口。

QtWebEngine 是可选依赖；不可用时调用方退回系统浏览器。
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout)

from .guide_panel import GuidePanel

OFFICIAL_MAPS = [
    ("", "全部地区（默认视图）"),
    ("8", "地面世界（瑝珑 / 黎那汐塔 / 黑海岸 / 罗伊冰原）"),
    ("906", "蚀刻平原（罗伊冰原）"),
    ("909", "落日堤屿 / 封存地 / 恒黯之原"),
    ("900", "泰缇斯之底（黑海岸地下）"),
    ("910", "时隙废都"),
    ("912", "梦枢天罗（瑝珑）"),
    ("902", "下层金库（黎那汐塔）"),
    ("903", "阿维纽林（黎那汐塔）"),
    ("905", "隐海试验场（黎那汐塔）"),
]

COUNTRY_BY_STATE = {
    "900": "900", "902": "3", "903": "3", "905": "3",
    "906": "4", "909": "4", "910": "900", "912": "1",
}

# 注入官方页面：找到 Leaflet mapInstance，维护本机标记 / 路线 / 下一站
HELPER_JS = r"""
(function () {
  if (window.__kuroHelperInstalled) return;
  window.__kuroHelperInstalled = true;
  window.__kuroHelperReady = false;
  window.__kuroHelperPending = null;

  function tileW() { return window.__TILE_IAMGE_WIDTH__ || 1024; }
  function latlng(x, y) {
    const T = tileW();
    return [(16 - y * 31) * T, (x * 30 - 12) * T];
  }
  function findMap() {
    try {
      let el = document.querySelector('#app') || document.querySelector('[data-v-app]');
      let app = el && el.__vue_app__;
      if (!app) {
        const nodes = document.querySelectorAll('body *');
        for (let i = 0; i < nodes.length && i < 4000; i++) {
          if (nodes[i].__vue_app__) { app = nodes[i].__vue_app__; break; }
        }
      }
      const pinia = app && app.config && app.config.globalProperties
        && app.config.globalProperties.$pinia;
      const stores = pinia && pinia._s;
      if (stores) {
        const iter = stores.values ? stores.values() : Object.values(stores);
        let best = null;
        for (const s of iter) {
          const m = s && s.mapInstance;
          if (!m || typeof m.panTo !== 'function'
              || typeof m.latLngToContainerPoint !== 'function') continue;
          let area = 0;
          try {
            const c = m.getContainer && m.getContainer();
            area = c ? (c.clientWidth || 0) * (c.clientHeight || 0) : 0;
          } catch (e) {}
          if (!best || area > best.area) best = {map: m, area: area};
        }
        if (best) return best.map;
      }
      for (const k of Object.keys(window)) {
        try {
          const v = window[k];
          if (v && typeof v.panTo === 'function'
              && typeof v.latLngToContainerPoint === 'function' && v._container) {
            return v;
          }
        } catch (e) {}
      }
    } catch (e) {}
    return null;
  }

  let map = null, marker = null, line = null, next = null;

  function ensure() {
    if (!window.L) { setTimeout(ensure, 400); return; }
    map = findMap();
    if (!map) { setTimeout(ensure, 400); return; }
    window.__kuroHelperReady = true;
    const L = window.L;
    if (!marker) {
      const icon = L.divIcon({
        className: 'kuro-helper-marker',
        html: '<div style="width:20px;height:20px;border-radius:50%;'
            + 'background:#4fd1c5;border:3px solid #fff;'
            + 'box-shadow:0 0 10px rgba(0,0,0,.85)"></div>',
        iconSize: [20, 20], iconAnchor: [10, 10]
      });
      marker = L.marker([0, 0], {icon: icon, interactive: false,
                                 zIndexOffset: 10000, opacity: 0}).addTo(map);
      line = L.polyline([], {color: '#4fd1c5', weight: 3, dashArray: '6 6',
                             opacity: 0, interactive: false}).addTo(map);
      next = L.circleMarker([0, 0], {radius: 7, color: '#fff', weight: 2,
                                     fillColor: '#4fd1c5', fillOpacity: 0,
                                     opacity: 0, interactive: false}).addTo(map);
      next.bindTooltip('', {direction: 'top'});
    }
    if (window.__kuroHelperPending) {
      const p = window.__kuroHelperPending;
      window.__kuroHelperPending = null;
      window.__kuroHelperApply(p);
    }
  }

  window.__kuroHelperApply = function (p) {
    if (!window.__kuroHelperReady || !marker) {
      window.__kuroHelperPending = p;
      return;
    }
    const pos = latlng(p.x, p.y);
    marker.setLatLng(pos);
    marker.setOpacity(1);
    const pts = (p.route || []).map(function (q) { return latlng(q[0], q[1]); });
    line.setLatLngs(pts);
    line.setStyle({opacity: pts.length > 1 ? 0.9 : 0});
    if (pts.length && p.index >= 0 && p.index < pts.length) {
      next.setLatLng(pts[p.index]);
      next.setStyle({opacity: 1, fillOpacity: 1});
      next.setTooltipContent(p.label || '下一站');
    } else {
      next.setStyle({opacity: 0, fillOpacity: 0});
    }
    window.__kuroHelperLast = pos;
    if (p.follow) map.panTo(pos, {animate: true, duration: 0.4});
  };

  window.__kuroHelperClear = function () {
    if (marker) marker.setOpacity(0);
    if (line) line.setStyle({opacity: 0});
    if (next) next.setStyle({opacity: 0, fillOpacity: 0});
    window.__kuroHelperPending = null;
  };

  window.__kuroHelperCenter = function () {
    if (map && window.__kuroHelperLast) {
      map.panTo(window.__kuroHelperLast, {animate: true, duration: 0.4});
    }
  };

  window.__kuroHelperDebug = function () {
    if (!marker) return null;
    const ll = marker.getLatLng();
    let center = null, zoom = null;
    try {
      const c = map.getCenter();
      center = [c.lat, c.lng];
      zoom = map.getZoom();
    } catch (e) {}
    return {lat: ll.lat, lng: ll.lng, route: (line && line.getLatLngs().length) || 0,
            markerEl: !!document.querySelector('.kuro-helper-marker'),
            center: center, zoom: zoom};
  };

  ensure();
})();
"""


def to_full_coords(bbox, x: float, y: float):
    """国家包局部归一化坐标 -> state 8 总图归一化坐标。"""
    if not bbox:
        return None
    x0, y0, x1, y1 = bbox
    return (x0 + float(x) * (x1 - x0), y0 + float(y) * (y1 - y0))


def official_map_url(state: str = "", country: str = "") -> str:
    url = "https://www.kurobbs.com/mc/map/"
    if state or country:
        params = []
        if state:
            params.append(f"state={state}")
        c = country or COUNTRY_BY_STATE.get(str(state), "")
        if c:
            params.append(f"country={c}")
        if params:
            url += "?" + "&".join(params)
    return url


class OfficialMapDialog(QDialog):
    """内嵌官方原始地图；可最小化，位置/路线由本机识图结果驱动。"""

    vision_toggled = Signal(bool)
    vision_calibrate = Signal()
    vision_probe = Signal()
    prev_waypoint = Signal()
    next_waypoint = Signal()
    complete_waypoint = Signal()
    show_main_requested = Signal()
    hide_main_requested = Signal()
    guide_requested = Signal()
    overlay_toggled = Signal(bool)
    map_pack_selected = Signal(str)

    def __init__(self, parent=None, state: str = "", pack=None):
        super().__init__(parent)
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView

        from ..core import paths

        self._pack = pack
        self._pos = None
        self._route: list[tuple[float, float]] = []
        self._index = -1
        self._label = ""
        self._page_ready = False
        self._checks = 0

        self.setWindowTitle("官方大地图（原始页面 + 本机位置）")
        self.setWindowFlags(Qt.Window | Qt.WindowMinimizeButtonHint |
                            Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint)
        self.setWindowModality(Qt.NonModal)
        self.resize(1280, 860)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("官方地区", self))
        self.combo = QComboBox(self)
        for st, name in OFFICIAL_MAPS:
            self.combo.addItem(name, st)
        row.addWidget(self.combo, 1)
        btn_open = QPushButton("打开", self)
        btn_open.clicked.connect(self._reload)
        row.addWidget(btn_open)
        btn_ext = QPushButton("在浏览器打开", self)
        btn_ext.clicked.connect(self._open_external)
        row.addWidget(btn_ext)
        btn_min = QPushButton("最小化", self)
        btn_min.clicked.connect(self.showMinimized)
        row.addWidget(btn_min)
        lay.addLayout(row)

        row_pack = QHBoxLayout()
        row_pack.addWidget(QLabel("本机地图", self))
        self.pack_combo = QComboBox(self)
        self.pack_combo.setToolTip("决定浮窗/向导使用的离线点位包；官方地区切换只影响官方页面")
        self.pack_combo.currentIndexChanged.connect(self._on_pack_changed)
        row_pack.addWidget(self.pack_combo, 1)
        lay.addLayout(row_pack)

        row2 = QHBoxLayout()
        btn_guide = QPushButton("向导", self)
        btn_guide.setToolTip("打开探索向导面板：自动规划顺路路线、到点提示、完成推进")
        btn_guide.clicked.connect(self.guide_requested.emit)
        row2.addWidget(btn_guide)
        self.cb_detect = QCheckBox("识图", self)
        self.cb_detect.setToolTip("开启/关闭屏幕识图定位；和主窗口的识图开关同步")
        self.cb_detect.toggled.connect(self.vision_toggled.emit)
        row2.addWidget(self.cb_detect)
        btn_calib = QPushButton("校准小地图", self)
        btn_calib.setToolTip("在屏幕上框出游戏小地图区域；自动检测失败时用这个")
        btn_calib.clicked.connect(self.vision_calibrate.emit)
        row2.addWidget(btn_calib)
        btn_probe = QPushButton("识图自检", self)
        btn_probe.setToolTip("立刻识别一次，并把成功坐标或失败原因写在下方")
        btn_probe.clicked.connect(self.vision_probe.emit)
        row2.addWidget(btn_probe)
        self.btn_prev = QPushButton("◀ 上一站", self)
        self.btn_prev.clicked.connect(self.prev_waypoint.emit)
        row2.addWidget(self.btn_prev)
        self.btn_next = QPushButton("下一站 ▶", self)
        self.btn_next.clicked.connect(self.next_waypoint.emit)
        row2.addWidget(self.btn_next)
        self.btn_done = QPushButton("✓ 完成", self)
        self.btn_done.clicked.connect(self.complete_waypoint.emit)
        row2.addWidget(self.btn_done)
        row2.addStretch(1)
        btn_main = QPushButton("主窗口", self)
        btn_main.clicked.connect(self.show_main_requested.emit)
        row2.addWidget(btn_main)
        btn_only = QPushButton("只留地图", self)
        btn_only.setToolTip("隐藏主窗口，只保留官方地图；识图与路线操作仍在本窗口")
        btn_only.clicked.connect(self.hide_main_requested.emit)
        row2.addWidget(btn_only)
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        self.cb_overlay = QCheckBox("浮窗", self)
        self.cb_overlay.setToolTip("开启左下角未探索点位浮窗；Ctrl+Alt+K 切换鼠标穿透/可点击")
        self.cb_overlay.toggled.connect(self.overlay_toggled.emit)
        row3.addWidget(self.cb_overlay)
        self.cb_sync = QCheckBox("显示我的位置", self)
        self.cb_sync.setChecked(True)
        self.cb_sync.setToolTip("只在官方地面世界（state 8）生效；需要先开启识图或手动定位")
        self.cb_sync.toggled.connect(self._apply)
        row3.addWidget(self.cb_sync)
        self.cb_follow = QCheckBox("跟随", self)
        self.cb_follow.setToolTip("位置更新时自动把官方地图移到你身上")
        self.cb_follow.toggled.connect(self._apply)
        row3.addWidget(self.cb_follow)
        btn_center = QPushButton("定位", self)
        btn_center.clicked.connect(self.center_on_player)
        row3.addWidget(btn_center)
        self.pos_status = QLabel("位置：等待识图/手动定位", self)
        self.pos_status.setObjectName("Disclaimer")
        row3.addWidget(self.pos_status, 1)
        lay.addLayout(row3)

        # 识图过程与失败原因：默认是「只开官方地图」模式，主窗口侧栏看不见，
        # 这里必须显示，否则用户只知道位置不动，不知道卡在哪一步。
        self.vision_status = QLabel("识图：等待状态", self)
        self.vision_status.setObjectName("Disclaimer")
        self.vision_status.setWordWrap(True)
        lay.addWidget(self.vision_status)
        for b in (self.btn_prev, self.btn_next, self.btn_done):
            b.setEnabled(False)

        self.status = QLabel(
            "官方原始地图，含全部地区与楼层。登录请在本窗口内完成；标记、路线、进度会与库街区账号同步，"
            "本工具只额外叠加本机识图位置。", self)
        self.status.setObjectName("Disclaimer")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.sync_status = QLabel("位置指示：等待页面加载…", self)
        self.sync_status.setObjectName("Disclaimer")
        self.sync_status.setWordWrap(True)
        lay.addWidget(self.sync_status)

        base = paths.data_root() / "webengine"
        (base / "storage").mkdir(parents=True, exist_ok=True)
        (base / "cache").mkdir(parents=True, exist_ok=True)
        self.profile = QWebEngineProfile("kurobbs", self)
        self.profile.setPersistentStoragePath(str(base / "storage"))
        self.profile.setCachePath(str(base / "cache"))
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)

        self.view = QWebEngineView(self)
        self.view.setPage(QWebEnginePage(self.profile, self.view))
        self.view.loadFinished.connect(self._on_load_finished)
        content = QHBoxLayout()
        content.setSpacing(6)
        content.addWidget(self.view, 1)
        self.guide = GuidePanel(self)
        self.guide.setVisible(False)
        content.addWidget(self.guide)
        lay.addLayout(content, 1)

        self._ready_timer = QTimer(self)
        self._ready_timer.setSingleShot(True)
        self._ready_timer.timeout.connect(self._check_helper)
        self._ready_timer.start(2500)

        initial = state or (str(getattr(pack, "state", "") or "")) or ""
        idx = self.combo.findData(initial)
        self.combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.load_state(initial)

    # ------------------------------------------------------------------ 数据
    @property
    def _active_state(self) -> str:
        return str(self.combo.currentData() or "8")

    def _full_xy(self, x: float, y: float):
        pack = self._pack
        bbox = getattr(pack, "bbox", None) if pack is not None else None
        if not bbox:
            return None
        return to_full_coords(bbox, x, y)

    def set_pack(self, pack) -> None:
        old_country = str(getattr(self._pack, "country", "") or "")
        self._pack = pack
        new_country = str(getattr(pack, "country", "") or "")
        if self._active_state == "8" and new_country and new_country != old_country:
            self.load_state("8")
        else:
            self._apply()

    def set_guide_mode(self, on: bool) -> None:
        self.guide.setVisible(bool(on))
        if on:
            self.resize(max(1520, self.width()), max(860, self.height()))
        else:
            self.resize(1280, 860)

    def set_map_packs(self, packs, current_key: str = "") -> None:
        self.pack_combo.blockSignals(True)
        self.pack_combo.clear()
        for key, name in packs or []:
            self.pack_combo.addItem(name, key)
        idx = self.pack_combo.findData(current_key)
        self.pack_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.pack_combo.blockSignals(False)

    def _on_pack_changed(self, index: int) -> None:
        key = self.pack_combo.itemData(index)
        if key:
            self.map_pack_selected.emit(str(key))

    def set_overlay_state(self, on: bool) -> None:
        self.cb_overlay.blockSignals(True)
        self.cb_overlay.setChecked(bool(on))
        self.cb_overlay.blockSignals(False)

    def set_vision_state(self, on: bool) -> None:
        self.cb_detect.blockSignals(True)
        self.cb_detect.setChecked(bool(on))
        self.cb_detect.blockSignals(False)

    def set_position_status(self, text: str) -> None:
        self.pos_status.setText("位置：" + (text or "等待识图/手动定位"))

    def set_vision_status(self, text: str) -> None:
        """显示识图状态/失败原因（含裁片位置、内点数等细节）。"""
        self.vision_status.setText(text or "识图：等待状态")

    def set_player(self, x: float, y: float, rotation=None) -> None:
        self._pos = (float(x), float(y))
        self._apply()

    def clear_player(self) -> None:
        self._pos = None
        self._apply()

    def set_route(self, points: list[tuple[float, float]], index: int, label: str = "") -> None:
        self._route = list(points or [])
        self._index = int(index)
        self._label = label or ""
        has = bool(self._route)
        self.btn_prev.setEnabled(has)
        self.btn_next.setEnabled(has)
        self.btn_done.setEnabled(has)
        self._apply()

    # ------------------------------------------------------------------ 页面
    def load_state(self, state: str) -> None:
        state = str(state or "")
        self._checks = 0
        country = ""
        if state in ("", "8") and self._pack is not None:
            country = str(getattr(self._pack, "country", "") or "")
        url = official_map_url(state, country)
        self._page_ready = False
        self.sync_status.setText(f"位置指示：正在加载 {url}")
        self.view.load(QUrl(url))
        self._ready_timer.start(2500)

    def _reload(self) -> None:
        self.load_state(str(self.combo.currentData() or ""))

    def _on_load_finished(self, ok: bool) -> None:
        self._page_ready = bool(ok)
        if ok:
            self.status.setText("已加载官方原始地图；地区、楼层请在页面内切换。")
            self.view.page().runJavaScript(HELPER_JS)
            QTimer.singleShot(800, self._apply)
            self._ready_timer.start(2500)
        else:
            self.status.setText("加载失败（网络 / 代理）。可点「在浏览器打开」用系统浏览器访问。")
            self.sync_status.setText("位置指示：页面未加载，无法叠加")

    def _check_helper(self) -> None:
        if not self._page_ready:
            return
        self._checks += 1
        self.view.page().runJavaScript("window.__kuroHelperReady===true", self._on_helper_state)

    def _on_helper_state(self, ready) -> None:
        if ready:
            self._checks = 0
            self.sync_status.setText("位置指示：已连接官方地图，等待识图位置")
            self._apply()
        elif self._checks < 10:
            self.sync_status.setText("位置指示：正在连接官方地图…")
            self._ready_timer.start(3000)
        else:
            self.sync_status.setText("位置指示：未找到官方地图实例；仍可正常浏览官方地图")

    def _payload(self):
        if not self.cb_sync.isChecked():
            return None
        if self._pos is None:
            return None
        if self._active_state != "8":
            return None
        full = self._full_xy(*self._pos)
        if full is None:
            return None
        route = []
        for x, y in self._route:
            f = self._full_xy(x, y)
            if f is not None:
                route.append([f[0], f[1]])
        return {
            "x": round(full[0], 7),
            "y": round(full[1], 7),
            "route": route,
            "index": int(self._index),
            "label": self._label,
            "follow": bool(self.cb_follow.isChecked()),
        }

    def _apply(self) -> None:
        if not self._page_ready:
            return
        if self._active_state != "8":
            self.sync_status.setText("位置指示：仅支持官方地面世界（state 8）")
            self.view.page().runJavaScript("window.__kuroHelperClear&&window.__kuroHelperClear()")
            return
        if self._pack is None or not getattr(self._pack, "bbox", None):
            self.sync_status.setText("位置指示：当前数据包没有总图坐标（需要四国地面包）")
            return
        payload = self._payload()
        if payload is None:
            self.sync_status.setText("位置指示：等待识图/手动定位"
                                     if self._pos is None else "位置指示：已关闭")
            self.view.page().runJavaScript("window.__kuroHelperClear&&window.__kuroHelperClear()")
            return
        js = "window.__kuroHelperApply && window.__kuroHelperApply(%s)" % json.dumps(payload)
        self.view.page().runJavaScript(js)
        self.sync_status.setText("位置指示：已连接到官方地面地图")

    def center_on_player(self) -> None:
        self.view.page().runJavaScript("window.__kuroHelperCenter&&window.__kuroHelperCenter()")

    def _open_external(self) -> None:
        QDesktopServices.openUrl(QUrl(official_map_url(str(self.combo.currentData() or ""))))
