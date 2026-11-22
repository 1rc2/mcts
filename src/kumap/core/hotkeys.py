"""全局快捷键（Windows RegisterHotKey）。

游戏内无需切出窗口即可：显示/隐藏面板、标记当前点位完成、上/下一个目标点、
切换路线、开启/关闭识图。实现方式是 Windows 原生 RegisterHotKey +
Qt 原生事件过滤器，不 Hook 游戏、不模拟输入，只接收系统发给本程序的按键消息。

非 Windows 平台（开发用）不支持全局热键，自动退化为「窗口内快捷键」，
需要窗口在前台才生效——这一点在设置界面里会明确提示。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
from dataclasses import dataclass

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal
from PySide6.QtGui import QKeySequence

WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000

MOD_FLAGS = {
    "ctrl": 0x0002,   # MOD_CONTROL
    "alt": 0x0001,    # MOD_ALT
    "shift": 0x0004,  # MOD_SHIFT
    "win": 0x0008,    # MOD_WIN
    "super": 0x0008,
    "cmd": 0x0008,
}

VK_LETTERS = {chr(c): c for c in range(ord("A"), ord("Z") + 1)}
VK_DIGITS = {str(d): ord(str(d)) for d in range(0, 10)}
VK_SPECIAL = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "backspace": 0x08, "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pageup": 0x21,
    "pgdn": 0x22, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "pause": 0x13, "capslock": 0x14,
    "comma": 0xBC, "period": 0xBE, "slash": 0xBF, "semicolon": 0xBA,
    "quote": 0xDE, "minus": 0xBD, "equal": 0xBB, "bracketleft": 0xDB,
    "bracketright": 0xDD, "backslash": 0xDC,
}
VK_FUNCTION = {f"f{i}": 0x6F + i for i in range(1, 25)}

HOTKEY_ACTIONS: dict[str, str] = {
    "toggle_panel": "显示 / 隐藏工具面板",
    "mark_done": "标记当前点位完成 / 未完成",
    "next_waypoint": "下一个目标点",
    "prev_waypoint": "上一个目标点",
    "toggle_vision": "开启 / 关闭识图定位",
    "toggle_overlay": "开启 / 关闭游戏内指示窗",
    "overlay_click": "悬浮窗在「鼠标穿透 / 可点击」之间切换",
    "next_route": "切换下一条路线",
    "toggle_topmost": "切换窗口置顶",
}


class HotkeyError(ValueError):
    pass


def parse_combo(combo: str) -> tuple[int, int]:
    """'ctrl+alt+h' → (MOD_ 掩码, VK 码)。"""
    parts = [p.strip().lower() for p in str(combo).replace(" ", "+").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("快捷键为空")
    mods = 0
    key = None
    for p in parts:
        if p in MOD_FLAGS:
            mods |= MOD_FLAGS[p]
        else:
            key = p
    if key is None:
        raise HotkeyError(f"「{combo}」只包含修饰键，需要一个主键")
    vk = VK_LETTERS.get(key.upper()) or VK_DIGITS.get(key) or VK_SPECIAL.get(key) or VK_FUNCTION.get(key)
    if vk is None:
        raise HotkeyError(f"无法识别的按键：{key}")
    if not (mods & (MOD_FLAGS["ctrl"] | MOD_FLAGS["alt"] | MOD_FLAGS["win"])):
        raise HotkeyError(f"「{combo}」缺少 Ctrl / Alt / Win 修饰键，容易和游戏操作冲突")
    return mods | MOD_NOREPEAT, vk


def to_qkey(combo: str) -> QKeySequence:
    parts = [p.strip().lower() for p in str(combo).replace(" ", "+").split("+") if p.strip()]
    out = []
    mapping = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Meta", "super": "Meta", "cmd": "Meta"}
    for p in parts:
        out.append(mapping.get(p, p.capitalize()))
    return QKeySequence("+".join(out))


def normalize_combo(combo: str) -> str:
    try:
        mods, vk = parse_combo(combo)
    except HotkeyError:
        return str(combo).strip().lower()
    names = []
    if mods & MOD_FLAGS["ctrl"]:
        names.append("Ctrl")
    if mods & MOD_FLAGS["alt"]:
        names.append("Alt")
    if mods & MOD_FLAGS["shift"]:
        names.append("Shift")
    if mods & MOD_FLAGS["win"]:
        names.append("Win")
    rev_vk = {}
    for table in (VK_LETTERS, VK_DIGITS, VK_SPECIAL, VK_FUNCTION):
        for k, v in table.items():
            rev_vk[v] = k
    names.append((rev_vk.get(vk, str(vk))).capitalize())
    return "+".join(names)


@dataclass
class RegistrationResult:
    ok: dict[str, str]
    failed: dict[str, str]
    supported: bool


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.wintypes.HWND),
        ("message", ctypes.wintypes.UINT),
        ("wParam", ctypes.wintypes.WPARAM),
        ("lParam", ctypes.wintypes.LPARAM),
        ("time", ctypes.wintypes.DWORD),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


class _WinHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, manager: "HotkeyManager"):
        super().__init__()
        self._manager = manager

    def nativeEventFilter(self, event_type, message):  # noqa: ANN001
        if event_type != b"windows_generic_MSG":
            return False, 0
        try:
            msg = _MSG.from_address(int(message))
        except Exception:
            return False, 0
        if msg.message == WM_HOTKEY:
            action = self._manager._ids.get(int(msg.wParam))
            if action:
                self._manager.triggered.emit(action)
                return True, 0
        return False, 0


class HotkeyManager(QObject):
    triggered = Signal(str)
    registration_changed = Signal(object)  # RegistrationResult

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.bindings: dict[str, str] = {}
        self._ids: dict[int, str] = {}
        self._user32 = None
        self._filter: _WinHotkeyFilter | None = None
        self._next_id = 0xC001
        if sys.platform == "win32":
            self._user32 = ctypes.windll.user32

    @property
    def supported(self) -> bool:
        return self._user32 is not None

    # ------------------------------------------------------------------ 注册
    def set_bindings(self, bindings: dict[str, str]) -> RegistrationResult:
        self.unregister_all()
        self.bindings = {k: v for k, v in bindings.items() if k in HOTKEY_ACTIONS and v}
        result = self.register_all()
        self.registration_changed.emit(result)
        return result

    def register_all(self) -> RegistrationResult:
        ok: dict[str, str] = {}
        failed: dict[str, str] = {}
        if not self.supported:
            return RegistrationResult(ok={}, failed={"__platform__": "当前系统不支持全局热键，已改用窗口内快捷键"},
                                      supported=False)
        if self._filter is None:
            self._filter = _WinHotkeyFilter(self)
            from PySide6.QtCore import QCoreApplication

            QCoreApplication.instance().installNativeEventFilter(self._filter)
        for action, combo in self.bindings.items():
            try:
                mods, vk = parse_combo(combo)
            except HotkeyError as exc:
                failed[action] = str(exc)
                continue
            hot_id = self._next_id
            self._next_id += 1
            try:
                res = int(self._user32.RegisterHotKey(None, hot_id, mods, vk))
            except Exception as exc:  # noqa: BLE001
                failed[action] = f"注册失败：{exc}"
                self._next_id -= 1
                continue
            if not res:
                self._next_id -= 1
                failed[action] = f"「{normalize_combo(combo)}」已被系统或其他程序占用"
                continue
            self._ids[hot_id] = action
            ok[action] = normalize_combo(combo)
        return RegistrationResult(ok=ok, failed=failed, supported=True)

    def unregister_all(self) -> None:
        if not self.supported:
            return
        for hot_id in list(self._ids):
            try:
                self._user32.UnregisterHotKey(None, hot_id)
            except Exception:
                pass
        self._ids.clear()

    def release(self) -> None:
        self.unregister_all()
        if self._filter is not None:
            from PySide6.QtCore import QCoreApplication

            app = QCoreApplication.instance()
            if app:
                app.removeNativeEventFilter(self._filter)
            self._filter = None
