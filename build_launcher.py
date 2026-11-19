"""PyInstaller 打包专用入口。

以脚本方式调用 ``app.main.main``，避免把 ``app/main.py`` 直接当脚本时包内
相对导入（``from . import cli``）因 ``__name__ == "__main__"`` 而失败的问题。
源码运行仍用 ``python -m app.main``。
"""

from app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
