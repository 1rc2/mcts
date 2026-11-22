# -*- coding: utf-8 -*-
"""首启合规确认流程。

回归点：QMessageBox.exec() 在自定义按钮下返回的是内部值（PySide6 实测为 2），
不能与 AcceptRole（0）比较。曾经的 bug：点「同意并继续」被当成拒绝，
main() 直接 return，表现为窗口一闪就退出。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from kumap import main as main_mod
from kumap.core.config import Config


def _click_button(box: QMessageBox, text: str) -> None:
    for btn in box.buttons():
        if btn.text() == text:
            btn.click()
            return


def test_consent_accept_starts_event_loop(tmp_path, monkeypatch, qt_app):
    monkeypatch.setenv("KUMAPH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)   # main() 会覆盖，结束后还原

    original_exec = QMessageBox.exec

    def auto_accept(self):
        _click_button(self, "同意并继续")
        return 2          # 模拟 PySide6 对自定义按钮的返回值，故意不等于 AcceptRole

    monkeypatch.setattr(QMessageBox, "exec", auto_accept)

    ran: list[bool] = []
    QTimer.singleShot(600, lambda: (ran.append(True), qt_app.quit()))

    rc = main_mod.main([])

    assert rc == 0
    assert ran, "点「同意」后没有进入 app.exec()，说明同意分支被误判"
    assert Config().ui.get("terms_accepted") is True
