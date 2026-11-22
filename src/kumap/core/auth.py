"""库街区官方授权登录（OAuth2 授权码 + PKCE）。

流程与合规说明：
1. 工具生成本次授权用的 code_verifier / code_challenge(S256) / state，
   然后在系统默认浏览器打开「库街区官方授权页面」，手机号登录或扫码均由
   官方页面完成——本工具不显示任何账号密码输入框，也不接收、不记录凭据。
2. 官方页面授权后回调 http://127.0.0.1:<port>/callback?code=...&state=...，
   监听器只在本机回环地址上存在数秒，state 校验通过后立即关闭。
3. 工具用 code + code_verifier 到官方 token 端点换取 access/refresh token，
   token 经 DPAPI 加密后仅存本机；退出登录时立刻删除。
4. 若官方端点未配置（库街区当前没有公开的第三方 OAuth 注册入口），
   begin_login() 会返回明确原因，界面引导用户使用「无账号模式」或
   「导入库街区导出文件」，功能不受影响。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtNetwork import QTcpServer, QTcpSocket

from .provider import ApiConfig, http_form
from .secure_store import SecureStore

logger = logging.getLogger("kumap.auth")

CALLBACK_TIMEOUT_MS = 180_000
_FORBIDDEN_KEYS = {"password", "passwd", "pwd", "account", "phone", "mobile", "sms_code", "otp"}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _mask_uid(uid: str) -> str:
    uid = str(uid or "")
    if len(uid) <= 4:
        return "*" * len(uid)
    return uid[:2] + "*" * (len(uid) - 4) + uid[-2:]


# ------------------------------------------------------------------ 回环监听


class LoopbackListener(QObject):
    """只监听 127.0.0.1 的一次性授权回调接收器。"""

    code_received = Signal(str, str)  # code, state
    failed = Signal(str)

    def __init__(self, callback_path: str = "/callback", parent: QObject | None = None):
        super().__init__(parent)
        self.callback_path = callback_path
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_connection)
        # nextPendingConnection() 返回的 socket 没有父对象，只被闭包引用；
        # 那样 Python 会在任意一次 GC 里把正在被事件循环使用的 socket 析构掉，
        # 造成后续 processEvents() 随机段错误。这里挂到本对象下并记录缓冲，
        # 生命周期完全由 C++ 父子关系与本线程事件循环决定。
        self._buffers: dict[QTcpSocket, bytearray] = {}

    def listen(self, port: int, host: str = "127.0.0.1") -> bool:
        if self._server.isListening():
            return True
        ok = bool(self._server.listen(_addr(host), int(port)))
        if not ok:
            self.failed.emit(f"无法监听本机回调端口 {port}：{self._server.errorString()}")
        return ok

    def close(self) -> None:
        # 只停监听，不动已建立的连接：AuthManager 在收到 code 的回调栈里就会调用
        # 这里，若此时 abort 掉 socket，回给浏览器的「授权完成」页就写不出去了。
        # 已连接的 socket 是本对象的子对象，随本对象一起析构，不会泄漏。
        if self._server.isListening():
            self._server.close()

    @property
    def listening(self) -> bool:
        return self._server.isListening()

    def _drop_socket(self, sock: QTcpSocket) -> None:
        if self._buffers.pop(sock, None) is None:
            return  # 已回收过（abort 会再触发一次 disconnected）
        if sock.state() != QTcpSocket.SocketState.UnconnectedState:
            sock.abort()
        sock.deleteLater()

    def _on_connection(self) -> None:
        sock = self._server.nextPendingConnection()
        if sock is None:
            return
        sock.setParent(self)
        self._buffers[sock] = bytearray()
        sock.readyRead.connect(self._on_ready_read)
        sock.disconnected.connect(self._on_disconnected)
        self._read(sock)  # 请求有可能在 newConnection 之前就已可读，先补读一次

    def _on_ready_read(self) -> None:
        self._read(self.sender())

    def _read(self, sock: QTcpSocket | None) -> None:
        buf = self._buffers.get(sock) if sock is not None else None
        if sock is None or buf is None:
            return
        buf.extend(sock.readAll().data())
        if b"\r\n\r\n" not in bytes(buf):
            return
        text = bytes(buf).decode("latin-1", "ignore")
        request_line = text.split("\r\n", 1)[0]
        parts = request_line.split(" ")
        path = parts[1] if len(parts) >= 2 else ""
        handled = False
        if path.startswith(self.callback_path):
            query = urllib.parse.urlparse(path).query
            params = urllib.parse.parse_qs(query)
            if "code" in params:
                self.code_received.emit(params["code"][0], (params.get("state") or [""])[0])
                handled = True
            elif "error" in params:
                self.failed.emit(f"官方授权页返回错误：{params['error'][0]}")
                handled = True
        _write_close(sock, handled)
        sock.disconnectFromHost()

    def _on_disconnected(self) -> None:
        sock = self.sender()
        if sock is not None:
            self._drop_socket(sock)


def _addr(host: str):
    from PySide6.QtNetwork import QHostAddress

    return QHostAddress(host)


def _write_close(sock: QTcpSocket, ok: bool) -> None:
    title = "授权完成" if ok else "无效回调"
    body = (
        "<!doctype html><meta charset='utf-8'><title>" + title + "</title>"
        "<body style='font-family:system-ui;padding:40px'>"
        f"<h2>{'已完成库街区授权，可以关闭此页面。' if ok else '这不是本工具等待的授权回调。'}</h2>"
        "</body>"
    ).encode("utf-8")
    head = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n"
    )
    try:
        if not sock.isOpen():  # 回调处理链路里连接可能已被对端关掉
            return
        sock.write(head + body)
        # Qt 的 socket 写是异步的：不先冲刷就直接断开，浏览器会收到空白页/连接重置，
        # 用户看不到「授权完成」这一页。回环上的几百字节，等待冲刷的开销可以忽略。
        sock.waitForBytesWritten(2000)
    except Exception:  # noqa: BLE001 - 冲刷失败也不能影响 code 已经收到的事实
        pass


# ------------------------------------------------------------------ token 交换


class _TokenWorker(QThread):
    ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, token_url: str, form: dict[str, str], parent: QObject | None = None):
        super().__init__(parent)
        self.token_url = token_url
        self.form = form

    def run(self) -> None:  # pragma: no cover - 依赖网络
        try:
            self.ok.emit(_sanitize_token_payload(http_form(self.token_url, self.form)))
        except Exception as exc:  # noqa: BLE001 - 统一成可读错误
            self.failed.emit(f"令牌交换失败：{exc}")


def _sanitize_token_payload(payload: dict) -> dict:
    """只保留 OAuth 标准字段（白名单），任何疑似账号密码的字段一律丢弃。"""
    keep = {"access_token", "refresh_token", "token_type", "expires_in", "scope"}
    dropped = [k for k in payload if k.lower() in _FORBIDDEN_KEYS]
    if dropped:
        logger.warning("授权返回了不该出现的凭据类字段，已丢弃：%s", dropped)
    return {k: payload[k] for k in keep if k in payload}


# ------------------------------------------------------------------ 会话管理


@dataclass
class AccountInfo:
    uid_masked: str = ""
    nickname: str = ""
    scopes: str = ""
    login_at: float = 0.0
    expires_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "uid_masked": self.uid_masked,
            "nickname": self.nickname or "库街区漂泊者",
            "scopes": self.scopes,
            "login_at": self.login_at,
            "expires_at": self.expires_at,
        }


class AuthManager(QObject):
    state_changed = Signal(bool)          # 是否已登录
    login_url_ready = Signal(str)         # 需要在浏览器打开的官方授权地址
    login_failed = Signal(str)
    login_succeeded = Signal(object)      # AccountInfo
    needs_configuration = Signal(str)

    def __init__(self, api: ApiConfig | None = None, store: SecureStore | None = None,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.api = api or ApiConfig.load()
        self.store = store or SecureStore()
        self._listener: LoopbackListener | None = None
        self._worker: _TokenWorker | None = None
        self._verifier = ""
        self._state = ""
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)

    # --- 状态 ---
    @property
    def configured(self) -> bool:
        return self.api.is_configured()

    def account(self) -> AccountInfo | None:
        data = self.store.read()
        if not data.get("access_token"):
            return None
        return AccountInfo(
            uid_masked=data.get("uid_masked", ""),
            nickname=data.get("nickname", ""),
            scopes=data.get("scope", ""),
            login_at=data.get("login_at", 0.0),
            expires_at=data.get("expires_at", 0.0),
        )

    def logged_in(self) -> bool:
        return self.account() is not None

    def access_token(self) -> str | None:
        data = self.store.read()
        token = data.get("access_token")
        if not token:
            return None
        exp = float(data.get("expires_at") or 0)
        if exp and time.time() > exp - 60:
            self.refresh()
            data = self.store.read()
            token = data.get("access_token")
        return token

    # --- 登录 ---
    def begin_login(self) -> str | None:
        if not self.configured:
            self.needs_configuration.emit(
                "库街区当前没有公开的第三方 OAuth 端点配置。可在「设置 · 账号接口」中"
                "填写官方开放平台提供的授权/token/标记端点后重试；或直接使用无账号模式 / 导入导出文件。"
            )
            return None
        self._verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(self._verifier.encode("ascii")).digest())
        self._state = _b64url(secrets.token_bytes(16))
        params = {
            "response_type": "code",
            "client_id": self.api.client_id,
            "redirect_uri": self.api.redirect_uri,
            "scope": " ".join(self.api.scopes),
            "state": self._state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        url = self.api.authorize_url + ("&" if "?" in self.api.authorize_url else "?") + urllib.parse.urlencode(params)
        self._start_listener()
        if not self._listener or not self._listener.listening:
            return None
        self.login_url_ready.emit(url)
        self._timeout.start(CALLBACK_TIMEOUT_MS)
        return url

    def _start_listener(self) -> None:
        self._close_listener()
        self._listener = LoopbackListener(self.api.redirect_path, self)
        self._listener.code_received.connect(self._on_code)
        self._listener.failed.connect(self._on_listener_failed)
        if not self._listener.listen(self.api.redirect_port, self.api.redirect_host):
            self._close_listener()

    def open_browser(self, url: str) -> bool:
        try:
            return webbrowser.open(url)
        except Exception:
            return False

    def _on_code(self, code: str, state: str) -> None:
        self._timeout.stop()
        self._close_listener()
        if state != self._state or not code:
            self.login_failed.emit("授权回调校验失败（state 不匹配），已忽略本次回调。")
            return
        self._state = ""
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.api.redirect_uri,
            "client_id": self.api.client_id,
            "code_verifier": self._verifier,
        }
        self._exchange(form)

    def _on_listener_failed(self, msg: str) -> None:
        self._timeout.stop()
        self._close_listener()
        self.login_failed.emit(msg)

    def _on_timeout(self) -> None:
        self._close_listener()
        self.login_failed.emit("等待官方授权回调超时，本次登录未完成。")

    def _close_listener(self) -> None:
        if self._listener:
            self._listener.close()
            self._listener.deleteLater()
            self._listener = None

    def _exchange(self, form: dict[str, str]) -> None:
        if self._worker and self._worker.isRunning():
            return
        self._worker = _TokenWorker(self.api.token_url, form, self)
        self._worker.ok.connect(self._on_token)
        self._worker.failed.connect(lambda m: self.login_failed.emit(m))
        self._worker.start()

    def _on_token(self, payload: dict) -> None:
        token = payload.get("access_token")
        if not token:
            self.login_failed.emit("官方未返回 access_token，登录未完成。")
            return
        now = time.time()
        data = {
            "access_token": token,
            "refresh_token": payload.get("refresh_token", ""),
            "token_type": payload.get("token_type", "Bearer"),
            "scope": payload.get("scope", " ".join(self.api.scopes)),
            "login_at": now,
            "expires_at": now + float(payload.get("expires_in") or 3600),
            "uid_masked": self.store.read().get("uid_masked", ""),
            "nickname": self.store.read().get("nickname", ""),
        }
        self.store.write(data)
        self._verifier = ""
        info = self.account()
        self.state_changed.emit(True)
        self.login_succeeded.emit(info)

    # --- 刷新 / 退出 ---
    def refresh(self) -> None:
        data = self.store.read()
        rt = data.get("refresh_token")
        if not rt or not self.configured:
            return
        form = {
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": self.api.client_id,
        }
        self._exchange(form)

    def set_profile(self, uid: str = "", nickname: str = "") -> None:
        """由 userinfo 端点返回后调用；只保存脱敏 uid 与昵称。"""
        data = self.store.read()
        if not data:
            return
        data["uid_masked"] = _mask_uid(uid)
        data["nickname"] = nickname or data.get("nickname", "")
        self.store.write(data)
        self.state_changed.emit(True)

    def logout(self) -> None:
        self._close_listener()
        self._timeout.stop()
        self.store.clear()  # 本地凭证立即清除
        self.state_changed.emit(False)


def build_provider(api: ApiConfig, auth: AuthManager):
    """按当前账号状态挑数据源：已登录且端点齐 → 官方接口；否则本地。"""
    from .provider import KuroApiProvider, LocalDataProvider

    if api.cloud_configured() and auth.logged_in():
        return KuroApiProvider(api, auth.access_token)
    return LocalDataProvider()
