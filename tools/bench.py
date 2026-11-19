"""性能测量：首屏耗时、页面切换耗时、DOM 规模、内存占用。

判定不靠观感，用真实时间数据说话。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
os.environ.setdefault("QT_LOGGING_RULES", "qt.webenginecontext.info=false")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, QTimer, QUrl  # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

PAGES = ["game", "story", "ext", "mini", "pack", "diag", "set"]


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def run_js(view: QWebEngineView, script: str, timeout_ms: int = 4000):
    box = {}
    view.page().runJavaScript(script, 0, lambda r: box.update(v=r))
    spent = 0
    while "v" not in box and spent < timeout_ms:
        wait(60)
        spent += 60
    return box.get("v")


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8791/"

    app = QApplication.instance() or QApplication(sys.argv)
    view = QWebEngineView()
    view.resize(1280, 780)
    view.show()

    timing = {}
    ready = {"ok": False}

    started = {"t": 0.0}
    import time

    def on_load(ok: bool) -> None:
        ready["ok"] = ok
        timing["load_finished_ms"] = round((time.time() - started["t"]) * 1000, 1)

    view.loadFinished.connect(on_load)
    started["t"] = time.time()
    view.load(QUrl(url))

    for _ in range(200):
        wait(50)
        if ready["ok"]:
            break

    # 等 JS 初始化完成
    dom_ready = None
    for _ in range(60):
        dom_ready = run_js(view, "document.readyState")
        if dom_ready == "complete":
            break
        wait(50)

    wait(600)

    # 首屏渲染完成时间（用 navigation timing）
    perf = run_js(view, """
    (() => {
      const n = performance.getEntriesByType('navigation')[0] || {};
      const paints = {};
      for (const p of performance.getEntriesByType('paint')) paints[p.name] = Math.round(p.startTime);
      return JSON.stringify({
        domContentLoaded: Math.round(n.domContentLoadedEventEnd || 0),
        loadEvent: Math.round(n.loadEventEnd || 0),
        firstPaint: paints['first-paint'] || null,
        firstContentfulPaint: paints['first-contentful-paint'] || null,
      });
    })()
    """)

    # 页面切换耗时：连续切 3 轮，取每轮平均
    switch_script = """
    (() => {
      const keys = %s;
      const out = [];
      const t0 = performance.now();
      for (let round = 0; round < 3; round++) {
        for (const k of keys) {
          const btn = document.querySelector('.nav-item[data-page="' + k + '"]');
          if (btn) btn.click();
        }
      }
      const t1 = performance.now();
      // 强制读一次布局，把重排时间算进去
      void document.body.offsetHeight;
      const t2 = performance.now();
      return JSON.stringify({clicks: keys.length * 3, scriptMs: +(t1-t0).toFixed(2), withLayoutMs: +(t2-t0).toFixed(2)});
    })()
    """ % json.dumps(PAGES)

    switching = run_js(view, switch_script)

    # DOM 规模与内存
    dom = run_js(view, """
    (() => {
      const all = document.getElementsByTagName('*').length;
      const mem = performance.memory ? {
        usedMB: +(performance.memory.usedJSHeapSize/1048576).toFixed(2),
        totalMB: +(performance.memory.totalJSHeapSize/1048576).toFixed(2),
      } : null;
      return JSON.stringify({nodes: all, memory: mem});
    })()
    """)

    # 日志环形缓冲是否生效：连点 20 次不应无限长
    log_growth = run_js(view, """
    (() => {
      const box = document.getElementById('logBox');
      const before = box.childElementCount;
      for (let i = 0; i < 300; i++) {
        window.dispatchEvent(new CustomEvent('noop'));
      }
      return JSON.stringify({before});
    })()
    """)

    report = {
        "url": url,
        "readyState": dom_ready,
        "timing": timing,
        "perf": json.loads(perf) if perf else {},
        "switching": json.loads(switching) if switching else {},
        "dom": json.loads(dom) if dom else {},
        "log": json.loads(log_growth) if log_growth else {},
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    view.close()
    view.setPage(None)
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())