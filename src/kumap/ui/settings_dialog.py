"""设置对话框：窗口外观、全局快捷键、识图、数据、账号接口、合规声明。"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSpinBox, QTabWidget,
                               QTextEdit, QVBoxLayout, QWidget, QKeySequenceEdit)

from .. import COMPLIANCE_STATEMENT, __app_name__, __version__
from ..core import paths
from ..core.config import Config
from ..core.hotkeys import HOTKEY_ACTIONS, HotkeyManager, normalize_combo, parse_combo
from ..core.provider import ApiConfig

MOD_NAMES = {Qt.ControlModifier: "ctrl", Qt.AltModifier: "alt", Qt.ShiftModifier: "shift",
             Qt.MetaModifier: "win"}
KEY_ALIASES = {
    " ": "space", "+": "plus", ",": "comma", ".": "period", "/": "slash",
    "-": "minus", "*": "asterisk", ":": "colon", ";": "semicolon",
}


def seq_to_combo(seq: QKeySequence) -> str:
    text = seq.toString(QKeySequence.PortableText) if seq else ""
    if not text:
        return ""
    parts = [p.strip() for p in text.split("+") if p.strip()]
    out = []
    for p in parts:
        low = p.lower()
        if low in ("ctrl", "control"):
            out.append("ctrl")
        elif low in ("alt", "option"):
            out.append("alt")
        elif low in ("shift",):
            out.append("shift")
        elif low in ("meta", "win", "cmd", "super"):
            out.append("win")
        else:
            out.append(KEY_ALIASES.get(low, low))
    return "+".join(out)


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, api: ApiConfig, parent: QWidget | None = None,
                 hotkeys: HotkeyManager | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self.api = api
        self.hotkeys = hotkeys
        self._data_changed = False
        self._hotkey_failed: dict[str, str] = {}
        self.setWindowTitle(f"设置 · {__app_name__}")
        self.resize(680, 560)
        tabs = QTabWidget(self)
        tabs.addTab(self._build_window_tab(), "窗口与外观")
        tabs.addTab(self._build_hotkey_tab(), "全局快捷键")
        tabs.addTab(self._build_vision_tab(), "识图定位")
        tabs.addTab(self._build_data_tab(), "数据与地图")
        tabs.addTab(self._build_account_tab(), "账号与同步")
        tabs.addTab(self._build_about_tab(), "合规与关于")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Apply | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self._on_ok)
        buttons.clicked.connect(lambda b: self._apply() if buttons.button(b) is buttons.button(QDialogButtonBox.Apply) else None)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)
        self._tabs = tabs

    # ------------------------------------------------------------------ 窗口
    def _build_window_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setLabelAlignment(Qt.AlignRight)
        self.cb_top = QCheckBox("窗口置顶（默认开启）")
        self.cb_top.setChecked(bool(self.cfg.ui.get("always_on_top", True)))
        self.cb_lock = QCheckBox("锁定窗口尺寸（防止误拖变形）")
        self.cb_lock.setChecked(bool(self.cfg.ui.get("lock_size", False)))
        self.cb_compact = QCheckBox("紧凑悬浮小窗（隐藏侧栏）")
        self.cb_compact.setChecked(bool(self.cfg.ui.get("compact", False)))
        self.cb_tray = QCheckBox("点击关闭时最小化到系统托盘")
        self.cb_tray.setChecked(bool(self.cfg.ui.get("close_to_tray", True)))
        self.cb_follow = QCheckBox("紧凑模式下跟随当前位置自动平移")
        self.cb_follow.setChecked(bool(self.cfg.ui.get("minimap_follow", True)))
        self.cb_mark = QCheckBox("点击点位即标记完成 / 未完成")
        self.cb_mark.setChecked(bool(self.cfg.ui.get("mark_mode", False)))
        self.combo_theme = QComboBox()
        self.combo_theme.addItems(["深色", "浅色"])
        self.combo_theme.setCurrentIndex(0 if self.cfg.ui.get("theme") != "light" else 1)
        self.sl_opacity = QSpinBox()
        self.sl_opacity.setRange(40, 100)
        self.sl_opacity.setSuffix(" %")
        self.sl_opacity.setValue(int(self.cfg.ui.get("opacity", 0.96) * 100))
        for lbl, wdg in (("主题", self.combo_theme), ("窗口不透明度", self.sl_opacity)):
            form.addRow(lbl, wdg)
        for cb in (self.cb_top, self.cb_lock, self.cb_compact, self.cb_tray, self.cb_follow, self.cb_mark):
            form.addRow(cb)
        tip = QLabel("提示：《鸣潮》请运行在「窗口化 / 无边框窗口」模式，置顶悬浮窗才会浮在游戏之上；"
                     "全屏独占模式下任何置顶窗口都会被遮挡。")
        tip.setWordWrap(True)
        form.addRow(tip)
        return w

    # ------------------------------------------------------------------ 快捷键
    def _build_hotkey_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self._hk_edits: dict[str, QKeySequenceEdit] = {}
        box = QGroupBox("在游戏内直接按下组合键即可，无需切出窗口", w)
        form = QFormLayout(box)
        for action, label in HOTKEY_ACTIONS.items():
            edit = QKeySequenceEdit(self)
            combo = self.cfg.hotkeys.get(action, "")
            if combo:
                edit.setKeySequence(QKeySequence(combo.replace("win", "meta").replace("+", "+")))
            self._hk_edits[action] = edit
            state = QLabel("待注册", box)
            state.setObjectName("Disclaimer")
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            row.addWidget(state)
            self._hk_status = getattr(self, "_hk_status", {})
            self._hk_status[action] = state
            form.addRow(label, row)
        lay.addWidget(box)
        note = QLabel(
            "全局热键通过 Windows 原生 RegisterHotKey 注册，仅接收系统分发给本程序的热键消息，"
            "不 Hook 游戏、不拦截或模拟任何输入。注册失败通常表示该组合键已被系统或其他软件占用。")
        note.setWordWrap(True)
        note.setObjectName("Disclaimer")
        lay.addWidget(note)
        if not sys.platform.startswith("win"):
            lay.addWidget(QLabel("当前系统不是 Windows：全局热键不可用，将退化为窗口内快捷键（窗口需在前台）。"))
        lay.addStretch(1)
        return w

    # ------------------------------------------------------------------ 识图
    def _build_vision_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.sp_interval = QSpinBox()
        self.sp_interval.setRange(500, 10000)
        self.sp_interval.setSingleStep(250)
        self.sp_interval.setSuffix(" ms")
        self.sp_interval.setValue(int(self.cfg.vision.get("interval_ms", 1500)))
        self.sp_sens = QSpinBox()
        self.sp_sens.setRange(10, 95)
        self.sp_sens.setValue(int(self.cfg.vision.get("sensitivity", 0.55) * 100))
        self.cb_auto = QCheckBox("自动在屏幕上寻找小地图边界")
        self.cb_auto.setChecked(bool(self.cfg.vision.get("auto_detect", True)))
        self.sp_rect = []
        rect_row = QHBoxLayout()
        for i, lbl in enumerate(("X", "Y", "宽", "高")):
            sp = QDoubleSpinBox()
            sp.setRange(0, 1)
            sp.setDecimals(3)
            sp.setSingleStep(0.005)
            sp.setValue(float(self.cfg.vision.get("minimap_rect", [0, 0, 0.1, 0.1])[i]))
            rect_row.addWidget(QLabel(lbl))
            rect_row.addWidget(sp)
            self.sp_rect.append(sp)
        self.cb_trail = QCheckBox("在大图上保留最近识别轨迹点")
        self.cb_trail.setChecked(bool(self.cfg.vision.get("show_trail", True)))
        rect_host = QWidget()
        rect_host.setLayout(rect_row)
        form.addRow("识别间隔", self.sp_interval)
        form.addRow("严格度（越高越容易判失败）", self.sp_sens)
        form.addRow("小地图区域（相对屏幕的比例）", rect_host)
        form.addRow(self.cb_auto)
        form.addRow(self.cb_trail)
        presets = QHBoxLayout()
        b_tl = QPushButton("设为屏幕左上角")
        b_tl.clicked.connect(lambda: self._set_rect(0.008, 0.012, 0.115, 0.115))
        b_tr = QPushButton("设为屏幕右上角")
        b_tr.clicked.connect(lambda: self._set_rect(0.877, 0.012, 0.115, 0.115))
        b_custom = QPushButton("框选屏幕区域（下一步在屏幕拖框）")
        b_custom.setToolTip("先最小化本对话框，再按住鼠标在游戏小地图上拖出一个矩形")
        b_custom.clicked.connect(self._screen_capture_rect)
        for b in (b_tl, b_tr, b_custom):
            presets.addWidget(b)
        form.addRow(presets)
        self.lbl_vision_test = QLabel("未测试")
        self.lbl_vision_test.setWordWrap(True)
        b_test = QPushButton("立即测试一次识别")
        b_test.clicked.connect(self._test_vision)
        row2 = QHBoxLayout()
        row2.addWidget(b_test)
        row2.addWidget(self.lbl_vision_test, 1)
        form.addRow(row2)
        warn = QLabel("识图只读取屏幕像素，不读取游戏内存；受 HUD 遮挡、天气特效、"
                      "地图素材差异影响，定位存在误差，仅作探索参考。识别连续失败时会自动提示改用「手动定位」。")
        warn.setWordWrap(True)
        warn.setObjectName("Disclaimer")
        form.addRow(warn)
        return w

    def _set_rect(self, x: float, y: float, ww: float, hh: float) -> None:
        for sp, v in zip(self.sp_rect, (x, y, ww, hh)):
            sp.setValue(v)

    def _screen_capture_rect(self) -> None:
        host = self.parent()
        if host:
            host.showMinimized()
        QTimer.singleShot(350, lambda: self._do_capture(host))

    def _do_capture(self, host) -> None:
        from .region_picker import pick_region

        rect = pick_region()
        if host:
            host.showNormal()
        if not rect:
            return
        screen = None
        try:
            from PySide6.QtWidgets import QApplication

            screen = QApplication.primaryScreen().size()
        except Exception:
            pass
        if not screen or not screen.width:
            return
        self._set_rect(rect.x() / screen.width(), rect.y() / screen.height(),
                       rect.width() / screen.width(), rect.height() / screen.height())
        self.cb_auto.setChecked(False)

    def _test_vision(self) -> None:
        win = self.parent()
        runner = getattr(win, "vision", None)
        if runner is None:
            self.lbl_vision_test.setText("无法访问识图模块")
            return
        self._apply(save=False)
        if not runner.enabled:
            runner.apply(self.cfg.vision, enabled=True)
            self.lbl_vision_test.setText("已临时开启识图做单次测试…")
        try:
            from ..core.recognizer import MinimapLocalizer, VisionConfig

            loc = MinimapLocalizer(str(runner.map_image or ""), VisionConfig(
                interval_ms=self.cfg.vision.get("interval_ms", 1500),
                minimap_rect=tuple(self.cfg.vision.get("minimap_rect")),
                auto_detect=self.cb_auto.isChecked(),
                sensitivity=self.sp_sens.value() / 100.0))
            if not loc.available:
                self.lbl_vision_test.setText(f"识图依赖不可用：{loc.error}")
                return
            res = loc.locate()
            if res.ok:
                self.lbl_vision_test.setText(
                    f"单次识别成功：({res.x:.3f}, {res.y:.3f}) 置信 {res.confidence:.0%}｜{res.accuracy_note}")
            else:
                self.lbl_vision_test.setText(f"单次识别失败：{res.reason}｜{res.accuracy_note}")
        except Exception as exc:  # noqa: BLE001
            self.lbl_vision_test.setText(f"测试异常：{exc}")

    # ------------------------------------------------------------------ 数据
    def _build_data_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.ed_points = QLineEdit(str(self.cfg.data_cfg.get("points_file") or ""))
        self.ed_points.setPlaceholderText("留空 = 使用内置示例数据 / import 目录")
        b1 = QPushButton("选择…")
        b1.clicked.connect(lambda: self._pick_file(self.ed_points, "JSON 数据文件 (*.json)"))
        r1 = QHBoxLayout()
        r1.addWidget(self.ed_points, 1)
        r1.addWidget(b1)
        self.ed_map = QLineEdit(str(self.cfg.data_cfg.get("map_image") or ""))
        self.ed_map.setPlaceholderText("留空 = 使用内置底图（应与点位数据同源）")
        b2 = QPushButton("选择…")
        b2.clicked.connect(lambda: self._pick_file(self.ed_map, "地图图片 (*.png *.jpg *.webp)"))
        r2 = QHBoxLayout()
        r2.addWidget(self.ed_map, 1)
        r2.addWidget(b2)
        form.addRow("点位数据文件", r1)
        form.addRow("大地图底图", r2)
        b_open = QPushButton("打开本机数据 / import 目录")
        b_open.clicked.connect(self._open_import_dir)
        form.addRow(b_open)
        info = QLabel(
            "数据口径说明：\n"
            "· 本工具内置的是一份【示例】点位与路线数据，用于演示界面、图层、导航与同步流程，"
            "不代表库街区官方坐标；\n"
            "· 要获得与库街区大地图一致的点位、路线、底图，请把官方或社区【公开导出】的 JSON 数据包"
            f"放入本机 import 目录（{paths.import_dir()}），或在上方指定文件路径；\n"
            "· 底图必须与点位数据同源（同一坐标归一化口径），否则点位会整体偏移；\n"
            "· 本工具不会自动抓取任何非公开接口。")
        info.setWordWrap(True)
        info.setObjectName("Disclaimer")
        form.addRow(info)
        return w

    def _pick_file(self, line: QLineEdit, filt: str) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "选择文件", str(Path.home()), filt)
        if p:
            line.setText(p)

    def _open_import_dir(self) -> None:
        import os
        import subprocess

        d = paths.import_dir()
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(d))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(d)])
            else:
                subprocess.Popen(["xdg-open", str(d)])
        except Exception as exc:  # noqa: BLE001
            QMessageBox.information(self, "本机目录", f"请手动打开：{d}\n（{exc}）")

    # ------------------------------------------------------------------ 账号
    def _build_account_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.ed_auth = QLineEdit(self.api.authorize_url)
        self.ed_token = QLineEdit(self.api.token_url)
        self.ed_markers = QLineEdit(self.api.markers_url)
        self.ed_push = QLineEdit(self.api.push_markers_url)
        self.ed_client = QLineEdit(self.api.client_id)
        self.ed_port = QSpinBox()
        self.ed_port.setRange(1024, 65535)
        self.ed_port.setValue(int(self.api.redirect_port))
        for lbl, wdg in (
            ("官方授权页 URL", self.ed_auth),
            ("官方 token URL", self.ed_token),
            ("云端标记读取 URL", self.ed_markers),
            ("云端标记回传 URL", self.ed_push),
            ("client_id", self.ed_client),
            ("本机回调端口", self.ed_port),
        ):
            form.addRow(lbl, wdg)
        self.combo_conflict = QComboBox()
        self.combo_conflict.addItems(["本地较新的改动优先（推荐）", "以云端为准"])
        self.combo_conflict.setCurrentIndex(0 if self.cfg.sync.get("conflict", "newer") == "newer" else 1)
        self.cb_autopull = QCheckBox("登录完成后自动拉取云端进度")
        self.cb_autopull.setChecked(bool(self.cfg.sync.get("auto_pull_on_login", True)))
        form.addRow(self.combo_conflict)
        form.addRow(self.cb_autopull)
        b_save = QPushButton("保存接口配置到 kuro_api.json")
        b_save.clicked.connect(self._save_api_config)
        form.addRow(b_save)
        tip = QLabel(
            "登录与进度同步只走库街区【官方授权】通道：\n"
            "1) 手机号 / 扫码都在官方页面完成，本工具不出现任何账号密码输入框，也不记录凭据；\n"
            "2) 官方回调只在本机 127.0.0.1 临时监听数秒，校验 state 后立即关闭；\n"
            "3) 授权令牌用 Windows DPAPI 加密后仅存本机，退出登录立刻删除；\n"
            "4) 若库街区未公开可用的第三方授权端点（当前状态），云端同步会保持「未配置」，"
            "此时全部功能以本地模式可用；也可以在库街区 App 内直接查看 / 编辑同一份进度。\n"
            "填写以上端点即表示你已获得官方授权许可。")
        tip.setWordWrap(True)
        tip.setObjectName("Disclaimer")
        form.addRow(tip)
        return w

    def _save_api_config(self) -> None:
        self.api.authorize_url = self.ed_auth.text().strip()
        self.api.token_url = self.ed_token.text().strip()
        self.api.markers_url = self.ed_markers.text().strip()
        self.api.push_markers_url = self.ed_push.text().strip()
        self.api.client_id = self.ed_client.text().strip()
        self.api.redirect_port = int(self.ed_port.value())
        path = self.api.save()
        QMessageBox.information(self, "已保存", f"接口配置写入：\n{path}\n\n"
                                f"授权可用状态：{'已配置' if self.api.is_configured() else '未配置（本地模式）'}")

    # ------------------------------------------------------------------ 关于
    def _build_about_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        box = QTextEdit()
        box.setReadOnly(True)
        box.setPlainText(
            f"{__app_name__} v{__version__}\n\n【合规声明】\n{COMPLIANCE_STATEMENT}\n\n"
            "【明确不做的事】\n"
            "· 不读取游戏进程内存、不打开进程句柄、不注入 DLL、不 Hook\n"
            "· 不修改游戏本地或云端任何数据\n"
            "· 不模拟鼠标键盘，没有自动点击 / 自动寻路 / 自动拾取\n"
            "· 不抓取非公开接口，不经手账号密码与短信验证码\n\n"
            "【数据位置】\n"
            f"配置：{paths.config_path()}\n"
            f"进度标记：{paths.markers_path()}\n"
            f"私人点位：{paths.private_points_path()}\n"
            f"公开数据导入目录：{paths.import_dir()}\n"
            f"授权令牌（DPAPI 加密）：{paths.secure_store_path()}\n\n"
            "【免责】识图定位存在误差，本工具仅为探索参考辅助，不保证识别结果完全准确；"
            "请遵守《鸣潮》用户协议与库街区社区规范。")
        lay.addWidget(box)
        return w

    # ------------------------------------------------------------------ 应用
    def _apply(self, save: bool = True) -> dict:
        ui = {
            "always_on_top": self.cb_top.isChecked(),
            "lock_size": self.cb_lock.isChecked(),
            "compact": self.cb_compact.isChecked(),
            "close_to_tray": self.cb_tray.isChecked(),
            "minimap_follow": self.cb_follow.isChecked(),
            "mark_mode": self.cb_mark.isChecked(),
            "theme": "light" if self.combo_theme.currentIndex() == 1 else "dark",
            "opacity": self.sl_opacity.value() / 100.0,
        }
        if save:
            for k, v in ui.items():
                self.cfg.set("ui", k, v, save=False)
            vision = {
                "interval_ms": int(self.sp_interval.value()),
                "sensitivity": self.sp_sens.value() / 100.0,
                "auto_detect": self.cb_auto.isChecked(),
                "minimap_rect": [float(sp.value()) for sp in self.sp_rect],
                "show_trail": self.cb_trail.isChecked(),
            }
            for k, v in vision.items():
                self.cfg.set("vision", k, v, save=False)
            self.cfg.set("sync", "conflict", "newer" if self.combo_conflict.currentIndex() == 0 else "remote", save=False)
            self.cfg.set("sync", "auto_pull_on_login", self.cb_autopull.isChecked(), save=False)
            new_points = self.ed_points.text().strip() or None
            new_map = self.ed_map.text().strip() or None
            if (new_points != (self.cfg.data_cfg.get("points_file") or None)
                    or new_map != (self.cfg.data_cfg.get("map_image") or None)):
                self._data_changed = True
            self.cfg.set("data", "points_file", new_points, save=False)
            self.cfg.set("data", "map_image", new_map, save=False)
            self.cfg.save()

            # 快捷键
            bindings = {}
            for action, edit in self._hk_edits.items():
                combo = seq_to_combo(edit.keySequence())
                if combo:
                    bindings[action] = combo
            if bindings:
                for action, combo in bindings.items():
                    self.cfg.set("hotkeys", action, combo, save=False)
                self.cfg.save()
                if self.hotkeys:
                    result = self.hotkeys.set_bindings(dict(self.cfg.hotkeys))
                    self._hotkey_failed = dict(result.failed)
                    for action, state_label in getattr(self, "_hk_status", {}).items():
                        if action in result.failed:
                            state_label.setText(f"失败：{result.failed[action]}")
                        elif action in result.ok:
                            state_label.setText(f"已注册 {result.ok[action]}")
                        else:
                            state_label.setText("未设置")
        return {"data_changed": self._data_changed, "hotkey_failed": self._hotkey_failed}

    def result_summary(self) -> dict:
        return {"data_changed": self._data_changed, "hotkey_failed": self._hotkey_failed}

    def _on_ok(self) -> None:
        self._apply()
        self.accept()
