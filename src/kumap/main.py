"""程序入口。

用法：
    python -m kumap.main                 # 开发运行
    库街区探索助手.exe                    # PyInstaller 打包后
    --data-dir <目录>  便携模式：把配置与进度放在指定目录
    --no-tray          不常驻系统托盘
    --map-only         只打开内置官方浏览器地图（主窗口隐藏）
    --guide            探索向导：只开官方地图，自动规划顺路路线并逐步指示
    --reset-view       复位窗口尺寸与位置
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _bootstrap_path() -> None:
    src = Path(__file__).resolve().parent.parent
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="kumaph", description="库街区探索助手 · 鸣潮大地图探索辅助")
    ap.add_argument("--data-dir", help="自定义可写数据目录（便携模式）")
    ap.add_argument("--no-tray", action="store_true", help="不最小化到系统托盘")
    ap.add_argument("--reset-view", action="store_true", help="复位窗口尺寸与位置")
    ap.add_argument("--compact", action="store_true", help="以紧凑悬浮小窗启动")
    ap.add_argument("--map-only", "--official-map", dest="map_only", action="store_true",
                    help="只打开内置官方浏览器地图（主窗口隐藏，识图/路线在官方窗口操作）")
    ap.add_argument("--local-map", action="store_true",
                    help="启动时显示本地主窗口，不进入官方浏览器主界面")
    ap.add_argument("--guide", action="store_true",
                    help="探索向导：只开官方地图，按当前位置自动规划顺路路线并逐步指示")
    ap.add_argument("--version", action="store_true", help="打印版本后退出")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _bootstrap_path()
    args = parse_args(argv)
    if args.data_dir:
        import os

        os.environ["KUMAPH_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())

    from PySide6.QtCore import QLockFile, Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from . import COMPLIANCE_STATEMENT, __app_name__, __version__
    from .core import paths

    if args.version:
        print(f"{__app_name__} v{__version__}")
        return 0

    log = logging.getLogger("kumaph")
    log.setLevel(logging.INFO)
    try:
        handler = logging.FileHandler(paths.log_path(), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        log.addHandler(handler)
    except OSError:
        pass

    if not QApplication.instance():
        QApplication.setApplicationName("KuroMapHelper")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("KuroMapHelper")
    app.setApplicationDisplayName(__app_name__)
    app.setOrganizationName("KuroMapHelper")
    app.setStyle("Fusion")

    # 单实例：避免两个托盘图标互相覆盖进度文件
    lock = QLockFile(str(paths.data_root() / "app.lock"))
    if not lock.tryLock(1200):
        QMessageBox.warning(None, __app_name__, "本工具已经在运行（请看系统托盘图标）。")
        return 0

    from .core.config import Config
    from .ui.main_window import MainWindow

    cfg = Config()
    if args.reset_view:
        cfg.set("ui", "window_geometry", None, save=False)
        cfg.set("ui", "compact", False, save=False)
        cfg.save()
    if args.compact:
        cfg.set("ui", "compact", True)

    def _excepthook(etype, value, tb) -> None:
        logging.getLogger("kumaph").error("未捕获异常", exc_info=(etype, value, tb))
        if getattr(sys, "stderr", None):
            sys.__excepthook__(etype, value, tb)

    sys.excepthook = _excepthook

    window = MainWindow(cfg)
    if args.no_tray:
        window.tray.disable()
        window.cfg.set("ui", "close_to_tray", False)

    need_terms = not bool(cfg.ui.get("terms_accepted", False))
    guide = bool(args.guide)
    if args.local_map:
        cfg.set("ui", "map_only", False)
    elif args.map_only:
        cfg.set("ui", "map_only", True)
    map_only = bool(args.map_only or guide or cfg.ui.get("map_only", True))
    if need_terms or not map_only:
        window.show()

    # 首次启动：展示合规声明与免责说明，确认后才继续
    if need_terms:
        box = QMessageBox(window)
        box.setWindowTitle(f"{__app_name__} v{__version__} · 使用前必读")
        box.setTextFormat(Qt.RichText)
        box.setText(
            f"<h3>{__app_name__} v{__version__}</h3>"
            f"<p>{COMPLIANCE_STATEMENT}</p>"
            "<p><b>数据口径：</b>内置点位 / 路线为<b>示例数据</b>，用于演示功能；"
            "要获得与库街区大地图一致的点位与底图，请把官方或社区<b>公开导出</b>的数据包放入本机 import 目录"
            "（设置 · 数据与地图 可查看路径）。</p>"
            "<p><b>账号：</b>绑定库街区账号全程走官方授权页面，本工具不经手账号密码；"
            "不登录也能使用全部本地功能。</p>"
            "<p>点击「同意并继续」表示你已了解：本工具仅为探索参考辅助，不保证识图结果完全准确。</p>")
        accept_btn = box.addButton("同意并继续", QMessageBox.AcceptRole)
        box.addButton("退出", QMessageBox.RejectRole)
        box.exec()
        # 自定义按钮时 exec() 的返回值是实现细节（PySide6 实测为 2），
        # 不能拿去和 AcceptRole 比较，否则点「同意」也会被当成拒绝而直接退出。
        # 以 clickedButton() 的身份为准；点窗口关闭按钮时 clickedButton() 为 None。
        if box.clickedButton() is not accept_btn:
            lock.unlock()
            return 0
        cfg.set("ui", "terms_accepted", True)

    if guide:
        window.start_guide_ui()
    elif map_only:
        window.enter_map_only()
    else:
        window.show()

    code = app.exec()
    window.shutdown()
    lock.unlock()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
