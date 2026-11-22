"""平台层测试：全局快捷键、本机加密存储、官方授权流程、以及一次合规静态扫描。

这些测试都不需要真实 Windows：热键在非 Windows 上必须明确报告「不支持」，
加密存储在非 Windows 上走 Fernet 回退，两者行为都要可验证。
"""

from __future__ import annotations

import ast
import os
import socket
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

from kumap.core import paths
from kumap.core.auth import (AuthManager, LoopbackListener, _b64url, _mask_uid,
                             _sanitize_token_payload)
from kumap.core.config import DEFAULTS
from kumap.core.hotkeys import (HOTKEY_ACTIONS, HotkeyError, HotkeyManager, MOD_NOREPEAT,
                                normalize_combo, parse_combo, to_qkey)
from kumap.core.provider import ApiConfig
from kumap.core.secure_store import SecureStore

SRC = Path(__file__).resolve().parents[1] / "src" / "kumap"


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMAPH_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture()
def qapp(qt_app):
    """复用 conftest 里 session 级的唯一 Qt 实例。"""
    return qt_app


def _pump_recv(sock: socket.socket, timeout: float = 3.0) -> bytes:
    """回环监听器的响应要跑 Qt 事件循环，所以边 pump 边等字节可读。"""
    import select

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if select.select([sock], [], [], 0.05)[0]:
            data = sock.recv(4096)
            if data:
                return data
    return b""


# ---------------------------------------------------------------- 快捷键组合


def test_parse_combo_valid():
    mods, vk = parse_combo("Ctrl+Alt+H")
    assert vk == ord("H")
    assert mods & 0x0002 and mods & 0x0001          # CONTROL | ALT
    assert mods & MOD_NOREPEAT                      # 长按不连发


def test_parse_combo_accepts_win_and_shift_aliases():
    assert parse_combo("win+d")[0] & 0x0008
    assert parse_combo("ctrl+shift+f12")[1] == 0x6F + 12
    assert parse_combo("alt+space")[1] == 0x20


@pytest.mark.parametrize("combo", ["", "h", "shift", "ctrl", "shift+a", "ctrl+alt+不认识的键"])
def test_parse_combo_rejects_dangerous_or_broken(combo):
    with pytest.raises(HotkeyError):
        parse_combo(combo)


def test_parse_combo_requires_global_modifier():
    """只有 Shift 或裸键的组合容易吃掉游戏操作，必须拒绝。"""
    for combo in ("a", "shift+a", "h"):
        with pytest.raises(HotkeyError):
            parse_combo(combo)


def test_normalize_combo_is_stable():
    assert normalize_combo("  ALT + CTRL + M ") == normalize_combo("ctrl+alt+m")
    assert normalize_combo("nonsense++") == "nonsense++"      # 非法组合原样返回


def test_to_qkey_maps_win_to_meta():
    seq = to_qkey("ctrl+alt+h")
    assert "Ctrl" in seq.toString() and "Alt" in seq.toString()
    assert "Meta" in to_qkey("win+d").toString()


def test_hotkey_actions_match_config_defaults():
    assert set(HOTKEY_ACTIONS) == set(DEFAULTS["hotkeys"])


def test_manager_reports_unsupported_off_windows(qapp):
    """非 Windows：不假装成功，明确回落到窗口内快捷键。"""
    mgr = HotkeyManager()
    mgr.set_bindings(dict(DEFAULTS["hotkeys"]))
    res = mgr.register_all()
    if not res.supported:
        assert res.ok == {} and "__platform__" in res.failed
    mgr.release()


def test_manager_ignores_unknown_actions(qapp):
    mgr = HotkeyManager()
    res = mgr.set_bindings({"toggle_panel": "ctrl+alt+h", "made_up": "ctrl+alt+y"})
    assert mgr.bindings == {"toggle_panel": "ctrl+alt+h"}
    assert res is not None
    mgr.release()


# ---------------------------------------------------------------- 本机加密存储


def test_secure_store_roundtrip_and_no_plaintext(data_dir):
    p = paths.secure_store_path()
    st = SecureStore(p)
    assert st.is_empty() is True
    st.write({"access_token": "AT-SECRET", "refresh_token": "RT-SECRET", "uid_masked": "12**78"})
    assert st.read()["access_token"] == "AT-SECRET"
    assert st.get("scope", "map") == "map"

    raw = p.read_text(encoding="utf-8")
    assert "AT-SECRET" not in raw and "RT-SECRET" not in raw
    if os.name != "nt":
        assert p.stat().st_mode & 0o077 == 0        # POSIX：只有本人可读写
    # Windows 没有 POSIX 权限位：密文靠 DPAPI（当前用户 + 应用熵）保护，
    # 文件又位于用户自己的 %APPDATA% 目录下，不适用 0600 断言。


def test_secure_store_survives_reload(data_dir):
    p = paths.secure_store_path()
    SecureStore(p).write({"access_token": "A"})
    assert SecureStore(p).read()["access_token"] == "A"


def test_secure_store_corrupt_is_logged_out(data_dir):
    p = paths.secure_store_path()
    SecureStore(p).write({"access_token": "A"})
    p.write_text('{"scheme": "fernet", "blob": "not-base64"}', encoding="utf-8")
    st = SecureStore(p)
    assert st.read() == {} and st.is_empty() is True   # 绝不明文回退


def test_secure_store_clear_removes_file(data_dir):
    p = paths.secure_store_path()
    st = SecureStore(p)
    st.write({"access_token": "A"})
    st.clear()
    assert not p.exists() and st.is_empty() is True


# ---------------------------------------------------------------- 授权流程


def test_api_config_roundtrip(data_dir):
    api = ApiConfig(authorize_url="https://example.invalid/oauth2/authorize",
                    token_url="https://example.invalid/oauth2/token",
                    markers_url="https://example.invalid/markers",
                    client_id="cid")
    path = api.save()
    assert path == paths.api_config_path()
    back = ApiConfig.load()
    assert back.client_id == "cid" and back.is_configured() and back.cloud_configured()
    assert back.redirect_uri == "http://127.0.0.1:8756/callback"
    assert ApiConfig().is_configured() is False


def test_api_config_ignores_unknown_keys(data_dir):
    p = paths.api_config_path()
    p.write_text('{"authorize_url": "https://a", "token_url": "https://b", '
                 '"client_id": "c", "evil": 1}', encoding="utf-8")
    api = ApiConfig.load()
    assert api.client_id == "c" and not hasattr(api, "evil")


def test_login_url_has_pkce_and_state(qapp, data_dir):
    api = ApiConfig(authorize_url="https://example.invalid/oauth2/authorize",
                    token_url="https://example.invalid/oauth2/token",
                    client_id="cid", redirect_port=18756)
    auth = AuthManager(api, store=SecureStore(paths.secure_store_path()))
    urls = []
    auth.login_url_ready.connect(lambda u: urls.append(u))
    url = auth.begin_login()
    try:
        assert url and urls == [url]
        assert "code_challenge_method=S256" in url and "code_challenge=" in url
        assert "state=" in url and "redirect_uri=http%3A%2F%2F127.0.0.1%3A18756%2Fcallback" in url
        assert "password" not in url.lower() and "phone" not in url.lower()
    finally:
        auth._close_listener()
        auth._timeout.stop()


def test_begin_login_without_endpoints_explains_itself(qapp, data_dir):
    auth = AuthManager(ApiConfig(), store=SecureStore(paths.secure_store_path()))
    msgs = []
    auth.needs_configuration.connect(lambda m: msgs.append(m))
    assert auth.begin_login() is None
    assert msgs and "无账号模式" in msgs[0]
    assert auth.logged_in() is False


def test_state_mismatch_is_rejected(qapp, data_dir):
    """回调里 state 不匹配（CSRF / 别人乱访问）不能换 token。"""
    api = ApiConfig(authorize_url="https://example.invalid/oauth2/authorize",
                    token_url="https://example.invalid/oauth2/token",
                    client_id="cid", redirect_port=18757)
    auth = AuthManager(api, store=SecureStore(paths.secure_store_path()))
    fails = []
    auth.login_failed.connect(lambda m: fails.append(m))
    url = auth.begin_login()
    assert url
    try:
        with socket.create_connection(("127.0.0.1", 18757), timeout=3) as s:
            s.sendall(b"GET /callback?code=abc&state=WRONG HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            assert b"HTTP/1.1" in _pump_recv(s)
        assert fails and "state" in fails[0]
        assert auth.store.is_empty() is True
    finally:
        auth._close_listener()
        auth._timeout.stop()


def test_loopback_listener_parses_callback(qapp):
    got = []
    lst = LoopbackListener("/callback")
    lst.code_received.connect(lambda code, state: got.append((code, state)))
    assert lst.listen(18758, "127.0.0.1") is True
    try:
        with socket.create_connection(("127.0.0.1", 18758), timeout=3) as s:
            s.sendall(b"GET /callback?code=XYZ%201&state=ST1 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            body = _pump_recv(s)
        assert got == [("XYZ 1", "ST1")]
        assert b"200 OK" in body
    finally:
        lst.close()


def test_logout_clears_credentials(qapp, data_dir):
    store = SecureStore(paths.secure_store_path())
    store.write({"access_token": "A", "refresh_token": "R", "uid_masked": "u"})
    auth = AuthManager(ApiConfig(), store=store)
    assert auth.logged_in() is True
    assert auth.account().uid_masked == "u"
    auth.logout()
    assert auth.logged_in() is False and store.is_empty() is True


def test_token_payload_drops_credential_like_fields():
    out = _sanitize_token_payload({"access_token": "A", "refresh_token": "R", "expires_in": 3600,
                                   "scope": "map.markers.read", "password": "hunter2",
                                   "phone": "13800000000", "uid": "12345678"})
    assert out == {"access_token": "A", "refresh_token": "R", "expires_in": 3600,
                   "scope": "map.markers.read"}


def test_mask_uid_hides_middle():
    assert _mask_uid("12345678") == "12****78"
    assert _mask_uid("ab") == "**"
    assert _mask_uid("") == ""


def test_b64url_is_unpadded_and_safe():
    v = _b64url(b"\x00\xff\x10")
    assert "=" not in v and "+" not in v and "/" not in v


# ---------------------------------------------------------------- 合规静态扫描

FORBIDDEN_NAMES = {
    # 进程读写 / 注入
    "ReadProcessMemory", "WriteProcessMemory", "VirtualAllocEx", "VirtualAlloc",
    "VirtualProtectEx", "VirtualFreeEx", "OpenProcess", "CreateRemoteThread",
    "WriteProcessMemory", "NtReadVirtualMemory",
    # Hook / 消息拦截
    "SetWindowsHookEx", "SetWinEventHook", "CallNextHookEx", "GetMessageHook",
    # 输入模拟
    "SendInput", "keybd_event", "mouse_event", "SetCursorPos", "BlockInput",
}
ALLOWED_WINDLL = {
    "ctypes.windll.user32", "ctypes.windll.crypt32", "ctypes.windll.kernel32",
    "ctypes.windll.user32.RegisterHotKey",
    "ctypes.windll.user32.UnregisterHotKey",
    "ctypes.windll.crypt32.CryptProtectData",
    "ctypes.windll.crypt32.CryptUnprotectData",
    "ctypes.windll.kernel32.LocalFree",
}
NETWORK_FILES_OK = {"core/provider.py", "core/auth.py"}   # 唯一允许联网/监听的文件
NETWORK_TOKENS = ("urllib.request", "http.client", "socket.socket", "QTcpServer")


def _sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py"))


def test_sources_are_scannable():
    assert len(_sources()) >= 20


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.relative_to(SRC.parent).as_posix())
def test_no_forbidden_api_usage(path):
    """AST 级检查：注释/文档字符串里的「禁止」字样不算，只看真实代码节点。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = path.relative_to(SRC).as_posix()   # 白名单用正斜杠，Windows 下也要一致

    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = set()
    dotted = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute):
            parts, cur = [], n
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
                dotted.append(".".join(reversed(parts)))
            attrs.add(n.attr)
    attrs |= names
    hits = {a for a in attrs if a in FORBIDDEN_NAMES}
    assert not hits, f"{rel} 出现禁止的进程/Hook/输入模拟 API：{hits}"
    assert not {"pyautogui", "pynput", "win32api", "win32gui"} & names, rel

    for d in dotted:
        if d.startswith("ctypes.windll."):
            assert d in ALLOWED_WINDLL, f"{rel} 调用了未列入白名单的 Win32 API：{d}"

    # 网络与回环监听只允许出现在 provider / auth 两个文件里
    text = path.read_text(encoding="utf-8")
    if rel not in NETWORK_FILES_OK:
        for tok in NETWORK_TOKENS:
            assert tok not in text, f"{rel} 不应使用 {tok}"


def test_no_input_simulation_imports():
    for p in _sources():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for al in n.names:
                    assert al.name.split(".")[0] not in {"pyautogui", "pynput", "keyboard",
                                                          "mouse", "win32api", "win32gui"}, \
                        f"{p.name} 导入了 {al.name}"
            elif isinstance(n, ast.ImportFrom) and n.module:
                assert n.module.split(".")[0] not in {"pyautogui", "pynput", "win32gui"}, \
                    f"{p.name} 从 {n.module} 导入"


def test_recognizer_only_uses_screen_pixels():
    """识图模块的依赖面：只允许 numpy / cv2 / mss / 标准库 / PySide6.QtCore。"""
    tree = ast.parse((SRC / "core" / "recognizer.py").read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    allowed = {"numpy", "cv2", "mss", "logging", "math", "threading", "time",
               "dataclasses", "typing", "PySide6", "__future__"}
    assert mods <= allowed, mods - allowed
    assert "PIL" not in mods and "socket" not in mods


def test_compliance_statement_is_shipped():
    from kumap import COMPLIANCE_STATEMENT

    for key in ("内存", "注入", "修改", "模拟", "外挂", "识图定位存在误差"):
        assert key in COMPLIANCE_STATEMENT
