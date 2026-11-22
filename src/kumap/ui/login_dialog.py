"""库街区官方授权登录对话框。

本对话框没有任何账号 / 密码 / 验证码输入项——所有登录动作都发生在
库街区官方页面，本工具只负责：生成 PKCE 参数 → 打开官方页 → 接收一次性回调。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QPushButton, QTextBrowser, QVBoxLayout, QWidget)

from ..core.auth import AuthManager
from ..core.provider import ApiConfig


class LoginDialog(QDialog):
    def __init__(self, auth: AuthManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.auth = auth
        self.setWindowTitle("绑定库街区账号")
        self.resize(560, 460)
        self._deadline = 0

        lay = QVBoxLayout(self)
        browser = QTextBrowser(self)
        browser.setOpenExternalLinks(False)
        browser.setHtml(self._explain_html(auth.api))
        lay.addWidget(browser, 1)

        self.status = QLabel("尚未开始授权。", self)
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        row = QHBoxLayout()
        self.btn_start = QPushButton("打开官方授权页面", self)
        self.btn_start.clicked.connect(self._start)
        self.btn_local = QDialogButtonBox(QDialogButtonBox.Close, self)
        self.btn_local.rejected.connect(self.reject)
        row.addWidget(self.btn_start)
        row.addStretch(1)
        row.addWidget(self.btn_local)
        lay.addLayout(row)

        auth.login_url_ready.connect(self._on_url)
        auth.login_succeeded.connect(self._on_ok)
        auth.login_failed.connect(self._on_fail)
        auth.needs_configuration.connect(lambda m: self._on_fail(m))

    def _explain_html(self, api: ApiConfig) -> str:
        configured = "已配置" if api.is_configured() else "未配置"
        return f"""
<h3>如何绑定你本人的库街区账号</h3>
<ol>
<li>点击下方「打开官方授权页面」，会在系统浏览器中打开<b>库街区官方</b>授权页。</li>
<li>手机号验证码登录 / 扫码登录<b>全部在官方页面完成</b>。本工具不显示任何账号密码输入框，
不接收、不记录、也无法读取你的凭据。</li>
<li>你在官方页面点「同意授权」后，官方页面会把一次性授权码回传给
<code>127.0.0.1:{api.redirect_port}</code>（仅本机监听数秒，校验 state 后立即关闭）。</li>
<li>本工具用授权码 + PKCE 校验串换取访问令牌，令牌用 Windows DPAPI 加密后<b>只存在你这台机器</b>，
不经过任何第三方服务器；退出登录会立即删除。</li>
<li>授权范围仅 <code>{" ".join(api.scopes)}</code>：读取你在库街区大地图上的标记进度，
以及把你在本工具里的标记回传同步。</li>
</ol>
<p><b>不绑定账号也能用：</b>地图查看、图层筛选、路线导航、本地标记、私人点位全部照常工作，
进度只保存在本机。</p>
<p>当前官方接口配置状态：<b>{configured}</b>。未配置时请点击「打开官方授权页面」查看具体说明。</p>
"""

    def _start(self) -> None:
        self.status.setText("正在准备授权参数…")
        url = self.auth.begin_login()
        if url:
            self.btn_start.setEnabled(False)
            self._deadline = 180
            self._tick.start()

    def _on_url(self, url: str) -> None:
        opened = self.auth.open_browser(url)
        self.status.setText(
            "已在浏览器打开官方授权页，请在官方页面完成登录 / 扫码并同意授权…"
            if opened else "自动打开浏览器失败，请手动复制地址：\n" + url)

    def _on_ok(self, info) -> None:
        self._tick.stop()
        self.status.setText(f"授权完成：{getattr(info, 'nickname', '') or '库街区漂泊者'}")
        QTimer.singleShot(700, self.accept)

    def _on_fail(self, msg: str) -> None:
        self._tick.stop()
        self.btn_start.setEnabled(True)
        self.status.setText(msg)

    @property
    def _tick(self):
        t = getattr(self, "_timer", None)
        if t is None:
            t = QTimer(self)
            t.setInterval(1000)
            t.timeout.connect(self._on_tick)
            self._timer = t
        return t

    def _on_tick(self) -> None:
        self._deadline -= 1
        if self._deadline <= 0:
            self._tick.stop()
