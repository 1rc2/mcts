"""pytest 公共配置：源码路径 + 整个会话只创建一个 Qt 应用实例。

Qt 不允许可靠地销毁后再创建 QCoreApplication，所以这里用 session 级 autouse
fixture 建一次 QApplication（offscreen 平台），各测试模块直接复用。
"""

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def qt_app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("KuroMapHelper-test")
    yield app
