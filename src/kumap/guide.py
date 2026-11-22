"""探索向导独立入口。

用法：
    set PYTHONPATH=src
    python -m kumap.guide
    python -m kumap.guide --reset-view

等价于 `python -m kumap.main --guide`：只打开官方浏览器地图，开启识图位置同步，
按当前位置规划顺路路线，并在官方窗口右侧逐步指示。
"""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    from .main import main as app_main

    args = list(sys.argv[1:] if argv is None else argv)
    if "--guide" not in args:
        args.insert(0, "--guide")
    return app_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
