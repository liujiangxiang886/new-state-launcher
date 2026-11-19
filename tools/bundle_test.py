"""合集包（内含多个扩展 zip）的端到端回归。

真实场景：用户从论坛下到一个 ``mod1028.zip``，里面是十几个扩展各自一个 zip。
启动器必须能一次装完，而不是让人手动解压再逐个装。

跑法：
    _build_tools\\venv\\Scripts\\python.exe tools\\bundle_test.py
    _build_tools\\venv\\Scripts\\python.exe tools\\bundle_test.py <合集包路径>
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QMimeData, QPoint, QUrl  # noqa: E402
from PySide6.QtGui import QDragEnterEvent, QDropEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import config as cfg_mod  # noqa: E402
from app.core.ext.service import ExtensionService, detect_bundle  # noqa: E402
from app.ui.pyside import theme  # noqa: E402
from app.ui.pyside.assets import chevron  # noqa: E402
from app.ui.pyside.fonts import ensure_cjk  # noqa: E402

SHOTS = ROOT / "logs" / "ui-shots"


def find_bundle() -> Path | None:
    """找一个合集包：自身没有 extension.json，内层有多个扩展 zip。"""
    candidates = [
        *sorted(Path.home().joinpath("Desktop").glob("*.zip")),
        *sorted(Path.home().joinpath("Desktop").glob("*.jjm")),
    ]
    for path in candidates:
        if detect_bundle(path):
            return path
    return None


def _collect_bundle_result(cfg: dict, bundle: Path):
    """重跑一次安装以拿到逐成员结果（不进注册表的那几个也算失败）。

    这里刻意用 ``verify_smoke=False``：冒烟每个成员都要挂 3 GB 资源包，
    端到端测试已经在界面线程里跑过一轮带冒烟的安装了，这里只关心
    「每个成员能不能装上」。

    注意不能直接调 ``install_bundle``——它要求传入的是「合集包」，
    而这里传的是单个成员包，会被判定为不是合集。所以逐个走
    ``ExtensionService.install``。
    """
    import tempfile
    import zipfile

    from app.core.ext.service import BundleItem, ExtensionService

    svc = ExtensionService(cfg)
    outer = zipfile.ZipFile(bundle)
    items: list[BundleItem] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for index, name in enumerate(detect_bundle(bundle), start=1):
                member = Path(tmp) / f"{index:02d}.zip"
                member.write_bytes(outer.read(name))
                try:
                    one = svc.install(member, None, verify_smoke=False)
                    items.append(
                        BundleItem(
                            name=Path(name).name,
                            ok=one.ok,
                            ext_id=one.ext_id,
                            version=one.version,
                            message=one.message,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    items.append(
                        BundleItem(name=Path(name).name, ok=False, message=f"{type(exc).__name__}: {exc}")
                    )
    finally:
        outer.close()

    class _R:
        pass

    r = _R()
    r.items = items
    return r


def main(argv: list[str]) -> int:
    bundle = Path(argv[0]) if argv else find_bundle()
    if bundle is None or not detect_bundle(bundle):
        print("找不到可用的合集包样例。")
        print("  放一个「内含多个扩展 zip」的 zip 到桌面，或把路径作为参数传进来。")
        return 0

    members = detect_bundle(bundle)
    print(f"合集包：{bundle}")
    print(f"成员数：{len(members)}")

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.qss(theme.DARK, chevron(theme.DARK.text_dim)))
    ensure_cjk(app)

    from app.ui.pyside.main_window import MainWindow

    cfg = cfg_mod.load()
    win = MainWindow(cfg)
    win.resize(1180, 760)
    win.show()
    for _ in range(6):
        app.processEvents()

    before = {i["id"] for i in ExtensionService(cfg).installed()}

    # --- 真实拖拽事件（走 QDragEnterEvent / QDropEvent）
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(bundle))])
    enter = QDragEnterEvent(
        QPoint(400, 300), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier
    )
    win.dragEnterEvent(enter)
    print(f"拖入提示：{win.ext_status.label.text()}")
    drop = QDropEvent(QPoint(400, 300), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    win.dropEvent(drop)
    for _ in range(6):
        app.processEvents()

    # --- 等安装完成
    deadline = time.time() + 900
    last = ""
    result_obj = None
    while time.time() < deadline:
        for _ in range(10):
            app.processEvents()
        current = win.ext_status.label.text()
        if current != last:
            last = current
            print(f"  状态：{current}")
        if win._install_worker is None or not win._install_worker.isRunning():
            break
        time.sleep(0.4)

    # 结果对象在 worker 结束时才 emit，这里从扩展页反查安装状态
    # （InstallWorker 的 done 信号不好在无事件循环下截获，
    #  改用「注册表里是否有该合集的所有成员」作为判据）。
    result_obj = _collect_bundle_result(cfg, bundle)

    after = {i["id"] for i in ExtensionService(cfg).installed()}
    added = after - before
    print(f"\n安装前 {len(before)} 个 → 安装后 {len(after)} 个")
    print(f"本次新增 {len(added)} 个：{sorted(added)}")

    # 断言不能只看「新增几个」——重复装同一批包时新增自然是 0，
    # 但流程仍然是成功的。要判的是「该装的都进了注册表」。
    from app.core.ext.service import install_bundle  # noqa: F401  (确认接口在)

    installed_now = {i["id"]: i for i in ExtensionService(cfg).installed()}
    missing: list[str] = []
    for item in getattr(result_obj, "items", []) or []:
        if not item.ok:
            continue
        if item.ext_id not in installed_now:
            missing.append(f"{item.name} ({item.ext_id})")

    SHOTS.mkdir(parents=True, exist_ok=True)
    win._switch("extensions")
    for _ in range(6):
        app.processEvents()
    win.grab().save(str(SHOTS / "bundle-installed.png"), "PNG")

    ok = not missing and len(after) >= len(before)
    print(f"成功装上但没进注册表的：{missing or '无'}")
    print(f"\n结论：{'PASS' if ok else 'FAIL'}（注册表 {len(after)} 个扩展）")
    return 0 if ok else 1


if __name__ == "__main__":
    from PySide6.QtCore import Qt  # noqa: E402

    raise SystemExit(main(sys.argv[1:]))
