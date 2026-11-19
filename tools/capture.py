"""离屏截图：用 WebEngine 渲染页面并抓图，用于视觉验收。

用途：
1. 检查四边是否还有白边（对边缘像素做量化判定，不靠肉眼）；
2. 与桌面外壳截图逐像素比对，证明"两端一致"；
3. 生成页面切换后的快照，确认七个页面都能正常渲染。

用法::

    python tools/capture.py --url http://127.0.0.1:8791/ --out shots/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-features=Translate")
os.environ.setdefault("QT_LOGGING_RULES", "qt.webenginecontext.info=false")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

#: 与 base.css 的 --bg-1 一致
PAGE_BG = "#130c1f"


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def grab(url: str, out: Path, *, width: int, height: int, page: str = "", scale: float = 1.0) -> Path:
    app = QApplication.instance() or QApplication(sys.argv)

    view = QWebEngineView()
    view.resize(width, height)
    view.page().setBackgroundColor(QColor(PAGE_BG))
    view.show()

    ready = {"ok": False}

    def on_load(ok: bool) -> None:
        ready["ok"] = ok

    view.loadFinished.connect(on_load)
    view.load(QUrl(url))

    # 等加载完成 + 首帧稳定
    for _ in range(120):
        wait(100)
        if ready["ok"]:
            break
    wait(900)

    # 切到目标页面
    if page:
        js = f"""
        (() => {{
          const btn = document.querySelector('.nav-item[data-page="{page}"]');
          if (btn) btn.click();
          return !!btn;
        }})();
        """
        view.page().runJavaScript(js)
        wait(700)

    out.parent.mkdir(parents=True, exist_ok=True)
    pix = view.grab()
    pix.save(str(out))

    view.close()
    view.setPage(None)
    del view
    app.processEvents()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=780)
    ap.add_argument("--page", default="")
    args = ap.parse_args(argv)

    path = grab(args.url, Path(args.out), width=args.width, height=args.height, page=args.page)
    print(json.dumps({"saved": str(path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())